"""
core/local — what Alexio can do without the network.

Three things, in descending order of how much they are ready today:

  · **reflex**  — short commands answered without any model at all. Pure Python,
                  no dependencies, 0.16 ms per decision. Works now.
  · **speech**  — Parakeet for recognition, Piper for speech. Real engines, real
                  choices for this hardware, but opt-in:
                  `pip install -r requirements-local.txt`.
  · **wake**    — Silero VAD and openWakeWord, so the microphone stops being
                  open for the whole session.

    from core import local

    print(local.report())               # what is installed and what is missing

    router = local.build_router()
    hit = router.dispatch("monte le son")
    if hit:
        match, spoken = hit             # handled locally, the model never saw it
"""

from __future__ import annotations

from core.desktop.caps import Capability

from .intents import INTENTS, build_router
from .reflex import Intent, Match, ReflexRouter, normalise, percent_slot, similarity
from .speech import (
    SpeechToText,
    TextToSpeech,
    stt_capability,
    tts_capability,
    vad_capability,
    wake_capability,
)

__all__ = [
    "build_router", "ReflexRouter", "Intent", "Match", "INTENTS",
    "similarity", "normalise", "percent_slot",
    "SpeechToText", "TextToSpeech",
    "capabilities", "report",
]


def reflex_capability() -> Capability:
    router = build_router()
    phrases = sum(len(i.examples) for i in router.intents)
    return Capability("reflex", True, "lexical",
                      f"{len(router.intents)} intents, {phrases} phrases, "
                      f"no dependencies")


def capabilities() -> dict[str, Capability]:
    return {
        "reflex":    reflex_capability(),
        "stt":       stt_capability(),
        "tts":       tts_capability(),
        "vad":       vad_capability(),
        "wake_word": wake_capability(),
    }


def report() -> str:
    """What the offline path can do on this machine right now.

    `python -c "from core import local; print(local.report())"`
    """
    lines = ["local pipeline:", ""]
    for cap in capabilities().values():
        lines.append("  " + str(cap))
    return "\n".join(lines)
