"""Portfolio screen: tags (= groups), one-liners, AI tag suggestions, and the
infrastructure map."""

import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import components
import models
import portfolio

UTC = datetime.timezone.utc


def _repo(name, owner="chris", **kw):
    r = {"owner": owner, "name": name, "full_name": f"{owner}/{name}", "default_branch": "main",
         "pushed_at": "2026-10-20T00:00:00Z", "private": True, "description": ""}
    r.update(kw)
    return r


class TestPureFunctions(unittest.TestCase):

    def test_clean_tag(self):
        self.assertEqual(portfolio.clean_tag("  Marketing   /  GTM "), "Marketing / GTM")
        self.assertIsNone(portfolio.clean_tag(""))
        self.assertIsNone(portfolio.clean_tag("<script>"))
        self.assertIsNone(portfolio.clean_tag("x" * 41))

    def test_one_liner_prefers_blurb_then_summary_then_description(self):
        r = _repo("a", description="GitHub desc. More.")
        self.assertEqual(portfolio.one_liner(r, {}, {}), ("GitHub desc.", "description"))
        self.assertEqual(portfolio.one_liner(r, {}, {"a": {"what_it_does": "Does X. And Y."}}),
                         ("Does X.", "summary"))
        self.assertEqual(portfolio.one_liner(r, {"chris/a": {"summary": "Blurb one. Two. Three."}}, {}),
                         ("Blurb one.", "blurb"))
        self.assertEqual(portfolio.one_liner(_repo("b"), {}, {}), ("", ""))

    def test_build_view_groups_by_tag_and_lists_untagged(self):
        repos = [_repo("a", pushed_at="2026-10-25T00:00:00Z"), _repo("b"), _repo("c"),
                 _repo("site", owner="org")]
        groups = {"Church": ["a"], "Infrastructure": ["a", "b"], "Empty": [], "Gone": ["ghost"]}
        items = [{"owner": "chris", "repo": "a", "kind": "merge"},
                 {"owner": "chris", "repo": "a", "kind": "complete"}]
        v = portfolio.build_view(repos, groups, {}, {}, items, activity_cutoff="2026-10-24T00:00:00Z")
        secs = {s["tag"]: s for s in v["sections"]}
        self.assertEqual([r["name"] for r in secs["Infrastructure"]["repos"]], ["a", "b"])  # newest first
        self.assertEqual(secs["Church"]["open_branches"], 1)  # merged branch doesn't count
        self.assertEqual(secs["Infrastructure"]["active"], 1)
        self.assertEqual(secs["Empty"]["count"], 0)
        self.assertEqual(secs["Gone"]["missing"], ["ghost"])
        self.assertEqual(sorted(r["name"] for r in v["untagged"]), ["c", "site"])
        self.assertEqual(v["rows"]["chris/a"]["tags"], ["Church", "Infrastructure"])
        self.assertEqual(v["total"], 4)
        self.assertEqual(v["tagged"], 2)
        self.assertEqual(len(v["missing_one_liners"]), 4)

    def test_normalize_suggestions(self):
        rows = [{"name": "a", "full_name": "chris/a", "tags": []},
                {"name": "b", "full_name": "chris/b", "tags": ["Church"]}]
        raw = {"assignments": [
            {"repo": "a", "tags": ["church", "Retro Games", "<bad>", "Third"]},
            {"repo": "b", "tags": ["Church"]},          # already has it: nothing to add
            {"repo": "ghost", "tags": ["Church"]},      # unknown repo dropped
            {"repo": "a", "tags": ["Other"]},           # duplicate repo dropped
            "junk",
        ]}
        out = portfolio.normalize_suggestions(raw, rows, ["Church", "School"])
        self.assertEqual(out, [{"repo": "a", "full_name": "chris/a",
                                "tags": ["Church", "Retro Games"], "new_tags": ["Retro Games"]}])

    def test_infrastructure_view_uses_components_scan_and_homes(self):
        repos = [_repo("mailer"), _repo("other")]
        dets = {"chris/mailer": components.detect_capabilities(["email/send.py", "sms/twilio_client.py"]),
                "chris/other": components.detect_capabilities(["README.md"])}
        infra = portfolio.infrastructure_view({"detections": dets}, repos, {"messaging": "chris/mailer"}, components)
        by_key = {a["key"]: a for a in infra}
        self.assertEqual(by_key["messaging"]["home"], "chris/mailer")
        self.assertEqual(by_key["messaging"]["sources"][0]["name"], "mailer")
        self.assertEqual(by_key["video"]["repo_count"], 0)
        self.assertTrue(by_key["video"]["scanned"])
        # Every area points at real capability keys.
        keys = {c["key"] for c in components.CAPABILITIES}
        for a in portfolio.FUNCTIONAL_AREAS:
            self.assertTrue(set(a["capabilities"]) <= keys, a["key"])
        # No scan yet: cards still render, nothing claimed.
        self.assertFalse(portfolio.infrastructure_view(None, repos, {}, components)[0]["scanned"])

    def test_new_capabilities_detect(self):
        d = components.detect_capabilities(["src/remotion.config.ts", "transcribe.py", "lessons/unit1.md"])
        self.assertEqual(d["video_production"]["hits"], 1)
        self.assertEqual(d["audio_transcription"]["hits"], 1)
        self.assertEqual(d["courseware"]["hits"], 1)

    def test_markdown(self):
        v = portfolio.build_view([_repo("a", description="Does a.")], {"Church": ["a"]}, {}, {}, [])
        md = portfolio.compose_markdown(v, [], "chris", "2026-10-26")
        self.assertIn("# Project portfolio", md)
        self.assertIn("## Church (1)", md)
        self.assertIn("- **a** — Does a.", md)


class TestRoutes(unittest.TestCase):

    PATHS = ("GROUPS_PATH", "PREFS_PATH", "_LEGACY_PREFS_PATH", "PORTFOLIO_SUGGESTIONS_PATH",
             "INFRA_HOMES_PATH", "PORTFOLIO_LOG_PATH", "INFLIGHT_PATH", "COMPONENTS_PATH",
             "SELECTOR_BLURBS_PATH", "SUMMARIES_PATH")

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
        app_module._scan_results = {
            "repos": [_repo("parishsite", description="Parish website."), _repo("budget"),
                      _repo("mailer")],
            "scanned_at": datetime.datetime.now(UTC).isoformat()}
        app_module._github_client = object()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["github_user"] = "chris"

    def tearDown(self):
        for p, v in self._saved.items():
            setattr(models, p, v)
        (self.m._scan_results, self.m._github_client, self.m._credentials) = self._orig

    def test_requires_auth(self):
        c = self.m.app.test_client()
        self.assertEqual(c.get("/portfolio").status_code, 302)
        self.assertEqual(c.post("/api/portfolio/tag", json={}).status_code, 401)

    def test_view_seeds_default_tags_once(self):
        resp = self.client.get("/portfolio")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Portfolio", resp.data)
        groups = models.get_groups()
        for t in portfolio.DEFAULT_TAGS:
            self.assertIn(t, groups)
        self.assertIn(b"Untagged", resp.data)
        self.assertIn(b"Parish website.", resp.data)
        # Deleting a default tag sticks: no re-seed on the next visit.
        self.client.post("/portfolio/tags", data={"action": "delete", "name": "Infrastructure"})
        self.client.get("/portfolio")
        self.assertNotIn("Infrastructure", models.get_groups())
        self.assertEqual(models.tail_portfolio_log()[0]["event"], "seed_tags")

    def test_tag_api_add_remove_new_and_category_single_select(self):
        self.client.get("/portfolio")
        r = self.client.post("/api/portfolio/tag", json={"repo": "budget", "tag": "Investing / Personal Finance"})
        self.assertEqual(r.get_json()["tags"], ["Investing / Personal Finance"])
        r = self.client.post("/api/portfolio/tag", json={"repo": "budget", "tag": "Brand New"})
        self.assertIn("Brand New", models.get_groups())
        self.client.post("/api/portfolio/tag", json={"repo": "parishsite", "tag": "Church"})
        self.client.post("/api/portfolio/tag", json={"repo": "parishsite", "tag": "School"})
        self.assertNotIn("parishsite", models.get_groups()["Church"])  # categories: one at a time
        self.assertIn("parishsite", models.get_groups()["School"])
        r = self.client.post("/api/portfolio/tag", json={"repo": "budget", "tag": "Brand New", "add": False})
        self.assertNotIn("Brand New", r.get_json()["tags"])
        self.assertEqual(self.client.post("/api/portfolio/tag", json={"repo": "ghost", "tag": "X"}).status_code, 404)
        self.assertEqual(self.client.post("/api/portfolio/tag", json={"repo": "budget", "tag": "<x>"}).status_code, 400)
        self.assertEqual(models.tail_portfolio_log()[-1]["event"], "tag_remove")

    def test_tag_manager_create_rename_delete(self):
        self.client.get("/portfolio")
        self.client.post("/portfolio/tags", data={"action": "create", "name": "Video"})
        self.assertIn("Video", models.get_groups())
        resp = self.client.post("/portfolio/tags", data={"action": "create", "name": "Video"}, follow_redirects=True)
        self.assertIn(b"already exists", resp.data)
        models.tag_repo("mailer", "Video")
        self.client.post("/portfolio/tags", data={"action": "rename", "name": "Video", "new_name": "Video Production"})
        self.assertEqual(models.get_groups()["Video Production"], ["mailer"])
        resp = self.client.post("/portfolio/tags", data={"action": "rename", "name": "Church", "new_name": "Parish"},
                                follow_redirects=True)
        self.assertIn(b"is a category", resp.data)
        self.client.post("/portfolio/tags", data={"action": "delete", "name": "Video Production"})
        self.assertNotIn("Video Production", models.get_groups())

    def test_suggest_then_apply_only_ticked(self):
        self.client.get("/portfolio")
        self.m._credentials = {"anthropic_key": "k"}
        fake = {"assignments": [{"repo": "budget", "tags": ["Investing / Personal Finance"]},
                                {"repo": "parishsite", "tags": ["Church", "Marketing / GTM"]},
                                {"repo": "mailer", "tags": ["Infrastructure"]}]}
        with patch.object(self.m.ai, "request_json", return_value=(fake, {"input_tokens": 10, "output_tokens": 5})), \
             patch.object(self.m, "_get_credentials", return_value={"anthropic_key": "k"}), \
             patch.object(self.m.anthropic, "Anthropic"):
            r = self.client.post("/api/portfolio/suggest", json={"scope": "untagged"})
        self.assertEqual(r.get_json(), {"ok": True, "count": 3})
        # Nothing applied yet.
        self.assertEqual(models.get_groups()["Church"], [])
        page = self.client.get("/portfolio").data
        self.assertIn(b"Suggested tags", page)
        self.client.post("/portfolio/suggestions", data={
            "action": "apply_close",
            "pick": ["budget::Investing / Personal Finance", "parishsite::Church", "mailer::Forged"]})
        g = models.get_groups()
        self.assertEqual(g["Investing / Personal Finance"], ["budget"])
        self.assertEqual(g["Church"], ["parishsite"])
        self.assertEqual(g["Marketing / GTM"], [])       # unticked
        self.assertEqual(g["Infrastructure"], [])        # unticked
        self.assertNotIn("Forged", g)                    # not a suggestion
        self.assertEqual(models.get_portfolio_suggestions(), {})

    def test_suggest_failure_saves_nothing(self):
        self.client.get("/portfolio")
        with patch.object(self.m.ai, "request_json", side_effect=ValueError("refused")), \
             patch.object(self.m, "_get_credentials", return_value={"anthropic_key": "k"}), \
             patch.object(self.m.anthropic, "Anthropic"):
            r = self.client.post("/api/portfolio/suggest", json={})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(models.get_portfolio_suggestions(), {})
        self.assertEqual(models.tail_portfolio_log()[-1]["event"], "suggest_error")

    def test_infra_tab_home_and_export(self):
        resp = self.client.get("/portfolio?tab=infra")
        self.assertIn(b"Video production", resp.data)
        self.assertIn(b"RUN A COMPONENTS SCAN", resp.data)
        self.client.post("/portfolio/infra-home", data={"area": "messaging", "repo": "chris/mailer"})
        self.assertEqual(models.get_infra_homes(), {"messaging": "chris/mailer"})
        resp = self.client.post("/portfolio/infra-home", data={"area": "messaging", "repo": "chris/ghost"},
                                follow_redirects=True)
        self.assertIn(b"isn&#39;t in the latest scan", resp.data)
        exp = self.client.get("/portfolio/export.md")
        self.assertEqual(exp.status_code, 200)
        self.assertIn(b"home: `chris/mailer`", exp.data)

    def test_nav_has_portfolio(self):
        self.assertIn(b'href="/portfolio"', self.client.get("/portfolio").data)


if __name__ == "__main__":
    unittest.main()
