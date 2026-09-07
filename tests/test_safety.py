"""
The two mechanisms that stand between a misheard sentence and a ruined afternoon.

core/undo.py   — anything reversible is done at once and can be taken back.
core/confirm.py — anything irreversible waits for a button the *interface*
                  issues, never a parameter the model fills in.

The confirmation test matters most: the gate it replaced read `confirmed` off
the tool call, which the model writes. Nothing stopped it answering its own
question. If that ever regresses, this is what catches it.
"""

from __future__ import annotations

import threading
import time

from core import confirm, undo


# ── undo ─────────────────────────────────────────────────────────────────────

def test_undo_is_last_in_first_out():
    order: list[str] = []
    undo.push_undo("moved a.txt", lambda: order.append("a") or "put back")
    undo.push_undo("moved b.txt", lambda: order.append("b") or "put back")

    undo.undo_last()
    undo.undo_last()
    assert order == ["b", "a"]


def test_undo_reports_what_it_reversed():
    undo.push_undo("volume → 40%", lambda: "restored to 65%")
    out = undo.undo_last()
    assert "volume → 40%" in out and "restored to 65%" in out


def test_empty_stack_explains_itself_instead_of_failing():
    out = undo.undo_last()
    assert "nothing to undo" in out.lower()
    assert "myself" in out          # explains the scope: only its own changes


def test_stack_is_capped_and_drops_the_oldest():
    for i in range(undo.MAX_DEPTH + 5):
        undo.push_undo(f"op {i}", lambda: "ok")
    hist = undo.history()
    assert len(hist) == undo.MAX_DEPTH
    assert hist[0] == f"op {undo.MAX_DEPTH + 4}"      # newest first
    assert "op 0" not in hist


def test_a_failing_undo_is_reported_and_not_retried_forever():
    def boom() -> str:
        raise OSError("file is gone")

    undo.push_undo("moved gone.txt", boom)
    out = undo.undo_last()
    assert "Could not undo" in out and "file is gone" in out
    assert not undo.can_undo()      # popped before running, so never retried


def test_a_non_callable_registration_is_ignored():
    undo.push_undo("nonsense", None)          # type: ignore[arg-type]
    assert not undo.can_undo()


# ── confirmation ─────────────────────────────────────────────────────────────

def test_without_an_interface_nothing_irreversible_runs():
    """No HUD bound means no way to ask, which must mean no action — not a
    silent shutdown."""
    ran: list[str] = []
    out = confirm.request("shutdown", "Shut down", "the computer",
                          lambda: ran.append("boom") or "done")
    assert ran == []
    assert "cannot confirm" in out.lower()
    assert "have not done it" in out.lower()


def test_request_returns_immediately_and_does_not_run_the_action(ui):
    ran: list[str] = []
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)

    out = confirm.request("restart", "Restart", "the computer",
                          lambda: ran.append("boom") or "done")

    assert ran == []                                  # nothing happened yet
    assert "[CONFIRMATION_PENDING]" in out
    assert "Do not claim it is done" in out
    assert ui.confirms == [("Restart", "the computer")]


def test_the_action_runs_only_after_the_user_presses_confirm(ui):
    done = threading.Event()
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    confirm.request("wifi", "Toggle WiFi", "disconnects you",
                    lambda: (done.set(), "wifi off")[1])

    confirm.resolve(accepted=True)
    assert done.wait(timeout=2.0), "confirmed action never ran"


def test_cancelling_runs_nothing(ui):
    ran: list[str] = []
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    confirm.request("shutdown", "Shut down", "now",
                    lambda: ran.append("boom") or "done")

    confirm.resolve(accepted=False)
    time.sleep(0.15)
    assert ran == []
    assert any("Cancelled" in l for l in ui.logs)


def test_an_expired_confirmation_does_not_run(ui, monkeypatch):
    """A shutdown button left on screen all afternoon must not still be live."""
    ran: list[str] = []
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    confirm.request("shutdown", "Shut down", "now",
                    lambda: ran.append("boom") or "done")

    monkeypatch.setattr(confirm, "TIMEOUT_SECONDS", -1.0)
    confirm.resolve(accepted=True)
    time.sleep(0.15)
    assert ran == []
    assert any("expired" in l.lower() for l in ui.logs)


def test_resolving_nothing_is_harmless():
    confirm.resolve(accepted=True)      # no pending action; must not raise


def test_pending_title_reports_what_is_waiting(ui):
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    assert confirm.pending_title() == ""
    confirm.request("shutdown", "Shut down", "now", lambda: "done")
    assert confirm.pending_title() == "Shut down"
