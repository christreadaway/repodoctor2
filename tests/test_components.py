"""
Tests for the Components Inventory — path-based capability detection, the
capability-centric inventory (best source, tie-breaks, gaps), the Markdown
export, and failure isolation in scan_repo_components. Route tests live
elsewhere.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import components


def _repo(name="demo", pushed_at="2026-07-28T20:00:00Z", **overrides) -> dict:
    base = {
        "owner": "chris", "name": name, "full_name": f"chris/{name}",
        "default_branch": "main", "pushed_at": pushed_at,
    }
    base.update(overrides)
    return base


_TREE = [
    "README.md",
    "CLAUDE.md",
    "PRODUCT_SPEC.md",
    ".claude/commands/start.md",
    "app.py",
    "auth.py",
    "security.py",
    "models.py",
    "firebase.json",
    "firestore.rules",
    "netlify.toml",
    ".github/workflows/deploy.yml",
    ".github/workflows/nightly-sync.yml",
    "tests/test_app.py",
    "jest.config.js",
    "src/email/sendgrid_client.py",
    "src/sms/twilio.js",
    "src/stripe_checkout.js",
    "static/js/chart-dashboard.js",
    "tailwind.config.js",
    "data/logs/app.log",
    "logging_config.py",
    "start.command",
    "Makefile",
    "export_pdf.py",
    "ai_analyzer.py",
    "github_client.py",
    "src/webhooks/slack.py",
    "landing/index.html",
    "sitemap.xml",
]


class TestCapabilityCatalog(unittest.TestCase):

    def test_keys_unique_and_groups_known(self):
        keys = [c["key"] for c in components.CAPABILITIES]
        self.assertEqual(len(keys), len(set(keys)))
        for cap in components.CAPABILITIES:
            self.assertIn(cap["group"], components.GROUPS, cap["key"])
            for field in ("key", "label", "group", "description", "patterns"):
                self.assertIn(field, cap)
            self.assertTrue(cap["patterns"], cap["key"])

    def test_required_capabilities_present(self):
        keys = {c["key"] for c in components.CAPABILITIES}
        for k in ("auth", "firebase_firestore", "database", "email", "sms", "payments",
                  "api_clients", "ai_integration", "logging", "testing", "security",
                  "deployment", "claude_docs", "data_viz", "marketing_gtm", "design_system",
                  "comms_notifications", "scheduling_automation", "pdf_docs_export",
                  "scripts_launchers"):
            self.assertIn(k, keys)

    def test_every_group_has_a_capability(self):
        used = {c["group"] for c in components.CAPABILITIES}
        self.assertEqual(used, set(components.GROUPS))


class TestDetection(unittest.TestCase):

    def setUp(self):
        self.det = components.detect_capabilities(list(_TREE))

    def _evidence(self, key):
        return self.det[key]["evidence"]

    def test_returns_every_capability(self):
        self.assertEqual(set(self.det), {c["key"] for c in components.CAPABILITIES})
        for v in self.det.values():
            self.assertIn("hits", v)
            self.assertIn("evidence", v)

    def test_positive_hits(self):
        self.assertIn("auth.py", self._evidence("auth"))
        self.assertIn("security.py", self._evidence("security"))
        self.assertIn("firebase.json", self._evidence("firebase_firestore"))
        self.assertIn("firestore.rules", self._evidence("firebase_firestore"))
        self.assertIn("models.py", self._evidence("database"))
        self.assertIn("src/email/sendgrid_client.py", self._evidence("email"))
        self.assertIn("src/sms/twilio.js", self._evidence("sms"))
        self.assertIn("src/stripe_checkout.js", self._evidence("payments"))
        self.assertIn("github_client.py", self._evidence("api_clients"))
        self.assertIn("ai_analyzer.py", self._evidence("ai_integration"))
        self.assertIn("data/logs/app.log", self._evidence("logging"))
        self.assertIn("logging_config.py", self._evidence("logging"))
        self.assertIn("tests/test_app.py", self._evidence("testing"))
        self.assertIn("jest.config.js", self._evidence("testing"))
        self.assertIn("netlify.toml", self._evidence("deployment"))
        self.assertIn(".github/workflows/deploy.yml", self._evidence("deployment"))
        self.assertIn(".github/workflows/nightly-sync.yml", self._evidence("scheduling_automation"))
        self.assertIn("CLAUDE.md", self._evidence("claude_docs"))
        self.assertIn(".claude/commands/start.md", self._evidence("claude_docs"))
        self.assertIn("static/js/chart-dashboard.js", self._evidence("data_viz"))
        self.assertIn("tailwind.config.js", self._evidence("design_system"))
        self.assertIn("src/webhooks/slack.py", self._evidence("comms_notifications"))
        self.assertIn("export_pdf.py", self._evidence("pdf_docs_export"))
        self.assertIn("start.command", self._evidence("scripts_launchers"))
        self.assertIn("Makefile", self._evidence("scripts_launchers"))
        self.assertIn("landing/index.html", self._evidence("marketing_gtm"))
        self.assertIn("sitemap.xml", self._evidence("marketing_gtm"))

    def test_negative_no_false_positives(self):
        det = components.detect_capabilities(["README.md", "LICENSE", "app.py", "author.md",
                                              "docs/authors.txt", "notes.txt"])
        for key, v in det.items():
            self.assertEqual(v["hits"], 0, f"{key} should have no hits: {v['evidence']}")

    def test_hits_count_matches_evidence_when_small(self):
        det = components.detect_capabilities(["tests/test_a.py", "tests/test_b.py"])
        self.assertEqual(det["testing"]["hits"], 2)
        self.assertEqual(len(det["testing"]["evidence"]), 2)

    def test_evidence_capped_but_hits_keep_counting(self):
        paths = [f"tests/test_{i}.py" for i in range(20)]
        det = components.detect_capabilities(paths)
        self.assertEqual(det["testing"]["hits"], 20)
        self.assertEqual(len(det["testing"]["evidence"]), components.MAX_EVIDENCE)

    def test_vendor_dirs_skipped(self):
        det = components.detect_capabilities([
            "node_modules/stripe/lib/stripe.js",
            "venv/lib/site-packages/twilio/rest.py",
            "dist/auth.js",
            "build/firebase.json",
            "__pycache__/security.cpython-311.pyc",
            ".git/hooks/pre-commit.sh",
        ])
        for key, v in det.items():
            self.assertEqual(v["hits"], 0, f"{key} matched vendor path: {v['evidence']}")

    def test_case_insensitive_but_evidence_keeps_original(self):
        det = components.detect_capabilities(["Tests/Test_App.py", "DOCKERFILE"])
        self.assertEqual(det["testing"]["evidence"], ["Tests/Test_App.py"])
        self.assertEqual(det["deployment"]["evidence"], ["DOCKERFILE"])

    def test_handles_empty_and_junk_input(self):
        self.assertEqual(sum(v["hits"] for v in components.detect_capabilities([]).values()), 0)
        det = components.detect_capabilities([None, "", 42, "tests/test_x.py"])  # type: ignore[list-item]
        self.assertEqual(det["testing"]["hits"], 1)

    def test_is_skipped_only_looks_at_directories(self):
        self.assertTrue(components.is_skipped("node_modules/x/y.js"))
        self.assertFalse(components.is_skipped("build.py"))  # filename, not a dir
        self.assertFalse(components.is_skipped("src/dist.js"))


class TestInventory(unittest.TestCase):

    def _det(self, **hits):
        base = components.detect_capabilities([])
        for key, n in hits.items():
            base[key] = {"hits": n, "evidence": [f"{key}_{i}.py" for i in range(min(n, 3))]}
        return base

    def test_best_source_highest_hits(self):
        repos = [_repo("a"), _repo("b"), _repo("c")]
        dets = {
            "chris/a": self._det(logging=2),
            "chris/b": self._det(logging=7),
            "chris/c": self._det(logging=5),
        }
        inv = components.inventory(repos, dets)
        row = next(c for c in inv["capabilities"] if c["key"] == "logging")
        self.assertEqual(row["best_source"]["full_name"], "chris/b")
        self.assertEqual([r["full_name"] for r in row["repos"]], ["chris/b", "chris/c", "chris/a"])
        self.assertEqual([r["full_name"] for r in row["others"]], ["chris/c", "chris/a"])
        self.assertEqual(row["repo_count"], 3)

    def test_tie_breaks_on_most_recent_push(self):
        repos = [
            _repo("old", pushed_at="2025-01-01T00:00:00Z"),
            _repo("new", pushed_at="2026-06-01T00:00:00Z"),
            _repo("mid", pushed_at="2025-09-01T00:00:00Z"),
        ]
        dets = {fn: self._det(auth=3) for fn in ("chris/old", "chris/new", "chris/mid")}
        inv = components.inventory(repos, dets)
        row = next(c for c in inv["capabilities"] if c["key"] == "auth")
        self.assertEqual(row["best_source"]["full_name"], "chris/new")
        self.assertEqual([r["name"] for r in row["repos"]], ["new", "mid", "old"])

    def test_hits_beat_recency(self):
        repos = [_repo("old", pushed_at="2024-01-01T00:00:00Z"),
                 _repo("new", pushed_at="2026-06-01T00:00:00Z")]
        dets = {"chris/old": self._det(testing=9), "chris/new": self._det(testing=1)}
        inv = components.inventory(repos, dets)
        row = next(c for c in inv["capabilities"] if c["key"] == "testing")
        self.assertEqual(row["best_source"]["full_name"], "chris/old")

    def test_missing_pushed_at_sorts_last_on_tie(self):
        repos = [_repo("nodate", pushed_at=""), _repo("dated", pushed_at="2026-01-01T00:00:00Z")]
        dets = {"chris/nodate": self._det(email=2), "chris/dated": self._det(email=2)}
        inv = components.inventory(repos, dets)
        row = next(c for c in inv["capabilities"] if c["key"] == "email")
        self.assertEqual(row["best_source"]["name"], "dated")

    def test_gaps_and_coverage(self):
        repos = [_repo("a"), _repo("b")]
        dets = {"chris/a": self._det(logging=1, testing=2), "chris/b": self._det(testing=1)}
        inv = components.inventory(repos, dets)
        self.assertEqual(inv["coverage"]["testing"], 2)
        self.assertEqual(inv["coverage"]["logging"], 1)
        self.assertEqual(inv["coverage"]["payments"], 0)
        gap_keys = {g["key"] for g in inv["gaps"]}
        self.assertIn("payments", gap_keys)
        self.assertIn("sms", gap_keys)
        self.assertNotIn("testing", gap_keys)
        self.assertEqual(inv["repos_scanned"], 2)
        self.assertEqual(inv["capabilities_found"], 2)
        self.assertEqual(len(inv["gaps"]) + inv["capabilities_found"], len(components.CAPABILITIES))

    def test_repo_counts_sorted_desc(self):
        repos = [_repo("a"), _repo("b")]
        dets = {"chris/a": self._det(logging=1), "chris/b": self._det(logging=1, testing=1, auth=1)}
        inv = components.inventory(repos, dets)
        self.assertEqual(inv["repo_counts"][0]["full_name"], "chris/b")
        self.assertEqual(inv["repo_counts"][0]["capability_count"], 3)
        self.assertEqual(set(inv["repo_counts"][0]["capabilities"]), {"logging", "testing", "auth"})

    def test_groups_in_owner_order_and_cover_all_caps(self):
        inv = components.inventory([_repo("a")], {"chris/a": self._det()})
        self.assertEqual([g["name"] for g in inv["groups"]], components.GROUPS)
        total = sum(len(g["capabilities"]) for g in inv["groups"])
        self.assertEqual(total, len(components.CAPABILITIES))

    def test_repo_without_detection_is_ignored(self):
        repos = [_repo("scanned"), _repo("skipped")]
        inv = components.inventory(repos, {"chris/scanned": self._det(logging=1)})
        self.assertEqual(inv["repos_scanned"], 1)
        self.assertEqual([r["full_name"] for r in inv["repo_counts"]], ["chris/scanned"])

    def test_empty_inputs(self):
        inv = components.inventory([], {})
        self.assertEqual(inv["repos_scanned"], 0)
        self.assertEqual(len(inv["gaps"]), len(components.CAPABILITIES))
        self.assertEqual(inv["extraction_order"], [])

    def test_extraction_order_ranks_by_spread(self):
        repos = [_repo(n) for n in "abcd"]
        dets = {
            "chris/a": self._det(claude_docs=1, logging=1, payments=1),
            "chris/b": self._det(claude_docs=1, logging=1),
            "chris/c": self._det(claude_docs=1, logging=1),
            "chris/d": self._det(claude_docs=1),
        }
        inv = components.inventory(repos, dets)
        order = [r["key"] for r in inv["extraction_order"]]
        self.assertEqual(order, ["claude_docs", "logging", "payments"])
        top = inv["extraction_order"][0]
        self.assertEqual(top["repo_count"], 4)
        self.assertEqual(top["spread"], 1.0)
        self.assertEqual(top["score"], 4.0)
        self.assertEqual(inv["extraction_order"][1]["score"], 2.25)  # 3 * 0.75


class TestMarkdown(unittest.TestCase):

    def _inv(self):
        repos = [_repo("alpha", pushed_at="2026-05-01T00:00:00Z"),
                 _repo("beta", pushed_at="2026-01-01T00:00:00Z")]
        dets = {
            "chris/alpha": components.detect_capabilities(["CLAUDE.md", "tests/test_a.py", "logging_config.py"]),
            "chris/beta": components.detect_capabilities(["CLAUDE.md", "PRODUCT_SPEC.md"]),
        }
        return components.inventory(repos, dets)

    def test_markdown_structure(self):
        md = components.compose_markdown(self._inv())
        self.assertTrue(md.startswith("# Component Inventory"))
        self.assertIn("path-based only", md)
        for g in components.GROUPS:
            self.assertIn(f"## {g}", md)
        self.assertIn("### Claude Docs Management", md)
        self.assertIn("**Best source:** `chris/beta` (2 hit(s))", md)
        self.assertIn("`chris/alpha` (1)", md)
        self.assertIn("Evidence: `CLAUDE.md`, `PRODUCT_SPEC.md`", md)
        self.assertIn("## Gaps (no repo has this yet)", md)
        self.assertIn("- Payments (APIs & Integrations)", md)
        self.assertIn("## Suggested extraction order", md)
        self.assertIn("Heuristic:", md)
        self.assertIn("1. **Claude Docs Management**", md)
        self.assertIn("start from `chris/beta`", md)
        self.assertIn("Repos scanned: 2", md)

    def test_markdown_for_empty_inventory(self):
        md = components.compose_markdown(components.inventory([], {}))
        self.assertIn("Repos scanned: 0", md)
        self.assertIn("Nothing to extract yet", md)
        self.assertIn("Best source:** none found (gap)", md)


class TestScanRepoComponents(unittest.TestCase):

    def test_calls_tree_once_with_default_branch(self):
        client = MagicMock()
        client.get_all_file_paths.return_value = ["CLAUDE.md", "tests/test_x.py"]
        det = components.scan_repo_components(client, _repo("demo", default_branch="develop"))
        client.get_all_file_paths.assert_called_once_with("chris", "demo", ref="develop", strict=True)
        self.assertEqual(det["claude_docs"]["hits"], 1)
        self.assertEqual(det["testing"]["hits"], 1)

    def test_failure_returns_none_not_empty_detection(self):
        # "GitHub didn't answer" must never be recorded as "repo has nothing".
        client = MagicMock()
        client.get_all_file_paths.side_effect = RuntimeError("boom")
        with self.assertLogs("components", level="WARNING") as cm:
            det = components.scan_repo_components(client, _repo("broken"))
        self.assertIsNone(det)
        self.assertTrue(any("broken" in line for line in cm.output))

    def test_auth_error_propagates(self):
        import github_client
        client = MagicMock()
        client.get_all_file_paths.side_effect = github_client.GitHubAuthError("dead PAT")
        with self.assertRaises(github_client.GitHubAuthError):
            components.scan_repo_components(client, _repo("x"))

    def test_vendor_skip_is_case_insensitive(self):
        self.assertTrue(components.is_skipped("Vendor/stripe_checkout.py"))
        self.assertTrue(components.is_skipped("Build/x.js"))

    def test_one_failure_does_not_blank_the_run(self):
        client = MagicMock()

        def _paths(owner, name, ref=None, strict=False):
            if name == "bad":
                raise RuntimeError("rate limited")
            return ["CLAUDE.md"]

        client.get_all_file_paths.side_effect = _paths
        repos = [_repo("good"), _repo("bad"), _repo("fine")]
        dets = {r["full_name"]: components.scan_repo_components(client, r) for r in repos}
        self.assertIsNone(dets.pop("chris/bad"))
        inv = components.inventory(repos, dets)
        self.assertEqual(inv["repos_scanned"], 2)
        self.assertEqual(inv["coverage"]["claude_docs"], 2)

    def test_none_tree_treated_as_empty(self):
        client = MagicMock()
        client.get_all_file_paths.return_value = None
        det = components.scan_repo_components(client, _repo("empty"))
        self.assertEqual(sum(v["hits"] for v in det.values()), 0)


if __name__ == "__main__":
    unittest.main()
