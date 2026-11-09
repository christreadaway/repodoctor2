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
    create_error = None
    deploy_error = None
    delete_error = None
    delete_outcome = "deleted"
    deploy_state = "ready"
    deployed: list = []
    sites_made = 0
    sites: dict = {}

    def __init__(self, token, session=None, log_event=None):
        self.token = token
        FakeNetlify.calls.append(("init", token))

    def verify(self):
        FakeNetlify.calls.append(("verify",))
        if FakeNetlify.verify_error:
            raise FakeNetlify.verify_error
        return {"ok": True}

    def create_site(self, name_prefix=""):
        FakeNetlify.calls.append(("create_site",))
        if FakeNetlify.create_error:
            raise FakeNetlify.create_error
        FakeNetlify.sites_made += 1
        n = FakeNetlify.sites_made
        sid = "site-123" if n == 1 else f"site-{n}"
        site = {"site_id": sid, "name": f"abc{n}",
                "url": "https://repodoctor-snapshot-abc123.netlify.app" if n == 1 else f"https://new{n}.netlify.app",
                "admin_url": f"https://app.netlify.com/sites/abc{n}"}
        FakeNetlify.sites[sid] = site
        return dict(site)

    def get_site(self, site_id):
        FakeNetlify.calls.append(("get_site", site_id))
        if FakeNetlify.get_site_error:
            raise FakeNetlify.get_site_error
        return dict(FakeNetlify.sites.get(site_id) or {"site_id": site_id, "name": "existing",
                                                        "url": "https://existing.netlify.app", "admin_url": ""})

    def deploy_zip(self, site_id, zip_bytes):
        FakeNetlify.calls.append(("deploy_zip", site_id, len(zip_bytes)))
        if FakeNetlify.deploy_error:
            raise FakeNetlify.deploy_error
        FakeNetlify.deployed.append(zip_bytes)
        return {"deploy_id": "dep-1", "state": "ready", "url": "https://dep--x.netlify.app", "elapsed_s": 1.0}

    def get_deploy(self, deploy_id):
        FakeNetlify.calls.append(("get_deploy", deploy_id))
        return {"deploy_id": deploy_id, "state": FakeNetlify.deploy_state, "url": None, "site_url": None,
                "error_message": "build exploded" if FakeNetlify.deploy_state == "error" else None}

    def delete_site(self, site_id):
        FakeNetlify.calls.append(("delete_site", site_id, self.token))
        if FakeNetlify.delete_error:
            raise FakeNetlify.delete_error
        return FakeNetlify.delete_outcome


def names(calls):
    """Call names/args without the token, for order checks."""
    return [c[:2] if c[0] == "delete_site" else c for c in calls if c[0] != "init"]


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
        FakeNetlify.verify_error = FakeNetlify.get_site_error = FakeNetlify.create_error = None
        FakeNetlify.deploy_error = FakeNetlify.delete_error = None
        FakeNetlify.delete_outcome, FakeNetlify.deploy_state, FakeNetlify.sites_made = "deleted", "ready", 0
        FakeNetlify.sites = {}
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
        """Pending messages as (category, text); snapshot messages carry a
        "snapshot-" category prefix (they render inside the Settings card)."""
        with self.client.session_transaction() as sess:
            out = list(sess.get("_flashes", []))
            sess.pop("_flashes", None)
        for cat, _ in out:
            self.assertTrue(cat.startswith("snapshot-"), cat)
        return [(cat[len("snapshot-"):], msg) for cat, msg in out]

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
        # Deleted on Netlify meanwhile (or on another account): a new one is
        # created and published, and only THEN is the old one deleted.
        FakeNetlify.calls = []
        FakeNetlify.get_site_error = nf.NetlifyError("gone", status=404)
        self.setup_ok()
        seq = names(FakeNetlify.calls)
        self.assertIn(("create_site",), seq)
        self.assertLess(seq.index(("create_site",)), seq.index(("delete_site", "kept-site")))
        self.assertLess([c[0] for c in seq].index("deploy_zip"), seq.index(("delete_site", "kept-site")))

    NEW_PASS = "violet harbor eleven candle"

    def change_passphrase(self, **over):
        data = {"passphrase": self.NEW_PASS, "passphrase_confirm": self.NEW_PASS}
        data.update(over)
        return self.setup_ok(**data)

    def test_new_passphrase_gets_a_new_site_and_the_old_one_goes_after_publish(self):
        # Copies under an old passphrase must not stay online (Netlify keeps
        # every old deploy at its own address), but the old site is deleted
        # only once the new one is live (E2E review H2).
        self.setup_ok()
        models.save_snapshot_state({**models.get_snapshot_state(), "site_id": "old-site",
                                    "url": "https://old.netlify.app"})
        self.flashes()
        FakeNetlify.calls = []
        r = self.change_passphrase()
        seq = names(FakeNetlify.calls)
        self.assertEqual([c[0] for c in seq], ["verify", "create_site", "deploy_zip", "delete_site"])
        self.assertEqual(seq[-1], ("delete_site", "old-site"))
        st = models.get_snapshot_state()
        self.assertEqual(st["site_id"], "site-2")
        self.assertEqual(st["retire"], [])
        self.assertIn("snapshot_new=1", r.headers["Location"])
        msgs = [m for _, m in self.flashes()]
        self.assertTrue(any("https://old.netlify.app" in m and "was deleted" in m for m in msgs), msgs)
        self.assertEqual(security.decrypt_snapshot_secrets(PASSWORD)["passphrase"], self.NEW_PASS)

    def test_create_failure_during_a_change_leaves_the_old_site_alone(self):
        # E2E review H2: the old site used to be deleted BEFORE the new one
        # existed, then "Nothing was changed" left a dead link marked live.
        self.setup_ok()
        before_state = models.get_snapshot_state()
        self.flashes()
        FakeNetlify.calls = []
        FakeNetlify.create_error = nf.NetlifyError("Netlify is having trouble right now (HTTP 503).", status=503)
        self.change_passphrase()
        self.assertNotIn("delete_site", [c[0] for c in FakeNetlify.calls])
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIn("Nothing was changed", msg)
        self.assertEqual(models.get_snapshot_state(), before_state)
        self.assertEqual(security.decrypt_snapshot_secrets(PASSWORD)["passphrase"], PASSPHRASE)
        self.assertEqual(self.m._snapshot_secrets["passphrase"], PASSPHRASE)

    def test_publish_failure_after_a_change_keeps_the_old_site_until_next_success(self):
        self.setup_ok()
        models.save_snapshot_state({**models.get_snapshot_state(), "url": "https://old.netlify.app"})
        self.flashes()
        FakeNetlify.calls = []
        FakeNetlify.deploy_error = nf.NetlifyError("Netlify is having trouble right now (HTTP 502).", status=502)
        self.change_passphrase()
        self.assertNotIn("delete_site", [c[0] for c in FakeNetlify.calls])
        st = models.get_snapshot_state()
        self.assertEqual(st["site_id"], "site-2")
        self.assertEqual([p["site_id"] for p in st["retire"]], ["site-123"])
        page = self.client.get("/settings").data.decode()
        self.assertIn("previous phone link (https://old.netlify.app) is still online", page)
        # The next good publish deletes it.
        FakeNetlify.deploy_error = None
        self.flashes()
        self.client.post("/snapshot/publish")
        self.assertIn(("delete_site", "site-123"), names(FakeNetlify.calls))
        self.assertEqual(models.get_snapshot_state()["retire"], [])

    def test_old_site_that_cant_be_deleted_is_named(self):
        self.setup_ok()
        models.save_snapshot_state({**models.get_snapshot_state(), "url": "https://old.netlify.app"})
        self.flashes()
        FakeNetlify.delete_error = nf.NetlifyError("no access", status=403)
        self.change_passphrase()
        msgs = [m for _, m in self.flashes()]
        self.assertTrue(any("https://old.netlify.app" in m and "still online" in m
                            and "app.netlify.com/sites/abc1" in m for m in msgs), msgs)
        self.assertEqual(models.get_snapshot_state()["retire"], [])  # nothing more we can do

    def test_old_site_delete_that_fails_temporarily_is_retried(self):
        self.setup_ok()
        self.flashes()
        FakeNetlify.delete_error = nf.NetlifyError("Netlify is having trouble right now (HTTP 503).", status=503)
        self.change_passphrase()
        self.assertEqual([p["site_id"] for p in models.get_snapshot_state()["retire"]], ["site-123"])
        self.assertTrue(any("tries again" in m for _, m in self.flashes()))
        FakeNetlify.delete_error = None
        self.client.post("/snapshot/publish")
        self.assertEqual(models.get_snapshot_state()["retire"], [])

    def test_token_only_change_keeps_the_site_and_link(self):
        # E2E review M3: a new token for the same account (same passphrase)
        # used to replace the site and change the phone URL.
        self.setup_ok()
        url = models.get_snapshot_state()["url"]
        FakeNetlify.calls = []
        new_token = "nfp_NEWtokenSAMEaccount1234567890"
        self.setup_ok(netlify_token=new_token, passphrase="", passphrase_confirm="")
        seq = names(FakeNetlify.calls)
        self.assertIn(("get_site", "site-123"), seq)
        self.assertNotIn(("create_site",), seq)
        self.assertNotIn("delete_site", [c[0] for c in seq])
        self.assertEqual(models.get_snapshot_state()["site_id"], "site-123")
        self.assertEqual(models.get_snapshot_state()["url"], url)
        self.assertEqual(security.decrypt_snapshot_secrets(PASSWORD),
                         {"netlify_token": new_token, "passphrase": PASSPHRASE})

    def test_blank_token_keeps_the_saved_one(self):
        # E2E review M2: Netlify shows a token once; changing only the
        # passphrase mustn't need a new one.
        self.setup_ok()
        self.m._snapshot_secrets = None  # even after a restart (read with the app password)
        FakeNetlify.calls = []
        self.change_passphrase(netlify_token="")
        self.assertIn(("init", TOKEN), FakeNetlify.calls)
        self.assertEqual(security.decrypt_snapshot_secrets(PASSWORD),
                         {"netlify_token": TOKEN, "passphrase": self.NEW_PASS})
        page = self.client.get("/settings").data.decode()
        self.assertIn("Leave blank to keep the saved token", page)

    def test_blank_fields_with_nothing_saved_are_refused(self):
        r = self.setup_ok(passphrase="", passphrase_confirm="")
        self.assertIn("snapshot_form=1", r.headers["Location"])
        (cat, msg), = self.flashes()
        self.assertIn("Pick a passphrase", msg)
        self.assertFalse(security.snapshot_secrets_exist())
        self.setup_ok(passphrase=PASSPHRASE, passphrase_confirm="")
        (cat, msg), = self.flashes()
        self.assertIn("don't match", msg)

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
        security.encrypt_snapshot_secrets(PASSWORD, {"netlify_token": TOKEN, "passphrase": PASSPHRASE})
        models.save_snapshot_state({"site_id": "site-123"})
        self.client.post("/snapshot/publish")
        (cat, msg), = self.flashes()
        self.assertIn("Unlock it in Settings", msg)

    def test_publish_after_takedown_says_not_set_up(self):
        # E2E review LOW: a stale PUBLISH said "The app restarted".
        self.client.post("/snapshot/publish")
        (cat, msg), = self.flashes()
        self.assertIn("isn't set up", msg)

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
        FakeNetlify.deploy_error = nf.NetlifyError(nf.MSG_SITE_GONE, status=404, kind="site_gone")
        self.client.post("/snapshot/publish")
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIn("CREATE NEW SITE", msg)
        st = models.get_snapshot_state()
        self.assertEqual(st["site_id"], "")
        self.assertEqual(st["gone_url"], "https://repodoctor-snapshot-abc123.netlify.app")
        # E2E review H3: the card explains it and offers one-click recovery
        # with the saved token and passphrase (no re-typing).
        page = self.client.get("/settings").data.decode()
        self.assertIn("no longer exists on Netlify", page)
        self.assertIn("CREATE NEW SITE &amp; PUBLISH", page)
        FakeNetlify.deploy_error = None
        FakeNetlify.calls = []
        r = self.client.post("/snapshot/recreate")
        self.assertIn("snapshot_new=1", r.headers["Location"])
        self.assertEqual([c[0] for c in names(FakeNetlify.calls)], ["create_site", "deploy_zip"])
        st = models.get_snapshot_state()
        self.assertEqual(st["site_id"], "site-2")
        self.assertNotIn("gone_url", st)
        self.assertEqual(st["last_error"], "")
        self.assertTrue(any("Snapshot published" in m for _, m in self.flashes()))

    def test_recreate_refuses_when_locked_or_site_exists(self):
        self.setup_ok()
        self.flashes()
        self.client.post("/snapshot/recreate")
        (cat, msg), = self.flashes()
        self.assertIn("already has a site", msg)
        self.m._snapshot_secrets = None
        models.save_snapshot_state({**models.get_snapshot_state(), "site_id": ""})
        FakeNetlify.calls = []
        self.client.post("/snapshot/recreate")
        (cat, msg), = self.flashes()
        self.assertIn("locked", msg)
        self.assertEqual(FakeNetlify.calls, [])

    def test_deploy_timeout_is_kept_and_can_be_checked(self):
        # E2E review M4: "check again in a minute" had no way to check.
        self.setup_ok()
        published = models.get_snapshot_state()["last_published_at"]
        self.flashes()
        FakeNetlify.deploy_error = nf.NetlifyError("slow", kind="deploy_timeout", deploy_id="dep-slow")
        self.client.post("/snapshot/publish")
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "warning")
        self.assertIn("CHECK", msg)
        st = models.get_snapshot_state()
        self.assertEqual(st["pending_deploy"]["deploy_id"], "dep-slow")
        self.assertEqual(st["last_error"], "")
        page = self.client.get("/settings").data.decode()
        self.assertIn('action="/snapshot/check"', page)
        FakeNetlify.deploy_state = "processing"
        self.client.post("/snapshot/check")
        (cat, msg), = self.flashes()
        self.assertIn("still working", msg)
        self.assertIn("pending_deploy", models.get_snapshot_state())
        FakeNetlify.deploy_state = "ready"
        self.client.post("/snapshot/check")
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "success")
        st = models.get_snapshot_state()
        self.assertNotIn("pending_deploy", st)
        self.assertNotEqual(st["last_published_at"], published)
        self.assertEqual(st["last_deploy_id"], "dep-slow")
        self.assertNotIn('action="/snapshot/check"', self.client.get("/settings").data.decode())

    def test_deploy_timeout_that_then_fails(self):
        self.setup_ok()
        self.flashes()
        FakeNetlify.deploy_error = nf.NetlifyError("slow", kind="deploy_timeout", deploy_id="dep-slow")
        self.client.post("/snapshot/publish")
        FakeNetlify.deploy_state = "error"
        self.flashes()
        self.client.post("/snapshot/check")
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIn("build exploded", msg)
        st = models.get_snapshot_state()
        self.assertNotIn("pending_deploy", st)
        self.assertIn("build exploded", st["last_error"])

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
        self.assertIn("nothing was deleted", msg)
        self.assertNotIn(("delete_site", "site-123"), FakeNetlify.calls)
        self.assertTrue(security.snapshot_secrets_exist())

    def test_takedown_deletes_site_and_forgets_everything(self):
        self.setup_ok()
        self.takedown()
        self.assertIn(("delete_site", "site-123"), names(FakeNetlify.calls))
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
        self.assertIn("Unlock it in Settings", msg)
        self.assertEqual(models.get_snapshot_state()["site_id"], "site-123")

    def test_takedown_also_deletes_a_site_waiting_to_be_retired(self):
        self.setup_ok()
        FakeNetlify.deploy_error = nf.NetlifyError("down", status=502)
        self.change_passphrase()
        FakeNetlify.calls = []
        self.takedown()
        deleted = [c[1] for c in names(FakeNetlify.calls) if c[0] == "delete_site"]
        self.assertEqual(deleted, ["site-2", "site-123"])
        self.assertEqual(models.get_snapshot_state(), {})

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
        page = self.client.get("/settings").data.decode()
        self.assertIn("the phone snapshot is locked", page)
        self.assertIn('action="/snapshot/unlock"', page)

    def test_unlock_in_the_card(self):
        # E2E review LOW: "log out and back in" had no link and landed on My Repos.
        self.client.post("/snapshot/unlock", data={"password": PASSWORD})
        (cat, msg), = self.flashes()
        self.assertIn("nothing to unlock", msg)
        self.setup_ok()
        self.flashes()
        self.m._snapshot_secrets = None
        self.client.post("/snapshot/unlock", data={"password": "wrong"})
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "error")
        self.assertIsNone(self.m._snapshot_secrets)
        self.client.post("/snapshot/unlock", data={"password": PASSWORD})
        (cat, msg), = self.flashes()
        self.assertEqual(cat, "success")
        self.assertEqual(self.m._snapshot_secrets, {"netlify_token": TOKEN, "passphrase": PASSPHRASE})

    def test_messages_render_inside_the_card_not_at_the_top(self):
        # E2E review H1: the redirect lands on #snapshot, so messages drawn
        # at the top of Settings were never seen.
        self.setup_ok(password="nope")
        page = self.client.get("/settings?snapshot_form=1").data.decode()
        card = page.index('id="snapshot"')
        msg = page.index("Wrong app password")
        self.assertGreater(msg, card)
        self.assertEqual(page.count("Wrong app password"), 1)
        self.assertNotIn("flash-snapshot-", page)
        self.assertIn('class="flash flash-error"', page[card:])
        # The setup form reopens so the owner can fix it.
        details = page.index("Change the Netlify token or passphrase") if "Change the Netlify" in page else -1
        self.assertEqual(details, -1)  # first-time: the form is already open
        # From /review (PUBLISH TO PHONE), they show at the top as usual.
        self.setup_ok()
        self.flashes()
        self.client.post("/snapshot/publish", data={"next": "/review"})
        page = self.client.get("/review").data.decode()
        self.assertIn('class="flash flash-success">Snapshot published', page)

    def test_setup_form_reopens_after_a_problem(self):
        self.setup_ok()
        self.flashes()
        r = self.setup_ok(password="nope")
        self.assertIn("snapshot_form=1", r.headers["Location"])
        page = self.client.get("/settings?snapshot_form=1").data.decode()
        self.assertRegex(page, r'<details class="mt-sm"\s+open>\s*<summary class="text-secondary">Change the Netlify token')
        page = self.client.get("/settings").data.decode()
        self.assertRegex(page, r'<details class="mt-sm"\s*>\s*<summary class="text-secondary">Change the Netlify token')

    def test_card_has_copy_link_and_qr_for_the_phone(self):
        # E2E review M5.
        self.setup_ok()
        page = self.client.get("/settings?snapshot_new=1").data.decode()
        self.assertIn('data-copy="https://repodoctor-snapshot-abc123.netlify.app"', page)
        self.assertIn("COPY LINK", page)
        self.assertRegex(page, r'<details class="snapshot-phone mt-sm"\s+open>')
        try:
            import qr  # noqa: F401
        except ImportError:
            return
        self.assertIn('<svg', page[page.index('class="snapshot-qr"'):])

    def test_preview_sends_one_charset(self):
        self.setup_ok()
        r = self.client.get("/snapshot/preview")
        self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")

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
