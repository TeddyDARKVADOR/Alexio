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

from . import linux, macos, windows
from .caps import Capability, UnsupportedOnThisPlatform

__all__ = [
    "screenshot", "brightness_get", "brightness_set", "volume_get", "volume_set",
    "trash", "clipboard_get", "clipboard_set", "notify", "set_wallpaper",
    "capabilities", "report", "backend", "backend_for", "Capability",
    "UnsupportedOnThisPlatform",
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
