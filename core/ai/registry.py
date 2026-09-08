"""
core/ai/registry.py — what each model can do, declared and measured.

TWO SETS OF NUMBERS, ON PURPOSE
    `declared` comes from the vendor's documentation: capabilities, context
    window, price. It is correct and it is useless for choosing between two
    models that both work, because it says nothing about what a call from *this*
    machine on *this* network with *these* prompts actually costs in seconds.

    `measured` comes from logs/ai_calls.jsonl — p50 and p95 latency, and the
    share of calls that succeeded. The router reads `measured` and falls back to
    `declared` until there are enough samples to mean anything.

    This is the same discipline core/audio_devices.py already applies to sound
    hardware: it does not trust the host API's capability flags, it opens a
    stream and times it. A published benchmark cannot tell you that DirectSound
    output on this box is a silent sink, and it cannot tell you that one model
    fumbles a tool call once in twenty.

PRICES
    Per million tokens, from each vendor's own pricing page, checked 2026-09-07.
    They exist so the router can refuse a budget, not so anyone can invoice from
    them; `cache_read_ratio` matters more than the headline number on a workload
    as repetitive as this one, where ~14 kB of tool declarations ride along on
    every single request.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core import telemetry

from .types import Tier

# Enough calls before observed numbers are believed. Routing on three samples is
# routing on noise.
MIN_SAMPLES = 5


@dataclass(frozen=True)
class Declared:
    """What the vendor says."""
    capabilities:      frozenset[str]
    context_tokens:    int
    usd_in_per_mtok:   float
    usd_out_per_mtok:  float
    cache_read_ratio:  float = 0.10     # cached input billed at this fraction
    tool_dialect:      str   = "gemini"
    parallel_tools:    bool  = True
    typical_latency_ms: int  = 1500     # only used before there is evidence
    local:             bool  = False


@dataclass(frozen=True)
class Measured:
    """What this machine has actually seen. Empty until MIN_SAMPLES calls."""
    calls:       int   = 0
    ok_rate:     float = 1.0
    latency_p50: float = 0.0
    latency_p95: float = 0.0

    @property
    def trustworthy(self) -> bool:
        return self.calls >= MIN_SAMPLES


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model:    str
    tiers:    frozenset[str]
    declared: Declared
    measured: Measured = field(default_factory=Measured)

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.model)

    @property
    def latency_ms(self) -> float:
        """The typical call, observed if there is enough of it.

        COMPARE LIKE WITH LIKE
            This returned `measured.latency_p95` while falling back to
            `declared.typical_latency_ms` — a p95 and a median, in the same
            property, feeding the same comparison in router.candidates(). The
            effect was invisible until the fifth call and then abrupt: a model
            whose median is 650 ms, comfortably inside Budget.conversation()'s
            900 ms, started failing the filter the moment its p95 of 1400 ms
            replaced the declared 700. Measuring a provider made the router
            *stop* being able to use it.

            The declared number is documented as typical, so the measured
            counterpart is p50. The tail is not discarded — it is a different
            question, asked separately by `latency_p95_ms` below.
        """
        if self.measured.trustworthy and self.measured.latency_p50 > 0:
            return self.measured.latency_p50
        return float(self.declared.typical_latency_ms)

    @property
    def latency_p95_ms(self) -> float:
        """The bad call. For a caller who cares about the tail rather than the
        middle — a worst-case budget is a real thing to want, and it should say
        so rather than being smuggled in through the typical one."""
        if self.measured.trustworthy and self.measured.latency_p95 > 0:
            return self.measured.latency_p95
        # No tail observed yet: assume the shape the vendors' own numbers imply.
        return float(self.declared.typical_latency_ms) * 2.5

    def cost_estimate(self, tokens_in: int, tokens_out: int,
                      cached_in: int = 0) -> float:
        """USD for one call. Cached input is billed at cache_read_ratio, which
        on Gemini and Anthropic is a tenth — ignoring it overstates the cost of
        this workload several-fold."""
        d = self.declared
        fresh = max(0, tokens_in - cached_in)
        return (
            fresh     / 1_000_000 * d.usd_in_per_mtok
            + cached_in / 1_000_000 * d.usd_in_per_mtok * d.cache_read_ratio
            + tokens_out / 1_000_000 * d.usd_out_per_mtok
        )


# ── the catalogue ────────────────────────────────────────────────────────────
#
# Only models a provider adapter in this package can actually reach. Adding a
# row without an adapter would let the router pick something that cannot run.

_TEXT   = "text"
_VISION = "vision"
_AUDIO  = "audio"
_VIDEO  = "video"
_GROUND = "grounding"
_SYS    = "system_prompt"

CATALOGUE: list[ModelSpec] = [
    # ── Google ───────────────────────────────────────────────────────────
    ModelSpec(
        provider="gemini", model="gemini-flash-latest",
        tiers=frozenset({Tier.STANDARD, Tier.DEEP}),
        declared=Declared(
            capabilities=frozenset({_TEXT, _VISION, _AUDIO, _VIDEO, _GROUND, _SYS}),
            context_tokens=1_000_000,
            usd_in_per_mtok=0.30, usd_out_per_mtok=2.50,
            tool_dialect="gemini", typical_latency_ms=1200,
        ),
    ),
    ModelSpec(
        provider="gemini", model="gemini-flash-lite-latest",
        tiers=frozenset({Tier.FAST}),
        declared=Declared(
            capabilities=frozenset({_TEXT, _VISION, _AUDIO, _SYS}),
            context_tokens=1_000_000,
            usd_in_per_mtok=0.10, usd_out_per_mtok=0.40,
            tool_dialect="gemini", typical_latency_ms=700,
        ),
    ),

    # ── Anthropic ────────────────────────────────────────────────────────
    # No speech-to-speech API exists, so none of these can ever serve plane A.
    ModelSpec(
        provider="anthropic", model="claude-sonnet-5",
        tiers=frozenset({Tier.STANDARD, Tier.DEEP}),
        declared=Declared(
            capabilities=frozenset({_TEXT, _VISION, _SYS}),
            context_tokens=1_000_000,
            usd_in_per_mtok=2.00, usd_out_per_mtok=10.00,
            tool_dialect="anthropic", typical_latency_ms=2000,
        ),
    ),
    ModelSpec(
        provider="anthropic", model="claude-opus-5",
        tiers=frozenset({Tier.DEEP}),
        declared=Declared(
            capabilities=frozenset({_TEXT, _VISION, _SYS}),
            context_tokens=1_000_000,
            usd_in_per_mtok=5.00, usd_out_per_mtok=25.00,
            tool_dialect="anthropic", typical_latency_ms=4000,
        ),
    ),
    ModelSpec(
        provider="anthropic", model="claude-haiku-4-5-20251001",
        tiers=frozenset({Tier.FAST}),
        declared=Declared(
            capabilities=frozenset({_TEXT, _VISION, _SYS}),
            context_tokens=200_000,
            usd_in_per_mtok=1.00, usd_out_per_mtok=5.00,
            tool_dialect="anthropic", typical_latency_ms=900,
        ),
    ),

    # ── OpenAI ───────────────────────────────────────────────────────────
    ModelSpec(
        provider="openai", model="gpt-5.6-terra",
        tiers=frozenset({Tier.STANDARD, Tier.DEEP}),
        declared=Declared(
            capabilities=frozenset({_TEXT, _VISION, _SYS}),
            context_tokens=1_050_000,
            usd_in_per_mtok=2.00, usd_out_per_mtok=12.00,
            tool_dialect="openai", typical_latency_ms=2000,
        ),
    ),
    ModelSpec(
        provider="openai", model="gpt-5.6-luna",
        tiers=frozenset({Tier.FAST}),
        declared=Declared(
            capabilities=frozenset({_TEXT, _VISION, _SYS}),
            context_tokens=1_050_000,
            usd_in_per_mtok=0.20, usd_out_per_mtok=1.20,
            tool_dialect="openai", typical_latency_ms=800,
        ),
    ),

    # ── Local ────────────────────────────────────────────────────────────
    # The honest entry. On an i5-8265U with no GPU this is the largest model
    # that answers at conversational speed, and it is a classifier, not a
    # reasoner. Declaring a high latency is not pessimism, it is the measurement
    # the router needs to stop sending it work it cannot do in time.
    ModelSpec(
        provider="local", model="qwen3:1.7b",
        tiers=frozenset({Tier.FAST, Tier.STANDARD}),
        declared=Declared(
            capabilities=frozenset({_TEXT, _SYS}),
            context_tokens=32_000,
            usd_in_per_mtok=0.0, usd_out_per_mtok=0.0,
            cache_read_ratio=0.0,
            tool_dialect="openai", parallel_tools=False,
            typical_latency_ms=6000, local=True,
        ),
    ),
]


def load(catalogue: list[ModelSpec] | None = None) -> list[ModelSpec]:
    """The catalogue with `measured` filled in from the telemetry log.

    Called on every routing decision. That is deliberate: reading a few hundred
    JSONL lines costs well under a millisecond, and a router that caches its
    view of the world never notices that a provider started failing.
    """
    specs = catalogue if catalogue is not None else CATALOGUE
    try:
        stats = telemetry.summarise(min_samples=MIN_SAMPLES)
    except Exception:
        stats = {}

    out: list[ModelSpec] = []
    for spec in specs:
        s = stats.get(spec.key)
        if s is None:
            # Enrich, never reset. The shipped CATALOGUE carries no measurements
            # so this branch usually keeps an empty Measured — but overwriting
            # unconditionally would silently discard observations a caller had
            # already attached, which is both surprising and untestable.
            out.append(spec)
            continue
        out.append(ModelSpec(
            provider=spec.provider, model=spec.model, tiers=spec.tiers,
            declared=spec.declared,
            measured=Measured(
                calls=s["calls"], ok_rate=s["ok_rate"],
                latency_p50=s["latency_p50"], latency_p95=s["latency_p95"],
            ),
        ))
    return out


def describe() -> str:
    """Human-readable dump — `python -m core.ai.registry`."""
    rows = load()
    width = max(len(f"{s.provider}/{s.model}") for s in rows)
    lines = [f"{'provider/model':<{width}}  {'tiers':<18} {'latency':>9}  {'source':<9} in/out $/Mtok"]
    for s in sorted(rows, key=lambda r: (r.provider, r.model)):
        tiers  = ",".join(sorted(s.tiers))
        source = "measured" if s.measured.trustworthy else "declared"
        lines.append(
            f"{s.provider + '/' + s.model:<{width}}  {tiers:<18} "
            f"{s.latency_ms:>8.0f}ms  {source:<9} "
            f"{s.declared.usd_in_per_mtok:.2f}/{s.declared.usd_out_per_mtok:.2f}"
        )
    return "\n".join(lines)

# Not a __main__ block: core/ai/__init__.py imports this module, so `python -m
# core.ai.registry` re-executes it and Python warns about the double import.
#   python -c "from core.ai import registry; print(registry.describe())"
