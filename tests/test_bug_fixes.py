"""Regression tests for bugs found in the 2026-07 comprehensive audit.

Each test pins a specific crash/correctness fix so the same defect can't
silently return. Grouped by the module the fix lives in.
"""

import ai_analyzer
import project_mapper
import tracker_data


class TestTrackerValidatorTypeCoercion:
    """validate_tracker / sort_recent_changes get raw LLM JSON, so a row field
    can be the wrong type. These must REPORT the bad value, not crash — a crash
    bypasses the generator's validation-retry loop and 500s the tracker page."""

    def test_non_string_prompt_reports_error_not_crash(self):
        errs = tracker_data.validate_tracker(
            {"next_actions": [{"id": "N1", "effort": "S", "priority": "P0",
                               "status": "todo", "prompt": 123}]})
        assert isinstance(errs, list) and errs  # reported, did not raise

    def test_non_string_id_reports_error_not_crash(self):
        errs = tracker_data.validate_tracker(
            {"modules": [{"id": 7, "status": "functional",
                          "priority": "P0", "routes": []}]})
        assert isinstance(errs, list) and errs

    def test_non_string_date_reports_error_not_crash(self):
        errs = tracker_data.validate_tracker(
            {"recent_changes": [{"date": 20260519, "kind": "fix", "title": "x"}]})
        assert isinstance(errs, list)

    def test_sort_recent_changes_mixed_date_types(self):
        t = {"recent_changes": [{"date": 20260519, "title": "a"},
                                {"date": "2026-05-18", "title": "b"}]}
        tracker_data.sort_recent_changes(t)  # must not raise on mixed int/str
        assert len(t["recent_changes"]) == 2

    def test_next_id_skips_non_string_ids(self):
        assert tracker_data.next_id("M", [7, "M3", None, "M5"]) == "M6"

    def test_valid_tracker_still_passes(self):
        valid = {
            "modules": [{"id": "M1", "name": "x", "status": "functional",
                         "priority": "P1", "routes": []}],
            "next_actions": [{"id": "N1", "effort": "M", "priority": "P1",
                              "status": "todo", "prompt": "x" * 60,
                              "related_ids": [], "depends_on": []}],
            "recent_changes": [{"date": "2026-05-18", "kind": "fix",
                                "title": "t", "related_ids": []}],
        }
        assert tracker_data.validate_tracker(valid) == []


class TestConversationNullProject:
    """Real Claude exports carry an explicit null project for unassigned chats.
    That null must never persist (it later crashes .lower())."""

    def test_parse_null_project_dict(self):
        conv = project_mapper._parse_conversation({"project": {"name": None}, "name": "x"})
        assert conv is not None and conv["project"] == ""

    def test_parse_null_project_name_field(self):
        conv = project_mapper._parse_conversation({"project_name": None, "name": "x"})
        assert conv["project"] == ""

    def test_parse_numeric_project_coerced_to_string(self):
        conv = project_mapper._parse_conversation({"project_name": 12345, "name": "x"})
        assert isinstance(conv["project"], str)

    def test_map_conversations_with_none_project_does_not_crash(self):
        convs = [{"id": "1", "project": None, "name": "chat",
                  "excerpt": "", "date": "2026-01-01"}]
        # Must not raise AttributeError on None.lower()
        result = project_mapper.map_conversations_to_repos(convs, ["myrepo"])
        assert isinstance(result, dict)


class TestEstimateCostPricing:
    """Pricing must be exact per model, not a substring guess that mispriced
    any non-opus/sonnet model as the cheapest tier."""

    def test_fable_priced_correctly(self):
        # 1M in + 1M out at $10/$50 per MTok = $60
        assert ai_analyzer.estimate_cost(1_000_000, 1_000_000, "claude-fable-5") == 60.0

    def test_opus_pricing(self):
        assert ai_analyzer.estimate_cost(1_000_000, 1_000_000, "claude-opus-4-8") == 30.0

    def test_sonnet_pricing(self):
        assert ai_analyzer.estimate_cost(1_000_000, 1_000_000, "claude-sonnet-5") == 18.0

    def test_haiku_pricing(self):
        assert ai_analyzer.estimate_cost(1_000_000, 1_000_000, "claude-haiku-4-5-20251001") == 6.0
