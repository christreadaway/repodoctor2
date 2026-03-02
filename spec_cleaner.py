"""Markdown cleanup + What's Next extraction for repo detail view.

Cleans raw .md content for readable display in the terminal UI,
and extracts actionable next-steps from specs and conversations.
"""

import re


def clean_markdown(text: str) -> str:
    """Strip markdown noise and return clean, readable text.

    Removes: front-matter blocks, excessive decoration (--- lines, ===),
    badge syntax, HTML tags, redundant blank lines, trailing whitespace.
    Keeps: headings as bold labels, bullet lists, tables, actual content.
    """
    if not text:
        return ""

    lines = text.splitlines()
    cleaned = []
    in_frontmatter = False
    prev_blank = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip YAML front-matter blocks (--- to ---)
        if stripped == "---" and i == 0:
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue

        # Skip pure decoration lines (---, ===, ***)
        if re.match(r'^[-=*]{3,}\s*$', stripped):
            continue

        # Skip HTML tags
        if re.match(r'^</?[a-z]', stripped, re.IGNORECASE):
            continue

        # Convert markdown headings to clean labels
        heading_match = re.match(r'^(#{1,6})\s+(.+)', stripped)
        if heading_match:
            text_content = heading_match.group(2)
            # Strip trailing # characters
            text_content = re.sub(r'\s*#+\s*$', '', text_content)
            stripped = text_content.upper() if len(heading_match.group(1)) <= 2 else text_content
            line = stripped

        # Clean inline markdown
        # Bold: **text** or __text__ -> text
        line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
        line = re.sub(r'__(.+?)__', r'\1', line)
        # Italic: *text* or _text_ (but not in the middle of words)
        line = re.sub(r'(?<!\w)\*([^*]+?)\*(?!\w)', r'\1', line)
        # Inline code: `text` -> text
        line = re.sub(r'`([^`]+)`', r'\1', line)
        # Links: [text](url) -> text
        line = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', line)
        # Images: ![alt](url) -> (remove entirely)
        line = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', line)
        # Blockquote markers
        line = re.sub(r'^>\s*', '', line)
        # Badge-style markers: > **text:** -> text:
        line = re.sub(r'^>\s*', '', line)

        stripped = line.strip()

        # Collapse multiple blank lines into one
        if not stripped:
            if prev_blank:
                continue
            prev_blank = True
            cleaned.append("")
            continue
        prev_blank = False

        cleaned.append(line.rstrip())

    # Strip leading/trailing blank lines
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    return "\n".join(cleaned)


def extract_whats_next(
    specs: dict[str, str | None],
    conversations: list[dict] | None = None,
) -> list[str]:
    """Extract actionable next-steps from specs and conversations.

    Pulls from:
    - PROJECT_STATUS: "Next Steps", "What's In Progress", "What's Broken"
    - SESSION_NOTES: "Next Steps" sections (most recent session only)
    - PRODUCT_SPEC: "Roadmap" section, items marked as inactive/pending
    - Conversations: recent topic names as context for what was discussed
    """
    items = []
    seen = set()

    def _add(item: str):
        normalized = item.strip().lower()
        # Skip useless placeholders
        if any(skip in normalized for skip in [
            "to be filled", "tbd", "none identified", "not yet tracked",
        ]):
            return
        if not normalized or len(normalized) <= 5:
            return
        # Fuzzy dedup: skip if >60% of words overlap with an existing item
        norm_words = set(re.findall(r'\w{3,}', normalized))
        for existing in seen:
            existing_words = set(re.findall(r'\w{3,}', existing))
            if norm_words and existing_words:
                overlap = len(norm_words & existing_words)
                smaller = min(len(norm_words), len(existing_words))
                if smaller > 0 and overlap / smaller > 0.6:
                    return
        seen.add(normalized)
        items.append(item.strip())

    # --- PROJECT_STATUS: highest priority ---
    status_text = specs.get("PROJECT_STATUS") or ""
    if status_text:
        _extract_section_items(status_text, [
            "Next Steps", "What's In Progress", "What's Broken", "Blockers",
        ], _add)

    # --- SESSION_NOTES: most recent session's Next Steps ---
    notes_text = specs.get("SESSION_NOTES") or ""
    if notes_text:
        # Only look at the first session block (most recent, since they're prepended)
        session_boundary = re.search(r'\n---\n|\n#{1,2}\s+Session', notes_text[100:])
        first_session = notes_text[:session_boundary.start() + 100] if session_boundary else notes_text[:3000]
        _extract_section_items(first_session, [
            "Next Steps", "Questions", "Blockers", "TODO",
        ], _add)

    # --- PRODUCT_SPEC: roadmap items, inactive features ---
    spec_text = specs.get("PRODUCT_SPEC") or ""
    if spec_text:
        # Find "Near Term" or "Roadmap" section
        roadmap_match = re.search(
            r'(?:Near Term|Roadmap|Next|TODO)[^\n]*\n((?:[\s\S](?!##))*)',
            spec_text, re.IGNORECASE,
        )
        if roadmap_match:
            _extract_list_items(roadmap_match.group(1), _add)

        # Find items marked as inactive/pending/not yet wired
        for match in re.finditer(r'(?:inactive|pending|not yet|code complete)[^.\n]*', spec_text, re.IGNORECASE):
            context = match.group(0).strip()
            if len(context) > 10:
                _add(f"Activate: {context}")

    # --- Conversations: recent discussion topics as signals ---
    if conversations:
        for conv in conversations[:5]:
            name = conv.get("name", "")
            if name and len(name) > 10:
                date_str = conv.get("date_display", "")
                prefix = f"({date_str}) " if date_str else ""
                _add(f"{prefix}Discussed: {name[:120]}")

    return items[:12]  # Cap at 12 items


def _extract_section_items(text: str, section_names: list[str], add_fn):
    """Extract bullet/numbered items from named sections in markdown."""
    for section in section_names:
        pattern = rf'(?:^|\n)#+\s*{re.escape(section)}[^\n]*\n((?:[\s\S](?!^#)){{0,1500}})'
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
        if not match:
            # Try without heading marker (plain label)
            pattern = rf'(?:^|\n)\*?\*?{re.escape(section)}\*?\*?:?\s*\n((?:[\s\S](?!^#|\n---)){{0,1500}})'
            match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
        if match:
            _extract_list_items(match.group(1), add_fn)


def _extract_list_items(block: str, add_fn):
    """Extract individual items from a block of markdown list content."""
    for line in block.splitlines():
        stripped = line.strip()
        # Match bullet or numbered items
        item_match = re.match(r'^(?:[-*+]|\d+[.)]\s)\s*(.*)', stripped)
        if item_match:
            item = item_match.group(1).strip()
            # Clean markdown formatting from the item
            item = re.sub(r'\*\*(.+?)\*\*', r'\1', item)
            item = re.sub(r'`([^`]+)`', r'\1', item)
            item = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', item)
            # Remove checkbox markers
            item = re.sub(r'^\[[ xX]\]\s*', '', item)
            # Remove status emojis at start
            item = re.sub(r'^[^\w\s]{1,3}\s*', '', item)
            if item:
                add_fn(item)
