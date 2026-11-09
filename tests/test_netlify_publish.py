"""
netlify_publish.NetlifyClient: the Netlify REST client behind the encrypted
phone snapshot.

No test touches the network. HTTP goes to a FakeSession (which still builds
a real requests.PreparedRequest, so auth, headers and body encoding are what
requests would send) or to a real requests.Session whose transport adapter
only records. Sleep and the clock are injected, so polling and retry waits
run instantly. Every test ends with a sweep: the token must not appear in
any raised error (message, repr, traceback), any log_event field, or any
Python log line.
"""

import email.utils
import io
import json
import logging
import time
import traceback
import unittest
import urllib.parse
import zipfile
from unittest.mock import patch

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

import netlify_publish as nf

TOKEN = "nfp_Zq8Lw3Rt6Yu1Io4Pa7Sd0Fg2Hj5Kl9Xc3Vb"
LEGACY_TOKEN = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
# Matches none of the token *shapes* (no nfp_ prefix, no 40-char run, no
# "Bearer"), so only exact replacement of the client's own token can catch
# it. Has '/', '"' and '+' so URL-encoded and JSON-escaped echoes differ.
ODD_TOKEN = 'Tk/9x-Q2w"E4r+T6y.U8i~O0p-Zz3'
SITE_ID = "3970e0fe-8564-4903-9a55-c5f8de49fb8b"
DEPLOY_ID = "5f1c2d3e4a5b6c7d8e9f0a1b"
API = "https://api.netlify.com/api/v1"


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", "<!doctype html><title>Snapshot</title>")
        z.writestr("_headers", "/*\n  X-Robots-Tag: noindex\n")
        z.writestr("robots.txt", "User-agent: *\nDisallow: /\n")
    return buf.getvalue()


ZIP = _zip()

SITE = {
    "id": SITE_ID,
    "site_id": SITE_ID,
    "name": "repodoctor-snapshot-ab12cd",
    "url": "http://repodoctor-snapshot-ab12cd.netlify.app",
    "ssl_url": "https://repodoctor-snapshot-ab12cd.netlify.app",
    "admin_url": "https://app.netlify.com/sites/repodoctor-snapshot-ab12cd",
}
SITE_SUMMARY = {
    "site_id": SITE_ID,
    "name": "repodoctor-snapshot-ab12cd",
    "url": "https://repodoctor-snapshot-ab12cd.netlify.app",
    "admin_url": "https://app.netlify.com/sites/repodoctor-snapshot-ab12cd",
}


def deploy(state, **extra):
    body = {"id": DEPLOY_ID, "site_id": SITE_ID, "state": state}
    body.update(extra)
    return body


READY = deploy(
    "ready",
    deploy_ssl_url=f"https://{DEPLOY_ID}--repodoctor-snapshot-ab12cd.netlify.app",
    ssl_url="https://repodoctor-snapshot-ab12cd.netlify.app",
    url="http://repodoctor-snapshot-ab12cd.netlify.app",
)

_UNSET = object()


def resp(status=200, body=_UNSET, *, raw=None, headers=None):
    """A real requests.Response, so .json()/.text behave exactly as in prod."""
    r = requests.Response()
    r.status_code = status
    h = CaseInsensitiveDict(headers or {})
    if raw is not None:
        r._content = raw.encode() if isinstance(raw, str) else raw
    elif body is not _UNSET:
        r._content = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    else:
        r._content = b""
    r.headers = h
    r.encoding = "utf-8"
    r.url = API
    return r


class FakeSession:
    """Plays back queued responses/exceptions; records what would be sent."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def request(self, method, url, **kw):
        prepared = requests.Request(
            method=method, url=url, headers=kw.get("headers"), json=kw.get("json"),
            data=kw.get("data"), auth=kw.get("auth")).prepare()
        self.calls.append({
            "method": method, "url": url, "headers": prepared.headers,
            "body": prepared.body, "timeout": kw.get("timeout"), "json": kw.get("json"),
        })
        if not self.items:
            raise AssertionError(f"unexpected request {method} {url}")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class _ListHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__(level=logging.DEBUG)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


class NetlifyTestBase(unittest.TestCase):
    token = TOKEN

    def setUp(self):
        self.events = []
        self.errors = []
        self.records = []
        self.clock = FakeClock()
        self.session = None
        handler = _ListHandler(self.records)
        lg = logging.getLogger("netlify_publish")
        previous = lg.level
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
        self.addCleanup(lg.removeHandler, handler)
        self.addCleanup(lg.setLevel, previous)

    def tearDown(self):
        """The token sweep, run after every test in this file."""
        plain = {self.token, self.token.strip(), nf._clean_token(self.token)}
        secrets = set()
        for s in plain:
            secrets |= {s, urllib.parse.quote(s, safe=""), json.dumps(s)[1:-1]}
        secrets = {s for s in secrets if len(s) >= 16}
        blobs = []
        for e in self.errors:
            blobs += [str(e), e.message, repr(e), repr(e.args), str(e.deploy_id),
                      "".join(traceback.format_exception(e))]
        for event, fields in self.events:
            blobs += [repr((event, fields))] + [str(v) for v in fields.values()]
        blobs += [r.getMessage() for r in self.records]
        for blob in blobs:
            for s in secrets:
                self.assertNotIn(s, blob)

    def client(self, *items, token=None, log_event=_UNSET):
        self.session = FakeSession(*items)
        return nf.NetlifyClient(
            self.token if token is None else token, session=self.session,
            log_event=self._record if log_event is _UNSET else log_event,
            sleep=self.clock.sleep, clock=self.clock.clock)

    def _record(self, event, **fields):
        self.events.append((event, fields))

    def raises(self, fn, *a, **kw) -> nf.NetlifyError:
        with self.assertRaises(nf.NetlifyError) as cm:
            fn(*a, **kw)
        self.errors.append(cm.exception)
        return cm.exception

    def events_named(self, name):
        return [f for e, f in self.events if e == name]


# --- wire level: a real requests.Session, only the transport is faked ----------

class RecordingAdapter(BaseAdapter):
    def __init__(self, routes):
        super().__init__()
        self.routes = routes  # {(method, url): [responses...]}
        self.sent = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        self.sent.append((request, timeout))
        r = self.routes[(request.method, request.url)].pop(0)
        r.request = request
        r.url = request.url
        return r

    def close(self):
        pass


class WireTests(NetlifyTestBase):
    def real_session_client(self, routes):
        session = requests.Session()
        adapter = RecordingAdapter(routes)
        session.mount("https://api.netlify.com/", adapter)
        client = nf.NetlifyClient(TOKEN, session=session, log_event=self._record,
                                  sleep=self.clock.sleep, clock=self.clock.clock)
        return client, adapter

    def test_verify_sends_bearer_user_agent_and_short_timeout(self):
        client, adapter = self.real_session_client({
            ("GET", f"{API}/user"): [resp(200, {"id": "u1", "email": "x@example.com"})],
            ("GET", f"{API}/accounts"): [resp(200, [{"slug": "my-team"}])],
        })
        self.assertEqual(client.verify(), {"ok": True, "account_slug": "my-team"})
        request, timeout = adapter.sent[0]
        self.assertEqual(request.url, f"{API}/user")
        self.assertEqual(request.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(request.headers["User-Agent"], "RepoDoctor-snapshot")
        self.assertEqual(timeout, (10, 20))

    def test_deploy_sends_exact_zip_bytes_as_application_zip(self):
        client, adapter = self.real_session_client({
            ("POST", f"{API}/sites/{SITE_ID}/deploys"): [resp(200, deploy("uploaded"))],
            ("GET", f"{API}/deploys/{DEPLOY_ID}"): [resp(200, READY)],
        })
        out = client.deploy_zip(SITE_ID, ZIP)
        self.assertEqual(out["state"], "ready")
        upload, upload_timeout = adapter.sent[0]
        self.assertEqual(upload.method, "POST")
        self.assertEqual(upload.headers["Content-Type"], "application/zip")
        self.assertEqual(upload.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(upload.headers["User-Agent"], "RepoDoctor-snapshot")
        self.assertEqual(upload.headers["Content-Length"], str(len(ZIP)))
        self.assertIsInstance(upload.body, bytes)
        self.assertEqual(upload.body, ZIP)
        self.assertEqual(upload_timeout, (10, 60))
        poll, poll_timeout = adapter.sent[1]
        self.assertEqual((poll.method, poll.url), ("GET", f"{API}/deploys/{DEPLOY_ID}"))
        self.assertEqual(poll_timeout, (10, 20))

    def test_default_session_is_a_real_requests_session(self):
        calls = []

        def fake_request(session_self, method, url, **kw):
            prepared = requests.Request(method=method, url=url, headers=kw["headers"],
                                        auth=kw["auth"]).prepare()
            calls.append((method, url, prepared.headers))
            return resp(204)

        with patch.object(requests.Session, "request", autospec=True,
                          side_effect=fake_request):
            client = nf.NetlifyClient(TOKEN, log_event=self._record)
            self.assertIsInstance(client._session, requests.Session)
            self.assertEqual(client.delete_site(SITE_ID), "deleted")
            client.close()
        method, url, headers = calls[0]
        self.assertEqual((method, url), ("DELETE", f"{API}/sites/{SITE_ID}"))
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(headers["User-Agent"], "RepoDoctor-snapshot")

    def test_netrc_cannot_replace_the_token(self):
        # Auth is passed as an auth object, so requests never consults
        # ~/.netrc (which would overwrite a plain Authorization header).
        client, adapter = self.real_session_client({
            ("DELETE", f"{API}/sites/{SITE_ID}"): [resp(204)],
        })
        with patch("requests.sessions.get_netrc_auth",
                   return_value=("someone", "netrc-password")) as netrc:
            client.delete_site(SITE_ID)
        netrc.assert_not_called()
        self.assertEqual(adapter.sent[0][0].headers["Authorization"], f"Bearer {TOKEN}")


# --- verify ---------------------------------------------------------------------

class VerifyTests(NetlifyTestBase):
    USER = {"id": "u1", "full_name": "[Staff Name]", "email": "staff@example.com",
            "site_count": 3}

    def test_ok_returns_only_ok_and_slug(self):
        c = self.client(resp(200, self.USER),
                        resp(200, [{"slug": "my-team", "billing_email": "b@example.com"}]))
        self.assertEqual(c.verify(), {"ok": True, "account_slug": "my-team"})
        self.assertEqual([x["url"] for x in self.session.calls],
                         [f"{API}/user", f"{API}/accounts"])
        self.assertEqual(self.events_named("verify")[0]["ok"], True)
        for ev in self.events:
            self.assertNotIn("example.com", repr(ev))
            self.assertNotIn("my-team", repr(ev))
        for rec in self.records:
            self.assertNotIn("example.com", rec.getMessage())

    def test_slug_on_user_skips_accounts_call(self):
        c = self.client(resp(200, {"id": "u1", "slug": "team-a"}))
        self.assertEqual(c.verify(), {"ok": True, "account_slug": "team-a"})
        self.assertEqual(len(self.session.calls), 1)

    def test_accounts_failure_still_verifies(self):
        for accounts in (resp(500, {"message": "boom"}), resp(200, {"not": "a list"}),
                         resp(200, raw="<html>"), resp(200, [None, 3, {"slug": ""}]),
                         requests.ConnectionError("down")):
            with self.subTest(accounts=accounts):
                c = self.client(resp(200, self.USER), accounts)
                self.assertEqual(c.verify(), {"ok": True, "account_slug": None})

    def test_401_says_make_a_new_token(self):
        c = self.client(resp(401, {"code": 401, "message": "Access Denied"}))
        e = self.raises(c.verify)
        self.assertEqual(e.status, 401)
        self.assertEqual(e.kind, "auth")
        self.assertEqual(
            e.message,
            "Netlify didn't accept that token. Create a new personal access token "
            "(Netlify → User settings → Applications → Personal access tokens) and "
            "paste it again.")
        self.assertEqual(len(self.session.calls), 1)  # 401 is never retried
        self.assertEqual(self.events_named("http_error")[0]["status"], 401)
        self.assertEqual(self.events_named("verify")[0]["ok"], False)

    def test_unreadable_user_reply(self):
        for bad in (resp(200, raw="<html>oops</html>"), resp(200, ["list"]),
                    resp(200, "string"), resp(200, None), resp(200, raw=b"")):
            with self.subTest(bad=bad.content):
                c = self.client(bad)
                e = self.raises(c.verify)
                self.assertEqual(e.kind, "bad_reply")
                self.assertIn("couldn't read", e.message)

    def test_missing_or_partial_token_is_never_sent(self):
        for token, expect in (("", "Paste your Netlify personal access token first"),
                              ("   \n", "Paste your Netlify personal access token first"),
                              ("nfp_short", "doesn't look like a complete"),
                              ("nfp_abc def ghi jkl mno pqr", "doesn't look like a complete"),
                              ("nfp_" + "é" * 30, "doesn't look like a complete")):
            with self.subTest(token=token):
                c = self.client(token=token)
                e = self.raises(c.verify)
                self.assertEqual(e.kind, "bad_token")
                self.assertIsNone(e.status)
                self.assertIn(expect, e.message)
                self.assertEqual(self.session.calls, [])
        c = nf.NetlifyClient(None, session=FakeSession())
        self.assertEqual(self.raises(c.verify).kind, "bad_token")

    def test_pasted_whitespace_and_bearer_prefix_are_trimmed(self):
        c = self.client(resp(200, {"id": "u1", "slug": "t"}),
                        token=f"  Bearer {TOKEN}\n")
        c.verify()
        self.assertEqual(self.session.calls[0]["headers"]["Authorization"],
                         f"Bearer {TOKEN}")


# --- create_site ------------------------------------------------------------------

class CreateSiteTests(NetlifyTestBase):
    # Session 25 review: no "repodoctor-snapshot" label in DNS; 24 random chars.
    NAME_RE = r"^[a-z][a-z0-9]{23}$"

    def test_success(self):
        c = self.client(resp(201, SITE))
        self.assertEqual(c.create_site(), SITE_SUMMARY)
        call = self.session.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", f"{API}/sites"))
        self.assertEqual(list(call["json"]), ["name"])
        self.assertRegex(call["json"]["name"], self.NAME_RE)
        self.assertEqual(json.loads(call["body"]), call["json"])
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["timeout"], (10, 20))
        ok = self.events_named("create_site")[-1]
        self.assertEqual((ok["outcome"], ok["site_id"], ok["attempt"]), ("ok", SITE_ID, 1))

    def test_name_taken_retries_with_a_new_suffix(self):
        taken = resp(422, {"code": 422, "message": "Validation Failed",
                           "errors": {"subdomain": ["must be unique"]}})
        c = self.client(taken, resp(201, SITE))
        self.assertEqual(c.create_site()["site_id"], SITE_ID)
        names = [x["json"]["name"] for x in self.session.calls]
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])
        for n in names:
            self.assertRegex(n, self.NAME_RE)
        outcomes = [(f["outcome"], f["attempt"]) for f in self.events_named("create_site")]
        self.assertEqual(outcomes, [("name_taken", 1), ("ok", 2)])
        self.assertIn("subdomain must be unique", self.events_named("create_site")[0]["detail"])
        self.assertEqual(self.clock.sleeps, [])  # a 422 isn't a rate-limit wait

    def test_gives_up_after_three_retries(self):
        taken = lambda: resp(422, {"errors": {"subdomain": ["must be unique"]}})  # noqa: E731
        c = self.client(taken(), taken(), taken(), taken(), resp(201, SITE))
        with patch("netlify_publish._random_name",
                   side_effect=["a" * 24, "b" * 24, "c" * 24, "d" * 24, "e" * 24]):
            e = self.raises(c.create_site)
        self.assertEqual((e.status, e.kind), (422, "name_taken"))
        self.assertIn("subdomain must be unique", e.message)
        self.assertEqual([x["json"]["name"] for x in self.session.calls],
                         [ch * 24 for ch in "abcd"])
        self.assertEqual(self.events_named("create_site")[-1]["outcome"], "failed")

    def test_prefix_is_made_site_name_safe(self):
        cases = {"My Snapshot!!": "my-snapshot", "--a--b--": "a-b", "x" * 100: "x" * 56}
        for prefix, expect in cases.items():
            with self.subTest(prefix=prefix):
                c = self.client(resp(201, SITE))
                c.create_site(prefix)
                name = self.session.calls[0]["json"]["name"]
                self.assertRegex(name, rf"^{expect}-[a-z0-9]{{6}}$")
                self.assertLessEqual(len(name), 63)
        for blank in ("", None, "!!!"):  # no usable prefix: a fully random, unlabeled name
            with self.subTest(prefix=blank):
                c = self.client(resp(201, SITE))
                c.create_site(blank)
                self.assertRegex(self.session.calls[0]["json"]["name"], self.NAME_RE)

    def test_url_falls_back_and_upgrades_netlify_http(self):
        c = self.client(resp(201, {"id": SITE_ID, "name": "n",
                                   "url": "http://n.netlify.app"}))
        site = c.create_site()
        self.assertEqual(site["url"], "https://n.netlify.app")
        self.assertIsNone(site["admin_url"])
        self.assertEqual(site["site_id"], SITE_ID)

    def test_reply_without_site_id_or_not_an_object(self):
        for bad in (resp(201, {"name": "n"}), resp(201, [SITE]), resp(201, raw="ok"),
                    resp(201, {"id": 12345, "site_id": None})):
            with self.subTest(bad=bad.content):
                c = self.client(bad)
                self.assertEqual(self.raises(c.create_site).kind, "bad_reply")

    def test_auth_failure_is_not_retried(self):
        c = self.client(resp(401, {"message": "Access Denied"}))
        self.assertEqual(self.raises(c.create_site).kind, "auth")
        self.assertEqual(len(self.session.calls), 1)


# --- get_site -------------------------------------------------------------------

class GetSiteTests(NetlifyTestBase):
    def test_ok(self):
        c = self.client(resp(200, SITE))
        self.assertEqual(c.get_site(SITE_ID), SITE_SUMMARY)
        self.assertEqual(self.session.calls[0]["url"], f"{API}/sites/{SITE_ID}")
        self.assertEqual(self.session.calls[0]["method"], "GET")

    def test_404_means_run_setup_again(self):
        c = self.client(resp(404, {"code": 404, "message": "Not Found"}))
        e = self.raises(c.get_site, SITE_ID)
        self.assertEqual((e.status, e.kind), (404, "site_gone"))
        self.assertIn("no longer exists on Netlify", e.message)
        self.assertIn("setup again", e.message)

    def test_403_means_token_lacks_access(self):
        c = self.client(resp(403, {"message": "Forbidden"}))
        e = self.raises(c.get_site, SITE_ID)
        self.assertEqual((e.status, e.kind), (403, "forbidden"))
        self.assertIn("doesn't have access to that site or team", e.message)

    def test_bad_site_ids_never_reach_the_url(self):
        for sid in ("", "   ", None, "../user", "a/b", "a b", "x..y", 42, "?q=1"):
            with self.subTest(sid=sid):
                c = self.client()
                e = self.raises(c.get_site, sid)
                self.assertEqual(e.kind, "bad_input")
                self.assertEqual(self.session.calls, [])

    def test_odd_field_types_do_not_crash(self):
        c = self.client(resp(200, {"id": "abc", "name": ["x"], "ssl_url": 5,
                                   "url": None, "admin_url": {"x": 1}}))
        self.assertEqual(c.get_site("abc"),
                         {"site_id": "abc", "name": None, "url": None, "admin_url": None})


# --- deploy_zip -----------------------------------------------------------------

class DeployTests(NetlifyTestBase):
    def test_happy_path_through_several_states(self):
        c = self.client(resp(200, deploy("uploaded")), resp(200, deploy("processing")),
                        resp(200, deploy("prepared")), resp(200, deploy("enqueued")),
                        resp(200, READY))
        out = c.deploy_zip(SITE_ID, ZIP)
        self.assertEqual(out, {
            "deploy_id": DEPLOY_ID, "state": "ready",
            "url": f"https://{DEPLOY_ID}--repodoctor-snapshot-ab12cd.netlify.app",
            "site_url": "https://repodoctor-snapshot-ab12cd.netlify.app",
            "elapsed_s": 6.0,
        })
        self.assertEqual(self.clock.sleeps, [1.5, 1.5, 1.5, 1.5])
        urls = [x["url"] for x in self.session.calls]
        self.assertEqual(urls[0], f"{API}/sites/{SITE_ID}/deploys")
        self.assertEqual(urls[1:], [f"{API}/deploys/{DEPLOY_ID}"] * 4)
        self.assertEqual(self.session.calls[0]["body"], ZIP)
        self.assertEqual(self.session.calls[0]["headers"]["Content-Type"], "application/zip")
        names = [e for e, _ in self.events]
        self.assertEqual(names[0], "deploy_started")
        self.assertEqual(names[-1], "deploy_ready")
        started = self.events_named("deploy_started")[0]
        self.assertEqual((started["bytes"], started["deploy_id"], started["site_id"]),
                         (len(ZIP), DEPLOY_ID, SITE_ID))
        self.assertEqual([f["state"] for f in self.events_named("deploy_state")],
                         ["processing", "prepared", "enqueued", "ready"])
        self.assertEqual(self.events_named("deploy_ready")[0]["polls"], 4)

    def test_already_ready_needs_no_polling(self):
        c = self.client(resp(200, READY))
        self.assertEqual(c.deploy_zip(SITE_ID, bytearray(ZIP))["state"], "ready")
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.session.calls[0]["body"], ZIP)
        self.assertEqual(self.clock.sleeps, [])

    def test_url_falls_back_to_site_url(self):
        c = self.client(resp(200, deploy("uploaded")),
                        resp(200, deploy("ready", ssl_url="https://s.netlify.app")))
        out = c.deploy_zip(SITE_ID, ZIP)
        self.assertEqual(out["url"], "https://s.netlify.app")
        self.assertEqual(out["site_url"], "https://s.netlify.app")

    def test_ready_without_any_url_still_returns(self):
        c = self.client(resp(200, deploy("ready", deploy_ssl_url=None, ssl_url=7)))
        out = c.deploy_zip(SITE_ID, ZIP)
        self.assertIsNone(out["url"])
        self.assertIsNone(out["site_url"])

    def test_error_state_raises_with_netlify_message(self):
        c = self.client(resp(200, deploy("uploaded")), resp(200, deploy("processing")),
                        resp(200, deploy("error", error_message="Failed to process _headers file.")))
        e = self.raises(c.deploy_zip, SITE_ID, ZIP)
        self.assertEqual(e.kind, "deploy_failed")
        self.assertIsNone(e.status)
        self.assertEqual(e.deploy_id, DEPLOY_ID)
        self.assertIn("Failed to process _headers file", e.message)
        self.assertIn(DEPLOY_ID, e.message)
        err = self.events_named("deploy_error")[0]
        self.assertEqual(err["reason"], "state_error")
        self.assertIn("_headers", err["error"])

    def test_error_state_without_message(self):
        for body in (deploy("error"), deploy("error", error_message=None),
                     deploy("error", error_message=""), deploy("rejected")):
            with self.subTest(body=body):
                c = self.client(resp(200, body))
                e = self.raises(c.deploy_zip, SITE_ID, ZIP)
                self.assertEqual(e.kind, "deploy_failed")
                self.assertIn("didn't say why", e.message)

    def test_times_out_with_deploy_id(self):
        c = self.client(resp(200, deploy("uploaded")),
                        *[resp(200, deploy("processing")) for _ in range(200)])
        e = self.raises(c.deploy_zip, SITE_ID, ZIP)
        self.assertEqual(e.kind, "deploy_timeout")
        self.assertIsNone(e.status)
        self.assertEqual(e.deploy_id, DEPLOY_ID)
        self.assertIn(DEPLOY_ID, e.message)
        self.assertIn("check again in a minute", e.message)
        self.assertAlmostEqual(sum(self.clock.sleeps), 90.0, places=6)
        self.assertEqual(len(self.session.calls) - 1, 60)  # 90s / 1.5s polls
        self.assertEqual(self.events_named("deploy_error")[0]["reason"], "timeout")

    def test_custom_wait_and_frozen_clock_cannot_spin_forever(self):
        session = FakeSession(resp(200, deploy("uploaded")),
                              *[resp(200, deploy("building")) for _ in range(50)])
        c = nf.NetlifyClient(TOKEN, session=session, log_event=self._record,
                             sleep=lambda s: None, clock=lambda: 5.0)
        e = self.raises(c.deploy_zip, SITE_ID, ZIP, max_wait_s=3, poll_interval_s=1)
        self.assertEqual(e.kind, "deploy_timeout")
        self.assertEqual(len(session.calls) - 1, 8)  # int(3/1) + 5 polls, then stop

    def test_nonsense_wait_arguments_fall_back_to_defaults(self):
        for wait, interval in ((float("nan"), 0), (-1, -1), (None, float("inf")), ("x", None)):
            with self.subTest(wait=wait, interval=interval):
                self.clock.sleeps.clear()
                c = self.client(resp(200, deploy("uploaded")),
                                *[resp(200, deploy("processing")) for _ in range(70)])
                e = self.raises(c.deploy_zip, SITE_ID, ZIP, max_wait_s=wait,
                                poll_interval_s=interval)
                self.assertEqual(e.kind, "deploy_timeout")
                self.assertEqual(len(self.clock.sleeps), 60)
                self.assertEqual(set(self.clock.sleeps), {1.5})

    def test_unknown_or_missing_state_keeps_polling(self):
        c = self.client(resp(200, deploy("uploaded")), resp(200, deploy("mystery")),
                        resp(200, {"id": DEPLOY_ID}), resp(200, deploy(None)),
                        resp(200, deploy(["x"])), resp(200, READY))
        self.assertEqual(c.deploy_zip(SITE_ID, ZIP)["state"], "ready")
        mystery = self.events_named("deploy_state")[0]
        self.assertEqual((mystery["state"], mystery["known"]), ("mystery", False))

    def test_site_deleted_on_netlify(self):
        c = self.client(resp(404, {"message": "Not Found"}))
        e = self.raises(c.deploy_zip, SITE_ID, ZIP)
        self.assertEqual((e.status, e.kind), (404, "site_gone"))
        self.assertIn("setup again", e.message)

    def test_422_and_413_upload_rejections(self):
        c = self.client(resp(422, {"message": "Invalid zip file"}))
        e = self.raises(c.deploy_zip, SITE_ID, ZIP)
        self.assertEqual((e.status, e.kind), (422, "rejected"))
        self.assertIn("Invalid zip file", e.message)
        c = self.client(resp(413, raw="<html>Request Entity Too Large</html>"))
        e = self.raises(c.deploy_zip, SITE_ID, ZIP)
        self.assertEqual((e.status, e.kind), (413, "rejected"))
        self.assertIn("too large", e.message)
        self.assertNotIn("<html>", e.message)

    def test_bad_zip_input_is_never_uploaded(self):
        for bad in (b"", None, "PK not bytes", b"not a zip file at all, really",
                    b"PK\x03\x04", 12345):
            with self.subTest(bad=bad):
                c = self.client()
                e = self.raises(c.deploy_zip, SITE_ID, bad)
                self.assertEqual(e.kind, "bad_input")
                self.assertEqual(self.session.calls, [])

    def test_upload_reply_without_deploy_id(self):
        for bad in (resp(200, {"state": "uploaded"}), resp(200, {"id": 5}),
                    resp(200, {"id": "../x"}), resp(200, raw="not json"), resp(200, [1])):
            with self.subTest(bad=bad.content):
                c = self.client(bad)
                self.assertEqual(self.raises(c.deploy_zip, SITE_ID, ZIP).kind, "bad_reply")

    def test_unreadable_poll_reply_keeps_deploy_id(self):
        c = self.client(resp(200, deploy("uploaded")), resp(200, raw="<html>"))
        e = self.raises(c.deploy_zip, SITE_ID, ZIP)
        self.assertEqual((e.kind, e.deploy_id), ("bad_reply", DEPLOY_ID))
        self.assertEqual(self.events_named("deploy_error")[0]["reason"], "bad_reply")

    def test_network_drop_while_polling_keeps_deploy_id(self):
        c = self.client(resp(200, deploy("uploaded")), requests.Timeout("read timed out"))
        e = self.raises(c.deploy_zip, SITE_ID, ZIP)
        self.assertEqual((e.kind, e.deploy_id), ("network", DEPLOY_ID))

    def test_poll_503_is_retried_once(self):
        c = self.client(resp(200, deploy("uploaded")),
                        resp(503, headers={"Retry-After": "1"}), resp(200, READY))
        self.assertEqual(c.deploy_zip(SITE_ID, ZIP)["state"], "ready")
        self.assertEqual(self.clock.sleeps, [1.5, 1.0])

    def test_get_deploy(self):
        c = self.client(resp(200, deploy("error", error_message=f"bad token {TOKEN}")))
        out = c.get_deploy(DEPLOY_ID)
        self.assertEqual(out["state"], "error")
        self.assertEqual(out["deploy_id"], DEPLOY_ID)
        self.assertNotIn(TOKEN, out["error_message"])
        self.assertIn("[redacted]", out["error_message"])
        self.assertEqual(self.raises(c.get_deploy, "../x").kind, "bad_input")


# --- delete_site ----------------------------------------------------------------

class DeleteTests(NetlifyTestBase):
    def test_204_deleted(self):
        c = self.client(resp(204))
        self.assertEqual(c.delete_site(SITE_ID), "deleted")
        call = self.session.calls[0]
        self.assertEqual((call["method"], call["url"]), ("DELETE", f"{API}/sites/{SITE_ID}"))
        self.assertEqual(self.events_named("delete_site")[0]["outcome"], "deleted")

    def test_404_counts_as_already_gone(self):
        c = self.client(resp(404, {"message": "Not Found"}))
        self.assertEqual(c.delete_site(SITE_ID), "already_gone")
        self.assertEqual(self.events_named("delete_site")[0]["outcome"], "already_gone")
        self.assertEqual(self.events_named("http_error"), [])

    def test_403_and_401_raise(self):
        c = self.client(resp(403, {"message": "Forbidden"}))
        self.assertEqual(self.raises(c.delete_site, SITE_ID).kind, "forbidden")
        c = self.client(resp(401, {"message": "Access Denied"}))
        self.assertEqual(self.raises(c.delete_site, SITE_ID).kind, "auth")

    def test_no_site_id(self):
        c = self.client()
        e = self.raises(c.delete_site, "")
        self.assertIn("run snapshot setup first", e.message.lower())
        self.assertEqual(self.session.calls, [])


# --- retries, status mapping, transport errors ----------------------------------

class RetryAndStatusTests(NetlifyTestBase):
    def test_429_waits_retry_after_then_retries_once(self):
        c = self.client(resp(429, {"message": "slow down"}, headers={"Retry-After": "3"}),
                        resp(200, SITE))
        self.assertEqual(c.get_site(SITE_ID)["site_id"], SITE_ID)
        self.assertEqual(self.clock.sleeps, [3.0])
        self.assertEqual(len(self.session.calls), 2)
        retry = self.events_named("http_retry")[0]
        self.assertEqual((retry["status"], retry["wait_s"]), (429, 3.0))

    def test_retry_after_is_capped_at_10s(self):
        c = self.client(resp(503, headers={"Retry-After": "120"}), resp(200, SITE))
        c.get_site(SITE_ID)
        self.assertEqual(self.clock.sleeps, [10.0])

    def test_retry_after_forms(self):
        future = email.utils.formatdate(time.time() + 5, usegmt=True)
        cases = [({}, 2.0), ({"Retry-After": "soon"}, 2.0), ({"Retry-After": "-5"}, 0.0),
                 ({"Retry-After": "1.5"}, 1.5), ({"Retry-After": "nan"}, 2.0),
                 ({"Retry-After": "inf"}, 2.0),
                 ({"X-RateLimit-Reset": str(int(time.time()) + 4)}, None),
                 ({"Retry-After": future}, None)]
        for headers, expect in cases:
            with self.subTest(headers=headers):
                self.clock.sleeps.clear()
                c = self.client(resp(502, headers=headers), resp(200, SITE))
                c.get_site(SITE_ID)
                (wait,) = self.clock.sleeps
                if expect is None:
                    self.assertGreaterEqual(wait, 2.0)
                    self.assertLessEqual(wait, 5.0)
                else:
                    self.assertEqual(wait, expect)

    def test_each_retryable_status_retries_once(self):
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                c = self.client(resp(status), resp(204))
                c.delete_site(SITE_ID)
                self.assertEqual(len(self.session.calls), 2)

    def test_post_is_not_retried_after_a_gateway_error(self):
        # A 502/504 on POST /sites may have created the site behind the
        # gateway; retrying would leave a stray empty site on the account.
        for status in (502, 504):
            with self.subTest(status=status):
                c = self.client(resp(status), resp(201, SITE))
                e = self.raises(c.create_site)
                self.assertEqual(e.status, status)
                self.assertEqual(len(self.session.calls), 1)
        for status in (429, 503):  # Netlify definitely didn't act: retry once
            with self.subTest(status=status):
                c = self.client(resp(status), resp(201, SITE))
                self.assertEqual(c.create_site(), SITE_SUMMARY)
                self.assertEqual(len(self.session.calls), 2)

    def test_second_failure_is_final(self):
        for status, kind in ((503, "server"), (429, "rate_limited"), (504, "server")):
            with self.subTest(status=status):
                self.clock.sleeps.clear()
                c = self.client(resp(status), resp(status), resp(200, SITE))
                e = self.raises(c.get_site, SITE_ID)
                self.assertEqual((e.status, e.kind), (status, kind))
                self.assertEqual(len(self.session.calls), 2)
                self.assertEqual(len(self.clock.sleeps), 1)
                expect = "Wait a minute" if kind == "rate_limited" else f"HTTP {status}"
                self.assertIn(expect, e.message)

    def test_500_is_not_retried(self):
        c = self.client(resp(500, raw="<html>Internal Server Error</html>"), resp(200, SITE))
        e = self.raises(c.get_site, SITE_ID)
        self.assertEqual((e.status, e.kind), (500, "server"))
        self.assertIn("HTTP 500", e.message)
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_other_4xx_is_a_plain_rejection(self):
        c = self.client(resp(400, {"message": "Bad Request", "errors": ["name is invalid"]}))
        e = self.raises(c.get_site, SITE_ID)
        self.assertEqual((e.status, e.kind), (400, "rejected"))
        self.assertIn("HTTP 400", e.message)
        self.assertIn("name is invalid", e.message)

    def test_timeout_and_connection_errors(self):
        msg = "Couldn't reach Netlify — check your internet connection and try again."
        for exc in (requests.Timeout("read timed out"), requests.ConnectTimeout("connect"),
                    requests.ConnectionError("Name or service not known"),
                    requests.exceptions.SSLError("cert")):
            with self.subTest(exc=type(exc).__name__):
                c = self.client(exc, resp(200, SITE))
                e = self.raises(c.get_site, SITE_ID)
                self.assertEqual((e.status, e.kind, e.message), (None, "network", msg))
                self.assertEqual(len(self.session.calls), 1)  # never retried
                self.assertIsNone(e.__cause__)
        c = self.client(requests.TooManyRedirects("loop"))
        e = self.raises(c.verify)
        self.assertEqual(e.kind, "network")
        self.assertIn("TooManyRedirects", e.message)

    def test_strange_error_bodies_do_not_crash(self):
        bodies = [{"errors": "plain string"}, {"errors": {"a": "not a list", "b": [1, None]}},
                  {"message": 5, "errors": None}, [1, 2], "just a string", 7, None,
                  {"error": "x" * 5000}]
        for body in bodies:
            with self.subTest(body=str(body)[:40]):
                c = self.client(resp(409, body))
                e = self.raises(c.get_site, SITE_ID)
                self.assertEqual(e.kind, "rejected")
                self.assertLess(len(e.message), 450)
        c = self.client(resp(409, raw=b"\xff\xfe\x00garbage"))
        self.assertEqual(self.raises(c.get_site, SITE_ID).kind, "rejected")

    def test_log_event_callback_failure_never_breaks_a_call(self):
        def broken(event, **fields):
            raise RuntimeError("log sink down")
        c = self.client(resp(200, {"id": "u1", "slug": "t"}), log_event=broken)
        self.assertEqual(c.verify()["ok"], True)

    def test_no_callback_is_fine(self):
        c = self.client(resp(204), log_event=None)
        self.assertEqual(c.delete_site(SITE_ID), "deleted")


# --- the token never leaks ----------------------------------------------------------

ECHO_JSON = {"code": 0, "message": f"Access denied for token {TOKEN}",
             "errors": {"authorization": [f"Bearer {TOKEN} is not valid"]}}
ECHO_TEXT = f"denied: Authorization: Bearer {TOKEN} ?access_token={TOKEN}&x=1"


class TokenNeverLeaksTests(NetlifyTestBase):
    def test_echoed_token_in_every_error_status(self):
        for status in (400, 401, 403, 404, 409, 413, 422, 429, 500, 502, 503):
            for body in ({"json": ECHO_JSON}, {"raw": ECHO_TEXT}):
                with self.subTest(status=status, body=list(body)[0]):
                    r1 = resp(status, body.get("json", _UNSET), raw=body.get("raw"))
                    r2 = resp(status, body.get("json", _UNSET), raw=body.get("raw"))
                    c = self.client(r1, r2)
                    e = self.raises(c.get_site, SITE_ID)
                    self.assertNotIn(TOKEN, e.message)
                    for _, fields in self.events:
                        self.assertNotIn(TOKEN, repr(fields))

    def test_echo_is_redacted_not_dropped(self):
        c = self.client(resp(400, ECHO_JSON))
        self.raises(c.get_site, SITE_ID)
        detail = self.events_named("http_error")[0]["detail"]
        self.assertIn("Access denied for token [redacted]", detail)
        self.assertIn("Bearer [redacted]", detail)

    def test_echoed_token_in_create_site_and_deploy_errors(self):
        echo_422 = lambda: resp(422, {"message": f"name rejected for {TOKEN}"})  # noqa: E731
        c = self.client(echo_422(), echo_422(), echo_422(), echo_422())
        e = self.raises(c.create_site)
        self.assertIn("[redacted]", e.message)
        c = self.client(resp(200, deploy("uploaded")),
                        resp(200, deploy("error", error_message=f"Bearer {TOKEN} expired; token={TOKEN}")))
        e = self.raises(c.deploy_zip, SITE_ID, ZIP)
        self.assertIn("[redacted]", e.message)

    def test_token_inside_a_transport_exception(self):
        c = self.client(requests.ConnectionError(f"proxy said: Authorization: Bearer {TOKEN}"))
        self.raises(c.verify)
        logged = self.events_named("http_error")[0]["error"]
        self.assertIn("ConnectionError", logged)
        self.assertIn("[redacted]", logged)

    def test_someone_elses_bearer_token_is_redacted_too(self):
        other = "nfp_" + "Q" * 36
        c = self.client(resp(400, {"message": f"Authorization: Bearer abc.def.ghi and {other}"}))
        self.raises(c.get_site, SITE_ID)
        detail = self.events_named("http_error")[0]["detail"]
        self.assertNotIn("abc.def.ghi", detail)
        self.assertNotIn(other, detail)

    def test_legacy_hex_token_and_encoded_echoes(self):
        self.token = LEGACY_TOKEN
        c = self.client(resp(403, raw=f"bad {LEGACY_TOKEN} / \"{LEGACY_TOKEN}\""),
                        token=LEGACY_TOKEN)
        e = self.raises(c.get_site, SITE_ID)
        self.assertEqual(e.kind, "forbidden")
        detail = self.events_named("http_error")[0]["detail"]
        self.assertNotIn(LEGACY_TOKEN, detail)

    def test_reprs_hide_the_token(self):
        c = self.client()
        self.assertNotIn(TOKEN, repr(c))
        self.assertNotIn(TOKEN, repr(nf._BearerAuth(TOKEN)))

    def test_redact_helper(self):
        self.assertEqual(nf.redact(f"x {TOKEN} y", TOKEN), "x [redacted] y")
        self.assertEqual(nf.redact("Authorization: bearer abc123"),
                         "Authorization: Bearer [redacted]")
        self.assertEqual(nf.redact("u?token=abc&z=1"), "u?token=[redacted]&z=1")
        self.assertEqual(nf.redact(LEGACY_TOKEN), "[redacted]")
        self.assertEqual(nf.redact("nfp_abcdefghijkl"), "[redacted]")
        # Netlify ids and URLs are not mistaken for tokens.
        keep = (f"deploy {DEPLOY_ID} site {SITE_ID} "
                f"https://{DEPLOY_ID}--repodoctor-snapshot-ab12cd.netlify.app")
        self.assertEqual(nf.redact(keep), keep)
        self.assertEqual(nf.redact(None), "None")


class OddShapedTokenTests(NetlifyTestBase):
    """A token no regex recognizes: only exact replacement protects it."""
    token = ODD_TOKEN

    def echoes(self):
        quoted = urllib.parse.quote(ODD_TOKEN, safe="")
        escaped = json.dumps(ODD_TOKEN)[1:-1]
        return [
            {"json": {"message": f"token {ODD_TOKEN} rejected"}},
            {"json": {"errors": {"auth": [ODD_TOKEN]}}},
            {"raw": f"rejected {ODD_TOKEN} | {quoted} | {escaped}"},
        ]

    def test_sent_as_is(self):
        c = self.client(resp(200, {"id": "u1", "slug": "t"}))
        c.verify()
        self.assertEqual(self.session.calls[0]["headers"]["Authorization"],
                         f"Bearer {ODD_TOKEN}")

    def test_echo_redacted_in_every_status(self):
        for status in (400, 401, 403, 404, 409, 422, 429, 500, 503):
            for body in self.echoes():
                with self.subTest(status=status, body=body):
                    first, second = (resp(status, body.get("json", _UNSET), raw=body.get("raw"))
                                     for _ in range(2))
                    c = self.client(first, second)
                    e = self.raises(c.get_site, SITE_ID)
                    self.assertNotIn(ODD_TOKEN, e.message)
                    for detail in [f.get("detail") for f in self.events_named("http_error")]:
                        if detail:
                            self.assertNotIn(ODD_TOKEN, detail)
                            self.assertNotIn(urllib.parse.quote(ODD_TOKEN, safe=""), detail)

    def test_echo_redacted_in_deploy_error_and_create_site(self):
        c = self.client(resp(200, deploy("uploaded")),
                        resp(200, deploy("error", error_message=f"bad auth {ODD_TOKEN}")))
        e = self.raises(c.deploy_zip, SITE_ID, ZIP)
        self.assertIn("bad auth [redacted]", e.message)
        taken = lambda: resp(422, {"message": f"no for {ODD_TOKEN}"})  # noqa: E731
        c = self.client(taken(), taken(), taken(), taken())
        e = self.raises(c.create_site)
        self.assertIn("no for [redacted]", e.message)

    def test_token_in_transport_exception(self):
        c = self.client(requests.ConnectionError(f"failed sending {ODD_TOKEN}"))
        self.raises(c.verify)
        self.assertIn("[redacted]", self.events_named("http_error")[0]["error"])


if __name__ == "__main__":
    unittest.main()
