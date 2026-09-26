"""The app icon drawn at the size the app bundle and the Dock ask for."""
import struct
import unittest
import zlib

from cairn.desktop import icon


def _decode(png):
    """Width, height and the raw RGBA rows of an unfiltered 8-bit RGBA PNG."""
    width, height = struct.unpack(">II", png[16:24])
    data, offset = b"", 8
    while offset < len(png):
        length, kind = struct.unpack(">I4s", png[offset:offset + 8])
        if kind == b"IDAT":
            data += png[offset + 8:offset + 8 + length]
        offset += 12 + length
    return width, height, zlib.decompress(data)


class IconTest(unittest.TestCase):
    def test_each_size_is_a_square_png_with_see_through_corners(self):
        for size in (64, 512):
            with self.subTest(size=size):
                png = icon.png(size)
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                width, height, rows = _decode(png)
                self.assertEqual((width, height), (size, size))
                self.assertEqual(len(rows), size * (1 + 4 * size))
                self.assertEqual(rows[1:5], b"\0\0\0\0")

    def test_a_smaller_icon_keeps_the_mark_cells_whole(self):
        # (600, 200) at full size lies in a sun cell
        full = _decode(icon.png(1024))[2]
        half = _decode(icon.png(512))[2]
        def pixel(rows, size, x, y):
            start = y * (1 + 4 * size) + 1 + 4 * x
            return rows[start:start + 4]
        self.assertEqual(pixel(full, 1024, 600, 200), pixel(half, 512, 300, 100))


if __name__ == "__main__":
    unittest.main()
