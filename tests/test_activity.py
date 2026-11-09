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

    def test_fetch_floor_covers_every_preset(self):
        for now in (local(2026, 11, 9), local(2027, 1, 15), local(2026, 3, 31)):
            floor = activity.fetch_floor(now.date())
            for preset in activity.PRESET_KEYS:
                rng, _ = activity.resolve_range({"range": preset}, now=now)
                self.assertLessEqual(floor, rng.start_date, (now, preset))
        # In January the 90-day quarter reaches into last year.
        self.assertEqual(activity.fetch_floor(datetime.date(2027, 1, 15)).year, 2026)


# ----------------------------------------------------------------------
# Strict GitHub pagination
# ----------------------------------------------------------------------

def _resp(status, payload):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = ""
    r.headers = {}
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

    def test_rate_limit_and_not_found_hints(self):
        c = self.client([_resp(403, {})])
        with self.assertRaisesRegex(gh.GitHubFetchError, "rate limited"):
            c.get_commits_range("o", "r", "x")
        c = self.client([_resp(404, {})])
        with self.assertRaisesRegex(gh.GitHubFetchError, "not found"):
            c.get_commits_range("o", "r", "x")

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
        merged, truncated = c.get_merged_pulls_since("o", "r", since)
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
        merged, _ = c.get_merged_pulls_since("o", "r", "2026-10-01T00:00:00Z")
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
        self.calls.append((name, since_iso))
        if name in self.auth_fail:
            raise gh.GitHubAuthError("401")
        if name in self.fail:
            raise gh.GitHubFetchError(f"commits {owner}/{name} page 1: HTTP 502")
        return list(self.commits.get(name, [])), False

    def get_merged_pulls_since(self, owner, name, since_iso, max_pages=10):
        return list(self.prs.get(name, [])), False


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

    def data_for(self, commits=(), prs=(), truncated=False):
        c = FakeClient(commits={"x": list(commits)}, prs={"x": list(prs)})
        d = activity.fetch_repo(c, repo("x"), "S")
        d["commits_truncated"] = truncated
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

    def test_truncation_surfaces(self):
        d = self.data_for(commits=[raw_commit("1", "a", utc_iso(local(2026, 11, 8)))], truncated=True)
        rep = activity.build_report([repo("x")], {"o/x": d}, {}, self.rng)
        self.assertTrue(rep["rows"][0]["truncated"])
        self.assertEqual(rep["totals"]["truncated"], 1)

    def test_missing_data_is_not_a_crash(self):
        rep = activity.build_report([repo("x")], {}, {}, self.rng)
        self.assertEqual(rep["totals"]["repos_updated"], 0)
        self.assertEqual(len(rep["idle"]), 1)


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
        app_mod._scan_results = {"repos": [repo("alpha"), repo("beta"), repo("gamma")],
                                 "scanned_at": self.now.isoformat(), "total_branches": 3}

    def tearDown(self):
        (self.m._scan_results, self.m._github_client, self.m._activity_cache) = self._orig
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

    def test_one_fetch_serves_every_preset(self):
        for preset in ("week", "month", "quarter", "ytd"):
            self.assertEqual(self.client.get(f"/stats?range={preset}").status_code, 200)
        commit_calls = [c for c in self.fake.calls]
        self.assertEqual(len(commit_calls), 3, "one fetch per repo, reused by every preset")
        floor = activity.fetch_floor(activity._local_now().date())
        self.assertTrue(all(c[1] == activity._local_midnight(floor).astimezone(datetime.timezone.utc).isoformat()
                            for c in commit_calls))

    def test_earlier_custom_start_refetches_from_that_day(self):
        self.client.get("/stats")
        self.fake.calls.clear()
        r, body = self.get("/stats?range=custom&from=2020-01-01&to=2020-12-31")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.fake.calls), 3)
        self.assertTrue(all(c[1].startswith("2020-01-01") for c in self.fake.calls))
        # A later preset is now served from that bigger cache.
        self.fake.calls.clear()
        self.client.get("/stats?range=ytd")
        self.assertEqual(self.fake.calls, [])

    def test_failed_repo_shown_and_retried_next_view(self):
        self.fake.fail = {"beta"}
        r, body = self.get("/stats?range=quarter")
        self.assertIn("couldn't be read for 1 repo", body)
        self.assertNotIn("Beta work", body)
        self.assertNotIn(">beta</a>", body.split("Not updated in this range")[-1])
        self.fake.fail = set()
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
