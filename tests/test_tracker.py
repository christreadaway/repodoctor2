"""
Tests for the Codebase Tracker.

Covers the §5.5 integrity invariants from CODEBASE_TRACKER_PRD.md plus
the ID-minting / ID-stability helpers and the prompt construction.
"""

import os
import sys
import unittest
import tempfile

# Make the project root importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tracker_data as td


def _valid_tracker() -> dict:
    """Return a fully-valid tracker so individual tests can break one
    field at a time."""
    return {
        "owner": "alice", "repo": "demo",
        "generated_at": "2026-05-19T16:00:00+00:00",
        "branch_at_verification": "main",
        "ai_model": "claude-haiku-4-5-20251001",
        "modules": [
            {"id": "M1", "name": "Dashboard", "category": "Core",
             "routes": ["/"], "status": "functional", "priority": "—",
             "notes": "Main view."},
            {"id": "M2", "name": "Settings", "category": "Admin",
             "routes": ["/settings"], "status": "prototype",
             "priority": "P1", "notes": "Prefs page."},
        ],
        "infra_gaps": [
            {"id": "I1", "name": "Webhook missing",
             "blocks": ["M1", "M2"], "priority": "P0",
             "description": "Webhook endpoint not configured."},
        ],
        "features": [
            {"id": "F1", "name": "Search",
             "modules": ["M1"], "build_priority": "P0",
             "roll_priority": "P1", "take": "Foundational.",
             "spec": "docs/specs/search.md", "status": "Proposed"},
        ],
        "external_systems": [
            {"id": "E1", "name": "GitHub API",
             "what": "Repo data.", "mode": "Core",
             "migration": "PAT in env."},
        ],
        "questions": [
            {"id": "Q1", "group": "Roadmap", "text": "What's MVP?"},
        ],
        "next_actions": [
            {"id": "N1", "title": "Wire webhook",
             "related_ids": ["I1", "M1"], "why": "Unblocks M1.",
             "effort": "S", "priority": "P0",
             "prompt": "Goal: wire the webhook end-to-end. Steps: 1. Add the route. 2. Verify. Acceptance: 200 on POST.",
             "depends_on": [], "status": "todo", "status_note": ""},
        ],
        "recent_changes": [
            {"date": "2026-05-19", "title": "Shipped M1",
             "kind": "shipped", "related_ids": ["M1", "N1"],
             "description": "Dashboard live."},
        ],
        "build_sequence": ["Wire webhook", "Polish dashboard"],
        "rollout_sequence": ["Beta", "GA"],
    }


class TestEmptyAndScaffold(unittest.TestCase):

    def test_empty_tracker_validates(self):
        self.assertEqual(td.validate_tracker(td.empty_tracker("a", "b")), [])

    def test_empty_tracker_owner_repo(self):
        t = td.empty_tracker("alice", "demo")
        self.assertEqual(t["owner"], "alice")
        self.assertEqual(t["repo"], "demo")
        self.assertEqual(t["modules"], [])


class TestIDPatterns(unittest.TestCase):

    def test_each_prefix_pattern(self):
        for prefix in "MIFEQN":
            self.assertTrue(td.ID_PATTERNS[prefix].match(f"{prefix}1"))
            self.assertTrue(td.ID_PATTERNS[prefix].match(f"{prefix}999"))
            self.assertFalse(td.ID_PATTERNS[prefix].match(f"{prefix}"))
            self.assertFalse(td.ID_PATTERNS[prefix].match(f"X1"))
            self.assertFalse(td.ID_PATTERNS[prefix].match(f"{prefix}1a"))


class TestNextId(unittest.TestCase):

    def test_empty_starts_at_one(self):
        self.assertEqual(td.next_id("M", []), "M1")

    def test_skips_gaps(self):
        # Never reuse deleted numbers — pick max+1
        self.assertEqual(td.next_id("M", ["M1", "M2", "M5"]), "M6")

    def test_ignores_other_prefixes(self):
        self.assertEqual(td.next_id("M", ["I1", "F7", "M3"]), "M4")

    def test_handles_unsorted(self):
        self.assertEqual(td.next_id("N", ["N7", "N1", "N3"]), "N8")

    def test_rejects_unknown_prefix(self):
        with self.assertRaises(ValueError):
            td.next_id("Z", [])


class TestValidationPositive(unittest.TestCase):

    def test_valid_tracker_has_no_errors(self):
        self.assertEqual(td.validate_tracker(_valid_tracker()), [])


class TestValidationNegative(unittest.TestCase):

    def test_bad_module_id_format(self):
        t = _valid_tracker()
        t["modules"][0]["id"] = "X1"
        errs = td.validate_tracker(t)
        self.assertTrue(any("bad ID 'X1'" in e for e in errs))

    def test_duplicate_module_id(self):
        t = _valid_tracker()
        t["modules"][1]["id"] = "M1"
        errs = td.validate_tracker(t)
        self.assertTrue(any("duplicate ID 'M1'" in e for e in errs))

    def test_unknown_module_status(self):
        t = _valid_tracker()
        t["modules"][0]["status"] = "almost-done"
        errs = td.validate_tracker(t)
        self.assertTrue(any("almost-done" in e for e in errs))

    def test_infra_blocks_unknown_module(self):
        t = _valid_tracker()
        t["infra_gaps"][0]["blocks"] = ["M99"]
        errs = td.validate_tracker(t)
        self.assertTrue(any("blocks 'M99'" in e for e in errs))

    def test_feature_modules_unknown(self):
        t = _valid_tracker()
        t["features"][0]["modules"] = ["M99"]
        errs = td.validate_tracker(t)
        self.assertTrue(any("'M99'" in e for e in errs))

    def test_feature_status_unknown(self):
        t = _valid_tracker()
        t["features"][0]["status"] = "Maybe"
        errs = td.validate_tracker(t)
        self.assertTrue(any("Maybe" in e for e in errs))

    def test_external_system_mode_unknown(self):
        t = _valid_tracker()
        t["external_systems"][0]["mode"] = "Sideways"
        errs = td.validate_tracker(t)
        self.assertTrue(any("Sideways" in e for e in errs))

    def test_next_action_short_prompt(self):
        t = _valid_tracker()
        t["next_actions"][0]["prompt"] = "too short"
        errs = td.validate_tracker(t)
        self.assertTrue(any("prompt must be ≥50 chars" in e for e in errs))

    def test_next_action_unknown_related_id(self):
        t = _valid_tracker()
        t["next_actions"][0]["related_ids"] = ["M99"]
        errs = td.validate_tracker(t)
        self.assertTrue(any("M99" in e for e in errs))

    def test_next_action_self_dep(self):
        t = _valid_tracker()
        t["next_actions"][0]["depends_on"] = ["N1"]
        errs = td.validate_tracker(t)
        self.assertTrue(any("self-reference" in e for e in errs))

    def test_next_action_cycle(self):
        t = _valid_tracker()
        t["next_actions"] = [
            {"id": "N1", "title": "a", "related_ids": [], "why": "w",
             "effort": "S", "priority": "P0",
             "prompt": "x" * 60, "depends_on": ["N2"], "status": "todo"},
            {"id": "N2", "title": "b", "related_ids": [], "why": "w",
             "effort": "S", "priority": "P0",
             "prompt": "x" * 60, "depends_on": ["N1"], "status": "todo"},
        ]
        errs = td.validate_tracker(t)
        self.assertTrue(any("cycle" in e for e in errs))

    def test_recent_changes_bad_date(self):
        t = _valid_tracker()
        t["recent_changes"][0]["date"] = "May 19 2026"
        errs = td.validate_tracker(t)
        self.assertTrue(any("bad date" in e for e in errs))

    def test_recent_changes_out_of_order(self):
        t = _valid_tracker()
        t["recent_changes"] = [
            {"date": "2026-05-01", "title": "older", "kind": "fix",
             "related_ids": [], "description": "d"},
            {"date": "2026-05-19", "title": "newer", "kind": "shipped",
             "related_ids": [], "description": "d"},
        ]
        errs = td.validate_tracker(t)
        self.assertTrue(any("not in newest-first order" in e for e in errs))

    def test_recent_changes_unknown_kind(self):
        t = _valid_tracker()
        t["recent_changes"][0]["kind"] = "neat"
        errs = td.validate_tracker(t)
        self.assertTrue(any("neat" in e for e in errs))


class TestIDPreservation(unittest.TestCase):
    """Critical: regeneration must preserve every existing ID
    (PRD §5.1)."""

    def test_collect_existing_ids(self):
        t = _valid_tracker()
        ids = td.collect_existing_ids(t)
        self.assertIn("M1", ids["M"])
        self.assertIn("M2", ids["M"])
        self.assertIn("I1", ids["I"])
        self.assertIn("F1", ids["F"])
        self.assertIn("E1", ids["E"])
        self.assertIn("Q1", ids["Q"])
        self.assertIn("N1", ids["N"])

    def test_collect_handles_missing_keys(self):
        ids = td.collect_existing_ids({})
        for prefix in "MIFEQN":
            self.assertEqual(ids[prefix], [])


class TestPromptBuilding(unittest.TestCase):

    def test_user_prompt_includes_load_bearing_ids(self):
        # Avoid importing tracker_generator at module level so test
        # environments without anthropic can still run earlier tests.
        try:
            import tracker_generator as tg
        except ImportError:
            self.skipTest("anthropic not installed")
            return
        prompt = tg.build_user_prompt(
            "alice", "demo",
            inputs={"docs": {}, "file_tree": [], "recent_commits": []},
            prior_tracker=_valid_tracker(),
        )
        self.assertIn("M1", prompt)
        self.assertIn("M2", prompt)
        self.assertIn("LOAD-BEARING IDS", prompt)

    def test_user_prompt_no_prior_omits_load_bearing(self):
        try:
            import tracker_generator as tg
        except ImportError:
            self.skipTest("anthropic not installed")
            return
        prompt = tg.build_user_prompt(
            "alice", "demo",
            inputs={"docs": {}, "file_tree": [], "recent_commits": []},
            prior_tracker=None,
        )
        self.assertNotIn("LOAD-BEARING IDS", prompt)

    def test_user_prompt_includes_docs(self):
        try:
            import tracker_generator as tg
        except ImportError:
            self.skipTest("anthropic not installed")
            return
        prompt = tg.build_user_prompt(
            "alice", "demo",
            inputs={
                "docs": {"product_spec": "PRODUCT SPEC CONTENT"},
                "file_tree": ["app.py", "templates/x.html"],
                "recent_commits": [{"date": "2026-05-19", "title": "msg"}],
            },
            prior_tracker=None,
        )
        self.assertIn("PRODUCT SPEC CONTENT", prompt)
        self.assertIn("app.py", prompt)
        self.assertIn("2026-05-19", prompt)


if __name__ == "__main__":
    unittest.main()
