"""
core/voice — the conversational plane behind a protocol.

The SDK is never touched here. What is tested is the translation layer: that
Gemini's response objects become VoiceEvents with the right shape, that the
three failure modes are told apart, and above all that `go_away` is read —
because it was not, and every scheduled disconnect therefore arrived as an
error in the middle of a sentence.
"""

from __future__ import annotations

import asyncio
import types as pytypes

import pytest

from core import voice
from core.voice import gemini_live
from core.voice.types import EventKind, ToolCall, VoiceConfig, VoiceEvent


# ── fake SDK responses ───────────────────────────────────────────────────────

def _ns(**kw):
    base = {"data": None, "server_content": None, "tool_call": None}
    base.update(kw)
    return pytypes.SimpleNamespace(**base)


def _content(**kw):
    base = {"output_transcription": None, "input_transcription": None,
            "turn_complete": False, "interrupted": False, "model_turn": None}
    base.update(kw)
    return pytypes.SimpleNamespace(**base)


def _audio(*chunks: bytes, extra_parts=()):
    """A model turn shaped the way the SDK delivers one.

    Audio does not arrive on a `data` attribute — that is a convenience property
    the SDK computes from `server_content.model_turn.parts`, and it warns
    whenever the turn also carries text or thoughts. This builds the real thing,
    so the extraction is tested against the shape it actually meets.
    """
    parts = [pytypes.SimpleNamespace(
        inline_data=pytypes.SimpleNamespace(data=c, mime_type="audio/pcm"))
        for c in chunks]
    parts += [pytypes.SimpleNamespace(inline_data=None, **p) for p in extra_parts]
    return pytypes.SimpleNamespace(parts=parts)


def _text(t):
    return pytypes.SimpleNamespace(text=t)


class _FakeLive:
    """Stands in for the object `client.aio.live.connect()` yields."""

    def __init__(self, responses):
        self._responses = responses
        self.sent_audio: list[bytes] = []
        self.sent_turns: list = []
        self.sent_tools: list = []

    async def receive(self):
        for r in self._responses:
            yield r

    async def send_realtime_input(self, media):
        self.sent_audio.append(media["data"])

    async def send_client_content(self, turns, turn_complete=True):
        self.sent_turns.append(turns)

    async def send_tool_response(self, function_responses):
        self.sent_tools.append(function_responses)


def _session_with(responses, output_rate=24_000):
    s = voice.GeminiLiveSession(api_key="test")
    s._session = _FakeLive(responses)
    s._config  = VoiceConfig(system_instruction="x", output_sample_rate=output_rate)
    return s


async def _drain(session) -> list[VoiceEvent]:
    return [ev async for ev in session.events()]


# ── event translation ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transcripts_become_typed_events():
    s = _session_with([
        _ns(server_content=_content(input_transcription=_text("quelle heure est-il"))),
        _ns(server_content=_content(output_transcription=_text("il est midi"))),
        _ns(server_content=_content(turn_complete=True)),
    ])
    kinds = [e.kind for e in await _drain(s)]
    assert kinds == [EventKind.TRANSCRIPT_IN, EventKind.TRANSCRIPT_OUT,
                     EventKind.TURN_COMPLETE]


@pytest.mark.asyncio
async def test_control_markers_are_stripped_from_transcripts():
    """Gemini emits <ctrl94> style markers inside transcripts. They are not
    speech and must never reach the log or the session summary."""
    s = _session_with([_ns(server_content=_content(output_transcription=_text("<ctrl94>bonjour")))])
    events = await _drain(s)
    assert events[0].text == "bonjour"


@pytest.mark.asyncio
async def test_a_transcript_that_is_only_noise_yields_nothing():
    s = _session_with([_ns(server_content=_content(output_transcription=_text("<ctrl0>")))])
    assert await _drain(s) == []


@pytest.mark.asyncio
async def test_audio_is_sliced_so_an_interrupt_lands_within_50ms():
    """One second of 24 kHz PCM16 is 48 000 bytes; at 50 ms a slice that is
    twenty events, so a drain of the queue stops playback almost immediately
    instead of after whatever the server happened to send in one go."""
    s = _session_with([_ns(server_content=_content(model_turn=_audio(b"\x00" * 48_000)))])
    events = await _drain(s)
    assert len(events) == 20
    assert all(e.kind == EventKind.AUDIO for e in events)
    assert sum(len(e.audio) for e in events) == 48_000


@pytest.mark.asyncio
async def test_audio_is_read_from_the_parts_even_when_the_turn_also_thinks():
    """A native-audio model sends `text` and `thought` parts alongside the PCM.
    The SDK's `response.data` handles that by logging a warning nobody can act
    on, once per process, in the middle of a conversation. The audio must come
    out whole and the non-audio parts must be dropped without a word."""
    turn = _audio(b"\x01" * 2_400, b"\x02" * 2_400,
                  extra_parts=({"text": "hmm"}, {"thought": True}))
    s = _session_with([_ns(server_content=_content(model_turn=turn))])
    events = await _drain(s)
    assert [e.kind for e in events] == [EventKind.AUDIO, EventKind.AUDIO]
    assert b"".join(e.audio for e in events) == b"\x01" * 2_400 + b"\x02" * 2_400


@pytest.mark.asyncio
async def test_a_turn_with_no_audio_parts_yields_no_audio():
    turn = _audio(extra_parts=({"text": "just words"},))
    s = _session_with([_ns(server_content=_content(model_turn=turn))])
    assert await _drain(s) == []


@pytest.mark.asyncio
async def test_tool_calls_arrive_as_a_single_event():
    call = pytypes.SimpleNamespace(id="c1", name="open_app", args={"app_name": "Chrome"})
    s = _session_with([_ns(tool_call=pytypes.SimpleNamespace(function_calls=[call]))])
    events = await _drain(s)
    assert len(events) == 1
    assert events[0].kind == EventKind.TOOL_CALL
    assert events[0].calls == [ToolCall(id="c1", name="open_app",
                                        args={"app_name": "Chrome"})]


@pytest.mark.asyncio
async def test_an_interruption_is_reported():
    s = _session_with([_ns(server_content=_content(interrupted=True))])
    assert [e.kind for e in await _drain(s)] == [EventKind.INTERRUPTED]


# ── session resumption ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_only_resumable_handles_are_kept():
    """`resumable` goes false while a turn is in flight. Replaying a handle
    captured then is exactly what the flag exists to prevent."""
    good = pytypes.SimpleNamespace(resumable=True,  new_handle="keep-me")
    bad  = pytypes.SimpleNamespace(resumable=False, new_handle="mid-turn")

    s = _session_with([_ns(session_resumption_update=bad),
                       _ns(session_resumption_update=good)])
    events = await _drain(s)

    assert [e.kind for e in events] == [EventKind.RESUMPTION]
    assert events[0].handle == "keep-me"
    assert s.resume_handle == "keep-me"


# ── go_away: the regression this phase exists for ────────────────────────────

@pytest.mark.asyncio
async def test_go_away_is_read():
    """The server announces the hang-up before it happens. This was never read,
    so every scheduled disconnect surfaced as an error mid-sentence."""
    away = pytypes.SimpleNamespace(time_left=pytypes.SimpleNamespace(seconds=12))
    s = _session_with([_ns(go_away=away)])
    events = await _drain(s)

    assert len(events) == 1
    assert events[0].kind == EventKind.GO_AWAY
    assert events[0].seconds_left == 12.0


@pytest.mark.asyncio
async def test_go_away_survives_every_shape_of_time_left():
    """`time_left` is a protobuf Duration on some SDK versions and a plain
    number on others; an unknown shape must still produce the event.

    The string cases are the ones that matter, and they are the ones this test
    did not have. `LiveServerGoAway.time_left` is typed `Optional[str]` in the
    installed SDK, and over the websocket transport the server sends a protobuf
    Duration — `"540s"`, trailing `s` included. The old parser tried
    `total_seconds`, then `seconds`, then `float(value)`; a string has neither
    attribute and `float("540s")` raises, so the real shape was the only one
    that produced `None`. Every go-away warning reached the user with the
    countdown missing — the single number in it worth reading.
    """
    for value, expected in [
        ("540s", 540.0),                                  # what the server sends
        ("10.5s", 10.5),
        ("  42s ", 42.0),
        ("600", 600.0),                                   # bare, seen on some builds
        ("soon", None),                                   # unparseable stays None
        (pytypes.SimpleNamespace(seconds=30), 30.0),
        (pytypes.SimpleNamespace(total_seconds=lambda: 8.5), 8.5),
        (5, 5.0),
        (None, None),
        (object(), None),
    ]:
        s = _session_with([_ns(go_away=pytypes.SimpleNamespace(time_left=value))])
        events = await _drain(s)
        assert events[0].kind == EventKind.GO_AWAY
        assert events[0].seconds_left == expected


# ── failure classification ───────────────────────────────────────────────────

def test_a_stale_handle_is_told_apart_from_a_rejected_feature():
    """Both surface as INVALID_ARGUMENT. Replaying a dead handle forever is the
    worse failure — the feature meant to survive a reconnect becomes the thing
    preventing one — so it is checked first."""
    assert gemini_live._looks_like_rejected_handle("resumption handle NOT_FOUND")
    assert gemini_live._looks_like_enhanced_rejection("affective dialog unsupported")


@pytest.mark.asyncio
async def test_a_dead_socket_raises_session_closed():
    class _Broken:
        async def receive(self):
            raise ConnectionResetError("socket gone")
            yield  # pragma: no cover

    s = voice.GeminiLiveSession(api_key="test")
    s._session = _Broken()
    s._config  = VoiceConfig(system_instruction="x")

    with pytest.raises(voice.SessionClosed):
        await _drain(s)


@pytest.mark.asyncio
async def test_sending_without_a_session_raises_rather_than_dropping():
    s = voice.GeminiLiveSession(api_key="test")
    with pytest.raises(voice.SessionClosed):
        await s.send_text("hello")


@pytest.mark.asyncio
async def test_closing_twice_is_safe():
    s = voice.GeminiLiveSession(api_key="test")
    await s.close()
    await s.close()


# ── sending ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_media_goes_into_the_open_conversation():
    """Vision is a turn in the session that already exists — not a second one."""
    s = _session_with([])
    await s.send_media(b"\x89PNG", "image/png", "what is this?")

    turn = s._session.sent_turns[0]
    parts = turn["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png"
    assert parts[1]["text"] == "what is this?"


@pytest.mark.asyncio
async def test_audio_is_forwarded_as_raw_pcm():
    s = _session_with([])
    await s.send_audio(b"\x01\x02")
    assert s._session.sent_audio == [b"\x01\x02"]


# ── the protocol holds ───────────────────────────────────────────────────────

def test_gemini_live_satisfies_the_voice_session_protocol():
    s = voice.GeminiLiveSession(api_key="test")
    assert isinstance(s, voice.VoiceSession)


def test_config_carries_no_vendor_types():
    """VoiceConfig must stay plain data: the moment an SDK object appears in it,
    every caller depends on that SDK again."""
    cfg = VoiceConfig(system_instruction="x", tools=[{"name": "t"}], voice="Puck")
    for value in vars(cfg).values():
        assert type(value).__module__ in ("builtins", "core.voice.types"), value
