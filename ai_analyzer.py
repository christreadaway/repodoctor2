"""
AI Analysis Engine for RepDoctor2.
Uses the Anthropic API (Claude) to analyze branch content.
"""

import json
import os

import anthropic

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
  "plain_english_summary": "2-3 sentences describing what this branch does, written for a non-developer",
  "feature_assessment": "SHOULD_MERGE | OPTIONAL | OBSOLETE | UNCLEAR",
  "risk_level": "LOW | MEDIUM | HIGH",
  "conflict_prediction": "Which files likely conflict and why, or 'No conflicts expected'",
  "merge_strategy": "fast-forward | merge | rebase",
  "claude_code_instructions": "Complete, copy/paste-ready text for Claude Code",
  "spec_alignment": "Which features this relates to, or null if no spec provided"
}"""


def estimate_tokens(branch_data: dict, spec_text: str | None = None) -> int:
    """Estimate token count for an analysis request."""
    text = json.dumps(branch_data)
    if spec_text:
        text += spec_text
    # Rough estimate: ~4 chars per token
    return len(text) // 4 + 500  # 500 for system prompt overhead


def estimate_cost(input_tokens: int, output_tokens: int = 1000, model: str = "claude-haiku-4-5-20251001") -> float:
    """Estimate cost in USD. Rough estimates based on public pricing."""
    if "opus" in model:
        input_cost = input_tokens * 15.0 / 1_000_000
        output_cost = output_tokens * 75.0 / 1_000_000
    elif "sonnet" in model:
        input_cost = input_tokens * 3.0 / 1_000_000
        output_cost = output_tokens * 15.0 / 1_000_000
    else:  # haiku
        input_cost = input_tokens * 0.80 / 1_000_000
        output_cost = output_tokens * 4.0 / 1_000_000
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
    model: str = "claude-haiku-4-5-20251001",
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

    response_text = message.content[0].text.strip()

    # Parse JSON, handling potential markdown fences
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        result = {
            "plain_english_summary": response_text,
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
