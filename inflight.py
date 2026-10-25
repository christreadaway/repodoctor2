"""
In Flight — every piece of coding work that is started but not finished,
across every repo, regardless of when it was started.

Next Up (sessions.py) answers "what did I do in this time window?". This
module answers the other half of context switching: "what have I kicked off
that is still running or waiting on me?" The owner runs projects through
Claude Code on the web, and each session lands in GitHub as a `claude/<slug>`
branch, usually with a pull request. So the full set of unmerged non-default
branches plus open PRs IS the list of work in flight — no transcript access
needed. (A Claude Code session that has not pushed yet is invisible here;
that is said plainly on the page.)

For each branch the screen shows, grounded in git facts:
  - the repo, branch, and a human topic,
  - PR state (open / draft) if one exists,
  - how far ahead/behind the default branch it is, last activity, age,
  - a deterministic status call: READY TO MERGE / FINISH IT / NEEDS PR /
    STALE / MERGED (delete the branch).

Gathering costs one branches call + one open-PRs call per repo that has any
non-default branch (the cached scan tells us which, so a tidy repo costs zero
calls), plus one compare per branch (capped). Results are cached in
data/inflight.json so the page opens instantly; REFRESH regathers. Every
gather is logged to data/logs/inflight.log for paste-back debugging.
"""

from __future__ import annotations

import datetime
import logging

import github_client as gh
from briefing import _parse_ts
from sessions import (
    _normalize_commit, _looks_unfinished, branch_topic, _pr_view,
    _local_time_label, MAX_BRANCH_COMPARES,
)

logger = logging.getLogger(__name__)

# A branch with no PR and no commits for this many days reads as abandoned.
STALE_DAYS = 14
# Commits kept per item (newest-first) for the card + the paste document.
MAX_COMMITS = 8

STATUS_LABELS = {
    "merge": "READY TO MERGE",
    "finish": "FINISH IT",
    "continue": "NEEDS PR",
    "stale": "STALE",
    "complete": "MERGED",
    "unknown": "CHECK ON GITHUB",
}
# Display order: what needs a decision first.
_STATUS_ORDER = {"merge": 0, "finish": 1, "continue": 2, "stale": 3, "unknown": 4, "complete": 5}
KINDS = ("merge", "finish", "continue", "stale", "unknown", "complete")


def _commits_phrase(n: int | None, base: str) -> str:
    n = n or 0
    return f"{n} commit{'s' if n != 1 else ''} {'aren' if n != 1 else 'isn'}'t on {base} yet"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def age_days(iso_ts: str, now: datetime.datetime | None = None) -> int | None:
    dt = _parse_ts(iso_ts)
    if dt is None:
        return None
    now = now or _now()
    return max(0, (now - dt).days)


def classify(item: dict, now: datetime.datetime | None = None) -> tuple[str, str]:
    """(kind, human sentence). Pure function of git facts."""
    base = item.get("base") or "main"
    pr = item.get("pr")
    ahead = item.get("ahead_by")
    behind = item.get("behind_by") or 0
    days = age_days(item.get("last_activity_at", ""), now)
    stale_bit = f" Last activity {days} days ago." if days is not None and days >= STALE_DAYS else ""

    merged_pr = item.get("merged_pr")
    if merged_pr:
        return ("complete",
                f"Merged via PR #{merged_pr['number']} (squash or rebase), and "
                f"nothing new has landed on the branch since. Delete the branch "
                f"to clear it off this list.")
    if ahead is not None and ahead == 0:
        return ("complete",
                f"Everything on this branch is already in {base}. Delete the "
                f"branch to clear it off this list.")
    if item.get("compare_failed"):
        pr_bit = f" PR #{pr['number']} is open." if pr else ""
        return ("unknown",
                f"GitHub couldn't compare this branch with {base}, so its state "
                f"is unknown.{pr_bit} Open it on GitHub to check.")
    if pr:
        n = pr["number"]
        if pr.get("draft"):
            return ("finish",
                    f"PR #{n} is a draft — the work isn't finished. Pick it up "
                    f"in Claude Code, finish, then mark it ready for review.{stale_bit}")
        if behind > 0:
            return ("merge",
                    f"PR #{n} is open but {base} has moved on ({behind} commit"
                    f"{'s' if behind != 1 else ''} behind). Update the branch on "
                    f"GitHub, then review and merge.{stale_bit}")
        return ("merge", f"PR #{n} is open and ready. Review the diff and merge it to {base}.{stale_bit}")
    if _looks_unfinished(item.get("commits", [])):
        return ("finish",
                f"The last commit reads like a work-in-progress and there's no "
                f"PR. Finish the change in Claude Code, then open a PR.{stale_bit}")
    if days is not None and days >= STALE_DAYS:
        return ("stale",
                f"{ahead or 0} commit{'s' if ahead != 1 else ''} not on {base}, "
                f"no PR, and nothing has happened for {days} days. Decide: "
                f"open a PR, pick it back up, or delete the branch.")
    return ("continue",
            f"{_commits_phrase(ahead, base)} and there's no PR. Keep going, or "
            f"open a PR to merge.")


def _build_item(repo: dict, branch: str, pr: dict | None, comp: dict | None,
                now: datetime.datetime | None = None, merged_pr: dict | None = None) -> dict:
    raw_commits = (comp or {}).get("commits") or []
    commits = [_normalize_commit(c) for c in raw_commits]
    commits.sort(key=lambda c: c["date"], reverse=True)
    commits = commits[:MAX_COMMITS]
    dates = [c["date"] for c in commits if c["date"]]
    last_commit = max(dates) if dates else ""
    last_activity = max([last_commit, (pr or {}).get("updated_at", "")]) if (last_commit or pr) else ""
    item = {
        "repo": repo["name"],
        "owner": repo["owner"],
        "html_url": repo.get("html_url", ""),
        "branch": branch,
        "branch_url": f"{repo.get('html_url', '')}/tree/{branch}" if repo.get("html_url") else "",
        "topic": ((pr or {}).get("title") or branch_topic(branch)).strip(),
        "base": (pr or {}).get("base") or repo.get("default_branch", "main"),
        "pr": pr,
        "commits": commits,
        "ahead_by": (comp or {}).get("ahead_by") if comp else None,
        "behind_by": (comp or {}).get("behind_by") if comp else None,
        "last_commit_at": last_commit,
        "last_activity_at": last_activity,
        "is_claude": branch.startswith("claude/"),
        # A None compare (404, unrelated histories, branch deleted mid-read)
        # is "unknown", never reported as "0 commits, keep going".
        "compare_failed": comp is None,
        "merged_pr": merged_pr,
    }
    item["age_days"] = age_days(last_activity, now)
    kind, text = classify(item, now)
    item["kind"] = kind
    item["label"] = STATUS_LABELS[kind]
    item["text"] = text
    return item


def _pr_view_with_draft(pr_raw: dict) -> dict:
    view = _pr_view(pr_raw)
    view["draft"] = bool(pr_raw.get("draft"))
    return view


def gather_repo_inflight(client, repo: dict, now: datetime.datetime | None = None,
                         truncated: dict | None = None) -> list[dict]:
    """All unmerged non-default branches of one repo, each classified."""
    owner, name = repo["owner"], repo["name"]
    default = repo.get("default_branch", "main")

    branches = client.get_branches(owner, name)
    pulls = client.get_pulls(owner, name, state="open")
    pr_by_head = {}
    for pr_raw in pulls:
        head = (pr_raw.get("head") or {}).get("ref")
        if head:
            pr_by_head[head] = _pr_view_with_draft(pr_raw)
    # Squash/rebase merges leave the branch "ahead" forever with a closed PR,
    # so ahead_by == 0 never fires. The newest page of closed PRs tells us
    # which heads were merged; only counts when the branch tip is still the
    # merged PR's head (nothing new pushed since).
    merged_by_head: dict[str, dict] = {}
    for pr_raw in client.get_pulls(owner, name, state="closed", sort="updated",
                                   direction="desc", max_pages=1):
        head = (pr_raw.get("head") or {}).get("ref")
        if head and pr_raw.get("merged_at") and head not in merged_by_head:
            merged_by_head[head] = _pr_view_with_draft(pr_raw)

    # Branches with an open PR first, and never capped: the compare cap must
    # not hide work that is waiting on a merge.
    named = [b for b in branches if b.get("name") and b["name"] != default]
    named.sort(key=lambda b: b["name"] not in pr_by_head)
    out: list[dict] = []
    compared = 0
    for b in named:
        bn = b["name"]
        has_pr = bn in pr_by_head
        if not has_pr and compared >= MAX_BRANCH_COMPARES:
            continue
        if not has_pr:
            compared += 1
        merged = merged_by_head.get(bn)
        tip = (b.get("commit") or {}).get("sha")
        if merged and not has_pr and tip and tip == merged.get("head_sha"):
            out.append(_build_item(repo, bn, None, {"ahead_by": None, "behind_by": None, "commits": []},
                                   now, merged_pr=merged))
            continue
        comp = client.compare_branches(owner, name, default, bn)
        out.append(_build_item(repo, bn, pr_by_head.get(bn), comp, now))
    skipped = len(named) - len(out)
    if skipped:
        logger.warning("inflight: branch-compare cap (%d) reached for %s/%s — %d branch(es) not listed",
                       MAX_BRANCH_COMPARES, owner, name, skipped)
        if truncated is not None:
            truncated[f"{owner}/{name}"] = skipped
    return out


def _scan_shows_tidy(repo: dict) -> bool:
    """The cached scan says this repo has only its default branch."""
    if repo.get("non_default_branch_count", 1) != 0:
        return False
    names = repo.get("branch_names")
    return not names or set(names) <= {repo.get("default_branch", "main")}


def gather_inflight(client, repos: list[dict], now: datetime.datetime | None = None,
                    scan_fresh: bool = True) -> dict:
    """Cross-repo gather. When the scan is fresh, repos whose cached scan shows
    only the default branch are skipped with zero API calls; an old scan can't
    be trusted for that (new branches since), so every repo is read. One
    repo's failure never blanks the run."""
    now = now or _now()
    items: list[dict] = []
    skipped = 0
    failed: list[str] = []
    truncated: dict[str, int] = {}
    for repo in repos:
        if scan_fresh and _scan_shows_tidy(repo):
            skipped += 1
            continue
        try:
            items.extend(gather_repo_inflight(client, repo, now, truncated))
        except gh.GitHubAuthError:
            raise
        except Exception as e:  # noqa: BLE001 - never let one repo blank the page
            failed.append(f"{repo.get('owner')}/{repo.get('name')}")
            logger.warning("inflight gather failed for %s/%s: %s",
                           repo.get("owner"), repo.get("name"), e)
    items.sort(key=lambda i: (_STATUS_ORDER.get(i["kind"], 9), -(_parse_ts(i["last_activity_at"]) or _EPOCH).timestamp()))
    return {
        "items": items,
        "gathered_at": now.isoformat(),
        "repos_checked": len(repos) - skipped,
        "repos_skipped": skipped,
        "failed_repos": failed,
        "truncated_repos": truncated,
    }


_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def summarize_counts(items: list[dict]) -> dict:
    by_kind: dict[str, int] = {}
    for i in items:
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
    return {
        # "total", not "items": in Jinja, counts.items resolves to dict.items.
        "total": len(items),
        "repos": len({i["repo"] for i in items}),
        "merge": by_kind.get("merge", 0),
        "finish": by_kind.get("finish", 0),
        "continue": by_kind.get("continue", 0),
        "stale": by_kind.get("stale", 0),
        "unknown": by_kind.get("unknown", 0),
        "complete": by_kind.get("complete", 0),
    }


def filter_items(items: list[dict], kind: str | None = None, repos: set[str] | None = None) -> list[dict]:
    """repos holds "owner/name" keys so a same-named org and personal repo
    don't merge; bare names (older callers) still match by name."""
    out = items
    if kind:
        out = [i for i in out if i["kind"] == kind]
    if repos is not None:
        out = [i for i in out
               if f"{i.get('owner')}/{i.get('repo')}" in repos or i.get("repo") in repos]
    return out


def compose_markdown(items: list[dict], owner_login: str, gathered_at: str) -> str:
    counts = summarize_counts(items)
    when = _local_time_label(gathered_at) or gathered_at[:10]
    lines = [
        "# Work in flight",
        "",
        f"> Every unmerged branch across my repos, read from GitHub by RepoDoctor on {when}.",
        f"> {counts['total']} branch(es) across {counts['repos']} repo(s): "
        f"{counts['merge']} ready to merge, {counts['finish']} to finish, "
        f"{counts['continue']} need a PR, {counts['stale']} stale, "
        f"{counts['unknown']} couldn't be checked, {counts['complete']} already merged.",
        "",
    ]
    if owner_login:
        lines += [f"Owner: github.com/{owner_login}", ""]
    if not items:
        lines += ["Nothing in flight — every branch is merged.", ""]
        return "\n".join(lines)

    for kind in KINDS:
        group = [i for i in items if i["kind"] == kind]
        if not group:
            continue
        lines += [f"## {STATUS_LABELS[kind]} ({len(group)})", ""]
        for i in group:
            pr = i.get("pr")
            pr_bit = f" · PR #{pr['number']}{' (draft)' if pr.get('draft') else ''}" if pr else ""
            age = f" · {i['age_days']}d old" if i.get("age_days") is not None else ""
            ahead = f" · +{i['ahead_by']}/-{i['behind_by']}" if i.get("ahead_by") is not None else ""
            lines.append(f"### {i['repo']}: {i['topic']}")
            lines.append(f"_branch `{i['branch']}`{pr_bit}{ahead}{age}_")
            lines.append("")
            lines.append(f"**Next:** {i['text']}")
            if i.get("commits"):
                lines.append("")
                lines.append("**Recent commits:**")
                for c in i["commits"][:5]:
                    stamp = _local_time_label(c["date"])
                    lines.append(f"- {c['summary']}{f' ({stamp})' if stamp else ''}")
            lines.append("")
    return "\n".join(lines)
