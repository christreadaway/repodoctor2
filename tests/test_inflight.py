"""
Tests for In Flight (every unmerged branch / open PR across repos) and Resume
(a context-switch packet for one project), plus the app-wide log file, the
stale-scan age helper, and the safe ?next= redirect on login.
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inflight
import resume
import models

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _commit(sha, msg, date):
    return {"sha": sha, "commit": {"message": msg, "author": {"name": "Chris", "date": date},
                                   "committer": {"name": "Chris", "date": date}},
            "parents": [{"sha": "p"}]}


def _repo(**over):
    base = {"owner": "chris", "name": "demo", "full_name": "chris/demo", "default_branch": "main",
            "html_url": "https://github.com/chris/demo", "pushed_at": "2026-10-24T20:00:00Z",
            "total_branch_count": 2, "non_default_branch_count": 1, "branch_names": ["main", "claude/add-foo-abc123"],
            "required_files": {"CLAUDE.md": True, "PROJECT_STATUS.md": False},
            "languages": {"Python": 100, "HTML": 20}, "code_size_bytes": 2048}
    base.update(over)
    return base


def _pr(number, head, draft=False, base="main"):
    return {"number": number, "title": f"PR {number} title", "state": "open", "draft": draft,
            "created_at": "2026-10-20T00:00:00Z", "updated_at": "2026-10-24T00:00:00Z",
            "html_url": f"https://github.com/chris/demo/pull/{number}",
            "base": {"ref": base}, "head": {"ref": head, "sha": "h"}}


class FakeClient:
    """Branch/PR/compare fixture for one repo."""

    def __init__(self, branches, pulls, compares):
        self._branches = branches
        self._pulls = pulls
        self._compares = compares  # head -> compare dict
        self.compare_calls = 0

    def get_branches(self, owner, name):
        return [{"name": b} for b in self._branches]

    def get_pulls(self, owner, name, state="open", **kw):
        assert state == "open"
        return self._pulls

    def compare_branches(self, owner, name, base, head):
        self.compare_calls += 1
        return self._compares.get(head)


class TestClassify(unittest.TestCase):

    def _item(self, **kw):
        base = {"base": "main", "pr": None, "ahead_by": 2, "behind_by": 0, "commits": [],
                "last_activity_at": "2026-10-24T00:00:00Z"}
        base.update(kw)
        return base

    def test_open_pr_ready(self):
        kind, text = inflight.classify(self._item(pr={"number": 7, "draft": False}), NOW)
        self.assertEqual(kind, "merge")
        self.assertIn("PR #7", text)

    def test_open_pr_behind_says_update(self):
        kind, text = inflight.classify(self._item(pr={"number": 7, "draft": False}, behind_by=3), NOW)
        self.assertEqual(kind, "merge")
        self.assertIn("3 commits behind", text)

    def test_draft_pr_is_finish(self):
        kind, _ = inflight.classify(self._item(pr={"number": 7, "draft": True}), NOW)
        self.assertEqual(kind, "finish")

    def test_fully_merged_branch_is_complete_even_with_pr(self):
        kind, text = inflight.classify(self._item(pr={"number": 7}, ahead_by=0), NOW)
        self.assertEqual(kind, "complete")
        self.assertIn("Delete the branch", text)

    def test_wip_commit_is_finish(self):
        kind, _ = inflight.classify(self._item(commits=[{"summary": "WIP: half done"}]), NOW)
        self.assertEqual(kind, "finish")

    def test_fresh_branch_without_pr_needs_pr(self):
        kind, text = inflight.classify(self._item(), NOW)
        self.assertEqual(kind, "continue")
        self.assertIn("2 commits", text)

    def test_old_branch_without_pr_is_stale(self):
        kind, text = inflight.classify(self._item(last_activity_at="2026-09-01T00:00:00Z"), NOW)
        self.assertEqual(kind, "stale")
        self.assertIn("54 days", text)

    def test_age_days(self):
        self.assertEqual(inflight.age_days("2026-10-20T12:00:00Z", NOW), 5)
        self.assertIsNone(inflight.age_days("", NOW))
        self.assertIsNone(inflight.age_days("garbage", NOW))


class TestGather(unittest.TestCase):

    def test_repo_gather_builds_items_for_each_non_default_branch(self):
        client = FakeClient(
            branches=["main", "claude/add-foo-abc123", "old-idea"],
            pulls=[_pr(5, "claude/add-foo-abc123")],
            compares={
                "claude/add-foo-abc123": {"ahead_by": 3, "behind_by": 1,
                                          "commits": [_commit("a", "Add foo", "2026-10-24T01:00:00Z"),
                                                      _commit("b", "Wire foo", "2026-10-24T02:00:00Z")]},
                "old-idea": {"ahead_by": 1, "behind_by": 40,
                             "commits": [_commit("c", "Try idea", "2026-08-01T00:00:00Z")]},
            })
        items = inflight.gather_repo_inflight(client, _repo(), NOW)
        self.assertEqual(len(items), 2)
        by_branch = {i["branch"]: i for i in items}
        foo = by_branch["claude/add-foo-abc123"]
        self.assertEqual(foo["kind"], "merge")
        self.assertEqual(foo["pr"]["number"], 5)
        self.assertEqual(foo["topic"], "PR 5 title")
        self.assertEqual(foo["commits"][0]["summary"], "Wire foo")  # newest first
        self.assertTrue(foo["is_claude"])
        self.assertEqual(foo["branch_url"], "https://github.com/chris/demo/tree/claude/add-foo-abc123")
        old = by_branch["old-idea"]
        self.assertEqual(old["kind"], "stale")
        self.assertEqual(old["topic"], "old idea")
        self.assertEqual(old["age_days"], 85)

    def test_compare_failure_still_yields_item(self):
        client = FakeClient(["main", "x"], [], {})  # compare returns None
        items = inflight.gather_repo_inflight(client, _repo(), NOW)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["ahead_by"])

    def test_cross_repo_skips_tidy_repos_and_isolates_failures(self):
        good = FakeClient(["main", "f"], [], {"f": {"ahead_by": 1, "behind_by": 0,
                                                    "commits": [_commit("a", "x", "2026-10-24T00:00:00Z")]}})
        tidy = _repo(name="tidy", non_default_branch_count=0, branch_names=[])
        broken = _repo(name="broken")
        calls = {"n": 0}

        def get_branches(owner, name):
            calls["n"] += 1
            if name == "broken":
                raise RuntimeError("boom")
            return good.get_branches(owner, name)

        client = MagicMock()
        client.get_branches.side_effect = get_branches
        client.get_pulls.side_effect = good.get_pulls
        client.compare_branches.side_effect = good.compare_branches
        data = inflight.gather_inflight(client, [_repo(), tidy, broken], NOW)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["repos_skipped"], 1)
        self.assertEqual(data["repos_checked"], 2)
        self.assertEqual(data["failed_repos"], ["chris/broken"])
        self.assertEqual(calls["n"], 2)  # tidy repo cost zero calls

    def test_sort_puts_merge_first_then_newest(self):
        items = [
            {"kind": "stale", "last_activity_at": "2026-10-01T00:00:00Z"},
            {"kind": "merge", "last_activity_at": "2026-10-02T00:00:00Z"},
            {"kind": "merge", "last_activity_at": "2026-10-05T00:00:00Z"},
        ]
        client = MagicMock()
        with patch.object(inflight, "gather_repo_inflight", return_value=items):
            data = inflight.gather_inflight(client, [_repo()], NOW)
        self.assertEqual([i["kind"] for i in data["items"]], ["merge", "merge", "stale"])
        self.assertEqual(data["items"][0]["last_activity_at"], "2026-10-05T00:00:00Z")

    def test_counts_filter_and_markdown(self):
        items = [
            {"kind": "merge", "repo": "a", "owner": "c", "branch": "b1", "topic": "t1", "text": "do", "label": "READY TO MERGE",
             "pr": {"number": 1, "draft": False}, "ahead_by": 2, "behind_by": 0, "age_days": 1, "commits": [], "last_activity_at": ""},
            {"kind": "stale", "repo": "b", "owner": "c", "branch": "b2", "topic": "t2", "text": "decide", "label": "STALE",
             "pr": None, "ahead_by": 1, "behind_by": 9, "age_days": 40,
             "commits": [{"summary": "old work", "date": "2026-09-01T00:00:00Z"}], "last_activity_at": ""},
        ]
        counts = inflight.summarize_counts(items)
        self.assertEqual((counts["total"], counts["repos"], counts["merge"], counts["stale"]), (2, 2, 1, 1))
        self.assertEqual(len(inflight.filter_items(items, kind="stale")), 1)
        self.assertEqual(len(inflight.filter_items(items, repos={"a"})), 1)
        md = inflight.compose_markdown(items, "chris", NOW.isoformat())
        self.assertIn("# Work in flight", md)
        self.assertIn("## READY TO MERGE (1)", md)
        self.assertIn("## STALE (1)", md)
        self.assertIn("PR #1", md)
        self.assertIn("- old work", md)
        self.assertIn("Nothing in flight", inflight.compose_markdown([], "", NOW.isoformat()))


class TestResumeHelpers(unittest.TestCase):

    def test_status_head_cuts_at_section_boundary(self):
        text = "# Status\n\nIntro " + ("x" * 2000) + "\n\n## Section A\n" + ("a" * 2000) + "\n## Section B\n" + ("b" * 2000)
        head = resume.status_head(text, max_chars=4500)
        self.assertIn("## Section A", head)
        self.assertNotIn("## Section B", head)
        self.assertTrue(head.endswith("read the full file in the repo)"))

    def test_status_head_short_text_untouched(self):
        self.assertEqual(resume.status_head("short"), "short")

    def test_latest_notes_entry_takes_first_h2(self):
        notes = "# Notes\n\npreamble\n\n## Session 19 — latest\nbuilt stuff\n\n## Session 18\nolder"
        entry = resume.latest_notes_entry(notes)
        self.assertTrue(entry.startswith("## Session 19"))
        self.assertIn("built stuff", entry)
        self.assertNotIn("Session 18", entry)

    def test_latest_notes_entry_without_h2_falls_back(self):
        self.assertEqual(resume.latest_notes_entry("just text"), "just text")
        self.assertEqual(resume.latest_notes_entry(""), "")

    def test_first_prompt_prefers_unfinished_branch(self):
        packet = {"repo": {"name": "demo"},
                  "inflight": [{"kind": "continue", "branch": "claude/x-abc123", "topic": "x", "base": "main"}],
                  "tracker_actions": [], "planned_steps": []}
        self.assertIn("Pick up the `claude/x-abc123` branch", resume.suggest_first_prompt(packet))

    def test_first_prompt_then_merge_then_tracker_then_steps_then_default(self):
        base = {"repo": {"name": "demo"}, "inflight": [], "tracker_actions": [], "planned_steps": []}
        merge = {**base, "inflight": [{"kind": "merge", "pr": {"number": 3}, "branch": "b", "topic": "t", "base": "main"}]}
        self.assertIn("PR #3 is ready to merge", resume.suggest_first_prompt(merge))
        tracker = {**base, "tracker_actions": [{"id": "N3", "priority": "P0", "title": "Fix login"}]}
        self.assertIn("N3 (P0): Fix login", resume.suggest_first_prompt(tracker))
        steps = {**base, "planned_steps": ["Ship the thing"]}
        self.assertIn("Ship the thing", resume.suggest_first_prompt(steps))
        self.assertIn("summarize where we are", resume.suggest_first_prompt(base))


class TestBuildPacket(unittest.TestCase):

    def test_packet_composes_all_sections(self):
        client = FakeClient(["main", "claude/add-foo-abc123"], [],
                            {"claude/add-foo-abc123": {"ahead_by": 1, "behind_by": 0,
                                                       "commits": [_commit("a", "Add foo", "2026-10-24T01:00:00Z")]}})
        fetched = {"docs": {"project_status": "# Status\n\nWe are 80% done.",
                            "session_notes": "# Notes\n\n## Session 9\nDid things\n\n## Session 8\nold"},
                   "readme": ""}
        tracker = {"next_actions": [{"id": "N1", "title": "Ship it", "priority": "P0", "status": "todo"}]}
        with patch.object(resume.gh, "fetch_repo_docs", return_value=fetched), \
             patch.object(resume.sessions, "gather_sessions", return_value=[]):
            packet = resume.build_packet(
                client, _repo(), summary={"what_it_does": "A demo.", "next_steps": ["Step one"]},
                brief={"what_it_is": "Demo app", "stage": "Building", "where_we_are": "Midway"},
                blurb={"summary": "blurb"}, tracker=tracker,
                groups={"School": ["demo"], "Church": []}, categories={"demo": "School"},
                history={"chris/demo": {"count": 2, "last_resumed_at": "2026-10-20T00:00:00Z"}})
        self.assertEqual(packet["category"], "School")
        self.assertEqual(packet["groups"], [])  # category group is not repeated as a group
        self.assertEqual(len(packet["inflight"]), 1)
        self.assertIn("80% done", packet["status_excerpt"])
        self.assertTrue(packet["notes_excerpt"].startswith("## Session 9"))
        self.assertEqual(packet["tracker_actions"][0]["id"], "N1")
        self.assertEqual(packet["planned_steps"], ["Step one"])
        self.assertEqual(packet["docs_missing"], ["PROJECT_STATUS.md"])
        self.assertEqual(packet["history"]["count"], 2)
        self.assertIn("claude/add-foo-abc123", packet["first_prompt"])

        md = resume.compose_markdown(packet, "chris")
        for needle in ("# Resume: demo", "Category: School", "Stage: Building", "## What it is", "Demo app",
                       "## Work in flight", "NEEDS PR", "## Recent sessions", "[P0] N1 Ship it",
                       "Step one", "## PROJECT_STATUS.md (top)", "## SESSION_NOTES.md (latest entry)",
                       "## Suggested first prompt for Claude Code"):
            self.assertIn(needle, md)

    def test_packet_isolates_github_failures(self):
        client = MagicMock()
        client.get_branches.side_effect = RuntimeError("branches down")
        with patch.object(resume.gh, "fetch_repo_docs", side_effect=RuntimeError("docs down")), \
             patch.object(resume.sessions, "gather_sessions", side_effect=RuntimeError("sessions down")):
            packet = resume.build_packet(client, _repo(), summary=None, brief=None, blurb=None,
                                         tracker=None, groups={}, categories={})
        self.assertEqual(len(packet["errors"]), 3)
        self.assertEqual(packet["inflight"], [])
        md = resume.compose_markdown(packet)
        self.assertIn("## Gaps in this packet", md)
        self.assertIn("summarize where we are", packet["first_prompt"])

    def test_readme_fallback_when_no_docs(self):
        client = FakeClient(["main"], [], {})
        with patch.object(resume.gh, "fetch_repo_docs", return_value={"docs": {}, "readme": "# Readme\nhello"}), \
             patch.object(resume.sessions, "gather_sessions", return_value=[]):
            packet = resume.build_packet(client, _repo(), summary=None, brief=None, blurb=None,
                                         tracker=None, groups={}, categories={})
        self.assertEqual(packet["status_source"], "README")
        self.assertIn("hello", packet["status_excerpt"])


class TestModelsStorage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "logs"))
        self._orig = (models.INFLIGHT_PATH, models.RESUME_HISTORY_PATH, models.APP_LOG_PATH,
                      models.COMPONENTS_PATH)
        models.INFLIGHT_PATH = os.path.join(self.tmp, "inflight.json")
        models.RESUME_HISTORY_PATH = os.path.join(self.tmp, "resume.json")
        models.APP_LOG_PATH = os.path.join(self.tmp, "logs", "app.log")
        models.COMPONENTS_PATH = os.path.join(self.tmp, "components.json")

    def tearDown(self):
        (models.INFLIGHT_PATH, models.RESUME_HISTORY_PATH, models.APP_LOG_PATH,
         models.COMPONENTS_PATH) = self._orig

    def test_inflight_round_trip(self):
        self.assertIsNone(models.get_inflight())
        models.save_inflight({"items": [], "gathered_at": "x"})
        self.assertEqual(models.get_inflight()["gathered_at"], "x")

    def test_resume_history_counts(self):
        models.record_resume("chris/demo")
        models.record_resume("chris/demo")
        h = models.get_resume_history()["chris/demo"]
        self.assertEqual(h["count"], 2)
        self.assertTrue(h["last_resumed_at"])

    def test_components_round_trip(self):
        self.assertIsNone(models.get_components())
        models.save_components({"scanned_at": "x", "detections": {"a/b": {}}})
        self.assertEqual(models.get_components()["scanned_at"], "x")

    def test_tail_app_log(self):
        self.assertEqual(models.tail_app_log(), [])
        with open(models.APP_LOG_PATH, "w") as f:
            f.write("line1\nline2\nline3\n")
        self.assertEqual(models.tail_app_log(2), ["line2", "line3"])


class TestRoutes(unittest.TestCase):

    def setUp(self):
        import app as app_module
        self.m = app_module
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self._orig = (app_module._scan_results, app_module._github_client, app_module._credentials)
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "logs"))
        self._paths = (models.INFLIGHT_PATH, models.INFLIGHT_LOG_PATH, models.RESUME_HISTORY_PATH,
                       models.RESUME_LOG_PATH, models.COMPONENTS_PATH, models.COMPONENTS_LOG_PATH)
        models.INFLIGHT_PATH = os.path.join(self.tmp, "inflight.json")
        models.INFLIGHT_LOG_PATH = os.path.join(self.tmp, "logs", "inflight.log")
        models.RESUME_HISTORY_PATH = os.path.join(self.tmp, "resume.json")
        models.RESUME_LOG_PATH = os.path.join(self.tmp, "logs", "resume.log")
        models.COMPONENTS_PATH = os.path.join(self.tmp, "components.json")
        models.COMPONENTS_LOG_PATH = os.path.join(self.tmp, "logs", "components.log")

    def tearDown(self):
        (self.m._scan_results, self.m._github_client, self.m._credentials) = self._orig
        (models.INFLIGHT_PATH, models.INFLIGHT_LOG_PATH, models.RESUME_HISTORY_PATH,
         models.RESUME_LOG_PATH, models.COMPONENTS_PATH, models.COMPONENTS_LOG_PATH) = self._paths

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "chris"

    def _scan(self, **kw):
        self.m._scan_results = {"repos": [_repo(**kw)],
                                "scanned_at": datetime.datetime.now(UTC).isoformat()}

    # --- In Flight -----------------------------------------------------------

    def test_inflight_requires_auth(self):
        self.assertEqual(self.client.get("/inflight").status_code, 302)
        self.assertEqual(self.client.post("/inflight/refresh").status_code, 302)

    def test_inflight_no_scan_onboarding(self):
        self._login()
        self.m._scan_results = None
        resp = self.client.get("/inflight")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"No scan data yet", resp.data)

    def test_inflight_no_cache_prompts_read(self):
        self._login()
        self._scan()
        self.m._github_client = MagicMock()
        resp = self.client.get("/inflight")
        self.assertIn(b"Nothing read yet", resp.data)
        self.assertIn(b"READ FROM GITHUB", resp.data)
        self.assertIn(b"In flight", resp.data)  # subnav present

    def test_inflight_refresh_gathers_caches_and_renders(self):
        self._login()
        self._scan()
        self.m._github_client = FakeClient(
            ["main", "claude/add-foo-abc123"], [_pr(5, "claude/add-foo-abc123")],
            {"claude/add-foo-abc123": {"ahead_by": 1, "behind_by": 0,
                                       "commits": [_commit("a", "Add foo", "2026-10-24T01:00:00Z")]}})
        resp = self.client.post("/inflight/refresh", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"In flight refreshed: 1 unmerged branch across 1 repo", resp.data)
        self.assertIn(b"READY TO MERGE", resp.data)
        self.assertIn(b"PR #5", resp.data)
        self.assertIn(b"COPY FOR CLAUDE", resp.data)
        self.assertIsNotNone(models.get_inflight())
        self.assertEqual(models.tail_inflight_log()[-1]["event"], "refresh")
        # Kind filter + export use the cache.
        self.assertIn(b"Nothing in this bucket", self.client.get("/inflight?kind=stale").data)
        exp = self.client.get("/inflight/export.md")
        self.assertEqual(exp.status_code, 200)
        self.assertIn(b"# Work in flight", exp.data)

    def test_inflight_refresh_not_connected(self):
        self._login()
        self._scan()
        self.m._github_client = None
        resp = self.client.post("/inflight/refresh", follow_redirects=True)
        self.assertIn(b"Not connected to GitHub", resp.data)

    # --- Resume --------------------------------------------------------------

    def test_resume_index_lists_repos_with_open_counts(self):
        self._login()
        self._scan()
        models.save_inflight({"items": [{"repo": "demo", "kind": "merge"}], "gathered_at": "x"})
        resp = self.client.get("/resume")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Resume a project", resp.data)
        self.assertIn(b"1 open", resp.data)
        self.assertIn(b"/resume/chris/demo", resp.data)

    def test_resume_view_unknown_repo_redirects(self):
        self._login()
        self._scan()
        self.m._github_client = MagicMock()
        resp = self.client.get("/resume/chris/ghost", follow_redirects=True)
        self.assertIn(b"Repo not found", resp.data)

    def test_resume_view_renders_packet_and_records_history(self):
        self._login()
        self._scan()
        self.m._github_client = FakeClient(["main"], [], {})
        fetched = {"docs": {"project_status": "# Status\n\nNearly done.",
                            "session_notes": "## Session 1\nhello"}, "readme": ""}
        with patch.object(resume.gh, "fetch_repo_docs", return_value=fetched), \
             patch.object(resume.sessions, "gather_sessions", return_value=[]):
            resp = self.client.get("/resume/chris/demo")
            self.assertEqual(resp.status_code, 200)
            self.assertIn(b"Resume: demo", resp.data)
            self.assertIn(b"Nearly done.", resp.data)
            self.assertIn(b"Suggested first prompt", resp.data)
            self.assertIn(b"COPY FOR CLAUDE CODE", resp.data)
            exp = self.client.get("/resume/chris/demo/export.md")
        self.assertEqual(exp.status_code, 200)
        self.assertIn(b"# Resume: demo", exp.data)
        self.assertEqual(models.get_resume_history()["chris/demo"]["count"], 1)
        self.assertEqual(models.tail_app_log and True, True)
        with open(models.RESUME_LOG_PATH) as f:
            self.assertIn('"event": "build"', f.read())

    # --- Components routes ---------------------------------------------------

    def test_components_view_and_scan(self):
        self._login()
        self._scan()
        client = MagicMock()
        client.get_all_file_paths.return_value = ["tests/test_app.py", "CLAUDE.md", "security.py"]
        self.m._github_client = client
        resp = self.client.get("/components")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post("/components/scan", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Components scanned: 1 repos", resp.data)
        self.assertIsNotNone(models.get_components())
        exp = self.client.get("/components/export.md")
        self.assertIn(b"# Component Inventory", exp.data)

    # --- Cross-cutting -------------------------------------------------------

    def test_nav_has_components_and_subnav_on_next_up(self):
        self._login()
        self._scan()
        self.m._github_client = None  # not-connected state still shows nav + subnav
        resp = self.client.get("/yesterday")
        self.assertIn(b'href="/components"', resp.data)
        self.assertIn(b"Recent sessions", resp.data)
        self.assertIn(b"Resume a project", resp.data)

    def test_stale_scan_banner(self):
        self._login()
        old = (datetime.datetime.now(UTC) - datetime.timedelta(days=5)).isoformat()
        self.m._scan_results = {"repos": [_repo()], "scanned_at": old}
        self.m._github_client = MagicMock()
        resp = self.client.get("/resume")
        self.assertIn(b"scan is 5 days old", resp.data)
        # Dashboard already shows the scan time + RE-SCAN button; no banner there.
        self.assertNotIn(b"scan is 5 days old", self.client.get("/").data)
        self.assertEqual(self.m._scan_age_days(), 5)

    def test_reconnect_banner_links_to_login_with_next(self):
        self._login()
        self._scan()
        self.m._github_client = None
        resp = self.client.get("/resume")
        self.assertIn(b"enter your password to reconnect", resp.data)
        self.assertIn(b"/login?next=", resp.data)

    def test_safe_next(self):
        with self.m.app.test_request_context("/login?next=/resume?group=x"):
            self.assertEqual(self.m._safe_next(), "/resume?group=x")
        with self.m.app.test_request_context("/login?next=//evil.com"):
            self.assertIsNone(self.m._safe_next())
        with self.m.app.test_request_context("/login?next=https://evil.com/x"):
            self.assertIsNone(self.m._safe_next())
        with self.m.app.test_request_context("/login"):
            self.assertIsNone(self.m._safe_next())

    def test_app_log_handler_installed_once(self):
        import logging
        root = logging.getLogger()
        handlers = [h for h in root.handlers if getattr(h, "_repodoctor_app_log", False)]
        self.assertEqual(len(handlers), 1)
        self.m._configure_file_logging()
        handlers = [h for h in root.handlers if getattr(h, "_repodoctor_app_log", False)]
        self.assertEqual(len(handlers), 1)


if __name__ == "__main__":
    unittest.main()
