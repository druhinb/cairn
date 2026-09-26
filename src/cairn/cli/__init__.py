"""The cairn command line."""
import sys

try:  # the first project import, so a missing rich fails with the hint below
    from cairn.core import ui  # noqa: F401
except ImportError:
    print("Cairn cannot import rich; reinstall the package: "
          "uv pip install -e <your cairn checkout>", file=sys.stderr)
    sys.exit(2)

from cairn.cli.entry import main
