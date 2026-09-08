"""
tools/jarvis_health/render.py — the terminal views.

Three of them, because three different questions get asked:
  · `health`            — is the tree better or worse than yesterday?
  · `health --tree`     — which files are carrying the debt?
  · `health <path>`     — what exactly is wrong here, and what would prove it?
  · `health --risk`     — what would I fix first?
"""

from __future__ import annotations

from .behaviors import CRITICAL, HIGH, LOW, MEDIUM
from .report import BY_RULE, TESTED, UNTESTED, VIOLATED, Report

BAR_WIDTH = 20

_MARK = {TESTED: "✓", BY_RULE: "▪", UNTESTED: "⚠", VIOLATED: "✗"}
_DOT  = {CRITICAL: "🔴", HIGH: "🟠", MEDIUM: "🟡", LOW: "⚪"}


def _bar(pct: float, width: int = BAR_WIDTH) -> str:
    filled = int(round(width * max(0.0, min(100.0, pct)) / 100.0))
    return "█" * filled + "░" * (width - filled)


def _grade(score: int) -> str:
    for cut, letter in ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (50, "E")):
        if score >= cut:
            return letter
    return "F"


def _pct(v: float) -> str:
    return f"{v:5.1f}%"


# ── the headline ─────────────────────────────────────────────────────────────

def global_view(rep: Report) -> str:
    out = ["", "JARVIS CODE HEALTH",
           "━" * 62, ""]
    out.append(f"                    {rep.score} / 100          grade {_grade(rep.score)}")
    out.append("")

    for key in ("lines", "branches", "functions", "behaviour", "security",
                "architecture", "types"):
        axis = rep.axes[key]
        if not axis.measured:
            out.append(f"  {axis.name:<14} {'·' * BAR_WIDTH}   not measured "
                       f"({axis.detail})")
            continue
        out.append(f"  {axis.name:<14} {_bar(axis.value)}  {_pct(axis.value)}"
                   f"   {axis.detail}")

    if rep.capped_by:
        out += ["", f"  ⚠  {rep.capped_by}.",
                "     A weighted average can be diluted by adding green elsewhere;",
                "     a cap cannot. See tools/jarvis_health/report.py."]

    crit = rep.critical_open
    if crit:
        out += ["", "  Critical guarantees not holding:"]
        for s in crit:
            b = s.behaviour
            out.append(f"    ✗ {b.id}  {b.what}")
            out.append(f"        {b.module}"
                       + (f"  ({b.audit})" if b.audit else ""))

    if rep.problems:
        out += ["", "  Ledger out of sync:"]
        out += [f"    ! {p}" for p in rep.problems]

    if not rep.coverage.available:
        out += ["", "  No coverage data. Run:  python -m tools.jarvis_health --measure"]

    out.append("")
    return "\n".join(out)


# ── the tree ─────────────────────────────────────────────────────────────────

def tree_view(rep: Report) -> str:
    by_module: dict[str, list] = {}
    for s in rep.statuses:
        by_module.setdefault(s.behaviour.module, []).append(s)

    rows = []
    for module in sorted(by_module):
        statuses = by_module[module]
        held = sum(1 for s in statuses if s.holds)
        cov = rep.coverage.for_file(module)
        pct = cov["summary"]["percent_covered"] if cov else None
        worst = min((s.behaviour.severity for s in statuses if not s.holds),
                    key=lambda sev: {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}[sev],
                    default=None)
        mark = "✓" if held == len(statuses) else _MARK[
            VIOLATED if any(s.status == VIOLATED for s in statuses) else UNTESTED]
        rows.append((module, pct, mark, held, len(statuses), worst))

    out = ["", "JARVIS CODE HEALTH — by module", "━" * 78, "",
           f"  {'module':<38} {'cover':>6}  {'':2} {'guarantees':>11}   worst", ""]
    for module, pct, mark, held, total, worst in rows:
        cover = f"{pct:5.1f}%" if pct is not None else "    —"
        badge = _DOT[worst] if worst else "  "
        out.append(f"  {module:<38} {cover}  {mark:2} {held:>5}/{total:<5}   {badge}")
    out += ["",
            "  cover = lines executed by the suite.  guarantees = behaviours proven.",
            "  A file can be green on the left and red on the right; that gap is the",
            "  reason this tool exists.", ""]
    return "\n".join(out)


# ── one file ─────────────────────────────────────────────────────────────────

def file_view(rep: Report, module: str) -> str:
    statuses = [s for s in rep.statuses if s.behaviour.module == module]
    cov = rep.coverage.for_file(module)

    out = ["", module, "━" * max(30, len(module)), ""]

    if cov:
        s = cov["summary"]
        out += ["Coverage",
                f"  Statements   {s['percent_statements_covered']:5.1f}%"
                f"   ({s['covered_lines']}/{s['num_statements']})",
                f"  Branches     {s['percent_branches_covered']:5.1f}%"
                f"   ({s['covered_branches']}/{s['num_branches']})",
                ""]
        fns = cov.get("functions", {})
        if fns:
            out.append("Functions")
            out.append("─" * 58)
            for name, data in sorted(fns.items()):
                fs = data["summary"]
                p = fs["percent_covered"]
                mark = "✓" if p >= 100 else ("⚠" if p >= 70 else "✗")
                label = name or "<module>"
                out.append(f"  {mark} {label:<34} {p:5.1f}%   "
                           f"{fs['covered_lines']}/{fs['num_statements']}")
            out.append("")
    else:
        out += ["Coverage", "  no data for this file", ""]

    out.append("Guarantees")
    out.append("─" * 58)
    for st in sorted(statuses, key=lambda s: s.behaviour.id):
        b = st.behaviour
        out.append(f"  {_MARK[st.status]} {b.id:<14} {b.what}")
        if b.rule:
            out.append(f"      rule    {b.rule}"
                       + ("  VIOLATED" if st.status == VIOLATED else "  clean"))
        if b.tests:
            out.append(f"      tests   {', '.join(b.tests)}")
        elif st.status != VIOLATED:
            out.append("      tests   none declared — this guarantee is unproven")
        if b.audit:
            out.append(f"      audit   {b.audit}"
                       + (f", open since {b.open_since}" if b.open_since else ""))
        if b.note:
            for line in _wrap(b.note, 68):
                out.append(f"      {line}")
        out.append("")

    findings = [f for f in rep.findings if f.path == module]
    if findings:
        out += ["Lint findings", "─" * 58]
        for f in findings:
            out.append(f"  ✗ {f.path}:{f.line}  [{f.rule}] {f.symbol}")
        out.append("")
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# ── the risk map ─────────────────────────────────────────────────────────────

def risk_view(rep: Report) -> str:
    out = ["", "JARVIS — RISK MAP", "━" * 70, ""]
    labels = {CRITICAL: "🔴 CRITICAL", HIGH: "🟠 HIGH",
              MEDIUM: "🟡 MEDIUM", LOW: "⚪ LOW"}
    rows = rep.risk_rows()
    for severity in (CRITICAL, HIGH, MEDIUM, LOW):
        group = [s for s in rows if s.behaviour.severity == severity]
        if not group:
            continue
        out.append(f"{labels[severity]}")
        for s in group:
            b = s.behaviour
            tag = "VIOLATED" if s.status == VIOLATED else "unproven"
            out.append(f"  {b.module:<34} {tag:<9} {b.what}")
            trail = []
            if b.rule:
                trail.append(f"rule {b.rule}")
            if b.audit:
                trail.append(f"audit {b.audit}")
            if trail:
                out.append(f"  {'':<34} {'':<9} └─ " + " · ".join(trail))
        out.append("")

    healthy = rep.healthy_modules()
    if healthy:
        out.append("🟢 HEALTHY  (every declared guarantee proven)")
        for module in healthy:
            cov = rep.coverage.for_file(module)
            pct = f"{cov['summary']['percent_covered']:.0f}%" if cov else "—"
            out.append(f"  {module:<34} {pct}")
        out.append("")
    return "\n".join(out)
