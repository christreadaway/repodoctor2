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
