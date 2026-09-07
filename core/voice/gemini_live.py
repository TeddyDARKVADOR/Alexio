"""
core/voice/gemini_live.py — Gemini Live behind the VoiceSession protocol.

Extracted from JarvisLive, which had the transport, the audio devices, the tool
dispatch, the memory, the monitors and the dashboard in one class. Only the
transport is here.

WHAT THIS FILE KNOWS THAT NOBODY ELSE SHOULD
  · the v1alpha / v1beta split that gates affective dialog and proactive audio
  · that a resumption update is only safe to keep while `resumable` is true
  · that the server sends `go_away` with the time left before it hangs up
  · that a tool call arrives as `response.tool_call.function_calls`
  · that audio arrives on `response.data` as raw PCM16 at 24 kHz
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

from .types import (
    EnhancedAudioUnavailable,
    EventKind,
    ResumptionRejected,
    SessionClosed,
    ToolCall,
    ToolResult,
    VoiceConfig,
    VoiceEvent,
)

MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"

# Gemini occasionally emits these control markers inside a transcript. They are
# not speech and must never reach the log or the session summary.
_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)


def _clean(text: str) -> str:
    text = _CTRL_RE.sub("", text or "")
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", text).strip()


def _looks_like_rejected_handle(err: str) -> bool:
    low = err.lower()
    return ("resum" in low or "handle" in low
            or "INVALID_ARGUMENT" in err or "NOT_FOUND" in err)


def _looks_like_enhanced_rejection(err: str) -> bool:
    low = err.lower()
    return ("affective" in low or "proactiv" in low
            or "Unknown name" in err or "unexpected keyword" in err
            or "INVALID_ARGUMENT" in err)


class GeminiLiveSession:
    """A VoiceSession backed by the Gemini Live API."""

    name = "gemini-live"

    def __init__(self, api_key: str, model: str = MODEL) -> None:
        self._api_key = api_key
        self._model   = model
        self._config: VoiceConfig | None = None
        self._session = None
        self._ctx     = None
        self._handle: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def connect(self, config: VoiceConfig) -> None:
        from google import genai
        from google.genai import types as gtypes

        self._config = config
        self._handle = config.resume_handle

        cfg: dict = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=config.system_instruction,
            tools=[{"function_declarations": config.tools}] if config.tools else None,
            session_resumption=gtypes.SessionResumptionConfig(handle=config.resume_handle),
            # Without this the session dies when the context window fills. With
            # it, one conversation can run for hours.
            context_window_compression=gtypes.ContextWindowCompressionConfig(
                sliding_window=gtypes.SlidingWindow(),
            ),
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(
                        voice_name=config.voice
                    )
                )
            ),
        )
        if cfg["tools"] is None:
            del cfg["tools"]

        if config.enhanced:
            cfg["enable_affective_dialog"] = True
            cfg["proactivity"] = gtypes.ProactivityConfig(proactive_audio=True)

        client = genai.Client(
            api_key=self._api_key,
            http_options={"api_version": "v1alpha" if config.enhanced else "v1beta"},
        )

        try:
            self._ctx = client.aio.live.connect(
                model=self._model, config=gtypes.LiveConnectConfig(**cfg)
            )
            self._session = await self._ctx.__aenter__()
        except Exception as e:
            self._session = None
            self._ctx     = None
            err = str(e)
            # Order matters: a stale handle and a rejected feature both surface
            # as INVALID_ARGUMENT, and replaying a dead handle forever is the
            # worse failure, so it is checked first.
            if config.resume_handle and _looks_like_rejected_handle(err):
                raise ResumptionRejected(err) from e
            if config.enhanced and _looks_like_enhanced_rejection(err):
                raise EnhancedAudioUnavailable(err) from e
            raise SessionClosed(err) from e

    async def close(self) -> None:
        ctx, self._ctx, self._session = self._ctx, None, None
        if ctx is None:
            return
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:
            pass          # already gone; closing twice must never raise

    # ── sending ──────────────────────────────────────────────────────────

    def _live(self):
        if self._session is None:
            raise SessionClosed("No open Gemini Live session.")
        return self._session

    async def send_audio(self, pcm16: bytes) -> None:
        await self._live().send_realtime_input(
            media={"data": pcm16, "mime_type": "audio/pcm"}
        )

    async def send_text(self, text: str) -> None:
        await self._live().send_client_content(
            turns={"parts": [{"text": text}]}, turn_complete=True
        )

    async def send_media(self, data: bytes, mime_type: str, text: str = "") -> None:
        import base64
        parts: list[dict] = [{
            "inline_data": {
                "mime_type": mime_type,
                "data": base64.b64encode(data).decode("ascii"),
            }
        }]
        if text:
            parts.append({"text": text})
        await self._live().send_client_content(turns={"parts": parts}, turn_complete=True)

    async def send_tool_results(self, results: list[ToolResult]) -> None:
        from google.genai import types as gtypes
        await self._live().send_tool_response(
            function_responses=[
                gtypes.FunctionResponse(id=r.id, name=r.name,
                                        response={"result": r.result})
                for r in results
            ]
        )

    # ── receiving ────────────────────────────────────────────────────────

    async def events(self) -> AsyncIterator[VoiceEvent]:
        """Translate the SDK's response objects into VoiceEvents.

        Audio is sliced into ~50 ms pieces so an interrupt stops playback within
        50 ms instead of after whatever the server happened to send in one go.
        """
        session = self._live()
        slice_bytes = int(self._config.output_sample_rate * 2 * 0.05) if self._config else 2400

        try:
            async for response in session.receive():

                update = getattr(response, "session_resumption_update", None)
                if update is not None:
                    # `resumable` goes false while a turn is in flight; replaying
                    # a handle captured then is exactly what the flag prevents.
                    if getattr(update, "resumable", False) and getattr(update, "new_handle", None):
                        self._handle = update.new_handle
                        yield VoiceEvent(kind=EventKind.RESUMPTION, handle=self._handle)

                go_away = getattr(response, "go_away", None)
                if go_away is not None:
                    # The server announces the hang-up before it happens. Never
                    # read before this refactor, so every scheduled disconnect
                    # arrived as an error and cost a silent gap in the middle of
                    # a sentence; now it is enough warning to rebuild cleanly.
                    left = getattr(go_away, "time_left", None)
                    yield VoiceEvent(kind=EventKind.GO_AWAY,
                                     seconds_left=_seconds(left))

                if response.data:
                    data = response.data
                    for i in range(0, len(data), slice_bytes):
                        yield VoiceEvent(kind=EventKind.AUDIO,
                                         audio=data[i:i + slice_bytes])

                sc = response.server_content
                if sc:
                    if getattr(sc, "interrupted", False):
                        yield VoiceEvent(kind=EventKind.INTERRUPTED)

                    out = getattr(sc, "output_transcription", None)
                    if out and out.text:
                        text = _clean(out.text)
                        if text:
                            yield VoiceEvent(kind=EventKind.TRANSCRIPT_OUT, text=text)

                    inp = getattr(sc, "input_transcription", None)
                    if inp and inp.text:
                        text = _clean(inp.text)
                        if text:
                            yield VoiceEvent(kind=EventKind.TRANSCRIPT_IN, text=text)

                    if sc.turn_complete:
                        yield VoiceEvent(kind=EventKind.TURN_COMPLETE)

                if response.tool_call:
                    calls = [
                        ToolCall(id=fc.id, name=fc.name, args=dict(fc.args or {}))
                        for fc in response.tool_call.function_calls
                    ]
                    if calls:
                        yield VoiceEvent(kind=EventKind.TOOL_CALL, calls=calls)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise SessionClosed(str(e)) from e

    @property
    def resume_handle(self) -> str | None:
        return self._handle


def _seconds(value) -> float | None:
    """`time_left` arrives as a protobuf Duration on some SDK versions and as a
    plain number on others."""
    if value is None:
        return None
    for attr in ("total_seconds", "seconds"):
        got = getattr(value, attr, None)
        if callable(got):
            try:
                return float(got())
            except Exception:
                pass
        elif got is not None:
            try:
                return float(got)
            except Exception:
                pass
    try:
        return float(value)
    except Exception:
        return None
