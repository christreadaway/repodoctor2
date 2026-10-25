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

# Inline tokens. Code spans are cut from the raw text (then escaped); the
# rest apply to ALREADY-ESCAPED text. The link pattern is
# length-bounded and its label can't contain '[' so a run of "[[[[..." is
# linear, not quadratic (40k '[' used to take ~5s per render).
_CODE_SPAN = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]\[]{1,500})\]\(([^)\s]{1,2000})\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)|(?<![_\w])_([^_\n]+)_(?![_\w])")

# Placeholders for stashed code spans / generated anchors. \x00 is stripped
# from input first, so only the renderer can produce these.
_CODE_PH = re.compile(r"\x00C(\d+)\x00")
_LINK_PH = re.compile(r"\x00A(\d+)\x00")

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_UL_ITEM = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_ITEM = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_TABLE_SEP_CELL = re.compile(r":?-+:?")

_SAFE_SCHEMES = {"http", "https", "mailto"}
# C0 controls, space and DEL. Browsers silently strip some of these
# ("\x01javascript:" runs as javascript:), so any URL containing one is
# rejected outright rather than guessed at.
_URL_BAD_CHARS = re.compile(r"[\x00-\x20\x7f]")

# Nesting cap for blockquotes and lists: deeper levels are flattened so
# pathological input (1000 '>'s) can't hit RecursionError -> HTTP 500.
_MAX_DEPTH = 50


def _is_table_sep(line: str) -> bool:
    """True for a |---|:---:| separator row (2+ columns). Done by splitting
    instead of one big regex: the old `^\\s*\\|?\\s*...` pattern backtracked
    quadratically (40k spaces after a '|' line took ~35s)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    cells = s.split("|")
    return len(cells) >= 2 and all(_TABLE_SEP_CELL.fullmatch(c.strip()) for c in cells)


def _url_scheme_ok(u: str) -> bool:
    if not u or _URL_BAD_CHARS.search(u):
        return False
    # A scheme is whatever precedes the first ':' when that ':' comes before
    # any '/', '?' or '#'. ("./a:b" and "#x:y" are relative; "foo:bar" isn't.)
    colon = u.find(":")
    if colon != -1:
        first_delim = min((p for p in (u.find(c) for c in "/?#") if p != -1),
                          default=len(u))
        if colon < first_delim:
            return u[:colon].lower() in _SAFE_SCHEMES
    return True  # relative link


def _safe_href(url: str) -> str | None:
    """Return a safe href or None. Allow-list: an http/https/mailto scheme,
    or no scheme at all (relative: #anchor, /path, ./x, ../x, foo.md).
    Anything else — javascript:, JaVaScRiPt:, data:, vbscript:, an
    entity-encoded scheme, a control-char-prefixed one — is rejected."""
    u = url.strip()
    # Check the URL as given AND fully entity-decoded, so "&#106;avascript:"
    # or "java&#9;script:" can't sneak a scheme past the allow-list.
    decoded = u
    for _ in range(5):
        nxt = html.unescape(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    if not (_url_scheme_ok(u) and _url_scheme_ok(decoded)):
        return None
    return u


def _render_inline(text: str) -> str:
    """Escape, then apply inline Markdown. Code spans and generated anchors
    are stashed as placeholders so later passes never re-interpret them, and
    placeholders are only ever restored in TEXT positions (never inside an
    attribute — a URL containing one is rejected by _safe_href)."""
    text = text.replace("\x00", "\ufffd")  # reserve \x00 for our placeholders

    spans: list[str] = []

    def _stash_code(m: re.Match) -> str:
        # Escaped from the raw source with quote=True: safe in any position.
        spans.append(f"<code>{html.escape(m.group(1), quote=True)}</code>")
        return f"\x00C{len(spans) - 1}\x00"

    # Pull code spans from the RAW text, then escape everything else.
    parts = []
    last = 0
    for m in _CODE_SPAN.finditer(text):
        parts.append(html.escape(text[last:m.start()], quote=False))
        parts.append(_stash_code(m))
        last = m.end()
    parts.append(html.escape(text[last:], quote=False))
    out = "".join(parts)

    anchors: list[str] = []

    def _emphasis(s: str) -> str:
        s = _BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", s)
        return _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", s)

    def _link(m: re.Match) -> str:
        label, url = _emphasis(m.group(1)), m.group(2)
        href = _safe_href(html.unescape(url))
        if href is None:
            return label  # inert: show the text, drop the unsafe target
        safe = html.escape(href, quote=True)
        anchors.append(f'<a href="{safe}" target="_blank" rel="noopener nofollow">{label}</a>')
        return f"\x00A{len(anchors) - 1}\x00"

    out = _LINK.sub(_link, out)
    out = _emphasis(out)

    # Restore anchors first (their labels may hold code placeholders), then
    # code spans. Both land in text positions only.
    out = _LINK_PH.sub(lambda m: anchors[int(m.group(1))], out)
    out = _CODE_PH.sub(lambda m: spans[int(m.group(1))], out)
    return out


def _heading_text(raw: str) -> str:
    """Strip an optional closing '#' run (must follow a space, per CommonMark).
    Done with str methods instead of a regex tail like `\\s*#*\\s*$`, which
    backtracks quadratically on long runs of spaces."""
    s = raw.rstrip()
    t = s.rstrip("#")
    if t != s and (not t or t[-1] in " \t"):
        s = t.rstrip()
    return s


def _indent_width(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _render_list(items: list[tuple[int, str, str]], start: int,
                 depth: int = 0) -> tuple[str, int]:
    """Build a (possibly nested) list from items[start:]. Each item is
    (indent, kind 'ul'|'ol', content). Returns (html, next_index), stopping
    at a dedent or kind change — the caller loops until every item is used.
    Past _MAX_DEPTH, deeper items are flattened into the current level."""
    base_indent = items[start][0]
    base_kind = items[start][1]
    parts = [f"<{base_kind}>"]
    i = start
    while i < len(items):
        indent, kind, content = items[i]
        if indent < base_indent:
            break
        if indent > base_indent and depth < _MAX_DEPTH:
            # Nested list: attach to the previous <li> (still open).
            nested, i = _render_list(items, i, depth + 1)
            parts[-1] = parts[-1][:-len("</li>")] + nested + "</li>"
            continue
        if kind != base_kind and indent == base_indent:
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


def render_markdown(text: str, _depth: int = 0) -> str:
    """Render Markdown to safe HTML. Never raises on odd input. `_depth` is
    internal (blockquote nesting level)."""
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
            out.append(f"<h{level}>{_render_inline(_heading_text(h.group(2)))}</h{level}>")
            i += 1
            continue

        # Table: header row followed by a |---|---| separator.
        if "|" in line and i + 1 < n and _is_table_sep(lines[i + 1]):
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
            # At the depth cap, strip ALL leading '>' markers so the inner
            # render can't recurse again (flattens pathological nesting).
            strip = r"^(?:\s*>)+\s?" if _depth >= _MAX_DEPTH else r"^\s*>\s?"
            while i < n and lines[i].lstrip().startswith(">"):
                inner.append(re.sub(strip, "", lines[i]))
                i += 1
            out.append("<blockquote>" + render_markdown("\n".join(inner), _depth + 1)
                       + "</blockquote>")
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
            # _render_list stops at a dedent below its first item or a kind
            # change (ul <-> ol); keep going so no trailing items are dropped.
            j = 0
            while j < len(items):
                list_html, j = _render_list(items, j)
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
    if "|" in line and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
        return True
    return False
