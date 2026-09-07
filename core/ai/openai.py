"""
core/ai/openai.py — GPT for the request/response plane.

Plain HTTP, same reasoning as core/ai/anthropic.py: the Chat Completions body is
a documented dictionary and `requests` is already here.

Note the module is named for the vendor but never imports a package called
`openai` — which matters, because `core/ai/local.py` speaks the same wire format
to LM Studio, LocalAI and llama.cpp. The difference between the two files is the
host and the key, not the protocol.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import requests

from .errors import EmptyResponse, ProviderUnavailable, QuotaExceeded
from .types import Completion, Media, Tier, Usage

NAME = "openai"

API_URL = "https://api.openai.com/v1/chat/completions"

DEFAULT_MODELS = {
    Tier.FAST:     "gpt-5.6-luna",
    Tier.STANDARD: "gpt-5.6-terra",
    Tier.DEEP:     "gpt-5.6-sol",
}

CAPABILITIES = {"text", "vision", "system_prompt"}


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
    key = (_config().get("openai_api_key") or "").strip()
    if not key:
        raise ProviderUnavailable(
            'No OpenAI key. Add "openai_api_key" to config/api_keys.json.'
        )
    return key


def has_credentials() -> bool:
    return bool((_config().get("openai_api_key") or "").strip())


def model_for(tier: str, grounding: bool = False) -> str:
    overrides = _config().get("openai_models") or {}
    return overrides.get(tier) or DEFAULT_MODELS.get(tier, DEFAULT_MODELS[Tier.STANDARD])


def _classify(status: int, body: str) -> Exception:
    if status == 429 or "rate_limit" in body or "insufficient_quota" in body:
        return QuotaExceeded(body[:300])
    if status in (401, 403):
        return ProviderUnavailable(f"OpenAI rejected the key: {body[:200]}")
    return ProviderUnavailable(f"OpenAI {status}: {body[:300]}")


def generate(
    prompt:    str,
    system:    str | None         = None,
    media:     list[Media] | None = None,
    tier:      str                = Tier.STANDARD,
    grounding: bool               = False,
    timeout:   int                = 60,
) -> Completion:
    model = model_for(tier)

    if media:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for item in media:
            b64 = base64.b64encode(item.data).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{item.mime_type};base64,{b64}"},
            })
        user_content: object = content
    else:
        user_content = prompt

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_content})

    try:
        resp = requests.post(
            API_URL, json={"model": model, "messages": messages}, timeout=timeout,
            headers={
                "Authorization": f"Bearer {_api_key()}",
                "Content-Type": "application/json",
            },
        )
    except requests.exceptions.Timeout:
        raise ProviderUnavailable(f"OpenAI timed out after {timeout}s.")
    except requests.exceptions.RequestException as e:
        raise ProviderUnavailable(f"Cannot reach OpenAI: {e}")

    if resp.status_code != 200:
        raise _classify(resp.status_code, resp.text)

    data   = resp.json()
    choice = (data.get("choices") or [{}])[0]
    text   = (choice.get("message", {}).get("content") or "").strip()

    if not text:
        raise EmptyResponse(f"{model} returned no text.")

    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    return Completion(
        text=text, provider=NAME, model=data.get("model", model),
        usage=Usage(
            tokens_in  = u.get("prompt_tokens"),
            tokens_out = u.get("completion_tokens"),
            cached_in  = cached,
        ),
    )
