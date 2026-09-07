"""
core/desktop/windows.py — the Windows backend.

Alexio was a Windows program first, and everything in here is the mechanism the
original code already used, moved behind the same facade as Linux and macOS so
that one call site works on all three:

  screenshot  — mss, which is unrestricted on Windows
  brightness  — WMI through PowerShell
  volume      — pycaw (Core Audio), with a keypress fallback
  trash       — send2trash, which calls SHFileOperation
  notify      — win10toast

NOTHING HERE IS IMPORTED AT MODULE LEVEL. `pycaw`, `comtypes` and `win10toast`
only exist on Windows, and this file has to be importable on Linux for the
capability report and the test suite to be able to say anything about Windows at
all. Every Windows-only import is inside the function that needs it.
"""

from __future__ import annotations

import math
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .caps import Capability, UnsupportedOnThisPlatform

NAME = "windows"

# Every subprocess on Windows must be windowless: main.py patches Popen for
# this, but a CompletedProcess call made before that patch — or from a test —
# would flash a console. Cheap to be explicit.
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _ps(script: str, timeout: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout, **_NO_WINDOW,
    )


def _on_windows() -> bool:
    return sys.platform == "win32"


def _requires_windows() -> None:
    if not _on_windows():
        raise UnsupportedOnThisPlatform(
            f"This is the Windows backend; the current platform is "
            f"{platform.system() or sys.platform}."
        )


# ── screenshot ───────────────────────────────────────────────────────────────

def screenshot_capability() -> Capability:
    try:
        import mss  # noqa: F401
        return Capability("screenshot", True, "mss")
    except Exception:
        return Capability("screenshot", False, "none", "pip install mss")


def screenshot() -> tuple[bytes, str]:
    _requires_windows()
    try:
        import mss
        import mss.tools
    # cf. core/desktop/linux.py — mss ne se contente pas d'ImportError.
    except Exception as e:
        raise UnsupportedOnThisPlatform(f"pip install mss ({type(e).__name__})") from e

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
        return mss.tools.to_png(shot.rgb, shot.size), "image/png"


# ── brightness ───────────────────────────────────────────────────────────────

def brightness_capability() -> Capability:
    if not _on_windows():
        return Capability("brightness", False, "wmi", "Windows only")
    if shutil.which("powershell") is None:
        return Capability("brightness", False, "none", "powershell.exe not on PATH")
    return Capability("brightness", True, "wmi/powershell",
                      "laptop panels only; most desktop monitors do not expose WMI")


def brightness_get() -> int | None:
    _requires_windows()
    try:
        out = _ps("(Get-WmiObject -Namespace root/wmi "
                  "-Class WmiMonitorBrightness).CurrentBrightness").stdout.strip()
        return max(0, min(100, int(out.splitlines()[0])))
    except Exception:
        return None


def brightness_set(percent: int) -> None:
    _requires_windows()
    percent = max(1, min(100, int(percent)))
    r = _ps("(Get-WmiObject -Namespace root/wmi "
            f"-Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {percent})")
    if r.returncode != 0:
        raise UnsupportedOnThisPlatform(
            "WMI refused to set brightness. External monitors usually do not "
            "support it; only the built-in panel does."
        )


# ── volume ───────────────────────────────────────────────────────────────────

def volume_capability() -> Capability:
    if not _on_windows():
        return Capability("volume", False, "pycaw", "Windows only")
    try:
        import pycaw  # noqa: F401
        return Capability("volume", True, "pycaw")
    except Exception:
        return Capability("volume", False, "keypress",
                          "pip install pycaw comtypes for exact levels; "
                          "without it only up/down keys work")


def _endpoint_volume():
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    devices   = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def volume_get() -> int | None:
    _requires_windows()
    try:
        db = _endpoint_volume().GetMasterVolumeLevel()
        if db <= -65.0:
            return 0
        return max(0, min(100, round(10 ** (db / 20) * 100)))
    except Exception:
        return None


def volume_set(percent: int) -> None:
    _requires_windows()
    percent = max(0, min(100, int(percent)))
    try:
        db = -65.25 if percent == 0 else max(-65.25, 20 * math.log10(percent / 100))
        _endpoint_volume().SetMasterVolumeLevel(db, None)
    except Exception as e:
        raise UnsupportedOnThisPlatform(
            f"pycaw could not set the volume ({e}). pip install pycaw comtypes"
        ) from e


# ── trash ────────────────────────────────────────────────────────────────────

def trash_capability() -> Capability:
    try:
        import send2trash  # noqa: F401
        return Capability("trash", True, "send2trash")
    except Exception:
        return Capability("trash", False, "none", "pip install send2trash")


def trash(path: str | Path) -> bool:
    _requires_windows()
    try:
        import send2trash
        send2trash.send2trash(str(path))
        return True
    except Exception as e:
        raise UnsupportedOnThisPlatform(f"Could not move to Recycle Bin: {e}") from e


# ── clipboard ────────────────────────────────────────────────────────────────

def clipboard_capability() -> Capability:
    try:
        import pyperclip  # noqa: F401
        return Capability("clipboard", True, "pyperclip")
    except Exception:
        return Capability("clipboard", False, "none", "pip install pyperclip")


def clipboard_get() -> str:
    _requires_windows()
    import pyperclip
    return pyperclip.paste()


def clipboard_set(text: str) -> None:
    _requires_windows()
    import pyperclip
    pyperclip.copy(text)


# ── notifications ────────────────────────────────────────────────────────────

def notify_capability() -> Capability:
    if not _on_windows():
        return Capability("notify", False, "win10toast", "Windows only")
    try:
        import win10toast  # noqa: F401
        return Capability("notify", True, "win10toast")
    except Exception:
        return Capability("notify", False, "none", "pip install win10toast")


def notify(title: str, body: str = "") -> None:
    _requires_windows()
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast(title, body, duration=15, threaded=False)
    except Exception as e:
        raise UnsupportedOnThisPlatform(f"Toast notification failed: {e}") from e


# ── wallpaper ────────────────────────────────────────────────────────────────

def wallpaper_capability() -> Capability:
    if not _on_windows():
        return Capability("wallpaper", False, "SystemParametersInfoW", "Windows only")
    return Capability("wallpaper", True, "SystemParametersInfoW")


def set_wallpaper(path: str | Path) -> None:
    _requires_windows()
    import ctypes

    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise UnsupportedOnThisPlatform(f"No such image: {target}")

    # SystemParametersInfoW takes BMP reliably and other formats depending on
    # the Windows build; converting is cheaper than debugging a silent no-op.
    if target.suffix.lower() in {".webp", ".png"}:
        try:
            import tempfile

            from PIL import Image
            bmp = Path(tempfile.mktemp(suffix=".bmp"))
            Image.open(target).convert("RGB").save(bmp, "BMP")
            target = bmp
        except Exception:
            pass

    if not ctypes.windll.user32.SystemParametersInfoW(20, 0, str(target), 3):
        raise UnsupportedOnThisPlatform("SystemParametersInfoW refused the image.")


# ── input injection ──────────────────────────────────────────────────────────

def input_capability() -> Capability:
    """Windows has no Wayland problem: SendInput reaches every window.

    Delegated to core/desktop/input.py, which owns this surface everywhere.
    """
    from . import input as _input
    return _input.capability_for("windows")
