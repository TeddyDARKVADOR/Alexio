"""
tests/conftest.py — fixtures shared by the suite.

Everything here exists to keep the tests off the real machine: no microphone,
no display, no API key, and above all no writing to the user's actual
memory/long_term.json or logs/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fake_ui import FakeUI  # noqa: E402


@pytest.fixture
def ui() -> FakeUI:
    """A recording stand-in for JarvisUI."""
    return FakeUI()


@pytest.fixture
def temp_memory(tmp_path, monkeypatch):
    """Redirect the long-term store to a throwaway file.

    memory_manager reads MEMORY_PATH at call time, not at import, so patching
    the module attribute is enough — no need to reload it.
    """
    from memory import memory_manager as mm

    store = tmp_path / "long_term.json"
    monkeypatch.setattr(mm, "MEMORY_PATH", store)
    return store


@pytest.fixture
def temp_telemetry(tmp_path, monkeypatch):
    """Redirect the JSONL call log to a throwaway directory."""
    from core import telemetry

    monkeypatch.setattr(telemetry, "LOG_DIR", tmp_path)
    monkeypatch.setattr(telemetry, "LOG_PATH", tmp_path / "ai_calls.jsonl")
    return tmp_path / "ai_calls.jsonl"


@pytest.fixture(autouse=True)
def clean_undo_stack():
    """The undo stack is module-level state shared across the process; a test
    that leaves entries behind would silently change the next one's result."""
    from core import undo

    undo.clear()
    yield
    undo.clear()


@pytest.fixture(autouse=True)
def clean_confirm_gate():
    """Same for the confirmation gate's bound callbacks and pending action."""
    from core import confirm

    confirm._pending = None
    confirm.bind(show=None, hide=None, log=None)
    yield
    confirm._pending = None
    confirm.bind(show=None, hide=None, log=None)
