"""
core/desktop/input.py — the keyboard and the pointer, behind one door.

WHY THIS FILE EXISTS
    R-09 says an action never calls `pyautogui` directly. Until now that rule was
    decorative, and not because anyone was careless: `core/desktop` exposed
    screenshot, brightness, volume, trash, clipboard, notify and wallpaper — and
    nothing at all for input. `linux.input_capability()` described a capability
    the facade did not offer. Nine action modules made 140 direct pyautogui calls
    because there was nowhere else for them to go.

    So this is not "one more backend". It is the piece that makes R-09 a rule
    somebody can actually follow.

FOUR MECHANISMS, PICKED PER CALL
    Wayland   xdg-desktop-portal RemoteDesktop, over D-Bus, in pure Python
    X11       pyautogui → XTEST
    Windows   pyautogui → SendInput
    macOS     pyautogui → Quartz (needs Accessibility permission)

    The choice happens inside each function, never at import (R-13). That is
    what lets `capability_for("Windows")` be answered from a Linux laptop, and
    what keeps this file importable in CI with none of it installed.

WHY THE PORTAL AND NOT libei
    libei is the modern mechanism and it is installed on the target machine
    (1.5.0), but its Python binding goes through GObject introspection, and R-10
    keeps PyGObject out of the venv for reasons core/desktop/portal.py explains
    at length. The RemoteDesktop portal reaches the same compositor code path —
    mutter implements it on top of libei — and it speaks plain D-Bus, which
    jeepney already gives us. Same destination, no compiled binding.

WHAT WAYLAND STILL CANNOT DO, AND WHY IT SAYS SO
    Absolute pointer motion — `move_to(x, y)`, `click(x, y)` — needs a PipeWire
    stream id, which only exists if the RemoteDesktop session is linked to a
    ScreenCast session. That is a second permission prompt for screen *sharing*,
    to move a mouse. It is not wired, and `capability()` says exactly that
    instead of moving the pointer somewhere plausible and reporting success.

    Everything else works: typing, every chord, buttons, relative motion,
    scrolling. That is 117 of the 140 calls this file was written to absorb.

THE SESSION IS NOT FREE
    A RemoteDesktop session costs one permission dialog. Creating it per
    keystroke would be unusable, so it is created once, kept on its own D-Bus
    connection, and reused. `persist_mode=2` asks the portal to remember the
    grant across restarts; the token it hands back is stored under
    ~/.config/alexio so the second run is silent. If the user revokes it, the
    next call raises with the reason — it does not silently fall back to XTEST,
    which would type into XWayland windows and nowhere else (R-12).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from .caps import Capability, UnsupportedOnThisPlatform

__all__ = [
    "type_text", "key", "hotkey", "click", "move_to", "drag_to",
    "scroll", "hscroll", "screen_size", "capability", "capability_for",
    "mechanism", "KeyUnknown",
]


class KeyUnknown(UnsupportedOnThisPlatform):
    """A key name no backend can turn into a keystroke.

    Separate from the generic error because the fix is different: the caller
    used a name that does not exist, rather than the machine lacking a way to
    deliver it.
    """


# ── key names ────────────────────────────────────────────────────────────────
#
# One vocabulary for every backend. pyautogui has its own spelling and the
# portal wants X11 keysyms; callers should have to know neither.
#
# Aliases are deliberate and load-bearing: "win", "super", "cmd" and "command"
# are the same physical key with four names depending on who is writing, and
# actions/computer_settings.py uses all of them within thirty lines. Resolving
# them here is what lets that file stop branching on the OS to pick a word.

_ALIASES = {
    "control": "ctrl", "ctl": "ctrl",
    "win": "super", "windows": "super", "cmd": "super", "command": "super",
    "meta": "super", "option": "alt", "opt": "alt", "altgr": "alt",
    "return": "enter", "esc": "escape", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "page_up": "pageup",
    "page_down": "pagedown", "printscreen": "print_screen",
    "prtsc": "print_screen", "capslock": "caps_lock",
    "volume_up": "volumeup", "volume_down": "volumedown",
    "volume_mute": "volumemute", "mute": "volumemute",
    "spacebar": "space", "back": "backspace",
}

# X11 keysyms. Characters are their own keysym for Latin-1, which is why the
# table only needs to list the named keys — see `_keysym`.
_KEYSYMS = {
    "ctrl": 0xFFE3, "shift": 0xFFE1, "alt": 0xFFE9, "super": 0xFFEB,
    "tab": 0xFF09, "enter": 0xFF0D, "escape": 0xFF1B, "backspace": 0xFF08,
    "delete": 0xFFFF, "insert": 0xFF63, "space": 0x0020, "caps_lock": 0xFFE5,
    "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    "print_screen": 0xFF61, "menu": 0xFF67,
    # XF86 multimedia keys — the volume trio actions/computer_settings.py leans on.
    "volumeup": 0x1008FF13, "volumedown": 0x1008FF11, "volumemute": 0x1008FF12,
    "brightnessup": 0x1008FF02, "brightnessdown": 0x1008FF03,
    "playpause": 0x1008FF14, "nexttrack": 0x1008FF17, "prevtrack": 0x1008FF16,
}
_KEYSYMS.update({f"f{n}": 0xFFBE + n - 1 for n in range(1, 13)})

# pyautogui's own vocabulary, where it differs from ours.
_PYAUTOGUI_NAMES = {
    "super": "win", "escape": "esc",
    "print_screen": "printscreen", "caps_lock": "capslock",
    "brightnessup": "", "brightnessdown": "",     # pyautogui has no name for these
}

# macOS is not a spelling difference, it is a different key. pyautogui's Quartz
# backend maps "win" to nothing there; the modifier every Mac chord means is
# Command. Getting this wrong is silent — pyautogui skips a key it cannot map —
# so it is spelled out rather than left to a shared table.
_PYAUTOGUI_NAMES_MACOS = {
    "super": "command", "alt": "option",
}

# A key that exists in hardware but has no keysym and no scancode: the laptop Fn
# modifier is handled by the keyboard controller and never reaches the OS. It is
# listed so `hotkey("fn", "f11")` fails with a sentence instead of a KeyError.
_UNDELIVERABLE = {
    "fn": "the Fn key is handled inside the keyboard and never reaches the "
          "operating system, so nothing can synthesise it",
}

# evdev button codes, for the portal.
_BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}


def _canonical(name: str) -> str:
    n = str(name).strip().lower().replace(" ", "")
    return _ALIASES.get(n, n)


def _keysym(name: str) -> int:
    """X11 keysym for one canonical key name.

    Single characters map to themselves for Latin-1 and to the 0x01000000 +
    codepoint form above it — that is the X11 rule, and it is what makes typing
    layout-independent. Synthesising a *keycode* instead would type "q" on an
    AZERTY keyboard when asked for "a", because a keycode is a physical
    position on the board, not a letter.
    """
    if name in _UNDELIVERABLE:
        raise KeyUnknown(f"{name!r}: {_UNDELIVERABLE[name]}")
    if name in _KEYSYMS:
        return _KEYSYMS[name]
    if len(name) == 1:
        code = ord(name)
        return code if code < 0x100 else 0x01000000 + code
    raise KeyUnknown(
        f"Unknown key {name!r}. Known names: "
        f"{', '.join(sorted(_KEYSYMS))}, any single character, "
        f"or an alias of one of those."
    )


def _pyautogui_name(pg, name: str) -> str:
    """Our name → pyautogui's, checked against the keys it will actually accept.

    The check is the point. `pyautogui.hotkey("ctrl", "nosuchkey")` does not
    raise: keyDown returns early for a name it does not know, so Ctrl goes down,
    comes back up, and the function reports success having done nothing. An
    assistant that says "pressed Ctrl+Shift+Esc" and did not is worse than one
    that says it cannot (R-12).
    """
    if name in _UNDELIVERABLE:
        raise KeyUnknown(f"{name!r}: {_UNDELIVERABLE[name]}")
    if sys.platform == "darwin" and name in _PYAUTOGUI_NAMES_MACOS:
        mapped = _PYAUTOGUI_NAMES_MACOS[name]
    else:
        mapped = _PYAUTOGUI_NAMES.get(name, name)
    if mapped == "":
        raise KeyUnknown(f"pyautogui has no name for {name!r} on this platform.")

    known = getattr(pg, "KEYBOARD_KEYS", None)
    if known and mapped not in known:
        raise KeyUnknown(
            f"pyautogui does not know the key {name!r} (tried {mapped!r}) on "
            f"this platform, and would have ignored it silently.")
    return mapped


# ── which mechanism ──────────────────────────────────────────────────────────

def _session_is_wayland() -> bool:
    """Whether the *described* Linux session is Wayland.

    Delegated to linux.py rather than reimplemented: one place decides what
    counts as a Wayland session, and it already handles the GNOME case where
    XDG_SESSION_TYPE is unset but WAYLAND_DISPLAY is not.
    """
    from . import linux
    return linux.is_wayland()


def _is_wayland() -> bool:
    """Whether *this* machine is one — which is a stricter question.

    The two differ when a report is asked about Linux from somewhere else. A
    capability report may describe a session it is not in; a keystroke may not
    be delivered to one.
    """
    return sys.platform == "linux" and _session_is_wayland()


def mechanism(system: str | None = None) -> str:
    """Which of the four paths a call would take. Named for the report."""
    name = (system or sys.platform).lower()
    if name.startswith("win"):
        return "SendInput"
    if name in ("darwin", "macos"):
        return "Quartz"
    return "portal/RemoteDesktop" if _session_is_wayland() else "XTEST"


def _pyautogui():
    """Import it here, never at module level.

    On Linux it opens an X display when imported and raises
    DisplayConnectionError — not an ImportError — when there is none: over SSH,
    in a TTY, under a systemd unit, in CI. That specific mistake cost phase 00
    five modules.
    """
    try:
        import pyautogui
    except Exception as e:
        raise UnsupportedOnThisPlatform(
            f"pyautogui is unusable here ({type(e).__name__}: {e}). "
            f"Fix: pip install pyautogui, and run inside a graphical session."
        ) from e

    # A voice assistant that stops working because the pointer drifted into a
    # corner is worse than one that never had a failsafe. The pause is what
    # keeps a 30-key chord from outrunning the application receiving it.
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.02
    return pyautogui


# ── the Wayland session ──────────────────────────────────────────────────────

# Path() around the whole thing, not just the fallback: os.environ.get returns a
# *str* when XDG_CONFIG_HOME is set, and `str / "alexio"` raises TypeError — at
# import, on Linux, only for users who set it. Exactly the shape of bug that
# never shows up on the machine it was written on.
_STATE_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "alexio"
_TOKEN_PATH = _STATE_DIR / "remote_desktop.json"

# Device bitmask the portal uses in SelectDevices and returns from Start.
_DEV_KEYBOARD = 1
_DEV_POINTER = 2

_RELEASED, _PRESSED = 0, 1


class _RemoteDesktop:
    """One RemoteDesktop portal session, created once and kept.

    Held on its own D-Bus connection because the session dies with the
    connection that created it — portal.call() opens and closes one per call,
    which is right for a screenshot and fatal for a session.

    Guarded by a lock: the facade is synchronous and gets called from the
    executor threads main.py runs actions in, and two threads interleaving
    press/release on the same session produce a stuck modifier.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conn = None
        self._session: str | None = None
        self._devices = 0

    # ── plumbing ─────────────────────────────────────────────────────────────

    @staticmethod
    def _load_restore_token() -> str:
        try:
            data = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
            return str(data.get("restore_token", ""))
        except Exception:
            return ""

    @staticmethod
    def _save_restore_token(token: str) -> None:
        """Best effort. A token that cannot be saved costs one dialog next time,
        which is not worth failing a keystroke over."""
        if not token:
            return
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _TOKEN_PATH.write_text(json.dumps({"restore_token": token}),
                                   encoding="utf-8")
            os.chmod(_TOKEN_PATH, 0o600)
        except Exception:
            pass

    def _request(self, conn, method: str, signature: str, body: tuple,
                 timeout: float) -> dict:
        """A portal Request on *our* connection.

        Same pattern as portal.call() — the match rule goes in before the call,
        or a portal that answers quickly delivers the signal into a void — but
        it must not open its own connection, so the code cannot simply be
        reused. See core/desktop/portal.py for the long version.
        """
        import secrets

        from jeepney import DBusAddress, MatchRule, message_bus, new_method_call

        from . import portal

        token = "alexio_" + secrets.token_hex(8)
        body = list(body)
        options = dict(body[-1][1]) if body and isinstance(body[-1], tuple) else {}
        options["handle_token"] = ("s", token)
        body[-1] = ("a{sv}", options)

        sender = conn.unique_name.lstrip(":").replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                         member="Response", path=request_path)
        conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=5)

        with conn.filter(rule) as queue:
            addr = DBusAddress(portal.PORTAL_PATH, bus_name=portal.PORTAL_BUS,
                               interface="org.freedesktop.portal.RemoteDesktop")
            msg = new_method_call(addr, method, signature, tuple(body))
            try:
                conn.send_and_get_reply(msg, timeout=timeout)
            except Exception as e:
                raise portal.PortalError(
                    f"RemoteDesktop.{method} was refused: {e}") from e
            try:
                signal = conn.recv_until_filtered(queue, timeout=timeout)
            except Exception as e:
                raise portal.PortalError(
                    f"RemoteDesktop.{method} did not answer within "
                    f"{timeout:.0f}s.") from e

        code, results = signal.body
        if code == 1:
            raise portal.PortalError(
                f"RemoteDesktop.{method}: you dismissed the permission dialog. "
                f"Alexio cannot type or click until it is granted.")
        if code != 0:
            raise portal.PortalError(
                f"RemoteDesktop.{method} ended with code {code}.")
        return {k: (v[1] if isinstance(v, tuple) and len(v) == 2 else v)
                for k, v in dict(results).items()}

    def _ensure(self) -> None:
        """Create the session if there is not one. Idempotent, and cheap after
        the first call — which is the entire point of holding it."""
        if self._session is not None:
            return

        from . import portal

        if sys.platform != "linux":
            raise UnsupportedOnThisPlatform("The RemoteDesktop portal is Linux only.")
        try:
            from jeepney.io.blocking import open_dbus_connection
        except ImportError as e:
            raise UnsupportedOnThisPlatform(
                "jeepney is not installed, so input cannot go through the "
                "portal. Fix: pip install jeepney") from e

        try:
            conn = open_dbus_connection(bus="SESSION")
        except Exception as e:
            raise UnsupportedOnThisPlatform(f"No D-Bus session bus: {e}") from e

        try:
            results = self._request(conn, "CreateSession", "a{sv}", ({},), 30.0)
            session = results.get("session_handle")
            if not session:
                raise portal.PortalError(
                    "The portal created no session handle for RemoteDesktop.")

            # persist_mode 2 = "until the user revokes it". Without it every
            # launch of Alexio shows a dialog before it can press a key, which
            # is the difference between a usable assistant and a demo.
            options: dict = {
                "types": ("u", _DEV_KEYBOARD | _DEV_POINTER),
                "persist_mode": ("u", 2),
            }
            restore = self._load_restore_token()
            if restore:
                options["restore_token"] = ("s", restore)
            self._request(conn, "SelectDevices", "oa{sv}", (session, options), 30.0)

            started = self._request(conn, "Start", "osa{sv}",
                                    (session, "", {}), 120.0)
            self._save_restore_token(str(started.get("restore_token", "")))
            self._devices = int(started.get("devices", 0) or 0)

            self._conn, self._session = conn, session
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise

    def _notify(self, method: str, signature: str, args: tuple) -> None:
        from . import portal

        with self._lock:
            # _ensure() first: it is the one that knows how to say "jeepney is
            # not installed" and "you dismissed the dialog". Importing jeepney
            # above it would replace both messages with a bare ImportError.
            self._ensure()
            try:
                from jeepney import DBusAddress, new_method_call
            except ImportError as e:
                raise UnsupportedOnThisPlatform(
                    "jeepney is not installed, so input cannot go through the "
                    "portal. Fix: pip install jeepney") from e

            addr = DBusAddress(portal.PORTAL_PATH, bus_name=portal.PORTAL_BUS,
                               interface="org.freedesktop.portal.RemoteDesktop")
            msg = new_method_call(addr, method, signature,
                                  (self._session, {}) + args)
            try:
                self._conn.send_and_get_reply(msg, timeout=10)
            except Exception as e:
                # The session is gone — revoked, compositor restarted, screen
                # locked. Drop it so the next call rebuilds rather than failing
                # forever on a dead handle, and say which it was.
                self._session = None
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                raise UnsupportedOnThisPlatform(
                    f"The RemoteDesktop session ended ({e}). It will be "
                    f"recreated on the next call; if a dialog appears, that is "
                    f"the permission being asked for again."
                ) from e

    # ── what the facade needs ────────────────────────────────────────────────

    def keysym(self, sym: int, state: int) -> None:
        self._notify("NotifyKeyboardKeysym", "oa{sv}iu", (sym, state))

    def button(self, code: int, state: int) -> None:
        self._notify("NotifyPointerButton", "oa{sv}iu", (code, state))

    def motion(self, dx: float, dy: float) -> None:
        self._notify("NotifyPointerMotion", "oa{sv}dd", (float(dx), float(dy)))

    def axis(self, axis: int, steps: int) -> None:
        # 0 = vertical, 1 = horizontal. Discrete rather than smooth: one step is
        # one wheel click, which is what every caller here means by "scroll 3".
        self._notify("NotifyPointerAxisDiscrete", "oa{sv}ui", (axis, int(steps)))


_remote = _RemoteDesktop()


# ── the facade ───────────────────────────────────────────────────────────────

def type_text(text: str, interval: float = 0.0) -> None:
    """Type a string as if it were typed on the keyboard.

    Not the clipboard. Pasting is faster and several callers do it, but it
    destroys whatever the user had copied — that belongs in the caller, where
    the trade-off is visible, with `desktop.clipboard_get/set` to save and
    restore it.
    """
    if not text:
        return
    if _is_wayland():
        for char in text:
            sym = _keysym(char) if len(char) == 1 else _keysym(_canonical(char))
            _remote.keysym(sym, _PRESSED)
            _remote.keysym(sym, _RELEASED)
            if interval:
                time.sleep(interval)
        return
    _pyautogui().write(text, interval=interval)


def key(name: str) -> None:
    """Press and release one key."""
    canon = _canonical(name)
    if _is_wayland():
        sym = _keysym(canon)
        _remote.keysym(sym, _PRESSED)
        _remote.keysym(sym, _RELEASED)
        return
    pg = _pyautogui()
    pg.press(_pyautogui_name(pg, canon))


def hotkey(*names: str) -> None:
    """A chord: hold each key in order, then release in the reverse order.

    Reverse order is not cosmetic. Releasing Ctrl before A in Ctrl+A delivers a
    bare A to the application, which types a letter into whatever was focused
    instead of selecting anything.
    """
    canon = [_canonical(n) for n in names if str(n).strip()]
    if not canon:
        return
    if _is_wayland():
        syms = [_keysym(n) for n in canon]      # resolve all before pressing any
        pressed: list[int] = []
        try:
            for sym in syms:
                _remote.keysym(sym, _PRESSED)
                pressed.append(sym)
        finally:
            # Even if a press failed halfway: a modifier left down is a keyboard
            # the user has to fix by hand.
            for sym in reversed(pressed):
                try:
                    _remote.keysym(sym, _RELEASED)
                except Exception:
                    pass
        return
    pg = _pyautogui()
    # Every name resolved before any key goes down, for the same reason as the
    # Wayland branch: a chord that fails halfway leaves a modifier held.
    pg.hotkey(*[_pyautogui_name(pg, n) for n in canon])


def click(x: int | None = None, y: int | None = None,
          button: str = "left", clicks: int = 1) -> None:
    """Click, optionally after moving to an absolute position.

    With no coordinates this works everywhere. With coordinates it does not work
    under Wayland, and says so — see the module docstring.
    """
    btn = str(button).lower()
    if btn not in _BUTTONS:
        raise KeyUnknown(f"Unknown mouse button {button!r}. "
                         f"Known: {', '.join(_BUTTONS)}")
    if _is_wayland():
        if x is not None or y is not None:
            raise UnsupportedOnThisPlatform(_NO_ABSOLUTE)
        code = _BUTTONS[btn]
        for _ in range(max(1, int(clicks))):
            _remote.button(code, _PRESSED)
            _remote.button(code, _RELEASED)
        return
    pg = _pyautogui()
    if x is not None and y is not None:
        pg.click(int(x), int(y), button=btn, clicks=max(1, int(clicks)))
    else:
        pg.click(button=btn, clicks=max(1, int(clicks)))


def move_to(x: int, y: int, duration: float = 0.0) -> None:
    """Move the pointer to an absolute screen position."""
    if _is_wayland():
        raise UnsupportedOnThisPlatform(_NO_ABSOLUTE)
    _pyautogui().moveTo(int(x), int(y), duration=duration)


def move_by(dx: float, dy: float) -> None:
    """Move the pointer relative to where it is.

    The one pointer motion Wayland does allow, because it needs no coordinate
    space — which is exactly why absolute motion needs a ScreenCast stream and
    this does not.
    """
    if _is_wayland():
        _remote.motion(dx, dy)
        return
    _pyautogui().moveRel(dx, dy)


def drag_to(x: int, y: int, button: str = "left", duration: float = 0.0) -> None:
    if _is_wayland():
        raise UnsupportedOnThisPlatform(_NO_ABSOLUTE)
    _pyautogui().dragTo(int(x), int(y), duration=duration, button=str(button).lower())


def scroll(clicks: int) -> None:
    """Vertical scroll. Positive is up, matching pyautogui and every caller."""
    if _is_wayland():
        # The portal's axis is positive-down, the opposite of pyautogui's.
        _remote.axis(0, -int(clicks))
        return
    _pyautogui().scroll(int(clicks))


def hscroll(clicks: int) -> None:
    """Horizontal scroll. Positive is right."""
    if _is_wayland():
        _remote.axis(1, int(clicks))
        return
    pg = _pyautogui()
    if not hasattr(pg, "hscroll"):
        raise UnsupportedOnThisPlatform(
            "pyautogui has no horizontal scroll on this platform.")
    pg.hscroll(int(clicks))


def screen_size() -> tuple[int, int]:
    """Primary display size in pixels.

    Read from the screenshot under Wayland rather than from pyautogui, whose
    answer there is the XWayland root window — frequently a different size from
    the screen the user is looking at.
    """
    if _is_wayland():
        from . import linux
        data, _ = linux.screenshot()
        return _png_size(data)
    pg = _pyautogui()
    size = pg.size()
    return int(size[0]), int(size[1])


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height out of a PNG header — 8-byte signature, then an IHDR
    chunk whose first eight payload bytes are the two dimensions, big-endian.
    Cheaper and more portable than requiring Pillow for two integers."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise UnsupportedOnThisPlatform(
            "Could not read the screen size: the portal did not return a PNG.")
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


_NO_ABSOLUTE = (
    "Absolute pointer positioning is not available under Wayland. The "
    "RemoteDesktop portal only moves the pointer relative to where it is; "
    "moving it to a coordinate needs a PipeWire stream id from a linked "
    "ScreenCast session, which is a second screen-sharing prompt that is not "
    "wired up. Relative motion, buttons, scrolling and the whole keyboard do "
    "work. Fix: use move_by(), or log into an X11 session."
)


# ── capability ───────────────────────────────────────────────────────────────

def capability_for(system: str | None = None) -> Capability:
    """What input can do on a platform — answerable about one you are not on.

    Nothing here presses a key or opens a dialog: a capability probe that asks
    the user for permission would make `desktop.report()` unrunnable, and a
    report is most useful exactly when you do not want to touch anything.
    """
    name = (system or sys.platform).lower()

    if name.startswith("win"):
        return _pyautogui_capability("pyautogui/SendInput", "")
    if name in ("darwin", "macos"):
        return _pyautogui_capability(
            "pyautogui/Quartz",
            "needs Accessibility permission in System Settings")

    if not _session_is_wayland():
        return _pyautogui_capability("pyautogui/XTEST", "")

    from . import portal

    version = portal.interface_version("org.freedesktop.portal.RemoteDesktop")
    if version is None:
        return Capability(
            "input", False, "none",
            "Wayland with no RemoteDesktop portal. XTEST would only reach "
            "XWayland windows, so it is not offered. Fix: install "
            "xdg-desktop-portal-gnome (or -kde, -wlr).")

    detail = (f"portal v{version}; keyboard, buttons, relative motion and "
              f"scroll. No absolute pointer positioning — see input.py. "
              f"First use asks for permission once.")
    if not _installed("jeepney"):
        return Capability("input", False, "portal/RemoteDesktop",
                          "portal is there but jeepney is not. Fix: pip install jeepney")
    return Capability("input", True, "portal/RemoteDesktop", detail)


def _pyautogui_capability(backend: str, detail: str) -> Capability:
    if not _installed("pyautogui"):
        return Capability("input", False, "none", "pip install pyautogui")
    return Capability("input", True, backend, detail)


def _installed(module: str) -> bool:
    """Present on the machine, without importing it.

    find_spec is the whole point: importing pyautogui to ask whether pyautogui
    is importable opens an X display as a side effect, which is the thing being
    asked about.
    """
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def capability() -> Capability:
    return capability_for()
