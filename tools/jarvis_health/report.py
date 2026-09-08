"""
tools/jarvis_health/report.py — assemble the three views into one report.

THE ONE DESIGN DECISION THAT MATTERS
    A critical violation CAPS the score; it does not merely subtract from it.

    A weight can always be diluted. Add twenty well-tested helper modules and a
    weighted average climbs back over any threshold you like while
    `Budget.private()` still ships the user's prompt to three vendors. That is
    not a hypothetical: core/ai/router.py measures 97% line coverage, 93% branch
    coverage, and `Budget.private` itself measures 100%, with the guarantee
    broken the whole time.

    So `score()` computes the weighted average and then clamps it. One critical
    guarantee broken and the number cannot exceed 49, however green everything
    else is. That is the difference between a dashboard that reports quality and
    one that can be farmed.

WHAT IS AND IS NOT MEASURED
    Type safety is not wired (no mypy configuration in this repository), so it
    is reported as "not measured" and excluded from the score rather than shown
    as 100%. An axis nobody runs displayed as a full bar is worse than a missing
    axis, because it reads as evidence.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tools import jarvis_lint
from tools.jarvis_lint.framework import CRITICAL as R_CRITICAL, HIGH as R_HIGH

from .behaviors import BEHAVIOURS, CRITICAL, HIGH, LOW, MEDIUM, WEIGHTS, Behaviour

ROOT = Path(__file__).resolve().parent.parent.parent
COVERAGE_JSON = ROOT / ".jarvis-health" / "coverage.json"

VIOLATED = "violated"
UNTESTED = "untested"
TESTED   = "tested"
BY_RULE  = "by_rule"     # no test of its own; a clean lint rule enforces it

# Both count as holding. They are kept apart because they are different kinds of
# evidence and the dashboard should say which one it has: a test demonstrates the
# behaviour, a rule forbids the shape that breaks it. The rule is not weaker —
# it applies to every file, forever, and carries its own positive and negative
# tests in tests/test_architecture_rules.py — but it proves a property of the
# *code*, not of the running system, and conflating the two is how "we have a
# linter" becomes "it is tested".
HOLDING = (TESTED, BY_RULE)

_SECURITY_AREAS = ("BEH-GATE", "BEH-FS", "BEH-NET")

_SEVERITY_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}


# ── inputs ───────────────────────────────────────────────────────────────────

@dataclass
class Coverage:
    """What coverage.py measured, or nothing at all."""

    available: bool = False
    lines:     float = 0.0
    branches:  float = 0.0
    functions: float = 0.0
    files:     dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = COVERAGE_JSON) -> "Coverage":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()

        totals = raw.get("totals", {})
        files = raw.get("files", {})

        # "Functions covered" is the strict reading: a function counts only when
        # every statement in it ran. A function that was entered and returned
        # early is not a tested function, and the loose reading would let the
        # axis sit at 90% while half the bodies never execute.
        total_fns = covered_fns = 0
        for data in files.values():
            for fn in data.get("functions", {}).values():
                total_fns += 1
                if fn["summary"]["percent_covered"] >= 100.0:
                    covered_fns += 1

        return cls(
            available=True,
            lines=totals.get("percent_covered", 0.0),
            branches=totals.get("percent_branches_covered", 0.0),
            functions=(100.0 * covered_fns / total_fns) if total_fns else 0.0,
            files=files,
        )

    def for_file(self, rel: str) -> dict | None:
        return self.files.get(rel)


@functools.lru_cache(maxsize=4)
def collected_tests(root: Path = ROOT) -> set[str]:
    """Every pytest node id the suite currently collects.

    Memoised: this shells out to `pytest --collect-only`, which imports the whole
    application, and several tests build a Report. Called from inside a pytest
    run it was spawning a nested collection per call and took the suite from
    48 s to over three minutes. The tree does not change during one process.

    A behaviour that names a test which no longer exists must read as UNTESTED,
    not as tested — a renamed test is exactly how a guarantee quietly loses its
    proof.
    """
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q"],
            cwd=root, capture_output=True, text=True, timeout=300,
        ).stdout
    except Exception:
        return set()
    ids = set()
    for line in out.splitlines():
        line = line.strip()
        if "::" in line:
            ids.add(line.split("[")[0])
            ids.add(line.split("::")[0])          # the file, for file-level claims
        elif line.endswith(".py"):
            ids.add(line)
    return ids


# ── derivation ───────────────────────────────────────────────────────────────

@dataclass
class BehaviourStatus:
    behaviour: Behaviour
    status:    str
    findings:  list = field(default_factory=list)

    @property
    def holds(self) -> bool:
        return self.status in HOLDING


def evaluate(findings: list, tests: set[str]) -> list[BehaviourStatus]:
    by_rule_module: dict[tuple[str, str], list] = {}
    for f in findings:
        by_rule_module.setdefault((f.rule, f.path), []).append(f)

    out = []
    for b in BEHAVIOURS:
        hits = by_rule_module.get((b.rule, b.module), []) if b.rule else []
        if hits:
            status = VIOLATED
        elif b.tests and all(t in tests for t in b.tests):
            status = TESTED
        elif b.rule and not b.tests:
            # A guarantee whose whole content is "this shape must not appear"
            # needs no separate demonstration: the rule is the proof, and the
            # rule is itself tested both ways.
            status = BY_RULE
        else:
            status = UNTESTED
        out.append(BehaviourStatus(behaviour=b, status=status, findings=hits))
    return out


def reconcile(statuses: list[BehaviourStatus]) -> list[str]:
    """Rows whose ledger entry no longer matches reality.

    The same discipline as the lint baseline's --check-stale. A row marked open
    that is now tested and unviolated is history pretending to be debt; a row
    with no open marker that is violated right now is debt pretending to be
    history. Both make the dashboard lie in the direction nobody notices.
    """
    problems = []
    for s in statuses:
        b = s.behaviour
        if b.open_since and s.holds:
            problems.append(
                f"{b.id} is marked open since {b.open_since} but is now tested and "
                f"unviolated — clear open_since in tools/jarvis_health/behaviors.py"
            )
        if not b.open_since and s.status == VIOLATED:
            problems.append(
                f"{b.id} is violated by {b.rule} and carries no open_since — a "
                f"regression, or a ledger entry that was never filled in"
            )
    return problems


# ── scoring ──────────────────────────────────────────────────────────────────

@dataclass
class Axis:
    name:    str
    value:   float
    measured: bool = True
    detail:  str = ""


@dataclass
class Report:
    coverage:  Coverage
    findings:  list
    statuses:  list[BehaviourStatus]
    problems:  list[str]
    axes:      dict[str, Axis]
    score:     int
    capped_by: str = ""

    @property
    def critical_open(self) -> list[BehaviourStatus]:
        return [s for s in self.statuses
                if s.behaviour.severity == CRITICAL and not s.holds]

    def risk_rows(self) -> list[BehaviourStatus]:
        return sorted(
            (s for s in self.statuses if not s.holds),
            key=lambda s: (_SEVERITY_ORDER[s.behaviour.severity], s.behaviour.module),
        )

    def healthy_modules(self) -> list[str]:
        broken = {s.behaviour.module for s in self.statuses if not s.holds}
        every = {s.behaviour.module for s in self.statuses}
        return sorted(every - broken)


def build(root: Path = ROOT, collect_tests: bool = True,
          findings: list | None = None) -> Report:
    """Assemble the report.

    `collect_tests=False` skips the pytest collection subprocess, which is most
    of the wall clock here. `jarvis check` uses it: that command exists to be run
    before every commit, and a pre-commit check that shells out to pytest is a
    pre-commit check people stop running. Without collection every behaviour
    reads as unproven, so only the half of the reconciliation that does not
    depend on tests — a violation with no open marker — is reported.
    """
    coverage = Coverage.load()
    # Accepting precomputed findings matters more than it looks: `jarvis check`
    # needs them for its own report *and* for the ledger, and running the twenty
    # rules over the tree twice was half that command's wall clock.
    findings = jarvis_lint.run(root) if findings is None else findings
    tests    = collected_tests(root) if collect_tests else set()
    statuses = evaluate(findings, tests)
    problems = [p for p in reconcile(statuses)
                if collect_tests or "is violated by" in p]

    # Rule checks: one per (rule, file) the linter actually looked at. This is
    # the "47/49" shape — how many places were checked and came back clean.
    from tools.jarvis_lint.framework import project_files
    n_files = sum(1 for _ in project_files(root))
    rules   = jarvis_lint.all_rules()
    sec_rules  = [m for m in rules if m.severity in (R_CRITICAL, R_HIGH)]
    arch_rules = [m for m in rules if m.severity not in (R_CRITICAL, R_HIGH)]

    def clean_ratio(family) -> tuple[int, int]:
        codes = {m.code for m in family}
        total = n_files * len(codes)
        bad   = len({(f.rule, f.path) for f in findings if f.rule in codes})
        return total - bad, total

    sec_ok, sec_total   = clean_ratio(sec_rules)
    arch_ok, arch_total = clean_ratio(arch_rules)

    held = [s for s in statuses if s.holds]
    sec_beh = [s for s in statuses
               if s.behaviour.id.rsplit("-", 1)[0] in _SECURITY_AREAS]
    sec_beh_ok = [s for s in sec_beh if s.holds]

    # Architecture is scored the same way security is — on guarantees held, not
    # on file×rule checks. `n_files * n_rules` produces a denominator in the
    # hundreds and a percentage in the high nineties no matter what is broken,
    # which is precisely the kind of number this dashboard exists to distrust.
    # The check counts are still reported, as context, never as the score.
    arch_beh = [s for s in statuses if s not in sec_beh]
    arch_beh_ok = [s for s in arch_beh if s.holds]
    n_violations = len({(f.rule, f.path) for f in findings})

    axes = {
        "lines": Axis("Lines", coverage.lines, coverage.available),
        "branches": Axis("Branches", coverage.branches, coverage.available),
        "functions": Axis("Functions", coverage.functions, coverage.available,
                          "fully covered"),
        "behaviour": Axis("Behaviour", 100.0 * len(held) / len(statuses),
                          True, f"{len(held)}/{len(statuses)} guarantees proven"),
        "security": Axis("Security", 100.0 * len(sec_beh_ok) / max(1, len(sec_beh)),
                         True, f"{len(sec_beh_ok)}/{len(sec_beh)} guarantees hold"),
        "architecture": Axis("Architecture",
                             100.0 * len(arch_beh_ok) / max(1, len(arch_beh)),
                             True,
                             f"{len(arch_beh_ok)}/{len(arch_beh)} guarantees hold · "
                             f"{n_violations} lint violation(s) in "
                             f"{n_files} files × {len(rules)} rules"),
        "types": Axis("Type safety", 0.0, False, "mypy not configured"),
    }

    # Weighted average over the axes that were actually measured.
    usable = {k: a for k, a in axes.items() if a.measured and k in WEIGHTS}
    total_w = sum(WEIGHTS[k] for k in usable) or 1.0
    raw = sum(axes[k].value * WEIGHTS[k] for k in usable) / total_w

    score = int(round(raw))
    capped_by = ""
    crit = [s for s in statuses if s.behaviour.severity == CRITICAL and not s.holds]
    high = [s for s in statuses if s.behaviour.severity == HIGH and not s.holds]
    if crit:
        if score > 49:
            capped_by = (f"{len(crit)} critical guarantee(s) broken — the score "
                         f"cannot exceed 49 while one stands")
        score = min(score, 49)
    elif high:
        if score > 74:
            capped_by = (f"{len(high)} high-severity guarantee(s) broken — the "
                         f"score cannot exceed 74 while one stands")
        score = min(score, 74)

    return Report(coverage=coverage, findings=findings, statuses=statuses,
                  problems=problems, axes=axes, score=score, capped_by=capped_by)
