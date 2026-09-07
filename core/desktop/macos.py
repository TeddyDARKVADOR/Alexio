"""
core/desktop/macos.py — the macOS backend.

Mostly `osascript` and `screencapture`, which is what the original code already
used. Two things are genuinely different from the other two platforms and worth
stating rather than discovering:

  · Screen recording and accessibility (input injection) are permissions the
    user grants per application in System Settings. The API does not fail — it
    returns an empty screen or does nothing — so the capability check has to
    look at the result, not at the return code.
  · There is no supported way to set display brightness on Apple Silicon from a
    script. The old `key code 144` trick sends the media key and is the honest
    ceiling here.

Like the Windows backend, nothing macOS-only is imported at module level, so
this file loads on Linux for the capability report and the tests.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import tempfile
from pathlib import Path

from .caps import Capability, UnsupportedOnThisPlatform

NAME = "macos"


def _on_macos() -> bool:
    return sys.platform == "darwin"


def _requires_macos() -> None:
    if not _on_macos():
        raise UnsupportedOnThisPlatform(
            f"This is the macOS backend; the current platform is "
            f"{platform.system() or sys.platform}."
        )


def _osa(script: str, timeout: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=timeout)


# ── screenshot ───────────────────────────────────────────────────────────────

def screenshot_capability() -> Capability:
    if not _on_macos():
        return Capability("screenshot", False, "screencapture", "macOS only")
    return Capability("screenshot", True, "screencapture",
                      "needs Screen Recording permission in System Settings")


def screenshot() -> tuple[bytes, str]:
    _requires_macos()
    out = Path(tempfile.mktemp(suffix=".png"))
    try:
        # -x silences the shutter sound, which an assistant taking a look at the
        # screen every few minutes would otherwise make unbearable.
        r = subprocess.run(["screencapture", "-x", str(out)],
                           capture_output=True, timeout=15)
        if r.returncode != 0 or not out.exists():
            raise UnsupportedOnThisPlatform(
                "screencapture failed — grant Screen Recording permission to the "
                "terminal or app running Alexio in System Settings › Privacy."
            )
        return out.read_bytes(), "image/png"
    finally:
        try:
            out.unlink()
        except OSError:
            pass


# ── brightness ───────────────────────────────────────────────────────────────

def brightness_capability() -> Capability:
    if not _on_macos():
        return Capability("brightness", False, "osascript", "macOS only")
    return Capability("brightness", False, "media-keys",
                      "no scriptable absolute brightness on macOS; only the "
                      "up/down media keys, so undo cannot restore a value")


def brightness_get() -> int | None:
    """Deliberately None.

    core/undo.py's rule: a setting whose previous value cannot be read is not
    registered as undoable, because an undo that restores a guess is worse than
    no undo. macOS is exactly that case.
    """
    return None


def brightness_set(percent: int) -> None:
    _requires_macos()
    raise UnsupportedOnThisPlatform(
        "macOS exposes no scriptable absolute brightness. Only relative "
        "up/down via the media keys is available."
    )


def brightness_step(up: bool) -> None:
    _requires_macos()
    _osa(f'tell application "System Events" to key code {144 if up else 145}')


# ── volume ───────────────────────────────────────────────────────────────────

def volume_capability() -> Capability:
    if not _on_macos():
        return Capability("volume", False, "osascript", "macOS only")
    return Capability("volume", True, "osascript")


def volume_get() -> int | None:
    _requires_macos()
    try:
        out = _osa("output volume of (get volume settings)").stdout.strip()
        return max(0, min(100, int(out)))
    except Exception:
        return None


def volume_set(percent: int) -> None:
    _requires_macos()
    percent = max(0, min(100, int(percent)))
    _osa(f"set volume output volume {percent}")


# ── trash ────────────────────────────────────────────────────────────────────

def trash_capability() -> Capability:
    try:
        import send2trash  # noqa: F401
        return Capability("trash", True, "send2trash")
    except Exception:
        if _on_macos():
            return Capability("trash", True, "osascript")
        return Capability("trash", False, "none", "pip install send2trash")


def trash(path: str | Path) -> bool:
    _requires_macos()
    try:
        import send2trash
        send2trash.send2trash(str(path))
        return True
    except Exception:
        pass
    target = Path(path).expanduser().resolve()
    r = _osa(f'tell application "Finder" to delete POSIX file "{target}"')
    if r.returncode != 0:
        raise UnsupportedOnThisPlatform(f"Could not move to Trash: {r.stderr.strip()}")
    return True


# ── clipboard ────────────────────────────────────────────────────────────────

def clipboard_capability() -> Capability:
    try:
        import pyperclip  # noqa: F401
        return Capability("clipboard", True, "pyperclip")
    except Exception:
        if _on_macos():
            return Capability("clipboard", True, "pbcopy/pbpaste")
        return Capability("clipboard", False, "none", "pip install pyperclip")


def clipboard_get() -> str:
    _requires_macos()
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception:
        return subprocess.run(["pbpaste"], capture_output=True, text=True,
                              timeout=5).stdout


def clipboard_set(text: str) -> None:
    _requires_macos()
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception:
        subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, timeout=5)


# ── notifications ────────────────────────────────────────────────────────────

def notify_capability() -> Capability:
    if not _on_macos():
        return Capability("notify", False, "osascript", "macOS only")
    return Capability("notify", True, "osascript")


def notify(title: str, body: str = "") -> None:
    _requires_macos()
    safe_title = title.replace('"', "'")
    safe_body  = body.replace('"', "'")
    _osa(f'display notification "{safe_body}" with title "{safe_title}"')


# ── wallpaper ────────────────────────────────────────────────────────────────

def wallpaper_capability() -> Capability:
    if not _on_macos():
        return Capability("wallpaper", False, "osascript", "macOS only")
    return Capability("wallpaper", True, "osascript")


def set_wallpaper(path: str | Path) -> None:
    _requires_macos()
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise UnsupportedOnThisPlatform(f"No such image: {target}")
    _osa('tell application "System Events" to tell every desktop to '
         f'set picture to POSIX file "{target}"')


# ── input injection ──────────────────────────────────────────────────────────

def input_capability() -> Capability:
    """Delegated to core/desktop/input.py, which owns this surface everywhere."""
    from . import input as _input
    return _input.capability_for("macos")
