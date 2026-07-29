"""
Tests for the Yesterday screen (cross-repo session recap).

Covers window resolution, branch-topic parsing, session tokens, the
deterministic "what's next" classifier, commit-window filtering, cross-repo
gathering with a mocked GitHub client (merged PR / bare branch / direct-to-main
/ dedup), recap staleness, Markdown composition, and the Flask routes.
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions
import github_client as gh
import models

UTC = datetime.timezone.utc


def _window(since: str, until: str, key: str = "test", preset: str = "date") -> sessions.Window:
    s = datetime.datetime.fromisoformat(since)
    u = datetime.datetime.fromisoformat(until)
    return sessions.Window(s, u, "test window", key, preset)


def _commit(sha: str, msg: str, date: str, parents: int = 1) -> dict:
    return {
        "sha": sha,
        "commit": {"message": msg, "author": {"name": "Chris", "date": date},
                   "committer": {"name": "Chris", "date": date}},
        "parents": [{"sha": f"p{i}"} for i in range(parents)],
    }


def _repo(**overrides) -> dict:
    base = {
        "owner": "chris",
        "name": "demo",
        "default_branch": "main",
        "html_url": "https://github.com/chris/demo",
        "pushed_at": "2026-07-28T20:00:00Z",
    }
    base.update(overrides)
    return base


class TestWindow(unittest.TestCase):

    def test_yesterday_default(self):
        w = sessions.resolve_window({})
        self.assertEqual(w.preset, "yesterday")
        self.assertEqual(w.key, "yesterday")
        # A full day wide.
        self.assertAlmostEqual((w.until - w.since).total_seconds(), 86400, delta=3600)

    def test_presets(self):
        self.assertEqual(sessions.resolve_window({"range": "today"}).preset, "today")
        self.assertEqual(sessions.resolve_window({"range": "last3"}).key, "last3")
        self.assertEqual(sessions.resolve_window({"range": "last7"}).key, "last7")

    def test_explicit_date(self):
        w = sessions.resolve_window({"date": "2026-07-04"})
        self.assertEqual(w.preset, "date")
        self.assertEqual(w.key, "date:2026-07-04")
        self.assertEqual((w.until - w.since).days, 1)

    def test_bad_date_falls_back(self):
        w = sessions.resolve_window({"date": "not-a-date"})
        self.assertEqual(w.preset, "yesterday")

    def test_since_before_until_and_iso(self):
        w = sessions.resolve_window({"range": "last7"})
        self.assertLess(w.since, w.until)
        self.assertIn("T", w.since_iso)


class TestBranchTopic(unittest.TestCase):

    def test_claude_branch_strips_prefix_and_suffix(self):
        self.assertEqual(sessions.branch_topic("claude/yesterday-work-summary-zqlr51"),
                         "yesterday work summary")

    def test_henry_prefix(self):
        self.assertEqual(sessions.branch_topic("henry/fix-login-abc123"), "fix login")

    def test_plain_branch(self):
        self.assertEqual(sessions.branch_topic("feature-x"), "feature x")

    def test_main(self):
        self.assertEqual(sessions.branch_topic("main"), "main")


class TestToken(unittest.TestCase):

    def test_round_trip_with_slash_branch(self):
        tok = sessions.session_token("demo", "claude/foo-abc123")
        self.assertNotIn("/", tok)
        self.assertEqual(sessions.decode_token(tok), ("demo", "claude/foo-abc123"))

    def test_bad_token(self):
        self.assertIsNone(sessions.decode_token("!!!not-base64!!!"))


class TestClassifyNext(unittest.TestCase):

    def _s(self, **kw):
        base = {"base": "main", "pr": None, "ahead_by": None, "behind_by": None,
                "commit_count": 1, "commits": [], "is_default": False}
        base.update(kw)
        return base

    def test_default_branch(self):
        kind, _ = sessions.classify_next(self._s(is_default=True))
        self.assertEqual(kind, "review")

    def test_pr_merged(self):
        kind, _ = sessions.classify_next(self._s(
            pr={"number": 5, "merged": True, "state": "closed", "merged_at": "2026-07-28T10:00:00Z"}))
        self.assertEqual(kind, "complete")

    def test_pr_closed_unmerged(self):
        kind, _ = sessions.classify_next(self._s(
            pr={"number": 6, "merged": False, "state": "closed"}))
        self.assertEqual(kind, "decide")

    def test_pr_open_behind(self):
        kind, _ = sessions.classify_next(self._s(
            pr={"number": 7, "merged": False, "state": "open"}, behind_by=3))
        self.assertEqual(kind, "merge")

    def test_pr_open_ready(self):
        kind, _ = sessions.classify_next(self._s(
            pr={"number": 8, "merged": False, "state": "open"}, ahead_by=2, behind_by=0))
        self.assertEqual(kind, "merge")

    def test_no_pr_already_merged(self):
        kind, _ = sessions.classify_next(self._s(ahead_by=0))
        self.assertEqual(kind, "complete")

    def test_no_pr_wip_is_finish(self):
        kind, _ = sessions.classify_next(self._s(
            ahead_by=2, commits=[{"summary": "WIP: half done"}]))
        self.assertEqual(kind, "finish")

    def test_no_pr_normal_is_continue(self):
        kind, _ = sessions.classify_next(self._s(
            ahead_by=2, commit_count=2, commits=[{"summary": "Add settings page"}]))
        self.assertEqual(kind, "continue")


class TestCommitsInWindow(unittest.TestCase):

    def setUp(self):
        self.w = _window("2026-07-28T00:00:00+00:00", "2026-07-29T00:00:00+00:00")

    def test_filters_by_window(self):
        raw = [
            _commit("a", "in window", "2026-07-28T12:00:00Z"),
            _commit("b", "too old", "2026-07-27T12:00:00Z"),
            _commit("c", "too new", "2026-07-29T12:00:00Z"),
        ]
        out = sessions._commits_in_window(raw, self.w)
        self.assertEqual([c["sha"] for c in out], ["a"])

    def test_drop_merges(self):
        raw = [
            _commit("a", "real work", "2026-07-28T12:00:00Z"),
            _commit("m", "Merge pull request #1", "2026-07-28T13:00:00Z", parents=2),
        ]
        out = sessions._commits_in_window(raw, self.w, drop_merges=True)
        self.assertEqual([c["sha"] for c in out], ["a"])

    def test_newest_first(self):
        raw = [
            _commit("a", "first", "2026-07-28T08:00:00Z"),
            _commit("b", "second", "2026-07-28T18:00:00Z"),
        ]
        out = sessions._commits_in_window(raw, self.w)
        self.assertEqual([c["sha"] for c in out], ["b", "a"])


class TestGather(unittest.TestCase):
    """Cross-repo gathering with a fully mocked GitHub client."""

    def setUp(self):
        self.w = _window("2026-07-28T00:00:00+00:00", "2026-07-29T00:00:00+00:00")

    def _client(self):
        client = MagicMock()
        client.get_branches.return_value = [
            {"name": "main", "commit": {"sha": "m0"}},
            {"name": "claude/foo-abc123", "commit": {"sha": "f2"}},
            {"name": "claude/bar-def456", "commit": {"sha": "b1"}},
        ]
        # One merged PR for claude/foo, updated inside the window.
        client.get_pulls.return_value = [{
            "number": 10,
            "title": "Add the foo feature",
            "state": "closed",
            "merged_at": "2026-07-28T15:00:00Z",
            "closed_at": "2026-07-28T15:00:00Z",
            "created_at": "2026-07-28T09:00:00Z",
            "updated_at": "2026-07-28T15:00:00Z",
            "html_url": "https://github.com/chris/demo/pull/10",
            "head": {"ref": "claude/foo-abc123", "sha": "f2"},
            "base": {"ref": "main"},
        }]
        client.get_pull_commits.return_value = [
            _commit("f1", "Start foo", "2026-07-28T10:00:00Z"),
            _commit("f2", "Finish foo", "2026-07-28T14:00:00Z"),
        ]
        # claude/bar has no PR -> compare shows 1 in-window commit ahead.
        client.compare_branches.return_value = {
            "ahead_by": 1, "behind_by": 0,
            "commits": [_commit("b1", "WIP bar", "2026-07-28T11:00:00Z")],
        }
        # Direct-to-main: one real commit + the merge commit (dropped).
        client.get_commits_since.return_value = [
            _commit("d1", "Hotfix on main", "2026-07-28T16:00:00Z"),
            _commit("mg", "Merge pull request #10", "2026-07-28T15:00:00Z", parents=2),
            _commit("f2", "Finish foo", "2026-07-28T14:00:00Z"),  # already covered by PR
        ]
        return client

    def test_three_session_kinds(self):
        out = sessions.gather_sessions(self._client(), [_repo()], self.w)
        by_branch = {s["branch"]: s for s in out}
        self.assertEqual(set(by_branch), {"claude/foo-abc123", "claude/bar-def456", "main"})

        foo = by_branch["claude/foo-abc123"]
        self.assertEqual(foo["next_kind"], "complete")  # merged PR
        self.assertEqual(foo["topic"], "Add the foo feature")
        self.assertEqual(foo["commit_count"], 2)

        bar = by_branch["claude/bar-def456"]
        self.assertEqual(bar["next_kind"], "finish")  # WIP, no PR
        self.assertIsNone(bar["pr"])

        main = by_branch["main"]
        self.assertTrue(main["is_default"])
        # Merge commit dropped; PR-covered f2 deduped; only d1 remains.
        self.assertEqual([c["sha"] for c in main["commits"]], ["d1"])

    def test_prune_repo_not_pushed_since(self):
        client = self._client()
        old = _repo(pushed_at="2026-07-01T00:00:00Z")
        out = sessions.gather_sessions(client, [old], self.w)
        self.assertEqual(out, [])
        client.get_branches.assert_not_called()

    def test_repo_failure_isolated(self):
        client = self._client()
        client.get_branches.side_effect = RuntimeError("boom")
        # Should swallow and return [] rather than raise.
        self.assertEqual(sessions.gather_sessions(client, [_repo()], self.w), [])

    def test_auth_error_propagates(self):
        client = self._client()
        client.get_branches.side_effect = gh.GitHubAuthError("401")
        with self.assertRaises(gh.GitHubAuthError):
            sessions.gather_sessions(client, [_repo()], self.w)

    def test_gather_one_session_prefers_pr(self):
        s = sessions.gather_one_session(self._client(), _repo(), "claude/foo-abc123", self.w)
        self.assertIsNotNone(s)
        self.assertEqual(s["pr"]["number"], 10)


class TestRecapStaleness(unittest.TestCase):

    def test_missing_is_stale(self):
        self.assertTrue(sessions.is_recap_stale(None, {"last_sha": "x"}))

    def test_sha_match_fresh(self):
        self.assertFalse(sessions.is_recap_stale({"_source_sha": "x"}, {"last_sha": "x"}))

    def test_sha_changed_stale(self):
        self.assertTrue(sessions.is_recap_stale({"_source_sha": "old"}, {"last_sha": "new"}))

    def test_attach_sets_needs_flag(self):
        sess = [{"recap_key": "demo::b::yesterday", "last_sha": "s1"}]
        sessions.attach_recaps(sess, {"demo::b::yesterday": {"_source_sha": "s1", "headline": "x"}})
        self.assertFalse(sess[0]["needs_summary"])
        self.assertEqual(sess[0]["recap"]["headline"], "x")

    def test_attach_marks_stale_as_needs(self):
        sess = [{"recap_key": "demo::b::yesterday", "last_sha": "s2"}]
        sessions.attach_recaps(sess, {"demo::b::yesterday": {"_source_sha": "s1"}})
        self.assertTrue(sess[0]["needs_summary"])
        self.assertIsNone(sess[0]["recap"])


class TestMarkdown(unittest.TestCase):

    def _sessions(self):
        return [{
            "repo": "demo", "owner": "chris", "topic": "add foo",
            "branch": "claude/foo-abc123", "commit_count": 2,
            "pr": {"number": 10, "merged": True, "state": "closed"},
            "next_text": "PR #10 was merged.",
            "commits": [{"summary": "Finish foo", "date": "2026-07-28T14:00:00Z"}],
            "recap": {"what_you_did": "Built foo.", "how_you_directed_claude": "Asked for foo.",
                      "whats_next": "It's merged — confirm live."},
        }]

    def test_structure(self):
        md = sessions.compose_markdown(self._sessions(), "chris", "yesterday")
        self.assertIn("# Where I left off — yesterday", md)
        self.assertIn("github.com/chris", md)
        self.assertIn("### demo: add foo", md)
        self.assertIn("**What you did:** Built foo.", md)
        self.assertIn("**How you directed Claude:** Asked for foo.", md)
        self.assertIn("PR #10 (merged)", md)
        self.assertIn("- Finish foo", md)

    def test_empty(self):
        md = sessions.compose_markdown([], "chris", "yesterday")
        self.assertIn("No session activity", md)

    def test_falls_back_to_next_text_without_recap(self):
        s = self._sessions()
        s[0]["recap"] = None
        md = sessions.compose_markdown(s, "chris", "yesterday")
        self.assertIn("**What's next:** PR #10 was merged.", md)


class TestNormalizeRecap(unittest.TestCase):

    def test_defaults(self):
        out = sessions.normalize_recap({})
        self.assertEqual(out["headline"], "")
        self.assertEqual(out["whats_next"], "")

    def test_non_dict(self):
        self.assertEqual(sessions.normalize_recap(["x"])["what_you_did"], "")

    def test_strips(self):
        out = sessions.normalize_recap({"headline": "  hi  "})
        self.assertEqual(out["headline"], "hi")


class TestRecapStorage(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig = (models.SESSION_RECAPS_PATH, models.SESSIONS_LOG_PATH)
        models.SESSION_RECAPS_PATH = os.path.join(self._tmp, "session_recaps.json")
        models.SESSIONS_LOG_PATH = os.path.join(self._tmp, "sessions.log")

    def tearDown(self):
        models.SESSION_RECAPS_PATH, models.SESSIONS_LOG_PATH = self._orig

    def test_round_trip(self):
        self.assertEqual(models.get_session_recaps(), {})
        models.save_session_recap("demo::b::yesterday", {"headline": "x"})
        stored = models.get_session_recaps()
        self.assertIn("demo::b::yesterday", stored)
        self.assertIn("_generated_at", stored["demo::b::yesterday"])

    def test_log_round_trip(self):
        models.log_sessions_event("summarize_done", repo="demo", branch="b")
        events = models.tail_sessions_log(10)
        self.assertEqual(events[-1]["event"], "summarize_done")
        self.assertEqual(events[-1]["repo"], "demo")


class TestYesterdayRoutes(unittest.TestCase):

    def setUp(self):
        import app as app_module
        self.app_module = app_module
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self._orig = (app_module._scan_results, app_module._github_client,
                      app_module._credentials)

    def tearDown(self):
        (self.app_module._scan_results, self.app_module._github_client,
         self.app_module._credentials) = self._orig

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "chris"

    def test_unauthenticated_redirects(self):
        self.assertEqual(self.client.get("/yesterday").status_code, 302)

    def test_unauthenticated_api_401(self):
        tok = sessions.session_token("demo", "claude/foo-abc123")
        resp = self.client.post(f"/api/yesterday/summarize/{tok}")
        self.assertEqual(resp.status_code, 401)

    def test_no_scan_onboarding(self):
        self._login()
        self.app_module._scan_results = None
        resp = self.client.get("/yesterday")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"No scan data yet", resp.data)

    def test_not_connected(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = None
        resp = self.client.get("/yesterday")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Not connected to GitHub", resp.data)

    def test_renders_sessions(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = MagicMock()
        fake = [{
            "repo": "demo", "owner": "chris", "html_url": "", "branch": "claude/foo-abc123",
            "is_default": False, "topic": "add foo", "base": "main", "pr": None,
            "commits": [], "commit_count": 1, "first_commit_at": "", "last_commit_at": "",
            "last_activity_at": "2026-07-28T14:00:00Z", "ahead_by": 2, "behind_by": 0,
            "last_sha": "f2", "token": sessions.session_token("demo", "claude/foo-abc123"),
            "recap_key": "demo::claude/foo-abc123::yesterday", "next_kind": "continue",
            "next_label": "CONTINUE", "next_text": "Keep going.", "recap": None, "needs_summary": True,
        }]
        with patch.object(self.app_module.sessions, "gather_sessions", return_value=fake):
            resp = self.client.get("/yesterday")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"add foo", resp.data)
        self.assertIn(b"COPY FOR CLAUDE", resp.data)

    def test_summarize_bad_token(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = MagicMock()
        self.app_module._credentials = {"anthropic_key": "k"}
        resp = self.client.post("/api/yesterday/summarize/!!!bad!!!", json={})
        self.assertEqual(resp.status_code, 400)

    def test_summarize_unknown_repo_404(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = MagicMock()
        self.app_module._credentials = {"anthropic_key": "k"}
        tok = sessions.session_token("ghost", "claude/x-abc123")
        resp = self.client.post(f"/api/yesterday/summarize/{tok}", json={})
        self.assertEqual(resp.status_code, 404)

    def test_summarize_success(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = MagicMock()
        self.app_module._credentials = {"anthropic_key": "k"}
        tmp = tempfile.mkdtemp()
        orig = (models.SESSION_RECAPS_PATH, models.SESSIONS_LOG_PATH)
        models.SESSION_RECAPS_PATH = os.path.join(tmp, "r.json")
        models.SESSIONS_LOG_PATH = os.path.join(tmp, "s.log")
        try:
            fake_session = {"recap_key": "demo::claude/foo-abc123::yesterday",
                            "last_sha": "f2", "repo": "demo", "branch": "claude/foo-abc123"}
            with patch.object(self.app_module.sessions, "gather_one_session", return_value=fake_session), \
                 patch.object(self.app_module.sessions, "summarize_session",
                              return_value={"headline": "Built foo", "what_you_did": "x",
                                            "how_you_directed_claude": "y", "whats_next": "z",
                                            "_usage": {"input_tokens": 5, "output_tokens": 3}}):
                tok = sessions.session_token("demo", "claude/foo-abc123")
                resp = self.client.post(f"/api/yesterday/summarize/{tok}", json={"range": "yesterday"})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["ok"])
            self.assertIn("demo::claude/foo-abc123::yesterday", models.get_session_recaps())
        finally:
            models.SESSION_RECAPS_PATH, models.SESSIONS_LOG_PATH = orig


if __name__ == "__main__":
    unittest.main()
