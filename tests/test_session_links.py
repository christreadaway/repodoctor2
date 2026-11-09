"""
OPEN SESSION links back into Claude Code sessions.

Claude Code commits carry a `Claude-Session: https://claude.ai/code/session_<id>`
trailer (PR bodies often end with the bare URL). Every In Flight item and every
Recent-sessions card gets a `session_url` (str, "" when none) that the pages,
the Markdown exports and the Resume packet turn into a one-click link.

Covers: the strict parser (trailer, bare URL, punctuation, hostile lookalikes),
newest-commit-wins selection before MAX_COMMITS truncation, the PR-body
fallback, In Flight / Recent sessions / Resume carrying the key, cached items
without the key, the Markdown exports, the gather-log counts, and XSS safety
of the rendered href.
"""

import datetime
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inflight
import models
import resume
import sessions

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
CANON = re.compile(r"^https://claude\.ai/code/session_[A-Za-z0-9_-]+$")

S_OLD = "https://claude.ai/code/session_01OldOldOldOldOldOldOld01"
S_MID = "https://claude.ai/code/session_01MidMidMidMidMidMidMid01"
S_NEW = "https://claude.ai/code/session_01NewNewNewNewNewNewNew01"
S_PR = "https://claude.ai/code/session_01PrBodyPrBodyPrBodyPr01"


def _trailer(url: str) -> str:
    return f"\n\nCo-Authored-By: Claude <noreply@anthropic.com>\nClaude-Session: {url}"


def _commit(sha, msg, date, parents=1):
    return {"sha": sha, "commit": {"message": msg, "author": {"name": "Chris", "date": date},
                                   "committer": {"name": "Chris", "date": date}},
            "parents": [{"sha": f"p{i}"} for i in range(parents)]}


def _repo(**over):
    base = {"owner": "chris", "name": "demo", "full_name": "chris/demo", "default_branch": "main",
            "html_url": "https://github.com/chris/demo", "pushed_at": "2026-10-24T20:00:00Z",
            "total_branch_count": 2, "non_default_branch_count": 1,
            "branch_names": ["main", "claude/add-foo-abc123"],
            "required_files": {"CLAUDE.md": True}, "languages": {"Python": 100}, "code_size_bytes": 2048}
    base.update(over)
    return base


def _pr(number, head, body="", state="open", merged_at=None, head_sha="h",
        updated="2026-10-24T00:00:00Z", created="2026-10-20T00:00:00Z"):
    return {"number": number, "title": f"PR {number} title", "state": state, "draft": False,
            "body": body, "merged_at": merged_at, "closed_at": merged_at,
            "created_at": created, "updated_at": updated,
            "html_url": f"https://github.com/chris/demo/pull/{number}",
            "base": {"ref": "main"}, "head": {"ref": head, "sha": head_sha}}


class FakeGitHub:
    """Every GitHub call In Flight, Recent sessions and Resume make, for one repo."""

    def __init__(self, branches=(), open_pulls=(), closed_pulls=(), compares=None,
                 pull_commits=None, default_commits=()):
        self.branches = list(branches)
        self.open_pulls = list(open_pulls)
        self.closed_pulls = list(closed_pulls)
        self.compares = compares or {}
        self.pull_commits = pull_commits or {}
        self.default_commits = list(default_commits)

    def get_branches(self, owner, name):
        return [b if isinstance(b, dict) else {"name": b, "commit": {"sha": f"tip-{b}"}}
                for b in self.branches]

    def get_pulls(self, owner, name, state="open", **kw):
        if state == "open":
            return list(self.open_pulls)
        if state == "closed":
            return list(self.closed_pulls)
        allp = self.open_pulls + self.closed_pulls
        return sorted(allp, key=lambda p: p.get("updated_at") or "", reverse=True)

    def compare_branches(self, owner, name, base, head):
        return self.compares.get(head)

    def get_pull_commits(self, owner, name, number, max_pages=3):
        return list(self.pull_commits.get(number, []))

    def get_commits_since(self, owner, name, since_iso, ref=None, max_pages=3, until_iso=None):
        return list(self.default_commits)


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------

class TestClaudeSessionUrl(unittest.TestCase):

    def test_trailer(self):
        msg = "Add the foo feature\n\nLonger body." + _trailer(S_NEW)
        self.assertEqual(sessions.claude_session_url(msg), S_NEW)

    def test_trailer_without_space_and_lowercase_label(self):
        self.assertEqual(sessions.claude_session_url(f"x\n\nClaude-Session:{S_NEW}"), S_NEW)
        self.assertEqual(sessions.claude_session_url(f"x\n\n  claude-session:   {S_NEW}  \r\n"), S_NEW)

    def test_bare_url_on_its_own_line(self):
        body = f"## Summary\n- did things\n\n{S_PR}\n"
        self.assertEqual(sessions.claude_session_url(body), S_PR)
        self.assertEqual(sessions.claude_session_url(S_PR), S_PR)

    def test_trailer_beats_bare_url_cited_in_body(self):
        msg = f"Continue the work from {S_OLD}\n\nClaude-Session: {S_NEW}"
        self.assertEqual(sessions.claude_session_url(msg), S_NEW)
        # ... even when the bare citation comes after the trailer line.
        msg2 = f"Claude-Session: {S_NEW}\nsee also {S_OLD}"
        self.assertEqual(sessions.claude_session_url(msg2), S_NEW)

    def test_last_of_several_bare_urls_wins(self):
        self.assertEqual(sessions.claude_session_url(f"{S_OLD} then {S_MID}"), S_MID)

    def test_trailing_punctuation_and_wrappers_stripped(self):
        for text in (f"see {S_NEW}.", f"see {S_NEW},", f"({S_NEW})", f"[open]({S_NEW})",
                     f"<{S_NEW}>", f"`{S_NEW}`", f"**{S_NEW}**", f'"{S_NEW}"', f"'{S_NEW}'",
                     f"| {S_NEW} |", f"{S_NEW}!", f"{S_NEW}?", f"{S_NEW});", f"{S_NEW} next"):
            self.assertEqual(sessions.claude_session_url(text), S_NEW, text)

    def test_hostile_and_lookalike_inputs_rejected(self):
        hostile = [
            "https://claude.ai.evil.com/code/session_abc",
            "https://claude.ai@evil.com/code/session_abc",
            "https://evilclaude.ai/code/session_abc",
            "https://claude.ai:8443/code/session_abc",
            "http://claude.ai/code/session_abc",
            "HTTPS://CLAUDE.AI/code/session_abc",
            "//claude.ai/code/session_abc",
            f"javascript:{S_NEW}",
            f"javascript:alert(1)//{S_NEW}",
            f"data:text/html,{S_NEW}",
            f"https://evil.example/?u={S_NEW}",
            f"https://evil.example/{S_NEW}",
            f"https://evil.example#{S_NEW}",
            f"x{S_NEW}",
            f'{S_NEW}"onmouseover=alert(1)',
            f"{S_NEW}'onclick='x'",
            f"{S_NEW}.evil.com",
            f"{S_NEW}/../../logout",
            f"{S_NEW}?next=https://evil.example",
            f"{S_NEW}#frag",
            f"{S_NEW}<script>alert(1)</script>",
            f"{S_NEW}​",
            "https://claude.ai/code/session_",
            "https://claude.ai/code/session_" + "a" * 129,
            "https://claude.ai/code/sessions_abc",
            "https://claude.ai/code/session_ab%22cd",
            "",
        ]
        for text in hostile:
            self.assertEqual(sessions.claude_session_url(text), "", text)

    def test_non_string_input(self):
        for value in (None, 123, b"https://claude.ai/code/session_abc", ["x"], {"a": 1}):
            self.assertEqual(sessions.claude_session_url(value), "")

    def test_result_is_always_canonical(self):
        # Fuzz-ish: every output over a pile of mixed inputs is either "" or
        # exactly the canonical shape (no quotes, spaces, '<', other hosts).
        pieces = [S_NEW, '"', "'", "<", ">", " ", "\n", ".", "/", "?", "#", ":", "javascript:",
                  "https://evil.example/", "Claude-Session: ", "(", ")", "`", "*", "\x00", "%22"]
        import itertools
        for a, b, c in itertools.product(pieces, repeat=3):
            out = sessions.claude_session_url(a + b + c)
            self.assertTrue(out == "" or CANON.match(out), repr(a + b + c))

    def test_real_trailer_shape_from_this_repo(self):
        msg = ("Stats ranges: fix all 9 independent-review findings\n\n"
               "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
               "Claude-Session: https://claude.ai/code/session_01Mko4TBU32FYhCyL3w1dYFV")
        self.assertEqual(sessions.claude_session_url(msg),
                         "https://claude.ai/code/session_01Mko4TBU32FYhCyL3w1dYFV")

    def test_safe_session_url(self):
        self.assertEqual(sessions.safe_session_url(S_NEW), S_NEW)
        for bad in (None, "", "javascript:alert(1)", S_NEW + '"', " " + S_NEW,
                    "https://claude.ai.evil.com/code/session_x", 5):
            self.assertEqual(sessions.safe_session_url(bad), "")


class TestSessionLink(unittest.TestCase):

    def test_newest_commit_with_link_wins(self):
        # GitHub lists compare / PR commits oldest-first.
        raw = [_commit("a", "one" + _trailer(S_OLD), "2026-10-20T00:00:00Z"),
               _commit("b", "two" + _trailer(S_NEW), "2026-10-22T00:00:00Z"),
               _commit("c", "three, no trailer", "2026-10-23T00:00:00Z")]
        self.assertEqual(sessions.session_link(raw), {"session_url": S_NEW, "session_count": 2})
        # Order of the input list doesn't matter, the dates do.
        self.assertEqual(sessions.session_link(list(reversed(raw)))["session_url"], S_NEW)

    def test_equal_timestamps_later_position_is_newer(self):
        raw = [_commit("a", "x" + _trailer(S_OLD), "2026-10-20T00:00:00Z"),
               _commit("b", "y" + _trailer(S_NEW), "2026-10-20T00:00:00Z")]
        self.assertEqual(sessions.session_link(raw)["session_url"], S_NEW)

    def test_normalized_commits_work_too(self):
        norm = [sessions._normalize_commit(_commit("a", "x" + _trailer(S_MID), "2026-10-20T00:00:00Z"))]
        self.assertEqual(sessions.session_link(norm)["session_url"], S_MID)

    def test_pr_body_only_when_no_commit_link(self):
        raw = [_commit("a", "hand-made commit", "2026-10-20T00:00:00Z")]
        self.assertEqual(sessions.session_link(raw, f"Body\n\n{S_PR}"),
                         {"session_url": S_PR, "session_count": 1})
        raw.append(_commit("b", "x" + _trailer(S_NEW), "2026-10-21T00:00:00Z"))
        self.assertEqual(sessions.session_link(raw, f"Body\n\n{S_PR}")["session_url"], S_NEW)

    def test_none(self):
        self.assertEqual(sessions.session_link([]), {"session_url": "", "session_count": 0})
        self.assertEqual(sessions.session_link(None, None), {"session_url": "", "session_count": 0})
        self.assertEqual(sessions.session_link([_commit("a", "nothing", "")], "no link here"),
                         {"session_url": "", "session_count": 0})

    def test_count_session_links_ignores_missing_and_bad(self):
        items = [{"session_url": S_NEW}, {"session_url": ""}, {}, {"session_url": "javascript:x"}]
        self.assertEqual(sessions.count_session_links(items), 1)


# ----------------------------------------------------------------------
# In Flight
# ----------------------------------------------------------------------

class TestInflightLinks(unittest.TestCase):

    def test_link_only_in_old_commit_beyond_max_commits(self):
        n = inflight.MAX_COMMITS + 4
        raw = [_commit("c00", "start" + _trailer(S_OLD), "2026-10-01T00:00:00Z")]
        raw += [_commit(f"c{k:02d}", f"step {k}", f"2026-10-{k + 1:02d}T00:00:00Z") for k in range(1, n)]
        client = FakeGitHub(["main", "claude/long-abc123"],
                            compares={"claude/long-abc123": {"ahead_by": n, "behind_by": 0, "commits": raw}})
        item = inflight.gather_repo_inflight(client, _repo(), NOW)[0]
        self.assertEqual(len(item["commits"]), inflight.MAX_COMMITS)
        self.assertNotIn("c00", [c["sha"] for c in item["commits"]])  # truncated away...
        self.assertEqual(item["session_url"], S_OLD)  # ...but its link still wins
        self.assertEqual(item["session_count"], 1)

    def test_branch_spanning_sessions_newest_wins(self):
        raw = [_commit("a", "x" + _trailer(S_OLD), "2026-10-20T00:00:00Z"),
               _commit("b", "y" + _trailer(S_NEW), "2026-10-23T00:00:00Z")]
        client = FakeGitHub(["main", "claude/x-abc123"],
                            compares={"claude/x-abc123": {"ahead_by": 2, "behind_by": 0, "commits": raw}})
        item = inflight.gather_repo_inflight(client, _repo(), NOW)[0]
        self.assertEqual((item["session_url"], item["session_count"]), (S_NEW, 2))

    def test_open_pr_body_fallback(self):
        client = FakeGitHub(["main", "claude/x-abc123"],
                            open_pulls=[_pr(5, "claude/x-abc123", body=f"## Summary\nstuff\n\n{S_PR}\n")],
                            compares={"claude/x-abc123": {"ahead_by": 1, "behind_by": 0, "commits": [
                                _commit("a", "hand-made", "2026-10-24T00:00:00Z")]}})
        item = inflight.gather_repo_inflight(client, _repo(), NOW)[0]
        self.assertEqual(item["session_url"], S_PR)
        self.assertNotIn("body", item["pr"])  # raw body never lands in the cache

    def test_squash_merged_branch_uses_merged_pr_body(self):
        client = FakeGitHub([{"name": "main"}, {"name": "claude/sq", "commit": {"sha": "h"}}],
                            closed_pulls=[_pr(12, "claude/sq", body=S_PR, state="closed",
                                              merged_at="2026-10-20T00:00:00Z")])
        item = inflight.gather_repo_inflight(client, _repo(), NOW)[0]
        self.assertEqual(item["kind"], "complete")
        self.assertEqual(item["session_url"], S_PR)

    def test_no_link_key_still_present(self):
        client = FakeGitHub(["main", "plain"], compares={"plain": {"ahead_by": 1, "behind_by": 0, "commits": [
            _commit("a", "no trailer", "2026-10-24T00:00:00Z")]}})
        item = inflight.gather_repo_inflight(client, _repo(), NOW)[0]
        self.assertEqual(item["session_url"], "")
        self.assertEqual(item["session_count"], 0)
        # A failed compare (no commits at all) still carries the key.
        client.compares = {}
        self.assertEqual(inflight.gather_repo_inflight(client, _repo(), NOW)[0]["session_url"], "")

    def test_hostile_commit_message_gives_no_link(self):
        evil = f'Fix\n\nClaude-Session: {S_NEW}"><script>alert(1)</script>'
        client = FakeGitHub(["main", "x"], compares={"x": {"ahead_by": 1, "behind_by": 0, "commits": [
            _commit("a", evil, "2026-10-24T00:00:00Z"),
            _commit("b", "Claude-Session: https://claude.ai.evil.com/code/session_x", "2026-10-24T01:00:00Z")]}})
        self.assertEqual(inflight.gather_repo_inflight(client, _repo(), NOW)[0]["session_url"], "")

    def test_counts_and_markdown_tolerate_cached_items_without_key(self):
        fresh = {"kind": "merge", "repo": "a", "owner": "c", "branch": "claude/a", "topic": "t1", "text": "do",
                 "label": "READY TO MERGE", "pr": {"number": 1, "draft": False}, "ahead_by": 2, "behind_by": 0,
                 "age_days": 1, "commits": [], "last_activity_at": "", "session_url": S_NEW, "session_count": 1}
        legacy = {"kind": "stale", "repo": "b", "owner": "c", "branch": "old", "topic": "t2", "text": "decide",
                  "label": "STALE", "pr": None, "ahead_by": 1, "behind_by": 9, "age_days": 40,
                  "commits": [], "last_activity_at": ""}  # cached before session links existed
        tampered = dict(legacy, branch="evil", session_url="javascript:alert(1)")
        counts = inflight.summarize_counts([fresh, legacy, tampered])
        self.assertEqual(counts["session_links"], 1)
        md = inflight.compose_markdown([fresh, legacy, tampered], "chris", NOW.isoformat())
        self.assertIn(f"**Claude Code session:** {S_NEW}", md)
        self.assertEqual(md.count("Claude Code session:"), 1)
        self.assertNotIn("javascript:", md)

    def test_gather_inflight_items_carry_key(self):
        client = FakeGitHub(["main", "claude/x-abc123"],
                            compares={"claude/x-abc123": {"ahead_by": 1, "behind_by": 0, "commits": [
                                _commit("a", "x" + _trailer(S_NEW), "2026-10-24T00:00:00Z")]}})
        data = inflight.gather_inflight(client, [_repo()], NOW)
        self.assertEqual(data["items"][0]["session_url"], S_NEW)
        self.assertEqual(inflight.summarize_counts(data["items"])["session_links"], 1)


# ----------------------------------------------------------------------
# Recent sessions
# ----------------------------------------------------------------------

def _window(since, until, key="test"):
    return sessions.Window(datetime.datetime.fromisoformat(since), datetime.datetime.fromisoformat(until),
                           "test window", key, "date")


W = _window("2026-07-28T00:00:00+00:00", "2026-07-29T00:00:00+00:00")


class TestSessionsLinks(unittest.TestCase):

    def test_pr_session_link_beyond_max_commits(self):
        n = sessions.MAX_COMMITS + 5
        raw = [_commit("c00", "start" + _trailer(S_OLD), "2026-07-28T00:10:00Z")]
        raw += [_commit(f"c{k:02d}", f"step {k}", f"2026-07-28T{k // 4 + 1:02d}:{(k % 4) * 10:02d}:00Z")
                for k in range(1, n)]
        client = FakeGitHub(["main", "claude/big-abc123"],
                            open_pulls=[_pr(7, "claude/big-abc123", updated="2026-07-28T20:00:00Z")],
                            compares={"claude/big-abc123": {"ahead_by": n, "behind_by": 0, "commits": raw}},
                            pull_commits={7: raw})
        s = sessions.gather_sessions(client, [_repo()], W)[0]
        self.assertEqual(s["commit_count"], sessions.MAX_COMMITS)
        self.assertNotIn("c00", [c["sha"] for c in s["commits"]])
        self.assertEqual(s["session_url"], S_OLD)

    def test_card_prefers_in_window_session_over_later_one(self):
        # The branch was picked up again in a later session; yesterday's card
        # still opens yesterday's session.
        raw = [_commit("a", "x" + _trailer(S_OLD), "2026-07-28T10:00:00Z"),
               _commit("b", "y" + _trailer(S_NEW), "2026-07-30T10:00:00Z")]
        client = FakeGitHub(["main", "claude/x-abc123"],
                            compares={"claude/x-abc123": {"ahead_by": 2, "behind_by": 0, "commits": raw}})
        s = sessions.gather_sessions(client, [_repo()], W)[0]
        self.assertEqual(s["session_url"], S_OLD)
        self.assertEqual(s["session_count"], 1)

    def test_falls_back_to_branch_commits_then_pr_body(self):
        # A merge-only card (no commits in the window): branch commits first...
        pr = _pr(9, "claude/m-abc123", body=S_PR, state="closed", merged_at="2026-07-28T15:00:00Z",
                 updated="2026-07-28T15:00:00Z")
        old = [_commit("a", "x" + _trailer(S_MID), "2026-07-20T10:00:00Z")]
        client = FakeGitHub(["main"], closed_pulls=[pr], pull_commits={9: old})
        s = sessions.gather_sessions(client, [_repo()], W)[0]
        self.assertEqual(s["commit_count"], 0)
        self.assertEqual(s["session_url"], S_MID)
        # ...then the PR body when no commit has one.
        client.pull_commits = {9: [_commit("a", "hand-made", "2026-07-20T10:00:00Z")]}
        self.assertEqual(sessions.gather_sessions(client, [_repo()], W)[0]["session_url"], S_PR)

    def test_default_branch_card_uses_only_its_own_commits(self):
        pr = _pr(10, "claude/foo-abc123", state="closed", merged_at="2026-07-28T15:00:00Z",
                 updated="2026-07-28T15:00:00Z")
        pr_commits = [_commit("f1", "foo" + _trailer(S_NEW), "2026-07-28T14:00:00Z")]
        client = FakeGitHub(["main"], closed_pulls=[pr], pull_commits={10: pr_commits},
                            default_commits=[_commit("d1", "hotfix by hand", "2026-07-28T16:00:00Z"),
                                             pr_commits[0]])
        by_branch = {s["branch"]: s for s in sessions.gather_sessions(client, [_repo()], W)}
        self.assertEqual(by_branch["claude/foo-abc123"]["session_url"], S_NEW)
        self.assertEqual(by_branch["main"]["session_url"], "")  # PR's session doesn't leak onto main
        self.assertEqual([c["sha"] for c in by_branch["main"]["commits"]], ["d1"])

    def test_direct_commits_survive_many_covered_ones(self):
        # Regression: the default-branch card capped at MAX_COMMITS BEFORE
        # dropping PR-covered commits, so a day of PR work on main pushed the
        # real direct pushes (and their session link) off the card.
        n = sessions.MAX_COMMITS + 1
        pr_commits = [_commit(f"p{k:02d}", f"pr step {k}", f"2026-07-28T{12 + k // 6:02d}:{(k % 6) * 10:02d}:00Z")
                      for k in range(n)]
        pr = _pr(11, "claude/big-abc123", state="closed", merged_at="2026-07-28T22:00:00Z",
                 updated="2026-07-28T22:00:00Z")
        direct = [_commit(f"d{k}", "direct" + _trailer(S_MID), f"2026-07-28T0{k + 1}:00:00Z") for k in range(3)]
        client = FakeGitHub(["main"], closed_pulls=[pr], pull_commits={11: pr_commits},
                            default_commits=pr_commits + direct)
        main = [s for s in sessions.gather_sessions(client, [_repo()], W) if s["is_default"]]
        self.assertEqual(len(main), 1)
        self.assertEqual(sorted(c["sha"] for c in main[0]["commits"]), ["d0", "d1", "d2"])
        self.assertEqual(main[0]["session_url"], S_MID)

    def test_pr_commits_past_the_cap_never_reappear_as_direct(self):
        # Merge-commit landing: main's history holds every PR commit. All of
        # them (not just the MAX_COMMITS shown) count as covered.
        n = sessions.MAX_COMMITS + 6
        pr_commits = [_commit(f"p{k:02d}", f"pr step {k}", f"2026-07-28T{1 + k // 6:02d}:{(k % 6) * 10:02d}:00Z")
                      for k in range(n)]
        pr = _pr(12, "claude/big-abc123", state="closed", merged_at="2026-07-28T22:00:00Z",
                 updated="2026-07-28T22:00:00Z")
        merge = _commit("mg", "Merge pull request #12", "2026-07-28T22:00:00Z", parents=2)
        client = FakeGitHub(["main"], closed_pulls=[pr], pull_commits={12: pr_commits},
                            default_commits=[merge] + pr_commits)
        out = sessions.gather_sessions(client, [_repo()], W)
        self.assertEqual([s["branch"] for s in out], ["claude/big-abc123"])

    def test_every_session_dict_has_the_key(self):
        client = FakeGitHub(["main", "claude/x-abc123"],
                            compares={"claude/x-abc123": {"ahead_by": 1, "behind_by": 0, "commits": [
                                _commit("a", "nothing", "2026-07-28T10:00:00Z")]}},
                            default_commits=[_commit("d", "direct", "2026-07-28T11:00:00Z")])
        out = sessions.gather_sessions(client, [_repo()], W)
        self.assertEqual(len(out), 2)
        for s in out:
            self.assertEqual(s["session_url"], "")
            self.assertEqual(s["session_count"], 0)

    def test_gather_one_session_carries_link(self):
        client = FakeGitHub(["main", "claude/x-abc123"],
                            compares={"claude/x-abc123": {"ahead_by": 1, "behind_by": 0, "commits": [
                                _commit("a", "x" + _trailer(S_NEW), "2026-07-28T10:00:00Z")]}})
        s = sessions.gather_one_session(client, _repo(), "claude/x-abc123", W)
        self.assertEqual(s["session_url"], S_NEW)

    def test_markdown_and_counts(self):
        base = {"repo": "demo", "owner": "chris", "branch": "claude/x", "topic": "t", "pr": None,
                "commit_count": 1, "commits": [], "next_kind": "continue", "next_text": "Keep going.",
                "recap": None}
        with_link = dict(base, session_url=S_NEW)
        legacy = dict(base, branch="claude/y")  # no key at all
        tampered = dict(base, branch="claude/z", session_url=f'{S_NEW}" onclick="x')
        md = sessions.compose_markdown([with_link, legacy, tampered], "chris", "yesterday")
        self.assertIn(f"**Claude Code session:** {S_NEW}", md)
        self.assertEqual(md.count("Claude Code session:"), 1)
        self.assertNotIn("onclick", md)
        self.assertEqual(sessions.summarize_counts([with_link, legacy, tampered])["session_links"], 1)


# ----------------------------------------------------------------------
# Resume
# ----------------------------------------------------------------------

class TestResumeLinks(unittest.TestCase):

    def _packet(self, inflight_items=(), recent=()):
        return {"repo": {"name": "demo"}, "inflight": list(inflight_items),
                "recent_sessions": list(recent), "tracker_actions": [], "planned_steps": []}

    def test_primary_session_priority(self):
        finish = {"kind": "finish", "branch": "claude/f", "topic": "finish me", "base": "main",
                  "session_url": S_OLD}
        merge = {"kind": "merge", "branch": "claude/m", "topic": "merge me", "pr": {"number": 3},
                 "session_url": S_MID}
        recent = {"branch": "claude/r", "topic": "recent", "session_url": S_NEW}
        stale = {"kind": "stale", "branch": "claude/s", "topic": "stale", "session_url": S_PR}
        p = resume.primary_session(self._packet([stale, merge, finish], [recent]))
        self.assertEqual(p, {"session_url": S_OLD, "session_branch": "claude/f", "session_topic": "finish me"})
        self.assertEqual(resume.primary_session(self._packet([stale, merge], [recent]))["session_url"], S_MID)
        self.assertEqual(resume.primary_session(self._packet([stale], [recent]))["session_url"], S_NEW)
        self.assertEqual(resume.primary_session(self._packet([stale]))["session_url"], S_PR)

    def test_primary_session_skips_items_without_a_link(self):
        finish = {"kind": "finish", "branch": "claude/f", "topic": "t"}  # cached / no link
        bad = {"kind": "continue", "branch": "claude/b", "topic": "t", "session_url": "javascript:alert(1)"}
        merge = {"kind": "merge", "branch": "claude/m", "topic": "t", "session_url": S_MID}
        self.assertEqual(resume.primary_session(self._packet([finish, bad, merge]))["session_branch"], "claude/m")
        self.assertEqual(resume.primary_session(self._packet()),
                         {"session_url": "", "session_branch": "", "session_topic": ""})

    def test_first_prompt_mentions_session(self):
        item = {"kind": "continue", "branch": "claude/x-abc123", "topic": "x", "base": "main",
                "session_url": S_NEW}
        prompt = resume.suggest_first_prompt(self._packet([item]))
        self.assertIn("Pick up the `claude/x-abc123` branch", prompt)
        self.assertIn(f"Claude Code session {S_NEW}", prompt)
        no_link = dict(item, session_url="")
        self.assertNotIn("claude.ai", resume.suggest_first_prompt(self._packet([no_link])))
        legacy = {k: v for k, v in item.items() if k != "session_url"}
        self.assertNotIn("claude.ai", resume.suggest_first_prompt(self._packet([legacy])))

    def test_build_packet_and_markdown(self):
        client = FakeGitHub(
            ["main", "claude/add-foo-abc123"],
            compares={"claude/add-foo-abc123": {"ahead_by": 1, "behind_by": 0, "commits": [
                _commit("a", "Add foo" + _trailer(S_NEW), "2026-10-24T01:00:00Z")]}})
        fetched = {"docs": {"project_status": "# Status\n\nok"}, "readme": ""}
        with patch.object(resume.gh, "fetch_repo_docs", return_value=fetched), \
             patch.object(resume.sessions, "gather_sessions", return_value=[
                 {"branch": "claude/r", "topic": "recent", "pr": None, "commit_count": 1, "commits": [],
                  "next_label": "DONE", "session_url": S_MID}]), \
             patch.object(inflight, "_now", return_value=NOW):
            packet = resume.build_packet(client, _repo(), summary=None, brief=None, blurb=None,
                                         tracker=None, groups={}, categories={})
        self.assertEqual(packet["inflight"][0]["session_url"], S_NEW)
        self.assertEqual(packet["session_url"], S_NEW)
        self.assertEqual(packet["session_branch"], "claude/add-foo-abc123")
        self.assertIn(S_NEW, packet["first_prompt"])
        md = resume.compose_markdown(packet, "chris")
        self.assertIn(f"Claude Code session to reopen: {S_NEW} (branch `claude/add-foo-abc123`)", md)
        self.assertIn(f"Session: {S_NEW}", md)          # Work in flight line
        self.assertIn(f"session: {S_MID}", md)          # Recent sessions line

    def test_markdown_without_any_link(self):
        client = FakeGitHub(["main"])
        with patch.object(resume.gh, "fetch_repo_docs", return_value={"docs": {}, "readme": ""}), \
             patch.object(resume.sessions, "gather_sessions", return_value=[]):
            packet = resume.build_packet(client, _repo(), summary=None, brief=None, blurb=None,
                                         tracker=None, groups={}, categories={})
        self.assertEqual(packet["session_url"], "")
        self.assertNotIn("claude.ai/code", resume.compose_markdown(packet))


# ----------------------------------------------------------------------
# Pages (Flask test client + fake GitHub), logs, XSS
# ----------------------------------------------------------------------

_HREF_RE = re.compile(r'<a href="([^"]*)"[^>]*class="session-open[^"]*"')


class TestPages(unittest.TestCase):

    def setUp(self):
        import app as app_module
        self.m = app_module
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self._orig = (app_module._scan_results, app_module._github_client, app_module._credentials)
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "logs"))
        self._paths = (models.INFLIGHT_PATH, models.INFLIGHT_LOG_PATH, models.RESUME_HISTORY_PATH,
                       models.RESUME_LOG_PATH, models.SESSIONS_LOG_PATH, models.SESSION_RECAPS_PATH)
        models.INFLIGHT_PATH = os.path.join(self.tmp, "inflight.json")
        models.INFLIGHT_LOG_PATH = os.path.join(self.tmp, "logs", "inflight.log")
        models.RESUME_HISTORY_PATH = os.path.join(self.tmp, "resume.json")
        models.RESUME_LOG_PATH = os.path.join(self.tmp, "logs", "resume.log")
        models.SESSIONS_LOG_PATH = os.path.join(self.tmp, "logs", "sessions.log")
        models.SESSION_RECAPS_PATH = os.path.join(self.tmp, "recaps.json")
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "chris"
        self.m._scan_results = {"repos": [_repo()],
                                "scanned_at": datetime.datetime.now(UTC).isoformat()}

    def tearDown(self):
        (self.m._scan_results, self.m._github_client, self.m._credentials) = self._orig
        (models.INFLIGHT_PATH, models.INFLIGHT_LOG_PATH, models.RESUME_HISTORY_PATH,
         models.RESUME_LOG_PATH, models.SESSIONS_LOG_PATH, models.SESSION_RECAPS_PATH) = self._paths

    def _log(self, path):
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _hrefs(self, html):
        return _HREF_RE.findall(html)

    # --- In Flight -----------------------------------------------------------

    def test_inflight_card_link_export_and_log(self):
        self.m._github_client = FakeGitHub(
            ["main", "claude/add-foo-abc123", "plain"],
            open_pulls=[_pr(5, "claude/add-foo-abc123")],
            compares={"claude/add-foo-abc123": {"ahead_by": 1, "behind_by": 0, "commits": [
                          _commit("a", "Add foo" + _trailer(S_NEW), "2026-10-24T01:00:00Z")]},
                      "plain": {"ahead_by": 1, "behind_by": 0, "commits": [
                          _commit("b", "manual", "2026-10-24T01:00:00Z")]}})
        self.client.post("/inflight/refresh")
        html = self.client.get("/inflight").data.decode()
        self.assertEqual(self._hrefs(html), [S_NEW])  # exactly one card has a link
        self.assertIn(f'<a href="{S_NEW}" target="_blank" rel="noopener" class="session-open"', html)
        self.assertIn("OPEN SESSION", html)
        self.assertIn(S_NEW, self.client.get("/inflight/export.md").data.decode())
        # COPY FOR CLAUDE uses the same Markdown (hidden textarea).
        self.assertIn(f"**Claude Code session:** {S_NEW}", html)
        event = self._log(models.INFLIGHT_LOG_PATH)[-1]
        self.assertEqual((event["event"], event["session_links"]), ("refresh", 1))

    def test_inflight_cache_from_before_session_links_renders(self):
        legacy = {"repo": "demo", "owner": "chris", "html_url": "https://github.com/chris/demo",
                  "branch": "claude/old-abc123", "branch_url": "", "topic": "old work", "base": "main",
                  "pr": None, "commits": [], "ahead_by": 2, "behind_by": 0, "last_commit_at": "",
                  "last_activity_at": "2026-10-20T00:00:00Z", "is_claude": True, "compare_failed": False,
                  "merged_pr": None, "age_days": 5, "kind": "continue", "label": "NEEDS PR", "text": "Keep going."}
        models.save_inflight({"items": [legacy], "gathered_at": "2026-10-25T00:00:00Z",
                              "repos_checked": 1, "repos_skipped": 0, "failed_repos": [], "truncated_repos": {}})
        self.m._github_client = FakeGitHub(["main"])
        resp = self.client.get("/inflight")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("old work", html)
        self.assertNotIn("OPEN SESSION", html)
        exp = self.client.get("/inflight/export.md")
        self.assertEqual(exp.status_code, 200)
        self.assertNotIn("Claude Code session", exp.data.decode())
        # Resume picker reads the same cache.
        self.assertEqual(self.client.get("/resume").status_code, 200)

    def test_inflight_tampered_cache_never_reaches_href(self):
        base = {"repo": "demo", "owner": "chris", "html_url": "", "branch_url": "", "base": "main",
                "pr": None, "commits": [], "ahead_by": 1, "behind_by": 0, "last_activity_at": "",
                "age_days": 1, "kind": "continue", "label": "NEEDS PR", "text": "Keep going."}
        evil = ["javascript:alert(1)", "https://claude.ai.evil.com/code/session_x",
                '"><script>alert(1)</script>', f'{S_NEW}" onmouseover="alert(1)', 42, None]
        items = [dict(base, branch=f"b{k}", topic=f"t{k}", session_url=v) for k, v in enumerate(evil)]
        models.save_inflight({"items": items, "gathered_at": "2026-10-25T00:00:00Z"})
        self.m._github_client = FakeGitHub(["main"])
        html = self.client.get("/inflight").data.decode()
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn('" onmouseover="alert(1)', html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn("claude.ai.evil.com", html.split("inflight-markdown")[0])
        for href in self._hrefs(html):
            # Only the prefix-valid one renders, attribute-escaped so it can't break out.
            self.assertTrue(href.startswith("https://claude.ai/code/session_"), href)
            self.assertNotIn('"', href)
        md = self.client.get("/inflight/export.md").data.decode()
        self.assertNotIn("Claude Code session", md)

    # --- Recent sessions -----------------------------------------------------

    def _sessions_client(self, msg):
        pr = _pr(10, "claude/foo-abc123", state="closed", merged_at="2026-07-28T15:00:00Z",
                 updated="2026-07-28T15:00:00Z", created="2026-07-28T09:00:00Z")
        return FakeGitHub(["main", "claude/foo-abc123"], closed_pulls=[pr],
                          pull_commits={10: [_commit("f1", msg, "2026-07-28T14:00:00Z")]})

    def test_recent_sessions_card_link_export_and_log(self):
        self.m._github_client = self._sessions_client("Finish foo" + _trailer(S_NEW))
        html = self.client.get("/yesterday?date=2026-07-28").data.decode()
        self.assertIn("PR 10 title", html)
        self.assertEqual(self._hrefs(html), [S_NEW])
        self.assertIn('target="_blank" rel="noopener" class="session-open"', html)
        self.assertIn(f"**Claude Code session:** {S_NEW}", html)  # COPY FOR CLAUDE source
        self.assertIn(S_NEW, self.client.get("/yesterday/export.md?date=2026-07-28").data.decode())
        event = self._log(models.SESSIONS_LOG_PATH)[-1]
        self.assertEqual((event["event"], event["sessions"], event["session_links"]), ("view", 1, 1))

    def test_recent_sessions_hostile_commit_message(self):
        self.m._github_client = self._sessions_client(
            f'Finish foo\n\nClaude-Session: {S_NEW}"><img src=x onerror=alert(1)>')
        html = self.client.get("/yesterday?date=2026-07-28").data.decode()
        self.assertIn("PR 10 title", html)
        self.assertEqual(self._hrefs(html), [])
        self.assertNotIn("<img src=x", html)

    def test_recent_sessions_dict_without_key_renders(self):
        self.m._github_client = FakeGitHub(["main"])
        fake = [{"repo": "demo", "owner": "chris", "html_url": "", "branch": "claude/foo-abc123",
                 "is_default": False, "topic": "add foo", "base": "main", "pr": None, "commits": [],
                 "commit_count": 1, "first_commit_at": "", "last_commit_at": "", "last_activity_at": "",
                 "ahead_by": 2, "behind_by": 0, "last_sha": "f2",
                 "token": sessions.session_token("demo", "claude/foo-abc123"),
                 "recap_key": "demo::claude/foo-abc123::yesterday", "next_kind": "continue",
                 "next_label": "CONTINUE", "next_text": "Keep going."}]
        with patch.object(self.m.sessions, "gather_sessions", return_value=fake):
            resp = self.client.get("/yesterday")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("add foo", resp.data.decode())
        self.assertNotIn("OPEN SESSION", resp.data.decode())

    # --- Resume --------------------------------------------------------------

    def test_resume_packet_shows_link_at_top_and_in_export(self):
        self.m._github_client = FakeGitHub(
            ["main", "claude/add-foo-abc123"],
            compares={"claude/add-foo-abc123": {"ahead_by": 1, "behind_by": 0, "commits": [
                _commit("a", "WIP foo" + _trailer(S_NEW), "2026-10-24T01:00:00Z")]}})
        fetched = {"docs": {"project_status": "# Status\n\nNearly done."}, "readme": ""}
        with patch.object(resume.gh, "fetch_repo_docs", return_value=fetched):
            html = self.client.get("/resume/chris/demo").data.decode()
            exp = self.client.get("/resume/chris/demo/export.md").data.decode()
        # Inside the first-prompt box, above the prompt itself.
        box = html.index('<div class="resume-first-prompt">')
        row = html.index('<div class="resume-session-row">')
        self.assertLess(box, row)
        self.assertLess(row, html.index('id="resume-first-prompt"'))
        self.assertLess(row, html.index("<h2>Work in flight"))
        self.assertIn('class="session-open session-open-lg"', html)
        self.assertIn("Reopen the Claude Code session for", html)
        self.assertEqual(self._hrefs(html)[0], S_NEW)
        self.assertIn(f"Claude Code session to reopen: {S_NEW}", exp)
        event = self._log(models.RESUME_LOG_PATH)[-1]
        self.assertEqual(event["event"], "build")
        self.assertGreaterEqual(event["session_links"], 1)

    def test_resume_packet_without_links(self):
        self.m._github_client = FakeGitHub(["main"])
        with patch.object(resume.gh, "fetch_repo_docs", return_value={"docs": {}, "readme": ""}):
            html = self.client.get("/resume/chris/demo").data.decode()
        self.assertIn("Suggested first prompt", html)
        self.assertNotIn("resume-session-row", html)
        self.assertNotIn("OPEN SESSION", html)


if __name__ == "__main__":
    unittest.main()
