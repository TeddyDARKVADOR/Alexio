"""
core/voice — the conversational plane (plane A).

A held-open socket carrying audio both ways, with state on the server and a
handle that survives a dropped connection. Plane B — one-shot requests — lives
in core/ai and is a different protocol on purpose; see that module's docstring.

    from core import voice

    session = voice.GeminiLiveSession(api_key)
    await session.connect(voice.VoiceConfig(system_instruction=..., tools=...))
    async for ev in session.events():
        if ev.kind == voice.EventKind.AUDIO:
            play(ev.audio)

Adding OpenAI Realtime, or a local VAD/STT/LLM/TTS pipeline, means writing one
class with the same six methods. Anthropic cannot serve this plane at all: there
is no speech-to-speech API, only a pipeline around a text model.
"""

from .gemini_live import MODEL as GEMINI_LIVE_MODEL, GeminiLiveSession
from .session import VoiceSession
from .types import (
    EnhancedAudioUnavailable,
    EventKind,
    ResumptionRejected,
    SessionClosed,
    ToolCall,
    ToolResult,
    VoiceConfig,
    VoiceError,
    VoiceEvent,
)

__all__ = [
    "GeminiLiveSession", "GEMINI_LIVE_MODEL", "VoiceSession",
    "VoiceConfig", "VoiceEvent", "EventKind", "ToolCall", "ToolResult",
    "VoiceError", "SessionClosed", "ResumptionRejected", "EnhancedAudioUnavailable",
]
