"""
AI Analysis Engine for RepoDoctor.
Uses the Anthropic API (Claude) to analyze branch content.
"""

import json
import os

import anthropic

# Single source of truth for selectable models. Everything that needs a
# model id, a Settings/tracker dropdown, or validation reads from here —
# a new Claude release is a one-line change.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MODEL_CHOICES = [
    ("claude-haiku-4-5-20251001", "Haiku 4.5 — Fastest, cheapest (~$1/M input) — Recommended"),
    ("claude-sonnet-4-5-20250929", "Sonnet 4.5 — Balanced quality & cost (~$3/M input)"),
    ("claude-opus-4-6", "Opus 4.6 — Most capable, highest cost (~$5/M input)"),
]
VALID_MODELS = {model_id for model_id, _ in MODEL_CHOICES}

SYSTEM_PROMPT = """You are a Git branch analyst helping a non-developer understand their repository branches. Your job is to analyze branch data and produce clear, actionable recommendations.

Rules:
- Write all summaries in plain English — no jargon, no commit hashes in user-facing text
- Explain what the branch DOES, not just what files it touches
- Make Claude Code instructions complete and copy/paste ready — include cd, branch verification, and rollback steps
- Map changes to spec features when a spec is available
- Be conservative with risk — when in doubt, flag MEDIUM or HIGH
- Return valid JSON only, no markdown fences or extra text

Return a JSON object with these exact fields:
{
  "plain_english_summary": "ONE short sentence (max 20 words) describing what was done on this branch overall, written for a non-developer. No semicolons, no compound 'and X and Y' lists — just the headline.",
  "screen_changes": [
    {"screen": "Name of the screen/page/area (e.g. 'Dashboard', 'Henry Branches', 'Login', 'Settings'). If the change is not user-facing, use a short area name like 'Backend / API', 'Data model', or 'Build config'.", "change": "One short phrase describing what changed on that screen — start with a verb (Added, Removed, Renamed, Fixed, etc.)"}
  ],
  "feature_assessment": "SHOULD_MERGE | OPTIONAL | OBSOLETE | UNCLEAR",
  "risk_level": "LOW | MEDIUM | HIGH",
  "conflict_prediction": "Which files likely conflict and why, or 'No conflicts expected'",
  "merge_strategy": "fast-forward | merge | rebase",
  "claude_code_instructions": "Complete, copy/paste-ready text for Claude Code",
  "spec_alignment": "Which features this relates to, or null if no spec provided"
}

For screen_changes: produce one bullet per distinct screen or area touched. Group multiple files for the same screen into a single bullet. Infer screen names from template/route names (e.g. templates/dashboard.html → 'Dashboard', templates/henry.html → 'Henry Branches'). Aim for 1–6 bullets — never more than 8."""


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


def estimate_tokens(branch_data: dict, spec_text: str | None = None) -> int:
    """Estimate token count for an analysis request."""
    text = json.dumps(branch_data)
    if spec_text:
        text += spec_text
    # Rough estimate: ~4 chars per token
    return len(text) // 4 + 500  # 500 for system prompt overhead


def estimate_cost(input_tokens: int, output_tokens: int = 1000, model: str = DEFAULT_MODEL) -> float:
    """Estimate cost in USD. Rough estimates based on public pricing.

    Per MTok: Opus 4.6 $5/$25, Sonnet 4.5 $3/$15, Haiku 4.5 $1/$5.
    """
    if "opus" in model:
        input_cost = input_tokens * 5.0 / 1_000_000
        output_cost = output_tokens * 25.0 / 1_000_000
    elif "sonnet" in model:
        input_cost = input_tokens * 3.0 / 1_000_000
        output_cost = output_tokens * 15.0 / 1_000_000
    else:  # haiku
        input_cost = input_tokens * 1.0 / 1_000_000
        output_cost = output_tokens * 5.0 / 1_000_000
    return round(input_cost + output_cost, 4)


def build_analysis_prompt(
    repo_name: str,
    branch_data: dict,
    default_branch: str,
    default_branch_commits: list[dict] | None = None,
    spec_text: str | None = None,
    local_path: str | None = None,
) -> str:
    """Build the user prompt for branch analysis."""
    parts = [
        f"Analyze this branch for repository: {repo_name}",
        f"\nBranch: {branch_data['name']}",
        f"Default branch: {default_branch}",
        f"Commits ahead: {branch_data['ahead_by']}",
        f"Commits behind: {branch_data['behind_by']}",
        f"Classification: {branch_data['classification']}",
        f"Last commit: {branch_data.get('last_commit_date', 'Unknown')}",
        f"Last author: {branch_data.get('last_commit_author', 'Unknown')}",
        f"Has open PR: {branch_data.get('has_pr', False)}",
    ]

    if branch_data.get("commit_messages"):
        parts.append("\nCommit history (branch-only commits):")
        for c in branch_data["commit_messages"]:
            parts.append(f"  - {c['message']} (by {c['author']}, {c.get('date', '')})")

    if branch_data.get("files_changed"):
        parts.append("\nFiles changed:")
        for f in branch_data["files_changed"]:
            parts.append(f"  - {f['filename']}: +{f['additions']}/-{f['deletions']} ({f['status']})")

    if default_branch_commits:
        parts.append(f"\nRecent commits on {default_branch} (for context):")
        for c in default_branch_commits[:10]:
            msg = c["commit"]["message"].split("\n")[0] if c.get("commit") else "Unknown"
            parts.append(f"  - {msg}")

    if spec_text:
        parts.append(f"\nProduct specification for this repo:\n{spec_text}")
    else:
        parts.append("\nNo product spec provided — omit spec_alignment from response (set to null).")

    cd_path = local_path or f"~/claudesync2/{repo_name}"
    parts.append(f"\nLocal project path for Claude Code instructions: {cd_path}")
    parts.append(f"Use this exact path in the cd command of the instructions.")

    return "\n".join(parts)


def analyze_branch(
    api_key: str,
    repo_name: str,
    branch_data: dict,
    default_branch: str,
    default_branch_commits: list[dict] | None = None,
    spec_text: str | None = None,
    local_path: str | None = None,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Analyze a branch using the Anthropic API. Returns structured analysis."""
    client = anthropic.Anthropic(api_key=api_key)

    user_prompt = build_analysis_prompt(
        repo_name, branch_data, default_branch,
        default_branch_commits, spec_text, local_path,
    )

    message = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    # A response can validly have no text block (e.g. a refusal) — indexing
    # content[0] unconditionally would crash before the fallback below runs.
    response_text = next(
        (b.text for b in message.content if getattr(b, "type", "") == "text"), ""
    ).strip()

    try:
        result = extract_json_object(response_text)
    except (ValueError, json.JSONDecodeError):
        result = {
            "plain_english_summary": (
                "AI response could not be parsed — see Claude Code instructions for manual review."
            ),
            "screen_changes": [],
            "feature_assessment": "UNCLEAR",
            "risk_level": "MEDIUM",
            "conflict_prediction": "Unable to parse AI response",
            "merge_strategy": "merge",
            "claude_code_instructions": f"# AI analysis returned non-JSON response.\n# Manual review recommended for {branch_data['name']}",
            "spec_alignment": None,
        }

    result["_usage"] = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "model": model,
    }

    return result
