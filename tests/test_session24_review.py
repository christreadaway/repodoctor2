"""Regression tests for the Session 24 independent backend review findings.

H1 (health trusting an In Flight cache that never saw a repo's branches) and
M2 (Update Anthropic key overwriting a working key when unverified) are
covered in tests/test_health.py and tests/test_scan_and_keys.py.
"""

import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_analyzer as ai
import github_client as gh
import health
import models
import nextup


def _resp(status, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = text
    r.headers = {}
    return r


class TestM1RateLimitedScanIsAnErrorNotAnF(unittest.TestCase):
    """A 403/5xx on the file tree used to read as 'no files' -> every doc
    missing -> a confident F. Now it raises and becomes the error row."""

    RAW = {"owner": {"login": "o"}, "name": "r", "full_name": "o/r", "default_branch": "main"}

    def client(self, tree_status=403, branches_status=200):
        c = gh.GitHubClient("x")

        def fake_get(url, **kw):
            if url.endswith("/branches"):
                return _resp(branches_status, [{"name": "main"}])
            if "/git/trees/" in url:
                return _resp(tree_status, {"tree": []}, text="API rate limit exceeded")
            if url.endswith("/languages"):
                return _resp(200, {"Python": 10})
            return _resp(200, [])
        c._get = MagicMock(side_effect=fake_get)
        return c

    def test_failed_tree_read_raises(self):
        with self.assertRaises(gh.GitHubFetchError):
            gh.scan_repo_lite(self.client(tree_status=403), self.RAW)
        with self.assertRaises(gh.GitHubFetchError):
            gh.scan_repo_lite(self.client(tree_status=502), self.RAW)

    def test_failed_branch_read_raises(self):
        with self.assertRaises(gh.GitHubFetchError):
            gh.scan_repo_lite(self.client(tree_status=200, branches_status=503), self.RAW)

    def test_through_the_parallel_scan_it_is_an_ungraded_error_row(self):
        import app as app_mod
        rows = app_mod._scan_repos_parallel(self.client(tree_status=403), [self.RAW])
        self.assertIn("error", rows[0])
        self.assertEqual(health.grade_repo(rows[0])["grade"], "—")

    def test_empty_repo_is_still_fine(self):
        # 409 = empty repository: not an error, just nothing there.
        c = self.client(tree_status=409, branches_status=200)
        row = gh.scan_repo_lite(c, self.RAW)
        self.assertNotIn("error", row)

    def test_other_callers_keep_the_forgiving_read(self):
        self.assertEqual(self.client(tree_status=403).get_all_file_paths("o", "r"), [])


class TestL1KeyCheckWorksWithoutSdkModels(unittest.TestCase):

    def _run(self, http):
        class NoModels:
            def __init__(self, *a, **k):
                pass
        with patch.object(ai.anthropic, "Anthropic", NoModels), \
             patch.object(ai.requests, "get", side_effect=http) as get:
            out = ai.verify_anthropic_key("sk-ant-api03-" + "x" * 30)
        return out, get

    def test_ok_invalid_unverified_via_http(self):
        (status, _), get = self._run(lambda *a, **k: _resp(200, {"data": []}))
        self.assertEqual(status, ai.KEY_OK)
        headers = get.call_args.kwargs["headers"]
        self.assertTrue(headers["x-api-key"].startswith("sk-ant-"))
        self.assertIn("anthropic-version", headers)
        (status, _), _ = self._run(lambda *a, **k: _resp(401))
        self.assertEqual(status, ai.KEY_INVALID)
        for code in (403, 429, 500, 529):
            (status, msg), _ = self._run(lambda *a, c=code, **k: _resp(c))
            self.assertEqual(status, ai.KEY_UNVERIFIED, code)
            self.assertIn(str(code), msg)

    def test_network_errors_via_http(self):
        import requests

        def timeout(*a, **k):
            raise requests.Timeout("slow")
        (status, msg), _ = self._run(timeout)
        self.assertEqual((status, msg), (ai.KEY_UNVERIFIED, "Timed out reaching Anthropic."))

        def down(*a, **k):
            raise requests.ConnectionError("refused sk-ant-api03-" + "x" * 30)
        with self.assertLogs("ai_analyzer", level="WARNING") as cm:
            (status, _), _ = self._run(down)
        self.assertEqual(status, ai.KEY_UNVERIFIED)
        self.assertNotIn("x" * 30, "\n".join(cm.output))

    def test_key_shape(self):
        self.assertTrue(ai.looks_like_anthropic_key("sk-ant-api03-" + "A" * 40))
        self.assertTrue(ai.looks_like_anthropic_key("  sk-ant-admin01-" + "b_-" * 10 + "  "))
        for bad in ("", None, "sk-ant-short", "my password", "ghp_" + "a" * 36, "sk-" + "a" * 40):
            self.assertFalse(ai.looks_like_anthropic_key(bad), bad)


class TestL2DoNextRanking(unittest.TestCase):

    def repo(self, name):
        return {"owner": "o", "name": name, "full_name": f"o/{name}", "pushed_at": "2026-11-08T00:00:00Z"}

    def test_blocked_only_repo_keeps_its_planned_step(self):
        res = nextup.build_do_next(
            [], {"o/a": {"next_actions": [{"id": "N1", "priority": "P1", "status": "blocked", "title": "x"}]}},
            {"a": {"next_steps": ["Ship it"]}}, [self.repo("a")])
        self.assertEqual([x["tier"] for x in res["items"]], ["planned", "blocked"])

    def test_low_priority_awaiting_deploy_still_confirms_live(self):
        res = nextup.build_do_next(
            [], {"o/a": {"next_actions": [{"id": "N1", "priority": "P3", "status": "awaiting_deploy", "title": "x"},
                                          {"id": "N2", "priority": "P2", "status": "todo", "title": "y"}]}},
            {}, [self.repo("a")])
        self.assertEqual([x["tier"] for x in res["items"]], ["deploy"])
        self.assertEqual(res["hidden_low_priority"], 1)


class TestL3RedactBeforeTruncate(unittest.TestCase):

    KEY = "sk-ant-api03-" + "Z" * 60

    def test_activity_redacts_a_key_near_the_cut(self):
        import activity
        msg = "x" * 170 + self.KEY + " trailing"
        out = activity._redact(msg, self.KEY)[:200]
        self.assertNotIn("ZZZZZZZZZZ", out)
        self.assertIn("[redacted]", out)

    def test_stats_redact_does_the_same(self):
        import app as app_mod
        msg = "RuntimeError: " + "y" * 280 + self.KEY
        out = app_mod._stats_redact(msg, {"anthropic_key": self.KEY})[:340]
        self.assertNotIn("ZZZZZZZZZZ", out)


class TestL4CorruptCachesNeverCrashPages(unittest.TestCase):

    def setUp(self):
        import app as app_mod
        self.m = app_mod
        app_mod.app.config["TESTING"] = True
        self.client = app_mod.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["github_user"] = "t"
        self.tmp = tempfile.mkdtemp()
        names = ("INFLIGHT_PATH", "GROUPS_PATH", "_LEGACY_GROUPS_PATH", "PREFS_PATH", "TRACKERS_DIR",
                 "SUMMARIES_PATH", "NEXTUP_LOG_PATH", "ACTIVITY_SUMMARIES_PATH", "ACTIVITY_LOG_PATH",
                 "HEALTH_LOG_PATH")
        self._paths = {p: getattr(models, p) for p in names}
        for p in names:
            setattr(models, p, os.path.join(self.tmp, p.lower().strip("_")))
        self._orig = (app_mod._scan_results, app_mod._github_client, app_mod._activity_cache,
                      app_mod._activity_ranges)
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        class Fake:
            def get_commits_range(self, owner, name, since_iso, ref=None, until_iso=None, max_pages=50):
                return [{"sha": "abc1234567", "parents": [{}], "html_url": "",
                         "commit": {"message": "m", "author": {"name": "c", "date": ts},
                                    "committer": {"date": ts}}}], False

            def get_merged_pulls_since(self, *a, **k):
                return [], False, ""
        app_mod._github_client = Fake()
        app_mod._activity_cache = None
        app_mod._activity_ranges = {}
        app_mod._scan_results = {"scanned_at": now.isoformat(), "repos": [{
            "owner": "o", "name": "a", "full_name": "o/a", "default_branch": "main",
            "html_url": "https://github.com/o/a", "pushed_at": ts, "branch_names": ["main", "x"],
            "required_files": {}, "files_present": 0, "files_total": 5, "total_branch_count": 2,
            "non_default_branch_count": 1, "code_size_bytes": 1}]}

    def tearDown(self):
        (self.m._scan_results, self.m._github_client, self.m._activity_cache,
         self.m._activity_ranges) = self._orig
        for p, v in self._paths.items():
            setattr(models, p, v)

    def write(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f)

    def test_odd_inflight_shapes(self):
        cases = [
            {"items": {"not": "a list"}, "gathered_at": "2026-11-09T00:00:00+00:00"},
            {"items": [None, "str", 5, {"repo": "a", "owner": "o", "kind": "merge", "pr": "nope",
                                        "last_activity_at": 12345, "branch": ["x"], "age_days": "3",
                                        "commits": "zzz", "session_url": {"x": 1}}],
             "gathered_at": 20261109, "failed_repos": "o/a", "truncated_repos": ["o/a"]},
        ]
        for data in cases:
            self.write(models.INFLIGHT_PATH, data)
            for url in ("/next", "/next/export.md", "/review", "/review/export.md", "/inflight",
                        "/inflight/export.md", "/portfolio", "/"):
                r = self.client.get(url)
                self.assertNotEqual(r.status_code, 500, (url, data))

    def test_non_string_summary_text_does_not_500_stats(self):
        self.write(models.ACTIVITY_SUMMARIES_PATH,
                   {"repos": {"fp": {"text": {"no": 1}, "repo": "o/a"}}, "portfolio": {"p": {"text": 7}}})
        for url in ("/stats", "/stats/export.md", "/review"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_clean_inflight_keeps_good_items_intact(self):
        good = {"repo": "a", "owner": "o", "kind": "merge", "branch": "b", "pr": {"number": 1},
                "ahead_by": 2, "commits": [{"sha": "x"}], "session_url": "https://claude.ai/code/session_01A"}
        self.write(models.INFLIGHT_PATH, {"items": [good], "gathered_at": "2026-11-09T00:00:00+00:00"})
        got = models.get_inflight()["items"][0]
        self.assertEqual({k: got[k] for k in good}, good, "good values survive unchanged")
        self.assertEqual(got["topic"], "", "missing string fields are filled, not left to KeyError")


class TestL5OldScansGetARescanHint(unittest.TestCase):

    def test_hint_only_for_low_grades_from_old_scans(self):
        old = {"owner": "o", "name": "r", "default_branch": "main", "branch_names": ["main"],
               "required_files": {k: False for k in ("CLAUDE.md", "LICENSE", "PRODUCT_SPEC.md",
                                                     "PROJECT_STATUS.md", "SESSION_NOTES.md")},
               "docs_updated": False, "pushed_at": "2024-01-01T00:00:00Z", "total_branch_count": 1}
        g = health.grade_repo(old)
        self.assertIn(g["grade"], ("D", "F"))
        self.assertIn("re-scan", g["summary"])
        modern = {**old, "archived": False, "fork": False}
        self.assertNotIn("re-scan", health.grade_repo(modern)["summary"])


if __name__ == "__main__":
    unittest.main()


# ======================================================================
# Browser-sweep review findings
# ======================================================================

class _RouteBase(unittest.TestCase):
    """Fresh app state + temp stores for route-level checks."""

    def setUp(self):
        import app as app_mod
        self.m = app_mod
        app_mod.app.config["TESTING"] = True
        self.client = app_mod.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["github_user"] = "t"
        self.tmp = tempfile.mkdtemp()
        names = ("INFLIGHT_PATH", "GROUPS_PATH", "_LEGACY_GROUPS_PATH", "PREFS_PATH", "TRACKERS_DIR",
                 "SUMMARIES_PATH", "NEXTUP_LOG_PATH", "ACTIVITY_SUMMARIES_PATH", "ACTIVITY_LOG_PATH",
                 "HEALTH_LOG_PATH", "INFLIGHT_LOG_PATH", "SESSIONS_LOG_PATH")
        self._paths = {p: getattr(models, p) for p in names}
        for p in names:
            setattr(models, p, os.path.join(self.tmp, p.lower().strip("_")))
        self._orig = (app_mod._scan_results, app_mod._github_client, app_mod._activity_cache,
                      app_mod._activity_ranges, dict(app_mod._anthropic_key_status))

    def tearDown(self):
        (self.m._scan_results, self.m._github_client, self.m._activity_cache,
         self.m._activity_ranges, ks) = self._orig
        self.m._anthropic_key_status = ks
        for p, v in self._paths.items():
            setattr(models, p, v)


class TestH1ErrorRowsAreNotTidy(unittest.TestCase):

    def test_scan_error_row_is_read_by_inflight(self):
        import inflight
        err = {"owner": "o", "name": "r", "default_branch": "main", "non_default_branch_count": 0,
               "branch_names": [], "error": "GitHubFetchError: HTTP 502"}
        self.assertFalse(inflight._scan_shows_tidy(err))
        self.assertTrue(inflight._scan_shows_tidy({**err, "error": None}))


class TestH2HonestCouldntRead(_RouteBase):

    def test_sessions_report_failures(self):
        import sessions
        failures = []

        class Boom:
            def __getattr__(self, name):
                def f(*a, **k):
                    raise RuntimeError("down")
                return f
        window = sessions.resolve_window({"range": "last7"})
        out = sessions.gather_sessions(Boom(), [{"owner": "o", "name": "r"}], window, failures=failures)
        self.assertEqual((out, failures), ([], ["o/r"]))

    def test_resume_sections_say_couldnt_read(self):
        import resume
        packet = {"repo": {"name": "r", "default_branch": "main"}, "full_name": "o/r", "built_at": "2026-11-09T00:00:00+00:00",
                  "inflight": [], "recent_sessions": [], "tracker_actions": [], "planned_steps": [],
                  "errors": ["x"], "inflight_failed": True, "sessions_failed": True, "docs_failed": True,
                  "first_prompt": "p", "status_excerpt": "", "notes_excerpt": "", "groups": [],
                  "docs_present": [], "docs_missing": [], "languages": [], "history": {}}
        md = resume.compose_markdown(packet)
        self.assertIn("Couldn't read this repo's branches", md)
        self.assertIn("Couldn't read recent sessions", md)
        self.assertNotIn("Nothing unmerged", md)
        self.assertNotIn("No Claude Code activity", md)

    def test_inflight_and_do_next_name_unread_repos(self):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.m._scan_results = {"scanned_at": now, "repos": [
            {"owner": "o", "name": "broken", "full_name": "o/broken", "default_branch": "main"}]}
        models.save_inflight({"items": [], "gathered_at": now, "failed_repos": ["o/broken"]})
        body = self.client.get("/inflight").data.decode()
        self.assertNotIn("Clean slate", body)
        self.assertIn("couldn't be read (o/broken)", body)
        body = self.client.get("/next").data.decode()
        self.assertIn("couldn't reach 1 repo (o/broken)", body)
        self.assertNotIn("Every branch is merged", body)

    def test_review_doesnt_claim_nothing_for_unread_repos(self):
        dn = nextup.build_do_next([], {}, {}, [{"owner": "o", "name": "b"}])
        week = {"updated": [], "failed": [{"name": "b"}],
                "totals": {"repos_updated": 0, "repos_total": 1, "read": 0, "errors": 1,
                           "commits": 0, "prs_merged": 0, "active_days": 0, "days": 7}}
        rv = nextup.build_review(week, {"updated": [], "failed": [{"name": "b"}]}, dn, [], [{"owner": "o", "name": "b"}])
        md = nextup.compose_review_markdown(rv)
        self.assertNotIn("Nothing landed", md)
        self.assertNotIn("Nothing went quiet.", md)
        self.assertIn("0 of 0 repos read were updated** (1 couldn't be read)", md)
        self.assertIn("No activity found in the repos GitHub could read", md)


class TestH4PortfolioBeforeFirstScan(_RouteBase):

    def test_no_scan_is_not_a_500(self):
        self.m._scan_results = None
        self.assertEqual(self.client.get("/portfolio").status_code, 200)


class TestM1KeyMessages(_RouteBase):

    def test_401_points_to_update_key_not_reset(self):
        import anthropic
        e = anthropic.AuthenticationError(message="no", response=MagicMock(status_code=401, headers={}), body=None)
        msg = self.m._stats_ai_error(e)
        self.assertIn("Update Anthropic key", msg)
        self.assertNotIn("Reset Credentials", msg)

    def test_stats_warns_when_key_was_rejected(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        class Fake:
            def get_commits_range(self, *a, **k):
                return [{"sha": "abc1234567", "parents": [{}], "html_url": "",
                         "commit": {"message": "m", "author": {"name": "c", "date": ts}, "committer": {"date": ts}}}], False

            def get_merged_pulls_since(self, *a, **k):
                return [], False, ""
        self.m._github_client = Fake()
        self.m._activity_cache = None
        self.m._activity_ranges = {}
        self.m._scan_results = {"scanned_at": now.isoformat(), "repos": [
            {"owner": "o", "name": "a", "full_name": "o/a", "default_branch": "main", "html_url": ""}]}
        self.m._anthropic_key_status = {"status": "invalid", "message": "", "checked_at": "", "source": "login"}
        body = self.client.get("/stats").data.decode()
        self.assertIn("rejected the last time it was checked", body)
        self.m._anthropic_key_status = {"status": "ok", "message": "", "checked_at": "", "source": "login"}
        self.assertNotIn("rejected the last time", self.client.get("/stats").data.decode())


class TestM2DoNextSkipsArchived(unittest.TestCase):

    def test_archived_out_fork_keeps_real_work_but_no_plan(self):
        repos = [{"owner": "o", "name": "arch", "archived": True},
                 {"owner": "o", "name": "frk", "fork": True},
                 {"owner": "o", "name": "live"}]
        items = [{"repo": "frk", "owner": "o", "kind": "merge", "pr": {"number": 1, "html_url": "u"},
                  "last_activity_at": "2026-11-01T00:00:00Z", "branch": "b", "topic": "t"}]
        summaries = {n: {"next_steps": [f"step {n}"]} for n in ("arch", "frk", "live")}
        res = nextup.build_do_next(items, {"o/arch": {"next_actions": [{"id": "N1", "priority": "P0", "status": "todo", "title": "x"}]}},
                                   summaries, repos)
        got = [(x["repo"], x["tier"]) for x in res["items"]]
        self.assertEqual(got, [("frk", "merge"), ("live", "planned")])


class TestLowFixes(_RouteBase):

    def test_l1_macro_drops_tampered_session_urls(self):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.m._scan_results = {"scanned_at": now, "repos": [
            {"owner": "o", "name": "a", "full_name": "o/a", "default_branch": "main"}]}
        bad = "https://claude.ai/code/session_01Tamper/../../logout"
        models.save_inflight({"gathered_at": now, "items": [
            {"repo": "a", "owner": "o", "kind": "finish", "branch": "claude/x-abc123", "topic": "t",
             "text": "x", "label": "FINISH IT", "base": "main", "session_url": bad, "commits": []}]})
        for url in ("/inflight", "/next"):
            body = self.client.get(url).data.decode()
            self.assertNotIn("session_01Tamper", body, url)

    def test_l2_inflight_refresh_keeps_the_filter(self):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.m._scan_results = {"scanned_at": now, "repos": []}
        models.save_inflight({"gathered_at": now, "items": []})
        body = self.client.get("/inflight?kind=stale").data.decode()
        self.assertIn('name="next" value="/inflight?kind=stale"', body)

    def test_l5_only_session_branches_lose_a_suffix(self):
        import sessions
        self.assertEqual(sessions.branch_topic("fix-mobile-layout"), "fix mobile layout")
        self.assertEqual(sessions.branch_topic("feature/add-health-grades"), "feature/add health grades")
        self.assertEqual(sessions.branch_topic("claude/add-health-grades-x1y2z3"), "add health grades")
        self.assertEqual(sessions.branch_topic("claude/yesterday-work-summary-zqlr51"), "yesterday work summary")

    def test_l6_bidi_controls_are_stripped(self):
        import activity
        import sessions
        evil = "Add logo ‮gnp.exe"
        c = sessions._normalize_commit({"sha": "s", "commit": {"message": evil}})
        self.assertNotIn("‮", c["summary"])
        self.assertNotIn("‮", sessions._pr_view({"title": evil})["title"])
        self.assertNotIn("‮", activity._normalize_pr({"number": 1, "title": evil})["title"])
        res = nextup.build_do_next([], {"o/a": {"next_actions": [{"id": "N1", "priority": "P0", "status": "todo", "title": evil}]}},
                                   {}, [{"owner": "o", "name": "a"}])
        self.assertNotIn("‮", res["items"][0]["title"])

    def test_l9_sort_headers_are_keyboard_reachable_and_l4_anchor(self):
        self.m._scan_results = {"scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "repos": []}
        self.assertIn("th.tabIndex = 0", self.client.get("/").data.decode())
        self.assertIn('id="credentials"', self.client.get("/settings").data.decode())
