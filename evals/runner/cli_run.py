"""Command-line handling of ``python -m evals.runner run``."""

from __future__ import annotations

import argparse
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals.runner.budget import DEFAULT_LEDGER, Budget, BudgetError
from evals.runner.cost import DEFAULT_PRICING
from evals.runner.report import render_run
from evals.runner.run import (
    RunError,
    RunOptions,
    Sut,
    charge,
    pending_cases,
    run_suite,
)
from evals.runner.sut import CodeReviewSut, CodeSentinelSut, SutConfig

SUTS = ("code-sentinel", "code-review-medium")
DEFAULT_ESTIMATES = {"code-sentinel": 0.15, "code-review-medium": 0.25}


def configure_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--suite", required=True, help="suite name, 'all' or a case id glob")
    parser.add_argument("--sut", choices=SUTS, default="code-sentinel")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--claude", default="claude", help="command that starts Claude Code")
    parser.add_argument("--plugin-dir", type=Path, default=Path("adapters/claude"))
    parser.add_argument("--config-dir", type=Path, default=None)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--cap-usd", type=float, default=0.5)
    parser.add_argument("--estimate-usd", type=float, default=None, help="expected cost per case")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--force", action="store_true", help="run cases that already have results")
    parser.add_argument("--no-budget", action="store_true")
    parser.add_argument("--budget-file", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--cases-dir", type=Path, default=Path("evals/cases"))
    parser.add_argument("--fixtures-dir", type=Path, default=Path("evals/fixtures"))
    parser.add_argument("--suites-dir", type=Path, default=Path("evals/suites"))
    parser.add_argument("--reports-dir", type=Path, default=Path("evals/reports"))
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)


def build_sut(name: str, config: SutConfig) -> Sut:
    if name == "code-sentinel":
        return CodeSentinelSut(config)
    return CodeReviewSut(config)


def run_command(args: argparse.Namespace) -> int:
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    options = RunOptions(
        suite=args.suite,
        run_id=run_id,
        cases_dir=args.cases_dir,
        fixtures_dir=args.fixtures_dir,
        suites_dir=args.suites_dir,
        reports_dir=args.reports_dir,
        force=args.force,
        pricing=args.pricing,
    )
    config = SutConfig(
        claude=shlex.split(args.claude),
        plugin_dir=args.plugin_dir.resolve() if args.plugin_dir else None,
        config_dir=args.config_dir,
        effort=args.effort,
        cap_usd=args.cap_usd,
        timeout=args.timeout,
    )
    sut = build_sut(args.sut, config)
    try:
        budget = None if args.no_budget else Budget.load(args.budget_file)
        if budget is not None:
            estimate = args.estimate_usd or DEFAULT_ESTIMATES[args.sut]
            budget.ensure_affordable(pending_cases(options, sut.name), estimate)
        result = run_suite(options, sut, on_case=lambda case: print(f"case {case}", flush=True))
    except (RunError, BudgetError) as error:
        print(f"run: {error}", file=sys.stderr)
        return 2
    if budget is not None:
        charge(budget, run_id, result)
        budget.save(args.budget_file)
    print(render_run(result.summary))
    for case_id, message in result.errors:
        print(f"error {case_id}: {message}", file=sys.stderr)
    print(f"results: {result.directory}")
    return 1 if result.errors else 0
