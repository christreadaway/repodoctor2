"""
Tests for the dependency-free Markdown renderer — with an emphasis on the
safety guarantees (no raw HTML, no dangerous link schemes).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import md_render


class TestSafety(unittest.TestCase):

    def test_script_is_escaped(self):
        out = md_render.render_markdown("Hello <script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_raw_html_neutralized(self):
        out = md_render.render_markdown('<img src=x onerror="alert(1)">')
        self.assertNotIn("<img", out)
        self.assertIn("&lt;img", out)

    def test_javascript_link_rejected(self):
        out = md_render.render_markdown("[click](javascript:alert(1))")
        self.assertNotIn("javascript:", out)
        self.assertNotIn("<a", out)
        self.assertIn("click", out)

    def test_data_uri_rejected(self):
        out = md_render.render_markdown("[x](data:text/html,<script>)")
        self.assertNotIn("<a", out)

    def test_safe_link_allowed(self):
        out = md_render.render_markdown("[docs](https://example.com/a)")
        self.assertIn('href="https://example.com/a"', out)
        self.assertIn('rel="noopener nofollow"', out)

    def test_relative_link_allowed(self):
        out = md_render.render_markdown("[top](#section) and [file](./PLAN.md)")
        self.assertIn('href="#section"', out)
        self.assertIn('href="./PLAN.md"', out)


class TestBlocks(unittest.TestCase):

    def test_headings(self):
        out = md_render.render_markdown("# Title\n## Sub")
        self.assertIn("<h1>Title</h1>", out)
        self.assertIn("<h2>Sub</h2>", out)

    def test_paragraph_joins_lines(self):
        out = md_render.render_markdown("one\ntwo\n\nthree")
        self.assertIn("<p>one two</p>", out)
        self.assertIn("<p>three</p>", out)

    def test_fenced_code_no_inline(self):
        out = md_render.render_markdown("```\n**not bold** <script>\n```")
        self.assertIn("<pre><code>", out)
        self.assertIn("**not bold**", out)  # left literal
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<strong>", out)

    def test_hr(self):
        self.assertIn("<hr>", md_render.render_markdown("a\n\n---\n\nb"))

    def test_blockquote(self):
        out = md_render.render_markdown("> quoted line\n> more")
        self.assertIn("<blockquote>", out)
        self.assertIn("quoted line more", out)

    def test_table(self):
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        out = md_render.render_markdown(md)
        self.assertIn("<table>", out)
        self.assertIn("<th>A</th>", out)
        self.assertIn("<td>1</td>", out)


class TestInline(unittest.TestCase):

    def test_bold_italic_code(self):
        out = md_render.render_markdown("**b** *i* `c`")
        self.assertIn("<strong>b</strong>", out)
        self.assertIn("<em>i</em>", out)
        self.assertIn("<code>c</code>", out)

    def test_underscore_in_word_not_italic(self):
        out = md_render.render_markdown("some_variable_name")
        self.assertNotIn("<em>", out)

    def test_code_span_protects_markup(self):
        out = md_render.render_markdown("`**literal**`")
        self.assertIn("<code>**literal**</code>", out)
        self.assertNotIn("<strong>", out)


class TestLists(unittest.TestCase):

    def test_unordered(self):
        out = md_render.render_markdown("- one\n- two")
        self.assertIn("<ul><li>one</li><li>two</li></ul>", out)

    def test_ordered(self):
        out = md_render.render_markdown("1. one\n2. two")
        self.assertIn("<ol>", out)
        self.assertIn("<li>one</li>", out)

    def test_nested(self):
        out = md_render.render_markdown("- top\n  - child\n- back")
        # child list nested inside the first <li>
        self.assertIn("<ul><li>top<ul><li>child</li></ul></li><li>back</li></ul>", out)


class TestRobustness(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(md_render.render_markdown(""), "")

    def test_unclosed_fence_does_not_hang(self):
        out = md_render.render_markdown("```\ncode without end")
        self.assertIn("<pre><code>", out)

    def test_only_whitespace(self):
        self.assertEqual(md_render.render_markdown("   \n\n  "), "")


class TestRegressions(unittest.TestCase):
    """One test per verified bug in the Docs Explorer renderer audit."""

    XSS = '[x](`" autofocus onfocus="alert(1)" a="`)'

    def _assert_no_injected_attrs(self, out):
        self.assertNotIn("onfocus", out)
        self.assertNotIn("autofocus", out)
        self.assertNotIn("onmouseover", out)
        self.assertNotIn('href="<', out)
        self.assertNotIn("\x00", out)

    # 1. Code span restored inside href -> attribute injection (stored XSS).
    def test_code_span_as_link_url_is_not_injected(self):
        out = md_render.render_markdown(self.XSS)
        self._assert_no_injected_attrs(out)
        self.assertNotIn("<a", out)

    def test_code_span_link_xss_in_table_cell(self):
        md = "| A | B |\n| --- | --- |\n| " + self.XSS + " | ok |"
        out = md_render.render_markdown(md)
        self.assertIn("<table>", out)
        self._assert_no_injected_attrs(out)

    def test_onmouseover_variant(self):
        out = md_render.render_markdown('[click](`" onmouseover="alert(1)" x="`)')
        self._assert_no_injected_attrs(out)

    def test_code_span_quotes_escaped(self):
        out = md_render.render_markdown('`a"b`')
        self.assertIn("<code>a&quot;b</code>", out)

    def test_input_nul_cannot_forge_placeholder(self):
        out = md_render.render_markdown("a \x000\x00 \x00C0\x00 `code`")
        self.assertNotIn("\x00", out)
        self.assertEqual(out.count("<code>"), 1)

    # 2. javascript: bypasses (control chars, case, entities).
    def test_javascript_scheme_variants_rejected(self):
        payloads = [
            "[x](\x01javascript:alert%281%29)",
            "[x](\x1fjavascript:alert(1))",
            "[x](JaVaScRiPt:alert(1))",
            "[x](java\x0bscript:alert(1))",
            "[x](&#106;avascript:alert(1))",
            "[x](java&#9;script:alert(1))",
            "[x](&#x6A;avascript:alert(1))",
            "[x](vbscript:msgbox(1))",
            "[x](data:text/html;base64,PHNjcmlwdD4=)",
        ]
        for p in payloads:
            out = md_render.render_markdown(p)
            self.assertNotIn("<a", out, p)
            self.assertNotIn('href="', out, p)

    def test_safe_schemes_still_allowed(self):
        for url in ("HTTPS://example.com", "mailto:a@example.com", "../up.md",
                    "/root", "plain.md", "./a:b", "#frag:x"):
            out = md_render.render_markdown(f"[x]({url})")
            self.assertIn("<a href=", out, url)

    def test_safe_href_unit(self):
        self.assertIsNone(md_render._safe_href("\x01javascript:alert(1)"))
        self.assertIsNone(md_render._safe_href("JaVaScRiPt:alert(1)"))
        self.assertIsNone(md_render._safe_href("x\x00y"))
        self.assertEqual(md_render._safe_href("http://a/b"), "http://a/b")

    # 3. List items dropped on kind change / dedent below the first item.
    def test_list_kind_change_keeps_all_items(self):
        out = md_render.render_markdown("- a\n- b\n1. c\n2. d")
        for x in "abcd":
            self.assertIn(f"<li>{x}</li>", out)
        self.assertIn("<ol><li>c</li><li>d</li></ol>", out)

    def test_list_dedent_below_first_keeps_all_items(self):
        out = md_render.render_markdown("  - a\n- b\n- c")
        for x in "abc":
            self.assertIn(f"<li>{x}</li>", out)

    def test_nested_kind_change_keeps_all_items(self):
        out = md_render.render_markdown("- a\n  1. x\n  - y\n- b")
        for x in "axyb":
            self.assertIn(f"<li>{x}", out)

    # 4. Deep nesting -> RecursionError.
    def test_deep_blockquote_does_not_raise(self):
        out = md_render.render_markdown(">" * 1000 + " deep")
        self.assertIn("deep", out)
        self.assertLessEqual(out.count("<blockquote>"), md_render._MAX_DEPTH + 1)

    def test_deep_list_does_not_raise(self):
        md = "".join(" " * k + "- x\n" for k in range(1500))
        out = md_render.render_markdown(md)
        self.assertEqual(out.count("<li>"), 1500)

    # 5. Quadratic regexes.
    def test_link_regex_not_quadratic(self):
        import time
        t = time.time()
        md_render.render_markdown("[" * 40000)
        md_render.render_markdown("[a](" + "b" * 40000)
        self.assertLess(time.time() - t, 1.0)

    def test_heading_and_table_sep_not_quadratic(self):
        import time
        t = time.time()
        md_render.render_markdown("# " + " " * 40000 + "x")
        md_render.render_markdown("a|b\n" + " " * 40000 + "x")
        self.assertLess(time.time() - t, 1.0)

    def test_heading_closing_hashes(self):
        self.assertIn("<h1>Title</h1>", md_render.render_markdown("# Title ##"))
        self.assertIn("<h1>C#</h1>", md_render.render_markdown("# C#"))

    # 6. Emphasis passes rewriting generated <a> tags.
    def test_emphasis_does_not_touch_anchor_attrs(self):
        out = md_render.render_markdown("[x](http://a) foo _")
        self.assertIn('target="_blank"', out)
        self.assertNotIn("<em>", out)

    def test_underscores_in_url_preserved(self):
        out = md_render.render_markdown("[x](http://a/_b_)")
        self.assertIn('href="http://a/_b_"', out)
        self.assertNotIn("<em>", out)

    def test_emphasis_inside_and_around_links_still_works(self):
        out = md_render.render_markdown("*see [**b** `c`](http://a)*")
        self.assertIn('<em>see <a href="http://a"', out)
        self.assertIn("<strong>b</strong> <code>c</code></a></em>", out)


if __name__ == "__main__":
    unittest.main()
