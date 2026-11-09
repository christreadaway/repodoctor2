"""
Health grade: one letter (A-F) per repo, with a visible breakdown, so
neglected projects surface themselves on My Repos and Portfolio.

Pure functions over data the app already has. Nothing here calls GitHub:
  - the cached scan (github_client.scan_repo_lite): required_files,
    docs_updated (the dashboard's "Current?" column), branch_names / counts,
    pushed_at / updated_at, error;
  - the In Flight cache (models.get_inflight), when it exists, for which
    branches are STALE or MERGED-but-not-deleted;
  - the owner's tags (models.get_groups), so a repo tagged "Archived" steps
    out of grading.

Four checks, weights below, score 0-100. A check the scan can't answer is
left out of the score (never guessed), and the score is re-scaled over the
checks that could be scored. A repo that shouldn't be graded at all (scan
error, archived, fork, empty, too little data) gets "—" with a reason
instead of an F that nags forever.

Deterministic: the same repo + In Flight items + `now` always give the same
grade. CI/test status and security alerts are deliberately out of scope
(they'd need new GitHub calls on page view).
"""

from __future__ import annotations

import datetime
import logging
from typing import Iterable

logger = logging.getLogger(__name__)

# --- Weights (sum to 100) ------------------------------------------------------
WEIGHT_DOCS = 30        # required docs present (6 points per file)
WEIGHT_CURRENT = 15     # docs current: the dashboard's "Current?" column
WEIGHT_BRANCHES = 25    # branches tidy
WEIGHT_ACTIVE = 30      # recently active (last push): neglect is mostly this

# The five files the dashboard checks, in the dashboard's column order.
REQUIRED_DOCS = ("CLAUDE.md", "LICENSE", "PRODUCT_SPEC.md", "PROJECT_STATUS.md", "SESSION_NOTES.md")
POINTS_PER_DOC = WEIGHT_DOCS // len(REQUIRED_DOCS)
# The files scan_repo_lite's docs_updated flag covers (github_client.py), and
# its window: docs count as current when last committed within this many days
# of the latest commit. Used for wording only; the flag itself is reused as-is.
FRESHNESS_DOCS = ("PRODUCT_SPEC.md", "SESSION_NOTES.md")
DOCS_CURRENT_WINDOW_DAYS = 7

# Ladders: (upper bound inclusive, points, status), first match wins; the
# trailing pair is what you get past the last bound.
# In Flight known: "dead" branches = STALE + MERGED-but-not-deleted.
DEAD_BRANCH_LADDER = ((0, 25, "ok"), (1, 15, "warn"), (3, 8, "bad"))
DEAD_BRANCH_FLOOR = (0, "bad")
# No In Flight data: the open-branch count is only a proxy (live work and
# leftovers look the same), so it never costs the whole check.
OPEN_BRANCH_LADDER = ((2, 25, "ok"), (5, 15, "warn"))
OPEN_BRANCH_FLOOR = (5, "bad")
# Days since the last push.
ACTIVE_LADDER = ((30, 30, "ok"), (90, 20, "warn"), (180, 10, "bad"))
ACTIVE_FLOOR = (0, "bad")

# Lowest score for each letter; anything below the last is F.
GRADE_FLOORS = (("A", 90), ("B", 80), ("C", 70), ("D", 60))
GRADES = ("A", "B", "C", "D", "F")
# Color tone per letter (the letter itself is always the visible text).
GRADE_TONES = {"A": "ok", "B": "ok", "C": "warn", "D": "bad", "F": "bad"}

# Fewer scoreable points than this (out of 100) and the repo isn't graded:
# a letter built on less than half the evidence would mislead.
MIN_GRADABLE_POINTS = 50

NOT_GRADED = "—"
# Tag a repo with one of these (case-insensitive) to park it: not graded.
ARCHIVE_TAGS = frozenset({"archived", "archive"})
# Branches with "henry" in the name don't count, same as the dashboard.
HENRY = "henry"

STATUS_MARKS = {"ok": "OK", "warn": "WARN", "bad": "FIX"}
NOT_SCORED_MARK = "N/A"

EXEMPT_REASONS = {
    "scan_error": "Not graded: the last scan couldn't read this repo. Re-scan to retry.",
    "archived": "Not graded: archived on GitHub.",
    "archived_tag": "Not graded: tagged Archived. Remove the tag to grade it again.",
    "fork": "Not graded: a fork. Its upkeep belongs to the original project.",
    "empty": "Not graded: empty repo, no commits yet.",
    "too_little_data": "Not graded: the scan has too little data on this repo. Re-scan to grade it.",
    "grade_error": "Not graded: grading this repo failed (details in data/logs/health.log).",
}

# What fixing each check means, for the "biggest lift" summary.
_FIX_PHRASES = {
    "docs": "Adding the missing docs",
    "current": "Updating the docs",
    "branches": "Cleaning up branches",
    "active": "A fresh push",
}


# --- small helpers ---------------------------------------------------------------

def repo_key(repo: dict) -> str:
    """owner/name, the key grades are stored under. My Repos looks grades up
    through this (app.dashboard's health_for); Portfolio rows key by full_name."""
    return repo.get("full_name") or f"{repo.get('owner') or ''}/{repo.get('name') or ''}"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_ts(value) -> datetime.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _ladder(value: int, ladder, floor) -> tuple[int, str]:
    for limit, points, status in ladder:
        if value <= limit:
            return points, status
    return floor


def _plural(n: int, word: str) -> str:
    """'1 open branch' / '3 open branches'."""
    if n == 1:
        return f"1 {word}"
    return f"{n} {word}{'es' if word.endswith(('ch', 'sh', 's', 'x')) else 's'}"


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _is_henry(branch: str, default: str) -> bool:
    return HENRY in branch.lower() and branch != default


def _check(key: str, label: str, status: str, points: int, max_points: int, detail: str,
           counted: bool = True, **extra) -> dict:
    out = {
        "key": key,
        "label": label,
        "status": status,
        "points": points if counted else 0,
        "max": max_points if counted else 0,
        "detail": detail,
        "counted": counted,
        "mark": STATUS_MARKS[status] if counted else NOT_SCORED_MARK,
    }
    out.update(extra)
    return out


def _not_scored(key: str, label: str, detail: str, **extra) -> dict:
    return _check(key, label, "warn", 0, 0, detail, counted=False, **extra)


# --- the four checks -------------------------------------------------------------

def check_docs(repo: dict) -> dict:
    """Required docs present: 6 points per file found (the dashboard's Y / - cells)."""
    label = "Required docs"
    rf = repo.get("required_files")
    if not isinstance(rf, dict) or not rf:
        return _not_scored("docs", label, "The scan has no file list for this repo. Re-scan to check.")
    present = [d for d in REQUIRED_DOCS if rf.get(d)]
    missing = [d for d in REQUIRED_DOCS if not rf.get(d)]
    points = POINTS_PER_DOC * len(present)
    if not missing:
        return _check("docs", label, "ok", points, WEIGHT_DOCS, f"All {len(REQUIRED_DOCS)} present.")
    status = "warn" if len(present) >= 3 else "bad"
    it = "it" if len(missing) == 1 else "them"
    return _check("docs", label, status, points, WEIGHT_DOCS,
                  f"Missing {_join(missing)}. Add {it}.", missing=missing)


def check_current(repo: dict) -> dict:
    """Docs current: reuses the scan's docs_updated flag, the exact value the
    dashboard's "Current?" column shows (True = Yes, False = No, None = —)."""
    label = "Docs current"
    flag = repo.get("docs_updated")
    rf = repo.get("required_files")
    known_files = isinstance(rf, dict) and bool(rf)
    present = [d for d in FRESHNESS_DOCS if known_files and rf.get(d)]
    names = _join(present) if present else "PRODUCT_SPEC.md / SESSION_NOTES.md"
    if flag is True:
        return _check("current", label, "ok", WEIGHT_CURRENT, WEIGHT_CURRENT,
                      f"{names} kept up with the latest commit.")
    if flag is False:
        it = "it" if len(present) == 1 else "them"
        verb = "is" if len(present) == 1 else "are"
        return _check("current", label, "bad", 0, WEIGHT_CURRENT,
                      f"{names} {verb} more than {DOCS_CURRENT_WINDOW_DAYS} days behind the "
                      f"latest commit. Update {it}.")
    # None: either the docs don't exist (nothing can be current: 0, never a
    # free pass, or deleting stale docs would raise the grade) or the scan
    # couldn't read a commit date (unknown: not scored).
    if known_files and not present:
        return _check("current", label, "bad", 0, WEIGHT_CURRENT,
                      "No PRODUCT_SPEC.md or SESSION_NOTES.md to keep current. Add them.")
    return _not_scored("current", label,
                       "Couldn't check: the scan has no commit date for these docs. Re-scan to check.")


def _open_branch_count(repo: dict) -> int | None:
    """Non-default, non-henry branches from the scan; None when unknown."""
    default = repo.get("default_branch") or "main"
    names = repo.get("branch_names")
    if isinstance(names, list) and names:
        return sum(1 for b in names if isinstance(b, str) and b != default and not _is_henry(b, default))
    nh = repo.get("non_henry_branch_count")
    if isinstance(nh, int) and not isinstance(nh, bool):
        return max(0, nh - 1)  # non_henry_branch_count includes the default branch
    nd = repo.get("non_default_branch_count")
    if isinstance(nd, int) and not isinstance(nd, bool):
        return max(0, nd)
    return None


def check_branches(repo: dict, inflight_items: list[dict] | None = None) -> dict:
    """Branches tidy. With In Flight data for this repo: count the dead
    branches (STALE + MERGED-not-deleted). Without it: the open-branch count
    from the scan, a gentler proxy."""
    label = "Branches tidy"
    default = repo.get("default_branch") or "main"
    if inflight_items is not None:
        existing = repo.get("branch_names")
        existing = set(existing) if isinstance(existing, list) and existing else None
        owner, name = repo.get("owner"), repo.get("name")
        mine = []
        for i in inflight_items:
            if not isinstance(i, dict):
                continue
            if i.get("repo") != name or (i.get("owner") and owner and i.get("owner") != owner):
                continue
            branch = i.get("branch") or ""
            if not isinstance(branch, str) or not branch or _is_henry(branch, default):
                continue
            # Cross-check with the scan so a branch deleted since one of the two
            # reads isn't counted against the repo.
            if existing is not None and branch not in existing:
                continue
            mine.append(i)
        stale = sum(1 for i in mine if i.get("kind") == "stale")
        merged = sum(1 for i in mine if i.get("kind") == "complete")
        dead = stale + merged
        live = len(mine) - dead
        points, status = _ladder(dead, DEAD_BRANCH_LADDER, DEAD_BRANCH_FLOOR)
        if dead == 0:
            detail = (f"No branches besides {default}." if live == 0 else
                      f"{_plural(live, 'branch')} in progress, none stale or merged.")
        else:
            parts = []
            if stale:
                parts.append(f"{_plural(stale, 'stale branch')}")
            if merged:
                parts.append(f"{_plural(merged, 'merged branch')} not deleted")
            if stale and merged:
                action = "Delete or finish them in In flight."
            elif merged:
                action = f"Delete {'it' if merged == 1 else 'them'} in In flight."
            else:
                action = f"Finish or delete {'it' if stale == 1 else 'them'} in In flight."
            detail = f"{_join(parts)}. {action}"
        return _check("branches", label, status, points, WEIGHT_BRANCHES, detail,
                      source="inflight", stale=stale, merged=merged, live=live)

    n = _open_branch_count(repo)
    if n is None:
        return _not_scored("branches", label, "The scan has no branch list for this repo. Re-scan to check.",
                           source="scan")
    points, status = _ladder(n, OPEN_BRANCH_LADDER, OPEN_BRANCH_FLOOR)
    if n == 0:
        detail = f"No branches besides {default}."
    elif status == "ok":
        detail = f"{_plural(n, 'open branch')}."
    else:
        detail = (f"{_plural(n, 'open branch')}. Refresh In flight to see which are stale "
                  f"or already merged.")
    return _check("branches", label, status, points, WEIGHT_BRANCHES, detail, source="scan", open=n)


def check_active(repo: dict, now: datetime.datetime | None = None) -> dict:
    """Recently active: days since the last push (pushed_at; updated_at only
    when the scan has no push date)."""
    label = "Recently active"
    now = now or _now()
    dt = _parse_ts(repo.get("pushed_at"))
    noun = "push"
    if dt is None:
        dt = _parse_ts(repo.get("updated_at"))
        noun = "update"
    if dt is None:
        return _not_scored("active", label, "The scan has no push date for this repo. Re-scan to check.")
    days = max(0, (now - dt).days)
    points, status = _ladder(days, ACTIVE_LADDER, ACTIVE_FLOOR)
    if status == "ok":
        when = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
        detail = f"Last {noun} {when}."
    elif status == "warn":
        detail = f"No {noun} in {days} days."
    else:
        detail = f"No {noun} in {days} days. Pick it back up, or tag it Archived if it's done."
    return _check("active", label, status, points, WEIGHT_ACTIVE, detail, days=days)


# --- grading -----------------------------------------------------------------------

def letter_for(score: int) -> str:
    for letter, floor in GRADE_FLOORS:
        if score >= floor:
            return letter
    return "F"


def _scaled(earned: int, possible: int) -> int:
    """earned/possible as 0-100, rounded half up with integer math (deterministic)."""
    return (200 * earned + possible) // (2 * possible)


def exempt_reason(repo: dict, tags: Iterable[str] = ()) -> str | None:
    """Why this repo isn't graded, or None. Order matters: a failed scan
    says nothing about the repo, so it wins."""
    if repo.get("error"):
        return "scan_error"
    if repo.get("archived") is True:
        return "archived"
    if any(isinstance(t, str) and t.strip().lower() in ARCHIVE_TAGS for t in tags or ()):
        return "archived_tag"
    if repo.get("fork") is True:
        return "fork"
    total = repo.get("total_branch_count")
    if total == 0 and not isinstance(total, bool) and not repo.get("branch_names"):
        return "empty"
    return None


def _exempt_result(reason: str) -> dict:
    summary = EXEMPT_REASONS[reason]
    return {
        "grade": NOT_GRADED,
        "score": None,
        "graded": False,
        "tone": "none",
        "reason": reason,
        "checks": [],
        "summary": summary,
        "title": f"Health {NOT_GRADED}\n{summary}",
    }


def breakdown_text(result: dict) -> str:
    """Plain-text breakdown (hover tooltip, logs, copy/paste)."""
    if not result.get("graded"):
        return f"Health {result.get('grade', NOT_GRADED)}\n{result.get('summary', '')}"
    lines = [f"Health {result['grade']} ({result['score']}/100)"]
    for c in result["checks"]:
        pts = f"{c['points']}/{c['max']}" if c["counted"] else "not scored"
        lines.append(f"{c['mark']} {c['label']} {pts}: {c['detail']}")
    lines.append(result["summary"])
    return "\n".join(lines)


def grade_repo(repo: dict, inflight_items: list[dict] | None = None,
               now: datetime.datetime | None = None, *, tags: Iterable[str] = ()) -> dict:
    """Grade one scanned repo.

    inflight_items: None when there's no In Flight data for this repo (no
    cache, or the last gather failed on it); otherwise In Flight items (any
    repo's: they're filtered to this one). tags: the repo's tag/group names.

    Returns {"grade", "score", "graded", "tone", "reason", "checks",
    "summary", "title"}; each check is {"key", "label", "status"
    ("ok"|"warn"|"bad"), "points", "max", "detail", "counted", "mark"}.
    """
    reason = exempt_reason(repo, tags)
    if reason:
        return _exempt_result(reason)
    now = now or _now()
    checks = [
        check_docs(repo),
        check_current(repo),
        check_branches(repo, inflight_items),
        check_active(repo, now),
    ]
    counted = [c for c in checks if c["counted"]]
    possible = sum(c["max"] for c in counted)
    if possible < MIN_GRADABLE_POINTS:
        return _exempt_result("too_little_data")
    earned = sum(c["points"] for c in counted)
    score = _scaled(earned, possible)
    grade = letter_for(score)

    worst = max(counted, key=lambda c: c["max"] - c["points"])  # first wins ties: check order
    lost = worst["max"] - worst["points"]
    if lost == 0:
        summary = (f"{score}/100. Every check passes." if len(counted) == len(checks) else
                   f"{score}/100. Every check that could be scored passes.")
    else:
        lifted = _scaled(earned + lost, possible)
        if letter_for(lifted) == "F":
            # Several things are wrong at once; in practice that's an idle repo.
            summary = f"{score}/100. No single fix gets it past F. If it's finished, tag it Archived."
        else:
            summary = (f"{score}/100. {_FIX_PHRASES[worst['key']]} alone lifts it to "
                       f"{lifted} ({letter_for(lifted)}).")
    result = {
        "grade": grade,
        "score": score,
        "graded": True,
        "tone": GRADE_TONES[grade],
        "reason": "",
        "checks": checks,
        "summary": summary,
    }
    result["title"] = breakdown_text(result)
    return result


def distribution(results: Iterable[dict]) -> dict:
    """{"A": n, ..., "F": n, "not_graded": n} — always every key, in order."""
    out = {g: 0 for g in GRADES}
    out["not_graded"] = 0
    for r in results:
        g = r.get("grade")
        if g in out:
            out[g] += 1
        else:
            out["not_graded"] += 1
    return out


def grade_scan(repos: list[dict], inflight: dict | None = None,
               now: datetime.datetime | None = None, groups: dict | None = None) -> dict:
    """Grade every repo in a scan.

    inflight: the In Flight cache dict ({"items": [...], "failed_repos":
    [...]}) or None. Repos the last gather failed on are graded as if there
    were no cache. groups: {tag: [repo names]}, for the Archived tag.

    Returns {"grades": {owner/name: result}, "distribution", "reasons",
    "errors": [{"repo", "error"}], "inflight_used", "graded_at"}. One repo's
    bad data never breaks the rest: it's marked "—" and listed in errors.
    """
    now = now or _now()
    have_inflight = isinstance(inflight, dict) and isinstance(inflight.get("items"), list)
    items_by_repo: dict[str, list[dict]] = {}
    failed: set[str] = set()
    if have_inflight:
        for i in inflight["items"]:
            if isinstance(i, dict):
                items_by_repo.setdefault(f"{i.get('owner')}/{i.get('repo')}", []).append(i)
        failed = {f for f in (inflight.get("failed_repos") or []) if isinstance(f, str)}
    tags_by_name: dict[str, list[str]] = {}
    for tag, members in (groups or {}).items():
        for m in members or []:
            tags_by_name.setdefault(m, []).append(tag)

    grades: dict[str, dict] = {}
    errors: list[dict] = []
    for repo in repos or []:
        if not isinstance(repo, dict):
            continue
        key = repo_key(repo)
        items = items_by_repo.get(key, []) if have_inflight and key not in failed else None
        try:
            grades[key] = grade_repo(repo, items, now, tags=tags_by_name.get(repo.get("name"), ()))
        except Exception as e:  # noqa: BLE001 - one repo's odd data never blanks the page
            logger.warning("health: grading %s failed: %s", key, e, exc_info=True)
            errors.append({"repo": key, "error": f"{type(e).__name__}: {e}"})
            grades[key] = _exempt_result("grade_error")
    reasons: dict[str, int] = {}
    for g in grades.values():
        if not g["graded"]:
            reasons[g["reason"]] = reasons.get(g["reason"], 0) + 1
    return {
        "grades": grades,
        "distribution": distribution(grades.values()),
        "reasons": reasons,
        "errors": errors,
        "inflight_used": have_inflight,
        "graded_at": now.isoformat(),
    }
