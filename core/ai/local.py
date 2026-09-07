"""
core/ai/local.py — the offline adapter, for Ollama and any OpenAI-compatible server.

Grown from core/llm_client.py, which was written for a fully local build of this
assistant and then left unimported when the project moved to Gemini Live. Its
transport work — auto-starting `ollama serve`, the two payload shapes, the
error mapping — was sound and is reused here rather than rewritten.

What this adapter is honestly for on the target machine: an i5-8265U with no
GPU runs a 1.7B model at conversational speed and nothing larger. So it serves
classification, extraction and reformatting — and the privacy route, where the
right answer to a question too big for it is to say so, not to hallucinate.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import requests

from .errors import CapabilityUnavailable, EmptyResponse, ProviderUnavailable
from .types import Completion, Media, Tier, Usage

NAME = "local"

DEFAULT_URL   = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:1.7b"

# No vision, no grounding: a text-only local model. Saying so lets the facade
# fall back to a capable provider instead of sending an image into a void.
CAPABILITIES = {"text", "system_prompt"}


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


def _settings() -> tuple[str, str, str]:
    """(base_url, model, flavour) where flavour is 'ollama' or 'openai'."""
    cfg     = _config()
    url     = (cfg.get("llm_url") or DEFAULT_URL).rstrip("/")
    raw     = (cfg.get("llm_provider") or "ollama").strip().lower()
    flavour = "openai" if raw in ("openai", "lmstudio", "localai", "jan", "llamacpp") else "ollama"
    return url, (cfg.get("llm_model") or DEFAULT_MODEL), flavour


def model_for(tier: str, grounding: bool = False) -> str:
    """One local model serves every tier: on this hardware there is no second
    one worth loading, and swapping weights costs more than the tier saves."""
    overrides = _config().get("local_models") or {}
    return overrides.get(tier) or _settings()[1]


def ensure_running(timeout: int = 15) -> bool:
    """Ping the server; start `ollama serve` if it is installed but idle.

    Only Ollama is auto-started. LM Studio and friends are GUI applications the
    user launches themselves, and spawning one from an assistant would be a
    surprise, not a convenience.
    """
    url, _, flavour = _settings()

    if flavour == "openai":
        try:
            return requests.get(f"{url}/v1/models", timeout=5).status_code == 200
        except Exception:
            return False

    def _up() -> bool:
        try:
            return requests.get(f"{url}/api/tags", timeout=3).status_code == 200
        except Exception:
            return False

    if _up():
        return True

    try:
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["ollama", "serve"], **kwargs)
    except FileNotFoundError:
        return False
    except Exception:
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        if _up():
            return True
    return False


def generate(
    prompt:    str,
    system:    str | None         = None,
    media:     list[Media] | None = None,
    tier:      str                = Tier.STANDARD,
    grounding: bool               = False,
    timeout:   int                = 120,
) -> Completion:
    if media:
        raise CapabilityUnavailable("The local model cannot look at images or listen to audio.")
    if grounding:
        raise CapabilityUnavailable("The local model cannot search the web.")

    url, _, flavour = _settings()
    model = model_for(tier)

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    if flavour == "openai":
        endpoint = f"{url}/v1/chat/completions"
        payload  = {"model": model, "messages": messages, "stream": False}
    else:
        endpoint = f"{url}/api/chat"
        payload  = {
            "model": model, "messages": messages, "stream": False,
            # keep_alive -1 pins the weights in RAM. On a machine with 5 GB free
            # and zram already loaded, reloading a model per call is the
            # difference between two seconds and twenty.
            "keep_alive": -1,
            "options": {"num_predict": 600, "num_gpu": 99},
        }

    def _post():
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    try:
        data = _post()
    except requests.exceptions.ConnectionError:
        if not ensure_running():
            raise ProviderUnavailable(
                f"No local model server at {url}. Install Ollama and run: ollama serve"
            )
        try:
            data = _post()
        except Exception as e:
            raise ProviderUnavailable(f"Local model server at {url} is not answering: {e}")
    except requests.exceptions.Timeout:
        raise ProviderUnavailable(f"Local model timed out after {timeout}s.")
    except Exception as e:
        raise ProviderUnavailable(f"Local model call failed: {e}")

    if flavour == "openai":
        text = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        u    = data.get("usage") or {}
        usage = Usage(tokens_in=u.get("prompt_tokens"), tokens_out=u.get("completion_tokens"))
    else:
        text  = (data.get("message", {}).get("content") or "").strip()
        usage = Usage(tokens_in=data.get("prompt_eval_count"),
                      tokens_out=data.get("eval_count"))

    if not text:
        raise EmptyResponse(f"{model} returned no text.")

    return Completion(text=text, provider=NAME, model=model, usage=usage)
