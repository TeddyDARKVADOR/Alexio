"""
Why a Live session ended — told apart, not lumped together.

THE BUG THIS EXISTS FOR
    JARVIS disconnected repeatedly mid-session and the log said, every time,
    "Connection dropped — reconnecting." That one line covers three unrelated
    events:

        · the server ended the session normally (1000/1001)
        · this client gave up because no keepalive pong came back (1011)
        · the socket vanished with no close frame at all (1006)

    They have three different causes and three different fixes, and the message
    named none of them. Worse, the classification was done in main.py by
    matching substrings on a raw `websockets` exception — a transport detail
    that should never have left core/voice in the first place.

WHAT IS ASSERTED
    That core/voice translates a real ConnectionClosed into a SessionClosed
    carrying the close code and *who sent it*, and that `describe()` says
    something a person can act on. The exceptions here are built by the
    websockets library itself, from real Close frames, so the attribute names
    are checked against the library rather than assumed.
"""

from __future__ import annotations

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from core import voice
from core.voice.gemini_live import _as_session_closed


def _server_closed(code: int, reason: str = "") -> ConnectionClosedOK:
    """The peer sent the Close frame and we echoed it back.

    `rcvd_then_sent=True` is the library's own way of recording who went first,
    and it asserts on it when both frames are present — which is a useful
    confirmation that "who hung up" is a real property of the close and not an
    inference from the message text.
    """
    return ConnectionClosedOK(Close(code, reason), Close(code, reason),
                              rcvd_then_sent=True)


def _we_closed(code: int, reason: str) -> ConnectionClosedError:
    """We sent the Close frame and got nothing back — exactly the shape of
    "sent 1011 (internal error) keepalive ping timeout; no close frame
    received", which is what JARVIS actually reported."""
    return ConnectionClosedError(None, Close(code, reason))


def test_our_own_keepalive_timeout_is_named_as_ours():
    closed = _as_session_closed(_we_closed(1011, "keepalive ping timeout"))

    assert isinstance(closed, voice.SessionClosed)
    assert closed.code == 1011
    assert closed.by == "client"
    assert closed.is_keepalive_timeout
    # The point of the message: it must not read like a Gemini fault.
    assert "network" in closed.describe() and "busy" in closed.describe()


def test_a_normal_server_close_is_not_reported_as_a_failure():
    closed = _as_session_closed(_server_closed(1000, ""))

    assert closed.code == 1000
    assert closed.by == "server"
    assert not closed.is_keepalive_timeout
    assert "server ended the session" in closed.describe()


def test_a_vanished_socket_says_so():
    """1006 is never sent on the wire — the library synthesises it when the
    connection breaks with no close frame in either direction."""
    closed = _as_session_closed(ConnectionClosedError(None, None))

    assert closed.code == 1006
    assert "vanished" in closed.describe()


def test_the_close_is_found_through_a_taskgroup_and_a_cause():
    """How it actually arrives: the microphone task raises inside a TaskGroup,
    which wraps it in an ExceptionGroup whose own str() names nothing."""
    inner = _we_closed(1011, "keepalive ping timeout")
    wrapped = RuntimeError("send failed")
    wrapped.__cause__ = inner
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [wrapped])

    closed = _as_session_closed(group)
    assert closed.code == 1011
    assert closed.by == "client"


def test_main_finds_the_typed_error_inside_the_group():
    """main.py must reach the SessionClosed through the ExceptionGroup a
    TaskGroup raises — the whole reason the old string match failed."""
    import main

    closed = voice.SessionClosed("gone", code=1000, by="server")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [closed])

    found = main._closed_in(group)
    assert found is closed
    assert main._closed_in(RuntimeError("something else")) is None


@pytest.mark.asyncio
async def test_the_loop_lag_watchdog_notices_a_blocked_loop():
    """The instrument that decides which of the two keepalive causes it was.

    A coroutine that calls a blocking function is the failure R-07 forbids;
    this asserts the watchdog would have seen it. Without the measurement, a
    "keepalive ping timeout" is a coin flip between the network and ourselves.
    """
    import asyncio
    import time as _time

    import main

    lag = main._LoopLag()
    task = asyncio.create_task(lag.run())
    await asyncio.sleep(0.1)
    assert lag.worst < 0.2, "an idle loop must measure near zero"

    _time.sleep(0.6)          # exactly what run_in_executor exists to prevent
    await asyncio.sleep(0.05)
    task.cancel()

    # It under-reports by up to one period, and that is inherent: a block that
    # starts just after a tick is already partly "spent" against the next
    # deadline. 0.6s of blocking therefore reads as at least 0.6 − 0.25. The
    # bound matters because the number is compared against a 20 s keepalive
    # timeout, where a quarter-second of slack changes nothing.
    floor = 0.6 - main._LoopLag.PERIOD
    assert lag.worst >= floor, f"blocked 0.6s, watchdog saw only {lag.worst:.3f}s"


@pytest.mark.parametrize("code,by,expect", [
    (1000, "server", "server ended"),
    (1001, "server", "server ended"),
    (1006, "network", "vanished"),
    (1007, "server", "code 1007"),
])
def test_every_close_describes_itself(code, by, expect):
    assert expect in voice.SessionClosed("x", code=code, by=by).describe()
