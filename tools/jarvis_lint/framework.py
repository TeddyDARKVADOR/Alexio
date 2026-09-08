"""
tools/jarvis_lint/framework.py — the plumbing every rule shares.

WHY THIS LAYER EXISTS AT ALL
    The audit of 2026-09-08 found twenty-one defects. Roughly half of them were
    not mistakes in the ordinary sense: they were places where an invariant
    *written down* in CLAUDE.md had no mechanical counterpart in the code.

        R-12  "un budget impossible lève une erreur"        — and core/ai/__init__
                                                              swallowed the raise
        R-18  "le dispatch consulte la porte"               — and the plugin branch
                                                              did not
        R-02  "aucun identifiant de modèle hors core/ai/"   — and two action
                                                              modules named one

    A rule that only exists in prose is a rule the next change breaks silently,
    whether the next change comes from a person or from an agent. So the rules
    move here, where they fail a build.

WHAT A RULE MUST BE, TO BE WORTH HAVING
    Precise before it is broad. A linter that cries wolf earns an ignore-list,
    and an ignore-list is prose again with extra steps. Every rule in this
    package is written against the *specific* shape of a defect that actually
    shipped, and each one is checked against its defect: see
    tests/test_architecture_rules.py, which asserts that each rule still fires
    on the code it was written for.

    Each finding carries three things, in the house style for error messages
    (what failed, then how to fix it):
      · where           — path and line
      · what is wrong   — naming the symbol, not the category
      · what to do      — the concrete edit that clears it

THE BASELINE, AND WHY IT IS NOT AN IGNORE-LIST
    The twenty-one defects are still in the tree. Shipping these rules green
    would mean exempting every one of them, which is how a guardrail becomes
    decoration on the day it is installed.

    Instead `baseline.json` records the violations that existed when the rules
    landed. The test fails on anything *not* in it. So:
      · new code cannot add a violation — that is the point;
      · fixing a defect means deleting a line from the baseline, and the file
        shrinks toward empty in public;
      · nothing is hidden — `python -m tools.jarvis_lint --all` prints the
        baseline too, and a stale entry is itself an error (a baseline that
        names a violation which no longer exists is history pretending to be
        debt, and `--check-stale` fails on it).

IDENTITY OF A FINDING
    Keyed on (rule, path, symbol), never on the line number. A finding that
    moves when someone adds an import above it is a finding that re-appears as
    "new" on every unrelated edit, and a baseline that churns is a baseline
    people regenerate blindly.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Iterator

ROOT = Path(__file__).resolve().parent.parent.parent

# Directories that are not Alexio's own source. `logs` and `config` hold data;
# `.venv` holds other people's code, and rglob would happily lint numpy.
_SKIP_DIRS = {".venv", ".git", "__pycache__", "logs", "build", "dist",
              ".pytest_cache", "config"}


@dataclass(frozen=True)
class Finding:
    """One violation, addressed so a human can act on it without a debugger."""

    rule:    str        # "JAR001"
    path:    str        # repo-relative, posix
    line:    int
    symbol:  str        # the function / flag / route this is about — the stable id
    message: str        # what is wrong, and what would clear it
    occurrence: int = 1  # nth identical (rule, path, symbol) in this file

    @property
    def key(self) -> str:
        """Baseline identity. Deliberately excludes the line number.

        The occurrence suffix exists because two findings can legitimately share
        all three coordinates: actions/dev_agent.py pins the same model id on
        lines 22 and 23, so both produce `JAR002|actions/dev_agent.py|<module>:
        gemini-flash-latest`. Collapsing them into one ledger entry would mean
        fixing one and having the other silently forgiven — a baseline that
        under-counts is a baseline that hides debt, which is the one thing it
        exists not to do.
        """
        base = f"{self.rule}|{self.path}|{self.symbol}"
        return base if self.occurrence == 1 else f"{base}#{self.occurrence}"

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.symbol} — {self.message}"


@dataclass
class Module:
    """One parsed source file, plus the two indexes rules keep re-deriving."""

    path:   Path
    rel:    str
    source: str
    tree:   ast.Module
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)

    #: every node in the file, walked once. Twenty rules each doing their own
    #: ast.walk(mod.tree) meant twenty full traversals per file and made
    #: `jarvis check` slow enough that people would stop running it — which
    #: costs far more than the traversals ever did.
    nodes: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.nodes = list(ast.walk(self.tree))
        for node in self.nodes:
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node

    # ── walking upward ───────────────────────────────────────────────────
    #
    # Rules about *dominance* ("was the gate consulted before this ran?") need
    # to climb, and ast gives you no parent pointer. Built once per file rather
    # than per rule.

    def ancestors(self, node: ast.AST) -> Iterator[ast.AST]:
        cur = self.parents.get(node)
        while cur is not None:
            yield cur
            cur = self.parents.get(cur)

    def enclosing_function(self, node: ast.AST):
        for a in self.ancestors(node):
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return a
        return None

    def enclosing_class(self, node: ast.AST):
        for a in self.ancestors(node):
            if isinstance(a, ast.ClassDef):
                return a
        return None

    def is_under(self, *prefixes: str) -> bool:
        return self.rel.startswith(prefixes)


# ── the registry ─────────────────────────────────────────────────────────────

RuleFn = Callable[[Module], Iterable[Finding]]

_RULES: dict[str, "RuleMeta"] = {}


# How much a violation of this rule is allowed to cost the health score.
# CRITICAL is not a louder HIGH: a single critical violation caps the global
# score outright (see tools/jarvis_health/score.py), because a weight can always
# be diluted by adding more green elsewhere and a cap cannot.
CRITICAL = "critical"
HIGH     = "high"
MEDIUM   = "medium"
LOW      = "low"

SEVERITIES = (CRITICAL, HIGH, MEDIUM, LOW)


@dataclass(frozen=True)
class RuleMeta:
    code:     str
    title:    str
    why:      str
    fn:       RuleFn
    severity: str = MEDIUM


def rule(code: str, title: str, why: str, severity: str = MEDIUM):
    """Register one rule.

    `why` is not decoration. A rule whose reason is not written down is a rule
    the next person deletes when it becomes inconvenient, and they will be right
    to, because nothing told them what it was protecting.
    """
    def _register(fn: RuleFn) -> RuleFn:
        if code in _RULES:
            raise ValueError(f"duplicate rule code {code}")
        if severity not in SEVERITIES:
            raise ValueError(f"{code}: unknown severity {severity!r}")
        _RULES[code] = RuleMeta(code=code, title=title, why=why, fn=fn,
                                severity=severity)
        return fn
    return _register


def all_rules() -> list[RuleMeta]:
    return [_RULES[c] for c in sorted(_RULES)]


# ── running ──────────────────────────────────────────────────────────────────

def project_files(root: Path = ROOT) -> Iterator[Path]:
    """Alexio's own Python, and nothing else.

    `glob`, not `rglob`, would be wrong here — the tree is genuinely nested. So
    the skip set is checked against every path component instead, which is the
    bug tests/test_optional_dependencies.py hit when `rglob("core/**/*.py")`
    matched `.venv/…/numpy/core/`.
    """
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def load_module(path: Path, root: Path = ROOT) -> Module | None:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, str(path))
    except (OSError, SyntaxError):
        return None
    return Module(path=path, rel=path.relative_to(root).as_posix(),
                  source=source, tree=tree)


def run(root: Path = ROOT, only: set[str] | None = None) -> list[Finding]:
    """Every finding in the tree, sorted for a stable diff."""
    findings: list[Finding] = []
    metas = [m for m in all_rules() if only is None or m.code in only]
    for path in project_files(root):
        module = load_module(path, root)
        if module is None:
            continue
        for meta in metas:
            findings.extend(meta.fn(module))

    findings.sort(key=lambda f: (f.path, f.line, f.rule, f.symbol))

    # Number the repeats, in the stable order above, so each real violation has
    # its own ledger line. See Finding.key.
    seen: dict[str, int] = {}
    numbered: list[Finding] = []
    for f in findings:
        base = f"{f.rule}|{f.path}|{f.symbol}"
        seen[base] = seen.get(base, 0) + 1
        numbered.append(f if seen[base] == 1
                        else replace(f, occurrence=seen[base]))
    return numbered


# ── the baseline ─────────────────────────────────────────────────────────────

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, str]:
    """{key: note}. The note says which audit finding this is, so the file reads
    as a ledger rather than as a list of hashes."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))["known"]
    except Exception:
        return {}


def write_baseline(findings: list[Finding], path: Path = BASELINE_PATH,
                   notes: dict[str, str] | None = None) -> None:
    notes = notes or {}
    payload = {
        "_comment": [
            "Violations that existed when tools/jarvis_lint landed (2026-09-08).",
            "This is a ledger of known debt, not an ignore-list: the test fails on",
            "anything NOT listed here, so new code cannot add to it. Fixing a defect",
            "means deleting its line. An entry that no longer reproduces is itself an",
            "error — run `python -m tools.jarvis_lint --check-stale`.",
        ],
        "known": {f.key: notes.get(f.key, f.message) for f in findings},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def partition(findings: list[Finding],
              baseline: dict[str, str]) -> tuple[list[Finding], list[str]]:
    """(new findings, baseline keys that no longer reproduce)."""
    seen = {f.key for f in findings}
    new = [f for f in findings if f.key not in baseline]
    stale = sorted(k for k in baseline if k not in seen)
    return new, stale
