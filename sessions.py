"""
Yesterday — cross-repo session recap ("what was I doing yesterday, and where
did I leave off?").

The owner runs every project through Claude Code on the web, and each web
session shows up in GitHub as a `claude/<slug>-<id>` branch (usually with a
pull request behind it). So a "session" here = one branch's activity inside a
time window, reconstructed from the only signal the app actually has: GitHub
commits, branch comparisons, and pull-request state. There is no access to the
Claude Code transcript itself, so "how you were directing Claude" is inferred
from the arc of commit messages and the PR title — honestly hedged, never
invented.

For each session the screen shows, grounded in git facts:
  - what the session was about (branch/PR topic + commits),
  - a deterministic "what's next" call (merge to main / keep going / finish the
    unfinished work / it's already done / decide on a closed PR),
  - and, on demand, a short plain-English AI narrative of the above.

AI narratives are cached in data/session_recaps.json, keyed per
repo/branch/window, and regenerate only when the branch tip changes. Every
generation is logged to data/logs/sessions.log for paste-back debugging.
"""

from __future__ import annotations

import base64
import datetime
import logging
import re

import anthropic

import github_client as gh
from ai_analyzer import DEFAULT_MODEL, request_json
from briefing import last_push_ts, _parse_ts

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("America/Chicago")  # the owner is in Austin
except Exception as _tz_err:  # pragma: no cover - needs tzdata on Windows
    # zoneinfo itself is stdlib, but Windows has no system tz database; the
    # `tzdata` package (requirements.txt) supplies it. Without it every day
    # window silently becomes a UTC day — say so loudly in the log.
    logger.warning(
        "America/Chicago timezone unavailable (%s: %s) — Yesterday/Next Up "
        "day windows will use UTC. Install tzdata: pip install tzdata",
        type(_tz_err).__name__, _tz_err,
    )
    _LOCAL_TZ = None

# --- Tuning knobs -----------------------------------------------------------

# Pages of PRs to page through per repo (100 per page). Sorted newest-updated
# first, so the first page already covers a normal day of work.
PULLS_MAX_PAGES = 3
# How many no-PR branches to compare per repo before giving up (logged, never
# silent). A branch with a PR is free to inspect; a bare branch costs a compare
# call, so cap the long tail of stale branches.
MAX_BRANCH_COMPARES = 40
# Cap commits kept per session so one giant merge can't dominate a card/prompt.
MAX_COMMITS = 30

# Commit-message markers that read as "left this unfinished".
_WIP_RE = re.compile(
    r"\b(wip|todo|fixme|temp|tmp|checkpoint|debug(?:ging)?|scratch|hack|stash|"
    r"do ?not ?merge|dont ?merge|half|partial|broken|revert me)\b",
    re.IGNORECASE,
)
# Trailing random id Claude Code appends to web-session branch names, e.g.
# "claude/yesterday-work-summary-zqlr51" -> drop "-zqlr51".
_CLAUDE_SUFFIX_RE = re.compile(r"-[a-z0-9]{6}$")

# "what's next" kinds -> chip color family (see style.css tk-* tokens).
NEXT_KIND_LABELS = {
    "merge": "MERGE TO MAIN",
    "continue": "CONTINUE",
    "finish": "FINISH IT",
    "complete": "DONE",
    "decide": "DECIDE",
    "review": "CONFIRM LIVE",
}


# ----------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------

class Window:
    """A resolved [since, until) time window plus display/cache metadata."""

    def __init__(self, since: datetime.datetime, until: datetime.datetime,
                 label: str, key: str, preset: str):
        self.since = since
        self.until = until
        self.since_iso = since.astimezone(datetime.timezone.utc).isoformat()
        self.until_iso = until.astimezone(datetime.timezone.utc).isoformat()
        self.label = label
        self.key = key
        self.preset = preset


def _local_now() -> datetime.datetime:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(_LOCAL_TZ) if _LOCAL_TZ else now


def _start_of_local_day(dt: datetime.datetime) -> datetime.datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def resolve_window(args) -> Window:
    """Resolve the requested window from query args.

    Supported: ?range=yesterday|today|last3|last7 (default yesterday) and
    ?date=YYYY-MM-DD for a single explicit day (overrides range). All day
    boundaries are local (America/Chicago) so "yesterday" means the owner's
    yesterday, then converted to UTC for the GitHub API.
    """
    now = _local_now()
    today_start = _start_of_local_day(now)

    explicit = (args.get("date") or "").strip()
    if explicit:
        try:
            d = datetime.date.fromisoformat(explicit)
            start = datetime.datetime(d.year, d.month, d.day, tzinfo=_LOCAL_TZ or datetime.timezone.utc)
            end = start + datetime.timedelta(days=1)
            # Build the label without %-d / %e — %-d raises ValueError on Windows.
            label = f"{start.strftime('%A, %b')} {start.day}, {start.year}"
            return Window(start, end, label, f"date:{explicit}", "date")
        except (ValueError, OverflowError):  # ?date=9999-12-31 overflows +1 day
            logger.warning("bad date arg %r; falling back to yesterday", explicit)

    preset = (args.get("range") or "yesterday").strip().lower()
    if preset == "today":
        return Window(today_start, now, "today", "today", "today")
    if preset == "last3":
        start = today_start - datetime.timedelta(days=2)
        return Window(start, now, "the last 3 days", "last3", "last3")
    if preset == "last7":
        start = today_start - datetime.timedelta(days=6)
        return Window(start, now, "the last 7 days", "last7", "last7")

    # Default: yesterday (the full previous local day). Avoid %-d (Windows).
    start = today_start - datetime.timedelta(days=1)
    label = f"yesterday ({start.strftime('%A, %b')} {start.day})"
    return Window(start, today_start, label, "yesterday", "yesterday")


def _in_window(ts: str | None, window: Window) -> bool:
    dt = _parse_ts(ts or "")
    if dt is None:
        return False
    return window.since <= dt < window.until


def _at_or_after(ts: str | None, moment: datetime.datetime) -> bool:
    dt = _parse_ts(ts or "")
    return dt is not None and dt >= moment


# ----------------------------------------------------------------------
# Commit + topic helpers
# ----------------------------------------------------------------------

def _commit_date(raw: dict) -> str:
    commit = raw.get("commit") or {}
    committer = commit.get("committer") or {}
    author = commit.get("author") or {}
    return committer.get("date") or author.get("date") or ""


def _normalize_commit(raw: dict) -> dict:
    commit = raw.get("commit") or {}
    message = (commit.get("message") or "").strip()
    summary = message.split("\n", 1)[0].strip()
    author = (commit.get("author") or {}).get("name") or "Unknown"
    sha = raw.get("sha") or ""
    return {
        "sha": sha,
        "short": sha[:7],
        "summary": summary,
        "message": message,
        "author": author,
        "date": _commit_date(raw),
        "is_merge": len(raw.get("parents") or []) > 1,
    }


def _commits_in_window(raw_commits: list[dict], window: Window,
                       drop_merges: bool = False) -> list[dict]:
    """Normalized commits whose date falls in the window, newest-first."""
    out = []
    for raw in raw_commits or []:
        if drop_merges and len(raw.get("parents") or []) > 1:
            continue
        if not _in_window(_commit_date(raw), window):
            continue
        out.append(_normalize_commit(raw))
    out.sort(key=lambda c: c["date"], reverse=True)
    return out[:MAX_COMMITS]


def branch_topic(branch: str) -> str:
    """Human topic from a branch name: 'claude/yesterday-work-summary-zqlr51'
    -> 'yesterday work summary'."""
    name = branch.split("/", 1)[1] if branch.startswith(("claude/", "henry/")) else branch
    name = _CLAUDE_SUFFIX_RE.sub("", name)
    return name.replace("-", " ").replace("_", " ").strip() or branch


def _looks_unfinished(commits: list[dict]) -> bool:
    return bool(commits) and bool(_WIP_RE.search(commits[0]["summary"]))


def session_token(repo: str, branch: str) -> str:
    """Opaque URL-safe id for a session (repo + branch), so the client-side
    generation queue can address one session without leaking '/' into a URL."""
    raw = f"{repo}\x1f{branch}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_token(token: str) -> tuple[str, str] | None:
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + pad).decode("utf-8")
        repo, branch = raw.split("\x1f", 1)
        return repo, branch
    except (ValueError, UnicodeDecodeError):
        return None


def recap_key(repo: str, branch: str, window_key: str) -> str:
    return f"{repo}::{branch}::{window_key}"


# ----------------------------------------------------------------------
# "What's next" — deterministic classification
# ----------------------------------------------------------------------

def classify_next(session: dict) -> tuple[str, str]:
    """(kind, human sentence). Pure function of the gathered git facts so the
    page is useful with zero AI calls."""
    base = session.get("base") or "main"
    pr = session.get("pr")
    ahead = session.get("ahead_by")
    behind = session.get("behind_by")
    n_commits = session.get("commit_count", 0)

    if session.get("is_default"):
        return ("review",
                f"These commits already landed on {base}. If this project is "
                f"live, confirm the change is deployed and working.")

    if pr:
        n = pr["number"]
        if pr.get("merged"):
            when = f" on {pr['merged_at'][:10]}" if pr.get("merged_at") else ""
            return ("complete",
                    f"PR #{n} was merged into {base}{when}. This line of work "
                    f"is done — confirm it's live if the project is in production.")
        if pr.get("state") == "closed":
            return ("decide",
                    f"PR #{n} was closed without merging. Decide whether to "
                    f"reopen it, move the work to a fresh branch, or drop it.")
        if behind and behind > 0:
            return ("merge",
                    f"PR #{n} is open but {base} has moved on ({behind} commit"
                    f"{'s' if behind != 1 else ''} behind). Update the branch, "
                    f"then review the diff and merge to {base}.")
        return ("merge",
                f"PR #{n} is open and ready. Review the diff and merge it to {base}.")

    # No PR on this branch.
    if ahead is not None and ahead == 0:
        return ("complete",
                f"This branch is already contained in {base} — nothing to "
                f"merge. Safe to delete the branch.")
    if _looks_unfinished(session.get("commits", [])):
        return ("finish",
                f"Looks unfinished — the last commit reads like a work-in-"
                f"progress, it's not on {base}, and there's no PR yet. Pick up "
                f"that change, finish it, then open a PR.")
    return ("continue",
            f"{n_commits} commit{'s' if n_commits != 1 else ''} on this branch "
            f"aren't on {base} yet and there's no PR. Keep going, or open a PR "
            f"when you're ready to merge.")


# ----------------------------------------------------------------------
# Gathering
# ----------------------------------------------------------------------

def _repo_touched_since(repo: dict, since: datetime.datetime,
                        until: datetime.datetime | None = None,
                        scanned_at: str | datetime.datetime | None = None) -> bool:
    """Cheap prune using the cached scan's pushed_at — a repo not pushed since
    the window opened cannot have commits inside it.

    The pushed_at values come from a scan SNAPSHOT (often restored from disk),
    so they only prove "no push in the window" when that snapshot was taken
    at or after the window closed. If the scan is older than the window end,
    or the scan time is unknown, the repo is never pruned (a stale snapshot
    would otherwise silently drop repos with real in-window commits)."""
    if until is None or scanned_at is None:
        return True  # can't prove the snapshot covers the window — keep it
    scanned = scanned_at if isinstance(scanned_at, datetime.datetime) else _parse_ts(str(scanned_at))
    if scanned is None:
        return True
    if scanned.tzinfo is None:
        scanned = scanned.replace(tzinfo=datetime.timezone.utc)
    if scanned < until:
        return True  # snapshot predates the window end — pushed_at may be stale
    pushed = _parse_ts(last_push_ts(repo))
    if pushed is None:
        return True  # unknown — don't rule it out
    return pushed >= since


def _build_session(repo: dict, branch: str, is_default: bool, commits: list[dict],
                   window: Window, pr: dict | None,
                   ahead_by: int | None, behind_by: int | None) -> dict:
    owner = repo["owner"]
    name = repo["name"]
    topic = (pr or {}).get("title") or branch_topic(branch)
    dates = [c["date"] for c in commits if c["date"]]
    session = {
        "repo": name,
        "owner": owner,
        "html_url": repo.get("html_url", ""),
        "branch": branch,
        "is_default": is_default,
        "topic": topic.strip(),
        "base": (pr or {}).get("base") or repo.get("default_branch", "main"),
        "pr": pr,
        "commits": commits,
        "commit_count": len(commits),
        "first_commit_at": min(dates) if dates else "",
        "last_commit_at": max(dates) if dates else "",
        "last_activity_at": max(dates) if dates else (pr or {}).get("updated_at", ""),
        "ahead_by": ahead_by,
        "behind_by": behind_by,
        "last_sha": commits[0]["sha"] if commits else (pr or {}).get("head_sha", ""),
        "token": session_token(name, branch),
        "recap_key": recap_key(name, branch, window.key),
    }
    kind, text = classify_next(session)
    session["next_kind"] = kind
    session["next_label"] = NEXT_KIND_LABELS.get(kind, kind.upper())
    session["next_text"] = text
    return session


def _pr_view(pr: dict) -> dict:
    """Compact, JSON-safe PR facts for a session card + prompt."""
    return {
        "number": pr.get("number"),
        "title": (pr.get("title") or "").strip(),
        "state": pr.get("state"),
        "merged": bool(pr.get("merged_at")),
        "merged_at": pr.get("merged_at") or "",
        "closed_at": pr.get("closed_at") or "",
        "merge_commit_sha": pr.get("merge_commit_sha") or "",
        "created_at": pr.get("created_at") or "",
        "updated_at": pr.get("updated_at") or "",
        "html_url": pr.get("html_url", ""),
        "base": (pr.get("base") or {}).get("ref", ""),
        "head": (pr.get("head") or {}).get("ref", ""),
        "head_sha": (pr.get("head") or {}).get("sha", ""),
    }


def _session_from_pr(client, repo: dict, pr_raw: dict, window: Window) -> dict | None:
    owner, name = repo["owner"], repo["name"]
    head = (pr_raw.get("head") or {}).get("ref")
    if not head:
        return None
    number = pr_raw.get("number")
    pr = _pr_view(pr_raw)

    raw_commits = client.get_pull_commits(owner, name, number)
    commits = _commits_in_window(raw_commits, window)

    merged = pr["merged"]
    # A PR is a session for this window if it has commits in-window OR it was
    # opened / merged / closed in-window (a merge with no new commits still
    # counts as "you finished this yesterday").
    events = [pr["created_at"], pr["merged_at"] if merged else pr.get("closed_at", "")]
    if not commits and not any(_in_window(ts, window) for ts in events):
        return None

    ahead_by = behind_by = None
    if pr["state"] == "open":
        comp = client.compare_branches(owner, name, pr["base"] or repo.get("default_branch", "main"), head)
        if comp:
            ahead_by = comp.get("ahead_by")
            behind_by = comp.get("behind_by")
    return _build_session(repo, head, False, commits, window, pr, ahead_by, behind_by)


def _session_from_branch(client, repo: dict, branch: str, window: Window) -> dict | None:
    owner, name = repo["owner"], repo["name"]
    default = repo.get("default_branch", "main")
    comp = client.compare_branches(owner, name, default, branch)
    if not comp:
        return None
    commits = _commits_in_window(comp.get("commits") or [], window)
    if not commits:
        return None
    return _build_session(repo, branch, False, commits, window, None,
                          comp.get("ahead_by", 0), comp.get("behind_by", 0))


_PR_REF_SUFFIX_RE = re.compile(r"\(#(\d+)\)\s*$")


def _is_pr_landing_commit(commit: dict, covered_prs: set[int]) -> bool:
    """True when a default-branch commit is a squash/rebase landing of a PR
    already shown as its own session — GitHub titles those "<title> (#N)"."""
    m = _PR_REF_SUFFIX_RE.search(commit.get("summary") or "")
    return bool(m) and int(m.group(1)) in covered_prs


def _session_from_default(client, repo: dict, window: Window,
                          covered_shas: set[str],
                          covered_prs: set[int] | None = None) -> dict | None:
    """Direct-to-default commits in the window not already attributed to a PR
    branch and not merge commits — i.e. work pushed straight to main.

    covered_shas should include each covered PR's merge_commit_sha (the squash
    or rebase result lands on main with a NEW sha); covered_prs holds the
    covered PR numbers so a "... (#N)" squash title is excluded even when the
    merge sha wasn't reported."""
    owner, name = repo["owner"], repo["name"]
    default = repo.get("default_branch", "main")
    raw = client.get_commits_since(owner, name, window.since_iso, ref=default,
                                   until_iso=window.until_iso)
    covered_prs = covered_prs or set()
    commits = [c for c in _commits_in_window(raw, window, drop_merges=True)
               if c["sha"] not in covered_shas
               and not _is_pr_landing_commit(c, covered_prs)]
    if not commits:
        return None
    return _build_session(repo, default, True, commits, window, None, 0, 0)


def _gather_repo_sessions(client, repo: dict, window: Window) -> list[dict]:
    owner, name = repo["owner"], repo["name"]
    default = repo.get("default_branch", "main")

    branches = client.get_branches(owner, name)
    branch_names = {b["name"] for b in branches}

    pulls = client.get_pulls(owner, name, state="all", sort="updated",
                             direction="desc", max_pages=PULLS_MAX_PAGES)

    out: list[dict] = []
    covered_branches: set[str] = set()
    covered_shas: set[str] = set()
    covered_prs: set[int] = set()

    for pr_raw in pulls:
        # Sorted newest-updated first: once a PR hasn't been touched since the
        # window opened, no later PR can be either — stop paging through them.
        if not _at_or_after(pr_raw.get("updated_at"), window.since):
            break
        sess = _session_from_pr(client, repo, pr_raw, window)
        if sess:
            out.append(sess)
            covered_branches.add(sess["branch"])
            covered_shas.update(c["sha"] for c in sess["commits"])
            pr = sess.get("pr") or {}
            if pr.get("merge_commit_sha"):
                covered_shas.add(pr["merge_commit_sha"])
            if isinstance(pr.get("number"), int):
                covered_prs.add(pr["number"])

    # Bare branches (no PR) with in-window commits — the "in progress, never
    # opened a PR yet" case, which is exactly "where I left off".
    compared = 0
    for b in branches:
        bn = b["name"]
        if bn == default or bn in covered_branches:
            continue
        if compared >= MAX_BRANCH_COMPARES:
            logger.warning(
                "branch-compare cap (%d) reached for %s/%s — remaining bare "
                "branches not checked for window activity",
                MAX_BRANCH_COMPARES, owner, name,
            )
            break
        compared += 1
        sess = _session_from_branch(client, repo, bn, window)
        if sess:
            out.append(sess)
            covered_shas.update(c["sha"] for c in sess["commits"])

    direct = _session_from_default(client, repo, window, covered_shas, covered_prs)
    if direct:
        out.append(direct)

    return out


def gather_sessions(client, repos: list[dict], window: Window,
                    scanned_at: str | datetime.datetime | None = None) -> list[dict]:
    """All sessions across all repos in the window, newest activity first.

    A repo-level failure (a single bad branch/PR fetch) is logged and skipped
    so one broken repo never blanks the whole page. GitHub auth errors
    propagate so the app's global handler can show the re-auth remedy.

    scanned_at is the scan snapshot's timestamp (the scan's "scanned_at").
    Repos are pruned by their pushed_at only when it's given and the scan was
    taken at/after the window end; otherwise every repo is checked live.
    """
    sessions: list[dict] = []
    pruned = 0
    for repo in repos:
        if not _repo_touched_since(repo, window.since, window.until, scanned_at):
            pruned += 1
            continue
        try:
            sessions.extend(_gather_repo_sessions(client, repo, window))
        except gh.GitHubAuthError:
            raise
        except Exception as e:  # noqa: BLE001 - never let one repo blank the page
            logger.warning("session gather failed for %s/%s: %s",
                           repo.get("owner"), repo.get("name"), e)
    if pruned:
        logger.info("session gather pruned %d/%d repos not pushed in window %s",
                    pruned, len(repos), window.key)
    sessions.sort(key=lambda s: s.get("last_activity_at") or "", reverse=True)
    return sessions


def gather_one_session(client, repo: dict, branch: str, window: Window) -> dict | None:
    """Re-gather a single session for the summarize endpoint (stateless, like
    the briefing generator). Handles the PR, bare-branch, and default cases."""
    owner, name = repo["owner"], repo["name"]
    default = repo.get("default_branch", "main")
    if branch == default:
        # Re-run the full repo gather so the default-branch session excludes
        # exactly what the page excluded (PR commits, squash/rebase landings,
        # bare-branch commits). A bare set() here would give a different
        # last_sha than the page and the recap would read stale forever.
        for sess in _gather_repo_sessions(client, repo, window):
            if sess.get("is_default"):
                return sess
        return None

    # Prefer the PR view if one exists for this head (richer state).
    pulls = client.get_pulls(owner, name, state="all", sort="updated",
                             direction="desc", max_pages=PULLS_MAX_PAGES)
    for pr_raw in pulls:
        if (pr_raw.get("head") or {}).get("ref") == branch:
            sess = _session_from_pr(client, repo, pr_raw, window)
            if sess:
                return sess
            break
    return _session_from_branch(client, repo, branch, window)


# ----------------------------------------------------------------------
# AI narrative
# ----------------------------------------------------------------------

SYSTEM_PROMPT = """You are helping a product builder (not a developer) remember what he was doing in a coding session and where he left off. He runs every project through Claude Code, and each session is one Git branch. You are given the FACTS of one session — the branch, its pull-request status, and its commit messages in time order — reconstructed from GitHub. You have NO access to the actual chat transcript.

Write a short, plain-English recap. Ground every statement in the facts provided. When you infer intent (what he asked Claude to do), base it on the commit messages and PR title and hedge honestly ("looks like", "seems to have been"). Never invent features, files, or steps that aren't evidenced.

Return ONLY valid JSON (no markdown fences, no prose before or after) with exactly these fields:
{
  "headline": "One short line (max 12 words) naming what this session was about.",
  "what_you_did": "2-4 plain sentences describing what actually happened this session, drawn from the commit messages. Concrete, past tense.",
  "how_you_directed_claude": "1-3 sentences inferring how he steered Claude — the goal he set and how the work progressed — from the commit arc and PR title. Hedge; do not fabricate transcript detail.",
  "whats_next": "1-2 sentences: the single most useful next action. Respect the provided next-step hint (merge to main / keep going / finish the unfinished work / it's already merged / decide on a closed PR)."
}

Rules:
- Plain English, no jargon, no commit hashes in the text.
- Keep it tight — this is a memory jog, not a report.
- If the facts are thin (one tiny commit), write less. Don't pad."""

# Enforced via structured outputs (ai_analyzer.request_json) so the model
# can't reply with prose. Mirrors normalize_recap, which stays as the final
# guard.
RECAP_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "what_you_did": {"type": "string"},
        "how_you_directed_claude": {"type": "string"},
        "whats_next": {"type": "string"},
    },
    "required": ["headline", "what_you_did", "how_you_directed_claude", "whats_next"],
    "additionalProperties": False,
}


def build_summary_prompt(session: dict) -> str:
    pr = session.get("pr")
    lines = [
        f"Repository: {session['repo']}",
        f"Branch (this session): {session['branch']}",
        f"Topic (from branch/PR): {session['topic']}",
        f"Base branch: {session.get('base', 'main')}",
    ]
    if pr:
        state = "merged" if pr.get("merged") else pr.get("state", "?")
        lines.append(f"Pull request: #{pr['number']} \"{pr.get('title', '')}\" — {state}")
        if pr.get("merged_at"):
            lines.append(f"Merged at: {pr['merged_at']}")
    else:
        lines.append("Pull request: none open for this branch")
    if session.get("ahead_by") is not None:
        lines.append(f"Ahead of base by {session['ahead_by']} commit(s); "
                     f"behind by {session.get('behind_by', 0)}.")
    lines.append(f"Deterministic next-step hint ({session['next_kind']}): {session['next_text']}")

    lines.append("\nCommits in this session (newest first):")
    for c in session.get("commits", []):
        when = c["date"][:16].replace("T", " ") if c["date"] else ""
        lines.append(f"  - {c['summary']} ({when})")
    if not session.get("commits"):
        lines.append("  (no new commits in the window — the session was a merge/close event)")

    lines.append("\nWrite the recap JSON now. Return ONLY the JSON object.")
    return "\n".join(lines)


def normalize_recap(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raw = {}

    def _t(key: str) -> str:
        v = raw.get(key)
        return v.strip() if isinstance(v, str) else ""

    return {
        "headline": _t("headline"),
        "what_you_did": _t("what_you_did"),
        "how_you_directed_claude": _t("how_you_directed_claude"),
        "whats_next": _t("whats_next"),
    }


def summarize_session(api_key: str, session: dict, model: str = DEFAULT_MODEL) -> dict:
    """Generate one session's plain-English recap. Returns normalized dict +
    _usage. A refusal/unparseable response raises ValueError (logged here) so
    the route keeps any existing good recap instead of saving an empty one
    stamped with the current _source_sha (which would then read as fresh)."""
    client = anthropic.Anthropic(api_key=api_key)
    try:
        parsed, usage = request_json(
            client,
            model=model,
            max_tokens=1200,
            schema=RECAP_SCHEMA,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_summary_prompt(session)}],
        )
    except ValueError as e:
        logger.warning("session recap generation failed for %s/%s: %s",
                       session.get("repo", "?"), session.get("branch", "?"), e)
        raise
    recap = normalize_recap(parsed)
    recap["_usage"] = usage
    return recap


def is_recap_stale(recap: dict | None, session: dict) -> bool:
    """A recap is stale when the branch tip moved since it was written."""
    if not recap:
        return True
    return recap.get("_source_sha", "") != (session.get("last_sha") or "")


def attach_recaps(sessions: list[dict], recaps: dict) -> None:
    """Attach a cached recap (if fresh) to each session, in place, and set a
    `needs_summary` flag for the generation queue."""
    for s in sessions:
        recap = recaps.get(s["recap_key"])
        fresh = recap and not is_recap_stale(recap, s)
        s["recap"] = recap if fresh else None
        s["needs_summary"] = not fresh


# ----------------------------------------------------------------------
# Markdown export (paste into a Claude chat to resume)
# ----------------------------------------------------------------------

def _local_time_label(iso_ts: str) -> str:
    dt = _parse_ts(iso_ts)
    if dt is None:
        return ""
    if _LOCAL_TZ:
        dt = dt.astimezone(_LOCAL_TZ)
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%b %d')} {hour}:{dt.strftime('%M %p')}"


def compose_markdown(sessions: list[dict], owner_login: str, window_label: str) -> str:
    repos = sorted({s["repo"] for s in sessions})
    lines = [
        f"# Where I left off — {window_label}",
        "",
        "> Reconstructed from GitHub activity by RepoDoctor. Each session is one",
        "> Claude Code branch. Hand this to a Claude chat to pick up where you",
        f"> left off. {len(sessions)} session(s) across {len(repos)} repo(s).",
        "",
    ]
    if owner_login:
        lines += [f"Owner: github.com/{owner_login}", ""]

    if not sessions:
        lines += [f"No session activity found for {window_label}.", ""]
        return "\n".join(lines)

    lines += ["## Sessions", ""]
    for s in sessions:
        pr = s.get("pr")
        pr_bit = f" · PR #{pr['number']} ({'merged' if pr.get('merged') else pr.get('state')})" if pr else ""
        lines.append(f"### {s['repo']}: {s['topic']}")
        lines.append(f"_branch `{s['branch']}`{pr_bit} · "
                     f"{s['commit_count']} commit(s) this period_")
        lines.append("")
        recap = s.get("recap")
        if recap:
            if recap.get("what_you_did"):
                lines += [f"**What you did:** {recap['what_you_did']}", ""]
            if recap.get("how_you_directed_claude"):
                lines += [f"**How you directed Claude:** {recap['how_you_directed_claude']}", ""]
            if recap.get("whats_next"):
                lines += [f"**What's next:** {recap['whats_next']}", ""]
        else:
            lines += [f"**What's next:** {s['next_text']}", ""]
        if s.get("commits"):
            lines.append("**Commits:**")
            for c in s["commits"]:
                when = _local_time_label(c["date"])
                stamp = f" ({when})" if when else ""
                lines.append(f"- {c['summary']}{stamp}")
            lines.append("")
    return "\n".join(lines)


def summarize_counts(sessions: list[dict]) -> dict:
    """Headline counts for the page banner."""
    repos = {s["repo"] for s in sessions}
    by_kind: dict[str, int] = {}
    for s in sessions:
        by_kind[s["next_kind"]] = by_kind.get(s["next_kind"], 0) + 1
    return {
        "sessions": len(sessions),
        "repos": len(repos),
        "needs_merge": by_kind.get("merge", 0),
        "in_progress": by_kind.get("continue", 0) + by_kind.get("finish", 0),
        "done": by_kind.get("complete", 0),
    }
