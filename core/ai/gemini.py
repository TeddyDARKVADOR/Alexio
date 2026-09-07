"""
core/ai/gemini.py — the Gemini adapter for the request/response plane.

This is plane B only: one round trip, no state. The conversational session that
main.py holds open is plane A and is a different protocol entirely; it is not
routed through here (see the module docstring of core/ai/__init__.py).

Everything Gemini-specific lives behind this file's edge: the SDK import, the
`Part.from_bytes` shape for images, the `{"tools": [{"google_search": {}}]}`
incantation for grounding, and the fact that a grounded answer arrives split
across `candidates[0].content.parts` instead of on `response.text`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .errors import EmptyResponse, ProviderUnavailable, QuotaExceeded
from .types import Completion, Media, Tier, Usage

NAME = "gemini"

# Aliases, not pinned ids. Google moves `-latest` forward as new Flash versions
# ship, which is what a system with no model registry wants: it keeps working.
# Phase 03 replaces this dict with the registry, which pins exact ids so the
# router can attribute measured latency to a version that cannot change
# underneath it.
DEFAULT_MODELS = {
    Tier.FAST:     "gemini-flash-lite-latest",
    Tier.STANDARD: "gemini-flash-latest",
    Tier.DEEP:     "gemini-flash-latest",
}

# Grounding needs a model that carries the google_search tool; the lite tier
# does not reliably, so a grounded FAST request is promoted rather than failed.
GROUNDING_MIN_TIER = Tier.STANDARD

CAPABILITIES = {"text", "vision", "audio", "video", "grounding", "system_prompt"}


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


def _api_key() -> str:
    key = (_config().get("gemini_api_key") or "").strip()
    if not key:
        raise ProviderUnavailable(
            "No Gemini API key in config/api_keys.json. "
            "Add one, or set \"ai_provider\": \"local\" to use Ollama."
        )
    return key


def model_for(tier: str, grounding: bool = False) -> str:
    """Which model id this tier resolves to right now."""
    if grounding and tier == Tier.FAST:
        tier = GROUNDING_MIN_TIER
    overrides = _config().get("ai_models") or {}
    return overrides.get(tier) or DEFAULT_MODELS.get(tier, DEFAULT_MODELS[Tier.STANDARD])


def _classify(exc: Exception) -> Exception:
    """Turn an SDK exception into one of ours, so callers can branch on meaning
    instead of grepping a message."""
    text = str(exc).lower()
    if any(k in text for k in ("quota", "rate limit", "429", "resource_exhausted")):
        return QuotaExceeded(str(exc)[:300])
    if any(k in text for k in ("api key", "permission", "401", "403",
                               "unauthenticated", "getaddrinfo", "connection")):
        return ProviderUnavailable(str(exc)[:300])
    return exc


def _text_of(response) -> str:
    """Pull the text out, whichever shape it arrived in.

    `response.text` is empty on a grounded answer, where the content is split
    across parts. Reading only `.text` is why grounded searches used to come
    back blank and get reported as "no results".
    """
    direct = getattr(response, "text", None)
    if direct:
        return direct.strip()

    out = ""
    try:
        for part in response.candidates[0].content.parts:
            piece = getattr(part, "text", None)
            if piece:
                out += piece
    except Exception:
        pass
    return out.strip()


def _usage_of(response) -> Usage:
    """Token counts, when the SDK reports them. Never fatal if it does not —
    a missing count costs the router a data point, not the answer."""
    try:
        um = getattr(response, "usage_metadata", None)
        if um is None:
            return Usage()
        return Usage(
            tokens_in  = getattr(um, "prompt_token_count", None),
            tokens_out = getattr(um, "candidates_token_count", None),
            cached_in  = getattr(um, "cached_content_token_count", None),
        )
    except Exception:
        return Usage()


def generate(
    prompt:    str,
    system:    str | None      = None,
    media:     list[Media] | None = None,
    tier:      str             = Tier.STANDARD,
    grounding: bool            = False,
    timeout:   int             = 60,
) -> Completion:
    """One request, one answer. Raises an errors.AIError subclass on failure."""
    from google import genai
    from google.genai import types as gtypes

    model  = model_for(tier, grounding)
    client = genai.Client(api_key=_api_key())

    contents: list = []
    for item in (media or []):
        contents.append(gtypes.Part.from_bytes(data=item.data, mime_type=item.mime_type))
    contents.append(prompt)

    cfg: dict = {}
    if system:
        cfg["system_instruction"] = system
    if grounding:
        cfg["tools"] = [{"google_search": {}}]

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            **({"config": cfg} if cfg else {}),
        )
    except Exception as e:
        raise _classify(e) from e

    text = _text_of(response)
    if not text:
        raise EmptyResponse(f"{model} returned no text.")

    return Completion(text=text, provider=NAME, model=model, usage=_usage_of(response))
