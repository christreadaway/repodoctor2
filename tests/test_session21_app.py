"""Session 21 regression tests for app.py / models.py / launcher fixes."""

import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models


class _TmpStore(unittest.TestCase):
    """Point every store this file touches at a throwaway dir."""

    PATHS = ("GROUPS_PATH", "PREFS_PATH", "_LEGACY_PREFS_PATH", "PROGRAM_META_PATH",
             "PROGRAM_BRIEFS_PATH", "TRACKERS_DIR")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = {p: getattr(models, p) for p in self.PATHS}
        for p in self.PATHS:
            setattr(models, p, os.path.join(self.tmp, p.lower().strip("_") + (".json" if p != "TRACKERS_DIR" else "")))

    def tearDown(self):
        for p, v in self._saved.items():
            setattr(models, p, v)


class TestLoadJson(_TmpStore):

    def test_transient_oserror_does_not_discard_the_file(self):
        path = os.path.join(self.tmp, "g.json")
        with open(path, "w") as f:
            json.dump({"Keep": ["a"]}, f)
        real_open = open
        calls = {"n": 0}

        def flaky(p, *a, **k):
            if p == path and calls["n"] == 0:
                calls["n"] += 1
                raise PermissionError("sharing violation")
            return real_open(p, *a, **k)

        with patch("builtins.open", flaky):
            self.assertEqual(models._load_json(path), {"Keep": ["a"]})
        self.assertFalse(os.path.exists(path + ".corrupt"))

    def test_persistent_oserror_raises_instead_of_wiping(self):
        path = os.path.join(self.tmp, "g.json")
        with open(path, "w") as f:
            json.dump({"Keep": ["a"]}, f)
        with patch("builtins.open", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                models._load_json(path)
        self.assertTrue(os.path.exists(path))

    def test_real_corruption_still_renamed_aside(self):
        path = os.path.join(self.tmp, "g.json")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(models._load_json(path), {})
        self.assertTrue(os.path.exists(path + ".corrupt"))


class TestPreferencesNoLongerWriteTrackedFile(_TmpStore):

    def test_legacy_seed_read_and_never_written(self):
        with open(models._LEGACY_PREFS_PATH, "w") as f:
            json.dump({"ai_model": models.DEFAULT_MODEL, "local_root": "~/x"}, f)
        before = open(models._LEGACY_PREFS_PATH).read()
        models.update_preferences(lambda p: p.update(active_group="Church"))
        self.assertEqual(open(models._LEGACY_PREFS_PATH).read(), before)
        prefs = models.get_preferences()
        self.assertEqual(prefs["active_group"], "Church")
        self.assertEqual(prefs["local_root"], "~/x")


class TestGroupRules(_TmpStore):

    def test_category_groups_cannot_be_renamed_or_deleted(self):
        models.set_group("Church", ["a"])
        self.assertFalse(models.rename_group("Church", "Parish"))
        self.assertFalse(models.delete_group("Church"))
        self.assertIn("Church", models.get_groups())

    def test_rename_carries_program_notes_and_brief(self):
        models.set_group("Edu", ["a"])
        models._save_json(models.PROGRAM_META_PATH, {"Edu": {"notes": "n"}})
        models._save_json(models.PROGRAM_BRIEFS_PATH, {"Edu": {"x": 1}})
        self.assertTrue(models.rename_group("Edu", "Education Suite"))
        self.assertEqual(models.get_program_meta()["Education Suite"], {"notes": "n"})
        self.assertEqual(models.get_program_briefs()["Education Suite"], {"x": 1})


class TestAppRoutes(_TmpStore):

    def setUp(self):
        super().setUp()
        import app as app_module
        self.m = app_module
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self._orig_scan = app_module._scan_results
        app_module._scan_results = {
            "repos": [{"owner": "chris", "name": n, "full_name": f"chris/{n}",
                       "default_branch": "main"} for n in ("a", "b", "c")],
            "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        with self.client.session_transaction() as s:
            s["authenticated"] = True

    def tearDown(self):
        self.m._scan_results = self._orig_scan
        super().tearDown()

    def test_create_group_does_not_overwrite_existing(self):
        models.set_group("Work", ["a", "b", "c"])
        resp = self.client.post("/projects/groups/save",
                                data={"group_name": "Work", "repos": ["a"]}, follow_redirects=True)
        self.assertIn(b"already exists", resp.data)
        self.assertEqual(models.get_groups()["Work"], ["a", "b", "c"])

    def test_edit_group_keeps_members_not_in_scan(self):
        models.set_group("Edu", ["a", "teacheraide"])
        self.client.post("/projects/groups/save",
                         data={"group_name": "Edu", "original_name": "Edu", "repos": ["a", "b"]})
        self.assertEqual(models.get_groups()["Edu"], ["a", "b", "teacheraide"])

    def test_briefing_export_filename_is_slugged(self):
        models.set_group("Ministry ✝ \"A\"", ["a"])
        resp = self.client.get("/briefing/export.md?group=Ministry ✝ \"A\"")
        self.assertEqual(resp.status_code, 200)
        cd = resp.headers["Content-Disposition"]
        cd.encode("latin-1")  # must be encodable into the header
        self.assertIn("portfolio-chat-briefing-ministry-a-", cd)

    def test_api_errors_return_json_not_redirect(self):
        import requests
        import github_client
        with self.m.app.test_request_context("/api/yesterday/summarize/x", method="POST"):
            body, code = self.m._handle_github_network_error(requests.ConnectionError("down"))
            self.assertEqual(code, 503)
            self.assertIn("error", body.get_json())
            body, code = self.m._handle_github_auth_error(github_client.GitHubAuthError("x"))
            self.assertEqual(code, 401)
        with self.m.app.test_request_context("/stats"):
            self.assertEqual(self.m._handle_github_network_error(requests.ConnectionError("d")).status_code, 302)

    def test_tracker_generation_keeps_status_clicked_mid_run(self):
        prior = {"next_actions": [{"id": "N1", "status": "todo", "status_note": ""},
                                  {"id": "N2", "status": "todo", "status_note": ""}]}
        models.save_tracker("chris", "a", {"next_actions": [
            {"id": "N1", "status": "blocked", "status_note": "waiting on vendor"},
            {"id": "N2", "status": "todo", "status_note": ""}]})
        fresh = {"next_actions": [{"id": "N1", "status": "todo", "status_note": ""},
                                  {"id": "N2", "status": "shipped", "status_note": ""}]}
        self.m._save_generated_tracker("chris", "a", fresh, prior)
        saved = {a["id"]: a for a in models.get_tracker("chris", "a")["next_actions"]}
        self.assertEqual(saved["N1"]["status"], "blocked")
        self.assertEqual(saved["N1"]["status_note"], "waiting on vendor")
        self.assertEqual(saved["N2"]["status"], "shipped")

    def test_yesterday_out_of_range_date_is_not_a_500(self):
        with patch.object(self.m, "_get_github_client", return_value=None):
            self.assertNotEqual(self.client.get("/yesterday?date=9999-12-31").status_code, 500)


class TestLaunchers(unittest.TestCase):

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_mac_launcher_kills_only_the_listener_and_reinstalls_on_new_requirements(self):
        text = open(os.path.join(self.ROOT, "start.command")).read()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("lsof -ti:5001", code)
        self.assertIn("-sTCP:LISTEN", text)
        self.assertIn("requirements.txt -nt venv/.deps-installed", text)


if __name__ == "__main__":
    unittest.main()


class TestGitHubClientEdges(unittest.TestCase):

    def test_exhausted_rate_limit_does_not_sleep(self):
        import time
        from unittest.mock import MagicMock
        import github_client
        c = github_client.GitHubClient("t")
        resp = MagicMock(status_code=403, text="API rate limit exceeded",
                         headers={"X-RateLimit-Remaining": "0",
                                  "X-RateLimit-Reset": str(int(time.time()) + 2400)})
        c.session.get = MagicMock(return_value=resp)
        with patch.object(github_client.time, "sleep") as slept:
            self.assertIs(c._get("https://api.github.com/x"), resp)
        slept.assert_not_called()
        self.assertEqual(c.session.get.call_count, 1)

    def test_large_file_falls_back_to_raw(self):
        from unittest.mock import MagicMock
        import github_client
        c = github_client.GitHubClient("t")
        meta = MagicMock(status_code=200, text="", headers={})
        meta.json.return_value = {"type": "file", "encoding": "none", "content": ""}
        raw = MagicMock(status_code=200, text="", headers={}, content=b"# Big doc")
        c.session.get = MagicMock(side_effect=[meta, raw])
        self.assertEqual(c.get_file_content("o", "r", "BIG.md"), "# Big doc")
