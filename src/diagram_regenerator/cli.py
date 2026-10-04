"""Command-line entry point: ``diagram-regen``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from diagram_regenerator import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diagram-regen",
        description="Always-current database schema diagrams, PR schema diffs and drift alerts.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
