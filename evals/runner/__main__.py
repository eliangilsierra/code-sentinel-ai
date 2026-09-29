"""Command-line entry point: python -m evals.runner <command>."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

COMMANDS = ("run", "judge", "report", "calibrate", "case-lint", "materialize", "budget")
NOT_IMPLEMENTED = 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner")
    parser.add_argument("command", choices=COMMANDS)
    args, _rest = parser.parse_known_args(argv)
    print(
        f"evals.runner {args.command}: not implemented",
        file=sys.stderr,
    )
    return NOT_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
