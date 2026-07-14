"""Program view — a group of repos rolled up as one initiative.

The Program screen answers "where is this PROGRAM?" for a set of repos
that ship together (e.g. the Education suite: parentpoint + teacherAIde +
beacon), including the pieces that aren't repos at all — a local LLM
running familygraph, beacon's own local LLM — captured as free-text
infrastructure notes.

The AI program brief is composed from data RepoDoctor already has (the
per-repo chat briefs, project summaries, tracker facts, and scan data),
so generation makes exactly one API call and needs no GitHub fetches.
Briefs are cached in data/program_briefs.json and considered stale when
membership changes or any member repo is pushed to after generation.
"""

from __future__ import annotations

import logging

import anthropic

from ai_analyzer import DEFAULT_MODEL, extract_json_object, extract_response_text
from briefing import (
    UNKNOWN_STAGE,
    _parse_ts,
    last_push_ts,
    open_tracker_actions,
)

logger = logging.getLogger(__name__)

# "Mixed" is program-only: member repos can legitimately sit at different
# stages, and forcing a single repo-style stage would hide that.
PROGRAM_STAGES = ("Idea", "Requirements", "Building", "Testing", "Live", "Paused", "Mixed")

MAX_BUILT = 10
MAX_LEFT = 10
MAX_DECISIONS = 6
MAX_RISKS = 5

SYSTEM_PROMPT = """You are a portfolio analyst writing a PROGRAM briefing — a rollup of several related software projects that ship together as one initiative, plus shared infrastructure that isn't a repo (e.g. a locally-hosted LLM). The owner is a product builder, not a developer.

Ground every claim in the provided material (per-project briefs, summaries, tracker facts, infrastructure notes). If the material doesn't say, write less — never invent features, integrations, or progress.

Return ONLY valid JSON (no markdown fences, no prose before or after) with exactly these fields:
{
  "what_it_is": "2-4 sentences: what the program as a whole is, the business problem it solves, and who it serves. Lead with the problem, not the tech.",
  "architecture": "2-5 sentences: how the pieces fit together — which app does what, what they share, and where non-repo components (local LLMs, shared data layers) sit. Only what's evidenced.",
  "stage": "Idea | Requirements | Building | Testing | Live | Paused | Mixed",
  "stage_note": "One sentence of evidence — if members are at different stages, name which is where.",
  "where_we_are": "2-4 sentences: the program's current state as a narrative across projects.",
  "whats_built": ["Up to 10 short bullets of what exists and works today, prefixed by the project it belongs to (e.g. 'parentpoint: ...'). Empty array if nothing is built."],
  "whats_left": ["Up to 10 short bullets of remaining work ACROSS the program, sequenced most-important-first, each prefixed by the project (or 'shared:' for infrastructure work)."],
  "open_decisions": ["Up to 6 bullets — decisions only the owner can make, program-level first. Empty array if none surfaced."],
  "risks": ["Up to 5 bullets — cross-project risks and dependencies (one app blocking another, shared infrastructure not ready, sequencing conflicts). Empty array if none."]
}

Rules:
- Plain English throughout; the owner is a non-developer.
- Bullets are short (aim under 15 words) and concrete.
- Pay special attention to the DEPENDENCIES BETWEEN projects and on shared infrastructure — that's what a per-project view can't show."""


def assemble_members(repos: list[dict], briefs: dict, summaries: dict,
                     trackers: dict) -> list[dict]:
    """One display dict per member repo, newest-push-first — the same
    cached sources the Briefing screen uses, so no GitHub calls."""
    members: list[dict] = []
    for repo in repos:
        name = repo["name"]
        brief = briefs.get(name)
        summary = summaries.get(name) or {}
        tracker = trackers.get(f"{repo['owner']}/{name}")
        open_actions = open_tracker_actions(tracker)
        one_liner = (
            (brief or {}).get("what_it_is")
            or summary.get("what_it_does")
            or repo.get("description")
            or "No brief or description yet."
        )
        members.append({
            "owner": repo["owner"],
            "name": name,
            "html_url": repo.get("html_url", ""),
            "stage": (brief or {}).get("stage") or UNKNOWN_STAGE,
            "one_liner": one_liner,
            "last_push": last_push_ts(repo)[:10] or "unknown",
            "has_brief": bool(brief),
            "brief": brief,
            "summary": summary,
            "open_actions": open_actions,
            "open_p0": sum(1 for a in open_actions if a["priority"] == "P0"),
        })
    members.sort(key=lambda m: m["last_push"], reverse=True)
    return members


def build_program_context(program_name: str, notes: str,
                          members: list[dict]) -> str:
    """The prompt context for one program — cached data only."""
    parts: list[str] = [f"Program: {program_name}"]
    if notes.strip():
        parts.append("--- INFRASTRUCTURE / COMPONENT NOTES (from the owner) ---\n" + notes.strip())

    for m in members:
        lines = [f"--- PROJECT: {m['name']} (last push {m['last_push']}) ---"]
        brief = m.get("brief")
        if brief:
            for key, label in (
                ("what_it_is", "What it is"), ("stack", "Stack"),
                ("stage", "Stage"), ("stage_note", "Stage evidence"),
                ("where_we_are", "Where we are"),
            ):
                if brief.get(key):
                    lines.append(f"{label}: {brief[key]}")
            for key, label in (("whats_built", "Built"), ("whats_left", "Left"),
                               ("open_decisions", "Open decisions")):
                for item in (brief.get(key) or [])[:8]:
                    lines.append(f"{label}: {item}")
        else:
            summary = m.get("summary") or {}
            if summary.get("what_it_does"):
                lines.append(f"What it does: {summary['what_it_does']}")
            if summary.get("how_finished"):
                lines.append(f"How finished: {summary['how_finished']}")
            for step in (summary.get("next_steps") or [])[:5]:
                lines.append(f"Next: {step}")
            if len(lines) == 1:
                lines.append(f"One-liner: {m['one_liner']}")
        for a in m.get("open_actions", [])[:6]:
            lines.append(f"Tracker action [{a['priority']}] {a['title']} ({a['status']})")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def generate_program_brief(api_key: str, program_name: str, context_text: str,
                           model: str = DEFAULT_MODEL) -> dict:
    """Generate the program brief. Returns normalized dict + _usage."""
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=2500,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"{context_text}\n\n"
                "Write the program briefing JSON now. Return ONLY the JSON object."
            ),
        }],
    )
    raw = extract_response_text(message)
    brief = normalize_program_brief(extract_json_object(raw))
    brief["_usage"] = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "model": model,
    }
    return brief


def normalize_program_brief(raw: dict) -> dict:
    """Coerce AI output into the program-brief shape."""
    if not isinstance(raw, dict):
        raw = {}

    def _text(key: str) -> str:
        v = raw.get(key)
        return v.strip() if isinstance(v, str) else ""

    def _bullets(key: str, cap: int) -> list[str]:
        v = raw.get(key)
        if not isinstance(v, list):
            return []
        return [s.strip() for s in v if isinstance(s, str) and s.strip()][:cap]

    stage = _text("stage")
    if stage not in PROGRAM_STAGES:
        stage = UNKNOWN_STAGE

    return {
        "what_it_is": _text("what_it_is"),
        "architecture": _text("architecture"),
        "stage": stage,
        "stage_note": _text("stage_note"),
        "where_we_are": _text("where_we_are"),
        "whats_built": _bullets("whats_built", MAX_BUILT),
        "whats_left": _bullets("whats_left", MAX_LEFT),
        "open_decisions": _bullets("open_decisions", MAX_DECISIONS),
        "risks": _bullets("risks", MAX_RISKS),
    }


def is_program_brief_stale(brief: dict | None, member_repos: list[dict],
                           member_names: list[str]) -> bool:
    """Stale when membership changed or any member was pushed to after the
    brief was generated. Missing timestamps count as stale (self-healing)."""
    if not brief:
        return True
    generated = _parse_ts(brief.get("_generated_at", ""))
    if generated is None:
        return True
    if set(brief.get("_members") or []) != set(member_names):
        return True
    for repo in member_repos:
        pushed = _parse_ts(last_push_ts(repo))
        if pushed is not None and pushed > generated:
            return True
    return False


def compose_markdown(program_name: str, notes: str, members: list[dict],
                     brief: dict | None, generated_label: str) -> str:
    """The copy-for-Claude-chat document for one program."""
    lines: list[str] = [
        f"# {program_name} — Program Briefing ({generated_label})",
        "",
        f"> {len(members)} projects rolled up as one initiative. Generated by RepoDoctor.",
        "",
    ]
    if notes.strip():
        lines += ["## Infrastructure / components", "", notes.strip(), ""]

    lines += ["## Projects", ""]
    for m in members:
        lines.append(f"- **{m['name']}** — {m['stage']} · last push {m['last_push']} · {m['one_liner']}")
    lines.append("")

    if brief:
        if brief.get("what_it_is"):
            lines += ["## What this program is", "", brief["what_it_is"], ""]
        if brief.get("architecture"):
            lines += ["## How the pieces fit", "", brief["architecture"], ""]
        where = " ".join(s for s in (brief.get("stage_note"), brief.get("where_we_are")) if s)
        if where:
            lines += [f"## Where we are — {brief.get('stage', UNKNOWN_STAGE)}", "", where, ""]
        for key, title, numbered in (
            ("whats_built", "What's built", False),
            ("whats_left", "What's left (program-wide)", True),
            ("open_decisions", "Open decisions (owner)", False),
            ("risks", "Cross-project risks & dependencies", False),
        ):
            items = brief.get(key) or []
            if items:
                lines.append(f"## {title}")
                lines.append("")
                if numbered:
                    lines += [f"{i}. {b}" for i, b in enumerate(items, 1)]
                else:
                    lines += [f"- {b}" for b in items]
                lines.append("")
    return "\n".join(lines)
