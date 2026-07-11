"""Tests for the Program tab — cross-project rollup of a repo group."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import models
import program


def _repo(name, pushed="2026-01-10T00:00:00Z", owner="o"):
    return {
        "owner": owner, "name": name, "full_name": f"{owner}/{name}",
        "default_branch": "main", "private": False,
        "html_url": f"https://github.com/{owner}/{name}",
        "description": f"{name} description", "created_at": "2025-01-01T00:00:00Z",
        "updated_at": pushed, "pushed_at": pushed, "docs_updated": None,
        "total_branch_count": 1, "non_default_branch_count": 0,
        "henry_branch_count": 0, "non_henry_branch_count": 1,
        "branch_names": ["main"], "required_files": {}, "files_present": 0,
        "files_total": 5, "code_size_bytes": 100, "languages": {},
    }


class TestNormalizeProgramBrief(unittest.TestCase):
    def test_caps_and_defaults(self):
        raw = {
            "what_it_is": "  An education suite.  ",
            "architecture": "Three apps share a local LLM.",
            "stage": "Mixed",
            "stage_note": "parentpoint is Live, beacon is Building.",
            "where_we_are": "Rolling out.",
            "whats_built": [f"item {i}" for i in range(20)],
            "whats_left": ["a", "", 42, "b"],
            "open_decisions": None,
            "risks": ["local LLM not provisioned"],
        }
        brief = program.normalize_program_brief(raw)
        self.assertEqual(brief["what_it_is"], "An education suite.")
        self.assertEqual(brief["stage"], "Mixed")
        self.assertEqual(len(brief["whats_built"]), program.MAX_BUILT)
        self.assertEqual(brief["whats_left"], ["a", "b"])
        self.assertEqual(brief["open_decisions"], [])
        self.assertEqual(brief["risks"], ["local LLM not provisioned"])

    def test_bad_stage_and_non_dict(self):
        self.assertEqual(program.normalize_program_brief({"stage": "Vibing"})["stage"], "Unknown")
        self.assertEqual(program.normalize_program_brief(None)["what_it_is"], "")


class TestProgramStaleness(unittest.TestCase):
    def test_missing_brief_is_stale(self):
        self.assertTrue(program.is_program_brief_stale(None, [], []))

    def test_fresh_brief_not_stale(self):
        brief = {"_generated_at": "2026-02-01T00:00:00+00:00", "_members": ["a", "b"]}
        repos = [_repo("a", "2026-01-10T00:00:00Z"), _repo("b", "2026-01-15T00:00:00Z")]
        self.assertFalse(program.is_program_brief_stale(brief, repos, ["a", "b"]))

    def test_push_after_generation_is_stale(self):
        brief = {"_generated_at": "2026-02-01T00:00:00+00:00", "_members": ["a"]}
        repos = [_repo("a", "2026-03-01T00:00:00Z")]
        self.assertTrue(program.is_program_brief_stale(brief, repos, ["a"]))

    def test_membership_change_is_stale(self):
        brief = {"_generated_at": "2026-02-01T00:00:00+00:00", "_members": ["a"]}
        repos = [_repo("a", "2026-01-10T00:00:00Z")]
        self.assertTrue(program.is_program_brief_stale(brief, repos, ["a", "newapp"]))


class TestProgramContextAndMarkdown(unittest.TestCase):
    def test_context_includes_notes_briefs_and_fallbacks(self):
        members = program.assemble_members(
            [_repo("parentpoint"), _repo("beacon")],
            briefs={"parentpoint": {
                "what_it_is": "Parent communication app.", "stage": "Live",
                "stage_note": "", "where_we_are": "", "stack": "",
                "whats_built": ["Messaging"], "whats_left": ["Billing"],
                "open_decisions": [],
            }},
            summaries={"beacon": {"what_it_does": "Beacon does X.",
                                  "how_finished": "Early", "next_steps": ["Ship MVP"]}},
            trackers={},
        )
        ctx = program.build_program_context(
            "Education", "A local LLM runs familygraph.", members)
        self.assertIn("A local LLM runs familygraph.", ctx)
        self.assertIn("PROJECT: parentpoint", ctx)
        self.assertIn("Parent communication app.", ctx)
        self.assertIn("Beacon does X.", ctx)

    def test_markdown_sections(self):
        members = program.assemble_members([_repo("parentpoint")], {}, {}, {})
        brief = program.normalize_program_brief({
            "what_it_is": "Suite.", "architecture": "Apps + local LLM.",
            "stage": "Building", "stage_note": "n", "where_we_are": "w",
            "whats_built": ["parentpoint: messaging"],
            "whats_left": ["beacon: ship MVP"],
            "open_decisions": ["pricing"], "risks": ["LLM hosting"],
        })
        md = program.compose_markdown("Education", "notes here", members, brief, "2026-07-11")
        for expected in ("# Education — Program Briefing", "notes here",
                         "How the pieces fit", "What's left (program-wide)",
                         "Cross-project risks", "parentpoint"):
            self.assertIn(expected, md)


class TestProgramRoutes(unittest.TestCase):
    def setUp(self):
        from app import app
        import app as app_module
        self.app_module = app_module
        app.config["TESTING"] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "tester"

        # Sandbox every store the program page touches.
        self.tmp = tempfile.mkdtemp()
        self._orig = {
            "GROUPS_PATH": models.GROUPS_PATH,
            "_LEGACY_GROUPS_PATH": models._LEGACY_GROUPS_PATH,
            "PROGRAM_META_PATH": models.PROGRAM_META_PATH,
            "PROGRAM_BRIEFS_PATH": models.PROGRAM_BRIEFS_PATH,
            "PROGRAM_LOG_PATH": models.PROGRAM_LOG_PATH,
            "PREFS_PATH": models.PREFS_PATH,
            "USER_DATA_DIR": models.USER_DATA_DIR,
        }
        models.GROUPS_PATH = os.path.join(self.tmp, "groups.json")
        models._LEGACY_GROUPS_PATH = os.path.join(self.tmp, "legacy_groups.json")
        models.PROGRAM_META_PATH = os.path.join(self.tmp, "program_meta.json")
        models.PROGRAM_BRIEFS_PATH = os.path.join(self.tmp, "program_briefs.json")
        models.PROGRAM_LOG_PATH = os.path.join(self.tmp, "program.log")
        models.PREFS_PATH = os.path.join(self.tmp, "prefs.json")
        models.USER_DATA_DIR = self.tmp

        self._orig_scan = self.app_module._scan_results
        self.app_module._scan_results = {
            "repos": [_repo("parentpoint"), _repo("Beacon"), _repo("unrelated")],
            "total_repos": 3, "total_branches": 3,
            "scanned_at": "2026-07-01T00:00:00Z",
        }

    def tearDown(self):
        for attr, val in self._orig.items():
            setattr(models, attr, val)
        self.app_module._scan_results = self._orig_scan
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_seeds_education_group_case_insensitively(self):
        resp = self.client.get("/program")
        self.assertEqual(resp.status_code, 200)
        groups = models.get_groups()
        self.assertIn("Education", groups)
        # "Beacon" matched the "beacon" seed despite the capital B.
        self.assertEqual(sorted(groups["Education"]), ["Beacon", "parentpoint"])
        self.assertIn("Subject Apps", groups)
        self.assertEqual(groups["Subject Apps"], [])
        # Default notes mention the local LLM pieces.
        notes = models.get_program_meta()["Education"]["notes"]
        self.assertIn("familygraph", notes)
        self.assertIn(b"parentpoint", resp.data)

    def test_deleted_group_not_resurrected(self):
        self.client.get("/program")
        models.delete_group("Education")
        self.client.get("/program")
        self.assertNotIn("Education", models.get_groups())

    def test_missing_members_listed(self):
        self.client.get("/program")  # seed
        models.set_group("Education", ["parentpoint", "teacheraide"])
        resp = self.client.get("/program?group=Education")
        self.assertEqual(resp.status_code, 200)
        # teacheraide isn't in the scan yet — surfaced, not silently dropped.
        self.assertIn(b"teacheraide", resp.data)

    def test_save_notes(self):
        self.client.get("/program")  # seed
        resp = self.client.post("/program/notes", data={
            "group": "Education",
            "notes": "beacon ships its own local LLM",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            models.get_program_meta()["Education"]["notes"],
            "beacon ships its own local LLM",
        )

    def test_notes_unknown_group_rejected(self):
        self.client.get("/program")
        resp = self.client.post("/program/notes", data={"group": "Nope", "notes": "x"})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("Nope", models.get_program_meta())

    def test_generate_saves_brief_and_members(self):
        self.client.get("/program")  # seed
        fake_brief = program.normalize_program_brief({
            "what_it_is": "Education suite.", "architecture": "Shared local LLM.",
            "stage": "Building", "stage_note": "n", "where_we_are": "w",
            "whats_built": [], "whats_left": ["ship beacon"],
            "open_decisions": [], "risks": [],
        })
        fake_brief["_usage"] = {"input_tokens": 10, "output_tokens": 5, "model": "m"}
        self.app_module._credentials = {"github_pat": "x", "anthropic_key": "k"}
        try:
            with patch.object(program, "generate_program_brief", return_value=fake_brief):
                with patch.object(self.app_module.program, "generate_program_brief",
                                  return_value=dict(fake_brief)):
                    resp = self.client.post("/program/generate", data={"group": "Education"})
            self.assertEqual(resp.status_code, 302)
            saved = models.get_program_briefs()["Education"]
            self.assertEqual(saved["what_it_is"], "Education suite.")
            self.assertEqual(saved["_members"], ["Beacon", "parentpoint"])
            self.assertIn("_generated_at", saved)
        finally:
            self.app_module._credentials = None

    def test_generate_requires_members(self):
        self.client.get("/program")  # seed
        self.app_module._credentials = {"github_pat": "x", "anthropic_key": "k"}
        try:
            resp = self.client.post("/program/generate", data={"group": "Subject Apps"},
                                    follow_redirects=True)
            self.assertIn(b"no scanned member projects", resp.data)
            self.assertNotIn("Subject Apps", models.get_program_briefs())
        finally:
            self.app_module._credentials = None


if __name__ == "__main__":
    unittest.main()
