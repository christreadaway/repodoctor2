"""
Session 24 features:
  6. POST /scan runs scan_repo_lite on 8 threads. Results must be identical
     (content AND order) to the old one-at-a-time loop, a GitHubAuthError
     must cancel the rest and reach the global handler, and any other per-repo
     error must stay isolated to that repo's row.
  7. The Anthropic key is checked at login and on a Settings save, so a bad
     key surfaces up front instead of on the first GENERATE click. The key
     itself must never reach a log line or a flash.

No test here touches the network: GitHub is a fake client (or a real
GitHubClient over a fake session), and anthropic.Anthropic is mocked.
"""

import importlib
import json
import logging
import os
import random
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import anthropic
import requests

import ai_analyzer as ai
import app as app_module
import github_client as gh
import models
import security

# The SDK's own HTTP library: httpx2 on anthropic 1.x+, httpx on 0.x. Either
# install runs these tests.
try:
    _httpx = importlib.import_module("httpx2")
except ImportError:  # pragma: no cover - older anthropic SDK
    _httpx = importlib.import_module("httpx")
_REQ = _httpx.Request("GET", "https://api.anthropic.com/v1/models")

SECRET_KEY = "sk-ant-api03-TESTSECRETdoNotLog_abc123"
SECRET_FRAGMENT = "TESTSECRETdoNotLog"


def _status_error(cls, code: int, message: str = "error"):
    return cls(message, response=_httpx.Response(code, request=_REQ), body=None)


# ---------------------------------------------------------------------------
# Fakes for the scan
# ---------------------------------------------------------------------------

def _repo(name: str, owner: str = "o") -> dict:
    return {
        "owner": {"login": owner},
        "name": name,
        "full_name": f"{owner}/{name}",
        "default_branch": "main",
        "private": name.endswith("p"),
        "html_url": f"https://github.com/{owner}/{name}",
        "description": f"desc {name}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-02-01T00:00:00Z",
        "pushed_at": "2026-03-01T00:00:00Z",
    }


class FakeScanClient:
    """Thread-safe stand-in for GitHubClient covering every method
    scan_repo_lite calls. Outputs are a pure function of the repo name, so a
    sequential and a parallel run must produce identical rows. Records how
    many scans overlap and the order they finish in."""

    def __init__(self, names, delays=None, failures=None, barrier=None):
        self.delays = delays or {}
        self.failures = failures or {}
        self.barrier = barrier
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        rng = random.Random(42)
        self.spec = {}
        for i, n in enumerate(names):
            # Few distinct branch counts, so many repos tie and the stable
            # sort's tie order (= input order) is part of what's compared.
            extra = i % 3
            henry = 1 if i % 4 == 0 else 0
            branches = ["main"] + [f"feat-{n}-{k}" for k in range(extra)] + \
                       [f"henry-{n}-{k}" for k in range(henry)]
            has_notes = i % 2 == 0
            has_spec = i % 3 == 0
            self.spec[n] = {
                "branches": branches,
                "files": {
                    "CLAUDE.md": i % 5 != 0, "LICENSE": True,
                    "PRODUCT_SPEC.md": has_spec, "PROJECT_STATUS.md": i % 2 == 1,
                    "SESSION_NOTES.md": has_notes,
                },
                "paths": ({"SESSION_NOTES.md": "docs/SESSION_NOTES.md"} if has_notes else {}) |
                         ({"PRODUCT_SPEC.md": "PRODUCT_SPEC.md"} if has_spec else {}),
                "langs": {"Python": rng.randint(1, 10_000), "HTML": rng.randint(0, 500)},
                "last_commit": None if i % 7 == 6 else "2026-03-10T00:00:00Z",
                "doc_ts": "2026-03-09T00:00:00Z" if i % 2 == 0 else "2026-01-01T00:00:00Z",
            }

    # scan_repo_lite's first call: where overlap is measured and faults fire.
    def get_branches(self, owner, name, strict=False):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.append(name)
        try:
            if self.barrier is not None:
                self.barrier.wait()
            time.sleep(self.delays.get(name, 0))
            fault = self.failures.get(name)
            if fault is not None:
                raise fault
            return [{"name": b, "commit": {"sha": "x"}} for b in self.spec[name]["branches"]]
        finally:
            with self.lock:
                self.active -= 1

    def check_required_files(self, owner, name, ref=None, strict=False):
        s = self.spec[name]
        return dict(s["files"]), dict(s["paths"])

    def get_language_bytes(self, owner, name):
        with self.lock:
            self.finished.append(name)
        return dict(self.spec[name]["langs"])

    def get_last_commit_date(self, owner, name, ref=None):
        return self.spec[name]["last_commit"]

    def get_last_commit_for_path(self, owner, name, path, ref=None):
        return self.spec[name]["doc_ts"]


def legacy_sequential_scan(client, repos, excluded):
    """The scan loop exactly as it was before parallelization (Session 23),
    including the per-repo error row and the stable sort."""
    results = []
    for repo in repos:
        if repo["full_name"] in excluded or repo["name"] in excluded:
            continue
        try:
            results.append(gh.scan_repo_lite(client, repo))
        except gh.GitHubAuthError:
            raise
        except Exception as e:
            results.append({
                "owner": repo["owner"]["login"],
                "name": repo["name"],
                "full_name": repo["full_name"],
                "default_branch": repo.get("default_branch", "main"),
                "private": repo.get("private", False),
                # Session 24: the scan now carries GitHub's archived / fork flags.
                "archived": bool(repo.get("archived", False)),
                "fork": bool(repo.get("fork", False)),
                "html_url": repo.get("html_url", ""),
                "description": repo.get("description", ""),
                "created_at": repo.get("created_at", ""),
                "updated_at": repo.get("updated_at", ""),
                "pushed_at": repo.get("pushed_at", ""),
                "docs_updated": None,
                "total_branch_count": 0,
                "non_default_branch_count": 0,
                "henry_branch_count": 0,
                "non_henry_branch_count": 0,
                "branch_names": [],
                "required_files": {},
                "files_present": 0,
                "files_total": 5,
                "code_size_bytes": 0,
                "languages": {},
                "error": str(e),
            })
    results.sort(key=lambda r: r.get("non_henry_branch_count", 0), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Feature 6: parallel scan
# ---------------------------------------------------------------------------

class TestScanReposParallel(unittest.TestCase):
    """Unit tests on app._scan_repos_parallel."""

    def test_identical_to_sequential_even_when_completion_order_differs(self):
        names = [f"r{i:02d}" for i in range(24)]
        repos = [_repo(n) for n in names]
        rng = random.Random(7)
        delays = {n: rng.uniform(0, 0.03) for n in names}
        seq_client = FakeScanClient(names)
        expected = [gh.scan_repo_lite(seq_client, r) for r in repos]

        par_client = FakeScanClient(names, delays=delays)
        got = app_module._scan_repos_parallel(par_client, repos)

        self.assertEqual(got, expected)
        self.assertEqual([r["name"] for r in got], names)
        # Prove the reorder path was exercised: repos really finished out of order.
        self.assertNotEqual(par_client.finished, names)

    def test_order_kept_when_last_repo_finishes_first(self):
        n = app_module.SCAN_MAX_WORKERS  # all start at once, none queued
        names = [f"r{i}" for i in range(n)]
        # Strictly decreasing latency: the last repo submitted finishes first.
        delays = {name: 0.02 * (n - i) for i, name in enumerate(names)}
        client = FakeScanClient(names, delays=delays)
        got = app_module._scan_repos_parallel(client, [_repo(x) for x in names])
        self.assertEqual([r["name"] for r in got], names)
        self.assertEqual(client.finished[0], names[-1])

    def test_empty_list_returns_empty_without_a_pool(self):
        with patch.object(app_module, "ThreadPoolExecutor") as pool_cls:
            self.assertEqual(app_module._scan_repos_parallel(FakeScanClient([]), []), [])
        pool_cls.assert_not_called()

    def test_auth_error_cancels_queued_repos_and_propagates(self):
        names = [f"r{i:02d}" for i in range(40)]
        delays = {n: 0.05 for n in names}
        delays["r02"] = 0
        client = FakeScanClient(names, delays=delays,
                                failures={"r02": gh.GitHubAuthError("401 for r02")})
        t0 = time.monotonic()
        with self.assertRaises(gh.GitHubAuthError) as ctx:
            app_module._scan_repos_parallel(client, [_repo(n) for n in names])
        elapsed = time.monotonic() - t0
        self.assertIn("r02", str(ctx.exception))
        # 8 in flight + at most a few picked up before cancel; never all 40.
        self.assertLessEqual(len(client.started), 2 * app_module.SCAN_MAX_WORKERS)
        # The queue was dropped: nowhere near the 40 x 0.05s = 2s a full run takes.
        self.assertLess(elapsed, 1.0)

    def test_other_errors_stay_on_their_own_row(self):
        names = ["a", "b", "c", "d"]
        client = FakeScanClient(names, failures={
            "b": RuntimeError("boom"),
            "d": requests.exceptions.ReadTimeout("read timed out"),
        })
        repos = [_repo(n) for n in names]
        got = app_module._scan_repos_parallel(client, repos)
        self.assertEqual([r["name"] for r in got], names)
        self.assertNotIn("error", got[0])
        self.assertNotIn("error", got[2])
        self.assertEqual(got[1]["error"], "boom")
        self.assertEqual(got[3]["error"], "read timed out")
        for row in (got[1], got[3]):
            self.assertIsNone(row["docs_updated"])
            self.assertEqual(row["non_henry_branch_count"], 0)
            self.assertEqual(row["files_total"], 5)
        # A healthy repo's row is exactly what the sequential call produces.
        self.assertEqual(got[0], gh.scan_repo_lite(FakeScanClient(names), repos[0]))

    def test_per_repo_error_is_logged(self):
        client = FakeScanClient(["a", "b"], failures={"b": RuntimeError("boom")})
        with self.assertLogs("app", level="WARNING") as cm:
            app_module._scan_repos_parallel(client, [_repo("a"), _repo("b")])
        self.assertTrue(any("o/b" in line and "RuntimeError" in line for line in cm.output))

    def test_error_entry_matches_the_legacy_row(self):
        repo = _repo("x")
        e = ValueError("bad thing")
        legacy = legacy_sequential_scan(
            FakeScanClient(["x"], failures={"x": e}), [repo], set())[0]
        self.assertEqual(app_module._scan_error_entry(repo, e), legacy)

    def test_eight_scans_really_run_at_once(self):
        """A Barrier(8) only releases when 8 scans are inside get_branches at
        the same moment, so this fails (BrokenBarrierError rows) if the pool
        ever runs fewer than 8 in parallel."""
        names = [f"r{i:02d}" for i in range(16)]
        barrier = threading.Barrier(8, timeout=5)  # literal 8: the spec, not the constant
        client = FakeScanClient(names, barrier=barrier)
        got = app_module._scan_repos_parallel(client, [_repo(n) for n in names])
        self.assertEqual([r.get("error") for r in got], [None] * 16)
        self.assertEqual(client.max_active, 8)
        self.assertEqual(app_module.SCAN_MAX_WORKERS, 8)

    def test_parallel_wall_time_beats_sequential(self):
        names = [f"r{i:02d}" for i in range(16)]
        delays = {n: 0.1 for n in names}  # sequential = 1.6s
        client = FakeScanClient(names, delays=delays)
        t0 = time.monotonic()
        got = app_module._scan_repos_parallel(client, [_repo(n) for n in names])
        elapsed = time.monotonic() - t0
        self.assertEqual(len(got), 16)
        self.assertLess(elapsed, 0.8, f"16 x 0.1s took {elapsed:.2f}s - not parallel")
        self.assertLessEqual(client.max_active, app_module.SCAN_MAX_WORKERS)

    def test_never_more_workers_than_repos(self):
        with patch.object(app_module, "ThreadPoolExecutor",
                          wraps=app_module.ThreadPoolExecutor) as pool_cls:
            app_module._scan_repos_parallel(FakeScanClient(["a", "b"]), [_repo("a"), _repo("b")])
        self.assertEqual(pool_cls.call_args.kwargs["max_workers"], 2)


class _FakeResp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


def _fake_github_get(url, params=None, **kwargs):
    """A pure function of the URL: what GitHub would answer for the calls
    scan_repo_lite makes. Sleeps a little so threads interleave."""
    time.sleep(random.uniform(0, 0.004))
    path = url.split("api.github.com", 1)[1]
    parts = path.strip("/").split("/")
    name = parts[2]
    seed = sum(ord(c) for c in name)
    if path.endswith("/branches"):
        return _FakeResp(200, [{"name": "main"}] + [{"name": f"b{k}"} for k in range(seed % 4)])
    if "/git/trees/" in path:
        tree = [{"path": "README.md", "type": "blob"}, {"path": "src/app.py", "type": "blob"}]
        if seed % 2 == 0:
            tree.append({"path": "SESSION_NOTES.md", "type": "blob"})
        if seed % 3 == 0:
            tree.append({"path": "docs/PRODUCT_SPEC.md", "type": "blob"})
        return _FakeResp(200, {"tree": tree, "truncated": False})
    if path.endswith("/languages"):
        return _FakeResp(200, {"Python": seed * 10, "CSS": seed})
    if path.endswith("/commits"):
        day = 9 if (params or {}).get("path") else 10
        return _FakeResp(200, [{"commit": {"committer": {"date": f"2026-03-{day:02d}T00:00:00Z"}}}])
    return _FakeResp(404, {"message": "Not Found"})


class TestScanThreadSafetyWithRealClient(unittest.TestCase):
    """The real GitHubClient (its _get, pagination, tree filtering, commit
    lookups) driven from 8 threads over one shared session must give the
    same rows as a sequential run and leave the session untouched."""

    def test_real_client_parallel_equals_sequential(self):
        client = gh.GitHubClient("ghp_fake")
        headers_before = dict(client.session.headers)
        repos = [_repo(f"repo{i}") for i in range(30)]
        with patch.object(client.session, "get", side_effect=_fake_github_get):
            expected = [gh.scan_repo_lite(client, r) for r in repos]
            got = app_module._scan_repos_parallel(client, repos)
        self.assertEqual(got, expected)
        self.assertTrue(any(r["docs_updated"] is not None for r in got))
        self.assertEqual(dict(client.session.headers), headers_before)

    def test_real_client_401_mid_scan_propagates(self):
        client = gh.GitHubClient("ghp_fake")

        def get(url, params=None, **kw):
            if "/repo3/" in url:
                return _FakeResp(401, {"message": "Bad credentials"})
            return _fake_github_get(url, params=params, **kw)

        with patch.object(client.session, "get", side_effect=get):
            with self.assertRaises(gh.GitHubAuthError):
                app_module._scan_repos_parallel(client, [_repo(f"repo{i}") for i in range(12)])


class _AppStateMixin:
    """Save/restore the app's process-global state and sandbox the files the
    routes write, so these tests can't leak into each other or the real data."""

    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self._saved = {k: getattr(app_module, k) for k in (
            "_github_client", "_credentials", "_scan_results", "_activity_cache",
            "_activity_ranges", "_anthropic_key_status")}
        self.tmp = tempfile.mkdtemp(prefix="s24-")
        self._paths = (models.SCAN_PATH, models.ACTION_LOG_PATH, security.CREDENTIALS_PATH)
        models.SCAN_PATH = os.path.join(self.tmp, "scan_history.json")
        models.ACTION_LOG_PATH = os.path.join(self.tmp, "action_log.json")
        security.CREDENTIALS_PATH = os.path.join(self.tmp, "credentials.enc")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(app_module, k, v)
        models.SCAN_PATH, models.ACTION_LOG_PATH, security.CREDENTIALS_PATH = self._paths

    def login_session(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def flashes(self) -> list[tuple[str, str]]:
        with self.client.session_transaction() as sess:
            return list(sess.get("_flashes", []))


class TestScanRoute(_AppStateMixin, unittest.TestCase):

    def _prefs_with_excluded(self, excluded):
        prefs = dict(models.get_preferences())
        prefs["excluded_repos"] = list(excluded)
        return prefs

    def test_route_matches_the_old_sequential_scan_exactly(self):
        names = [f"r{i:02d}" for i in range(20)] + ["skipme", "alsoskip"]
        repos = [_repo(n) for n in names]
        failures = {"r05": RuntimeError("tree exploded"), "r11": ValueError("bad json")}
        rng = random.Random(3)
        delays = {n: rng.uniform(0, 0.02) for n in names}
        excluded = ["o/skipme", "alsoskip"]  # by full_name and by bare name

        legacy_client = FakeScanClient(names, failures=failures)
        expected = legacy_sequential_scan(legacy_client, repos, set(excluded))

        fake = FakeScanClient(names, delays=delays, failures=failures)
        fake.get_repos = lambda: [dict(r) for r in repos]
        app_module._github_client = fake
        self.login_session()
        with patch.object(app_module.models, "get_preferences",
                          return_value=self._prefs_with_excluded(excluded)):
            resp = self.client.post("/scan")
        self.assertEqual(resp.status_code, 302)
        got = app_module._scan_results
        self.assertEqual(got["repos"], expected)
        self.assertEqual(got["total_repos"], 20)
        self.assertEqual(got["total_branches"],
                         sum(r.get("non_henry_branch_count", 0) for r in expected))
        self.assertNotIn("skipme", fake.started)
        self.assertNotIn("alsoskip", fake.started)
        # Persisted exactly as held in memory.
        self.assertEqual(models.get_latest_scan()["repos"], expected)
        self.assertTrue(any(c == "success" and "Scan complete: 20 repos" in m
                            for c, m in self.flashes()))

    def test_route_auth_error_redirects_with_remedy_and_saves_nothing(self):
        names = [f"r{i}" for i in range(12)]
        fake = FakeScanClient(names, delays={n: 0.02 for n in names},
                              failures={"r4": gh.GitHubAuthError("401")})
        fake.get_repos = lambda: [_repo(n) for n in names]
        app_module._github_client = fake
        app_module._scan_results = {"repos": [{"name": "previous"}], "total_repos": 1,
                                    "total_branches": 0, "scanned_at": "2026-01-01T00:00:00+00:00"}
        self.login_session()
        with patch.object(app_module.models, "save_scan") as save:
            resp = self.client.post("/scan")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))
        self.assertTrue(any(c == "error" and "authentication failed" in m
                            for c, m in self.flashes()))
        save.assert_not_called()
        self.assertEqual(app_module._scan_results["repos"], [{"name": "previous"}])

    def test_route_logs_elapsed_count_and_errors(self):
        fake = FakeScanClient(["a", "b", "c"], failures={"c": RuntimeError("x")})
        fake.get_repos = lambda: [_repo("a"), _repo("b"), _repo("c")]
        app_module._github_client = fake
        self.login_session()
        with self.assertLogs("app", level="INFO") as cm:
            self.client.post("/scan")
        line = next(l for l in cm.output if "Scan finished" in l)
        self.assertIn("3 repos in", line)
        self.assertIn("1 errors", line)
        self.assertRegex(line, r"in \d+\.\ds")
        last = models.get_action_log()[-1]
        self.assertEqual(last["type"], "scan")
        self.assertRegex(last["details"], r"Scanned 3 repos, \d+ total branches in \d+\.\ds \(1 errors\)")

    def test_route_with_no_repos(self):
        fake = FakeScanClient([])
        fake.get_repos = lambda: []
        app_module._github_client = fake
        self.login_session()
        resp = self.client.post("/scan")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(app_module._scan_results["repos"], [])


# ---------------------------------------------------------------------------
# Feature 7: Anthropic key check
# ---------------------------------------------------------------------------

def _mock_sdk(list_side_effect=None):
    """Patch for ai_analyzer.anthropic.Anthropic: a class mock whose
    instance's models.list raises/returns as told."""
    cls = MagicMock(name="Anthropic")
    if list_side_effect is not None:
        cls.return_value.models.list.side_effect = list_side_effect
    return patch.object(ai.anthropic, "Anthropic", cls), cls


class TestVerifyAnthropicKey(unittest.TestCase):

    def _verify(self, side_effect=None, key=SECRET_KEY):
        p, cls = _mock_sdk(side_effect)
        with p:
            result = ai.verify_anthropic_key(key)
        return result, cls

    def test_ok(self):
        (status, message), cls = self._verify()
        self.assertEqual(status, "ok")
        self.assertIn("accepted", message)
        cls.return_value.models.list.assert_called_once_with(limit=1)

    def test_client_is_built_for_one_fast_attempt(self):
        _, cls = self._verify()
        kwargs = cls.call_args.kwargs
        self.assertEqual(kwargs["api_key"], SECRET_KEY)
        self.assertEqual(kwargs["max_retries"], 0)
        t = kwargs["timeout"]
        self.assertLessEqual(t.connect, 2.0)
        self.assertLessEqual(t.read, 2.5)
        self.assertLessEqual(t.connect + t.read, 5.0)
        self.assertLessEqual(ai.VERIFY_DEADLINE_SECONDS, 5.0)

    def test_key_is_stripped(self):
        _, cls = self._verify(key=f"  {SECRET_KEY}\n")
        self.assertEqual(cls.call_args.kwargs["api_key"], SECRET_KEY)

    def test_401_is_invalid(self):
        (status, message), _ = self._verify(
            _status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"))
        self.assertEqual(status, "invalid")
        self.assertIn("401", message)

    def test_timeout_is_unverified(self):
        (status, message), _ = self._verify(anthropic.APITimeoutError(request=_REQ))
        self.assertEqual(status, "unverified")
        self.assertIn("Timed out", message)

    def test_connection_error_is_unverified(self):
        (status, message), _ = self._verify(anthropic.APIConnectionError(request=_REQ))
        self.assertEqual(status, "unverified")
        self.assertIn("connect", message)

    def test_500_and_503_are_unverified(self):
        for code in (500, 503, 529):
            (status, message), _ = self._verify(
                _status_error(anthropic.InternalServerError, code, "overloaded"))
            self.assertEqual(status, "unverified", code)
            self.assertIn(f"HTTP {code}", message)
            self.assertIn("server error", message)

    def test_other_http_errors_are_unverified(self):
        cases = [(anthropic.RateLimitError, 429, "rate limited"),
                 (anthropic.PermissionDeniedError, 403, "permission denied")]
        for cls, code, words in cases:
            (status, message), _ = self._verify(_status_error(cls, code))
            self.assertEqual(status, "unverified", code)
            self.assertIn(words, message)

    def test_unexpected_exception_is_unverified_and_never_raises(self):
        (status, message), _ = self._verify(AttributeError("no models on this SDK"))
        self.assertEqual(status, "unverified")
        self.assertIn("AttributeError", message)

    def test_client_construction_failure_is_unverified(self):
        cls = MagicMock(side_effect=TypeError("unexpected kwarg"))
        with patch.object(ai.anthropic, "Anthropic", cls):
            status, _ = ai.verify_anthropic_key(SECRET_KEY)
        self.assertEqual(status, "unverified")

    def test_empty_key_is_invalid_without_a_network_call(self):
        for empty in ("", "   ", None):
            p, cls = _mock_sdk()
            with p:
                status, message = ai.verify_anthropic_key(empty)
            self.assertEqual(status, "invalid", repr(empty))
            self.assertIn("No Anthropic API key is set", message)
            cls.assert_not_called()

    def test_thread_start_failure_is_unverified_not_a_crash(self):
        p, cls = _mock_sdk()
        with p, patch.object(ai.threading.Thread, "start",
                             side_effect=RuntimeError("can't start new thread")):
            status, message = ai.verify_anthropic_key(SECRET_KEY)
        self.assertEqual(status, "unverified")
        self.assertIn("Couldn't run the key check", message)
        cls.assert_not_called()

    def test_hard_deadline_when_the_call_hangs(self):
        release = threading.Event()
        p, cls = _mock_sdk(lambda **kw: release.wait(10))
        try:
            with p, patch.object(ai, "VERIFY_DEADLINE_SECONDS", 0.2):
                t0 = time.monotonic()
                status, message = ai.verify_anthropic_key(SECRET_KEY)
                elapsed = time.monotonic() - t0
        finally:
            release.set()
        self.assertEqual(status, "unverified")
        self.assertIn("didn't answer", message)
        self.assertLess(elapsed, 1.0)

    def test_key_never_logged_or_returned_in_any_outcome(self):
        """Every branch, including errors whose text contains the key."""
        outcomes = [
            None,
            _status_error(anthropic.AuthenticationError, 401, f"bad key {SECRET_KEY}"),
            anthropic.APITimeoutError(request=_REQ),
            anthropic.APIConnectionError(message=f"proxy refused {SECRET_KEY}", request=_REQ),
            _status_error(anthropic.InternalServerError, 500, f"oops {SECRET_KEY}"),
            RuntimeError(f"weird failure with {SECRET_KEY} inside"),
        ]
        for effect in outcomes:
            p, _ = _mock_sdk(effect)
            with p, self.assertLogs(level="DEBUG") as cm:
                status, message = ai.verify_anthropic_key(SECRET_KEY)
            blob = "\n".join(cm.output) + message
            self.assertNotIn(SECRET_FRAGMENT, blob, repr(effect))
            self.assertNotIn("sk-ant-api03", blob, repr(effect))
            self.assertTrue(any("Anthropic key check" in line for line in cm.output))

    def test_redact_strips_key_shapes(self):
        self.assertEqual(ai._redact(f"a {SECRET_KEY} b", SECRET_KEY), "a [redacted] b")
        self.assertNotIn("sk-ant", ai._redact("other sk-ant-api03-XYZ_12 key", "irrelevant"))


class TestLoginKeyCheck(_AppStateMixin, unittest.TestCase):

    GITHUB_USER = {"login": "builder", "_scopes": "repo, read:org"}

    def _returning_login(self, verify_result=None, sdk_effect="unused", creds=None):
        creds = creds or {"github_pat": "ghp_x", "anthropic_key": SECRET_KEY}
        patches = [
            patch.object(security, "credentials_exist", return_value=True),
            patch.object(security, "decrypt_credentials", return_value=dict(creds)),
            patch("github_client.GitHubClient.verify_token", return_value=dict(self.GITHUB_USER)),
        ]
        if verify_result is not None:
            patches.append(patch.object(ai, "verify_anthropic_key", return_value=verify_result))
        else:
            patches.append(_mock_sdk(sdk_effect)[0])
        for p in patches:
            p.start()
        try:
            return self.client.post("/login", data={"password": "right"})
        finally:
            for p in reversed(patches):
                p.stop()

    def _authenticated(self) -> bool:
        with self.client.session_transaction() as sess:
            return bool(sess.get("authenticated"))

    def test_invalid_key_warns_but_does_not_block_login(self):
        resp = self._returning_login(("invalid", "Anthropic rejected the API key (HTTP 401)."))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))
        self.assertTrue(self._authenticated())
        warnings = [m for c, m in self.flashes() if c == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("AI features", warnings[0])
        self.assertIn("summaries", warnings[0])
        self.assertIn("Settings", warnings[0])
        self.assertIn("Update Anthropic key", warnings[0])
        self.assertEqual(app_module.get_anthropic_key_status()["status"], "invalid")
        self.assertEqual(app_module.get_anthropic_key_status()["source"], "login")

    def test_unverified_logs_only_no_flash(self):
        with self.assertLogs("app", level="WARNING") as cm:
            resp = self._returning_login(("unverified", "Timed out reaching Anthropic."))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self._authenticated())
        self.assertEqual(self.flashes(), [])
        self.assertTrue(any("unverified" in line for line in cm.output))
        self.assertEqual(app_module.get_anthropic_key_status()["status"], "unverified")

    def test_ok_is_silent(self):
        resp = self._returning_login(("ok", "Anthropic accepted the API key."))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.flashes(), [])
        self.assertEqual(app_module.get_anthropic_key_status()["status"], "ok")

    def test_next_redirect_still_honored(self):
        with patch.object(security, "credentials_exist", return_value=True), \
             patch.object(security, "decrypt_credentials",
                          return_value={"github_pat": "p", "anthropic_key": SECRET_KEY}), \
             patch("github_client.GitHubClient.verify_token", return_value=dict(self.GITHUB_USER)), \
             patch.object(ai, "verify_anthropic_key", return_value=("invalid", "x")):
            resp = self.client.post("/login?next=/stats", data={"password": "right"})
        self.assertTrue(resp.headers["Location"].endswith("/stats"))

    def test_empty_stored_key_flags_no_key_set(self):
        resp = self._returning_login(sdk_effect=None,
                                     creds={"github_pat": "ghp_x", "anthropic_key": ""})
        self.assertEqual(resp.status_code, 302)
        warnings = [m for c, m in self.flashes() if c == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("No Anthropic API key is set", warnings[0])

    def test_end_to_end_401_through_the_real_helper(self):
        resp = self._returning_login(
            sdk_effect=_status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self._authenticated())
        warnings = [m for c, m in self.flashes() if c == "warning"]
        self.assertTrue(warnings and "401" in warnings[0])

    def test_github_failure_still_gates_and_skips_the_anthropic_check(self):
        with patch.object(security, "credentials_exist", return_value=True), \
             patch.object(security, "decrypt_credentials",
                          return_value={"github_pat": "bad", "anthropic_key": SECRET_KEY}), \
             patch("github_client.GitHubClient.verify_token", return_value=None), \
             patch.object(ai, "verify_anthropic_key") as verify:
            resp = self.client.post("/login", data={"password": "right"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._authenticated())
        verify.assert_not_called()

    def test_first_time_setup_with_invalid_key_still_saves_and_logs_in(self):
        with patch.object(security, "credentials_exist", return_value=False), \
             patch("github_client.GitHubClient.verify_token", return_value=dict(self.GITHUB_USER)), \
             patch.object(ai, "verify_anthropic_key",
                          return_value=("invalid", "Anthropic rejected the API key (HTTP 401).")):
            resp = self.client.post("/login", data={
                "password": "longenough", "github_pat": "ghp_new", "anthropic_key": SECRET_KEY})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self._authenticated())
        self.assertEqual(security.decrypt_credentials("longenough")["anthropic_key"], SECRET_KEY)
        cats = [c for c, _ in self.flashes()]
        self.assertEqual(cats, ["success", "warning"])
        self.assertEqual(app_module.get_anthropic_key_status()["source"], "setup")

    def test_login_never_logs_or_flashes_the_key(self):
        effects = [
            _status_error(anthropic.AuthenticationError, 401, f"bad {SECRET_KEY}"),
            anthropic.APIConnectionError(message=f"refused {SECRET_KEY}", request=_REQ),
            RuntimeError(f"boom {SECRET_KEY}"),
        ]
        for effect in effects:
            with self.assertLogs(level="DEBUG") as cm:
                self._returning_login(sdk_effect=effect)
            blob = "\n".join(cm.output) + json.dumps(self.flashes())
            self.assertNotIn(SECRET_FRAGMENT, blob, repr(effect))
            with self.client.session_transaction() as sess:
                sess.pop("_flashes", None)

    def test_status_line_reaches_the_app_log_file_without_the_key(self):
        """The real data/logs/app.log handler (not a test capture)."""
        before = os.path.getsize(models.APP_LOG_PATH) if os.path.exists(models.APP_LOG_PATH) else 0
        self._returning_login(sdk_effect=anthropic.APIConnectionError(
            message=f"refused {SECRET_KEY}", request=_REQ))
        for h in logging.getLogger().handlers:
            h.flush()
        with open(models.APP_LOG_PATH, encoding="utf-8") as f:
            f.seek(before)
            new = f.read()
        self.assertIn("Anthropic key unverified at login", new)
        self.assertNotIn(SECRET_FRAGMENT, new)


class TestSettingsKeyUpdate(_AppStateMixin, unittest.TestCase):

    PASSWORD = "correct-horse-battery"
    NEW_KEY = "sk-ant-api03-NEWKEYsecretValue_999"

    def setUp(self):
        super().setUp()
        security.encrypt_credentials(self.PASSWORD, "ghp_keepme", "sk-ant-api03-OLDKEYsecret")
        app_module._credentials = {"github_pat": "ghp_keepme",
                                   "anthropic_key": "sk-ant-api03-OLDKEYsecret"}
        self.login_session()

    def _update(self, key=None, password=None, verify_result=("ok", "Anthropic accepted the API key."),
                save_unverified=False):
        data = {
            "action": "update_anthropic_key",
            "anthropic_key": self.NEW_KEY if key is None else key,
            "password": self.PASSWORD if password is None else password,
        }
        if save_unverified:
            data["save_unverified"] = "1"
        with patch.object(ai, "verify_anthropic_key", return_value=verify_result) as verify:
            resp = self.client.post("/settings", data=data)
        self.assertEqual(resp.status_code, 302)
        return verify

    def test_ok_saves_reencrypts_and_flashes_success(self):
        verify = self._update(key=f"  {self.NEW_KEY} ")
        verify.assert_called_once_with(self.NEW_KEY)
        saved = security.decrypt_credentials(self.PASSWORD)
        self.assertEqual(saved, {"github_pat": "ghp_keepme", "anthropic_key": self.NEW_KEY})
        self.assertEqual(app_module._credentials["anthropic_key"], self.NEW_KEY)
        self.assertEqual(app_module._credentials["github_pat"], "ghp_keepme")
        self.assertEqual(self.flashes(), [
            ("success", "Anthropic key saved and verified: AI features are ready.")])
        st = app_module.get_anthropic_key_status()
        self.assertEqual((st["status"], st["source"]), ("ok", "settings"))
        last = models.get_action_log()[-1]
        self.assertEqual(last["type"], "anthropic_key")
        self.assertIn("saved", last["details"])

    def test_invalid_is_not_saved_and_old_key_kept(self):
        app_module._anthropic_key_status = {"status": "ok", "message": "m",
                                            "checked_at": "", "source": "login"}
        self._update(verify_result=("invalid", "Anthropic rejected the API key (HTTP 401): x."))
        self.assertEqual(security.decrypt_credentials(self.PASSWORD)["anthropic_key"],
                         "sk-ant-api03-OLDKEYsecret")
        self.assertEqual(app_module._credentials["anthropic_key"], "sk-ant-api03-OLDKEYsecret")
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIn("Not saved", msg)
        self.assertIn("previous key is unchanged", msg)
        # The loaded (old) key's status is untouched by a rejected candidate.
        self.assertEqual(app_module.get_anthropic_key_status()["status"], "ok")

    def test_unverified_is_held_unless_save_anyway(self):
        # Session 24 review M2: offline, an unchecked key must not replace a
        # working one by default.
        self._update(verify_result=("unverified", "Timed out reaching Anthropic."))
        self.assertEqual(security.decrypt_credentials(self.PASSWORD)["anthropic_key"],
                         "sk-ant-api03-OLDKEYsecret")
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "warning")
        self.assertIn("Not saved yet", msg)
        self.assertIn("Save even if it can't be checked", msg)

    def test_unverified_with_save_anyway_saves_with_a_warning(self):
        self._update(verify_result=("unverified", "Timed out reaching Anthropic."), save_unverified=True)
        self.assertEqual(security.decrypt_credentials(self.PASSWORD)["anthropic_key"], self.NEW_KEY)
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "warning")
        self.assertIn("saved without a check", msg)
        self.assertIn("Timed out", msg)
        self.assertEqual(app_module.get_anthropic_key_status()["status"], "unverified")

    def test_text_that_is_not_a_key_is_refused_before_any_check(self):
        for junk in ("my password is hunter2", "sk-ant-short", "ghp_abcdefghijklmnopqrstuvwxyz0123"):
            verify = self._update(key=junk)
            verify.assert_not_called()
            self.assertEqual(security.decrypt_credentials(self.PASSWORD)["anthropic_key"],
                             "sk-ant-api03-OLDKEYsecret", junk)
            (cat, msg), = self.flashes()
            self.assertEqual(cat, "error")
            self.assertIn("doesn't look like an Anthropic API key", msg)
            with self.client.session_transaction() as sess:
                sess.pop("_flashes", None)

    def test_wrong_password_changes_nothing_and_skips_the_check(self):
        verify = self._update(password="nope")
        verify.assert_not_called()
        self.assertEqual(security.decrypt_credentials(self.PASSWORD)["anthropic_key"],
                         "sk-ant-api03-OLDKEYsecret")
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIn("Wrong password", msg)

    def test_empty_key_changes_nothing(self):
        verify = self._update(key="   ")
        verify.assert_not_called()
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIn("No Anthropic key entered", msg)

    def test_disk_error_on_save_keeps_old_key_and_status(self):
        app_module._anthropic_key_status = {"status": "ok", "message": "m",
                                            "checked_at": "", "source": "login"}
        with patch.object(security, "encrypt_credentials", side_effect=OSError("No space left")):
            self._update()
        self.assertEqual(security.decrypt_credentials(self.PASSWORD)["anthropic_key"],
                         "sk-ant-api03-OLDKEYsecret")
        self.assertEqual(app_module._credentials["anthropic_key"], "sk-ant-api03-OLDKEYsecret")
        self.assertEqual(app_module.get_anthropic_key_status()["message"], "m")
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIn("No space left", msg)
        self.assertNotIn("NEWKEYsecret", msg)

    def test_update_while_disconnected_saves_file_only(self):
        app_module._credentials = None
        self._update()
        self.assertEqual(security.decrypt_credentials(self.PASSWORD)["anthropic_key"], self.NEW_KEY)
        self.assertIsNone(app_module._credentials)

    def test_key_never_in_logs_flashes_or_page(self):
        with self.assertLogs(level="DEBUG") as cm:
            p, _ = _mock_sdk(anthropic.APIConnectionError(
                message=f"refused {self.NEW_KEY}", request=_REQ))
            with p:
                self.client.post("/settings", data={
                    "action": "update_anthropic_key", "anthropic_key": self.NEW_KEY,
                    "password": self.PASSWORD, "save_unverified": "1"})
        page = self.client.get("/settings").get_data(as_text=True)
        blob = "\n".join(cm.output) + json.dumps(self.flashes()) + page
        self.assertNotIn("NEWKEYsecret", blob)
        self.assertNotIn("OLDKEYsecret", blob)
        self.assertIn("not verified", page)

    def test_check_key_action_flashes_each_outcome(self):
        cases = [
            (("ok", "fine"), "success", "verified"),
            (("invalid", "Anthropic rejected the API key (HTTP 401)."), "error", "AI features"),
            (("unverified", "Timed out reaching Anthropic."), "warning", "Couldn't verify"),
        ]
        for result, cat, words in cases:
            with patch.object(ai, "verify_anthropic_key", return_value=result) as verify:
                self.client.post("/settings", data={"action": "check_anthropic_key"})
            verify.assert_called_once_with("sk-ant-api03-OLDKEYsecret")
            flashes = self.flashes()
            self.assertEqual(len(flashes), 1, result)
            self.assertEqual(flashes[0][0], cat)
            self.assertIn(words, flashes[0][1])
            self.assertEqual(app_module.get_anthropic_key_status()["status"], result[0])
            with self.client.session_transaction() as sess:
                sess.pop("_flashes", None)

    def test_check_key_when_disconnected(self):
        app_module._credentials = None
        with patch.object(ai, "verify_anthropic_key") as verify:
            self.client.post("/settings", data={"action": "check_anthropic_key"})
        verify.assert_not_called()
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIn("Not connected", msg)

    def test_settings_page_shows_each_status(self):
        cases = [
            ({"status": "", "message": "", "checked_at": "", "source": ""}, "Encrypted &amp; loaded"),
            ({"status": "ok", "message": "m", "checked_at": "2026-11-09T15:00:00+00:00",
              "source": "login"}, "Encrypted &amp; verified"),
            ({"status": "invalid", "message": "No Anthropic API key is set.", "checked_at": "",
              "source": "login"}, "Not working. No Anthropic API key is set."),
            ({"status": "unverified", "message": "Timed out reaching Anthropic.", "checked_at": "",
              "source": "login"}, "not verified (Timed out reaching Anthropic.)"),
        ]
        for status, text in cases:
            app_module._anthropic_key_status = status
            page = self.client.get("/settings").get_data(as_text=True)
            self.assertIn(text, page, status["status"])
            self.assertIn('value="update_anthropic_key"', page)
            self.assertIn('value="check_anthropic_key"', page)


if __name__ == "__main__":
    unittest.main()
