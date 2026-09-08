"""
`python -m tools.jarvis_lint` — the one command that must be green before a commit.

Exit codes are the contract:
    0  nothing new
    1  a violation outside the baseline (or a stale baseline entry)
    2  the linter itself could not run
"""

from __future__ import annotations

import argparse
import sys

from . import all_rules, load_baseline, partition, run, write_baseline
from .framework import BASELINE_PATH, ROOT


def _print_rules() -> None:
    for meta in all_rules():
        print(f"\n{meta.code}  {meta.title}")
        for line in meta.why.split(". "):
            line = line.strip()
            if line:
                print(f"    {line}{'' if line.endswith('.') else '.'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tools.jarvis_lint",
        description="Alexio's architecture and safety invariants, enforced.",
    )
    ap.add_argument("--all", action="store_true",
                    help="print every finding, including the known baseline")
    ap.add_argument("--rules", action="store_true",
                    help="what each rule is for, and which defect it came from")
    ap.add_argument("--write-baseline", action="store_true",
                    help="record the current findings as known debt")
    ap.add_argument("--check-stale", action="store_true",
                    help="fail if the baseline names a violation that no longer exists")
    ap.add_argument("--rule", action="append", metavar="CODE",
                    help="run only this rule (repeatable)")
    args = ap.parse_args(argv)

    if args.rules:
        _print_rules()
        return 0

    only = set(args.rule) if args.rule else None
    findings = run(ROOT, only=only)

    if args.write_baseline:
        write_baseline(findings)
        print(f"Baseline written: {len(findings)} known violation(s) → "
              f"{BASELINE_PATH.relative_to(ROOT)}")
        return 0

    baseline = load_baseline()
    new, stale = partition(findings, baseline)

    if args.all:
        for f in findings:
            mark = " " if f.key in baseline else "!"
            print(f"{mark} {f.render()}")
        print(f"\n{len(findings)} finding(s); {len(new)} outside the baseline.")
    else:
        for f in new:
            print(f.render())

    status = 0
    if new:
        print(f"\n{len(new)} new violation(s). Each message says what would clear it.",
              file=sys.stderr)
        status = 1

    if stale:
        print(f"\n{len(stale)} baseline entr(y/ies) no longer reproduce — delete "
              f"them so the file names real debt:", file=sys.stderr)
        for key in stale:
            print(f"  {key}", file=sys.stderr)
        if args.check_stale:
            status = 1

    if not new and not args.all:
        print(f"jarvis-lint: clean ({len(findings)} known, 0 new).")
    return status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # the linter's own failure
        print(f"jarvis-lint could not run: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(2)
