"""
core/local/speech.py — offline speech in and out, when the machine can afford it.

WHAT WAS CHOSEN AND WHY
    The engines here are picked for *this* hardware — an i5-8265U with no GPU
    and about 5 GB of RAM free — not for what benchmarks like best on a 4090.

    STT: **Parakeet TDT 0.6B v3** (NVIDIA, May 2026). ONNX int8, around 30×
         real time on a laptop CPU, 6.34% average WER, and 25 European
         languages including French with automatic detection. `faster-whisper`
         is the fallback, not the default: it manages roughly 3× real time on
         CPU, which is the difference between transcribing while you speak and
         transcribing after you stop.

    TTS: **Piper** (OHF-Voice, v1.6.0, July 2026). First audio in about 40 ms,
         real-time factor around 0.03, French voices `siwis`, `tom`, `upmc`.
         Kokoro sounds better and needs 3.6 s before the first sample and 2 GB
         of peak memory — on this laptop that is a conversation that stutters.
         `edge-tts` is the online fallback for machines that would rather not
         download a voice.

NOTHING IS INSTALLED BY THIS FILE
    Each engine is imported inside the function that uses it, and `available()`
    reports what is missing with the exact command that fixes it. The models are
    hundreds of megabytes; downloading them because someone imported a module
    would be rude. `pip install -r requirements-local.txt` is opt-in.

    That is also what keeps this importable in CI and on a machine that will
    never run the local pipeline at all.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass

from core.desktop.caps import Capability

# Voice defaults. Piper names are `<lang>_<REGION>-<voice>-<quality>`; `siwis`
# is the clearest French voice at medium quality, which is also the smallest
# one worth using.
DEFAULT_PIPER_VOICE = "fr_FR-siwis-medium"
DEFAULT_PARAKEET    = "nvidia/parakeet-tdt-0.6b-v3"


def _installed(module: str) -> bool:
    """True if importable, without importing it — these packages are heavy and
    a capability probe must stay cheap."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# ── speech to text ───────────────────────────────────────────────────────────

def stt_capability() -> Capability:
    if _installed("onnx_asr"):
        return Capability("stt", True, "parakeet-tdt-0.6b-v3",
                          "ONNX int8, ~30x real time on CPU, 25 languages")
    if _installed("faster_whisper"):
        return Capability("stt", True, "faster-whisper",
                          "~3x real time on CPU — Parakeet is roughly ten times "
                          "faster: pip install onnx-asr[cpu]")
    if _installed("vosk"):
        return Capability("stt", True, "vosk", "streaming, lowest accuracy")
    return Capability("stt", False, "none",
                      "pip install -r requirements-local.txt "
                      "(onnx-asr[cpu] for Parakeet)")


class SpeechToText:
    """Lazily loaded transcriber. Construction is cheap; the model loads on the
    first `transcribe`, because that is the call that can afford to wait."""

    def __init__(self, model: str = DEFAULT_PARAKEET, language: str | None = None):
        self.model_name = model
        self.language   = None if (language or "").lower() in ("", "auto") else language
        self._engine    = None
        self._backend   = ""

    def _load(self) -> None:
        if self._engine is not None:
            return

        if _installed("onnx_asr"):
            import onnx_asr
            print(f"[STT] Loading {self.model_name} (ONNX, CPU int8)…")
            self._engine  = onnx_asr.load_model(self.model_name)
            self._backend = "parakeet"
            return

        if _installed("faster_whisper"):
            from faster_whisper import WhisperModel
            print("[STT] Parakeet not installed — falling back to faster-whisper.")
            # int8 on CPU: float16 needs CUDA, and this machine has none.
            self._engine  = WhisperModel("base", device="cpu", compute_type="int8")
            self._backend = "whisper"
            return

        raise RuntimeError(stt_capability().detail)

    def transcribe(self, audio) -> str:
        """`audio` is float32 mono at 16 kHz, or a path to a wav file."""
        self._load()

        if self._backend == "parakeet":
            return (self._engine.recognize(audio) or "").strip()

        segments, _info = self._engine.transcribe(
            audio,
            language=self.language,
            beam_size=1,                       # greedy: 2-3x faster, same words
            condition_on_previous_text=False,  # stops it inventing continuations
            vad_filter=True,
        )
        return " ".join(s.text for s in segments).strip()

    @property
    def backend(self) -> str:
        return self._backend or "unloaded"


# ── text to speech ───────────────────────────────────────────────────────────

def tts_capability() -> Capability:
    if _installed("piper") or shutil.which("piper"):
        return Capability("tts", True, "piper",
                          "~40 ms to first audio, RTF ~0.03, French voices")
    if _installed("edge_tts"):
        return Capability("tts", True, "edge-tts", "free but needs the network")
    if _installed("kokoro"):
        return Capability("tts", True, "kokoro",
                          "3.6 s to first audio and 2 GB peak — too heavy for "
                          "this machine; prefer piper")
    return Capability("tts", False, "none",
                      "pip install -r requirements-local.txt (piper-tts)")


@dataclass
class Utterance:
    """Synthesised audio, ready for sounddevice."""
    samples:     object            # numpy float32 mono
    sample_rate: int


class TextToSpeech:
    """Lazily loaded synthesiser, same contract as SpeechToText."""

    def __init__(self, voice: str = DEFAULT_PIPER_VOICE, speed: float = 1.0):
        self.voice   = voice
        self.speed   = speed
        self._engine = None
        self._backend = ""

    def _load(self) -> None:
        if self._engine is not None:
            return
        if _installed("piper"):
            from piper import PiperVoice
            print(f"[TTS] Loading Piper voice {self.voice}…")
            self._engine  = PiperVoice.load(self.voice)
            self._backend = "piper"
            return
        raise RuntimeError(tts_capability().detail)

    def synthesize(self, text: str) -> Utterance:
        import numpy as np

        self._load()
        chunks = []
        rate   = 22_050
        for chunk in self._engine.synthesize(text):
            rate = getattr(chunk, "sample_rate", rate)
            raw  = getattr(chunk, "audio_int16_bytes", None)
            if raw:
                chunks.append(np.frombuffer(raw, dtype=np.int16))
        if not chunks:
            raise RuntimeError("Piper produced no audio.")
        samples = np.concatenate(chunks).astype(np.float32) / 32768.0
        return Utterance(samples=samples, sample_rate=rate)

    @property
    def backend(self) -> str:
        return self._backend or "unloaded"


# ── voice activity and wake word ─────────────────────────────────────────────

def wake_capability() -> Capability:
    if _installed("openwakeword"):
        return Capability("wake_word", True, "openWakeWord",
                          "CPU-only, bundles Silero VAD")
    return Capability("wake_word", False, "none",
                      "pip install -r requirements-local.txt (openwakeword). "
                      "Until then the microphone is open the whole session and "
                      "every sound reaches the network.")


def vad_capability() -> Capability:
    if _installed("silero_vad") or _installed("openwakeword"):
        return Capability("vad", True, "silero-vad")
    return Capability("vad", False, "none",
                      "pip install -r requirements-local.txt (silero-vad)")
