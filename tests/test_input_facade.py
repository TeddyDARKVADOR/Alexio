"""
R-09, enforced — plus the behaviour of the facade that makes it followable.

WHY R-09 WAS DECORATIVE UNTIL NOW
    "An action never calls pyautogui directly" was in CLAUDE.md from the start,
    and nine action modules made 140 direct pyautogui calls anyway. Not
    carelessness: `core/desktop` exposed screenshot, brightness, volume, trash,
    clipboard, notify and wallpaper, and nothing for the keyboard or the mouse.
    `linux.input_capability()` described a capability the facade did not offer.
    An action that needed to press a key had nowhere else to go.

    So the rule needed two things it did not have: somewhere to go
    (core/desktop/input.py) and something that notices when someone doesn't
    (this file). The pattern is tests/test_tool_wiring.py's — an explicit
    exemption list, and a second test that fails when a name on it is no longer
    needed, so the list can never quietly become history.

WHAT THE SECOND HALF IS FOR
    The rule is worth nothing if the facade is wrong. The Wayland path cannot be
    exercised here — this suite runs with no compositor and no permission dialog
    — so it is driven against a recording stand-in for the portal session, which
    is enough to pin the things that are silent when they break: the release
    order of a chord, the keysym of every key name, and a chord that fails
    halfway leaving no modifier held down.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.desktop import input as di  # noqa: E402


# ── 1. the rule ──────────────────────────────────────────────────────────────
#
# Packages that inject keystrokes or pointer events. Reading window geometry is
# a different surface: `pygetwindow` is not here, because listing windows is not
# input and the facade does not claim to do it.
_INPUT_LIBRARIES = {"pyautogui", "pynput", "ydotool", "wtype", "pydirectinput",
                    "keyboard", "mouse", "autopy"}

# Files allowed to touch one directly. core/desktop/ is the implementation, so
# it is not an exemption — it is the destination. This list was every action
# module that pressed a key; it is empty, and the test below keeps it honest.
_INPUT_EXEMPT: set[str] = set()


def _project_files():
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).parts
        if parts[0] in (".venv", "tests", "__pycache__", "logs", "build"):
            continue
        yield path


def _input_library_uses(path: Path) -> list[str]:
    """Lines that import an input library or call through one.

    Both halves matter. An import is the obvious form; `pyautogui.press(...)`
    against a module imported somewhere else is the form that survives a
    search-and-replace of the import line.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    hits: list[str] = []
    for node in ast.walk(tree):
        mods: set[str] = set()
        if isinstance(node, ast.Import):
            mods = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = {node.module.split(".")[0]}
        elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
              and node.value.id in _INPUT_LIBRARIES):
            hits.append(f"line {node.lineno}: {node.value.id}.{node.attr}")
            continue
        found = mods & _INPUT_LIBRARIES
        if found:
            hits.append(f"line {node.lineno}: import {sorted(found)[0]}")
    return hits


def test_no_module_outside_core_desktop_injects_input():
    """R-09. The facade is `from core import desktop`; everything it offers is
    in core/desktop/input.py."""
    offenders: list[str] = []
    for path in _project_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("core/desktop/") or rel in _INPUT_EXEMPT:
            continue
        offenders += [f"{rel}:{hit}" for hit in _input_library_uses(path)]

    assert not offenders, (
        "An input library is used outside core/desktop/. Under Wayland these "
        "reach XWayland windows only — they succeed and deliver nothing. Use "
        "desktop.type_text / key / hotkey / click / scroll instead:\n  "
        + "\n  ".join(offenders)
    )


def test_the_input_exemption_list_does_not_grow_stale():
    """A file that no longer needs the exemption must leave the list, so the
    list always names real remaining work rather than old history."""
    stale = [rel for rel in _INPUT_EXEMPT if not _input_library_uses(ROOT / rel)]
    assert not stale, (
        f"these no longer use an input library — drop them from "
        f"_INPUT_EXEMPT: {stale}"
    )


def test_the_facade_offers_every_surface_the_report_claims():
    """The bug underneath R-09's decorativeness, stated as a test.

    `desktop.capabilities()` reports an "input" surface. If the facade does not
    expose the calls to use it, actions go around it — which is precisely how
    140 direct calls accumulated while the rule sat in CLAUDE.md.
    """
    from core import desktop

    for name in ("type_text", "key", "hotkey", "click", "move_to", "move_by",
                 "drag_to", "scroll", "hscroll", "screen_size"):
        assert name in desktop.__all__, f"desktop.{name} is not exported"
        assert callable(getattr(desktop, name)), f"desktop.{name} is not callable"

    assert "input" in desktop.SURFACES
    assert desktop.capabilities()["input"].name == "input"


# ── 2. key names ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("alias,canonical", [
    ("win", "super"), ("cmd", "super"), ("command", "super"), ("meta", "super"),
    ("control", "ctrl"), ("CTRL", "ctrl"), (" Ctrl ", "ctrl"),
    ("option", "alt"), ("esc", "escape"), ("del", "delete"),
    ("pgup", "pageup"), ("page_down", "pagedown"), ("printscreen", "print_screen"),
    ("return", "enter"), ("mute", "volumemute"),
])
def test_one_key_has_one_name(alias, canonical):
    """actions/computer_settings.py used "win", "super", "cmd" and "command"
    within thirty lines, each behind an OS branch. They are the same key."""
    assert di._canonical(alias) == canonical


def test_every_named_key_resolves_to_a_keysym():
    for name in di._KEYSYMS:
        assert isinstance(di._keysym(name), int)


def test_characters_are_their_own_keysym():
    """The X11 rule, and the reason typing is layout-independent: a keysym is a
    letter, a keycode is a position on the board. Synthesising keycodes would
    type "q" on an AZERTY keyboard when asked for "a"."""
    assert di._keysym("a") == ord("a")
    assert di._keysym("0") == ord("0")
    assert di._keysym("é") == ord("é")            # Latin-1, below 0x100
    assert di._keysym("€") == 0x01000000 + ord("€")   # above it


def test_an_unknown_key_says_so_instead_of_doing_nothing():
    with pytest.raises(di.KeyUnknown, match="Unknown key"):
        di._keysym("nosuchkey")


def test_the_fn_key_explains_why_it_cannot_be_pressed():
    """It is resolved inside the keyboard controller and never reaches the OS.
    actions/computer_settings.py asked for Fn+F11 on macOS for years, and
    pyautogui accepted it and did nothing."""
    with pytest.raises(di.KeyUnknown, match="inside the keyboard"):
        di._keysym("fn")


def test_pyautogui_names_are_checked_against_what_it_accepts():
    """pyautogui.hotkey("ctrl", "nosuchkey") does not raise: keyDown returns
    early for a name it does not know, so Ctrl goes down, comes back up, and the
    call reports success having done nothing. R-12 says that is not allowed."""
    fake = mock.Mock(KEYBOARD_KEYS=["ctrl", "a", "win"])
    assert di._pyautogui_name(fake, "ctrl") == "ctrl"
    with pytest.raises(di.KeyUnknown, match="ignored it silently"):
        di._pyautogui_name(fake, "nosuchkey")


def test_macos_gets_command_and_not_win():
    """pyautogui's Quartz backend maps "win" to nothing on macOS — silently. The
    modifier every Mac chord means is Command."""
    fake = mock.Mock(KEYBOARD_KEYS=["command", "option", "win", "alt"])
    with mock.patch.object(sys, "platform", "darwin"):
        assert di._pyautogui_name(fake, "super") == "command"
        assert di._pyautogui_name(fake, "alt") == "option"
    with mock.patch.object(sys, "platform", "win32"):
        assert di._pyautogui_name(fake, "super") == "win"


# ── 3. the Wayland path ──────────────────────────────────────────────────────

class _RecordingSession:
    """Stands in for the RemoteDesktop portal session.

    The real one costs a permission dialog and a compositor, neither of which
    exists in this suite. What it records is exactly what the compositor would
    receive, which is what has to be right.
    """

    def __init__(self, fail_on: int | None = None):
        self.calls: list[tuple] = []
        self._fail_on = fail_on

    def _record(self, *call):
        self.calls.append(call)
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise RuntimeError("session ended")

    def keysym(self, sym, state):  self._record("keysym", sym, state)
    def button(self, code, state): self._record("button", code, state)
    def motion(self, dx, dy):      self._record("motion", dx, dy)
    def axis(self, axis, steps):   self._record("axis", axis, steps)


@pytest.fixture
def wayland(monkeypatch):
    """Run the facade down its Wayland branch with a recording session."""
    session = _RecordingSession()
    monkeypatch.setattr(di, "_is_wayland", lambda: True)
    monkeypatch.setattr(di, "_remote", session)
    return session


def test_a_chord_releases_in_reverse_order(wayland):
    """Not cosmetic. Releasing Ctrl before A in Ctrl+A delivers a bare A to the
    application — which types a letter into whatever was focused instead of
    selecting anything."""
    di.hotkey("ctrl", "shift", "a")

    ctrl, shift, a = di._keysym("ctrl"), di._keysym("shift"), ord("a")
    assert wayland.calls == [
        ("keysym", ctrl,  1), ("keysym", shift, 1), ("keysym", a, 1),
        ("keysym", a,     0), ("keysym", shift, 0), ("keysym", ctrl, 0),
    ]


def test_a_chord_that_fails_halfway_leaves_no_key_held(monkeypatch):
    """A modifier left down is a keyboard the user has to fix by hand — and
    they will not know why."""
    session = _RecordingSession(fail_on=2)
    monkeypatch.setattr(di, "_is_wayland", lambda: True)
    monkeypatch.setattr(di, "_remote", session)

    with pytest.raises(RuntimeError):
        di.hotkey("ctrl", "shift", "a")

    pressed = [c for c in session.calls if c[2] == 1]
    released = [c for c in session.calls if c[2] == 0]
    assert {c[1] for c in released} >= {c[1] for c in pressed if c[1] != di._keysym("shift")}, (
        "every key that went down must come back up"
    )
    assert session.calls[-1] == ("keysym", di._keysym("ctrl"), 0)


def test_an_unknown_key_in_a_chord_presses_nothing(wayland):
    """Every name is resolved before any key goes down. Otherwise Ctrl is held
    while the error propagates."""
    with pytest.raises(di.KeyUnknown):
        di.hotkey("ctrl", "nosuchkey")
    assert wayland.calls == []


def test_typing_presses_and_releases_each_character(wayland):
    di.type_text("hi")
    assert wayland.calls == [
        ("keysym", ord("h"), 1), ("keysym", ord("h"), 0),
        ("keysym", ord("i"), 1), ("keysym", ord("i"), 0),
    ]


def test_typing_nothing_does_nothing(wayland):
    di.type_text("")
    assert wayland.calls == []


def test_a_click_with_no_coordinates_works_under_wayland(wayland):
    di.click(button="right", clicks=2)
    right = di._BUTTONS["right"]
    assert wayland.calls == [
        ("button", right, 1), ("button", right, 0),
        ("button", right, 1), ("button", right, 0),
    ]


def test_an_unknown_mouse_button_is_refused(wayland):
    with pytest.raises(di.KeyUnknown, match="Unknown mouse button"):
        di.click(button="scroll_wheel_left")
    assert wayland.calls == []


@pytest.mark.parametrize("call", [
    lambda: di.click(100, 200),
    lambda: di.move_to(100, 200),
    lambda: di.drag_to(100, 200),
])
def test_absolute_pointing_refuses_under_wayland_and_says_why(call, wayland):
    """The honest half of R-06. Absolute motion needs a PipeWire stream id from
    a linked ScreenCast session, which is a second screen-sharing prompt that is
    not wired. Moving the pointer somewhere plausible and reporting success
    would be worse than refusing (R-12)."""
    with pytest.raises(di.UnsupportedOnThisPlatform) as excinfo:
        call()
    message = str(excinfo.value)
    assert "Wayland" in message
    assert "move_by" in message, "a no must say what would fix it"
    assert wayland.calls == []


def test_relative_motion_is_the_one_that_works(wayland):
    di.move_by(5, -3)
    assert wayland.calls == [("motion", 5.0, -3.0)]


def test_scroll_direction_is_not_inverted(wayland):
    """pyautogui's positive is up; the portal's axis is positive-down. Getting
    this wrong scrolls the wrong way and nothing raises."""
    di.scroll(3)
    di.hscroll(2)
    assert wayland.calls == [("axis", 0, -3), ("axis", 1, 2)]


# ── 4. capability, on every platform ─────────────────────────────────────────

@pytest.mark.parametrize("system", ["windows", "macos", "linux"])
def test_every_platform_reports_an_input_capability(system):
    from core import desktop

    cap = desktop.capabilities(system)["input"]
    assert cap.name == "input"
    assert cap.backend, f"{system}: no mechanism named"
    if not cap.available:
        assert cap.detail, f"{system}: says no without saying how to fix it"


@pytest.mark.parametrize("system,expected", [
    ("Windows", "SendInput"), ("win32", "SendInput"),
    ("Darwin", "Quartz"), ("macos", "Quartz"),
])
def test_the_mechanism_is_nameable_for_a_platform_you_are_not_on(system, expected):
    from core import desktop
    assert desktop.input_mechanism(system) == expected


def test_wayland_reports_the_portal_and_x11_reports_xtest(monkeypatch):
    from core import desktop

    monkeypatch.setattr("core.desktop.linux.is_wayland", lambda: True)
    assert desktop.input_mechanism("linux") == "portal/RemoteDesktop"
    monkeypatch.setattr("core.desktop.linux.is_wayland", lambda: False)
    assert desktop.input_mechanism("linux") == "XTEST"


def test_wayland_without_a_portal_refuses_rather_than_falling_back(monkeypatch):
    """R-12. XTEST under Wayland reaches XWayland windows only, so offering it
    as a fallback would mean an assistant that types into nothing and says it
    typed. A missing portal is a no, with the package that fixes it."""
    monkeypatch.setattr("core.desktop.linux.is_wayland", lambda: True)
    monkeypatch.setattr("core.desktop.portal.interface_version", lambda _i: None)

    cap = di.capability_for("linux")
    assert not cap.available
    assert "xdg-desktop-portal" in cap.detail


def test_the_probe_never_opens_a_display_or_a_dialog():
    """`desktop.report()` has to be runnable when everything is broken, which
    means the probe cannot import pyautogui (that opens an X display) or start a
    portal session (that shows a permission dialog)."""
    with mock.patch.dict(sys.modules):
        for system in ("windows", "macos", "linux"):
            with mock.patch.object(di, "_pyautogui",
                                   side_effect=AssertionError("probe imported pyautogui")):
                with mock.patch.object(di._RemoteDesktop, "_ensure",
                                       side_effect=AssertionError("probe started a session")):
                    di.capability_for(system)


def test_the_capability_probe_and_the_implementation_cannot_disagree():
    """linux.py used to answer "no, install ydotool" while input.py now goes
    through the portal. Two answers to one question is how a report starts
    lying, so the backends delegate."""
    from core.desktop import linux, macos, windows

    for module, name in ((linux, "linux"), (windows, "windows"), (macos, "macos")):
        source = (ROOT / "core" / "desktop" / f"{name}.py").read_text(encoding="utf-8")
        body = source.split("def input_capability()")[1]
        assert "capability_for" in body, (
            f"core/desktop/{name}.py answers input_capability() itself instead "
            f"of delegating to input.py")
        assert module.input_capability().name == "input"


# ── 5. what the portal session must not do ───────────────────────────────────

@pytest.fixture
def fake_jeepney(monkeypatch):
    """jeepney carries a `sys_platform == "linux"` marker, so it is genuinely
    absent on a Windows or macOS checkout. The session logic under test is not
    about D-Bus wire format, so a stub is enough — and it means these tests run
    on every developer machine rather than only on the target one."""
    import types

    module = types.ModuleType("jeepney")
    module.DBusAddress = mock.Mock(name="DBusAddress")
    module.new_method_call = mock.Mock(name="new_method_call")
    module.MatchRule = mock.Mock(name="MatchRule")
    module.message_bus = mock.Mock(name="message_bus")
    monkeypatch.setitem(sys.modules, "jeepney", module)
    return module


def test_the_session_is_created_once_and_reused(monkeypatch, fake_jeepney):
    """One RemoteDesktop session costs one permission dialog. Creating one per
    keystroke would make the assistant unusable, so `_ensure` is idempotent."""
    remote = di._RemoteDesktop()
    created = []

    def fake_ensure(self):
        if self._session is not None:
            return
        created.append(1)
        self._session = "/session/1"
        self._conn = mock.Mock()

    monkeypatch.setattr(di._RemoteDesktop, "_ensure", fake_ensure)

    remote.keysym(0x61, 1)
    remote.keysym(0x61, 0)
    remote.button(di._BUTTONS["left"], 1)

    assert len(created) == 1, "a session was created more than once"
    assert remote._conn.send_and_get_reply.call_count == 3


def test_a_dead_session_is_dropped_so_the_next_call_rebuilds(fake_jeepney):
    """The compositor restarts, the screen locks, the user revokes the grant.
    Holding a dead handle would fail forever; the next call should ask again."""
    remote = di._RemoteDesktop()
    remote._session = "/session/1"
    conn = mock.Mock()
    conn.send_and_get_reply.side_effect = RuntimeError("no such session")
    remote._conn = conn

    with pytest.raises(di.UnsupportedOnThisPlatform, match="recreated"):
        remote.keysym(0x61, 1)

    assert remote._session is None
    assert remote._conn is None


def test_without_jeepney_the_message_names_the_package(monkeypatch):
    """Not a bare ImportError from three frames down — the sentence that fixes
    it. A missing jeepney is the normal state of a fresh Linux checkout."""
    remote = di._RemoteDesktop()
    remote._session = "/session/1"
    remote._conn = mock.Mock()

    class _Blocked:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "jeepney" or fullname.startswith("jeepney."):
                raise ModuleNotFoundError("blocked", name=fullname)
            return None

    monkeypatch.delitem(sys.modules, "jeepney", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Blocked(), *sys.meta_path])

    with pytest.raises(di.UnsupportedOnThisPlatform, match="pip install jeepney"):
        remote.keysym(0x61, 1)


def test_the_restore_token_is_written_privately(tmp_path, monkeypatch):
    """It is what lets the second launch skip the permission dialog. It is also
    a capability to control the keyboard, so it is not world-readable."""
    target = tmp_path / "alexio" / "remote_desktop.json"
    monkeypatch.setattr(di, "_STATE_DIR", target.parent)
    monkeypatch.setattr(di, "_TOKEN_PATH", target)

    di._RemoteDesktop._save_restore_token("abc123")
    assert di._RemoteDesktop._load_restore_token() == "abc123"
    if sys.platform != "win32":       # POSIX permission bits only exist there
        assert target.stat().st_mode & 0o077 == 0


def test_an_unwritable_token_does_not_fail_the_keystroke(tmp_path, monkeypatch):
    """Losing the token costs one dialog next time. Failing the keystroke costs
    the thing the user actually asked for.

    The directory is made unwritable by putting a *file* where it wants a
    directory — the one way to do that which behaves identically on Windows,
    where chmod does not.
    """
    blocker = tmp_path / "alexio"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(di, "_STATE_DIR", blocker)
    monkeypatch.setattr(di, "_TOKEN_PATH", blocker / "remote_desktop.json")

    di._RemoteDesktop._save_restore_token("abc123")     # must not raise
    assert di._RemoteDesktop._load_restore_token() == ""
