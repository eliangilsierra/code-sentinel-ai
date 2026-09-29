"""Command-line entry point for review-ctx."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from review_ctx import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-ctx",
        description="Deterministic context, evidence ledger and gate for review-squad.",
    )
    parser.add_argument("--version", action="version", version=f"review-ctx {__version__}")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
