"""
Phone snapshot: an encrypted, read-only copy of the dashboard for the phone.

RepoDoctor runs only on the owner's Mac. This module turns Do next, the
Weekly review, What changed (Week / Month / Quarter / YTD) and the Portfolio
into one read-only page, ENCRYPTS it with a passphrase only the owner knows,
and builds the files the uploader publishes (to Netlify). The published files
only ever hold ciphertext:

  index.html  a generic shell: the title "RepoDoctor snapshot", the build
              time, a passphrase box, the encrypted payload (base64) and ONE
              inline script that decrypts it with WebCrypto in the browser
  robots.txt  disallow everything
  _headers    Netlify headers: a strict Content-Security-Policy whose only
              allowed script is the shell's (pinned by its SHA-256 hash),
              noindex, no-referrer, nosniff, no-store

Crypto: PBKDF2-HMAC-SHA256 (600,000 iterations, a 16-byte random salt)
derives a 256-bit key; AES-256-GCM with a 12-byte random nonce encrypts the
UTF-8 HTML. The ciphertext ends with GCM's 16-byte tag, exactly the layout
WebCrypto's AES-GCM decrypt expects. Salt and nonce are fresh on every call,
so two snapshots of the same data never share bytes. The passphrase is
Unicode-normalized (NFC) on both sides, so an accented character typed on a
phone matches the one typed on the Mac, and the phone trims what's typed
(Settings refuses a passphrase that starts or ends with a space).

Flow for the integrator:
    files = build_site(data, passphrase)        # render + encrypt + shell
    zip_bytes = build_zip(files)                # or upload files one by one
or step by step: render_body(data) -> encrypt() -> build_shell() -> site_files().

The passphrase and the plaintext are never logged, stored, or written to any
published file. Every build is logged (sizes, counts, timings, the script
hash) to data/logs/snapshot.log, one JSON object per line, for paste-back
debugging, and through the root logger to data/logs/app.log.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import html
import io
import json
import logging
import math
import os
import re
import time
import unicodedata
import urllib.parse
import zipfile

import jinja2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from sessions import _LOCAL_TZ, safe_session_url, strip_bidi

logger = logging.getLogger(__name__)

# --- Crypto parameters (the shell's script reads them from the payload) ------
FORMAT_VERSION = 1
KDF_NAME = "PBKDF2-SHA256"
KDF_ITERATIONS = 600_000
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
TAG_BYTES = 16
PAYLOAD_KEYS = ("v", "kdf", "iter", "salt", "iv", "ct")
# decrypt() refuses an "iter" outside this range, so a tampered payload can't
# make the Mac (or a test) grind for minutes.
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 10_000_000

# --- Passphrase rules -------------------------------------------------------
MIN_PASSPHRASE_LEN = 12
MAX_PASSPHRASE_LEN = 1024
MIN_DISTINCT_CHARS = 5

# --- Page limits (phone-sized) -----------------------------------------------
DO_NEXT_LIMIT = 40          # Do next rows before "+N more on your Mac"
RANGE_REPO_LIMIT = 25       # repos per What changed range / shipped list
RANGE_TITLES = 3            # PR / commit titles per repo when there's no AI paragraph
LABEL_MAX_CHARS = 80        # built label (a date/time string)

SHELL_TITLE = "RepoDoctor snapshot"
WRONG_PASSPHRASE = "That passphrase didn't unlock this snapshot."
# Present in every rendered body, never in a published file: site_files()
# refuses to publish a page that contains it (a body passed as the shell).
BODY_MARKER = "rd-snapshot-body"

SITE_FILE_NAMES = ("index.html", "robots.txt", "_headers")
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "snapshot")


# ----------------------------------------------------------------------
# Logging (data/logs/snapshot.log; never the passphrase or any data text)
# ----------------------------------------------------------------------

def _log_path() -> str:
    import models  # lazy: keeps this module importable without the app's data dir
    # One snapshot log for the app and this module (models.SNAPSHOT_LOG_PATH).
    return getattr(models, "SNAPSHOT_LOG_PATH", None) or os.path.join(models.DATA_DIR, "logs", "snapshot.log")


def log_snapshot_event(event: str, **fields) -> None:
    """Append one JSON line to data/logs/snapshot.log. Never raises."""
    try:
        import models
        models._append_log_event(_log_path(), event, fields)
    except Exception as e:  # noqa: BLE001 - logging must never break a build
        logger.warning("snapshot: log write failed: %s", e)


def tail_snapshot_log(n: int = 100) -> list[dict]:
    try:
        import models
        return models._tail_log(_log_path(), n)
    except Exception as e:  # noqa: BLE001
        logger.warning("snapshot: log read failed: %s", e)
        return []


# ----------------------------------------------------------------------
# Passphrase + encryption
# ----------------------------------------------------------------------

def _normalize(passphrase: str) -> str:
    return unicodedata.normalize("NFC", passphrase)


def check_passphrase(passphrase) -> str | None:
    """A plain-English problem with `passphrase`, or None when it's usable."""
    if not isinstance(passphrase, str) or not passphrase:
        return "Enter a passphrase."
    p = _normalize(passphrase)
    if not p.strip():
        return "A passphrase can't be only spaces."
    if p != p.strip():
        return ("Remove the space at the start or end. It's easy to miss when you "
                "type it on your phone.")
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn") for ch in p):
        return ("Use letters, numbers, spaces and punctuation only. Line breaks, tabs "
                "and invisible characters can't be typed on a phone.")
    if len(p) < MIN_PASSPHRASE_LEN:
        return (f"Use at least {MIN_PASSPHRASE_LEN} characters (this one has {len(p)}). "
                f"Four random words is easy to type and hard to guess.")
    if len(p) > MAX_PASSPHRASE_LEN:
        return f"Keep it under {MAX_PASSPHRASE_LEN:,} characters."
    if len(set(p.lower())) < MIN_DISTINCT_CHARS:
        return _REPEAT_MSG
    # The encrypted file is public, so it can be guessed at offline. Score
    # only what's left after the guesses that come first (repeats, common
    # words, keyboard / alphabet / digit runs, recent years) are taken out.
    strength = _strength(p)
    if strength["bits"] >= MIN_PASSPHRASE_BITS:
        return None
    main = strength["main"]
    if main == "word":
        return _WORD_MSG.format(strength["word"])
    if main == "run":
        return _RUN_MSG
    if main == "repeat":
        return _REPEAT_MSG
    return _WEAK_MSG


# Strength floor, in estimated bits of what's left once the guessable pieces
# are stripped. Four random words or SUGGEST's 4x4 groups clear it with room
# to spare; 12 lowercase letters alone (56 bits) don't.
MIN_PASSPHRASE_BITS = 60
_WORD_MSG = "That's built on \"{}\", one of the first things anyone guesses. Pick something else."
_RUN_MSG = "That's mostly a keyboard row or a sequence, which is guessed first. Pick something else."
_REPEAT_MSG = "Use more variety. That one repeats the same few characters."
_WEAK_MSG = ("That's too easy to guess for a file anyone can download. Make it longer, or mix in "
             "capitals, numbers or symbols. Four random words, or SUGGEST in Settings, works well.")
_COMMON = ("password", "passw0rd", "qwerty", "letmein", "welcome", "admin", "iloveyou",
           "monkey", "dragon", "football", "baseball", "sunshine", "princess", "secret",
           "repodoctor", "snapshot", "netlify", "github", "claude", "changeme", "trustno1",
           "abc123", "111111", "superman", "batman", "master", "hello", "freedom", "whatever",
           "qazwsx")
_COMMON_LONGEST_FIRST = tuple(sorted(set(_COMMON), key=lambda w: (-len(w), w)))
# Keyboard rows / columns, the alphabet and the digits. A run is 4+ characters
# in a row along one of these, forward or backward ("7890123" wraps).
_RUNS = ("abcdefghijklmnopqrstuvwxyz", "01234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm",
         "1qaz2wsx3edc4rfv5tgb", "qazwsxedcrfvtgb", "!@#$%^&*()")
_RUN_LEN = 4
_RUN_PAIRS = tuple(pairs for seq in _RUNS
                   for pairs in (frozenset(zip(seq, seq[1:])), frozenset(zip(seq[1:], seq))))
# Skipped while matching, so "pass word" and "1234-5678" are still found. A
# separator inside a stripped piece goes with it; the others still count.
_SEPARATORS = frozenset(" -_.")
_REPEAT_CHAR_RE = re.compile(r"(.)\1{2,}", re.DOTALL)        # "aaa", "!!!!"
_REPEAT_CHUNK_RE = re.compile(r"(.{2,64}?)\1+", re.DOTALL)   # "abab", "abc123abc123"
_YEAR_RE = re.compile(r"19[5-9][0-9]|20[0-3][0-9]")           # "Summer2024!" is an early guess
# Common letter-for-symbol swaps ("p@ssw0rd"). "1" reads as either "i" or "l".
_LEET_MAPS = (str.maketrans("@4031!$5+7", "aaoeiisstt"), str.maketrans("@4031!$5+7", "aaoelisstt"))
# What a stripped piece is still worth: it's one entry on a short list of
# early guesses, so a few bits (zxcvbn scores dictionary hits by rank the same
# way). Counted once per distinct piece.
_TOKEN_BITS = 4
_STRIP_PASSES = 64


def _pool_bits(text: str) -> float:
    """Brute-force estimate: length x log2(character pool)."""
    if not text:
        return 0.0
    pool = 0
    if re.search(r"[a-z]", text):
        pool += 26
    if re.search(r"[A-Z]", text):
        pool += 26
    if re.search(r"[0-9]", text):
        pool += 10
    if re.search(r"[^A-Za-z0-9 ]", text):
        pool += 33
    if " " in text:
        pool += 1
    return len(text) * math.log2(max(pool, 2))


def _runs_in(view: str) -> list[tuple[int, int]]:
    """(start, end) of every maximal run of _RUN_LEN+ characters in `view`."""
    out = []
    n = len(view)
    for pairs in _RUN_PAIRS:
        start = 0
        for k in range(1, n + 1):
            if k < n and (view[k - 1], view[k]) in pairs:
                continue
            if k - start >= _RUN_LEN:
                out.append((start, k))
            start = k
    return out


def _guessable_pieces(views: list[str]) -> list[tuple[int, int, int, str, str]]:
    """Non-overlapping guessable pieces as (start, end, keep, kind, token):
    repeats first ("password" x 3 is "password" plus a count), then common
    words (found in any of `views`, the same text with symbol swaps undone),
    runs and years (in views[0], as typed). `keep` characters at the start
    stay in play: a repeat keeps one copy, which the next pass scores."""
    view = views[0]
    taken = bytearray(len(view))
    found = []

    def add(start, end, keep, kind, token):
        if any(taken[start:end]):
            return
        taken[start:end] = b"\x01" * (end - start)
        found.append((start, end, keep, kind, token))

    for m in _REPEAT_CHAR_RE.finditer(view):
        add(m.start(), m.end(), 1, "repeat", m.group(1))
    for m in _REPEAT_CHUNK_RE.finditer(view):
        add(m.start(), m.end(), len(m.group(1)), "repeat", m.group(1))
    for word in _COMMON_LONGEST_FIRST:
        for v in views:
            i = v.find(word)
            while i != -1:
                add(i, i + len(word), 0, "word", word)
                i = v.find(word, i + 1)
    for start, end in sorted(_runs_in(view), key=lambda r: (r[0] - r[1], r[0])):
        add(start, end, 0, "run", view[start:end])
    for m in _YEAR_RE.finditer(view):
        add(m.start(), m.end(), 0, "year", m.group(0))
    return found


def _strip_guessable(p: str) -> tuple[str, list[tuple[str, str, int]]]:
    """Strip guessable pieces from `p` until none are left (taking one out can
    join two halves into a new one: "12password34" -> "1234"). Matching is on
    lowercased readings of `p` with separators ignored; what's stripped is
    removed from `p` itself, so the remainder keeps its capitals.
    Returns (remainder, [(kind, token, characters stripped)])."""
    # One character in, one character out (the few characters whose lowercase
    # is two characters stay as typed), so every reading lines up index for
    # index with `p`.
    plain = "".join(c.lower() if len(c.lower()) == 1 else c for c in p)
    readings = [plain] + [r for r in dict.fromkeys(plain.translate(m) for m in _LEET_MAPS) if r != plain]
    alive = list(range(len(p)))
    hits: list[tuple[str, str, int]] = []
    for _ in range(_STRIP_PASSES):
        view_idx = [i for i in alive if p[i] not in _SEPARATORS]
        pieces = _guessable_pieces(["".join(low[i] for i in view_idx) for low in readings])
        if not pieces:
            break
        dead = bytearray(len(p))
        for start, end, keep, kind, token in pieces:
            # The span in `p` runs from the piece's first to last character,
            # separators between them included; a repeat keeps its first copy.
            lo, hi = view_idx[start], view_idx[end - 1]
            dead[lo:hi + 1] = b"\x01" * (hi + 1 - lo)
            for i in view_idx[start:start + keep]:
                dead[i] = 0
            hits.append((kind, token, end - start - keep))
        alive = [i for i in alive if not dead[i]]
    return "".join(p[i] for i in alive), hits


def _strength(p: str) -> dict:
    """Estimated bits of `p` once its guessable pieces are stripped, plus
    _TOKEN_BITS per distinct piece (the same word ten times is one guess, not
    ten), and which kind of piece made up most of it ("main": "word" / "run"
    / "repeat" / "year", or "" when the guessable pieces are under half of
    it)."""
    rest, hits = _strip_guessable(p)
    bits = _pool_bits(rest) + _TOKEN_BITS * len({(kind, token) for kind, token, _ in hits})
    matched = {"word": 0, "run": 0, "repeat": 0, "year": 0}
    later = {token: kind for kind, token, _ in hits if kind != "repeat"}
    for kind, token, size in hits:
        # "passwordpassword" is mostly the word, "12341234" mostly the run:
        # a repeat counts as what its kept copy turned out to be.
        matched[later.get(token, kind) if kind == "repeat" else kind] += size
    significant = sum(1 for ch in p if ch not in _SEPARATORS)
    main = ""
    if sum(matched.values()) and 2 * sum(matched.values()) >= significant:
        main = max(matched, key=lambda k: matched[k])  # ties: word, run, repeat, year
    words = sorted((t for k, t, _ in hits if k == "word"), key=len, reverse=True)
    return {"bits": bits, "rest": rest, "main": main,
            "word": words[0] if words else "", "hits": [k for k, _, _ in hits]}


def _estimated_bits(p: str) -> float:
    """The strength check's estimate for `p` (see _strength)."""
    return _strength(p)["bits"]


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


_B64_RE = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")


def _unb64(value, field: str) -> bytes:
    if not isinstance(value, str) or not value or not _B64_RE.fullmatch(value):
        raise ValueError(f"Snapshot payload field {field!r} isn't base64.")
    return base64.b64decode(value, validate=True)


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt,
                     iterations=iterations)
    return kdf.derive(_normalize(passphrase).encode("utf-8"))


def validate_payload(payload) -> tuple[int, bytes, bytes, bytes]:
    """(iterations, salt, iv, ct) when `payload` is exactly an encrypt()
    result; ValueError otherwise. Extra keys are refused, so nothing but
    ciphertext and its parameters can ride along into a published file."""
    if not isinstance(payload, dict):
        raise ValueError("Snapshot payload must be a dict.")
    if set(payload) != set(PAYLOAD_KEYS):
        raise ValueError(f"Snapshot payload must have exactly the keys {', '.join(PAYLOAD_KEYS)}.")
    if payload["v"] != FORMAT_VERSION or isinstance(payload["v"], bool):
        raise ValueError(f"Snapshot payload version {payload['v']!r} isn't supported.")
    if payload["kdf"] != KDF_NAME:
        raise ValueError(f"Snapshot payload KDF {payload['kdf']!r} isn't supported.")
    it = payload["iter"]
    if isinstance(it, bool) or not isinstance(it, int) or not MIN_ITERATIONS <= it <= MAX_ITERATIONS:
        raise ValueError("Snapshot payload iteration count is out of range.")
    salt = _unb64(payload["salt"], "salt")
    iv = _unb64(payload["iv"], "iv")
    ct = _unb64(payload["ct"], "ct")
    if len(salt) != SALT_BYTES or len(iv) != NONCE_BYTES or len(ct) < TAG_BYTES:
        raise ValueError("Snapshot payload salt / iv / ciphertext have the wrong length.")
    return it, salt, iv, ct


# The ciphertext length would otherwise track how much is in the snapshot
# (repos, items). Pad to fixed-size blocks with a trailing HTML comment: inert
# when injected, stripped by decrypt().
PAD_BLOCK = 32 * 1024
_PAD_OPEN, _PAD_CLOSE = b"<!--", b"-->"
_PAD_RE = re.compile(r"<!--[ ]*-->\Z")


def _pad(raw: bytes) -> bytes:
    overhead = len(_PAD_OPEN) + len(_PAD_CLOSE)
    total = -(-(len(raw) + overhead) // PAD_BLOCK) * PAD_BLOCK
    return raw + _PAD_OPEN + b" " * (total - len(raw) - overhead) + _PAD_CLOSE


def _require_passphrase(passphrase) -> None:
    """Building only needs SOME passphrase. The strength rules
    (check_passphrase) apply when one is CHOSEN, in Settings: re-checking a
    saved one here would break every publish whenever the rules tighten,
    and the message could quote part of it into a log."""
    if not isinstance(passphrase, str) or not passphrase.strip():
        raise ValueError("Enter a passphrase.")


def encrypt(plaintext: str, passphrase: str) -> dict:
    """Encrypt `plaintext` for the phone. Raises ValueError for a missing
    passphrase (strength is checked when it's chosen, not here)."""
    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be a str")
    _require_passphrase(passphrase)
    salt = os.urandom(SALT_BYTES)
    iv = os.urandom(NONCE_BYTES)
    key = _derive_key(passphrase, salt, KDF_ITERATIONS)
    # errors="replace": a lone surrogate from odd GitHub JSON becomes "?"
    # instead of failing the whole build.
    ct = AESGCM(key).encrypt(iv, _pad(plaintext.encode("utf-8", errors="replace")), None)
    return {"v": FORMAT_VERSION, "kdf": KDF_NAME, "iter": KDF_ITERATIONS,
            "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct)}


def decrypt(payload: dict, passphrase: str) -> str:
    """Inverse of encrypt() (tests and self-checks). ValueError on a wrong
    passphrase, a tampered payload, or a malformed one."""
    it, salt, iv, ct = validate_payload(payload)
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError(WRONG_PASSPHRASE)
    key = _derive_key(passphrase, salt, it)
    try:
        raw = AESGCM(key).decrypt(iv, ct, None)
    except InvalidTag:
        raise ValueError(WRONG_PASSPHRASE) from None
    return _PAD_RE.sub("", raw.decode("utf-8"))


# ----------------------------------------------------------------------
# Time formatting (US Central, same output as app.py's central_time)
# ----------------------------------------------------------------------

def _parse_ts(value) -> datetime.datetime | None:
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(_LOCAL_TZ) if _LOCAL_TZ else dt


def central_time(iso_ts, seconds: bool = False) -> str:
    """'Nov 09, 2026 2:47 PM CST' ('Nov 09, 2026 2:47:05 PM CST' with
    seconds). Empty string on bad input."""
    dt = _parse_ts(iso_ts)
    if dt is None:
        return ""
    hour = dt.strftime("%I").lstrip("0") or "12"
    clock = dt.strftime("%M:%S %p" if seconds else "%M %p")
    return f"{dt.strftime('%b %d, %Y')} {hour}:{clock} {dt.tzname() or 'CT'}"


def central_date(iso_ts) -> str:
    """'Nov 9, 2026'. Empty string on bad input."""
    dt = _parse_ts(iso_ts)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}" if dt else ""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def make_built_label(now: datetime.datetime | None = None) -> str:
    """The build time as the phone shows it (the lock screen's "Built ..."
    line and the page's "Snapshot built ..."), in Central time and to the
    second, so two publishes in the same minute are told apart:
    'Nov 09, 2026 2:16:05 PM CST'. `now` is an aware datetime (UTC from the
    app; a naive one is read as UTC) or an ISO string; None means now."""
    return central_time(now or _now(), seconds=True) or central_time(_now(), seconds=True)


# ----------------------------------------------------------------------
# View model: every field from GitHub / caches is treated as hostile.
# Autoescape does the escaping; this layer makes types predictable so a
# missing, None or odd-typed field becomes an empty state, never a crash.
# ----------------------------------------------------------------------

def _s(value) -> str:
    if isinstance(value, str):
        return strip_bidi(value).strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _n(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return max(0, int(value))
    return 0


def _opt_n(value) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _n(value)


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _https_url(value) -> str:
    """value when it is an https URL with a host and no credentials, else ""."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value) or "\\" in value:
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        return ""
    return value


def _clean_label(value) -> str:
    text = re.sub(r"\s+", " ", "".join(ch for ch in _s(value) if unicodedata.category(ch)[0] != "C"))
    return text.strip()[:LABEL_MAX_CHARS]


def _plural(n, word: str, plural_word: str | None = None) -> str:
    n = _n(n)
    return f"{n:,} {word if n == 1 else (plural_word or word + 's')}"


def _age_text(days: int | None) -> str:
    if days is None:
        return ""
    return "today" if days == 0 else f"{_plural(days, 'day')} ago"


_AGE_TIERS = ("merge", "finish", "open_pr", "decide")


def _tier_key(value) -> str:
    return re.sub(r"[^a-z0-9_]", "", _s(value).lower())[:24] or "other"


def _item(x: dict) -> dict:
    """One nextup.build_do_next item, display-ready."""
    tier = _tier_key(x.get("tier"))
    links = []
    for link in _list(x.get("links")):
        if not isinstance(link, dict):
            continue
        url = _https_url(link.get("url"))  # app-internal links (/tracker/...) drop out here
        if url:
            links.append({"label": _s(link.get("label")) or "LINK", "url": url})
    return {
        "tier": tier,
        "label": _s(x.get("label")) or tier.upper().replace("_", " "),
        "repo": _s(x.get("repo")),
        "title": _s(x.get("title")),
        "why": _s(x.get("why")),
        "links": links,
        # Re-checked here (and again in the template): only a canonical
        # https://claude.ai/code/session_<id> ever becomes an href.
        "session_url": safe_session_url(x.get("session_url")),
        "prompt": _s(x.get("prompt")),
        "age": _age_text(_opt_n(x.get("age_days"))) if tier in _AGE_TIERS else "",
    }


def _items(value) -> list[dict]:
    return [_item(x) for x in _list(value) if isinstance(x, dict)]


def _names(value) -> list[str]:
    out = []
    for entry in _list(value):
        name = (_s(entry.get("name")) or _s(entry.get("full_name"))) if isinstance(entry, dict) else _s(entry)
        if name:
            out.append(name)
    return out


def _ai(value) -> dict | None:
    value = _dict(value)
    text = _s(value.get("text"))
    return {"text": text, "stale": bool(value.get("stale"))} if text else None


def _totals(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    t = {k: _n(value.get(k)) for k in ("repos_total", "repos_updated", "commits", "prs_merged",
                                        "active_days", "days", "errors")}
    t["read"] = _n(value["read"]) if "read" in value else max(0, t["repos_total"] - t["errors"])
    return t


def _freshness(iso_ts, now: datetime.datetime) -> dict:
    dt = _parse_ts(iso_ts)
    if dt is None:
        return {"label": "", "days": None}
    return {"label": central_time(dt), "days": max(0, (now - dt).days)}


def _do_next_view(value) -> dict:
    present = isinstance(value, dict)
    dn = _dict(value)
    items = _items(dn.get("items"))
    shown = items[:DO_NEXT_LIMIT]
    groups: list[dict] = []
    anchored: set[str] = set()
    for it in shown:
        if not groups or groups[-1]["tier"] != it["tier"]:
            # Only a tier's first group gets the jump anchor, so ids stay
            # unique even if a caller hands over items out of tier order.
            groups.append({"tier": it["tier"], "label": it["label"], "items": [],
                           "anchor": it["tier"] not in anchored})
            anchored.add(it["tier"])
        groups[-1]["items"].append(it)
    counts: dict[str, dict] = {}
    for it in items:
        c = counts.setdefault(it["tier"], {"tier": it["tier"], "label": it["label"], "n": 0,
                                           "linked": it["tier"] in anchored})
        c["n"] += 1
    return {
        "present": present,
        "total": len(items),
        "groups": groups,
        "counts": list(counts.values()),
        "more": len(items) - len(shown),
        "hidden_low": _n(dn.get("hidden_low_priority")),
        "planned_more": _n(dn.get("planned_more")),
    }


def _shipped_view(s: dict) -> dict:
    titles = [t for t in (_s(x) for x in _list(s.get("titles"))) if t][:RANGE_TITLES]
    return {
        "repo": _s(s.get("repo")),
        "commits": _n(s.get("commits")),
        "prs": _n(s.get("prs")),
        "merges": _n(s.get("merges")),
        "titles": titles,
        "more": _n(s.get("more")),
        "ai": _ai(s.get("ai")),
    }


def _review_view(value) -> dict:
    present = isinstance(value, dict)
    rv = _dict(value)
    week = rv.get("week")
    read = isinstance(week, dict)
    shipped = [_shipped_view(s) for s in _list(rv.get("shipped")) if isinstance(s, dict)]
    quiet = [{"repo": _s(q.get("repo")), "days": _opt_n(q.get("days")),
              "commits": _n(q.get("commits")), "prs": _n(q.get("prs"))}
             for q in _list(rv.get("quiet")) if isinstance(q, dict)]
    stale = [{"repo": _s(i.get("repo")), "branch": _s(i.get("branch")),
              "age_days": _opt_n(i.get("age_days")),
              "session_url": safe_session_url(i.get("session_url"))}
             for i in _list(rv.get("stale")) if isinstance(i, dict)]
    nxt = _items(rv.get("next"))
    return {
        "present": present,
        "read": read,
        "totals": _totals(_dict(week).get("totals")) if read else None,
        "overview": _ai(rv.get("overview")),
        "shipped": shipped[:RANGE_REPO_LIMIT],
        "shipped_more": max(0, len(shipped) - RANGE_REPO_LIMIT),
        "failed": _names(rv.get("failed")),
        "waiting": _items(rv.get("waiting")),
        "quiet": quiet,
        "stale": stale,
        "next": nxt,
        "next_total": max(_n(rv.get("next_total")), len(nxt)),
    }


def _range_repo(r: dict) -> dict:
    prs = [p for p in _list(r.get("prs")) if isinstance(p, dict)]
    commits = [c for c in _list(r.get("commits")) if isinstance(c, dict)]
    titles = []
    for p in prs:
        title, number = _s(p.get("title")), _opt_n(p.get("number"))
        if title:
            titles.append(f"#{number} {title}" if number is not None else title)
    titles += [t for t in (_s(c.get("summary")) for c in commits) if t]
    titles = titles[:RANGE_TITLES]
    pr_count = _n(r.get("pr_count")) if "pr_count" in r else len(prs)
    commit_count = _n(r.get("commit_count")) if "commit_count" in r else len(commits)
    return {
        "name": _s(r.get("name")),
        "commits": commit_count,
        "prs": pr_count,
        "merges": _n(r.get("merge_count")),
        "active_days": _n(r.get("active_days")),
        "last_at": _s(r.get("last_at")),
        "ai": _ai(r.get("ai")),
        "titles": titles,
        "more": max(0, pr_count + commit_count - len(titles)),
    }


def _range_view(r: dict, index: int) -> dict:
    key = re.sub(r"[^a-z0-9_-]", "", _s(r.get("key")).lower())[:24] or f"range{index}"
    totals = _totals(r.get("totals"))
    updated = [_range_repo(x) for x in _list(r.get("updated")) if isinstance(x, dict)]
    return {
        "key": key,
        "name": _s(r.get("name")) or key.title(),
        "dates": _s(r.get("dates")),
        "read": totals is not None,
        "totals": totals or {},
        "overview": _ai(r.get("ai_portfolio")),
        "failed": _names(r.get("failed")),
        "repos": updated[:RANGE_REPO_LIMIT],
        "more": max(0, len(updated) - RANGE_REPO_LIMIT),
    }


def _ranges_view(value) -> list[dict]:
    ranges = [_range_view(r, i) for i, r in enumerate(_list(value)) if isinstance(r, dict)]
    open_key = "week" if any(r["key"] == "week" for r in ranges) else (ranges[0]["key"] if ranges else "")
    for r in ranges:
        r["open"] = r["key"] == open_key
    return ranges


_TONES = {"good": "good", "ok": "good", "warn": "warn", "bad": "bad"}


def _portfolio_view(value) -> dict:
    themes = []
    for t in _list(value):
        if not isinstance(t, dict):
            continue
        rows = []
        for r in _list(t.get("rows")):
            if not isinstance(r, dict):
                continue
            rows.append({
                "name": _s(r.get("name")),
                "one_liner": _s(r.get("one_liner")),
                "grade": _s(r.get("grade"))[:3] or "—",
                "tone": _TONES.get(_s(r.get("tone")).lower(), "none"),
                "summary": _s(r.get("summary")),
                "html_url": _https_url(r.get("html_url")),
                "pushed_at": _s(r.get("pushed_at")),
                "open_branches": _n(r.get("open_branches")),
            })
        themes.append({"tag": _s(t.get("tag")) or "Untagged", "rows": rows})
    return {"present": isinstance(value, list), "themes": themes,
            "repos": sum(len(t["rows"]) for t in themes)}


def build_view(data: dict, now: datetime.datetime | None = None) -> dict:
    """Normalize the snapshot `data` dict (see render_body) for the template."""
    data = data if isinstance(data, dict) else {}
    now = now or _now()
    return {
        "built_label": _clean_label(data.get("built_label")) or make_built_label(now),
        "inflight": _freshness(data.get("inflight_gathered_at"), now),
        "activity": _freshness(data.get("activity_fetched_at"), now),
        "do_next": _do_next_view(data.get("do_next")),
        "review": _review_view(data.get("review")),
        "review_week_label": _s(data.get("review_week_label")),
        "ranges": _ranges_view(data.get("ranges")),
        "portfolio": _portfolio_view(data.get("portfolio")),
        "marker": BODY_MARKER,
    }


# ----------------------------------------------------------------------
# Rendering (own Jinja environment: no Flask app context needed)
# ----------------------------------------------------------------------

_ENV: jinja2.Environment | None = None


def _jinja_env() -> jinja2.Environment:
    global _ENV
    if _ENV is None:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
            autoescape=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters["central_time"] = central_time
        env.filters["central_date"] = central_date
        env.filters["safe_session_url"] = safe_session_url
        env.filters["plural"] = _plural
        env.filters["commas"] = lambda n: f"{_n(n):,}"
        _ENV = env
    return _ENV


def render_body(data: dict) -> str:
    """The read-only snapshot HTML: a fragment the shell injects after it
    decrypts. `data` keys (all optional; a missing one shows an empty state):

      built_label           str, e.g. "Nov 09, 2026 2:47:05 PM CST" (make_built_label())
      inflight_gathered_at  ISO timestamp of the In Flight read, or ""
      activity_fetched_at   ISO timestamp of the activity (commits) read, or ""
      do_next               nextup.build_do_next(...) result
      review                nextup.build_review(...) result
      review_week_label     str, e.g. "Nov 3 - Nov 9, 2026"
      ranges                [{"key", "name", "dates", "totals", "updated",
                              "failed", "ai_portfolio"}] for week/month/quarter/ytd
                            (activity.build_report + attach_summaries fields)
      portfolio             [{"tag", "rows": [{"name", "one_liner", "grade", "tone",
                              "summary", "html_url", "pushed_at", "open_branches"}]}]
    """
    t0 = time.monotonic()
    view = build_view(data)
    try:
        out = _jinja_env().get_template("body.html").render(v=view)
    except Exception as e:
        logger.exception("snapshot: render failed")
        log_snapshot_event("render_error", error=f"{type(e).__name__}: {str(e)[:300]}")
        raise
    log_snapshot_event(
        "render",
        bytes=len(out.encode("utf-8", errors="replace")),
        do_next=view["do_next"]["total"],
        ranges=len(view["ranges"]),
        portfolio_repos=view["portfolio"]["repos"],
        inflight_read=bool(view["inflight"]["label"]),
        activity_read=bool(view["activity"]["label"]),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
    return out


# ----------------------------------------------------------------------
# The public shell (index.html). No data in it, ever: only the generic
# title, the built label, the ciphertext, and the script below.
# ----------------------------------------------------------------------

# ASCII only, \n line endings: the CSP hash is over these exact bytes.
SHELL_SCRIPT = r"""
(function () {
  "use strict";
  var LEGACY_KEY = "repodoctor.snapshot.passphrase";
  var DB_NAME = "repodoctor-snapshot", DB_STORE = "keys", DB_ITEM = "passphrase-key";
  var WRONG = "That passphrase didn't unlock this snapshot.";
  function el(id) { return document.getElementById(id); }
  var lock = el("rd-lock"), app = el("rd-app"), content = el("rd-content");
  var form = el("rd-form"), input = el("rd-pass"), remember = el("rd-remember");
  var unlockBtn = el("rd-unlock"), statusEl = el("rd-status"), forgetBtn = el("rd-forget");
  var payload = null, busy = false;

  function setStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.className = "rd-status" + (kind ? " is-" + kind : "");
  }

  // Every storage access is wrapped: private mode, blocked site data or a
  // full quota must never stop the page from unlocking.
  // Earlier versions remembered the passphrase itself in localStorage; it is
  // only read once, to migrate it, and then removed.
  function legacy(action) {
    try {
      var ls = window.localStorage;
      if (action === "get") { return ls.getItem(LEGACY_KEY); }
      if (action === "clear") { ls.removeItem(LEGACY_KEY); }
    } catch (e) { /* storage blocked */ }
    return null;
  }

  // "Remember on this device" keeps a NON-EXTRACTABLE key object in
  // IndexedDB: this browser can reuse it to unlock, but no script and no one
  // reading storage can turn it back into the passphrase. Always resolves.
  function store(mode, work) {
    return new Promise(function (resolve) {
      var req;
      try { req = window.indexedDB.open(DB_NAME, 1); } catch (e) { resolve(null); return; }
      req.onupgradeneeded = function () {
        try { req.result.createObjectStore(DB_STORE); } catch (e) { /* exists */ }
      };
      req.onerror = function () { resolve(null); };
      req.onblocked = function () { resolve(null); };
      req.onsuccess = function () {
        var db = req.result, out = null;
        try {
          var tx = db.transaction(DB_STORE, mode), r = work(tx.objectStore(DB_STORE));
          if (r) { r.onsuccess = function () { out = r.result; }; }
          tx.oncomplete = function () { db.close(); resolve(out === undefined ? null : out); };
          tx.onerror = function () { db.close(); resolve(null); };
          tx.onabort = function () { db.close(); resolve(null); };
        } catch (e) {
          try { db.close(); } catch (e2) { /* ignore */ }
          resolve(null);
        }
      };
    });
  }

  function savedKey(action, key) {
    if (!window.indexedDB) { return Promise.resolve(null); }
    if (action === "get") { return store("readonly", function (st) { return st.get(DB_ITEM); }); }
    if (action === "set") { return store("readwrite", function (st) { return st.put(key, DB_ITEM); }); }
    return store("readwrite", function (st) { return st.delete(DB_ITEM); });
  }

  function bytes(b64) {
    var bin = window.atob(b64), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) { out[i] = bin.charCodeAt(i); }
    return out;
  }

  function readPayload() {
    var p = JSON.parse(el("rd-payload").textContent);
    if (!p || p.v !== 1 || p.kdf !== "PBKDF2-SHA256" || typeof p.iter !== "number" ||
        typeof p.salt !== "string" || typeof p.iv !== "string" || typeof p.ct !== "string") {
      throw new Error("format");
    }
    return p;
  }

  // The Mac derives the key from the NFC form of the passphrase, and Settings
  // refuses one that starts or ends with a space. So the typed text is
  // NFC-normalized and trimmed: a stray space from the phone's keyboard or
  // autocorrect never costs an unlock.
  function clean(pass) {
    var s = String(pass || "");
    if (s.normalize) { s = s.normalize("NFC"); }
    return s.trim();
  }

  // The passphrase becomes a non-extractable PBKDF2 base key; each publish's
  // salt derives the AES-GCM key from it.
  function baseKey(pass) {
    var subtle = window.crypto && window.crypto.subtle;
    if (!subtle || !window.TextEncoder || !window.TextDecoder) {
      return Promise.reject(new Error("nocrypto"));
    }
    var secret = new TextEncoder().encode(clean(pass));
    return subtle.importKey("raw", secret, "PBKDF2", false, ["deriveKey"]);
  }

  function decryptWith(base) {
    var subtle = window.crypto && window.crypto.subtle;
    if (!subtle || !window.TextDecoder) { return Promise.reject(new Error("nocrypto")); }
    return subtle.deriveKey(
      { name: "PBKDF2", salt: bytes(payload.salt), iterations: payload.iter, hash: "SHA-256" },
      base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]
    ).then(function (key) {
      return subtle.decrypt({ name: "AES-GCM", iv: bytes(payload.iv) }, key, bytes(payload.ct));
    }).then(function (buf) {
      return new TextDecoder("utf-8").decode(buf);
    });
  }

  function setBusy(on) {
    busy = on;
    unlockBtn.disabled = on;
    if (remember) { remember.disabled = on; }
    input.readOnly = on;
    unlockBtn.textContent = on ? "UNLOCKING..." : "UNLOCK";
    lock.setAttribute("aria-busy", on ? "true" : "false");
  }

  function show(html) {
    content.innerHTML = html;
    lock.hidden = true;
    app.hidden = false;
    input.value = "";
    setBusy(false);
    setStatus("");
    window.scrollTo(0, 0);
    var h = content.querySelector("h1");
    if (h) {
      h.setAttribute("tabindex", "-1");
      try { h.focus({ preventScroll: true }); } catch (e) { h.focus(); }
    }
  }

  function relock(message) {
    content.innerHTML = "";
    app.hidden = true;
    lock.hidden = false;
    input.value = "";
    if (remember) { remember.checked = false; }
    setBusy(false);
    setStatus(message || "", message ? "ok" : "");
    window.scrollTo(0, 0);
  }

  // source: the typed passphrase, or the key object saved on this device.
  function unlock(source, fromSaved) {
    if (busy) { return; }
    setBusy(true);
    setStatus(fromSaved ? "Unlocking with the key saved on this device..."
                        : "Unlocking... this takes a second or two on a phone.", "busy");
    var base = typeof source === "string" ? baseKey(source) : Promise.resolve(source);
    base.then(function (key) {
      return decryptWith(key).then(function (html) {
        if (fromSaved || !remember) { return html; }
        return savedKey(remember.checked ? "set" : "clear", key).then(function () { return html; });
      });
    }).then(function (html) {
      show(html);
    }, function (err) {
      setBusy(false);
      var name = err && err.name, msg = err && err.message;
      if (name === "OperationError") {
        if (fromSaved) {
          savedKey("clear");
          if (remember) { remember.checked = false; }
          setStatus("The passphrase saved on this device didn't unlock this snapshot. Type the current one.", "error");
        } else {
          setStatus(WRONG, "error");
          try { input.focus(); input.select(); } catch (e) { /* ignore */ }
        }
      } else if (msg === "nocrypto") {
        setStatus("This browser can't unlock the snapshot. Open the link in Safari or Chrome over https.", "error");
      } else {
        setStatus("Something went wrong opening this snapshot (" + (name || "error") + "). Reload the page and try again.", "error");
      }
    });
  }

  // Scripts inside the injected HTML never run (innerHTML + the CSP), so
  // COPY PROMPT buttons are handled here by delegation.
  function flash(btn, text) {
    if (!btn.getAttribute("data-label")) { btn.setAttribute("data-label", btn.textContent); }
    btn.textContent = text;
    window.clearTimeout(btn.rdTimer);
    btn.rdTimer = window.setTimeout(function () { btn.textContent = btn.getAttribute("data-label"); }, 1600);
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.left = "-9999px";
    ta.style.fontSize = "16px";
    document.body.appendChild(ta);
    ta.select();
    try { ta.setSelectionRange(0, text.length); } catch (e) { /* ignore */ }
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function onCopyClick(e) {
    var t = e.target, btn = t && t.closest ? t.closest(".js-copy") : null;
    if (!btn || !content.contains(btn)) { return; }
    var text = btn.getAttribute("data-copy") || "";
    if (!text) { return; }
    function done() { flash(btn, "COPIED!"); }
    function fallback() { flash(btn, fallbackCopy(text) ? "COPIED!" : "COPY FAILED"); }
    if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  try {
    payload = readPayload();
  } catch (e) {
    unlockBtn.disabled = true;
    setStatus("This snapshot file is damaged and can't be unlocked. Publish it again from RepoDoctor on your Mac.", "error");
    return;
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var pass = clean(input.value);
    if (!pass) {
      setStatus("Type your passphrase first.", "error");
      input.focus();
      return;
    }
    unlock(pass, false);
  });
  forgetBtn.addEventListener("click", function () {
    legacy("clear");
    savedKey("clear");
    relock("Locked. This device no longer remembers the passphrase.");
  });
  document.addEventListener("click", onCopyClick);

  // Only pages that offer "Remember" (not the Mac's preview) read storage.
  function savedOnLoad() {
    var old = legacy("get");
    if (old) {
      legacy("clear");
      return baseKey(old).then(function (key) {
        return savedKey("set", key).then(function () { return key; });
      }, function () { return null; });
    }
    return savedKey("get");
  }
  if (remember) {
    savedOnLoad().then(function (key) {
      if (key) { remember.checked = true; unlock(key, true); }
    });
  }
})();
"""

_SHELL_CSS = """
:root { color-scheme: dark; }
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body {
  margin: 0; min-height: 100vh; background: #080a08; color: #b8d8b8;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size: 15px; line-height: 1.55;
}
body::after {
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 50;
  background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
}
[hidden] { display: none !important; }
.rd-lock {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 24px 16px;
}
.rd-card {
  width: 100%; max-width: 420px; padding: 28px 22px 22px;
  background: #0b0e0b; border: 1px solid #1a2a1a; border-radius: 8px;
}
.rd-brand { margin: 0; color: #33ff33; font-size: 12px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; }
.rd-brand::before { content: "> "; color: #22cc22; }
.rd-card h1 { margin: 10px 0 4px; color: #33ff33; font-size: 22px; font-weight: 600; line-height: 1.25; }
.rd-built { margin: 0 0 22px; color: #6a8a6a; font-size: 13px; overflow-wrap: anywhere; }
.rd-label { display: block; margin: 0 0 6px; color: #6a8a6a; font-size: 12px; letter-spacing: 0.1em; text-transform: uppercase; }
.rd-input {
  display: block; width: 100%; min-height: 48px; padding: 10px 12px;
  font: inherit; font-size: 16px; color: #b8d8b8; caret-color: #33ff33;
  background: #080a08; border: 1px solid #2a3f2a; border-radius: 5px;
}
.rd-input:focus { outline: 2px solid #33ff33; outline-offset: 1px; border-color: #22cc22; }
.rd-remember { display: flex; align-items: center; gap: 10px; min-height: 44px; margin: 12px 0 2px; font-size: 14px; cursor: pointer; }
.rd-remember input { width: 20px; height: 20px; margin: 0; accent-color: #33ff33; flex: 0 0 auto; }
.rd-hint { margin: 0 0 16px 30px; color: #6a8a6a; font-size: 12px; line-height: 1.45; }
.rd-hint-solo { margin: 10px 0 16px; }
.rd-btn {
  display: block; width: 100%; min-height: 48px; padding: 10px 14px;
  font: inherit; font-size: 15px; font-weight: 700; letter-spacing: 0.1em;
  color: #080a08; background: #33ff33; border: 1px solid #33ff33; border-radius: 5px; cursor: pointer;
}
.rd-btn:hover { background: #22cc22; border-color: #22cc22; }
.rd-btn:focus-visible { outline: 2px solid #6dffa0; outline-offset: 2px; }
.rd-btn:disabled { opacity: 0.65; cursor: progress; }
.rd-status { min-height: 1.55em; margin: 14px 0 0; color: #6a8a6a; font-size: 14px; overflow-wrap: anywhere; }
.rd-status.is-error { color: #ff7a7a; }
.rd-status.is-ok { color: #6dffa0; }
.rd-status.is-busy { color: #b8d8b8; }
.rd-status.is-busy::before {
  content: ""; display: inline-block; width: 0.6em; height: 1.05em; margin-right: 0.5em;
  vertical-align: -0.18em; background: #33ff33; animation: rd-blink 0.9s steps(2, start) infinite;
}
@keyframes rd-blink { to { visibility: hidden; } }
.rd-note { margin: 20px 0 0; padding-top: 14px; border-top: 1px solid #1a2a1a; color: #6a8a6a; font-size: 12px; line-height: 1.5; }
.rd-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  max-width: 860px; margin: 0 auto; padding: 12px 16px 2px;
}
.rd-forget {
  min-height: 44px; padding: 0 14px; font: inherit; font-size: 12px; font-weight: 700; letter-spacing: 0.08em;
  color: #b8d8b8; background: transparent; border: 1px solid #2a3f2a; border-radius: 5px; cursor: pointer;
}
.rd-forget:hover { border-color: #22cc22; color: #33ff33; }
.rd-forget:focus-visible { outline: 2px solid #33ff33; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .rd-status.is-busy::before { animation: none; } }
"""

_FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
            "%3Crect width='16' height='16' rx='3' fill='%23080a08'/%3E"
            "%3Crect x='5' y='3' width='6' height='10' fill='%2333ff33'/%3E%3C/svg%3E")

_SHELL_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="@@CSP@@">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#080a08">
<title>@@TITLE@@</title>
<link rel="icon" href="@@FAVICON@@">
<style>@@CSS@@</style>
</head>
<body>
<main id="rd-lock" class="rd-lock">
  <div class="rd-card">
    <p class="rd-brand">RepoDoctor</p>
    <h1>Snapshot</h1>
    <p class="rd-built">Built @@LABEL@@</p>
    <form id="rd-form" novalidate>
      <label class="rd-label" for="rd-pass">Passphrase</label>
      <input id="rd-pass" class="rd-input" type="password" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" enterkeyhint="go" aria-describedby="rd-status">
      @@REMEMBER@@
      <button id="rd-unlock" class="rd-btn" type="submit">UNLOCK</button>
      <p id="rd-status" class="rd-status" role="status" aria-live="polite"></p>
    </form>
    <noscript><p class="rd-status is-error">This snapshot needs JavaScript to unlock.</p></noscript>
    <p class="rd-note">Read-only and encrypted. It opens only with your passphrase, right here in this browser.</p>
  </div>
</main>
<div id="rd-app" hidden>
  <div class="rd-bar"><p class="rd-brand">RepoDoctor snapshot</p><button id="rd-forget" class="rd-forget" type="button" title="Lock this page and forget the passphrase on this device">FORGET</button></div>
  <div id="rd-content"></div>
</div>
<script type="application/json" id="rd-payload">@@PAYLOAD@@</script>
<script>@@SCRIPT@@</script>
</body>
</html>
"""


def script_hash(script: str) -> str:
    """CSP source for an inline script: 'sha256-<base64 digest>'."""
    return "'sha256-" + _b64(hashlib.sha256(script.encode("utf-8")).digest()) + "'"


def _csp(script_hashes: list[str], meta: bool = False) -> str:
    parts = [
        "default-src 'none'",
        "script-src " + " ".join(script_hashes),
        "style-src 'unsafe-inline'",
        "img-src data:",
        "connect-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ]
    if not meta:
        # Browsers ignore (and warn about) frame-ancestors in a <meta> policy.
        parts.append("frame-ancestors 'none'")
    return "; ".join(parts)


_REMEMBER_HTML = ('<label class="rd-remember"><input id="rd-remember" type="checkbox"> Remember on this device</label>\n'
                  '      <p class="rd-hint">Skips this screen next time. Only on your own phone.</p>')
# No checkbox to sit beside, so this hint lines up with the input instead.
_NO_REMEMBER_HTML = '<p class="rd-hint rd-hint-solo">Preview on your Mac: nothing is remembered here.</p>'


def build_shell(payload: dict, built_label: str, allow_remember: bool = True) -> str:
    """The public index.html. Holds no plaintext data: only the generic title,
    the built label (a date/time string), the validated ciphertext payload, and
    SHELL_SCRIPT. The page carries its own <meta> CSP too, so it stays locked
    down even where the _headers file isn't applied."""
    validate_payload(payload)
    label = _clean_label(built_label) or "recently"
    # The payload is ints, two fixed strings and base64 (validated above), so
    # it can't close the <script> element; escape <>& anyway, belt and braces.
    payload_json = json.dumps({k: payload[k] for k in PAYLOAD_KEYS}, separators=(",", ":"))
    payload_json = (payload_json.replace("<", "\\u003c").replace(">", "\\u003e")
                    .replace("&", "\\u0026"))
    values = {
        "CSP": html.escape(_csp([script_hash(SHELL_SCRIPT)], meta=True), quote=True),
        "TITLE": html.escape(SHELL_TITLE),
        "FAVICON": html.escape(_FAVICON, quote=True),
        "CSS": _SHELL_CSS,
        "LABEL": html.escape(label),
        "PAYLOAD": payload_json,
        "SCRIPT": SHELL_SCRIPT,
        "REMEMBER": _REMEMBER_HTML if allow_remember else _NO_REMEMBER_HTML,
    }
    # One pass, so text inserted for one placeholder is never re-scanned.
    return re.sub(r"@@([A-Z]+)@@", lambda m: values[m.group(1)], _SHELL_TEMPLATE)


_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_DATA_BLOCK_RE = re.compile(r"""\btype\s*=\s*["']?application/json["']?""", re.IGNORECASE)


def inline_scripts(page_html: str) -> list[str]:
    """The text of every executable inline <script> in `page_html` (JSON data
    blocks excluded), exactly as written, which is what CSP hashes cover."""
    return [body for attrs, body in _SCRIPT_RE.findall(page_html)
            if not _DATA_BLOCK_RE.search(attrs)]


def headers_file(script_hashes: list[str]) -> str:
    """Netlify _headers for every path on the site."""
    return (
        "/*\n"
        f"  Content-Security-Policy: {_csp(script_hashes)}\n"
        "  X-Robots-Tag: noindex, nofollow\n"
        "  Referrer-Policy: no-referrer\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Cache-Control: no-store\n"
    )


def site_files(shell_html: str) -> dict[str, bytes]:
    """{filename: bytes} to publish: index.html, robots.txt, _headers. The CSP
    script hash is computed from the scripts actually in `shell_html`."""
    if not isinstance(shell_html, str) or not shell_html.strip():
        raise ValueError("The snapshot shell is empty.")
    if BODY_MARKER in shell_html:
        raise ValueError("Refusing to publish: the page contains unencrypted snapshot content.")
    scripts = inline_scripts(shell_html)
    if not scripts:
        raise ValueError("The snapshot shell has no unlock script.")
    hashes_ = list(dict.fromkeys(script_hash(s) for s in scripts))
    return {
        "index.html": shell_html.encode("utf-8"),
        "robots.txt": ROBOTS_TXT.encode("ascii"),
        "_headers": headers_file(hashes_).encode("ascii"),
    }


_ZIP_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")


def build_zip(files: dict[str, bytes]) -> bytes:
    """A zip with every file at the archive root (Netlify's zip deploy).
    Fixed timestamps and sorted names, so the same files give the same zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            if not isinstance(name, str) or not _ZIP_NAME_RE.fullmatch(name) or ".." in name:
                raise ValueError(f"Not a root-level file name: {name!r}")
            content = files[name]
            if isinstance(content, str):
                content = content.encode("utf-8")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, content)
    return buf.getvalue()


def build_site(data: dict, passphrase: str) -> dict[str, bytes]:
    """render_body -> encrypt -> build_shell -> site_files, logged. Raises
    ValueError for a missing passphrase (never logs anything about it)."""
    try:
        _require_passphrase(passphrase)
    except ValueError:
        log_snapshot_event("build_refused", reason="no_passphrase")
        raise
    t0 = time.monotonic()
    data = data if isinstance(data, dict) else {}
    label = _clean_label(data.get("built_label")) or make_built_label()
    body = render_body({**data, "built_label": label})
    t1 = time.monotonic()
    payload = encrypt(body, passphrase)
    t2 = time.monotonic()
    files = site_files(build_shell(payload, label))
    log_snapshot_event(
        "build",
        body_bytes=len(body.encode("utf-8", errors="replace")),
        ciphertext_b64_chars=len(payload["ct"]),
        iterations=payload["iter"],
        files={k: len(v) for k, v in files.items()},
        script_hash=script_hash(SHELL_SCRIPT),
        render_ms=int((t1 - t0) * 1000),
        encrypt_ms=int((t2 - t1) * 1000),
        total_ms=int((time.monotonic() - t0) * 1000),
    )
    logger.info("snapshot: built %s bytes of ciphertext in %d ms",
                f"{len(payload['ct']):,}", int((time.monotonic() - t0) * 1000))
    return files
