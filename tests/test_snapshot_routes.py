"""Integration tests for the phone snapshot: encrypted secrets, Settings
setup, publish, preview and takedown, and the data gathered for it.

The Netlify API is always faked; snapshot rendering / encryption is real."""

import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
import security

try:
    import netlify_publish as nf
    import snapshot as snap
    HAVE = True
except ImportError:  # pragma: no cover - before the modules are merged
    HAVE = False

PASSWORD = "app-password-123"
PASSPHRASE = "correct horse battery staple"
TOKEN = "nfp_SECRETtokenVALUE1234567890"


class FakeNetlify:
    """Records calls; behavior set per test through class attributes."""
    calls: list = []
    verify_error = None
    get_site_error = None
    deploy_error = None
    delete_error = None
    deployed: list = []

    def __init__(self, token, session=None, log_event=None):
        self.token = token
        FakeNetlify.calls.append(("init", token))

    def verify(self):
        FakeNetlify.calls.append(("verify",))
        if FakeNetlify.verify_error:
            raise FakeNetlify.verify_error
        return {"ok": True}

    def create_site(self, name_prefix="repodoctor-snapshot"):
        FakeNetlify.calls.append(("create_site",))
        return {"site_id": "site-123", "name": "repodoctor-snapshot-abc123",
                "url": "https://repodoctor-snapshot-abc123.netlify.app", "admin_url": "https://app.netlify.com/x"}

    def get_site(self, site_id):
        FakeNetlify.calls.append(("get_site", site_id))
        if FakeNetlify.get_site_error:
            raise FakeNetlify.get_site_error
        return {"site_id": site_id, "name": "existing", "url": "https://existing.netlify.app", "admin_url": ""}

    def deploy_zip(self, site_id, zip_bytes):
        FakeNetlify.calls.append(("deploy_zip", site_id, len(zip_bytes)))
        if FakeNetlify.deploy_error:
            raise FakeNetlify.deploy_error
        FakeNetlify.deployed.append(zip_bytes)
        return {"deploy_id": "dep-1", "state": "ready", "url": "https://dep--x.netlify.app", "elapsed_s": 1.0}

    def delete_site(self, site_id):
        FakeNetlify.calls.append(("delete_site", site_id))
        if FakeNetlify.delete_error:
            raise FakeNetlify.delete_error


class TestSecretsStorage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._p = security.SNAPSHOT_SECRETS_PATH
        security.SNAPSHOT_SECRETS_PATH = os.path.join(self.tmp, "snapshot.enc")

    def tearDown(self):
        security.SNAPSHOT_SECRETS_PATH = self._p

    def test_round_trip_wrong_password_and_corrupt(self):
        security.encrypt_snapshot_secrets(PASSWORD, {"netlify_token": TOKEN, "passphrase": PASSPHRASE})
        self.assertTrue(security.snapshot_secrets_exist())
        raw = open(security.SNAPSHOT_SECRETS_PATH, "rb").read()
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertNotIn(PASSPHRASE.encode(), raw)
        self.assertEqual(security.decrypt_snapshot_secrets(PASSWORD),
                         {"netlify_token": TOKEN, "passphrase": PASSPHRASE})
        self.assertIsNone(security.decrypt_snapshot_secrets("wrong"))
        with open(security.SNAPSHOT_SECRETS_PATH, "wb") as f:
            f.write(b"x" * 40)
        self.assertIsNone(security.decrypt_snapshot_secrets(PASSWORD))
        security.delete_snapshot_secrets()
        self.assertFalse(security.snapshot_secrets_exist())
        self.assertIsNone(security.decrypt_snapshot_secrets(PASSWORD))


@unittest.skipUnless(HAVE, "snapshot / netlify_publish modules not merged yet")
class TestSnapshotRoutes(unittest.TestCase):

    def setUp(self):
        import app as app_mod
        self.m = app_mod
        app_mod.app.config["TESTING"] = True
        self.client = app_mod.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["github_user"] = "tester"
        self.tmp = tempfile.mkdtemp()
        self._sec = (security.CREDENTIALS_PATH, security.SNAPSHOT_SECRETS_PATH)
        security.CREDENTIALS_PATH = os.path.join(self.tmp, "credentials.enc")
        security.SNAPSHOT_SECRETS_PATH = os.path.join(self.tmp, "snapshot.enc")
        security.encrypt_credentials(PASSWORD, "ghp_x", "sk-ant-api03-" + "k" * 30)
        names = ("SNAPSHOT_STATE_PATH", "SNAPSHOT_LOG_PATH", "INFLIGHT_PATH", "GROUPS_PATH",
                 "_LEGACY_GROUPS_PATH", "PREFS_PATH", "TRACKERS_DIR", "SUMMARIES_PATH",
                 "ACTIVITY_SUMMARIES_PATH", "ACTIVITY_LOG_PATH", "NEXTUP_LOG_PATH", "HEALTH_LOG_PATH")
        self._paths = {p: getattr(models, p) for p in names}
        for p in names:
            setattr(models, p, os.path.join(self.tmp, p.lower().strip("_")))
        self._orig = (app_mod._scan_results, app_mod._github_client, app_mod._activity_cache,
                      app_mod._activity_ranges, app_mod._snapshot_secrets)
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = (now - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        class FakeGH:
            def get_commits_range(self, owner, name, since_iso, ref=None, until_iso=None, max_pages=50):
                return [{"sha": f"{name}1234567", "parents": [{}], "html_url": "",
                         "commit": {"message": f"SECRETCOMMIT work on {name}", "author": {"name": "c", "date": ts},
                                    "committer": {"date": ts}}}], False

            def get_merged_pulls_since(self, *a, **k):
                return [], False, ""
        app_mod._github_client = FakeGH()
        app_mod._activity_cache = None
        app_mod._activity_ranges = {}
        app_mod._snapshot_secrets = None
        app_mod._scan_results = {"scanned_at": now.isoformat(), "repos": [
            {"owner": "o", "name": "SECRETREPO", "full_name": "o/SECRETREPO", "default_branch": "main",
             "html_url": "https://github.com/o/SECRETREPO", "pushed_at": ts, "branch_names": ["main"],
             "required_files": {}, "files_present": 0, "files_total": 5, "total_branch_count": 1,
             "non_default_branch_count": 0, "archived": False, "fork": False}]}
        FakeNetlify.calls = []
        FakeNetlify.deployed = []
        FakeNetlify.verify_error = FakeNetlify.get_site_error = None
        FakeNetlify.deploy_error = FakeNetlify.delete_error = None
        self.patcher = patch.object(nf, "NetlifyClient", FakeNetlify)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        (self.m._scan_results, self.m._github_client, self.m._activity_cache,
         self.m._activity_ranges, self.m._snapshot_secrets) = self._orig
        security.CREDENTIALS_PATH, security.SNAPSHOT_SECRETS_PATH = self._sec
        for p, v in self._paths.items():
            setattr(models, p, v)

    def flashes(self):
        with self.client.session_transaction() as sess:
            out = list(sess.get("_flashes", []))
            sess.pop("_flashes", None)
        return out

    def setup_ok(self, **over):
        data = {"netlify_token": TOKEN, "passphrase": PASSPHRASE, "passphrase_confirm": PASSPHRASE,
                "password": PASSWORD}
        data.update(over)
        return self.client.post("/snapshot/setup", data=data)

    # --- setup ------------------------------------------------------------

    def test_setup_validations_change_nothing(self):
        cases = [({"netlify_token": ""}, "Netlify personal access token"),
                 ({"passphrase": "short", "passphrase_confirm": "short"}, ""),
                 ({"passphrase_confirm": PASSPHRASE + "x"}, "don't match"),
                 ({"password": "nope"}, "Wrong app password")]
        for over, needle in cases:
            r = self.setup_ok(**over)
            self.assertEqual(r.status_code, 302)
            (cat, msg), = self.flashes()
            self.assertEqual(cat, "error", over)
            self.assertIn(needle, msg, over)
            self.assertFalse(security.snapshot_secrets_exist(), over)
            self.assertEqual(models.get_snapshot_state(), {}, over)

    def test_setup_netlify_error_saves_nothing(self):
        FakeNetlify.verify_error = nf.NetlifyError("Netlify didn't accept that token.", status=401)
        self.setup_ok()
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIn("didn't accept that token", msg)
        self.assertFalse(security.snapshot_secrets_exist())

    def test_setup_creates_site_saves_encrypted_and_publishes(self):
        self.setup_ok()
        flashes = self.flashes()
        self.assertTrue(any(c == "success" and "Snapshot published" in m for c, m in flashes), flashes)
        self.assertEqual(security.decrypt_snapshot_secrets(PASSWORD),
                         {"netlify_token": TOKEN, "passphrase": PASSPHRASE})
        st = models.get_snapshot_state()
        self.assertEqual(st["site_id"], "site-123")
        self.assertTrue(st["last_published_at"])
        self.assertIn(("create_site",), FakeNetlify.calls)
        # The published zip holds only ciphertext.
        z = zipfile.ZipFile(io.BytesIO(FakeNetlify.deployed[-1]))
        self.assertIn("index.html", z.namelist())
        blob = b"".join(z.read(n) for n in z.namelist())
        for secret in (b"SECRETREPO", b"SECRETCOMMIT", TOKEN.encode(), PASSPHRASE.encode()):
            self.assertNotIn(secret, blob)

    def test_rerun_with_same_token_and_passphrase_reuses_the_site(self):
        self.setup_ok()
        models.save_snapshot_state({**models.get_snapshot_state(), "site_id": "kept-site"})
        FakeNetlify.calls = []
        self.m._snapshot_secrets = None  # after a restart: old secrets come from the file
        self.setup_ok()
        self.assertIn(("get_site", "kept-site"), FakeNetlify.calls)
        self.assertNotIn(("create_site",), FakeNetlify.calls)
        self.assertNotIn(("delete_site", "kept-site"), FakeNetlify.calls)
        # Deleted on Netlify meanwhile: a new one is created.
        FakeNetlify.calls = []
        FakeNetlify.get_site_error = nf.NetlifyError("gone", status=404)
        self.setup_ok()
        self.assertIn(("create_site",), FakeNetlify.calls)

    def test_new_passphrase_or_token_replaces_the_site(self):
        # Session 25 review: copies under an old passphrase / on another
        # account must not stay online where TAKE DOWN can't reach them.
        self.setup_ok()
        models.save_snapshot_state({**models.get_snapshot_state(), "site_id": "old-site"})
        FakeNetlify.calls = []
        new_pass = "violet harbor eleven candle"
        self.setup_ok(passphrase=new_pass, passphrase_confirm=new_pass)
        self.assertIn(("delete_site", "old-site"), FakeNetlify.calls)
        self.assertIn(("create_site",), FakeNetlify.calls)
        self.assertTrue(any("old snapshot site was deleted" in m for _, m in self.flashes()))

    def test_old_site_that_cant_be_deleted_is_named(self):
        self.setup_ok()
        models.save_snapshot_state({**models.get_snapshot_state(), "site_id": "old-site",
                                    "url": "https://old.netlify.app"})
        self.flashes()
        FakeNetlify.delete_error = nf.NetlifyError("no access", status=403)
        self.setup_ok(netlify_token="nfp_OTHERaccountTOKEN1234567890")
        msgs = [m for _, m in self.flashes()]
        self.assertTrue(any("https://old.netlify.app" in m and "still online" in m for m in msgs), msgs)

    def test_passphrase_equal_to_app_password_refused(self):
        strong_password = "Harbor-Candle-11-Violet"
        security.encrypt_credentials(strong_password, "ghp_x", "sk-ant-api03-" + "k" * 30)
        r = self.setup_ok(password=strong_password, passphrase=strong_password,
                          passphrase_confirm=strong_password)
        self.assertEqual(r.status_code, 302)
        (cat, msg), = self.flashes()
        self.assertIn("different from your app password", msg)
        self.assertFalse(security.snapshot_secrets_exist())

    # --- publish ----------------------------------------------------------

    def test_publish_locked_after_restart(self):
        models.save_snapshot_state({"site_id": "site-123"})
        self.client.post("/snapshot/publish")
        (cat, msg), = self.flashes()
        self.assertIn("Log out and back in", msg)

    def test_publish_success_and_redirect_back(self):
        self.setup_ok()
        self.flashes()
        r = self.client.post("/snapshot/publish", data={"next": "/review?group="})
        self.assertEqual(r.headers["Location"], "/review?group=")
        self.assertTrue(any("Snapshot published" in m for _, m in self.flashes()))
        events = [e["event"] for e in models.tail_snapshot_log(20)]
        self.assertIn("publish", events)

    def test_publish_site_deleted_on_netlify_clears_site(self):
        self.setup_ok()
        self.flashes()
        FakeNetlify.deploy_error = nf.NetlifyError("That site no longer exists on Netlify.", status=404)
        self.client.post("/snapshot/publish")
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertEqual(models.get_snapshot_state()["site_id"], "")

    def test_unexpected_failure_is_reported_not_500(self):
        self.setup_ok()
        self.flashes()
        with patch.object(snap, "render_body", side_effect=RuntimeError("boom")):
            r = self.client.post("/snapshot/publish")
        self.assertEqual(r.status_code, 302)
        (cat, msg), = self.flashes()
        self.assertIn("RuntimeError: boom", msg)

    def test_secrets_never_logged_or_flashed(self):
        self.setup_ok()
        self.client.post("/snapshot/publish")
        FakeNetlify.deploy_error = nf.NetlifyError("bad", status=500)
        self.client.post("/snapshot/publish")
        blob = json.dumps(self.flashes()) + open(models.SNAPSHOT_LOG_PATH).read()
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(PASSPHRASE, blob)
        page = self.client.get("/settings").data.decode()
        self.assertNotIn(TOKEN, page)
        self.assertNotIn(PASSPHRASE, page)

    # --- preview ----------------------------------------------------------

    def test_preview_offers_no_remember_and_refuses_cross_site(self):
        self.setup_ok()
        body = self.client.get("/snapshot/preview").data.decode()
        self.assertNotIn('id="rd-remember"', body)
        r = self.client.get("/snapshot/preview", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)

    def test_credential_reset_clears_and_locks_snapshot_secrets(self):
        # Session 25 review: a reset with a new app password used to leave
        # snapshot.enc unopenable forever and the site un-takedownable.
        self.setup_ok()
        self.flashes()
        self.client.post("/settings", data={"action": "reset_credentials", "reset_token": self.m._RESET_TOKEN})
        self.assertFalse(security.snapshot_secrets_exist())
        self.assertIsNone(self.m._snapshot_secrets)
        self.assertTrue(models.get_snapshot_state()["needs_setup"])
        with self.client.session_transaction() as s:
            s["authenticated"] = True
        page = self.client.get("/settings").data.decode()
        self.assertIn("credentials were reset", page)
        # /login/reset does the same.
        security.encrypt_snapshot_secrets(PASSWORD, {"netlify_token": TOKEN, "passphrase": PASSPHRASE})
        self.m._snapshot_secrets = {"netlify_token": TOKEN, "passphrase": PASSPHRASE}
        self.client.post("/login/reset", data={"reset_token": self.m._RESET_TOKEN})
        self.assertFalse(security.snapshot_secrets_exist())
        self.assertIsNone(self.m._snapshot_secrets)

    def test_post_guard_rejects_null_origin_and_cross_site_fetches(self):
        # Session 25 review: a page on another 127.0.0.1 port posting to
        # itself, then 307-redirecting here, arrives with Origin: null.
        self.setup_ok()
        for headers in ({"Origin": "null"}, {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Origin": "http://127.0.0.1:8777"}):
            r = self.client.post("/snapshot/takedown", data={"reset_token": self.m._RESET_TOKEN},
                                 headers=headers)
            self.assertEqual(r.status_code, 403, headers)
        self.assertTrue(security.snapshot_secrets_exist())
        ok = self.client.post("/snapshot/publish", headers={"Sec-Fetch-Site": "same-origin",
                                                            "Origin": "http://localhost"})
        self.assertEqual(ok.status_code, 302)

    def test_preview_is_the_encrypted_page_with_its_csp(self):
        r = self.client.get("/snapshot/preview")
        self.assertEqual(r.status_code, 302)  # locked / not set up
        self.setup_ok()
        r = self.client.get("/snapshot/preview")
        self.assertEqual(r.status_code, 200)
        self.assertIn("script-src 'sha256-", r.headers.get("Content-Security-Policy", ""))
        body = r.data.decode()
        self.assertNotIn("SECRETREPO", body)
        self.assertNotIn("SECRETCOMMIT", body)

    # --- takedown ---------------------------------------------------------

    def takedown(self, token=True):
        return self.client.post("/snapshot/takedown",
                                data={"reset_token": self.m._RESET_TOKEN} if token else {})

    def test_takedown_needs_the_per_boot_token(self):
        self.setup_ok()
        self.flashes()
        self.takedown(token=False)
        (cat, msg), = self.flashes()
        self.assertIn("Invalid reset request", msg)
        self.assertNotIn(("delete_site", "site-123"), FakeNetlify.calls)
        self.assertTrue(security.snapshot_secrets_exist())

    def test_takedown_deletes_site_and_forgets_everything(self):
        self.setup_ok()
        self.takedown()
        self.assertIn(("delete_site", "site-123"), FakeNetlify.calls)
        self.assertFalse(security.snapshot_secrets_exist())
        self.assertEqual(models.get_snapshot_state(), {})
        self.assertIsNone(self.m._snapshot_secrets)

    def test_takedown_failure_changes_nothing(self):
        self.setup_ok()
        self.flashes()
        FakeNetlify.delete_error = nf.NetlifyError("Netlify is down.", status=503)
        self.takedown()
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertTrue(security.snapshot_secrets_exist())
        self.assertEqual(models.get_snapshot_state()["site_id"], "site-123")

    def test_takedown_locked_refuses(self):
        security.encrypt_snapshot_secrets(PASSWORD, {"netlify_token": TOKEN, "passphrase": PASSPHRASE})
        models.save_snapshot_state({"site_id": "site-123"})
        self.takedown()
        (cat, msg), = self.flashes()
        self.assertIn("Log out and back in", msg)
        self.assertEqual(models.get_snapshot_state()["site_id"], "site-123")

    # --- unlock / lock + buttons -------------------------------------------

    def test_unlock_at_login_and_lock_at_logout(self):
        security.encrypt_snapshot_secrets(PASSWORD, {"netlify_token": TOKEN, "passphrase": PASSPHRASE})
        self.m._unlock_snapshot("wrong")
        self.assertIsNone(self.m._snapshot_secrets)
        self.m._unlock_snapshot(PASSWORD)
        self.assertEqual(self.m._snapshot_secrets["netlify_token"], TOKEN)
        self.client.get("/logout")
        self.assertIsNone(self.m._snapshot_secrets)

    def test_publish_to_phone_button_only_when_ready(self):
        self.assertNotIn("PUBLISH TO PHONE", self.client.get("/review").data.decode())
        self.setup_ok()
        self.assertIn("PUBLISH TO PHONE", self.client.get("/review").data.decode())

    def test_settings_card_states(self):
        page = self.client.get("/settings").data.decode()
        self.assertIn("SET UP &amp; PUBLISH", page)
        self.assertIn("Personal access tokens", page)
        self.setup_ok()
        page = self.client.get("/settings").data.decode()
        self.assertIn("PUBLISH NOW", page)
        self.assertIn("repodoctor-snapshot-abc123.netlify.app", page)
        self.m._snapshot_secrets = None
        self.assertIn("publishing is locked", self.client.get("/settings").data.decode())

    # --- data -------------------------------------------------------------

    def test_snapshot_data_shape_and_offline(self):
        with self.m.app.test_request_context("/"):
            data = self.m._snapshot_data(self.m._scan_results)
        self.assertEqual([r["key"] for r in data["ranges"]], ["week", "month", "quarter", "ytd"])
        self.assertIn("items", data["do_next"])
        self.assertIn("shipped", data["review"])
        self.assertTrue(data["portfolio"])
        self.assertEqual(data["portfolio"][0]["rows"][0]["name"], "SECRETREPO")
        self.m._github_client = None
        self.m._activity_cache = None
        with self.m.app.test_request_context("/"):
            data = self.m._snapshot_data(self.m._scan_results)
        self.assertEqual(data["ranges"], [])
        self.assertEqual(data["activity_fetched_at"], "")


if __name__ == "__main__":
    unittest.main()
