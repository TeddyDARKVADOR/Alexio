"""
`./jarvis <command>` — the development interface.

Exit codes are the contract, so a hook or a CI step can just use them:
    0  fine
    1  something failed that should block a commit
    2  the command could not run (bad arguments, missing data)
"""

from __future__ import annotations

import argparse
import sys

from . import commands, ui

COMMANDS = [
    ("check",    "static checks only — parse, names, invariants. Seconds, not minutes."),
    ("test",     "the test suite, whole or in named groups"),
    ("coverage", "lines, branches and functions, by package or by file"),
    ("rules",    "the invariants, their status, and why each one exists"),
    ("health",   "the composite score across coverage, behaviour and rules"),
    ("audit",    "what to fix, worst first"),
    ("run",      "launch Alexio"),
]


def _help() -> int:
    print(ui.title("jarvis"))
    print(f"\n  {ui.c('./jarvis <command> [options]', 'bold')}\n")
    for name, blurb in COMMANDS:
        print(f"  {ui.c(name, 'cyan'):<20} {blurb}")
    print(f"""
  {ui.c('Common', 'bold')}
    ./jarvis check                  before every commit
    ./jarvis test --group security  the suite that guards the dashboard
    ./jarvis test --failed          only what broke last time
    ./jarvis coverage core.ai.router
    ./jarvis rules --explain JAR012
    ./jarvis audit --critical

  {ui.c('Underneath', 'grey')}
    {ui.c('python -m tools.jarvis_lint     the rule engine on its own', 'grey')}
    {ui.c('python -m tools.jarvis_health   the score on its own', 'grey')}
""")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        return _help()

    ap = argparse.ArgumentParser(prog="jarvis", add_help=False)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("check", add_help=False)

    t = sub.add_parser("test", add_help=False)
    t.add_argument("target", nargs="?", help="a module: core.ai, dashboard, …")
    t.add_argument("--group", "-g", metavar="NAME",
                   help="security | rules | ai | voice | desktop")
    t.add_argument("--failed", "-f", action="store_true", help="only what failed last")
    t.add_argument("-k", metavar="EXPR", help="pytest -k expression")
    t.add_argument("--verbose", "-v", action="store_true")

    cv = sub.add_parser("coverage", add_help=False)
    cv.add_argument("module", nargs="?")
    cv.add_argument("--measure", "-m", action="store_true", help="re-run the suite first")

    r = sub.add_parser("rules", add_help=False)
    r.add_argument("--explain", "-e", metavar="CODE")

    sub.add_parser("health", add_help=False)

    a = sub.add_parser("audit", add_help=False)
    a.add_argument("--critical", "-c", action="store_true")
    a.add_argument("--tree", "-t", action="store_true")

    sub.add_parser("run", add_help=False)

    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    fn = getattr(commands, args.cmd, None) if args.cmd else None
    if fn is None:
        print(f"unknown command {argv[0]!r}", file=sys.stderr)
        return _help() or 2
    return fn(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
