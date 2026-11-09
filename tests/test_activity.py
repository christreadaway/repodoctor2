"""Tests for the Stats → What changed view (activity.py + /stats routes).

Covers range resolution (presets, custom, bad input, DST, year boundaries),
the strict GitHub pagination it relies on, report building, the markdown
export, and the /stats + /stats/export.md routes including caching,
self-healing of failed repos, refresh, and the not-connected path.
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import activity
import github_client as gh
import models
from sessions import _LOCAL_TZ

TZ = _LOCAL_TZ or datetime.timezone.utc


def local(y, m, d, h=12, mi=0):
    return datetime.datetime(y, m, d, h, mi, tzinfo=TZ)


def utc_iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def raw_commit(sha, msg, when, merge=False, author="Chris"):
    return {"sha": sha, "html_url": f"https://github.com/o/r/commit/{sha}",
            "parents": [{}, {}] if merge else [{}],
            "commit": {"message": msg, "author": {"name": author, "date": when},
                       "committer": {"date": when}}}


def raw_pr(number, title, merged_at, updated_at=None):
    return {"number": number, "title": title, "merged_at": merged_at,
            "updated_at": updated_at or merged_at,
            "html_url": f"https://github.com/o/r/pull/{number}",
            "user": {"login": "chris"}, "base": {"ref": "main"}}


def repo(name, owner="o"):
    return {"owner": owner, "name": name, "full_name": f"{owner}/{name}",
            "default_branch": "main", "html_url": f"https://github.com/{owner}/{name}",
            "code_size_bytes": 1234}


# ----------------------------------------------------------------------
# Range resolution
# ----------------------------------------------------------------------

class TestResolveRange(unittest.TestCase):
    NOW = local(2026, 11, 9, 15, 30)

    def r(self, **args):
        return activity.resolve_range(args, now=self.NOW)

    def test_default_is_last_7_days_inclusive_of_today(self):
        rng, notice = self.r()
        self.assertIsNone(notice)
        self.assertEqual(rng.preset, "week")
        self.assertEqual(rng.start_date, datetime.date(2026, 11, 3))
        self.assertEqual(rng.end_date, datetime.date(2026, 11, 9))
        self.assertEqual(rng.days, 7)

    def test_presets(self):
        cases = {"week": (datetime.date(2026, 11, 3), 7),
                 "month": (datetime.date(2026, 10, 11), 30),
                 "quarter": (datetime.date(2026, 8, 12), 90),
                 "ytd": (datetime.date(2026, 1, 1), 313)}
        for preset, (start, days) in cases.items():
            rng, notice = self.r(range=preset)
            self.assertIsNone(notice, preset)
            self.assertEqual(rng.start_date, start, preset)
            self.assertEqual(rng.end_date, datetime.date(2026, 11, 9), preset)
            self.assertEqual(rng.days, days, preset)

    def test_preset_is_case_and_space_insensitive(self):
        rng, notice = self.r(range="  YTD ")
        self.assertEqual(rng.preset, "ytd")
        self.assertIsNone(notice)

    def test_ytd_on_jan_1_is_one_day(self):
        rng, _ = activity.resolve_range({"range": "ytd"}, now=local(2027, 1, 1, 0, 5))
        self.assertEqual((rng.start_date, rng.end_date, rng.days),
                         (datetime.date(2027, 1, 1), datetime.date(2027, 1, 1), 1))

    def test_custom_range(self):
        rng, notice = self.r(range="custom", **{"from": "2026-10-01", "to": "2026-10-31"})
        self.assertIsNone(notice)
        self.assertEqual(rng.preset, "custom")
        self.assertEqual(rng.days, 31)
        self.assertEqual(rng.dates_label, "Oct 1 – Oct 31, 2026")
        self.assertEqual(rng.query_args(), {"range": "custom", "from": "2026-10-01", "to": "2026-10-31"})

    def test_from_without_range_param_means_custom(self):
        rng, _ = self.r(**{"from": "2026-10-01", "to": "2026-10-02"})
        self.assertEqual(rng.preset, "custom")

    def test_known_preset_wins_over_stray_dates(self):
        rng, _ = self.r(range="month", **{"from": "2020-01-01"})
        self.assertEqual(rng.preset, "month")

    def test_custom_missing_to_runs_through_today(self):
        rng, notice = self.r(range="custom", **{"from": "2026-11-01"})
        self.assertIsNone(notice)
        self.assertEqual(rng.end_date, datetime.date(2026, 11, 9))

    def test_custom_single_day(self):
        rng, _ = self.r(range="custom", **{"from": "2026-11-05", "to": "2026-11-05"})
        self.assertEqual(rng.days, 1)
        self.assertEqual(rng.dates_label, "Nov 5, 2026")

    def test_custom_missing_from_falls_back_with_notice(self):
        rng, notice = self.r(range="custom", to="2026-11-01")
        self.assertEqual(rng.preset, "week")
        self.assertIn("needs a start date", notice)

    def test_custom_garbage_dates_fall_back_with_notice(self):
        for args in ({"from": "not-a-date"}, {"from": "2026-13-45"},
                     {"from": "2026-10-01", "to": "nope"}):
            rng, notice = self.r(range="custom", **args)
            self.assertEqual(rng.preset, "week", args)
            self.assertIn("isn't a date", notice, args)

    def test_reversed_dates_are_swapped_and_said(self):
        rng, notice = self.r(range="custom", **{"from": "2026-11-05", "to": "2026-11-01"})
        self.assertEqual((rng.start_date, rng.end_date),
                         (datetime.date(2026, 11, 1), datetime.date(2026, 11, 5)))
        self.assertIn("swapped", notice)

    def test_future_dates_clamp_to_today(self):
        rng, notice = self.r(range="custom", **{"from": "2026-11-01", "to": "2099-01-01"})
        self.assertEqual(rng.end_date, datetime.date(2026, 11, 9))
        self.assertIn("future", notice)
        rng, notice = self.r(range="custom", **{"from": "9999-12-31", "to": "9999-12-31"})
        self.assertEqual((rng.start_date, rng.end_date),
                         (datetime.date(2026, 11, 9), datetime.date(2026, 11, 9)))

    def test_pre_github_dates_clamp_to_2008(self):
        rng, notice = self.r(range="custom", **{"from": "0202-01-01", "to": "0303-01-01"})
        self.assertEqual(rng.start_date, datetime.date(2008, 1, 1))
        self.assertEqual(rng.end_date, datetime.date(2008, 1, 1))
        self.assertIn("2008", notice)

    def test_unknown_preset_falls_back_with_notice(self):
        rng, notice = self.r(range="decade")
        self.assertEqual(rng.preset, "week")
        self.assertIn("Unknown range", notice)

    def test_cross_year_label(self):
        rng, _ = activity.resolve_range({"range": "week"}, now=local(2027, 1, 2))
        self.assertEqual(rng.dates_label, "Dec 27, 2026 – Jan 2, 2027")

    def test_bounds_are_local_midnights(self):
        rng, _ = self.r(range="custom", **{"from": "2026-11-01", "to": "2026-11-01"})
        # Nov 1 2026 is the US DST change; the day is 25 hours long in Chicago.
        self.assertEqual(rng.since.hour, 0)
        self.assertEqual(rng.until.date(), datetime.date(2026, 11, 2))
        if _LOCAL_TZ:
            span = rng.until.astimezone(datetime.timezone.utc) - rng.since.astimezone(datetime.timezone.utc)
            self.assertEqual(span, datetime.timedelta(hours=25))

    def test_contains_is_half_open_in_local_time(self):
        rng, _ = self.r(range="custom", **{"from": "2026-11-03", "to": "2026-11-04"})
        self.assertTrue(rng.contains(utc_iso(local(2026, 11, 3, 0, 0))))
        self.assertTrue(rng.contains(utc_iso(local(2026, 11, 4, 23, 59))))
        self.assertFalse(rng.contains(utc_iso(local(2026, 11, 5, 0, 0))))
        self.assertFalse(rng.contains(utc_iso(local(2026, 11, 2, 23, 59))))
        self.assertFalse(rng.contains(""))
        self.assertFalse(rng.contains("garbage"))
        self.assertFalse(rng.contains(None))

    def test_default_floor_covers_rolling_presets_and_early_ytd(self):
        for now in (local(2026, 11, 9), local(2027, 1, 15), local(2026, 3, 31)):
            floor = activity.default_floor(now.date())
            for preset in ("week", "month", "quarter"):
                rng, _ = activity.resolve_range({"range": preset}, now=now)
                self.assertLessEqual(floor, rng.start_date, (now, preset))
        # YTD rides the same read from January through March...
        for now in (local(2027, 1, 15), local(2026, 3, 31)):
            rng, _ = activity.resolve_range({"range": "ytd"}, now=now)
            self.assertLessEqual(activity.default_floor(now.date()), rng.start_date)
        # ...and later in the year it needs a wider read (app widens once).
        rng, _ = activity.resolve_range({"range": "ytd"}, now=local(2026, 11, 9))
        self.assertGreater(activity.default_floor(datetime.date(2026, 11, 9)), rng.start_date)
        self.assertEqual(activity.default_floor(datetime.date(2027, 1, 15)).year, 2026)


# ----------------------------------------------------------------------
# Strict GitHub pagination
# ----------------------------------------------------------------------

def _resp(status, payload, text="", headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = text
    r.headers = headers or {}
    return r


class TestStrictPagination(unittest.TestCase):

    def client(self, pages):
        c = gh.GitHubClient("x")
        c._get = MagicMock(side_effect=pages)
        return c

    def test_empty_repo_409_is_empty_not_error(self):
        c = self.client([_resp(409, {"message": "Git Repository is empty."})])
        self.assertEqual(c.get_commits_range("o", "r", "2026-01-01T00:00:00Z"), ([], False))

    def test_failed_page_raises_instead_of_partial(self):
        c = self.client([_resp(200, [{"sha": str(i)} for i in range(100)]),
                         _resp(502, {})])
        with self.assertRaises(gh.GitHubFetchError) as ctx:
            c.get_commits_range("o", "r", "2026-01-01T00:00:00Z")
        self.assertIn("page 2", str(ctx.exception))
        self.assertIn("502", str(ctx.exception))

    def test_failure_hints_name_the_real_cause(self):
        cases = [
            (_resp(403, {}, headers={"X-RateLimit-Remaining": "0"}), "rate limited"),
            (_resp(403, {}, text='{"message":"You have exceeded a secondary rate limit"}'), "rate limited"),
            (_resp(429, {}), "rate limited"),
            # A SAML / org-policy block is NOT a rate limit — different fix.
            (_resp(403, {}, text='{"message":"Resource protected by organization SAML enforcement"}'), "access denied"),
            (_resp(404, {}), "not found"),
            (_resp(502, {}), "GitHub error"),
        ]
        for resp, needle in cases:
            c = self.client([resp])
            with self.assertRaises(gh.GitHubFetchError) as ctx:
                c.get_commits_range("o", "r", "x")
            self.assertIn(needle, str(ctx.exception), (resp.status_code, resp.text))
            if needle == "access denied":
                self.assertNotIn("rate limited", str(ctx.exception))

    def test_truncation_flag(self):
        full = [{"sha": str(i)} for i in range(100)]
        c = self.client([_resp(200, full), _resp(200, full)])
        items, truncated = c.get_commits_range("o", "r", "x", max_pages=2)
        self.assertEqual(len(items), 200)
        self.assertTrue(truncated)
        c = self.client([_resp(200, full), _resp(200, full[:3])])
        items, truncated = c.get_commits_range("o", "r", "x", max_pages=2)
        self.assertEqual(len(items), 103)
        self.assertFalse(truncated)

    def test_commit_params(self):
        c = self.client([_resp(200, [])])
        c.get_commits_range("o", "r", "S", ref="dev", until_iso="U")
        params = c._get.call_args.kwargs["params"]
        self.assertEqual((params["since"], params["until"], params["sha"]), ("S", "U", "dev"))

    def test_merged_prs_filter_and_stop_early(self):
        since = "2026-10-01T00:00:00Z"
        page1 = ([raw_pr(i, "in window", "2026-10-15T00:00:00Z") for i in range(50)]
                 + [{"number": 900, "title": "closed unmerged", "merged_at": None,
                     "updated_at": "2026-10-10T00:00:00Z"}]
                 + [raw_pr(1000 + i, "merged before, touched after", "2026-09-01T00:00:00Z",
                           updated_at="2026-10-05T00:00:00Z") for i in range(29)]
                 + [raw_pr(2000 + i, "old", "2026-08-01T00:00:00Z") for i in range(20)])
        c = self.client([_resp(200, page1), _resp(200, [raw_pr(1, "never fetched", "2026-10-20T00:00:00Z")])])
        merged, truncated, reached = c.get_merged_pulls_since("o", "r", since)
        self.assertEqual(reached, "2026-08-01T00:00:00Z")
        self.assertEqual(len(merged), 50)
        self.assertTrue(all(p["title"] == "in window" for p in merged))
        self.assertFalse(truncated)
        self.assertEqual(c._get.call_count, 1, "should stop once PRs predate the window")
        params = c._get.call_args.kwargs["params"]
        self.assertEqual((params["state"], params["sort"], params["direction"]), ("closed", "updated", "desc"))

    def test_merged_prs_page_on_when_still_in_window(self):
        page1 = [raw_pr(i, "a", "2026-10-15T00:00:00Z") for i in range(100)]
        page2 = [raw_pr(200, "b", "2026-10-02T00:00:00Z")]
        c = self.client([_resp(200, page1), _resp(200, page2)])
        merged, _, _ = c.get_merged_pulls_since("o", "r", "2026-10-01T00:00:00Z")
        self.assertEqual(len(merged), 101)


# ----------------------------------------------------------------------
# Fetch + gather
# ----------------------------------------------------------------------

class FakeClient:
    def __init__(self, commits=None, prs=None, fail=None, auth_fail=None):
        self.commits = commits or {}
        self.prs = prs or {}
        self.fail = set(fail or ())
        self.auth_fail = set(auth_fail or ())
        self.calls = []

    def get_commits_range(self, owner, name, since_iso, ref=None, until_iso=None, max_pages=50):
        self.calls.append((name, since_iso, until_iso))
        if name in self.auth_fail:
            raise gh.GitHubAuthError("401")
        if name in self.fail:
            raise gh.GitHubFetchError(f"commits {owner}/{name} page 1: HTTP 502")
        return list(self.commits.get(name, [])), False

    def get_merged_pulls_since(self, owner, name, since_iso, max_pages=10):
        return list(self.prs.get(name, [])), False, ""


class TestFetchAndGather(unittest.TestCase):

    def test_fetch_repo_normalizes(self):
        c = FakeClient(commits={"a": [raw_commit("abcdef123456", "Subject line\n\nbody", "2026-11-01T10:00:00Z")]},
                       prs={"a": [raw_pr(5, "  Title  ", "2026-11-01T11:00:00Z")]})
        d = activity.fetch_repo(c, repo("a"), "2026-10-01T00:00:00Z")
        self.assertEqual(d["commits"][0]["summary"], "Subject line")
        self.assertEqual(d["commits"][0]["short"], "abcdef1")
        self.assertNotIn("message", d["commits"][0])
        self.assertEqual(d["commits"][0]["html_url"], "https://github.com/o/r/commit/abcdef123456")
        self.assertEqual(d["prs"][0]["title"], "Title")
        self.assertEqual(d["prs"][0]["author"], "chris")
        self.assertFalse(d["commits_truncated"])

    def test_gather_isolates_failures_and_logs(self):
        events = []
        c = FakeClient(commits={"a": [raw_commit("1", "x", "2026-11-01T10:00:00Z")]}, fail={"b"})
        data, errors = activity.gather(c, [repo("a"), repo("b"), repo("c")], "S",
                                       log_event=lambda e, **f: events.append((e, f)))
        self.assertEqual(set(data), {"o/a", "o/c"})
        self.assertIn("HTTP 502", errors["o/b"])
        kinds = [e for e, _ in events]
        self.assertIn("fetch_error", kinds)
        fetch = dict(events)["fetch"]
        self.assertEqual((fetch["repos"], fetch["ok"], fetch["errors"]), (3, 2, 1))

    def test_gather_propagates_auth_error(self):
        c = FakeClient(auth_fail={"b"})
        with self.assertRaises(gh.GitHubAuthError):
            activity.gather(c, [repo("a"), repo("b")], "S")

    def test_gather_empty(self):
        self.assertEqual(activity.gather(FakeClient(), [], "S"), ({}, {}))


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------

class TestBuildReport(unittest.TestCase):
    NOW = local(2026, 11, 9, 15)

    def setUp(self):
        self.rng, _ = activity.resolve_range({"range": "week"}, now=self.NOW)  # Nov 3-9

    def data_for(self, commits=(), prs=(), truncated=False, reached=""):
        c = FakeClient(commits={"x": list(commits)}, prs={"x": list(prs)})
        d = activity.fetch_repo(c, repo("x"), "S")
        d["commits_truncated"] = truncated
        d["commits_reached"] = reached
        return d

    def test_counts_exclude_merges_and_out_of_range(self):
        d = self.data_for(
            commits=[raw_commit("1", "in range A", utc_iso(local(2026, 11, 8, 9))),
                     raw_commit("2", "in range B", utc_iso(local(2026, 11, 3, 0, 1))),
                     raw_commit("3", "Merge pull request #4", utc_iso(local(2026, 11, 8, 10)), merge=True),
                     raw_commit("4", "too old", utc_iso(local(2026, 11, 2, 23, 59)))],
            prs=[raw_pr(4, "Feature", utc_iso(local(2026, 11, 8, 10))),
                 raw_pr(3, "Old feature", utc_iso(local(2026, 10, 1)))])
        rep = activity.build_report([repo("x")], {"o/x": d}, {}, self.rng)
        row = rep["rows"][0]
        self.assertEqual(row["commit_count"], 2)
        self.assertEqual(row["merge_count"], 1)
        self.assertEqual([c["summary"] for c in row["commits"]], ["in range A", "in range B"])
        self.assertEqual(row["pr_count"], 1)
        self.assertEqual(row["active_days"], 2)  # Nov 8 + Nov 3
        self.assertEqual(row["last_at"], utc_iso(local(2026, 11, 8, 10)))
        self.assertTrue(row["updated"])
        self.assertIn("/commits/main?since=2026-11-03&until=2026-11-09", row["commits_url"])

    def test_pr_only_repo_counts_as_updated(self):
        d = self.data_for(prs=[raw_pr(1, "Squashed long ago commits", utc_iso(local(2026, 11, 5)))])
        rep = activity.build_report([repo("x")], {"o/x": d}, {}, self.rng)
        self.assertEqual(rep["totals"]["repos_updated"], 1)
        self.assertEqual(rep["totals"]["commits"], 0)

    def test_idle_failed_and_updated_are_disjoint(self):
        d_up = self.data_for(commits=[raw_commit("1", "a", utc_iso(local(2026, 11, 8)))])
        rep = activity.build_report(
            [repo("up"), repo("idle"), repo("down")],
            {"o/up": d_up, "o/idle": {"commits": [], "prs": []}},
            {"o/down": "GitHubFetchError: HTTP 502"}, self.rng)
        self.assertEqual([r["name"] for r in rep["updated"]], ["up"])
        self.assertEqual([r["name"] for r in rep["idle"]], ["idle"])
        self.assertEqual([r["name"] for r in rep["failed"]], ["down"])
        t = rep["totals"]
        self.assertEqual((t["repos_total"], t["repos_updated"], t["errors"]), (3, 1, 1))

    def test_sorting_and_portfolio_active_days(self):
        def commits(n, day):
            return [raw_commit(f"{day}{i}", "c", utc_iso(local(2026, 11, day, 10 + i))) for i in range(n)]
        data = {"o/a": self.data_for(commits=commits(1, 4)),
                "o/b": self.data_for(commits=commits(3, 4)),
                "o/c": self.data_for(commits=commits(1, 6))}
        rep = activity.build_report([repo("a"), repo("b"), repo("c"), repo("z")], data, {}, self.rng)
        self.assertEqual([r["name"] for r in rep["updated"]], ["b", "a", "c"])
        self.assertEqual(rep["totals"]["active_days"], 2)  # Nov 4 + Nov 6 across repos
        self.assertEqual(rep["totals"]["commits"], 5)

    def test_capped_read_that_reaches_past_the_range_is_complete(self):
        # The cap was hit, but the read got back to Oct 1 — the week is whole.
        d = self.data_for(commits=[raw_commit("1", "a", utc_iso(local(2026, 11, 8)))],
                          truncated=True, reached=utc_iso(local(2026, 10, 1)))
        rep = activity.build_report([repo("x")], {"o/x": d}, {}, self.rng)
        self.assertFalse(rep["rows"][0]["incomplete"])
        self.assertEqual(rep["incomplete"], [])

    def test_capped_read_that_stops_inside_the_range_is_partial(self):
        d = self.data_for(commits=[raw_commit("1", "a", utc_iso(local(2026, 11, 8)))],
                          truncated=True, reached=utc_iso(local(2026, 11, 6)))
        rep = activity.build_report([repo("x")], {"o/x": d}, {}, self.rng)
        row = rep["rows"][0]
        self.assertTrue(row["updated"])
        self.assertTrue(row["incomplete"])
        self.assertEqual(rep["totals"]["incomplete"], 1)
        self.assertIn("**Partial:** x", activity.compose_markdown(rep))

    def test_capped_read_that_never_reached_the_range_is_partial_not_idle(self):
        # 5,000 newer commits used the whole cap; nothing seen in the range.
        d = self.data_for(commits=[raw_commit("1", "newer", utc_iso(local(2026, 11, 20)))],
                          truncated=True, reached=utc_iso(local(2026, 11, 15)))
        rep = activity.build_report([repo("x")], {"o/x": d}, {}, self.rng)
        self.assertEqual(rep["idle"], [])
        self.assertEqual([r["name"] for r in rep["partial"]], ["x"])
        md = activity.compose_markdown(rep)
        self.assertNotIn("Nothing landed", md)

    def test_capped_pr_read_is_range_aware(self):
        d = self.data_for(prs=[raw_pr(1, "p", utc_iso(local(2026, 11, 8)))])
        d["prs_truncated"], d["prs_reached"] = True, utc_iso(local(2026, 11, 5))
        rep = activity.build_report([repo("x")], {"o/x": d}, {}, self.rng)
        self.assertTrue(rep["rows"][0]["incomplete"])
        d["prs_reached"] = utc_iso(local(2026, 11, 2))
        rep = activity.build_report([repo("x")], {"o/x": d}, {}, self.rng)
        self.assertFalse(rep["rows"][0]["incomplete"])

    def test_fetch_repo_records_how_far_a_capped_read_reached(self):
        class Capped(FakeClient):
            def get_commits_range(self, *a, **k):
                return [raw_commit("2", "b", "2026-11-08T00:00:00Z"),
                        raw_commit("1", "a", "2026-11-06T00:00:00Z")], True

            def get_merged_pulls_since(self, *a, **k):
                return [], True, "2026-11-05T00:00:00Z"
        d = activity.fetch_repo(Capped(), repo("x"), "S")
        self.assertEqual(d["commits_reached"], "2026-11-06T00:00:00Z")
        self.assertEqual(d["prs_reached"], "2026-11-05T00:00:00Z")
        d = activity.fetch_repo(FakeClient(commits={"x": [raw_commit("1", "a", "2026-11-06T00:00:00Z")]}), repo("x"), "S")
        self.assertEqual((d["commits_reached"], d["prs_reached"]), ("", ""))

    def test_merge_without_pr_counts_as_updated(self):
        # Branch commits predate the range; the merge (no PR) landed inside it.
        d = self.data_for(commits=[
            raw_commit("m1", "Merge branch 'feature'", utc_iso(local(2026, 11, 4, 9)), merge=True),
            raw_commit("b1", "branch work", utc_iso(local(2026, 10, 30)))])
        rep = activity.build_report([repo("x")], {"o/x": d}, {}, self.rng)
        row = rep["rows"][0]
        self.assertTrue(row["updated"])
        self.assertEqual((row["commit_count"], row["merge_count"], row["active_days"]), (0, 1, 1))
        self.assertEqual(rep["idle"], [])
        md = activity.compose_markdown(rep)
        self.assertIn("## x — 0 commits, 1 merge", md)
        self.assertIn("Merges (no PR):", md)
        self.assertIn("Merge branch 'feature'", md)

    def test_repo_never_read_is_unknown_not_idle(self):
        # e.g. a scan finished mid-read and added a repo the cache never saw
        rep = activity.build_report([repo("x")], {}, {}, self.rng)
        self.assertEqual(rep["totals"]["repos_updated"], 0)
        self.assertEqual(rep["idle"], [])
        self.assertIn("Not read yet", rep["failed"][0]["error"])


class TestTimeline(unittest.TestCase):

    def report(self, now, args, commit_days):
        rng, _ = activity.resolve_range(args, now=now)
        commits = [raw_commit(str(i), "c", utc_iso(local(*d, 12))) for i, d in enumerate(commit_days)]
        d = activity.fetch_repo(FakeClient(commits={"x": commits}), repo("x"), "S")
        return activity.build_report([repo("x")], {"o/x": d}, {}, rng)["timeline"]

    def test_daily_for_a_week(self):
        tl = self.report(local(2026, 11, 9), {"range": "week"}, [(2026, 11, 3), (2026, 11, 3), (2026, 11, 9)])
        self.assertEqual(tl["unit"], "day")
        self.assertEqual(len(tl["buckets"]), 7)
        self.assertEqual(tl["buckets"][0]["commits"], 2)
        self.assertEqual(tl["buckets"][-1]["commits"], 1)
        self.assertEqual(tl["peak"], 2)
        self.assertEqual(sum(b["commits"] for b in tl["buckets"]), 3)

    def test_weekly_for_a_quarter_with_partial_last_week(self):
        tl = self.report(local(2026, 11, 9), {"range": "quarter"}, [(2026, 8, 12), (2026, 11, 9)])
        self.assertEqual(tl["unit"], "week")
        self.assertEqual(len(tl["buckets"]), 13)  # 12 full weeks + 6-day tail
        self.assertEqual(tl["buckets"][0]["start"], "2026-08-12")
        self.assertEqual(tl["buckets"][-1]["end"], "2026-11-09")
        self.assertEqual(sum(b["commits"] for b in tl["buckets"]), 2)

    def test_monthly_for_ytd(self):
        tl = self.report(local(2026, 11, 9), {"range": "ytd"}, [(2026, 1, 1), (2026, 2, 28), (2026, 11, 9)])
        self.assertEqual(tl["unit"], "month")
        self.assertEqual(len(tl["buckets"]), 11)
        self.assertEqual(tl["buckets"][0]["label"], "Jan 2026")
        self.assertEqual(tl["buckets"][1]["commits"], 1)
        self.assertEqual(tl["buckets"][-1]["end"], "2026-11-09")

    def test_quarters_then_years_for_long_ranges(self):
        tl = self.report(local(2026, 11, 9), {"range": "custom", "from": "2023-02-15", "to": "2026-11-09"},
                         [(2023, 3, 31), (2023, 4, 1), (2026, 11, 9)])
        self.assertEqual(tl["unit"], "quarter")
        labels = [b["label"] for b in tl["buckets"]]
        self.assertEqual((labels[0], labels[1], labels[-1]), ("Q1 2023", "Q2 2023", "Q4 2026"))
        self.assertEqual(tl["buckets"][0]["start"], "2023-02-15")
        self.assertEqual((tl["buckets"][0]["commits"], tl["buckets"][1]["commits"]), (1, 1))
        tl = self.report(local(2026, 11, 9), {"range": "custom", "from": "2008-01-01", "to": "2026-11-09"},
                         [(2008, 1, 1), (2026, 11, 9)])
        self.assertEqual(tl["unit"], "year")
        self.assertEqual([b["label"] for b in tl["buckets"]][:2], ["2008", "2009"])
        self.assertEqual(len(tl["buckets"]), 19)
        self.assertEqual((tl["buckets"][0]["commits"], tl["buckets"][-1]["commits"]), (1, 1))

    def test_no_range_makes_more_than_31_columns(self):
        now = local(2026, 11, 9)
        for start in ("2026-10-10", "2026-05-11", "2024-11-10", "2020-11-11", "2008-01-01",
                      "2025-01-01", "2023-01-01", "2016-01-01"):
            tl = self.report(now, {"range": "custom", "from": start, "to": "2026-11-09"}, [])
            self.assertLessEqual(len(tl["buckets"]), 31, (start, tl["unit"], len(tl["buckets"])))
            self.assertEqual(tl["buckets"][-1]["end"], "2026-11-09")
            # Buckets tile the range with no gaps or overlaps.
            for a, b in zip(tl["buckets"], tl["buckets"][1:]):
                self.assertEqual(datetime.date.fromisoformat(a["end"]) + datetime.timedelta(days=1),
                                 datetime.date.fromisoformat(b["start"]))

    def test_monthly_across_year_end_starts_mid_month(self):
        tl = self.report(local(2027, 2, 10),
                         {"range": "custom", "from": "2026-06-15", "to": "2027-02-10"},
                         [(2026, 12, 31), (2027, 1, 1)])
        labels = [b["label"] for b in tl["buckets"]]
        self.assertEqual(labels[0], "Jun 2026")
        self.assertIn("Dec 2026", labels)
        self.assertIn("Jan 2027", labels)
        self.assertEqual(tl["buckets"][0]["start"], "2026-06-15")
        by = {b["label"]: b["commits"] for b in tl["buckets"]}
        self.assertEqual((by["Dec 2026"], by["Jan 2027"]), (1, 1))


# ----------------------------------------------------------------------
# Markdown export
# ----------------------------------------------------------------------

class TestComposeMarkdown(unittest.TestCase):

    def setUp(self):
        self.rng, _ = activity.resolve_range({"range": "month"}, now=local(2026, 11, 9))

    def test_document_shape(self):
        commits = [raw_commit(f"{i:07d}aaa", f"Change {i}\n\nbody", utc_iso(local(2026, 11, 8, 12) - datetime.timedelta(hours=i)))
                   for i in range(60)]
        d = activity.fetch_repo(FakeClient(commits={"x": commits},
                                           prs={"x": [raw_pr(9, "Ship it\nwith newline", utc_iso(local(2026, 11, 8)))]}),
                                repo("x"), "S")
        rep = activity.build_report([repo("x"), repo("idle"), repo("down")],
                                    {"o/x": d, "o/idle": {"commits": [], "prs": []}},
                                    {"o/down": "boom"}, self.rng)
        md = activity.compose_markdown(rep, "chris", "Church", generated_at="Nov 09 3:00 PM")
        self.assertTrue(md.startswith("# What changed: Last 30 days (Oct 11 – Nov 9, 2026)"))
        self.assertIn("the **Church** group", md)
        self.assertIn("**1 of 3 repos updated** · 60 commits · 1 PR merged", md)
        self.assertIn("**Incomplete:** GitHub couldn't be read for down", md)
        self.assertIn("## x — 60 commits, 1 PR merged", md)
        self.assertIn("- #9 Ship it with newline", md)
        self.assertIn("· Change 0 (0000000)", md)
        self.assertIn("…and 10 more: https://github.com/o/x/commits/main?since=2026-10-11", md)
        self.assertNotIn("Change 55", md)
        self.assertIn("## Not updated in this range (1)", md)
        self.assertIn("\nidle\n", md)
        self.assertNotIn("down,", md.split("## Not updated")[1])

    def test_empty(self):
        rep = activity.build_report([repo("a")], {"o/a": {"commits": [], "prs": []}}, {}, self.rng)
        md = activity.compose_markdown(rep)
        self.assertIn("Nothing landed on any default branch in this range.", md)
        self.assertIn("**0 of 1 repos updated** · 0 commits · 0 PRs merged", md)


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------

class TestStatsRoutes(unittest.TestCase):

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
                       ("GROUPS_PATH", "_LEGACY_GROUPS_PATH", "PREFS_PATH", "ACTIVITY_LOG_PATH")}
        models.GROUPS_PATH = os.path.join(self.tmp, "groups.json")
        models._LEGACY_GROUPS_PATH = os.path.join(self.tmp, "legacy.json")
        models.PREFS_PATH = os.path.join(self.tmp, "prefs.json")
        models.ACTIVITY_LOG_PATH = os.path.join(self.tmp, "activity.log")
        self._orig = (app_mod._scan_results, app_mod._github_client, app_mod._activity_cache)
        self.now = datetime.datetime.now(datetime.timezone.utc)
        hour_ago = utc_iso(self.now - datetime.timedelta(hours=1))
        self.fake = FakeClient(
            commits={"alpha": [raw_commit("a1b2c3d4", "Alpha <b>bold</b> & \"quoted\"", hour_ago),
                               raw_commit("m", "Merge pull request #2", hour_ago, merge=True)],
                     "beta": [raw_commit("b1", "Beta work", utc_iso(self.now - datetime.timedelta(days=40)))]},
            prs={"alpha": [raw_pr(2, "Alpha PR", hour_ago)]})
        app_mod._github_client = self.fake
        app_mod._activity_cache = None
        app_mod._activity_ranges = {}
        app_mod._scan_results = {"repos": [repo("alpha"), repo("beta"), repo("gamma")],
                                 "scanned_at": self.now.isoformat(), "total_branches": 3}

    def tearDown(self):
        (self.m._scan_results, self.m._github_client, self.m._activity_cache) = self._orig
        self.m._activity_ranges = {}
        for p, v in self._paths.items():
            setattr(models, p, v)

    def get(self, url):
        r = self.client.get(url)
        return r, r.data.decode()

    def test_week_view(self):
        r, body = self.get("/stats")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Last 7 days", body)
        self.assertIn("Alpha PR", body)
        self.assertNotIn("Beta work", body)  # 40 days old
        self.assertIn("Not updated in this range (2)", body)
        self.assertIn('class="stats-kpi-value">1<span class="stats-kpi-of"> of 3', body)

    def test_commit_text_is_escaped(self):
        r, body = self.get("/stats")
        self.assertNotIn("<b>bold</b>", body)
        self.assertIn("Alpha &lt;b&gt;bold&lt;/b&gt;", body)
        # The JSON island and the markdown textarea can't be broken out of either.
        self.assertNotIn("</textarea><", body.split('id="activity-markdown"')[1].split("</textarea>")[0])

    def test_quarter_includes_older_work(self):
        r, body = self.get("/stats?range=quarter")
        self.assertIn("Beta work", body)
        self.assertIn("Commits per week", body)

    def _iso_floor(self, d):
        return activity._local_midnight(d).astimezone(datetime.timezone.utc).isoformat()

    def test_one_90_day_read_serves_rolling_presets_and_ytd_widens_once(self):
        today = activity._local_now().date()
        for preset in ("week", "month", "quarter"):
            self.assertEqual(self.client.get(f"/stats?range={preset}").status_code, 200)
        self.assertEqual(len(self.fake.calls), 3, "one read per repo serves Week, Month, Quarter")
        self.assertTrue(all(c[1] == self._iso_floor(activity.default_floor(today)) for c in self.fake.calls))
        self.fake.calls.clear()
        self.client.get("/stats?range=ytd")
        jan1 = datetime.date(today.year, 1, 1)
        widened = jan1 < activity.default_floor(today)
        self.assertEqual(len(self.fake.calls), 3 if widened else 0)
        self.fake.calls.clear()
        for preset in ("week", "month", "quarter", "ytd"):
            self.client.get(f"/stats?range={preset}")
        self.assertEqual(self.fake.calls, [], "after widening, every preset is a cache hit")

    def test_past_custom_range_gets_its_own_bounded_read(self):
        self.client.get("/stats")
        self.fake.calls.clear()
        r, body = self.get("/stats?range=custom&from=2020-01-01&to=2020-12-31")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.fake.calls), 3)
        self.assertTrue(all(c[1].startswith("2020-01-01") for c in self.fake.calls))
        self.assertTrue(all(c[2] and c[2].startswith("2021-01-01") for c in self.fake.calls),
                        "a past range is read with an `until`, so newer history can't crowd it out")
        self.fake.calls.clear()
        self.client.get("/stats?range=custom&from=2020-01-01&to=2020-12-31")
        self.assertEqual(self.fake.calls, [], "same past range again is a cache hit")
        # The main cache was not widened to 2020.
        self.assertGreater(self.m._activity_cache["floor"], datetime.date(2020, 12, 31))

    def test_custom_range_through_today_widens_the_main_cache(self):
        today = activity._local_now().date().isoformat()
        self.client.get(f"/stats?range=custom&from=2025-06-01&to={today}")
        self.assertEqual(self.m._activity_cache["floor"], datetime.date(2025, 6, 1))
        self.assertTrue(all(c[2] is None for c in self.fake.calls))
        self.fake.calls.clear()
        for preset in ("week", "ytd"):
            self.client.get(f"/stats?range={preset}")
        self.assertEqual(self.fake.calls, [])

    def test_bounded_reads_are_capped(self):
        for year in range(2010, 2020):
            self.client.get(f"/stats?range=custom&from={year}-01-01&to={year}-01-31")
        self.assertLessEqual(len(self.m._activity_ranges), self.m._ACTIVITY_RANGE_SLOTS)
        # Most recent stays.
        self.assertIn(datetime.date(2019, 1, 1), [k[1] for k in self.m._activity_ranges])

    def test_bounded_read_served_when_offline(self):
        self.client.get("/stats?range=custom&from=2020-01-01&to=2020-12-31")
        self.m._github_client = None
        r, body = self.get("/stats?range=custom&from=2020-01-01&to=2020-12-31")
        self.assertNotIn("Not connected to GitHub", body)
        self.assertIn("Custom range", body)

    def test_cache_is_keyed_by_the_request_snapshot(self):
        scan_a = dict(self.m._scan_results)
        # A scan finishing mid-request swaps the global; the cache must still
        # be keyed (and read) by the snapshot the request started with.
        self.m._scan_results = {**scan_a, "scanned_at": "2099-01-01T00:00:00+00:00",
                                "repos": scan_a["repos"] + [repo("delta")]}
        rng, _ = activity.resolve_range({"range": "week"})
        self.m._activity_data(self.fake, scan_a, rng)
        self.assertEqual(self.m._activity_cache["key"], self.m._activity_scan_key(scan_a))
        self.assertNotIn("o/delta", self.m._activity_cache["data"])

    def test_failed_repo_waits_out_its_backoff(self):
        self.fake.fail = {"beta"}
        self.client.get("/stats?range=quarter")
        self.fake.calls.clear()
        for preset in ("week", "month", "quarter", "week"):  # all served from the cache
            self.client.get(f"/stats?range={preset}")
        self.assertEqual(self.fake.calls, [], "a failed repo is not re-read on every click")
        # Widening to YTD (when Jan 1 is outside the 90-day read) re-reads the
        # healthy repos but carries beta's failure instead of hitting it again.
        self.fake.calls.clear()
        self.client.get("/stats?range=custom&from=2020-01-01&to=" + activity._local_now().date().isoformat())
        self.assertEqual(sorted(c[0] for c in self.fake.calls), ["alpha", "gamma"])
        self.assertIn("o/beta", self.m._activity_cache["errors"])
        # REFRESH re-reads everything once; the GET it redirects to must not
        # immediately retry the repo that just failed again.
        self.fake.calls.clear()
        self.client.get("/stats?range=quarter&refresh=1", follow_redirects=True)
        self.assertEqual(sorted(c[0] for c in self.fake.calls), ["alpha", "beta", "gamma"])

    def test_refresh_redirect_only_carries_known_args(self):
        r = self.client.get("/stats?range=month&refresh=1&endpoint=x&_method=POST&_scheme=https&_external=1&group=")
        self.assertEqual(r.status_code, 302)
        loc = r.headers["Location"]
        self.assertTrue(loc.startswith("/stats?"), loc)
        for bad in ("endpoint", "_method", "_scheme", "_external", "https", "refresh"):
            self.assertNotIn(bad, loc)
        self.assertIn("range=month", loc)

    def test_all_repos_failing_never_says_nothing_landed(self):
        self.fake.fail = {"alpha", "beta", "gamma"}
        r, body = self.get("/stats")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Nothing landed in this range", body)
        self.assertIn("No activity found in the repos GitHub could read", body)
        self.assertIn("couldn&#39;t be read for 3 repos", body.replace("couldn't", "couldn&#39;t"))
        self.assertIn('<span class="stats-kpi-of"> of 0 read</span>', body)
        self.assertNotIn("Not updated in this range", body)
        md = self.client.get("/stats/export.md").data.decode()
        self.assertNotIn("Nothing landed", md)

    def test_failed_repo_shown_and_retried_after_backoff(self):
        self.fake.fail = {"beta"}
        r, body = self.get("/stats?range=quarter")
        self.assertIn("couldn't be read for 1 repo", body)
        self.assertNotIn("Beta work", body)
        self.assertNotIn(">beta</a>", body.split("Not updated in this range")[-1])
        self.fake.fail = set()
        self.m._activity_cache["retry_at"]["o/beta"] = 0  # backoff over
        self.fake.calls.clear()
        r, body = self.get("/stats?range=quarter")
        self.assertEqual([c[0] for c in self.fake.calls], ["beta"], "only the failed repo is re-read")
        self.assertIn("Beta work", body)
        self.assertNotIn("flash flash-error stats-notice", body)  # the warning banner is gone

    def test_refresh_refetches_and_redirects_without_refresh(self):
        self.client.get("/stats")
        self.fake.calls.clear()
        r = self.client.get("/stats?range=month&group=&refresh=1")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("refresh", r.headers["Location"])
        self.assertIn("range=month", r.headers["Location"])
        self.assertEqual(len(self.fake.calls), 3)

    def test_cache_expires(self):
        self.client.get("/stats")
        self.m._activity_cache["fetched_ts"] -= self.m.ACTIVITY_CACHE_TTL_SECONDS + 1
        self.fake.calls.clear()
        self.client.get("/stats")
        self.assertEqual(len(self.fake.calls), 3)

    def test_new_scan_invalidates_cache(self):
        self.client.get("/stats")
        self.m._scan_results = {**self.m._scan_results, "scanned_at": "2099-01-01T00:00:00+00:00"}
        self.fake.calls.clear()
        self.client.get("/stats")
        self.assertEqual(len(self.fake.calls), 3)

    def test_not_connected_without_cache(self):
        self.m._github_client = None
        r, body = self.get("/stats")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Not connected to GitHub", body)
        self.assertNotIn("COPY FOR CLAUDE", body)

    def test_not_connected_serves_stale_cache(self):
        self.client.get("/stats")
        self.m._activity_cache["fetched_ts"] -= self.m.ACTIVITY_CACHE_TTL_SECONDS + 1
        self.m._github_client = None
        r, body = self.get("/stats")
        self.assertIn("Alpha PR", body)
        self.assertIn("offline", body)

    def test_auth_error_redirects_with_remedy(self):
        self.fake.auth_fail = {"alpha"}
        r = self.client.get("/stats")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.m._activity_cache)

    def test_no_scan(self):
        self.m._scan_results = None
        r, body = self.get("/stats")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No scan data yet", body)

    def test_bad_custom_range_shows_notice(self):
        r, body = self.get("/stats?range=custom&from=garbage")
        self.assertEqual(r.status_code, 200)
        self.assertIn("isn&#39;t a date", body)
        events = models.tail_activity_log(10)
        self.assertIn("range_adjusted", [e["event"] for e in events])

    def test_group_scope_and_links_keep_range(self):
        models.set_group("Church", ["beta"])
        r, body = self.get("/stats?range=quarter&group=Church")
        self.assertIn("Beta work", body)
        self.assertNotIn("Alpha PR", body)
        self.assertIn('href="/stats?range=quarter&amp;group=Church"', body)  # preset chip keeps group
        self.assertIn('href="/stats?group=&amp;range=quarter"', body)  # "All" tab keeps range

    def test_export_markdown(self):
        r = self.client.get("/stats/export.md?range=quarter")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/markdown", r.headers["Content-Type"])
        self.assertRegex(r.headers["Content-Disposition"], r'what-changed-\d{4}-\d{2}-\d{2}-to-\d{4}-\d{2}-\d{2}\.md')
        md = r.data.decode()
        self.assertIn("# What changed: Last 90 days", md)
        self.assertIn("## alpha", md)
        self.assertIn("Beta work", md)
        self.assertIn("export", [e["event"] for e in models.tail_activity_log(10)])

    def test_export_not_connected_redirects_back(self):
        self.m._github_client = None
        r = self.client.get("/stats/export.md?range=custom&from=2026-01-01&to=2026-02-01")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/stats?", r.headers["Location"])
        self.assertIn("from=2026-01-01", r.headers["Location"])

    def test_view_and_fetch_are_logged(self):
        self.client.get("/stats")
        events = [e["event"] for e in models.tail_activity_log(20)]
        self.assertIn("fetch", events)
        self.assertIn("view", events)

    def test_scan_route_clears_cache(self):
        self.client.get("/stats")
        self.assertIsNotNone(self.m._activity_cache)
        with patch.object(self.fake, "get_repos", create=True, return_value=[]):
            self.client.post("/scan")
        self.assertIsNone(self.m._activity_cache)


if __name__ == "__main__":
    unittest.main()
