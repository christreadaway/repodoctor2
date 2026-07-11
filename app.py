"""
RepoDoctor — AI-Powered Repository Management Tool
Main Flask application.

Simplified mode: repo overview with branch counts + required file checks.
"""

import datetime
import logging
import os
import secrets

import requests

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _CENTRAL_TZ = ZoneInfo("America/Chicago")
except Exception:
    _CENTRAL_TZ = None

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, Response,
)

import security
import github_client as gh
import ai_analyzer as ai
import anthropic
import models
import spec_cleaner
import project_mapper
import firestore_detector
import tracker_data
import tracker_generator
import briefing

def _load_or_create_secret_key() -> str:
    """Stable Flask secret key. A random per-process key would invalidate
    every session cookie (and drop pending flash messages) on each restart,
    so persist one under ~/.repodoctor on first run."""
    env_key = os.environ.get("FLASK_SECRET_KEY")
    if env_key:
        return env_key
    key_path = os.path.join(os.path.expanduser("~"), ".repodoctor", "secret_key")
    try:
        with open(key_path, "r") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
    except OSError as e:
        # Fall back to a per-process key; sessions won't survive restart.
        logger.warning(
            "Could not persist Flask secret key to %s: %s — logins will not survive restarts",
            key_path, e,
        )
    return key


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()

# Anti-CSRF token for the destructive credential-reset endpoints. /login/reset
# is deliberately reachable without auth (recovery from a revoked PAT), which
# means without this check ANY web page the user visits could cross-origin
# POST it and silently wipe the stored credentials. Per-boot is enough: the
# token is rendered into our own forms and unknowable to another origin.
_RESET_TOKEN = secrets.token_hex(16)


@app.context_processor
def inject_reset_token():
    return {"reset_token": _RESET_TOKEN}


def _reset_token_ok() -> bool:
    # Compare as bytes: compare_digest on str raises TypeError for
    # non-ASCII input, which would turn a bad token into a 500.
    supplied = request.form.get("reset_token", "").encode("utf-8", errors="replace")
    return secrets.compare_digest(supplied, _RESET_TOKEN.encode("utf-8"))


def _reject_bad_reset_token(page_name: str, target: str):
    """The one guard every credential-destroying endpoint calls. Returns a
    redirect response when the token is missing/wrong, else None."""
    if _reset_token_ok():
        return None
    logger.warning("Rejected credential reset without a valid token (path=%s)", request.path)
    flash(f"Invalid reset request. Use the button on the {page_name} page.", "error")
    return redirect(url_for(target))


@app.template_filter("central_time")
def _central_time(iso_ts):
    """Format an ISO 8601 UTC timestamp as US Central Time (Chicago).

    Returns 'May 19, 2026 2:47 PM CDT'. Empty string on bad input.
    Uses the tz abbreviation (CST/CDT) so DST is unambiguous.
    """
    if not iso_ts:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    if _CENTRAL_TZ is not None:
        dt = dt.astimezone(_CENTRAL_TZ)
    hour = dt.strftime("%I").lstrip("0") or "12"
    tz_abbrev = dt.tzname() or "CT"
    return f"{dt.strftime('%b %d, %Y')} {hour}:{dt.strftime('%M %p')} {tz_abbrev}"


# In-memory session state
_github_client: gh.GitHubClient | None = None
_credentials: dict | None = None
_session_cost = models.SessionCost()
_scan_results: dict | None = models.get_latest_scan()  # Latest scan, restored on restart


GITHUB_AUTH_REMEDY = (
    "GitHub authentication failed — your Personal Access Token is no longer "
    "valid (revoked, expired, or its 'repo' scope was removed). To fix this: "
    "(1) Go to GitHub → Settings → Developer settings → Personal access "
    "tokens, (2) Generate a new token with the 'repo' scope, (3) Click "
    "Logout below, (4) On the login page click \"Reset Credentials\" and "
    "enter your new token."
)


@app.errorhandler(gh.GitHubAuthError)
def _handle_github_auth_error(e):
    """Any GitHub 401 surfaces here: flash the remedy and bounce to dashboard.

    Without this, routes that call get_branches / get_file_content / etc.
    would either silently render with empty data or crash with a 500. The
    handler is registered globally so each new route gets the right
    behavior for free.
    """
    flash(GITHUB_AUTH_REMEDY, "error")
    target = url_for("dashboard") if session.get("authenticated") else url_for("login")
    return redirect(target)


@app.context_processor
def inject_preferences():
    """Make preferences + the model list available in all templates
    (Settings dropdown, tracker per-generation override)."""
    return {
        "preferences": models.get_preferences(),
        "model_choices": ai.MODEL_CHOICES,
    }


def _require_auth(f):
    """Decorator: redirect to login if not authenticated."""
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def _require_auth_api(f):
    """Decorator for JSON endpoints: 401 + JSON error instead of a login
    redirect, so client-side generation queues can show a clear message."""
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Session expired — reload the page and log in again."}), 401
        return f(*args, **kwargs)
    return wrapper


NOT_CONNECTED_REMEDY = (
    "Not connected to GitHub — the app restarted since you logged in. "
    "Log out and back in, then retry."
)


def _find_repo_by_name(name: str) -> dict | None:
    """Find a repo in the most recent scan by name alone (repo names are
    unique within the scanned account)."""
    if not _scan_results:
        return None
    for r in _scan_results.get("repos", []):
        if r["name"] == name:
            return r
    return None


def _get_github_client() -> gh.GitHubClient | None:
    global _github_client
    return _github_client


def _get_credentials() -> dict | None:
    global _credentials
    return _credentials


# --- Auth Routes ---

@app.route("/login", methods=["GET", "POST"])
def login():
    has_creds = security.credentials_exist()
    if request.method == "POST":
        password = request.form.get("password", "")
        if has_creds:
            creds = security.decrypt_credentials(password)
            if creds is None:
                flash("Wrong password. Try again.", "error")
                return render_template("login.html", has_credentials=True)

            # Verify the decrypted PAT still works with GitHub BEFORE
            # marking the user authenticated. Without this check, an
            # invalid/revoked PAT silently lands the user on an empty
            # dashboard with no indication of what went wrong.
            test_client = gh.GitHubClient(creds["github_pat"])
            try:
                user_info = test_client.verify_token()
            except requests.RequestException:
                flash("Could not reach GitHub — check your internet connection and try again.", "error")
                return render_template("login.html", has_credentials=True)
            if user_info is None:
                flash(
                    "Your saved GitHub Personal Access Token is no longer valid "
                    "(likely revoked, expired, or its scopes changed). "
                    "Click \"Reset Credentials\" below, then re-enter a fresh "
                    "token from GitHub → Settings → Developer settings → "
                    "Personal access tokens (needs 'repo' scope).",
                    "error",
                )
                return render_template("login.html", has_credentials=True, pat_invalid=True)

            scopes = user_info.get("_scopes", "")
            if "repo" not in scopes:
                flash(
                    f"Your saved GitHub token is missing the 'repo' scope "
                    f"(current scopes: {scopes or '(none)'}). "
                    "Click \"Reset Credentials\" below and re-enter a token "
                    "that has the 'repo' scope checked.",
                    "error",
                )
                return render_template("login.html", has_credentials=True, pat_invalid=True)

            _init_session(creds, user_info)
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        else:
            github_pat = request.form.get("github_pat", "").strip()
            anthropic_key = request.form.get("anthropic_key", "").strip()
            if not all([password, github_pat, anthropic_key]):
                flash("All fields are required.", "error")
                return render_template("login.html", has_credentials=False)
            # This password is the only thing protecting an offline copy of
            # credentials.enc (which holds a repo-scoped PAT) from brute
            # force — 4 characters was trivially crackable.
            if len(password) < 8:
                flash("Password must be at least 8 characters.", "error")
                return render_template("login.html", has_credentials=False)

            # Verify GitHub PAT
            test_client = gh.GitHubClient(github_pat)
            try:
                user_info = test_client.verify_token()
            except requests.RequestException:
                flash("Could not reach GitHub — check your internet connection and try again.", "error")
                return render_template("login.html", has_credentials=False)
            if user_info is None:
                flash("Invalid GitHub PAT. Check your token and try again.", "error")
                return render_template("login.html", has_credentials=False)

            scopes = user_info.get("_scopes", "")
            if "repo" not in scopes:
                flash(f"GitHub PAT needs 'repo' scope. Current scopes: {scopes}", "error")
                return render_template("login.html", has_credentials=False)

            security.encrypt_credentials(password, github_pat, anthropic_key)
            creds = {"github_pat": github_pat, "anthropic_key": anthropic_key}
            _init_session(creds)
            session["authenticated"] = True
            session["github_user"] = user_info.get("login", "")
            flash(f"Welcome, {user_info.get('login', '')}! Credentials saved.", "success")
            return redirect(url_for("dashboard"))

    return render_template("login.html", has_credentials=has_creds)


@app.route("/logout")
def logout():
    global _github_client, _credentials
    _github_client = None
    _credentials = None
    session.clear()
    return redirect(url_for("login"))


def _init_session(creds: dict, user_info: dict | None = None):
    global _github_client, _credentials
    _credentials = creds
    _github_client = gh.GitHubClient(creds["github_pat"])
    if user_info is None:
        user_info = _github_client.verify_token()
    if user_info:
        session["github_user"] = user_info.get("login", "")


@app.route("/login/reset", methods=["POST"])
def login_reset():
    """Delete saved encrypted credentials so the user can re-enter fresh ones.

    Reachable without auth so users locked out by a revoked/expired PAT can
    recover from the login screen without manually deleting files.
    """
    rejected = _reject_bad_reset_token("login", "login")
    if rejected:
        return rejected
    global _github_client, _credentials
    _github_client = None
    _credentials = None
    security.delete_credentials()
    session.clear()
    flash(
        "Stored credentials deleted. Enter your new GitHub PAT and Anthropic key below.",
        "success",
    )
    return redirect(url_for("login"))


# --- Dashboard ---

@app.route("/")
@_require_auth
def dashboard():
    global _scan_results
    prefs = models.get_preferences()
    return render_template(
        "dashboard.html",
        scan_results=_scan_results,
        preferences=prefs,
        session_cost=_session_cost.to_dict(),
    )


@app.route("/scan", methods=["POST"])
@_require_auth
def scan():
    global _scan_results, _stats_cache
    _stats_cache = None
    client = _get_github_client()
    if not client:
        flash("Not authenticated with GitHub.", "error")
        return redirect(url_for("dashboard"))

    prefs = models.get_preferences()
    excluded = set(prefs.get("excluded_repos", []))

    # NOTE: GitHubAuthError (401 anywhere in the scan) propagates to the
    # global handler, which flashes GITHUB_AUTH_REMEDY and redirects.
    repos = client.get_repos()
    results = []
    for repo in repos:
        if repo["full_name"] in excluded or repo["name"] in excluded:
            continue
        try:
            repo_data = gh.scan_repo_lite(client, repo)
            results.append(repo_data)
        except gh.GitHubAuthError:
            raise
        except Exception as e:
            results.append({
                "owner": repo["owner"]["login"],
                "name": repo["name"],
                "full_name": repo["full_name"],
                "default_branch": repo.get("default_branch", "main"),
                "private": repo.get("private", False),
                "html_url": repo.get("html_url", ""),
                "description": repo.get("description", ""),
                "created_at": repo.get("created_at", ""),
                "updated_at": repo.get("updated_at", ""),
                "pushed_at": repo.get("pushed_at", ""),
                # None = unknown, so the dashboard shows "—" instead of
                # falsely claiming the docs are stale for a failed repo.
                "docs_updated": None,
                "total_branch_count": 0,
                "non_default_branch_count": 0,
                "henry_branch_count": 0,
                "non_henry_branch_count": 0,
                "branch_names": [],
                "required_files": {},
                "files_present": 0,
                "files_total": 5,
                "code_size_bytes": 0,
                "languages": {},
                "error": str(e),
            })

    # Sort by non-henry branch count descending (henry branches are hidden
    # from the dashboard count, so they shouldn't drive sort order either).
    results.sort(key=lambda r: r.get("non_henry_branch_count", 0), reverse=True)

    _scan_results = {
        "repos": results,
        "total_repos": len(results),
        # "total_branches" is the cross-repo total displayed on the dashboard
        # — excludes henry branches to match the per-repo column.
        "total_branches": sum(r.get("non_henry_branch_count", 0) for r in results),
        "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    models.save_scan(_scan_results)
    models.log_action("scan", "all", "all", f"Scanned {len(results)} repos, {_scan_results['total_branches']} total branches")

    flash(f"Scan complete: {len(results)} repos, {_scan_results['total_branches']} total branches found.", "success")
    return redirect(url_for("dashboard"))


# --- Repo Detail ---

@app.route("/repo/<owner>/<name>")
@_require_auth
def repo_detail(owner, name):
    client = _get_github_client()
    if not client:
        flash("Not authenticated with GitHub.", "error")
        return redirect(url_for("dashboard"))

    # Find repo in scan results for basic info
    repo_info = None
    if _scan_results:
        for r in _scan_results.get("repos", []):
            if r["owner"] == owner and r["name"] == name:
                repo_info = r
                break

    if not repo_info:
        flash("Repo not found. Try scanning first.", "error")
        return redirect(url_for("dashboard"))

    ref = repo_info.get("default_branch", "main")

    # Recursive spec-file search: prefers root, falls back to subfolders.
    _, actual_paths = client.check_required_files(owner, name, ref=ref)

    spec_files = {
        "PRODUCT_SPEC": None,
        "PROJECT_STATUS": None,
        "SESSION_NOTES": None,
    }
    spec_display_map = {
        "PRODUCT_SPEC": "PRODUCT_SPEC.md",
        "PROJECT_STATUS": "PROJECT_STATUS.md",
        "SESSION_NOTES": "SESSION_NOTES.md",
    }

    raw_specs = {}
    for key, display_name in spec_display_map.items():
        path = actual_paths.get(display_name)
        if path:
            content = client.get_file_content(owner, name, path, ref=ref)
            if content:
                if len(content) > 10000:
                    content = content[:10000] + "\n\n... (truncated)"
                raw_specs[key] = content
                spec_files[key] = spec_cleaner.clean_markdown(content)

    # Pull conversations mapped to this repo
    conversations = project_mapper.get_conversations_for_repo(name)

    # Extract What's Next from raw specs + conversations
    whats_next = spec_cleaner.extract_whats_next(raw_specs, conversations)

    return render_template(
        "repo_detail.html",
        repo=repo_info,
        specs=spec_files,
        whats_next=whats_next,
        conversations=conversations,
    )


# --- Settings (kept active for excluded repos / credential reset) ---

@app.route("/settings", methods=["GET", "POST"])
@_require_auth
def settings():
    prefs = models.get_preferences()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "save_preferences":
            prefs["local_root"] = request.form.get("local_root", "~/claudesync2")
            requested_model = request.form.get("ai_model", ai.DEFAULT_MODEL)
            prefs["ai_model"] = (
                requested_model if requested_model in ai.VALID_MODELS else ai.DEFAULT_MODEL
            )
            prefs["display_mode"] = request.form.get("display_mode", "plain_english")
            excluded = request.form.get("excluded_repos", "")
            prefs["excluded_repos"] = [r.strip() for r in excluded.split(",") if r.strip()]
            models.save_preferences(prefs)
            flash("Preferences saved.", "success")

        elif action == "save_spec":
            spec_repo = request.form.get("spec_repo", "").strip()
            spec_content = request.form.get("spec_content", "").strip()
            # Accept "owner/repo" paste-ins by keeping the repo part.
            if "/" in spec_repo:
                spec_repo = spec_repo.rsplit("/", 1)[-1].strip()
            if spec_repo and spec_content:
                try:
                    models.save_spec(spec_repo, spec_content)
                    flash(f"Spec saved for {spec_repo}.", "success")
                except ValueError:
                    flash(
                        f'"{spec_repo}" is not a valid repo name — use letters, '
                        "numbers, dots, dashes, and underscores only.",
                        "error",
                    )

        elif action == "reset_credentials":
            rejected = _reject_bad_reset_token("Settings", "settings")
            if rejected:
                return rejected
            security.delete_credentials()
            flash("Credentials deleted. You will need to re-enter them.", "success")
            return redirect(url_for("logout"))

        return redirect(url_for("settings"))

    specs = models.list_specs()
    return render_template(
        "settings.html",
        preferences=prefs,
        specs=specs,
        session_cost=_session_cost.to_dict(),
    )


# --- API: Session Cost ---

@app.route("/api/session-cost")
@_require_auth
def session_cost():
    return jsonify(_session_cost.to_dict())


# --- API: Debug file detection for a repo ---

@app.route("/api/debug-files/<owner>/<name>")
@_require_auth
def debug_files(owner, name):
    """Show exactly what files the GitHub API returns for a repo root, and how they match."""
    client = _get_github_client()
    if not client:
        return jsonify({"error": "Not authenticated"}), 401

    # Find default branch from scan results or fall back to 'main'
    default_branch = "main"
    if _scan_results:
        for r in _scan_results.get("repos", []):
            if r["owner"] == owner and r["name"] == name:
                default_branch = r.get("default_branch", "main")
                break

    root_files = client.get_root_files(owner, name, ref=default_branch)
    all_paths = client.get_all_file_paths(owner, name, ref=default_branch)
    required_files, actual_names = client.check_required_files(owner, name, ref=default_branch)

    return jsonify({
        "repo": f"{owner}/{name}",
        "default_branch": default_branch,
        "root_files_from_api": root_files,
        "root_file_count": len(root_files),
        "total_file_count": len(all_paths),
        "required_files_result": required_files,
        "actual_names": actual_names,
        "files_present": sum(1 for v in required_files.values() if v),
        "files_total": len(required_files),
    })


# --- Projects Summary ---

def _resolve_active_group(groups: dict, persist: bool = True) -> str:
    """Shared group-filter resolution for Projects / What's Next / Briefing:
    ?group=X wins (and is persisted unless persist=False); otherwise the
    saved preference. A group that no longer exists resolves to '' (All)."""
    prefs = models.get_preferences()
    requested = request.args.get("group")
    if requested is not None:
        active_group = requested if requested in groups else ""
        if persist and prefs.get("active_group", "") != active_group:
            prefs["active_group"] = active_group
            models.save_preferences(prefs)
        return active_group
    active_group = prefs.get("active_group", "")
    if active_group and active_group not in groups:
        return ""
    return active_group


@app.route("/projects")
@_require_auth
def projects():
    summaries = models.get_project_summaries()
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)

    all_repos = list(_scan_results.get("repos", []) if _scan_results else [])
    # Most recently pushed first; missing/blank dates sink to the bottom.
    all_repos.sort(key=briefing.last_push_ts, reverse=True)
    if active_group:
        group_repos = set(groups.get(active_group, []))
        repos = [r for r in all_repos if r["name"] in group_repos]
    else:
        repos = all_repos

    # Repos that aren't a member of any group — surfaced in the Manage Groups
    # panel so Chris can see at a glance what hasn't been categorized yet.
    assigned = {name for member_list in groups.values() for name in member_list}
    unassigned_repos = [r for r in all_repos if r["name"] not in assigned]

    return render_template(
        "projects.html",
        repos=repos,
        all_repos=all_repos,
        unassigned_repos=unassigned_repos,
        summaries=summaries,
        scan_results=_scan_results,
        groups=groups,
        active_group=active_group,
    )


@app.route("/projects/groups/save", methods=["POST"])
@_require_auth
def save_group():
    name = request.form.get("group_name", "").strip()
    original = request.form.get("original_name", "").strip()
    repos = request.form.getlist("repos")

    if not name:
        flash("Group name is required.", "error")
        return redirect(url_for("projects"))

    if original and original != name:
        existing = models.get_groups()
        if name in existing:
            flash(f'A group named "{name}" already exists. Pick a different name.', "error")
            return redirect(url_for("projects"))
        if not models.rename_group(original, name):
            flash("Could not rename group.", "error")
            return redirect(url_for("projects"))

    models.set_group(name, repos)
    flash(f'Group "{name}" saved with {len(repos)} project(s).', "success")
    return redirect(url_for("projects", group=name))


@app.route("/projects/groups/delete", methods=["POST"])
@_require_auth
def delete_group_route():
    # Prefer original_name (hidden field) so an in-flight edit to the
    # editable name field can't redirect the delete at the wrong group.
    name = (request.form.get("original_name") or request.form.get("group_name") or "").strip()
    if models.delete_group(name):
        flash(f'Group "{name}" deleted.', "success")
    else:
        flash("Group not found.", "error")
    return redirect(url_for("projects"))


def _generate_summary_for_repo(client, creds: dict, repo: dict) -> dict:
    """Generate + save the Projects-page summary for one repo. Returns the
    saved summary. Raises on AI failure — callers decide how to surface it
    (an existing good summary is never overwritten with an error)."""
    owner = repo["owner"]
    name = repo["name"]
    ref = repo.get("default_branch", "main")

    # Spec docs via the shared doc-fetch path (recursive lookup, truncated).
    fetched = gh.fetch_repo_docs(client, owner, name, ref=ref, max_chars=5000)

    # Build context for AI
    context_parts = []
    if repo.get("description"):
        context_parts.append(f"GitHub description: {repo['description']}")
    for key, content in fetched["docs"].items():
        context_parts.append(f"--- {key.upper()} ---\n{content}")

    if not context_parts:
        # No specs or description — save a minimal summary
        summary = {
            "what_it_does": f"{name} — no spec files or description available.",
            "how_finished": "Unknown — no spec files found.",
            "next_steps": ["Add PRODUCT_SPEC.md with project description", "Add SESSION_NOTES.md with session tracking"],
        }
        models.save_project_summary(name, summary)
        return summary

    context_text = "\n\n".join(context_parts)

    # Honor the Settings model preference — briefs and trackers already do.
    model = models.get_ai_model()

    ai_client = anthropic.Anthropic(api_key=creds["anthropic_key"])
    response = ai_client.messages.create(
        model=model,
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": (
                f"Project: {name}\n\n{context_text}\n\n"
                "Based on the above, return ONLY valid JSON with:\n"
                '1. "what_it_does": 1-2 sentence description of what this project does\n'
                '2. "how_finished": 1-2 sentence assessment of how complete/finished the project is\n'
                '3. "next_steps": array of up to 5 short bullet strings for what needs to be built or tested next\n'
                "Return raw JSON only, no markdown fencing."
            ),
        }],
    )
    raw = next(
        (b.text for b in response.content if getattr(b, "type", "") == "text"), ""
    ).strip()
    summary = ai.extract_json_object(raw)
    # Ensure next_steps is capped at 5
    if "next_steps" in summary and len(summary["next_steps"]) > 5:
        summary["next_steps"] = summary["next_steps"][:5]
    models.save_project_summary(name, summary)

    in_tok = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    _session_cost.add(in_tok, out_tok, ai.estimate_cost(in_tok, out_tok, model=model))
    return summary


@app.route("/api/projects/summary/<name>", methods=["POST"])
@_require_auth_api
def generate_project_summary_one(name):
    """Generate the summary for ONE repo. The Projects page drives this
    sequentially per repo in view, with live progress — replacing the old
    whole-portfolio POST that blocked one request for minutes."""
    client = _get_github_client()
    creds = _get_credentials()
    if not client or not creds:
        return jsonify({"error": NOT_CONNECTED_REMEDY}), 401
    repo = _find_repo_by_name(name)
    if not repo:
        return jsonify({"error": f"Repo '{name}' not in the latest scan. Run a scan first."}), 404

    try:
        _generate_summary_for_repo(client, creds, repo)
    except gh.GitHubAuthError:
        return jsonify({"error": GITHUB_AUTH_REMEDY}), 401
    except Exception as e:
        models.log_action("generate_summary_error", name, "all", str(e)[:200])
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}"}), 502

    models.log_action("generate_summary", name, "all", "ok")
    return jsonify({"ok": True, "repo": name})


# --- Henry Branches ---
#
# Scan all repos for branches whose name contains "henry" (case-insensitive)
# and produce an AI summary of each branch vs. its repo's default branch.

HENRY_KEYWORD = "henry"


def _find_henry_branches(scan_results: dict | None) -> list[dict]:
    """Return [{owner, repo, branch_name, default_branch, private}] for every
    non-default branch whose name contains 'henry'."""
    found = []
    if not scan_results:
        return found
    for repo in scan_results.get("repos", []):
        default_branch = repo.get("default_branch", "main")
        for bname in repo.get("branch_names", []):
            if HENRY_KEYWORD in bname.lower() and bname != default_branch:
                found.append({
                    "owner": repo["owner"],
                    "repo": repo["name"],
                    "full_name": repo["full_name"],
                    "branch_name": bname,
                    "default_branch": default_branch,
                    "private": repo.get("private", False),
                    "html_url": repo.get("html_url", ""),
                })
    return found


@app.route("/henry")
@_require_auth
def henry():
    summaries = models.get_henry_summaries()
    branches = _find_henry_branches(_scan_results)
    # Attach summary (if cached) and sort: summarized first, then alphabetical.
    for b in branches:
        b["summary"] = summaries.get(f"{b['repo']}/{b['branch_name']}")
    branches.sort(key=lambda b: (b["summary"] is None, b["repo"].lower(), b["branch_name"].lower()))
    return render_template(
        "henry.html",
        branches=branches,
        scan_results=_scan_results,
        any_summaries=bool(summaries),
    )


@app.route("/henry/generate", methods=["POST"])
@_require_auth
def generate_henry_summaries():
    client = _get_github_client()
    creds = _get_credentials()
    if not client or not creds:
        flash("Not authenticated.", "error")
        return redirect(url_for("henry"))
    if not _scan_results:
        flash("Run a scan first from My Repos.", "error")
        return redirect(url_for("henry"))

    targets = _find_henry_branches(_scan_results)
    if not targets:
        flash('No branches with "henry" in the name were found in your latest scan.', "error")
        return redirect(url_for("henry"))

    generated = 0
    failed = 0
    # Honor the Settings model preference, like briefs and trackers do.
    model = models.get_ai_model()
    # Group by repo so we only fetch get_branches/get_pulls once per repo.
    by_repo: dict[tuple[str, str], list[dict]] = {}
    for t in targets:
        by_repo.setdefault((t["owner"], t["repo"]), []).append(t)

    for (owner, name), items in by_repo.items():
        default_branch = items[0]["default_branch"]
        try:
            branches_full = client.get_branches(owner, name)
        except gh.GitHubAuthError:
            raise
        except Exception as e:
            for t in items:
                models.save_henry_summary(name, t["branch_name"], _henry_error_record(t, str(e)))
                failed += 1
            continue
        sha_lookup = {b["name"]: b["commit"]["sha"] for b in branches_full}
        try:
            pulls = client.get_pulls(owner, name)
            pr_branches = {pr["head"]["ref"] for pr in pulls}
        except gh.GitHubAuthError:
            raise
        except Exception:
            pr_branches = set()

        spec_text = models.get_spec(name)

        for t in items:
            bname = t["branch_name"]
            # A transient network error on one branch must not 500 the whole
            # batch — record the failure and keep going, like every other
            # step in this loop.
            try:
                comparison = client.compare_branches(owner, name, default_branch, bname)
            except gh.GitHubAuthError:
                raise
            except Exception as e:
                models.save_henry_summary(name, bname, _henry_error_record(t, str(e)))
                failed += 1
                continue
            if comparison is None:
                models.save_henry_summary(name, bname, _henry_error_record(t, "Could not compare branch."))
                failed += 1
                continue

            # Shared with scan_repo — handles the compare API's 250-commit cap.
            last_commit_date, last_commit_author = client.branch_last_commit(
                owner, name, bname, comparison,
            )
            has_pr = bname in pr_branches
            classification = client.classify_branch(comparison, last_commit_date, has_pr)

            files_changed = [
                {
                    "filename": f["filename"],
                    "additions": f["additions"],
                    "deletions": f["deletions"],
                    "status": f["status"],
                }
                for f in comparison.get("files", [])
            ]
            commit_messages = []
            for c in comparison.get("commits", []):
                c_commit = c.get("commit", {})
                msg = (c_commit.get("message") or "").split("\n", 1)[0] or "(no message)"
                c_author = c_commit.get("author") or {}
                c_committer = c_commit.get("committer") or {}
                commit_messages.append({
                    "sha": (c.get("sha") or "")[:7],
                    "message": msg,
                    "author": c_author.get("name", "Unknown"),
                    "date": c_committer.get("date"),
                })

            branch_data = {
                "name": bname,
                "classification": classification,
                "ahead_by": comparison.get("ahead_by", 0),
                "behind_by": comparison.get("behind_by", 0),
                "last_commit_date": last_commit_date,
                "last_commit_author": last_commit_author,
                "has_pr": has_pr,
                "commit_sha": sha_lookup.get(bname, ""),
                "files_changed": files_changed,
                "commit_messages": commit_messages,
            }

            try:
                analysis = ai.analyze_branch(
                    api_key=creds["anthropic_key"],
                    repo_name=name,
                    branch_data=branch_data,
                    default_branch=default_branch,
                    spec_text=spec_text,
                    model=model,
                )
                usage = analysis.get("_usage", {})
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                cost = ai.estimate_cost(in_tok, out_tok, model=model)
                _session_cost.add(in_tok, out_tok, cost)

                models.save_henry_summary(name, bname, {
                    "owner": owner,
                    "repo": name,
                    "branch_name": bname,
                    "default_branch": default_branch,
                    "html_url": t["html_url"],
                    "private": t["private"],
                    "plain_english_summary": analysis.get("plain_english_summary", ""),
                    "screen_changes": analysis.get("screen_changes", []),
                    "feature_assessment": analysis.get("feature_assessment", "UNCLEAR"),
                    "risk_level": analysis.get("risk_level", "MEDIUM"),
                    "conflict_prediction": analysis.get("conflict_prediction", ""),
                    "merge_strategy": analysis.get("merge_strategy", "merge"),
                    "ahead_by": branch_data["ahead_by"],
                    "behind_by": branch_data["behind_by"],
                    "classification": classification,
                    "last_commit_date": last_commit_date,
                    "last_commit_author": last_commit_author,
                    "has_pr": has_pr,
                    "files_count": len(files_changed),
                    "commits_count": len(commit_messages),
                    "commit_sha": branch_data["commit_sha"],
                })
                generated += 1
            except Exception as e:
                models.save_henry_summary(name, bname, _henry_error_record(t, str(e)))
                failed += 1

    flash(f"Henry branches summarized: {generated} done, {failed} failed.", "success")
    models.log_action(
        "generate_henry_summaries",
        "all",
        "all",
        f"Generated {generated}, failed {failed}",
    )
    return redirect(url_for("henry"))


def _henry_error_record(target: dict, err: str) -> dict:
    return {
        "owner": target["owner"],
        "repo": target["repo"],
        "branch_name": target["branch_name"],
        "default_branch": target["default_branch"],
        "html_url": target.get("html_url", ""),
        "private": target.get("private", False),
        "plain_english_summary": f"Summary failed: {err[:200]}",
        "feature_assessment": "UNCLEAR",
        "risk_level": "MEDIUM",
        "error": err[:200],
    }


# --- Stats ---

# Cached stats data so the page doesn't re-hit the API on every visit.
# Keyed by scan identity (len + first repo full_name + total_branches).
_stats_cache: dict | None = None


def _stats_cache_key() -> str:
    if not _scan_results:
        return ""
    repos = _scan_results.get("repos", [])
    first = repos[0]["full_name"] if repos else ""
    return f"{len(repos)}|{first}|{_scan_results.get('total_branches', 0)}"


PERIOD_DAYS = {
    "1d": 1, "3d": 3, "1w": 7, "2w": 14, "1m": 30, "2m": 60,
}


def _collect_repo_activity(client, repo, since_iso, now_utc):
    """Fetch commit activity for one repo. Returns dict to merge."""
    import datetime as _dt
    owner = repo["owner"]
    name = repo["name"]
    ref = repo.get("default_branch", "main")

    commits_by_period = {k: 0 for k in PERIOD_DAYS}

    # Page through all commits in the longest stats window. The hard ceiling
    # (50 pages = 5000 commits) just bounds API calls for pathological repos;
    # it doesn't visibly cap the bar like the old 200 limit did.
    try:
        commits = client.get_commits_since(owner, name, since_iso, ref=ref, max_pages=50)
    except gh.GitHubAuthError:
        raise
    except Exception:
        commits = []

    for c in commits:
        date_str = (
            c.get("commit", {}).get("committer", {}).get("date")
            or c.get("commit", {}).get("author", {}).get("date")
        )
        if not date_str:
            continue
        try:
            dt = _dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        age_days = (now_utc - dt).total_seconds() / 86400.0
        for pkey, pdays in PERIOD_DAYS.items():
            if age_days <= pdays:
                commits_by_period[pkey] += 1

    return {
        "name": name,
        "owner": owner,
        "full_name": repo.get("full_name", f"{owner}/{name}"),
        "html_url": repo.get("html_url", ""),
        "commits_by_period": commits_by_period,
        "code_size_bytes": repo.get("code_size_bytes", 0),
    }


@app.route("/stats")
@_require_auth
def stats():
    global _stats_cache

    groups = models.get_groups()
    requested_group = request.args.get("group")
    active_group = (requested_group if requested_group in groups else "") if requested_group is not None else ""

    if not _scan_results:
        return render_template(
            "stats.html",
            repos=None,
            scan_results=None,
            groups=groups,
            active_group=active_group,
        )

    force = request.args.get("refresh") == "1"
    key = _stats_cache_key()
    if not force and _stats_cache and _stats_cache.get("key") == key:
        return render_template(
            "stats.html",
            repos=_stats_cache["repos"],
            scan_results=_scan_results,
            periods=list(PERIOD_DAYS.keys()),
            groups=groups,
            active_group=active_group,
        )

    client = _get_github_client()
    if not client:
        flash("Not authenticated with GitHub.", "error")
        return redirect(url_for("dashboard"))

    import datetime as _dt
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now_utc = _dt.datetime.now(_dt.timezone.utc)
    since = (now_utc - _dt.timedelta(days=max(PERIOD_DAYS.values()))).isoformat()

    repos = _scan_results.get("repos", [])
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_collect_repo_activity, client, r, since, now_utc): r
            for r in repos
        }
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except gh.GitHubAuthError:
                # Surface the PAT remediation via the global handler instead
                # of caching all-zero rows that hide the real problem.
                raise
            except Exception as e:
                r = futures[fut]
                results.append({
                    "name": r["name"],
                    "owner": r["owner"],
                    "full_name": r.get("full_name", ""),
                    "html_url": r.get("html_url", ""),
                    "commits_by_period": {k: 0 for k in PERIOD_DAYS},
                    "code_size_bytes": r.get("code_size_bytes", 0),
                    "error": str(e),
                })

    _stats_cache = {"key": key, "repos": results}
    return render_template(
        "stats.html",
        repos=results,
        scan_results=_scan_results,
        periods=list(PERIOD_DAYS.keys()),
        groups=groups,
        active_group=active_group,
    )


# --- What's Next (aggregated across repos) ---

@app.route("/whats-next")
@_require_auth
def whats_next_all():
    summaries = models.get_project_summaries()
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)

    all_repos = _scan_results.get("repos", []) if _scan_results else []
    if active_group:
        group_repos = set(groups.get(active_group, []))
        repos = [r for r in all_repos if r["name"] in group_repos]
    else:
        repos = all_repos

    items = []
    for repo in repos:
        s = summaries.get(repo["name"]) or {}
        next_steps = s.get("next_steps") or []
        if not next_steps:
            continue
        items.append({
            "repo": repo["name"],
            "owner": repo["owner"],
            "html_url": repo.get("html_url", ""),
            "next_steps": next_steps[:5],
            "generated_at": s.get("_generated_at", ""),
        })

    items.sort(key=lambda x: x["repo"].lower())

    return render_template(
        "whats_next.html",
        items=items,
        all_repos=all_repos,
        scan_results=_scan_results,
        has_summaries=bool(summaries),
        groups=groups,
        active_group=active_group,
    )


# --- Chat Briefing ---
#
# One screen that summarizes every project comprehensively — what business
# problem it solves, where it stands, what's built, what's left, open
# decisions — and composes a single Markdown document to paste into a
# Claude chat session (modeled on the CHAT_BRIEFING.md format). AI briefs
# are cached in data/briefs.json and regenerated only when a repo has been
# pushed to since its brief was generated (or on force-regenerate). Every
# generation is logged to data/logs/briefing.log for paste-back debugging.

def _briefing_projects(active_group: str) -> list[dict]:
    """Assemble the per-project briefing dicts for the current scan +
    group filter. Shared by the view, the export, and generation."""
    groups = models.get_groups()
    all_repos = list(_scan_results.get("repos", []) if _scan_results else [])
    if active_group:
        in_group = set(groups.get(active_group, []))
        repos = [r for r in all_repos if r["name"] in in_group]
    else:
        repos = all_repos
    return briefing.assemble_projects(
        repos,
        briefs=models.get_briefs(),
        summaries=models.get_project_summaries(),
        trackers=models.list_trackers(),
        groups=groups,
    )


def _briefing_markdown(projects: list[dict], active_group: str) -> str:
    generated_label = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return briefing.compose_markdown(
        projects,
        owner_login=session.get("github_user", ""),
        active_group=active_group,
        generated_label=generated_label,
    )


@app.route("/briefing")
@_require_auth
def briefing_view():
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)
    projects = _briefing_projects(active_group)
    markdown = _briefing_markdown(projects, active_group)

    briefed = sum(1 for p in projects if p["brief"])
    stale = sum(1 for p in projects if p["stale"])
    sections_md = {p["name"]: briefing.project_section_markdown(p) for p in projects}

    # Target lists for the client-side generation queue: one POST per repo
    # so progress is visible and one failure never kills the batch.
    targets_all = [p["name"] for p in projects]
    targets_needs = [p["name"] for p in projects if not p["brief"] or p["stale"]]

    return render_template(
        "briefing.html",
        scan_results=_scan_results,
        projects=projects,
        markdown=markdown,
        sections_md=sections_md,
        groups=groups,
        active_group=active_group,
        all_repo_count=len(_scan_results.get("repos", [])) if _scan_results else 0,
        briefed_count=briefed,
        stale_count=stale,
        missing_count=len(projects) - briefed,
        targets_all=targets_all,
        targets_needs=targets_needs,
    )


@app.route("/briefing/export.md")
@_require_auth
def briefing_export():
    """The same document the COPY button produces, as a downloadable .md
    file (handy for attaching to a Claude chat instead of pasting)."""
    groups = models.get_groups()
    active_group = _resolve_active_group(groups, persist=False)
    projects = _briefing_projects(active_group)
    markdown = _briefing_markdown(projects, active_group)
    date_part = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    group_part = f"-{active_group.lower().replace(' ', '-')}" if active_group else ""
    filename = f"portfolio-chat-briefing{group_part}-{date_part}.md"
    return Response(
        markdown,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/briefing/generate/<name>", methods=["POST"])
@_require_auth_api
def briefing_generate_one(name):
    """Generate the chat brief for ONE repo. The Briefing page drives this
    sequentially per repo with live progress; the server re-checks
    staleness so a fresh brief is never paid for twice (force overrides)."""
    client = _get_github_client()
    creds = _get_credentials()
    if not client or not creds:
        return jsonify({"error": NOT_CONNECTED_REMEDY}), 401
    repo = _find_repo_by_name(name)
    if not repo:
        return jsonify({"error": f"Repo '{name}' not in the latest scan. Run a scan first."}), 404

    force = bool((request.json or {}).get("force")) if request.is_json else False
    existing = models.get_briefs().get(name)
    if existing and not force and not briefing.is_brief_stale(existing, repo):
        return jsonify({"ok": True, "repo": name, "skipped": "fresh"})

    model = models.get_ai_model()
    tracker = models.get_tracker(repo["owner"], name)

    try:
        context_text = briefing.gather_brief_inputs(client, repo, tracker)
        brief = briefing.generate_brief(
            api_key=creds["anthropic_key"],
            repo_name=name,
            context_text=context_text,
            model=model,
        )
    except gh.GitHubAuthError:
        return jsonify({"error": GITHUB_AUTH_REMEDY}), 401
    except Exception as e:
        # Keep any existing good brief; report the failure to the queue.
        models.log_briefing_event(
            "generate_error", repo=name, error=f"{type(e).__name__}: {str(e)[:300]}",
        )
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}"}), 502

    usage = brief.pop("_usage", {})
    if usage:
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        _session_cost.add(in_tok, out_tok,
                          ai.estimate_cost(in_tok, out_tok, model=model))
    models.save_brief(name, brief)
    models.log_briefing_event(
        "generate_done", repo=name, stage=brief.get("stage"), force=force,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )
    models.log_action("generate_brief", name, "all", f"stage={brief.get('stage')}")
    return jsonify({"ok": True, "repo": name, "stage": brief.get("stage")})


# --- Firestore Setup ---
#
# Cross-repo view of which projects need Firestore configured (a manual
# Firebase-console step) plus per-repo step-by-step instructions. Detection
# is cached in ~/.repodoctor/firestore_data.json so the page is fast after
# the first scan; the SCAN button forces a fresh detection across all repos.

@app.route("/firestore")
@_require_auth
def firestore():
    data = models.get_firestore_data()
    repos_map = data.get("repos", {}) if data else {}
    scanned_at = data.get("_scanned_at", "") if data else ""

    # Apply same group filter as Projects / What's Next.
    groups = models.get_groups()
    requested = request.args.get("group")
    active_group = (requested if requested in groups else "") if requested is not None else ""

    all_repo_names = sorted(repos_map.keys())
    if active_group:
        in_group = set(groups.get(active_group, []))
        visible_names = [n for n in all_repo_names if n in in_group]
    else:
        visible_names = all_repo_names

    needs_setup = []
    configured = []
    not_using = []
    for n in visible_names:
        entry = repos_map[n]
        s = entry.get("status")
        if s == "needs_setup":
            needs_setup.append(entry)
        elif s == "configured":
            configured.append(entry)
        else:
            not_using.append(entry)

    needs_setup.sort(key=lambda r: r["name"].lower())
    configured.sort(key=lambda r: r["name"].lower())
    not_using.sort(key=lambda r: r["name"].lower())

    return render_template(
        "firestore.html",
        scan_results=_scan_results,
        has_data=bool(repos_map),
        scanned_at=scanned_at,
        needs_setup=needs_setup,
        configured=configured,
        not_using=not_using,
        groups=groups,
        active_group=active_group,
        total_visible=len(visible_names),
    )


@app.route("/firestore/scan", methods=["POST"])
@_require_auth
def firestore_scan():
    client = _get_github_client()
    if not client:
        flash("Not authenticated with GitHub.", "error")
        return redirect(url_for("firestore"))
    if not _scan_results:
        flash("Run a repo scan first from My Repos.", "error")
        return redirect(url_for("firestore"))

    from concurrent.futures import ThreadPoolExecutor, as_completed

    repos = _scan_results.get("repos", [])
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                firestore_detector.detect_firestore_status,
                client,
                r["owner"],
                r["name"],
                r.get("default_branch", "main"),
            ): r
            for r in repos
        }
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                results.append(fut.result())
            except gh.GitHubAuthError:
                # Don't overwrite good saved detection data with "not_using"
                # rows caused by a dead PAT — let the global handler show
                # the remediation message instead.
                raise
            except Exception as e:
                results.append({
                    "owner": r["owner"],
                    "name": r["name"],
                    "uses_firestore": False,
                    "status": "not_using",
                    "indicators": [],
                    "missing": [],
                    "project_id": None,
                    "site_domain": None,
                    "indexes_count": 0,
                    "files": {},
                    "instructions": [],
                    "error": str(e)[:200],
                })

    models.save_firestore_data(results)
    needs = sum(1 for r in results if r.get("status") == "needs_setup")
    using = sum(1 for r in results if r.get("uses_firestore"))
    models.log_action(
        "firestore_scan", "all", "all",
        f"Scanned {len(results)} repos: {using} use Firestore, {needs} need setup",
    )
    flash(f"Firestore scan complete: {using} use Firestore, {needs} need setup.", "success")
    return redirect(url_for("firestore"))


# --- Mac Setup ---

@app.route("/mac-setup")
@_require_auth
def mac_setup():
    return render_template("mac_setup.html")


# --- Codebase Tracker ---
#
# Per-repo deep view: modules + infra gaps + features + external systems
# + open questions + next actions + recent changes. Generated on demand
# via Claude; IDs (M1, I1, F1, ...) preserved across regenerations.
# See CODEBASE_TRACKER_PRD.md for the full spec.

def _resolve_repo(owner: str, name: str) -> dict | None:
    """Find a repo in the most recent scan, by owner+name."""
    if not _scan_results:
        return None
    for r in _scan_results.get("repos", []):
        if r["owner"] == owner and r["name"] == name:
            return r
    return None


def _tracker_repo_list() -> list[dict]:
    """[{owner, name, full_name, has_tracker, generated_at}] for the dropdown.
    Includes every scanned repo PLUS any repo that has a saved tracker
    (so a fresh Flask restart with no in-memory scan still lets you reach
    your existing trackers via the dropdown)."""
    out: list[dict] = []
    saved = models.list_trackers()
    seen: set[str] = set()

    if _scan_results:
        for r in _scan_results.get("repos", []):
            key = f"{r['owner']}/{r['name']}"
            existing = saved.get(key)
            out.append({
                "owner": r["owner"],
                "name": r["name"],
                "full_name": r.get("full_name", key),
                "has_tracker": bool(existing),
                "generated_at": (existing or {}).get("generated_at", ""),
            })
            seen.add(key)

    # Saved trackers for repos that aren't in the current scan (e.g. excluded,
    # deleted on GitHub, or scan_results not yet populated this session).
    for key, t in saved.items():
        if key in seen:
            continue
        owner, name = key.split("/", 1)
        out.append({
            "owner": owner,
            "name": name,
            "full_name": key,
            "has_tracker": True,
            "generated_at": t.get("generated_at", ""),
        })

    out.sort(key=lambda r: r["name"].lower())
    return out


@app.route("/tracker")
@_require_auth
def tracker_index():
    """Landing page — auto-routes to the most-recently-generated tracker
    if one exists, otherwise renders the onboarding view with a dropdown."""
    saved = models.list_trackers()

    # If any tracker is saved, jump straight to the most-recent one so
    # the user lands on data, not a blank dropdown.
    recent_key = ""
    recent_ts = ""
    for key, t in saved.items():
        ts = t.get("generated_at", "")
        if ts and ts > recent_ts:
            recent_ts = ts
            recent_key = key
    if recent_key:
        owner, name = recent_key.split("/", 1)
        return redirect(url_for("tracker_view", owner=owner, name=name))

    return render_template(
        "tracker.html",
        mode="index",
        scan_results=_scan_results,
        repos=_tracker_repo_list(),
        selected_owner="",
        selected_name="",
        tracker=None,
        meta=tracker_data,
    )


@app.route("/tracker/<owner>/<name>")
@_require_auth
def tracker_view(owner, name):
    """Render the full 8-tab tracker for one repo."""
    repos = _tracker_repo_list()
    repo_info = _resolve_repo(owner, name)
    tracker = models.get_tracker(owner, name)
    errors: list[str] = []
    if tracker:
        errors = tracker_data.validate_tracker(tracker)
        if errors:
            models.log_tracker_event(
                "render_validation_warn",
                owner=owner, repo=name, errors=errors[:5],
            )
    return render_template(
        "tracker.html",
        mode="view",
        scan_results=_scan_results,
        repos=repos,
        selected_owner=owner,
        selected_name=name,
        repo_info=repo_info,
        tracker=tracker,
        validation_errors=errors,
        meta=tracker_data,
    )


@app.route("/tracker/<owner>/<name>/generate", methods=["POST"])
@_require_auth
def tracker_generate(owner, name):
    """Run AI generation against the chosen repo and save the tracker JSON."""
    client = _get_github_client()
    creds = _get_credentials()
    if not client or not creds:
        flash("Not authenticated.", "error")
        return redirect(url_for("tracker_view", owner=owner, name=name))

    repo_info = _resolve_repo(owner, name)
    prior = models.get_tracker(owner, name)
    if not repo_info and prior:
        # Saved trackers stay reachable (and regenerable) after a restart
        # with no scan — build minimal repo info from the tracker itself.
        repo_info = {
            "owner": owner,
            "name": name,
            "default_branch": prior.get("branch_at_verification") or "main",
        }
    if not repo_info:
        flash("Repository not found in scan results. Run a scan first.", "error")
        return redirect(url_for("tracker_index"))

    default_branch = repo_info.get("default_branch", "main")

    # Per-generation model override from the toolbar dropdown (form field
    # "model"); falls back to the global Settings preference.
    requested_model = (request.form.get("model") or "").strip()
    if requested_model and requested_model in ai.VALID_MODELS:
        model = requested_model
    else:
        model = models.get_ai_model()

    models.log_tracker_event(
        "generate_start", owner=owner, repo=name, model=model,
        had_prior=bool(prior), override=bool(requested_model),
    )

    try:
        inputs = tracker_generator.gather_repo_inputs(
            client, owner, name, default_branch,
        )
        tracker = tracker_generator.generate_tracker(
            api_key=creds["anthropic_key"],
            owner=owner, repo=name,
            default_branch=default_branch,
            inputs=inputs,
            prior_tracker=prior,
            model=model,
        )
    except tracker_generator.TrackerGenerationError as e:
        models.log_tracker_event(
            "generate_error", owner=owner, repo=name, error=str(e)[:500],
        )
        flash(f"Tracker generation failed: {e}", "error")
        return redirect(url_for("tracker_view", owner=owner, name=name))
    except Exception as e:
        models.log_tracker_event(
            "generate_exception", owner=owner, repo=name, error=str(e)[:500],
        )
        flash(f"Tracker generation failed: {type(e).__name__}: {e}", "error")
        return redirect(url_for("tracker_view", owner=owner, name=name))

    usage = tracker.pop("_usage", {})
    if usage:
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cost = ai.estimate_cost(in_tok, out_tok, model=model)
        _session_cost.add(in_tok, out_tok, cost)

    models.save_tracker(owner, name, tracker)
    models.log_tracker_event(
        "generate_done", owner=owner, repo=name,
        n_modules=len(tracker.get("modules", [])),
        n_next=len(tracker.get("next_actions", [])),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )
    models.log_action("generate_tracker", name, default_branch,
                      f"{len(tracker.get('modules', []))} modules, "
                      f"{len(tracker.get('next_actions', []))} next actions")
    flash("Tracker generated.", "success")
    return redirect(url_for("tracker_view", owner=owner, name=name))


@app.route("/tracker/<owner>/<name>/debug")
@_require_auth
def tracker_debug(owner, name):
    """Debug surface: live integrity check + recent log events.
    Copy-for-Claude-Code text block surfaced in the template."""
    tracker = models.get_tracker(owner, name)
    errors: list[str] = []
    if tracker:
        errors = tracker_data.validate_tracker(tracker)
    log_events = models.tail_tracker_log(100)
    return render_template(
        "tracker.html",
        mode="debug",
        scan_results=_scan_results,
        repos=_tracker_repo_list(),
        selected_owner=owner,
        selected_name=name,
        tracker=tracker,
        validation_errors=errors,
        log_events=log_events,
        meta=tracker_data,
    )


@app.route("/api/tracker/<owner>/<name>/copy-event", methods=["POST"])
@_require_auth
def tracker_copy_event(owner, name):
    """Log a copy-prompt event from the client so the debug surface
    can reflect activity. Fire-and-forget — never blocks the UI."""
    action_id = (request.json or {}).get("action_id") if request.is_json else None
    ok = (request.json or {}).get("ok", True) if request.is_json else True
    models.log_tracker_event(
        "copy_prompt", owner=owner, repo=name,
        action_id=action_id, ok=bool(ok),
    )
    return jsonify({"ok": True})


@app.route("/tracker/<owner>/<name>/action/<action_id>/status", methods=["POST"])
@_require_auth
def tracker_action_status(owner, name, action_id):
    """Update a single next-action's status (and optional status_note).
    Used by the BLOCK / DISMISS / mark-shipped buttons on each card."""
    new_status = (request.form.get("status") or "").strip()
    new_note = request.form.get("status_note", "").strip()
    if new_status not in tracker_data.NEXT_ACTION_STATUSES:
        flash(f"Bad status '{new_status}'.", "error")
        return redirect(url_for("tracker_view", owner=owner, name=name))

    tracker = models.get_tracker(owner, name)
    if not tracker:
        flash("No tracker found for this repo.", "error")
        return redirect(url_for("tracker_view", owner=owner, name=name))

    updated = False
    for n in tracker.get("next_actions") or []:
        if n.get("id") == action_id:
            n["status"] = new_status
            if new_note or new_status in ("blocked", "dismissed"):
                # Always record a note for blocked/dismissed so the UI
                # can show WHY. Empty string is fine if user didn't supply.
                n["status_note"] = new_note
            updated = True
            break
    if not updated:
        flash(f"Action {action_id} not found.", "error")
        return redirect(url_for("tracker_view", owner=owner, name=name))

    models.save_tracker(owner, name, tracker)
    models.log_tracker_event(
        "action_status_update",
        owner=owner, repo=name, action_id=action_id,
        new_status=new_status, has_note=bool(new_note),
    )
    flash(f"{action_id} → {new_status}.", "success")
    return redirect(url_for("tracker_view", owner=owner, name=name) + f"#row-{action_id}")


# =====================================================================
# COMMENTED OUT — full branch analysis features (will re-enable later)
# =====================================================================

# # --- Repo Detail ---
#
# @app.route("/repo/<owner>/<name>")
# @_require_auth
# def repo_detail(owner, name):
#     global _scan_results
#     if not _scan_results:
#         flash("Run a scan first.", "error")
#         return redirect(url_for("dashboard"))
#
#     repo = None
#     for r in _scan_results.get("repos", []):
#         if r["owner"] == owner and r["name"] == name:
#             repo = r
#             break
#
#     if not repo:
#         flash("Repository not found in scan results.", "error")
#         return redirect(url_for("dashboard"))
#
#     # Check for cached analyses
#     cache = models.get_analysis_cache()
#     for branch in repo["branches"]:
#         key = f"{name}/{branch['name']}/{branch['commit_sha']}"
#         if key in cache:
#             branch["analysis"] = cache[key]
#
#     prefs = models.get_preferences()
#     spec = models.get_spec(name)
#
#     return render_template(
#         "repo_detail.html",
#         repo=repo,
#         preferences=prefs,
#         has_spec=spec is not None,
#         session_cost=_session_cost.to_dict(),
#     )
#
#
# # --- AI Analysis ---
#
# @app.route("/analyze", methods=["POST"])
# @_require_auth
# def analyze_branch_route():
#     creds = _get_credentials()
#     client = _get_github_client()
#     if not creds or not client:
#         return jsonify({"error": "Not authenticated"}), 401
#
#     data = request.json
#     repo_name = data.get("repo_name")
#     owner = data.get("owner")
#     branch_name = data.get("branch_name")
#     commit_sha = data.get("commit_sha")
#
#     if not all([repo_name, owner, branch_name, commit_sha]):
#         return jsonify({"error": "Missing required fields"}), 400
#
#     # Check cache
#     cached = models.get_cached_analysis(repo_name, branch_name, commit_sha)
#     if cached:
#         return jsonify({"analysis": cached, "from_cache": True})
#
#     # Find branch data in scan results
#     branch_data = None
#     default_branch = "main"
#     if _scan_results:
#         for repo in _scan_results.get("repos", []):
#             if repo["name"] == repo_name and repo["owner"] == owner:
#                 default_branch = repo["default_branch"]
#                 for b in repo["branches"]:
#                     if b["name"] == branch_name:
#                         branch_data = b
#                         break
#                 break
#
#     if not branch_data:
#         return jsonify({"error": "Branch not found in scan results"}), 404
#
#     prefs = models.get_preferences()
#     model = prefs.get("ai_model", "claude-sonnet-4-5-20250929")
#     spec_text = models.get_spec(repo_name)
#     local_root = prefs.get("local_root", "~/claudesync2")
#     local_path = f"{local_root}/{repo_name}"
#
#     # Get default branch recent commits for context
#     default_commits = client.get_default_branch_commits(owner, repo_name, default_branch)
#
#     try:
#         analysis = ai.analyze_branch(
#             api_key=creds["anthropic_key"],
#             repo_name=repo_name,
#             branch_data=branch_data,
#             default_branch=default_branch,
#             default_branch_commits=default_commits,
#             spec_text=spec_text,
#             local_path=local_path,
#             model=model,
#         )
#     except Exception as e:
#         return jsonify({"error": f"AI analysis failed: {str(e)}"}), 500
#
#     # Cache and log
#     models.cache_analysis(repo_name, branch_name, commit_sha, analysis)
#     models.log_action("analyze", repo_name, branch_name, f"AI analysis ({model})")
#
#     # Track cost
#     usage = analysis.get("_usage", {})
#     cost = ai.estimate_cost(
#         usage.get("input_tokens", 0),
#         usage.get("output_tokens", 0),
#         model,
#     )
#     _session_cost.add(
#         usage.get("input_tokens", 0),
#         usage.get("output_tokens", 0),
#         cost,
#     )
#
#     return jsonify({"analysis": analysis, "from_cache": False, "cost": cost})
#
#
# @app.route("/estimate", methods=["POST"])
# @_require_auth
# def estimate_cost_route():
#     data = request.json
#     repo_name = data.get("repo_name")
#     branch_name = data.get("branch_name")
#
#     branch_data = None
#     if _scan_results:
#         for repo in _scan_results.get("repos", []):
#             if repo["name"] == repo_name:
#                 for b in repo["branches"]:
#                     if b["name"] == branch_name:
#                         branch_data = b
#                         break
#                 break
#
#     if not branch_data:
#         return jsonify({"error": "Branch not found"}), 404
#
#     spec_text = models.get_spec(repo_name)
#     tokens = ai.estimate_tokens(branch_data, spec_text)
#     prefs = models.get_preferences()
#     model = prefs.get("ai_model", "claude-sonnet-4-5-20250929")
#     cost = ai.estimate_cost(tokens, 1000, model)
#
#     return jsonify({"estimated_tokens": tokens, "estimated_cost": cost, "model": model})
#
#
# # --- Archive ---
#
# @app.route("/archive")
# @_require_auth
# def archive():
#     client = _get_github_client()
#     if not client or not _scan_results:
#         return render_template("archive.html", archives=[], scan_results=_scan_results, session_cost=_session_cost.to_dict())
#
#     archives = []
#     for repo in _scan_results.get("repos", []):
#         tags = client.get_tags(repo["owner"], repo["name"])
#         for tag in tags:
#             if tag["name"].startswith("archive/"):
#                 parts = tag["name"].split("/")
#                 branch_name = "/".join(parts[1:-1]) if len(parts) > 2 else parts[1]
#                 archive_date = parts[-1] if len(parts) > 2 else "Unknown"
#
#                 # Check for cached analysis
#                 cached = None
#                 cache = models.get_analysis_cache()
#                 for cache_key, cache_val in cache.items():
#                     if cache_key.startswith(f"{repo['name']}/{branch_name}/"):
#                         cached = cache_val
#                         break
#
#                 archives.append({
#                     "repo_name": repo["name"],
#                     "repo_full_name": repo["full_name"],
#                     "owner": repo["owner"],
#                     "branch_name": branch_name,
#                     "tag_name": tag["name"],
#                     "archive_date": archive_date,
#                     "sha": tag["commit"]["sha"],
#                     "html_url": f"{repo['html_url']}/tree/{tag['name']}",
#                     "analysis": cached,
#                 })
#
#     return render_template("archive.html", archives=archives, scan_results=_scan_results, session_cost=_session_cost.to_dict())
#
#
# @app.route("/archive/create", methods=["POST"])
# @_require_auth
# def create_archive():
#     client = _get_github_client()
#     if not client:
#         return jsonify({"error": "Not authenticated"}), 401
#
#     data = request.json
#     owner = data.get("owner")
#     repo_name = data.get("repo_name")
#     branch_name = data.get("branch_name")
#     commit_sha = data.get("commit_sha")
#     note = data.get("note", "")
#
#     if not all([owner, repo_name, branch_name, commit_sha]):
#         return jsonify({"error": "Missing required fields"}), 400
#
#     # Build tag message
#     cached = models.get_cached_analysis(repo_name, branch_name, commit_sha)
#     summary = ""
#     if cached:
#         summary = cached.get("plain_english_summary", "")
#
#     message_parts = [
#         f"Archived branch: {branch_name}",
#         f"Repository: {owner}/{repo_name}",
#         f"Commit: {commit_sha[:7]}",
#     ]
#     if summary:
#         message_parts.append(f"AI Summary: {summary}")
#     if note:
#         message_parts.append(f"User note: {note}")
#
#     message = "\n".join(message_parts)
#
#     result = client.create_archive_tag(owner, repo_name, branch_name, commit_sha, message)
#     if result is None:
#         return jsonify({"error": "Failed to create archive tag"}), 500
#
#     models.log_action("archive", repo_name, branch_name, f"Created tag {result['tag_name']}")
#
#     # Generate delete instructions
#     prefs = models.get_preferences()
#     local_root = prefs.get("local_root", "~/claudesync2")
#     delete_instructions = (
#         f"# Delete archived branch: {branch_name}\n"
#         f"# Archived as: {result['tag_name']}\n"
#         f"cd {local_root}/{repo_name} && claude --continue\n\n"
#         f"# Paste into Claude Code:\n"
#         f"Please delete the branch '{branch_name}' both locally and on the remote.\n"
#         f"It has been archived as tag '{result['tag_name']}'.\n"
#         f"1. git branch -D {branch_name}\n"
#         f"2. git push origin --delete {branch_name}\n"
#         f"3. Confirm deletion with: git branch -a | grep {branch_name}"
#     )
#
#     return jsonify({
#         "success": True,
#         "tag_name": result["tag_name"],
#         "delete_instructions": delete_instructions,
#     })
#
#
# @app.route("/archive/reinstate-instructions", methods=["POST"])
# @_require_auth
# def reinstate_instructions():
#     data = request.json
#     repo_name = data.get("repo_name")
#     owner = data.get("owner")
#     branch_name = data.get("branch_name")
#     tag_name = data.get("tag_name")
#
#     prefs = models.get_preferences()
#     local_root = prefs.get("local_root", "~/claudesync2")
#
#     instructions = (
#         f"# Reinstate archived branch: {branch_name}\n"
#         f"# From tag: {tag_name}\n"
#         f"cd {local_root}/{repo_name} && claude --continue\n\n"
#         f"# Paste into Claude Code:\n"
#         f"I need to reinstate an archived branch. Please:\n"
#         f"1. Create branch from archive tag:\n"
#         f"   git branch {branch_name} {tag_name}\n"
#         f"2. Push to origin: git push origin {branch_name}\n"
#         f"3. Checkout: git checkout {branch_name}\n"
#         f"4. Show contents: git log --oneline main..{branch_name}"
#     )
#
#     models.log_action("reinstate_instructions", repo_name, branch_name, f"Generated reinstate from {tag_name}")
#     return jsonify({"instructions": instructions})
#
#
# # --- Setup Guide ---
#
# @app.route("/setup-guide")
# @_require_auth
# def setup_guide():
#     repos = []
#     if _scan_results:
#         repos = _scan_results.get("repos", [])
#
#     prefs = models.get_preferences()
#     local_root = prefs.get("local_root", "~/claudesync2")
#
#     return render_template(
#         "setup_guide.html",
#         repos=repos,
#         local_root=local_root,
#         session_cost=_session_cost.to_dict(),
#     )
#
#
# # --- Action Log ---
#
# @app.route("/action-log")
# @_require_auth
# def action_log():
#     actions = models.get_action_log()
#     actions.reverse()  # Most recent first
#     return render_template(
#         "action_log.html",
#         actions=actions,
#         session_cost=_session_cost.to_dict(),
#     )
#
#
# # --- API: Mark branch as done ---
#
# @app.route("/api/mark-done", methods=["POST"])
# @_require_auth
# def mark_done():
#     data = request.json
#     repo_name = data.get("repo_name")
#     branch_name = data.get("branch_name")
#     models.log_action("mark_done", repo_name, branch_name, "Marked as done by user")
#     return jsonify({"success": True})
#
#
# # --- API: Toggle display mode ---
#
# @app.route("/api/toggle-display-mode", methods=["POST"])
# @_require_auth
# def toggle_display_mode():
#     prefs = models.get_preferences()
#     current = prefs.get("display_mode", "plain_english")
#     new_mode = "shorthand" if current == "plain_english" else "plain_english"
#     prefs["display_mode"] = new_mode
#     models.save_preferences(prefs)
#     return jsonify({"display_mode": new_mode})


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    models._ensure_dirs()
    app.run(host="127.0.0.1", port=5001, debug=True)
