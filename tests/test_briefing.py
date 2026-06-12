"""
Tests for the Chat Briefing screen.

Covers brief normalization, staleness, tracker-action extraction, project
assembly, Markdown composition, input gathering (mocked GitHub client),
briefs storage round-trip, and the Flask routes.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

# Make the project root importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import briefing
import models


def _repo(**overrides) -> dict:
    base = {
        "owner": "alice",
        "name": "demo",
        "full_name": "alice/demo",
        "default_branch": "main",
        "private": True,
        "html_url": "https://github.com/alice/demo",
        "description": "A demo app",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        "total_branch_count": 3,
        "non_henry_branch_count": 2,
        "required_files": {
            "CLAUDE.md": True, "LICENSE": True, "PRODUCT_SPEC.md": True,
            "PROJECT_STATUS.md": False, "SESSION_NOTES.md": True,
        },
        "files_present": 4,
        "files_total": 5,
        "code_size_bytes": 2_500_000,
        "languages": {"Python": 2_000_000, "JavaScript": 400_000, "CSS": 100_000},
    }
    base.update(overrides)
    return base


def _brief(**overrides) -> dict:
    base = {
        "what_it_is": "Solves scheduling chaos for school parents.",
        "stack": "Flask app, runs locally.",
        "stage": "Building",
        "stage_note": "Core screens exist; messaging unfinished.",
        "where_we_are": "Dashboard and inbox work; send pipeline is next.",
        "whats_built": ["Parents: daily briefing", "Admins: roster import"],
        "whats_left": ["Wire send pipeline", "Add consent screen"],
        "open_decisions": ["Pricing model"],
        "constraints": ["Children's data — fail closed"],
        "_generated_at": "2026-06-05T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _tracker(**overrides) -> dict:
    base = {
        "generated_at": "2026-05-20T00:00:00+00:00",
        "next_actions": [
            {"id": "N1", "title": "Ship login", "priority": "P1", "status": "todo"},
            {"id": "N2", "title": "Done thing", "priority": "P0", "status": "shipped"},
            {"id": "N3", "title": "Fix rules", "priority": "P0", "status": "blocked",
             "status_note": "waiting on console access"},
            {"id": "N4", "title": "Old idea", "priority": "P2", "status": "dismissed"},
        ],
        "questions": [
            {"id": "Q1", "group": "Roadmap", "text": "Which Stripe model?"},
        ],
        "recent_changes": [],
        "modules": [
            {"id": "M1", "name": "Dashboard", "status": "functional"},
            {"id": "M2", "name": "Inbox", "status": "prototype"},
        ],
        "infra_gaps": [],
    }
    base.update(overrides)
    return base


class TestNormalizeBrief(unittest.TestCase):

    def test_valid_passthrough(self):
        out = briefing.normalize_brief(_brief())
        self.assertEqual(out["stage"], "Building")
        self.assertEqual(out["whats_left"], ["Wire send pipeline", "Add consent screen"])

    def test_invalid_stage_becomes_unknown(self):
        out = briefing.normalize_brief(_brief(stage="Almost done!"))
        self.assertEqual(out["stage"], briefing.UNKNOWN_STAGE)

    def test_caps_enforced(self):
        out = briefing.normalize_brief(_brief(
            whats_left=[f"item {i}" for i in range(30)],
            open_decisions=[f"d {i}" for i in range(30)],
        ))
        self.assertEqual(len(out["whats_left"]), briefing.MAX_LEFT)
        self.assertEqual(len(out["open_decisions"]), briefing.MAX_DECISIONS)

    def test_non_list_and_empty_bullets_dropped(self):
        out = briefing.normalize_brief(_brief(
            whats_built="not a list",
            whats_left=["ok", "", "  ", 42, "also ok"],
        ))
        self.assertEqual(out["whats_built"], [])
        self.assertEqual(out["whats_left"], ["ok", "also ok"])

    def test_missing_fields_defaulted(self):
        out = briefing.normalize_brief({})
        self.assertEqual(out["stage"], briefing.UNKNOWN_STAGE)
        self.assertEqual(out["what_it_is"], "")
        self.assertEqual(out["constraints"], [])

    def test_non_dict_input(self):
        out = briefing.normalize_brief(["garbage"])
        self.assertEqual(out["stage"], briefing.UNKNOWN_STAGE)


class TestStaleness(unittest.TestCase):

    def test_fresh_brief(self):
        brief = _brief(_generated_at="2026-06-10T00:00:00+00:00")
        repo = _repo(updated_at="2026-06-01T00:00:00Z")
        self.assertFalse(briefing.is_brief_stale(brief, repo))

    def test_stale_when_repo_pushed_after_generation(self):
        brief = _brief(_generated_at="2026-05-01T00:00:00+00:00")
        repo = _repo(updated_at="2026-06-01T00:00:00Z")
        self.assertTrue(briefing.is_brief_stale(brief, repo))

    def test_missing_brief_is_stale(self):
        self.assertTrue(briefing.is_brief_stale(None, _repo()))

    def test_missing_generated_at_is_stale(self):
        brief = _brief()
        brief.pop("_generated_at")
        self.assertTrue(briefing.is_brief_stale(brief, _repo()))

    def test_missing_repo_timestamp_not_stale(self):
        brief = _brief(_generated_at="2026-05-01T00:00:00+00:00")
        self.assertFalse(briefing.is_brief_stale(brief, _repo(updated_at="")))


class TestOpenTrackerActions(unittest.TestCase):

    def test_excludes_shipped_and_dismissed(self):
        actions = briefing.open_tracker_actions(_tracker())
        ids = [a["id"] for a in actions]
        self.assertNotIn("N2", ids)
        self.assertNotIn("N4", ids)

    def test_sorted_p0_first(self):
        actions = briefing.open_tracker_actions(_tracker())
        self.assertEqual(actions[0]["id"], "N3")  # P0 blocked
        self.assertEqual(actions[1]["id"], "N1")  # P1 todo

    def test_no_tracker(self):
        self.assertEqual(briefing.open_tracker_actions(None), [])


class TestAssembleProjects(unittest.TestCase):

    def test_full_assembly(self):
        projects = briefing.assemble_projects(
            [_repo()],
            briefs={"demo": _brief()},
            summaries={},
            trackers={"alice/demo": _tracker()},
            groups={"School": ["demo"], "Fun": ["other"]},
        )
        self.assertEqual(len(projects), 1)
        p = projects[0]
        self.assertEqual(p["stage"], "Building")
        self.assertEqual(p["group_names"], ["School"])
        self.assertEqual(p["missing_files"], ["PROJECT_STATUS.md"])
        self.assertEqual(p["branch_count"], 2)
        self.assertEqual(p["languages_label"], "Python, JavaScript")
        self.assertEqual(len(p["open_actions"]), 2)
        self.assertEqual(p["open_questions"], ["Which Stripe model?"])
        self.assertTrue(p["has_tracker"])
        # Brief generated 06-05, repo updated 06-01 → not stale.
        self.assertFalse(p["stale"])

    def test_fallback_without_brief_or_tracker(self):
        projects = briefing.assemble_projects(
            [_repo()], briefs={}, summaries={"demo": {"what_it_does": "Quick summary."}},
            trackers={}, groups={},
        )
        p = projects[0]
        self.assertIsNone(p["brief"])
        self.assertEqual(p["stage"], briefing.UNKNOWN_STAGE)
        self.assertFalse(p["stale"])  # nothing to be stale
        self.assertFalse(p["has_tracker"])
        self.assertEqual(p["summary"]["what_it_does"], "Quick summary.")

    def test_sorted_by_last_push_desc(self):
        projects = briefing.assemble_projects(
            [
                _repo(name="older", updated_at="2026-01-01T00:00:00Z"),
                _repo(name="newer", updated_at="2026-06-10T00:00:00Z"),
            ],
            briefs={}, summaries={}, trackers={}, groups={},
        )
        self.assertEqual([p["name"] for p in projects], ["newer", "older"])


class TestMarkdown(unittest.TestCase):

    def _projects(self):
        return briefing.assemble_projects(
            [_repo()],
            briefs={"demo": _brief()},
            summaries={},
            trackers={"alice/demo": _tracker()},
            groups={"School": ["demo"]},
        )

    def test_document_structure(self):
        md = briefing.compose_markdown(
            self._projects(), owner_login="alice", active_group="", generated_label="2026-06-12",
        )
        self.assertIn("# Portfolio Chat Briefing — 2026-06-12", md)
        self.assertIn("github.com/alice", md)
        self.assertIn("## At a Glance", md)
        self.assertIn("| demo | Building | 2026-06-01 | 2 | 4/5 |", md)
        self.assertIn("## demo — Building", md)

    def test_brief_sections_present(self):
        md = briefing.project_section_markdown(self._projects()[0])
        self.assertIn("**What it is:** Solves scheduling chaos", md)
        self.assertIn("**Stack:** Flask app", md)
        self.assertIn("**Where we are:** Core screens exist", md)
        self.assertIn("- Parents: daily briefing", md)
        self.assertIn("1. Wire send pipeline", md)
        self.assertIn("**Open decisions (owner):**", md)
        self.assertIn("**Constraints a chat session must respect:**", md)
        self.assertIn("- [P0] Fix rules (blocked) — waiting on console access", md)
        self.assertIn("- Which Stripe model?", md)
        self.assertIn("missing: PROJECT_STATUS.md", md)
        self.assertIn("2.4 MB", md)
        self.assertIn("groups: School", md)
        self.assertIn("Tracker generated 2026-05-20", md)

    def test_no_brief_fallback(self):
        projects = briefing.assemble_projects(
            [_repo()], briefs={},
            summaries={"demo": {
                "what_it_does": "Quick summary.",
                "how_finished": "About half done.",
                "next_steps": ["Do a thing"],
            }},
            trackers={}, groups={},
        )
        md = briefing.project_section_markdown(projects[0])
        self.assertIn("**What it is:** Quick summary.", md)
        self.assertIn("**Where we are:** About half done.", md)
        self.assertIn("1. Do a thing", md)
        self.assertIn("No AI brief yet", md)

    def test_stale_note_in_markdown(self):
        projects = briefing.assemble_projects(
            [_repo(updated_at="2026-06-10T00:00:00Z")],
            briefs={"demo": _brief(_generated_at="2026-06-01T00:00:00+00:00")},
            summaries={}, trackers={}, groups={},
        )
        md = briefing.project_section_markdown(projects[0])
        self.assertIn("may be stale", md)

    def test_group_scope_in_header(self):
        md = briefing.compose_markdown(
            self._projects(), owner_login="alice", active_group="School",
            generated_label="2026-06-12",
        )
        self.assertIn("(group: School)", md)


class TestGatherBriefInputs(unittest.TestCase):

    def _client(self, paths=None, contents=None):
        client = MagicMock()
        client.check_required_files.return_value = ({}, paths or {})
        contents = contents or {}

        def get_file_content(owner, name, path, ref=None):
            return contents.get(path)

        client.get_file_content.side_effect = get_file_content
        return client

    def test_includes_docs_and_tracker_facts(self):
        client = self._client(
            paths={"PRODUCT_SPEC.md": "PRODUCT_SPEC.md"},
            contents={"PRODUCT_SPEC.md": "The grand plan."},
        )
        text = briefing.gather_brief_inputs(client, _repo(), _tracker())
        self.assertIn("GitHub description: A demo app", text)
        self.assertIn("--- PRODUCT SPEC ---", text)
        self.assertIn("The grand plan.", text)
        self.assertIn("TRACKER FACTS", text)
        self.assertIn("[P0] Fix rules (blocked)", text)
        self.assertIn("Which Stripe model?", text)
        self.assertIn("Languages: Python, JavaScript, CSS", text)

    def test_readme_fallback_when_no_docs(self):
        client = self._client(paths={}, contents={"README.md": "Readme text here."})
        text = briefing.gather_brief_inputs(client, _repo(), None)
        self.assertIn("--- README ---", text)
        self.assertIn("Readme text here.", text)

    def test_doc_truncation(self):
        client = self._client(
            paths={"PRODUCT_SPEC.md": "PRODUCT_SPEC.md"},
            contents={"PRODUCT_SPEC.md": "x" * 50_000},
        )
        text = briefing.gather_brief_inputs(client, _repo(), None)
        self.assertLess(len(text), 20_000)


class TestBriefsStorage(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_briefs = models.BRIEFS_PATH
        self._orig_briefing_log = models.BRIEFING_LOG_PATH
        models.BRIEFS_PATH = os.path.join(self._tmp, "briefs.json")
        models.BRIEFING_LOG_PATH = os.path.join(self._tmp, "briefing.log")

    def tearDown(self):
        models.BRIEFS_PATH = self._orig_briefs
        models.BRIEFING_LOG_PATH = self._orig_briefing_log

    def test_round_trip_sets_generated_at(self):
        self.assertEqual(models.get_briefs(), {})
        models.save_brief("demo", _brief())
        stored = models.get_briefs()
        self.assertIn("demo", stored)
        self.assertIn("_generated_at", stored["demo"])

    def test_briefing_log_round_trip(self):
        models.log_briefing_event("generate_done", repo="demo", stage="Building")
        events = models.tail_briefing_log(10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "generate_done")
        self.assertEqual(events[0]["repo"], "demo")
        self.assertIn("ts", events[0])


class TestBriefingRoutes(unittest.TestCase):

    def setUp(self):
        import app as app_module
        self.app_module = app_module
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        self.client = app_module.app.test_client()
        self._orig_scan = app_module._scan_results

    def tearDown(self):
        self.app_module._scan_results = self._orig_scan

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "alice"

    def test_unauthenticated_redirects(self):
        for path in ("/briefing", "/briefing/export.md"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 302, path)
        resp = self.client.post("/briefing/generate")
        self.assertEqual(resp.status_code, 302)

    def test_briefing_renders_onboarding_without_scan(self):
        self._login()
        self.app_module._scan_results = None
        resp = self.client.get("/briefing")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"No scan data yet", resp.data)

    def test_briefing_renders_projects_and_markdown(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        resp = self.client.get("/briefing")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"COPY FOR CLAUDE CHAT", resp.data)
        self.assertIn(b"Portfolio Chat Briefing", resp.data)
        self.assertIn(b"demo", resp.data)

    def test_export_returns_markdown_attachment(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        resp = self.client.get("/briefing/export.md")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/markdown", resp.content_type)
        self.assertIn("portfolio-chat-briefing", resp.headers["Content-Disposition"])
        self.assertIn(b"# Portfolio Chat Briefing", resp.data)

    def test_generate_without_client_flashes_error(self):
        self._login()
        resp = self.client.post("/briefing/generate", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/briefing", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
