"""
AI helpers for RepoDoctor.
Model catalog, cost estimation, and the shared Anthropic request path that
every JSON-producing feature (project summaries, briefs, recaps) goes
through.
"""

import json

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
