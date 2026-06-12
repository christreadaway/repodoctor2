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

import github_client as gh
import tracker_data as td
import firestore_detector
from ai_analyzer import extract_json_object

logger = logging.getLogger(__name__)

# Vendor / build dirs we never want in the file tree handed to the model.
VENDOR_DIRS = {
    "node_modules", "dist", ".venv", "venv", "env", "__pycache__",
    "target", "vendor", ".next", ".nuxt", ".cache", "coverage",
    ".tox", "bower_components", "site-packages", ".git",
}

# Truncation budgets — keep prompt under ~50k input tokens on Haiku and
# leave plenty of output room. Complex repos (parentpoint, catholicevents)
# can push the output to 12K+ tokens once next-action prompts, modules,
# and recent_changes are populated, so generation runs with max_tokens=16000.
MAX_DOC_CHARS = 6000
MAX_TREE_PATHS = 200
MAX_COMMITS = 40
MAX_PRIOR_TRACKER_CHARS = 8000

SYSTEM_PROMPT = """You are a codebase analyst producing a structured tracker for a software project, following a strict JSON schema.

You read the repo's docs (PRODUCT_SPEC, PROJECT_STATUS, SESSION_NOTES, README, CLAUDE.md), a list of file paths, and recent commit titles. You output a single JSON object that classifies every meaningful module, lists infra gaps that block multiple modules, surfaces feature ideas worth tracking, names external systems the app depends on, captures open questions the code can't answer, and proposes copy/paste-ready next actions for Claude Code.

Hard rules:

1. Return ONLY valid JSON. No markdown fences, no leading prose, no trailing commentary. The first character is `{`.
2. IDs are stable. If the user supplies a "prior_tracker" object, every ID it contains MUST appear in your response with the same prefix+integer. Add NEW rows with the next unused integer per prefix (e.g. if prior has M1..M7, new modules start at M8).
2a. USER-DRIVEN STATUSES — preserve exactly. If a prior next_action has status "blocked", "dismissed", "in_progress", or "awaiting_deploy", keep that exact value AND the existing `status_note`. The human set those manually; do not override. You may freely update status from "todo" → other states based on SESSION_NOTES evidence.
3. Never reuse a deleted integer. Always pick max(prefix)+1 for new rows.
4. Every `next_actions[].prompt` is a complete copy/paste-ready instruction to Claude Code: opens with a one-sentence goal, then numbered steps, then acceptance criteria. Minimum 50 characters, aim for 200-600 characters — concise but complete. Reference the M/I/F IDs the action touches.
5. Every `next_actions[].related_ids`, every `infra_gaps[].blocks`, every `features[].modules`, every `recent_changes[].related_ids` must point at IDs you actually defined in this same response. No dangling references.
6. Status enums are exact strings — never paraphrase:
   - module.status: "functional" | "prototype" | "visual" | "missing"
   - priority / build_priority / roll_priority / next_action.priority: "P0" | "P1" | "P2" | "P3" | "—"
   - feature.status: "Proposed" | "In Discussion" | "In Progress" | "Shipped" | "Abandoned"
   - effort: "XS" | "S" | "M" | "L" | "XL"
   - next_action.status: "todo" | "in_progress" | "awaiting_deploy" | "shipped"
   - change.kind: "shipped" | "unblocked" | "doc" | "fix" | "blocked"
   - external_system.mode: "Core" | "Integrate" | "Replace" | "Optional"
7. Be ruthless about scope. HARD CAPS — exceeding any of these is a failure of the task:
   - At most 25 modules. Group sub-screens under one module. Skip helpers and one-off utilities.
   - At most 8 infra_gaps. Only list a gap when ≥2 modules are blocked. Otherwise fold into the module's `notes`.
   - At most 12 features. Skip features that ship without a spec doc.
   - At most 12 external_systems. Combine related services (e.g. all Firebase services → one E-row).
   - At most 15 questions. Only genuine ambiguity that needs a human decision.
   - At most 15 next_actions. Prioritise P0/P1 work + every infra gap. Drop P3 items; they don't earn their place in this list.
   - At most 20 recent_changes. Newest-first, group commits that ship together.
   - At most 8 items each in build_sequence and rollout_sequence.
   If you can't fit everything, prefer (a) all P0/P1 next_actions over P2/P3, (b) modules with status != "functional" over fully-finished ones, (c) infra_gaps blocking more modules over fewer.
8. Infra gaps only when ≥2 modules are blocked. If exactly one module is blocked, fold the note into that module's `notes` field.
9. `recent_changes` are newest-first, dated YYYY-MM-DD, drawn from the supplied commit titles + SESSION_NOTES timeline. Group related commits into one entry.
10. `questions` capture genuine ambiguity ("which Stripe model do we use?"), not "what should I do next?" — that's a next_action.
11. Keep prose tight throughout. `notes`, `description`, `take`, `why`, `what`, `migration` are all 1-3 sentences max — never a paragraph.

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
        "firestore": None,
    }

    # Documents the user mandates in PRODUCT_SPEC §4.3, plus README —
    # via the shared doc-fetch path (github_client.fetch_repo_docs).
    fetched = gh.fetch_repo_docs(
        client, owner, repo, ref=default_branch,
        max_chars=MAX_DOC_CHARS, actual_paths=actual_paths,
        include_readme="always",
    )
    inputs["docs"] = fetched["docs"]
    inputs["readme"] = fetched["readme"]

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

    # Firestore / Firebase auto-detection. Quietly skipped if the repo
    # doesn't use Firebase. When it does, the AI receives status +
    # indicators + missing config so it can emit an E* row for Firestore
    # and I* rows for any missing setup.
    try:
        fs = firestore_detector.detect_firestore_status(
            client, owner, repo, default_branch,
        )
        if fs and fs.get("status") != "not_using":
            inputs["firestore"] = {
                "status": fs.get("status"),
                "project_id": fs.get("project_id"),
                "site_domain": fs.get("site_domain"),
                "indicators": fs.get("indicators", []),
                "missing": fs.get("missing", []),
            }
    except Exception as e:
        logger.warning("tracker: firestore detect failed for %s/%s: %s", owner, repo, e)

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

    # Firestore auto-detection — fed in only when the repo uses Firebase.
    fs = inputs.get("firestore")
    if fs:
        parts.append("\n--- FIRESTORE / FIREBASE DETECTION ---")
        parts.append(f"  Status: {fs.get('status', 'unknown')}")
        if fs.get("project_id"):
            parts.append(f"  Project ID: {fs['project_id']}")
        if fs.get("site_domain"):
            parts.append(f"  Hosting site: {fs['site_domain']}")
        if fs.get("indicators"):
            parts.append("  Indicators:")
            for ind in fs["indicators"]:
                parts.append(f"    - {ind}")
        if fs.get("missing"):
            parts.append("  Missing config:")
            for m in fs["missing"]:
                parts.append(f"    - {m}")
        parts.append(
            "  REQUIRED: emit a Firestore/Firebase row in `external_systems` "
            "(name: 'Firestore', mode: 'Core' if status=configured else 'Integrate', "
            "migration: summarise what's already set up vs. needs setup). "
            "For each item in 'Missing config', emit a corresponding `infra_gaps` row "
            "(priority P0 if status=needs_setup, otherwise P2), and have at least one "
            "`next_actions` entry per gap with a copy/paste Claude Code prompt to fix it."
        )

    parts.append(
        "\nProduce the tracker JSON now. Return ONLY the JSON object. "
        "Every section is required — use an empty array when there's nothing to list."
    )

    return "\n".join(parts)


def _compact_prior(prior: dict) -> dict:
    """Return a smaller prior-tracker view focusing on the load-bearing
    fields: IDs, names, statuses, priorities. Saves prompt tokens.
    Defensively skips non-list sections or non-dict rows in case a
    hand-edited tracker file got into a weird state."""
    compact: dict = {}
    keep_fields = {"id", "name", "title", "text", "status",
                   "priority", "build_priority", "roll_priority",
                   "mode", "category", "status_note"}
    for key in ("modules", "infra_gaps", "features", "external_systems",
                "questions", "next_actions"):
        section = prior.get(key) or []
        if not isinstance(section, list):
            compact[key] = []
            continue
        rows = []
        for r in section:
            if not isinstance(r, dict):
                continue
            rows.append({k: v for k, v in r.items() if k in keep_fields})
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
        # Haiku 4.5 supports up to 64K output tokens; 32K gives enough room
        # for any reasonable tracker even on complex repos like parentpoint
        # while still failing fast if the model starts writing essays.
        #
        # Streaming is REQUIRED for large max_tokens: the Anthropic SDK
        # refuses non-streaming requests that could exceed a ~10-minute
        # wall-clock budget. We accumulate the text chunks, then pull
        # usage + stop_reason off the final message.
        with client.messages.stream(
            model=model,
            max_tokens=32000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            chunks: list[str] = []
            for piece in stream.text_stream:
                chunks.append(piece)
            final = stream.get_final_message()

        last_raw = ("".join(chunks)).strip()
        usage["input_tokens"] += final.usage.input_tokens
        usage["output_tokens"] += final.usage.output_tokens
        stop_reason = getattr(final, "stop_reason", None)

        # Truncation: stop_reason == "max_tokens" means the JSON is cut off
        # mid-output and no retry will help — fail fast with a clear message.
        if stop_reason == "max_tokens":
            raise TrackerGenerationError(
                "AI response was truncated (hit the 32000-token output cap) "
                "despite hard caps in the prompt. The model ignored the "
                "scope limits. Try: (1) switch to Sonnet in Settings; "
                "(2) regenerate — output varies run to run and the next "
                "attempt often fits. "
                f"Tokens used: input={usage['input_tokens']}, "
                f"output={usage['output_tokens']}."
            )

        try:
            parsed = extract_json_object(last_raw)
        except (ValueError, json.JSONDecodeError) as e:
            last_error = f"JSON parse failed: {e}"
            logger.warning("tracker.generate parse failed (attempt %d): %s",
                           attempt, e)
            continue

        # Compose final tracker dict and validate.
        # Type-guard each section: if the model returns something other than
        # a list for a list-typed field (rare but happens with malformed
        # output), fall back to the empty default rather than crashing the
        # validator's iteration loop downstream.
        tracker = td.empty_tracker(owner, repo)
        if not isinstance(parsed, dict):
            last_error = f"AI returned a non-object top-level value: {type(parsed).__name__}"
            logger.warning("tracker.generate %s/%s: %s", owner, repo, last_error)
            continue
        for key in ("modules", "infra_gaps", "features", "external_systems",
                    "questions", "next_actions", "recent_changes",
                    "build_sequence", "rollout_sequence"):
            value = parsed.get(key)
            if value is None:
                continue
            if not isinstance(value, list):
                logger.warning(
                    "tracker.generate %s/%s: '%s' was %s, expected list — using empty",
                    owner, repo, key, type(value).__name__,
                )
                continue
            tracker[key] = value

        tracker["generated_at"] = datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        tracker["branch_at_verification"] = default_branch
        tracker["ai_model"] = model

        # Normalise things the model gets cosmetically wrong before we
        # validate — sort recent_changes newest-first regardless of the
        # order the model emitted them in.
        td.sort_recent_changes(tracker)

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
