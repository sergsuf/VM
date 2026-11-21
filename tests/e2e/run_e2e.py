#!/usr/bin/env python3
"""Stdlib-only 5-tier E2E test runner for the Remote 3ds Max Deployment System.

Usage:
    python3 tests/e2e/run_e2e.py --check-syntax
    python3 tests/e2e/run_e2e.py --list [--tier 1|2|3|4|5|all] [-v]
    python3 tests/e2e/run_e2e.py --tier 1|2|3|4|5|all [-v] [--failfast]
"""

from __future__ import annotations

import argparse
import importlib
import pathlib
import py_compile
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e import ALL_FEATURE_IDS, TIER_LABELS, TIER_MINIMUMS, TIER_MODULES
from tests.e2e.helpers import (
    audit_python_file_stdlib_and_no_skips,
    extract_feature_coverage,
    iter_test_cases,
)

E2E_DIR = pathlib.Path(__file__).resolve().parent


def _parse_tier_selection(tier_arg: str) -> list[int]:
    if tier_arg == "all":
        return [1, 2, 3, 4, 5]
    return [int(tier_arg)]


def _load_tier_suites(tiers: list[int]) -> tuple[dict[int, unittest.TestSuite], list[str]]:
    loader = unittest.TestLoader()
    suites: dict[int, unittest.TestSuite] = {}
    errors: list[str] = []

    for tier in tiers:
        mod_name = TIER_MODULES[tier]
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:
            errors.append(f"Failed to import {mod_name}: {type(exc).__name__}: {exc}")
            continue
        suite = loader.loadTestsFromModule(mod)
        if loader.errors:
            errors.extend(loader.errors)
            loader.errors.clear()
        for tc in iter_test_cases(suite):
            if tc.__class__.__name__ == "_FailedTest":
                errors.append(f"Loader error in {mod_name}: {tc.id()}")
        suites[tier] = suite
    return suites, errors


def _cmd_check_syntax() -> int:
    failures: list[str] = []
    py_files = sorted(E2E_DIR.glob("*.py"))
    if not py_files:
        print("[E2E FAIL] No Python files found under tests/e2e/", file=sys.stderr)
        return 1

    # 1. py_compile check + AST stdlib & zero-skip audit on every tests/e2e/*.py
    for path in py_files:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"Syntax error in {path.name}: {exc}")
            continue
        failures.extend(audit_python_file_stdlib_and_no_skips(path))

    # 2. Import all 5 tier modules and discover tests via unittest.TestLoader
    suites, load_errors = _load_tier_suites([1, 2, 3, 4, 5])
    failures.extend(load_errors)

    # 3. Validate tier minimums, @feature tags, and 24/24 feature coverage
    tier_counts: dict[int, int] = {}
    if len(suites) == 5:
        for tier, suite in suites.items():
            cases = list(iter_test_cases(suite))
            tier_counts[tier] = len(cases)
            minimum = TIER_MINIMUMS[tier]
            if tier == 4:
                if len(cases) != 8:
                    failures.append(
                        f"Tier 4 must contain exactly 8 scenarios, found {len(cases)}"
                    )
            elif len(cases) < minimum:
                failures.append(
                    f"Tier {tier} has {len(cases)} tests, below required minimum {minimum}"
                )

        coverage, untagged = extract_feature_coverage(suites)
        for tid in untagged:
            failures.append(f"Test method missing @feature(...) tag: {tid}")

        uncovered = [
            fid
            for fid in ALL_FEATURE_IDS
            if sum(len(v) for v in coverage[fid].values()) == 0
        ]
        if uncovered:
            failures.append(
                f"Uncovered features ({len(uncovered)}/24): {', '.join(uncovered)}"
            )
    else:
        coverage = {}

    if failures:
        print(f"[E2E CHECK-SYNTAX FAILED] {len(failures)} issue(s) found:", file=sys.stderr)
        for err in failures:
            print(f"  - {err}", file=sys.stderr)
        return 1

    total = sum(tier_counts.values())
    covered_count = sum(
        1 for fid in ALL_FEATURE_IDS if sum(len(v) for v in coverage[fid].values()) > 0
    )
    print(
        f"[E2E CHECK-SYNTAX OK] {len(py_files)} files compiled & AST-audited "
        f"(stdlib-only, zero skips)."
    )
    for tier in (1, 2, 3, 4, 5):
        print(f"  Tier {tier}: {tier_counts[tier]:>3d} tests ({TIER_LABELS[tier]})")
    print(f"  Total : {total:>3d} tests | Feature coverage: {covered_count}/24 (F-01..F-24)")
    return 0


def _cmd_list(tier_arg: str, verbose: bool) -> int:
    selected_tiers = _parse_tier_selection(tier_arg)
    suites, errors = _load_tier_suites(selected_tiers)
    if errors:
        for err in errors:
            print(f"[E2E LIST ERROR] {err}", file=sys.stderr)
        return 1

    total = 0
    for tier in selected_tiers:
        suite = suites[tier]
        cases = list(iter_test_cases(suite))
        total += len(cases)
        print(f"\n=== {TIER_LABELS[tier]} ({len(cases)} tests) ===")
        for idx, tc in enumerate(cases, 1):
            method = getattr(tc, tc._testMethodName, None)
            fids = getattr(method, "_feature_ids", ())
            tag_str = ", ".join(fids) if fids else "UNTAGGED"
            print(f"  {idx:02d}. {tc.__class__.__name__}.{tc._testMethodName}  [{tag_str}]")
            if verbose and method is not None and method.__doc__:
                first_line = method.__doc__.strip().splitlines()[0]
                print(f"      {first_line}")

    # Always compute coverage summary over the selected tiers (and enforce 24/24 when --tier all)
    coverage, untagged = extract_feature_coverage(suites)
    print(f"\n--- Summary: {total} test(s) listed across tier(s) {selected_tiers} ---")

    if tier_arg == "all":
        print("\n=== 24-Feature Coverage Matrix (F-01..F-24) ===")
        print(f"{'Feature':<8} {'T1':>4} {'T2':>4} {'T3':>4} {'T4':>4} {'T5':>4} {'Total':>6}")
        print("-" * 39)
        uncovered: list[str] = []
        for fid in ALL_FEATURE_IDS:
            t1 = len(coverage[fid].get(1, []))
            t2 = len(coverage[fid].get(2, []))
            t3 = len(coverage[fid].get(3, []))
            t4 = len(coverage[fid].get(4, []))
            t5 = len(coverage[fid].get(5, []))
            tot = t1 + t2 + t3 + t4 + t5
            if tot == 0:
                uncovered.append(fid)
            print(f"{fid:<8} {t1:>4} {t2:>4} {t3:>4} {t4:>4} {t5:>4} {tot:>6}")
        covered = len(ALL_FEATURE_IDS) - len(uncovered)
        print("-" * 39)
        print(f"Covered: {covered}/{len(ALL_FEATURE_IDS)} features | Untagged tests: {len(untagged)}")
        if uncovered or untagged:
            if uncovered:
                print(f"[E2E LIST ERROR] Uncovered features: {', '.join(uncovered)}", file=sys.stderr)
            if untagged:
                print(f"[E2E LIST ERROR] Untagged tests: {len(untagged)}", file=sys.stderr)
            return 1
    elif untagged:
        print(f"[E2E LIST ERROR] Untagged tests: {len(untagged)}", file=sys.stderr)
        return 1

    return 0


def _cmd_run(tier_arg: str, verbose: bool, failfast: bool) -> int:
    selected_tiers = _parse_tier_selection(tier_arg)
    suites, errors = _load_tier_suites(selected_tiers)
    if errors:
        for err in errors:
            print(f"[E2E LOAD ERROR] {err}", file=sys.stderr)
        return 1

    combined = unittest.TestSuite()
    for tier in selected_tiers:
        combined.addTests(suites[tier])

    runner = unittest.TextTestRunner(
        verbosity=2 if verbose else 1,
        failfast=failfast,
    )
    result = runner.run(combined)

    if len(result.skipped) > 0:
        print(
            f"[E2E ERROR] Skipped tests are forbidden in the E2E suite "
            f"(skipped={len(result.skipped)}).",
            file=sys.stderr,
        )
        return 1

    ok = result.wasSuccessful() and result.testsRun > 0 and len(result.skipped) == 0
    return 0 if ok else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="4-Tier Opaque-Box E2E Test Runner for Remote 3ds Max Deployment System"
    )
    parser.add_argument(
        "--tier",
        choices=("1", "2", "3", "4", "5", "all"),
        default="all",
        help="Select E2E tier to list or run (default: all)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List discovered tests with @feature tags and coverage matrix",
    )
    parser.add_argument(
        "--check-syntax",
        action="store_true",
        help="Compile, AST-audit (stdlib-only & zero skips), and validate 24/24 feature coverage",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose test runner or listing output",
    )
    parser.add_argument(
        "--failfast",
        action="store_true",
        help="Stop test run on first failure or error",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.check_syntax:
        return _cmd_check_syntax()
    if args.list:
        return _cmd_list(args.tier, args.verbose)
    return _cmd_run(args.tier, args.verbose, args.failfast)


if __name__ == "__main__":
    raise SystemExit(main())
