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

import json
import sys
from pathlib import Path

from core import telemetry

from . import gemini, local
from .errors import (
    AIError,
    CapabilityUnavailable,
    EmptyResponse,
    ProviderUnavailable,
    QuotaExceeded,
)
from .types import Completion, Image, Media, Tier, Usage

__all__ = [
    "generate", "available_providers", "active_provider",
    "Completion", "Image", "Media", "Tier", "Usage",
    "AIError", "CapabilityUnavailable", "EmptyResponse",
    "ProviderUnavailable", "QuotaExceeded",
]

_PROVIDERS = {
    gemini.NAME: gemini,
    local.NAME:  local,
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


def _resolve(needs: set[str]) -> list:
    """Providers that can serve `needs`, preferred first.

    Deliberately dumb: the configured provider, then anything else that has the
    capability. Phase 03 replaces this with the registry lookup and a Budget —
    filter by capability, order by measured p95 latency, take the first that
    fits. The signature is what matters; the body is a placeholder.
    """
    preferred = active_provider()
    order     = [preferred] + [n for n in _PROVIDERS if n != preferred]
    return [_PROVIDERS[n] for n in order if needs <= _PROVIDERS[n].CAPABILITIES]


def generate(
    prompt:    str,
    *,
    system:    str | None         = None,
    media:     list[Media] | None = None,
    tier:      str                = Tier.STANDARD,
    grounding: bool               = False,
    task:      str                = "generic",
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

    candidates = _resolve(needs)
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
