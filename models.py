"""
Data models and local storage for RepoDoctor.
Handles scan history, analysis cache, action log, and preferences.
"""

import collections
import datetime
import json
import logging
import os
import re
import tempfile
import threading
import time

from ai_analyzer import DEFAULT_MODEL, VALID_MODELS

logger = logging.getLogger(__name__)

# Flask's dev server is threaded: two concurrent requests (e.g. the Projects
# summary queue and the Briefing queue in two tabs) doing load→mutate→save on
# the same JSON file can silently drop each other's writes. One in-process
# lock around every read-modify-write pair prevents that. _atomic_write
# already prevents torn files; this prevents lost updates.
_STORE_LOCK = threading.RLock()


def get_ai_model() -> str:
    """The configured AI model, validated — a stale/invalid saved value
    falls back to the default instead of 404ing at the API."""
    model = get_preferences().get("ai_model", DEFAULT_MODEL)
    return model if model in VALID_MODELS else DEFAULT_MODEL

# REPODOCTOR_HOME re-roots every on-disk location (data/, config/,
# ~/.repodoctor). The test suite sets it (conftest.py) so a test run can never
# write fake errors into the real debug log or touch real groups/credentials.
_HOME_OVERRIDE = os.environ.get("REPODOCTOR_HOME")
DATA_DIR = os.path.join(_HOME_OVERRIDE or os.path.dirname(__file__), "data")
CONFIG_DIR = os.path.join(_HOME_OVERRIDE or os.path.dirname(__file__), "config")
# User-scoped storage that survives wiping/re-cloning the project directory.
USER_DATA_DIR = (os.path.join(_HOME_OVERRIDE, "user") if _HOME_OVERRIDE
                 else os.path.join(os.path.expanduser("~"), ".repodoctor"))


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "specs"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "trackers"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _load_json(path: str) -> dict | list:
    if not os.path.exists(path):
        return {}
    for attempt in (1, 2):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # Corrupted file should not crash the app. Rename it aside so the
            # user can inspect, and start fresh.
            logger.warning("Could not parse %s (%s); renaming to .corrupt and starting fresh", path, e)
            try:
                os.rename(path, path + ".corrupt")
            except OSError:
                pass
            return {}
        except OSError as e:
            # A transient read error (Windows sharing violation while a writer
            # os.replace()s, EMFILE) is NOT corruption: renaming the good file
            # aside here meant the next save made the loss permanent. Retry
            # once, then surface the error instead of discarding data.
            if attempt == 2:
                logger.error("Could not read %s: %s", path, e)
                raise
            time.sleep(0.05)
    return {}


def _atomic_write(path: str, write_fn, mode: str = "w"):
    """Write a file atomically: write to a unique temp file in the same
    directory, fsync, then rename over the target. A crash or full disk
    mid-write can no longer leave a truncated file behind — which is the
    exact corruption _load_json's .corrupt rename exists to clean up.
    OSErrors are logged (not swallowed) so write failures surface."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(path), prefix=os.path.basename(path) + ".", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, mode) as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception as e:
        logger.error("Failed to save %s: %s", path, e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _save_json(path: str, data):
    _atomic_write(path, lambda f: json.dump(data, f, indent=2, default=str))


# --- Preferences ---

# The app writes preferences.local.json (gitignored). config/preferences.json
# is tracked in git, and the app used to rewrite it on every group switch:
# that dirty tracked file made the launchers' `git pull origin main` refuse to
# run the moment upstream touched it. It is now read once as the starting
# point for an existing install and never written again.
PREFS_PATH = os.path.join(CONFIG_DIR, "preferences.local.json")
_LEGACY_PREFS_PATH = os.path.join(CONFIG_DIR, "preferences.json")

DEFAULT_PREFS = {
    "local_root": "~/claudesync2",
    "sort_repos_by": "branch_count",
    "sort_branches_by": "classification",
    "excluded_repos": [],
    "ai_model": DEFAULT_MODEL,
    "display_mode": "plain_english",
    "active_group": "",
}


def get_preferences() -> dict:
    prefs = _load_json(PREFS_PATH)
    if not prefs:
        legacy = _load_json(_LEGACY_PREFS_PATH) if PREFS_PATH != _LEGACY_PREFS_PATH else {}
        prefs = {**DEFAULT_PREFS, **(legacy if isinstance(legacy, dict) else {})}
        _save_json(PREFS_PATH, prefs)
    for k, v in DEFAULT_PREFS.items():
        if k not in prefs:
            prefs[k] = v
    return prefs


def save_preferences(prefs: dict):
    _save_json(PREFS_PATH, prefs)


def update_preferences(mutate) -> dict:
    """Load -> mutate -> save preferences under the store lock, so two requests
    writing prefs at once (e.g. a settings save racing a group switch in
    another tab) don't silently lose each other's update. `mutate(prefs)` edits
    in place."""
    with _STORE_LOCK:
        prefs = get_preferences()
        mutate(prefs)
        save_preferences(prefs)
        return prefs


# --- Scan History ---

SCAN_PATH = os.path.join(DATA_DIR, "scan_history.json")


def get_scan_history() -> dict:
    data = _load_json(SCAN_PATH)
    # Tolerate a corrupt/hand-edited file that isn't a dict with a list
    # "scans" — otherwise save_scan/get_latest_scan raise on the bad shape.
    if isinstance(data, dict) and isinstance(data.get("scans"), list):
        return data
    return {"scans": []}


def save_scan(scan_data: dict):
    with _STORE_LOCK:
        history = get_scan_history()
        scan_data["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        history["scans"].append(scan_data)
        # Keep last 50 scans
        history["scans"] = history["scans"][-50:]
        _save_json(SCAN_PATH, history)


def get_latest_scan() -> dict | None:
    history = get_scan_history()
    if history.get("scans"):
        return history["scans"][-1]
    return None


# --- Analysis Cache ---

CACHE_PATH = os.path.join(DATA_DIR, "analysis_cache.json")


def get_analysis_cache() -> dict:
    return _load_json(CACHE_PATH) or {}


def cache_analysis(repo_name: str, branch_name: str, commit_sha: str, analysis: dict):
    with _STORE_LOCK:
        cache = get_analysis_cache()
        key = f"{repo_name}/{branch_name}/{commit_sha}"
        analysis["_cached_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cache[key] = analysis
        _save_json(CACHE_PATH, cache)


def get_cached_analysis(repo_name: str, branch_name: str, commit_sha: str) -> dict | None:
    cache = get_analysis_cache()
    key = f"{repo_name}/{branch_name}/{commit_sha}"
    return cache.get(key)


# --- Action Log ---

ACTION_LOG_PATH = os.path.join(DATA_DIR, "action_log.json")


def get_action_log() -> list:
    data = _load_json(ACTION_LOG_PATH)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        actions = data.get("actions", [])
        return actions if isinstance(actions, list) else []
    return []


def log_action(action_type: str, repo: str, branch: str, details: str = ""):
    with _STORE_LOCK:
        actions = get_action_log()
        actions.append({
            "type": action_type,
            "repo": repo,
            "branch": branch,
            "details": details,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        # Keep the log bounded — it's rewritten in full on every action.
        actions = actions[-2000:]
        _save_json(ACTION_LOG_PATH, {"actions": actions})


# --- Product Specs ---

def _safe_spec_name(repo_name: str) -> str | None:
    """Repo names come from a form field — reject anything that could
    escape the specs directory (path separators, '..', empty)."""
    name = (repo_name or "").strip()
    if not name or not re.fullmatch(r"[\w.-]+", name) or name in (".", ".."):
        return None
    return name


def get_spec(repo_name: str) -> str | None:
    safe = _safe_spec_name(repo_name)
    if safe is None:
        return None
    specs_dir = os.path.join(DATA_DIR, "specs")
    for ext in (".md", ".txt"):
        path = os.path.join(specs_dir, f"{safe}{ext}")
        if os.path.exists(path):
            with open(path, "r") as f:
                return f.read()
    return None


def save_spec(repo_name: str, content: str):
    safe = _safe_spec_name(repo_name)
    if safe is None:
        raise ValueError(f"Invalid spec name: {repo_name!r}")
    _ensure_dirs()
    path = os.path.join(DATA_DIR, "specs", f"{safe}.md")
    _atomic_write(path, lambda f: f.write(content))


def list_specs() -> list[str]:
    specs_dir = os.path.join(DATA_DIR, "specs")
    if not os.path.exists(specs_dir):
        return []
    return [
        os.path.splitext(f)[0]
        for f in os.listdir(specs_dir)
        if f.endswith((".md", ".txt"))
    ]


# --- Project Summaries ---

SUMMARIES_PATH = os.path.join(DATA_DIR, "project_summaries.json")


def get_project_summaries() -> dict:
    return _load_json(SUMMARIES_PATH) or {}


def save_project_summary(repo_name: str, summary: dict):
    with _STORE_LOCK:
        summaries = get_project_summaries()
        summary["_generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        summaries[repo_name] = summary
        _save_json(SUMMARIES_PATH, summaries)


def save_project_summaries(summaries: dict):
    _save_json(SUMMARIES_PATH, summaries)


# --- Selector Blurbs (Repo Selector screen) ---
#
# A one-shot, three-sentence plain-English blurb per repo, for the Selector
# screen where you tick the repos you care about and copy a clean summary
# sheet into another document. Keyed by full_name ("owner/name") — NOT bare
# name — because the scan now includes organization repos, and an org repo can
# share a name with a personal one. Keying by full_name keeps the two apart.

SELECTOR_BLURBS_PATH = os.path.join(DATA_DIR, "selector_blurbs.json")


def get_selector_blurbs() -> dict:
    return _load_json(SELECTOR_BLURBS_PATH) or {}


def save_selector_blurb(full_name: str, blurb: dict):
    with _STORE_LOCK:
        blurbs = get_selector_blurbs()
        blurb["_generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        blurbs[full_name] = blurb
        _save_json(SELECTOR_BLURBS_PATH, blurbs)


# --- Chat Briefs (Briefing screen) ---
#
# Keyed by repo name. One rich AI-generated brief per repo (what it is,
# stage, what's built, what's left, open decisions, constraints) feeding
# the cross-project Chat Briefing export.

BRIEFS_PATH = os.path.join(DATA_DIR, "briefs.json")


def get_briefs() -> dict:
    return _load_json(BRIEFS_PATH) or {}


def save_brief(repo_name: str, brief: dict):
    with _STORE_LOCK:
        briefs = get_briefs()
        brief["_generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        briefs[repo_name] = brief
        _save_json(BRIEFS_PATH, briefs)


# --- Session Recaps (Yesterday screen) ---
#
# Plain-English AI recaps of one Claude Code session (= one branch's activity
# in a time window), keyed "{repo}::{branch}::{window_key}". Each recap stores
# the branch tip sha it was written from so it self-heals when the branch moves.

SESSION_RECAPS_PATH = os.path.join(DATA_DIR, "session_recaps.json")


def get_session_recaps() -> dict:
    return _load_json(SESSION_RECAPS_PATH) or {}


def save_session_recap(key: str, recap: dict):
    with _STORE_LOCK:
        recaps = get_session_recaps()
        recap["_generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        recaps[key] = recap
        _save_json(SESSION_RECAPS_PATH, recaps)


# --- Firestore Configuration Data ---
#
# Keyed by repo name. One entry per scanned repo with detection results +
# per-repo setup instructions. Lives in the user dir so it survives wiping
# the codebase, mirroring groups storage.

FIRESTORE_DATA_PATH = os.path.join(USER_DATA_DIR, "firestore_data.json")


def get_firestore_data() -> dict:
    """Return {repo_name: detection_dict, '_scanned_at': iso_ts}."""
    data = _load_json(FIRESTORE_DATA_PATH)
    if isinstance(data, dict):
        return data
    return {}


def save_firestore_data(repos: list[dict]):
    """Save a freshly-scanned set of detection results."""
    payload = {
        "_scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "repos": {r["name"]: r for r in repos if r.get("name")},
    }
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    _save_json(FIRESTORE_DATA_PATH, payload)


def clear_firestore_data():
    if os.path.exists(FIRESTORE_DATA_PATH):
        os.remove(FIRESTORE_DATA_PATH)


# --- Project Groups ---
#
# Groups live in the user's home dir (~/.repodoctor/groups.json) so they
# survive deleting/re-cloning the project. We keep a one-shot migration
# from the legacy config/groups.json location.

GROUPS_PATH = os.path.join(USER_DATA_DIR, "groups.json")
_LEGACY_GROUPS_PATH = os.path.join(CONFIG_DIR, "groups.json")


def _migrate_legacy_groups():
    if os.path.exists(GROUPS_PATH) or not os.path.exists(_LEGACY_GROUPS_PATH):
        return
    try:
        os.makedirs(os.path.dirname(GROUPS_PATH), exist_ok=True)
        with open(_LEGACY_GROUPS_PATH, "r") as src, open(GROUPS_PATH, "w") as dst:
            dst.write(src.read())
    except OSError:
        pass


def get_groups() -> dict:
    """Return {group_name: [repo_name, ...]} mapping."""
    _migrate_legacy_groups()
    data = _load_json(GROUPS_PATH)
    if isinstance(data, dict):
        return data
    return {}


def save_groups(groups: dict):
    _save_json(GROUPS_PATH, groups)


def set_group(name: str, repos: list[str]):
    with _STORE_LOCK:
        groups = get_groups()
        groups[name] = sorted(set(repos))
        save_groups(groups)


def rename_group(old_name: str, new_name: str) -> bool:
    with _STORE_LOCK:
        groups = get_groups()
        if old_name not in groups or not new_name or new_name == old_name:
            return False
        if new_name in groups:
            # Would silently overwrite a different group — refuse.
            return False
        if old_name in CATEGORY_GROUPS:
            # Categories are looked up by these exact names; renaming one
            # silently empties every project's category.
            return False
        groups[new_name] = groups.pop(old_name)
        save_groups(groups)
        prefs = get_preferences()
        if prefs.get("active_group") == old_name:
            prefs["active_group"] = new_name
            save_preferences(prefs)
        # Program notes and the cached program brief are keyed by group name:
        # carry them over so a rename doesn't orphan them.
        meta = get_program_meta()
        if old_name in meta and new_name not in meta:
            meta[new_name] = meta.pop(old_name)
            _save_json(PROGRAM_META_PATH, meta)
        briefs = get_program_briefs()
        if old_name in briefs and new_name not in briefs:
            briefs[new_name] = briefs.pop(old_name)
            _save_json(PROGRAM_BRIEFS_PATH, briefs)
        return True


def delete_group(name: str) -> bool:
    with _STORE_LOCK:
        groups = get_groups()
        if name not in groups or name in CATEGORY_GROUPS:
            return False
        groups.pop(name)
        save_groups(groups)
        prefs = get_preferences()
        if prefs.get("active_group") == name:
            prefs["active_group"] = ""
            save_preferences(prefs)
        return True


# --- Project Categories (church / school / combined / personal) ---
#
# A single-select category per project, on top of the existing (free-form,
# many-to-many) groups: the four categories ARE groups, so they flow through
# every group filter in the app for free — but the Categorize panel assigns
# them single-select (one per repo), which is what "tag a project once" means.
# "Combined" is its own value (a project that serves both church and school),
# so the four are mutually exclusive by design. Any other groups the user
# makes are left untouched.

CATEGORY_GROUPS = ["Church", "School", "Combined", "Personal"]


def seed_category_groups():
    """Create the four category groups (empty) if absent. Idempotent, and it
    never clobbers an existing group of the same name (keeps its members)."""
    with _STORE_LOCK:
        groups = get_groups()
        changed = False
        for name in CATEGORY_GROUPS:
            if name not in groups:
                groups[name] = []
                changed = True
        if changed:
            save_groups(groups)


def get_repo_categories() -> dict:
    """{repo_name: category} for every repo assigned to a category group. If a
    repo somehow lands in more than one (e.g. via Manage Groups checkboxes),
    the first in CATEGORY_GROUPS order wins so the badge stays deterministic."""
    groups = get_groups()
    out: dict[str, str] = {}
    for cat in CATEGORY_GROUPS:
        for repo_name in groups.get(cat, []):
            out.setdefault(repo_name, cat)
    return out


def set_repo_category(repo_name: str, category: str | None) -> bool:
    """Assign one category to a repo (single-select): drop it from all four
    category groups, then add it to `category`. An empty/None category just
    clears it. Non-category groups are never touched. Returns False for an
    unknown category value."""
    if category and category not in CATEGORY_GROUPS:
        return False
    with _STORE_LOCK:
        groups = get_groups()
        for cat in CATEGORY_GROUPS:
            members = groups.get(cat)
            if members and repo_name in members:
                groups[cat] = [r for r in members if r != repo_name]
        if category:
            members = groups.get(category, [])
            groups[category] = sorted(set(members) | {repo_name})
        save_groups(groups)
        return True


# --- Programs (Program screen) ---
#
# A "program" is a group of repos viewed as one initiative (e.g. the
# Education suite), plus free-text infrastructure notes for the pieces
# that aren't repos (local LLMs, shared services). Notes live in the user
# dir so they survive re-cloning; the AI program brief is cached like the
# per-repo chat briefs. Every generation logs to data/logs/program.log.

PROGRAM_META_PATH = os.path.join(USER_DATA_DIR, "program_meta.json")
PROGRAM_BRIEFS_PATH = os.path.join(DATA_DIR, "program_briefs.json")
PROGRAM_LOG_PATH = os.path.join(DATA_DIR, "logs", "program.log")


def get_program_meta() -> dict:
    """{group_name: {"notes": str}, "_seeded": bool}"""
    data = _load_json(PROGRAM_META_PATH)
    return data if isinstance(data, dict) else {}


def save_program_meta(meta: dict):
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    _save_json(PROGRAM_META_PATH, meta)


def save_program_notes(group_name: str, notes: str):
    with _STORE_LOCK:
        meta = get_program_meta()
        meta.setdefault(group_name, {})["notes"] = notes
        save_program_meta(meta)


def get_program_briefs() -> dict:
    return _load_json(PROGRAM_BRIEFS_PATH) or {}


def save_program_brief(group_name: str, brief: dict):
    with _STORE_LOCK:
        briefs = get_program_briefs()
        brief["_generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        briefs[group_name] = brief
        _save_json(PROGRAM_BRIEFS_PATH, briefs)


def log_program_event(event: str, **fields):
    _append_log_event(PROGRAM_LOG_PATH, event, fields)


def tail_program_log(n: int = 100) -> list[dict]:
    """Return the last n events from the program log."""
    return _tail_log(PROGRAM_LOG_PATH, n)


# --- Codebase Trackers ---
#
# One JSON file per repo at data/trackers/<owner>__<name>.json, containing
# the full tracker (modules, infra_gaps, features, external_systems,
# questions, next_actions, recent_changes, build_sequence,
# rollout_sequence). IDs (M1, I1, F1, ...) are stable across regenerations;
# never reuse a deleted integer.

TRACKERS_DIR = os.path.join(DATA_DIR, "trackers")


def _tracker_path(owner: str, repo: str) -> str:
    safe = f"{owner}__{repo}".replace("/", "_").replace("..", "_")
    return os.path.join(TRACKERS_DIR, f"{safe}.json")


def get_tracker(owner: str, repo: str) -> dict | None:
    path = _tracker_path(owner, repo)
    if not os.path.exists(path):
        return None
    return _load_json(path) or None


def save_tracker(owner: str, repo: str, tracker: dict):
    _ensure_dirs()
    _save_json(_tracker_path(owner, repo), tracker)


def update_tracker(owner: str, repo: str, mutate) -> dict | None:
    """Load -> mutate -> save a tracker atomically under the store lock, so a
    status click and a concurrent regeneration (or two status clicks) can't
    lose each other's write. `mutate(tracker)` edits in place; returns the
    saved tracker, or None if the tracker doesn't exist."""
    with _STORE_LOCK:
        tracker = get_tracker(owner, repo)
        if tracker is None:
            return None
        mutate(tracker)
        save_tracker(owner, repo, tracker)
        return tracker


def list_trackers() -> dict[str, dict]:
    """Return {f'{owner}/{repo}': tracker_dict} for every saved tracker."""
    _ensure_dirs()
    out: dict[str, dict] = {}
    if not os.path.exists(TRACKERS_DIR):
        return out
    for fname in os.listdir(TRACKERS_DIR):
        if not fname.endswith(".json"):
            continue
        stem = fname[:-5]
        if "__" not in stem:
            continue
        owner, repo = stem.split("__", 1)
        data = _load_json(os.path.join(TRACKERS_DIR, fname))
        if isinstance(data, dict):
            out[f"{owner}/{repo}"] = data
    return out


def delete_tracker(owner: str, repo: str) -> bool:
    path = _tracker_path(owner, repo)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


# --- Tracker Event Log ---
#
# Append-only line-oriented log file at data/logs/tracker.log. Captures
# every generation, validation result, render error, and copy-prompt
# event. Format is one JSON object per line so it's grep-able and
# copy-paste-able into Claude Code for debugging (per CLAUDE.md).

TRACKER_LOG_PATH = os.path.join(DATA_DIR, "logs", "tracker.log")
BRIEFING_LOG_PATH = os.path.join(DATA_DIR, "logs", "briefing.log")


# Event logs are append-only; cap growth so they don't balloon on a
# long-running deployment. When a file passes the byte cap, rewrite it keeping
# only the newest _LOG_KEEP_LINES lines.
_LOG_MAX_BYTES = 2_000_000
_LOG_KEEP_LINES = 5000


def _truncate_log(path: str, keep: int):
    try:
        with open(path, "r") as f:
            tail = collections.deque(f, maxlen=keep)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(tail)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("event log truncate failed (%s): %s", path, e)


def _append_log_event(path: str, event: str, fields: dict):
    _ensure_dirs()
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": event,
    }
    record.update(fields)
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        if os.path.getsize(path) > _LOG_MAX_BYTES:
            _truncate_log(path, _LOG_KEEP_LINES)
    except OSError as e:
        logger.warning("event log write failed (%s): %s", path, e)


def _tail_log(path: str, n: int) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        # deque(maxlen=n) keeps only the last n lines in memory instead of
        # loading the whole (potentially multi-MB) file via readlines().
        with open(path, "r") as f:
            lines = collections.deque(f, maxlen=n)
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def log_tracker_event(event: str, **fields):
    _append_log_event(TRACKER_LOG_PATH, event, fields)


def tail_tracker_log(n: int = 100) -> list[dict]:
    """Return the last n events from the tracker log."""
    return _tail_log(TRACKER_LOG_PATH, n)


def log_briefing_event(event: str, **fields):
    _append_log_event(BRIEFING_LOG_PATH, event, fields)


def tail_briefing_log(n: int = 100) -> list[dict]:
    """Return the last n events from the briefing log."""
    return _tail_log(BRIEFING_LOG_PATH, n)


SESSIONS_LOG_PATH = os.path.join(DATA_DIR, "logs", "sessions.log")


def log_sessions_event(event: str, **fields):
    _append_log_event(SESSIONS_LOG_PATH, event, fields)


def tail_sessions_log(n: int = 100) -> list[dict]:
    """Return the last n events from the Yesterday/session-recap log."""
    return _tail_log(SESSIONS_LOG_PATH, n)


# Projects-page summary generation. This was the one AI feature without its
# own event log, so a failed batch left nothing to paste back for debugging
# beyond a truncated flash message.
SUMMARIES_LOG_PATH = os.path.join(DATA_DIR, "logs", "summaries.log")


def log_summary_event(event: str, **fields):
    _append_log_event(SUMMARIES_LOG_PATH, event, fields)


def tail_summary_log(n: int = 100) -> list[dict]:
    """Return the last n events from the project-summary log."""
    return _tail_log(SUMMARIES_LOG_PATH, n)


# Repo Selector three-sentence blurb generation gets its own event log so a
# failed blurb leaves a paste-back trail (which repo, which model, the error).
SELECTOR_LOG_PATH = os.path.join(DATA_DIR, "logs", "selector.log")


def log_selector_event(event: str, **fields):
    _append_log_event(SELECTOR_LOG_PATH, event, fields)


def tail_selector_log(n: int = 100) -> list[dict]:
    """Return the last n events from the Selector blurb log."""
    return _tail_log(SELECTOR_LOG_PATH, n)


# --- Session Cost Tracking ---

class SessionCost:
    """Track cumulative API costs for the current session."""

    def __init__(self):
        # Own lock — this is pure in-memory arithmetic and shouldn't ever
        # queue behind another thread's multi-megabyte JSON rewrite.
        self._lock = threading.Lock()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0
        self.analyses_count = 0

    def add(self, input_tokens: int, output_tokens: int, cost: float):
        with self._lock:
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_cost += cost
            self.analyses_count += 1

    def to_dict(self) -> dict:
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost": round(self.total_cost, 4),
            "analyses_count": self.analyses_count,
        }


# --- In Flight (open branches / PRs across repos) -------------------------
# Cached so the page opens instantly; REFRESH regathers from GitHub.
INFLIGHT_PATH = os.path.join(DATA_DIR, "inflight.json")
INFLIGHT_LOG_PATH = os.path.join(DATA_DIR, "logs", "inflight.log")


def get_inflight() -> dict | None:
    data = _load_json(INFLIGHT_PATH)
    return data if isinstance(data, dict) and data.get("items") is not None else None


def save_inflight(data: dict):
    _save_json(INFLIGHT_PATH, data)


def log_inflight_event(event: str, **fields):
    _append_log_event(INFLIGHT_LOG_PATH, event, fields)


def tail_inflight_log(n: int = 100) -> list[dict]:
    return _tail_log(INFLIGHT_LOG_PATH, n)


# --- Resume (context-switch packets) --------------------------------------
# Remembers when each project was last resumed so the picker can say "you
# last opened this 3 days ago" — the whole point is not having to remember.
RESUME_HISTORY_PATH = os.path.join(DATA_DIR, "resume_history.json")
RESUME_LOG_PATH = os.path.join(DATA_DIR, "logs", "resume.log")


def get_resume_history() -> dict:
    data = _load_json(RESUME_HISTORY_PATH)
    return data if isinstance(data, dict) else {}


def record_resume(full_name: str):
    with _STORE_LOCK:
        hist = get_resume_history()
        entry = hist.get(full_name) or {"count": 0}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_resumed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        hist[full_name] = entry
        _save_json(RESUME_HISTORY_PATH, hist)


def log_resume_event(event: str, **fields):
    _append_log_event(RESUME_LOG_PATH, event, fields)


# --- App-wide log (everything Python logs, plus request lines) ------------
APP_LOG_PATH = os.path.join(DATA_DIR, "logs", "app.log")


def tail_app_log(n: int = 200) -> list[str]:
    """Last n lines of the plain-text app log (empty list if none yet)."""
    try:
        with open(APP_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [ln.rstrip("\n") for ln in lines[-n:]]


# --- Components inventory (reusable-infrastructure detection per repo) -----
COMPONENTS_PATH = os.path.join(DATA_DIR, "components.json")
COMPONENTS_LOG_PATH = os.path.join(DATA_DIR, "logs", "components.log")


def get_components() -> dict | None:
    """{"scanned_at": iso, "detections": {full_name: detection}} or None."""
    data = _load_json(COMPONENTS_PATH)
    return data if isinstance(data, dict) and isinstance(data.get("detections"), dict) else None


def save_components(data: dict):
    _save_json(COMPONENTS_PATH, data)


def log_components_event(event: str, **fields):
    _append_log_event(COMPONENTS_LOG_PATH, event, fields)


def tail_components_log(n: int = 100) -> list[dict]:
    return _tail_log(COMPONENTS_LOG_PATH, n)
