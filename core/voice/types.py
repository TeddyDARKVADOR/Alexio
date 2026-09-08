"""
core/voice/types.py — the vocabulary of the conversational plane.

Plane A is not plane B with audio bolted on. It is a socket held open for the
whole session, carrying audio in both directions, with state living on the
server and a handle that lets a dropped connection pick the conversation back
up. Nothing here mentions a vendor, so a second implementation — OpenAI
Realtime, or a local VAD/STT/LLM/TTS pipeline — is a file, not a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── configuration ────────────────────────────────────────────────────────────

@dataclass
class VoiceConfig:
    """Everything fixed at connect time.

    Most of these cannot be changed on a live session — the voice in particular
    is baked in when the socket opens, which is why picking a new one rebuilds
    the session rather than sending a message.
    """
    system_instruction: str
    tools:              list[dict] = field(default_factory=list)
    voice:              str        = "Charon"
    resume_handle:      str | None = None

    # Affective dialog and proactive audio. Available on the 2.5 native-audio
    # generation and NOT on Gemini 3.1 Flash Live, so an implementation that
    # cannot honour them must say so rather than pretend.
    enhanced:           bool = True

    input_sample_rate:  int = 16_000
    output_sample_rate: int = 24_000


# ── events ───────────────────────────────────────────────────────────────────

class EventKind:
    AUDIO          = "audio"           # .audio  — PCM16 to play
    TRANSCRIPT_IN  = "transcript_in"   # .text   — what the user said
    TRANSCRIPT_OUT = "transcript_out"  # .text   — what the assistant said
    TURN_COMPLETE  = "turn_complete"   # the model finished generating
    TOOL_CALL      = "tool_call"       # .calls  — list of ToolCall
    RESUMPTION     = "resumption"      # .handle — replay this to resume
    GO_AWAY        = "go_away"         # .seconds_left — the socket is closing
    INTERRUPTED    = "interrupted"     # the server dropped the pending response


@dataclass(frozen=True)
class ToolCall:
    """One function call the model wants performed."""
    id:   str
    name: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """The answer to a ToolCall, on its way back."""
    id:     str
    name:   str
    result: Any


@dataclass(frozen=True)
class VoiceEvent:
    """One thing that happened on the session.

    A single flat type rather than a class per kind: the consumer is one
    `async for` with a dispatch on `.kind`, and a hierarchy would buy nothing
    but imports.
    """
    kind:         str
    audio:        bytes | None      = None
    text:         str  | None       = None
    calls:        list[ToolCall]    = field(default_factory=list)
    handle:       str  | None       = None
    seconds_left: float | None      = None

    def __repr__(self) -> str:                       # keeps logs readable
        if self.kind == EventKind.AUDIO:
            return f"VoiceEvent(audio, {len(self.audio or b'')}B)"
        if self.text is not None:
            return f"VoiceEvent({self.kind}, {self.text[:40]!r})"
        if self.calls:
            return f"VoiceEvent(tool_call, {[c.name for c in self.calls]})"
        return f"VoiceEvent({self.kind})"


# ── errors ───────────────────────────────────────────────────────────────────

class VoiceError(RuntimeError):
    """Base class for conversational-plane failures."""


class SessionClosed(VoiceError):
    """The socket is gone. The caller should rebuild, replaying the handle.

    Carries *why* it went, because "the connection dropped" is three different
    events that need three different answers and look identical from outside:

      · the server ended the session normally (code 1000/1001) — the ordinary
        end of a long conversation, or the connection-duration limit arriving
        without a go-away. Reconnect at once; nothing is wrong.
      · we gave up (code 1011, "keepalive ping timeout") — *our* client closed
        because no pong came back within 20 s. That is the network stalling or
        this process starving its own event loop, and it is worth saying so:
        the two have very different fixes and neither is a Gemini fault.
      · the connection vanished (1006, no close frame) — a real break.

    `by` is "server" or "client", and it is the field that matters most: it is
    the difference between "they hung up" and "we hung up", which a message
    reading only "connection dropped" hides. Without it main.py was reduced to
    matching substrings on a transport exception it should never have seen.
    """

    def __init__(self, message: str, *, code: int | None = None,
                 reason: str = "", by: str = "") -> None:
        super().__init__(message)
        self.code   = code
        self.reason = reason
        self.by     = by

    @property
    def is_keepalive_timeout(self) -> bool:
        """We closed it ourselves because a keepalive pong never arrived."""
        return self.by == "client" and "keepalive" in self.reason.lower()

    def describe(self) -> str:
        """One line, in the terms someone reading a log can act on."""
        if self.is_keepalive_timeout:
            return ("no reply to a keepalive ping for 20s — the network stalled "
                    "or this process was too busy to answer")
        if self.by == "server" and self.code in (1000, 1001):
            return f"the server ended the session (code {self.code})"
        if self.code == 1006:
            return "the connection vanished without a close frame (code 1006)"
        if self.code is not None:
            who = self.by or "someone"
            return (f"closed by {who} with code {self.code}"
                    + (f" — {self.reason}" if self.reason else ""))
        return str(self)


class ResumptionRejected(VoiceError):
    """The server refused the resumption handle — expired, or belonging to a
    session it has since dropped.

    Its own class because the recovery is specific and easy to get wrong: drop
    the handle and connect clean. Retrying with the same handle turns the
    feature meant to survive a reconnect into the thing preventing one.
    """


class EnhancedAudioUnavailable(VoiceError):
    """The server rejected affective dialog / proactive audio. Reconnect without
    them rather than failing the session."""
