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


if __name__ == "__main__":
    unittest.main()
