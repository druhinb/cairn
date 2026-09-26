"""Draw the 1024 px app icon as a PNG with the standard library alone.

The icon is the Cairn mark on the paper rounded square of a macOS icon: ink stones
and a brand-coloured sun, 40 px to a grid cell. Only the square's corners are
anti-aliased; the mark's cells keep hard edges, as in the sidebar and the menu bar.

    python packaging/make_icon.py OUT.png
"""
import math
import struct
import sys
import zlib

from cairn import mark

SIZE = 1024
BODY = (100, 100, 924, 924, 185)  # left, top, right, bottom, corner radius
CELL = 40
MARK_ORIGIN = (BODY[0] + BODY[2] - CELL * mark.SIZE) // 2  # left and top, centred in BODY
BRAND = (0xB3, 0x46, 0x21)
INK = (0x1B, 0x1B, 0x1F)
PAPER = (0xF4, 0xF3, 0xEF)
COLOURS = {mark.STONE: INK, mark.SUN: BRAND}


def _distance(x, y, shape, radius):
    """Signed distance from the pixel centre (x, y) to a rounded rectangle's edge,
    negative inside."""
    left, top, right, bottom = shape
    half_w, half_h = (right - left) / 2, (bottom - top) / 2
    qx = abs(x - (left + half_w)) - (half_w - radius)
    qy = abs(y - (top + half_h)) - (half_h - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - radius


def _coverage(x, y, shape, radius):
    # a pixel one unit wide is half covered when its centre sits on the edge
    return min(max(0.5 - _distance(x + 0.5, y + 0.5, shape, radius), 0.0), 1.0)


def _cell(x, y):
    column, row = (x - MARK_ORIGIN) // CELL, (y - MARK_ORIGIN) // CELL
    if 0 <= column < mark.SIZE and 0 <= row < mark.SIZE:
        return mark.ROWS[row][column]
    return None


def _pixel(x, y):
    body = _coverage(x, y, BODY[:4], BODY[4])
    if body == 0.0:
        return b"\0\0\0\0"
    return bytes([*COLOURS.get(_cell(x, y), PAPER), round(body * 255)])


def _chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


def png():
    # filter type 0 (none) opens every row
    rows = b"".join(b"\0" + b"".join(_pixel(x, y) for x in range(SIZE)) for y in range(SIZE))
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(rows, 9)) + _chunk(b"IEND", b""))


if __name__ == "__main__":
    with open(sys.argv[1], "wb") as f:
        f.write(png())
