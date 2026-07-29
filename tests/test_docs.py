"""
Tests for the Docs Explorer — file listing/grouping and the Flask route.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docs_explorer


def _repo(**overrides) -> dict:
    base = {
        "owner": "chris", "name": "demo", "default_branch": "main",
        "html_url": "https://github.com/chris/demo", "pushed_at": "2026-07-28T20:00:00Z",
    }
    base.update(overrides)
    return base


_TREE = [
    "README.md",
    "PRODUCT_SPEC.md",
    "app.py",
    "docs/design.md",
    "docs/API.markdown",
    "node_modules/pkg/README.md",
    "src/notes.md",
    "LICENSE",
]


class TestListing(unittest.TestCase):

    def _client(self, tree=None):
        client = MagicMock()
        client.get_all_file_paths.return_value = tree if tree is not None else list(_TREE)
        return client

    def test_filters_to_markdown_and_skips_vendor(self):
        files = docs_explorer.list_md_files(self._client(), "chris", "demo", "main")
        paths = [f["path"] for f in files]
        self.assertIn("README.md", paths)
        self.assertIn("docs/API.markdown", paths)
        self.assertNotIn("app.py", paths)
        self.assertNotIn("LICENSE", paths)
        self.assertNotIn("node_modules/pkg/README.md", paths)

    def test_root_first_readme_first(self):
        files = docs_explorer.list_md_files(self._client(), "chris", "demo", "main")
        # README preferred at the very top; both root docs precede subfolders.
        self.assertEqual(files[0]["path"], "README.md")
        self.assertEqual(files[1]["path"], "PRODUCT_SPEC.md")
        self.assertTrue(files[2]["dir"] != "")

    def test_tree_failure_returns_empty(self):
        client = MagicMock()
        client.get_all_file_paths.side_effect = RuntimeError("boom")
        self.assertEqual(docs_explorer.list_md_files(client, "c", "d", "main"), [])


class TestPickDefault(unittest.TestCase):

    def test_prefers_readme(self):
        files = [{"path": "docs/x.md", "name": "x.md", "dir": "docs"},
                 {"path": "README.md", "name": "README.md", "dir": ""}]
        self.assertEqual(docs_explorer.pick_default(files), "README.md")

    def test_falls_to_product_spec(self):
        files = [{"path": "PRODUCT_SPEC.md", "name": "PRODUCT_SPEC.md", "dir": ""},
                 {"path": "docs/x.md", "name": "x.md", "dir": "docs"}]
        self.assertEqual(docs_explorer.pick_default(files), "PRODUCT_SPEC.md")

    def test_first_when_no_preferred(self):
        files = [{"path": "docs/a.md", "name": "a.md", "dir": "docs"}]
        self.assertEqual(docs_explorer.pick_default(files), "docs/a.md")

    def test_none_when_empty(self):
        self.assertIsNone(docs_explorer.pick_default([]))


class TestGrouping(unittest.TestCase):

    def test_root_group_first(self):
        client = MagicMock()
        client.get_all_file_paths.return_value = list(_TREE)
        files = docs_explorer.list_md_files(client, "chris", "demo", "main")
        groups = docs_explorer.group_by_dir(files)
        self.assertEqual(groups[0]["label"], "/")
        labels = [g["label"] for g in groups]
        self.assertEqual(labels, ["/", "docs/", "src/"])

    def test_is_allowed_path(self):
        files = [{"path": "README.md"}, {"path": "docs/x.md"}]
        self.assertTrue(docs_explorer.is_allowed_path("README.md", files))
        self.assertFalse(docs_explorer.is_allowed_path("secret.md", files))


class TestDocsRoute(unittest.TestCase):

    def setUp(self):
        import app as app_module
        self.app_module = app_module
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self._orig = (app_module._scan_results, app_module._github_client)

    def tearDown(self):
        self.app_module._scan_results, self.app_module._github_client = self._orig

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "chris"

    def _gh(self):
        client = MagicMock()
        client.get_all_file_paths.return_value = list(_TREE)
        client.get_file_content.return_value = "# Hello\n\nThis is **bold** doc."
        return client

    def test_unauthenticated_redirects(self):
        self.assertEqual(self.client.get("/docs").status_code, 302)

    def test_no_scan_onboarding(self):
        self._login()
        self.app_module._scan_results = None
        resp = self.client.get("/docs")
        self.assertIn(b"No scan data yet", resp.data)

    def test_not_connected(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = None
        resp = self.client.get("/docs")
        self.assertIn(b"Not connected to GitHub", resp.data)

    def test_renders_default_doc(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = self._gh()
        resp = self.client.get("/docs")
        self.assertEqual(resp.status_code, 200)
        # Default file is README; content rendered as HTML.
        self.assertIn(b"<h1>Hello</h1>", resp.data)
        self.assertIn(b"<strong>bold</strong>", resp.data)
        self.assertIn(b"PRODUCT_SPEC.md", resp.data)  # sidebar listing

    def test_raw_toggle(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = self._gh()
        resp = self.client.get("/docs?path=README.md&raw=1")
        self.assertIn(b"docs-raw", resp.data)
        self.assertNotIn(b"<h1>Hello</h1>", resp.data)  # not rendered in raw mode

    def test_bad_path_falls_back_to_default(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = self._gh()
        resp = self.client.get("/docs?path=../../etc/passwd")
        self.assertEqual(resp.status_code, 200)
        # Falls back to README render, never fetches the crafted path.
        self.assertIn(b"<h1>Hello</h1>", resp.data)

    def test_unknown_repo_falls_back_to_first(self):
        self._login()
        self.app_module._scan_results = {"repos": [_repo()]}
        self.app_module._github_client = self._gh()
        resp = self.client.get("/docs?repo=ghost")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
