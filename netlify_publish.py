"""
Netlify API client for RepoDoctor's encrypted, read-only phone snapshot.

The app publishes a zip of static files (index.html, _headers, robots.txt)
to one Netlify site it owns. This module is only the HTTP client for that:
check the token, create the site, look it up, deploy a zip, delete the site.
Netlify REST API v1, https://api.netlify.com/api/v1, Bearer personal
access token.

Contract for callers:
  * Every failure raises NetlifyError. Its .message is plain English and
    says what to do next; .status is Netlify's HTTP status, or None for
    network trouble, an unreadable reply, a failed or slow deploy, or bad
    local input; .kind is a short machine-readable reason.
  * The token never reaches a message, a Python log line, or a log_event
    field. Every string is scrubbed of it, and of anything shaped like a
    bearer token, before it is logged or raised.
  * No automatic retries, except ONE retry of a 429/502/503/504 after the
    Retry-After wait (capped at 10s).
"""

from __future__ import annotations

import datetime
import email.utils
import json as _json
import logging
import math
import re
import secrets
import string
import time
import urllib.parse

import requests
from requests.auth import AuthBase

NETLIFY_API = "https://api.netlify.com/api/v1"
USER_AGENT = "RepoDoctor-snapshot"
DEFAULT_SITE_PREFIX = "repodoctor-snapshot"
logger = logging.getLogger(__name__)

# --- Owner-facing messages ----------------------------------------------------
TOKEN_HELP = "Netlify → User settings → Applications → Personal access tokens"
MSG_NO_TOKEN = f"Paste your Netlify personal access token first ({TOKEN_HELP})."
MSG_TOKEN_SHAPE = ("That doesn't look like a complete Netlify personal access token. "
                   f"Copy the whole token from Netlify ({TOKEN_HELP}) and paste it again.")
MSG_AUTH = ("Netlify didn't accept that token. Create a new personal access token "
            f"({TOKEN_HELP}) and paste it again.")
MSG_FORBIDDEN = ("Your Netlify token doesn't have access to that site or team. Use a token "
                 "from the Netlify account that owns the snapshot site, or run snapshot "
                 "setup again.")
MSG_SITE_GONE = ("The snapshot site no longer exists on Netlify (it was deleted, or it "
                 "belongs to a different Netlify account). Run snapshot setup again to "
                 "create a new one.")
MSG_NOT_FOUND = ("Netlify couldn't find what RepoDoctor asked for. It may have been "
                 "deleted; run snapshot setup again.")
MSG_RATE_LIMITED = ("Netlify is limiting how fast RepoDoctor can make requests. "
                    "Wait a minute and try again.")
MSG_SERVER = ("Netlify is having trouble right now (HTTP {status}). Wait a few minutes "
              "and try again; netlifystatus.com shows any outage.")
MSG_NETWORK = "Couldn't reach Netlify — check your internet connection and try again."
MSG_BAD_REPLY = ("Netlify sent back a reply RepoDoctor couldn't read. Try again in a "
                 "minute. If it keeps happening, copy the Debug log from Settings into "
                 "Claude Code.")
MSG_NO_SITE = "No Netlify snapshot site is set up yet. Run snapshot setup first."
MSG_BAD_SITE_ID = "The saved Netlify site id isn't valid. Run snapshot setup again."
MSG_BAD_ZIP = ("The snapshot RepoDoctor built isn't a valid zip file, so nothing was sent "
               "to Netlify. Publish again; if it keeps happening, copy the Debug log from "
               "Settings into Claude Code.")

# Deploy states (Netlify's deploy model). Only "ready" is done and only
# "error"/"rejected" are failures; every other state, known or not, means
# "keep polling" until the max wait.
DEPLOY_READY = "ready"
DEPLOY_FAILED_STATES = frozenset({"error", "rejected"})
DEPLOY_IN_PROGRESS_STATES = frozenset({
    "new", "pending_review", "accepted", "enqueued", "building", "uploading",
    "uploaded", "preparing", "prepared", "processing", "processed", "retrying",
})

# --- Secret scrubbing ----------------------------------------------------------
REDACTED = "[redacted]"
# A pasted token: printable ASCII, no spaces. Real Netlify tokens are 40+
# characters; anything under 16 is a partial paste and is never sent.
_TOKEN_SHAPE = re.compile(r"^[\x21-\x7e]{16,1024}$")
_BEARER_SHAPE = re.compile(r"(?i)\bbearer\s+[^\s\"',;]+")
_QUERY_TOKEN_SHAPE = re.compile(r"(?i)\b((?:access_)?token=)[^&\s\"']+")
# Netlify token prefixes (nfp_ personal, nfo_/nfc_ etc.).
_NETLIFY_TOKEN_SHAPE = re.compile(r"\bnf[a-z]_[A-Za-z0-9_\-]{8,}")
# Legacy 64-hex personal tokens and long opaque OAuth tokens. Ids Netlify
# uses (24-hex deploy ids, hyphenated UUID site ids) are shorter runs.
_LONG_OPAQUE_SHAPE = re.compile(r"\b[A-Za-z0-9_]{40,}\b")
# Site ids are UUIDs, deploy ids are hex; site names are also accepted by
# the API. Anything else (slashes, dot-dot, spaces) never goes into a URL.
_ID_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,254}$")
_NEVER_LOG_KEYS = frozenset({"token", "authorization", "headers", "auth", "body", "text"})
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


def redact(text, token: str = "") -> str:
    """Remove `token` and anything shaped like a bearer token from `text`."""
    s = text if isinstance(text, str) else str(text)
    for secret in _secret_variants(token or ""):
        s = s.replace(secret, REDACTED)
    s = _BEARER_SHAPE.sub("Bearer " + REDACTED, s)
    s = _QUERY_TOKEN_SHAPE.sub(lambda m: m.group(1) + REDACTED, s)
    s = _NETLIFY_TOKEN_SHAPE.sub(REDACTED, s)
    return _LONG_OPAQUE_SHAPE.sub(REDACTED, s)


def _secret_variants(*raw_tokens: str) -> list[str]:
    """Every spelling of the token a reply could echo: as pasted, cleaned,
    URL-encoded, JSON-escaped. Longest first so a longer form is replaced
    whole. Strings under 8 characters are skipped: they can't be a real
    token (those are never sent) and replacing them would mangle text."""
    out: set[str] = set()
    for raw in raw_tokens:
        if not isinstance(raw, str):
            continue
        for t in {raw.strip(), _clean_token(raw)}:
            if len(t) >= 8:
                out.update({t, urllib.parse.quote(t, safe=""), _json.dumps(t)[1:-1]})
    return sorted(out, key=len, reverse=True)


def _clean_token(raw) -> str:
    """Trim whitespace and a pasted 'Bearer ' prefix."""
    t = raw.strip() if isinstance(raw, str) else ""
    if t[:7].lower() == "bearer ":
        t = t[7:].strip()
    return t


def _random_suffix() -> str:
    """Six lowercase letters/digits for a fresh site name."""
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(6))


def _site_name_prefix(prefix) -> str:
    """Netlify site names allow lowercase letters, digits and single hyphens
    ('--' is reserved for deploy permalinks), 63 characters max with the
    7-character '-xxxxxx' suffix."""
    p = re.sub(r"[^a-z0-9-]+", "-", str(prefix or "").lower())
    p = re.sub(r"-{2,}", "-", p).strip("-")[:56].rstrip("-")
    return p or DEFAULT_SITE_PREFIX


def _text(value) -> str | None:
    """A non-empty stripped string, or None for anything else."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _pick_url(*candidates) -> str | None:
    """First usable http(s) URL. A plain-http *.netlify.app URL is upgraded:
    Netlify always serves those over HTTPS."""
    for c in candidates:
        u = _text(c)
        if not u or not u.lower().startswith(("https://", "http://")):
            continue
        if u.lower().startswith("http://"):
            host = (urllib.parse.urlsplit(u).hostname or "").lower()
            if host.endswith((".netlify.app", ".netlify.com")):
                u = "https://" + u[7:]
        return u
    return None


def _positive(value, default: float) -> float:
    """A finite number > 0, else the default (None, NaN, 0, negatives)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) and v > 0 else default


def _deploy_state(deploy: dict) -> str:
    state = deploy.get("state")
    return state.strip().lower() if isinstance(state, str) else ""


class NetlifyError(Exception):
    """Any Netlify failure, phrased for the owner.

    .message    plain English: what happened and what to do next
    .status     Netlify's HTTP status, or None (network, unreadable reply,
                failed or slow deploy, bad local input)
    .kind       bad_token | auth | forbidden | not_found | site_gone |
                name_taken | rejected | rate_limited | server | network |
                bad_reply | bad_input | deploy_failed | deploy_timeout
    .deploy_id  the deploy in question, for deploy failures and timeouts
    """

    def __init__(self, message: str, status: int | None = None, *,
                 kind: str = "error", deploy_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.kind = kind
        self.deploy_id = deploy_id

    def __str__(self) -> str:
        return self.message


class _BearerAuth(AuthBase):
    """Sets the Authorization header at send time. Passing auth (rather than
    a plain header) also stops requests from swapping in ~/.netrc
    credentials, and requests drops it on a redirect to another host."""

    def __init__(self, token: str):
        self._token = token

    def __call__(self, r):
        r.headers["Authorization"] = f"Bearer {self._token}"
        return r

    def __repr__(self) -> str:
        return f"<BearerAuth {REDACTED}>"


class NetlifyClient:
    CONNECT_TIMEOUT_S = 10
    READ_TIMEOUT_S = 20
    UPLOAD_READ_TIMEOUT_S = 60
    RETRY_STATUSES = frozenset({429, 502, 503, 504})
    POST_RETRY_STATUSES = frozenset({429, 503})
    RETRY_AFTER_CAP_S = 10.0
    RETRY_AFTER_DEFAULT_S = 2.0
    POLL_INTERVAL_S = 1.5
    DEPLOY_MAX_WAIT_S = 90.0
    NAME_RETRIES = 3

    def __init__(self, token: str, session=None, log_event=None, *,
                 sleep=None, clock=None):
        """Never raises: a missing or malformed token surfaces as a
        NetlifyError (kind 'bad_token') from the first call, before any
        request is sent. `sleep`/`clock` are injectable for tests."""
        raw = token if isinstance(token, str) else ""
        self._token = _clean_token(raw)
        self._secrets = _secret_variants(raw, self._token)
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._log_event = log_event
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic

    def __repr__(self) -> str:
        return f"<NetlifyClient token={REDACTED}>"

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- public API -----------------------------------------------------------

    def verify(self) -> dict:
        """Check the token. Returns {"ok": True, "account_slug": str | None}.
        The slug (the Netlify team's URL name) is best-effort and only for
        display; nothing personal (name, email) is returned or logged."""
        t0 = self._clock()
        try:
            resp = self._call("GET", "/user", action="verify")
            user = self._json_dict(resp, action="verify")
        except NetlifyError as e:
            self._emit("verify", ok=False, status=e.status, kind=e.kind,
                       elapsed_ms=self._ms(t0))
            raise
        slug = _text(user.get("slug")) or self._first_account_slug()
        self._emit("verify", ok=True, status=resp.status_code,
                   has_account_slug=bool(slug), elapsed_ms=self._ms(t0))
        return {"ok": True, "account_slug": slug}

    def create_site(self, name_prefix: str = DEFAULT_SITE_PREFIX) -> dict:
        """Create an empty site named '<prefix>-<6 random a-z0-9>'. A 422
        (name taken) retries with a new suffix up to NAME_RETRIES times.
        Returns {"site_id", "name", "url", "admin_url"}."""
        prefix = _site_name_prefix(name_prefix)
        t0 = self._clock()
        detail = ""
        attempts = self.NAME_RETRIES + 1
        for attempt in range(1, attempts + 1):
            name = f"{prefix}-{_random_suffix()}"
            resp = self._call("POST", "/sites", action="create_site",
                              json={"name": name}, allow=(422,))
            if resp.status_code == 422:
                detail = self._detail(resp)
                self._emit("create_site", outcome="name_taken", attempt=attempt,
                           status=422, name=name, detail=detail)
                continue
            site = self._site_summary(self._json_dict(resp, action="create_site"),
                                      action="create_site", fallback_name=name)
            self._emit("create_site", outcome="ok", attempt=attempt,
                       status=resp.status_code, site_id=site["site_id"],
                       name=site["name"], elapsed_ms=self._ms(t0))
            return site
        self._emit("create_site", outcome="failed", attempt=attempts, status=422,
                   detail=detail, elapsed_ms=self._ms(t0))
        reason = f" Netlify said: {detail.rstrip('.')}." if detail else ""
        raise NetlifyError(self._scrub(
            f"Netlify wouldn't create the snapshot site: it turned down {attempts} "
            f"different site names.{reason} Try again in a minute. If it keeps "
            "happening, copy the Debug log from Settings into Claude Code."),
            422, kind="name_taken")

    def get_site(self, site_id: str) -> dict:
        """Look the site up. Returns {"site_id", "name", "url", "admin_url"};
        a 404 raises NetlifyError(status=404, kind='site_gone')."""
        sid = self._check_site_id(site_id)
        t0 = self._clock()
        resp = self._call("GET", f"/sites/{sid}", action="get_site",
                          not_found_msg=MSG_SITE_GONE, not_found_kind="site_gone")
        site = self._site_summary(self._json_dict(resp, action="get_site"),
                                  action="get_site")
        self._emit("get_site", site_id=sid, status=resp.status_code,
                   elapsed_ms=self._ms(t0))
        return site

    def deploy_zip(self, site_id: str, zip_bytes: bytes, *,
                   max_wait_s: float | None = None,
                   poll_interval_s: float | None = None) -> dict:
        """Upload the zip as a production deploy, then poll until Netlify
        says it's live. Returns {"deploy_id", "state": "ready", "url",
        "site_url", "elapsed_s"}: url is this deploy's permalink
        (deploy_ssl_url, falling back to the site URL); site_url is the
        site's stable https URL. Raises NetlifyError kind 'deploy_failed'
        (Netlify's error_message, scrubbed) or 'deploy_timeout' after
        max_wait_s of processing; both carry .deploy_id."""
        sid = self._check_site_id(site_id)
        body = self._check_zip(zip_bytes)
        max_wait = _positive(max_wait_s, self.DEPLOY_MAX_WAIT_S)
        interval = _positive(poll_interval_s, self.POLL_INTERVAL_S)
        t0 = self._clock()
        resp = self._call("POST", f"/sites/{sid}/deploys", action="deploy_zip",
                          data=body, content_type="application/zip",
                          read_timeout=self.UPLOAD_READ_TIMEOUT_S,
                          not_found_msg=MSG_SITE_GONE, not_found_kind="site_gone")
        deploy = self._json_dict(resp, action="deploy_zip")
        deploy_id = _text(deploy.get("id"))
        if not deploy_id or not _ID_SHAPE.match(deploy_id) or ".." in deploy_id:
            self._emit("deploy_error", site_id=sid, reason="no_deploy_id",
                       status=resp.status_code, elapsed_ms=self._ms(t0))
            raise NetlifyError(MSG_BAD_REPLY, resp.status_code, kind="bad_reply")
        state = _deploy_state(deploy)
        self._emit("deploy_started", site_id=sid, deploy_id=deploy_id, bytes=len(body),
                   state=state, status=resp.status_code, elapsed_ms=self._ms(t0))

        wait_start = self._clock()
        deadline = wait_start + max_wait
        # A hard cap on polls too, so a clock that doesn't move can't spin forever.
        max_polls = int(max_wait / interval) + 5
        polls = 0
        last_state = state
        while True:
            if state == DEPLOY_READY:
                result = {
                    "deploy_id": deploy_id,
                    "state": DEPLOY_READY,
                    "url": _pick_url(deploy.get("deploy_ssl_url"), deploy.get("ssl_url"),
                                     deploy.get("deploy_url"), deploy.get("url")),
                    "site_url": _pick_url(deploy.get("ssl_url"), deploy.get("url")),
                    "elapsed_s": round(self._clock() - t0, 1),
                }
                self._emit("deploy_ready", site_id=sid, deploy_id=deploy_id, polls=polls,
                           elapsed_ms=self._ms(t0))
                return result
            if state in DEPLOY_FAILED_STATES:
                reason = self._scrub(" ".join(str(deploy.get("error_message") or "").split()))[:300]
                self._emit("deploy_error", site_id=sid, deploy_id=deploy_id,
                           reason=f"state_{state}", error=reason, polls=polls,
                           elapsed_ms=self._ms(t0))
                why = f": {reason.rstrip('.')}." if reason else " and didn't say why."
                raise NetlifyError(self._scrub(
                    f"Netlify couldn't publish the snapshot (deploy {deploy_id}){why} "
                    "Publish again; if it keeps failing, copy the Debug log from "
                    "Settings into Claude Code."),
                    None, kind="deploy_failed", deploy_id=deploy_id)
            now = self._clock()
            if now >= deadline or polls >= max_polls:
                self._emit("deploy_error", site_id=sid, deploy_id=deploy_id,
                           reason="timeout", state=state, polls=polls,
                           elapsed_ms=self._ms(t0))
                raise NetlifyError(self._scrub(
                    f"Netlify is still processing the snapshot (deploy {deploy_id}); "
                    "check again in a minute."),
                    None, kind="deploy_timeout", deploy_id=deploy_id)
            self._sleep(min(interval, max(0.0, deadline - now)))
            try:
                deploy = self._fetch_deploy(deploy_id)
            except NetlifyError as e:
                e.deploy_id = e.deploy_id or deploy_id
                self._emit("deploy_error", site_id=sid, deploy_id=deploy_id,
                           reason=e.kind, status=e.status, polls=polls,
                           elapsed_ms=self._ms(t0))
                raise
            polls += 1
            state = _deploy_state(deploy)
            if state != last_state:
                self._emit("deploy_state", deploy_id=deploy_id, state=state,
                           known=state in DEPLOY_IN_PROGRESS_STATES
                           or state == DEPLOY_READY or state in DEPLOY_FAILED_STATES,
                           polls=polls, elapsed_ms=self._ms(t0))
                last_state = state

    def get_deploy(self, deploy_id: str) -> dict:
        """One look at a deploy (e.g. after a deploy_timeout). Returns
        {"deploy_id", "state", "url", "site_url", "error_message"}."""
        did = _text(deploy_id)
        if not did or not _ID_SHAPE.match(did) or ".." in did:
            raise NetlifyError("That Netlify deploy id isn't valid. Publish again.",
                               None, kind="bad_input")
        deploy = self._fetch_deploy(did)
        error_message = _text(deploy.get("error_message"))
        return {
            "deploy_id": did,
            "state": _deploy_state(deploy),
            "url": _pick_url(deploy.get("deploy_ssl_url"), deploy.get("ssl_url"),
                             deploy.get("deploy_url"), deploy.get("url")),
            "site_url": _pick_url(deploy.get("ssl_url"), deploy.get("url")),
            "error_message": self._scrub(error_message)[:300] if error_message else None,
        }

    def delete_site(self, site_id: str) -> None:
        """Delete the site. A 404 means it's already gone: not an error."""
        sid = self._check_site_id(site_id)
        t0 = self._clock()
        resp = self._call("DELETE", f"/sites/{sid}", action="delete_site", allow=(404,))
        self._emit("delete_site", site_id=sid, status=resp.status_code,
                   outcome="already_gone" if resp.status_code == 404 else "deleted",
                   elapsed_ms=self._ms(t0))

    # --- HTTP plumbing ----------------------------------------------------------

    def _call(self, method: str, path: str, *, action: str, allow=(),
              not_found_msg: str | None = None, not_found_kind: str = "not_found",
              json=None, data=None, content_type: str | None = None,
              read_timeout: float | None = None) -> requests.Response:
        """Send, then return the response if it's 2xx or in `allow`;
        otherwise raise the NetlifyError for its status."""
        resp = self._send(method, path, action=action, json=json, data=data,
                          content_type=content_type, read_timeout=read_timeout)
        status = resp.status_code
        if (isinstance(status, int) and 200 <= status < 300) or status in allow:
            return resp
        raise self._http_error(resp, method=method, path=path, action=action,
                               not_found_msg=not_found_msg, not_found_kind=not_found_kind)

    def _send(self, method: str, path: str, *, action: str, json=None, data=None,
              content_type: str | None = None,
              read_timeout: float | None = None) -> requests.Response:
        token = self._require_token(action)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        kwargs = {
            "headers": headers,
            "auth": _BearerAuth(token),
            "timeout": (self.CONNECT_TIMEOUT_S, read_timeout or self.READ_TIMEOUT_S),
        }
        if json is not None:
            kwargs["json"] = json
        if data is not None:
            kwargs["data"] = data
        url = NETLIFY_API + path
        for attempt in (1, 2):
            t0 = self._clock()
            try:
                resp = self._session.request(method, url, **kwargs)
            except requests.Timeout as e:
                raise self._transport_error(e, "timeout", method, path, action, t0) from None
            except requests.ConnectionError as e:
                raise self._transport_error(e, "connection", method, path, action, t0) from None
            except requests.RequestException as e:
                raise self._transport_error(e, "request", method, path, action, t0) from None
            # A POST (create site, upload) is not idempotent: after a 502/504
            # the gateway may have passed the first request through, and a
            # retry would leave a stray empty site. Only retry POSTs when
            # Netlify definitely didn't act on them (429 / 503).
            retryable = self.RETRY_STATUSES if method.upper() != "POST" else self.POST_RETRY_STATUSES
            if attempt == 1 and resp.status_code in retryable:
                wait = self._retry_wait(resp)
                self._emit("http_retry", action=action, method=method, path=path,
                           status=resp.status_code, wait_s=wait,
                           elapsed_ms=self._ms(t0))
                self._sleep(wait)
                continue
            return resp
        return resp  # unreachable; keeps type checkers happy

    def _require_token(self, action: str) -> str:
        if self._token and _TOKEN_SHAPE.match(self._token):
            return self._token
        self._emit("http_error", action=action, error_kind="bad_token",
                   empty=not self._token)
        raise NetlifyError(MSG_NO_TOKEN if not self._token else MSG_TOKEN_SHAPE,
                           None, kind="bad_token")

    def _retry_wait(self, resp) -> float:
        """Seconds to wait before the one retry: Retry-After (seconds or an
        HTTP date), else X-RateLimit-Reset, else 2s; capped at 10s."""
        headers = getattr(resp, "headers", None) or {}
        wait = None
        raw = str(headers.get("Retry-After") or "").strip()
        if raw:
            try:
                wait = float(raw)
            except ValueError:
                try:
                    when = email.utils.parsedate_to_datetime(raw)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=datetime.timezone.utc)
                    wait = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                except (TypeError, ValueError, IndexError, OverflowError):
                    wait = None
        if wait is None:
            reset = str(headers.get("X-RateLimit-Reset") or "").strip()
            if reset.isdigit():
                wait = int(reset) - time.time()
        if wait is None or not math.isfinite(wait):
            wait = self.RETRY_AFTER_DEFAULT_S
        return round(min(max(wait, 0.0), self.RETRY_AFTER_CAP_S), 2)

    def _http_error(self, resp, *, method: str, path: str, action: str,
                    not_found_msg: str | None, not_found_kind: str) -> NetlifyError:
        status = resp.status_code
        detail = self._detail(resp)
        said = f" Netlify said: {detail.rstrip('.')}." if detail else ""
        if status == 401:
            msg, kind = MSG_AUTH, "auth"
        elif status == 403:
            msg, kind = MSG_FORBIDDEN, "forbidden"
        elif status == 404:
            msg, kind = not_found_msg or MSG_NOT_FOUND, not_found_kind
        elif status == 413:
            msg, kind = ("The snapshot is too large for Netlify to accept (HTTP 413). "
                         "Copy the Debug log from Settings into Claude Code."), "rejected"
        elif status == 429:
            msg, kind = MSG_RATE_LIMITED, "rate_limited"
        elif isinstance(status, int) and status >= 500:
            msg, kind = MSG_SERVER.format(status=status), "server"
        else:
            msg, kind = (f"Netlify turned down the request (HTTP {status}).{said} Try again "
                         "in a minute. If it keeps happening, copy the Debug log from "
                         "Settings into Claude Code."), "rejected"
        self._emit("http_error", action=action, method=method, path=path, status=status,
                   error_kind=kind, detail=detail)
        return NetlifyError(self._scrub(msg), status if isinstance(status, int) else None,
                            kind=kind)

    def _transport_error(self, exc, kind: str, method: str, path: str, action: str,
                         t0: float) -> NetlifyError:
        self._emit("http_error", action=action, method=method, path=path, status=None,
                   error_kind=kind, error=f"{type(exc).__name__}: {exc}",
                   elapsed_ms=self._ms(t0))
        if kind == "request":
            msg = (f"Couldn't talk to Netlify ({type(exc).__name__}). Check your internet "
                   "connection and try again.")
        else:
            msg = MSG_NETWORK
        return NetlifyError(msg, None, kind="network")

    def _detail(self, resp) -> str:
        """Netlify's own short explanation from an error body, scrubbed,
        one line, at most 200 characters. HTML error pages are dropped."""
        text = ""
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - any unreadable body just means no detail
            body = None
        if isinstance(body, dict):
            parts = []
            for key in ("message", "error_message", "error"):
                if isinstance(body.get(key), str):
                    parts.append(body[key])
                    break
            errors = body.get("errors")
            if isinstance(errors, dict):
                for field, problems in list(errors.items())[:5]:
                    problems = problems if isinstance(problems, list) else [problems]
                    parts.append(f"{field} {', '.join(str(p) for p in problems[:3])}")
            elif isinstance(errors, list):
                parts.extend(str(p) for p in errors[:5])
            elif isinstance(errors, str):
                parts.append(errors)
            text = "; ".join(parts)
        elif isinstance(body, str):
            text = body
        elif body is None:
            try:
                text = resp.text or ""
            except Exception:  # noqa: BLE001
                text = ""
            if text.lstrip().startswith("<"):
                text = ""
        text = self._scrub(text)
        return self._scrub(" ".join(text.split()))[:200]

    def _json_dict(self, resp, *, action: str) -> dict:
        """The reply as a JSON object, or a 'bad_reply' NetlifyError."""
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - not JSON, truncated, too deep...
            data = None
        if isinstance(data, dict):
            return data
        self._emit("http_error", action=action, status=resp.status_code,
                   error_kind="bad_reply", got=type(data).__name__)
        raise NetlifyError(MSG_BAD_REPLY, resp.status_code, kind="bad_reply")

    def _fetch_deploy(self, deploy_id: str) -> dict:
        resp = self._call(
            "GET", f"/deploys/{deploy_id}", action="get_deploy",
            not_found_msg=(f"Netlify couldn't find deploy {deploy_id}. Publish again; "
                           "if it keeps happening, run snapshot setup again."))
        return self._json_dict(resp, action="get_deploy")

    def _first_account_slug(self) -> str | None:
        """Best-effort team slug for display. Any failure is just None."""
        try:
            resp = self._send("GET", "/accounts", action="verify_accounts")
            accounts = resp.json() if resp.status_code == 200 else None
        except Exception as e:  # noqa: BLE001 - a display nicety must never fail verify
            self._emit("http_error", action="verify_accounts",
                       error_kind=getattr(e, "kind", type(e).__name__))
            return None
        if not isinstance(accounts, list):
            self._emit("http_error", action="verify_accounts", status=resp.status_code,
                       error_kind="no_accounts")
            return None
        for account in accounts:
            if isinstance(account, dict) and _text(account.get("slug")):
                return _text(account.get("slug"))
        return None

    def _site_summary(self, data: dict, *, action: str, fallback_name: str | None = None) -> dict:
        site_id = _text(data.get("site_id")) or _text(data.get("id"))
        if not site_id:
            self._emit("http_error", action=action, error_kind="bad_reply", got="no_site_id")
            raise NetlifyError(MSG_BAD_REPLY, None, kind="bad_reply")
        return {
            "site_id": site_id,
            "name": _text(data.get("name")) or fallback_name,
            "url": _pick_url(data.get("ssl_url"), data.get("url")),
            "admin_url": _pick_url(data.get("admin_url")),
        }

    def _check_site_id(self, site_id) -> str:
        sid = _text(site_id)
        if not sid:
            raise NetlifyError(MSG_NO_SITE, None, kind="bad_input")
        if not _ID_SHAPE.match(sid) or ".." in sid:
            raise NetlifyError(MSG_BAD_SITE_ID, None, kind="bad_input")
        return sid

    @staticmethod
    def _check_zip(zip_bytes) -> bytes:
        """Bytes that start like a zip (PK) and are at least an empty
        archive's 22 bytes; anything else is never uploaded."""
        if isinstance(zip_bytes, (bytearray, memoryview)):
            zip_bytes = bytes(zip_bytes)
        if not isinstance(zip_bytes, bytes) or len(zip_bytes) < 22 \
                or not zip_bytes.startswith(b"PK"):
            raise NetlifyError(MSG_BAD_ZIP, None, kind="bad_input")
        return zip_bytes

    # --- logging ----------------------------------------------------------------

    def _scrub(self, text) -> str:
        s = text if isinstance(text, str) else str(text)
        for secret in self._secrets:
            s = s.replace(secret, REDACTED)
        return redact(s)

    def _ms(self, t0: float) -> int:
        return int(max(0.0, self._clock() - t0) * 1000)

    def _emit(self, event: str, **fields) -> None:
        """Log to the app log and the caller's log_event callback. Every
        string is scrubbed; a callback that raises never breaks a call."""
        safe = {}
        for key, value in fields.items():
            if key.lower() in _NEVER_LOG_KEYS:
                continue
            if value is None or isinstance(value, (bool, int, float)):
                safe[key] = value
            else:
                safe[key] = self._scrub(value)[:300]
        level = logging.WARNING if event in ("http_error", "deploy_error") else logging.INFO
        logger.log(level, "netlify %s %s", event, safe)
        if self._log_event is None:
            return
        try:
            self._log_event(event, **safe)
        except Exception as e:  # noqa: BLE001 - logging must never break a publish
            logger.warning("netlify log_event callback failed: %s", type(e).__name__)
