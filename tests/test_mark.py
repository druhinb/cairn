"""The mark's grid, and the SVG copies of it the page and the favicon carry."""
import re
import unittest
from importlib import resources

from cairn import mark

STATIC = resources.files("cairn") / "server" / "static"


def _path(cell):
    return "".join(f"M{x} {y}h{w}v1h-{w}z" for x, y, w in mark.runs(cell))


class MarkTest(unittest.TestCase):
    def test_grid_is_square(self):
        self.assertEqual({len(row) for row in mark.ROWS}, {mark.SIZE})

    def test_runs_cover_every_cell_once(self):
        for cell in (mark.STONE, mark.SUN):
            covered = {(x + i, y) for x, y, w in mark.runs(cell) for i in range(w)}
            expected = {(x, y) for y, row in enumerate(mark.ROWS)
                        for x, c in enumerate(row) if c == cell}
            self.assertEqual(covered, expected)

    def test_svg_copies_match_the_grid(self):
        for name in ("index.html", "favicon.svg"):
            with self.subTest(name):
                paths = re.findall(r'<path [^>]*d="([^"]+)"', (STATIC / name).read_text())
                self.assertEqual(paths, [_path(mark.STONE), _path(mark.SUN)])
