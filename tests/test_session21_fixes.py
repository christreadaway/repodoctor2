"""
Regression tests for the session-21 reviewer findings (sessions, briefing,
program, tracker validator, firestore/docs auth handling, spec_cleaner,
project_mapper). Each test names the bug it pins down.
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import briefing
import docs_explorer
import firestore_detector
import github_client as gh
import models
import program
import project_mapper
import sessions
import spec_cleaner
import tracker_data

UTC = datetime.timezone.utc
IN = "2026-10-20T18:00:00Z"
OLD = "2026-10-01T18:00:00Z"


def _window():
    s = datetime.datetime(2026, 10, 20, 5, tzinfo=UTC)
    return sessions.Window(s, s + datetime.timedelta(days=1), "test", "date:2026-10-20", "date")


def _commit(sha, msg, date, parents=1):
    return {"sha": sha,
            "commit": {"message": msg, "author": {"name": "C", "date": date},
                       "committer": {"name": "C", "date": date}},
            "parents": [{} for _ in range(parents)]}


def _repo(**kw):
    base = {"owner": "o", "name": "r", "default_branch": "main", "pushed_at": IN}
    base.update(kw)
    return base


def _client(pulls=(), default_commits=(), pr_commits=None):
    c = MagicMock()
    c.get_branches.return_value = [{"name": "main"}]
    c.get_pulls.return_value = list(pulls)
    c.get_pull_commits.side_effect = lambda o, n, num: (pr_commits or {}).get(num, [])
    c.compare_branches.return_value = {"ahead_by": 1, "behind_by": 0, "commits": []}
    c.get_commits_since.return_value = list(default_commits)
    return c


# --- 1. stale scan snapshot must not prune repos ------------------------------

class TestStaleScanPrune(unittest.TestCase):

    def setUp(self):
        self.w = _window()
        self.stale = _repo(pushed_at=OLD)  # snapshot says "not pushed since Oct 1"
        self.commits = [_commit("c1", "real work", IN)]

    def test_unknown_scan_time_never_prunes(self):
        out = sessions.gather_sessions(_client(default_commits=self.commits), [self.stale], self.w)
        self.assertEqual(len(out), 1)

    def test_scan_older_than_window_end_does_not_prune(self):
        out = sessions.gather_sessions(_client(default_commits=self.commits), [self.stale], self.w,
                                       scanned_at="2026-10-20T12:00:00Z")  # mid-window
        self.assertEqual(len(out), 1)

    def test_scan_after_window_end_prunes(self):
        c = _client(default_commits=self.commits)
        out = sessions.gather_sessions(c, [self.stale], self.w, scanned_at="2026-10-21T06:00:00+00:00")
        self.assertEqual(out, [])
        c.get_branches.assert_not_called()

    def test_unparseable_scan_time_does_not_prune(self):
        self.assertTrue(sessions._repo_touched_since(self.stale, self.w.since, self.w.until, "garbage"))

    def test_datetime_scan_time_accepted(self):
        after = datetime.datetime(2026, 10, 22, tzinfo=UTC)
        self.assertFalse(sessions._repo_touched_since(self.stale, self.w.since, self.w.until, after))


# --- 2. refusal/truncation must raise, not return an empty dict --------------

class TestGeneratorsRaiseOnRefusal(unittest.TestCase):

    def _boom(self, *a, **k):
        raise ValueError("The AI declined this request (safety refusal).")

    def test_generate_brief_raises(self):
        with patch.object(briefing, "request_json", self._boom), \
             patch.object(briefing.anthropic, "Anthropic", MagicMock()):
            with self.assertRaises(ValueError):
                briefing.generate_brief("k", "r", "ctx")

    def test_summarize_session_raises(self):
        with patch.object(sessions, "request_json", self._boom), \
             patch.object(sessions.anthropic, "Anthropic", MagicMock()):
            with self.assertRaises(ValueError):
                sessions.summarize_session("k", {"repo": "r", "branch": "b", "topic": "t",
                                                 "next_kind": "merge", "next_text": "x",
                                                 "commits": []})

    def test_generate_program_brief_raises(self):
        with patch.object(program, "request_json", self._boom), \
             patch.object(program.anthropic, "Anthropic", MagicMock()):
            with self.assertRaises(ValueError):
                program.generate_program_brief("k", "Prog", "ctx")


class TestRoutesKeepGoodOutputOnRefusal(unittest.TestCase):
    """End to end through the real Flask routes: a refusal must leave the
    existing brief / recap untouched (the routes catch and return 502)."""

    def setUp(self):
        import app as app_module
        self.app_module = app_module
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self._orig = (app_module._scan_results, app_module._github_client,
                      app_module._credentials)
        self._paths = (models.BRIEFS_PATH, models.BRIEFING_LOG_PATH,
                       models.SESSION_RECAPS_PATH, models.SESSIONS_LOG_PATH)
        tmp = tempfile.mkdtemp()
        models.BRIEFS_PATH = os.path.join(tmp, "briefs.json")
        models.BRIEFING_LOG_PATH = os.path.join(tmp, "briefing.log")
        models.SESSION_RECAPS_PATH = os.path.join(tmp, "recaps.json")
        models.SESSIONS_LOG_PATH = os.path.join(tmp, "sessions.log")
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["github_user"] = "c"
        app_module._scan_results = {"repos": [_repo(name="demo")]}
        app_module._github_client = MagicMock()
        app_module._credentials = {"anthropic_key": "k"}

    def tearDown(self):
        (self.app_module._scan_results, self.app_module._github_client,
         self.app_module._credentials) = self._orig
        (models.BRIEFS_PATH, models.BRIEFING_LOG_PATH,
         models.SESSION_RECAPS_PATH, models.SESSIONS_LOG_PATH) = self._paths

    def _boom(self, *a, **k):
        raise ValueError("refused")

    def test_brief_route_keeps_existing(self):
        models.save_brief("demo", {"what_it_is": "GOOD", "stage": "Build"})
        with patch.object(briefing, "gather_brief_inputs", return_value="ctx"), \
             patch.object(briefing, "request_json", self._boom), \
             patch.object(briefing.anthropic, "Anthropic", MagicMock()):
            resp = self.client.post("/api/briefing/generate/demo", json={"force": True})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(models.get_briefs()["demo"]["what_it_is"], "GOOD")

    def test_recap_route_keeps_existing(self):
        key = "demo::claude/x-abc123::yesterday"
        models.save_session_recap(key, {"headline": "GOOD", "_source_sha": "old"})
        fake = {"recap_key": key, "last_sha": "new", "repo": "demo",
                "branch": "claude/x-abc123", "topic": "x", "next_kind": "merge",
                "next_text": "merge it", "commits": []}
        with patch.object(sessions, "gather_one_session", return_value=fake), \
             patch.object(sessions, "request_json", self._boom), \
             patch.object(sessions.anthropic, "Anthropic", MagicMock()):
            tok = sessions.session_token("demo", "claude/x-abc123")
            resp = self.client.post(f"/api/yesterday/summarize/{tok}", json={"range": "yesterday"})
        self.assertEqual(resp.status_code, 502)
        stored = models.get_session_recaps()[key]
        self.assertEqual(stored["headline"], "GOOD")
        self.assertEqual(stored["_source_sha"], "old")


# --- 3. closed-unmerged PR in window ------------------------------------------

class TestClosedPrInWindow(unittest.TestCase):

    def test_closed_at_carried_and_session_found(self):
        pr = {"number": 7, "title": "Try X", "state": "closed", "merged_at": None,
              "closed_at": IN, "created_at": OLD, "updated_at": IN,
              "head": {"ref": "claude/try-x-abc123", "sha": "h"}, "base": {"ref": "main"}}
        self.assertEqual(sessions._pr_view(pr)["closed_at"], IN)
        c = _client(pulls=[pr], pr_commits={7: [_commit("a1", "wip", OLD)]})
        out = sessions.gather_sessions(c, [_repo()], _window())
        self.assertEqual([s["next_kind"] for s in out], ["decide"])


# --- 4. squash/rebase merges not double counted -------------------------------

class TestSquashMergeNotDoubleCounted(unittest.TestCase):

    def _pr(self, **kw):
        pr = {"number": 8, "title": "Add Y", "state": "closed", "merged_at": IN,
              "closed_at": IN, "created_at": IN, "updated_at": IN,
              "head": {"ref": "claude/add-y-abc123", "sha": "h2"}, "base": {"ref": "main"}}
        pr.update(kw)
        return pr

    def _pc(self):
        return {8: [_commit("b1", "Add Y", IN)]}

    def test_squash_title_suffix_excluded(self):
        c = _client(pulls=[self._pr()], pr_commits=self._pc(),
                    default_commits=[_commit("SQ", "Add Y (#8)", IN)])
        out = sessions.gather_sessions(c, [_repo()], _window())
        self.assertEqual([s["branch"] for s in out], ["claude/add-y-abc123"])

    def test_merge_commit_sha_excluded(self):
        c = _client(pulls=[self._pr(merge_commit_sha="RB")], pr_commits=self._pc(),
                    default_commits=[_commit("RB", "Add Y rebased", IN)])
        out = sessions.gather_sessions(c, [_repo()], _window())
        self.assertEqual([s["branch"] for s in out], ["claude/add-y-abc123"])

    def test_unrelated_pr_number_kept(self):
        c = _client(pulls=[self._pr()], pr_commits=self._pc(),
                    default_commits=[_commit("Z", "Old thing (#3)", IN)])
        out = sessions.gather_sessions(c, [_repo()], _window())
        self.assertIn("main", [s["branch"] for s in out])

    def test_gather_one_default_matches_page(self):
        c = _client(pulls=[self._pr()], pr_commits=self._pc(),
                    default_commits=[_commit("SQ", "Add Y (#8)", IN), _commit("D", "hotfix", IN)])
        page = [s for s in sessions.gather_sessions(c, [_repo()], _window()) if s["is_default"]][0]
        one = sessions.gather_one_session(c, _repo(), "main", _window())
        self.assertEqual(one["last_sha"], page["last_sha"])
        self.assertEqual([x["sha"] for x in one["commits"]], ["D"])


# --- 5. tzdata pinned ---------------------------------------------------------

class TestTzdataRequirement(unittest.TestCase):

    def test_tzdata_in_requirements(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "requirements.txt")
        with open(path) as f:
            self.assertIn("tzdata", f.read())


# --- 6. tracker validator: unhashable model output ----------------------------

class TestTrackerUnhashable(unittest.TestCase):

    def test_list_and_dict_ids_and_refs_report_errors(self):
        t = {
            "modules": [{"id": ["M1"], "status": "functional", "priority": "P0", "routes": []}],
            "features": [{"id": "F1", "build_priority": "P0", "roll_priority": "P0",
                          "status": tracker_data.FEATURE_STATUSES[0], "modules": [{"x": 1}]}],
            "infra_gaps": [{"id": "I1", "priority": "P0", "blocks": [["M1"]]}],
            "next_actions": [
                {"id": {"n": 1}, "effort": "S", "priority": "P0", "prompt": "x" * 60,
                 "related_ids": [["M1"]], "depends_on": [{"a": 1}]},
                {"id": "N2", "effort": "S", "priority": "P0", "prompt": "x" * 60,
                 "related_ids": 5, "depends_on": "N2"},
            ],
            "recent_changes": [{"date": "2026-01-01", "kind": "fix", "title": "t",
                                "related_ids": [{"a": 1}]}],
        }
        errors = tracker_data.validate_tracker(t)  # must not raise TypeError
        joined = "\n".join(errors)
        self.assertIn("modules: bad ID", joined)
        self.assertIn("next_actions: bad ID", joined)
        self.assertIn("related_ids entry", joined)
        self.assertIn("depends_on entry", joined)
        self.assertIn("blocks entry", joined)
        self.assertIn("modules entry", joined)
        self.assertIn("related_ids must be a list", joined)


# --- 7. GitHubAuthError not swallowed -----------------------------------------

class TestAuthErrorPropagates(unittest.TestCase):

    def test_firestore_reraises_auth(self):
        c = MagicMock()
        c.get_all_file_paths.side_effect = gh.GitHubAuthError("dead PAT")
        with self.assertRaises(gh.GitHubAuthError):
            firestore_detector.detect_firestore_status(c, "o", "r", "main")

    def test_firestore_tree_failure_not_reported_as_not_using(self):
        c = MagicMock()
        c.get_all_file_paths.side_effect = RuntimeError("502")
        with self.assertRaises(RuntimeError):
            firestore_detector.detect_firestore_status(c, "o", "r", "main")

    def test_docs_reraises_auth(self):
        c = MagicMock()
        c.get_all_file_paths.side_effect = gh.GitHubAuthError("dead PAT")
        with self.assertRaises(gh.GitHubAuthError):
            docs_explorer.list_md_files(c, "o", "r", "main")

    def test_docs_transient_still_empty(self):
        c = MagicMock()
        c.get_all_file_paths.side_effect = RuntimeError("boom")
        self.assertEqual(docs_explorer.list_md_files(c, "o", "r", "main"), [])


# --- 8/9. spec_cleaner ---------------------------------------------------------

class TestSpecCleaner(unittest.TestCase):

    def test_image_removed_not_bang_alt(self):
        out = spec_cleaner.clean_markdown("See ![logo](img.png) and [docs](http://x)")
        self.assertNotIn("!", out)
        self.assertNotIn("logo", out)
        self.assertIn("docs", out)

    def test_next_js_prose_not_roadmap(self):
        spec = "Built with Next.js and Flask.\n- uses React\n- deployed on Netlify\n## Other"
        self.assertEqual(spec_cleaner.extract_whats_next({"PRODUCT_SPEC": spec}), [])

    def test_roadmap_heading_still_found(self):
        spec = ("# App\nBuilt with Next.js\n- uses React\n## Roadmap\n- Add billing page\n"
                "- Ship mobile app\n## Other\n- nope")
        self.assertEqual(spec_cleaner.extract_whats_next({"PRODUCT_SPEC": spec}),
                         ["Add billing page", "Ship mobile app"])

    def test_next_steps_heading_found(self):
        spec = "### Next Steps\n- Add billing page\n"
        self.assertEqual(spec_cleaner.extract_whats_next({"PRODUCT_SPEC": spec}),
                         ["Add billing page"])


# --- 10. briefing action sort --------------------------------------------------

class TestOpenActionSort(unittest.TestCase):

    def test_mixed_and_none_ids(self):
        tracker = {"next_actions": [
            {"id": "N2", "priority": "P1", "status": "todo"},
            {"id": None, "priority": "P1", "status": "todo"},
            {"id": 3, "priority": "P1", "status": "todo"},
            {"id": "N1", "priority": "P0", "status": "todo"},
            {"id": "N9", "priority": ["P0"], "status": "todo"},
        ]}
        out = briefing.open_tracker_actions(tracker)  # must not raise TypeError
        self.assertEqual(out[0]["id"], "N1")
        self.assertEqual(len(out), 5)


# --- 11. project_mapper --------------------------------------------------------

class TestProjectMapper(unittest.TestCase):

    REPOS = ["parentpoint", "parentpointedu", "desmond"]

    def test_longest_repo_wins(self):
        self.assertEqual(project_mapper.match_project_to_repo("ParentPoint EDU", self.REPOS)[0],
                         "parentpointedu")
        self.assertEqual(project_mapper.match_project_to_repo("ParentPoint - Mobile", self.REPOS)[0],
                         "parentpoint")
        self.assertEqual(project_mapper.match_project_to_repo("Parent Point Edu v2", self.REPOS)[0],
                         "parentpointedu")

    def test_mapping_uses_shared_matcher(self):
        convs = [{"id": "1", "project": "ParentPoint EDU", "name": "x", "excerpt": "", "date": ""}]
        with patch.object(project_mapper, "_load_config", return_value={"mappings": {}, "dismissed": []}):
            res = project_mapper.map_conversations_to_repos(convs, self.REPOS)
        self.assertEqual(res["auto_matched"][0]["matched_repo"], "parentpointedu")

    def test_timeline_includes_auto_matched(self):
        convs = [
            {"id": "1", "project": "ParentPoint EDU", "date": "2026-01-02"},
            {"id": "2", "project": "ParentPoint", "date": "2026-01-01"},
            {"id": "3", "project": "Desmond", "date": "2026-01-03"},  # manual -> parentpoint
            {"id": "4", "project": "parent-point-edu", "date": "2026-01-04"},
        ]
        cfg = {"mappings": {"3": "parentpoint"}, "dismissed": []}
        with patch.object(project_mapper, "get_conversations", return_value=convs), \
             patch.object(project_mapper, "_load_config", return_value=cfg):
            edu = project_mapper.get_conversations_for_repo("parentpointedu", self.REPOS)
            pp = project_mapper.get_conversations_for_repo("parentpoint", self.REPOS)
            pp_no_list = project_mapper.get_conversations_for_repo("parentpoint")
        self.assertEqual([c["id"] for c in edu], ["4", "1"])
        self.assertEqual([c["id"] for c in pp], ["3", "2"])
        # Without the repo list, only exact (normalized) names match: never
        # misattributes "ParentPoint EDU" to parentpoint.
        self.assertEqual([c["id"] for c in pp_no_list], ["3", "2"])


if __name__ == "__main__":
    unittest.main()
