"""Claude export parser + conversation-to-repo mapping.

Parses the Claude data export zip, groups conversations by project,
and maps them to GitHub repos. Conversation content is stored locally
and NEVER sent to any API.
"""

import json
import os
import re
import zipfile
from datetime import datetime

PROJECTS_DIR = os.path.join(os.path.dirname(__file__), "projects")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")


def _ensure_dirs():
    os.makedirs(PROJECTS_DIR, exist_ok=True)


def _load_config() -> dict:
    if os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"mappings": {}, "dismissed": []}


def _save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def parse_claude_export(zip_path: str) -> dict:
    """Parse a Claude data export zip. Returns parsed conversation data."""
    _ensure_dirs()
    conversations = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            try:
                data = json.loads(zf.read(name))
            except (json.JSONDecodeError, KeyError):
                continue

            # Handle both flat conversation lists and nested structures
            if isinstance(data, list):
                for item in data:
                    conv = _parse_conversation(item)
                    if conv:
                        conversations.append(conv)
            elif isinstance(data, dict):
                # Could be a single conversation or a wrapper
                if "chat_messages" in data or "uuid" in data:
                    conv = _parse_conversation(data)
                    if conv:
                        conversations.append(conv)
                elif isinstance(data.get("conversations"), list):
                    for item in data["conversations"]:
                        conv = _parse_conversation(item)
                        if conv:
                            conversations.append(conv)

    # Sort by date, most recent first
    conversations.sort(key=lambda c: c["date"], reverse=True)

    # Save parsed data
    output_path = os.path.join(PROJECTS_DIR, "conversations.json")
    with open(output_path, "w") as f:
        json.dump(conversations, f, indent=2)

    return {"conversations": conversations, "count": len(conversations)}


def _parse_conversation(item: dict) -> dict | None:
    """Parse a single conversation item from the export."""
    if not isinstance(item, dict):
        return None

    # Extract conversation name/topic
    name = item.get("name") or item.get("title") or ""
    if not name:
        # Try to extract from first message
        messages = item.get("chat_messages", [])
        if messages and isinstance(messages, list):
            first = messages[0] if messages else {}
            content = first.get("content", first.get("text", ""))
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            name = content[:120] if content else "Untitled conversation"

    # Extract project name
    project = ""
    if "project" in item and isinstance(item["project"], dict):
        project = item["project"].get("name", "")
    elif "project_name" in item:
        project = item["project_name"]
    elif "project_uuid" in item:
        project = item.get("project_title", "")

    # Extract date
    date_str = (
        item.get("created_at")
        or item.get("updated_at")
        or item.get("create_time")
        or ""
    )
    try:
        if date_str:
            # Handle various date formats
            for fmt in [
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d",
            ]:
                try:
                    date = datetime.strptime(date_str[:26], fmt)
                    break
                except ValueError:
                    continue
            else:
                date = datetime.now()
        else:
            date = datetime.now()
    except Exception:
        date = datetime.now()

    # Extract messages for excerpt
    messages = item.get("chat_messages", [])
    message_count = len(messages) if isinstance(messages, list) else 0

    # Get first user message as excerpt
    excerpt = ""
    if isinstance(messages, list):
        for msg in messages:
            sender = msg.get("sender") or msg.get("role") or ""
            if sender in ("human", "user"):
                content = msg.get("content", msg.get("text", ""))
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if content:
                    excerpt = content[:300]
                    break

    uuid = item.get("uuid") or item.get("id") or ""

    return {
        "id": uuid,
        "name": name[:200],
        "project": project,
        "date": date.isoformat(),
        "date_display": date.strftime("%b %d, %Y"),
        "message_count": message_count,
        "excerpt": excerpt,
    }


def get_conversations() -> list[dict]:
    """Load parsed conversations from disk."""
    path = os.path.join(PROJECTS_DIR, "conversations.json")
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return json.load(f)


def map_conversations_to_repos(conversations: list[dict], repos: list[str]) -> dict:
    """Map conversations to repos using name matching + content analysis.

    Returns {
        "auto_matched": [...],
        "suggested": [...],
        "unmatched": [...],
        "stats": {"total": N, "auto": N, "suggested": N, "unmatched": N}
    }
    """
    config = _load_config()
    existing_mappings = config.get("mappings", {})
    dismissed = set(config.get("dismissed", []))

    # Normalize repo names for matching
    repo_names_lower = {r.lower(): r for r in repos}

    auto_matched = []
    suggested = []
    unmatched = []

    for conv in conversations:
        conv_id = conv.get("id", "")
        if conv_id in dismissed:
            continue

        # Check existing manual mapping
        if conv_id in existing_mappings:
            conv["matched_repo"] = existing_mappings[conv_id]
            conv["match_type"] = "manual"
            conv["match_reason"] = "Manually assigned"
            auto_matched.append(conv)
            continue

        project_name = conv.get("project", "").lower().strip()
        topic = conv.get("name", "").lower()
        excerpt = conv.get("excerpt", "").lower()

        matched = False

        # 1. Exact project name match (case-insensitive)
        if project_name and project_name in repo_names_lower:
            conv["matched_repo"] = repo_names_lower[project_name]
            conv["match_type"] = "auto"
            conv["match_reason"] = (
                f'Name match: Claude project "{conv.get("project")}" '
                f'= repo "{repo_names_lower[project_name]}"'
            )
            auto_matched.append(conv)
            matched = True

        # 2. Project name contains repo name or vice versa
        if not matched and project_name:
            for repo_lower, repo_actual in repo_names_lower.items():
                if repo_lower in project_name or project_name in repo_lower:
                    conv["matched_repo"] = repo_actual
                    conv["match_type"] = "auto"
                    conv["match_reason"] = (
                        f'Name match: "{conv.get("project")}" ~ "{repo_actual}"'
                    )
                    auto_matched.append(conv)
                    matched = True
                    break

        # 3. Content-based matching — look for repo name in topic or excerpt
        if not matched:
            best_score = 0
            best_repo = None
            best_keywords = []

            for repo_lower, repo_actual in repo_names_lower.items():
                score = 0
                keywords = []

                # Check for repo name parts in content
                repo_parts = re.split(r"[-_]", repo_lower)
                for part in repo_parts:
                    if len(part) >= 3:  # Skip tiny parts
                        if part in topic:
                            score += 3
                            keywords.append(part)
                        if part in excerpt:
                            score += 2
                            keywords.append(part)

                # Check for repo name as whole
                if repo_lower.replace("-", " ") in topic:
                    score += 5
                    keywords.append(repo_lower.replace("-", " "))
                if repo_lower.replace("-", " ") in excerpt:
                    score += 4
                    keywords.append(repo_lower.replace("-", " "))

                if score > best_score:
                    best_score = score
                    best_repo = repo_actual
                    best_keywords = list(set(keywords))

            if best_score >= 4 and best_repo:
                conv["matched_repo"] = best_repo
                conv["match_type"] = "suggested"
                kw_str = '", "'.join(best_keywords[:4])
                conv["match_reason"] = (
                    f'Content match: mentions "{kw_str}" '
                    f'— likely matches repo "{best_repo}"'
                )
                suggested.append(conv)
                matched = True

        if not matched:
            conv["matched_repo"] = None
            conv["match_type"] = "none"
            conv["match_reason"] = ""
            unmatched.append(conv)

    return {
        "auto_matched": auto_matched,
        "suggested": suggested,
        "unmatched": unmatched,
        "stats": {
            "total": len(conversations),
            "auto": len(auto_matched),
            "suggested": len(suggested),
            "unmatched": len(unmatched),
        },
    }


def assign_conversation(conv_id: str, repo_name: str):
    """Manually assign a conversation to a repo."""
    config = _load_config()
    config.setdefault("mappings", {})[conv_id] = repo_name
    _save_config(config)


def dismiss_conversation(conv_id: str):
    """Dismiss an unmatched conversation."""
    config = _load_config()
    dismissed = config.setdefault("dismissed", [])
    if conv_id not in dismissed:
        dismissed.append(conv_id)
    _save_config(config)


def get_conversations_for_repo(repo_name: str) -> list[dict]:
    """Get all conversations mapped to a specific repo, for timeline view."""
    conversations = get_conversations()
    config = _load_config()
    mappings = config.get("mappings", {})
    repo_names_lower = {repo_name.lower()}

    result = []
    for conv in conversations:
        # Check manual mapping
        if mappings.get(conv.get("id")) == repo_name:
            result.append(conv)
            continue
        # Check project name match
        if conv.get("project", "").lower() == repo_name.lower():
            result.append(conv)
            continue

    result.sort(key=lambda c: c.get("date", ""), reverse=True)
    return result
