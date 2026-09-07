"""
core/local/intents.py — the phrases Alexio answers without asking a model.

Every intent here is instant, reversible and unambiguous. That is not a style
preference: a lexical matcher is right most of the time, not all of the time,
and the cost of being wrong has to be one keystroke of annoyance rather than a
deleted folder. Anything irreversible goes through the model and through
core/confirm.py, which is what those exist for.

The examples are written in French and English because those are the languages
this assistant is spoken to in. Adding a language means adding phrases — no
retraining, no model, no download.
"""

from __future__ import annotations

from .reflex import Intent, ReflexRouter, percent_slot


def _settings():
    """Imported late: actions/computer_settings.py pulls in pyautogui, and the
    reflex router must be importable in a test or on a headless box."""
    from actions import computer_settings as cs
    return cs


# ── handlers ─────────────────────────────────────────────────────────────────
# Each returns the short sentence the assistant will speak back, so the reply is
# immediate too — the round trip saved is on the acknowledgement as much as on
# the action.

def _volume_up() -> str:
    _settings().volume_up()
    return "Volume up."


def _volume_down() -> str:
    _settings().volume_down()
    return "Volume down."


def _volume_mute() -> str:
    _settings().volume_mute()
    return "Muted."


def _volume_set(percent: int | None = None) -> str:
    if percent is None:
        raise ValueError("no level in the utterance")
    cs = _settings()
    before = cs.volume_get()
    cs.volume_set(percent)
    if before is not None:
        from core.undo import push_undo
        push_undo(f"volume → {percent}%", lambda: (cs.volume_set(before),
                                                   f"restored to {before}%")[1])
    return f"Volume at {percent} percent."


def _brightness_up() -> str:
    _settings().brightness_up()
    return "Brighter."


def _brightness_down() -> str:
    _settings().brightness_down()
    return "Dimmer."


def _play_pause() -> str:
    _settings().pause_video()
    return "Done."


def _undo_last() -> str:
    from core import undo
    return undo.undo_last()


# ── the phrase book ──────────────────────────────────────────────────────────

INTENTS: list[Intent] = [
    Intent(
        name="volume_up",
        run=_volume_up,
        examples=[
            "monte le son", "monte le volume", "plus fort", "augmente le son",
            "monte", "un peu plus fort", "augmente le volume",
            "volume up", "louder", "turn it up", "turn the volume up",
            "increase the volume", "a bit louder",
        ],
    ),
    Intent(
        name="volume_down",
        run=_volume_down,
        examples=[
            "baisse le son", "baisse le volume", "moins fort", "diminue le son",
            "baisse", "un peu moins fort", "reduis le volume",
            "volume down", "quieter", "turn it down", "turn the volume down",
            "lower the volume", "a bit quieter",
        ],
    ),
    Intent(
        name="volume_mute",
        run=_volume_mute,
        examples=[
            "coupe le son", "mute", "silence", "coupe le volume",
            "mets en sourdine", "plus de son",
            "mute the sound", "silence it", "turn the sound off",
        ],
    ),
    Intent(
        name="volume_set",
        run=_volume_set,
        slots=percent_slot,
        examples=[
            "mets le son a 40", "mets le volume a 50", "volume a 30",
            "regle le son sur 60", "son a 20 pour cent",
            "set the volume to 40", "volume to 50", "set volume 30 percent",
            "put the volume at 60",
        ],
    ),
    Intent(
        name="brightness_up",
        run=_brightness_up,
        examples=[
            "monte la luminosite", "plus lumineux", "augmente la luminosite",
            "ecran plus clair", "eclaircis l ecran",
            "brightness up", "brighter", "increase the brightness",
            "make the screen brighter",
        ],
    ),
    Intent(
        name="brightness_down",
        run=_brightness_down,
        examples=[
            "baisse la luminosite", "moins lumineux", "diminue la luminosite",
            "ecran plus sombre", "assombris l ecran",
            "brightness down", "dimmer", "decrease the brightness",
            "make the screen darker",
        ],
    ),
    Intent(
        name="play_pause",
        run=_play_pause,
        examples=[
            "pause", "mets en pause", "reprends", "lecture", "play",
            "resume", "pause the video", "play the video",
        ],
    ),
    Intent(
        name="undo",
        run=_undo_last,
        examples=[
            "annule", "annule ca", "reviens en arriere", "undo", "undo that",
            "take it back", "revert that", "non pas ca",
        ],
    ),
]


def build_router() -> ReflexRouter:
    """The router Alexio uses. Built fresh so a test can build its own."""
    return ReflexRouter(INTENTS)
