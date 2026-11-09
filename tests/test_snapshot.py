"""Tests for the encrypted phone snapshot (snapshot.py + templates/snapshot/).

The fixture is built with the app's real builders (nextup.build_do_next,
nextup.build_review, activity.build_report), so a change to their output
shape shows up here, not on the owner's phone. `realistic_data(adversarial=
True)` adds hostile GitHub text (XSS payloads, 200-char words, RTL, emoji,
direction overrides, tampered session URLs); the browser check reuses it.
"""

import base64
import datetime
import hashlib
import html
import io
import json
import os
import re
import sys
import unittest
import zipfile
from html.parser import HTMLParser

import markupsafe

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import activity
import nextup
import snapshot

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 9, 18, 0, tzinfo=UTC)
PASS = "harbor candle eleven violet"

# Distinctive strings that must never appear in a published file.
REPO_NAMES = ["zebra-parish-app", "quokka-school-portal", "marmot-crm", "ocelot-grants",
              "narwhal-events", "ibis-volunteer-hub"]
LONG_TEST_NAME = "test_volunteer_signup_rejects_duplicate_email_addresses_across_all_ministries_v2"
LONG_PATH = "src/components/volunteers/forms/VolunteerSignupFormWithDuplicateDetection.tsx"
COMMIT_TEXT = "Fix the volunteer sign-up flow for Advent"
PR_TITLE = "Add RSVP reminders for the fall festival"
AI_REPO_TEXT = "This week the parish app gained a working volunteer sign-up."
AI_OVERVIEW = "Across the portfolio, volunteer tools moved the most."
ONE_LINER = "Sign-ups and schedules for parish ministries"
XSS = '<script>alert("xss")</script>'
XSS_IMG = '"><img src=x onerror=alert(1)>'
XSS_CLOSE = "</script><script>alert(2)</script>"
LONG_WORD = "W" * 200
RTL = "שלום עולם مرحبا"
EMOJI = "\U0001F680 Ship it \U0001F389" + "\U0001F525" * 40
BIDI = "‮gnp.exe"
GOOD_SESSION = "https://claude.ai/code/session_01AbCdEfGh"
GOOD_SESSION_2 = "https://claude.ai/code/session_stale_branch-9"
BAD_SESSIONS = [
    'https://claude.ai/code/session_x" onmouseover="alert(1)',
    "https://evil.example/code/session_abc",
    "javascript:alert(1)//https://claude.ai/code/session_abc",
    "https://claude.ai/code/session_abc/../../steal",
    "https://claude.ai/code/session_abc?next=https://evil.example",
]
CANONICAL_SESSION = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]{1,128}")


def iso(days_ago, hours=0):
    return (NOW - datetime.timedelta(days=days_ago, hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo(name, owner="o"):
    return {"owner": owner, "name": name, "full_name": f"{owner}/{name}", "default_branch": "main",
            "html_url": f"https://github.com/{owner}/{name}", "pushed_at": iso(1)}


def inflight(name, kind, branch, days=1, pr=None, session_url="", ahead=2, topic=None):
    return {"repo": name, "owner": "o", "branch": branch, "kind": kind,
            "topic": topic or f"{name} {kind} work", "base": "main", "pr": pr, "ahead_by": ahead,
            "text": f"{kind}: {COMMIT_TEXT}", "last_activity_at": iso(days), "age_days": days,
            "branch_url": f"https://github.com/o/{name}/tree/{branch}", "session_url": session_url}


def commit(sha, summary, days_ago, merge=False):
    return {"sha": sha, "short": sha[:7], "summary": summary, "author": "Owner",
            "date": iso(days_ago), "is_merge": merge, "html_url": f"https://github.com/o/r/commit/{sha}"}


def merged_pr(number, title, days_ago):
    return {"number": number, "title": title, "merged_at": iso(days_ago),
            "html_url": f"https://github.com/o/r/pull/{number}", "author": "owner", "base": "main"}


def realistic_data(adversarial=False, extra_items=0, extra_repos=0):
    """A snapshot `data` dict shaped exactly like the integrator's."""
    names = list(REPO_NAMES) + [f"filler-repo-{i:02d}" for i in range(extra_repos)]
    if adversarial:
        names.append("repo-" + LONG_WORD)
    repos = [repo(n) for n in names]
    zebra, quokka, marmot, ocelot, narwhal, ibis = REPO_NAMES
    topic = f"{PR_TITLE} {XSS} {LONG_WORD} {RTL} {EMOJI}" if adversarial else PR_TITLE
    items = [
        inflight(zebra, "merge", "claude/rsvp-abc123", days=3,
                 pr={"number": 41, "html_url": "https://github.com/o/zebra-parish-app/pull/41"},
                 session_url=GOOD_SESSION, topic=topic),
        inflight(quokka, "finish", "claude/forms-def456",
                 pr={"number": 7, "html_url": "https://github.com/o/quokka-school-portal/pull/7",
                     "draft": True}, session_url=BAD_SESSIONS[0]),
        inflight(narwhal, "continue", "feature/" + (LONG_WORD if adversarial else "rsvp"),
                 session_url=BAD_SESSIONS[1]),
        inflight(marmot, "stale", "old/experiment", days=40, session_url=GOOD_SESSION_2),
        inflight(ocelot, "stale", "old/" + XSS_IMG, days=50, session_url=BAD_SESSIONS[2]),
        inflight(zebra, "complete", "claude/done-111111"),
    ]
    for i in range(extra_items):
        items.append(inflight(names[i % len(names)], "continue", f"bulk/branch-{i:03d}", days=i % 9,
                              session_url=BAD_SESSIONS[3] if i % 2 else BAD_SESSIONS[4]))
    trackers = {f"o/{zebra}": {"next_actions": [
        {"id": "N1", "priority": "P0", "status": "todo",
         "title": f"Fix login {XSS_IMG}" if adversarial else "Fix login on phones",
         "prompt": f'In {zebra}, fix "login" on <phones>.\nThen run {LONG_TEST_NAME}.'},
        {"id": "N2", "priority": "P1", "status": "awaiting_deploy", "title": "Confirm RSVP emails"},
        {"id": "N3", "priority": "P2", "status": "todo", "title": "Low priority"},
    ]}}
    # ibis has no branch work, so its planned step shows; marmot is busy (stale branch).
    summaries = {ibis: {"next_steps": ["Ship the RSVP page", "Then the reminders"]},
                 marmot: {"next_steps": [f"Clean up {LONG_PATH}"]}}
    do_next = nextup.build_do_next(items, trackers, summaries, repos, now=NOW)

    week_commits = [commit("a1b2c3d4e5", COMMIT_TEXT, 1),
                    commit("b2c3d4e5f6", f"Run {LONG_TEST_NAME}", 2),
                    commit("c3d4e5f6a7", f"Touch {LONG_PATH}", 3)]
    if adversarial:
        week_commits += [commit("d4e5f6a7b8", f"{XSS} {XSS_CLOSE}", 2),
                         commit("e5f6a7b8c9", f"{BIDI} {RTL} {EMOJI} {LONG_WORD}", 1)]
    gh = {
        f"o/{zebra}": {"commits": week_commits + [commit("f00f00f00f", "Older work", 20)],
                       "prs": [merged_pr(41, PR_TITLE, 2), merged_pr(39, "Earlier PR", 25)]},
        f"o/{quokka}": {"commits": [commit("1111111111", "Quokka grade book", 4)], "prs": []},
        f"o/{marmot}": {"commits": [commit("2222222222", "Marmot contacts import", 40)], "prs": []},
        f"o/{narwhal}": {"commits": [commit("3333333333", "Narwhal calendar", 60)],
                         "prs": [merged_pr(5, "Narwhal first release", 61)]},
        f"o/{ibis}": {"commits": [], "prs": []},  # read fine, idle: not a failed read
    }
    for i in range(extra_repos):
        gh[f"o/filler-repo-{i:02d}"] = {"commits": [commit(f"{i:010d}", f"Filler commit {i}", 1 + i % 5)],
                                        "prs": []}
    if adversarial:
        gh[f"o/repo-{LONG_WORD}"] = {"commits": [commit("9999999999", LONG_WORD + LONG_WORD, 1)],
                                     "prs": [merged_pr(1, f"{XSS_IMG} {LONG_WORD}", 1)]}
    for d in gh.values():
        d.update(commits_truncated=False, prs_truncated=False, commits_reached="", prs_reached="")
    errors = {f"o/{ocelot}": "GitHubFetchError: 502 Bad Gateway"}

    today = NOW.astimezone(activity._LOCAL_TZ).date() if activity._LOCAL_TZ else NOW.date()
    ranges, reports = [], {}
    for key in ("week", "month", "quarter", "ytd"):
        rng = activity.preset_range(key, today)
        rep = activity.build_report(repos, gh, errors, rng)
        for r in rep["rows"]:
            r["ai"] = None
        rep["ai_portfolio"] = None
        reports[key] = rep
        ranges.append({"key": key, "name": rng.name, "dates": rng.dates_label,
                       "totals": rep["totals"], "updated": rep["updated"],
                       "failed": rep["failed"], "ai_portfolio": rep["ai_portfolio"]})
    week = reports["week"]
    zebra_row = next(r for r in week["updated"] if r["name"] == zebra)
    zebra_row["ai"] = {"text": AI_REPO_TEXT + (f" {XSS}" if adversarial else ""), "stale": False}
    quokka_row = next(r for r in week["updated"] if r["name"] == quokka)
    quokka_row["ai"] = {"text": "Quokka got a grade book.", "stale": True}
    week["ai_portfolio"] = {"text": AI_OVERVIEW, "stale": True}
    ranges[0]["ai_portfolio"] = week["ai_portfolio"]

    review = nextup.build_review(week, reports["quarter"], do_next, items, repos, now=NOW)
    portfolio = [
        {"tag": "Church", "rows": [
            {"name": zebra, "one_liner": ONE_LINER + (f" {XSS} {LONG_WORD}" if adversarial else ""),
             "grade": "B", "tone": "ok", "summary": "84/100. Cleaning up branches alone lifts it to 92 (A).",
             "html_url": f"https://github.com/o/{zebra}", "pushed_at": iso(1), "open_branches": 3},
            {"name": marmot, "one_liner": "Contacts", "grade": "F", "tone": "bad",
             "summary": "40/100. No single fix gets it past F. If it's finished, tag it Archived.",
             "html_url": "javascript:alert(1)", "pushed_at": iso(40), "open_branches": 0},
        ]},
        {"tag": "School" + (f" {XSS_IMG}" if adversarial else ""), "rows": [
            {"name": quokka, "one_liner": "", "grade": "C", "tone": "warn", "summary": "72/100.",
             "html_url": f"https://github.com/o/{quokka}", "pushed_at": iso(4), "open_branches": 1},
            {"name": narwhal, "one_liner": RTL if adversarial else "Events", "grade": "—",
             "tone": "none", "summary": "Not graded: a fork. Its upkeep belongs to the original project.",
             "html_url": f"https://github.com/o/{narwhal}", "pushed_at": "", "open_branches": 0},
        ]},
    ]
    return {
        "built_label": "Nov 09, 2026 12:00 PM CST",
        "inflight_gathered_at": iso(0, hours=2),
        "activity_fetched_at": iso(2),
        "do_next": do_next,
        "review": review,
        "review_week_label": ranges[0]["dates"],
        "ranges": ranges,
        "portfolio": portfolio,
    }


def secret_strings(adversarial=True):
    """Text from the fixture that must never appear in a published file."""
    out = REPO_NAMES + [LONG_TEST_NAME, LONG_PATH, COMMIT_TEXT, PR_TITLE, AI_REPO_TEXT, AI_OVERVIEW,
                        ONE_LINER, "Fix login", "Confirm RSVP emails", "Ship the RSVP page",
                        "volunteer", "Quokka", "84/100", "Snapshot built", "Do next", "Weekly review",
                        "Portfolio"]
    if adversarial:
        out += [XSS, XSS_IMG, XSS_CLOSE, LONG_WORD, RTL, "\U0001F680", "alert("]
    return out


class _Collect(HTMLParser):
    """Every start tag's attributes, for href / data-copy checks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def parse(page):
    p = _Collect()
    p.feed(page)
    return p.tags


# ----------------------------------------------------------------------
# Crypto
# ----------------------------------------------------------------------

class TestCrypto(unittest.TestCase):

    def test_round_trip(self):
        text = f"<p>{COMMIT_TEXT} {RTL} {EMOJI}</p>"
        self.assertEqual(snapshot.decrypt(snapshot.encrypt(text, PASS), PASS), text)

    def test_wrong_passphrase_fails(self):
        payload = snapshot.encrypt("secret body", PASS)
        with self.assertRaises(ValueError) as cm:
            snapshot.decrypt(payload, PASS + "x")
        self.assertEqual(str(cm.exception), snapshot.WRONG_PASSPHRASE)
        with self.assertRaises(ValueError):
            snapshot.decrypt(payload, "")

    def test_fresh_salt_and_iv_every_call(self):
        a = snapshot.encrypt("same text", PASS)
        b = snapshot.encrypt("same text", PASS)
        self.assertNotEqual(a["salt"], b["salt"])
        self.assertNotEqual(a["iv"], b["iv"])
        self.assertNotEqual(a["ct"], b["ct"])

    def test_payload_parameters(self):
        p = snapshot.encrypt("hello world", PASS)
        self.assertEqual(list(p), ["v", "kdf", "iter", "salt", "iv", "ct"])
        self.assertEqual((p["v"], p["kdf"], p["iter"]), (1, "PBKDF2-SHA256", 600000))
        self.assertEqual(len(base64.b64decode(p["salt"])), 16)
        self.assertEqual(len(base64.b64decode(p["iv"])), 12)
        # Plaintext is padded to PAD_BLOCK (size doesn't reveal how much is in
        # it), plus the 16-byte GCM tag at the end.
        self.assertEqual(len(base64.b64decode(p["ct"])), snapshot.PAD_BLOCK + 16)
        big = snapshot.encrypt("x" * (snapshot.PAD_BLOCK + 5), PASS)
        self.assertEqual(len(base64.b64decode(big["ct"])), 2 * snapshot.PAD_BLOCK + 16)
        self.assertEqual(snapshot.decrypt(big, PASS), "x" * (snapshot.PAD_BLOCK + 5))

    def test_layout_matches_webcrypto_independently(self):
        """Decrypt with raw primitives (no snapshot code): PBKDF2-SHA256 ->
        AES-256-GCM over ct||tag, no AAD. That's WebCrypto's layout."""
        p = snapshot.encrypt("independent check", PASS)
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(p["salt"]),
                         iterations=p["iter"]).derive(PASS.encode("utf-8"))
        plain = AESGCM(key).decrypt(base64.b64decode(p["iv"]), base64.b64decode(p["ct"]), None).decode("utf-8")
        # The browser injects the padding comment as-is (inert HTML).
        self.assertTrue(plain.startswith("independent check<!--"))
        self.assertRegex(plain, r"^independent check<!--[ ]*-->$")

    def test_tampered_ciphertext_fails(self):
        p = snapshot.encrypt("tamper me", PASS)
        raw = bytearray(base64.b64decode(p["ct"]))
        raw[0] ^= 1
        with self.assertRaises(ValueError):
            snapshot.decrypt({**p, "ct": base64.b64encode(bytes(raw)).decode()}, PASS)

    def test_malformed_payloads_refused(self):
        p = snapshot.encrypt("x" * 20, PASS)
        bad = [
            None, [], {**p, "iter": 10 ** 9}, {**p, "iter": 1000}, {**p, "iter": True},
            {**p, "v": 2}, {**p, "kdf": "scrypt"}, {**p, "salt": "not base64!"},
            {**p, "iv": base64.b64encode(b"short").decode()}, {**p, "extra": "plaintext"},
            {k: v for k, v in p.items() if k != "ct"}, {**p, "ct": "<script>"},
        ]
        for payload in bad:
            with self.subTest(payload=str(payload)[:60]):
                with self.assertRaises(ValueError):
                    snapshot.decrypt(payload, PASS)

    def test_unicode_passphrase_is_nfc_normalized(self):
        composed, decomposed = "café café bells ring", "café café bells ring"
        p = snapshot.encrypt("accent", composed)
        self.assertEqual(snapshot.decrypt(p, decomposed), "accent")

    def test_encrypt_derives_the_key_from_the_nfc_form(self):
        """The phone's script NFC-normalizes before deriving; the Mac must
        too, or an accent typed one way on the phone and the other way on the
        Mac would never unlock. Checked with raw primitives over NFC bytes."""
        composed, decomposed = "café au lait demain", "café au lait demain"
        p = snapshot.encrypt("nfc body", decomposed)
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(p["salt"]),
                         iterations=p["iter"]).derive(composed.encode("utf-8"))
        plain = AESGCM(key).decrypt(base64.b64decode(p["iv"]), base64.b64decode(p["ct"]), None)
        self.assertTrue(plain.decode("utf-8").startswith("nfc body<!--"))

    def test_encrypt_needs_a_passphrase_but_not_the_strength_rules(self):
        # Session 25 review: a passphrase saved under older rules must keep
        # publishing (the rules apply when one is chosen, in Settings), and
        # a refusal must never quote part of it into a log.
        for missing in ("", "   ", None):
            with self.assertRaises(ValueError) as cm:
                snapshot.encrypt("body", missing)
            self.assertEqual(str(cm.exception), "Enter a passphrase.")
        older = "monkey banana tree"
        self.assertIsNotNone(snapshot.check_passphrase(older))
        self.assertEqual(snapshot.decrypt(snapshot.encrypt("body", older), older), "body")
        with self.assertRaises(TypeError):
            snapshot.encrypt(None, PASS)

    def test_lone_surrogate_does_not_break_the_build(self):
        p = snapshot.encrypt("odd \ud800 text", PASS)
        self.assertEqual(snapshot.decrypt(p, PASS), "odd ? text")


class TestPassphrase(unittest.TestCase):

    def test_good_passphrases(self):
        for p in (PASS, "correct horse battery staple", "Tr0ub4dor&3xyz", "café au lait demain",
                  "Tq7v-Lm2x!Zp", "k7mq-x2pd-9wtr-h4nb", "xkqvbnwtrplmzjhgfdsy",
                  "violet harbor eleven candle", "Harbor-Candle-11-Violet", "MyDog Has 2 Fleas!",
                  # A common word or a year among random words is a small penalty, not a refusal.
                  "master candle eleven violet", "orchid 1984 tunnel marble",
                  "remember banana orchard sky"):
            with self.subTest(p=p):
                self.assertIsNone(snapshot.check_passphrase(p))
        self.assertEqual(snapshot.MIN_PASSPHRASE_LEN, 12)

    def test_common_word_or_run_plus_filler_refused(self):
        """Session 25 end-to-end review: each piece passed on its own because
        the rest was 8+ characters, and the old estimate scored them ~80 bits."""
        cases = {
            "password12345678": 'built on "password"',
            "iloveyou12345678": 'built on "iloveyou"',
            "qwertyuiop123456": "keyboard row or a sequence",
            "12345678abcdefgh": "keyboard row or a sequence",
            "abc123abc123abc123": 'built on "abc123"',  # a repeated common word names the word
            "aaaaaaaaaaab1234": "repeats",
        }
        for weak, expected in cases.items():
            with self.subTest(weak=weak):
                problem = snapshot.check_passphrase(weak)
                self.assertIsNotNone(problem)
                self.assertIn(expected, problem)
                self.assertLess(snapshot._estimated_bits(weak), snapshot.MIN_PASSPHRASE_BITS)

    def test_stripping_is_iterative_and_sees_through_disguises(self):
        cases = {
            "12password34": 'built on "password"',        # word out -> "1234" is a run
            "pass word 1234": 'built on "password"',      # separators don't hide a word
            "P@ssw0rd2024!!": 'built on "password"',      # symbol swaps + a year
            "he11o-w0rld-2024": 'built on "hello"',
            "passwordpasswordpassword": 'built on "password"',  # a repeat is one guess, not three
            "xyz789 xyz789": "repeats",
            "Summer2024!!": "too easy to guess",            # word + year + symbols
            "dragon monkey master violet": 'built on "dragon"',
            "!@#$%^&*()qwerty": "keyboard row or a sequence",
            "7890123456789": "keyboard row or a sequence",  # digit runs wrap
            "kxqvbnwtrplm": "too easy to guess",            # nothing guessable, just short
        }
        for weak, expected in cases.items():
            with self.subTest(weak=weak):
                problem = snapshot.check_passphrase(weak)
                self.assertIsNotNone(problem)
                self.assertIn(expected, problem)

    def test_settings_suggest_format_always_accepted(self):
        """SUGGEST in Settings: 4 groups of 4 from an unambiguous alphabet. A
        random group can hold a short run ("abcd", "2345") or a repeat; that
        costs a few bits, never the whole passphrase."""
        import random
        alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
        rng = random.Random(20261109)
        for _ in range(2000):
            p = "-".join("".join(rng.choice(alphabet) for _ in range(4)) for _ in range(4))
            self.assertIsNone(snapshot.check_passphrase(p), p)
        for p in ("abcd-k7mq-x2pd-9wtr", "k7mq-2345-x2pd-wxyz", "ghjk-x2pd-9wtr-5432",
                  "k7ab-cdmq-x2pd-9wtr",   # a run across a dash
                  "k7mq-aaa2-x2pd-9wtr", "x2x2-k7mq-9wtr-h4nb"):
            with self.subTest(p=p):
                self.assertTrue(snapshot._strength(p)["hits"], "the run / repeat is still noticed")
                self.assertIsNone(snapshot.check_passphrase(p))

    def test_long_random_lowercase_accepted(self):
        # Holds a reversed keyboard run ("jhgfds"); 14 random letters remain.
        self.assertIsNone(snapshot.check_passphrase("xkqvbnwtrplmzjhgfdsy"))
        self.assertEqual(snapshot._strength("xkqvbnwtrplmzjhgfdsy")["rest"], "xkqvbnwtrplmzy")

    def test_strength_check_is_fast_on_long_input(self):
        import random
        import time
        rng = random.Random(5)
        samples = ["password" * 128, "a1b2c3" * 170 + "zz", ("abcd-" * 205)[:1024],
                   "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789!@#") for _ in range(1024)),
                   "".join(w + "x7#" for w in ["password", "qwerty", "dragon", "monkey"] * 30)[:1024]]
        for p in samples:
            t0 = time.monotonic()
            snapshot.check_passphrase(p)
            self.assertLess(time.monotonic() - t0, 0.5, p[:30])
        self.assertIsNotNone(snapshot.check_passphrase("password" * 128))

    def test_problems_are_plain_english(self):
        cases = {
            None: "Enter a passphrase.",
            "": "Enter a passphrase.",
            "            ": "only spaces",
            "short one": "at least 12 characters (this one has 9)",
            " harbor candle eleven": "start or end",
            "harbor candle eleven ": "start or end",
            "harbor\ncandle eleven": "Line breaks",
            "harbor​candle eleven": "invisible",
            "aaaaaaaaaaaaaaaa": "variety",
            "abababababababab": "variety",
            "x" * 1025: "under 1,024",
        }
        for value, expected in cases.items():
            with self.subTest(value=repr(value)[:30]):
                problem = snapshot.check_passphrase(value)
                self.assertIsNotNone(problem)
                self.assertIn(expected, problem)

    def test_exactly_min_length_passes(self):
        # 12 characters with a mixed pool clears the strength floor; a
        # 12-letter alphabet run (Session 25 review) is refused.
        self.assertIsNone(snapshot.check_passphrase("Tq7v-Lm2x!Zp"))
        self.assertIsNotNone(snapshot.check_passphrase("Tq7v-Lm2x!Z"))
        self.assertIsNotNone(snapshot.check_passphrase("abcdefghijkl"))

    def test_guessable_passphrases_refused(self):
        for weak in ("password1234", "qwertyuiop12", "123456789012", "abcdefghijkl", "repodoctor12",
                     "zyxwvutsrqpo", "letmein!2024", "aaaaaaaaaaaaaaa", "mysnapshot99"):
            self.assertIsNotNone(snapshot.check_passphrase(weak), weak)
        for ok in ("harbor candle eleven violet", "k7mp-x2qr-9tvz-h4wn", "blue tiger lamp river",
                   "MyDog Has 2 Fleas!", "Tr0ub4dor&3xyz"):
            self.assertIsNone(snapshot.check_passphrase(ok), ok)

    def test_preview_shell_never_offers_remember(self):
        p = snapshot.encrypt("<p>x</p>", PASS)
        self.assertIn('id="rd-remember"', snapshot.build_shell(p, "now"))
        preview = snapshot.build_shell(p, "now", allow_remember=False)
        self.assertNotIn('id="rd-remember"', preview)
        self.assertIn("nothing is remembered here", preview)

    def test_preview_hint_lines_up_with_the_input(self):
        """The preview hint has no checkbox to sit beside: no 30px indent, and
        a gap above so it doesn't touch the input (Session 25 review)."""
        preview = snapshot.build_shell(snapshot.encrypt("<p>x</p>", PASS), "now", allow_remember=False)
        self.assertIn('<p class="rd-hint rd-hint-solo">Preview on your Mac', preview)
        css = snapshot._SHELL_CSS
        self.assertIn(".rd-hint-solo { margin: 10px 0 16px; }", css)
        self.assertGreater(css.index(".rd-hint-solo"), css.index(".rd-hint {"))  # wins the cascade
        # The Remember hint keeps its indent under the checkbox label.
        self.assertIn('<p class="rd-hint">Skips this screen', snapshot.build_shell(snapshot.encrypt("x", PASS), "now"))

    def test_typed_passphrase_is_trimmed_and_nfc_normalized(self):
        """Setup refuses a passphrase with a leading / trailing space, so the
        phone trims what's typed (a stray space from the keyboard used to fail
        with "didn't unlock") and NFC-normalizes it like the Mac does."""
        script = snapshot.SHELL_SCRIPT
        clean = script[script.index("function clean(pass)"):]
        clean = clean[:clean.index("\n  }\n")]
        self.assertLess(clean.index('s.normalize("NFC")'), clean.index("s.trim()"))
        self.assertIn("var pass = clean(input.value);", script)
        self.assertIn("new TextEncoder().encode(clean(pass))", script)
        self.assertIsNotNone(snapshot.check_passphrase(PASS + " "))
        script.encode("ascii")  # the CSP hash is over these exact bytes

    def test_remember_stores_a_key_object_not_the_passphrase(self):
        script = snapshot.SHELL_SCRIPT
        self.assertIn("indexedDB", script)
        self.assertIn('importKey("raw", secret, "PBKDF2", false', script)  # non-extractable
        self.assertNotIn("setItem", script)  # the passphrase is never written to localStorage
        self.assertIn('autocomplete="off"', snapshot.build_shell(snapshot.encrypt("x", PASS), "now"))


# ----------------------------------------------------------------------
# Shell + published files
# ----------------------------------------------------------------------

def _published(data=None, adversarial=True):
    data = data if data is not None else realistic_data(adversarial=adversarial)
    body = snapshot.render_body(data)
    payload = snapshot.encrypt(body, PASS)
    shell = snapshot.build_shell(payload, data.get("built_label", ""))
    return body, payload, shell, snapshot.site_files(shell)


class TestShell(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.body, cls.payload, cls.shell, cls.files = _published()

    def test_body_really_contains_the_secrets(self):
        # Guard for the next test: the fixture strings are in the plaintext.
        for s in (REPO_NAMES[0], COMMIT_TEXT, LONG_TEST_NAME, AI_REPO_TEXT, str(markupsafe.escape(XSS))):
            self.assertIn(s, self.body)

    def test_published_files_contain_no_data(self):
        self.assertEqual(sorted(self.files), ["_headers", "index.html", "robots.txt"])
        for name, content in self.files.items():
            text = content.decode("utf-8")
            for secret in secret_strings():
                for form in {secret, html.escape(secret), str(markupsafe.escape(secret)),
                             json.dumps(secret)[1:-1]}:
                    with self.subTest(file=name, secret=secret[:40]):
                        self.assertNotIn(form, text)
        self.assertNotIn(snapshot.BODY_MARKER, self.files["index.html"].decode())

    def test_shell_is_ascii_generic_and_carries_the_payload(self):
        page = self.files["index.html"].decode("ascii")  # ASCII only: no encoding ambiguity
        self.assertIn("<title>RepoDoctor snapshot</title>", page)
        self.assertIn("Built Nov 09, 2026 12:00 PM CST", page)
        m = re.search(r'<script type="application/json" id="rd-payload">(.*?)</script>', page, re.S)
        embedded = json.loads(m.group(1))
        self.assertEqual(embedded, self.payload)
        self.assertEqual(snapshot.decrypt(embedded, PASS), self.body)

    def test_built_label_is_escaped_and_cleaned(self):
        shell = snapshot.build_shell(self.payload, '<b onclick="x">Nov 9</b>\n\x00')
        self.assertIn("&lt;b onclick=&quot;x&quot;&gt;Nov 9&lt;/b&gt;", shell)
        self.assertNotIn("<b onclick", shell)
        self.assertIn("Built recently", snapshot.build_shell(self.payload, ""))

    def test_build_shell_refuses_anything_but_ciphertext(self):
        for bad in ({**self.payload, "body": "<p>plaintext</p>"}, {**self.payload, "ct": XSS},
                    {"v": 1}, "not a dict"):
            with self.subTest(bad=str(bad)[:50]):
                with self.assertRaises(ValueError):
                    snapshot.build_shell(bad, "Nov 9")

    def test_headers_hash_matches_the_script_in_index(self):
        page = self.files["index.html"].decode()
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
        self.assertEqual(len(scripts), 1, "exactly one executable inline script")
        digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
        self.assertEqual(scripts[0], snapshot.SHELL_SCRIPT)
        headers = self.files["_headers"].decode()
        self.assertIn(f"script-src 'sha256-{digest}';", headers)
        # The page's own <meta> policy pins the same hash.
        meta = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page).group(1)
        self.assertIn(f"script-src &#x27;sha256-{digest}&#x27;", meta)
        self.assertNotIn("frame-ancestors", meta)  # ignored (and warned about) in <meta>

    def test_headers_file_content(self):
        lines = self.files["_headers"].decode().splitlines()
        self.assertEqual(lines[0], "/*")
        rules = dict(line.strip().split(": ", 1) for line in lines[1:])
        csp = rules["Content-Security-Policy"]
        directives = [d.strip() for d in csp.split(";")]
        self.assertEqual(directives[0], "default-src 'none'")
        self.assertRegex(directives[1], r"^script-src 'sha256-[A-Za-z0-9+/]{43}='$")
        self.assertEqual(directives[2:], ["style-src 'unsafe-inline'", "img-src data:", "connect-src 'none'",
                                          "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'"])
        self.assertEqual(rules["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(rules["Referrer-Policy"], "no-referrer")
        self.assertEqual(rules["X-Content-Type-Options"], "nosniff")
        self.assertEqual(rules["Cache-Control"], "no-store")

    def test_robots(self):
        self.assertEqual(self.files["robots.txt"].decode(), "User-agent: *\nDisallow: /\n")

    def test_script_hash_follows_the_actual_page(self):
        """site_files hashes what's really in the page: an edited script gets
        its own hash, so a stale constant can't silently break the unlock."""
        edited = self.shell.replace('"use strict";', '"use strict"; ')
        self.assertNotEqual(edited, self.shell)
        files = snapshot.site_files(edited)
        script = re.search(r"<script>(.*?)</script>", edited, re.S).group(1)
        self.assertIn(snapshot.script_hash(script), files["_headers"].decode())
        self.assertNotIn(snapshot.script_hash(snapshot.SHELL_SCRIPT), files["_headers"].decode())

    def test_site_files_refuses_plaintext_or_scriptless_pages(self):
        with self.assertRaises(ValueError):
            snapshot.site_files(self.body)  # a body passed as the shell
        with self.assertRaises(ValueError):
            snapshot.site_files("<html><body>no script</body></html>")
        with self.assertRaises(ValueError):
            snapshot.site_files("")

    def test_zip(self):
        blob = snapshot.build_zip(self.files)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            self.assertEqual(sorted(zf.namelist()), ["_headers", "index.html", "robots.txt"])
            for name in zf.namelist():
                self.assertEqual(zf.read(name), self.files[name])
        self.assertEqual(blob, snapshot.build_zip(self.files))  # deterministic
        for bad in ("../index.html", "dir/index.html", "/abs", ""):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    snapshot.build_zip({bad: b"x"})

    def test_build_site_end_to_end_and_logs(self):
        data = realistic_data()
        files = snapshot.build_site(data, PASS)
        page = files["index.html"].decode()
        payload = json.loads(re.search(r'id="rd-payload">(.*?)</script>', page, re.S).group(1))
        body = snapshot.decrypt(payload, PASS)
        self.assertIn(REPO_NAMES[0], body)
        log = snapshot.tail_snapshot_log(20)
        events = [e["event"] for e in log]
        self.assertIn("render", events)
        self.assertIn("build", events)
        raw_log = json.dumps(log)
        self.assertNotIn(PASS, raw_log)
        for secret in (REPO_NAMES[0], COMMIT_TEXT, AI_REPO_TEXT):
            self.assertNotIn(secret, raw_log)
        build = [e for e in log if e["event"] == "build"][-1]
        self.assertEqual(build["iterations"], 600000)
        self.assertEqual(set(build["files"]), {"index.html", "robots.txt", "_headers"})

    def test_build_site_refuses_a_missing_passphrase_and_logs_it(self):
        with self.assertRaises(ValueError):
            snapshot.build_site(realistic_data(), "")
        last = snapshot.tail_snapshot_log(1)[-1]
        self.assertEqual(last["event"], "build_refused")
        self.assertEqual(last["reason"], "no_passphrase")
        self.assertNotIn("problem", last)
        # A passphrase saved under older, looser rules still builds.
        files = snapshot.build_site(realistic_data(), "monkey banana tree")
        self.assertIn("index.html", files)


# ----------------------------------------------------------------------
# Body
# ----------------------------------------------------------------------

class TestBody(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = realistic_data()
        cls.page = snapshot.render_body(cls.data)
        cls.tags = parse(cls.page)

    def section(self, sid):
        start = self.page.index(f'id="{sid}"')
        end = self.page.find('<section class="sn-section"', start + 10)
        return self.page[start:end if end != -1 else len(self.page)]

    def test_every_section_in_order(self):
        order = [self.page.index(s) for s in ("Snapshot built Nov 09, 2026 12:00 PM CST",
                                               'id="sn-next"', 'id="sn-review"', 'id="sn-changed"',
                                               'id="sn-portfolio"', "Read-only snapshot. Published from "
                                               "RepoDoctor on your Mac.")]
        self.assertEqual(order, sorted(order))
        for href in ("#sn-next", "#sn-review", "#sn-changed", "#sn-portfolio"):
            self.assertIn(f'href="{href}"', self.page)
        self.assertNotIn("<script", self.page)

    def test_freshness_lines(self):
        self.assertIn("Branches and PRs read Nov 09, 2026 10:00 AM CST.", self.page)
        self.assertIn("Commits read Nov 07, 2026 12:00 PM CST (2 days before this snapshot)", self.page)

    def test_do_next_grouped_by_tier_in_order(self):
        sec = self.section("sn-next")
        labels = re.findall(r'<h3 class="sn-h3[^"]*"[^>]*><span class="sn-chip sn-chip-(\w+)">([^<]+)</span>', sec)
        self.assertEqual([t for t, _ in labels],
                         ["merge", "finish", "deploy", "p0", "open_pr", "decide", "planned", "cleanup"])
        self.assertEqual(labels[2][1], "CONFIRM LIVE")
        self.assertIn("PR #41", sec)
        self.assertIn('href="https://github.com/o/zebra-parish-app/pull/41"', sec)
        self.assertIn("3 days ago", sec)
        # App-internal links (TRACKER, IN FLIGHT) can't work on the phone.
        self.assertNotIn("/tracker/", sec)
        self.assertNotIn("/inflight", sec)
        self.assertIn("1 lower-priority (P2/P3) tracker action in Tracker.", sec)

    def test_tier_tally_jumps_to_groups_with_unique_ids(self):
        sec = self.section("sn-next")
        self.assertIn('<a class="sn-chip sn-chip-merge" href="#sn-tier-merge">MERGE<strong>1</strong></a>', sec)
        self.assertEqual(sec.count('id="sn-tier-merge"'), 1)
        # Items out of tier order (a caller bug) still give one id per tier, and
        # a tier past the 40-item cap is a plain chip, not a dead link.
        items = ([{"tier": "merge", "label": "MERGE", "title": "a"}, {"tier": "p0", "label": "P0", "title": "b"},
                  {"tier": "merge", "label": "MERGE", "title": "c"}]
                 + [{"tier": "p1", "label": "P1", "title": f"x{i}"} for i in range(40)]
                 + [{"tier": "blocked", "label": "BLOCKED", "title": "late"}])
        page = snapshot.render_body({"do_next": {"items": items}})
        ids = re.findall(r'id="(sn-tier-[a-z0-9_]+)"', page)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, ["sn-tier-merge", "sn-tier-p0", "sn-tier-p1"])
        self.assertIn('<span class="sn-chip sn-chip-blocked">BLOCKED<strong>1</strong></span>', page)
        self.assertNotIn('href="#sn-tier-blocked"', page)

    def test_range_kpi_wording(self):
        sec = self.section("sn-changed")
        self.assertIn("<strong>2 of 5 repos read were updated</strong> (1 couldn't be read) · 4 commits · "
                      "1 PR merged · active on 4 of 7 days", sec)
        page = snapshot.render_body({"ranges": [{"key": "week", "name": "Last 7 days", "totals": {
            "repos_total": 1, "repos_updated": 1, "commits": 1, "prs_merged": 0, "active_days": 1, "days": 7}}]})
        self.assertIn("<strong>1 of 1 repo updated</strong> · 1 commit · 0 PRs merged · active on 1 of 7 days", page)

    def test_copy_prompt_round_trips_exactly(self):
        copies = [a["data-copy"] for t, a in self.tags if "data-copy" in a]
        prompts = [x["prompt"] for x in self.data["do_next"]["items"] if x["prompt"]]
        for prompt in prompts:
            self.assertIn(prompt, copies)
        tracker_prompt = next(p for p in prompts if p.startswith("In zebra-parish-app, fix"))
        self.assertIn('"login" on <phones>.\nThen', tracker_prompt)
        for t, a in self.tags:
            if "data-copy" in a:
                self.assertEqual(t, "button")
                self.assertIn("js-copy", a["class"])

    def test_session_links_only_canonical(self):
        hrefs = [a["href"] for t, a in self.tags if t == "a" and "href" in a]
        sessions = [h for h in hrefs if "session_" in h]
        self.assertTrue(sessions)
        for h in sessions:
            self.assertRegex(h, r"^https://claude\.ai/code/session_[A-Za-z0-9_-]{1,128}$")
        self.assertIn(GOOD_SESSION, sessions)
        self.assertIn(GOOD_SESSION_2, sessions)  # the stale branch in Gone quiet
        for bad in BAD_SESSIONS:
            self.assertNotIn(html.escape(bad), self.page)
        self.assertNotIn("evil.example", self.page)

    def test_every_link_is_https_or_in_page_and_opens_safely(self):
        for t, a in self.tags:
            if t != "a":
                continue
            href = a.get("href", "")
            self.assertTrue(href.startswith("#") or href.startswith("https://"), href)
            if href.startswith("https://"):
                self.assertEqual(a.get("target"), "_blank")
                self.assertEqual(a.get("rel"), "noopener noreferrer")
        self.assertNotIn("javascript:", self.page)

    def test_weekly_review(self):
        sec = self.section("sn-review")
        self.assertIn(self.data["review_week_label"], sec)
        self.assertIn(AI_REPO_TEXT, sec)
        self.assertIn(AI_OVERVIEW, sec)
        self.assertIn("STALE", sec)
        self.assertIn("GitHub couldn't be read for ocelot-grants", sec)
        self.assertIn("Waiting on you", sec)
        self.assertIn("Gone quiet", sec)
        self.assertIn("no activity for 40 days", sec)  # marmot: quiet repo
        self.assertIn("old/experiment", sec)             # marmot: stale branch
        self.assertIn("Next up", sec)
        self.assertIn("Repos updated", sec)
        self.assertNotIn("Nothing landed", sec)

    def test_what_changed_week_open_others_collapsed(self):
        sec = self.section("sn-changed")
        details = re.findall(r'<details class="sn-range"( open)?>\s*<summary>\s*<span class="sn-range-name">([^<]+)<',
                             sec)
        self.assertEqual([n for _, n in details], ["Last 7 days", "Last 30 days", "Last 90 days", "Year to date"])
        self.assertEqual([bool(o) for o, _ in details], [True, False, False, False])
        self.assertIn("couldn't be read for ocelot-grants", sec)
        self.assertIn("active on", sec)
        week = sec[:sec.index("Last 30 days")]
        self.assertIn(AI_REPO_TEXT, week)  # AI paragraph replaces the titles
        month = sec[sec.index("Last 30 days"):sec.index("Last 90 days")]
        # Month has no AI: PR titles first, then commits, 3 at most, then +N more.
        z = month[month.index("zebra-parish-app"):]
        titles = re.findall(r"<li>([^<]+)</li>", z[:z.index("</ul>")])
        self.assertEqual(titles[0], f"#41 {PR_TITLE}")
        self.assertEqual(len(titles), 3)
        self.assertIn('class="sn-more-items">+', z)

    def test_portfolio(self):
        sec = self.section("sn-portfolio")
        self.assertIn('<h3 class="sn-theme">Church', sec)
        self.assertIn('<h3 class="sn-theme">School', sec)
        self.assertIn('class="sn-grade sn-grade-good"', sec)   # tone "ok" -> green
        self.assertIn('class="sn-grade sn-grade-bad"', sec)
        self.assertIn('class="sn-grade sn-grade-warn"', sec)
        self.assertIn('class="sn-grade sn-grade-none"', sec)
        self.assertIn("<details class=\"sn-pf-row\">", sec)
        self.assertIn("Cleaning up branches alone lifts it to 92 (A).", sec)
        self.assertIn("3 open branches", sec)
        self.assertIn(ONE_LINER, sec)
        self.assertNotIn("javascript:", sec)  # marmot's html_url was hostile: no GITHUB link

    def test_empty_and_missing_data_never_crash(self):
        for data in ({}, None, [], "nope", {"do_next": None, "review": None, "ranges": None,
                                              "portfolio": None}):
            with self.subTest(data=repr(data)):
                page = snapshot.render_body(data)
                self.assertIn("Do next wasn't included in this snapshot.", page)
                self.assertIn("The Weekly review wasn't included in this snapshot.", page)
                self.assertIn("What changed wasn't included in this snapshot.", page)
                self.assertIn("The Portfolio wasn't included in this snapshot.", page)
                self.assertIn("Branches and PRs weren't read from GitHub", page)
                self.assertIn("Commits weren't read from GitHub", page)
                self.assertIn("Snapshot built ", page)

    def test_sparse_data_shows_sensible_empty_states(self):
        dn = nextup.build_do_next([], {}, {}, [], now=NOW)
        review = nextup.build_review(None, None, dn, [], [], now=NOW)
        page = snapshot.render_body({"do_next": dn, "review": review, "ranges": [], "portfolio": [],
                                     "inflight_gathered_at": iso(0)})
        self.assertIn("Nothing waiting. Every branch is merged", page)
        self.assertIn("Commits weren't read from GitHub for this snapshot, so what shipped", page)
        self.assertIn("Nothing to merge, finish, or confirm.", page)
        self.assertIn("Needs a GitHub read", page)
        self.assertIn("Nothing queued.", page)
        self.assertIn("No repos in the Portfolio yet.", page)

    def test_week_read_with_nothing_shipped_vs_failed(self):
        def page_for(failed):
            rv = {"week": {"totals": {"repos_total": 3, "repos_updated": 0, "errors": len(failed),
                                      "read": 3 - len(failed), "days": 7}},
                  "shipped": [], "failed": failed, "waiting": [], "quiet": [], "stale": [], "next": []}
            return snapshot.render_body({"review": rv})
        self.assertIn("Nothing landed on a default branch this week.", page_for([]))
        failed_page = page_for([{"name": "ocelot-grants"}])
        self.assertNotIn("Nothing landed", failed_page)
        self.assertIn("isn't a claim that nothing shipped there", failed_page)
        self.assertIn("ocelot-grants", failed_page)

    def test_odd_types_in_every_field(self):
        data = {
            "built_label": 12345, "inflight_gathered_at": "not a date", "activity_fetched_at": 7,
            "do_next": {"items": ["str", None, 3, {"tier": None, "title": ["x"], "links": "x",
                                                  "session_url": 5, "prompt": None, "age_days": "9"}]},
            "review": {"week": {"totals": "bad"}, "shipped": [None, {"titles": "abc", "ai": "x"}],
                       "failed": "ocelot", "waiting": {}, "quiet": [{"days": "x"}], "stale": [1],
                       "next": None, "next_total": -4},
            "ranges": [None, {"key": "<b>", "totals": None}, {"updated": [{"prs": [None, {"title": 5}]}]}],
            "portfolio": [None, {"tag": None, "rows": [None, {"grade": "ABCDEF", "tone": "<x>"}]}],
        }
        page = snapshot.render_body(data)
        self.assertIn("Snapshot built 12345", page)
        self.assertIn('class="sn-grade sn-grade-none"', page)
        self.assertNotIn("None", re.sub(r"<style>.*?</style>", "", page, flags=re.S))

    def test_hostile_text_is_escaped(self):
        page = snapshot.render_body(realistic_data(adversarial=True))
        tags = parse(page)
        self.assertNotIn(XSS, page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<script", page)
        self.assertIn(str(markupsafe.escape(XSS)), page)
        self.assertEqual({t for t, _ in tags} - {"div", "header", "h1", "h2", "h3", "ul", "ol", "li", "nav",
                                                  "a", "section", "p", "span", "strong", "button", "details",
                                                  "summary", "footer", "style"}, set())
        for t, a in tags:
            for name in a:
                self.assertFalse(name.startswith("on"), f"event handler attribute {name} on <{t}>")
        self.assertNotIn("‮", page)  # direction overrides stripped
        self.assertIn(RTL, page)
        self.assertIn(LONG_WORD, page)

    def test_tampered_session_urls_never_become_links(self):
        items = [{"tier": "merge", "label": "MERGE", "repo": "r", "title": "t", "session_url": bad}
                 for bad in BAD_SESSIONS + [GOOD_SESSION + "\n", " " + GOOD_SESSION, GOOD_SESSION.upper()]]
        stale = [{"repo": "r", "branch": "b", "session_url": bad} for bad in BAD_SESSIONS]
        page = snapshot.render_body({"do_next": {"items": items},
                                     "review": {"stale": stale, "week": None, "waiting": items}})
        self.assertNotIn("OPEN SESSION", page)
        self.assertNotIn("session_", page)

    def test_caps(self):
        many = [{"tier": "open_pr", "label": "OPEN A PR", "repo": f"repo{i}", "title": f"t{i}"}
                for i in range(60)]
        updated = [{"name": f"repo{i}", "commit_count": 5, "pr_count": 1,
                    "prs": [{"number": i, "title": f"pr {i}"}],
                    "commits": [{"summary": f"c{i}-{j}"} for j in range(5)]} for i in range(30)]
        totals = {"repos_total": 40, "repos_updated": 30, "commits": 150, "prs_merged": 30,
                  "active_days": 5, "days": 7, "errors": 0}
        page = snapshot.render_body({
            "do_next": {"items": many},
            "ranges": [{"key": "week", "name": "Last 7 days", "dates": "Nov 3 - Nov 9, 2026",
                        "totals": totals, "updated": updated, "failed": []}],
        })
        self.assertEqual(page.count('<li class="sn-item'), 40)
        self.assertIn("+20 more on your Mac.", page)
        self.assertEqual(page.count('<li class="sn-card">'), 25)
        self.assertIn("+5 more repos on your Mac.", page)
        self.assertIn('<li class="sn-more-items">+3 more</li>', page)  # 6 items, 3 shown

    def test_stale_freshness_is_flagged(self):
        page = snapshot.render_body({"inflight_gathered_at": "2020-01-01T00:00:00Z"})
        self.assertRegex(page, r'<li class="is-stale">Branches and PRs read Dec 31, 2019 6:00 PM CST '
                               r'\([\d,]+ days before this snapshot\)\.</li>')

    def test_central_time_filter(self):
        self.assertEqual(snapshot.central_time("2026-11-09T18:00:00Z"), "Nov 09, 2026 12:00 PM CST")
        self.assertEqual(snapshot.central_time("2026-07-04T17:05:00+00:00"), "Jul 04, 2026 12:05 PM CDT")
        self.assertEqual(snapshot.central_time("garbage"), "")
        self.assertEqual(snapshot.central_date("2026-11-03T18:00:00Z"), "Nov 3, 2026")
        # The built label carries seconds (see test_make_built_label).
        self.assertEqual(snapshot.make_built_label(NOW), "Nov 09, 2026 12:00:00 PM CST")

    def test_make_built_label(self):
        """To the second, so two publishes in the same minute look different
        on the phone. Takes the aware UTC datetime app.py builds."""
        at = datetime.datetime(2026, 11, 9, 20, 16, 5, tzinfo=UTC)
        self.assertEqual(snapshot.make_built_label(at), "Nov 09, 2026 2:16:05 PM CST")
        self.assertNotEqual(snapshot.make_built_label(at),
                            snapshot.make_built_label(at + datetime.timedelta(seconds=30)))
        self.assertEqual(snapshot.make_built_label(datetime.datetime(2026, 7, 4, 4, 0, 9, tzinfo=UTC)),
                         "Jul 03, 2026 11:00:09 PM CDT")
        self.assertEqual(snapshot.make_built_label(at.replace(tzinfo=None)), "Nov 09, 2026 2:16:05 PM CST")
        self.assertEqual(snapshot.make_built_label(at.isoformat()), "Nov 09, 2026 2:16:05 PM CST")
        label_re = r"^[A-Z][a-z]{2} \d{2}, \d{4} \d{1,2}:\d{2}:\d{2} [AP]M C[SD]T$"
        self.assertRegex(snapshot.make_built_label(), label_re)
        self.assertRegex(snapshot.make_built_label("garbage"), label_re)  # never blank
        self.assertLessEqual(len(snapshot.make_built_label(at)), snapshot.LABEL_MAX_CHARS)

    def test_built_label_reaches_lock_screen_and_page(self):
        data = {k: v for k, v in realistic_data().items() if k != "built_label"}
        page = snapshot.build_site(data, PASS)["index.html"].decode()
        label = re.search(r'<p class="rd-built">Built ([^<]+)</p>', page).group(1)
        self.assertRegex(label, r"^[A-Z][a-z]{2} \d{2}, \d{4} \d{1,2}:\d{2}:\d{2} [AP]M C[SD]T$")
        payload = json.loads(re.search(r'id="rd-payload">(.*?)</script>', page, re.S).group(1))
        self.assertIn(f"Snapshot built {label}", snapshot.decrypt(payload, PASS))
        # The app's own label (make_built_label(built_at)) is used as given.
        at = datetime.datetime(2026, 11, 9, 20, 16, 5, tzinfo=UTC)
        page = snapshot.build_site({**data, "built_label": snapshot.make_built_label(at)}, PASS)["index.html"].decode()
        self.assertIn("Built Nov 09, 2026 2:16:05 PM CST", page)

    def test_tap_targets_are_at_least_44px(self):
        """Every tappable thing on the phone page: the shell's controls and the
        body's links, buttons, chips and expanders (Session 25 review)."""
        body_css = re.search(r"<style>(.*?)</style>", snapshot.render_body({}), re.S).group(1)

        def min_height(css, selector):
            m = re.search(r"(?:^|\n)" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
            self.assertIsNotNone(m, selector)
            return int(re.search(r"min-height:\s*(\d+)px", m.group(1)).group(1))

        for selector in (".rd-input", ".rd-remember", ".rd-btn", ".rd-forget"):
            with self.subTest(selector=selector):
                self.assertGreaterEqual(min_height(snapshot._SHELL_CSS, selector), 44)
        for selector in (".sn-nav a", ".sn .sn-tally a.sn-chip", ".sn-btn", ".sn-range > summary",
                         ".sn-pf-row > summary", ".sn .sn-more a"):
            with self.subTest(selector=selector):
                self.assertGreaterEqual(min_height(body_css, selector), 44)
        # Plain (non-link) tally chips keep their size next to the 44px ones.
        self.assertRegex(body_css, r"\.sn-tally \{[^}]*align-items: center")


if __name__ == "__main__":
    unittest.main()
