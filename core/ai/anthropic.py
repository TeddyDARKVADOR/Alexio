"""
core/ai/anthropic.py — Claude for the request/response plane.

PLANE B ONLY, AND NOT BY CHOICE
    Anthropic has no speech-to-speech API. Claude's voice mode is a pipeline —
    speech to text, a text model, then an external text-to-speech provider — so
    Claude can serve reasoning, code and vision here, and can never hold the
    conversational session in core/voice. The registry encodes that; this
    docstring says it out loud so nobody spends a day trying.

NO SDK
    Plain HTTP over `requests`, which is already a dependency. The Messages API
    is one POST and a documented JSON body; pulling in a package to build that
    dictionary would add an install to every machine for no benefit, and this
    file is the only place that would use it.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import requests

from .errors import EmptyResponse, ProviderUnavailable, QuotaExceeded
from .types import Completion, Media, Tier, Usage

NAME = "anthropic"

API_URL = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"

DEFAULT_MODELS = {
    Tier.FAST:     "claude-haiku-4-5-20251001",
    Tier.STANDARD: "claude-sonnet-5",
    Tier.DEEP:     "claude-opus-5",
}

# No web grounding and no audio: Claude reads text and images.
CAPABILITIES = {"text", "vision", "system_prompt"}

MAX_TOKENS = 4096


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
    key = (_config().get("anthropic_api_key") or "").strip()
    if not key:
        raise ProviderUnavailable(
            'No Anthropic key. Add "anthropic_api_key" to config/api_keys.json.'
        )
    return key


def has_credentials() -> bool:
    return bool((_config().get("anthropic_api_key") or "").strip())


def model_for(tier: str, grounding: bool = False) -> str:
    overrides = _config().get("anthropic_models") or {}
    return overrides.get(tier) or DEFAULT_MODELS.get(tier, DEFAULT_MODELS[Tier.STANDARD])


def _classify(status: int, body: str) -> Exception:
    if status == 429 or "rate_limit" in body or "overloaded" in body:
        return QuotaExceeded(body[:300])
    if status in (401, 403):
        return ProviderUnavailable(f"Anthropic rejected the key: {body[:200]}")
    if status >= 500:
        return ProviderUnavailable(f"Anthropic {status}: {body[:200]}")
    return ProviderUnavailable(f"Anthropic {status}: {body[:300]}")


def generate(
    prompt:    str,
    system:    str | None         = None,
    media:     list[Media] | None = None,
    tier:      str                = Tier.STANDARD,
    grounding: bool               = False,
    timeout:   int                = 60,
) -> Completion:
    model = model_for(tier)

    # Images come first in the content list: Anthropic's own guidance is that a
    # question placed after the image it refers to is answered more reliably.
    content: list[dict] = []
    for item in (media or []):
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": item.mime_type,
                "data": base64.b64encode(item.data).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": prompt})

    payload: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": content}],
    }
    if system:
        payload["system"] = system

    try:
        resp = requests.post(
            API_URL, json=payload, timeout=timeout,
            headers={
                "x-api-key": _api_key(),
                "anthropic-version": VERSION,
                "content-type": "application/json",
            },
        )
    except requests.exceptions.Timeout:
        raise ProviderUnavailable(f"Anthropic timed out after {timeout}s.")
    except requests.exceptions.RequestException as e:
        raise ProviderUnavailable(f"Cannot reach Anthropic: {e}")

    if resp.status_code != 200:
        raise _classify(resp.status_code, resp.text)

    data = resp.json()
    text = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()

    if not text:
        raise EmptyResponse(f"{model} returned no text.")

    u = data.get("usage") or {}
    return Completion(
        text=text, provider=NAME, model=data.get("model", model),
        usage=Usage(
            tokens_in  = u.get("input_tokens"),
            tokens_out = u.get("output_tokens"),
            cached_in  = u.get("cache_read_input_tokens"),
        ),
    )
