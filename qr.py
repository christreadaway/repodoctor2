"""
Dependency-free QR code encoder with an inline-SVG renderer.

Settings shows the phone link (a Netlify URL); `svg()` turns it into a QR
code the owner can scan with the phone camera instead of typing it.

Why hand-rolled instead of `segno` or `qrcode`: the launchers install
requirements exactly once (a venv stamp), so a new runtime dependency would
leave every existing install failing with ModuleNotFoundError on the next
launch. This module uses only the standard library, on purpose.

Scope (ISO/IEC 18004): byte mode (UTF-8), error-correction level M, the
smallest version 1-10 that fits (up to 213 bytes, plenty for a URL).
Reed-Solomon over GF(256), block interleaving, every function pattern,
format and version information, and the data mask with the lowest penalty
score (lowest mask number on ties).

The standard leaves room to read the mask-penalty rules more than one way,
and libraries disagree. This follows Nayuki's reference qrcodegen: each
candidate is scored as a finished symbol (format/version info included),
the quiet zone counts as light for finder-like patterns, and those patterns
may be any unit width. tests/test_qr.py pins the output module-for-module
against qrcodegen.
"""

from __future__ import annotations

import html
from functools import lru_cache
from itertools import groupby

# Level M, versions 1-10 (ISO/IEC 18004 Table 9):
# version -> (total codewords, EC codewords per block, number of blocks).
_BLOCKS = {
    1: (26, 10, 1), 2: (44, 16, 1), 3: (70, 26, 1), 4: (100, 18, 2),
    5: (134, 24, 2), 6: (172, 16, 4), 7: (196, 18, 4), 8: (242, 22, 4),
    9: (292, 22, 5), 10: (346, 26, 5),
}
# Alignment pattern centre coordinates per version (Annex E).
_ALIGN = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
    7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
}
MAX_BYTES = 213  # version 10-M, byte mode
_LEVEL_M = 0b00  # error-correction bits in the format information
# The eight data masks; x = column, y = row. A True flips the module.
_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


# --- Reed-Solomon over GF(256), primitive polynomial 0x11D ------------------

def _gf_tables():
    exp, log = [0] * 510, [0] * 256
    value = 1
    for power in range(255):
        exp[power] = exp[power + 255] = value
        log[value] = power
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    return exp, log


_EXP, _LOG = _gf_tables()


def _gf_mul(a: int, b: int) -> int:
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _rs_generator(degree: int) -> list[int]:
    """Coefficients of (x - a^0)(x - a^1)...(x - a^(degree-1)), highest
    first, without the leading 1."""
    poly = [1]
    for i in range(degree):
        poly = [hi ^ _gf_mul(lo, _EXP[i]) for hi, lo in zip(poly + [0], [0] + poly)]
    return poly[1:]


def _rs_remainder(data: list[int], generator: list[int]) -> list[int]:
    """EC codewords: data * x^n mod generator (polynomial long division)."""
    rem = [0] * len(generator)
    for byte in data:
        factor = byte ^ rem[0]
        rem = [r ^ _gf_mul(g, factor) for r, g in zip(rem[1:] + [0], generator)]
    return rem


# --- Bit stream -> codewords -----------------------------------------------

def _data_capacity(version: int) -> int:
    total, ec_per_block, blocks = _BLOCKS[version]
    return total - ec_per_block * blocks


def _count_bits(version: int) -> int:
    return 8 if version < 10 else 16


def _choose_version(n_bytes: int) -> int:
    for version in _BLOCKS:
        if 4 + _count_bits(version) + 8 * n_bytes <= 8 * _data_capacity(version):
            return version
    raise ValueError(
        f"Text is too long for a QR code here: {n_bytes} bytes as UTF-8, "
        f"the limit is {MAX_BYTES} bytes (version 10, error correction M)."
    )


def _data_codewords(data: bytes, version: int) -> list[int]:
    """Mode indicator, length, payload, terminator and pad codewords."""
    capacity = _data_capacity(version)
    bits = "0100" + format(len(data), f"0{_count_bits(version)}b")
    bits += "".join(f"{byte:08b}" for byte in data)
    bits += "0" * min(4, 8 * capacity - len(bits))  # terminator
    bits += "0" * (-len(bits) % 8)  # pad to a byte boundary
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    return words + [(0xEC, 0x11)[i % 2] for i in range(capacity - len(words))]


def _final_message(data: list[int], version: int) -> list[int]:
    """Split into blocks, add EC codewords, interleave (ISO 7.6)."""
    total, ec_len, n_blocks = _BLOCKS[version]
    short = total // n_blocks - ec_len  # data codewords in a short block
    first_long = n_blocks - total % n_blocks  # later blocks carry one more
    generator = _rs_generator(ec_len)
    blocks, start = [], 0
    for i in range(n_blocks):
        size = short + (i >= first_long)
        blocks.append(data[start:start + size])
        start += size
    eccs = [_rs_remainder(block, generator) for block in blocks]
    message = [b[i] for i in range(short + 1) for b in blocks if i < len(b)]
    return message + [e[i] for i in range(ec_len) for e in eccs]


# --- Matrix ----------------------------------------------------------------

def _format_bits(mask: int) -> int:
    data = _LEVEL_M << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)  # BCH(15,5)
    return (data << 10 | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)  # BCH(18,6)
    return version << 12 | rem


def _format_spots(size: int) -> list[tuple[int, int]]:
    """(x, y) of format bits 0..14, both copies, in bit order."""
    first = ([(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)]
             + [(14 - i, 8) for i in range(9, 15)])
    second = ([(size - 1 - i, 8) for i in range(8)]
              + [(8, size - 15 + i) for i in range(8, 15)])
    return first + second


def _version_spots(size: int) -> list[tuple[int, int]]:
    """(x, y) of version bits 0..17: top-right block, then bottom-left."""
    top_right = [(size - 11 + i % 3, i // 3) for i in range(18)]
    return top_right + [(y, x) for x, y in top_right]


def _blank(version: int):
    """Every function pattern drawn (version info and the dark module too);
    the format areas stay reserved and light until a mask is picked."""
    size = 4 * version + 17
    dark = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def put(x: int, y: int, on: bool = False) -> None:
        dark[y][x] = on
        reserved[y][x] = True

    for i in range(size):  # timing patterns
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finders + separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    pos = _ALIGN[version]
    for cx in pos:
        for cy in pos:
            if {cx, cy} <= {pos[0], pos[-1]} and (cx, cy) != (pos[-1], pos[-1]):
                continue  # these three corners would sit on a finder pattern
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    put(8, size - 8, True)  # the "dark module"
    for x, y in _format_spots(size):  # reserved; written once a mask is picked
        put(x, y)
    if version >= 7:
        bits = _version_bits(version)
        for i, (x, y) in enumerate(_version_spots(size)):
            put(x, y, (bits >> (i % 18)) & 1 == 1)
    return dark, reserved


def _place(dark, reserved, message: list[int]) -> None:
    """Zigzag the message bits through the free modules (ISO 7.7.3).
    Leftover remainder bits stay 0 (light)."""
    size = len(dark)
    bits = "".join(f"{word:08b}" for word in message)
    i = 0
    for right in range(size - 1, 0, -2):
        if right <= 6:  # skip the vertical timing column
            right -= 1
        upward = (right + 1) & 2 == 0  # alternate up / down per column pair
        for vert in range(size):
            y = size - 1 - vert if upward else vert
            for x in (right, right - 1):
                if not reserved[y][x] and i < len(bits):
                    dark[y][x] = bits[i] == "1"
                    i += 1


def _with_mask(dark, reserved, mask: int):
    """A finished symbol: `mask` applied to the data modules, format bits
    written."""
    flip = _MASKS[mask]
    grid = [[on ^ (not reserved[y][x] and flip(x, y)) for x, on in enumerate(row)]
            for y, row in enumerate(dark)]
    bits = _format_bits(mask)
    for i, (x, y) in enumerate(_format_spots(len(grid))):
        grid[y][x] = (bits >> (i % 15)) & 1 == 1
    return grid


def _runs(line) -> list[int]:
    return [len(list(group)) for _, group in groupby(line)]


def _penalty(grid) -> int:
    """ISO/IEC 18004 7.8.3.1 mask penalty, read the same way as Nayuki's
    reference qrcodegen: N1 long runs, N2 2x2 blocks, N3 finder-like
    patterns, N4 dark/light balance."""
    size = len(grid)
    quiet = [False] * size  # the quiet zone around the symbol is light
    score = 0
    for line in grid + [list(column) for column in zip(*grid)]:
        # N1: each run of 5+ same-colour modules scores 3 + (length - 5).
        score += sum(n - 2 for n in _runs(line) if n >= 5)
        # N3: dark:light:dark:light:dark at 1:1:3:1:1 (unit width n) with a
        # light run of 4n on one side (and n on the other): 40 per side.
        runs = _runs(quiet + line + quiet)  # light, dark, light, ... light
        for i in range(1, len(runs) - 5, 2):
            before, n, a, b, c, d, after = runs[i - 1:i + 6]
            if a == c == d == n and b == 3 * n:
                score += 40 * (before >= 4 * n and after >= n)
                score += 40 * (after >= 4 * n and before >= n)
    # N2: every 2x2 block of one colour scores 3.
    for upper, lower in zip(grid, grid[1:]):
        score += 3 * sum(upper[x] == upper[x + 1] == lower[x] == lower[x + 1]
                         for x in range(size - 1))
    return score + _balance_penalty(sum(map(sum, grid)), size * size)


def _balance_penalty(dark: int, total: int) -> int:
    """N4: 10k for the smallest k with the dark share within 50% +/- (5k+5)%
    (so exactly 40% dark is k=1, not 2)."""
    return 10 * ((abs(20 * dark - 10 * total) + total - 1) // total - 1)


@lru_cache(maxsize=32)
def _encode(text: str) -> tuple[tuple[bool, ...], ...]:
    if not isinstance(text, str):
        raise TypeError(f"QR text must be str, not {type(text).__name__}")
    if not text:
        raise ValueError("Text is empty: nothing to put in the QR code.")
    data = text.encode("utf-8")
    version = _choose_version(len(data))
    message = _final_message(_data_codewords(data, version), version)
    dark, reserved = _blank(version)
    _place(dark, reserved, message)
    candidates = [_with_mask(dark, reserved, mask) for mask in range(8)]
    best = min(candidates, key=_penalty)  # min() keeps the lowest mask on ties
    return tuple(tuple(row) for row in best)


def matrix(text: str) -> list[list[bool]]:
    """The QR symbol for `text` as rows of modules (True = dark), without
    the quiet zone. Raises ValueError if `text` is empty or longer than
    213 UTF-8 bytes."""
    return [list(row) for row in _encode(text)]


def svg(text: str, *, scale: int = 4, border: int = 4, label: str = "QR code") -> str:
    """An inline <svg> of the QR code: black modules on a white background
    (so it stays scannable on a dark-mode page), `border` modules of quiet
    zone, `scale` pixels per module, and `label` as its accessible name."""
    if not isinstance(scale, int) or scale < 1:
        raise ValueError("scale must be a positive integer")
    if not isinstance(border, int) or border < 0:
        raise ValueError("border must be a non-negative integer")
    grid = _encode(text)
    dim = len(grid) + 2 * border
    runs = []
    for y, row in enumerate(grid):  # one subpath per horizontal run of dark
        x = 0
        for on, run in groupby(row):
            n = len(list(run))
            if on:
                runs.append(f"M{x + border},{y + border}h{n}v1h-{n}z")
            x += n
    path = "".join(runs)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'width="{dim * scale}" height="{dim * scale}" role="img" '
        f'aria-label="{html.escape(label, quote=True)}">'
        f'<rect width="{dim}" height="{dim}" fill="#fff"/>'
        f'<path d="{path}" fill="#000" shape-rendering="crispEdges"/>'
        "</svg>"
    )
