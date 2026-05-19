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

    def test_next_action_related_ids_accepts_questions(self):
        """An N can reference a Q (action answers question) — lenient
        from PRD's strict M/F/I read; matches model output in practice."""
        t = _valid_tracker()
        t["next_actions"][0]["related_ids"] = ["Q1"]
        errs = td.validate_tracker(t)
        self.assertFalse(any("Q1" in e for e in errs))

    def test_depends_on_accepts_non_action_ids(self):
        """An N may depend on an I being fixed or an M being built, not
        just another N. Cycle detection still only walks N→N edges."""
        t = _valid_tracker()
        t["next_actions"][0]["depends_on"] = ["I1", "M1"]
        errs = td.validate_tracker(t)
        self.assertFalse(any("depends_on" in e for e in errs))

    def test_depends_on_rejects_unknown_id(self):
        t = _valid_tracker()
        t["next_actions"][0]["depends_on"] = ["X999"]
        errs = td.validate_tracker(t)
        self.assertTrue(any("X999" in e for e in errs))

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

    def test_recent_changes_sort_order_auto_fixed(self):
        """Validation no longer rejects out-of-order recent_changes.
        sort_recent_changes() normalises them newest-first before save."""
        t = _valid_tracker()
        t["recent_changes"] = [
            {"date": "2026-05-01", "title": "older", "kind": "fix",
             "related_ids": [], "description": "d"},
            {"date": "2026-05-19", "title": "newer", "kind": "shipped",
             "related_ids": [], "description": "d"},
        ]
        # Validation passes even out-of-order.
        errs = td.validate_tracker(t)
        self.assertFalse(any("order" in e for e in errs))
        # Sort flips it.
        td.sort_recent_changes(t)
        self.assertEqual(t["recent_changes"][0]["title"], "newer")
        self.assertEqual(t["recent_changes"][1]["title"], "older")

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


class TestStorageRoundTrip(unittest.TestCase):
    """Cover the per-repo tracker JSON storage helpers in models.py.
    Runs in a tempdir so it doesn't pollute the real data/ dir."""

    def setUp(self):
        import models
        self.models = models
        self._tmp = tempfile.mkdtemp()
        self._orig_data_dir = models.DATA_DIR
        self._orig_trackers = models.TRACKERS_DIR
        self._orig_log = models.TRACKER_LOG_PATH
        models.DATA_DIR = os.path.join(self._tmp, "data")
        models.TRACKERS_DIR = os.path.join(models.DATA_DIR, "trackers")
        models.TRACKER_LOG_PATH = os.path.join(models.DATA_DIR, "logs", "tracker.log")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        self.models.DATA_DIR = self._orig_data_dir
        self.models.TRACKERS_DIR = self._orig_trackers
        self.models.TRACKER_LOG_PATH = self._orig_log

    def test_save_and_get_roundtrip(self):
        t = _valid_tracker()
        self.models.save_tracker("alice", "demo", t)
        loaded = self.models.get_tracker("alice", "demo")
        self.assertEqual(loaded, t)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.models.get_tracker("nobody", "noproj"))

    def test_list_trackers(self):
        self.models.save_tracker("a", "r1", _valid_tracker())
        self.models.save_tracker("b", "r2", _valid_tracker())
        listed = self.models.list_trackers()
        self.assertIn("a/r1", listed)
        self.assertIn("b/r2", listed)

    def test_list_trackers_skips_corrupt_files(self):
        # Write a junk file into the trackers dir; list shouldn't crash.
        os.makedirs(self.models.TRACKERS_DIR, exist_ok=True)
        with open(os.path.join(self.models.TRACKERS_DIR, "x__y.json"), "w") as f:
            f.write("{not json")
        listed = self.models.list_trackers()
        self.assertIsInstance(listed, dict)

    def test_delete_tracker(self):
        self.models.save_tracker("a", "r", _valid_tracker())
        self.assertTrue(self.models.delete_tracker("a", "r"))
        self.assertIsNone(self.models.get_tracker("a", "r"))
        self.assertFalse(self.models.delete_tracker("a", "r"))

    def test_log_event_roundtrip(self):
        self.models.log_tracker_event("smoke", n=42)
        events = self.models.tail_tracker_log(10)
        self.assertGreater(len(events), 0)
        self.assertEqual(events[-1]["event"], "smoke")
        self.assertEqual(events[-1]["n"], 42)


class TestPathSafety(unittest.TestCase):
    """`_tracker_path` should never escape the trackers dir even when
    given hostile owner/repo segments. Flask URL routing strips `/` from
    segments by default but defense-in-depth is cheap."""

    def test_dotdot_paths_dont_escape(self):
        import models
        p = models._tracker_path("../etc", "passwd")
        # The constructed path stays under the trackers dir.
        self.assertTrue(p.startswith(models.TRACKERS_DIR + os.sep))
        # The dotted segment doesn't survive verbatim.
        self.assertNotIn("../", p)

    def test_slash_in_segment_neutralized(self):
        import models
        p = models._tracker_path("a/b", "c")
        self.assertTrue(p.startswith(models.TRACKERS_DIR + os.sep))


class TestStatusCountsTemplateLogic(unittest.TestCase):
    """The template's status-counts block previously crashed when a
    module had an unknown status (e.g. a typoed AI response). The fix
    uses .get() with a safe fallback. This test renders the template
    with a deliberately bad status to confirm no crash."""

    def test_unknown_status_does_not_crash_template(self):
        from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
        env = Environment(loader=ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "templates",
            )),
        ]))
        env.filters['central_time'] = lambda x: x or ""
        env.globals['url_for'] = lambda *a, **k: "/u"
        env.globals['request'] = type('R', (), {'endpoint': 'tracker_view'})()
        env.globals['session'] = {"authenticated": True}
        env.globals['get_flashed_messages'] = lambda **k: []
        tpl = env.get_template("tracker.html")

        bad = _valid_tracker()
        bad["modules"][0]["status"] = "weird"
        bad["modules"].append({
            "id": "M9", "name": "Bad", "category": "C", "routes": [],
            "status": None, "priority": "P0", "notes": "",
        })

        out = tpl.render(
            mode="view",
            scan_results={"repos": [], "scanned_at": ""},
            repos=[],
            selected_owner="alice", selected_name="demo",
            tracker=bad, repo_info=None,
            validation_errors=["bad status 'weird'"],
            meta=td, preferences={"ai_model": "x"},
        )
        # No crash, output non-empty.
        self.assertGreater(len(out), 1000)


class TestFirestoreInputs(unittest.TestCase):
    """When firestore_detector reports the repo uses Firebase, the
    tracker prompt must include the detection so the AI emits E* + I*
    rows. When status='not_using', firestore stays None and the prompt
    doesn't mention it."""

    def test_firestore_included_in_prompt_when_present(self):
        try:
            import tracker_generator as tg
        except ImportError:
            self.skipTest("anthropic not installed")
        prompt = tg.build_user_prompt(
            "a", "r",
            inputs={
                "docs": {}, "file_tree": [], "recent_commits": [],
                "firestore": {
                    "status": "needs_setup",
                    "project_id": "test-123",
                    "indicators": ["firebase.json present"],
                    "missing": ["firestore.rules"],
                },
            },
            prior_tracker=None,
        )
        self.assertIn("FIRESTORE", prompt)
        self.assertIn("test-123", prompt)
        self.assertIn("firestore.rules", prompt)
        self.assertIn("external_systems", prompt)

    def test_firestore_omitted_when_none(self):
        try:
            import tracker_generator as tg
        except ImportError:
            self.skipTest("anthropic not installed")
        prompt = tg.build_user_prompt(
            "a", "r",
            inputs={"docs": {}, "file_tree": [], "recent_commits": [],
                    "firestore": None},
            prior_tracker=None,
        )
        self.assertNotIn("FIRESTORE", prompt)


if __name__ == "__main__":
    unittest.main()
