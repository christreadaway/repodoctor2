"""
RepoDoctor — AI-Powered Repository Management Tool
Main Flask application.

Simplified mode: repo overview with branch counts + required file checks.
"""

import datetime
import logging
import os
import re
import secrets
import time
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _CENTRAL_TZ = ZoneInfo("America/Chicago")
except Exception as _tz_err:
    logging.getLogger(__name__).warning(
        "America/Chicago timezone unavailable (%s: %s) - times will show in UTC. "
        "Install tzdata: pip install tzdata", type(_tz_err).__name__, _tz_err)
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
import sessions
import inflight
import resume
import components
import portfolio
import docs_explorer
import activity
import nextup
import md_render
import program

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


def _configure_file_logging() -> None:
    """Send everything Python logs (every module's logger, Werkzeug's request
    lines, unhandled exceptions Flask reports) to a rotating file at
    data/logs/app.log (the console handler is added in __main__). One place to look — and to
    copy from Settings → Debug log — when something goes wrong. Idempotent."""
    root = logging.getLogger()
    if any(getattr(h, "_repodoctor_app_log", False) for h in root.handlers):
        return
    try:
        from logging.handlers import RotatingFileHandler
        os.makedirs(os.path.dirname(models.APP_LOG_PATH), exist_ok=True)
        handler = RotatingFileHandler(models.APP_LOG_PATH, maxBytes=2_000_000,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.setLevel(logging.INFO)
        handler._repodoctor_app_log = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
    except OSError as e:  # never let logging setup take the app down
        logger.warning("Could not open app log file: %s", e)


_configure_file_logging()

app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()
# Harden the session cookie. HttpOnly keeps client JS from reading it;
# SameSite=Lax stops other sites from riding a logged-in session with
# cross-site POSTs (Chrome/Firefox default to Lax, but Safari does not — and
# this app runs on a Mac). SESSION_COOKIE_SECURE is deliberately left off: the
# local app is served over http://127.0.0.1, where a Secure cookie is never
# sent, which would break login.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

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


@app.context_processor
def inject_snapshot_flag():
    """PUBLISH TO PHONE shows only when the snapshot is set up and unlocked."""
    try:
        ready = bool(_snapshot_secrets) and bool(models.get_snapshot_state().get("site_id"))
    except Exception:  # noqa: BLE001 - never break a page over this
        ready = False
    return {"snapshot_ready": ready}


@app.context_processor
def inject_anthropic_key_flag():
    """Lets AI buttons warn up front when the key was rejected at the last
    check (login, Settings), instead of failing per repo."""
    return {"anthropic_key_invalid": _anthropic_key_status.get("status") == "invalid"}


@app.template_filter("safe_session_url")
def _safe_session_url_filter(value):
    """Templates re-check any session URL with the same strict rule as the
    parser, so a corrupted cache value can never become a link."""
    return sessions.safe_session_url(value)


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

# Ensure the four category groups (Church/School/Combined/Personal) exist so
# they show up as filters everywhere from first launch. Idempotent — only
# writes when one is missing, and never clobbers existing members.
models.seed_category_groups()


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
    if request.path.startswith("/api/"):
        # Client-side queues expect JSON; a redirect would be followed and
        # show "Server error (200)" with the flash swallowed.
        return jsonify({"error": GITHUB_AUTH_REMEDY}), 401
    flash(GITHUB_AUTH_REMEDY, "error")
    target = url_for("dashboard") if session.get("authenticated") else url_for("login")
    return redirect(target)


@app.errorhandler(requests.RequestException)
def _handle_github_network_error(e):
    """A transient network failure talking to GitHub (timeout, DNS, connection
    reset) is not a GitHubAuthError, so without this it would surface as a raw
    500. Flash a retry message and bounce — covers the unguarded get_repos()
    in scan() and the GitHub calls in repo_detail / debug_files."""
    logger.warning("GitHub network error on %s: %s", request.path, e)
    if request.path.startswith("/api/"):
        return jsonify({"error": "Couldn't reach GitHub — check your connection and retry."}), 503
    flash("Couldn't reach GitHub — check your internet connection and try again.", "error")
    target = url_for("dashboard") if session.get("authenticated") else url_for("login")
    return redirect(target)


# The app only ever runs at 127.0.0.1 / localhost. Rejecting any other Host
# header blocks DNS-rebinding (a hostile site re-pointing its own name at
# 127.0.0.1 to read pages and the per-boot form token); rejecting a POST whose
# Origin is a different site blocks cross-site form posts, including ones from
# other localhost ports that SameSite=Lax treats as same-site.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


@app.before_request
def _local_only_guard():
    host = urllib.parse.urlsplit(f"//{request.host or ''}").hostname or ""
    if host.lower() not in {h.strip("[]") for h in _LOCAL_HOSTS}:
        logger.warning("Rejected request with Host %r to %s", request.host, request.path)
        return Response("RepoDoctor only answers on 127.0.0.1.", status=403)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin != "null":
            if urllib.parse.urlsplit(origin).netloc != request.host:
                logger.warning("Rejected cross-origin POST from %s to %s", origin, request.path)
                return Response("Cross-site request blocked.", status=403)
    return None


@app.context_processor
def inject_preferences():
    """Make preferences + the model list available in all templates
    (Settings dropdown, tracker per-generation override).

    github_connected lets base.html warn, on every page, when the session
    outlived an app restart: the signed cookie still says authenticated and the
    last scan is restored from disk (so pages render), but the in-memory GitHub
    client is gone — so scans and AI generation would 401 with a confusing
    "not connected" error until the user logs in again."""
    return {
        "preferences": models.get_preferences(),
        "model_choices": ai.MODEL_CHOICES,
        "github_connected": _get_github_client() is not None,
        # Category tags flow through every template: a name→category map for
        # badges, plus the fixed list for the Categorize dropdowns.
        "repo_categories": models.get_repo_categories(),
        "category_groups": models.CATEGORY_GROUPS,
        # Age of the cached scan in whole days (None when there is no scan),
        # so base.html can nudge a re-scan once it's a couple of days old.
        "scan_age_days": _scan_age_days(),
    }


def _central_today() -> str:
    """Today's date in Central time (every screen shows Central; a UTC date
    stamped exports made after 7pm CDT with tomorrow's date)."""
    return datetime.datetime.now(_CENTRAL_TZ or datetime.timezone.utc).strftime("%Y-%m-%d")


def _scan_age_days() -> int | None:
    if not _scan_results or not _scan_results.get("scanned_at"):
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(_scan_results["scanned_at"]).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return max(0, (datetime.datetime.now(datetime.timezone.utc) - dt).days)


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

def _has_repo_scope(scopes: str) -> bool:
    """Exact 'repo' scope. A substring test let 'public_repo' pass, and then
    every private repo silently vanished from scans."""
    return "repo" in {s.strip() for s in (scopes or "").split(",")}


def _safe_next() -> str | None:
    """A same-site relative path from ?next= (or the form), else None. Rejects
    anything that could redirect off-site ('//evil', 'http://...')."""
    target = (request.form.get("next") or request.args.get("next") or "").strip()
    # Browsers read '\' as '/' and drop tabs/newlines, so '/\evil.com' and
    # '/\t/evil.com' become '//evil.com'. Reject backslashes and control or
    # whitespace characters outright, then require no scheme and no host.
    if not target.startswith("/") or "\\" in target or any(c.isspace() or ord(c) < 32 for c in target):
        return None
    parts = urllib.parse.urlsplit(target)
    if parts.scheme or parts.netloc or target.startswith("//"):
        return None
    return target.rstrip("?")


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
            if not _has_repo_scope(scopes):
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
            _check_anthropic_key_at_login(creds)
            _unlock_snapshot(password)
            return redirect(_safe_next() or url_for("dashboard"))
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
            if not _has_repo_scope(scopes):
                flash(f"GitHub PAT needs 'repo' scope. Current scopes: {scopes}", "error")
                return render_template("login.html", has_credentials=False)

            security.encrypt_credentials(password, github_pat, anthropic_key)
            creds = {"github_pat": github_pat, "anthropic_key": anthropic_key}
            _init_session(creds)
            session["authenticated"] = True
            session["github_user"] = user_info.get("login", "")
            flash(f"Welcome, {user_info.get('login', '')}! Credentials saved.", "success")
            _check_anthropic_key_at_login(creds, source="setup")
            return redirect(url_for("dashboard"))

    return render_template("login.html", has_credentials=has_creds)


@app.route("/logout")
def logout():
    global _github_client, _credentials
    # Only tear down the shared client/credentials for an actually-authenticated
    # request. Without this gate a cross-origin GET (e.g. <img src=".../logout">)
    # — which SameSite=Lax already strips the session cookie from, so it isn't
    # authenticated — could still null the process-global client and knock the
    # real user offline.
    if session.get("authenticated"):
        _github_client = None
        _credentials = None
        _lock_snapshot()
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


# Result of the last Anthropic key check for the key currently loaded in
# _credentials: {"status": "ok"|"invalid"|"unverified"|"", "message",
# "checked_at", "source": "login"|"setup"|"settings"}. Replaced whole (never
# mutated) so a reader on another thread always sees one consistent result.
_anthropic_key_status: dict = {"status": "", "message": "", "checked_at": "", "source": ""}

ANTHROPIC_KEY_FIX = (
    "AI features (project summaries, chat briefs, session recaps) will fail "
    "until this is fixed. To fix it: Settings → Credentials → Update "
    "Anthropic key, and paste a working key from console.anthropic.com."
)


def get_anthropic_key_status() -> dict:
    """Last key-check result (a copy). Status "" means not checked yet."""
    return dict(_anthropic_key_status)


def _record_anthropic_key_status(status: str, message: str, source: str) -> None:
    """Remember (and log) the check result for the key now loaded."""
    global _anthropic_key_status
    _anthropic_key_status = {
        "status": status,
        "message": message,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": source,
    }
    log = logger.info if status == ai.KEY_OK else logger.warning
    log("Anthropic key %s at %s: %s", status, source, message)


def _check_anthropic_key(api_key: str | None, source: str) -> tuple[str, str]:
    """Verify the loaded key and remember the result. Never logs the key."""
    status, message = ai.verify_anthropic_key(api_key)
    _record_anthropic_key_status(status, message, source)
    return status, message


def _check_anthropic_key_at_login(creds: dict, source: str = "login") -> None:
    """GitHub is the login gate; the Anthropic key is not. A rejected key
    warns now (instead of when the user first clicks GENERATE) but never
    blocks; a check that couldn't run (network) only logs."""
    status, message = _check_anthropic_key(creds.get("anthropic_key"), source)
    if status == ai.KEY_INVALID:
        flash(f"Anthropic API key problem: {message} {ANTHROPIC_KEY_FIX}", "warning")


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

def _filter_by_group(repos: list, active_group: str, groups: dict) -> list:
    """Scope a repo list to the active group (by repo name). Empty group = all.
    Shared by every list screen so one group selection filters the whole app."""
    if not active_group:
        return list(repos)
    members = set(groups.get(active_group, []))
    return [r for r in repos if r.get("name") in members]


# --- Health grade (Session 24 [S24-C]) ---
# One letter per repo (A-F, or "—" with a reason) from data the app already
# caches: the scan, the In Flight cache and the tags. No GitHub calls on page
# view. My Repos and Portfolio share it; events go to data/logs/health.log.
import health  # noqa: E402  (kept beside its only callers so parallel Session 24 work merges cleanly)


def _health_report(repos: list, groups: dict, log_as: str | None = None,
                   inflight_cache: dict | None = None) -> dict:
    """Grade every repo. Never raises: a grading bug shows "—", not a 500.
    log_as names the distribution event to log (None = don't log).
    inflight_cache: pass it when already loaded; None reads it."""
    if inflight_cache is None:
        inflight_cache = models.get_inflight()
    try:
        report = health.grade_scan(repos, inflight_cache, groups=groups)
    except Exception as e:  # noqa: BLE001 - the dashboard must render regardless
        logger.exception("health: grading the scan failed")
        models.log_health_event("grade_failed", repos=len(repos), error=f"{type(e).__name__}: {e}")
        return {"grades": {}, "distribution": health.distribution([]), "reasons": {},
                "errors": [], "inflight_used": False, "inflight_at": "", "attention": 0}
    for err in report["errors"]:
        models.log_health_event("grade_error", **err)
    report["inflight_at"] = (inflight_cache or {}).get("gathered_at", "") if report["inflight_used"] else ""
    report["attention"] = report["distribution"]["D"] + report["distribution"]["F"]
    if log_as and repos:
        models.log_health_event(
            log_as, repos=len(repos), distribution=report["distribution"],
            reasons=report["reasons"], inflight=report["inflight_used"],
            scanned_at=(_scan_results or {}).get("scanned_at", ""),
        )
    return report


@app.route("/")
@_require_auth
def dashboard():
    global _scan_results
    prefs = models.get_preferences()
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)
    all_repos = list(_scan_results.get("repos", []) if _scan_results else [])
    repos = _filter_by_group(all_repos, active_group, groups)
    # Graded over the whole scan (one distribution in the log per render);
    # the table shows the group-scoped rows.
    health_report = _health_report(all_repos, groups, log_as="dashboard")
    return render_template(
        "dashboard.html",
        scan_results=_scan_results,
        repos=repos,
        all_repos_count=len(all_repos),
        groups=groups,
        active_group=active_group,
        preferences=prefs,
        session_cost=_session_cost.to_dict(),
        health_for=lambda r: health_report["grades"].get(health.repo_key(r)),
        health_attention=health_report["attention"],
        health_inflight_at=health_report["inflight_at"],
    )


# Same width as the Stats page (activity.MAX_WORKERS) and below requests'
# default 10-connection pool per host, so workers never fight over sockets.
SCAN_MAX_WORKERS = 8


def _scan_error_entry(repo: dict, e: Exception) -> dict:
    """The row a repo gets when its scan fails: listed with zeros and the
    error, so one bad repo never sinks the whole scan."""
    return {
        "owner": repo["owner"]["login"],
        "name": repo["name"],
        "full_name": repo["full_name"],
        "default_branch": repo.get("default_branch", "main"),
        "private": repo.get("private", False),
        "archived": bool(repo.get("archived", False)),
        "fork": bool(repo.get("fork", False)),
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
    }


def _scan_repos_parallel(client, repos: list[dict]) -> list[dict]:
    """scan_repo_lite for every repo on SCAN_MAX_WORKERS threads. Returns one
    row per repo in the SAME order as `repos` (the later sort is stable, so
    order decides ties — the result is identical to a one-at-a-time loop).

    Thread-safe because scan_repo_lite only reads: each call uses its own
    locals and the client's GET helpers, which share one requests.Session
    (connection pool is thread-safe; headers are set once at construction and
    never mutated). A GitHubAuthError cancels every queued repo and
    propagates — one dead PAT means every repo would fail the same way."""
    if not repos:
        return []
    rows: list[dict | None] = [None] * len(repos)
    pool = ThreadPoolExecutor(max_workers=min(SCAN_MAX_WORKERS, len(repos)),
                              thread_name_prefix="scan")
    try:
        futures = {pool.submit(gh.scan_repo_lite, client, repo): i
                   for i, repo in enumerate(repos)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                rows[i] = fut.result()
            except gh.GitHubAuthError:
                raise
            except Exception as e:  # noqa: BLE001 - one repo must not sink the scan
                logger.warning("Scan: %s failed: %s: %s", repos[i].get("full_name", "?"),
                               type(e).__name__, str(e)[:200])
                rows[i] = _scan_error_entry(repos[i], e)
    finally:
        # On an auth error, drop the queued repos instead of letting each one
        # 401 in turn before the redirect can happen.
        pool.shutdown(wait=True, cancel_futures=True)
    return rows  # type: ignore[return-value] - every slot is filled above


@app.route("/scan", methods=["POST"])
@_require_auth
def scan():
    global _scan_results, _activity_cache, _activity_ranges
    _activity_cache = None
    _activity_ranges = {}
    client = _get_github_client()
    if not client:
        flash("Not authenticated with GitHub.", "error")
        return redirect(url_for("dashboard"))

    prefs = models.get_preferences()
    excluded = set(prefs.get("excluded_repos", []))

    # NOTE: GitHubAuthError (401 anywhere in the scan) propagates to the
    # global handler, which flashes GITHUB_AUTH_REMEDY and redirects.
    t0 = time.monotonic()
    repos = client.get_repos()
    to_scan = [r for r in repos
               if not (r["full_name"] in excluded or r["name"] in excluded)]
    results = _scan_repos_parallel(client, to_scan)
    elapsed = time.monotonic() - t0
    error_count = sum(1 for r in results if r.get("error"))
    logger.info("Scan finished: %d repos in %.1fs, %d errors (%d excluded, %d workers)",
                len(results), elapsed, error_count, len(repos) - len(to_scan),
                min(SCAN_MAX_WORKERS, len(to_scan)))

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
    models.log_action("scan", "all", "all",
                      f"Scanned {len(results)} repos, {_scan_results['total_branches']} total branches "
                      f"in {elapsed:.1f}s ({error_count} errors)")

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
    conversations = project_mapper.get_conversations_for_repo(
        name, [r["name"] for r in (_scan_results or {}).get("repos", [])])

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
            local_root = request.form.get("local_root", "~/claudesync2")
            requested_model = request.form.get("ai_model", ai.DEFAULT_MODEL)
            ai_model = requested_model if requested_model in ai.VALID_MODELS else ai.DEFAULT_MODEL
            display_mode = request.form.get("display_mode", "plain_english")
            excluded_raw = request.form.get("excluded_repos", "")
            excluded = [r.strip() for r in excluded_raw.split(",") if r.strip()]

            # Atomic get-modify-save under the store lock (returns the saved
            # prefs so downstream rendering stays consistent).
            def _apply(p):
                p["local_root"] = local_root
                p["ai_model"] = ai_model
                p["display_mode"] = display_mode
                p["excluded_repos"] = excluded
            prefs = models.update_preferences(_apply)
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

        elif action == "update_anthropic_key":
            _settings_update_anthropic_key()

        elif action == "check_anthropic_key":
            creds = _get_credentials()
            if not creds:
                flash(NOT_CONNECTED_REMEDY, "error")
            else:
                status, message = _check_anthropic_key(creds.get("anthropic_key"), "settings")
                if status == ai.KEY_OK:
                    flash("Anthropic key verified: AI features are ready.", "success")
                elif status == ai.KEY_INVALID:
                    flash(f"Anthropic API key problem: {message} {ANTHROPIC_KEY_FIX}", "error")
                else:
                    flash(f"Couldn't verify the Anthropic key right now: {message} "
                          "Try again in a minute.", "warning")

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
        app_log_tail=models.tail_app_log(150),
        app_log_path=models.APP_LOG_PATH,
        anthropic_key_status=get_anthropic_key_status(),
        snapshot_state=models.get_snapshot_state(),
        snapshot_configured=security.snapshot_secrets_exist(),
        snapshot_unlocked=_snapshot_secrets is not None,
        snapshot_min_len=_snapshot_min_len(),
    )


def _settings_update_anthropic_key() -> None:
    """Settings → Update Anthropic key. Needs the app password (the key is
    stored encrypted with it). The new key is checked BEFORE it is saved: a
    key Anthropic rejects (401) is not saved, so a typo can't replace a
    working key; a key that couldn't be checked (network) is saved with a
    warning. Flashes the outcome; never logs or flashes the key."""
    global _credentials
    new_key = request.form.get("anthropic_key", "").strip()
    password = request.form.get("password", "")
    if not new_key:
        flash("No Anthropic key entered. Paste the new key, then click SAVE KEY.", "error")
        return
    if not ai.looks_like_anthropic_key(new_key):
        flash("That doesn't look like an Anthropic API key (they start with sk-ant-). "
              "Nothing was changed.", "error")
        return
    creds = security.decrypt_credentials(password) if password else None
    if creds is None:
        logger.warning("Anthropic key update rejected: wrong or missing app password")
        flash("Wrong password. Your Anthropic key was not changed.", "error")
        return

    status, message = ai.verify_anthropic_key(new_key)
    if status == ai.KEY_INVALID:
        logger.warning("Anthropic key update rejected: %s", message)
        models.log_action("anthropic_key", "all", "all", f"Settings update rejected: {message}")
        flash(f"Not saved: {message} Your previous key is unchanged. "
              "Copy a fresh key from console.anthropic.com and try again.", "error")
        return
    if status != ai.KEY_OK and not request.form.get("save_unverified"):
        # Couldn't reach Anthropic: saving now could replace a working key with
        # a typo nobody checked. Only on an explicit "save anyway".
        logger.warning("Anthropic key update held: could not verify (%s)", message)
        models.log_action("anthropic_key", "all", "all", f"Settings update held, unverified: {message}")
        flash(f"Not saved yet: couldn't check the new key with Anthropic ({message}) "
              "Your previous key is unchanged. Try again in a minute, or tick "
              "\"Save even if it can't be checked\" to save it anyway.", "warning")
        return

    try:
        security.encrypt_credentials(password, creds["github_pat"], new_key)
    except OSError as e:  # the write is atomic: on failure the old file is intact
        logger.error("Anthropic key update: could not save credentials: %s: %s", type(e).__name__, e)
        flash(f"Couldn't save the new key ({type(e).__name__}: {e}). "
              "Your previous key is unchanged.", "error")
        return
    if _credentials is not None:
        _credentials = {**_credentials, "anthropic_key": new_key}
    _record_anthropic_key_status(status, message, "settings")
    logger.info("Anthropic key updated in Settings (check: %s)", status)
    models.log_action("anthropic_key", "all", "all", f"Settings update saved (check: {status})")
    if status == ai.KEY_OK:
        flash("Anthropic key saved and verified: AI features are ready.", "success")
    else:
        flash(f"Anthropic key saved without a check (you chose save anyway): {message} "
              "Use CHECK KEY in a minute, or it will be checked at your next login.", "warning")


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
            # Atomic get-modify-save so a concurrent prefs write (another tab)
            # doesn't clobber this active_group update, or vice versa.
            models.update_preferences(lambda p: p.__setitem__("active_group", active_group))
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

    existing = models.get_groups()
    if not original and name in existing:
        # The CREATE form has no original_name: without this check it silently
        # replaced an existing group's members.
        flash(f'A group named "{name}" already exists. Edit it instead, or pick a different name.', "error")
        return redirect(url_for("projects"))
    if original and original != name:
        if name in existing:
            flash(f'A group named "{name}" already exists. Pick a different name.', "error")
            return redirect(url_for("projects"))
        if original in models.CATEGORY_GROUPS:
            flash(f'"{original}" is a category and can\'t be renamed.', "error")
            return redirect(url_for("projects"))
        if not models.rename_group(original, name):
            flash("Could not rename group.", "error")
            return redirect(url_for("projects"))

    # The editor only shows checkboxes for scanned repos. Keep members that
    # aren't in the scan (not created yet, excluded) instead of dropping them.
    scanned = {r["name"] for r in (_scan_results or {}).get("repos", [])}
    kept = [m for m in existing.get(original or name, []) if m not in scanned]
    repos = list(dict.fromkeys(repos + kept))
    models.set_group(name, repos)
    flash(f'Group "{name}" saved with {len(repos)} project(s).', "success")
    return redirect(url_for("projects", group=name))


@app.route("/projects/groups/delete", methods=["POST"])
@_require_auth
def delete_group_route():
    # Prefer original_name (hidden field) so an in-flight edit to the
    # editable name field can't redirect the delete at the wrong group.
    name = (request.form.get("original_name") or request.form.get("group_name") or "").strip()
    if name in models.CATEGORY_GROUPS:
        flash(f'"{name}" is a category and can\'t be deleted. Clear its projects instead.', "error")
        return redirect(url_for("projects"))
    if models.delete_group(name):
        flash(f'Group "{name}" deleted.', "success")
    else:
        flash("Group not found.", "error")
    return redirect(url_for("projects"))


@app.route("/api/categorize", methods=["POST"])
@_require_auth_api
def set_category():
    """Assign one category (Church/School/Combined/Personal, or empty to clear)
    to a repo. Driven by the per-repo dropdown on the Projects Categorize panel,
    saved instantly so tagging a whole portfolio never needs a page reload."""
    data = request.json or {}
    repo = (data.get("repo") or "").strip()
    category = (data.get("category") or "").strip()
    if not repo:
        return jsonify({"error": "Missing repo."}), 400
    if not models.set_repo_category(repo, category or None):
        return jsonify({"error": f"Unknown category '{category}'."}), 400
    models.log_action("set_category", repo, "all", category or "(cleared)")
    return jsonify({"ok": True, "repo": repo, "category": category})


# Enforced via structured outputs so the model cannot answer with prose —
# the cause of the old "No JSON object found in AI response" failures.
PROJECT_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "what_it_does": {
            "type": "string",
            "description": "1-2 sentence plain-English description of what this project does",
        },
        "how_finished": {
            "type": "string",
            "description": "1-2 sentence assessment of how complete/finished the project is",
        },
        "next_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 5 short bullets: what needs to be built or tested next",
        },
    },
    "required": ["what_it_does", "how_finished", "next_steps"],
    "additionalProperties": False,
}


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
        models.log_summary_event("generate_skipped", repo=name, reason="no docs or description")
        return summary

    context_text = "\n\n".join(context_parts)

    # Honor the Settings model preference — briefs and trackers already do.
    model = models.get_ai_model()

    ai_client = anthropic.Anthropic(api_key=creds["anthropic_key"])
    summary, usage = ai.request_json(
        ai_client,
        model=model,
        max_tokens=800,
        schema=PROJECT_SUMMARY_SCHEMA,
        messages=[{
            "role": "user",
            "content": (
                f"Project: {name}\n\n{context_text}\n\n"
                "Based on the above, write the project summary JSON: "
                "what_it_does, how_finished, and up to 5 next_steps."
            ),
        }],
    )
    # Ensure next_steps is capped at 5
    if "next_steps" in summary and len(summary["next_steps"]) > 5:
        summary["next_steps"] = summary["next_steps"][:5]
    models.save_project_summary(name, summary)

    in_tok = usage["input_tokens"]
    out_tok = usage["output_tokens"]
    _session_cost.add(in_tok, out_tok, ai.estimate_cost(in_tok, out_tok, model=model))
    models.log_summary_event(
        "generate_done", repo=name, model=model,
        input_tokens=in_tok, output_tokens=out_tok,
        docs=sorted(fetched["docs"].keys()),
        next_steps=len(summary.get("next_steps") or []),
    )
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
        # 300 chars so request_json's "the model replied: ..." diagnostic
        # snippet survives into the log and the Projects-page error line.
        detail = f"{type(e).__name__}: {str(e)[:300]}"
        models.log_summary_event("generate_error", repo=name, error=detail)
        models.log_action("generate_summary_error", name, "all", str(e)[:300])
        return jsonify({"error": detail}), 502

    models.log_action("generate_summary", name, "all", "ok")
    return jsonify({"ok": True, "repo": name})


# --- Repo Selector ---
#
# One screen to pick from EVERY repo (personal, private, public, and org repos
# now that the scan's affiliation includes organization_member), each tagged
# PUBLIC/PRIVATE, each with a three-sentence plain-English blurb. You tick the
# repos you want and copy a clean summary sheet to paste into another document.

# Structured output so the model returns a single clean paragraph, never prose
# framing or a JSON-in-prose reply (the failure the Projects schema also fixes).
SELECTOR_BLURB_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "Exactly three sentences, plain English, describing what this "
                "project is, who it's for, and roughly how far along it is. No "
                "bullet points, no headings, no markdown — just three sentences."
            ),
        },
    },
    "required": ["summary"],
    "additionalProperties": False,
}


def _find_repo_by_full_name(full_name: str) -> dict | None:
    """Find a repo in the latest scan by 'owner/name'. Full-name lookup (not
    bare name) so an org repo never collides with a personal repo of the same
    name — the collision the scan's new organization_member affiliation makes
    real."""
    if not _scan_results:
        return None
    for r in _scan_results.get("repos", []):
        if r.get("full_name") == full_name:
            return r
    return None


def _generate_blurb_for_repo(client, creds: dict, repo: dict) -> dict:
    """Generate + save the Selector's three-sentence blurb for one repo.
    Falls back to the README when the repo has none of Chris's spec files, so
    org and public repos still get a real summary. Raises on AI failure — an
    existing good blurb is never overwritten with an error."""
    owner = repo["owner"]
    name = repo["name"]
    full_name = repo.get("full_name", f"{owner}/{name}")
    ref = repo.get("default_branch", "main")

    # Spec docs first; README as a fallback so a repo without PRODUCT_SPEC etc.
    # (most org/public repos) still has something to summarize from.
    fetched = gh.fetch_repo_docs(
        client, owner, name, ref=ref, max_chars=5000, include_readme="if_no_docs",
    )

    context_parts = []
    if repo.get("description"):
        context_parts.append(f"GitHub description: {repo['description']}")
    for key, content in fetched["docs"].items():
        context_parts.append(f"--- {key.upper()} ---\n{content}")
    if fetched.get("readme"):
        context_parts.append(f"--- README ---\n{fetched['readme']}")

    if not context_parts:
        # Nothing to read — save a minimal, honest blurb rather than inventing.
        blurb = {
            "summary": (
                f"{name} is a GitHub repository with no README, spec files, or "
                "description to summarize from. What it does can't be determined "
                "from its contents yet. Add a README or description to get a real "
                "summary here."
            ),
        }
        models.save_selector_blurb(full_name, blurb)
        models.log_selector_event("generate_skipped", repo=full_name, reason="no docs, readme, or description")
        return blurb

    context_text = "\n\n".join(context_parts)
    model = models.get_ai_model()

    ai_client = anthropic.Anthropic(api_key=creds["anthropic_key"])
    blurb, usage = ai.request_json(
        ai_client,
        model=model,
        max_tokens=400,
        schema=SELECTOR_BLURB_SCHEMA,
        messages=[{
            "role": "user",
            "content": (
                f"Project: {name}\n\n{context_text}\n\n"
                "Write a summary of this project in EXACTLY three sentences: "
                "what it is, who it's for, and roughly how finished it is. "
                "Plain English, no jargon, no markdown."
            ),
        }],
    )
    models.save_selector_blurb(full_name, blurb)

    in_tok = usage["input_tokens"]
    out_tok = usage["output_tokens"]
    _session_cost.add(in_tok, out_tok, ai.estimate_cost(in_tok, out_tok, model=model))
    models.log_selector_event(
        "generate_done", repo=full_name, model=model,
        input_tokens=in_tok, output_tokens=out_tok,
        docs=sorted(fetched["docs"].keys()),
        used_readme=bool(fetched.get("readme")),
    )
    return blurb


@app.route("/selector")
@_require_auth
def repo_selector():
    blurbs = models.get_selector_blurbs()
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)

    all_repos = list(_scan_results.get("repos", []) if _scan_results else [])
    # Most recently pushed first; missing/blank dates sink to the bottom.
    all_repos.sort(key=briefing.last_push_ts, reverse=True)
    scoped = _filter_by_group(all_repos, active_group, groups)

    # Attach each repo's blurb (by full_name) so the template stays dumb.
    repos = []
    for r in scoped:
        full_name = r.get("full_name", f"{r.get('owner')}/{r.get('name')}")
        blurb = blurbs.get(full_name)
        repos.append({**r, "full_name": full_name, "blurb": blurb})

    private_count = sum(1 for r in repos if r.get("private"))
    return render_template(
        "selector.html",
        repos=repos,
        scan_results=_scan_results,
        groups=groups,
        active_group=active_group,
        all_repos_count=len(all_repos),
        blurbed_count=sum(1 for r in repos if r["blurb"]),
        private_count=private_count,
        public_count=len(repos) - private_count,
    )


@app.route("/api/selector/blurb/<owner>/<name>", methods=["POST"])
@_require_auth_api
def generate_selector_blurb_one(owner, name):
    """Generate the three-sentence blurb for ONE repo. The Selector page drives
    this per selected repo with live progress, so a 60-repo portfolio never
    blocks one request and one failure never kills the batch."""
    client = _get_github_client()
    creds = _get_credentials()
    if not client or not creds:
        return jsonify({"error": NOT_CONNECTED_REMEDY}), 401

    full_name = f"{owner}/{name}"
    repo = _find_repo_by_full_name(full_name)
    if not repo:
        return jsonify({"error": f"Repo '{full_name}' not in the latest scan. Run a scan first."}), 404

    force = bool((request.json or {}).get("force")) if request.is_json else False
    existing = models.get_selector_blurbs().get(full_name)
    if existing and not force:
        return jsonify({"ok": True, "repo": full_name, "skipped": "exists"})

    try:
        _generate_blurb_for_repo(client, creds, repo)
    except gh.GitHubAuthError:
        return jsonify({"error": GITHUB_AUTH_REMEDY}), 401
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e)[:300]}"
        models.log_selector_event("generate_error", repo=full_name, error=detail)
        models.log_action("generate_selector_blurb_error", name, "all", str(e)[:300])
        return jsonify({"error": detail}), 502

    models.log_action("generate_selector_blurb", name, "all", "ok")
    return jsonify({"ok": True, "repo": full_name})


# --- Stats (commits, code size, and What changed — any date range) ---
#
# Every number on the Stats page comes from cached GitHub reads (see
# activity.py): default-branch commits + merged PRs for every scanned repo.
#   - The MAIN cache reads back to activity.default_floor (90 days), so Week,
#     Month and Quarter are local filters over one read. YTD later in the year
#     and custom ranges that run through today widen it once.
#   - A custom range that ends in the past and starts before the main cache
#     gets its own BOUNDED read (since -> until), kept in a small LRU, so a
#     busy repo's recent commits can't crowd the old range out of a capped read.
# Caches are keyed by scan, expire hourly (REFRESH forces a re-read) and are
# dropped on every scan. A repo whose read failed is retried on a later view,
# but no sooner than ACTIVITY_RETRY_BACKOFF_SECONDS, so a rate-limited repo
# can't stall every click behind GitHub's Retry-After.

_activity_cache: dict | None = None
_activity_ranges: dict = {}
_ACTIVITY_RANGE_SLOTS = 6
# Serializes fetches: a second tab (or a double-click) waits for the first
# fetch and then reads its cache instead of doubling the GitHub calls.
_activity_lock = threading.Lock()
ACTIVITY_CACHE_TTL_SECONDS = 3600
ACTIVITY_RETRY_BACKOFF_SECONDS = 300
# The only query args /stats understands; REFRESH's redirect carries these
# and nothing else (arbitrary args into url_for could 500 or redirect away).
_STATS_ARGS = ("range", "from", "to", "group")


def _activity_scan_key(scan: dict | None) -> str:
    """Identity of the scan a cache was built from; a new scan changes it."""
    if not scan:
        return ""
    repos = scan.get("repos", [])
    first = repos[0].get("full_name", "") if repos else ""
    return f"{scan.get('scanned_at', '')}|{len(repos)}|{first}"


def _repo_full_name(r: dict) -> str:
    return r.get("full_name") or f"{r['owner']}/{r['name']}"


def _activity_fresh(entry: dict | None) -> bool:
    return bool(entry) and (time.time() - entry["fetched_ts"]) < ACTIVITY_CACHE_TTL_SECONDS


def _activity_fetch(client, repos: list[dict], key: str, since_date, until_iso=None,
                    carry: dict | None = None) -> dict:
    """Read `repos` from `since_date`. `carry` maps full_name -> (error,
    retry_at) for repos still inside their retry backoff: they are not read
    again (a rate-limited repo would stall this read behind Retry-After for
    nothing) and keep their failure until the backoff ends."""
    carry = carry or {}
    since_iso = activity._local_midnight(since_date).astimezone(datetime.timezone.utc).isoformat()
    todo = [r for r in repos if _repo_full_name(r) not in carry]
    data, errors = activity.gather(client, todo, since_iso, models.log_activity_event,
                                   until_iso=until_iso)
    now = time.time()
    retry_at = {k: now + ACTIVITY_RETRY_BACKOFF_SECONDS for k in errors}
    for k, (err, at) in carry.items():
        errors[k], retry_at[k] = err, at
    return {
        "key": key, "floor": since_date, "since_iso": since_iso, "until_iso": until_iso,
        "fetched_ts": now,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "data": data, "errors": errors, "retry_at": retry_at,
    }


def _activity_heal(client, repos: list[dict], entry: dict) -> dict:
    """Re-read only the repos whose last read failed, once their backoff has
    passed. Returns a new entry (never mutates one another request reads)."""
    now = time.time()
    due = [r for r in repos if _repo_full_name(r) in entry["errors"]
           and now >= entry["retry_at"].get(_repo_full_name(r), 0)]
    if not due:
        return entry
    got, errs = activity.gather(client, due, entry["since_iso"], models.log_activity_event,
                                until_iso=entry["until_iso"])
    errors = {k: v for k, v in entry["errors"].items() if k not in got}
    errors.update(errs)
    retry_at = {k: v for k, v in entry["retry_at"].items() if k in errors}
    retry_at.update({k: now + ACTIVITY_RETRY_BACKOFF_SECONDS for k in errs})
    return {**entry, "data": {**entry["data"], **got}, "errors": errors, "retry_at": retry_at}


def _activity_result(entry: dict, cache_hit: bool, stale: bool) -> dict:
    return {"data": entry["data"], "errors": entry["errors"], "fetched_at": entry["fetched_at"],
            "cache_hit": cache_hit, "stale": stale}


def _activity_data(client, scan: dict, rng, force: bool = False) -> dict | None:
    """Cached commits + merged PRs for every repo in `scan`, covering `rng`.

    `scan` is the caller's one snapshot of _scan_results, used for both the
    repo list and the cache key, so a scan finishing mid-request can't key a
    cache that's missing its new repos. Returns {"data", "errors",
    "fetched_at", "cache_hit", "stale"}, or None when there is no usable
    cache and no GitHub client (app restarted)."""
    global _activity_cache
    key = _activity_scan_key(scan)
    repos = list(scan.get("repos", []))
    today = activity._local_now().date()

    with _activity_lock:
        main = _activity_cache if (_activity_cache and _activity_cache["key"] == key) else None
        main_covers = main is not None and main["floor"] <= rng.start_date

        if rng.end_date < today and not main_covers:
            rkey = (key, rng.start_date, rng.end_date)
            entry = _activity_ranges.pop(rkey, None)
            if not client:
                if entry:
                    _activity_ranges[rkey] = entry
                    return _activity_result(entry, True, not _activity_fresh(entry))
                return None
            if force or not _activity_fresh(entry):
                entry, hit = _activity_fetch(client, repos, key, rng.start_date, rng.until_iso), False
            else:
                entry, hit = _activity_heal(client, repos, entry), True
            _activity_ranges[rkey] = entry  # most recently used goes last
            while len(_activity_ranges) > _ACTIVITY_RANGE_SLOTS:
                _activity_ranges.pop(next(iter(_activity_ranges)))
            return _activity_result(entry, hit, False)

        if not client:
            return _activity_result(main, True, not _activity_fresh(main)) if main_covers else None

        if main_covers and _activity_fresh(main) and not force:
            _activity_cache = _activity_heal(client, repos, main)
            return _activity_result(_activity_cache, True, False)

        floor = min(activity.default_floor(today), rng.start_date)
        carry = {}
        if main is not None:
            floor = min(floor, main["floor"])  # a re-read keeps the reach it had
            if not force:  # REFRESH is an explicit "read everything now"
                now = time.time()
                carry = {k: (v, main["retry_at"][k]) for k, v in main["errors"].items()
                         if main["retry_at"].get(k, 0) > now}
        _activity_cache = _activity_fetch(client, repos, key, floor, carry=carry)
        return _activity_result(_activity_cache, False, False)


def _stats_report(scan: dict, result: dict, active_group: str, groups: dict, rng):
    scoped = _filter_by_group(list(scan.get("repos", [])), active_group, groups)
    return activity.build_report(scoped, result["data"], result["errors"], rng)


def _stats_attach_ai(report: dict, active_group: str, model: str) -> None:
    """Attach cached "What we shipped" paragraphs (fresh or STALE) to the
    report. An unreadable summaries file must never take the Stats page down:
    log it and show the page as if nothing was summarized yet."""
    try:
        store = models.get_activity_summaries()
    except Exception as e:  # noqa: BLE001 - the page matters more than the cache
        logger.warning("stats: could not read activity summaries: %s", e)
        models.log_activity_event("summaries_load_error", error=f"{type(e).__name__}: {str(e)[:200]}")
        store = {"repos": {}, "portfolio": {}}
    try:
        activity.attach_summaries(report, store, model, active_group)
    except Exception as e:  # noqa: BLE001 - odd cached data must not 500 Stats / Review
        logger.warning("stats: could not attach activity summaries: %s", e)
        models.log_activity_event("summaries_attach_error", error=f"{type(e).__name__}: {str(e)[:200]}")
        for r in report.get("rows", []):
            r["ai"] = None
        report["ai_portfolio"] = None
        report["ai_targets"] = {"all": [], "needs": [], "portfolio_fresh": False, "fingerprint": ""}


def _stats_markdown(report: dict, active_group: str) -> str:
    return activity.compose_markdown(
        report, session.get("github_user", ""), active_group,
        generated_at=sessions._local_time_label(
            datetime.datetime.now(datetime.timezone.utc).isoformat()))


@app.route("/stats")
@_require_auth
def stats():
    scan = _scan_results  # one snapshot for the whole request
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)
    rng, notice = activity.resolve_range(request.args)
    if notice:
        models.log_activity_event("range_adjusted", args=request.args.to_dict(), notice=notice)
    all_repos = list(scan.get("repos", []) if scan else [])
    ctx = dict(groups=groups, active_group=active_group, rng=rng, notice=notice,
               presets=activity.PRESETS, today=activity._local_now().date().isoformat(),
               all_repos_count=len(all_repos), scan_results=scan)

    if not scan:
        return render_template("stats.html", **ctx)

    t0 = time.monotonic()
    force = request.args.get("refresh") == "1"
    result = _activity_data(_get_github_client(), scan, rng, force=force)
    if force:
        # Post/Redirect/Get: a browser reload after REFRESH must not refetch.
        # Carry only the args /stats understands.
        keep = {k: request.args[k] for k in _STATS_ARGS if k in request.args}
        return redirect(url_for("stats", **keep))
    if result is None:
        return render_template("stats.html", not_connected=True, **ctx)

    report = _stats_report(scan, result, active_group, groups, rng)
    model = models.get_ai_model()
    _stats_attach_ai(report, active_group, model)
    chart_rows = [{
        "name": r["name"], "html_url": r["html_url"], "commits": r["commit_count"],
        "prs": r["pr_count"], "code_size_bytes": r["code_size_bytes"],
        "error": bool(r["error"]),
    } for r in report["rows"]]
    t = report["totals"]
    models.log_activity_event(
        "view", range=rng.key, group=active_group, cache_hit=result["cache_hit"],
        stale=result["stale"], repos=t["repos_total"], updated=t["repos_updated"],
        commits=t["commits"], prs=t["prs_merged"], errors=t["errors"],
        incomplete=t["incomplete"], elapsed_ms=int((time.monotonic() - t0) * 1000))
    return render_template(
        "stats.html", report=report, chart_rows=chart_rows,
        markdown=_stats_markdown(report, active_group),
        fetched_at=result["fetched_at"], stale=result["stale"],
        card_commit_limit=activity.CARD_COMMIT_LIMIT,
        ai_cost_each=ai.estimate_cost(*_STATS_AI_TYPICAL_TOKENS, model=model),
        ai_model_label=_stats_model_label(model), **ctx)


@app.route("/stats/export.md")
@_require_auth
def stats_export():
    """The same document COPY FOR CLAUDE produces, as a .md download."""
    scan = _scan_results
    rng, _notice = activity.resolve_range(request.args)
    groups = models.get_groups()
    active_group = _resolve_active_group(groups, persist=False)
    back = url_for("stats", group=active_group or None, **rng.query_args())
    if not scan:
        flash("No scan data yet — run a scan first.", "error")
        return redirect(back)
    result = _activity_data(_get_github_client(), scan, rng)
    if result is None:
        flash(NOT_CONNECTED_REMEDY, "error")
        return redirect(back)
    report = _stats_report(scan, result, active_group, groups, rng)
    _stats_attach_ai(report, active_group, models.get_ai_model())
    models.log_activity_event("export", range=rng.key, group=active_group,
                              updated=report["totals"]["repos_updated"])
    filename = f"what-changed-{rng.start_date.isoformat()}-to-{rng.end_date.isoformat()}.md"
    return Response(
        _stats_markdown(report, active_group),
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Stats → What we shipped (on-demand AI paragraphs) ---
#
# SUMMARIZE on the Stats page posts once per updated repo, then once for the
# overview, so progress is visible and one failure never sinks the batch.
# Both endpoints read the SAME cached GitHub data the page shows
# (_activity_data — no new fetching), re-check the content fingerprint so a
# current paragraph is never paid for twice (unless force), add the cost to
# the session total, and log every generation, skip and failure to
# data/logs/activity.log. The API key is never logged.

# Typical tokens for one repo paragraph (in, out), for the page's cost hint:
# ~$0.005 each on Haiku.
_STATS_AI_TYPICAL_TOKENS = (3500, 250)


def _stats_model_label(model: str) -> str:
    """'Haiku 4.5' from the Settings model list, for the cost hint."""
    for model_id, label in ai.MODEL_CHOICES:
        if model_id == model:
            return label.split(" — ")[0]
    return model


def _stats_ai_error(e: Exception) -> str:
    """A sentence the SUMMARIZE overlay can show for an AI failure."""
    if isinstance(e, anthropic.AuthenticationError):
        return ("Anthropic rejected the API key (401). Fix it in Settings → Credentials → "
                "Update Anthropic key (your GitHub token stays as it is).")
    if isinstance(e, anthropic.PermissionDeniedError):
        return ("Anthropic refused the request (403); the key may not have access to this "
                "model. Pick another model in Settings and retry.")
    if isinstance(e, anthropic.NotFoundError):
        return "Anthropic doesn't recognize the model chosen in Settings (404). Pick another and retry."
    if isinstance(e, anthropic.RateLimitError):
        return ("Anthropic's rate limit was hit (429). Wait a minute and press SUMMARIZE "
                "again; finished paragraphs are kept.")
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach Anthropic. Check your connection and retry."
    if isinstance(e, anthropic.BadRequestError):
        # Retrying won't fix a 400; Anthropic's own reason is the useful part.
        return f"Anthropic rejected the request (400): {str(getattr(e, 'message', e))[:160]}"
    if isinstance(e, anthropic.APIStatusError):
        return f"Anthropic returned an error ({e.status_code}). Retry in a minute."
    if isinstance(e, ValueError):
        return str(e)[:300]
    return f"{type(e).__name__}: {str(e)[:200]}"


def _stats_redact(text: str, creds: dict) -> str:
    """Belt and braces: an exception string must never carry the API key
    into the log file."""
    # ai._redact also catches any sk-ant-… shaped token, so a key that a
    # caller truncated mid-way still can't leak a usable prefix.
    return ai._redact(text, (creds or {}).get("anthropic_key") or "")


def _stats_ai_context():
    """Shared setup for both SUMMARIZE endpoints: (ctx, None) on success, or
    (None, (json response, status)) when there's no scan (404), no API key or
    GitHub data (503 — the app restarted since login)."""
    scan = _scan_results  # one snapshot for the whole request
    if not scan:
        return None, (jsonify({"error": "No scan data yet. Run a scan from My Repos first."}), 404)
    creds = _get_credentials()
    if not creds or not creds.get("anthropic_key"):
        return None, (jsonify({"error": NOT_CONNECTED_REMEDY}), 503)
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    args = {k: str(body[k]) for k in ("range", "from", "to") if body.get(k) not in (None, "")}
    rng, _notice = activity.resolve_range(args)
    groups = models.get_groups()
    if "group" in body:
        group = body["group"] if isinstance(body["group"], str) and body["group"] in groups else ""
    else:
        group = _resolve_active_group(groups, persist=False)
    result = _activity_data(_get_github_client(), scan, rng)
    if result is None:
        return None, (jsonify({"error": NOT_CONNECTED_REMEDY}), 503)
    return {"scan": scan, "creds": creds, "rng": rng, "groups": groups, "group": group,
            "force": body.get("force") in (True, 1, "1", "true"), "result": result,
            "model": models.get_ai_model()}, None


def _stats_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


@app.route("/api/stats/summarize/<owner>/<name>", methods=["POST"])
@_require_auth_api
def stats_summarize_repo(owner, name):
    """Write (or reuse) ONE repo's plain-English paragraph for the range in
    the JSON body ({range, from, to, group, force})."""
    t0 = time.monotonic()
    ctx, err = _stats_ai_context()
    if err:
        return err
    full = f"{owner}/{name}"
    rng, model = ctx["rng"], ctx["model"]
    repo = next((r for r in ctx["scan"].get("repos", []) if _repo_full_name(r) == full), None)
    if not repo:
        return jsonify({"error": f"{full} isn't in the latest scan. Run a scan, then reload Stats."}), 404
    row = activity.build_report([repo], ctx["result"]["data"], ctx["result"]["errors"], rng)["rows"][0]
    if row["error"]:
        models.log_activity_event("summarize_error", repo=full, range=rng.key, model=model,
                                  error=f"GitHub read failed: {row['error']}", elapsed_ms=_stats_ms(t0))
        return jsonify({"error": f"GitHub couldn't be read for {name} ({row['error'][:120]}), so "
                                 f"there's nothing to summarize yet. Try again in a few minutes."}), 502
    if not row["updated"]:
        return jsonify({"ok": True, "skipped": "nothing"})
    fp = activity.repo_fingerprint(row, model)
    if not ctx["force"]:
        hit = models.get_activity_summaries()["repos"].get(fp)
        if hit and hit.get("text") and hit.get("repo") == full:
            models.log_activity_event("summarize_skip", repo=full, range=rng.key, fingerprint=fp,
                                      reason="fresh")
            return jsonify({"ok": True, "skipped": "fresh", "fingerprint": fp})
    try:
        out = activity.summarize_repo(ctx["creds"]["anthropic_key"], row, model=model)
    except Exception as e:  # noqa: BLE001 - any AI failure is a 502 with a readable message
        models.log_activity_event(
            "summarize_error", repo=full, range=rng.key, fingerprint=fp, model=model,
            forced=ctx["force"], elapsed_ms=_stats_ms(t0),
            error=_stats_redact(f"{type(e).__name__}: {str(e)}", ctx["creds"])[:340])
        return jsonify({"error": _stats_ai_error(e)}), 502

    usage = out.pop("_usage", None) or {}
    in_tok, out_tok = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    cost = ai.estimate_cost(in_tok, out_tok, model=model)
    _session_cost.add(in_tok, out_tok, cost)
    try:
        models.save_activity_repo_summary(fp, {
            "repo": full, "slot": activity.range_slot(rng), "range_key": rng.key, "model": model,
            "text": out["text"], "items": out["items"], "total_items": out["total_items"],
            "capped": out["capped"], "input_tokens": in_tok, "output_tokens": out_tok, "cost": cost,
        })
    except OSError as e:
        models.log_activity_event("summarize_error", repo=full, range=rng.key, fingerprint=fp,
                                  error=f"save failed: {e}", elapsed_ms=_stats_ms(t0))
        return jsonify({"error": f"The paragraph was written but couldn't be saved ({e})."}), 500
    models.log_activity_event(
        "summarize", repo=full, range=rng.key, fingerprint=fp, model=model, forced=ctx["force"],
        items=out["items"], total_items=out["total_items"], capped=out["capped"],
        input_tokens=in_tok, output_tokens=out_tok, cost=cost, elapsed_ms=_stats_ms(t0))
    return jsonify({"ok": True, "repo": full, "fingerprint": fp, "cost": cost})


@app.route("/api/stats/summarize-portfolio", methods=["POST"])
@_require_auth_api
def stats_summarize_portfolio():
    """Write (or reuse) the overview paragraph for the range + group in the
    JSON body, built from each repo's current paragraph (else its titles)."""
    t0 = time.monotonic()
    ctx, err = _stats_ai_context()
    if err:
        return err
    rng, model, group = ctx["rng"], ctx["model"], ctx["group"]
    report = _stats_report(ctx["scan"], ctx["result"], group, ctx["groups"], rng)
    if not report["updated"]:
        return jsonify({"ok": True, "skipped": "nothing"})
    _stats_attach_ai(report, group, model)
    fp = report["ai_targets"]["fingerprint"]
    if report["ai_targets"]["portfolio_fresh"] and not ctx["force"]:
        models.log_activity_event("summarize_skip", repo="(overview)", group=group, range=rng.key,
                                  fingerprint=fp, reason="fresh")
        return jsonify({"ok": True, "skipped": "fresh", "fingerprint": fp})
    notes, total = activity.portfolio_inputs(report)
    from_paragraphs = sum(1 for n in notes if n.get("summary"))
    try:
        out = activity.summarize_portfolio(ctx["creds"]["anthropic_key"], notes, total, group, model=model)
    except Exception as e:  # noqa: BLE001
        models.log_activity_event(
            "summarize_error", repo="(overview)", group=group, range=rng.key, fingerprint=fp,
            model=model, forced=ctx["force"], elapsed_ms=_stats_ms(t0),
            error=_stats_redact(f"{type(e).__name__}: {str(e)}", ctx["creds"])[:340])
        return jsonify({"error": _stats_ai_error(e)}), 502

    usage = out.pop("_usage", None) or {}
    in_tok, out_tok = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    cost = ai.estimate_cost(in_tok, out_tok, model=model)
    _session_cost.add(in_tok, out_tok, cost)
    try:
        models.save_activity_portfolio_summary(fp, {
            "group": group, "slot": activity.range_slot(rng), "range_key": rng.key, "model": model,
            "text": out["text"], "repos": total, "listed": len(notes),
            "from_paragraphs": from_paragraphs, "input_tokens": in_tok, "output_tokens": out_tok,
            "cost": cost,
        })
    except OSError as e:
        models.log_activity_event("summarize_error", repo="(overview)", group=group, range=rng.key,
                                  fingerprint=fp, error=f"save failed: {e}", elapsed_ms=_stats_ms(t0))
        return jsonify({"error": f"The overview was written but couldn't be saved ({e})."}), 500
    models.log_activity_event(
        "summarize_portfolio", group=group, range=rng.key, fingerprint=fp, model=model,
        forced=ctx["force"], repos=total, listed=len(notes), from_paragraphs=from_paragraphs,
        input_tokens=in_tok, output_tokens=out_tok, cost=cost, elapsed_ms=_stats_ms(t0))
    return jsonify({"ok": True, "fingerprint": fp, "cost": cost})


# --- What's Next (aggregated across repos) ---

@app.route("/whats-next")
@_require_auth
def whats_next_all():
    """Merged into Next Up (the Yesterday screen), which now carries the
    planned next-steps alongside the live session reconstruction. Kept as a
    permanent redirect so old bookmarks and links don't 404."""
    return redirect(url_for("yesterday_view"))


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
    generated_label = _central_today()
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
    date_part = _central_today()
    # Slug only: a raw group name ("Ministry ✝", 'Big "A"') can't be encoded
    # into the header (the request hung) or breaks the quoted filename.
    slug = re.sub(r"[^a-z0-9]+", "-", active_group.lower()).strip("-") if active_group else ""
    group_part = f"-{slug}" if slug else ""
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


# --- Yesterday (cross-repo session recap) ---
#
# "What was I doing yesterday, and where did I leave off?" Each Claude Code web
# session is a claude/<slug> branch, so this screen reconstructs the day's work
# across every repo from GitHub commits + PR state, broken out by session, with
# a deterministic "what's next" call and an on-demand plain-English AI recap.
# Unlike the Briefing screen this gathers LIVE from GitHub on each visit (the
# whole point is freshness); every gather + generation logs to
# data/logs/sessions.log for paste-back debugging.

_RANGE_PRESETS = [
    ("yesterday", "Yesterday"),
    ("today", "Today"),
    ("last3", "Last 3 days"),
    ("last7", "Last 7 days"),
]


def _yesterday_repos() -> list[dict]:
    """Every repo in the latest scan — Yesterday spans the whole portfolio."""
    return list(_scan_results.get("repos", []) if _scan_results else [])


@app.route("/yesterday")
@_require_auth
def yesterday_view():
    window = sessions.resolve_window(request.args)
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)

    if not _scan_results:
        return render_template("yesterday.html", scan_results=None, window=window,
                               range_presets=_RANGE_PRESETS,
                               groups=groups, active_group=active_group,
                               all_repos_count=0)

    all_repos = _yesterday_repos()
    scoped_repos = _filter_by_group(all_repos, active_group, groups)

    client = _get_github_client()
    if not client:
        return render_template("yesterday.html", scan_results=_scan_results,
                               not_connected=True, window=window,
                               range_presets=_RANGE_PRESETS, sessions=[],
                               groups=groups, active_group=active_group,
                               all_repos_count=len(all_repos))

    t0 = time.monotonic()
    failed_repos: list[str] = []
    session_list = sessions.gather_sessions(client, scoped_repos, window,
                                            scanned_at=_scan_results.get("scanned_at"),
                                            failures=failed_repos)
    sessions.attach_recaps(session_list, models.get_session_recaps())
    counts = sessions.summarize_counts(session_list)
    markdown = sessions.compose_markdown(
        session_list, session.get("github_user", ""), window.label)
    models.log_sessions_event(
        "view", range=window.key, sessions=len(session_list),
        repos=counts["repos"], session_links=counts.get("session_links", 0),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

    return render_template(
        "yesterday.html",
        scan_results=_scan_results,
        not_connected=False,
        window=window,
        range_presets=_RANGE_PRESETS,
        groups=groups,
        active_group=active_group,
        all_repos_count=len(all_repos),
        sessions=session_list,
        failed_repos=failed_repos,
        counts=counts,
        markdown=markdown,
        next_up_items=_next_up_items(active_group, groups),
        targets_all=[s["token"] for s in session_list],
        targets_needs=[s["token"] for s in session_list if s["needs_summary"]],
    )


def _next_up_items(active_group: str = "", groups: dict | None = None) -> list[dict]:
    """The planned next-steps, pulled from the Projects-page AI summaries and
    scoped to the active group. This is what the old What's Next tab showed;
    it now lives as a section on the Next Up (Yesterday) screen — the two
    were answering the same question ("where do I pick up?") from different
    angles (planned steps vs. live git activity)."""
    summaries = models.get_project_summaries()
    repos = _scan_results.get("repos", []) if _scan_results else []
    if active_group:
        repos = _filter_by_group(repos, active_group, groups or {})
    items = []
    for repo in repos:
        s = summaries.get(repo["name"]) or {}
        steps = s.get("next_steps") or []
        if not steps:
            continue
        items.append({
            "repo": repo["name"],
            "owner": repo["owner"],
            "html_url": repo.get("html_url", ""),
            "next_steps": steps[:5],
            "generated_at": s.get("_generated_at", ""),
        })
    items.sort(key=lambda x: x["repo"].lower())
    return items


@app.route("/yesterday/export.md")
@_require_auth
def yesterday_export():
    """The same recap the COPY button produces, as a downloadable .md file."""
    window = sessions.resolve_window(request.args)
    client = _get_github_client()
    if not _scan_results or not client:
        flash(NOT_CONNECTED_REMEDY if _scan_results else
              "No scan data yet — run a scan first.", "error")
        return redirect(url_for("yesterday_view", range=window.preset))

    # Same group scope as the page, so the download matches COPY.
    groups = models.get_groups()
    active_group = _resolve_active_group(groups, persist=False)
    scoped = _filter_by_group(_yesterday_repos(), active_group, groups)
    session_list = sessions.gather_sessions(client, scoped, window,
                                            scanned_at=_scan_results.get("scanned_at"))
    sessions.attach_recaps(session_list, models.get_session_recaps())
    markdown = sessions.compose_markdown(
        session_list, session.get("github_user", ""), window.label)
    safe_key = window.key.replace(":", "-")
    filename = f"where-i-left-off-{safe_key}.md"
    return Response(
        markdown,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/yesterday/summarize/<token>", methods=["POST"])
@_require_auth_api
def yesterday_summarize_one(token):
    """Generate the plain-English recap for ONE session. The Yesterday page
    drives this once per session with live progress; the server re-gathers the
    session and re-checks staleness so a fresh recap is never paid for twice."""
    client = _get_github_client()
    creds = _get_credentials()
    if not client or not creds:
        return jsonify({"error": NOT_CONNECTED_REMEDY}), 401

    decoded = sessions.decode_token(token)
    if not decoded:
        return jsonify({"error": "Unrecognized session id — reload the page."}), 400
    repo_name, branch = decoded
    repo = _find_repo_by_name(repo_name)
    if not repo:
        return jsonify({"error": f"Repo '{repo_name}' not in the latest scan. Run a scan first."}), 404

    body = (request.json or {}) if request.is_json else {}
    window = sessions.resolve_window(body)
    force = bool(body.get("force"))

    try:
        session_obj = sessions.gather_one_session(client, repo, branch, window)
    except gh.GitHubAuthError:
        return jsonify({"error": GITHUB_AUTH_REMEDY}), 401
    if not session_obj:
        # The branch no longer has activity in this window (e.g. deleted). Not
        # an error — just nothing to summarize.
        return jsonify({"ok": True, "skipped": "gone"})

    existing = models.get_session_recaps().get(session_obj["recap_key"])
    if existing and not force and not sessions.is_recap_stale(existing, session_obj):
        return jsonify({"ok": True, "skipped": "fresh"})

    model = models.get_ai_model()
    try:
        recap = sessions.summarize_session(creds["anthropic_key"], session_obj, model=model)
    except Exception as e:
        models.log_sessions_event(
            "summarize_error", repo=repo_name, branch=branch,
            error=f"{type(e).__name__}: {str(e)[:300]}",
        )
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}"}), 502

    usage = recap.pop("_usage", {})
    if usage:
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        _session_cost.add(in_tok, out_tok, ai.estimate_cost(in_tok, out_tok, model=model))
    recap["_source_sha"] = session_obj.get("last_sha", "")
    recap["_window_key"] = window.key
    models.save_session_recap(session_obj["recap_key"], recap)
    models.log_sessions_event(
        "summarize_done", repo=repo_name, branch=branch, window=window.key,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )
    return jsonify({"ok": True, "headline": recap.get("headline", "")})


# --- Docs Explorer ---
#
# Quick access to the Markdown files in each repo, rendered in the browser.
# One tree call lists a repo's .md files (vendor dirs excluded, root docs
# first); one content call fetches the selected file, rendered to SAFE HTML by
# the dependency-free md_render (every line escaped before markup, link schemes
# checked). A ?raw=1 toggle shows the source instead.

# --- START [S24-E] Do next + Weekly review (routes) ---
#
# Do next ranks everything actionable across the portfolio (In Flight cache +
# Tracker P0/P1 actions + Projects planned steps) into one list; the Weekly
# review adds what shipped in the last 7 days and what went quiet, from the
# same cached Stats read. Neither page calls GitHub for the list itself, so
# they open instantly; freshness is the In Flight cache age (REFRESH re-reads
# and comes back here). Every view and export logs to data/logs/nextup.log.

def _do_next_data(scan: dict | None, active_group: str, groups: dict):
    all_repos = list(scan.get("repos", []) if scan else [])
    scoped = _filter_by_group(all_repos, active_group, groups)
    cached = models.get_inflight()
    items = (cached or {}).get("items") or []
    result = nextup.build_do_next(items, models.list_trackers(),
                                  models.get_project_summaries(), scoped)
    return cached, items, scoped, all_repos, result


def _inflight_age(cached: dict | None):
    return inflight.age_days((cached or {}).get("gathered_at", "")) if cached else None


@app.route("/next")
@_require_auth
def do_next():
    scan = _scan_results
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)
    ctx = dict(groups=groups, active_group=active_group, scan_results=scan,
               is_monday=activity._local_now().weekday() == 0)
    if not scan:
        return render_template("next.html", all_repos_count=0, **ctx)
    t0 = time.monotonic()
    cached, _items, scoped, all_repos, result = _do_next_data(scan, active_group, groups)
    gathered_at = (cached or {}).get("gathered_at", "")
    scoped_names = {f"{r.get('owner')}/{r['name']}" for r in scoped}
    failed_scoped = [f for f in (cached or {}).get("failed_repos") or [] if f in scoped_names]
    markdown = nextup.compose_do_next_markdown(result, session.get("github_user", ""),
                                               gathered_at, active_group)
    models.log_nextup_event(
        "view_do_next", group=active_group, total=result["total"], tiers=result["counts"],
        inflight_cached=bool(cached), inflight_age_days=_inflight_age(cached),
        elapsed_ms=int((time.monotonic() - t0) * 1000))
    return render_template(
        "next.html", result=result, cached=cached, markdown=markdown, failed_scoped=failed_scoped,
        gathered_age_days=_inflight_age(cached), all_repos_count=len(all_repos), **ctx)


@app.route("/next/export.md")
@_require_auth
def do_next_export():
    scan = _scan_results
    groups = models.get_groups()
    active_group = _resolve_active_group(groups, persist=False)
    if not scan:
        flash("No scan data yet — run a scan first.", "error")
        return redirect(url_for("do_next"))
    cached, _items, _scoped, _all, result = _do_next_data(scan, active_group, groups)
    models.log_nextup_event("export_do_next", group=active_group, total=result["total"])
    return Response(
        nextup.compose_do_next_markdown(result, session.get("github_user", ""),
                                        (cached or {}).get("gathered_at", ""), active_group),
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="do-next.md"'},
    )


def _review_data(scan: dict, active_group: str, groups: dict):
    cached, items, scoped, all_repos, dn = _do_next_data(scan, active_group, groups)
    week_rng, _ = activity.resolve_range({"range": "week"})
    quarter_rng, _ = activity.resolve_range({"range": "quarter"})
    # Both ranges come from the same 90-day Stats read (one cache).
    result = _activity_data(_get_github_client(), scan, quarter_rng)
    week = quarter = None
    if result is not None:
        week = _stats_report(scan, result, active_group, groups, week_rng)
        quarter = _stats_report(scan, result, active_group, groups, quarter_rng)
        # Reuse any "What we shipped" paragraphs written on Stats for this
        # week's work (matched by content, so Stats → Week SUMMARIZE fills it).
        _stats_attach_ai(week, active_group, models.get_ai_model())
    review = nextup.build_review(week, quarter, dn, items, scoped)
    return cached, all_repos, week_rng, result, review


@app.route("/review")
@_require_auth
def weekly_review():
    scan = _scan_results
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)
    ctx = dict(groups=groups, active_group=active_group, scan_results=scan)
    if not scan:
        return render_template("review.html", all_repos_count=0, **ctx)
    t0 = time.monotonic()
    cached, all_repos, week_rng, result, review = _review_data(scan, active_group, groups)
    markdown = nextup.compose_review_markdown(review, session.get("github_user", ""),
                                              active_group, week_rng.dates_label)
    t = ((review.get("week") or {}).get("totals") or {})
    models.log_nextup_event(
        "view_review", group=active_group, shipped_repos=len(review["shipped"]),
        commits=t.get("commits"), waiting=len(review["waiting"]), quiet=len(review["quiet"]),
        stale=len(review["stale"]), next=review["next_total"],
        github_read=result is not None, cache_hit=(result or {}).get("cache_hit"),
        elapsed_ms=int((time.monotonic() - t0) * 1000))
    return render_template(
        "review.html", review=review, week_rng=week_rng, markdown=markdown,
        fetched_at=(result or {}).get("fetched_at"), stale_read=(result or {}).get("stale"),
        cached=cached, gathered_age_days=_inflight_age(cached),
        all_repos_count=len(all_repos), **ctx)


@app.route("/review/export.md")
@_require_auth
def weekly_review_export():
    scan = _scan_results
    groups = models.get_groups()
    active_group = _resolve_active_group(groups, persist=False)
    if not scan:
        flash("No scan data yet — run a scan first.", "error")
        return redirect(url_for("weekly_review"))
    _cached, _all, week_rng, _result, review = _review_data(scan, active_group, groups)
    models.log_nextup_event("export_review", group=active_group)
    filename = f"weekly-review-{week_rng.end_date.isoformat()}.md"
    return Response(
        nextup.compose_review_markdown(review, session.get("github_user", ""),
                                       active_group, week_rng.dates_label),
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
# --- END [S24-E] ---

# --- Phone snapshot (Netlify) ---------------------------------------------
#
# A read-only copy of Do next, Weekly review, What changed (Week / Month /
# Quarter / YTD) and Portfolio, rendered here, ENCRYPTED here with a passphrase
# only the owner knows (snapshot.py), and published as static files to a
# Netlify site (netlify_publish.py). Netlify only ever holds ciphertext; the
# phone's browser decrypts it. The Netlify token and the passphrase are stored
# encrypted with the app password (config/snapshot.enc) and unlocked into
# memory at login, like the GitHub / Anthropic keys. Every setup, publish,
# preview and takedown is logged to data/logs/snapshot.log (never a secret).

_snapshot_secrets: dict | None = None


def _snapshot_min_len() -> int:
    try:
        import snapshot as _snap
        return _snap.MIN_PASSPHRASE_LEN
    except Exception:  # noqa: BLE001 - Settings must render regardless
        return 12


def _unlock_snapshot(password: str) -> None:
    """At login: load the snapshot secrets with the same app password."""
    global _snapshot_secrets
    try:
        _snapshot_secrets = security.decrypt_snapshot_secrets(password)
    except OSError as e:
        logger.warning("snapshot: could not read secrets: %s", e)
        _snapshot_secrets = None


def _lock_snapshot() -> None:
    global _snapshot_secrets
    _snapshot_secrets = None


def _snapshot_data(scan: dict) -> dict:
    """Everything the snapshot shows, from the same caches the pages use.
    One Stats read (widened to cover YTD) serves all four ranges."""
    groups = models.get_groups()
    model = models.get_ai_model()
    client = _get_github_client()
    keys = ("week", "month", "quarter", "ytd")
    rngs = {k: activity.resolve_range({"range": k})[0] for k in keys}
    widest = min(rngs.values(), key=lambda r: r.start_date)
    result = _activity_data(client, scan, widest)  # None only when offline with no cache
    reports = {}
    if result is not None:
        for k in keys:
            rep = _stats_report(scan, result, "", groups, rngs[k])
            _stats_attach_ai(rep, "", model)
            reports[k] = rep
    ranges = [{
        "key": k, "name": rngs[k].name, "dates": rngs[k].dates_label,
        "totals": reports[k]["totals"], "updated": reports[k]["updated"],
        "failed": reports[k]["failed"], "ai_portfolio": reports[k].get("ai_portfolio"),
    } for k in keys if k in reports]

    cached, items, scoped, _all, dn = _do_next_data(scan, "", groups)
    review = nextup.build_review(reports.get("week"), reports.get("quarter"), dn, items, scoped)

    by_full = {_repo_full_name(r): r for r in scan.get("repos", [])}
    _groups, view, _infra = _portfolio_view_data()

    def prow(row):
        h = row.get("health") or {}
        return {"name": row["name"], "one_liner": row.get("one_liner") or "",
                "grade": h.get("grade") or "—", "tone": h.get("tone") or "none",
                "summary": h.get("summary") or "",
                "html_url": (by_full.get(row["full_name"]) or {}).get("html_url") or "",
                "pushed_at": row.get("pushed_at") or "", "open_branches": row.get("open_branches") or 0}
    portfolio_sections = [{"tag": sec["tag"], "rows": [prow(r) for r in sec["repos"]]}
                          for sec in view["sections"] if sec["repos"]]
    if view["untagged"]:
        portfolio_sections.append({"tag": "Untagged", "rows": [prow(r) for r in view["untagged"]]})

    return {
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "owner": session.get("github_user", ""),
        "do_next": dn,
        "review": review,
        "review_week_label": rngs["week"].dates_label,
        "ranges": ranges,
        "portfolio": portfolio_sections,
        "inflight_gathered_at": (cached or {}).get("gathered_at", ""),
        "activity_fetched_at": (result or {}).get("fetched_at", ""),
    }


def _snapshot_build(scan: dict, passphrase: str) -> dict:
    """Render -> encrypt -> shell -> files. Returns {"files", "zip", "bytes"}."""
    import snapshot as snap
    data = _snapshot_data(scan)
    body = snap.render_body(data)
    payload = snap.encrypt(body, passphrase)
    shell = snap.build_shell(payload, _central_time(data["built_at"]))
    files = snap.site_files(shell)
    blob = snap.build_zip(files)
    return {"files": files, "zip": blob, "bytes": len(blob), "body_chars": len(body)}


def _snapshot_csp(files: dict) -> str:
    """The Content-Security-Policy line from the generated _headers, so the
    local preview runs under the same policy as Netlify."""
    for line in (files.get("_headers") or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.lower().startswith("content-security-policy:"):
            return line.split(":", 1)[1].strip()
    return ""


def _snapshot_back():
    return redirect(url_for("settings") + "#snapshot")


def _snapshot_publish_now(scan: dict) -> bool:
    """Build and deploy. Flashes the outcome; True on success."""
    import netlify_publish as nf
    state = models.get_snapshot_state()
    if not state.get("site_id"):
        flash("No Netlify site yet. Run the setup below first.", "error")
        return False
    t0 = time.monotonic()
    try:
        built = _snapshot_build(scan, _snapshot_secrets["passphrase"])
        client = nf.NetlifyClient(_snapshot_secrets["netlify_token"], log_event=models.log_snapshot_event)
        dep = client.deploy_zip(state["site_id"], built["zip"])
    except nf.NetlifyError as e:
        models.log_snapshot_event("publish_error", status=e.status, error=e.message)
        if e.status == 404:
            models.save_snapshot_state({**state, "site_id": "", "url": "",
                                        "last_error": e.message})
        else:
            models.save_snapshot_state({**state, "last_error": e.message})
        flash(f"Snapshot not published: {e.message}", "error")
        return False
    except gh.GitHubAuthError:
        raise
    except Exception as e:  # noqa: BLE001 - say what failed, keep the app up
        logger.exception("snapshot: publish failed")
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        models.log_snapshot_event("publish_error", error=msg)
        models.save_snapshot_state({**state, "last_error": msg})
        flash(f"Snapshot not published ({msg}). Details are in data/logs/snapshot.log.", "error")
        return False
    url = state.get("url") or dep.get("site_url") or dep.get("url") or ""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    models.save_snapshot_state({**state, "url": url, "last_published_at": now,
                                "last_bytes": built["bytes"], "last_deploy_id": dep.get("deploy_id", ""),
                                "last_error": ""})
    models.log_snapshot_event("publish", site_id=state["site_id"], deploy_id=dep.get("deploy_id"),
                              bytes=built["bytes"], elapsed_ms=int((time.monotonic() - t0) * 1000))
    flash(f"Snapshot published: {url} (open it on your phone and enter your passphrase).",
          "success")
    return True


@app.route("/snapshot/setup", methods=["POST"])
@_require_auth
def snapshot_setup():
    """Settings -> Phone snapshot: save the Netlify token + passphrase
    (encrypted with the app password), create the site if needed, publish."""
    global _snapshot_secrets
    import netlify_publish as nf
    import snapshot as snap
    token = request.form.get("netlify_token", "").strip()
    passphrase = request.form.get("passphrase", "")
    confirm = request.form.get("passphrase_confirm", "")
    password = request.form.get("password", "")
    if not token:
        flash("Paste your Netlify personal access token first.", "error")
        return _snapshot_back()
    problem = snap.check_passphrase(passphrase)
    if problem:
        flash(problem, "error")
        return _snapshot_back()
    if passphrase != confirm:
        flash("The two passphrases don't match. Nothing was changed.", "error")
        return _snapshot_back()
    if not password or security.decrypt_credentials(password) is None:
        flash("Wrong app password. Nothing was changed.", "error")
        return _snapshot_back()
    client = nf.NetlifyClient(token, log_event=models.log_snapshot_event)
    state = models.get_snapshot_state()
    try:
        client.verify()
        site, created = None, False
        if state.get("site_id"):
            try:
                site = client.get_site(state["site_id"])
            except nf.NetlifyError as e:
                if e.status != 404:
                    raise
        if site is None:
            site, created = client.create_site(), True
    except nf.NetlifyError as e:
        models.log_snapshot_event("setup_error", status=e.status, error=e.message)
        flash(f"Netlify setup failed: {e.message} Nothing was changed.", "error")
        return _snapshot_back()
    try:
        security.encrypt_snapshot_secrets(password, {"netlify_token": token, "passphrase": passphrase})
    except OSError as e:
        logger.error("snapshot: could not save secrets: %s", e)
        flash(f"Couldn't save the snapshot settings ({type(e).__name__}).", "error")
        return _snapshot_back()
    _snapshot_secrets = {"netlify_token": token, "passphrase": passphrase}
    models.save_snapshot_state({**state, "site_id": site["site_id"], "name": site.get("name", ""),
                                "url": site.get("url", ""), "admin_url": site.get("admin_url", ""),
                                "set_up_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                "last_error": ""})
    models.log_snapshot_event("setup", site_id=site["site_id"], created=created)
    if _scan_results:
        _snapshot_publish_now(_scan_results)
    else:
        flash("Snapshot site is ready. Run a scan, then PUBLISH NOW.", "success")
    return _snapshot_back()


@app.route("/snapshot/publish", methods=["POST"])
@_require_auth
def snapshot_publish():
    if not _scan_results:
        flash("No scan data yet — run a scan first.", "error")
        return redirect(_safe_next() or url_for("settings") + "#snapshot")
    if _snapshot_secrets is None:
        flash("The app restarted, so publishing is locked. Log out and back in to unlock it.", "error")
        return redirect(_safe_next() or url_for("settings") + "#snapshot")
    _snapshot_publish_now(_scan_results)
    return redirect(_safe_next() or url_for("settings") + "#snapshot")


@app.route("/snapshot/preview")
@_require_auth
def snapshot_preview():
    """The exact page the phone gets (encrypted with the real passphrase),
    served locally under the same Content-Security-Policy, to try unlocking
    before publishing."""
    if not _scan_results:
        flash("No scan data yet — run a scan first.", "error")
        return _snapshot_back()
    if _snapshot_secrets is None:
        flash("Set up the phone snapshot first (or log in again to unlock it).", "error")
        return _snapshot_back()
    built = _snapshot_build(_scan_results, _snapshot_secrets["passphrase"])
    models.log_snapshot_event("preview", bytes=built["bytes"])
    resp = Response(built["files"]["index.html"], mimetype="text/html; charset=utf-8")
    csp = _snapshot_csp(built["files"])
    if csp:
        resp.headers["Content-Security-Policy"] = csp
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/snapshot/takedown", methods=["POST"])
@_require_auth
def snapshot_takedown():
    """Delete the Netlify site and forget the snapshot settings."""
    import netlify_publish as nf
    state = models.get_snapshot_state()
    if state.get("site_id"):
        if _snapshot_secrets is None:
            flash("Log out and back in first: taking the site down needs the Netlify token, "
                  "which is locked after a restart.", "error")
            return _snapshot_back()
        try:
            nf.NetlifyClient(_snapshot_secrets["netlify_token"],
                             log_event=models.log_snapshot_event).delete_site(state["site_id"])
        except nf.NetlifyError as e:
            models.log_snapshot_event("takedown_error", status=e.status, error=e.message)
            flash(f"Couldn't delete the Netlify site: {e.message} Nothing was changed.", "error")
            return _snapshot_back()
    security.delete_snapshot_secrets()
    models.clear_snapshot_state()
    _lock_snapshot()
    models.log_snapshot_event("takedown", site_id=state.get("site_id", ""))
    flash("Phone snapshot taken down: the Netlify site is deleted and the token and passphrase are "
          "forgotten.", "success")
    return _snapshot_back()




# --- In Flight: every unmerged branch / open PR across repos -----------------
# Window-independent counterpart to Next Up: "what have I kicked off that is
# still running or waiting on me?" Cached in data/inflight.json so the page
# opens instantly; REFRESH regathers. Logged to data/logs/inflight.log.

_INFLIGHT_KINDS = [("", "All"), ("merge", "Ready to merge"), ("finish", "Finish it"),
                   ("continue", "Needs PR"), ("stale", "Stale"), ("unknown", "Check on GitHub"),
                   ("complete", "Merged")]


_INFLIGHT_LOCK = threading.Lock()


def _inflight_scope():
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)
    all_repos = _yesterday_repos()
    scoped = _filter_by_group(all_repos, active_group, groups)
    return groups, active_group, all_repos, scoped


@app.route("/inflight")
@_require_auth
def inflight_view():
    groups, active_group, all_repos, scoped = _inflight_scope()
    kind = request.args.get("kind", "")
    if kind not in {k for k, _ in _INFLIGHT_KINDS}:
        kind = ""
    if not _scan_results:
        return render_template("inflight.html", scan_results=None, groups=groups,
                               active_group=active_group, all_repos_count=0,
                               kinds=_INFLIGHT_KINDS, kind=kind)

    cached = models.get_inflight()
    items_all = (cached or {}).get("items") or []
    scoped_names = {f"{r.get('owner')}/{r['name']}" for r in scoped}
    items_scoped = inflight.filter_items(items_all, repos=scoped_names)
    items = inflight.filter_items(items_scoped, kind=kind or None)
    counts = inflight.summarize_counts(items_scoped)
    failed_scoped = [f for f in (cached or {}).get("failed_repos") or [] if f in scoped_names]
    markdown = inflight.compose_markdown(items, session.get("github_user", ""),
                                         (cached or {}).get("gathered_at", "")) if cached else ""
    return render_template(
        "inflight.html",
        scan_results=_scan_results,
        github_connected=_get_github_client() is not None,
        cached=cached,
        items=items,
        counts=counts,
        markdown=markdown,
        kinds=_INFLIGHT_KINDS,
        kind=kind,
        failed_scoped=failed_scoped,
        groups=groups,
        active_group=active_group,
        all_repos_count=len(all_repos),
        gathered_age_days=inflight.age_days((cached or {}).get("gathered_at", "")) if cached else None,
        max_compares=sessions.MAX_BRANCH_COMPARES,
    )


@app.route("/inflight/refresh", methods=["POST"])
@_require_auth
def inflight_refresh():
    client = _get_github_client()
    if not _scan_results:
        flash("No scan data yet — run a scan first.", "error")
        return redirect(_safe_next() or url_for("inflight_view"))
    if not client:
        flash(NOT_CONNECTED_REMEDY, "error")
        return redirect(_safe_next() or url_for("inflight_view"))
    t0 = time.monotonic()
    # Always gather the whole portfolio so the cache is complete; the view
    # scopes to the active group afterwards.
    # A scan older than a day can't vouch that a repo still has no branches.
    data = inflight.gather_inflight(client, _yesterday_repos(),
                                    scan_fresh=(_scan_age_days() or 0) < 1)
    with _INFLIGHT_LOCK:
        models.save_inflight(data)
    counts = inflight.summarize_counts(data["items"])
    models.log_inflight_event(
        "refresh", items=counts["total"], repos=counts["repos"],
        session_links=counts.get("session_links", 0),
        repos_checked=data["repos_checked"], repos_skipped=data["repos_skipped"],
        failed=data["failed_repos"], elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
    msg = (f"In flight refreshed: {counts['total']} unmerged branch"
           f"{'es' if counts['total'] != 1 else ''} across {counts['repos']} repo"
           f"{'s' if counts['repos'] != 1 else ''}.")
    if data["failed_repos"]:
        msg += f" Couldn't read {len(data['failed_repos'])} repo(s): {', '.join(data['failed_repos'][:5])}."
    flash(msg, "success" if not data["failed_repos"] else "error")
    return redirect(_safe_next() or url_for("inflight_view"))


@app.route("/inflight/export.md")
@_require_auth
def inflight_export():
    cached = models.get_inflight()
    if not cached:
        flash("Nothing cached yet — click REFRESH first.", "error")
        return redirect(url_for("inflight_view"))
    groups, active_group, _, scoped = _inflight_scope()
    kind = request.args.get("kind", "")
    if kind not in {k for k, _ in _INFLIGHT_KINDS}:
        kind = ""
    # Same kind filter as the page, so DOWNLOAD matches COPY FOR CLAUDE.
    items = inflight.filter_items(cached["items"], kind=kind or None,
                                  repos={f"{r.get('owner')}/{r['name']}" for r in scoped})
    markdown = inflight.compose_markdown(items, session.get("github_user", ""),
                                         cached.get("gathered_at", ""))
    return Response(markdown, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="work-in-flight.md"'})


# Cap per click so a stray bulk request can't fan out into hundreds of writes.
_MAX_BRANCH_DELETES = 50


@app.route("/inflight/delete-merged", methods=["POST"])
@_require_auth
def inflight_delete_merged():
    """Delete branches whose work is already on the default branch. The only
    destructive GitHub write in the app, so every target is re-verified live
    (0 commits ahead, no open PR) right before it is deleted; nothing is
    trusted from the cache or the form beyond the branch name."""
    bad = _reject_bad_reset_token("In Flight", "inflight_view")
    if bad:
        return bad
    back = redirect(url_for("inflight_view", kind=request.form.get("kind") or None,
                            group=request.form.get("group") or None))
    client = _get_github_client()
    if not client:
        flash(NOT_CONNECTED_REMEDY, "error")
        return back
    cached = models.get_inflight()
    if not cached:
        flash("Nothing cached yet — click REFRESH first.", "error")
        return back

    repos = {(r.get("owner"), r["name"]): r for r in _yesterday_repos()}
    targets = []
    for raw in request.form.getlist("target")[:_MAX_BRANCH_DELETES]:
        full, sep, branch = raw.partition(":")
        owner, _, name = full.partition("/")
        if sep and owner and name and branch:
            targets.append((owner, name, branch))
    if not targets:
        flash("No branches selected.", "error")
        return back

    deleted, refused = [], []
    for owner, name, branch in targets:
        label = f"{name}:{branch}"
        repo = repos.get((owner, name))
        if not repo:
            refused.append(f"{label} (repo not in the scan)")
            continue
        default = repo.get("default_branch") or "main"
        if branch == default:
            refused.append(f"{label} (default branch)")
            continue
        try:
            comp = client.compare_branches(owner, name, default, branch)
            if not comp or comp.get("ahead_by") != 0:
                refused.append(f"{label} (has commits not on {default})")
                continue
            open_prs = client.get_pulls(owner, name, state="open")
            if any((p.get("head") or {}).get("ref") == branch for p in open_prs):
                refused.append(f"{label} (has an open PR)")
                continue
            ok, reason = client.delete_branch(owner, name, branch)
        except gh.GitHubAuthError:
            flash(NOT_CONNECTED_REMEDY, "error")
            return back
        except Exception as e:  # noqa: BLE001 - one branch must not abort the rest
            logger.warning("delete branch %s/%s:%s failed: %s", owner, name, branch, e)
            ok, reason = False, "GitHub error"
        if ok:
            deleted.append((owner, name, branch))
        else:
            refused.append(f"{label} ({reason})")

    gone = set(deleted)
    with _INFLIGHT_LOCK:
        latest = models.get_inflight() or cached
        latest["items"] = [i for i in latest.get("items") or []
                           if (i.get("owner"), i.get("repo"), i.get("branch")) not in gone]
        models.save_inflight(latest)
    models.log_inflight_event(
        "delete_merged", deleted=[f"{o}/{n}:{b}" for o, n, b in deleted], refused=refused,
    )
    if deleted:
        flash(f"Deleted {len(deleted)} merged branch{'es' if len(deleted) != 1 else ''}.", "success")
    if refused:
        flash(f"Left {len(refused)} branch{'es' if len(refused) != 1 else ''} alone: "
              f"{'; '.join(refused[:5])}{'…' if len(refused) > 5 else ''}", "error")
    return back


# --- Resume: a context-switch packet for one project ---------------------------
# Deterministic, zero AI cost: composes everything RepoDoctor knows about one
# repo (open work, recent sessions, docs excerpts, tracker actions, planned
# steps) into a page + paste-ready Markdown. Logged to data/logs/resume.log.

@app.route("/resume")
@_require_auth
def resume_index():
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)
    if not _scan_results:
        return render_template("resume.html", scan_results=None, groups=groups,
                               active_group=active_group, all_repos_count=0, repos=[])
    all_repos = sorted(_yesterday_repos(), key=lambda r: briefing.last_push_ts(r) or "", reverse=True)
    scoped = _filter_by_group(all_repos, active_group, groups)
    history = models.get_resume_history()
    inflight_cache = models.get_inflight() or {}
    open_by_repo: dict[str, int] = {}
    for i in inflight_cache.get("items") or []:
        if i.get("kind") != "complete":
            key = f"{i.get('owner')}/{i.get('repo')}"
            open_by_repo[key] = open_by_repo.get(key, 0) + 1
    repos = []
    for r in scoped:
        full = r.get("full_name", f"{r['owner']}/{r['name']}")
        h = history.get(full) or {}
        repos.append({**r, "full_name": full, "open_count": open_by_repo.get(f"{r.get('owner')}/{r['name']}", 0),
                      "last_resumed_at": h.get("last_resumed_at", ""),
                      "resume_count": h.get("count", 0)})
    recent = sorted([r for r in repos if r["last_resumed_at"]],
                    key=lambda r: r["last_resumed_at"], reverse=True)[:6]
    return render_template("resume.html", scan_results=_scan_results, repos=repos,
                           recent=recent, groups=groups, active_group=active_group,
                           all_repos_count=len(all_repos), packet=None)


def _build_resume_packet(owner: str, name: str):
    """Shared by the page and the export. Returns (packet, error_redirect)."""
    client = _get_github_client()
    repo = next((r for r in _yesterday_repos() if r["owner"] == owner and r["name"] == name), None)
    if not repo:
        flash("Repo not found in the last scan — run a scan first.", "error")
        return None, redirect(url_for("resume_index"))
    if not client:
        flash(NOT_CONNECTED_REMEDY, "error")
        return None, redirect(url_for("resume_index"))
    t0 = time.monotonic()
    packet = resume.build_packet(
        client, repo,
        summary=models.get_project_summaries().get(name),
        brief=models.get_briefs().get(name),
        blurb=models.get_selector_blurbs().get(f"{owner}/{name}"),
        tracker=models.get_tracker(owner, name),
        groups=models.get_groups(),
        categories=models.get_repo_categories(),
        history=models.get_resume_history(),
    )
    models.log_resume_event(
        "build", repo=f"{owner}/{name}", inflight=len(packet["inflight"]),
        sessions=len(packet["recent_sessions"]), errors=packet["errors"],
        session_links=sessions.count_session_links(packet["inflight"] + packet["recent_sessions"]),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
    return packet, None


@app.route("/resume/<owner>/<name>")
@_require_auth
def resume_view(owner, name):
    packet, bounce = _build_resume_packet(owner, name)
    if bounce:
        return bounce
    models.record_resume(f"{owner}/{name}")
    groups = models.get_groups()
    return render_template(
        "resume.html",
        scan_results=_scan_results,
        packet=packet,
        markdown=resume.compose_markdown(packet, session.get("github_user", "")),
        groups=groups,
        active_group=_resolve_active_group(groups, persist=False),
        all_repos_count=len(_yesterday_repos()),
        repos=[],
    )


@app.route("/resume/<owner>/<name>/export.md")
@_require_auth
def resume_export(owner, name):
    packet, bounce = _build_resume_packet(owner, name)
    if bounce:
        return bounce
    markdown = resume.compose_markdown(packet, session.get("github_user", ""))
    return Response(markdown, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="resume-{re.sub(r"[^A-Za-z0-9._-]+", "-", name)}.md"'})



# --- Components: reusable-infrastructure inventory across repos ---------------
# Step one of componentizing: which repo has which capability (auth, logging,
# email/SMS, testing, Claude docs, deployment, ...) and which is the best
# extraction source. Path-based detection (one tree call per repo), cached in
# data/components.json, logged to data/logs/components.log.

@app.route("/components")
@_require_auth
def components_view():
    groups = models.get_groups()
    active_group = _resolve_active_group(groups)
    if not _scan_results:
        return render_template("components.html", scan_results=None, inventory=None,
                               markdown="", all_repos_count=0, groups=groups,
                               active_group=active_group, components_meta={})
    all_repos = _yesterday_repos()
    scoped = _filter_by_group(all_repos, active_group, groups)
    cached = models.get_components()
    inv = markdown = None
    if cached:
        inv = components.inventory(scoped, cached.get("detections") or {})
        markdown = components.compose_markdown(inv)
    return render_template(
        "components.html", scan_results=_scan_results, inventory=inv, markdown=markdown or "",
        all_repos_count=len(all_repos), groups=groups, active_group=active_group,
        components_meta={"scanned_at": (cached or {}).get("scanned_at", "")},
        github_connected=_get_github_client() is not None,
    )


@app.route("/components/scan", methods=["POST"])
@_require_auth
def components_scan():
    client = _get_github_client()
    if not _scan_results:
        flash("No scan data yet — run a scan first.", "error")
        return redirect(url_for("components_view"))
    if not client:
        flash(NOT_CONNECTED_REMEDY, "error")
        return redirect(url_for("components_view"))
    t0 = time.monotonic()
    previous = (models.get_components() or {}).get("detections") or {}
    detections, failed = {}, []
    for r in _yesterday_repos():  # whole portfolio so the cache is complete
        full = r.get("full_name", f"{r['owner']}/{r['name']}")
        det = components.scan_repo_components(client, r)
        if det is None:
            # GitHub didn't answer: keep the last good detection, never "nothing".
            failed.append(full)
            if full in previous:
                detections[full] = previous[full]
            continue
        detections[full] = det
    data = {"scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "detections": detections}
    models.save_components(data)
    inv = components.inventory(_yesterday_repos(), detections)
    models.log_components_event(
        "scan", repos=len(detections), capabilities_found=inv.get("capabilities_found"),
        gaps=len(inv.get("gaps") or []), failed=failed,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
    msg = (f"Components scanned: {len(detections)} repos, "
           f"{inv.get('capabilities_found', 0)} capabilities found.")
    if failed:
        msg += (f" Couldn't read {len(failed)} repo(s) (kept their last result): "
                f"{', '.join(failed[:5])}{'…' if len(failed) > 5 else ''}.")
    flash(msg, "error" if failed else "success")
    return redirect(url_for("components_view"))


@app.route("/components/export.md")
@_require_auth
def components_export():
    cached = models.get_components()
    if not cached or not _scan_results:
        flash("Nothing to export yet — run a components scan first.", "error")
        return redirect(url_for("components_view"))
    groups = models.get_groups()
    active_group = _resolve_active_group(groups, persist=False)
    scoped = _filter_by_group(_yesterday_repos(), active_group, groups)
    markdown = components.compose_markdown(components.inventory(scoped, cached["detections"]))
    return Response(markdown, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="component-inventory.md"'})


# --- Portfolio: every repo grouped by theme tag + the infrastructure map -------
# Tags ARE groups, so a tag picked here scopes every other screen. AI tag
# suggestions are saved for review and only applied on an explicit click.
# Logged to data/logs/portfolio.log.

_PORTFOLIO_ACTIVE_DAYS = 30


def _portfolio_seed_tags():
    """Create the owner's default themes once (empty), never again after they
    delete one — the flag lives in preferences."""
    if models.get_preferences().get("portfolio_seeded"):
        return
    for tag in portfolio.DEFAULT_TAGS:
        models.create_tag(tag)
    models.update_preferences(lambda p: p.update(portfolio_seeded=True))
    models.log_portfolio_event("seed_tags", tags=portfolio.DEFAULT_TAGS)


def _portfolio_view_data():
    groups = models.get_groups()
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=_PORTFOLIO_ACTIVE_DAYS)).isoformat()
    repos = _yesterday_repos()
    inflight_cache = models.get_inflight()
    view = portfolio.build_view(
        repos, groups, models.get_selector_blurbs(),
        models.get_project_summaries(), (inflight_cache or {}).get("items") or [],
        activity_cutoff=cutoff.replace("+00:00", "Z"),
        grades=_health_report(repos, groups, inflight_cache=inflight_cache)["grades"],
    )
    infra = portfolio.infrastructure_view(models.get_components(), repos,
                                          models.get_infra_homes(), components)
    return groups, view, infra


@app.route("/portfolio")
@_require_auth
def portfolio_view():
    if not _scan_results:
        return render_template("portfolio.html", scan_results=None)
    _portfolio_seed_tags()
    groups, view, infra = _portfolio_view_data()
    tab = request.args.get("tab", "themes")
    if tab not in ("themes", "infra"):
        tab = "themes"
    suggestions = models.get_portfolio_suggestions()
    markdown = portfolio.compose_markdown(view, infra, session.get("github_user", ""), _central_today())
    return render_template(
        "portfolio.html",
        scan_results=_scan_results,
        github_connected=_get_github_client() is not None,
        view=view,
        infra=infra,
        tab=tab,
        groups=groups,
        category_tags=models.CATEGORY_GROUPS,
        suggestions=suggestions.get("items") or [],
        suggestions_at=suggestions.get("generated_at", ""),
        components_scanned=bool(models.get_components()),
        markdown=markdown,
        active_days=_PORTFOLIO_ACTIVE_DAYS,
    )


def _portfolio_back():
    return redirect(url_for("portfolio_view", tab=request.form.get("tab") or None))


@app.route("/api/portfolio/tag", methods=["POST"])
@_require_auth_api
def portfolio_tag_repo():
    """Add or remove one tag on one repo (chip × / + tag). Saved instantly."""
    data = request.get_json(silent=True) or {}
    repo = (data.get("repo") or "").strip()
    tag = portfolio.clean_tag(data.get("tag") or "")
    add = bool(data.get("add", True))
    if not repo or not _find_repo_by_name(repo):
        return jsonify({"error": f"Repo '{repo}' isn't in the latest scan."}), 404
    if not tag:
        return jsonify({"error": f"Tag names are 1-{portfolio.MAX_TAG_LEN} characters, no < or >."}), 400
    if add and tag in models.CATEGORY_GROUPS:
        # Categories stay single-select (one per repo), as on Projects.
        models.set_repo_category(repo, tag)
    elif not models.tag_repo(repo, tag, add=add):
        return jsonify({"error": f"No tag named '{tag}'."}), 404
    models.log_portfolio_event("tag_add" if add else "tag_remove", repo=repo, tag=tag)
    tags = sorted(t for t, members in models.get_groups().items() if repo in members)
    return jsonify({"ok": True, "repo": repo, "tags": tags})


@app.route("/portfolio/tags", methods=["POST"])
@_require_auth
def portfolio_tags():
    """Create / rename / delete a tag from the Portfolio tag manager."""
    action = request.form.get("action", "")
    name = portfolio.clean_tag(request.form.get("name") or "")
    groups = models.get_groups()
    if action == "create":
        if not name:
            flash(f"Tag names are 1-{portfolio.MAX_TAG_LEN} characters, no < or >.", "error")
        elif name in groups:
            flash(f'A tag named "{name}" already exists.', "error")
        else:
            models.create_tag(name)
            models.log_portfolio_event("tag_create", tag=name)
            flash(f'Tag "{name}" created. Add projects to it with + TAG on any row.', "success")
    elif action == "rename":
        new = portfolio.clean_tag(request.form.get("new_name") or "")
        if not name or not new or name not in groups:
            flash("Pick a tag and give it a new name.", "error")
        elif name in models.CATEGORY_GROUPS:
            flash(f'"{name}" is a category and can\'t be renamed.', "error")
        elif new in groups:
            flash(f'A tag named "{new}" already exists.', "error")
        elif models.rename_group(name, new):
            models.log_portfolio_event("tag_rename", old=name, new=new)
            flash(f'Renamed "{name}" to "{new}".', "success")
        else:
            flash("Could not rename the tag.", "error")
    elif action == "delete":
        if name in models.CATEGORY_GROUPS:
            flash(f'"{name}" is a category and can\'t be deleted.', "error")
        elif name and models.delete_group(name):
            models.log_portfolio_event("tag_delete", tag=name)
            flash(f'Deleted tag "{name}". The projects themselves are untouched.', "success")
        else:
            flash("Tag not found.", "error")
    else:
        flash("Unknown tag action.", "error")
    return _portfolio_back()


@app.route("/api/portfolio/suggest", methods=["POST"])
@_require_auth_api
def portfolio_suggest():
    """One AI call proposes tags for the untagged repos (or all repos). The
    result is saved for review; nothing is applied here."""
    creds = _get_credentials()
    if not creds:
        return jsonify({"error": NOT_CONNECTED_REMEDY}), 401
    if not _scan_results:
        return jsonify({"error": "No scan data yet — run a scan first."}), 400
    scope = ((request.get_json(silent=True) or {}).get("scope") or "untagged")
    _, view, _ = _portfolio_view_data()
    rows = list(view["rows"].values())
    if scope != "all":
        rows = [r for r in rows if not r["tags"]]
    if not rows:
        return jsonify({"ok": True, "count": 0, "note": "Every repo already has a tag."})
    model = models.get_ai_model()
    t0 = time.monotonic()
    try:
        raw, usage = ai.request_json(
            anthropic.Anthropic(api_key=creds["anthropic_key"]),
            model=model, max_tokens=min(8000, 400 + 60 * len(rows)),
            schema=portfolio.SUGGEST_SCHEMA,
            messages=[{"role": "user", "content": portfolio.suggestion_prompt(rows, view["tag_names"])}],
        )
    except Exception as e:  # noqa: BLE001 - surfaced to the page, nothing saved
        detail = f"{type(e).__name__}: {str(e)[:300]}"
        models.log_portfolio_event("suggest_error", scope=scope, repos=len(rows), error=detail)
        return jsonify({"error": detail}), 502
    items = portfolio.normalize_suggestions(raw, rows, view["tag_names"])
    _session_cost.add(usage["input_tokens"], usage["output_tokens"],
                      ai.estimate_cost(usage["input_tokens"], usage["output_tokens"], model=model))
    models.save_portfolio_suggestions({
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": scope, "items": items,
    })
    models.log_portfolio_event(
        "suggest_done", scope=scope, repos=len(rows), suggested=len(items), model=model,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
    return jsonify({"ok": True, "count": len(items)})


@app.route("/portfolio/suggestions", methods=["POST"])
@_require_auth
def portfolio_apply_suggestions():
    """Apply the ticked suggestions (repo::tag pairs), or dismiss them all."""
    data = models.get_portfolio_suggestions()
    items = data.get("items") or []
    if request.form.get("action") == "dismiss":
        models.save_portfolio_suggestions({})
        models.log_portfolio_event("suggest_dismiss", count=len(items))
        flash("Suggestions dismissed.", "success")
        return _portfolio_back()
    allowed = {(i["repo"], t) for i in items for t in i["tags"]}
    picked = []
    for raw in request.form.getlist("pick"):
        repo, sep, tag = raw.partition("::")
        if sep and (repo, tag) in allowed and _find_repo_by_name(repo):
            picked.append((repo, tag))
    for repo, tag in picked:
        if tag in models.CATEGORY_GROUPS:
            models.set_repo_category(repo, tag)
        else:
            models.tag_repo(repo, tag, add=True)
    done = set(picked)
    remaining = []
    for i in items:
        left = [t for t in i["tags"] if (i["repo"], t) not in done]
        if left and request.form.get("action") != "apply_close":
            remaining.append({**i, "tags": left})
    models.save_portfolio_suggestions({**data, "items": remaining} if remaining else {})
    models.log_portfolio_event("suggest_apply", applied=[f"{r}::{t}" for r, t in picked],
                               remaining=len(remaining))
    flash(f"Applied {len(picked)} tag{'s' if len(picked) != 1 else ''}.", "success")
    return _portfolio_back()


@app.route("/portfolio/infra-home", methods=["POST"])
@_require_auth
def portfolio_infra_home():
    area = request.form.get("area", "")
    full = (request.form.get("repo") or "").strip()
    if area not in portfolio._AREA_KEYS:
        flash("Unknown infrastructure area.", "error")
    elif full and not _find_repo_by_full_name(full):
        flash(f"'{full}' isn't in the latest scan.", "error")
    else:
        models.set_infra_home(area, full)
        models.log_portfolio_event("infra_home", area=area, repo=full)
        flash("Home repo saved." if full else "Home repo cleared.", "success")
    return redirect(url_for("portfolio_view", tab="infra"))


@app.route("/portfolio/export.md")
@_require_auth
def portfolio_export():
    if not _scan_results:
        flash("No scan data yet — run a scan first.", "error")
        return redirect(url_for("portfolio_view"))
    _, view, infra = _portfolio_view_data()
    markdown = portfolio.compose_markdown(view, infra, session.get("github_user", ""), _central_today())
    return Response(markdown, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="portfolio-{_central_today()}.md"'})


@app.route("/docs")
@_require_auth
def docs_index():
    if not _scan_results:
        return render_template("docs.html", scan_results=None)

    client = _get_github_client()
    if not client:
        return render_template("docs.html", scan_results=_scan_results,
                               not_connected=True, repos=[])

    all_repos = sorted(_scan_results.get("repos", []),
                       key=lambda r: briefing.last_push_ts(r) or "", reverse=True)
    # Repo-groups (distinct from the file-directory `groups` the template uses
    # for the sidebar) scope the repo picker so one group selection filters the
    # whole app, Docs included.
    repo_groups = models.get_groups()
    active_group = _resolve_active_group(repo_groups)
    repos = _filter_by_group(all_repos, active_group, repo_groups)
    if not repos:
        return render_template("docs.html", scan_results=_scan_results, repos=[],
                               repo_groups=repo_groups, active_group=active_group,
                               all_repos_count=len(all_repos))

    req_name = request.args.get("repo") or ""
    req_owner = request.args.get("owner") or ""
    requested = None
    if req_name:
        # Match on owner too: a same-named org and personal repo can coexist.
        matches = [r for r in all_repos if r["name"] == req_name
                   and (not req_owner or r.get("owner") == req_owner)]
        requested = matches[0] if matches else None
    # An explicit ?repo= wins over the saved group filter (a link from Resume
    # must open that repo, not silently fall back to the group's first one).
    repo = requested or repos[0]
    if requested and requested not in repos:
        repos = [requested] + repos  # keep the picker showing what's open
    ref = request.args.get("ref") or repo.get("default_branch", "main")

    files = docs_explorer.list_md_files(client, repo["owner"], repo["name"], ref)
    groups = docs_explorer.group_by_dir(files)

    selected = request.args.get("path")
    if not selected or not docs_explorer.is_allowed_path(selected, files):
        selected = docs_explorer.pick_default(files)

    content = None
    rendered = None
    file_url = ""
    view_raw = request.args.get("raw") == "1"
    if selected:
        content = client.get_file_content(repo["owner"], repo["name"], selected, ref=ref) or ""
        if not view_raw:
            rendered = md_render.render_markdown(content)
        html_url = repo.get("html_url", "")
        file_url = f"{html_url}/blob/{ref}/{selected}" if html_url else ""
        logger.info("docs view repo=%s ref=%s path=%s (%d bytes)",
                    repo["name"], ref, selected, len(content))

    return render_template(
        "docs.html",
        scan_results=_scan_results,
        not_connected=False,
        repos=repos,
        active_repo=repo,
        ref=ref,
        groups=groups,
        repo_groups=repo_groups,
        active_group=active_group,
        all_repos_count=len(all_repos),
        file_count=len(files),
        selected=selected,
        content=content,
        rendered=rendered,
        view_raw=view_raw,
        file_url=file_url,
    )


# --- Program (cross-project rollup) ---
#
# One tab that treats a group of repos as a single initiative — built for
# the Education suite (parentpoint + teacherAIde + beacon, plus the local
# LLM running familygraph and beacon's own local LLM) and reusable for any
# group (e.g. Subject Apps). Membership is managed with the existing
# Groups editor on the Projects page; the non-repo pieces live in
# free-text infrastructure notes. Every generation logs to
# data/logs/program.log for paste-back debugging.

EDUCATION_PROGRAM = "Education"
# Matched case-insensitively against scanned repo names on first visit.
_EDUCATION_SEED_REPOS = ("parentpoint", "parentpointedu", "teacheraide", "beacon", "familygraph")
SUBJECT_APPS_PROGRAM = "Subject Apps"
_DEFAULT_EDUCATION_NOTES = (
    "Shared infrastructure: a local LLM runs familygraph, and beacon ships "
    "its own local LLM. parentpoint, teacherAIde, and beacon together form "
    "the education suite."
)


def _seed_program_groups():
    """One-shot: create the Education group from scan matches and an empty
    Subject Apps group. Guarded by a marker so a deliberately deleted
    group is not resurrected on the next visit."""
    meta = models.get_program_meta()
    if meta.get("_seeded"):
        return
    # Don't seed (or burn the one-shot marker) before a scan exists — otherwise
    # the Education group seeds empty and, with the marker set, never
    # re-populates once repos are actually scanned.
    if not (_scan_results and _scan_results.get("repos")):
        return
    groups = models.get_groups()
    scanned = {r["name"].lower(): r["name"]
               for r in (_scan_results.get("repos", []) if _scan_results else [])}

    if EDUCATION_PROGRAM not in groups:
        matched = [scanned[s] for s in _EDUCATION_SEED_REPOS if s in scanned]
        models.set_group(EDUCATION_PROGRAM, matched)
        models.log_program_event("seed_group", group=EDUCATION_PROGRAM, members=matched)
    if SUBJECT_APPS_PROGRAM not in groups:
        models.set_group(SUBJECT_APPS_PROGRAM, [])
        models.log_program_event("seed_group", group=SUBJECT_APPS_PROGRAM, members=[])

    if not meta.get(EDUCATION_PROGRAM, {}).get("notes"):
        models.save_program_notes(EDUCATION_PROGRAM, _DEFAULT_EDUCATION_NOTES)
    if not meta.get(SUBJECT_APPS_PROGRAM, {}).get("notes"):
        models.save_program_notes(
            SUBJECT_APPS_PROGRAM,
            "Subject-specific education apps. Add members on the Projects "
            "page (Manage Groups), then list any shared pieces here.",
        )

    meta = models.get_program_meta()
    meta["_seeded"] = True
    models.save_program_meta(meta)


def _program_members(group_name: str) -> list[dict]:
    groups = models.get_groups()
    member_names = set(groups.get(group_name, []))
    repos = _scan_results.get("repos", []) if _scan_results else []
    return [r for r in repos if r["name"] in member_names]


@app.route("/program")
@_require_auth
def program_view():
    _seed_program_groups()
    groups = models.get_groups()

    requested = request.args.get("group")
    if requested is not None and requested in groups:
        active = requested
    elif EDUCATION_PROGRAM in groups:
        active = EDUCATION_PROGRAM
    else:
        active = sorted(groups)[0] if groups else ""

    member_repos = _program_members(active) if active else []
    member_names = models.get_groups().get(active, []) if active else []
    members = program.assemble_members(
        member_repos,
        briefs=models.get_briefs(),
        summaries=models.get_project_summaries(),
        trackers=models.list_trackers(),
    )
    meta = models.get_program_meta().get(active, {}) if active else {}
    notes = meta.get("notes", "")
    pbrief = models.get_program_briefs().get(active) if active else None
    stale = program.is_program_brief_stale(pbrief, member_repos, member_names)
    generated_label = _central_today()
    markdown = program.compose_markdown(active, notes, members, pbrief, generated_label) if active else ""

    # Members in the group but missing from the latest scan (e.g. repo not
    # created yet, or excluded) — shown so the user knows why they're absent.
    scanned_names = {r["name"] for r in (_scan_results.get("repos", []) if _scan_results else [])}
    missing_members = [n for n in member_names if n not in scanned_names]

    return render_template(
        "program.html",
        scan_results=_scan_results,
        groups=groups,
        active_program=active,
        members=members,
        missing_members=missing_members,
        notes=notes,
        program_brief=pbrief,
        brief_stale=stale,
        markdown=markdown,
    )


@app.route("/program/notes", methods=["POST"])
@_require_auth
def program_notes():
    group_name = (request.form.get("group") or "").strip()
    notes = request.form.get("notes", "").strip()
    if group_name not in models.get_groups():
        flash(f'Unknown program "{group_name}".', "error")
        return redirect(url_for("program_view"))
    models.save_program_notes(group_name, notes)
    models.log_program_event("save_notes", group=group_name, chars=len(notes))
    flash("Infrastructure notes saved.", "success")
    return redirect(url_for("program_view", group=group_name))


@app.route("/program/generate", methods=["POST"])
@_require_auth
def program_generate():
    creds = _get_credentials()
    if not creds:
        flash("Not authenticated.", "error")
        return redirect(url_for("program_view"))
    group_name = (request.form.get("group") or "").strip()
    if group_name not in models.get_groups():
        flash(f'Unknown program "{group_name}".', "error")
        return redirect(url_for("program_view"))

    member_repos = _program_members(group_name)
    if not member_repos:
        flash("This program has no scanned member projects yet — add repos "
              "to the group on the Projects page, then re-scan.", "error")
        return redirect(url_for("program_view", group=group_name))

    members = program.assemble_members(
        member_repos,
        briefs=models.get_briefs(),
        summaries=models.get_project_summaries(),
        trackers=models.list_trackers(),
    )
    notes = models.get_program_meta().get(group_name, {}).get("notes", "")
    model = models.get_ai_model()
    context_text = program.build_program_context(group_name, notes, members)
    models.log_program_event(
        "generate_start", group=group_name, model=model,
        members=[m["name"] for m in members], context_chars=len(context_text),
    )

    try:
        brief = program.generate_program_brief(
            api_key=creds["anthropic_key"],
            program_name=group_name,
            context_text=context_text,
            model=model,
        )
    except Exception as e:
        models.log_program_event(
            "generate_error", group=group_name,
            error=f"{type(e).__name__}: {str(e)[:300]}",
        )
        flash(f"Program brief generation failed: {type(e).__name__}: {str(e)[:200]}", "error")
        return redirect(url_for("program_view", group=group_name))

    usage = brief.pop("_usage", {})
    if usage:
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        _session_cost.add(in_tok, out_tok, ai.estimate_cost(in_tok, out_tok, model=model))
    brief["_members"] = sorted(m["name"] for m in members)
    models.save_program_brief(group_name, brief)
    models.log_program_event(
        "generate_done", group=group_name, stage=brief.get("stage"),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )
    models.log_action("generate_program_brief", group_name, "all", f"stage={brief.get('stage')}")
    flash(f'Program brief generated for "{group_name}".', "success")
    return redirect(url_for("program_view", group=group_name))


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
    prior_data = models.get_firestore_data().get("repos") or {}
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
                # A repo GitHub didn't answer for keeps its last good row
                # rather than being recorded as a confident "not_using".
                prior = prior_data.get(r["name"])
                if isinstance(prior, dict) and not prior.get("error"):
                    results.append(prior)
                    continue
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
    except gh.GitHubAuthError:
        # Surface the actionable PAT-reset remedy via the global handler
        # instead of a generic "generation failed: GitHubAuthError" message,
        # matching the other AI-generation routes.
        raise
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

    _save_generated_tracker(owner, name, tracker, prior)
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


def _save_generated_tracker(owner: str, name: str, tracker: dict, prior: dict | None):
    """Save a fresh tracker without losing status clicks made while the AI
    call ran (30-60s). Under the store lock, any next_action whose status or
    note changed on disk since `prior` was read is carried onto the new one."""
    def _by_id(t):
        return {a.get("id"): a for a in (t or {}).get("next_actions", []) or []
                if isinstance(a, dict) and isinstance(a.get("id"), str)}
    with models._STORE_LOCK:
        current = _by_id(models.get_tracker(owner, name))
        before = _by_id(prior)
        for aid, action in _by_id(tracker).items():
            now = current.get(aid)
            if not now:
                continue
            was = before.get(aid) or {}
            if (now.get("status"), now.get("status_note")) != (was.get("status"), was.get("status_note")):
                action["status"] = now.get("status")
                action["status_note"] = now.get("status_note", "")
        models.save_tracker(owner, name, tracker)


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

    # Atomic get-modify-save under the store lock so two quick status clicks
    # (or a click racing a regeneration's save) can't lose each other's write.
    found = {"ok": False}

    def _apply(tracker):
        for n in tracker.get("next_actions") or []:
            if n.get("id") == action_id:
                n["status"] = new_status
                if new_note or new_status in ("blocked", "dismissed"):
                    # Always record a note for blocked/dismissed so the UI
                    # can show WHY. Empty string is fine if user didn't supply.
                    n["status_note"] = new_note
                found["ok"] = True
                break

    tracker = models.update_tracker(owner, name, _apply)
    if tracker is None:
        flash("No tracker found for this repo.", "error")
        return redirect(url_for("tracker_view", owner=owner, name=name))
    if not found["ok"]:
        flash(f"Action {action_id} not found.", "error")
        return redirect(url_for("tracker_view", owner=owner, name=name))
    models.log_tracker_event(
        "action_status_update",
        owner=owner, repo=name, action_id=action_id,
        new_status=new_status, has_note=bool(new_note),
    )
    flash(f"{action_id} → {new_status}.", "success")
    return redirect(url_for("tracker_view", owner=owner, name=name) + f"#row-{action_id}")


# =====================================================================
# COMMENTED OUT — full branch analysis features (will re-enable later)
#
# NOTE (2026-08-04): the AI engine these routes call — ai.analyze_branch,
# ai.build_analysis_prompt, ai.estimate_tokens, and the branch-analyst
# SYSTEM_PROMPT — was deleted from ai_analyzer.py when the Henry tab was
# removed (Henry was its only live caller). To re-enable this block,
# restore those functions from git history, then port them onto
# ai.request_json + a JSON Schema the way the Projects summary does, so
# the model can't answer with prose.
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
#     model = prefs.get("ai_model", "claude-sonnet-5")
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
#     model = prefs.get("ai_model", "claude-sonnet-5")
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
    # Werkzeug's debug mode exposes an interactive, code-executing console on
    # error pages — handy for a quick local hack, dangerous for an app holding
    # a GitHub PAT. Default it OFF; opt in with REPODOCTOR_DEBUG=1 when needed.
    debug_mode = os.environ.get("REPODOCTOR_DEBUG", "").lower() in ("1", "true", "yes")
    # basicConfig() is a no-op once the app-log file handler is on the root
    # logger, which left the terminal silent (no "Running on…", no
    # tracebacks) and ignored REPODOCTOR_DEBUG. Attach the console handler
    # and set the level explicitly instead.
    _root = logging.getLogger()
    _console = logging.StreamHandler()
    _console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _root.addHandler(_console)
    _root.setLevel(logging.DEBUG if debug_mode else logging.INFO)
    models._ensure_dirs()
    app.run(host="127.0.0.1", port=5001, debug=debug_mode)
