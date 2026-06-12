"""
Data models and local storage for RepoDoctor.
Handles scan history, analysis cache, action log, and preferences.
"""

import datetime
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
# User-scoped storage that survives wiping/re-cloning the project directory.
USER_DATA_DIR = os.path.join(os.path.expanduser("~"), ".repodoctor")


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "specs"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "trackers"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _load_json(path: str) -> dict | list:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # Corrupted or unreadable file should not crash the app. Rename it
        # aside so the user can inspect, and start fresh.
        logger.warning("Could not load %s (%s); renaming to .corrupt and starting fresh", path, e)
        try:
            os.rename(path, path + ".corrupt")
        except OSError:
            pass
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

PREFS_PATH = os.path.join(CONFIG_DIR, "preferences.json")

DEFAULT_PREFS = {
    "local_root": "~/claudesync2",
    "sort_repos_by": "branch_count",
    "sort_branches_by": "classification",
    "excluded_repos": [],
    "ai_model": "claude-haiku-4-5-20251001",
    "display_mode": "plain_english",
    "active_group": "",
}


def get_preferences() -> dict:
    prefs = _load_json(PREFS_PATH)
    if not prefs:
        prefs = DEFAULT_PREFS.copy()
        _save_json(PREFS_PATH, prefs)
    for k, v in DEFAULT_PREFS.items():
        if k not in prefs:
            prefs[k] = v
    return prefs


def save_preferences(prefs: dict):
    _save_json(PREFS_PATH, prefs)


# --- Scan History ---

SCAN_PATH = os.path.join(DATA_DIR, "scan_history.json")


def get_scan_history() -> dict:
    return _load_json(SCAN_PATH) or {"scans": []}


def save_scan(scan_data: dict):
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
    return data.get("actions", [])


def log_action(action_type: str, repo: str, branch: str, details: str = ""):
    actions = get_action_log()
    actions.append({
        "type": action_type,
        "repo": repo,
        "branch": branch,
        "details": details,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    _save_json(ACTION_LOG_PATH, {"actions": actions})


# --- Product Specs ---

def get_spec(repo_name: str) -> str | None:
    specs_dir = os.path.join(DATA_DIR, "specs")
    for ext in (".md", ".txt"):
        path = os.path.join(specs_dir, f"{repo_name}{ext}")
        if os.path.exists(path):
            with open(path, "r") as f:
                return f.read()
    return None


def save_spec(repo_name: str, content: str):
    _ensure_dirs()
    path = os.path.join(DATA_DIR, "specs", f"{repo_name}.md")
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
    summaries = get_project_summaries()
    summary["_generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    summaries[repo_name] = summary
    _save_json(SUMMARIES_PATH, summaries)


def save_project_summaries(summaries: dict):
    _save_json(SUMMARIES_PATH, summaries)


# --- Chat Briefs (Briefing screen) ---
#
# Keyed by repo name. One rich AI-generated brief per repo (what it is,
# stage, what's built, what's left, open decisions, constraints) feeding
# the cross-project Chat Briefing export.

BRIEFS_PATH = os.path.join(DATA_DIR, "briefs.json")


def get_briefs() -> dict:
    return _load_json(BRIEFS_PATH) or {}


def save_brief(repo_name: str, brief: dict):
    briefs = get_briefs()
    brief["_generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    briefs[repo_name] = brief
    _save_json(BRIEFS_PATH, briefs)


# --- Henry Branch Summaries ---
#
# Keyed by "{repo_name}/{branch_name}" so multiple henry-named branches in the
# same repo each get their own card.

HENRY_SUMMARIES_PATH = os.path.join(DATA_DIR, "henry_summaries.json")


def get_henry_summaries() -> dict:
    return _load_json(HENRY_SUMMARIES_PATH) or {}


def save_henry_summary(repo_name: str, branch_name: str, summary: dict):
    summaries = get_henry_summaries()
    summary["_generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    summaries[f"{repo_name}/{branch_name}"] = summary
    _save_json(HENRY_SUMMARIES_PATH, summaries)


def clear_henry_summaries():
    _save_json(HENRY_SUMMARIES_PATH, {})


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
        "repos": {r["name"]: r for r in repos},
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


# TEMPORARY (April 2026): one-shot recovery of Chris's existing groups after a
# codebase wipe. seed_default_groups_if_missing() runs once on login and only
# fills in groups that don't already exist — your edits are never overwritten.
# NEXT REBUILD: delete DEFAULT_USER_GROUPS and seed_default_groups_if_missing,
# and stop calling it from app._init_session. Groups now live in
# ~/.repodoctor/groups.json and will simply persist there.
DEFAULT_USER_GROUPS = {
    "School": [
        "audioscribe", "desmond", "grantfinder", "LA-pipeline", "lessonalign",
        "missionIQ", "parentpoint", "parentpointmeals", "standardscollector",
    ],
    "Church": [
        "catholicevents", "grantfinder", "ministryfair", "missionIQ",
        "sacramentalrecords", "worshipaidcreator",
    ],
    "Catholic Games": ["RCC_letmypeoplego", "RCC_longwayhome"],
    "Infrastructure": [
        "audioscribe", "claudecodearchiver", "desmond", "personalcrm",
        "personalfinance", "redmon", "repodoctor2", "vibecoach",
    ],
    "Fun": [
        "C64_archon", "C64_loderunner", "C64_nflchallenge", "c64HOA-original",
        "c64SFR-original", "c64mule-original", "nordstromshopper",
        "personalcrm", "polygraph",
    ],
}


def seed_default_groups_if_missing() -> list[str]:
    """Add any of DEFAULT_USER_GROUPS that aren't present yet. Returns added names."""
    groups = get_groups()
    added: list[str] = []
    for name, repos in DEFAULT_USER_GROUPS.items():
        if name not in groups:
            groups[name] = sorted(set(repos))
            added.append(name)
    if added:
        save_groups(groups)
    return added


def set_group(name: str, repos: list[str]):
    groups = get_groups()
    groups[name] = sorted(set(repos))
    save_groups(groups)


def rename_group(old_name: str, new_name: str) -> bool:
    groups = get_groups()
    if old_name not in groups or not new_name or new_name == old_name:
        return False
    if new_name in groups:
        # Would silently overwrite a different group — refuse.
        return False
    groups[new_name] = groups.pop(old_name)
    save_groups(groups)
    prefs = get_preferences()
    if prefs.get("active_group") == old_name:
        prefs["active_group"] = new_name
        save_preferences(prefs)
    return True


def delete_group(name: str) -> bool:
    groups = get_groups()
    if name not in groups:
        return False
    groups.pop(name)
    save_groups(groups)
    prefs = get_preferences()
    if prefs.get("active_group") == name:
        prefs["active_group"] = ""
        save_preferences(prefs)
    return True


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
    except OSError as e:
        logger.warning("event log write failed (%s): %s", path, e)


def _tail_log(path: str, n: int) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-n:]:
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


# --- Session Cost Tracking ---

class SessionCost:
    """Track cumulative API costs for the current session."""

    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0
        self.analyses_count = 0

    def add(self, input_tokens: int, output_tokens: int, cost: float):
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
