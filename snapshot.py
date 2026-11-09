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
phone matches the one typed on the Mac.

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
    return os.path.join(models.DATA_DIR, "logs", "snapshot.log")


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
        return "Use more variety. That one repeats the same few characters."
    return None


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


def encrypt(plaintext: str, passphrase: str) -> dict:
    """Encrypt `plaintext` for the phone. Raises ValueError (the plain-English
    check_passphrase problem) for an unusable passphrase."""
    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be a str")
    problem = check_passphrase(passphrase)
    if problem:
        raise ValueError(problem)
    salt = os.urandom(SALT_BYTES)
    iv = os.urandom(NONCE_BYTES)
    key = _derive_key(passphrase, salt, KDF_ITERATIONS)
    # errors="replace": a lone surrogate from odd GitHub JSON becomes "?"
    # instead of failing the whole build.
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8", errors="replace"), None)
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
    return raw.decode("utf-8")


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


def central_time(iso_ts) -> str:
    """'Nov 09, 2026 2:47 PM CST'. Empty string on bad input."""
    dt = _parse_ts(iso_ts)
    if dt is None:
        return ""
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%b %d, %Y')} {hour}:{dt.strftime('%M %p')} {dt.tzname() or 'CT'}"


def central_date(iso_ts) -> str:
    """'Nov 9, 2026'. Empty string on bad input."""
    dt = _parse_ts(iso_ts)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}" if dt else ""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def make_built_label(now: datetime.datetime | None = None) -> str:
    """The build time as the snapshot shows it, in Central time."""
    return central_time(now or _now())


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

      built_label           str, e.g. "Nov 09, 2026 2:47 PM CST" (make_built_label())
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
  var STORE_KEY = "repodoctor.snapshot.passphrase";
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
  function saved(action, value) {
    try {
      var ls = window.localStorage;
      if (action === "get") { return ls.getItem(STORE_KEY); }
      if (action === "set") { ls.setItem(STORE_KEY, value); }
      if (action === "clear") { ls.removeItem(STORE_KEY); }
    } catch (e) { /* remembering just doesn't happen */ }
    return null;
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

  function decrypt(pass) {
    var subtle = window.crypto && window.crypto.subtle;
    if (!subtle || !window.TextEncoder || !window.TextDecoder) {
      return Promise.reject(new Error("nocrypto"));
    }
    var secret = new TextEncoder().encode(pass.normalize ? pass.normalize("NFC") : pass);
    return subtle.importKey("raw", secret, "PBKDF2", false, ["deriveKey"]).then(function (base) {
      return subtle.deriveKey(
        { name: "PBKDF2", salt: bytes(payload.salt), iterations: payload.iter, hash: "SHA-256" },
        base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
    }).then(function (key) {
      return subtle.decrypt({ name: "AES-GCM", iv: bytes(payload.iv) }, key, bytes(payload.ct));
    }).then(function (buf) {
      return new TextDecoder("utf-8").decode(buf);
    });
  }

  function setBusy(on) {
    busy = on;
    unlockBtn.disabled = on;
    remember.disabled = on;
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
    remember.checked = false;
    setBusy(false);
    setStatus(message || "", message ? "ok" : "");
    window.scrollTo(0, 0);
  }

  function unlock(pass, fromSaved) {
    if (busy) { return; }
    setBusy(true);
    setStatus(fromSaved ? "Unlocking with the passphrase saved on this device..."
                        : "Unlocking... this takes a second or two on a phone.", "busy");
    decrypt(pass).then(function (html) {
      if (!fromSaved) {
        if (remember.checked) { saved("set", pass); } else { saved("clear"); }
      }
      show(html);
    }, function (err) {
      setBusy(false);
      var name = err && err.name, msg = err && err.message;
      if (name === "OperationError") {
        if (fromSaved) {
          saved("clear");
          remember.checked = false;
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
    var pass = input.value;
    if (!pass) {
      setStatus("Type your passphrase first.", "error");
      input.focus();
      return;
    }
    unlock(pass, false);
  });
  forgetBtn.addEventListener("click", function () {
    saved("clear");
    relock("Locked. This device no longer remembers the passphrase.");
  });
  document.addEventListener("click", onCopyClick);

  var remembered = saved("get");
  if (remembered) {
    remember.checked = true;
    unlock(remembered, true);
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
  min-height: 40px; padding: 0 14px; font: inherit; font-size: 12px; font-weight: 700; letter-spacing: 0.08em;
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
      <input id="rd-pass" class="rd-input" type="password" autocomplete="current-password" autocapitalize="none" autocorrect="off" spellcheck="false" enterkeyhint="go" aria-describedby="rd-status">
      <label class="rd-remember"><input id="rd-remember" type="checkbox"> Remember on this device</label>
      <p class="rd-hint">Skips this screen next time. Only on your own phone.</p>
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


def build_shell(payload: dict, built_label: str) -> str:
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
    ValueError with a plain-English problem for an unusable passphrase."""
    problem = check_passphrase(passphrase)
    if problem:
        log_snapshot_event("build_refused", reason="passphrase", problem=problem)
        raise ValueError(problem)
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
