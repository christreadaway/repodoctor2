"""
Chat Briefing — cross-project snapshot for pasting into a Claude chat.

Each repo gets an AI-generated "chat brief" (modeled on the CHAT_BRIEFING.md
format: what it is, where we are, what's built, what's left, open decisions,
constraints). The briefing screen assembles every brief together with hard
facts from the latest scan + the repo's tracker into one Markdown document
that answers "where am I across all my projects?" in a single read.

Briefs are cached in data/briefs.json and only regenerated when the repo
has been pushed to since the brief was generated (or on force-regenerate).
"""

from __future__ import annotations

import datetime
import json
import logging

import anthropic

import github_client as gh
from ai_analyzer import DEFAULT_MODEL, extract_json_object

logger = logging.getLogger(__name__)

STAGES = ("Idea", "Requirements", "Building", "Testing", "Live", "Paused")
UNKNOWN_STAGE = "Unknown"

# Next-action statuses that count as "open" work when summarizing a tracker.
OPEN_ACTION_STATUSES = ("todo", "in_progress", "blocked", "awaiting_deploy")
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "—": 4}

MAX_DOC_CHARS = 5000

# Caps applied to AI output so one chatty repo can't dominate the document.
MAX_BUILT = 10
MAX_LEFT = 10
MAX_DECISIONS = 5
MAX_CONSTRAINTS = 4

SYSTEM_PROMPT = """You are a portfolio analyst writing a "chat briefing" about ONE software project. The owner is a product builder, not a developer. He will paste briefings for every project into an AI chat session as shared context, so each briefing must answer "where is this project?" in one read — specific, factual, plain English.

Ground every claim in the provided material (docs, tracker facts, repo metadata). If the material doesn't say, write less — never invent features, integrations, or progress.

Return ONLY valid JSON (no markdown fences, no prose before or after) with exactly these fields:
{
  "what_it_is": "2-4 sentences: what the project is, the business problem it solves, who it's for, and its positioning. Lead with the problem, not the tech.",
  "stack": "1-2 sentences: tech stack and how it runs/deploys (local app, web app, static site...). Only what's evidenced. Empty string if unknown.",
  "stage": "Idea | Requirements | Building | Testing | Live | Paused",
  "stage_note": "One sentence of evidence for the stage you picked.",
  "where_we_are": "2-4 sentences: current state narrative — what works today, what happened most recently, momentum or lack of it.",
  "whats_built": ["Up to 10 short bullets of what exists and works today. Group by audience or area when the docs make that obvious (e.g. 'Parents: ...', 'Admins: ...'). Empty array if nothing is built."],
  "whats_left": ["Up to 10 short bullets of remaining work, sequenced most-important-first. Start each with a verb."],
  "open_decisions": ["Up to 5 bullets — decisions only the owner can make (pricing, naming, scope calls, launch timing). Empty array if none surfaced."],
  "constraints": ["Up to 4 bullets — privacy/security/operational rules an AI chat session must respect when working on this project (e.g. children's data, no real names, web-deploy only). Empty array if none apply."]
}

Stage definitions — pick the single best fit:
- "Idea": little or no code yet; mostly notes or an empty repo
- "Requirements": specs/docs exist but meaningful building hasn't started
- "Building": active development; core features incomplete
- "Testing": feature-complete or close; being tested, polished, or prepared for rollout
- "Live": deployed and in real use (including maintenance mode)
- "Paused": no meaningful activity for 60+ days and not Live

Rules:
- Plain English throughout; the owner is a non-developer.
- Bullets are short (aim under 15 words) and concrete.
- Scale length to the project: a small experiment gets a short briefing, not padded sections."""


# ----------------------------------------------------------------------
# Input gathering + generation
# ----------------------------------------------------------------------

def gather_brief_inputs(client, repo: dict, tracker: dict | None) -> str:
    """Build the context text for one repo's brief: GitHub metadata, doc
    excerpts fetched via the API, README fallback, and tracker facts."""
    owner = repo["owner"]
    name = repo["name"]
    ref = repo.get("default_branch", "main")

    parts: list[str] = []
    if repo.get("description"):
        parts.append(f"GitHub description: {repo['description']}")
    if repo.get("created_at"):
        parts.append(f"Repo created: {repo['created_at'][:10]}")
    if last_push_ts(repo):
        parts.append(f"Last push: {last_push_ts(repo)[:10]}")
    langs = repo.get("languages") or {}
    if langs:
        top = sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:3]
        parts.append("Languages: " + ", ".join(k for k, _ in top))

    # Spec docs via the shared doc-fetch path, with README fallback so
    # spec-less repos still get a grounded brief.
    fetched = gh.fetch_repo_docs(
        client, owner, name, ref=ref,
        max_chars=MAX_DOC_CHARS, include_readme="if_no_docs",
    )
    for key, content in fetched["docs"].items():
        parts.append(f"--- {key.upper().replace('_', ' ')} ---\n{content}")
    if fetched["readme"]:
        parts.append(f"--- README ---\n{fetched['readme']}")

    facts = tracker_facts(tracker)
    if facts:
        parts.append("--- TRACKER FACTS (from RepoDoctor's codebase tracker) ---\n" + facts)

    return "\n\n".join(parts)


def tracker_facts(tracker: dict | None) -> str:
    """Compact plain-text view of a tracker for the brief prompt."""
    if not tracker:
        return ""
    lines: list[str] = []

    modules = [m for m in tracker.get("modules") or [] if isinstance(m, dict)]
    if modules:
        counts: dict[str, int] = {}
        for m in modules:
            s = m.get("status", "?")
            counts[s] = counts.get(s, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        lines.append(f"Modules: {len(modules)} total ({summary})")

    open_actions = open_tracker_actions(tracker)
    if open_actions:
        lines.append("Open next actions:")
        for a in open_actions[:10]:
            note = f" — note: {a['status_note']}" if a.get("status_note") else ""
            lines.append(f"  [{a['priority']}] {a['title']} ({a['status']}){note}")

    gaps = [g for g in tracker.get("infra_gaps") or [] if isinstance(g, dict)]
    if gaps:
        lines.append("Infra gaps: " + "; ".join(
            f"[{g.get('priority', '?')}] {g.get('name', '')}" for g in gaps[:8]))

    questions = [q for q in tracker.get("questions") or [] if isinstance(q, dict)]
    if questions:
        lines.append("Open questions:")
        for q in questions[:8]:
            lines.append(f"  - {q.get('text', '')}")

    changes = [c for c in tracker.get("recent_changes") or [] if isinstance(c, dict)]
    if changes:
        lines.append("Recent changes (newest first):")
        for c in changes[:8]:
            lines.append(f"  {c.get('date', '')} — {c.get('title', '')}")

    return "\n".join(lines)


def generate_brief(
    api_key: str,
    repo_name: str,
    context_text: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Generate one repo's chat brief. Returns normalized dict + _usage."""
    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = (
        f"Project: {repo_name}\n\n{context_text}\n\n"
        "Write the chat briefing JSON now. Return ONLY the JSON object."
    )
    message = client.messages.create(
        model=model,
        max_tokens=2500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = next(
        (b.text for b in message.content if getattr(b, "type", "") == "text"), ""
    ).strip()
    brief = normalize_brief(extract_json_object(raw))
    brief["_usage"] = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "model": model,
    }
    return brief


def normalize_brief(raw: dict) -> dict:
    """Coerce AI output into the brief shape: enforce the stage enum,
    cap list lengths, drop empty bullets, default missing fields."""
    if not isinstance(raw, dict):
        raw = {}

    def _text(key: str) -> str:
        v = raw.get(key)
        return v.strip() if isinstance(v, str) else ""

    def _bullets(key: str, cap: int) -> list[str]:
        v = raw.get(key)
        if not isinstance(v, list):
            return []
        out = [s.strip() for s in v if isinstance(s, str) and s.strip()]
        return out[:cap]

    stage = _text("stage")
    if stage not in STAGES:
        stage = UNKNOWN_STAGE

    return {
        "what_it_is": _text("what_it_is"),
        "stack": _text("stack"),
        "stage": stage,
        "stage_note": _text("stage_note"),
        "where_we_are": _text("where_we_are"),
        "whats_built": _bullets("whats_built", MAX_BUILT),
        "whats_left": _bullets("whats_left", MAX_LEFT),
        "open_decisions": _bullets("open_decisions", MAX_DECISIONS),
        "constraints": _bullets("constraints", MAX_CONSTRAINTS),
    }


def last_push_ts(repo: dict) -> str:
    """The repo's last-push timestamp. GitHub's pushed_at is the actual push
    time; updated_at only tracks metadata changes (stars, settings, renames)
    and does NOT change on push. Falls back to updated_at for scans saved
    before pushed_at was captured."""
    return repo.get("pushed_at") or repo.get("updated_at") or ""


def is_brief_stale(brief: dict | None, repo: dict) -> bool:
    """A brief is stale when the repo was pushed to after the brief was
    generated. Missing timestamps count as stale so generation self-heals."""
    if not brief:
        return True
    generated = _parse_ts(brief.get("_generated_at", ""))
    if generated is None:
        return True
    pushed = _parse_ts(last_push_ts(repo))
    if pushed is None:
        return False
    return pushed > generated


def _parse_ts(value: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


# ----------------------------------------------------------------------
# Assembly — merge scan + briefs + trackers + summaries per project
# ----------------------------------------------------------------------

def open_tracker_actions(tracker: dict | None) -> list[dict]:
    """Open next-actions sorted P0-first. Shipped/dismissed excluded."""
    if not tracker:
        return []
    out = []
    for n in tracker.get("next_actions") or []:
        if not isinstance(n, dict):
            continue
        if n.get("status") not in OPEN_ACTION_STATUSES:
            continue
        out.append({
            "id": n.get("id", ""),
            "title": n.get("title", ""),
            "priority": n.get("priority", "—"),
            "status": n.get("status", "todo"),
            "status_note": n.get("status_note", ""),
        })
    out.sort(key=lambda a: (_PRIORITY_ORDER.get(a["priority"], 5), a["id"]))
    return out


def format_bytes(n: int) -> str:
    if not n:
        return "—"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{round(n / 1024)} KB"
    return f"{n} B"


def assemble_projects(
    repos: list[dict],
    briefs: dict,
    summaries: dict,
    trackers: dict,
    groups: dict,
) -> list[dict]:
    """One display/export dict per repo, newest-push-first. `trackers` is
    keyed 'owner/name' (models.list_trackers shape); `briefs` and
    `summaries` are keyed by repo name."""
    projects: list[dict] = []
    for repo in repos:
        owner = repo["owner"]
        name = repo["name"]
        brief = briefs.get(name)
        tracker = trackers.get(f"{owner}/{name}")
        summary = summaries.get(name)

        required = repo.get("required_files") or {}
        missing_files = sorted(k for k, v in required.items() if not v)

        langs = repo.get("languages") or {}
        top_langs = [k for k, _ in sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:2]]

        open_actions = open_tracker_actions(tracker)
        questions = [
            q.get("text", "") for q in (tracker.get("questions") if tracker else None) or []
            if isinstance(q, dict) and q.get("text")
        ]

        projects.append({
            "owner": owner,
            "name": name,
            "html_url": repo.get("html_url", ""),
            "private": repo.get("private", False),
            "description": repo.get("description", "") or "",
            "updated_at": last_push_ts(repo),
            "last_push": last_push_ts(repo)[:10] or "unknown",
            "branch_count": repo.get("non_henry_branch_count",
                                     repo.get("total_branch_count", 0)),
            "files_present": repo.get("files_present", 0),
            "files_total": repo.get("files_total", 0),
            "missing_files": missing_files,
            "docs_present": sorted(k for k, v in required.items() if v),
            "size_label": format_bytes(repo.get("code_size_bytes", 0)),
            "languages_label": ", ".join(top_langs),
            "group_names": sorted(g for g, members in groups.items() if name in members),
            "brief": brief,
            "stage": (brief or {}).get("stage") or UNKNOWN_STAGE,
            "brief_generated_at": (brief or {}).get("_generated_at", ""),
            "stale": bool(brief) and is_brief_stale(brief, repo),
            "summary": summary,
            "has_tracker": bool(tracker),
            "tracker_generated_at": (tracker or {}).get("generated_at", ""),
            "open_actions": open_actions,
            "open_questions": questions[:5],
        })
    # Most recently pushed first — "where am I" reads in recency order.
    projects.sort(key=lambda p: p["updated_at"] or "", reverse=True)
    return projects


# ----------------------------------------------------------------------
# Markdown composition
# ----------------------------------------------------------------------

def compose_markdown(
    projects: list[dict],
    owner_login: str,
    active_group: str,
    generated_label: str,
) -> str:
    """The full portfolio briefing document for Claude Chat."""
    briefed = sum(1 for p in projects if p["brief"])
    scope = f' (group: {active_group})' if active_group else ""

    lines: list[str] = [
        f"# Portfolio Chat Briefing — {generated_label}",
        "",
        "> Purpose: a single document to hand a Claude chat session that answers",
        f'> "where is this portfolio?" in one read. {len(projects)} projects{scope},',
        f"> {briefed} with AI briefs. Generated by RepoDoctor from each repo's docs",
        "> (PRODUCT_SPEC, PROJECT_STATUS, SESSION_NOTES, README), its codebase",
        "> tracker, and GitHub activity. This is a SNAPSHOT — regenerate when stale.",
        "",
        f"Owner: github.com/{owner_login}" if owner_login else "",
        "",
        "## At a Glance",
        "",
        "| Project | Stage | Last push | Open actions | Docs |",
        "|---|---|---|---|---|",
    ]
    for p in projects:
        actions = str(len(p["open_actions"])) if p["has_tracker"] else "—"
        lines.append(
            f"| {p['name']} | {p['stage']} | {p['last_push']} | "
            f"{actions} | {p['files_present']}/{p['files_total']} |"
        )
    lines.append("")

    for p in projects:
        lines.append(project_section_markdown(p))
    return "\n".join(line for line in lines if line is not None)


def project_section_markdown(p: dict) -> str:
    """One project's section of the briefing document."""
    brief = p["brief"]
    lines: list[str] = ["---", "", f"## {p['name']} — {p['stage']}", ""]

    # LICENSE is a required file but not a content source for the brief.
    source_docs = [d for d in p["docs_present"] if d != "LICENSE"]
    sources = ", ".join(source_docs) if source_docs else "no spec docs found"
    repo_ref = p["html_url"] or f"{p['owner']}/{p['name']}"
    lines.append(f"> Sources: {sources}. Repo: {repo_ref}")
    lines.append("")

    if brief:
        if brief.get("what_it_is"):
            lines += [f"**What it is:** {brief['what_it_is']}", ""]
        if brief.get("stack"):
            lines += [f"**Stack:** {brief['stack']}", ""]
        where = " ".join(s for s in (brief.get("stage_note"), brief.get("where_we_are")) if s)
        if where:
            lines += [f"**Where we are:** {where}", ""]
        if brief.get("whats_built"):
            lines.append("**What's built:**")
            lines += [f"- {b}" for b in brief["whats_built"]]
            lines.append("")
        if brief.get("whats_left"):
            lines.append("**What's left:**")
            lines += [f"{i}. {b}" for i, b in enumerate(brief["whats_left"], 1)]
            lines.append("")
        if brief.get("open_decisions"):
            lines.append("**Open decisions (owner):**")
            lines += [f"- {b}" for b in brief["open_decisions"]]
            lines.append("")
        if brief.get("constraints"):
            lines.append("**Constraints a chat session must respect:**")
            lines += [f"- {b}" for b in brief["constraints"]]
            lines.append("")
    else:
        what = p["description"] or "No description available."
        summary = p["summary"] or {}
        if summary.get("what_it_does"):
            what = summary["what_it_does"]
        lines += [f"**What it is:** {what}", ""]
        if summary.get("how_finished"):
            lines += [f"**Where we are:** {summary['how_finished']}", ""]
        steps = summary.get("next_steps") or []
        if steps:
            lines.append("**What's left:**")
            lines += [f"{i}. {s}" for i, s in enumerate(steps[:5], 1)]
            lines.append("")
        lines += ["_No AI brief yet — generate one from RepoDoctor's Briefing screen "
                  "for problem/stage/remaining detail._", ""]

    if p["open_actions"]:
        lines.append("**Open actions (tracker):**")
        for a in p["open_actions"][:8]:
            note = f" — {a['status_note']}" if a.get("status_note") else ""
            lines.append(f"- [{a['priority']}] {a['title']} ({a['status']}){note}")
        lines.append("")
    if p["open_questions"]:
        lines.append("**Open questions (tracker):**")
        lines += [f"- {q}" for q in p["open_questions"]]
        lines.append("")

    facts = [
        f"Last push {p['last_push']}",
        f"{p['branch_count']} branch{'es' if p['branch_count'] != 1 else ''}",
        f"docs {p['files_present']}/{p['files_total']}"
        + (f" (missing: {', '.join(p['missing_files'])})" if p["missing_files"] else ""),
        p["size_label"],
    ]
    if p["languages_label"]:
        facts.append(p["languages_label"])
    facts.append("private" if p["private"] else "public")
    if p["group_names"]:
        facts.append("groups: " + ", ".join(p["group_names"]))
    lines += [f"**Facts:** {' · '.join(facts)}", ""]

    meta: list[str] = []
    if p["brief_generated_at"]:
        stale = " (may be stale — repo updated since)" if p["stale"] else ""
        meta.append(f"Brief generated {p['brief_generated_at'][:10]}{stale}")
    if p["tracker_generated_at"]:
        meta.append(f"Tracker generated {p['tracker_generated_at'][:10]}")
    if meta:
        lines += [f"_{' · '.join(meta)}_", ""]

    return "\n".join(lines)
