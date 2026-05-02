"""
RepoDoctor — AI-Powered Repository Management Tool
Main Flask application.

Simplified mode: repo overview with branch counts + required file checks.
"""

import os
import secrets

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify,
)

import security
import github_client as gh
import ai_analyzer as ai
import anthropic
import models
import spec_cleaner
import project_mapper

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

# In-memory session state
_github_client: gh.GitHubClient | None = None
_credentials: dict | None = None
_session_cost = models.SessionCost()
_scan_results: dict | None = None  # Latest scan results


@app.context_processor
def inject_preferences():
    """Make preferences available in all templates."""
    return {"preferences": models.get_preferences()}


def _require_auth(f):
    """Decorator: redirect to login if not authenticated."""
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


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
            _init_session(creds)
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        else:
            github_pat = request.form.get("github_pat", "").strip()
            anthropic_key = request.form.get("anthropic_key", "").strip()
            if not all([password, github_pat, anthropic_key]):
                flash("All fields are required.", "error")
                return render_template("login.html", has_credentials=False)
            if len(password) < 4:
                flash("Password must be at least 4 characters.", "error")
                return render_template("login.html", has_credentials=False)

            # Verify GitHub PAT
            test_client = gh.GitHubClient(github_pat)
            user_info = test_client.verify_token()
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


def _init_session(creds: dict):
    global _github_client, _credentials
    _credentials = creds
    _github_client = gh.GitHubClient(creds["github_pat"])
    user_info = _github_client.verify_token()
    if user_info:
        session["github_user"] = user_info.get("login", "")
    # TEMPORARY: see DEFAULT_USER_GROUPS in models.py — remove on next rebuild.
    seeded = models.seed_default_groups_if_missing()
    if seeded:
        models.log_action("seed_groups", "all", "all", f"Seeded default groups: {', '.join(seeded)}")


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

    repos = client.get_repos()
    results = []
    for repo in repos:
        if repo["full_name"] in excluded or repo["name"] in excluded:
            continue
        try:
            repo_data = gh.scan_repo_lite(client, repo)
            results.append(repo_data)
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
                "total_branch_count": 0,
                "non_default_branch_count": 0,
                "branch_names": [],
                "required_files": {},
                "files_present": 0,
                "files_total": 5,
                "code_size_bytes": 0,
                "languages": {},
                "error": str(e),
            })

    # Sort by total branch count descending
    results.sort(key=lambda r: r.get("total_branch_count", 0), reverse=True)

    _scan_results = {
        "repos": results,
        "total_repos": len(results),
        "total_branches": sum(r.get("total_branch_count", 0) for r in results),
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
            prefs["ai_model"] = request.form.get("ai_model", "claude-haiku-4-5-20251001")
            prefs["display_mode"] = request.form.get("display_mode", "plain_english")
            excluded = request.form.get("excluded_repos", "")
            prefs["excluded_repos"] = [r.strip() for r in excluded.split(",") if r.strip()]
            models.save_preferences(prefs)
            flash("Preferences saved.", "success")

        elif action == "save_spec":
            spec_repo = request.form.get("spec_repo", "").strip()
            spec_content = request.form.get("spec_content", "").strip()
            if spec_repo and spec_content:
                models.save_spec(spec_repo, spec_content)
                flash(f"Spec saved for {spec_repo}.", "success")

        elif action == "reset_credentials":
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

@app.route("/projects")
@_require_auth
def projects():
    summaries = models.get_project_summaries()
    groups = models.get_groups()
    prefs = models.get_preferences()

    # Resolve active group: ?group=X wins and is persisted; otherwise use saved pref.
    requested = request.args.get("group")
    if requested is not None:
        active_group = requested if requested in groups else ""
        if prefs.get("active_group", "") != active_group:
            prefs["active_group"] = active_group
            models.save_preferences(prefs)
    else:
        active_group = prefs.get("active_group", "")
        if active_group and active_group not in groups:
            active_group = ""

    all_repos = list(_scan_results.get("repos", []) if _scan_results else [])
    # Most recently updated first; missing/blank dates sink to the bottom.
    all_repos.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    if active_group:
        group_repos = set(groups.get(active_group, []))
        repos = [r for r in all_repos if r["name"] in group_repos]
    else:
        repos = all_repos

    return render_template(
        "projects.html",
        repos=repos,
        all_repos=all_repos,
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


@app.route("/projects/generate", methods=["POST"])
@_require_auth
def generate_project_summaries():
    global _scan_results
    client = _get_github_client()
    creds = _get_credentials()
    if not client or not creds:
        flash("Not authenticated.", "error")
        return redirect(url_for("projects"))

    if not _scan_results:
        flash("Run a scan first from the Dashboard.", "error")
        return redirect(url_for("projects"))

    repos = _scan_results.get("repos", [])
    generated = 0
    skipped = 0

    for repo in repos:
        owner = repo["owner"]
        name = repo["name"]
        ref = repo.get("default_branch", "main")

        # Recursive search so specs in subfolders are found.
        _, actual_paths = client.check_required_files(owner, name, ref=ref)
        spec_lookup = {
            "product_spec": actual_paths.get("PRODUCT_SPEC.md"),
            "project_status": actual_paths.get("PROJECT_STATUS.md"),
            "session_notes": actual_paths.get("SESSION_NOTES.md"),
            "claude": actual_paths.get("CLAUDE.md"),
        }

        spec_content = {}
        for key, path in spec_lookup.items():
            if not path:
                continue
            content = client.get_file_content(owner, name, path, ref=ref)
            if content:
                # Truncate to keep prompt small
                spec_content[key] = content[:5000]

        # Build context for AI
        context_parts = []
        if repo.get("description"):
            context_parts.append(f"GitHub description: {repo['description']}")
        for key, content in spec_content.items():
            context_parts.append(f"--- {key.upper()} ---\n{content}")

        if not context_parts:
            # No specs or description — generate a minimal summary
            models.save_project_summary(name, {
                "what_it_does": f"{name} — no spec files or description available.",
                "how_finished": "Unknown — no spec files found.",
                "next_steps": ["Add PRODUCT_SPEC.md with project description", "Add SESSION_NOTES.md with session tracking"],
            })
            skipped += 1
            continue

        context_text = "\n\n".join(context_parts)

        # Call Claude Haiku for a concise summary
        try:
            ai_client = anthropic.Anthropic(api_key=creds["anthropic_key"])
            response = ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
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
            import json
            raw = response.content[0].text.strip()
            # Handle markdown fencing if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3].strip()
            summary = json.loads(raw)
            # Ensure next_steps is capped at 5
            if "next_steps" in summary and len(summary["next_steps"]) > 5:
                summary["next_steps"] = summary["next_steps"][:5]
            models.save_project_summary(name, summary)
            generated += 1
        except Exception as e:
            models.save_project_summary(name, {
                "what_it_does": repo.get("description") or f"{name} — summary generation failed.",
                "how_finished": "Unknown — AI summary could not be generated.",
                "next_steps": [f"Error: {str(e)[:100]}"],
            })
            skipped += 1

    flash(f"Generated summaries for {generated} projects ({skipped} skipped/fallback).", "success")
    models.log_action("generate_summaries", "all", "all", f"Generated {generated}, skipped {skipped}")
    return redirect(url_for("projects"))


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
    # Group by repo so we only fetch get_branches/get_pulls once per repo.
    by_repo: dict[tuple[str, str], list[dict]] = {}
    for t in targets:
        by_repo.setdefault((t["owner"], t["repo"]), []).append(t)

    for (owner, name), items in by_repo.items():
        default_branch = items[0]["default_branch"]
        try:
            branches_full = client.get_branches(owner, name)
        except Exception as e:
            for t in items:
                models.save_henry_summary(name, t["branch_name"], _henry_error_record(t, str(e)))
                failed += 1
            continue
        sha_lookup = {b["name"]: b["commit"]["sha"] for b in branches_full}
        try:
            pulls = client.get_pulls(owner, name)
            pr_branches = {pr["head"]["ref"] for pr in pulls}
        except Exception:
            pr_branches = set()

        spec_text = models.get_spec(name)

        for t in items:
            bname = t["branch_name"]
            comparison = client.compare_branches(owner, name, default_branch, bname)
            if comparison is None:
                models.save_henry_summary(name, bname, _henry_error_record(t, "Could not compare branch."))
                failed += 1
                continue

            last_commit_date = None
            last_commit_author = None
            if comparison.get("commits"):
                lc = comparison["commits"][-1]
                last_commit_date = lc["commit"]["committer"]["date"]
                last_commit_author = (
                    lc["commit"]["author"]["name"] if lc["commit"].get("author") else "Unknown"
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
            commit_messages = [
                {
                    "sha": c["sha"][:7],
                    "message": c["commit"]["message"].split("\n")[0],
                    "author": c["commit"]["author"]["name"] if c["commit"].get("author") else "Unknown",
                    "date": c["commit"]["committer"]["date"] if c["commit"].get("committer") else None,
                }
                for c in comparison.get("commits", [])
            ]

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
                )
                usage = analysis.get("_usage", {})
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                cost = ai.estimate_cost(in_tok, out_tok)
                _session_cost.add(in_tok, out_tok, cost)

                models.save_henry_summary(name, bname, {
                    "owner": owner,
                    "repo": name,
                    "branch_name": bname,
                    "default_branch": default_branch,
                    "html_url": t["html_url"],
                    "private": t["private"],
                    "plain_english_summary": analysis.get("plain_english_summary", ""),
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
    prefs = models.get_preferences()

    # Same group-resolution behavior as /projects: ?group= wins and persists,
    # otherwise fall back to the saved active_group pref.
    requested = request.args.get("group")
    if requested is not None:
        active_group = requested if requested in groups else ""
        if prefs.get("active_group", "") != active_group:
            prefs["active_group"] = active_group
            models.save_preferences(prefs)
    else:
        active_group = prefs.get("active_group", "")
        if active_group and active_group not in groups:
            active_group = ""

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


# --- Mac Setup ---

@app.route("/mac-setup")
@_require_auth
def mac_setup():
    return render_template("mac_setup.html")


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
