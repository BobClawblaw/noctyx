"""noctyx command line. Commands arrive as modules are extracted (docs/PLAN.md, phase 1)."""
import argparse
import sys

from . import __version__


def main(argv=None):
    p = argparse.ArgumentParser(prog="noctyx", description=__doc__)
    p.add_argument("--version", action="version", version=f"noctyx {__version__}")
    p.parse_args(argv)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
