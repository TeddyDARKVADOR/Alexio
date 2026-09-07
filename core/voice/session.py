"""
core/voice/session.py — what a conversational backend must provide.

Deliberately small. Everything above this line — audio devices, the HUD, tool
dispatch, memory, the proactive engine — belongs to the assistant, not to the
transport, and none of it should have to change to swap Gemini Live for OpenAI
Realtime or for a local pipeline.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from .types import ToolResult, VoiceConfig, VoiceEvent


@runtime_checkable
class VoiceSession(Protocol):
    """One open conversation.

    Lifecycle is `connect()` → consume `events()` → `close()`. An implementation
    reconnects internally only if it can do so without losing the conversation;
    otherwise it raises SessionClosed and lets the caller decide, because only
    the caller knows whether the resumption handle is still worth replaying.
    """

    name: str

    async def connect(self, config: VoiceConfig) -> None:
        """Open the socket. Raises ResumptionRejected if the handle is stale,
        EnhancedAudioUnavailable if the server refuses the advanced features."""
        ...

    async def close(self) -> None:
        """Close cleanly. Safe to call twice."""
        ...

    async def send_audio(self, pcm16: bytes) -> None:
        """Feed one chunk of microphone audio, mono PCM16 at the configured
        input rate."""
        ...

    async def send_text(self, text: str) -> None:
        """Send a turn as text. Used for typed input, and for the internal
        prompts — briefings, proactive check-ins, system alerts."""
        ...

    async def send_media(self, data: bytes, mime_type: str, text: str = "") -> None:
        """Send an image (or other blob) as a turn, optionally with a question.

        This is how vision works: capture, then hand the bytes to the session
        that is already open. A second session for looking at things is a second
        conversation that does not know what the first one is talking about.
        """
        ...

    async def send_tool_results(self, results: list[ToolResult]) -> None:
        """Answer a TOOL_CALL event."""
        ...

    def events(self) -> AsyncIterator[VoiceEvent]:
        """Everything the server says, until the session ends."""
        ...

    @property
    def resume_handle(self) -> str | None:
        """The most recent resumable handle, or None.

        Held in memory by the caller, never written to disk: a fresh launch that
        continued yesterday's conversation would never end a session, and a
        session that never ends never produces the summary the morning briefing
        reads back.
        """
        ...
