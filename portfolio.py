"""
Portfolio — every repo at a glance, grouped by the tags (themes) you give
them, plus the map of functional infrastructure areas every new project can
attach to.

Tags ARE the app's groups (models.get_groups): many-to-many, editable, and a
tag picked here scopes every other screen (In Flight, Briefing, Resume, Docs)
through the shared group filter — which is what makes the Portfolio a
context-switching surface rather than a report. The four category groups
(Church / School / Combined / Personal) are tags too.

Everything here is a pure function of data the app already caches (scan,
selector blurbs, project summaries, In Flight, Components) except tag
suggestion, which is one AI call over the whole portfolio. Suggestions are
never applied directly: they're saved for review and applied per repo with an
explicit click. Events log to data/logs/portfolio.log.
"""

from __future__ import annotations

import re

# The themes the owner named, seeded (empty) the first time the Portfolio
# opens. "Church" and "School" already exist as category groups.
DEFAULT_TAGS = [
    "Investing / Personal Finance",
    "Marketing / GTM",
    "School",
    "Church",
    "Infrastructure",
]

MAX_TAG_LEN = 40
_TAG_OK = re.compile(r"^[^\x00-\x1f<>]{1,%d}$" % MAX_TAG_LEN)

# Functional infrastructure: the shared pieces any new build should attach to
# instead of rebuilding. The owner's list first, then the ones the Components
# inventory shows recurring across repos. `capabilities` are components.py
# keys, so each area can name its best source repo from the last scan.
FUNCTIONAL_AREAS: list[dict] = [
    {"key": "marketing", "label": "Marketing / GTM",
     "description": "Landing pages, SEO and sitemaps, analytics tags, pricing pages, launch checklists.",
     "capabilities": ["marketing_gtm", "design_system"]},
    {"key": "video", "label": "Video production",
     "description": "Scripted video, render pipelines (Remotion / ffmpeg), captions and subtitles.",
     "capabilities": ["video_production"]},
    {"key": "security", "label": "Security",
     "description": "CSRF / headers / rate limiting, secrets handling, credential encryption, dependency audits.",
     "capabilities": ["security"]},
    {"key": "messaging", "label": "Messaging (email, SMS)",
     "description": "Email and SMS senders, notification templates, webhooks, retry + dead-letter logging.",
     "capabilities": ["email", "sms", "comms_notifications"]},
    {"key": "bi", "label": "BI / charts / data viz",
     "description": "Chart defaults, dashboards, stat tiles, exports.",
     "capabilities": ["data_viz"]},
    {"key": "courseware", "label": "Courseware",
     "description": "Lessons, curricula, standards alignment, quizzes and grading.",
     "capabilities": ["courseware"]},
    # --- found across the portfolio ---
    {"key": "audio", "label": "Audio / transcription",
     "description": "Recording, Whisper-style transcription, audio clean-up.",
     "capabilities": ["audio_transcription"]},
    {"key": "ai", "label": "AI integration",
     "description": "Claude / LLM client wrapper, prompts, structured outputs, cost logging.",
     "capabilities": ["ai_integration"]},
    {"key": "identity", "label": "Sign-in & data (Google Auth, Firebase)",
     "description": "Google sign-in, Firebase / Firestore access layer, database models.",
     "capabilities": ["auth", "firebase_firestore", "database"]},
    {"key": "crm", "label": "CRM & contacts",
     "description": "People and contact records, leads, follow-ups (three CRM repos share this today).",
     "capabilities": ["crm_contacts"]},
    {"key": "privacy", "label": "Privacy & PII redaction",
     "description": "Anonymizing documents and stripping personal data before it goes to an AI or a report.",
     "capabilities": ["privacy_redaction"]},
    {"key": "payments", "label": "Payments",
     "description": "Stripe checkout, subscriptions, invoices.",
     "capabilities": ["payments"]},
    {"key": "logging", "label": "Logging & debugging",
     "description": "Rotating app log, copy-for-Claude error format, per-feature event logs.",
     "capabilities": ["logging"]},
    {"key": "testing", "label": "Testing",
     "description": "pytest / Jest scaffolds, route smoke tests, browser checks.",
     "capabilities": ["testing"]},
    {"key": "claude_docs", "label": "Claude docs management",
     "description": "CLAUDE.md, PRODUCT_SPEC / PROJECT_STATUS / SESSION_NOTES, /start and /end commands.",
     "capabilities": ["claude_docs"]},
    {"key": "ops", "label": "Deploy, launchers & scheduling",
     "description": "Netlify / Firebase / Docker config, start.command / start.ps1, scheduled jobs.",
     "capabilities": ["deployment", "scripts_launchers", "scheduling_automation"]},
    {"key": "documents", "label": "Documents & PDF",
     "description": "PDF / DOCX generation, document templates, exports.",
     "capabilities": ["pdf_docs_export"]},
]
_AREA_KEYS = {a["key"] for a in FUNCTIONAL_AREAS}


def clean_tag(name: str) -> str | None:
    """Normalize a user- or AI-supplied tag name; None when unusable."""
    name = re.sub(r"\s+", " ", (name or "")).strip()
    return name if name and _TAG_OK.match(name) else None


def first_sentence(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    out = m.group(1) if m else text
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def one_liner(repo: dict, blurbs: dict, summaries: dict) -> tuple[str, str]:
    """(text, source) — the best one-line description already cached, cheapest
    first: Selector blurb → Projects summary → GitHub description."""
    full = repo.get("full_name") or f"{repo.get('owner')}/{repo.get('name')}"
    blurb = (blurbs.get(full) or {}).get("summary")
    if blurb:
        return first_sentence(blurb), "blurb"
    summ = (summaries.get(repo.get("name", "")) or {}).get("what_it_does")
    if summ:
        return first_sentence(summ), "summary"
    if repo.get("description"):
        return first_sentence(repo["description"]), "description"
    return "", ""


def build_view(repos: list[dict], groups: dict, blurbs: dict, summaries: dict,
               inflight_items: list[dict], activity_cutoff: str = "") -> dict:
    """Sections per tag (a repo appears under each of its tags) plus Untagged.

    activity_cutoff: ISO timestamp; repos pushed at/after it count as active.
    """
    open_by_full: dict[str, int] = {}
    for i in inflight_items or []:
        if i.get("kind") != "complete":
            key = f"{i.get('owner')}/{i.get('repo')}"
            open_by_full[key] = open_by_full.get(key, 0) + 1

    tags_by_repo: dict[str, list[str]] = {}
    for tag, members in groups.items():
        for m in members:
            tags_by_repo.setdefault(m, []).append(tag)

    rows: dict[str, dict] = {}
    for r in repos:
        full = r.get("full_name") or f"{r.get('owner')}/{r.get('name')}"
        text, source = one_liner(r, blurbs, summaries)
        summ = summaries.get(r.get("name", "")) or {}
        rows[full] = {
            "full_name": full,
            "owner": r.get("owner", ""),
            "name": r.get("name", ""),
            "private": bool(r.get("private")),
            "pushed_at": r.get("pushed_at") or "",
            "one_liner": text,
            "one_liner_source": source,
            "how_finished": first_sentence(summ.get("how_finished", ""), 160),
            "tags": sorted(tags_by_repo.get(r.get("name", ""), [])),
            "open_branches": open_by_full.get(full, 0),
            "active": bool(activity_cutoff and (r.get("pushed_at") or "") >= activity_cutoff),
        }

    def _sorted(items):
        return sorted(items, key=lambda x: x["pushed_at"], reverse=True)

    sections = []
    for tag, members in groups.items():
        member_set = set(members)
        items = _sorted([row for row in rows.values() if row["name"] in member_set])
        sections.append({
            "tag": tag,
            "repos": items,
            "count": len(items),
            "active": sum(1 for i in items if i["active"]),
            "open_branches": sum(i["open_branches"] for i in items),
            "missing": sorted(member_set - {i["name"] for i in items}),
        })
    untagged = _sorted([row for row in rows.values() if not row["tags"]])
    return {
        "sections": sections,
        "untagged": untagged,
        "rows": rows,
        "tag_names": list(groups.keys()),
        "total": len(rows),
        "tagged": len(rows) - len(untagged),
        "missing_one_liners": [row["full_name"] for row in rows.values() if not row["one_liner"]],
    }


def infrastructure_view(components_cache: dict | None, repos: list[dict],
                        homes: dict, comp_module) -> list[dict]:
    """One card per functional area: the home repo you assigned (if any), the
    best source repos from the last Components scan, and coverage."""
    inv = None
    if components_cache and components_cache.get("detections"):
        inv = comp_module.inventory(repos, components_cache["detections"])
    caps = {c["key"]: c for c in (inv or {}).get("capabilities", [])}
    out = []
    for area in FUNCTIONAL_AREAS:
        sources: dict[str, dict] = {}
        for key in area["capabilities"]:
            for entry in (caps.get(key) or {}).get("repos", []):
                cur = sources.setdefault(entry["full_name"], {**entry, "hits": 0})
                cur["hits"] += entry["hits"]
        ranked = sorted(sources.values(), key=lambda e: (-e["hits"], e["full_name"]))
        out.append({
            **area,
            "home": homes.get(area["key"], ""),
            "sources": ranked[:3],
            "repo_count": len(ranked),
            "scanned": inv is not None,
        })
    return out


def compose_markdown(view: dict, infra: list[dict], owner_login: str, when: str) -> str:
    lines = [
        "# Project portfolio",
        "",
        f"> {view['total']} repos, {view['tagged']} tagged, grouped by theme. Read from RepoDoctor on {when}.",
        "",
    ]
    if owner_login:
        lines += [f"Owner: github.com/{owner_login}", ""]
    for sec in view["sections"]:
        if not sec["repos"]:
            continue
        lines += [f"## {sec['tag']} ({sec['count']})", ""]
        for r in sec["repos"]:
            bits = [f"**{r['name']}**"]
            if r["one_liner"]:
                bits.append(r["one_liner"])
            extra = []
            if r["open_branches"]:
                extra.append(f"{r['open_branches']} open branch{'es' if r['open_branches'] != 1 else ''}")
            if r["pushed_at"]:
                extra.append(f"last push {r['pushed_at'][:10]}")
            tail = f" _({', '.join(extra)})_" if extra else ""
            lines.append(f"- {' — '.join(bits)}{tail}")
        lines.append("")
    if view["untagged"]:
        lines += [f"## Untagged ({len(view['untagged'])})", ""]
        for r in view["untagged"]:
            lines.append(f"- **{r['name']}**{f' — ' + r['one_liner'] if r['one_liner'] else ''}")
        lines.append("")
    if infra:
        lines += ["## Infrastructure (shared functional components)", ""]
        for a in infra:
            home = f"home: `{a['home']}`" if a["home"] else "no home repo yet"
            src = ", ".join(f"{s['name']} ({s['hits']})" for s in a["sources"]) or "none found"
            lines.append(f"- **{a['label']}** — {home}; best sources: {src}")
        lines.append("")
    return "\n".join(lines)


# --- AI tag suggestion -------------------------------------------------------

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Exact repo name from the list."},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "1-2 theme tags for this repo."},
                },
                "required": ["repo", "tags"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


def suggestion_prompt(rows: list[dict], existing_tags: list[str]) -> str:
    listing = "\n".join(
        f"- {r['name']}: {r['one_liner'] or '(no description)'}" for r in rows
    )
    return (
        "You are organizing a builder's GitHub portfolio into themes so they can "
        "switch context between projects quickly.\n\n"
        f"Existing tags (prefer these): {', '.join(existing_tags)}\n\n"
        "Rules:\n"
        "- Give every repo 1 tag, 2 only when it clearly serves two themes.\n"
        "- 'Infrastructure' is for reusable building blocks (auth, logging, "
        "messaging, deploy tooling, dev tools, data pipelines) rather than end products.\n"
        "- 'Combined' means a project serving both church and school.\n"
        "- You may introduce a NEW tag only when 3+ repos share a theme no "
        "existing tag fits (e.g. retro games, sports, family). Keep new tags "
        "short, Title Case, 1-3 words.\n"
        "- Use the repo name exactly as listed.\n\n"
        f"Repos:\n{listing}"
    )


def normalize_suggestions(raw: dict, rows: list[dict], existing_tags: list[str]) -> list[dict]:
    """Keep only known repos and clean tags; map case-insensitive matches onto
    existing tag names so 'church' doesn't create a second Church."""
    by_name = {r["name"]: r for r in rows}
    canon = {t.lower(): t for t in existing_tags}
    out = []
    seen = set()
    for a in (raw or {}).get("assignments", []) or []:
        if not isinstance(a, dict):
            continue
        name = a.get("repo")
        if not isinstance(name, str) or name not in by_name or name in seen:
            continue
        tags = []
        for t in a.get("tags") or []:
            t = clean_tag(t) if isinstance(t, str) else None
            if not t:
                continue
            t = canon.get(t.lower(), t)
            if t not in tags:
                tags.append(t)
        if not tags:
            continue
        seen.add(name)
        current = set(by_name[name]["tags"])
        add = [t for t in tags[:2] if t not in current]
        if add:
            out.append({"repo": name, "full_name": by_name[name]["full_name"], "tags": add,
                        "new_tags": [t for t in add if t not in existing_tags]})
    return out
