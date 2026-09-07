"""
core/telemetry — one row per model call, and it must never break the call.

The second property is the important one. Telemetry sits in the hot path of
every request; if a full disk or an unserialisable value can raise out of it,
it has turned an observation into an outage.
"""

from __future__ import annotations

import json

import pytest

from core import telemetry


def _rows(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_a_successful_call_writes_one_row(temp_telemetry):
    with telemetry.record(plane="B", provider="gemini",
                          model="gemini-3.8-flash", task="summarize") as rec:
        rec.usage(tokens_in=800, tokens_out=90, cached_in=740, cost_est=0.0004)

    rows = _rows(temp_telemetry)
    assert len(rows) == 1
    row = rows[0]
    assert row["ok"] is True
    assert row["provider"] == "gemini"
    assert row["model"] == "gemini-3.8-flash"
    assert row["task"] == "summarize"
    assert row["tokens_in"] == 800 and row["cached_in"] == 740
    assert row["latency_ms"] >= 0
    assert "error" not in row


def test_a_failing_call_is_recorded_and_the_exception_still_propagates(temp_telemetry):
    with pytest.raises(RuntimeError, match="upstream is down"):
        with telemetry.record(plane="B", provider="anthropic",
                              model="claude-sonnet-5", task="plan"):
            raise RuntimeError("upstream is down")

    row = _rows(temp_telemetry)[0]
    assert row["ok"] is False
    assert row["error"] == "RuntimeError"
    assert "upstream is down" in row["error_message"]


def test_extra_fields_survive(temp_telemetry):
    with telemetry.record(plane="A", provider="gemini", model="live",
                          task="converse", budget="conversation") as rec:
        rec.note(route="reflex")

    row = _rows(temp_telemetry)[0]
    assert row["extra"]["budget"] == "conversation"
    assert row["extra"]["route"] == "reflex"


def test_an_unwritable_log_never_breaks_the_call(temp_telemetry, monkeypatch):
    """A read-only directory must cost the observation, not the request."""
    def explode(*a, **kw):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(telemetry, "write", lambda row: explode())

    with telemetry.record(plane="B", provider="x", model="y", task="z"):
        pass                    # must not raise


def test_unserialisable_values_do_not_raise(temp_telemetry):
    with telemetry.record(plane="B", provider="x", model="y", task="z") as rec:
        rec.note(weird=object())        # default=str in json.dumps handles it

    assert len(_rows(temp_telemetry)) == 1


def test_summarise_needs_enough_samples_before_it_speaks(temp_telemetry):
    for _ in range(3):
        with telemetry.record(plane="B", provider="p", model="m", task="t"):
            pass
    assert telemetry.summarise(min_samples=5) == {}

    for _ in range(4):
        with telemetry.record(plane="B", provider="p", model="m", task="t"):
            pass
    stats = telemetry.summarise(min_samples=5)
    assert stats[("p", "m")]["calls"] == 7
    assert stats[("p", "m")]["ok_rate"] == 1.0


def test_summarise_reports_the_failure_rate(temp_telemetry):
    for i in range(10):
        try:
            with telemetry.record(plane="B", provider="p", model="m", task="t"):
                if i % 2 == 0:
                    raise ValueError("nope")
        except ValueError:
            pass

    stats = telemetry.summarise(min_samples=5)[("p", "m")]
    assert stats["calls"] == 10
    assert stats["ok_rate"] == 0.5


def test_a_torn_line_does_not_blind_the_router(temp_telemetry):
    """A crash mid-write leaves a partial last line; it must be skipped, not
    fatal — the router needs the rest of the history."""
    with telemetry.record(plane="B", provider="p", model="m", task="t"):
        pass
    with open(temp_telemetry, "a", encoding="utf-8") as f:
        f.write('{"ts": "2026-09-07T10:00:00', )      # truncated, no newline

    assert len(telemetry.read_rows()) == 1


def test_percentiles_are_ordered(temp_telemetry):
    vals = [float(i) for i in range(100)]
    assert telemetry._percentile(vals, 0.5) <= telemetry._percentile(vals, 0.95)
    assert telemetry._percentile([], 0.5) == 0.0
