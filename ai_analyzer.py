"""
AI helpers for RepoDoctor.
Model catalog, cost estimation, and the shared Anthropic request path that
every JSON-producing feature (project summaries, briefs, recaps) goes
through.
"""

import json
import logging
import re
import threading

import anthropic
import requests

logger = logging.getLogger(__name__)

# Single source of truth for selectable models. Everything that needs a
# model id, a Settings/tracker dropdown, or validation reads from here —
# a new Claude release is a one-line change.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MODEL_CHOICES = [
    ("claude-haiku-4-5-20251001", "Haiku 4.5 — Fastest, cheapest (~$1/M input) — Recommended"),
    ("claude-sonnet-5", "Sonnet 5 — Balanced quality & cost (~$3/M input; intro ~$2/M through Aug 2026)"),
    ("claude-opus-4-8", "Opus 4.8 — Most capable, highest cost (~$5/M input)"),
]
VALID_MODELS = {model_id for model_id, _ in MODEL_CHOICES}


def thinking_kwargs(model: str) -> dict:
    """Extra request kwargs to keep AI responses fast and untruncated.

    Sonnet 5 runs adaptive thinking by DEFAULT whenever the `thinking` field
    is omitted — that spends output tokens and, under the small max_tokens
    budgets in this app, can eat the budget and truncate the JSON we need.
    Disable thinking so behavior matches the prior Sonnet 4.5. Opus 4.8 and
    Haiku 4.5 already default to no thinking, so they get nothing extra
    (passing `disabled` to Haiku 4.5 is avoided intentionally)."""
    if "sonnet-5" in model:
        return {"thinking": {"type": "disabled"}}
    return {}


def extract_response_text(message) -> str:
    """First text block of an Anthropic response, or "".

    A response can validly contain no text block (e.g. a refusal) —
    indexing content[0].text unconditionally crashes before any caller's
    fallback can run. Every Anthropic call site should use this.
    """
    return next(
        (b.text for b in message.content if getattr(b, "type", "") == "text"), ""
    ).strip()


def extract_json_object(text: str) -> dict:
    """Find and parse the first complete JSON object in `text`.

    Tolerates leading/trailing prose, markdown code fences (```/```json),
    and trailing commentary after the closing brace. Tracks brace depth
    while respecting string literals so braces inside strings don't break
    the scan.
    """
    if not text:
        raise ValueError("Empty AI response")

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in AI response")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])

    raise ValueError("Unbalanced braces in AI response")


def request_json(
    client,
    *,
    model: str,
    messages: list,
    schema: dict,
    max_tokens: int,
    system: str | None = None,
) -> tuple[dict, dict]:
    """Messages API call whose response must be a JSON object matching
    `schema`. The single request path for every JSON-producing AI feature.

    Uses structured outputs (output_config.format) so the API itself
    constrains the model to emit valid JSON — the root fix for the
    "No JSON object found in AI response" failures that happened when a
    model answered a summary prompt with prose instead of JSON. Passed via
    extra_body so it works on any anthropic SDK version we might have
    installed (typed output_config support is newer than our pin).

    The two documented cases where output can still deviate from the schema
    are handled explicitly: max_tokens truncation gets one retry with double
    the budget, and a safety refusal raises with a clear message. If parsing
    still fails, the raised error includes the start of what the model
    actually said, so the Projects-page error / action log is enough to
    debug from.

    Returns (parsed, usage) with usage = {input_tokens, output_tokens,
    model} summed across attempts.
    """
    usage = {"input_tokens": 0, "output_tokens": 0, "model": model}

    def _call(budget: int):
        kwargs: dict = dict(
            model=model,
            max_tokens=budget,
            messages=messages,
            **thinking_kwargs(model),
            extra_body={
                "output_config": {
                    "format": {"type": "json_schema", "schema": schema},
                },
            },
        )
        if system is not None:
            kwargs["system"] = system
        message = client.messages.create(**kwargs)
        usage["input_tokens"] += message.usage.input_tokens
        usage["output_tokens"] += message.usage.output_tokens
        return message

    message = _call(max_tokens)
    if getattr(message, "stop_reason", None) == "max_tokens":
        message = _call(max_tokens * 2)
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise ValueError(
                f"AI response was cut off by the output-token limit even "
                f"after retrying with {max_tokens * 2} tokens. Regenerate, "
                f"or switch models in Settings."
            )

    if getattr(message, "stop_reason", None) == "refusal":
        raise ValueError(
            "The AI declined this request (safety refusal). Regenerate, or "
            "check the repo's docs for content that could trip a filter."
        )

    raw = extract_response_text(message)
    try:
        return extract_json_object(raw), usage
    except ValueError as e:
        snippet = " ".join(raw.split())[:160] or "(empty response)"
        raise ValueError(f'{e}. The model ({model}) replied: "{snippet}"') from e


def estimate_cost(input_tokens: int, output_tokens: int = 1000, model: str = DEFAULT_MODEL) -> float:
    """Estimate cost in USD. Rough estimates based on public pricing.

    Per MTok: Opus 4.8 $5/$25, Sonnet 5 $3/$15 (intro $2/$10 through
    Aug 2026 — we bill at the $3/$15 sticker to stay conservative),
    Haiku 4.5 $1/$5.
    """
    # Explicit per-model rates ($ per MTok, input/output). Substring matching
    # alone silently mispriced any model without "opus"/"sonnet" in its id as
    # Haiku — e.g. Fable 5 (really $10/$50) would have billed at $1/$5.
    RATES = {
        "claude-opus-4-8": (5.0, 25.0),
        "claude-sonnet-5": (3.0, 15.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0),
    }
    if model in RATES:
        in_rate, out_rate = RATES[model]
    elif "opus" in model:
        in_rate, out_rate = 5.0, 25.0
    elif "sonnet" in model:
        in_rate, out_rate = 3.0, 15.0
    elif "fable" in model:
        in_rate, out_rate = 10.0, 50.0
    else:  # unknown / haiku
        in_rate, out_rate = 1.0, 5.0
    input_cost = input_tokens * in_rate / 1_000_000
    output_cost = output_tokens * out_rate / 1_000_000
    return round(input_cost + output_cost, 4)


# --- API key check -----------------------------------------------------------
# Login waits on this, so it must never hang: one attempt (the SDK's default
# two retries with backoff would multiply the wait), short per-phase HTTP
# timeouts, and a hard wall-clock cap on top for the cases per-phase timeouts
# don't cover (a stuck DNS lookup, a server that drips bytes).
KEY_OK = "ok"
KEY_INVALID = "invalid"
KEY_UNVERIFIED = "unverified"
VERIFY_CONNECT_SECONDS = 2.0
VERIFY_READ_SECONDS = 2.5
VERIFY_DEADLINE_SECONDS = 5.0

# Anything shaped like an Anthropic key, so a key that some library echoes
# into an error string can never reach a log line.
_KEY_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def _redact(text: str, api_key: str) -> str:
    if api_key:
        text = text.replace(api_key, "[redacted]")
    return _KEY_SHAPE.sub("[redacted]", text)


ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
# Anthropic API keys look like sk-ant-api03-…; used to refuse obvious
# non-keys before they can replace a working one in Settings.
KEY_FORMAT = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$")


def looks_like_anthropic_key(value: str | None) -> bool:
    return isinstance(value, str) and bool(KEY_FORMAT.match(value.strip()))


def _probe_models_http(key: str) -> tuple[str, str]:
    """GET /v1/models?limit=1 without the SDK. Same outcomes as the SDK path."""
    try:
        resp = requests.get(
            ANTHROPIC_MODELS_URL, params={"limit": 1},
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=(VERIFY_CONNECT_SECONDS, VERIFY_READ_SECONDS),
        )
    except requests.Timeout:
        return KEY_UNVERIFIED, "Timed out reaching Anthropic."
    except requests.RequestException as e:
        logger.warning("Anthropic key check (http): %s: %s", type(e).__name__, _redact(str(e), key)[:200])
        return KEY_UNVERIFIED, "Couldn't connect to Anthropic (network error)."
    code = resp.status_code
    if code == 200:
        return KEY_OK, "Anthropic accepted the API key."
    if code == 401:
        return KEY_INVALID, "Anthropic rejected the API key (HTTP 401): it's wrong, revoked, or disabled."
    kind = "server error" if code >= 500 else {403: "permission denied", 429: "rate limited"}.get(code, "unexpected response")
    return KEY_UNVERIFIED, f"Anthropic returned HTTP {code} ({kind})."


def verify_anthropic_key(api_key: str | None) -> tuple[str, str]:
    """Check an Anthropic API key with the cheapest call that proves it works:
    GET /v1/models?limit=1, which spends no tokens.

    Returns (status, message):
      "ok"          Anthropic accepted the key.
      "invalid"     no key is set, or Anthropic answered 401 (wrong, revoked,
                    or disabled key).
      "unverified"  anything else: timeout, network error, 5xx, 429, 403, an
                    unexpected SDK error. The key may well be fine.

    Never raises and never takes more than VERIFY_DEADLINE_SECONDS. The
    message is safe to show and log: it never contains the key. "ok" proves
    the key authenticates, not that the account has credit left; a zero
    balance still only shows up when a generation is attempted.
    """
    key = api_key.strip() if isinstance(api_key, str) else ""
    if not key:
        logger.warning("Anthropic key check: invalid (no key set)")
        return KEY_INVALID, "No Anthropic API key is set."

    outcome: dict[str, tuple[str, str]] = {}

    def _probe():
        try:
            client = anthropic.Anthropic(
                api_key=key,
                max_retries=0,
                timeout=anthropic.Timeout(VERIFY_READ_SECONDS, connect=VERIFY_CONNECT_SECONDS),
            )
            models_api = getattr(client, "models", None)
            if models_api is not None and hasattr(models_api, "list"):
                client.models.list(limit=1)
            else:
                # Older SDKs the requirements still allow (anthropic 0.40.x)
                # have no Models resource; ask the same endpoint directly so
                # the check works on any installed version.
                outcome["result"] = _probe_models_http(key)
                return
            outcome["result"] = (KEY_OK, "Anthropic accepted the API key.")
        except anthropic.AuthenticationError:
            outcome["result"] = (
                KEY_INVALID,
                "Anthropic rejected the API key (HTTP 401): it's wrong, revoked, or disabled.",
            )
        except anthropic.APITimeoutError:  # subclass of APIConnectionError: catch first
            outcome["result"] = (KEY_UNVERIFIED, "Timed out reaching Anthropic.")
        except anthropic.APIConnectionError as e:
            logger.warning("Anthropic key check: connection error: %s",
                           _redact(str(e), key)[:200])
            outcome["result"] = (KEY_UNVERIFIED, "Couldn't connect to Anthropic (network error).")
        except anthropic.APIStatusError as e:
            code = getattr(e, "status_code", "?")
            if isinstance(code, int) and code >= 500:
                kind = "server error"
            else:
                kind = {403: "permission denied", 429: "rate limited"}.get(code, "unexpected response")
            outcome["result"] = (KEY_UNVERIFIED, f"Anthropic returned HTTP {code} ({kind}).")
        except Exception as e:  # noqa: BLE001 - a key check must never take login down
            logger.warning("Anthropic key check: unexpected %s: %s",
                           type(e).__name__, _redact(str(e), key)[:200])
            outcome["result"] = (
                KEY_UNVERIFIED, f"Unexpected error while checking the key ({type(e).__name__}).")

    # Daemon thread + join(timeout) is the hard cap: if the call is still
    # stuck at the deadline we answer "unverified" and let the thread finish
    # (bounded by its own HTTP timeouts) in the background.
    worker = threading.Thread(target=_probe, name="anthropic-key-check", daemon=True)
    try:
        worker.start()
    except RuntimeError as e:  # "can't start new thread": never let that 500 a login
        logger.warning("Anthropic key check: could not start: %s", e)
        return KEY_UNVERIFIED, "Couldn't run the key check (out of threads)."
    worker.join(VERIFY_DEADLINE_SECONDS)
    status, message = outcome.get("result") or (
        KEY_UNVERIFIED, f"Anthropic didn't answer within {VERIFY_DEADLINE_SECONDS:g} seconds.")

    if status == KEY_OK:
        logger.info("Anthropic key check: ok")
    else:
        logger.warning("Anthropic key check: %s (%s)", status, message)
    return status, message
