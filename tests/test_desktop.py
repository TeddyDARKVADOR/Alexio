"""
core/desktop — one facade, three backends, and a report that tells the truth.

Two things are being protected here.

  1. The facade dispatches to the right backend, and every backend implements
     the whole surface. A missing function is an AttributeError at the moment
     the user asks for something, which is the worst possible time to find out.

  2. **Every backend imports on every platform.** windows.py must load on Linux
     and linux.py on Windows, because that is what lets this suite check the
     Windows paths from a Linux machine — and it is the whole basis of the
     Windows-compatibility work in phase 07.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from core import desktop
from core.desktop import linux, macos, windows
from core.desktop.caps import Capability, UnsupportedOnThisPlatform

ROOT = Path(__file__).resolve().parent.parent
BACKENDS = {"linux": linux, "windows": windows, "macos": macos}

# What the facade promises. A backend missing one of these is a bug, not a
# platform limitation — the limitation is expressed by the capability saying no.
REQUIRED = [
    "screenshot", "brightness_get", "brightness_set", "volume_get", "volume_set",
    "trash", "clipboard_get", "clipboard_set", "notify", "set_wallpaper",
]


# ── every backend is complete ────────────────────────────────────────────────

@pytest.mark.parametrize("name,mod", BACKENDS.items())
def test_every_backend_implements_the_whole_surface(name, mod):
    missing = [fn for fn in REQUIRED if not callable(getattr(mod, fn, None))]
    assert not missing, f"{name} backend is missing {missing}"


@pytest.mark.parametrize("name,mod", BACKENDS.items())
def test_every_backend_can_describe_every_surface(name, mod):
    for surface in desktop.SURFACES:
        probe = getattr(mod, f"{surface}_capability", None)
        assert callable(probe), f"{name} cannot describe '{surface}'"
        cap = probe()
        assert isinstance(cap, Capability)
        assert cap.name == surface
        # A "no" must always say what would fix it.
        if not cap.available:
            assert cap.detail, f"{name}.{surface} says no without saying why"


# ── the cross-platform import rule (phase 07's foundation) ───────────────────

@pytest.mark.parametrize("name", BACKENDS)
def test_no_backend_imports_a_platform_specific_module_at_load_time(name):
    """Windows-only packages must be imported inside functions.

    `pycaw`, `comtypes`, `win10toast` and `winreg` do not exist on Linux; a
    module-level import of any of them would make core/desktop unimportable
    here, and with it the whole capability report and this test suite.
    """
    platform_only = {
        "pycaw", "comtypes", "win10toast", "winreg", "win32com", "win32api",
        "wmi", "AppKit", "Quartz", "Foundation", "jeepney",
    }
    path = ROOT / "core" / "desktop" / f"{name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    offenders = []
    for node in tree.body:                       # module level only
        mods: set[str] = set()
        if isinstance(node, ast.Import):
            mods = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = {node.module.split(".")[0]}
        if mods & platform_only:
            offenders.append(f"line {node.lineno}: {sorted(mods & platform_only)}")

    assert not offenders, (
        f"core/desktop/{name}.py imports a platform-specific module at load "
        f"time, so it cannot be inspected from another OS: {offenders}"
    )


def test_the_report_can_describe_a_platform_it_is_not_running_on():
    """This is how Windows compatibility gets checked from Linux."""
    for system in ("Linux", "Windows", "Darwin"):
        text = desktop.report(system)
        assert "desktop backend:" in text
        for surface in desktop.SURFACES:
            assert surface in text


def test_capabilities_never_raise_even_when_a_probe_does(monkeypatch):
    """The report is most useful exactly when something is broken."""
    def explode():
        raise OSError("no such device")

    monkeypatch.setattr(windows, "volume_capability", explode)
    caps = desktop.capabilities("Windows")
    assert caps["volume"].available is False
    assert "OSError" in caps["volume"].detail


# ── dispatch ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("system,expected", [
    ("Windows", windows), ("windows", windows),
    ("Darwin", macos),    ("macos", macos),
    ("Linux", linux),     ("FreeBSD", linux),      # anything else: POSIX-ish
])
def test_platform_names_map_to_the_right_backend(system, expected):
    assert desktop.backend_for(system) is expected


def test_the_live_backend_matches_this_machine():
    expected = {"linux": linux, "win32": windows, "darwin": macos}
    assert desktop.backend() is expected.get(sys.platform, linux)


# ── refusing to act on the wrong platform ────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="running on Windows")
def test_the_windows_backend_refuses_to_run_elsewhere():
    """Calling it from Linux must be a clear error, not a silent no-op or a
    confusing crash from deep inside ctypes."""
    for call in (lambda: windows.screenshot(),
                 lambda: windows.brightness_set(50),
                 lambda: windows.notify("t", "b")):
        with pytest.raises(UnsupportedOnThisPlatform, match="Windows backend"):
            call()


@pytest.mark.skipif(sys.platform == "darwin", reason="running on macOS")
def test_the_macos_backend_refuses_to_run_elsewhere():
    with pytest.raises(UnsupportedOnThisPlatform, match="macOS backend"):
        macos.screenshot()


def test_macos_reports_brightness_as_unreadable():
    """Not an oversight: macOS has no scriptable absolute brightness, so
    core/undo.py must not register a change it cannot reverse."""
    assert macos.brightness_get() is None
    assert macos.brightness_capability().available is False


# ── Linux: the Wayland / X11 split ───────────────────────────────────────────

def test_wayland_is_detected_from_either_signal(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert linux.is_wayland()

    monkeypatch.setenv("XDG_SESSION_TYPE", "tty")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert linux.is_wayland(), "a compositor started from a TTY is still Wayland"

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not linux.is_wayland()


def test_wayland_without_a_portal_says_no_rather_than_returning_black(monkeypatch):
    """mss under Wayland does not fail — it succeeds and returns a black
    rectangle, and the assistant describes an empty screen. Refusing is the
    only honest answer."""
    from core.desktop import portal

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(portal, "available", lambda: False)

    cap = linux.screenshot_capability()
    assert cap.available is False
    assert "portal" in cap.detail.lower()


def test_brightness_is_clamped_away_from_zero(monkeypatch):
    """A screen at 0% reads as a crash, not as a setting."""
    seen = []
    monkeypatch.setattr(linux, "_backlight_device", lambda: None)
    monkeypatch.setattr(linux, "has", lambda b: b == "brightnessctl")
    monkeypatch.setattr(linux, "_run",
                        lambda cmd, timeout=5.0: seen.append(cmd) or
                        type("R", (), {"returncode": 0, "stdout": ""})())

    linux.brightness_set(0)
    assert "1%" in " ".join(seen[-1])


# ── this machine, live ───────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
def test_this_machine_reports_a_coherent_picture():
    caps = desktop.capabilities()
    assert set(caps) == set(desktop.SURFACES)
    for cap in caps.values():
        assert cap.backend != "" and isinstance(cap.available, bool)
