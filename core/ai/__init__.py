"""
core/ai — the one place Alexio talks to a language model.

TWO PLANES, NOT ONE
    A voice assistant carries two kinds of traffic, and they are not the same
    shape:

      Plane A — the conversation. A socket held open for the whole session,
                audio in both directions, server-side state, a resumption
                handle, measured in milliseconds. Very few providers can do it
                at all; Anthropic has no speech-to-speech API, so Claude can
                never serve this plane. It lives in main.py and stays there
                until phase 02 extracts a VoiceSession protocol for it.

      Plane B — everything else. One round trip, no state, cancellable,
                measured in seconds: summarise this file, plan this project,
                pull today's headlines, read this screenshot. Every provider
                can do it. **That is this module.**

    Forcing both through one `generate()` would either cripple the realtime
    path or make the batch path carry session state it has no use for.

WHY IT EXISTS
    Plane B used to be roughly forty `genai.Client(...)` calls spread across ten
    action modules, each with its own key loading, its own model name, its own
    error handling and no telemetry at all. Adding a second provider meant
    touching ten files; measuring anything meant touching forty call sites.

WHAT A CALLER SAYS
    An action states what it needs, never who should answer:

        from core import ai

        text = ai.generate("Summarise this:\\n" + body, task="summarize").text

        ai.generate(prompt, media=[ai.Image.from_path(p)], task="describe_image")
        ai.generate(query, grounding=True, tier=ai.Tier.STANDARD, task="web_search")

    Every call is timed and logged to logs/ai_calls.jsonl (core/telemetry), which
    is what the registry and router in phase 03 are built on.

CHOOSING A PROVIDER
    config/api_keys.json → "ai_provider": "gemini" (default) | "local".
    A request needing a capability the chosen provider lacks — vision or web
    grounding on the local model — falls back to a provider that has it, and
    says so in the log. There is no router yet: that is phase 03, and it will
    replace `_resolve` below without touching a single call site.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

from core import telemetry

from . import anthropic, gemini, local, openai, registry, router
from .errors import (
    AIError,
    CapabilityUnavailable,
    EmptyResponse,
    ProviderUnavailable,
    QuotaExceeded,
)
from .router import Budget, NoModelFits, Privacy
from .tools import ToolSpec, adopt_gemini_declarations, render_all
from .types import Completion, Image, Media, Tier, Usage

__all__ = [
    "generate", "generate_text", "available_providers", "active_provider",
    "reachable_providers", "explain_routing",
    "Completion", "Image", "Media", "Tier", "Usage",
    "Budget", "Privacy", "NoModelFits",
    "ToolSpec", "adopt_gemini_declarations", "render_all",
    "AIError", "CapabilityUnavailable", "EmptyResponse",
    "ProviderUnavailable", "QuotaExceeded",
]

_PROVIDERS = {
    gemini.NAME:    gemini,
    anthropic.NAME: anthropic,
    openai.NAME:    openai,
    local.NAME:     local,
}

_DEFAULT_PROVIDER = gemini.NAME

# Set by main.py so a fallback or a provider outage is visible in the HUD
# instead of on a stdout nobody reads.
_log = None


def set_logger(fn) -> None:
    """Register a callable(str) for gateway-level notices."""
    global _log
    _log = fn


def _notify(msg: str) -> None:
    print(f"[AI] {msg}")
    if _log:
        try:
            _log(f"SYS: {msg}")
        except Exception:
            pass


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


def _config() -> dict:
    try:
        return json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def active_provider() -> str:
    name = (_config().get("ai_provider") or _DEFAULT_PROVIDER).strip().lower()
    return name if name in _PROVIDERS else _DEFAULT_PROVIDER


def available_providers() -> dict[str, set[str]]:
    """{provider name: capabilities} — the seed of phase 03's registry."""
    return {name: set(mod.CAPABILITIES) for name, mod in _PROVIDERS.items()}


def reachable_providers() -> set[str]:
    """Providers with an adapter AND credentials on this machine.

    The catalogue in registry.py describes what exists in the world; this says
    what this install can actually call. Without the distinction the router
    happily picks Claude on a box that has no Anthropic key.
    """
    out = set()
    for name, mod in _PROVIDERS.items():
        check = getattr(mod, "has_credentials", None)
        if check is None or check():
            out.add(name)
    return out


def _resolve(needs: set[str], tier: str, budget: Budget | None = None) -> list:
    """Providers that can serve this request, best first.

    This is the seam phase 03 filled in. It used to be "the configured provider,
    then anyone else"; it now asks the router, which filters the catalogue by
    capability, privacy, latency and cost and orders what is left by *measured*
    p95 latency where there is enough evidence to mean anything.

    The configured provider still leads when it survives the filter — an
    explicit choice in config should not be quietly overruled by a router that
    thinks it knows better — but it no longer wins by default.
    """
    reachable = reachable_providers()
    asked = budget or Budget(tier=tier, needs=frozenset(needs))

    # `replace`, not a fresh Budget: rebuilding it field by field dropped
    # est_tokens_in / est_tokens_out / est_cached_in, so a caller who had
    # described the shape of their request got the median-request defaults
    # priced against their max_cost_usd instead of their own numbers.
    budget = dataclasses.replace(asked, tier=tier, needs=frozenset(needs))

    # A budget states a constraint. When nothing satisfies it, the honest answer
    # is to say so — NOT to widen the search until something does.
    #
    # This used to catch NoModelFits, set `ranked = []`, and fall through to the
    # loop below, which appends every reachable provider. So Budget.private(),
    # documented as "ne quitte pas la machine", sent the prompt to Gemini,
    # Anthropic and OpenAI in precisely the situation the guarantee exists for:
    # no local model available. R-12 says a budget that cannot be met raises.
    #
    # The one widening that is still correct is the *unconstrained* case: a
    # caller who asked only for a tier is not asserting anything the catalogue
    # could violate, so a provider the catalogue has not caught up with may
    # still answer. That is the `constrained` test below, and it is the whole
    # difference between a fallback and a broken promise.
    constrained = (budget.max_latency_ms is not None
                   or budget.max_cost_usd is not None
                   or budget.privacy != Privacy.ANY)
    try:
        ranked = router.candidates(budget, available=reachable)
    except NoModelFits:
        if constrained:
            raise
        ranked = []

    order: list[str] = []
    for spec in ranked:
        if spec.provider not in order:
            order.append(spec.provider)

    preferred = active_provider()
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)

    if not constrained:
        for name in _PROVIDERS:
            if (name not in order and name in reachable
                    and needs <= _PROVIDERS[name].CAPABILITIES):
                order.append(name)

    return [_PROVIDERS[n] for n in order if needs <= _PROVIDERS[n].CAPABILITIES]


def explain_routing(tier: str = Tier.STANDARD, needs: set[str] | None = None) -> str:
    """Why the router would pick what it picks — for the log, and for the day a
    choice looks wrong. A router nobody can interrogate is a router nobody
    trusts."""
    return router.explain(
        Budget(tier=tier, needs=frozenset(needs or {"text"})),
        available=reachable_providers(),
    )


def generate(
    prompt:    str,
    *,
    system:    str | None         = None,
    media:     list[Media] | None = None,
    tier:      str                = Tier.STANDARD,
    grounding: bool               = False,
    task:      str                = "generic",
    budget:    Budget | None      = None,
    timeout:   int                = 60,
) -> Completion:
    """Ask for one answer.

    prompt    — the request.
    system    — standing instructions, when the provider supports them.
    media     — pictures to look at or audio to listen to. The mime type
                decides which capability the request needs.
    tier      — Tier.FAST | STANDARD | DEEP. What the job deserves, not a model.
    grounding — answer must be backed by a live web search.
    task      — a short label for the telemetry log ("summarize", "plan", …).
                It is what makes the log readable months later, so name it.
    budget    — optional latency / cost / privacy ceiling. Budget.conversation()
                when the user is listening, Budget.private() when nothing may
                leave the machine. Omitted, only the tier constrains the choice.

    Raises an AIError subclass when no provider can serve the request, or when
    the one that could, failed. Never returns an empty Completion: a model that
    says nothing raises EmptyResponse, because silence spoken aloud is a bug
    that used to reach the user as a pause.
    """
    if tier not in Tier.ALL:
        tier = Tier.STANDARD

    needs = {"text"}
    for item in (media or []):
        needs.add(item.capability())
    if grounding:
        needs.add("grounding")

    candidates = _resolve(needs, tier, budget)
    if not candidates:
        raise CapabilityUnavailable(
            f"No configured provider can handle {sorted(needs)}. "
            f"Available: { {k: sorted(v) for k, v in available_providers().items()} }"
        )

    last: Exception | None = None
    for i, provider in enumerate(candidates):
        if i > 0:
            _notify(f"{candidates[i-1].NAME} unavailable — falling back to {provider.NAME}.")
        model = provider.model_for(tier, grounding)
        try:
            with telemetry.record(plane="B", provider=provider.NAME, model=model,
                                  task=task, tier=tier, grounding=grounding) as rec:
                out = provider.generate(
                    prompt=prompt, system=system, media=media,
                    tier=tier, grounding=grounding, timeout=timeout,
                )
                rec.usage(tokens_in=out.usage.tokens_in,
                          tokens_out=out.usage.tokens_out,
                          cached_in=out.usage.cached_in)
            return out
        except (ProviderUnavailable, QuotaExceeded) as e:
            # Worth trying someone else: the request was fine, the provider was not.
            last = e
            continue
        except AIError:
            # A bad request or an empty answer will fail the same way everywhere.
            raise

    raise last or ProviderUnavailable("Every provider failed.")


def generate_text(prompt: str, **kwargs) -> str:
    """`generate(...).text`, for the many call sites that want only the string."""
    return generate(prompt, **kwargs).text
