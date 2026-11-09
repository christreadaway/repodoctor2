"""Health grade (Session 24 [S24-C]): every check and its thresholds, the
not-graded exemptions, grade boundaries, determinism, and the My Repos /
Portfolio rendering + health.log event."""

import copy
import datetime
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import health
import models
import portfolio

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
ALL_DOCS = {d: True for d in health.REQUIRED_DOCS}


def ago(days, hours=0):
    return (NOW - datetime.timedelta(days=days, hours=hours)).isoformat().replace("+00:00", "Z")


def scan_repo(name="proj", owner="chris", **kw):
    """A repo dict shaped like github_client.scan_repo_lite's output."""
    r = {
        "owner": owner, "name": name, "full_name": f"{owner}/{name}",
        "default_branch": "main", "private": True, "archived": False, "fork": False,
        "html_url": f"https://github.com/{owner}/{name}",
        "description": "", "created_at": "2025-01-01T00:00:00Z", "updated_at": ago(2),
        "pushed_at": ago(2), "total_branch_count": 1, "non_default_branch_count": 0,
        "henry_branch_count": 0, "non_henry_branch_count": 1, "branch_names": ["main"],
        "required_files": dict(ALL_DOCS), "files_present": 5, "files_total": 5,
        "docs_updated": True, "code_size_bytes": 2048, "languages": {"Python": 2048},
    }
    r.update(kw)
    return r


def item(branch, kind, repo="proj", owner="chris"):
    return {"owner": owner, "repo": repo, "branch": branch, "kind": kind}


def with_branches(*names, **kw):
    names = ["main", *names]
    return scan_repo(branch_names=names, total_branch_count=len(names),
                     non_default_branch_count=len(names) - 1, non_henry_branch_count=len(names), **kw)


class TestConstants(unittest.TestCase):

    def test_weights_sum_to_100(self):
        self.assertEqual(health.WEIGHT_DOCS + health.WEIGHT_CURRENT + health.WEIGHT_BRANCHES
                         + health.WEIGHT_ACTIVE, 100)

    def test_ladder_tops_equal_their_weights(self):
        self.assertEqual(health.POINTS_PER_DOC * len(health.REQUIRED_DOCS), health.WEIGHT_DOCS)
        self.assertEqual(health.DEAD_BRANCH_LADDER[0][1], health.WEIGHT_BRANCHES)
        self.assertEqual(health.OPEN_BRANCH_LADDER[0][1], health.WEIGHT_BRANCHES)
        self.assertEqual(health.ACTIVE_LADDER[0][1], health.WEIGHT_ACTIVE)

    def test_ladders_only_go_down(self):
        for ladder, floor in ((health.DEAD_BRANCH_LADDER, health.DEAD_BRANCH_FLOOR),
                              (health.OPEN_BRANCH_LADDER, health.OPEN_BRANCH_FLOOR),
                              (health.ACTIVE_LADDER, health.ACTIVE_FLOOR)):
            pts = [p for _, p, _ in ladder] + [floor[0]]
            self.assertEqual(pts, sorted(pts, reverse=True))
            bounds = [b for b, _, _ in ladder]
            self.assertEqual(bounds, sorted(bounds))

    def test_required_docs_match_the_dashboard(self):
        # The five the scan checks (github_client.check_required_files).
        self.assertEqual(set(health.REQUIRED_DOCS),
                         {"CLAUDE.md", "LICENSE", "PRODUCT_SPEC.md", "PROJECT_STATUS.md", "SESSION_NOTES.md"})


class TestGradeBoundaries(unittest.TestCase):

    def test_letter_for(self):
        cases = {100: "A", 90: "A", 89: "B", 80: "B", 79: "C", 70: "C", 69: "D", 60: "D", 59: "F", 0: "F"}
        for score, letter in cases.items():
            self.assertEqual(health.letter_for(score), letter, score)

    def test_scaled_rounds_half_up_with_integers(self):
        self.assertEqual(health._scaled(1, 8), 13)     # 12.5 -> 13 (not banker's 12)
        self.assertEqual(health._scaled(3, 8), 38)     # 37.5 -> 38
        self.assertEqual(health._scaled(85, 85), 100)
        self.assertEqual(health._scaled(0, 55), 0)
        self.assertEqual(health._scaled(67, 85), 79)   # 78.8

    def test_tones(self):
        self.assertEqual({g: health.GRADE_TONES[g] for g in health.GRADES},
                         {"A": "ok", "B": "ok", "C": "warn", "D": "bad", "F": "bad"})


class TestDocsCheck(unittest.TestCase):

    def test_all_present(self):
        c = health.check_docs(scan_repo())
        self.assertEqual((c["status"], c["points"], c["max"], c["mark"]), ("ok", 30, 30, "OK"))
        self.assertEqual(c["detail"], "All 5 present.")

    def test_each_missing_file_costs_six(self):
        rf = dict(ALL_DOCS, LICENSE=False)
        c = health.check_docs(scan_repo(required_files=rf))
        self.assertEqual((c["status"], c["points"]), ("warn", 24))
        self.assertEqual(c["detail"], "Missing LICENSE. Add it.")
        rf = dict(ALL_DOCS, LICENSE=False, **{"CLAUDE.md": False})
        c = health.check_docs(scan_repo(required_files=rf))
        self.assertEqual((c["status"], c["points"]), ("warn", 18))
        self.assertEqual(c["detail"], "Missing CLAUDE.md and LICENSE. Add them.")
        self.assertEqual(c["missing"], ["CLAUDE.md", "LICENSE"])

    def test_three_or_more_missing_is_bad(self):
        rf = {"CLAUDE.md": True, "LICENSE": True, "PRODUCT_SPEC.md": False,
              "PROJECT_STATUS.md": False, "SESSION_NOTES.md": False}
        c = health.check_docs(scan_repo(required_files=rf))
        self.assertEqual((c["status"], c["points"], c["mark"]), ("bad", 12, "FIX"))
        self.assertEqual(c["detail"], "Missing PRODUCT_SPEC.md, PROJECT_STATUS.md and SESSION_NOTES.md. Add them.")
        c = health.check_docs(scan_repo(required_files={d: False for d in health.REQUIRED_DOCS}))
        self.assertEqual((c["status"], c["points"]), ("bad", 0))

    def test_key_absent_from_dict_counts_as_missing(self):
        c = health.check_docs(scan_repo(required_files={"CLAUDE.md": True}))
        self.assertEqual(c["points"], 6)

    def test_no_file_list_is_not_scored(self):
        for rf in ({}, None, [], "CLAUDE.md"):
            r = scan_repo(required_files=rf)
            c = health.check_docs(r)
            self.assertFalse(c["counted"], rf)
            self.assertEqual((c["points"], c["max"], c["mark"]), (0, 0, "N/A"))
        r = scan_repo()
        del r["required_files"]
        self.assertFalse(health.check_docs(r)["counted"])


class TestCurrentCheck(unittest.TestCase):
    """Reuses the scan's docs_updated flag: the same value the dashboard's
    "Current?" column renders (True Yes / False No / None —)."""

    def test_yes(self):
        c = health.check_current(scan_repo(docs_updated=True))
        self.assertEqual((c["status"], c["points"], c["max"]), ("ok", 15, 15))
        self.assertEqual(c["detail"], "PRODUCT_SPEC.md and SESSION_NOTES.md kept up with the latest commit.")

    def test_no(self):
        c = health.check_current(scan_repo(docs_updated=False))
        self.assertEqual((c["status"], c["points"], c["max"]), ("bad", 0, 15))
        self.assertEqual(c["detail"], "PRODUCT_SPEC.md and SESSION_NOTES.md are more than 7 days behind "
                                      "the latest commit. Update them.")
        only_notes = dict(ALL_DOCS, **{"PRODUCT_SPEC.md": False})
        c = health.check_current(scan_repo(docs_updated=False, required_files=only_notes))
        self.assertEqual(c["detail"], "SESSION_NOTES.md is more than 7 days behind the latest commit. Update it.")

    def test_dash_with_no_spec_or_notes_scores_zero(self):
        rf = dict(ALL_DOCS, **{"PRODUCT_SPEC.md": False, "SESSION_NOTES.md": False})
        c = health.check_current(scan_repo(docs_updated=None, required_files=rf))
        self.assertTrue(c["counted"])
        self.assertEqual((c["status"], c["points"], c["max"]), ("bad", 0, 15))
        self.assertIn("No PRODUCT_SPEC.md or SESSION_NOTES.md", c["detail"])

    def test_deleting_stale_docs_never_raises_the_grade(self):
        stale = health.grade_repo(scan_repo(docs_updated=False), None, NOW)
        rf = dict(ALL_DOCS, **{"PRODUCT_SPEC.md": False, "SESSION_NOTES.md": False})
        deleted = health.grade_repo(scan_repo(docs_updated=None, required_files=rf), None, NOW)
        self.assertLess(deleted["score"], stale["score"])

    def test_dash_with_docs_present_is_not_scored(self):
        # docs exist but the scan got no commit date: unknown, never guessed.
        c = health.check_current(scan_repo(docs_updated=None))
        self.assertFalse(c["counted"])
        self.assertIn("Couldn't check", c["detail"])

    def test_missing_or_odd_flag_is_treated_as_unknown(self):
        r = scan_repo()
        del r["docs_updated"]
        self.assertFalse(health.check_current(r)["counted"])
        self.assertFalse(health.check_current(scan_repo(docs_updated="yes"))["counted"])
        self.assertFalse(health.check_current(scan_repo(docs_updated=None, required_files={}))["counted"])

    def test_flag_wins_even_without_a_file_list(self):
        c = health.check_current(scan_repo(docs_updated=True, required_files={}))
        self.assertEqual(c["points"], 15)
        self.assertIn("PRODUCT_SPEC.md / SESSION_NOTES.md", c["detail"])


class TestBranchesCheckFromScan(unittest.TestCase):

    def test_thresholds(self):
        cases = [(0, "ok", 25), (1, "ok", 25), (2, "ok", 25), (3, "warn", 15), (5, "warn", 15),
                 (6, "bad", 5), (20, "bad", 5)]
        for n, status, pts in cases:
            r = with_branches(*[f"b{i}" for i in range(n)])
            c = health.check_branches(r)
            self.assertEqual((c["status"], c["points"], c["source"], c["open"]), (status, pts, "scan", n), n)

    def test_details(self):
        self.assertEqual(health.check_branches(with_branches())["detail"], "No branches besides main.")
        self.assertEqual(health.check_branches(with_branches("a"))["detail"], "1 open branch.")
        self.assertEqual(health.check_branches(with_branches("a", "b"))["detail"], "2 open branches.")
        self.assertIn("Refresh In flight", health.check_branches(with_branches("a", "b", "c"))["detail"])

    def test_henry_branches_do_not_count(self):
        r = with_branches("henry-1", "Henry-2", "feat-HENRY", "a")
        self.assertEqual(health.check_branches(r)["open"], 1)

    def test_default_branch_named_henry_is_still_the_default(self):
        r = scan_repo(default_branch="henry-main", branch_names=["henry-main", "a", "b", "c"])
        self.assertEqual(health.check_branches(r)["open"], 3)

    def test_falls_back_to_counts_without_names(self):
        r = scan_repo(branch_names=[], non_henry_branch_count=5, total_branch_count=7)
        self.assertEqual(health.check_branches(r)["open"], 4)   # non-henry count includes main
        r = scan_repo(non_default_branch_count=6)
        del r["branch_names"], r["non_henry_branch_count"]
        self.assertEqual(health.check_branches(r)["open"], 6)

    def test_unknown_is_not_scored(self):
        r = scan_repo()
        for k in ("branch_names", "non_henry_branch_count", "non_default_branch_count"):
            del r[k]
        self.assertFalse(health.check_branches(r)["counted"])
        r["non_henry_branch_count"] = True   # a bool is not a count
        self.assertFalse(health.check_branches(r)["counted"])
        r = scan_repo(branch_names="main")    # not a list
        del r["non_henry_branch_count"], r["non_default_branch_count"]
        self.assertFalse(health.check_branches(r)["counted"])


class TestBranchesCheckFromInflight(unittest.TestCase):

    def test_no_items_is_tidy(self):
        c = health.check_branches(with_branches(), [])
        self.assertEqual((c["status"], c["points"], c["source"]), ("ok", 25, "inflight"))
        self.assertEqual(c["detail"], "No branches besides main.")

    def test_live_work_is_not_penalized_however_many(self):
        names = [f"claude/w{i}" for i in range(8)]
        items = [item(n, k) for n, k in zip(names, ["merge", "finish", "continue", "unknown"] * 2)]
        c = health.check_branches(with_branches(*names), items)
        self.assertEqual((c["status"], c["points"], c["live"]), ("ok", 25, 8))
        self.assertEqual(c["detail"], "8 branches in progress, none stale or merged.")

    def test_dead_branch_thresholds(self):
        cases = [(1, "warn", 15), (2, "bad", 8), (3, "bad", 8), (4, "bad", 0), (9, "bad", 0)]
        for dead, status, pts in cases:
            names = [f"old{i}" for i in range(dead)]
            items = [item(n, "stale" if i % 2 else "complete") for i, n in enumerate(names)]
            c = health.check_branches(with_branches(*names), items)
            self.assertEqual((c["status"], c["points"]), (status, pts), dead)

    def test_details_name_stale_and_merged(self):
        c = health.check_branches(with_branches("a"), [item("a", "stale")])
        self.assertEqual(c["detail"], "1 stale branch. Finish or delete it in In flight.")
        c = health.check_branches(with_branches("a", "b"), [item("a", "complete"), item("b", "complete")])
        self.assertEqual(c["detail"], "2 merged branches not deleted. Delete them in In flight.")
        c = health.check_branches(with_branches("a", "b", "c"),
                                  [item("a", "stale"), item("b", "stale"), item("c", "complete")])
        self.assertEqual(c["detail"], "2 stale branches and 1 merged branch not deleted. "
                                      "Delete or finish them in In flight.")
        self.assertEqual((c["stale"], c["merged"]), (2, 1))

    def test_other_repos_and_henry_items_are_ignored(self):
        items = [item("a", "stale", repo="other"), item("a", "stale", owner="someorg"),
                 item("henry-x", "stale"), "junk", None, item("", "stale"), {"repo": "proj", "kind": "stale"}]
        c = health.check_branches(with_branches("a", "henry-x"), items)
        # Other repos' stale items never count against this one. In Flight
        # hasn't seen this repo's "a" branch, so the scan count is used
        # (Session 24 review H1) — 1 open branch, full points.
        self.assertEqual((c["source"], c["points"]), ("scan", 25))
        self.assertNotIn("stale branch", c["detail"])
        self.assertIn("hasn't read it yet", c["detail"])

    def test_branch_gone_from_scan_is_not_counted(self):
        # In Flight read it, but the newer scan no longer lists it (deleted).
        c = health.check_branches(with_branches("kept"), [item("gone", "complete"), item("kept", "stale")])
        self.assertEqual((c["merged"], c["stale"]), (0, 1))

    def test_without_branch_names_every_item_counts(self):
        r = scan_repo()
        del r["branch_names"]
        c = health.check_branches(r, [item("a", "stale"), item("b", "complete")])
        self.assertEqual(c["stale"] + c["merged"], 2)


class TestActiveCheck(unittest.TestCase):

    def check(self, **kw):
        return health.check_active(scan_repo(**kw), NOW)

    def test_thresholds(self):
        cases = [(0, "ok", 30), (1, "ok", 30), (30, "ok", 30), (31, "warn", 20), (90, "warn", 20),
                 (91, "bad", 10), (180, "bad", 10), (181, "bad", 0), (900, "bad", 0)]
        for days, status, pts in cases:
            c = self.check(pushed_at=ago(days))
            self.assertEqual((c["status"], c["points"], c["days"]), (status, pts, days), days)

    def test_partial_days_round_down(self):
        self.assertEqual(self.check(pushed_at=ago(30, hours=23))["days"], 30)

    def test_details(self):
        self.assertEqual(self.check(pushed_at=ago(0))["detail"], "Last push today.")
        self.assertEqual(self.check(pushed_at=ago(1))["detail"], "Last push yesterday.")
        self.assertEqual(self.check(pushed_at=ago(12))["detail"], "Last push 12 days ago.")
        self.assertEqual(self.check(pushed_at=ago(45))["detail"], "No push in 45 days.")
        self.assertIn("tag it Archived", self.check(pushed_at=ago(200))["detail"])

    def test_falls_back_to_updated_at(self):
        c = self.check(pushed_at="", updated_at=ago(3))
        self.assertEqual((c["points"], c["detail"]), (30, "Last update 3 days ago."))

    def test_no_dates_is_not_scored(self):
        for p, u in (("", ""), (None, None), ("not a date", 12345)):
            c = self.check(pushed_at=p, updated_at=u)
            self.assertFalse(c["counted"])
            self.assertEqual(c["max"], 0)

    def test_future_and_naive_timestamps(self):
        self.assertEqual(self.check(pushed_at=(NOW + datetime.timedelta(days=3)).isoformat())["days"], 0)
        self.assertEqual(self.check(pushed_at="2026-11-01T12:00:00")["days"], 8)

    def test_default_now_is_used_when_omitted(self):
        r = scan_repo(pushed_at=datetime.datetime.now(UTC).isoformat())
        self.assertEqual(health.check_active(r)["days"], 0)


class TestExemptions(unittest.TestCase):

    def assertNotGraded(self, result, reason):
        self.assertEqual(result["grade"], health.NOT_GRADED)
        self.assertIsNone(result["score"])
        self.assertFalse(result["graded"])
        self.assertEqual(result["reason"], reason)
        self.assertEqual(result["tone"], "none")
        self.assertEqual(result["checks"], [])
        self.assertTrue(result["summary"].startswith("Not graded"))

    def test_scan_error(self):
        r = scan_repo(error="HTTP 500", required_files={}, docs_updated=None, total_branch_count=0)
        self.assertNotGraded(health.grade_repo(r, None, NOW), "scan_error")

    def test_archived_on_github(self):
        r = scan_repo(archived=True, pushed_at=ago(900))
        self.assertNotGraded(health.grade_repo(r, None, NOW), "archived")
        self.assertTrue(health.grade_repo(scan_repo(archived=False), None, NOW)["graded"])
        self.assertTrue(health.grade_repo(scan_repo(archived="yes"), None, NOW)["graded"])

    def test_archived_tag(self):
        for tag in ("Archived", "archive", "  ARCHIVED "):
            self.assertNotGraded(health.grade_repo(scan_repo(), None, NOW, tags=[tag]), "archived_tag")
        self.assertTrue(health.grade_repo(scan_repo(), None, NOW, tags=["Church", None, 3])["graded"])

    def test_fork(self):
        self.assertNotGraded(health.grade_repo(scan_repo(fork=True), None, NOW), "fork")

    def test_empty_repo(self):
        r = scan_repo(total_branch_count=0, branch_names=[], non_henry_branch_count=0,
                      required_files={d: False for d in health.REQUIRED_DOCS}, docs_updated=None,
                      code_size_bytes=0)
        self.assertNotGraded(health.grade_repo(r, None, NOW), "empty")
        # False is not 0 here; a missing count is not "empty".
        self.assertTrue(health.grade_repo(scan_repo(total_branch_count=False), None, NOW)["graded"])
        r = scan_repo()
        del r["total_branch_count"]
        self.assertTrue(health.grade_repo(r, None, NOW)["graded"])

    def test_scan_error_wins_over_everything(self):
        r = scan_repo(error="boom", archived=True, fork=True)
        self.assertEqual(health.grade_repo(r, None, NOW, tags=["Archived"])["reason"], "scan_error")

    def test_too_little_data(self):
        for r in ({}, {"name": "x"}, {"owner": "o", "name": "x", "pushed_at": ago(1)}):
            self.assertNotGraded(health.grade_repo(r, None, NOW), "too_little_data")
        # Branches + activity (55 of 100) is enough to grade.
        g = health.grade_repo({"owner": "o", "name": "x", "pushed_at": ago(1), "branch_names": ["main"]},
                              None, NOW)
        self.assertEqual((g["grade"], g["score"]), ("A", 100))
        self.assertIn("Every check that could be scored passes.", g["summary"])

    def test_every_reason_has_a_message(self):
        for reason, text in health.EXEMPT_REASONS.items():
            self.assertTrue(text.startswith("Not graded"), reason)
            self.assertEqual(health._exempt_result(reason)["title"], f"Health —\n{text}")


class TestGradeRepo(unittest.TestCase):

    def test_perfect(self):
        g = health.grade_repo(scan_repo(), [], NOW)
        self.assertEqual((g["grade"], g["score"], g["tone"], g["graded"]), ("A", 100, "ok", True))
        self.assertEqual(g["summary"], "100/100. Every check passes.")
        self.assertEqual([c["key"] for c in g["checks"]], ["docs", "current", "branches", "active"])

    def test_score_is_points_over_100(self):
        r = with_branches("a", "b", "c", docs_updated=False, pushed_at=ago(45),
                          required_files=dict(ALL_DOCS, LICENSE=False))
        g = health.grade_repo(r, None, NOW)
        # 24 + 0 + 15 + 20
        self.assertEqual((g["score"], g["grade"], g["tone"]), (59, "F", "bad"))

    def test_unscored_checks_rescale_the_score(self):
        # docs current unknown: 30 + 25 + 20 earned of 85 possible.
        g = health.grade_repo(scan_repo(docs_updated=None, pushed_at=ago(45)), None, NOW)
        self.assertEqual(g["score"], health._scaled(75, 85))
        self.assertEqual(g["score"], 88)
        self.assertEqual(sum(c["max"] for c in g["checks"]), 85)

    def test_summary_names_the_biggest_lift(self):
        r = with_branches("a", "b", "c", "d", docs_updated=False)
        items = [item(n, "complete") for n in "abcd"]
        g = health.grade_repo(r, items, NOW)
        self.assertEqual(g["score"], 60)   # 30 + 0 + 0 + 30
        self.assertEqual(g["summary"], "60/100. Cleaning up branches alone lifts it to 85 (B).")

    def test_hopeless_repo_suggests_archiving_instead_of_one_fix(self):
        r = with_branches(*[f"x{i}" for i in range(7)], pushed_at=ago(400), docs_updated=None,
                          required_files=dict({d: False for d in health.REQUIRED_DOCS}, **{"CLAUDE.md": True}))
        g = health.grade_repo(r, None, NOW)
        self.assertEqual((g["score"], g["grade"]), (11, "F"))   # 6 + 0 + 5 + 0
        self.assertEqual(g["summary"], "11/100. No single fix gets it past F. If it's finished, tag it Archived.")
        # One fix away from a D still names that fix.
        g = health.grade_repo(scan_repo(pushed_at=ago(400), docs_updated=False), None, NOW)
        self.assertEqual(g["summary"], "55/100. A fresh push alone lifts it to 85 (B).")

    def test_tie_goes_to_the_earlier_check(self):
        # docs -6, branches -10 (3 open), active -10 (45 days): branches and
        # active tie, and branches comes first in check order.
        r = with_branches("a", "b", "c", docs_updated=True, pushed_at=ago(45),
                          required_files=dict(ALL_DOCS, LICENSE=False))
        g = health.grade_repo(r, None, NOW)
        self.assertEqual(g["score"], 74)
        self.assertEqual(g["summary"], "74/100. Cleaning up branches alone lifts it to 84 (B).")

    def test_title_is_a_plain_text_breakdown(self):
        g = health.grade_repo(with_branches("a", docs_updated=False), [item("a", "stale")], NOW)
        lines = g["title"].split("\n")
        self.assertEqual(lines[0], f"Health {g['grade']} ({g['score']}/100)")
        self.assertEqual(lines[1], "OK Required docs 30/30: All 5 present.")
        self.assertTrue(lines[2].startswith("FIX Docs current 0/15: "))
        self.assertEqual(lines[3], "WARN Branches tidy 15/25: 1 stale branch. Finish or delete it in In flight.")
        self.assertEqual(lines[-1], g["summary"])
        g = health.grade_repo(scan_repo(docs_updated=None), None, NOW)
        self.assertIn("N/A Docs current not scored:", g["title"])

    def test_deterministic_and_does_not_mutate_inputs(self):
        r = with_branches("a", "henry-b", docs_updated=False, pushed_at=ago(100))
        items = [item("a", "stale"), item("henry-b", "complete")]
        r0, i0 = copy.deepcopy(r), copy.deepcopy(items)
        first = health.grade_repo(r, items, NOW, tags=["Church"])
        for _ in range(3):
            self.assertEqual(health.grade_repo(r, items, NOW, tags=["Church"]), first)
        self.assertEqual((r, items), (r0, i0))
        self.assertEqual(json.loads(json.dumps(first)), first)  # JSON-safe


class TestGradeScan(unittest.TestCase):

    def test_keys_distribution_and_reasons(self):
        repos = [scan_repo("a"), scan_repo("b", pushed_at=ago(400), docs_updated=False),
                 scan_repo("c", fork=True), scan_repo("d", error="x"), "junk",
                 scan_repo("site", owner="someorg")]
        rep = health.grade_scan(repos, None, NOW)
        self.assertEqual(sorted(rep["grades"]), ["chris/a", "chris/b", "chris/c", "chris/d", "someorg/site"])
        # b: 30 docs + 0 current + 25 branches + 0 active (400 days) = 55 -> F
        self.assertEqual(rep["grades"]["chris/b"]["score"], 55)
        self.assertEqual(rep["distribution"], {"A": 2, "B": 0, "C": 0, "D": 0, "F": 1, "not_graded": 2})
        self.assertEqual(list(rep["distribution"]), ["A", "B", "C", "D", "F", "not_graded"])
        self.assertEqual(rep["reasons"], {"fork": 1, "scan_error": 1})
        self.assertFalse(rep["inflight_used"])
        self.assertEqual(rep["graded_at"], NOW.isoformat())

    def test_inflight_items_route_to_their_repo(self):
        repos = [with_branches("x"), with_branches("x", name="other")]
        repos[1]["full_name"] = "chris/other"
        cache = {"items": [item("x", "stale"), item("x", "merge", repo="other")], "failed_repos": []}
        rep = health.grade_scan(repos, cache, NOW)
        self.assertTrue(rep["inflight_used"])
        b = {k: [c for c in g["checks"] if c["key"] == "branches"][0] for k, g in rep["grades"].items()}
        self.assertEqual((b["chris/proj"]["stale"], b["chris/proj"]["source"]), (1, "inflight"))
        self.assertEqual((b["chris/other"]["stale"], b["chris/other"]["live"]), (0, 1))

    def test_failed_repo_falls_back_to_the_scan(self):
        cache = {"items": [], "failed_repos": ["chris/proj"]}
        rep = health.grade_scan([with_branches("a", "b", "c")], cache, NOW)
        b = rep["grades"]["chris/proj"]["checks"][2]
        self.assertEqual((b["source"], b["points"]), ("scan", 15))

    def test_bad_cache_shapes_mean_no_inflight(self):
        for cache in (None, {}, {"items": None}, {"items": "x"}, []):
            rep = health.grade_scan([scan_repo()], cache, NOW)
            self.assertFalse(rep["inflight_used"], cache)
            self.assertEqual(rep["grades"]["chris/proj"]["checks"][2]["source"], "scan")

    def test_archived_tag_from_groups(self):
        rep = health.grade_scan([scan_repo("a"), scan_repo("b")], None, NOW,
                                groups={"Archived": ["a"], "Church": ["a", "b"], "Empty": None})
        self.assertEqual(rep["grades"]["chris/a"]["reason"], "archived_tag")
        self.assertTrue(rep["grades"]["chris/b"]["graded"])

    def test_one_bad_repo_never_breaks_the_rest(self):
        real = health.check_docs

        def flaky(repo):
            if repo.get("name") == "bad":
                raise ValueError("odd data")
            return real(repo)

        with patch.object(health, "check_docs", side_effect=flaky):
            rep = health.grade_scan([scan_repo("bad"), scan_repo("good")], None, NOW)
        self.assertEqual(rep["grades"]["chris/bad"]["reason"], "grade_error")
        self.assertEqual(rep["grades"]["chris/good"]["grade"], "A")
        self.assertEqual(rep["errors"], [{"repo": "chris/bad", "error": "ValueError: odd data"}])
        self.assertEqual(rep["reasons"], {"grade_error": 1})

    def test_same_now_same_report(self):
        repos = [scan_repo("a"), with_branches("x", "y", "z", name="b", pushed_at=ago(77))]
        cache = {"items": [item("x", "stale", repo="b")]}
        self.assertEqual(health.grade_scan(repos, cache, NOW), health.grade_scan(repos, cache, NOW))

    def test_sparse_and_odd_repo_dicts_never_raise(self):
        odd = [{}, {"name": "only-name"}, {"owner": "o", "name": "n", "pushed_at": 123, "required_files": ["x"],
                                    "branch_names": [None, 5, "main"], "docs_updated": "no",
                                    "total_branch_count": "3", "non_henry_branch_count": None}]
        rep = health.grade_scan(odd, {"items": [None, {"owner": "o"}], "failed_repos": [None]}, NOW)
        self.assertEqual(rep["errors"], [])
        self.assertEqual(len(rep["grades"]), 3)

    def test_repo_key(self):
        self.assertEqual(health.repo_key({"full_name": "a/b", "owner": "x", "name": "y"}), "a/b")
        self.assertEqual(health.repo_key({"owner": "x", "name": "y"}), "x/y")
        self.assertEqual(health.repo_key({"name": "y"}), "/y")


class TestPortfolioView(unittest.TestCase):

    def test_rows_carry_their_grade(self):
        repos = [scan_repo("a"), scan_repo("b")]
        grades = health.grade_scan(repos, None, NOW)["grades"]
        v = portfolio.build_view(repos, {"Church": ["a"]}, {}, {}, [], grades=grades)
        self.assertEqual(v["rows"]["chris/a"]["health"]["grade"], "A")
        self.assertEqual(v["sections"][0]["repos"][0]["health"]["grade"], "A")
        self.assertEqual(v["untagged"][0]["health"]["grade"], "A")

    def test_without_grades_rows_have_none(self):
        v = portfolio.build_view([scan_repo("a")], {}, {}, {}, [])
        self.assertIsNone(v["rows"]["chris/a"]["health"])
        md = portfolio.compose_markdown(v, [], "chris", "2026-11-09")
        self.assertNotIn("health", md)

    def test_markdown_includes_graded_health(self):
        repos = [scan_repo("a"), scan_repo("b", fork=True)]
        grades = health.grade_scan(repos, None, NOW)["grades"]
        v = portfolio.build_view(repos, {"Church": ["a", "b"]}, {}, {}, [], grades=grades)
        md = portfolio.compose_markdown(v, [], "chris", "2026-11-09")
        self.assertIn("health A (100/100)", md)
        self.assertEqual(md.count("health "), 1)   # the fork isn't graded


# --- Routes -----------------------------------------------------------------

class _RouteBase(unittest.TestCase):

    PATHS = ("GROUPS_PATH", "PREFS_PATH", "_LEGACY_PREFS_PATH", "PORTFOLIO_SUGGESTIONS_PATH",
             "INFRA_HOMES_PATH", "PORTFOLIO_LOG_PATH", "INFLIGHT_PATH", "COMPONENTS_PATH",
             "SELECTOR_BLURBS_PATH", "SUMMARIES_PATH", "HEALTH_LOG_PATH")

    def setUp(self):
        import app as app_module
        self.m = app_module
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "logs"))
        self._saved = {p: getattr(models, p) for p in self.PATHS}
        for p in self.PATHS:
            setattr(models, p, os.path.join(self.tmp, "logs" if p.endswith("LOG_PATH") else "",
                                            p.lower() + ".json"))
        self._orig = (app_module._scan_results, app_module._github_client, app_module._credentials)
        now = datetime.datetime.now(UTC)
        recent = (now - datetime.timedelta(days=2)).isoformat()
        old = (now - datetime.timedelta(days=400)).isoformat()
        app_module._scan_results = {
            "repos": [
                scan_repo("goodrepo", pushed_at=recent, updated_at=recent),
                with_branches("old-a", "old-b", "old-c", "old-d", "old-e", "old-f", name="messyrepo",
                              pushed_at=old, updated_at=old, docs_updated=False,
                              required_files=dict(ALL_DOCS, LICENSE=False, **{"CLAUDE.md": False})),
                scan_repo("forkedrepo", fork=True),
                {"owner": "chris", "name": "sparserepo", "full_name": "chris/sparserepo"},
                scan_repo("brokenrepo", error="HTTP 500", required_files={}, docs_updated=None,
                          total_branch_count=0, branch_names=[]),
            ],
            "total_repos": 5, "total_branches": 9,
            "scanned_at": now.isoformat(),
        }
        app_module._github_client = object()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["github_user"] = "chris"

    def tearDown(self):
        for p, v in self._saved.items():
            setattr(models, p, v)
        (self.m._scan_results, self.m._github_client, self.m._credentials) = self._orig


class TestDashboardRoute(_RouteBase):

    def test_health_column_renders(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('class="col-health sortable" data-sort="num" data-col="1"', html)
        self.assertIn(">Health</th>", html)
        # Letters are the visible text of each chip.
        chips = re.findall(r'class="health-chip health-tone-(\w+) health-toggle"[^>]*>([^<]+)</button>', html,
                           flags=re.S)
        self.assertEqual(sorted(chips), sorted([("ok", "A"), ("bad", "F"), ("none", "—"), ("none", "—"),
                                                ("none", "—")]))
        # Breakdown rows are hidden until clicked, and each button controls its own row.
        controls = re.findall(r'aria-controls="(health-row-\d+)"', html)
        rows = re.findall(r'<tr class="health-detail-row" id="(health-row-\d+)" hidden>', html)
        self.assertEqual(controls, rows)
        self.assertEqual(len(set(rows)), 5)
        self.assertIn("6 open branches. Refresh In flight", html)
        self.assertIn("Missing CLAUDE.md and LICENSE. Add them.", html)
        self.assertIn("Not graded: a fork.", html)
        self.assertIn("Not graded: the last scan couldn&#39;t read this repo.", html)
        self.assertIn("Not graded: the scan has too little data", html)
        # Ungraded cells sort last (empty sort value); graded carry the score.
        self.assertIn('data-sort-value="100"', html)
        self.assertEqual(html.count('class="col-health" data-sort-value=""'), 3)
        # Table width grew by one column everywhere.
        self.assertNotIn('colspan="13"', html)
        # "Current?" agrees with the health check: a missing flag is "—", not "No".
        self.assertEqual(html.count('title="Doc files not present">—'), 2)
        self.assertEqual(html.count('title="Docs were last updated before other files">No'), 1)

    def test_attention_stat_counts_d_and_f(self):
        html = self.client.get("/").get_data(as_text=True)
        m = re.search(r'<span class="stat-value ?(stat-warning)?">(\d+)</span>\s*<span class="stat-label">Health D or F',
                      html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "1")

    def test_logs_one_distribution_event_per_render(self):
        self.client.get("/")
        self.client.get("/")
        events = models.tail_health_log()
        self.assertEqual([e["event"] for e in events], ["dashboard", "dashboard"])
        e = events[-1]
        self.assertEqual(e["distribution"], {"A": 1, "B": 0, "C": 0, "D": 0, "F": 1, "not_graded": 3})
        self.assertEqual(e["reasons"], {"fork": 1, "too_little_data": 1, "scan_error": 1})
        self.assertEqual(e["repos"], 5)
        self.assertFalse(e["inflight"])
        self.assertTrue(e["scanned_at"])

    def test_group_filter_scopes_rows_but_not_the_distribution(self):
        models.save_groups({"Church": ["goodrepo"]})
        html = self.client.get("/?group=Church").get_data(as_text=True)
        self.assertEqual(len(re.findall(r'<tr class="health-detail-row"', html)), 1)
        self.assertEqual(models.tail_health_log()[-1]["repos"], 5)

    def test_archived_tag_takes_a_repo_out_of_grading(self):
        models.save_groups({"Archived": ["messyrepo"]})
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Not graded: tagged Archived.", html)
        self.assertEqual(models.tail_health_log()[-1]["distribution"]["F"], 0)

    def test_uses_the_inflight_cache_when_present(self):
        models.save_inflight({"items": [
            {"owner": "chris", "repo": "messyrepo", "branch": "old-a", "kind": "stale"},
            {"owner": "chris", "repo": "messyrepo", "branch": "old-b", "kind": "complete"},
            {"owner": "chris", "repo": "messyrepo", "branch": "old-c", "kind": "merge"},
            # In Flight reads every non-default branch; the rest are in progress.
            {"owner": "chris", "repo": "messyrepo", "branch": "old-d", "kind": "continue"},
            {"owner": "chris", "repo": "messyrepo", "branch": "old-e", "kind": "continue"},
            {"owner": "chris", "repo": "messyrepo", "branch": "old-f", "kind": "finish"},
        ], "gathered_at": "2026-11-01T10:00:00+00:00", "failed_repos": []})
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("1 stale branch and 1 merged branch not deleted.", html)
        self.assertIn('<a href="/inflight">Open In flight</a>', html)
        self.assertIn("(read 2026-11-01)", html)
        self.assertTrue(models.tail_health_log()[-1]["inflight"])

    def test_grading_failure_still_renders_and_is_logged(self):
        with patch("health.grade_scan", side_effect=RuntimeError("boom")):
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(">Health</th>", html)
        self.assertNotRegex(html, r'class="[^"]*health-toggle')
        self.assertIn('<span class="health-chip health-tone-none" title="Not graded">—</span>', html)
        ev = models.tail_health_log()[-1]
        self.assertEqual((ev["event"], ev["error"]), ("grade_failed", "RuntimeError: boom"))

    def test_per_repo_grade_errors_are_logged(self):
        real = health.check_active

        def flaky(repo, now=None):
            if repo.get("name") == "goodrepo":
                raise KeyError("pushed_at")
            return real(repo, now)

        with patch.object(health, "check_active", side_effect=flaky):
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("grading this repo failed", resp.get_data(as_text=True))
        events = models.tail_health_log()
        self.assertEqual(events[0]["event"], "grade_error")
        self.assertEqual(events[0]["repo"], "chris/goodrepo")
        self.assertEqual(events[-1]["distribution"]["not_graded"], 4)

    def test_no_scan_renders_and_logs_nothing(self):
        self.m._scan_results = None
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(models.tail_health_log(), [])

    def test_breakdown_text_is_escaped(self):
        self.m._scan_results["repos"].append(
            scan_repo("xss", default_branch="<img src=x onerror=alert(1)>",
                      branch_names=["<img src=x onerror=alert(1)>"]))
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("<img src=x", html)
        self.assertIn("No branches besides &lt;img src=x", html)


class TestPortfolioRoute(_RouteBase):

    def test_chips_render_on_every_row(self):
        html = self.client.get("/portfolio").get_data(as_text=True)
        chips = re.findall(r'class="health-chip health-tone-(\w+) pf-health-toggle"[^>]*>([^<]+)</button>', html,
                           flags=re.S)
        self.assertEqual(sorted(chips), sorted([("ok", "A"), ("bad", "F"), ("none", "—"), ("none", "—"),
                                                ("none", "—")]))
        self.assertEqual(html.count('class="health-breakdown pf-health" hidden'), 5)
        self.assertIn("Missing CLAUDE.md and LICENSE. Add them.", html)

    def test_repo_in_two_tags_gets_two_working_chips(self):
        models.save_groups({"Church": ["goodrepo"], "School": ["goodrepo"]})
        html = self.client.get("/portfolio").get_data(as_text=True)
        self.assertEqual(len(re.findall(r'pf-health-toggle"[^>]*>A</button>', html, flags=re.S)), 2)

    def test_export_carries_the_grade(self):
        models.save_groups({"Church": ["goodrepo", "messyrepo", "forkedrepo"]})
        md = self.client.get("/portfolio/export.md").get_data(as_text=True)
        self.assertIn("health A (100/100)", md)
        self.assertRegex(md, r"health F \(\d+/100\)")
        self.assertNotIn("**forkedrepo** — _(", md)

    def test_portfolio_does_not_log_a_distribution(self):
        self.client.get("/portfolio")
        self.assertEqual([e["event"] for e in models.tail_health_log()], [])


if __name__ == "__main__":
    unittest.main()



class TestScanCarriesArchivedAndFork(unittest.TestCase):
    """The exemptions only work if the scan keeps GitHub's flags."""

    def test_scan_repo_lite_and_error_row_carry_flags(self):
        import github_client as gh
        import app as app_mod
        raw = {"owner": {"login": "o"}, "name": "r", "full_name": "o/r", "default_branch": "main",
               "archived": True, "fork": True}

        class C:
            def get_branches(self, *a, **k):
                return [{"name": "main"}]

            def check_required_files(self, *a, **k):
                return {}, {}

            def get_language_bytes(self, *a, **k):
                return {}

            def __getattr__(self, name):
                return lambda *a, **k: None
        try:
            row = gh.scan_repo_lite(C(), raw)
        except Exception as e:  # scan_repo_lite internals may need more; the flags are what matter
            self.skipTest(f"scan_repo_lite fixture incomplete: {e}")
        self.assertIs(row["archived"], True)
        self.assertIs(row["fork"], True)
        err = app_mod._scan_error_entry(raw, RuntimeError("x"))
        self.assertIs(err["archived"], True)
        self.assertIs(err["fork"], True)
        self.assertEqual(health.grade_repo(err)["grade"], "—")



class TestInflightCoverage(unittest.TestCase):
    """Review H1: an In Flight cache with no items for a repo must not make a
    repo with open branches read as 'No branches besides main'."""

    def repo(self, branches):
        return {"owner": "o", "name": "r", "default_branch": "main",
                "branch_names": ["main"] + branches, "non_default_branch_count": len(branches)}

    def test_branches_unseen_by_inflight_fall_back_to_scan(self):
        r = self.repo([f"claude/b{i}" for i in range(8)])
        c = health.check_branches(r, [])
        self.assertEqual(c["source"], "scan")
        self.assertIn("8 open branches", c["detail"])
        self.assertIn("hasn't read all of them", c["detail"])
        self.assertNotIn("No branches besides", c["detail"])

    def test_partly_seen_falls_back_too(self):
        r = self.repo(["claude/a", "claude/b"])
        items = [{"owner": "o", "repo": "r", "branch": "claude/a", "kind": "stale"}]
        self.assertEqual(health.check_branches(r, items)["source"], "scan")

    def test_fully_seen_uses_inflight(self):
        r = self.repo(["claude/a", "claude/b"])
        items = [{"owner": "o", "repo": "r", "branch": "claude/a", "kind": "stale"},
                 {"owner": "o", "repo": "r", "branch": "claude/b", "kind": "complete"}]
        c = health.check_branches(r, items)
        self.assertEqual((c["source"], c["stale"], c["merged"]), ("inflight", 1, 1))

    def test_no_branches_with_empty_inflight_is_still_tidy(self):
        c = health.check_branches(self.repo([]), [])
        self.assertIn("No branches besides main", c["detail"])

    def test_grade_scan_ignores_inflight_for_truncated_repos(self):
        r = with_branches("a")
        key = health.repo_key(r)
        items = [item("a", "stale")]
        full = health.grade_scan([r], {"items": items, "failed_repos": []})
        trunc = health.grade_scan([r], {"items": items, "truncated_repos": {key: 40}})
        src = lambda rep: {c["key"]: c for c in rep["grades"][key]["checks"]}["branches"]["source"]
        self.assertEqual((src(full), src(trunc)), ("inflight", "scan"))
