"""The Cairn mark: a pixel cairn with the sun rising behind its right shoulder.

ROWS is the 16 by 16 grid, top row first: `#` a stone, `o` the sun, `.` empty. The
app icon and the menu-bar item draw from it; server/static/index.html and
server/static/favicon.svg carry the same grid as SVG paths.
"""
import re

ROWS = (
    "................",
    ".........oooo...",
    "........oooooo..",
    ".......oooooooo.",
    ".......oooooooo.",
    ".......oooooooo.",
    ".....####.ooooo.",
    "....######.ooo..",
    "...........oo...",
    "..#######.......",
    ".#########......",
    "................",
    "..############..",
    ".##############.",
    "..############..",
    "................",
)
SIZE = len(ROWS)
STONE, SUN = "#", "o"


def runs(cell):
    """(x, y, width) for each horizontal run of cell in ROWS."""
    return [(found.start(), y, len(found.group()))
            for y, row in enumerate(ROWS) for found in re.finditer(f"{re.escape(cell)}+", row)]
