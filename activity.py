"""
Activity — what changed across every repo in a date range.

Stats answers "how busy was I?" with bar charts. This module answers the
portfolio question underneath it: "across ALL my repos, what did we actually
update in the last week / month / quarter / year-to-date / any custom range?"

For every repo in the latest scan it reads, live from GitHub:
  - commits on the default branch (what actually landed), and
  - pull requests merged in the range (the human-readable "what").
Merge commits ("Merge pull request #51 …") are counted separately and kept
out of the commit list and totals: the branch commits they bring in are
already counted, so including them would double-count the work.

Ranges are whole local days (America/Chicago, same as Next Up), inclusive of
both ends, so "Week" on a Monday means last Tuesday 00:00 through today
23:59. Presets are rolling: Week = last 7 days, Month = last 30, Quarter =
last 90, YTD = Jan 1 through today. Custom = any from/to pair.

Fetching is the expensive part, so the app fetches ONE superset window (far
enough back to cover every preset — see `fetch_floor`) and every preset is
then a local filter over that cache. A custom range that starts earlier
refetches from its own start. Every fetch, per-repo failure, and page view is
logged to data/logs/activity.log (one JSON object per line) for paste-back
debugging, and through the root logger to data/logs/app.log.
"""

from __future__ import annotations

import collections
import datetime
import logging
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import github_client as gh
from briefing import _parse_ts
from sessions import _LOCAL_TZ, _normalize_commit

logger = logging.getLogger(__name__)

# --- Tuning knobs -----------------------------------------------------------

# Presets: (key, chip label, rolling days or None for year-to-date).
PRESETS = (
    ("week", "Week", 7),
    ("month", "Month", 30),
    ("quarter", "Quarter", 90),
    ("ytd", "YTD", None),
)
PRESET_KEYS = tuple(p[0] for p in PRESETS)
_PRESET_LONG = {
    "week": "Last 7 days",
    "month": "Last 30 days",
    "quarter": "Last 90 days",
    "ytd": "Year to date",
    "custom": "Custom range",
}
DEFAULT_PRESET = "week"
# GitHub launched in 2008; nothing can predate it, and clamping keeps a typo
# like 0202-01-01 from asking the API for two millennia of history.
EARLIEST_DATE = datetime.date(2008, 1, 1)

# Commit pages per repo (100 per page). 50 pages = 5,000 commits on one
# default branch inside the window, far past any personal repo; hitting it is
# flagged on the card ("showing the first 5,000"), never silent.
COMMIT_MAX_PAGES = 50
# Closed-PR pages per repo, newest-updated first; paging stops as soon as a
# page reaches PRs last touched before the window, so this is only a ceiling.
PR_MAX_PAGES = 10
# Parallel repo fetches. Matches the old Stats page.
MAX_WORKERS = 8
# Commits shown per repo card before "+N more on GitHub", and per repo in the
# COPY / .md export (a YTD paste of 900 commits helps nobody).
CARD_COMMIT_LIMIT = 40
EXPORT_COMMIT_LIMIT = 50


# ----------------------------------------------------------------------
# Date range
# ----------------------------------------------------------------------

def _local_now() -> datetime.datetime:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(_LOCAL_TZ) if _LOCAL_TZ else now


def _local_midnight(d: datetime.date) -> datetime.datetime:
    # Constructing with tzinfo (not .replace on a converted datetime) lets
    # zoneinfo pick the right UTC offset for that date across DST changes.
    return datetime.datetime(d.year, d.month, d.day, tzinfo=_LOCAL_TZ or datetime.timezone.utc)


def _day_label(d: datetime.date, with_year: bool = True) -> str:
    # No %-d: it raises ValueError on Windows.
    base = f"{d.strftime('%b')} {d.day}"
    return f"{base}, {d.year}" if with_year else base


def span_label(start: datetime.date, end: datetime.date) -> str:
    """'Nov 3 – Nov 9, 2026', 'Dec 28, 2025 – Jan 3, 2026', or 'Nov 9, 2026'."""
    if start == end:
        return _day_label(start)
    if start.year == end.year:
        return f"{_day_label(start, with_year=False)} – {_day_label(end)}"
    return f"{_day_label(start)} – {_day_label(end)}"


class DateRange:
    """Whole local days [start_date, end_date], inclusive, plus display and
    cache metadata. `since`/`until` are the half-open datetime bounds."""

    def __init__(self, start: datetime.date, end: datetime.date, preset: str):
        self.start_date = start
        self.end_date = end
        self.preset = preset
        self.since = _local_midnight(start)
        self.until = _local_midnight(end + datetime.timedelta(days=1))
        self.since_iso = self.since.astimezone(datetime.timezone.utc).isoformat()
        self.until_iso = self.until.astimezone(datetime.timezone.utc).isoformat()
        self.days = (end - start).days + 1
        self.name = _PRESET_LONG.get(preset, "Custom range")
        self.dates_label = span_label(start, end)
        self.label = f"{self.name} ({self.dates_label})"
        self.key = f"{preset}:{start.isoformat()}:{end.isoformat()}"

    def contains(self, iso_ts: str | None) -> bool:
        dt = _parse_ts(iso_ts or "")
        return dt is not None and self.since <= dt < self.until

    def query_args(self) -> dict:
        """URL args that reproduce this range (for links/exports)."""
        if self.preset == "custom":
            return {"range": "custom", "from": self.start_date.isoformat(),
                    "to": self.end_date.isoformat()}
        return {"range": self.preset}


def preset_range(preset: str, today: datetime.date) -> DateRange:
    if preset == "ytd":
        return DateRange(datetime.date(today.year, 1, 1), today, "ytd")
    days = dict((k, d) for k, _, d in PRESETS).get(preset) or 7
    return DateRange(today - datetime.timedelta(days=days - 1), today, preset)


def _parse_day(raw: str | None) -> datetime.date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


def resolve_range(args, now: datetime.datetime | None = None) -> tuple[DateRange, str | None]:
    """Resolve ?range=week|month|quarter|ytd|custom&from=YYYY-MM-DD&to=YYYY-MM-DD.

    Returns (range, notice). `notice` is a plain sentence when the request was
    adjusted (bad date, reversed dates, future end) so the page can say so
    instead of silently showing a different range than the user picked.
    """
    now = now or _local_now()
    today = now.date()
    preset = (args.get("range") or "").strip().lower()
    raw_from, raw_to = args.get("from"), args.get("to")

    if preset == "custom" or ((raw_from or raw_to) and preset not in PRESET_KEYS):
        start, end = _parse_day(raw_from), _parse_day(raw_to)
        if (raw_to or "").strip() and end is None:
            return (preset_range(DEFAULT_PRESET, today),
                    f"“{raw_to}” isn't a date — showing the last 7 days instead.")
        if start is None:
            what = f"“{raw_from}” isn't a date" if (raw_from or "").strip() else "A custom range needs a start date"
            return preset_range(DEFAULT_PRESET, today), f"{what} — showing the last 7 days instead."
        notes = []
        end = end or today
        if end > today:
            end = today
            notes.append("the end date was in the future, so it stops at today")
        if start > today:
            start = today
            notes.append("the start date was in the future, so it starts today")
        if start > end:
            start, end = end, start
            notes.append("the dates were reversed, so they were swapped")
        if start < EARLIEST_DATE:
            start = EARLIEST_DATE
            notes.append("GitHub history starts in 2008, so the range starts Jan 1, 2008")
        if end < EARLIEST_DATE:
            end = EARLIEST_DATE
        notice = ("Adjusted: " + "; ".join(notes) + ".") if notes else None
        return DateRange(start, end, "custom"), notice

    if preset and preset not in PRESET_KEYS:
        return (preset_range(DEFAULT_PRESET, today),
                f"Unknown range “{preset}” — showing the last 7 days instead.")
    return preset_range(preset or DEFAULT_PRESET, today), None


def fetch_floor(today: datetime.date) -> datetime.date:
    """The earliest day one fetch must reach so EVERY preset is served from
    the same cache: Jan 1 (YTD) or 90 days back (Quarter), whichever is
    earlier — in January the quarter reaches into last year."""
    return min(datetime.date(today.year, 1, 1),
               today - datetime.timedelta(days=89))


# ----------------------------------------------------------------------
# Fetch (one repo, then the whole portfolio)
# ----------------------------------------------------------------------

def _normalize_pr(raw: dict) -> dict:
    return {
        "number": raw.get("number"),
        "title": (raw.get("title") or "").strip() or f"PR #{raw.get('number')}",
        "merged_at": raw.get("merged_at") or "",
        "html_url": raw.get("html_url") or "",
        "author": ((raw.get("user") or {}).get("login") or ""),
        "base": ((raw.get("base") or {}).get("ref") or ""),
    }


def fetch_repo(client, repo: dict, since_iso: str) -> dict:
    """Default-branch commits + merged PRs since `since_iso` for one repo.

    Raises GitHubAuthError (dead PAT — the caller surfaces the remedy) and
    GitHubFetchError / requests errors (rate limit, 5xx, network) so a
    partial read is reported as a failure, never shown as "no activity".
    """
    owner, name = repo["owner"], repo["name"]
    ref = repo.get("default_branch") or None
    raw_commits, commits_truncated = client.get_commits_range(
        owner, name, since_iso, ref=ref, max_pages=COMMIT_MAX_PAGES)
    raw_prs, prs_truncated = client.get_merged_pulls_since(
        owner, name, since_iso, max_pages=PR_MAX_PAGES)
    commits = []
    for raw in raw_commits:
        c = _normalize_commit(raw)
        c.pop("message", None)  # summary is enough; keeps the cache small
        c["html_url"] = raw.get("html_url") or ""
        commits.append(c)
    return {
        "commits": commits,
        "prs": [_normalize_pr(p) for p in raw_prs],
        "commits_truncated": bool(commits_truncated),
        "prs_truncated": bool(prs_truncated),
    }


def gather(client, repos: list[dict], since_iso: str, log_event=None) -> tuple[dict, dict]:
    """Fetch every repo in parallel. Returns (data, errors), both keyed by
    full_name. A GitHubAuthError cancels the rest and propagates: one dead
    PAT means every repo would fail the same way."""
    log_event = log_event or (lambda *a, **k: None)
    data: dict[str, dict] = {}
    errors: dict[str, str] = {}
    if not repos:
        return data, errors
    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = {pool.submit(fetch_repo, client, r, since_iso): r for r in repos}
        for fut in as_completed(futures):
            r = futures[fut]
            full = r.get("full_name") or f"{r['owner']}/{r['name']}"
            try:
                data[full] = fut.result()
            except gh.GitHubAuthError:
                raise
            except Exception as e:  # noqa: BLE001 - one repo must not sink the page
                detail = f"{type(e).__name__}: {str(e)[:200]}"
                errors[full] = detail
                logger.warning("activity: fetch failed for %s: %s", full, detail)
                log_event("fetch_error", repo=full, error=detail)
    finally:
        # On an auth error, drop queued repos instead of letting them all
        # fail one by one before the page can respond.
        pool.shutdown(wait=True, cancel_futures=True)
    truncated = sorted(k for k, v in data.items() if v["commits_truncated"] or v["prs_truncated"])
    log_event("fetch", since=since_iso, repos=len(repos), ok=len(data), errors=len(errors),
              truncated=truncated, commits=sum(len(v["commits"]) for v in data.values()),
              prs=sum(len(v["prs"]) for v in data.values()),
              elapsed_ms=int((time.monotonic() - t0) * 1000))
    return data, errors


# ----------------------------------------------------------------------
# Report (pure: cached data + range -> what the page and export show)
# ----------------------------------------------------------------------

def _local_date(iso_ts: str) -> datetime.date | None:
    dt = _parse_ts(iso_ts or "")
    if dt is None:
        return None
    return dt.astimezone(_LOCAL_TZ).date() if _LOCAL_TZ else dt.date()


def github_commits_url(repo: dict, rng: DateRange) -> str:
    """GitHub's own commit history page filtered to the range (its since/until
    query params take dates), for the "+N more" link."""
    base = repo.get("html_url") or f"https://github.com/{repo['owner']}/{repo['name']}"
    branch = urllib.parse.quote(repo.get("default_branch") or "main", safe="")
    qs = urllib.parse.urlencode({"since": rng.start_date.isoformat(),
                                 "until": rng.end_date.isoformat()})
    return f"{base}/commits/{branch}?{qs}"


def _repo_row(repo: dict, d: dict | None, error: str | None, rng: DateRange) -> dict:
    d = d or {}
    in_range = [c for c in d.get("commits", []) if rng.contains(c.get("date"))]
    commits = sorted((c for c in in_range if not c.get("is_merge")),
                     key=lambda c: c["date"], reverse=True)
    merge_count = len(in_range) - len(commits)
    prs = sorted((p for p in d.get("prs", []) if rng.contains(p.get("merged_at"))),
                 key=lambda p: p["merged_at"], reverse=True)
    stamps = [c["date"] for c in commits] + [p["merged_at"] for p in prs]
    days = {_local_date(s) for s in stamps} - {None}
    authors = collections.Counter(c.get("author") or "Unknown" for c in commits)
    return {
        "name": repo["name"],
        "owner": repo["owner"],
        "full_name": repo.get("full_name") or f"{repo['owner']}/{repo['name']}",
        "html_url": repo.get("html_url") or "",
        "default_branch": repo.get("default_branch") or "main",
        "code_size_bytes": repo.get("code_size_bytes") or 0,
        "commits": commits,
        "commit_count": len(commits),
        "merge_count": merge_count,
        "prs": prs,
        "pr_count": len(prs),
        "active_days": len(days),
        "active_dates": sorted(days),
        "first_at": min(stamps) if stamps else "",
        "last_at": max(stamps) if stamps else "",
        "authors": authors.most_common(3),
        "truncated": bool(d.get("commits_truncated") or d.get("prs_truncated")),
        "error": error,
        "updated": bool(commits or prs),
        "commits_url": github_commits_url(repo, rng),
    }


def _buckets(rng: DateRange) -> tuple[str, list[dict]]:
    """Timeline buckets sized so the strip stays readable: days up to a month,
    weeks up to ~6 months, then calendar months."""
    start, end = rng.start_date, rng.end_date
    out = []
    if rng.days <= 31:
        unit = "day"
        d = start
        while d <= end:
            out.append({"start": d, "end": d})
            d += datetime.timedelta(days=1)
    elif rng.days <= 182:
        unit = "week"
        d = start
        while d <= end:
            e = min(d + datetime.timedelta(days=6), end)
            out.append({"start": d, "end": e})
            d = e + datetime.timedelta(days=1)
    else:
        unit = "month"
        d = start
        while d <= end:
            nxt = datetime.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
            e = min(nxt - datetime.timedelta(days=1), end)
            out.append({"start": d, "end": e})
            d = nxt
    for b in out:
        b["commits"] = 0
        b["prs"] = 0
        if unit == "month":
            b["label"] = b["start"].strftime("%b %Y")
        else:
            b["label"] = span_label(b["start"], b["end"])
    return unit, out


def _timeline(rows: list[dict], rng: DateRange) -> dict:
    unit, buckets = _buckets(rng)
    starts = [b["start"] for b in buckets]

    def slot(day):
        # Buckets are contiguous and sorted: the last start <= day owns it.
        lo, hi = 0, len(starts) - 1
        if day is None or day < starts[0] or day > rng.end_date:
            return None
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= day:
                lo = mid
            else:
                hi = mid - 1
        return buckets[lo]

    for r in rows:
        for c in r["commits"]:
            b = slot(_local_date(c["date"]))
            if b:
                b["commits"] += 1
        for p in r["prs"]:
            b = slot(_local_date(p["merged_at"]))
            if b:
                b["prs"] += 1
    peak = max((b["commits"] for b in buckets), default=0)
    return {
        "unit": unit,
        "peak": peak,
        "buckets": [{"label": b["label"], "start": b["start"].isoformat(),
                     "end": b["end"].isoformat(), "commits": b["commits"],
                     "prs": b["prs"]} for b in buckets],
    }


def build_report(repos: list[dict], data: dict, errors: dict, rng: DateRange) -> dict:
    """Everything the page + export need for `repos` (already group-scoped)."""
    rows = []
    for repo in repos:
        full = repo.get("full_name") or f"{repo['owner']}/{repo['name']}"
        rows.append(_repo_row(repo, data.get(full), errors.get(full), rng))
    updated = [r for r in rows if r["updated"]]
    # A repo GitHub couldn't read is "unknown", not "idle" — it goes in
    # `failed` only, so the page never claims it had no activity.
    idle = [r for r in rows if not r["updated"] and not r["error"]]
    updated.sort(key=lambda r: (-r["commit_count"], -r["pr_count"], r["name"].lower()))
    idle.sort(key=lambda r: r["name"].lower())
    all_days = set()
    for r in updated:
        all_days.update(r["active_dates"])
    totals = {
        "repos_total": len(rows),
        "repos_updated": len(updated),
        "commits": sum(r["commit_count"] for r in rows),
        "prs_merged": sum(r["pr_count"] for r in rows),
        "active_days": len(all_days),
        "days": rng.days,
        "errors": sum(1 for r in rows if r["error"]),
        "truncated": sum(1 for r in rows if r["truncated"]),
    }
    return {
        "range": rng,
        "rows": rows,
        "updated": updated,
        "idle": idle,
        "failed": [r for r in rows if r["error"]],
        "totals": totals,
        "timeline": _timeline(updated, rng),
    }


# ----------------------------------------------------------------------
# Markdown export (COPY FOR CLAUDE / DOWNLOAD .MD)
# ----------------------------------------------------------------------

def _md_escape(text: str) -> str:
    # Commit subjects are free text; keep them from opening headings/lists.
    return (text or "").replace("\r", " ").replace("\n", " ").strip()


def compose_markdown(report: dict, owner_login: str = "", group: str = "",
                     generated_at: str = "", commit_limit: int = EXPORT_COMMIT_LIMIT) -> str:
    rng: DateRange = report["range"]
    t = report["totals"]
    scope = f"the **{group}** group" if group else "every scanned repo"
    who = f" for {owner_login}" if owner_login else ""
    lines = [
        f"# What changed: {rng.label}",
        "",
        f"> Built by RepoDoctor{who}{(' on ' + generated_at) if generated_at else ''}. "
        f"Covers {scope}: commits that landed on each repo's default branch "
        f"(merge commits excluded) plus pull requests merged in the range. "
        f"Unmerged branch work lives in Next Up → In flight.",
        "",
        f"**{t['repos_updated']} of {t['repos_total']} repos updated** · "
        f"{t['commits']} commit{'s' if t['commits'] != 1 else ''} · "
        f"{t['prs_merged']} PR{'s' if t['prs_merged'] != 1 else ''} merged · "
        f"active on {t['active_days']} of {t['days']} day{'s' if t['days'] != 1 else ''}",
        "",
    ]
    if report["failed"]:
        lines += ["**Incomplete:** GitHub couldn't be read for "
                  + ", ".join(r["name"] for r in report["failed"])
                  + " — totals leave them out.", ""]
    for r in report["updated"]:
        bits = [f"{r['commit_count']} commit{'s' if r['commit_count'] != 1 else ''}"]
        if r["pr_count"]:
            bits.append(f"{r['pr_count']} PR{'s' if r['pr_count'] != 1 else ''} merged")
        last = _local_date(r["last_at"])
        if last:
            bits.append(f"last {_day_label(last)}")
        lines += [f"## {r['name']} — {', '.join(bits)}", ""]
        if r["prs"]:
            lines.append("Merged PRs:")
            for p in r["prs"]:
                d = _local_date(p["merged_at"])
                lines.append(f"- #{p['number']} {_md_escape(p['title'])}"
                             + (f" ({_day_label(d, with_year=False)})" if d else ""))
            lines.append("")
        if r["commits"]:
            lines.append("Commits:")
            for c in r["commits"][:commit_limit]:
                d = _local_date(c["date"])
                when = _day_label(d, with_year=False) if d else "?"
                lines.append(f"- {when} · {_md_escape(c['summary'])} ({c['short']})")
            more = r["commit_count"] - commit_limit
            if more > 0:
                lines.append(f"- …and {more} more: {r['commits_url']}")
            lines.append("")
        if r["truncated"]:
            lines += ["_GitHub returned the maximum this app reads for this repo; "
                      "the oldest commits in the range may be missing._", ""]
    if report["idle"]:
        lines += [f"## Not updated in this range ({len(report['idle'])})", "",
                  ", ".join(r["name"] for r in report["idle"]), ""]
    if not report["updated"] and not report["failed"]:
        lines += ["Nothing landed on any default branch in this range.", ""]
    return "\n".join(lines).rstrip() + "\n"
