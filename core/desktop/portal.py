"""
core/desktop/portal.py — xdg-desktop-portal over D-Bus, in pure Python.

WHY THIS EXISTS
    Wayland does not let an application read the screen or move the pointer just
    because it asked. Those go through org.freedesktop.portal.*, which is the
    compositor's own doorway: the user grants access once, and the portal hands
    back the result.

    On the target machine — Fedora 43, GNOME 49, Wayland — this is not one
    option among several. `mss` returns a black image and `pyautogui` reaches
    only XWayland windows. The portal is the way screen capture works now.

WHY jeepney AND NOT PyGObject
    PyGObject is a compiled binding that has to match the system's GLib. It is
    present on Fedora's own Python 3.14 and absent from the project's pyenv
    3.11 venv, and building it there drags in gobject-introspection headers.
    jeepney is pure Python, has no dependencies, and speaks D-Bus directly.
    That is rule R-10, and this file is why the rule exists.

THE REQUEST PATTERN
    Portal methods do not return results. They return the object path of a
    Request, and the answer arrives later as a Response signal on that path. The
    match rule therefore has to be installed *before* the call — a portal that
    answers quickly would otherwise deliver the signal into a void, and the call
    would hang until its timeout for no reason at all.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

PORTAL_BUS  = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"

DEFAULT_TIMEOUT = 30.0


class PortalError(RuntimeError):
    """The portal refused, was not there, or the user cancelled."""


def available() -> bool:
    """True when a session bus and a desktop portal are both reachable."""
    if sys.platform != "linux":
        return False
    try:
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return False
    try:
        conn = open_dbus_connection(bus="SESSION")
    except Exception:
        return False
    try:
        return _interface_version(conn, "org.freedesktop.portal.Screenshot") is not None
    except Exception:
        return False
    finally:
        conn.close()


def _interface_version(conn, interface: str) -> int | None:
    from jeepney import DBusAddress, new_method_call

    addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS,
                       interface="org.freedesktop.DBus.Properties")
    msg = new_method_call(addr, "Get", "ss", (interface, "version"))
    try:
        reply = conn.send_and_get_reply(msg, timeout=5)
    except Exception:
        return None
    try:
        return int(reply.body[0][1])
    except Exception:
        return None


def interface_version(interface: str) -> int | None:
    """Version of one portal interface, or None when it is not offered."""
    if sys.platform != "linux":
        return None
    try:
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return None
    try:
        conn = open_dbus_connection(bus="SESSION")
    except Exception:
        return None
    try:
        return _interface_version(conn, interface)
    finally:
        conn.close()


def _handle_token() -> str:
    return "alexio_" + secrets.token_hex(8)


def _expected_request_path(conn, token: str) -> str:
    """Where the Response will arrive.

    The portal derives the Request path from the caller's unique bus name with
    the dots and the leading colon stripped. Computing it lets the match rule go
    in before the call, which closes the race the docstring above describes.
    """
    sender = conn.unique_name.lstrip(":").replace(".", "_")
    return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"


def call(interface: str, method: str, signature: str, body: tuple,
         timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Invoke one portal method and wait for its Response.

    `body` must already contain the options dict, with `handle_token` left out —
    this adds it, because it also has to know the token to predict the path.

    Returns the results dict. Raises PortalError when the user cancelled
    (response code 1), when the portal ended it another way (2), or on timeout.
    """
    if sys.platform != "linux":
        raise PortalError("Desktop portals are a Linux mechanism.")
    try:
        from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError as e:
        raise PortalError(
            "jeepney is not installed, so the desktop portal cannot be reached. "
            "Run: pip install jeepney"
        ) from e

    token = _handle_token()

    # Inject handle_token into the options dict, which portal methods always
    # take last.
    body = list(body)
    options = dict(body[-1][1]) if body and isinstance(body[-1], tuple) else {}
    options["handle_token"] = ("s", token)
    body[-1] = ("a{sv}", options)
    body = tuple(body)

    try:
        conn = open_dbus_connection(bus="SESSION")
    except Exception as e:
        raise PortalError(f"No D-Bus session bus: {e}") from e

    try:
        request_path = _expected_request_path(conn, token)

        rule = MatchRule(
            type="signal",
            interface="org.freedesktop.portal.Request",
            member="Response",
            path=request_path,
        )
        # Before the call, deliberately. See the module docstring.
        conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=5)

        with conn.filter(rule) as queue:
            addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS,
                               interface=f"org.freedesktop.portal.{interface}")
            msg = new_method_call(addr, method, signature, body)
            try:
                conn.send_and_get_reply(msg, timeout=timeout)
            except Exception as e:
                raise PortalError(f"{interface}.{method} was refused: {e}") from e

            try:
                signal = conn.recv_until_filtered(queue, timeout=timeout)
            except Exception as e:
                raise PortalError(
                    f"{interface}.{method} did not answer within {timeout:.0f}s."
                ) from e

        code, results = signal.body
        if code == 1:
            raise PortalError(f"{interface}.{method}: you cancelled the request.")
        if code != 0:
            raise PortalError(f"{interface}.{method} ended with code {code}.")

        # jeepney gives variants as (signature, value) pairs; unwrap them.
        return {k: (v[1] if isinstance(v, tuple) and len(v) == 2 else v)
                for k, v in dict(results).items()}
    finally:
        conn.close()


# ── the calls Alexio actually makes ──────────────────────────────────────────

def screenshot(interactive: bool = False, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Capture the screen and return PNG bytes.

    `interactive=False` works without a dialog *after* the user has granted the
    permission once. The first call shows GNOME's share prompt; every one after
    it is silent. That is why this is usable for a voice assistant at all.
    """
    results = call("Screenshot", "Screenshot", "sa{sv}",
                   ("", {"interactive": ("b", interactive)}), timeout=timeout)
    uri = results.get("uri")
    if not uri:
        raise PortalError("The portal returned no screenshot URI.")

    path = Path(unquote(urlparse(uri).path))
    try:
        data = path.read_bytes()
    except OSError as e:
        raise PortalError(f"Could not read the screenshot the portal wrote: {e}") from e
    finally:
        # The portal writes into a temp dir it does not clean up.
        try:
            path.unlink()
        except OSError:
            pass
    return data


def set_wallpaper(path: str | Path, timeout: float = DEFAULT_TIMEOUT) -> None:
    uri = Path(path).expanduser().resolve().as_uri()
    call("Wallpaper", "SetWallpaperURI", "ssa{sv}",
         ("", uri, {"show-preview": ("b", False),
                    "set-on": ("s", "both")}), timeout=timeout)


def trash(path: str | Path) -> bool:
    """Move a file to the desktop trash. Returns False if the portal declined.

    Not a Request-pattern call: Trash.TrashFile takes a file descriptor and
    answers immediately.
    """
    if sys.platform != "linux":
        raise PortalError("Desktop portals are a Linux mechanism.")
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError as e:
        raise PortalError("jeepney is not installed. Run: pip install jeepney") from e

    target = Path(path).expanduser().resolve()
    fd = os.open(target, os.O_RDONLY)
    try:
        conn = open_dbus_connection(bus="SESSION")
        try:
            addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS,
                               interface="org.freedesktop.portal.Trash")
            msg = new_method_call(addr, "TrashFile", "h", (fd,))
            reply = conn.send_and_get_reply(msg, timeout=10)
            return reply.body[0] == 1
        finally:
            conn.close()
    except PortalError:
        raise
    except Exception as e:
        raise PortalError(f"Trash.TrashFile failed: {e}") from e
    finally:
        os.close(fd)
