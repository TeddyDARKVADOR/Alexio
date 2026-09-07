"""
core/desktop/linux.py — the Linux backend.

Two very different Linux desktops hide behind one name, and Alexio has to work
on both:

  X11      — the old contract. Anything can read the screen and inject input.
             mss and pyautogui work.
  Wayland  — the compositor mediates. Screen reading and input injection go
             through xdg-desktop-portal, and the X11 tools either return black
             frames or reach only XWayland windows.

So every function here picks by session type, not by "Linux". Getting that wrong
is not a crash, which is the problem: `mss` on Wayland succeeds and returns a
black rectangle, and the assistant confidently describes an empty screen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .caps import Capability, UnsupportedOnThisPlatform

NAME = "linux"


def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def is_wayland() -> bool:
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    # A GNOME session sets WAYLAND_DISPLAY even when XDG_SESSION_TYPE is unset
    # (a bare TTY login into a compositor, some display managers).
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def has(binary: str) -> bool:
    return shutil.which(binary) is not None


# ── screenshot ───────────────────────────────────────────────────────────────

def screenshot_capability() -> Capability:
    from . import portal

    if is_wayland():
        if portal.available():
            return Capability("screenshot", True, "xdg-portal",
                              "silent after the first permission grant")
        return Capability("screenshot", False, "none",
                          "Wayland session without a reachable portal — "
                          "install xdg-desktop-portal-gnome, or: pip install jeepney")
    try:
        import mss  # noqa: F401
        return Capability("screenshot", True, "mss/X11")
    except Exception:
        return Capability("screenshot", False, "none", "pip install mss")


def screenshot() -> tuple[bytes, str]:
    from . import portal

    if is_wayland():
        return portal.screenshot(), "image/png"

    try:
        import mss
        import mss.tools
    except ImportError as e:
        raise UnsupportedOnThisPlatform("pip install mss") from e

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
        return mss.tools.to_png(shot.rgb, shot.size), "image/png"


# ── brightness ───────────────────────────────────────────────────────────────
#
# org.gnome.SettingsDaemon.Power.Screen is gone as of GNOME 49 — only the
# Keyboard interface survives. logind's SetBrightness is the replacement, it is
# unprivileged for the active session, and it is what brightnessctl itself uses.

def _backlight_device() -> tuple[str, str] | None:
    root = Path("/sys/class/backlight")
    try:
        devices = sorted(p.name for p in root.iterdir())
    except OSError:
        return None
    return ("backlight", devices[0]) if devices else None


def brightness_capability() -> Capability:
    dev = _backlight_device()
    if dev is None:
        return Capability("brightness", False, "none",
                          "no /sys/class/backlight device — desktop monitor, or a "
                          "GPU driver that does not expose one")
    if has("busctl") or has("gdbus"):
        return Capability("brightness", True, "logind", f"device {dev[1]}")
    if has("brightnessctl"):
        return Capability("brightness", True, "brightnessctl")
    return Capability("brightness", False, "none",
                      "install systemd's busctl, or: sudo dnf install brightnessctl")


def brightness_get() -> int | None:
    dev = _backlight_device()
    if dev is None:
        return None
    base = Path("/sys/class/backlight") / dev[1]
    try:
        cur = int((base / "brightness").read_text(encoding="utf-8").strip())
        mx  = int((base / "max_brightness").read_text(encoding="utf-8").strip())
        return max(0, min(100, round(cur * 100 / mx))) if mx else None
    except (OSError, ValueError):
        pass
    if has("brightnessctl"):
        try:
            cur = int(_run(["brightnessctl", "get"]).stdout.strip())
            mx  = int(_run(["brightnessctl", "max"]).stdout.strip())
            return max(0, min(100, round(cur * 100 / mx))) if mx else None
        except Exception:
            return None
    return None


def brightness_set(percent: int) -> None:
    percent = max(1, min(100, int(percent)))    # never 0: a black screen reads as a crash
    dev = _backlight_device()

    if dev is not None and has("busctl"):
        base = Path("/sys/class/backlight") / dev[1]
        try:
            mx  = int((base / "max_brightness").read_text(encoding="utf-8").strip())
            raw = max(1, round(mx * percent / 100))
            r = _run(["busctl", "--user", "call", "org.freedesktop.login1",
                      "/org/freedesktop/login1/session/auto",
                      "org.freedesktop.login1.Session", "SetBrightness",
                      "ssu", dev[0], dev[1], str(raw)])
            if r.returncode == 0:
                return
            # --user is wrong on some setups; the system bus owns login1.
            r = _run(["busctl", "call", "org.freedesktop.login1",
                      "/org/freedesktop/login1/session/auto",
                      "org.freedesktop.login1.Session", "SetBrightness",
                      "ssu", dev[0], dev[1], str(raw)])
            if r.returncode == 0:
                return
        except Exception:
            pass

    if has("brightnessctl"):
        if _run(["brightnessctl", "set", f"{percent}%"]).returncode == 0:
            return

    raise UnsupportedOnThisPlatform(
        "Could not set brightness. On GNOME 49+ this needs systemd's busctl "
        "(logind SetBrightness); the old org.gnome.SettingsDaemon.Power.Screen "
        "interface no longer exists. Alternative: sudo dnf install brightnessctl"
    )


# ── volume ───────────────────────────────────────────────────────────────────

def volume_capability() -> Capability:
    if has("wpctl"):
        return Capability("volume", True, "wpctl/PipeWire")
    if has("pactl"):
        return Capability("volume", True, "pactl")
    return Capability("volume", False, "none",
                      "neither wpctl nor pactl — is PipeWire running?")


def volume_get() -> int | None:
    if has("wpctl"):
        try:
            out = _run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]).stdout
            # "Volume: 0.65" or "Volume: 0.00 [MUTED]"
            value = float(out.split(":", 1)[1].split()[0])
            return max(0, min(100, round(value * 100)))
        except Exception:
            pass
    if has("pactl"):
        try:
            import re
            out = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]).stdout
            m = re.search(r"(\d+)%", out)
            return max(0, min(100, int(m.group(1)))) if m else None
        except Exception:
            return None
    return None


def volume_set(percent: int) -> None:
    percent = max(0, min(100, int(percent)))
    if has("wpctl"):
        if _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@",
                 f"{percent}%"]).returncode == 0:
            return
    if has("pactl"):
        if _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@",
                 f"{percent}%"]).returncode == 0:
            return
    raise UnsupportedOnThisPlatform("No wpctl or pactl to set the volume with.")


# ── trash ────────────────────────────────────────────────────────────────────

def trash_capability() -> Capability:
    from . import portal

    if portal.available():
        return Capability("trash", True, "xdg-portal")
    try:
        import send2trash  # noqa: F401
        return Capability("trash", True, "send2trash")
    except Exception:
        return Capability("trash", False, "none", "pip install send2trash")


def trash(path: str | Path) -> bool:
    from . import portal

    if portal.available():
        try:
            return portal.trash(path)
        except portal.PortalError:
            pass          # fall through rather than refusing to delete anything
    try:
        import send2trash
        send2trash.send2trash(str(path))
        return True
    except Exception as e:
        raise UnsupportedOnThisPlatform(f"Could not move to trash: {e}") from e


# ── clipboard ────────────────────────────────────────────────────────────────

def clipboard_capability() -> Capability:
    try:
        import pyperclip
        pyperclip.determine_clipboard()
        return Capability("clipboard", True, "pyperclip")
    except Exception:
        pass
    if has("wl-copy"):
        return Capability("clipboard", True, "wl-clipboard")
    if has("xclip"):
        return Capability("clipboard", True, "xclip")
    return Capability("clipboard", False, "none",
                      "sudo dnf install wl-clipboard")


def clipboard_get() -> str:
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception:
        pass
    if has("wl-paste"):
        return _run(["wl-paste", "--no-newline"]).stdout
    if has("xclip"):
        return _run(["xclip", "-selection", "clipboard", "-o"]).stdout
    raise UnsupportedOnThisPlatform("No clipboard tool. sudo dnf install wl-clipboard")


def clipboard_set(text: str) -> None:
    try:
        import pyperclip
        pyperclip.copy(text)
        return
    except Exception:
        pass
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if has(cmd[0]):
            subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=5)
            return
    raise UnsupportedOnThisPlatform("No clipboard tool. sudo dnf install wl-clipboard")


# ── notifications ────────────────────────────────────────────────────────────

def notify_capability() -> Capability:
    if has("notify-send"):
        return Capability("notify", True, "notify-send")
    from . import portal
    if portal.available():
        return Capability("notify", True, "xdg-portal")
    return Capability("notify", False, "none", "sudo dnf install libnotify")


def notify(title: str, body: str = "") -> None:
    if has("notify-send"):
        _run(["notify-send", "--urgency=normal", "--expire-time=15000", title, body])
        return
    raise UnsupportedOnThisPlatform("notify-send is not installed.")


# ── wallpaper ────────────────────────────────────────────────────────────────

def wallpaper_capability() -> Capability:
    from . import portal
    if portal.available():
        return Capability("wallpaper", True, "xdg-portal")
    if has("gsettings"):
        return Capability("wallpaper", True, "gsettings", "GNOME only")
    return Capability("wallpaper", False, "none", "no portal and no gsettings")


def set_wallpaper(path: str | Path) -> None:
    from . import portal

    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise UnsupportedOnThisPlatform(f"No such image: {target}")

    if portal.available():
        try:
            portal.set_wallpaper(target)
            return
        except portal.PortalError:
            pass
    if has("gsettings"):
        uri = target.as_uri()
        for key in ("picture-uri", "picture-uri-dark"):
            _run(["gsettings", "set", "org.gnome.desktop.background", key, uri])
        return
    raise UnsupportedOnThisPlatform("No portal and no gsettings to set a wallpaper.")


# ── input injection ──────────────────────────────────────────────────────────
#
# The hardest surface, and the one where an honest "no" matters most: an
# assistant that reports a keystroke it never delivered is worse than one that
# says it cannot type.

def input_capability() -> Capability:
    if not is_wayland():
        try:
            import pyautogui  # noqa: F401
            return Capability("input", True, "pyautogui/XTEST")
        except Exception:
            return Capability("input", False, "none", "pip install pyautogui")

    if has("ydotool"):
        return Capability("input", True, "ydotool",
                          "needs the ydotoold daemon running")
    if has("wtype"):
        return Capability("input", True, "wtype", "keyboard only, no pointer")

    try:
        import pyautogui  # noqa: F401
        return Capability(
            "input", False, "pyautogui/XTEST",
            "Wayland: XTEST reaches XWayland windows only, so native GTK/Qt apps "
            "silently ignore it. Install ydotool, or wire the RemoteDesktop "
            "portal (libei 1.5.0 and portal v2 are present on this machine)."
        )
    except Exception:
        return Capability("input", False, "none",
                          "sudo dnf install ydotool && systemctl --user enable --now ydotoold")
