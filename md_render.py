"""
Safe, dependency-free Markdown -> HTML renderer for the Docs Explorer.

Why hand-rolled instead of the `markdown` package: the Mac launcher installs
requirements exactly once (a venv/.deps-installed stamp), so adding a runtime
dependency would leave every existing install throwing ModuleNotFoundError on
next launch — breaking the "one command that always works" promise. So this
uses only the standard library.

Safety model: EVERY line is HTML-escaped BEFORE any Markdown token is applied,
so no raw HTML (and no <script>) from a repo's docs can ever reach the browser.
Link targets are scheme-checked (http/https/mailto/relative only) so a
`[x](javascript:...)` link renders as inert text. It handles the constructs
real project docs use — headings, fenced/inline code, bold/italic, links,
nested lists, blockquotes, tables, horizontal rules, paragraphs — and treats
anything it doesn't recognize as plain escaped text (never as markup).
"""

from __future__ import annotations

import html
import re

# Inline tokens, applied to ALREADY-ESCAPED text.
_CODE_SPAN = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)|(?<![_\w])_([^_\n]+)_(?![_\w])")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_UL_ITEM = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_ITEM = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")

_SAFE_SCHEME = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)
_HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _safe_href(url: str) -> str | None:
    """Return a safe href or None. Allows http/https/mailto and relative
    links (anchors, root- or path-relative); rejects javascript:, data:, etc."""
    u = url.strip()
    if not u:
        return None
    if _SAFE_SCHEME.match(u):
        return u
    # No scheme at all -> relative link (#anchor, /path, ./x, foo.md). Safe.
    if not _HAS_SCHEME.match(u):
        return u
    return None  # some other scheme (javascript:, data:, vbscript:, ...)


def _render_inline(text: str) -> str:
    """Escape, then apply inline Markdown. Code spans are pulled out first so
    their contents are never re-interpreted as markup."""
    escaped = html.escape(text, quote=False)

    spans: list[str] = []

    def _stash_code(m: re.Match) -> str:
        spans.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(spans) - 1}\x00"

    out = _CODE_SPAN.sub(_stash_code, escaped)

    def _link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        href = _safe_href(html.unescape(url))
        if href is None:
            return label  # inert: show the text, drop the unsafe target
        safe = html.escape(href, quote=True)
        return f'<a href="{safe}" target="_blank" rel="noopener nofollow">{label}</a>'

    out = _LINK.sub(_link, out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", out)

    # Restore code spans.
    out = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], out)
    return out


def _indent_width(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _render_list(items: list[tuple[int, str, str]], start: int) -> tuple[str, int]:
    """Build a (possibly nested) list from items[start:]. Each item is
    (indent, kind 'ul'|'ol', content). Returns (html, next_index)."""
    base_indent = items[start][0]
    base_kind = items[start][1]
    parts = [f"<{base_kind}>"]
    i = start
    while i < len(items):
        indent, kind, content = items[i]
        if indent < base_indent:
            break
        if indent > base_indent:
            # Nested list: attach to the previous <li> (still open).
            nested, i = _render_list(items, i)
            parts[-1] = parts[-1][:-len("</li>")] + nested + "</li>"
            continue
        if kind != base_kind:
            break
        parts.append(f"<li>{_render_inline(content)}</li>")
        i += 1
    parts.append(f"</{base_kind}>")
    return "".join(parts), i


def _render_table(header: str, rows: list[str]) -> str:
    def cells(line: str) -> list[str]:
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    out = ["<table><thead><tr>"]
    out += [f"<th>{_render_inline(c)}</th>" for c in cells(header)]
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        out += [f"<td>{_render_inline(c)}</td>" for c in cells(r)]
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def render_markdown(text: str) -> str:
    """Render Markdown to safe HTML. Never raises on odd input."""
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # Fenced code block.
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)[0]
            buf: list[str] = []
            i += 1
            while i < n and not (lines[i].lstrip().startswith(marker * 3)):
                buf.append(html.escape(lines[i], quote=False))
                i += 1
            i += 1  # consume closing fence (or EOF)
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            continue

        # Blank line.
        if not line.strip():
            i += 1
            continue

        # Horizontal rule.
        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue

        # Heading.
        h = _HEADING.match(line)
        if h:
            level = len(h.group(1))
            out.append(f"<h{level}>{_render_inline(h.group(2))}</h{level}>")
            i += 1
            continue

        # Table: header row followed by a |---|---| separator.
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            header = line
            rows = []
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            out.append(_render_table(header, rows))
            continue

        # Blockquote (one or more consecutive '>' lines).
        if line.lstrip().startswith(">"):
            inner: list[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                inner.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + render_markdown("\n".join(inner)) + "</blockquote>")
            continue

        # List (unordered or ordered), possibly nested.
        if _UL_ITEM.match(line) or _OL_ITEM.match(line):
            items: list[tuple[int, str, str]] = []
            while i < n:
                ul = _UL_ITEM.match(lines[i])
                ol = _OL_ITEM.match(lines[i])
                if ul:
                    items.append((_indent_width(ul.group(1)), "ul", ul.group(2)))
                elif ol:
                    items.append((_indent_width(ol.group(1)), "ol", ol.group(2)))
                else:
                    break
                i += 1
            list_html, _ = _render_list(items, 0)
            out.append(list_html)
            continue

        # Paragraph: gather until a blank line or a block-starting line.
        para: list[str] = []
        while i < n and lines[i].strip() and not _is_block_start(lines[i], lines, i):
            para.append(lines[i].strip())
            i += 1
        if para:
            out.append(f"<p>{_render_inline(' '.join(para))}</p>")
        else:
            i += 1  # safety: never spin on a line we didn't consume

    return "\n".join(out)


def _is_block_start(line: str, lines: list[str], i: int) -> bool:
    """True if `line` begins a non-paragraph block, so paragraph gathering
    stops before swallowing it."""
    if _HEADING.match(line) or _HR.match(line) or _FENCE.match(line):
        return True
    if line.lstrip().startswith(">"):
        return True
    if _UL_ITEM.match(line) or _OL_ITEM.match(line):
        return True
    if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
        return True
    return False
