"""
Do next + Weekly review — one ranked answer to "what should I work on?",
and a Monday look back + look ahead across every repo.

Before this, the answer was spread across four screens: In Flight (unmerged
branches and PRs), Tracker (P0/P1 next actions), Projects (planned next
steps from AI summaries) and Recent sessions. This module merges them into
one deterministic, ranked list. Nothing here calls GitHub or an AI model:
it reads the In Flight cache (data/inflight.json), saved trackers, and the
project summaries, so the page opens instantly. Freshness is the In Flight
cache's age, which the page shows with a refresh button.

Ranking — what needs YOU first, then what moves work forward, then cleanup:
  1. MERGE          PR open and ready (oldest waiting first; PRs rot)
  2. FINISH         draft PR or WIP commits (most recent first: context is warm)
  3. CONFIRM LIVE   tracker action awaiting deploy
  4. P0             tracker P0 actions (todo / in progress)
  5. OPEN A PR      commits on a branch with no PR
  6. P1             tracker P1 actions
  7. DECIDE         stale branches, branches GitHub couldn't compare
  8. PLANNED        first planned step, only for repos with nothing above
  9. CLEAN UP       merged branches still lying around (one line, all repos)
 10. BLOCKED        blocked P0/P1 actions, with their note
P2/P3 tracker actions are counted but not listed (they live in Tracker).

The Weekly review composes: what shipped in the last 7 days (from the Stats
activity report), what's waiting on you (MERGE / FINISH / CONFIRM LIVE),
what went quiet (repos active in the last 90 days with nothing in the last
21, plus stale branches), and the top of the Do next list.
"""

from __future__ import annotations

import datetime
import logging

from briefing import _parse_ts, open_tracker_actions
from sessions import safe_session_url

logger = logging.getLogger(__name__)

TIERS = (
    ("merge", "MERGE"),
    ("finish", "FINISH"),
    ("deploy", "CONFIRM LIVE"),
    ("p0", "P0"),
    ("open_pr", "OPEN A PR"),
    ("p1", "P1"),
    ("decide", "DECIDE"),
    ("planned", "PLANNED"),
    ("cleanup", "CLEAN UP"),
    ("blocked", "BLOCKED"),
)
TIER_LABELS = dict(TIERS)
TIER_ORDER = {k: i for i, (k, _) in enumerate(TIERS)}
# Tiers that are the owner's move right now (Weekly review "Waiting on you").
WAITING_TIERS = ("merge", "finish", "deploy")
# Weekly review "Next up": what moves work forward after the waiting items.
FORWARD_TIERS = ("p0", "open_pr", "p1", "planned")

PLANNED_LIMIT = 12          # planned-step rows before "+N more on Projects"
QUIET_AFTER_DAYS = 21       # active in the last 90 days, nothing in the last 21
REVIEW_NEXT_LIMIT = 6       # Do next rows on the Weekly review
REVIEW_SHIPPED_TITLES = 3   # PR/commit titles per repo on the Weekly review


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _days_since(iso_ts: str, now: datetime.datetime) -> int | None:
    dt = _parse_ts(iso_ts or "")
    return None if dt is None else max(0, (now - dt).days)


def _full(owner: str, name: str) -> str:
    return f"{owner}/{name}"


# ----------------------------------------------------------------------
# Prompts (copy-ready for a fresh Claude Code session)
# ----------------------------------------------------------------------

def _orient() -> str:
    return "Read PROJECT_STATUS.md and the latest SESSION_NOTES.md entry first."


def _prompt_for_inflight(tier: str, i: dict) -> str:
    repo, branch, base = i.get("repo", ""), i.get("branch", ""), i.get("base") or "main"
    topic = i.get("topic") or branch
    pr = i.get("pr") or {}
    if tier == "merge":
        return (f"In {repo}, PR #{pr.get('number')} ({topic}) looks ready to merge into {base}. "
                f"Review the diff, run the tests, and tell me in plain English what it changes "
                f"and anything risky. I'll merge it on GitHub myself.")
    if tier == "finish":
        close = (f"then mark PR #{pr.get('number')} ready for review" if pr.get("number")
                 else "then open a PR")
        return (f"Pick up the `{branch}` branch in {repo} ({topic}). {_orient()} "
                f"Check the diff against {base}, finish the work, {close}.")
    if tier == "open_pr":
        n = i.get("ahead_by") or 0
        return (f"Pick up the `{branch}` branch in {repo} ({topic}): {n} commit"
                f"{'s' if n != 1 else ''} aren't on {base} yet and there's no PR. {_orient()} "
                f"If the work is finished, open a PR; if not, finish it first, then open one.")
    # decide
    days = i.get("age_days")
    when = f" and nothing has happened for {days} days" if days is not None else ""
    return (f"In {repo}, the `{branch}` branch has work that isn't on {base}{when}. "
            f"Summarize what it contains and recommend one of: finish it, open a PR, or "
            f"delete it. Don't change anything until I decide.")


def _prompt_for_action(repo: str, a: dict, raw_prompt: str) -> str:
    if isinstance(raw_prompt, str) and len(raw_prompt.strip()) >= 20:
        return raw_prompt.strip()
    return (f"In {repo}, start tracker action {a.get('id')} ({a.get('priority')}): "
            f"{a.get('title')}. {_orient()}")


# ----------------------------------------------------------------------
# Do next
# ----------------------------------------------------------------------

def _inflight_tier(kind: str) -> str | None:
    return {"merge": "merge", "finish": "finish", "continue": "open_pr",
            "stale": "decide", "unknown": "decide"}.get(kind)


def _links_for_inflight(i: dict) -> list[dict]:
    links = []
    pr = i.get("pr") or {}
    if pr.get("html_url"):
        links.append({"label": f"PR #{pr.get('number')}", "url": pr["html_url"], "external": True})
    elif i.get("branch_url"):
        links.append({"label": "BRANCH", "url": i["branch_url"], "external": True})
    return links


def build_do_next(inflight_items: list[dict] | None, trackers: dict | None,
                  summaries: dict | None, repos: list[dict],
                  now: datetime.datetime | None = None) -> dict:
    """Rank everything actionable for `repos` (already group-scoped).

    inflight_items: the In Flight cache's items (any repos; scoped here).
    trackers: {"owner/name": tracker} from models.list_trackers().
    summaries: {repo name: summary} from models.get_project_summaries().
    """
    now = now or _now()
    trackers = trackers or {}
    summaries = summaries or {}
    in_scope = {_full(r.get("owner", ""), r["name"]): r for r in repos}
    by_name = {r["name"]: r for r in repos}
    items: list[dict] = []
    busy: set[str] = set()       # repos with anything above PLANNED
    cleanup: dict[str, int] = {}  # full name -> merged branches to delete
    hidden_low = 0

    for i in inflight_items or []:
        full = _full(i.get("owner", ""), i.get("repo", ""))
        if full not in in_scope:
            continue
        kind = i.get("kind", "")
        if kind == "complete":
            cleanup[full] = cleanup.get(full, 0) + 1
            continue
        tier = _inflight_tier(kind)
        if not tier:
            continue
        busy.add(full)
        when = i.get("last_activity_at") or ""
        items.append({
            "tier": tier, "label": TIER_LABELS[tier], "source": "inflight",
            "repo": i.get("repo", ""), "owner": i.get("owner", ""), "full_name": full,
            "title": i.get("topic") or i.get("branch", ""),
            "branch": i.get("branch", ""),
            "why": i.get("text", ""),
            "when": when,
            "age_days": _days_since(when, now),
            "links": _links_for_inflight(i),
            # In Flight items come back from data/inflight.json: re-check so a
            # hand-edited or corrupted cache can't put a non-session URL in an href.
            "session_url": safe_session_url(i.get("session_url")),
            "prompt": _prompt_for_inflight(tier, i),
        })

    for full, tracker in trackers.items():
        if full not in in_scope:
            continue
        repo = in_scope[full]
        raw = {n.get("id"): n for n in (tracker.get("next_actions") or []) if isinstance(n, dict)}
        for a in open_tracker_actions(tracker):
            pri, status = a.get("priority"), a.get("status")
            if pri not in ("P0", "P1"):
                hidden_low += 1
                continue
            if status == "blocked":
                tier = "blocked"
            elif status == "awaiting_deploy":
                tier = "deploy"
            else:
                tier = "p0" if pri == "P0" else "p1"
            busy.add(full)
            why = {"blocked": f"Blocked{': ' + a['status_note'] if a.get('status_note') else ''}.",
                   "deploy": "Shipped in code; confirm it's live, then mark it shipped in Tracker.",
                   }.get(tier, f"{pri} tracker action ({status.replace('_', ' ')})."
                         + (f" Note: {a['status_note']}" if a.get("status_note") else ""))
            items.append({
                "tier": tier, "label": TIER_LABELS[tier], "source": "tracker",
                "repo": repo["name"], "owner": repo.get("owner", ""), "full_name": full,
                "title": f"{a.get('id')} {a.get('title')}".strip(),
                "branch": "", "why": why, "when": "", "age_days": None,
                "links": [{"label": "TRACKER", "url": f"/tracker/{repo.get('owner', '')}/{repo['name']}",
                           "external": False}],
                "session_url": "",
                "prompt": _prompt_for_action(repo["name"], a, (raw.get(a.get("id")) or {}).get("prompt")),
                "priority": pri,
            })

    planned = []
    for name, s in summaries.items():
        repo = by_name.get(name)
        if not repo or not isinstance(s, dict):
            continue
        full = _full(repo.get("owner", ""), name)
        steps = [x for x in (s.get("next_steps") or []) if isinstance(x, str) and x.strip()]
        if full in busy or not steps:
            continue
        planned.append({
            "tier": "planned", "label": TIER_LABELS["planned"], "source": "summary",
            "repo": name, "owner": repo.get("owner", ""), "full_name": full,
            "title": steps[0].strip(), "branch": "",
            "why": "First planned step from the Projects summary"
                   + (f" ({len(steps) - 1} more)" if len(steps) > 1 else "") + ".",
            "when": repo.get("pushed_at") or repo.get("updated_at") or "",
            "age_days": None,
            "links": [], "session_url": "",
            "prompt": (f"In {name}, read PROJECT_STATUS.md and the latest SESSION_NOTES.md "
                       f"entry, then start this next step: {steps[0].strip()}"),
        })
    # Most recently pushed projects first: their context is freshest.
    planned.sort(key=lambda x: x["when"] or "", reverse=True)
    planned_more = max(0, len(planned) - PLANNED_LIMIT)
    items.extend(planned[:PLANNED_LIMIT])

    if cleanup:
        n, m = sum(cleanup.values()), len(cleanup)
        items.append({
            "tier": "cleanup", "label": TIER_LABELS["cleanup"], "source": "inflight",
            "repo": "", "owner": "", "full_name": "",
            "title": f"Delete {n} merged branch{'es' if n != 1 else ''} across {m} repo{'s' if m != 1 else ''}",
            "branch": "",
            "why": "Their work is already on the default branch. In Flight can delete them in one click.",
            "when": "", "age_days": None,
            "links": [{"label": "IN FLIGHT", "url": "/inflight?kind=complete", "external": False}],
            "session_url": "", "prompt": "",
        })

    ordered = _order(items)
    for n, x in enumerate(ordered, 1):
        x["rank"] = n

    counts = {k: 0 for k, _ in TIERS}
    for x in ordered:
        counts[x["tier"]] += 1
    return {
        "items": ordered,
        "counts": counts,
        "tiers": [(k, label, counts[k]) for k, label in TIERS if counts[k]],
        "total": len(ordered),
        "hidden_low_priority": hidden_low,
        "planned_more": planned_more,
        "cleanup_branches": sum(cleanup.values()),
    }


def _order(items: list[dict]) -> list[dict]:
    """Tier order, then within a tier: MERGE oldest-waiting first (PRs rot),
    FINISH / OPEN A PR / DECIDE most-recent first (context is warm),
    PLANNED as given (most recently pushed project first), the rest by repo."""
    out = []
    for tier, _ in TIERS:
        group = [x for x in items if x["tier"] == tier]
        if tier == "merge":
            group.sort(key=lambda x: (x["when"] or "9999", x["repo"].lower()))
        elif tier in ("finish", "open_pr", "decide"):
            group.sort(key=lambda x: x["repo"].lower())
            # Stable second sort: newest first; missing timestamps ("") last.
            group.sort(key=lambda x: x["when"] or "", reverse=True)
        elif tier != "planned":
            group.sort(key=lambda x: (x["repo"].lower(), x["title"]))
        out.extend(group)
    return out


def compose_do_next_markdown(result: dict, owner_login: str = "", gathered_at: str = "",
                             group: str = "") -> str:
    scope = f"the **{group}** group" if group else "every scanned repo"
    lines = [
        "# Do next",
        "",
        f"> Ranked by RepoDoctor{(' for ' + owner_login) if owner_login else ''} across {scope}: "
        f"PRs to merge, work to finish, tracker P0/P1 actions, branches needing a PR or a "
        f"decision, then planned steps."
        + (f" In Flight data read {gathered_at[:16].replace('T', ' ')} UTC." if gathered_at else ""),
        "",
    ]
    if not result["items"]:
        lines += ["Nothing waiting. Every branch is merged and no P0/P1 actions are open.", ""]
    current = None
    for x in result["items"]:
        if x["tier"] != current:
            current = x["tier"]
            lines += [f"## {x['label']}", ""]
        head = f"{x['rank']}. **{x['repo']}**: {x['title']}" if x["repo"] else f"{x['rank']}. {x['title']}"
        lines.append(head)
        if x["why"]:
            lines.append(f"   - {x['why']}")
        for link in x["links"]:
            if link.get("external"):
                lines.append(f"   - {link['label']}: {link['url']}")
        if x.get("session_url"):
            lines.append(f"   - Claude Code session: {x['session_url']}")
        if x["prompt"]:
            lines.append(f"   - Prompt: {x['prompt']}")
        lines.append("")
    if result["hidden_low_priority"]:
        lines += [f"_{result['hidden_low_priority']} lower-priority (P2/P3) tracker actions are in Tracker._", ""]
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------
# Weekly review
# ----------------------------------------------------------------------

def gone_quiet(quarter_report: dict | None, now: datetime.datetime | None = None,
               quiet_days: int = QUIET_AFTER_DAYS) -> list[dict]:
    """Repos with activity in the last 90 days but nothing in the last
    `quiet_days` — the projects that may have been dropped mid-stride."""
    if not quarter_report:
        return []
    now = now or _now()
    out = []
    for r in quarter_report.get("updated", []):
        last = _parse_ts(r.get("last_at") or "")
        if last is None:
            continue
        days = (now - last).days
        if days >= quiet_days:
            out.append({"repo": r["name"], "owner": r["owner"], "last_at": r["last_at"],
                        "days": days, "commits": r["commit_count"], "prs": r["pr_count"]})
    out.sort(key=lambda x: x["days"])
    return out


def build_review(week_report: dict | None, quarter_report: dict | None, do_next: dict,
                 inflight_items: list[dict] | None, repos: list[dict],
                 now: datetime.datetime | None = None) -> dict:
    """Everything the Weekly review page + export show (repos group-scoped)."""
    now = now or _now()
    scope = {_full(r.get("owner", ""), r["name"]) for r in repos}
    shipped = []
    if week_report:
        for r in week_report.get("updated", []):
            titles = [f"#{p['number']} {p['title']}" for p in r.get("prs", [])]
            if len(titles) < REVIEW_SHIPPED_TITLES:
                titles += [c.get("summary", "") for c in r.get("commits", [])]
            shipped.append({
                "repo": r["name"], "owner": r["owner"], "commits": r["commit_count"],
                "prs": r["pr_count"], "merges": r.get("merge_count", 0),
                "titles": [t for t in titles if t][:REVIEW_SHIPPED_TITLES],
                "more": max(0, r["pr_count"] + r["commit_count"] - REVIEW_SHIPPED_TITLES),
            })
    stale = [{**i, "session_url": safe_session_url(i.get("session_url"))}
             for i in (inflight_items or [])
             if i.get("kind") == "stale" and _full(i.get("owner", ""), i.get("repo", "")) in scope]
    stale.sort(key=lambda i: i.get("last_activity_at") or "")
    waiting = [x for x in do_next["items"] if x["tier"] in WAITING_TIERS]
    forward = [x for x in do_next["items"] if x["tier"] in FORWARD_TIERS][:REVIEW_NEXT_LIMIT]
    return {
        "now": now,
        "week": week_report,
        "shipped": shipped,
        "failed": (week_report or {}).get("failed", []),
        "waiting": waiting,
        "quiet": gone_quiet(quarter_report, now),
        "stale": stale,
        "next": forward,
        "next_total": sum(1 for x in do_next["items"] if x["tier"] in FORWARD_TIERS),
    }


def compose_review_markdown(review: dict, owner_login: str = "", group: str = "",
                            week_label: str = "") -> str:
    scope = f"the **{group}** group" if group else "every scanned repo"
    t = (review.get("week") or {}).get("totals") or {}
    lines = [f"# Weekly review{(': ' + week_label) if week_label else ''}", "",
             f"> Built by RepoDoctor{(' for ' + owner_login) if owner_login else ''} across {scope}.", ""]
    lines += ["## Shipped this week", ""]
    if t:
        lines += [f"**{t.get('repos_updated', 0)} of {t.get('repos_total', 0)} repos updated** · "
                  f"{t.get('commits', 0)} commits · {t.get('prs_merged', 0)} PRs merged · "
                  f"active on {t.get('active_days', 0)} of {t.get('days', 7)} days", ""]
    if review["failed"]:
        lines += ["_GitHub couldn't be read for " + ", ".join(r["name"] for r in review["failed"])
                  + "; they're left out._", ""]
    if review["shipped"]:
        for s in review["shipped"]:
            bits = [f"{s['commits']} commit{'s' if s['commits'] != 1 else ''}"]
            if s["prs"]:
                bits.append(f"{s['prs']} PR{'s' if s['prs'] != 1 else ''} merged")
            lines.append(f"- **{s['repo']}** ({', '.join(bits)}): " + "; ".join(s["titles"]))
        lines.append("")
    elif review.get("week") is not None:
        lines += ["Nothing landed on a default branch this week.", ""]
    else:
        lines += ["_Not read from GitHub (the app isn't connected)._", ""]

    lines += ["## Waiting on you", ""]
    if review["waiting"]:
        for x in review["waiting"]:
            lines.append(f"- [{x['label']}] **{x['repo']}**: {x['title']}")
        lines.append("")
    else:
        lines += ["Nothing to merge, finish, or confirm.", ""]

    lines += ["## Gone quiet", ""]
    if review["quiet"] or review["stale"]:
        for q in review["quiet"]:
            lines.append(f"- **{q['repo']}**: no activity for {q['days']} days "
                         f"({q['commits']} commits, {q['prs']} PRs in the last 90 days)")
        for i in review["stale"]:
            lines.append(f"- **{i.get('repo')}** `{i.get('branch')}`: stale branch, "
                         f"{i.get('age_days')} days without activity")
        lines.append("")
    else:
        lines += ["Nothing went quiet.", ""]

    lines += ["## Next up", ""]
    if review["next"]:
        for x in review["next"]:
            lines.append(f"- [{x['label']}] **{x['repo']}**: {x['title']}")
            if x["prompt"]:
                lines.append(f"  - Prompt: {x['prompt']}")
        if review["next_total"] > len(review["next"]):
            lines.append(f"- …{review['next_total'] - len(review['next'])} more on Do next")
        lines.append("")
    else:
        lines += ["Nothing queued. Generate Projects summaries or Tracker next actions to fill this.", ""]
    return "\n".join(lines).rstrip() + "\n"
