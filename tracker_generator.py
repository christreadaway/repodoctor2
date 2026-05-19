"""
Codebase Tracker — AI generation pipeline.

Reads a repo's docs + code map + recent commits and asks Claude to
produce a structured tracker JSON (PRD §6 shape). On regeneration,
the prior tracker is included in the prompt so the model preserves
every existing M*/I*/F*/E*/Q*/N* ID (PRD §5.1).

Output is validated against tracker_data.validate_tracker before
being saved; an invalid AI response triggers a retry, then a graceful
error returned to the route.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any

import anthropic

import tracker_data as td
from ai_analyzer import extract_json_object

logger = logging.getLogger(__name__)

# Vendor / build dirs we never want in the file tree handed to the model.
VENDOR_DIRS = {
    "node_modules", "dist", ".venv", "venv", "env", "__pycache__",
    "target", "vendor", ".next", ".nuxt", ".cache", "coverage",
    ".tox", "bower_components", "site-packages", ".git",
}

# Truncation budgets — keep prompt under ~50k input tokens on Haiku.
MAX_DOC_CHARS = 8000
MAX_TREE_PATHS = 250
MAX_COMMITS = 50
MAX_PRIOR_TRACKER_CHARS = 8000

SYSTEM_PROMPT = """You are a codebase analyst producing a structured tracker for a software project, following a strict JSON schema.

You read the repo's docs (PRODUCT_SPEC, PROJECT_STATUS, SESSION_NOTES, README, CLAUDE.md), a list of file paths, and recent commit titles. You output a single JSON object that classifies every meaningful module, lists infra gaps that block multiple modules, surfaces feature ideas worth tracking, names external systems the app depends on, captures open questions the code can't answer, and proposes copy/paste-ready next actions for Claude Code.

Hard rules:

1. Return ONLY valid JSON. No markdown fences, no leading prose, no trailing commentary. The first character is `{`.
2. IDs are stable. If the user supplies a "prior_tracker" object, every ID it contains MUST appear in your response with the same prefix+integer. Add NEW rows with the next unused integer per prefix (e.g. if prior has M1..M7, new modules start at M8).
3. Never reuse a deleted integer. Always pick max(prefix)+1 for new rows.
4. Every `next_actions[].prompt` is a complete copy/paste-ready instruction to Claude Code: opens with a one-sentence goal, then numbered steps, then acceptance criteria. Minimum 50 characters. Reference the M/I/F IDs the action touches.
5. Every `next_actions[].related_ids`, every `infra_gaps[].blocks`, every `features[].modules`, every `recent_changes[].related_ids` must point at IDs you actually defined in this same response. No dangling references.
6. Status enums are exact strings — never paraphrase:
   - module.status: "functional" | "prototype" | "visual" | "missing"
   - priority / build_priority / roll_priority / next_action.priority: "P0" | "P1" | "P2" | "P3" | "—"
   - feature.status: "Proposed" | "In Discussion" | "In Progress" | "Shipped" | "Abandoned"
   - effort: "XS" | "S" | "M" | "L" | "XL"
   - next_action.status: "todo" | "in_progress" | "awaiting_deploy" | "shipped"
   - change.kind: "shipped" | "unblocked" | "doc" | "fix" | "blocked"
   - external_system.mode: "Core" | "Integrate" | "Replace" | "Optional"
7. Be ruthless about scope. A module is a real surface in the app (a route, a page, a domain). Don't list every helper function. 5-25 modules for most repos.
8. Infra gaps only when ≥2 modules are blocked. If exactly one module is blocked, fold the note into that module's `notes` field.
9. `recent_changes` are newest-first, dated YYYY-MM-DD, drawn from the supplied commit titles + SESSION_NOTES timeline. Group related commits into one entry.
10. `questions` capture genuine ambiguity ("which Stripe model do we use?"), not "what should I do next?" — that's a next_action.

Output schema (every field required unless marked optional):

{
  "modules": [
    {"id": "M1", "name": "Dashboard", "category": "Core", "routes": ["/"], "status": "functional", "priority": "—", "notes": "One-paragraph context."}
  ],
  "infra_gaps": [
    {"id": "I1", "name": "Cloud Functions not deployed", "blocks": ["M3", "M5"], "priority": "P0", "description": "What's missing and what unblocks it."}
  ],
  "features": [
    {"id": "F1", "name": "...", "modules": ["M1"], "build_priority": "P1", "roll_priority": "P0", "take": "2-4 sentences.", "spec": "docs/specs/x.md", "status": "Proposed", "built_note": "(optional, only when status implies built)"}
  ],
  "external_systems": [
    {"id": "E1", "name": "Anthropic API", "what": "What the app uses it for.", "mode": "Core", "migration": "Setup notes."}
  ],
  "questions": [
    {"id": "Q1", "group": "Roadmap & priorities", "text": "Open question."}
  ],
  "next_actions": [
    {"id": "N1", "title": "Short verb phrase", "related_ids": ["M1", "I1"], "why": "1 sentence.", "effort": "S", "priority": "P0", "prompt": "Full copy/paste Claude Code prompt — goal, numbered steps, acceptance criteria. Minimum 50 chars.", "depends_on": [], "status": "todo", "status_note": ""}
  ],
  "recent_changes": [
    {"date": "2026-05-19", "title": "Shipped X", "kind": "shipped", "related_ids": ["M1", "N1"], "description": "One sentence."}
  ],
  "build_sequence": ["Ordered narrative bullet 1", "Bullet 2"],
  "rollout_sequence": ["Ordered narrative bullet 1", "Bullet 2"]
}

If a section has no items, return an empty array — do not omit the key."""


# ----------------------------------------------------------------------
# Repo content gathering
# ----------------------------------------------------------------------

def gather_repo_inputs(
    client,
    owner: str,
    repo: str,
    default_branch: str,
    actual_paths: dict | None = None,
) -> dict:
    """Pull every input the AI needs about a repo. Returns a dict of
    raw strings + file tree + commit titles. Callers can pass actual
    spec paths (from check_required_files) to avoid a second tree walk."""
    inputs: dict[str, Any] = {
        "docs": {},
        "file_tree": [],
        "recent_commits": [],
        "readme": "",
    }

    # Documents the user mandates in PRODUCT_SPEC §4.3
    if actual_paths is None:
        _, actual_paths = client.check_required_files(owner, repo, ref=default_branch)

    doc_keys = {
        "PRODUCT_SPEC.md": "product_spec",
        "PROJECT_STATUS.md": "project_status",
        "SESSION_NOTES.md": "session_notes",
        "CLAUDE.md": "claude",
    }
    for filename, key in doc_keys.items():
        path = actual_paths.get(filename)
        if not path:
            continue
        try:
            content = client.get_file_content(owner, repo, path, ref=default_branch)
            if content:
                inputs["docs"][key] = content[:MAX_DOC_CHARS]
        except Exception as e:
            logger.warning("tracker: failed to fetch %s for %s/%s: %s", path, owner, repo, e)

    # README — try common variants at root
    for readme_name in ("README.md", "readme.md", "README.rst", "README"):
        try:
            content = client.get_file_content(owner, repo, readme_name, ref=default_branch)
            if content:
                inputs["readme"] = content[:MAX_DOC_CHARS]
                break
        except Exception:
            continue

    # File tree
    try:
        all_paths = client.get_all_file_paths(owner, repo, ref=default_branch) or []
    except Exception as e:
        logger.warning("tracker: get_all_file_paths failed for %s/%s: %s", owner, repo, e)
        all_paths = []

    filtered: list[str] = []
    for p in all_paths:
        parts = p.split("/")
        if any(part in VENDOR_DIRS for part in parts):
            continue
        if p.endswith(("/.gitkeep",)) or p == ".gitignore":
            continue
        filtered.append(p)
    # Bias toward source/template/config dirs over random noise
    filtered.sort()
    inputs["file_tree"] = filtered[:MAX_TREE_PATHS]
    inputs["file_tree_truncated"] = len(filtered) > MAX_TREE_PATHS

    # Recent commits
    try:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
        commits = client.get_commits_since(owner, repo, since.isoformat())
        for c in (commits or [])[:MAX_COMMITS]:
            msg = (c.get("commit", {}).get("message") or "").split("\n")[0]
            date = (c.get("commit", {}).get("committer", {}) or {}).get("date", "")[:10]
            if msg:
                inputs["recent_commits"].append({"date": date, "title": msg})
    except Exception as e:
        logger.warning("tracker: get_commits_since failed for %s/%s: %s", owner, repo, e)

    return inputs


# ----------------------------------------------------------------------
# Prompt construction
# ----------------------------------------------------------------------

def build_user_prompt(
    owner: str,
    repo: str,
    inputs: dict,
    prior_tracker: dict | None,
) -> str:
    """Compose the user-side prompt block."""
    parts: list[str] = [
        f"Repository: {owner}/{repo}",
    ]

    # Prior tracker — drives ID preservation
    if prior_tracker:
        prior_compact = _compact_prior(prior_tracker)
        as_json = json.dumps(prior_compact, indent=2)
        if len(as_json) > MAX_PRIOR_TRACKER_CHARS:
            as_json = as_json[:MAX_PRIOR_TRACKER_CHARS] + "\n... (truncated)"
        parts.append(
            "\nPRIOR TRACKER — preserve every ID; update fields as needed; "
            "add new rows with the next unused integer per prefix.\n"
            f"```json\n{as_json}\n```"
        )
        ids = td.collect_existing_ids(prior_tracker)
        flat = [v for vs in ids.values() for v in vs]
        if flat:
            parts.append("\nLOAD-BEARING IDS that MUST appear in output: " + ", ".join(sorted(flat)))

    # Docs
    for key, val in inputs.get("docs", {}).items():
        parts.append(f"\n--- {key.upper().replace('_', ' ')} ---\n{val}")
    if inputs.get("readme"):
        parts.append(f"\n--- README ---\n{inputs['readme']}")

    # File tree
    tree = inputs.get("file_tree") or []
    if tree:
        parts.append(
            "\n--- FILE TREE (paths only, vendor dirs excluded) ---\n"
            + "\n".join(tree)
            + ("\n... (more files omitted)" if inputs.get("file_tree_truncated") else "")
        )

    # Recent commits
    commits = inputs.get("recent_commits") or []
    if commits:
        parts.append("\n--- COMMITS (last 30 days, newest first) ---")
        for c in commits:
            date = c.get("date") or "????-??-??"
            parts.append(f"  {date} — {c.get('title', '')}")

    parts.append(
        "\nProduce the tracker JSON now. Return ONLY the JSON object. "
        "Every section is required — use an empty array when there's nothing to list."
    )

    return "\n".join(parts)


def _compact_prior(prior: dict) -> dict:
    """Return a smaller prior-tracker view focusing on the load-bearing
    fields: IDs, names, statuses, priorities. Saves prompt tokens."""
    compact: dict = {}
    for key in ("modules", "infra_gaps", "features", "external_systems",
                "questions", "next_actions"):
        rows = []
        for r in prior.get(key, []) or []:
            rows.append({k: v for k, v in r.items()
                         if k in {"id", "name", "title", "text", "status",
                                  "priority", "build_priority", "roll_priority",
                                  "mode", "category"}})
        compact[key] = rows
    return compact


# ----------------------------------------------------------------------
# Generate
# ----------------------------------------------------------------------

class TrackerGenerationError(Exception):
    """Raised when the AI response cannot be parsed or fails validation
    after retry."""


def generate_tracker(
    api_key: str,
    owner: str,
    repo: str,
    default_branch: str,
    inputs: dict,
    prior_tracker: dict | None = None,
    model: str = "claude-haiku-4-5-20251001",
    max_attempts: int = 2,
) -> dict:
    """Run the AI generation. Returns a validated tracker dict + _usage."""
    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = build_user_prompt(owner, repo, inputs, prior_tracker)

    last_error: str = ""
    last_raw: str = ""
    usage = {"input_tokens": 0, "output_tokens": 0, "model": model}

    for attempt in range(1, max_attempts + 1):
        logger.info("tracker.generate %s/%s attempt %d/%d (model=%s)",
                    owner, repo, attempt, max_attempts, model)
        message = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        last_raw = message.content[0].text.strip()
        usage["input_tokens"] += message.usage.input_tokens
        usage["output_tokens"] += message.usage.output_tokens

        try:
            parsed = extract_json_object(last_raw)
        except (ValueError, json.JSONDecodeError) as e:
            last_error = f"JSON parse failed: {e}"
            logger.warning("tracker.generate parse failed (attempt %d): %s",
                           attempt, e)
            continue

        # Compose final tracker dict and validate
        tracker = td.empty_tracker(owner, repo)
        for key in ("modules", "infra_gaps", "features", "external_systems",
                    "questions", "next_actions", "recent_changes",
                    "build_sequence", "rollout_sequence"):
            value = parsed.get(key)
            if value is None:
                continue
            tracker[key] = value

        tracker["generated_at"] = datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        tracker["branch_at_verification"] = default_branch
        tracker["ai_model"] = model

        errors = td.validate_tracker(tracker)
        if not errors:
            tracker["_usage"] = usage
            logger.info("tracker.generate %s/%s OK on attempt %d", owner, repo, attempt)
            return tracker

        last_error = "validation failed: " + "; ".join(errors[:5])
        logger.warning("tracker.generate %s/%s validation failed (attempt %d): %s",
                       owner, repo, attempt, last_error)

        # Retry: append a corrective message
        user_prompt += (
            "\n\nYour previous response failed validation:\n"
            + "\n".join(f"  - {e}" for e in errors[:8])
            + "\nReturn corrected JSON only — keep all IDs stable."
        )

    raise TrackerGenerationError(
        f"AI failed to produce valid tracker after {max_attempts} attempts. "
        f"Last error: {last_error}. Last raw (first 200 chars): {last_raw[:200]}"
    )
