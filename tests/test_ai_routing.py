"""
Phase 03 — tool dialects, the registry, and the router.

The three pieces that turn "one gateway" into "a system that chooses". The
router's contract is the one worth pinning down: it must refuse rather than
quietly answer with something ten times weaker, and it must prefer what this
machine has *observed* over what a vendor page *claims*.
"""

from __future__ import annotations

import pytest

from core.ai import registry, router, tools
from core.ai.registry import Declared, Measured, ModelSpec
from core.ai.router import Budget, NoModelFits, Privacy
from core.ai.types import Tier


# ── tool dialects ────────────────────────────────────────────────────────────

GEMINI_DECL = {
    "name": "open_app",
    "description": "Opens any application on the computer.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {"type": "STRING", "description": "Exact name"},
            "retries":  {"type": "INTEGER"},
            "tags":     {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["app_name"],
    },
}


def test_gemini_capitals_become_json_schema():
    spec = tools.ToolSpec.from_gemini(GEMINI_DECL)
    assert spec.parameters["type"] == "object"
    assert spec.parameters["properties"]["app_name"]["type"] == "string"
    assert spec.parameters["properties"]["retries"]["type"] == "integer"


def test_nested_types_are_converted_too():
    """A converter that only touches the top level produces a document that
    looks right and fails on the first array of objects."""
    spec = tools.ToolSpec.from_gemini(GEMINI_DECL)
    assert spec.parameters["properties"]["tags"]["items"]["type"] == "string"


def test_the_three_dialects_have_the_shapes_each_vendor_wants():
    spec = tools.ToolSpec.from_gemini(GEMINI_DECL)

    g = spec.to_gemini()
    assert g["parameters"]["type"] == "OBJECT"
    assert g["parameters"]["properties"]["app_name"]["type"] == "STRING"

    a = spec.to_anthropic()
    assert "input_schema" in a and "parameters" not in a
    assert a["input_schema"]["type"] == "object"

    o = spec.to_openai()
    assert o["type"] == "function"
    assert o["function"]["parameters"]["properties"]["retries"]["type"] == "integer"


def test_a_gemini_declaration_survives_a_round_trip():
    spec = tools.ToolSpec.from_gemini(GEMINI_DECL)
    assert spec.to_gemini()["parameters"] == GEMINI_DECL["parameters"]


def test_an_unknown_dialect_is_refused_by_name():
    spec = tools.ToolSpec.from_gemini(GEMINI_DECL)
    with pytest.raises(ValueError, match="Unknown tool dialect"):
        spec.render("cohere")


def test_the_real_declarations_all_convert():
    """The 24 tools Alexio ships must survive translation, or routing a
    tool-calling request anywhere but Gemini produces a 400."""
    import main

    specs = tools.adopt_gemini_declarations(
        main.TOOL_DECLARATIONS, irreversible={"shutdown_jarvis"}
    )
    assert len(specs) == len(main.TOOL_DECLARATIONS)
    for spec in specs:
        for dialect in ("gemini", "anthropic", "openai"):
            rendered = spec.render(dialect)
            assert rendered["name"] if dialect != "openai" else rendered["function"]["name"]

    assert [s for s in specs if s.name == "shutdown_jarvis"][0].irreversible


# ── registry ─────────────────────────────────────────────────────────────────

def _spec(provider="p", model="m", tier=Tier.FAST, latency=1000,
          caps=("text",), local=False, **kw):
    return ModelSpec(
        provider=provider, model=model, tiers=frozenset({tier}),
        declared=Declared(
            capabilities=frozenset(caps), context_tokens=100_000,
            usd_in_per_mtok=kw.get("usd_in", 1.0),
            usd_out_per_mtok=kw.get("usd_out", 5.0),
            typical_latency_ms=latency, local=local,
        ),
        measured=kw.get("measured", Measured()),
    )


def test_declared_latency_is_used_until_there_is_evidence():
    s = _spec(latency=1500, measured=Measured(calls=2, latency_p95=99_000))
    assert s.measured.trustworthy is False
    assert s.latency_ms == 1500


def test_measured_latency_wins_once_there_are_enough_samples():
    s = _spec(latency=1500,
              measured=Measured(calls=registry.MIN_SAMPLES, latency_p95=4200))
    assert s.latency_ms == 4200


def test_cached_input_is_priced_at_a_tenth():
    """Ignoring the cache overstates this workload several-fold: ~14 kB of tool
    declarations ride along on every single request."""
    s = _spec(usd_in=10.0, usd_out=0.0)
    full   = s.cost_estimate(tokens_in=1_000_000, tokens_out=0, cached_in=0)
    cached = s.cost_estimate(tokens_in=1_000_000, tokens_out=0, cached_in=1_000_000)
    assert full == pytest.approx(10.0)
    assert cached == pytest.approx(1.0)


def test_the_shipped_catalogue_is_coherent():
    for s in registry.CATALOGUE:
        assert s.tiers, f"{s.model} serves no tier"
        assert "text" in s.declared.capabilities, f"{s.model} cannot do text"
        assert s.declared.tool_dialect in ("gemini", "anthropic", "openai")
        if s.declared.local:
            assert s.declared.usd_in_per_mtok == 0.0


def test_no_anthropic_model_claims_audio():
    """There is no speech-to-speech API; a catalogue that suggested otherwise
    would send the conversational plane somewhere it cannot go."""
    for s in registry.CATALOGUE:
        if s.provider == "anthropic":
            assert "audio" not in s.declared.capabilities


# ── router ───────────────────────────────────────────────────────────────────

CAT = [
    _spec("slow",  "big",   Tier.FAST, latency=5000, usd_in=0.1),
    _spec("quick", "small", Tier.FAST, latency=300,  usd_in=9.9),
    _spec("mid",   "mid",   Tier.FAST, latency=800,  usd_in=1.0),
]


def test_the_fastest_model_that_fits_wins():
    best = router.choose(Budget(tier=Tier.FAST), catalogue=CAT)
    assert best.provider == "quick"


def test_price_breaks_ties_between_models_that_both_answer_in_time():
    tied = [
        _spec("cheap", "a", Tier.FAST, latency=500, usd_in=0.1),
        _spec("dear",  "b", Tier.FAST, latency=500, usd_in=9.0),
    ]
    assert router.choose(Budget(tier=Tier.FAST), catalogue=tied).provider == "cheap"


def test_a_latency_ceiling_excludes_what_cannot_meet_it():
    best = router.choose(Budget(tier=Tier.FAST, max_latency_ms=1000), catalogue=CAT)
    assert best.provider in ("quick", "mid")

    with pytest.raises(NoModelFits):
        router.choose(Budget(tier=Tier.FAST, max_latency_ms=100), catalogue=CAT)


def test_a_missing_capability_excludes_a_model():
    cat = [_spec("blind", "t", Tier.FAST, caps=("text",)),
           _spec("seeing", "v", Tier.FAST, caps=("text", "vision"), latency=9000)]
    best = router.choose(Budget(tier=Tier.FAST, needs=frozenset({"text", "vision"})),
                         catalogue=cat)
    assert best.provider == "seeing"


def test_local_only_refuses_to_leave_the_machine():
    cat = [_spec("cloud", "c", Tier.FAST, latency=100),
           _spec("local", "l", Tier.FAST, latency=9000, local=True)]
    best = router.choose(Budget(tier=Tier.FAST, privacy=Privacy.LOCAL_ONLY),
                         catalogue=cat)
    assert best.provider == "local"


def test_local_only_raises_when_nothing_is_local():
    with pytest.raises(NoModelFits, match="not local"):
        router.choose(Budget(tier=Tier.FAST, privacy=Privacy.LOCAL_ONLY), catalogue=CAT)


def test_a_provider_observed_to_be_failing_is_skipped():
    cat = [
        _spec("broken", "b", Tier.FAST, latency=100,
              measured=Measured(calls=50, ok_rate=0.10, latency_p95=100)),
        _spec("healthy", "h", Tier.FAST, latency=2000,
              measured=Measured(calls=50, ok_rate=1.0, latency_p95=2000)),
    ]
    assert router.choose(Budget(tier=Tier.FAST), catalogue=cat).provider == "healthy"


def test_one_bad_call_does_not_route_away_from_a_good_model():
    """A single blip is a network, not a broken provider."""
    cat = [_spec("fine", "f", Tier.FAST, latency=100,
                 measured=Measured(calls=100, ok_rate=0.99, latency_p95=100))]
    assert router.choose(Budget(tier=Tier.FAST), catalogue=cat).provider == "fine"


def test_an_impossible_budget_explains_itself_per_candidate():
    with pytest.raises(NoModelFits) as excinfo:
        router.choose(Budget(tier=Tier.FAST, max_latency_ms=50), catalogue=CAT)
    message = str(excinfo.value)
    assert "latency<=50ms" in message
    assert "quick/small" in message      # names a candidate and why it lost


def test_a_cost_ceiling_is_enforced():
    with pytest.raises(NoModelFits, match="costs"):
        router.choose(Budget(tier=Tier.FAST, max_cost_usd=0.0000001), catalogue=CAT)


def test_available_restricts_to_providers_this_install_can_reach():
    best = router.choose(Budget(tier=Tier.FAST), available={"mid"}, catalogue=CAT)
    assert best.provider == "mid"


def test_the_ready_made_budgets_are_what_they_claim():
    assert Budget.conversation().max_latency_ms == 900
    assert Budget.private().privacy == Privacy.LOCAL_ONLY
    assert Budget.work().tier == Tier.DEEP


def test_explain_names_the_winner_and_the_source_of_its_latency():
    out = router.explain(Budget(tier=Tier.FAST),
                         available={"quick", "mid"}, catalogue=CAT)
    assert "→ quick/small" in out
    assert "declared" in out          # no telemetry yet, and it says so


def test_explain_says_why_when_nothing_fits():
    out = router.explain(Budget(tier=Tier.FAST, max_latency_ms=1), catalogue=CAT)
    assert "No model fits" in out


# ── the facade uses it ───────────────────────────────────────────────────────

def test_only_providers_with_credentials_are_reachable(monkeypatch):
    from core import ai

    monkeypatch.setattr(ai.anthropic, "has_credentials", lambda: False)
    monkeypatch.setattr(ai.openai,    "has_credentials", lambda: True)

    reachable = ai.reachable_providers()
    assert "anthropic" not in reachable
    assert "openai" in reachable
    # gemini and local expose no has_credentials(); they are always candidates
    # and fail at call time with a typed error instead.
    assert {"gemini", "local"} <= reachable
