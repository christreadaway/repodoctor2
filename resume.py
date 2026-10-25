"""
Resume — a context-switch packet for ONE project.

The owner works across many campaigns and wants to switch between them
without having to remember where each one was. This module composes, on
demand and with zero AI cost, everything RepoDoctor already knows about one
repo into a single page + a paste-ready Markdown document for a new Claude
Code or Claude chat session:

  1. Project facts (category, groups, stage, last push, languages, docs).
  2. What it is — the cached AI summary / brief / blurb if one exists.
  3. Work in flight — this repo's unmerged branches and open PRs (live).
  4. Recent sessions — the last RECENT_DAYS of Claude Code activity (live).
  5. Where the docs say we are — the head of PROJECT_STATUS.md and the most
     recent SESSION_NOTES.md entry, pulled straight from the default branch.
  6. Open tracker actions (P0 first) and planned next steps.
  7. A suggested first prompt to paste into Claude Code.

Every packet build is logged to data/logs/resume.log.
"""

from __future__ import annotations

import datetime
import logging
import re

import github_client as gh
import inflight
import sessions
from briefing import open_tracker_actions, last_push_ts, format_bytes

logger = logging.getLogger(__name__)

RECENT_DAYS = 14
STATUS_HEAD_CHARS = 3500
NOTES_ENTRY_CHARS = 3500
DOC_FETCH_CHARS = 60_000

_H2_RE = re.compile(r"^## ", re.MULTILINE)


def recent_window() -> sessions.Window:
    now = sessions._local_now()
    start = sessions._start_of_local_day(now) - datetime.timedelta(days=RECENT_DAYS - 1)
    return sessions.Window(start, now, f"the last {RECENT_DAYS} days",
                           f"last{RECENT_DAYS}", f"last{RECENT_DAYS}")


def status_head(text: str, max_chars: int = STATUS_HEAD_CHARS) -> str:
    """The top of PROJECT_STATUS.md — the snapshot — cut at a section boundary
    when possible so the excerpt doesn't end mid-sentence."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = cut.rfind("\n## ")
    if boundary > max_chars // 3:
        cut = cut[:boundary]
    return cut.rstrip() + "\n\n… (truncated — read the full file in the repo)"


def latest_notes_entry(text: str, max_chars: int = NOTES_ENTRY_CHARS) -> str:
    """The most recent entry of SESSION_NOTES.md. Entries are '## ' sections,
    newest first by project convention; take the first one after any preamble."""
    text = (text or "").strip()
    if not text:
        return ""
    starts = [m.start() for m in _H2_RE.finditer(text)]
    if not starts:
        return status_head(text, max_chars)
    first = starts[0]
    end = starts[1] if len(starts) > 1 else len(text)
    entry = text[first:end].strip()
    if len(entry) > max_chars:
        entry = entry[:max_chars].rstrip() + "\n\n… (truncated — read the full entry in the repo)"
    return entry


def _repo_groups(repo_name: str, groups: dict, categories: dict) -> list[str]:
    cat = categories.get(repo_name)
    return [g for g, members in (groups or {}).items()
            if repo_name in members and g != cat]


def build_packet(client, repo: dict, *, summary: dict | None, brief: dict | None,
                 blurb: dict | None, tracker: dict | None, groups: dict,
                 categories: dict, history: dict | None = None) -> dict:
    """Gather everything for one repo. GitHub failures in any one section are
    logged and leave that section empty rather than failing the packet."""
    owner, name = repo["owner"], repo["name"]
    ref = repo.get("default_branch", "main")
    full_name = f"{owner}/{name}"

    packet = {
        "repo": repo,
        "full_name": full_name,
        "category": categories.get(name),
        "groups": _repo_groups(name, groups, categories),
        "last_push": last_push_ts(repo),
        "size": format_bytes(repo.get("code_size_bytes") or 0),
        "languages": [k for k, _ in sorted((repo.get("languages") or {}).items(),
                                           key=lambda kv: kv[1], reverse=True)[:4]],
        "docs_present": [k for k, v in (repo.get("required_files") or {}).items() if v],
        "docs_missing": [k for k, v in (repo.get("required_files") or {}).items() if not v],
        "summary": summary or None,
        "brief": brief or None,
        "blurb": (blurb or {}).get("summary") or "",
        "tracker_actions": open_tracker_actions(tracker)[:8],
        "planned_steps": ((summary or {}).get("next_steps") or [])[:6],
        "inflight": [],
        "recent_sessions": [],
        "status_excerpt": "",
        "notes_excerpt": "",
        "errors": [],
        "history": (history or {}).get(full_name) or {},
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    try:
        packet["inflight"] = inflight.gather_repo_inflight(client, repo)
    except gh.GitHubAuthError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("resume: inflight failed for %s: %s", full_name, e)
        packet["errors"].append(f"Open branches unavailable: {e}")

    try:
        window = recent_window()
        packet["recent_sessions"] = sessions.gather_sessions(client, [repo], window)[:8]
        packet["recent_label"] = window.label
    except gh.GitHubAuthError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("resume: sessions failed for %s: %s", full_name, e)
        packet["errors"].append(f"Recent sessions unavailable: {e}")

    try:
        fetched = gh.fetch_repo_docs(client, owner, name, ref=ref,
                                     max_chars=DOC_FETCH_CHARS, include_readme="if_no_docs")
        docs = fetched["docs"]
        packet["status_excerpt"] = status_head(docs.get("project_status", ""))
        packet["notes_excerpt"] = latest_notes_entry(docs.get("session_notes", ""))
        if not docs and fetched.get("readme"):
            packet["status_excerpt"] = status_head(fetched["readme"])
            packet["status_source"] = "README"
        else:
            packet["status_source"] = "PROJECT_STATUS.md"
    except gh.GitHubAuthError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("resume: docs failed for %s: %s", full_name, e)
        packet["errors"].append(f"Docs unavailable: {e}")

    packet["first_prompt"] = suggest_first_prompt(packet)
    return packet


def suggest_first_prompt(packet: dict) -> str:
    """A concrete opening line for Claude Code, picked from the most pressing
    fact in the packet. Deterministic so it is predictable."""
    name = packet["repo"]["name"]
    for i in packet.get("inflight", []):
        if i["kind"] in ("finish", "continue"):
            return (f"Pick up the `{i['branch']}` branch in {name} ({i['topic']}). "
                    f"Read PROJECT_STATUS.md and the latest SESSION_NOTES.md entry, "
                    f"check the diff against {i['base']}, finish the work, and open a PR.")
    merges = [i for i in packet.get("inflight", []) if i["kind"] == "merge"]
    if merges:
        prs = ", ".join(f"PR #{i['pr']['number']}" for i in merges if i.get("pr"))
        return (f"In {name}, {prs} {'is' if len(merges) == 1 else 'are'} ready to merge. "
                f"I'll merge on GitHub myself. Then start the next item from "
                f"PROJECT_STATUS.md's next steps and tell me what you chose.")
    for a in packet.get("tracker_actions", []):
        if a.get("priority") in ("P0", "P1"):
            return (f"In {name}, start tracker action {a['id']} ({a['priority']}): {a['title']}. "
                    f"Read PROJECT_STATUS.md and the latest SESSION_NOTES.md entry first.")
    if packet.get("planned_steps"):
        return (f"In {name}, read PROJECT_STATUS.md and the latest SESSION_NOTES.md entry, "
                f"then start this next step: {packet['planned_steps'][0]}")
    return (f"In {name}, read PROJECT_STATUS.md and the latest SESSION_NOTES.md entry, "
            f"summarize where we are in five sentences, and propose the next three steps.")


def compose_markdown(packet: dict, owner_login: str = "") -> str:
    repo = packet["repo"]
    name = repo["name"]
    lines = [
        f"# Resume: {name}",
        "",
        f"> Context-switch packet built by RepoDoctor on {sessions._local_time_label(packet['built_at'])}. "
        f"Hand this to Claude Code (or a Claude chat) to pick this project back up.",
        "",
    ]
    facts = [f"Repo: {repo.get('html_url') or packet['full_name']}"]
    if packet.get("category"):
        facts.append(f"Category: {packet['category']}")
    if packet.get("groups"):
        facts.append("Groups: " + ", ".join(packet["groups"]))
    if packet.get("last_push"):
        facts.append(f"Last push: {packet['last_push'][:10]}")
    if packet.get("languages"):
        facts.append("Languages: " + ", ".join(packet["languages"]))
    brief = packet.get("brief") or {}
    if brief.get("stage"):
        facts.append(f"Stage: {brief['stage']}")
    if packet.get("docs_missing"):
        facts.append("Missing docs: " + ", ".join(packet["docs_missing"]))
    lines += [f"- {f}" for f in facts] + [""]

    what = brief.get("what_it_is") or (packet.get("summary") or {}).get("what_it_does") or packet.get("blurb")
    if what:
        lines += ["## What it is", "", what.strip(), ""]
    if brief.get("where_we_are"):
        lines += ["## Where we are (AI brief)", "", brief["where_we_are"].strip(), ""]

    lines += ["## Work in flight", ""]
    if packet["inflight"]:
        for i in packet["inflight"]:
            pr = i.get("pr")
            pr_bit = f" · PR #{pr['number']}{' (draft)' if pr.get('draft') else ''}" if pr else ""
            age = f" · {i['age_days']}d old" if i.get("age_days") is not None else ""
            lines.append(f"- **{i['label']}** `{i['branch']}` — {i['topic']}{pr_bit}{age}. {i['text']}")
    else:
        lines.append("- Nothing unmerged — every branch is in the default branch.")
    lines.append("")

    lines += [f"## Recent sessions ({packet.get('recent_label', f'last {RECENT_DAYS} days')})", ""]
    if packet["recent_sessions"]:
        for s in packet["recent_sessions"]:
            pr = s.get("pr")
            pr_bit = f" · PR #{pr['number']} ({'merged' if pr.get('merged') else pr.get('state')})" if pr else ""
            lines.append(f"- `{s['branch']}` — {s['topic']}{pr_bit} · {s['commit_count']} commit(s) · {s['next_label']}")
            for c in s.get("commits", [])[:3]:
                lines.append(f"    - {c['summary']}")
    else:
        lines.append("- No Claude Code activity in this window.")
    lines.append("")

    if packet["tracker_actions"]:
        lines += ["## Open tracker actions", ""]
        for a in packet["tracker_actions"]:
            note = f" — {a['status_note']}" if a.get("status_note") else ""
            lines.append(f"- [{a['priority']}] {a['id']} {a['title']} ({a['status']}){note}")
        lines.append("")
    if packet["planned_steps"]:
        lines += ["## Planned next steps (from the project summary)", ""]
        lines += [f"- {s}" for s in packet["planned_steps"]] + [""]

    if packet["status_excerpt"]:
        lines += [f"## {packet.get('status_source', 'PROJECT_STATUS.md')} (top)", "", packet["status_excerpt"], ""]
    if packet["notes_excerpt"]:
        lines += ["## SESSION_NOTES.md (latest entry)", "", packet["notes_excerpt"], ""]
    if packet["errors"]:
        lines += ["## Gaps in this packet", ""] + [f"- {e}" for e in packet["errors"]] + [""]

    lines += ["## Suggested first prompt for Claude Code", "", "```", packet["first_prompt"], "```", ""]
    return "\n".join(lines)
