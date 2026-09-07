"""
FakeUI must stay in step with the real JarvisUI.

Without this, someone adds a method to ui.JarvisUI, every test keeps passing
against a fake that no longer resembles it, and the divergence shows up months
later as an AttributeError in a running assistant.

ui.py is importable headlessly — it only builds a QApplication inside
JarvisUI.__init__, which this test never calls.
"""

from __future__ import annotations

import pytest

from tests.fake_ui import FakeUI


def _public_surface(cls) -> set[str]:
    return {
        name for name in dir(cls)
        if not name.startswith("_") and not name.startswith("mro")
    }


def test_fake_covers_the_real_interface():
    ui_mod = pytest.importorskip("ui", reason="PyQt6 not installed")

    real = _public_surface(ui_mod.JarvisUI)
    fake = _public_surface(FakeUI) | set(vars(FakeUI()))

    missing = real - fake
    assert not missing, (
        "ui.JarvisUI grew members that FakeUI does not have, so the test suite "
        f"no longer exercises the real contract: {sorted(missing)}"
    )


def test_fake_records_what_it_was_told(ui):
    ui.write_log("SYS: online")
    ui.set_state("LISTENING")
    ui.show_content("NEWS — today", "headline one")

    assert ui.last_log == "SYS: online"
    assert ui.last_state == "LISTENING"
    assert ui.contents == [("NEWS — today", "headline one")]
    assert ui.log_containing("online") == ["SYS: online"]


def test_camera_stream_state_is_observable(ui):
    assert ui.camera_open is False
    ui.start_camera_stream()
    assert ui.camera_open is True
    ui.show_camera_frame(b"\x00\x01")
    assert ui.camera_frames == 1
    ui.stop_camera_stream()
    assert ui.camera_open is False


def test_muted_is_settable_like_the_real_property(ui):
    assert ui.muted is False
    ui.muted = True
    assert ui.muted is True
