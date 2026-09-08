"""
The health ledger says only true things.

WHY THIS EXISTS
    tools/jarvis_health/behaviors.py is written by hand — it has to be, because
    no tool can infer that the interesting question about core/ai/__init__.py is
    whether Budget.private() still refuses. Hand-written means it can drift, and
    a dashboard that drifts is worse than no dashboard: it reports green for a
    guarantee nobody checks any more.

    So the ledger is held to the same discipline as the lint baseline. Three
    ways it could quietly become fiction, and a test for each:

      · a behaviour names a module that no longer exists;
      · a behaviour names a test that was renamed — which would silently
        downgrade it to "unproven" instead of failing loudly, so the guarantee
        looks merely undone rather than unmoored;
      · a row's `open_since` no longer matches what the tools observe, in
        either direction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import jarvis_lint                                    # noqa: E402
from tools.jarvis_health import BEHAVIOURS                       # noqa: E402
from tools.jarvis_health.behaviors import WEIGHTS, CRITICAL, HIGH, MEDIUM, LOW  # noqa: E402
from tools.jarvis_health import report as health_report          # noqa: E402


@pytest.fixture(scope="module")
def collected() -> set[str]:
    return health_report.collected_tests(ROOT)


def test_every_behaviour_points_at_a_real_module():
    missing = [b.id for b in BEHAVIOURS if not (ROOT / b.module).exists()]
    assert not missing, (
        f"these guarantees name a module that does not exist: {missing}. "
        f"Update tools/jarvis_health/behaviors.py."
    )


def test_every_named_test_exists(collected):
    """A renamed test must fail here, not quietly turn its guarantee amber.

    The dashboard degrades a behaviour whose test is missing to UNTESTED, which
    reads as 'nobody has written this yet' — the wrong story entirely when what
    happened is that the proof was renamed out from under it.
    """
    if not collected:
        pytest.skip("pytest collection unavailable in this environment")
    orphans = [f"{b.id} → {t}" for b in BEHAVIOURS for t in b.tests
               if t not in collected]
    assert not orphans, (
        f"these guarantees name tests that no longer exist: {orphans}"
    )


def test_every_referenced_rule_exists():
    codes = {m.code for m in jarvis_lint.all_rules()}
    unknown = [f"{b.id} → {b.rule}" for b in BEHAVIOURS
               if b.rule and b.rule not in codes]
    assert not unknown, f"unknown jarvis_lint rules referenced: {unknown}"


def test_behaviour_ids_are_unique():
    ids = [b.id for b in BEHAVIOURS]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate behaviour ids: {dupes}"


def test_severities_are_known():
    bad = [b.id for b in BEHAVIOURS
           if b.severity not in (CRITICAL, HIGH, MEDIUM, LOW)]
    assert not bad, f"unknown severity on: {bad}"


def test_an_open_defect_carries_the_date_it_was_found():
    """`open_since` is what lets the risk map age. A row marked open with no
    date is debt with no clock on it.

    A row with a `rule` and no tests is not one of those: it is held by a clean
    lint rule, which is its own kind of evidence and carries its own positive
    and negative tests. See BY_RULE in tools/jarvis_health/report.py.
    """
    bad = [b.id for b in BEHAVIOURS
           if b.audit and not b.open_since and not b.tests and not b.rule]
    assert not bad, (
        f"these cite an audit finding, declare no test, and carry no open_since "
        f"— say when it was found: {bad}"
    )


def test_the_ledger_agrees_with_what_the_tools_observe():
    """The reconciliation, run as a test.

    Fails in both directions: a row marked open that is now tested and
    unviolated (history pretending to be debt), and a row violated right now
    with no open marker (a regression, or an entry never filled in).
    """
    rep = health_report.build(ROOT)
    assert not rep.problems, (
        "tools/jarvis_health/behaviors.py disagrees with the code:\n  "
        + "\n  ".join(rep.problems)
    )


def test_the_score_cannot_be_farmed_while_a_critical_guarantee_is_broken():
    """The one property the whole dashboard rests on.

    core/ai/router.py sits at 99% statement coverage with `Budget.private`
    measured at 100%, and the private-budget guarantee is broken. If a weighted
    average could outvote that, the number would be worth nothing.
    """
    rep = health_report.build(ROOT)
    if rep.critical_open:
        assert rep.score <= 49, (
            f"score {rep.score} with {len(rep.critical_open)} critical "
            f"guarantee(s) broken — the cap in tools/jarvis_health/report.py "
            f"is not holding"
        )


def test_the_axis_weights_are_a_distribution():
    total = sum(WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"axis weights sum to {total}, not 1.0"
    assert WEIGHTS["security"] + WEIGHTS["behaviour"] > WEIGHTS["lines"] \
        + WEIGHTS["branches"] + WEIGHTS["functions"], (
        "raw coverage outweighs guarantees — that is the ranking this whole "
        "tool exists to invert"
    )
