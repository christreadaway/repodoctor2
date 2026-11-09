"""Tests for Do next + Weekly review (nextup.py + /next, /review routes)."""

import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
import nextup

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 9, 18, 0, tzinfo=UTC)


def iso(days_ago, hours=0):
    return (NOW - datetime.timedelta(days=days_ago, hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo(name, owner="o", pushed=0):
    return {"owner": owner, "name": name, "full_name": f"{owner}/{name}", "default_branch": "main",
            "html_url": f"https://github.com/{owner}/{name}", "pushed_at": iso(pushed)}


def item(name, kind, branch="claude/x-abc123", days=1, pr=None, session_url="", ahead=2, owner="o"):
    return {"repo": name, "owner": owner, "branch": branch, "kind": kind,
            "topic": f"{name} {kind} topic", "base": "main", "pr": pr, "ahead_by": ahead,
            "text": f"{kind} sentence", "last_activity_at": iso(days), "age_days": days,
            "branch_url": f"https://github.com/{owner}/{name}/tree/{branch}",
            "session_url": session_url}


def pr(n, draft=False):
    return {"number": n, "html_url": f"https://github.com/o/r/pull/{n}", "draft": draft}


def tracker(*actions):
    return {"next_actions": [dict(a) for a in actions]}


def action(id_, pri, status="todo", title="Do the thing", prompt="", note=""):
    return {"id": id_, "priority": pri, "status": status, "title": title,
            "prompt": prompt, "status_note": note}


class TestBuildDoNext(unittest.TestCase):

    def build(self, items=(), trackers=None, summaries=None, repos=None):
        repos = repos or [repo(n) for n in ("a", "b", "c", "d", "e")]
        return nextup.build_do_next(list(items), trackers or {}, summaries or {}, repos, now=NOW)

    def test_tier_order_end_to_end(self):
        res = self.build(
            items=[item("a", "stale", days=30), item("b", "continue"), item("c", "finish", pr=pr(3, True)),
                   item("d", "merge", pr=pr(4)), item("e", "complete"), item("a", "unknown", branch="b2")],
            trackers={"o/a": tracker(action("N1", "P0"), action("N2", "P1"), action("N3", "P2"),
                                     action("N4", "P0", status="blocked", note="waiting on vendor"),
                                     action("N5", "P1", status="awaiting_deploy"))},
            summaries={"e": {"next_steps": ["Ship the volunteer form"]}})
        tiers = [x["tier"] for x in res["items"]]
        self.assertEqual(tiers, ["merge", "finish", "deploy", "p0", "open_pr", "p1",
                                 "decide", "decide", "planned", "cleanup", "blocked"])
        self.assertEqual([x["rank"] for x in res["items"]], list(range(1, 12)))
        self.assertEqual(res["hidden_low_priority"], 1)  # the P2
        self.assertEqual(res["counts"]["decide"], 2)
        self.assertEqual(res["cleanup_branches"], 1)
        blocked = res["items"][-1]
        self.assertIn("waiting on vendor", blocked["why"])

    def test_merge_oldest_first_finish_newest_first(self):
        res = self.build(items=[item("a", "merge", days=1, pr=pr(1)), item("b", "merge", days=9, pr=pr(2)),
                                item("c", "finish", days=8), item("d", "finish", days=0)])
        self.assertEqual([x["repo"] for x in res["items"]], ["b", "a", "d", "c"])

    def test_scope_excludes_other_repos_and_owners(self):
        res = self.build(items=[item("a", "merge", pr=pr(1)), item("zzz", "merge", pr=pr(2)),
                                item("a", "merge", owner="other", pr=pr(3))],
                         trackers={"o/zzz": tracker(action("N1", "P0")),
                                   "other/a": tracker(action("N1", "P0"))},
                         repos=[repo("a")])
        self.assertEqual([(x["repo"], x["owner"], x["tier"]) for x in res["items"]], [("a", "o", "merge")])

    def test_planned_only_for_repos_with_nothing_else_and_capped(self):
        names = [f"r{i:02d}" for i in range(15)]
        repos = [repo(n, pushed=i) for i, n in enumerate(names)]
        summaries = {n: {"next_steps": [f"step for {n}", "second"]} for n in names}
        res = self.build(items=[item("r00", "finish")], summaries=summaries, repos=repos)
        planned = [x for x in res["items"] if x["tier"] == "planned"]
        self.assertNotIn("r00", [x["repo"] for x in planned], "busy repo gets no planned row")
        self.assertEqual(len(planned), nextup.PLANNED_LIMIT)
        self.assertEqual(res["planned_more"], 14 - nextup.PLANNED_LIMIT)
        # Most recently pushed first (r01 pushed 1 day ago, r02 2 days ago...)
        self.assertEqual(planned[0]["repo"], "r01")
        self.assertIn("(1 more)", planned[0]["why"])
        self.assertIn("step for r01", planned[0]["prompt"])

    def test_planned_ignores_blank_and_non_string_steps(self):
        res = self.build(summaries={"a": {"next_steps": ["  ", None, 3]}, "b": "garbage", "zzz": {"next_steps": ["x"]}})
        self.assertEqual(res["items"], [])

    def test_cleanup_is_one_line_across_repos(self):
        res = self.build(items=[item("a", "complete"), item("a", "complete", branch="b2"), item("b", "complete")])
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(res["items"][0]["title"], "Delete 3 merged branches across 2 repos")
        self.assertEqual(res["items"][0]["links"][0]["url"], "/inflight?kind=complete")

    def test_session_and_pr_links(self):
        url = "https://claude.ai/code/session_01ABC"
        res = self.build(items=[item("a", "merge", pr=pr(7), session_url=url), item("b", "continue")])
        merge, open_pr = res["items"]
        labels = [l["label"] for l in merge["links"]]
        self.assertEqual(labels, ["PR #7", "OPEN SESSION"])
        self.assertTrue(merge["links"][1]["primary"])
        self.assertEqual(merge["session_url"], url)
        self.assertEqual([l["label"] for l in open_pr["links"]], ["BRANCH"])

    def test_cached_items_without_session_url_are_fine(self):
        it = item("a", "finish")
        del it["session_url"]
        res = self.build(items=[it])
        self.assertEqual(res["items"][0]["session_url"], "")

    def test_prompts(self):
        res = self.build(items=[item("a", "merge", pr=pr(7)), item("b", "finish", pr=pr(8, True)),
                                item("c", "continue", ahead=1), item("d", "stale", days=40)],
                         trackers={"o/e": tracker(action("N9", "P0", prompt="x" * 60),
                                                  action("N8", "P1", title="Fix login"))})
        p = {x["repo"] + x["tier"]: x["prompt"] for x in res["items"]}
        self.assertIn("PR #7", p["amerge"])
        self.assertIn("merge it on GitHub myself", p["amerge"])
        self.assertIn("mark PR #8 ready for review", p["bfinish"])
        self.assertIn("1 commit aren't", p["copen_pr"])
        self.assertIn("40 days", p["ddecide"])
        self.assertIn("Don't change anything", p["ddecide"])
        self.assertEqual(p["ep0"], "x" * 60, "tracker's own prompt wins")
        self.assertIn("N8 (P1): Fix login", p["ep1"])

    def test_tracker_shipped_and_dismissed_excluded(self):
        res = self.build(trackers={"o/a": tracker(action("N1", "P0", status="shipped"),
                                                  action("N2", "P0", status="dismissed"))})
        self.assertEqual(res["items"], [])

    def test_empty(self):
        res = self.build()
        self.assertEqual((res["total"], res["tiers"]), (0, []))
        md = nextup.compose_do_next_markdown(res)
        self.assertIn("Nothing waiting", md)

    def test_markdown(self):
        url = "https://claude.ai/code/session_01ABC"
        res = self.build(items=[item("a", "merge", pr=pr(7), session_url=url)],
                         trackers={"o/b": tracker(action("N1", "P3"))})
        md = nextup.compose_do_next_markdown(res, "tester", "2026-11-09T15:00:00+00:00", "Church")
        self.assertIn("# Do next", md)
        self.assertIn("the **Church** group", md)
        self.assertIn("## MERGE", md)
        self.assertIn("1. **a**: a merge topic", md)
        self.assertIn(f"OPEN SESSION: {url}", md)
        self.assertIn("Prompt: In a, PR #7", md)
        self.assertIn("1 lower-priority (P2/P3)", md)


class TestReview(unittest.TestCase):

    def report(self, rows):
        return {"updated": rows, "failed": [],
                "totals": {"repos_updated": len(rows), "repos_total": 5, "commits": 9,
                           "prs_merged": 2, "active_days": 3, "days": 7}}

    def row(self, name, last_days, commits=3, prs=1):
        return {"name": name, "owner": "o", "last_at": iso(last_days), "commit_count": commits,
                "pr_count": prs, "merge_count": 0,
                "prs": [{"number": i, "title": f"PR {i}"} for i in range(prs)],
                "commits": [{"summary": f"commit {i}"} for i in range(commits)]}

    def test_gone_quiet_threshold(self):
        q = nextup.gone_quiet(self.report([self.row("fresh", 3), self.row("edge", 21),
                                           self.row("old", 60), self.row("almost", 20)]), now=NOW)
        self.assertEqual([x["repo"] for x in q], ["edge", "old"])
        self.assertEqual(nextup.gone_quiet(None), [])

    def test_build_review_sections(self):
        dn = nextup.build_do_next(
            [item("a", "merge", pr=pr(1)), item("b", "stale", days=30), item("c", "continue")],
            {"o/d": tracker(action("N1", "P0"))}, {}, [repo(n) for n in "abcd"], now=NOW)
        week = self.report([self.row("a", 1, commits=5, prs=1)])
        quarter = self.report([self.row("a", 1), self.row("q", 40)])
        stale = [item("b", "stale", days=30), item("zzz", "stale", days=50)]
        rv = nextup.build_review(week, quarter, dn, stale, [repo(n) for n in "abcd"], now=NOW)
        self.assertEqual([x["tier"] for x in rv["waiting"]], ["merge"])
        self.assertEqual([x["tier"] for x in rv["next"]], ["p0", "open_pr"])
        self.assertEqual([q["repo"] for q in rv["quiet"]], ["q"])
        self.assertEqual([i["repo"] for i in rv["stale"]], ["b"], "out-of-scope stale branch excluded")
        s = rv["shipped"][0]
        self.assertEqual(s["titles"], ["#0 PR 0", "commit 0", "commit 1"])
        self.assertEqual(s["more"], 3)
        md = nextup.compose_review_markdown(rv, "tester", "", "Nov 3 – Nov 9, 2026")
        for needle in ("# Weekly review: Nov 3 – Nov 9, 2026", "## Shipped this week",
                       "**1 of 5 repos updated**", "## Waiting on you", "[MERGE] **a**",
                       "## Gone quiet", "**q**: no activity for 40 days", "`claude/x-abc123`: stale branch",
                       "## Next up", "[P0] **d**"):
            self.assertIn(needle, md)

    def test_review_without_github(self):
        dn = nextup.build_do_next([], {}, {}, [repo("a")], now=NOW)
        rv = nextup.build_review(None, None, dn, [], [repo("a")], now=NOW)
        md = nextup.compose_review_markdown(rv)
        self.assertIn("Not read from GitHub", md)
        self.assertIn("Nothing to merge, finish, or confirm.", md)


class TestRoutes(unittest.TestCase):

    def setUp(self):
        import app as app_mod
        self.m = app_mod
        app_mod.app.config["TESTING"] = True
        self.client = app_mod.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["github_user"] = "tester"
        self.tmp = tempfile.mkdtemp()
        names = ("GROUPS_PATH", "_LEGACY_GROUPS_PATH", "PREFS_PATH", "INFLIGHT_PATH", "TRACKERS_DIR",
                 "SUMMARIES_PATH", "NEXTUP_LOG_PATH", "ACTIVITY_LOG_PATH", "INFLIGHT_LOG_PATH")
        self._paths = {p: getattr(models, p) for p in names}
        for p in names:
            setattr(models, p, os.path.join(self.tmp, p.lower().strip("_")))
        self._orig = (app_mod._scan_results, app_mod._github_client, app_mod._activity_cache,
                      app_mod._activity_ranges)
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = (now - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (now - datetime.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")

        class Fake:
            calls = 0

            def get_commits_range(self, owner, name, since_iso, ref=None, until_iso=None, max_pages=50):
                Fake.calls += 1
                when = ts if name == "alpha" else old
                return [{"sha": f"{name}123456", "parents": [{}], "html_url": "",
                         "commit": {"message": f"{name} <b>work</b>", "author": {"name": "c", "date": when},
                                    "committer": {"date": when}}}], False

            def get_merged_pulls_since(self, owner, name, since_iso, max_pages=10):
                return [], False, ""

        self.Fake = Fake
        app_mod._github_client = Fake()
        app_mod._activity_cache = None
        app_mod._activity_ranges = {}
        app_mod._scan_results = {"repos": [repo("alpha"), repo("beta"), repo("gamma")],
                                 "scanned_at": now.isoformat()}
        models.save_inflight({"gathered_at": now.isoformat(), "items": [
            item("alpha", "merge", pr=pr(5), session_url="https://claude.ai/code/session_01XYZ"),
            item("beta", "stale", days=30),
            item("gamma", "complete")]})
        models.save_tracker("o", "gamma", tracker(action("N1", "P0", title="Ship <script>x</script>")))

    def tearDown(self):
        (self.m._scan_results, self.m._github_client, self.m._activity_cache,
         self.m._activity_ranges) = self._orig
        for p, v in self._paths.items():
            setattr(models, p, v)

    def get(self, url):
        r = self.client.get(url)
        return r, r.data.decode()

    def test_do_next_page(self):
        r, body = self.get("/next")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Do next", body)
        self.assertIn("alpha merge topic", body)
        self.assertIn('href="https://claude.ai/code/session_01XYZ"', body)
        self.assertIn("OPEN SESSION", body)
        self.assertIn("Delete 1 merged branch across 1 repo", body)
        self.assertIn("Ship &lt;script&gt;x&lt;/script&gt;", body)
        self.assertNotIn("<script>x</script>", body)
        self.assertIn("COPY PROMPT", body)
        self.assertIn("view_do_next", [e["event"] for e in models.tail_nextup_log(5)])

    def test_nav_lands_on_do_next(self):
        r, body = self.get("/next")
        self.assertIn('<a href="/next" class="nav-link active">Next Up</a>', body)
        self.assertIn('class="subnav-link active">Do next', body)
        r, body = self.get("/yesterday")
        self.assertIn('href="/next" class="nav-link active"', body)

    def test_do_next_without_inflight_cache(self):
        models.save_inflight({})
        r, body = self.get("/next")
        self.assertEqual(r.status_code, 200)
        self.assertIn("haven't been read from GitHub yet", body)
        self.assertIn("READ FROM GITHUB", body)
        self.assertIn("Ship &lt;script&gt;", body, "tracker items still show")

    def test_do_next_group_scope(self):
        models.set_group("Church", ["beta"])
        r, body = self.get("/next?group=Church")
        self.assertNotIn("alpha merge topic", body)
        self.assertIn("beta stale topic", body)

    def test_do_next_no_scan(self):
        self.m._scan_results = None
        r, body = self.get("/next")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No scan data yet", body)

    def test_do_next_export(self):
        r = self.client.get("/next/export.md")
        self.assertEqual(r.status_code, 200)
        self.assertIn('filename="do-next.md"', r.headers["Content-Disposition"])
        self.assertIn("## MERGE", r.data.decode())

    def test_review_page(self):
        r, body = self.get("/review")
        self.assertEqual(r.status_code, 200)
        for needle in ("Shipped this week", "Waiting on you", "Gone quiet", "Next up",
                       "alpha &lt;b&gt;work&lt;/b&gt;", "alpha merge topic", "stale branch"):
            self.assertIn(needle, body)
        # beta and gamma last committed 40 days ago: active in 90, quiet for 21+
        self.assertIn("no activity for 40 days", body)
        self.assertIn("view_review", [e["event"] for e in models.tail_nextup_log(5)])

    def test_review_reuses_the_stats_cache(self):
        self.client.get("/stats")
        before = self.Fake.calls
        self.client.get("/review")
        self.assertEqual(self.Fake.calls, before, "Weekly review reads the same 90-day cache")

    def test_review_offline(self):
        self.m._github_client = None
        r, body = self.get("/review")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Not read from GitHub", body)
        self.assertIn("alpha merge topic", body, "In Flight cache still drives Waiting on you")

    def test_review_export(self):
        r = self.client.get("/review/export.md")
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.headers["Content-Disposition"], r'weekly-review-\d{4}-\d{2}-\d{2}\.md')
        self.assertIn("## Gone quiet", r.data.decode())

    def test_inflight_refresh_returns_to_the_page_it_came_from(self):
        self.m._github_client = None  # not connected: the early-return path
        r = self.client.post("/inflight/refresh", data={"next": "/next?group="})
        self.assertEqual(r.headers["Location"], "/next?group=")
        r = self.client.post("/inflight/refresh", data={"next": "//evil.example/x"})
        self.assertEqual(r.headers["Location"], "/inflight", "off-site next is refused")


if __name__ == "__main__":
    unittest.main()
