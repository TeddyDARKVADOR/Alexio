"""
core/ai/router.py — choose a model from a budget, not from a preference.

WHY A BUDGET AND NOT A RULE
    "reasoning + coding → Claude" is a rule, and rules say nothing about what
    happens when the model is slow, rate-limited, or more expensive than the
    caller can afford. It also cannot express the thing that matters most in a
    voice assistant: the user is listening, and there is a number of
    milliseconds past which the right answer is the wrong answer.

    So a caller states constraints, and the router finds what fits:

        Budget(max_latency_ms=900, privacy=Privacy.ANY, needs={"vision"})

    Filter by capability and privacy, drop anything observed to be failing,
    order by latency then by price, take the first. If nothing fits, raise —
    a budget that cannot be met is a decision for the caller, and an assistant
    that quietly answers with a model ten times weaker is worse than one that
    says it cannot.

WHAT THIS IS NOT, YET
    The intent classifier that would let "turn the volume up" skip the model
    entirely still has to be built; that is the Reflex class in the plan, and it
    belongs next to the wake word in the local-pipeline phase. This module
    decides *which model*, not *whether a model at all*.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import CapabilityUnavailable
from .registry import ModelSpec, load
from .types import Tier


class Privacy:
    ANY        = "any"
    LOCAL_ONLY = "local_only"   # nothing leaves the machine


# A provider observed to fail more often than this is skipped while a healthy
# alternative exists. Not zero: one bad call in a hundred is a network, not a
# broken provider, and routing away from a good model on a single blip would
# make the system less reliable, not more.
MIN_OK_RATE = 0.80


@dataclass(frozen=True)
class Budget:
    """What a request can afford."""

    tier:           str        = Tier.STANDARD
    max_latency_ms: int | None = None
    max_cost_usd:   float | None = None
    privacy:        str        = Privacy.ANY
    needs:          frozenset[str] = field(default_factory=lambda: frozenset({"text"}))

    # Rough shape of the call, used only to price candidates against
    # max_cost_usd. Defaults are the median Alexio request: the system prompt
    # plus tool declarations in, a short answer out.
    est_tokens_in:  int = 4_000
    est_tokens_out: int = 300
    est_cached_in:  int = 3_000

    @staticmethod
    def conversation() -> "Budget":
        """The user is listening. Anything slower than this is the wrong answer."""
        return Budget(tier=Tier.FAST, max_latency_ms=900)

    @staticmethod
    def work() -> "Budget":
        """Planning, projects, analysis. Worth waiting for, within reason."""
        return Budget(tier=Tier.DEEP, max_latency_ms=60_000)

    @staticmethod
    def private() -> "Budget":
        """Must not leave the machine."""
        return Budget(tier=Tier.FAST, privacy=Privacy.LOCAL_ONLY)


class NoModelFits(CapabilityUnavailable):
    """No catalogue entry satisfies the budget. Carries why, per candidate."""

    def __init__(self, budget: Budget, reasons: list[str]):
        self.budget  = budget
        self.reasons = reasons
        detail = "; ".join(reasons[:6]) or "the catalogue is empty"
        super().__init__(
            f"No model fits {_describe(budget)} — {detail}"
        )


def _describe(b: Budget) -> str:
    bits = [f"tier={b.tier}", f"needs={sorted(b.needs)}"]
    if b.max_latency_ms is not None:
        bits.append(f"latency<={b.max_latency_ms}ms")
    if b.max_cost_usd is not None:
        bits.append(f"cost<=${b.max_cost_usd:.4f}")
    if b.privacy != Privacy.ANY:
        bits.append(f"privacy={b.privacy}")
    return "Budget(" + ", ".join(bits) + ")"


def candidates(budget: Budget,
               available: set[str] | None = None,
               catalogue: list[ModelSpec] | None = None) -> list[ModelSpec]:
    """Every model that fits, best first.

    `available` restricts to providers that have an adapter *and* credentials
    right now — the catalogue describes what exists, not what this install can
    reach.
    """
    rows = load(catalogue)
    fits: list[ModelSpec] = []
    reasons: list[str] = []
    unreachable: set[str] = set()

    for spec in rows:
        label = f"{spec.provider}/{spec.model}"

        if available is not None and spec.provider not in available:
            # Not an interesting reason *per model* — but when it is the reason
            # for every model, it is the only thing worth saying. A fresh install
            # with no API key was told "the catalogue is empty", which named the
            # wrong cause on the single most common failure there is.
            unreachable.add(spec.provider)
            continue
        if budget.tier not in spec.tiers:
            continue
        if not budget.needs <= spec.declared.capabilities:
            missing = sorted(budget.needs - spec.declared.capabilities)
            reasons.append(f"{label} lacks {missing}")
            continue
        if budget.privacy == Privacy.LOCAL_ONLY and not spec.declared.local:
            reasons.append(f"{label} is not local")
            continue
        if budget.max_latency_ms is not None and spec.latency_ms > budget.max_latency_ms:
            reasons.append(f"{label} is ~{spec.latency_ms:.0f}ms")
            continue
        if budget.max_cost_usd is not None:
            cost = spec.cost_estimate(budget.est_tokens_in, budget.est_tokens_out,
                                      budget.est_cached_in)
            if cost > budget.max_cost_usd:
                reasons.append(f"{label} costs ~${cost:.4f}")
                continue
        if spec.measured.trustworthy and spec.measured.ok_rate < MIN_OK_RATE:
            reasons.append(f"{label} is failing ({spec.measured.ok_rate:.0%} ok)")
            continue

        fits.append(spec)

    # Fastest first, then cheapest. Latency leads because this is an assistant
    # someone is waiting on; price breaks ties between models that both answer
    # in time.
    fits.sort(key=lambda s: (
        s.latency_ms,
        s.cost_estimate(budget.est_tokens_in, budget.est_tokens_out, budget.est_cached_in),
    ))

    if not fits:
        if not reasons and unreachable:
            reasons = [f"no credentials on this machine for "
                       f"{', '.join(sorted(unreachable))} — add a key to "
                       f"config/api_keys.json, or install the local model"]
        raise NoModelFits(budget, reasons)
    return fits


def choose(budget: Budget,
           available: set[str] | None = None,
           catalogue: list[ModelSpec] | None = None) -> ModelSpec:
    """The single best model for this budget."""
    return candidates(budget, available, catalogue)[0]


def explain(budget: Budget,
            available: set[str] | None = None,
            catalogue: list[ModelSpec] | None = None) -> str:
    """Why the router would pick what it picks. For the log and for debugging a
    surprising choice — a router nobody can interrogate is a router nobody
    trusts."""
    try:
        rows = candidates(budget, available, catalogue)
    except NoModelFits as e:
        return str(e)

    lines = [f"{_describe(budget)} →"]
    for i, s in enumerate(rows):
        source = "measured" if s.measured.trustworthy else "declared"
        cost   = s.cost_estimate(budget.est_tokens_in, budget.est_tokens_out,
                                 budget.est_cached_in)
        mark   = "→" if i == 0 else " "
        lines.append(f"  {mark} {s.provider}/{s.model}  "
                     f"{s.latency_ms:.0f}ms ({source})  ${cost:.5f}")
    return "\n".join(lines)
