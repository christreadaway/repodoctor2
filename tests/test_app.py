"""
Tests for RepDoctor2.
Covers security, models, github client classification, AI analyzer prompt building, and Flask routes.
"""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Set up test directories before importing modules
TEST_DIR = tempfile.mkdtemp()
TEST_DATA_DIR = os.path.join(TEST_DIR, "data")
TEST_CONFIG_DIR = os.path.join(TEST_DIR, "config")
os.makedirs(TEST_DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(TEST_DATA_DIR, "specs"), exist_ok=True)
os.makedirs(TEST_CONFIG_DIR, exist_ok=True)

import security
import models
import github_client as gh
import ai_analyzer as ai


class TestSecurity(unittest.TestCase):
    """Test credential encryption/decryption."""

    def setUp(self):
        self.test_cred_path = os.path.join(TEST_CONFIG_DIR, "test_credentials.enc")
        self._orig_path = security.CREDENTIALS_PATH
        security.CREDENTIALS_PATH = self.test_cred_path

    def tearDown(self):
        security.CREDENTIALS_PATH = self._orig_path
        if os.path.exists(self.test_cred_path):
            os.remove(self.test_cred_path)

    def test_encrypt_decrypt_roundtrip(self):
        """Credentials can be encrypted and decrypted with the same password."""
        security.encrypt_credentials("testpass", "ghp_test123", "sk-ant-test456")
        result = security.decrypt_credentials("testpass")
        self.assertIsNotNone(result)
        self.assertEqual(result["github_pat"], "ghp_test123")
        self.assertEqual(result["anthropic_key"], "sk-ant-test456")

    def test_wrong_password_returns_none(self):
        """Wrong password returns None."""
        security.encrypt_credentials("correct", "ghp_test", "sk-ant-test")
        result = security.decrypt_credentials("wrong")
        self.assertIsNone(result)

    def test_credentials_exist(self):
        """credentials_exist detects file presence."""
        self.assertFalse(security.credentials_exist())
        security.encrypt_credentials("pass", "ghp", "sk")
        self.assertTrue(security.credentials_exist())

    def test_delete_credentials(self):
        """delete_credentials removes the file."""
        security.encrypt_credentials("pass", "ghp", "sk")
        self.assertTrue(os.path.exists(self.test_cred_path))
        security.delete_credentials()
        self.assertFalse(os.path.exists(self.test_cred_path))

    def test_decrypt_missing_file(self):
        """Decrypt with no file returns None."""
        result = security.decrypt_credentials("anything")
        self.assertIsNone(result)


class TestModels(unittest.TestCase):
    """Test data storage models."""

    def setUp(self):
        self._orig_data = models.DATA_DIR
        self._orig_config = models.CONFIG_DIR
        self._orig_prefs = models.PREFS_PATH
        self._orig_scan = models.SCAN_PATH
        self._orig_cache = models.CACHE_PATH
        self._orig_log = models.ACTION_LOG_PATH

        models.DATA_DIR = TEST_DATA_DIR
        models.CONFIG_DIR = TEST_CONFIG_DIR
        models.PREFS_PATH = os.path.join(TEST_CONFIG_DIR, "test_prefs.json")
        models.SCAN_PATH = os.path.join(TEST_DATA_DIR, "test_scan.json")
        models.CACHE_PATH = os.path.join(TEST_DATA_DIR, "test_cache.json")
        models.ACTION_LOG_PATH = os.path.join(TEST_DATA_DIR, "test_log.json")

    def tearDown(self):
        models.DATA_DIR = self._orig_data
        models.CONFIG_DIR = self._orig_config
        models.PREFS_PATH = self._orig_prefs
        models.SCAN_PATH = self._orig_scan
        models.CACHE_PATH = self._orig_cache
        models.ACTION_LOG_PATH = self._orig_log

        for f in ["test_prefs.json", "test_scan.json", "test_cache.json", "test_log.json"]:
            p = os.path.join(TEST_DATA_DIR, f)
            if os.path.exists(p):
                os.remove(p)
            p = os.path.join(TEST_CONFIG_DIR, f)
            if os.path.exists(p):
                os.remove(p)

    def test_preferences_defaults(self):
        """get_preferences returns defaults when no file exists."""
        prefs = models.get_preferences()
        self.assertEqual(prefs["local_root"], "~/claudesync2")
        self.assertIn("ai_model", prefs)

    def test_save_and_get_preferences(self):
        """Preferences round-trip."""
        prefs = models.get_preferences()
        prefs["local_root"] = "~/custom"
        models.save_preferences(prefs)
        loaded = models.get_preferences()
        self.assertEqual(loaded["local_root"], "~/custom")

    def test_scan_history(self):
        """Scan history save and retrieve."""
        models.save_scan({"repos": [{"name": "test-repo"}], "total_repos": 1})
        latest = models.get_latest_scan()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["total_repos"], 1)
        self.assertIn("timestamp", latest)

    def test_analysis_cache(self):
        """Analysis cache store and retrieve."""
        analysis = {"plain_english_summary": "Test summary"}
        models.cache_analysis("repo", "branch", "abc123", analysis)
        cached = models.get_cached_analysis("repo", "branch", "abc123")
        self.assertIsNotNone(cached)
        self.assertEqual(cached["plain_english_summary"], "Test summary")

    def test_analysis_cache_miss(self):
        """Cache miss returns None."""
        cached = models.get_cached_analysis("repo", "branch", "missing")
        self.assertIsNone(cached)

    def test_action_log(self):
        """Action log append and retrieve."""
        models.log_action("scan", "test-repo", "all", "Scanned 1 repo")
        actions = models.get_action_log()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["type"], "scan")
        self.assertEqual(actions[0]["repo"], "test-repo")

    def test_session_cost(self):
        """SessionCost tracks cumulative costs."""
        cost = models.SessionCost()
        cost.add(100, 50, 0.001)
        cost.add(200, 100, 0.002)
        d = cost.to_dict()
        self.assertEqual(d["total_input_tokens"], 300)
        self.assertEqual(d["total_output_tokens"], 150)
        self.assertAlmostEqual(d["total_cost"], 0.003)
        self.assertEqual(d["analyses_count"], 2)

    def test_spec_save_and_get(self):
        """Product spec save and retrieve."""
        models.save_spec("test-repo", "# Test Spec\nFeature list here")
        spec = models.get_spec("test-repo")
        self.assertIsNotNone(spec)
        self.assertIn("Test Spec", spec)

    def test_spec_missing(self):
        """Missing spec returns None."""
        result = models.get_spec("nonexistent-repo")
        self.assertIsNone(result)

    def test_list_specs(self):
        """list_specs finds saved specs."""
        models.save_spec("repo-a", "spec a")
        models.save_spec("repo-b", "spec b")
        specs = models.list_specs()
        self.assertIn("repo-a", specs)
        self.assertIn("repo-b", specs)


class TestGitHubClassification(unittest.TestCase):
    """Test branch classification logic."""

    def setUp(self):
        self.client = gh.GitHubClient.__new__(gh.GitHubClient)

    def test_safe_to_delete(self):
        """Fully merged branch (0 ahead) is SAFE TO DELETE."""
        comparison = {"ahead_by": 0, "behind_by": 5}
        result = self.client.classify_branch(comparison, None, False)
        self.assertEqual(result, "SAFE TO DELETE")

    def test_ahead_only(self):
        """Branch ahead only is AHEAD ONLY."""
        comparison = {"ahead_by": 3, "behind_by": 0}
        result = self.client.classify_branch(comparison, "2026-02-13T00:00:00Z", False)
        self.assertEqual(result, "AHEAD ONLY")

    def test_diverged(self):
        """Branch both ahead and behind is DIVERGED."""
        comparison = {"ahead_by": 3, "behind_by": 2}
        result = self.client.classify_branch(comparison, "2026-02-13T00:00:00Z", False)
        self.assertEqual(result, "DIVERGED")

    def test_stale(self):
        """Branch with old last commit and not merged is STALE."""
        old_date = "2025-12-01T00:00:00Z"
        comparison = {"ahead_by": 1, "behind_by": 0}
        result = self.client.classify_branch(comparison, old_date, False)
        self.assertEqual(result, "STALE")

    def test_active_pr(self):
        """Branch with open PR is ACTIVE PR."""
        comparison = {"ahead_by": 5, "behind_by": 2}
        result = self.client.classify_branch(comparison, "2026-02-13T00:00:00Z", True)
        self.assertEqual(result, "ACTIVE PR")

    def test_safe_to_delete_with_pr(self):
        """Even with PR, if fully merged, still ACTIVE PR (PR takes priority)."""
        comparison = {"ahead_by": 0, "behind_by": 0}
        result = self.client.classify_branch(comparison, None, True)
        self.assertEqual(result, "ACTIVE PR")


class TestAIAnalyzer(unittest.TestCase):
    """Test AI analyzer utilities."""

    def test_estimate_tokens(self):
        """Token estimation returns positive integer."""
        branch_data = {"name": "feature-test", "ahead_by": 3, "behind_by": 0}
        tokens = ai.estimate_tokens(branch_data)
        self.assertGreater(tokens, 0)
        self.assertIsInstance(tokens, int)

    def test_estimate_tokens_with_spec(self):
        """Token estimation increases with spec text."""
        branch_data = {"name": "test"}
        without_spec = ai.estimate_tokens(branch_data)
        with_spec = ai.estimate_tokens(branch_data, "A very long spec " * 100)
        self.assertGreater(with_spec, without_spec)

    def test_estimate_cost_sonnet(self):
        """Cost estimation for Sonnet model."""
        cost = ai.estimate_cost(1000, 500, "claude-sonnet-4-5-20250929")
        self.assertGreater(cost, 0)
        self.assertLess(cost, 1.0)

    def test_estimate_cost_opus_more_expensive(self):
        """Opus model costs more than Sonnet."""
        sonnet_cost = ai.estimate_cost(1000, 500, "claude-sonnet-4-5-20250929")
        opus_cost = ai.estimate_cost(1000, 500, "claude-opus-4-6")
        self.assertGreater(opus_cost, sonnet_cost)

    def test_build_analysis_prompt(self):
        """Build prompt includes all required context."""
        branch_data = {
            "name": "feature-signup",
            "ahead_by": 4,
            "behind_by": 0,
            "classification": "AHEAD ONLY",
            "last_commit_date": "2026-02-13T10:00:00Z",
            "last_commit_author": "testuser",
            "has_pr": False,
            "commit_messages": [
                {"sha": "abc1234", "message": "Add signup form", "author": "testuser", "date": "2026-02-13"},
            ],
            "files_changed": [
                {"filename": "signup.py", "additions": 50, "deletions": 0, "status": "added"},
            ],
        }
        prompt = ai.build_analysis_prompt(
            "test-repo", branch_data, "main",
            local_path="~/projects/test-repo",
        )
        self.assertIn("feature-signup", prompt)
        self.assertIn("test-repo", prompt)
        self.assertIn("AHEAD ONLY", prompt)
        self.assertIn("Add signup form", prompt)
        self.assertIn("signup.py", prompt)
        self.assertIn("~/projects/test-repo", prompt)

    def test_build_prompt_with_spec(self):
        """Prompt includes spec text when provided."""
        branch_data = {
            "name": "test",
            "ahead_by": 1,
            "behind_by": 0,
            "classification": "AHEAD ONLY",
        }
        prompt = ai.build_analysis_prompt(
            "repo", branch_data, "main",
            spec_text="Feature: User authentication",
        )
        self.assertIn("User authentication", prompt)

    def test_build_prompt_without_spec(self):
        """Prompt notes missing spec."""
        branch_data = {
            "name": "test",
            "ahead_by": 1,
            "behind_by": 0,
            "classification": "AHEAD ONLY",
        }
        prompt = ai.build_analysis_prompt("repo", branch_data, "main")
        self.assertIn("No product spec provided", prompt)


class TestFlaskApp(unittest.TestCase):
    """Test Flask routes."""

    def setUp(self):
        from app import app
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        self.app = app
        self.client = app.test_client()

    def test_login_page_loads(self):
        """Login page returns 200."""
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"RepDoctor2", resp.data)

    def test_unauthenticated_redirect(self):
        """Unauthenticated access to dashboard redirects to login."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_unauthenticated_setup_guide(self):
        """Unauthenticated access to setup guide redirects."""
        resp = self.client.get("/setup-guide")
        self.assertEqual(resp.status_code, 302)

    def test_unauthenticated_archive(self):
        """Unauthenticated access to archive redirects."""
        resp = self.client.get("/archive")
        self.assertEqual(resp.status_code, 302)

    def test_unauthenticated_settings(self):
        """Unauthenticated access to settings redirects."""
        resp = self.client.get("/settings")
        self.assertEqual(resp.status_code, 302)

    def test_unauthenticated_action_log(self):
        """Unauthenticated access to action log redirects."""
        resp = self.client.get("/action-log")
        self.assertEqual(resp.status_code, 302)

    def test_dashboard_when_authenticated(self):
        """Authenticated user can access dashboard."""
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "testuser"
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Dashboard", resp.data)

    def test_setup_guide_when_authenticated(self):
        """Authenticated user can access setup guide."""
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "testuser"
        resp = self.client.get("/setup-guide")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Setup Guide", resp.data)
        self.assertIn(b"CLAUDE.md", resp.data)

    def test_archive_when_authenticated(self):
        """Authenticated user can access archive."""
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "testuser"
        resp = self.client.get("/archive")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Archive", resp.data)

    def test_settings_when_authenticated(self):
        """Authenticated user can access settings."""
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "testuser"
        resp = self.client.get("/settings")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Settings", resp.data)

    def test_action_log_when_authenticated(self):
        """Authenticated user can access action log."""
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["github_user"] = "testuser"
        resp = self.client.get("/action-log")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Action Log", resp.data)

    def test_logout(self):
        """Logout clears session and redirects."""
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
        resp = self.client.get("/logout")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_session_cost_api(self):
        """Session cost API returns JSON."""
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
        resp = self.client.get("/api/session-cost")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("total_cost", data)
        self.assertIn("analyses_count", data)

    def test_login_first_time_missing_fields(self):
        """First-time login with missing fields shows error."""
        with patch.object(security, "credentials_exist", return_value=False):
            resp = self.client.post("/login", data={
                "password": "test",
                "github_pat": "",
                "anthropic_key": "",
            })
            self.assertEqual(resp.status_code, 200)

    def test_login_wrong_password(self):
        """Login with wrong password shows error."""
        with patch.object(security, "credentials_exist", return_value=True):
            with patch.object(security, "decrypt_credentials", return_value=None):
                resp = self.client.post("/login", data={"password": "wrong"})
                self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
