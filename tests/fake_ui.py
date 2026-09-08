"""
tests/fake_ui.py — JarvisUI's contract, without Qt.

WHY
    Every action takes `player=` and calls back into the interface, and main.py
    drives the interface from a background thread. That made the whole system
    untestable: importing ui.py builds a QApplication, which needs a display, a
    theme, fonts and an event loop. So nothing was ever tested.

    This is the same surface with none of that. It records what it was told
    instead of drawing it, so a test can assert on what the assistant *said it
    was doing* — which is usually the interesting part.

CONTRACT
    Mirrors the public surface of ui.JarvisUI exactly, as used by main.py, the
    21 action modules and core/confirm.py. `test_ui_contract.py` asserts that
    this class stays in step with the real one; if someone adds a method to
    JarvisUI and not here, that test fails rather than the fake silently
    diverging until an integration bug shows up months later.
"""

from __future__ import annotations

from typing import Callable


class FakeUI:
    """Records instead of rendering. Never raises."""

    def __init__(self, muted: bool = False, current_file: str | None = None) -> None:
        # ── recorded output ──────────────────────────────────────────────
        self.logs:          list[str] = []
        self.states:        list[str] = []
        self.contents:      list[tuple[str, str]] = []
        self.confirms:      list[tuple[str, str]] = []
        self.confirm_hides: int = 0
        self.audio_levels:  list[float] = []
        self.camera_frames: int = 0
        self.camera_open:   bool = False
        self.said:          list[str] = []
        self.phone_notices: int = 0
        self.reconfig_prompts: int = 0

        # ── mutable state main.py reads ──────────────────────────────────
        self._muted        = muted
        self._current_file = current_file
        self.assistant_name = "JARVIS"

        # ── callbacks main.py assigns ────────────────────────────────────
        self.on_text_command:        Callable | None = None
        self.on_remote_clicked:      Callable | None = None
        self.on_interrupt:           Callable | None = None
        self.on_voice_change:        Callable | None = None
        self.on_audio_device_change: Callable | None = None
        self.get_plugins:            Callable | None = None
        self.request_say:            Callable | None = None

        # main.py touches ui._win._ready in the invalid-API-key path.
        self._win = _FakeWindow()
        self.root = _FakeRoot()

    # ── properties ───────────────────────────────────────────────────────

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, v: bool) -> None:
        self._muted = bool(v)

    @property
    def current_file(self) -> str | None:
        return self._current_file

    @current_file.setter
    def current_file(self, v: str | None) -> None:
        self._current_file = v

    # ── methods ──────────────────────────────────────────────────────────

    def write_log(self, text: str) -> None:
        self.logs.append(str(text))

    def set_state(self, state: str) -> None:
        self.states.append(str(state))

    def show_content(self, title: str, text: str) -> None:
        self.contents.append((str(title), str(text)))

    def show_confirm(self, title: str, detail: str) -> None:
        self.confirms.append((str(title), str(detail)))

    def hide_confirm(self) -> None:
        # Counted, not ignored: an expired confirmation must take its own
        # banner down, and a double that records only shows cannot see that
        # happen. core/confirm.py never called this on expiry at all.
        self.confirm_hides += 1

    def set_audio_level(self, level: float) -> None:
        self.audio_levels.append(float(level))

    def notify_phone_connected(self) -> None:
        self.phone_notices += 1

    def prompt_reconfig(self) -> None:
        self.reconfig_prompts += 1
        self._win._ready = True      # unblock main.py's wait loop

    def show_camera_frame(self, img_bytes: bytes) -> None:
        self.camera_frames += 1

    def start_camera_stream(self) -> None:
        self.camera_open = True

    def stop_camera_stream(self) -> None:
        self.camera_open = False

    def wait_for_api_key(self) -> None:
        pass

    def start_speaking(self) -> None:
        self.states.append("SPEAKING")

    def stop_speaking(self) -> None:
        self.states.append("LISTENING")

    # ── convenience for assertions ───────────────────────────────────────

    def log_containing(self, needle: str) -> list[str]:
        return [l for l in self.logs if needle in l]

    @property
    def last_log(self) -> str:
        return self.logs[-1] if self.logs else ""

    @property
    def last_state(self) -> str:
        return self.states[-1] if self.states else ""


class _FakeWindow:
    def __init__(self) -> None:
        self._ready = True
        self._muted = False
        self._assistant_name = "JARVIS"


class _FakeRoot:
    def mainloop(self) -> None:
        pass
