"""
core/local — the reflex path, and the offline engines.

The reflex matcher is the only place in Alexio where a wrong answer is silently
destructive: it acts *without* the model, so a false positive is the assistant
doing something nobody asked for. Most of what follows is therefore about the
things it must refuse.
"""

from __future__ import annotations

import pytest

from core import local
from core.local import speech
from core.local.reflex import Intent, Match, ReflexRouter, normalise, percent_slot, similarity


# ── normalisation ────────────────────────────────────────────────────────────

def test_accents_and_punctuation_are_folded():
    """Transcripts spell the same French word with and without diacritics
    depending on the recogniser."""
    assert normalise("Monté  le SON !") == "monte le son"
    assert normalise("baisse la luminosité…") == "baisse la luminosite"


def test_normalising_nothing_is_safe():
    assert normalise("") == ""
    assert normalise(None) == ""


# ── similarity ───────────────────────────────────────────────────────────────

def test_identical_phrases_score_one():
    assert similarity("monte le son", "monte le son") == pytest.approx(1.0)


def test_unrelated_phrases_score_low():
    assert similarity("monte le son", "écris un script python") < 0.2


def test_word_endings_do_not_break_the_match():
    """Jaccard alone misses 'baisse' against 'baisser'; trigrams catch it."""
    assert similarity("baisse le son", "baisser le son") > 0.7


# ── matching ─────────────────────────────────────────────────────────────────

@pytest.fixture
def router() -> ReflexRouter:
    return local.build_router()


@pytest.mark.parametrize("utterance,expected", [
    ("monte le son",          "volume_up"),
    ("plus fort",             "volume_up"),
    ("turn it up",            "volume_up"),
    ("baisse le volume",      "volume_down"),
    ("turn it down",          "volume_down"),
    ("coupe le son",          "volume_mute"),
    ("baisse la luminosité",  "brightness_down"),
    ("monte la luminosité",   "brightness_up"),
    ("annule ça",             "undo"),
    ("undo that",             "undo"),
])
def test_short_commands_are_recognised_in_both_languages(router, utterance, expected):
    match = router.match(utterance)
    assert match is not None, f"{utterance!r} was not recognised"
    assert match.intent == expected


@pytest.mark.parametrize("utterance", [
    "quelle heure est-il",
    "le volume était trop fort hier soir pendant le film",
    "écris-moi un script python qui trie mes fichiers",
    "supprime tous mes fichiers",
    "éteins l'ordinateur",
    "qu'est-ce que tu vois sur mon écran",
    "",
    "   ",
])
def test_everything_else_is_left_to_the_model(router, utterance):
    """None is the correct, normal answer — it means "hand it over", not
    "failed". A matcher that reaches for anything vaguely related is how an
    assistant ends up acting on a sentence that was not a command."""
    assert router.match(utterance) is None


def test_a_sentence_merely_about_the_volume_does_not_change_it(router):
    """The false positive that actually matters, spelled out."""
    assert router.match("je trouve que le volume de ce film est bizarre") is None


# ── slots ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("utterance,expected", [
    ("mets le son à 40",        40),
    ("set the volume to 70",    70),
    ("volume à 30 pour cent",   30),
    ("mets le son à cinquante", 50),
    ("set the volume to max",  100),
])
def test_a_level_is_extracted_from_the_utterance(utterance, expected):
    assert percent_slot(utterance) == {"percent": expected}


def test_a_level_is_clamped_to_the_range():
    assert percent_slot("volume to 900") == {"percent": 100}


def test_no_number_means_no_slot():
    assert percent_slot("monte le son") == {}


@pytest.mark.parametrize("utterance,level", [
    ("mets le son à 40",      40),
    ("volume à 30 pour cent", 30),
    ("set the volume to 70",  70),
])
def test_volume_set_carries_its_level(router, utterance, level):
    match = router.match(utterance)
    assert match is not None, f"{utterance!r} was not recognised"
    assert match.intent == "volume_set"
    assert match.slots == {"percent": level}


def test_the_number_itself_does_not_decide_the_intent(router):
    """"volume à 30" and the stored example "volume à 20" are one command. Left
    unfolded, two differing digits dragged the score under the threshold."""
    from core.local.reflex import similarity
    assert similarity("volume a 30 pour cent", "volume a 20 pour cent") > 0.95


def test_folding_digits_does_not_leak_into_slot_extraction():
    """The matcher sees a placeholder; percent_slot must still see the number."""
    assert percent_slot("mets le son à 40") == {"percent": 40}


def test_a_sentence_that_merely_contains_a_number_is_not_a_volume_command(router):
    assert router.match("écris-moi un script python qui trie 30 fichiers") is None


# ── the safety rule ──────────────────────────────────────────────────────────

def test_an_irreversible_intent_cannot_be_registered():
    """core/undo.py's rule, enforced at construction: a matcher that is right
    most of the time must never hold something that cannot be taken back."""
    danger = Intent(name="wipe_disk", examples=["efface tout"], reversible=False)
    with pytest.raises(ValueError, match="not reversible"):
        ReflexRouter([danger])


def test_no_shipped_intent_is_irreversible(router):
    for intent in router.intents:
        assert intent.reversible, f"{intent.name} must not be a reflex"


def test_no_shipped_intent_touches_files_or_power(router):
    """A guard against someone adding a convenient-looking reflex later."""
    forbidden = {"delete", "remove", "trash", "shutdown", "restart",
                 "wifi", "format", "send"}
    for intent in router.intents:
        assert not (set(intent.name.split("_")) & forbidden), intent.name


# ── dispatch ─────────────────────────────────────────────────────────────────

def test_dispatch_runs_the_handler_and_returns_what_to_say():
    calls = []
    r = ReflexRouter([Intent(name="ping", examples=["ping"],
                             run=lambda: calls.append(1) or "pong")])
    result = r.dispatch("ping")
    assert result is not None
    match, spoken = result
    assert isinstance(match, Match) and spoken == "pong" and calls == [1]


def test_a_handler_that_raises_defers_to_the_model_instead_of_crashing():
    def boom():
        raise OSError("pactl is gone")

    r = ReflexRouter([Intent(name="ping", examples=["ping"], run=boom)])
    assert r.dispatch("ping") is None


def test_an_intent_without_a_handler_defers():
    r = ReflexRouter([Intent(name="ping", examples=["ping"])])
    assert r.match("ping") is not None      # recognised
    assert r.dispatch("ping") is None       # but nothing to run


def test_dispatch_passes_slots_as_keyword_arguments():
    seen = {}
    r = ReflexRouter([Intent(
        name="level", examples=["set level to 40"], slots=percent_slot,
        run=lambda percent=None: seen.update(percent=percent) or "ok",
    )])
    r.dispatch("set level to 40")
    assert seen == {"percent": 40}


# ── speed, which is the entire point ─────────────────────────────────────────

def test_a_decision_is_far_cheaper_than_a_round_trip(router):
    import time

    router.match("warmup")
    phrases = ["monte le son", "quelle heure est-il", "set the volume to 70"]
    n = 200
    start = time.perf_counter()
    for _ in range(n):
        for p in phrases:
            router.match(p)
    per_call_ms = (time.perf_counter() - start) / (n * len(phrases)) * 1000

    # Measured at ~0.16 ms on an i5-8265U. 5 ms is a ceiling generous enough for
    # a loaded CI box and still two orders of magnitude under a model call.
    assert per_call_ms < 5.0, f"{per_call_ms:.2f} ms per decision"


def test_examples_are_prepared_once_not_per_call():
    """The optimisation that made it seven times faster; a refactor that
    reintroduces per-call tokenisation should fail here."""
    intent = Intent(name="x", examples=["monte le son", "plus fort"])
    assert len(intent._prepared) == 2
    tokens, trigrams = intent._prepared[0]
    assert "monte" in tokens and any(g.strip() for g in trigrams)


# ── the offline engines ──────────────────────────────────────────────────────

def test_capability_probes_never_import_the_heavy_packages():
    """A probe must stay cheap: these are hundreds of megabytes, and the report
    is called on startup."""
    caps = local.capabilities()
    assert set(caps) == {"reflex", "stt", "tts", "vad", "wake_word"}
    for cap in caps.values():
        if not cap.available:
            assert cap.detail, f"{cap.name} says no without saying how to fix it"


def test_the_reflex_layer_is_always_available():
    """It has no dependencies, so it is the one part of the local pipeline that
    cannot be missing."""
    assert local.capabilities()["reflex"].available


def test_a_missing_engine_raises_with_the_install_command(monkeypatch):
    monkeypatch.setattr(speech, "_installed", lambda mod: False)
    stt = speech.SpeechToText()
    with pytest.raises(RuntimeError, match="requirements-local.txt"):
        stt.transcribe(b"")

    tts = speech.TextToSpeech()
    with pytest.raises(RuntimeError, match="requirements-local.txt"):
        tts.synthesize("hello")


def test_engines_are_not_loaded_at_construction(monkeypatch):
    monkeypatch.setattr(speech, "_installed", lambda mod: False)
    assert speech.SpeechToText().backend == "unloaded"
    assert speech.TextToSpeech().backend == "unloaded"


def test_the_report_reads_cleanly():
    text = local.report()
    assert "local pipeline:" in text
    for surface in ("reflex", "stt", "tts", "vad", "wake_word"):
        assert surface in text
