"""
core/desktop — one way to touch the machine, three implementations.

WHY
    Every action module reimplemented `if _OS == "Windows": … elif "Darwin": …
    else: …`, and each got a slightly different set of things wrong. On this
    machine the Linux branch of that pattern was mostly broken: `mss` returns a
    black frame under Wayland, `brightnessctl` was not installed and the GNOME
    interface it replaced no longer exists, `pygetwindow` raises on import.

    One facade, three backends, and — this is the part that matters — a
    capability report, so the answer to "can it take a screenshot?" is a
    mechanism and a reason rather than a shrug.

USE
    from core import desktop

    data, mime = desktop.screenshot()
    desktop.brightness_set(60)
    desktop.notify("Reminder", "Stand up")
    print(desktop.report())          # the capability matrix for this machine

EVERY BACKEND IMPORTS ON EVERY PLATFORM
    windows.py and macos.py are importable on Linux, and linux.py on Windows:
    nothing platform-specific is imported at module level. That is what lets
    `report()` describe a platform it is not running on, and what lets the test
    suite check the Windows paths from a Linux CI — see tests/test_desktop.py
    and phase 07.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from . import input as _input
from . import linux, macos, windows
from .caps import Capability, UnsupportedOnThisPlatform
from .input import KeyUnknown

__all__ = [
    "screenshot", "brightness_get", "brightness_set", "volume_get", "volume_set",
    "trash", "clipboard_get", "clipboard_set", "notify", "set_wallpaper",
    "type_text", "key", "hotkey", "click", "move_to", "move_by", "drag_to",
    "scroll", "hscroll", "screen_size", "input_mechanism",
    "capabilities", "report", "backend", "backend_for", "Capability",
    "UnsupportedOnThisPlatform", "KeyUnknown",
]

_BACKENDS = {"linux": linux, "windows": windows, "macos": macos}

SURFACES = ("screenshot", "brightness", "volume", "trash",
            "clipboard", "notify", "wallpaper", "input")


def _platform_name(system: str | None = None) -> str:
    name = (system or platform.system()).lower()
    if name.startswith("win"):
        return "windows"
    if name in ("darwin", "macos"):
        return "macos"
    return "linux"


def backend_for(system: str | None = None):
    """The backend module for a platform name. Used by the report and by tests
    to inspect a platform they are not running on."""
    return _BACKENDS[_platform_name(system)]


def backend():
    """The backend for the machine this is running on."""
    return backend_for()


# ── the facade ───────────────────────────────────────────────────────────────

def screenshot() -> tuple[bytes, str]:
    """Capture the primary display. Returns (bytes, mime_type)."""
    return backend().screenshot()


def brightness_get() -> int | None:
    """0-100, or None where the platform will not say.

    None is a real answer, not a failure: core/undo.py refuses to register a
    setting change it cannot reverse, and an undo that restores a guess is
    worse than no undo.
    """
    return backend().brightness_get()


def brightness_set(percent: int) -> None:
    backend().brightness_set(percent)


def volume_get() -> int | None:
    return backend().volume_get()


def volume_set(percent: int) -> None:
    backend().volume_set(percent)


def trash(path: str | Path) -> bool:
    """Move to the desktop trash — never an unlink. The user asked to delete a
    file, not to make it unrecoverable."""
    return backend().trash(path)


def clipboard_get() -> str:
    return backend().clipboard_get()


def clipboard_set(text: str) -> None:
    backend().clipboard_set(text)


def notify(title: str, body: str = "") -> None:
    backend().notify(title, body)


def set_wallpaper(path: str | Path) -> None:
    backend().set_wallpaper(path)


# ── input injection ──────────────────────────────────────────────────────────
#
# These do not go through backend(). The other surfaces genuinely have three
# unrelated implementations; input has one implementation that picks among four
# mechanisms, and two of the four (X11 and Windows) are the same code. Splitting
# it across three files to preserve the pattern would triple it.
#
# This is the half of R-09 that was missing. An action that needs to press a key
# has somewhere to go now, so "never call pyautogui from an action" is a rule
# rather than a wish. tests/test_input_facade.py enforces it.

def type_text(text: str, interval: float = 0.0) -> None:
    """Type a string. Not a paste — the clipboard belongs to the user."""
    _input.type_text(text, interval=interval)


def key(name: str) -> None:
    """Press and release one key, by a name that means the same everywhere:
    `key("enter")`, `key("volumeup")`, `key("f11")`."""
    _input.key(name)


def hotkey(*names: str) -> None:
    """A chord. `hotkey("ctrl", "shift", "escape")`.

    "win", "cmd", "command" and "super" are the same key — the caller does not
    branch on the OS to choose the word.
    """
    _input.hotkey(*names)


def click(x: int | None = None, y: int | None = None,
          button: str = "left", clicks: int = 1) -> None:
    _input.click(x, y, button=button, clicks=clicks)


def move_to(x: int, y: int, duration: float = 0.0) -> None:
    _input.move_to(x, y, duration=duration)


def move_by(dx: float, dy: float) -> None:
    """Relative pointer motion — the only kind Wayland allows."""
    _input.move_by(dx, dy)


def drag_to(x: int, y: int, button: str = "left", duration: float = 0.0) -> None:
    _input.drag_to(x, y, button=button, duration=duration)


def scroll(clicks: int) -> None:
    """Vertical. Positive is up."""
    _input.scroll(clicks)


def hscroll(clicks: int) -> None:
    """Horizontal. Positive is right."""
    _input.hscroll(clicks)


def screen_size() -> tuple[int, int]:
    return _input.screen_size()


def input_mechanism(system: str | None = None) -> str:
    """Which of the four input paths this machine uses. For logs and reports."""
    return _input.mechanism(system)


# ── capability report ────────────────────────────────────────────────────────

def capabilities(system: str | None = None) -> dict[str, Capability]:
    """What each surface can do here, and by what mechanism.

    A surface whose probe itself raises is reported as unavailable with the
    exception on it, rather than taking down the report — the report is most
    useful exactly when something is broken.
    """
    mod = backend_for(system)
    out: dict[str, Capability] = {}
    for surface in SURFACES:
        probe = getattr(mod, f"{surface}_capability", None)
        if probe is None:
            out[surface] = Capability(surface, False, "none", "not implemented")
            continue
        try:
            out[surface] = probe()
        except Exception as e:
            out[surface] = Capability(surface, False, "error",
                                      f"{type(e).__name__}: {e}")
    return out


def report(system: str | None = None) -> str:
    """The capability matrix, for a human.

    `python -c "from core import desktop; print(desktop.report())"`
    """
    name = _platform_name(system)
    lines = [f"desktop backend: {name}"]
    if name == "linux":
        session = "wayland" if linux.is_wayland() else "x11"
        lines[0] += f"  ({session})"
    lines.append("")
    for cap in capabilities(system).values():
        lines.append("  " + str(cap))
    return "\n".join(lines)
