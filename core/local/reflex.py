"""
core/local/reflex.py — answer the short commands without a model.

THE PROBLEM IT SOLVES
    "Monte le son" currently travels: microphone → Gemini Live socket → the
    model decides to call a tool → tool call comes back → computer_settings
    runs → tool response goes up → the model generates an acknowledgement →
    audio comes back down. Best case that is most of a second; on a bad network
    it is several. For an action that is one subprocess call.

    A fixed set of phrases does not need a language model to be recognised. It
    needs a nearest-neighbour lookup. Measured on this machine — an i5-8265U,
    no GPU — over eight intents and seventy-eight example phrases: **0.27 ms
    per decision**. That is the whole idea, and it is the only place in Alexio
    where three orders of magnitude are available without giving anything up.

WHY NO EMBEDDINGS HERE
    The plan called for FastEmbed with multilingual-e5-small. That is the right
    long-term answer and there is a seam for it below — but it means ~330 MB of
    ONNX runtime and model weights on a machine with 5 GB of RAM free, and a
    download before the first word works. This matcher is pure Python, needs
    nothing, and is measurably good enough for a closed set of about thirty
    phrases per intent. Swap it when the local pipeline is already pulling
    onnxruntime in for speech recognition anyway.

WHAT MAY AND MAY NOT BE A REFLEX
    Only actions that are instant, reversible and unambiguous. Volume,
    brightness, play/pause, "undo". Never a deletion, never a power command,
    never anything that leaves the machine. The rule is core/undo.py's rule: if
    it cannot be taken back, a confident mishearing is unacceptable, and a
    matcher that is right 97% of the time is a mishearing generator at scale.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

# Below this, hand the utterance to the model. Tuned so that a phrase which is
# merely *related* to an intent ("the volume was too loud yesterday") does not
# trigger it: the cost of a false positive is the assistant doing something
# nobody asked for, the cost of a false negative is one ordinary round trip.
THRESHOLD = 0.68

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Fold case, strip accents and punctuation, squeeze whitespace.

    Accent folding matters more than it looks: speech transcripts spell the same
    French word with and without diacritics depending on the recogniser, and
    "monte le son" must match "monté le son".
    """
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


_DIGITS = re.compile(r"\d+")


def _fold_digits(text: str) -> str:
    """Collapse every number to one placeholder, for matching only.

    "volume a 30 pour cent" and the stored example "son a 20 pour cent" are the
    same command; the number is a slot, not something that distinguishes one
    intent from another. Left in, the two differing digits dragged the score
    under the threshold and a perfectly clear instruction fell through to the
    model.

    Applied on the matching path only — percent_slot() reads the original text,
    or there would be no number left to extract.
    """
    return _DIGITS.sub("#", text)


def _tokens(text: str) -> set[str]:
    return set(_fold_digits(normalise(text)).split())


def _trigrams(text: str) -> set[str]:
    s = " " + _fold_digits(normalise(text)) + " "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _prepare(text: str) -> tuple[set[str], set[str]]:
    """Everything the comparison needs, computed once."""
    return _tokens(text), _trigrams(text)


# Two tokens count as the same word when one is a prefix of the other and they
# agree on at least this many characters. French conjugation is the reason:
# "baisse", "baisser" and "baissez" are one command, and exact-set Jaccard
# scores that pair at 0.5 — under the threshold, so a perfectly ordinary way of
# saying it would silently fall through to the model.
_STEM_MIN = 4


def _soft_overlap(ta: set[str], tb: set[str]) -> float:
    """Jaccard, but tolerant of word endings."""
    matched_a: set[str] = set()
    matched_b: set[str] = set()
    for x in ta:
        for y in tb:
            if x == y or (
                len(x) >= _STEM_MIN and len(y) >= _STEM_MIN
                and (x.startswith(y[:_STEM_MIN]) and y.startswith(x[:_STEM_MIN]))
            ):
                matched_a.add(x)
                matched_b.add(y)
    shared = (len(matched_a) + len(matched_b)) / 2
    union  = len(ta) + len(tb) - shared
    return shared / union if union else 0.0


def _compare(a: tuple[set[str], set[str]], b: tuple[set[str], set[str]]) -> float:
    ta, ga = a
    tb, gb = b
    if not ta or not tb:
        return 0.0
    overlap = _soft_overlap(ta, tb)
    dice    = 2 * len(ga & gb) / (len(ga) + len(gb)) if (ga and gb) else 0.0
    return 0.6 * overlap + 0.4 * dice


def similarity(a: str, b: str) -> float:
    """0.0-1.0. Token overlap for meaning, character trigrams for typos.

    Jaccard alone misses "baisse" against "baisser"; trigrams alone rate
    "monte le son" and "monte le ton" nearly equal. Together they disagree in
    the right places.
    """
    return _compare(_prepare(a), _prepare(b))


@dataclass
class Intent:
    """One thing that can be recognised without a model."""

    name:     str
    examples: list[str]
    run:      Callable[..., str] | None = None
    # Extracted from the utterance and passed to `run` as keyword arguments.
    slots:    Callable[[str], dict] | None = None
    # Stated, not inferred. An intent that cannot be undone must never be a
    # reflex, and this field is what a test can assert on.
    reversible: bool = True

    # Tokens and trigrams for every example, computed once at construction.
    # Recomputing them per call made the router seven times slower than it
    # needed to be — the examples never change, only the utterance does.
    _prepared: list = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._prepared = [_prepare(ex) for ex in self.examples]

    def score(self, utterance: str) -> float:
        return self.score_prepared(_prepare(utterance))

    def score_prepared(self, prepared) -> float:
        return max((_compare(prepared, ex) for ex in self._prepared), default=0.0)


@dataclass(frozen=True)
class Match:
    intent: str
    score:  float
    slots:  dict = field(default_factory=dict)


class ReflexRouter:
    """A nearest-neighbour lookup over a closed set of phrases."""

    def __init__(self, intents: list[Intent] | None = None,
                 threshold: float = THRESHOLD) -> None:
        self._intents: list[Intent] = []
        self.threshold = threshold
        for intent in (intents or []):
            self.register(intent)

    def register(self, intent: Intent) -> None:
        if not intent.reversible:
            raise ValueError(
                f"'{intent.name}' is not reversible, so it cannot be a reflex. "
                "Irreversible actions go through the model and core/confirm.py."
            )
        self._intents.append(intent)

    @property
    def intents(self) -> list[Intent]:
        return list(self._intents)

    def match(self, utterance: str) -> Match | None:
        """The best intent above the threshold, or None.

        None is the normal, correct answer for anything that is not one of the
        handful of phrases this knows. It means "give it to the model", not
        "failed".
        """
        if not (utterance or "").strip():
            return None

        # Prepare the utterance once, then compare it against every example.
        prepared = _prepare(utterance)
        best: Intent | None = None
        best_score = 0.0
        for intent in self._intents:
            score = intent.score_prepared(prepared)
            if score > best_score:
                best, best_score = intent, score

        if best is None or best_score < self.threshold:
            return None

        slots = {}
        if best.slots:
            try:
                slots = best.slots(utterance) or {}
            except Exception:
                slots = {}
        return Match(intent=best.name, score=round(best_score, 4), slots=slots)

    def dispatch(self, utterance: str) -> tuple[Match, str] | None:
        """Match and run. Returns (match, result) or None when nothing matched.

        A handler that raises is reported as a miss rather than as a crash: the
        model is a perfectly good fallback, and a broken reflex should degrade
        into the slow path, not into an error.
        """
        found = self.match(utterance)
        if found is None:
            return None
        intent = next((i for i in self._intents if i.name == found.intent), None)
        if intent is None or intent.run is None:
            return None
        try:
            return found, intent.run(**found.slots)
        except Exception as e:
            print(f"[Reflex] '{found.intent}' failed, deferring to the model: {e}")
            return None


# ── slot extraction ──────────────────────────────────────────────────────────

_NUMBER = re.compile(r"\b(\d{1,3})\b")

# Spoken numbers that actually turn up in volume commands, in the languages this
# assistant is used in. Not a full number parser: "set volume to seventy-three"
# is rare enough to be worth one model round trip.
_WORD_NUMBERS = {
    "zero": 0, "zéro": 0,
    "ten": 10, "dix": 10, "diez": 10, "zehn": 10,
    "twenty": 20, "vingt": 20, "veinte": 20,
    "thirty": 30, "trente": 30, "treinta": 30,
    "forty": 40, "quarante": 40, "cuarenta": 40,
    "fifty": 50, "cinquante": 50, "cincuenta": 50, "half": 50, "moitie": 50,
    "sixty": 60, "soixante": 60, "sesenta": 60,
    "seventy": 70, "soixante dix": 70, "setenta": 70,
    "eighty": 80, "quatre vingt": 80, "ochenta": 80,
    "ninety": 90, "quatre vingt dix": 90, "noventa": 90,
    "hundred": 100, "cent": 100, "cien": 100, "max": 100, "maximum": 100,
}


# "pour cent" and "percent" are the unit, not a number. Left in, they made
# "volume a 30 pour cent" resolve to 100, because `cent` is a word-number and
# the word pass ran first.
_UNIT_WORDS = re.compile(r"\b(pour ?cent|percent|per ?cent|pourcent)\b")


def percent_slot(utterance: str) -> dict:
    """Pull a 0-100 level out of "mets le son à 40" / "set volume to 40%"."""
    text = _UNIT_WORDS.sub(" ", normalise(utterance))

    # Digits first: they are unambiguous, and a spoken number is the fallback.
    m = _NUMBER.search(text)
    if m:
        return {"percent": max(0, min(100, int(m.group(1))))}

    # Longest first, so "quatre vingt dix" wins over "quatre vingt".
    for word in sorted(_WORD_NUMBERS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(word)}\b", text):
            return {"percent": _WORD_NUMBERS[word]}
    return {}


# ── the encoder seam ─────────────────────────────────────────────────────────

def use_encoder(encode: Callable[[list[str]], list]) -> None:
    """Replace the lexical matcher with real embeddings.

    Left as a hook rather than a dependency. When the local speech pipeline
    installs onnxruntime anyway, wiring FastEmbed's multilingual-e5-small in
    here costs nothing extra and buys paraphrase tolerance — "j'entends rien"
    would then match "monte le son", which no amount of trigram overlap will
    ever do.
    """
    raise NotImplementedError(
        "Embedding-backed reflex matching is not wired yet. The lexical matcher "
        "handles the closed phrase set; see this module's docstring for when to "
        "swap it."
    )
