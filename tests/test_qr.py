"""
Tests for the dependency-free QR encoder (qr.py): known answers from a
reference library, the spec structure of the symbol, and the SVG output.

Where the known answers come from: Nayuki's qrcodegen 1.8.0 (automatic mask,
level M, byte mode, no EC boost), run in a scratch venv outside the repo.
The same matrices were cross-checked against python-qrcode 8.2 and segno
1.6.6 forced to the same mask, and the SVG renderings decoded by zxing-cpp
and OpenCV. segno is not usable for automatic-mask answers: it adds an extra
0x00 data codeword when the bit stream is already byte-aligned, and encodes
Latin-1-only text as ISO-8859-1, so its symbols scan fine but differ.

Each reference is (size, mask, SHA-256 of the rows as "0"/"1" lines joined
by newlines). This file uses only the standard library.
"""

import hashlib
import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qr

NETLIFY = "https://f3phqdpibdlzccvhy5zmg4bj.netlify.app"
DOCS = NETLIFY + "/docs/getting-started/phone-link?source=settings&step=scan-qr"

# name -> (text, UTF-8 byte length, version)
INPUTS = {
    "netlify": (NETLIFY, 44, 4),
    "one_char": ("A", 1, 1),
    "seventeen": ("Hello, RepoDoctor", 17, 2),
    "unicode": ("café ☕", 9, 1),
    "url_33": ("https://example.netlify.app/phone", 33, 3),
    "url_60": (NETLIFY + "/settings?tab=qr", 60, 4),
    "url_83": (NETLIFY + "/settings?tab=phone-link&source=qr-code", 83, 5),
    "url_100": (NETLIFY + "/settings/phone-link?source=qr&utm_campaign=setup&ref=ab", 100, 6),
    "url_110": (DOCS + "&v=22", 110, 7),
    "url_141": (DOCS + "&utm_source=repodoctor&utm_medium=qr", 141, 8),
    "url_168": (DOCS + "&utm_source=repodoctor&utm_medium=qr&utm_campaign=onboarding-v2", 168, 9),
    "max_213": (NETLIFY + "/?" + "x" * 167, 213, 10),
    # Picked because its mask choice hinges on a double-width (2:2:6:2:2)
    # finder-like pattern; unit-width-only N3 scoring picks another mask.
    "scaled_n3": ("https://tc.-gawmj&tdlvw24pvxlhte93g9hk-z3ccc6.g0=i0/wex&kxk"
                  "&fva=4tjqggphj5r88hu3pk/8c6qxmsz9", 92, 6),
}

# name -> (size, mask, sha256) from qrcodegen 1.8.0.
REFS = {
    "netlify": (33, 2, "34cf0ab4b5b165ee585237976beadf6438f81acb3cca6b9916126c559848dc0a"),
    "one_char": (21, 4, "177b3a1897a152aee17ab1ee4b667913c6bb873e6ba357debfc5ba243dabff0e"),
    "seventeen": (25, 1, "15fd028d913a0027eea7fd3316c65cbe36e19c46448107bb9d4f21e3d1eec0c6"),
    "unicode": (21, 4, "ec0d391a4dddbc978931749cb4738ee8ba4c6dcc3a62a538f68f0f969e22b7f5"),
    "url_33": (29, 3, "997f4a28839f03cd3de3a5b3e835be3f19227715b5fe6fe6b917d72b1c4f19fa"),
    "url_60": (33, 2, "614cdbdcb7b8733788e09c2493d4f3dab1ff4f42354b6c29a8b9bd5a9109c68b"),
    "url_83": (37, 4, "8e51a6652f8b4893c71c520b00389b18652632deb0126280fe44bcfa622678a1"),
    "url_100": (41, 2, "0d1c9c3acfac1f1974adb43bd1c7e118b4e788fa540cf54d00bbb530ca9f3ef5"),
    "url_110": (45, 2, "5c69f3f7a501c7f0d0486fb71f5633f90ed8ba03a1c6a8606b1b621f9cdd94ea"),
    "url_141": (49, 2, "274a05345ce903a7b13fccd8a598398fa1185b3d8a2665c5d972dee32be2c1d5"),
    "url_168": (53, 6, "9b6bbcee208b6947b408f613952fcdeacaefc2a2b59033eaf62f288d58e25b07"),
    "max_213": (57, 0, "ce4a76d8985246f6ea2dcadb7109cd2b1ced4ab45a152ef5d90eb5d5fd0cfb66"),
    "scaled_n3": (41, 2, "10adcb798ec37ee7ca44fa1b6c4e8e019c7dc7db742c9ffe26cd408e28a88d61"),
}

# The phone link itself, row by row (MSB = leftmost module), so a failure
# shows exactly which rows moved.
NETLIFY_ROWS = [
    "1fc5cf27f", "1042f9441", "17589e15d", "175771d5d", "17542495d", "105d7dd41",
    "1fd55557f", "001e20900", "17c6e847c", "120513e6d", "10c934996", "1b2972d5d",
    "17f706abb", "173a71ecb", "007f0a9c2", "0b26979fc", "00504b5b1", "15981f26d",
    "186568574", "1238cd1bd", "06d053c39", "1a3eec24d", "114a358ae", "15be24d5c",
    "1650b85fb", "0013d3b15", "1fc8b4b56", "105931f1d", "1751649f8", "1758b9e1d",
    "1754461e4", "1044138dc", "1fd21e762",
]

# ISO/IEC 18004 Annex C: format words for level M, masks 0-7.
FORMAT_WORDS_M = [0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0]
# ISO/IEC 18004 Annex D: version information words.
VERSION_WORDS = {7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3}
# Byte-mode capacity at level M for versions 1-10 (ISO/IEC 18004 Table 7).
CAPACITY = [14, 26, 42, 62, 84, 106, 122, 152, 180, 213]


def serialize(grid):
    return "\n".join("".join("1" if m else "0" for m in row) for row in grid)


def bits_to_int(bits):
    return int("".join("1" if b else "0" for b in bits), 2)


def read_format(grid):
    """Both copies of the 15-bit format word, bit 14 first (ISO Figure 25)."""
    n = len(grid)
    first = [grid[8][x] for x in (0, 1, 2, 3, 4, 5, 7, 8)]
    first += [grid[y][8] for y in (7, 5, 4, 3, 2, 1, 0)]
    second = [grid[y][8] for y in range(n - 1, n - 8, -1)]
    second += [grid[8][x] for x in range(n - 8, n)]
    return bits_to_int(first), bits_to_int(second)


def read_version(grid):
    """Both copies of the 18-bit version word (ISO Figure 27)."""
    n = len(grid)
    top_right = sum(grid[i // 3][n - 11 + i % 3] << i for i in range(18))
    bottom_left = sum(grid[n - 11 + i % 3][i // 3] << i for i in range(18))
    return top_right, bottom_left


def mask_of(grid):
    first, second = read_format(grid)
    assert first == second, "format copies disagree"
    return FORMAT_WORDS_M.index(first)


class TestKnownAnswers(unittest.TestCase):

    def test_inputs_hit_their_versions(self):
        for name, (text, n_bytes, version) in INPUTS.items():
            with self.subTest(name):
                self.assertEqual(len(text.encode("utf-8")), n_bytes)
                self.assertEqual(len(qr.matrix(text)), 4 * version + 17)

    def test_matrices_match_reference(self):
        self.assertEqual(set(INPUTS), set(REFS))
        for name, (size, mask, digest) in REFS.items():
            with self.subTest(name):
                grid = qr.matrix(INPUTS[name][0])
                self.assertEqual(len(grid), size)
                self.assertTrue(all(len(row) == size for row in grid))
                self.assertEqual(mask_of(grid), mask, "mask choice differs from reference")
                self.assertEqual(hashlib.sha256(serialize(grid).encode()).hexdigest(), digest)

    def test_phone_link_rows(self):
        grid = qr.matrix(NETLIFY)
        rows = [format(bits_to_int(row), "09x") for row in grid]
        self.assertEqual(rows, NETLIFY_ROWS)

    def test_reed_solomon_iso_example(self):
        # ISO/IEC 18004 Annex I: "01234567" at 1-M.
        data = [0x10, 0x20, 0x0C, 0x56, 0x61, 0x80, 0xEC, 0x11,
                0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11]
        ec = qr._rs_remainder(data, qr._rs_generator(10))
        self.assertEqual(bytes(ec).hex(), "a524d4c1ed36c7872c55")

    def test_balance_penalty_boundaries(self):
        # Exact 5% boundaries only occur in 25x25 and 45x45 symbols, too
        # rarely for the known answers to pin them, so pin the rule here:
        # the smallest k with the dark share inside 50% +/- (5k + 5)%.
        total = 25 * 25
        for dark, expected in ((313, 0), (282, 0), (281, 10), (250, 10), (249, 20),
                               (375, 10), (376, 20), (125, 50), (0, 90), (625, 90)):
            with self.subTest(dark=dark):
                self.assertEqual(qr._balance_penalty(dark, total), expected)


class TestSymbolStructure(unittest.TestCase):

    def test_version_steps_at_capacity(self):
        for version, cap in enumerate(CAPACITY, start=1):
            with self.subTest(version=version):
                self.assertEqual(len(qr.matrix("x" * cap)), 4 * version + 17)
                if version < 10:
                    self.assertEqual(len(qr.matrix("x" * (cap + 1))), 4 * version + 21)

    def test_multibyte_text_counts_bytes(self):
        self.assertEqual(len(qr.matrix("☕" * 71)), 57)  # 213 bytes fits v10
        with self.assertRaises(ValueError):
            qr.matrix("☕" * 72)  # 216 bytes

    def test_finders_separators_timing_dark_module(self):
        for name in ("one_char", "netlify", "url_110", "max_213"):
            grid = qr.matrix(INPUTS[name][0])
            n = len(grid)
            with self.subTest(name):
                for ox, oy in ((0, 0), (n - 7, 0), (0, n - 7)):
                    for dy in range(7):
                        for dx in range(7):
                            ring = max(abs(dx - 3), abs(dy - 3))
                            self.assertEqual(grid[oy + dy][ox + dx], ring != 2)
                for i in range(8):  # separators
                    for x, y in ((7, i), (i, 7), (n - 8, i), (n - 1 - i, 7),
                                 (7, n - 1 - i), (i, n - 8)):
                        self.assertFalse(grid[y][x])
                for i in range(8, n - 8):
                    self.assertEqual(grid[6][i], i % 2 == 0)
                    self.assertEqual(grid[i][6], i % 2 == 0)
                self.assertTrue(grid[n - 8][8])

    def test_bottom_right_alignment_pattern(self):
        for name in ("seventeen", "netlify", "url_141"):
            grid = qr.matrix(INPUTS[name][0])
            c = len(grid) - 7
            with self.subTest(name):
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.assertEqual(grid[c + dy][c + dx], max(abs(dx), abs(dy)) != 1)

    def test_format_info_level_m_both_copies(self):
        for name, (text, _, _) in INPUTS.items():
            with self.subTest(name):
                first, second = read_format(qr.matrix(text))
                self.assertEqual(first, second)
                self.assertIn(first, FORMAT_WORDS_M)
                self.assertEqual((first ^ 0x5412) >> 13, 0b00)  # level M

    def test_version_info_v7_and_up(self):
        for name, (text, _, version) in INPUTS.items():
            if version >= 7:
                with self.subTest(name):
                    self.assertEqual(read_version(qr.matrix(text)),
                                     (VERSION_WORDS[version],) * 2)

    def test_matrix_returns_fresh_lists(self):
        grid = qr.matrix(NETLIFY)
        grid[0][0] = not grid[0][0]
        self.assertTrue(qr.matrix(NETLIFY)[0][0])


class TestErrors(unittest.TestCase):

    def test_too_long_raises_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            qr.matrix("x" * 214)
        self.assertIn("214 bytes", str(ctx.exception))
        self.assertIn("213", str(ctx.exception))
        with self.assertRaises(ValueError):
            qr.svg(NETLIFY + "/" + "y" * 400)

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError) as ctx:
            qr.matrix("")
        self.assertIn("empty", str(ctx.exception))
        with self.assertRaises(ValueError):
            qr.svg("")

    def test_non_text_raises(self):
        with self.assertRaises(TypeError):
            qr.matrix(NETLIFY.encode())
        with self.assertRaises(TypeError):
            qr.svg(None)

    def test_bad_scale_or_border(self):
        bad = ({"scale": 0}, {"scale": -2}, {"scale": 2.5}, {"border": -1}, {"border": "4"})
        for kwargs in bad:
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    qr.svg(NETLIFY, **kwargs)


class TestSvg(unittest.TestCase):

    def test_attributes_and_viewbox(self):
        out = qr.svg(NETLIFY, label="Phone link")
        size = len(qr.matrix(NETLIFY))  # 33 -> 41 with the quiet zone
        self.assertTrue(out.startswith("<svg "))
        self.assertIn(f'viewBox="0 0 {size + 8} {size + 8}"', out)
        self.assertIn(f'width="{(size + 8) * 4}" height="{(size + 8) * 4}"', out)
        self.assertIn('role="img"', out)
        self.assertIn('aria-label="Phone link"', out)
        self.assertIn('shape-rendering="crispEdges"', out)
        self.assertNotIn("<script", out.lower())

    def test_default_label_and_scale(self):
        out = qr.svg("A", scale=10, border=2)
        self.assertIn('aria-label="QR code"', out)
        self.assertIn('viewBox="0 0 25 25"', out)
        self.assertIn('width="250" height="250"', out)

    def test_label_is_escaped(self):
        out = qr.svg(NETLIFY, label='"><script>alert(1)</script>&')
        self.assertNotIn("<script", out)
        self.assertIn('aria-label="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;&amp;"', out)

    def test_white_background_then_one_black_path(self):
        root = ET.fromstring(qr.svg(NETLIFY))  # well-formed XML
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        children = list(root)
        self.assertEqual([c.tag.split("}")[1] for c in children], ["rect", "path"])
        rect, path = children
        self.assertEqual(rect.get("fill"), "#fff")
        self.assertEqual(rect.get("width"), "41")
        self.assertEqual(rect.get("height"), "41")
        self.assertEqual(path.get("fill"), "#000")

    def test_path_draws_exactly_the_dark_modules(self):
        for name in ("one_char", "netlify", "max_213"):
            text = INPUTS[name][0]
            grid = qr.matrix(text)
            n = len(grid)
            d = ET.fromstring(qr.svg(text, border=4))[1].get("d")
            self.assertRegex(d, r"^(M\d+,\d+h\d+v1h-\d+z)+$")
            drawn = [[False] * n for _ in range(n)]
            for x, y, w, back in re.findall(r"M(\d+),(\d+)h(\d+)v1h-(\d+)z", d):
                self.assertEqual(w, back)
                for i in range(int(w)):
                    self.assertFalse(drawn[int(y) - 4][int(x) - 4 + i], "module drawn twice")
                    drawn[int(y) - 4][int(x) - 4 + i] = True
            with self.subTest(name):
                self.assertEqual(drawn, grid)

    def test_deterministic(self):
        first = qr.svg(INPUTS["url_110"][0], label="Phone link")
        qr._encode.cache_clear()
        self.assertEqual(qr.svg(INPUTS["url_110"][0], label="Phone link"), first)


if __name__ == "__main__":
    unittest.main()
