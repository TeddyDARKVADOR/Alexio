"""
core/ai — the one place Alexio talks to a model.

The gateway's job is to be boring and predictable, so these tests are about its
promises rather than about any provider: capability routing, fallback, typed
errors, and the fact that every call lands in the telemetry log. No network is
touched — the providers are replaced with stubs.
"""

from __future__ import annotations

import json
import types as pytypes

import pytest

from core import ai, telemetry
from core.ai import gemini, local
from core.ai.types import Completion, Media, Tier, Usage


# ── stub providers ───────────────────────────────────────────────────────────

def _stub(name: str, capabilities: set[str], *, fails_with=None, text="ok"):
    """A provider module with the same shape as gemini.py / local.py."""
    mod = pytypes.SimpleNamespace()
    mod.NAME = name
    mod.CAPABILITIES = capabilities
    mod.calls = []

    def model_for(tier, grounding=False):
        return f"{name}-{tier}"

    def generate(prompt, system=None, media=None, tier=Tier.STANDARD,
                 grounding=False, timeout=60):
        mod.calls.append({"prompt": prompt, "tier": tier, "grounding": grounding,
                          "media": media, "system": system})
        if fails_with is not None:
            raise fails_with
        return Completion(text=text, provider=name, model=model_for(tier),
                          usage=Usage(tokens_in=10, tokens_out=5, cached_in=4))

    mod.model_for = model_for
    mod.generate  = generate
    return mod


@pytest.fixture
def providers(monkeypatch):
    """Swap the provider table; yield a dict the test can rebuild."""
    def install(**mods):
        monkeypatch.setattr(ai, "_PROVIDERS", dict(mods))
        monkeypatch.setattr(ai, "_config", lambda: {"ai_provider": next(iter(mods))})
        return mods
    return install


def _rows(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── the vocabulary ───────────────────────────────────────────────────────────

def test_media_capability_is_read_from_the_mime_type():
    assert Media(b"", "image/png").capability()  == "vision"
    assert Media(b"", "audio/mp3").capability()  == "audio"
    assert Media(b"", "video/mp4").capability()  == "video"


def test_completion_reads_as_its_text():
    c = Completion(text="  hello  ", provider="p", model="m")
    assert str(c) == "  hello  "
    assert c.stripped == "hello"


# ── routing ──────────────────────────────────────────────────────────────────

def test_the_configured_provider_answers_when_it_can(providers, temp_telemetry):
    mods = providers(alpha=_stub("alpha", {"text"}), beta=_stub("beta", {"text"}))
    out = ai.generate("hi", task="t")
    assert out.provider == "alpha"
    assert mods["beta"].calls == []


def test_a_provider_without_the_capability_is_skipped(providers, temp_telemetry):
    """Sending an image to a text-only model must reach the one that can see,
    not fail and not silently drop the image."""
    mods = providers(
        blind=_stub("blind", {"text"}),
        seeing=_stub("seeing", {"text", "vision"}),
    )
    out = ai.generate("what is this", media=[Media(b"\x89PNG", "image/png")], task="t")
    assert out.provider == "seeing"
    assert mods["blind"].calls == []


def test_no_capable_provider_raises_rather_than_guessing(providers, temp_telemetry):
    providers(blind=_stub("blind", {"text"}))
    with pytest.raises(ai.CapabilityUnavailable):
        ai.generate("look", media=[Media(b"", "image/png")], task="t")


def test_grounding_needs_a_provider_that_can_search(providers, temp_telemetry):
    mods = providers(
        plain=_stub("plain", {"text"}),
        grounded=_stub("grounded", {"text", "grounding"}),
    )
    out = ai.generate("news today", grounding=True, task="t")
    assert out.provider == "grounded"
    assert mods["grounded"].calls[0]["grounding"] is True


# ── failure handling ─────────────────────────────────────────────────────────

def test_an_unavailable_provider_falls_back_to_the_next(providers, temp_telemetry):
    mods = providers(
        broken=_stub("broken", {"text"}, fails_with=ai.ProviderUnavailable("down")),
        spare=_stub("spare", {"text"}),
    )
    out = ai.generate("hi", task="t")
    assert out.provider == "spare"
    assert len(mods["broken"].calls) == 1


def test_a_quota_error_also_falls_back(providers, temp_telemetry):
    providers(
        tapped=_stub("tapped", {"text"}, fails_with=ai.QuotaExceeded("429")),
        spare=_stub("spare", {"text"}),
    )
    assert ai.generate("hi", task="t").provider == "spare"


def test_an_empty_answer_is_not_retried_elsewhere(providers, temp_telemetry):
    """A model that returns nothing is not a provider outage — trying someone
    else just spends a second call to get the same nothing."""
    mods = providers(
        mute=_stub("mute", {"text"}, fails_with=ai.EmptyResponse("nothing")),
        spare=_stub("spare", {"text"}),
    )
    with pytest.raises(ai.EmptyResponse):
        ai.generate("hi", task="t")
    assert mods["spare"].calls == []


def test_when_everyone_fails_the_last_error_surfaces(providers, temp_telemetry):
    providers(
        a=_stub("a", {"text"}, fails_with=ai.ProviderUnavailable("a down")),
        b=_stub("b", {"text"}, fails_with=ai.ProviderUnavailable("b down")),
    )
    with pytest.raises(ai.ProviderUnavailable, match="b down"):
        ai.generate("hi", task="t")


# ── telemetry (rule R-11) ────────────────────────────────────────────────────

def test_every_call_is_logged_with_its_task_and_usage(providers, temp_telemetry):
    providers(alpha=_stub("alpha", {"text"}))
    ai.generate("hi", task="summarize", tier=Tier.FAST)

    row = _rows(temp_telemetry)[0]
    assert row["plane"] == "B"
    assert row["provider"] == "alpha"
    assert row["task"] == "summarize"
    assert row["tokens_in"] == 10 and row["cached_in"] == 4
    assert row["extra"]["tier"] == "fast"


def test_a_failed_attempt_is_logged_too(providers, temp_telemetry):
    providers(
        broken=_stub("broken", {"text"}, fails_with=ai.ProviderUnavailable("down")),
        spare=_stub("spare", {"text"}),
    )
    ai.generate("hi", task="t")

    rows = _rows(temp_telemetry)
    assert len(rows) == 2, "the fallback must not hide the failed first attempt"
    assert rows[0]["ok"] is False and rows[0]["provider"] == "broken"
    assert rows[1]["ok"] is True and rows[1]["provider"] == "spare"


# ── tiers ────────────────────────────────────────────────────────────────────

def test_an_unknown_tier_degrades_to_standard(providers, temp_telemetry):
    mods = providers(alpha=_stub("alpha", {"text"}))
    ai.generate("hi", tier="enormous", task="t")
    assert mods["alpha"].calls[0]["tier"] == Tier.STANDARD


def test_gemini_promotes_a_grounded_fast_request():
    """The lite model does not reliably carry the search tool, so a grounded
    FAST request is promoted rather than quietly returning an ungrounded answer."""
    assert gemini.model_for(Tier.FAST) != gemini.model_for(Tier.FAST, grounding=True)
    assert gemini.model_for(Tier.FAST, grounding=True) == gemini.model_for(Tier.STANDARD)


def test_the_local_provider_admits_what_it_cannot_do():
    assert "vision" not in local.CAPABILITIES
    assert "grounding" not in local.CAPABILITIES
    with pytest.raises(ai.CapabilityUnavailable):
        local.generate("hi", media=[Media(b"", "image/png")])
    with pytest.raises(ai.CapabilityUnavailable):
        local.generate("hi", grounding=True)


def test_gemini_reads_text_split_across_parts():
    """A grounded answer arrives in parts with `.text` empty. Reading only
    `.text` is what turned good answers into "no results"."""
    class _Part:  text = "world"
    class _Content: parts = [pytypes.SimpleNamespace(text="hello "), _Part()]
    class _Cand:  content = _Content()
    resp = pytypes.SimpleNamespace(text="", candidates=[_Cand()])

    assert gemini._text_of(resp) == "hello world"


def test_gemini_classifies_errors_by_meaning():
    assert isinstance(gemini._classify(Exception("429 RESOURCE_EXHAUSTED")), ai.QuotaExceeded)
    assert isinstance(gemini._classify(Exception("API key not valid")), ai.ProviderUnavailable)
    assert not isinstance(gemini._classify(Exception("something odd")), ai.AIError)
