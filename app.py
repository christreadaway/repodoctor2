"""
RepDoctor2 — AI-Powered Repository Management Tool
Main Flask application.
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
import models

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

# In-memory session state
_github_client: gh.GitHubClient | None = None
_credentials: dict | None = None
_session_cost = models.SessionCost()
_scan_results: dict | None = None  # Latest scan results


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
    global _scan_results
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
            repo_data = gh.scan_repo(client, repo)
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
                "branches": [],
                "branch_count": 0,
                "has_claude_md": False,
                "error": str(e),
            })

    # Sort by branch count descending
    results.sort(key=lambda r: r.get("branch_count", 0), reverse=True)

    _scan_results = {
        "repos": results,
        "total_repos": len(results),
        "total_branches": sum(r.get("branch_count", 0) for r in results),
    }
    models.save_scan(_scan_results)
    models.log_action("scan", "all", "all", f"Scanned {len(results)} repos, {_scan_results['total_branches']} branches")

    flash(f"Scan complete: {len(results)} repos, {_scan_results['total_branches']} non-default branches found.", "success")
    return redirect(url_for("dashboard"))


# --- Repo Detail ---

@app.route("/repo/<owner>/<name>")
@_require_auth
def repo_detail(owner, name):
    global _scan_results
    if not _scan_results:
        flash("Run a scan first.", "error")
        return redirect(url_for("dashboard"))

    repo = None
    for r in _scan_results.get("repos", []):
        if r["owner"] == owner and r["name"] == name:
            repo = r
            break

    if not repo:
        flash("Repository not found in scan results.", "error")
        return redirect(url_for("dashboard"))

    # Check for cached analyses
    cache = models.get_analysis_cache()
    for branch in repo["branches"]:
        key = f"{name}/{branch['name']}/{branch['commit_sha']}"
        if key in cache:
            branch["analysis"] = cache[key]

    prefs = models.get_preferences()
    spec = models.get_spec(name)

    return render_template(
        "repo_detail.html",
        repo=repo,
        preferences=prefs,
        has_spec=spec is not None,
        session_cost=_session_cost.to_dict(),
    )


# --- AI Analysis ---

@app.route("/analyze", methods=["POST"])
@_require_auth
def analyze_branch_route():
    creds = _get_credentials()
    client = _get_github_client()
    if not creds or not client:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.json
    repo_name = data.get("repo_name")
    owner = data.get("owner")
    branch_name = data.get("branch_name")
    commit_sha = data.get("commit_sha")

    if not all([repo_name, owner, branch_name, commit_sha]):
        return jsonify({"error": "Missing required fields"}), 400

    # Check cache
    cached = models.get_cached_analysis(repo_name, branch_name, commit_sha)
    if cached:
        return jsonify({"analysis": cached, "from_cache": True})

    # Find branch data in scan results
    branch_data = None
    default_branch = "main"
    if _scan_results:
        for repo in _scan_results.get("repos", []):
            if repo["name"] == repo_name and repo["owner"] == owner:
                default_branch = repo["default_branch"]
                for b in repo["branches"]:
                    if b["name"] == branch_name:
                        branch_data = b
                        break
                break

    if not branch_data:
        return jsonify({"error": "Branch not found in scan results"}), 404

    prefs = models.get_preferences()
    model = prefs.get("ai_model", "claude-sonnet-4-5-20250929")
    spec_text = models.get_spec(repo_name)
    local_root = prefs.get("local_root", "~/claudesync2")
    local_path = f"{local_root}/{repo_name}"

    # Get default branch recent commits for context
    default_commits = client.get_default_branch_commits(owner, repo_name, default_branch)

    try:
        analysis = ai.analyze_branch(
            api_key=creds["anthropic_key"],
            repo_name=repo_name,
            branch_data=branch_data,
            default_branch=default_branch,
            default_branch_commits=default_commits,
            spec_text=spec_text,
            local_path=local_path,
            model=model,
        )
    except Exception as e:
        return jsonify({"error": f"AI analysis failed: {str(e)}"}), 500

    # Cache and log
    models.cache_analysis(repo_name, branch_name, commit_sha, analysis)
    models.log_action("analyze", repo_name, branch_name, f"AI analysis ({model})")

    # Track cost
    usage = analysis.get("_usage", {})
    cost = ai.estimate_cost(
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        model,
    )
    _session_cost.add(
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        cost,
    )

    return jsonify({"analysis": analysis, "from_cache": False, "cost": cost})


@app.route("/estimate", methods=["POST"])
@_require_auth
def estimate_cost_route():
    data = request.json
    repo_name = data.get("repo_name")
    branch_name = data.get("branch_name")

    branch_data = None
    if _scan_results:
        for repo in _scan_results.get("repos", []):
            if repo["name"] == repo_name:
                for b in repo["branches"]:
                    if b["name"] == branch_name:
                        branch_data = b
                        break
                break

    if not branch_data:
        return jsonify({"error": "Branch not found"}), 404

    spec_text = models.get_spec(repo_name)
    tokens = ai.estimate_tokens(branch_data, spec_text)
    prefs = models.get_preferences()
    model = prefs.get("ai_model", "claude-sonnet-4-5-20250929")
    cost = ai.estimate_cost(tokens, 1000, model)

    return jsonify({"estimated_tokens": tokens, "estimated_cost": cost, "model": model})


# --- Archive ---

@app.route("/archive")
@_require_auth
def archive():
    client = _get_github_client()
    if not client or not _scan_results:
        return render_template("archive.html", archives=[], scan_results=_scan_results, session_cost=_session_cost.to_dict())

    archives = []
    for repo in _scan_results.get("repos", []):
        tags = client.get_tags(repo["owner"], repo["name"])
        for tag in tags:
            if tag["name"].startswith("archive/"):
                parts = tag["name"].split("/")
                branch_name = "/".join(parts[1:-1]) if len(parts) > 2 else parts[1]
                archive_date = parts[-1] if len(parts) > 2 else "Unknown"

                # Check for cached analysis
                cached = None
                cache = models.get_analysis_cache()
                for cache_key, cache_val in cache.items():
                    if cache_key.startswith(f"{repo['name']}/{branch_name}/"):
                        cached = cache_val
                        break

                archives.append({
                    "repo_name": repo["name"],
                    "repo_full_name": repo["full_name"],
                    "owner": repo["owner"],
                    "branch_name": branch_name,
                    "tag_name": tag["name"],
                    "archive_date": archive_date,
                    "sha": tag["commit"]["sha"],
                    "html_url": f"{repo['html_url']}/tree/{tag['name']}",
                    "analysis": cached,
                })

    return render_template("archive.html", archives=archives, scan_results=_scan_results, session_cost=_session_cost.to_dict())


@app.route("/archive/create", methods=["POST"])
@_require_auth
def create_archive():
    client = _get_github_client()
    if not client:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.json
    owner = data.get("owner")
    repo_name = data.get("repo_name")
    branch_name = data.get("branch_name")
    commit_sha = data.get("commit_sha")
    note = data.get("note", "")

    if not all([owner, repo_name, branch_name, commit_sha]):
        return jsonify({"error": "Missing required fields"}), 400

    # Build tag message
    cached = models.get_cached_analysis(repo_name, branch_name, commit_sha)
    summary = ""
    if cached:
        summary = cached.get("plain_english_summary", "")

    message_parts = [
        f"Archived branch: {branch_name}",
        f"Repository: {owner}/{repo_name}",
        f"Commit: {commit_sha[:7]}",
    ]
    if summary:
        message_parts.append(f"AI Summary: {summary}")
    if note:
        message_parts.append(f"User note: {note}")

    message = "\n".join(message_parts)

    result = client.create_archive_tag(owner, repo_name, branch_name, commit_sha, message)
    if result is None:
        return jsonify({"error": "Failed to create archive tag"}), 500

    models.log_action("archive", repo_name, branch_name, f"Created tag {result['tag_name']}")

    # Generate delete instructions
    prefs = models.get_preferences()
    local_root = prefs.get("local_root", "~/claudesync2")
    delete_instructions = (
        f"# Delete archived branch: {branch_name}\n"
        f"# Archived as: {result['tag_name']}\n"
        f"cd {local_root}/{repo_name} && claude --continue\n\n"
        f"# Paste into Claude Code:\n"
        f"Please delete the branch '{branch_name}' both locally and on the remote.\n"
        f"It has been archived as tag '{result['tag_name']}'.\n"
        f"1. git branch -D {branch_name}\n"
        f"2. git push origin --delete {branch_name}\n"
        f"3. Confirm deletion with: git branch -a | grep {branch_name}"
    )

    return jsonify({
        "success": True,
        "tag_name": result["tag_name"],
        "delete_instructions": delete_instructions,
    })


@app.route("/archive/reinstate-instructions", methods=["POST"])
@_require_auth
def reinstate_instructions():
    data = request.json
    repo_name = data.get("repo_name")
    owner = data.get("owner")
    branch_name = data.get("branch_name")
    tag_name = data.get("tag_name")

    prefs = models.get_preferences()
    local_root = prefs.get("local_root", "~/claudesync2")

    instructions = (
        f"# Reinstate archived branch: {branch_name}\n"
        f"# From tag: {tag_name}\n"
        f"cd {local_root}/{repo_name} && claude --continue\n\n"
        f"# Paste into Claude Code:\n"
        f"I need to reinstate an archived branch. Please:\n"
        f"1. Create branch from archive tag:\n"
        f"   git branch {branch_name} {tag_name}\n"
        f"2. Push to origin: git push origin {branch_name}\n"
        f"3. Checkout: git checkout {branch_name}\n"
        f"4. Show contents: git log --oneline main..{branch_name}"
    )

    models.log_action("reinstate_instructions", repo_name, branch_name, f"Generated reinstate from {tag_name}")
    return jsonify({"instructions": instructions})


# --- Setup Guide ---

@app.route("/setup-guide")
@_require_auth
def setup_guide():
    repos = []
    if _scan_results:
        repos = _scan_results.get("repos", [])

    prefs = models.get_preferences()
    local_root = prefs.get("local_root", "~/claudesync2")

    return render_template(
        "setup_guide.html",
        repos=repos,
        local_root=local_root,
        session_cost=_session_cost.to_dict(),
    )


# --- Action Log ---

@app.route("/action-log")
@_require_auth
def action_log():
    actions = models.get_action_log()
    actions.reverse()  # Most recent first
    return render_template(
        "action_log.html",
        actions=actions,
        session_cost=_session_cost.to_dict(),
    )


# --- Settings ---

@app.route("/settings", methods=["GET", "POST"])
@_require_auth
def settings():
    prefs = models.get_preferences()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "save_preferences":
            prefs["local_root"] = request.form.get("local_root", "~/claudesync2")
            prefs["ai_model"] = request.form.get("ai_model", "claude-sonnet-4-5-20250929")
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


# --- API: Mark branch as done ---

@app.route("/api/mark-done", methods=["POST"])
@_require_auth
def mark_done():
    data = request.json
    repo_name = data.get("repo_name")
    branch_name = data.get("branch_name")
    models.log_action("mark_done", repo_name, branch_name, "Marked as done by user")
    return jsonify({"success": True})


if __name__ == "__main__":
    models._ensure_dirs()
    app.run(host="127.0.0.1", port=5001, debug=True)
