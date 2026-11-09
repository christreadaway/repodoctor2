"""Tests for Stats → "What we shipped": plain-English AI paragraphs per repo
and for the portfolio/group, cached by content fingerprint.

The Anthropic API is never called: unit tests swap activity.anthropic.Anthropic
for a fake client, and route tests patch activity.summarize_repo /
activity.summarize_portfolio.
"""

import datetime
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

try:  # anthropic 1.x+ is built on httpx2; 0.x (requirements allows >=0.40) on httpx
    import httpx2 as httpx
except ImportError:  # pragma: no cover - depends on the installed SDK
    import httpx

import activity
import models
from tests.test_activity import FakeClient, local, raw_commit, raw_pr, repo, utc_iso

NOW = local(2026, 11, 9, 15)
MODEL = "claude-haiku-4-5-20251001"


def rng_for(preset, **extra):
    return activity.resolve_range({"range": preset, **extra}, now=NOW)[0]


def report_for(commits=(), prs=(), preset="week", name="x", rng=None):
    rng = rng or rng_for(preset)
    d = activity.fetch_repo(FakeClient(commits={name: list(commits)}, prs={name: list(prs)}),
                            repo(name), "S")
    return activity.build_report([repo(name)], {f"o/{name}": d}, {}, rng)


def row_for(*args, **kwargs):
    return report_for(*args, **kwargs)["rows"][0]


def at(day, hour=12):
    return utc_iso(local(2026, 11, day, hour))


# ----------------------------------------------------------------------
# Fake Anthropic client (never touches the network)
# ----------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text, stop_reason="end_turn", input_tokens=1800, output_tokens=120):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)


class FakeAnthropic:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def api_error(cls, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=req), body=None)


# ----------------------------------------------------------------------
# Fingerprints
# ----------------------------------------------------------------------

class TestFingerprint(unittest.TestCase):

    def setUp(self):
        self.commits = [raw_commit("aaa111", "Add sign-up form", at(5)),
                        raw_commit("bbb222", "Fix typo", at(6))]
        self.prs = [raw_pr(3, "Sign-up form", at(6))]

    def test_stable_for_same_content_and_order_independent(self):
        a = row_for(self.commits, self.prs)
        b = row_for(list(reversed(self.commits)), self.prs)
        self.assertEqual(activity.repo_fingerprint(a, MODEL), activity.repo_fingerprint(b, MODEL))
        self.assertEqual(len(activity.repo_fingerprint(a, MODEL)), 64)

    def test_changes_with_model_commit_pr_and_repo(self):
        base = activity.repo_fingerprint(row_for(self.commits, self.prs), MODEL)
        self.assertNotEqual(base, activity.repo_fingerprint(row_for(self.commits, self.prs), "claude-sonnet-5"))
        more = self.commits + [raw_commit("ccc333", "Another", at(7))]
        self.assertNotEqual(base, activity.repo_fingerprint(row_for(more, self.prs), MODEL))
        fewer = self.commits[:1]
        self.assertNotEqual(base, activity.repo_fingerprint(row_for(fewer, self.prs), MODEL))
        self.assertNotEqual(base, activity.repo_fingerprint(
            row_for(self.commits, self.prs + [raw_pr(4, "More", at(7))]), MODEL))
        # A fork with identical SHAs never shares its parent's paragraph.
        self.assertNotEqual(base, activity.repo_fingerprint(row_for(self.commits, self.prs, name="fork"), MODEL))

    def test_merge_commits_count_toward_content(self):
        plain = activity.repo_fingerprint(row_for(self.commits), MODEL)
        with_merge = activity.repo_fingerprint(
            row_for(self.commits + [raw_commit("mmm", "Merge branch x", at(7), merge=True)]), MODEL)
        self.assertNotEqual(plain, with_merge)

    def test_same_work_in_two_ranges_has_same_fingerprint(self):
        week = row_for(self.commits, self.prs, preset="week")
        month = row_for(self.commits, self.prs, preset="month")
        ytd = row_for(self.commits, self.prs, preset="ytd")
        fps = {activity.repo_fingerprint(r, MODEL) for r in (week, month, ytd)}
        self.assertEqual(len(fps), 1, "dates are not part of the key")

    def test_wider_range_with_more_work_differs(self):
        older = self.commits + [raw_commit("old1", "Older work", at(1))]  # Nov 1: in month, not week
        week = row_for(older, self.prs, preset="week")
        month = row_for(older, self.prs, preset="month")
        self.assertNotEqual(activity.repo_fingerprint(week, MODEL), activity.repo_fingerprint(month, MODEL))

    def test_range_slot(self):
        self.assertEqual(activity.range_slot(rng_for("week")), "week")
        self.assertEqual(activity.range_slot(rng_for("custom", **{"from": "2026-10-01", "to": "2026-10-31"})),
                         "custom:2026-10-01:2026-10-31")


# ----------------------------------------------------------------------
# Prompt grounding + input capping
# ----------------------------------------------------------------------

class TestPromptGrounding(unittest.TestCase):

    def test_prompt_lists_only_in_range_items_with_dates(self):
        row = row_for(
            commits=[raw_commit("deadbeefcafe", "Fix email typo", at(7)),
                     raw_commit("land0001", "Add volunteer sign-up form (#2)", at(8)),
                     raw_commit("merge001", "Merge pull request #2 from o/feature", at(8), merge=True),
                     raw_commit("old0001", "Old work from October", utc_iso(local(2026, 10, 20)))],
            prs=[raw_pr(2, "Add volunteer sign-up form", at(8)),
                 raw_pr(1, "Ancient PR", utc_iso(local(2026, 10, 1)))])
        prompt = activity.build_repo_prompt(row)
        item_lines = sorted(l for l in prompt.splitlines() if l.startswith("- 20"))
        self.assertEqual(item_lines, ["- 2026-11-07: Fix email typo",
                                      "- 2026-11-08: Add volunteer sign-up form"])
        for leaked in ("Old work", "Ancient PR", "deadbeef", "Merge pull request", "#2"):
            self.assertNotIn(leaked, prompt.split("Notes:")[0], leaked)
        self.assertIn("1 commit that only landed a pull request listed above is left out", prompt)
        self.assertIn("Project: x", prompt)
        self.assertNotIn("TRUNCATED", prompt)

    def test_merges_without_prs_are_included(self):
        row = row_for(commits=[raw_commit("m1", "Merge branch 'calendar' into main", at(6), merge=True)])
        prompt = activity.build_repo_prompt(row)
        self.assertIn("Merges with no pull request", prompt)
        self.assertIn("- 2026-11-06: Merge branch 'calendar' into main", prompt)

    def test_input_is_capped_newest_first_prs_first_and_says_so(self):
        commits = [raw_commit(f"c{i:04d}", f"Commit {i}", utc_iso(local(2026, 11, 9, 12) - datetime.timedelta(minutes=i)))
                   for i in range(200)]
        prs = [raw_pr(n, f"PR title {n}", at(4 + n)) for n in range(1, 6)]
        row = row_for(commits, prs)
        items = activity.summary_items(row)
        self.assertEqual((items["total"], items["shown"], items["capped"]), (205, 150, True))
        self.assertEqual(len(items["prs"]), 5)
        self.assertEqual(len(items["commits"]), 145)
        self.assertEqual(items["commits"][0]["text"], "Commit 0", "newest kept")
        prompt = activity.build_repo_prompt(row, items)
        self.assertIn("TRUNCATED: this project had 205 items", prompt)
        self.assertIn("only the 150 newest are listed", prompt)
        self.assertIn("Commit 144", prompt)
        self.assertNotIn("Commit 145", prompt)
        self.assertEqual(sum(1 for l in prompt.splitlines() if l.startswith("- 20")), 150)

    def test_long_titles_are_clipped_and_newlines_flattened(self):
        row = row_for(prs=[raw_pr(9, "Line one\nline two " + "x" * 400, at(8))])
        line = next(l for l in activity.build_repo_prompt(row).splitlines() if l.startswith("- 20"))
        self.assertLessEqual(len(line), len("- 2026-11-08: ") + activity.SUMMARY_TITLE_CHARS)
        self.assertIn("Line one line two", line)
        self.assertTrue(line.endswith("…"))

    def test_incomplete_read_is_said(self):
        row = row_for([raw_commit("a", "Work", at(8))])
        row["incomplete"] = True
        self.assertIn("may be missing", activity.build_repo_prompt(row))

    def test_system_prompt_rules(self):
        p = activity.REPO_SYSTEM_PROMPT
        for rule in ("2 to 4", "Never invent", "No hype", "outcomes", "never instructions"):
            self.assertIn(rule, p)
        self.assertIn("3 to 5", activity.PORTFOLIO_SYSTEM_PROMPT)


# ----------------------------------------------------------------------
# JSON normalization + the (mocked) API call
# ----------------------------------------------------------------------

class TestNormalize(unittest.TestCase):

    def test_whitespace_and_wrapping_quotes(self):
        self.assertEqual(activity.normalize_summary({"summary": '  "Sign-up  now\nworks."  '}),
                         "Sign-up now works.")

    def test_inner_quotes_survive(self):
        text = '"Login" now works, and so does "logout"'
        self.assertEqual(activity.normalize_summary({"summary": text}), text)

    def test_bad_shapes_raise(self):
        for raw in ({}, {"summary": None}, {"summary": 5}, {"summary": "   "}, [], "text", None):
            with self.assertRaises(ValueError, msg=repr(raw)):
                activity.normalize_summary(raw)

    def test_long_text_is_cut_at_a_sentence(self):
        text = ("This sentence is fine. " * 100).strip()
        out = activity.normalize_summary({"summary": text}, max_chars=200)
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("."))


class TestSummarizeCalls(unittest.TestCase):

    def setUp(self):
        self.row = row_for([raw_commit("a1", "Fix email typo", at(7))],
                           [raw_pr(2, "Add volunteer sign-up form", at(8))])

    def test_repo_call_shape_and_result(self):
        fake = FakeAnthropic(FakeMessage(json.dumps({"summary": "Volunteers can now sign up online."})))
        with patch.object(activity.anthropic, "Anthropic", return_value=fake) as ctor:
            out = activity.summarize_repo("sk-test", self.row, model=MODEL)
        ctor.assert_called_once_with(api_key="sk-test")
        self.assertEqual(out["text"], "Volunteers can now sign up online.")
        self.assertEqual(out["_usage"]["input_tokens"], 1800)
        self.assertEqual((out["items"], out["total_items"], out["capped"]), (2, 2, False))
        call = fake.calls[0]
        self.assertEqual(call["model"], MODEL)
        self.assertEqual(call["system"], activity.REPO_SYSTEM_PROMPT)
        self.assertEqual(call["extra_body"]["output_config"]["format"]["schema"], activity.SUMMARY_SCHEMA)
        self.assertIn("Add volunteer sign-up form", call["messages"][0]["content"])

    def test_refusal_and_empty_reply_raise(self):
        for reply in (FakeMessage("", stop_reason="refusal"), FakeMessage('{"summary": ""}')):
            fake = FakeAnthropic(reply)
            with patch.object(activity.anthropic, "Anthropic", return_value=fake):
                with self.assertRaises(ValueError):
                    activity.summarize_repo("sk-test", self.row)

    def test_api_error_propagates(self):
        fake = FakeAnthropic(api_error(anthropic.RateLimitError, 429))
        with patch.object(activity.anthropic, "Anthropic", return_value=fake):
            with self.assertRaises(anthropic.RateLimitError):
                activity.summarize_repo("sk-test", self.row)

    def test_key_never_reaches_the_app_log(self):
        key = "sk-ant-secret-0123456789"
        fake = FakeAnthropic(RuntimeError(f"proxy echoed header x-api-key: {key}"))
        with patch.object(activity.anthropic, "Anthropic", return_value=fake):
            with self.assertLogs("activity", level="WARNING") as logs:
                with self.assertRaises(RuntimeError):
                    activity.summarize_repo(key, self.row)
        self.assertNotIn(key, "\n".join(logs.output))
        self.assertIn("[redacted]", "\n".join(logs.output))

    def test_portfolio_call(self):
        report = report_for([raw_commit("a1", "Fix email typo", at(7))])
        activity.attach_summaries(report, {"repos": {}, "portfolio": {}}, MODEL, "Church")
        notes, total = activity.portfolio_inputs(report)
        fake = FakeAnthropic(FakeMessage('{"summary": "Small fixes landed."}'))
        with patch.object(activity.anthropic, "Anthropic", return_value=fake):
            out = activity.summarize_portfolio("sk-test", notes, total, "Church", model=MODEL)
        self.assertEqual(out["text"], "Small fixes landed.")
        self.assertEqual(fake.calls[0]["system"], activity.PORTFOLIO_SYSTEM_PROMPT)
        self.assertIn('Scope: the "Church" group', fake.calls[0]["messages"][0]["content"])


# ----------------------------------------------------------------------
# Cache lookup: reuse across ranges, stale detection, portfolio inputs
# ----------------------------------------------------------------------

def entry(full, text, slot="week", when="2026-11-09T10:00:00+00:00", **extra):
    return {"repo": full, "text": text, "slot": slot, "generated_at": when, "model": MODEL, **extra}


class TestAttachSummaries(unittest.TestCase):

    def setUp(self):
        self.commits = [raw_commit("a1", "Fix email typo", at(7))]
        self.prs = [raw_pr(2, "Add volunteer sign-up form", at(8))]

    def test_reused_across_ranges_with_same_content(self):
        week = report_for(self.commits, self.prs, preset="week")
        fp = activity.repo_fingerprint(week["rows"][0], MODEL)
        store = {"repos": {fp: entry("o/x", "Volunteers can sign up.")}, "portfolio": {}}
        for preset in ("week", "month", "quarter"):
            rep = report_for(self.commits, self.prs, preset=preset)
            activity.attach_summaries(rep, store, MODEL)
            ai = rep["rows"][0]["ai"]
            self.assertEqual(ai["text"], "Volunteers can sign up.", preset)
            self.assertFalse(ai["stale"], preset)
            self.assertEqual(rep["ai_targets"]["needs"], [], preset)

    def test_changed_work_shows_stale_for_same_slot_only(self):
        old = report_for(self.commits, self.prs)
        old_fp = activity.repo_fingerprint(old["rows"][0], MODEL)
        store = {"repos": {old_fp: entry("o/x", "Old paragraph.", slot="week")}, "portfolio": {}}
        new = report_for(self.commits + [raw_commit("b2", "New work", at(9))], self.prs)
        activity.attach_summaries(new, store, MODEL)
        ai = new["rows"][0]["ai"]
        self.assertEqual((ai["text"], ai["stale"]), ("Old paragraph.", True))
        self.assertEqual(new["ai_targets"]["needs"], ["o/x"])
        # A paragraph written for Quarter is never shown (even stale) on Week.
        store = {"repos": {old_fp: entry("o/x", "Quarter paragraph.", slot="quarter")}, "portfolio": {}}
        new2 = report_for(self.commits + [raw_commit("b2", "New work", at(9))], self.prs)
        activity.attach_summaries(new2, store, MODEL)
        self.assertIsNone(new2["rows"][0]["ai"])

    def test_newest_stale_wins_and_model_change_reads_stale(self):
        rep = report_for(self.commits, self.prs)
        fp = activity.repo_fingerprint(rep["rows"][0], MODEL)
        store = {"repos": {
            "older": entry("o/x", "Older.", when="2026-11-01T00:00:00+00:00"),
            "newer": entry("o/x", "Newer.", when="2026-11-08T00:00:00+00:00"),
            fp: entry("o/x", "Made with Haiku."),
        }, "portfolio": {}}
        activity.attach_summaries(rep, store, "claude-sonnet-5")
        ai = rep["rows"][0]["ai"]
        self.assertTrue(ai["stale"])
        self.assertEqual(ai["text"], "Made with Haiku.")  # newest in the slot

    def test_other_repos_entry_never_matches(self):
        rep = report_for(self.commits, self.prs)
        fp = activity.repo_fingerprint(rep["rows"][0], MODEL)
        store = {"repos": {fp: entry("o/someone-else", "Not yours.")}, "portfolio": {}}
        activity.attach_summaries(rep, store, MODEL)
        self.assertIsNone(rep["rows"][0]["ai"])

    def test_idle_rows_get_none_and_targets(self):
        rng = rng_for("week")
        d = activity.fetch_repo(FakeClient(commits={"x": self.commits}), repo("x"), "S")
        rep = activity.build_report([repo("x"), repo("idle")],
                                    {"o/x": d, "o/idle": {"commits": [], "prs": []}}, {}, rng)
        activity.attach_summaries(rep, {"repos": {}, "portfolio": {}}, MODEL)
        self.assertIsNone(rep["idle"][0]["ai"])
        self.assertEqual(rep["ai_targets"]["all"], ["o/x"])
        self.assertFalse(rep["ai_targets"]["portfolio_fresh"])
        self.assertIsNone(rep["ai_portfolio"])

    def test_portfolio_inputs_use_fresh_paragraphs_else_titles(self):
        rng = rng_for("week")
        da = activity.fetch_repo(FakeClient(commits={"a": self.commits}, prs={"a": self.prs}), repo("a"), "S")
        db = activity.fetch_repo(FakeClient(commits={"b": [raw_commit("b1", "Calendar export", at(6))]}),
                                 repo("b"), "S")
        rep = activity.build_report([repo("a"), repo("b")], {"o/a": da, "o/b": db}, {}, rng)
        fp_a = activity.repo_fingerprint(rep["rows"][0], MODEL)
        store = {"repos": {fp_a: entry("o/a", "Volunteers can sign up.")}, "portfolio": {}}
        activity.attach_summaries(rep, store, MODEL)
        notes, total = activity.portfolio_inputs(rep)
        by = {n["repo"]: n for n in notes}
        self.assertEqual(total, 2)
        self.assertEqual(by["o/a"]["summary"], "Volunteers can sign up.")
        self.assertEqual(by["o/b"]["titles"], ["Calendar export"])
        prompt = activity.build_portfolio_prompt(notes, total)
        self.assertIn("Scope: every project. 2 projects had work.", prompt)
        self.assertIn("Summary: Volunteers can sign up.", prompt)
        self.assertIn("- Calendar export", prompt)

    def test_portfolio_fingerprint_tracks_inputs_and_group(self):
        rep = report_for(self.commits, self.prs)
        activity.attach_summaries(rep, {"repos": {}, "portfolio": {}}, MODEL)
        notes, total = activity.portfolio_inputs(rep)
        base = activity.portfolio_fingerprint(notes, total, "", MODEL)
        self.assertEqual(base, activity.portfolio_fingerprint(notes, total, "", MODEL))
        self.assertNotEqual(base, activity.portfolio_fingerprint(notes, total, "Church", MODEL))
        self.assertNotEqual(base, activity.portfolio_fingerprint(notes, total, "", "claude-sonnet-5"))
        with_text = [dict(notes[0], summary="A paragraph.")]
        self.assertNotEqual(base, activity.portfolio_fingerprint(with_text, total, "", MODEL))
        self.assertNotEqual(activity.portfolio_fingerprint([dict(notes[0], summary="One.")], total, "", MODEL),
                            activity.portfolio_fingerprint([dict(notes[0], summary="Two.")], total, "", MODEL))

    def test_portfolio_fresh_and_stale(self):
        rep = report_for(self.commits, self.prs)
        activity.attach_summaries(rep, {"repos": {}, "portfolio": {}}, MODEL, "Church")
        pfp = rep["ai_targets"]["fingerprint"]
        store = {"repos": {}, "portfolio": {pfp: {"group": "Church", "slot": "week", "text": "Overview.",
                                                  "generated_at": "2026-11-09T00:00:00+00:00"}}}
        activity.attach_summaries(rep, store, MODEL, "Church")
        self.assertEqual(rep["ai_portfolio"]["text"], "Overview.")
        self.assertTrue(rep["ai_targets"]["portfolio_fresh"])
        # The same overview for another group is not borrowed, even stale.
        activity.attach_summaries(rep, store, MODEL, "School")
        self.assertIsNone(rep["ai_portfolio"])
        # Changed work in the same group + slot: shown, marked stale.
        rep2 = report_for(self.commits + [raw_commit("n", "New", at(9))], self.prs)
        activity.attach_summaries(rep2, store, MODEL, "Church")
        self.assertTrue(rep2["ai_portfolio"]["stale"])
        self.assertFalse(rep2["ai_targets"]["portfolio_fresh"])

    def test_portfolio_caps_repo_count(self):
        rng = rng_for("week")
        repos_ = [repo(f"r{i:02d}") for i in range(activity.PORTFOLIO_MAX_REPOS + 5)]
        data = {f"o/{r['name']}": activity.fetch_repo(
            FakeClient(commits={r["name"]: [raw_commit(f"s{r['name']}", f"Work on {r['name']}", at(8))]}), r, "S")
            for r in repos_}
        rep = activity.build_report(repos_, data, {}, rng)
        activity.attach_summaries(rep, {"repos": {}, "portfolio": {}}, MODEL)
        notes, total = activity.portfolio_inputs(rep)
        self.assertEqual((len(notes), total), (activity.PORTFOLIO_MAX_REPOS, activity.PORTFOLIO_MAX_REPOS + 5))
        self.assertIn("Note: 5 smaller projects with work are left out", activity.build_portfolio_prompt(notes, total))


# ----------------------------------------------------------------------
# Markdown export
# ----------------------------------------------------------------------

class TestMarkdown(unittest.TestCase):

    def test_includes_overview_and_repo_paragraphs(self):
        rep = report_for([raw_commit("a1", "Fix email typo", at(7))])
        fp = activity.repo_fingerprint(rep["rows"][0], MODEL)
        activity.attach_summaries(rep, {"repos": {fp: entry("o/x", "Emails read right now.")}, "portfolio": {}}, MODEL)
        rep["ai_portfolio"] = {"text": "A quiet week of fixes.", "stale": False}
        md = activity.compose_markdown(rep)
        self.assertIn("## What we shipped\n\nA quiet week of fixes.\n", md)
        self.assertIn("## x — 1 commit", md)
        self.assertIn("**In plain English:** Emails read right now.\n", md)
        self.assertLess(md.index("What we shipped"), md.index("## x —"))
        self.assertLess(md.index("In plain English"), md.index("Commits:"))
        self.assertNotIn("written before the latest changes", md)

    def test_stale_marked_and_absent_when_none(self):
        rep = report_for([raw_commit("a1", "Fix email typo", at(7))])
        activity.attach_summaries(rep, {"repos": {}, "portfolio": {}}, MODEL)
        md = activity.compose_markdown(rep)
        self.assertNotIn("What we shipped", md)
        self.assertNotIn("In plain English", md)
        rep["rows"][0]["ai"] = {"text": "Old.", "stale": True}
        rep["ai_portfolio"] = {"text": "Old overview.", "stale": True}
        md = activity.compose_markdown(rep)
        self.assertIn("**In plain English:** Old. _(written before the latest changes)_", md)
        self.assertIn("_Written before the latest changes in this range", md)


# ----------------------------------------------------------------------
# Storage (models.py S24-D)
# ----------------------------------------------------------------------

class TestStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._path = models.ACTIVITY_SUMMARIES_PATH
        models.ACTIVITY_SUMMARIES_PATH = os.path.join(self.tmp, "activity_summaries.json")

    def tearDown(self):
        models.ACTIVITY_SUMMARIES_PATH = self._path

    def test_empty_and_malformed(self):
        self.assertEqual(models.get_activity_summaries(), {"repos": {}, "portfolio": {}})
        with open(models.ACTIVITY_SUMMARIES_PATH, "w") as f:
            json.dump({"repos": ["not", "a", "dict"], "portfolio": {"ok": {"text": "x"}, "bad": 3}}, f)
        self.assertEqual(models.get_activity_summaries(), {"repos": {}, "portfolio": {"ok": {"text": "x"}}})
        with open(models.ACTIVITY_SUMMARIES_PATH, "w") as f:
            f.write("{not json")
        self.assertEqual(models.get_activity_summaries(), {"repos": {}, "portfolio": {}})

    def test_save_stamps_and_round_trips(self):
        saved = models.save_activity_repo_summary("fp1", {"repo": "o/x", "text": "Hi."})
        self.assertIn("generated_at", saved)
        models.save_activity_portfolio_summary("pf1", {"group": "", "text": "All."})
        store = models.get_activity_summaries()
        self.assertEqual(store["repos"]["fp1"]["text"], "Hi.")
        self.assertEqual(store["portfolio"]["pf1"]["text"], "All.")

    def test_cap_drops_oldest(self):
        data = {"repos": {f"fp{i:04d}": {"repo": "o/x", "text": str(i),
                                         "generated_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"}
                          for i in range(models.ACTIVITY_SUMMARIES_MAX + 10)},
                "portfolio": {}}
        with open(models.ACTIVITY_SUMMARIES_PATH, "w") as f:
            json.dump(data, f)
        models.save_activity_repo_summary("newest", {"repo": "o/x", "text": "new"})
        repos_ = models.get_activity_summaries()["repos"]
        self.assertEqual(len(repos_), models.ACTIVITY_SUMMARIES_MAX)
        self.assertIn("newest", repos_)
        self.assertNotIn("fp0000", repos_)
        self.assertNotIn("fp0010", repos_)
        self.assertIn("fp0011", repos_)


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------

API_KEY = "sk-ant-test-key-0123456789"


class TestSummarizeRoutes(unittest.TestCase):

    def setUp(self):
        import app as app_mod
        self.m = app_mod
        app_mod.app.config["TESTING"] = True
        self.client = app_mod.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["github_user"] = "tester"
        self.tmp = tempfile.mkdtemp()
        self._paths = {p: getattr(models, p) for p in
                       ("GROUPS_PATH", "_LEGACY_GROUPS_PATH", "PREFS_PATH", "ACTIVITY_LOG_PATH",
                        "ACTIVITY_SUMMARIES_PATH")}
        models.GROUPS_PATH = os.path.join(self.tmp, "groups.json")
        models._LEGACY_GROUPS_PATH = os.path.join(self.tmp, "legacy.json")
        models.PREFS_PATH = os.path.join(self.tmp, "prefs.json")
        models.ACTIVITY_LOG_PATH = os.path.join(self.tmp, "activity.log")
        models.ACTIVITY_SUMMARIES_PATH = os.path.join(self.tmp, "activity_summaries.json")
        self._orig = (app_mod._scan_results, app_mod._github_client, app_mod._activity_cache,
                      app_mod._credentials, app_mod._session_cost)
        now = datetime.datetime.now(datetime.timezone.utc)
        self.hour_ago = utc_iso(now - datetime.timedelta(hours=1))
        self.fake = FakeClient(
            commits={"alpha": [raw_commit("a1b2c3d4", "Alpha work", self.hour_ago)],
                     "beta": [raw_commit("b1", "Beta work", utc_iso(now - datetime.timedelta(days=40)))]},
            prs={"alpha": [raw_pr(2, "Alpha PR", self.hour_ago)]})
        app_mod._github_client = self.fake
        app_mod._credentials = {"github_pat": "ghp_x", "anthropic_key": API_KEY}
        app_mod._session_cost = models.SessionCost()
        app_mod._activity_cache = None
        app_mod._activity_ranges = {}
        app_mod._scan_results = {"repos": [repo("alpha"), repo("beta"), repo("gamma")],
                                 "scanned_at": now.isoformat(), "total_branches": 3}
        self.repo_calls = []
        self.portfolio_calls = []
        self.repo_reply = lambda row: f"Paragraph for {row['name']} ({len(row['commits'])} commits)."
        self.p_repo = patch.object(activity, "summarize_repo", side_effect=self._fake_repo)
        self.p_portfolio = patch.object(activity, "summarize_portfolio", side_effect=self._fake_portfolio)
        self.p_repo.start()
        self.p_portfolio.start()

    def tearDown(self):
        self.p_repo.stop()
        self.p_portfolio.stop()
        (self.m._scan_results, self.m._github_client, self.m._activity_cache,
         self.m._credentials, self.m._session_cost) = self._orig
        self.m._activity_ranges = {}
        for p, v in self._paths.items():
            setattr(models, p, v)

    def _fake_repo(self, api_key, row, model=activity.DEFAULT_MODEL):
        self.repo_calls.append((api_key, row["full_name"], model))
        return {"text": self.repo_reply(row), "items": 2, "total_items": 2, "capped": False,
                "_usage": {"input_tokens": 1000, "output_tokens": 100, "model": model}}

    def _fake_portfolio(self, api_key, notes, total, group="", model=activity.DEFAULT_MODEL):
        self.portfolio_calls.append({"notes": notes, "total": total, "group": group})
        return {"text": f"Overview of {total} repo(s) for {group or 'all'}.",
                "_usage": {"input_tokens": 2000, "output_tokens": 200, "model": model}}

    def post(self, url, body=None):
        r = self.client.post(url, json=body if body is not None else {"range": "week", "group": ""})
        return r, r.get_json()

    def events(self, name=None):
        evs = models.tail_activity_log(200)
        return [e for e in evs if name is None or e["event"] == name]

    # --- auth / connection ---------------------------------------------

    def test_auth_required_json_401(self):
        anon = self.m.app.test_client()
        for url in ("/api/stats/summarize/o/alpha", "/api/stats/summarize-portfolio"):
            r = anon.post(url, json={})
            self.assertEqual(r.status_code, 401, url)
            self.assertIn("Session expired", r.get_json()["error"])
        self.assertEqual(self.repo_calls, [])

    def test_not_connected_503(self):
        self.m._credentials = None
        for url in ("/api/stats/summarize/o/alpha", "/api/stats/summarize-portfolio"):
            r, data = self.post(url)
            self.assertEqual(r.status_code, 503, url)
            self.assertIn("Not connected", data["error"])
        # Key present but GitHub lost and nothing cached: still 503, not a crash.
        self.m._credentials = {"anthropic_key": API_KEY}
        self.m._github_client = None
        r, data = self.post("/api/stats/summarize/o/alpha")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.repo_calls, [])

    def test_no_scan_and_unknown_repo_404(self):
        r, data = self.post("/api/stats/summarize/o/nope")
        self.assertEqual(r.status_code, 404)
        self.assertIn("isn't in the latest scan", data["error"])
        self.m._scan_results = None
        r, data = self.post("/api/stats/summarize/o/alpha")
        self.assertEqual(r.status_code, 404)

    def test_github_auth_error_is_json_401(self):
        self.fake.auth_fail = {"alpha"}
        r, data = self.post("/api/stats/summarize/o/alpha")
        self.assertEqual(r.status_code, 401)
        self.assertIn("GitHub authentication failed", data["error"])

    # --- generation, reuse, force, cost, logging ------------------------

    def test_generates_saves_logs_and_adds_cost(self):
        r, data = self.post("/api/stats/summarize/o/alpha")
        self.assertEqual(r.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(self.repo_calls, [(API_KEY, "o/alpha", MODEL)])
        store = models.get_activity_summaries()["repos"]
        saved = store[data["fingerprint"]]
        self.assertEqual(saved["repo"], "o/alpha")
        self.assertEqual(saved["slot"], "week")
        self.assertEqual(saved["text"], "Paragraph for alpha (1 commits).")
        self.assertEqual(saved["model"], MODEL)
        cost = self.m._session_cost.to_dict()
        self.assertEqual((cost["total_input_tokens"], cost["total_output_tokens"], cost["analyses_count"]),
                         (1000, 100, 1))
        self.assertAlmostEqual(cost["total_cost"], 0.0015)
        ev = self.events("summarize")[-1]
        for k in ("repo", "range", "fingerprint", "input_tokens", "output_tokens", "cost", "elapsed_ms"):
            self.assertIn(k, ev)
        self.assertEqual(ev["fingerprint"], data["fingerprint"])
        self.assertTrue(ev["range"].startswith("week:"))
        self.assertNotIn(API_KEY, open(models.ACTIVITY_LOG_PATH).read())

    def test_fresh_is_skipped_force_regenerates(self):
        self.post("/api/stats/summarize/o/alpha")
        r, data = self.post("/api/stats/summarize/o/alpha")
        self.assertEqual(data.get("skipped"), "fresh")
        self.assertEqual(len(self.repo_calls), 1)
        self.assertIn("summarize_skip", [e["event"] for e in self.events()])
        self.repo_reply = lambda row: "Rewritten."
        r, data = self.post("/api/stats/summarize/o/alpha", {"range": "week", "group": "", "force": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.repo_calls), 2)
        self.assertEqual(models.get_activity_summaries()["repos"][data["fingerprint"]]["text"], "Rewritten.")
        self.assertEqual(self.m._session_cost.to_dict()["analyses_count"], 2)

    def test_same_work_in_another_range_is_reused(self):
        _, first = self.post("/api/stats/summarize/o/alpha", {"range": "week"})
        _, again = self.post("/api/stats/summarize/o/alpha", {"range": "month"})
        self.assertEqual(again.get("skipped"), "fresh")
        self.assertEqual(again["fingerprint"], first["fingerprint"])
        self.assertEqual(len(self.repo_calls), 1)
        body = self.client.get("/stats?range=month").data.decode()
        self.assertIn("Paragraph for alpha (1 commits).", body)
        self.assertNotIn(">STALE<", body)

    def test_ai_failure_502_nothing_cached_logged(self):
        def boom(*a, **k):
            raise api_error(anthropic.AuthenticationError, 401)
        with patch.object(activity, "summarize_repo", side_effect=boom):
            r, data = self.post("/api/stats/summarize/o/alpha")
        self.assertEqual(r.status_code, 502)
        self.assertIn("rejected the API key", data["error"])
        self.assertEqual(models.get_activity_summaries()["repos"], {})
        self.assertEqual(self.m._session_cost.to_dict()["analyses_count"], 0)
        err = self.events("summarize_error")[-1]
        self.assertEqual(err["repo"], "o/alpha")
        self.assertIn("AuthenticationError", err["error"])

    def test_ai_error_messages_are_readable(self):
        cases = [(api_error(anthropic.RateLimitError, 429), "rate limit"),
                 (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), "Couldn't reach Anthropic"),
                 (api_error(anthropic.InternalServerError, 500), "error (500)"),
                 (api_error(anthropic.BadRequestError, 400), "rejected the request (400): boom"),
                 (ValueError("The AI declined this request (safety refusal)."), "safety refusal")]
        for exc, phrase in cases:
            with patch.object(activity, "summarize_repo", side_effect=exc):
                r, data = self.post("/api/stats/summarize/o/alpha")
            self.assertEqual(r.status_code, 502)
            self.assertIn(phrase, data["error"])
            # The overlay's queue stops on "log in/out" text — only a bad key should.
            self.assertNotRegex(data["error"].lower(), r"log in|log out")

    def test_api_key_is_redacted_from_logged_errors(self):
        with patch.object(activity, "summarize_repo", side_effect=RuntimeError(f"bad header {API_KEY}")):
            r, _ = self.post("/api/stats/summarize/o/alpha")
        self.assertEqual(r.status_code, 502)
        log = open(models.ACTIVITY_LOG_PATH).read()
        self.assertNotIn(API_KEY, log)
        self.assertIn("[redacted]", log)

    def test_unreadable_repo_is_502_and_idle_is_skipped(self):
        self.fake.fail = {"alpha"}
        r, data = self.post("/api/stats/summarize/o/alpha")
        self.assertEqual(r.status_code, 502)
        self.assertIn("GitHub couldn't be read for alpha", data["error"])
        self.assertEqual(self.repo_calls, [])
        r, data = self.post("/api/stats/summarize/o/gamma")
        self.assertEqual((r.status_code, data.get("skipped")), (200, "nothing"))

    def test_bad_body_falls_back_to_week(self):
        for body in ({"range": ["x"], "from": {"a": 1}}, {"force": "yes"}, []):
            r = self.client.post("/api/stats/summarize/o/alpha", json=body)
            self.assertIn(r.status_code, (200,), body)
        r = self.client.post("/api/stats/summarize/o/alpha", data="not json",
                             content_type="text/plain")
        self.assertEqual(r.status_code, 200)

    # --- portfolio -----------------------------------------------------

    def test_portfolio_uses_paragraphs_then_skips_fresh_and_logs(self):
        self.post("/api/stats/summarize/o/alpha")
        r, data = self.post("/api/stats/summarize-portfolio")
        self.assertEqual(r.status_code, 200, data)
        call = self.portfolio_calls[-1]
        self.assertEqual([n["repo"] for n in call["notes"]], ["o/alpha"])
        self.assertEqual(call["notes"][0]["summary"], "Paragraph for alpha (1 commits).")
        self.assertEqual(call["group"], "")
        ev = self.events("summarize_portfolio")[-1]
        self.assertEqual((ev["from_paragraphs"], ev["repos"]), (1, 1))
        for k in ("range", "fingerprint", "input_tokens", "cost", "elapsed_ms"):
            self.assertIn(k, ev)
        r, data = self.post("/api/stats/summarize-portfolio")
        self.assertEqual(data.get("skipped"), "fresh")
        self.assertEqual(len(self.portfolio_calls), 1)
        r, data = self.post("/api/stats/summarize-portfolio", {"range": "week", "group": "", "force": True})
        self.assertEqual(len(self.portfolio_calls), 2)
        self.assertEqual(self.m._session_cost.to_dict()["analyses_count"], 3)

    def test_portfolio_without_paragraphs_uses_titles(self):
        self.post("/api/stats/summarize-portfolio")
        note = self.portfolio_calls[-1]["notes"][0]
        self.assertNotIn("summary", note)
        self.assertEqual(note["titles"], ["Alpha PR", "Alpha work"])

    def test_portfolio_is_scoped_to_the_group(self):
        models.set_group("Church", ["beta"])
        self.post("/api/stats/summarize-portfolio", {"range": "quarter", "group": "Church"})
        call = self.portfolio_calls[-1]
        self.assertEqual(([n["repo"] for n in call["notes"]], call["group"], call["total"]),
                         (["o/beta"], "Church", 1))
        self.post("/api/stats/summarize-portfolio", {"range": "quarter", "group": ""})
        self.assertEqual(sorted(n["repo"] for n in self.portfolio_calls[-1]["notes"]), ["o/alpha", "o/beta"])
        # An unknown group is treated as All, never trusted as a scope.
        self.post("/api/stats/summarize-portfolio", {"range": "quarter", "group": "Nope", "force": True})
        self.assertEqual(self.portfolio_calls[-1]["group"], "")
        store = models.get_activity_summaries()["portfolio"]
        self.assertEqual(sorted(e["group"] for e in store.values()), ["", "Church"])

    def test_portfolio_failure_502(self):
        with patch.object(activity, "summarize_portfolio", side_effect=ValueError("Empty AI response")):
            r, data = self.post("/api/stats/summarize-portfolio")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(models.get_activity_summaries()["portfolio"], {})
        self.assertEqual(self.events("summarize_error")[-1]["repo"], "(overview)")

    def test_portfolio_nothing_updated(self):
        r, data = self.post("/api/stats/summarize-portfolio",
                            {"range": "custom", "from": "2020-01-01", "to": "2020-01-31"})
        self.assertEqual(data.get("skipped"), "nothing")

    # --- the page + export ---------------------------------------------

    def test_page_buttons_before_and_after(self):
        body = self.client.get("/stats").data.decode()
        self.assertIn('id="shipped-panel"', body)
        self.assertIn("SUMMARIZE (1)", body)
        self.assertNotIn('id="shipped-redo-btn"', body)
        self.assertIn("~$0.005 each on Haiku 4.5", body)
        cfg = json.loads(body.split('id="shipped-config" type="application/json">')[1].split("</script>")[0])
        self.assertEqual(cfg["needs"], ["o/alpha"])
        self.assertEqual(cfg["body"], {"range": "week", "group": ""})
        self.assertEqual(cfg["overview"], "Overview: all repos")

        self.post("/api/stats/summarize/o/alpha")
        body = self.client.get("/stats").data.decode()
        self.assertIn("SUMMARIZE OVERVIEW", body)
        self.assertIn("REDO ALL (1)", body)
        self.assertIn("Paragraph for alpha (1 commits).", body)

        self.post("/api/stats/summarize-portfolio")
        body = self.client.get("/stats").data.decode()
        self.assertIn("Overview of 1 repo(s) for all.", body)
        self.assertRegex(body, r'id="shipped-summarize-btn"\s+disabled')
        self.assertIn("SUMMARIZE (0)", body)
        md = body.split('id="activity-markdown" hidden readonly>')[1].split("</textarea>")[0]
        self.assertIn("## What we shipped", md)
        self.assertIn("**In plain English:** Paragraph for alpha", md)

    def test_stale_after_content_changes(self):
        self.post("/api/stats/summarize/o/alpha")
        self.post("/api/stats/summarize-portfolio")
        self.fake.commits["alpha"].append(raw_commit("zz99", "Brand new work", self.hour_ago))
        self.m._activity_cache = None  # next view re-reads GitHub
        body = self.client.get("/stats").data.decode()
        self.assertEqual(body.count(">STALE</span>"), 2, "repo card and overview")
        self.assertIn("SUMMARIZE (1)", body)
        self.assertIn("Paragraph for alpha (1 commits).", body)
        md = self.client.get("/stats/export.md").data.decode()
        self.assertIn("_(written before the latest changes)_", md)
        # Regenerate: fresh again.
        self.post("/api/stats/summarize/o/alpha")
        self.post("/api/stats/summarize-portfolio")
        body = self.client.get("/stats").data.decode()
        self.assertNotIn(">STALE</span>", body)
        self.assertIn("Paragraph for alpha (2 commits).", body)

    def test_group_page_config_and_text_escaped(self):
        models.set_group("Church", ["alpha"])
        self.repo_reply = lambda row: "<script>alert(1)</script> & done"
        self.post("/api/stats/summarize/o/alpha")
        body = self.client.get("/stats?group=Church").data.decode()
        self.assertIn("What we shipped · Church", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; done", body)
        cfg = json.loads(body.split('id="shipped-config" type="application/json">')[1].split("</script>")[0])
        self.assertEqual(cfg["body"]["group"], "Church")
        self.assertEqual(cfg["overview"], "Overview: Church group")

    def test_export_includes_paragraphs(self):
        self.post("/api/stats/summarize/o/alpha")
        self.post("/api/stats/summarize-portfolio")
        md = self.client.get("/stats/export.md").data.decode()
        self.assertIn("## What we shipped\n\nOverview of 1 repo(s) for all.", md)
        self.assertIn("**In plain English:** Paragraph for alpha (1 commits).", md)

    def test_offline_page_hides_buttons(self):
        self.client.get("/stats")
        self.m._github_client = None
        body = self.client.get("/stats").data.decode()
        self.assertIn('id="shipped-panel"', body)
        self.assertNotIn('id="shipped-summarize-btn"', body)
        self.assertIn("Log in again to write these", body)

    def test_unreadable_store_never_breaks_the_page(self):
        with patch.object(models, "get_activity_summaries", side_effect=OSError("disk gone")):
            r = self.client.get("/stats")
        self.assertEqual(r.status_code, 200)
        self.assertIn("SUMMARIZE (1)", r.data.decode())
        self.assertIn("summaries_load_error", [e["event"] for e in self.events()])

    def test_no_panel_when_nothing_updated(self):
        body = self.client.get("/stats?range=custom&from=2020-01-01&to=2020-01-31").data.decode()
        self.assertNotIn('id="shipped-panel"', body)
        self.assertNotIn("shipped-config", body)


if __name__ == "__main__":
    unittest.main()
