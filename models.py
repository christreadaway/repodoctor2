"""
Data models and local storage for RepDoctor2.
Handles scan history, analysis cache, action log, and preferences.
"""

import datetime
import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "specs"), exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _load_json(path: str) -> dict | list:
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def _save_json(path: str, data):
    _ensure_dirs()
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# --- Preferences ---

PREFS_PATH = os.path.join(CONFIG_DIR, "preferences.json")

DEFAULT_PREFS = {
    "local_root": "~/claudesync2",
    "sort_repos_by": "branch_count",
    "sort_branches_by": "classification",
    "excluded_repos": [],
    "ai_model": "claude-haiku-4-5-20251001",
    "display_mode": "plain_english",
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
    with open(path, "w") as f:
        f.write(content)


def list_specs() -> list[str]:
    specs_dir = os.path.join(DATA_DIR, "specs")
    if not os.path.exists(specs_dir):
        return []
    return [
        os.path.splitext(f)[0]
        for f in os.listdir(specs_dir)
        if f.endswith((".md", ".txt"))
    ]


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
