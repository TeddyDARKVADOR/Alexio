"""
`python -m tools.jarvis_health` — is the tree better or worse than yesterday?

    --measure          run the suite under coverage first (slow, ~40 s)
    --tree             per-module view
    --risk             what to fix, worst first
    --html FILE        write the dashboard
    <path>             everything known about one file
    --fail-under N     exit 1 below this score (for CI)

Exit codes: 0 fine · 1 below threshold or ledger out of sync · 2 could not run.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .report import COVERAGE_JSON, ROOT, build
from .render import file_view, global_view, risk_view, tree_view


def measure() -> bool:
    """Run the suite under coverage and write coverage.json.

    Kept out of the default path: a health check that takes forty seconds is a
    health check nobody runs before a commit. The stale data is labelled instead.
    """
    COVERAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    omit = ".venv/*,tests/*,tools/*,setup.py"
    try:
        subprocess.run(
            [sys.executable, "-m", "coverage", "run", "--branch",
             "--source=.", f"--omit={omit}", "-m", "pytest", "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=1800,
        )
        r = subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", str(COVERAGE_JSON), "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
        return r.returncode == 0
    except Exception as exc:
        print(f"coverage run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.jarvis_health")
    ap.add_argument("path", nargs="?", help="a module to look at in detail")
    ap.add_argument("--measure", action="store_true",
                    help="run the suite under coverage before reporting")
    ap.add_argument("--tree", action="store_true", help="per-module view")
    ap.add_argument("--risk", action="store_true", help="what to fix, worst first")
    ap.add_argument("--html", metavar="FILE", help="write the dashboard")
    ap.add_argument("--fail-under", type=int, metavar="N",
                    help="exit 1 when the score is below N")
    args = ap.parse_args(argv)

    if args.measure and not measure():
        return 2

    rep = build(ROOT)

    if args.path:
        rel = Path(args.path).as_posix().removeprefix("./")
        known = {s.behaviour.module for s in rep.statuses} | set(rep.coverage.files)
        if rel not in known:
            print(f"no health data for {rel}. Known modules:\n  "
                  + "\n  ".join(sorted(known)), file=sys.stderr)
            return 2
        print(file_view(rep, rel))
    elif args.tree:
        print(tree_view(rep))
    elif args.risk:
        print(risk_view(rep))
    else:
        print(global_view(rep))

    if args.html:
        from .render_html import write_dashboard
        out = Path(args.html)
        write_dashboard(rep, out)
        print(f"dashboard written: {out}")

    status = 0
    if rep.problems:
        status = 1
    if args.fail_under is not None and rep.score < args.fail_under:
        print(f"\nscore {rep.score} is below --fail-under {args.fail_under}",
              file=sys.stderr)
        status = 1
    return status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"jarvis-health could not run: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(2)
