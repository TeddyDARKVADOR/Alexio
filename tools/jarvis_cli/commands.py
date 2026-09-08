"""
tools/jarvis_cli/commands.py — what each subcommand does.

THE DIVISION THAT MATTERS
    `jarvis check` must stay fast enough to run without thinking about it —
    a few seconds, no test suite, no coverage. That is the whole reason it is a
    separate command from `jarvis test`: a pre-commit check that takes a minute
    is a pre-commit check people stop running, and then the guardrails only fire
    in CI, where they cost a round trip instead of a keystroke.

    So: check = static only. test = the suite. coverage = the slow measurement.
    health = everything already computed, assembled.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from pathlib import Path

from tools import jarvis_lint
from tools.jarvis_lint.framework import CRITICAL, HIGH, project_files

from . import ui

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable


# ── jarvis check ─────────────────────────────────────────────────────────────

def _pyflakes_findings() -> tuple[list[str], bool]:
    """Undefined names and unused imports. Returns (messages, ran)."""
    try:
        from pyflakes import api, reporter
    except Exception:
        return [], False

    import io
    out, err = io.StringIO(), io.StringIO()
    rep = reporter.Reporter(out, err)
    for path in project_files(ROOT):
        api.checkPath(str(path), rep)
    msgs = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    return msgs, True


# An undefined name is a NameError waiting for the right input; an unused import
# is tidiness. actions/screen_processor.py:144 calls np.mean() with numpy never
# imported — pyflakes has always reported it and nobody was running pyflakes.
_HARD_PYFLAKES = ("undefined name", "syntax error", "redefinition of unused")


def check(args) -> int:
    t0 = time.perf_counter()
    print(ui.title("jarvis check"))

    errors, warnings, checks = 0, 0, 0

    # 1 · does it parse
    bad_syntax = []
    for path in project_files(ROOT):
        checks += 1
        try:
            # compile(), not ast.parse(): the two disagree, and the difference
            # is not academic. ast.parse accepts `from __future__ import …`
            # after other statements; compile rejects it, which is what Python
            # will do at import time. This check reported a clean tree while
            # core/desktop/windows.py could not be imported at all — found by
            # the suite, one command after `jarvis check` said zero errors.
            # `compile` writes no __pycache__, which is why py_compile went.
            compile(path.read_text(encoding="utf-8"), str(path), "exec",
                    dont_inherit=True)
        except (SyntaxError, ValueError, OSError) as exc:
            bad_syntax.append(f"{path.relative_to(ROOT)}: {exc}")
    errors += len(bad_syntax)
    print(ui.line("bad" if bad_syntax else "ok", "Python",
                  f"{checks} files parse"))
    for b in bad_syntax[:5]:
        print(f"      {ui.c(b, 'red')}")

    # 2 · names and imports
    msgs, ran = _pyflakes_findings()
    if not ran:
        print(ui.line("skip", "Imports", "pyflakes not installed"))
    else:
        hard = [m for m in msgs if any(h in m.lower() for h in _HARD_PYFLAKES)]
        soft = [m for m in msgs if m not in hard]
        checks += len(msgs) or 1
        errors += len(hard)
        warnings += len(soft)
        state = "bad" if hard else ("warn" if soft else "ok")
        print(ui.line(state, "Imports",
                      f"{len(hard)} undefined, {len(soft)} unused"))
        for m in hard[:6]:
            print(f"      {ui.c(m.replace(str(ROOT) + '/', ''), 'red')}")

    # 3 · the invariants
    findings = jarvis_lint.run(ROOT)
    baseline = jarvis_lint.load_baseline()
    new, stale = jarvis_lint.partition(findings, baseline)
    rules = jarvis_lint.all_rules()
    checks += len(rules)

    sec = [m for m in rules if m.severity in (CRITICAL, HIGH)]
    arch = [m for m in rules if m.severity not in (CRITICAL, HIGH)]
    sec_new = [f for f in new if f.rule in {m.code for m in sec}]
    arch_new = [f for f in new if f.rule in {m.code for m in arch}]

    print(ui.line("bad" if arch_new else "ok", "Architecture",
                  f"{len(arch)} rules, {len(arch_new)} new"))
    print(ui.line("bad" if sec_new else "ok", "Security rules",
                  f"{len(sec)} rules, {len(sec_new)} new"))
    errors += len(new)

    if stale:
        warnings += len(stale)
        print(ui.line("warn", "Baseline",
                      f"{len(stale)} entr(y/ies) no longer reproduce"))
    else:
        print(ui.line("ok", "Baseline", f"{len(baseline)} known, none stale"))

    # 4 · the ledger
    try:
        from tools.jarvis_health import report as health_report
        rep = health_report.build(ROOT, collect_tests=False,
                                  findings=findings)
        if rep.problems:
            errors += len(rep.problems)
            print(ui.line("bad", "Health ledger", f"{len(rep.problems)} out of sync"))
            for p in rep.problems[:4]:
                print(f"      {ui.c(p, 'red')}")
        else:
            print(ui.line("ok", "Health ledger",
                          f"{len(rep.statuses)} guarantees declared"))
        checks += len(rep.statuses)
    except Exception as exc:
        print(ui.line("skip", "Health ledger", f"{type(exc).__name__}"))

    for f in new[:12]:
        print(f"\n  {ui.c(f.render(), 'red')}")
    if len(new) > 12:
        print(f"\n  {ui.c(f'… and {len(new) - 12} more', 'grey')}")

    dt = time.perf_counter() - t0
    print()
    print(f"  {checks} checks · "
          f"{ui.c(f'{errors} errors', 'red' if errors else 'green')} · "
          f"{ui.c(f'{warnings} warnings', 'yellow' if warnings else 'grey')}")
    print(f"  {ui.c(f'{dt:.1f}s', 'grey')}")
    if errors:
        print(ui.hint("jarvis rules --explain <CODE>   why a rule exists"))
    return 1 if errors else 0


# ── jarvis test ──────────────────────────────────────────────────────────────

_TEST_GROUPS = {
    "security": ["tests/test_dashboard_security.py", "tests/test_safety.py",
                 "tests/test_tool_policy.py", "tests/test_actions.py"],
    "rules":    ["tests/test_architecture_rules.py", "tests/test_health_ledger.py"],
    "ai":       ["tests/test_ai_gateway.py", "tests/test_ai_routing.py",
                 "tests/test_telemetry.py"],
    "voice":    ["tests/test_voice_session.py", "tests/test_voice_disconnect.py"],
    "desktop":  ["tests/test_desktop.py", "tests/test_input_facade.py",
                 "tests/test_windows_compat.py"],
}


def test(args) -> int:
    cmd = [PY, "-m", "pytest", "-o", "addopts=", "-q", "--no-header"]

    if args.failed:
        cmd.append("--last-failed")
    if args.group:
        targets = _TEST_GROUPS.get(args.group)
        if targets is None:
            print(f"unknown group {args.group!r}. Known: "
                  f"{', '.join(sorted(_TEST_GROUPS))}", file=sys.stderr)
            return 2
        cmd += targets
    if args.target:
        # `jarvis test core.ai` → the test files that name that package.
        dotted = args.target.replace("/", ".").removesuffix(".py")
        matches = [str(p.relative_to(ROOT)) for p in sorted((ROOT / "tests").glob("test_*.py"))
                   if dotted.split(".")[-1] in p.name
                   or dotted in p.read_text(encoding="utf-8")]
        if not matches:
            print(f"no test file mentions {args.target!r}", file=sys.stderr)
            return 2
        cmd += matches
    if args.k:
        cmd += ["-k", args.k]

    print(ui.title("tests"))
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    out = proc.stdout

    passed = failed = skipped = 0
    for token, key in (("passed", "p"), ("failed", "f"), ("skipped", "s")):
        import re
        m = re.search(rf"(\d+) {token}", out)
        if m:
            n = int(m.group(1))
            if key == "p":
                passed = n
            elif key == "f":
                failed = n
            else:
                skipped = n

    print(ui.line("ok" if passed else "skip", f"{passed} passed"))
    if failed:
        print(ui.line("bad", f"{failed} failed"))
    if skipped:
        print(ui.line("skip", f"{skipped} skipped"))

    if failed:
        print(f"\n  {ui.c('Failed:', 'red')}")
        import re
        for name in re.findall(r"^FAILED (\S+)", out, re.MULTILINE):
            print(f"    {name.split('::')[-1]}")
        if args.verbose:
            print("\n" + out)
        else:
            print(ui.hint("jarvis test --failed -v   re-run just these, with output"))

    print(f"\n  {ui.c(f'{dt:.1f}s', 'grey')}")
    return proc.returncode


# ── jarvis coverage ──────────────────────────────────────────────────────────

_PACKAGES = ("core/ai", "core/voice", "core/desktop", "core/local", "core",
             "actions", "dashboard", "memory", "plugins")


def coverage(args) -> int:
    from tools.jarvis_health.report import Coverage

    if args.measure:
        from tools.jarvis_health.__main__ import measure
        print("  measuring…")
        if not measure():
            return 2

    cov = Coverage.load()
    if not cov.available:
        print("no coverage data. Run:  jarvis coverage --measure", file=sys.stderr)
        return 2

    if args.module:
        rel = args.module.replace(".", "/")
        if not rel.endswith(".py"):
            rel += ".py"
        from tools.jarvis_health import report as hr
        from tools.jarvis_health.render import file_view
        print(file_view(hr.build(ROOT), rel))
        return 0

    print(ui.title("jarvis coverage"))
    print(f"  {'':<16} {'Lines':>7} {'Branch':>8} {'Funcs':>7}")
    print("  " + ui.rule_line())

    claimed: set[str] = set()
    for pkg in _PACKAGES:
        files = {p: d for p, d in cov.files.items()
                 if p.startswith(pkg + "/") and p not in claimed}
        if not files:
            continue
        claimed |= set(files)
        st = sum(d["summary"]["num_statements"] for d in files.values())
        cv = sum(d["summary"]["covered_lines"] for d in files.values())
        nb = sum(d["summary"]["num_branches"] for d in files.values())
        cb = sum(d["summary"]["covered_branches"] for d in files.values())
        fns = [f for d in files.values() for f in d.get("functions", {}).values()]
        fok = sum(1 for f in fns if f["summary"]["percent_covered"] >= 100)
        lp = 100 * cv / st if st else 0
        bp = 100 * cb / nb if nb else 0
        fp = 100 * fok / len(fns) if fns else 0
        print(f"  {pkg:<16} {lp:>6.1f}% {bp:>7.1f}% {fp:>6.1f}%")

    print("  " + ui.rule_line())
    print(f"  {'TOTAL':<16} {cov.lines:>6.1f}% {cov.branches:>7.1f}% "
          f"{cov.functions:>6.1f}%")

    weak = [(p, name, f["summary"]["percent_covered"])
            for p, d in cov.files.items()
            for name, f in d.get("functions", {}).items()
            if f["summary"]["num_statements"] >= 4
            and f["summary"]["percent_covered"] < 50]
    if weak:
        print(f"\n  {ui.mark('warn')} {len(weak)} functions below 50%")
        for p, name, pct in sorted(weak, key=lambda x: x[2])[:6]:
            print(f"      {ui.c(f'{p}::{name or chr(60)+chr(62)}', 'grey')} {pct:.0f}%")

    print(ui.hint("jarvis coverage core.ai.router   one file, function by function"))
    return 0


# ── jarvis rules ─────────────────────────────────────────────────────────────

def rules(args) -> int:
    metas = jarvis_lint.all_rules()

    if args.explain:
        code = args.explain.upper()
        meta = next((m for m in metas if m.code == code), None)
        if meta is None:
            print(f"unknown rule {code}. Known: "
                  f"{', '.join(m.code for m in metas)}", file=sys.stderr)
            return 2
        findings = [f for f in jarvis_lint.run(ROOT) if f.rule == code]
        print(ui.title(f"{meta.code} · {meta.title}"))
        print()
        for para in _wrap(meta.why, 72):
            print(f"  {para}")
        print(f"\n  {ui.c('severity', 'grey')}  {meta.severity}")
        print(f"  {ui.c('status  ', 'grey')}  "
              + (ui.c("clean", "green") if not findings
                 else ui.c(f"{len(findings)} violation(s)", "red")))
        for f in findings:
            print(f"      {f.path}:{f.line}  {f.symbol}")
        return 0

    findings = jarvis_lint.run(ROOT)
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.rule] = counts.get(f.rule, 0) + 1

    print(ui.title("jarvis rules"))
    for family, label in (((CRITICAL, HIGH), "Security & safety"),
                          (("medium", "low"), "Architecture & hygiene")):
        group = [m for m in metas if m.severity in family]
        if not group:
            continue
        print(f"\n  {ui.c(label, 'bold')}")
        for m in group:
            n = counts.get(m.code, 0)
            state = "ok" if n == 0 else ("bad" if m.severity == CRITICAL else "warn")
            detail = "" if n == 0 else f"{n} violation(s)"
            print("  " + ui.line(state, f"{m.code}  {m.title[:52]}", detail))

    clean = sum(1 for m in metas if counts.get(m.code, 0) == 0)
    print(f"\n  {ui.c(f'{clean} clean', 'green')} · "
          f"{ui.c(f'{len(metas) - clean} with violations', 'yellow')} "
          f"· {len(findings)} findings, all in the baseline")
    print(ui.hint("jarvis rules --explain JAR012   why this rule exists"))
    return 0


def _wrap(text: str, w: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for word in words:
        if len(cur) + len(word) + 1 > w:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out


# ── jarvis health / audit ────────────────────────────────────────────────────

def health(args) -> int:
    from tools.jarvis_health import report as hr
    from tools.jarvis_health.render import global_view
    rep = hr.build(ROOT)
    print(global_view(rep))
    if rep.critical_open:
        print(ui.hint("jarvis audit --critical   what to fix, and why it matters"))
    return 0


def audit(args) -> int:
    from tools.jarvis_health import report as hr
    from tools.jarvis_health.render import risk_view, tree_view
    rep = hr.build(ROOT)
    if args.tree:
        print(tree_view(rep))
        return 0
    if args.critical:
        print(ui.title("critical"))
        for s in rep.critical_open:
            b = s.behaviour
            print(f"\n  {ui.mark('bad')} {ui.c(b.id, 'bold')}  {b.what}")
            print(f"      {ui.c(b.module, 'grey')}"
                  + (f"   rule {b.rule}" if b.rule else "")
                  + (f"   audit {b.audit}" if b.audit else ""))
            if b.note:
                for ln in _wrap(b.note, 66):
                    print(f"      {ui.c(ln, 'grey')}")
        print()
        return 0
    print(risk_view(rep))
    return 0


# ── jarvis run ───────────────────────────────────────────────────────────────

def run(args) -> int:
    """Launch Alexio itself. The one command that is not about the code."""
    print(ui.c(f"  {PY} main.py", "grey"))
    return subprocess.run([PY, "main.py"], cwd=ROOT).returncode
