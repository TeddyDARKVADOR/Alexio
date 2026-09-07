"""
actions/ — the part that actually touches the machine, and had no tests at all.

WHERE THE COVERAGE WAS
    293 tests covered core/, dashboard/ and memory/. actions/ is ~8 000 lines
    across 20 modules, and it is the only part of Alexio that deletes a file,
    presses a key or powers something off. It had none.

    `test_tool_wiring.py` checks that every declared tool has a handler. It does
    not check that calling the handler does anything, or that the dispatch calls
    it with arguments it accepts — a keyword typo there is a TypeError that only
    appears the first time a user asks for that specific tool.

WHAT IS TESTED HERE, AND WHY THESE
    Not everything: a test that launches Steam is not a test. The choice is the
    behaviour whose failure is silent or expensive.

      · the call contract — every handler is reachable AND callable as the
        dispatch calls it
      · file_controller — the module that gets R-05 right, pinned so it stays
        right: trash instead of unlink, an undo for everything that changes,
        protected directories, and paths confined to home
      · the pure resolvers — URL normalisation and spoken-description matching,
        which are the two places a wrong answer is invisible rather than loud

NOTHING HERE TOUCHES THE REAL MACHINE
    No file outside tmp_path, no trash, no keystroke, no subprocess, no network.
    Where a module reaches for the desktop, the desktop is redirected first.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── 1. the call contract ─────────────────────────────────────────────────────

def _handler_table():
    """main.py's dispatch table, built without a Live session."""
    import main

    class _Stub:
        ui = None

        def speak(self, *_a, **_kw):    # pragma: no cover - never called
            raise AssertionError("building the table must not run anything")

    return main.JarvisLive._sync_handlers(_Stub(), {})


def test_every_dispatched_handler_is_callable_with_the_arguments_it_is_given():
    """The gap `test_tool_wiring.py` leaves open.

    That file matches tool *names*. This one binds the actual call — the same
    keywords `_sync_handlers` passes — against the real signature. A handler
    given `session_memory=` when it does not take one raises TypeError, and only
    on the first request for that tool, in production, months later.

    `Signature.bind` does the check without running anything.
    """
    import main

    class _Recorder:
        """Stands in for one action function and records how it was called.

        Substituting by *identity* rather than by name is deliberate: two tools
        are imported under a different name than the tool has
        (`weather_report` → `weather_action`, `web_search` →
        `web_search_action`), so matching names would have reported those two as
        missing and quietly skipped checking them.
        """

        def __init__(self, original):
            self.original = original
            self.calls: list[tuple] = []

        def __call__(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return "recorded"

    # Every action function main.py imported, whatever it called it.
    originals = {
        attr: value for attr, value in vars(main).items()
        if callable(value) and getattr(value, "__module__", "").startswith("actions.")
    }
    assert originals, "main.py imported no action functions — the test is wrong"

    recorders = {attr: _Recorder(value) for attr, value in originals.items()}
    failures = []

    for attr, recorder in recorders.items():
        setattr(main, attr, recorder)
    try:
        for name, handler in _handler_table().items():
            try:
                handler()
            except Exception as exc:                  # noqa: BLE001
                failures.append(f"{name}: dispatch raised {type(exc).__name__}: {exc}")
                continue

            fired = [r for r in recorders.values() if r.calls]
            if not fired:
                failures.append(f"{name}: the handler called no action function")
                continue

            args, kwargs = fired[0].calls.pop()
            try:
                inspect.signature(fired[0].original).bind(*args, **kwargs)
            except TypeError as exc:
                failures.append(
                    f"{name}: dispatch calls "
                    f"{fired[0].original.__module__}.{fired[0].original.__name__} "
                    f"wrongly — {exc}")
    finally:
        for attr, value in originals.items():
            setattr(main, attr, value)

    assert not failures, (
        "main.py dispatches to these handlers with arguments they do not "
        "accept:\n  " + "\n  ".join(failures))


@pytest.mark.parametrize("module_name", [
    "browser_control", "code_helper", "computer_control", "computer_settings",
    "dev_agent", "file_controller", "file_processor", "flight_finder",
    "game_updater", "open_app", "reminder", "send_message", "web_search",
    "youtube_video",
])
def test_every_action_entry_point_takes_parameters_first_and_returns_a_string(module_name):
    """R-03's core: `parameters` is the first argument and the result is a str.

    Three modules — file_processor, flight_finder, game_updater — take
    `(parameters, player, speak)` and not the full
    `(parameters, response, player, session_memory, speak)` R-03 spells out.
    That is why `_sync_handlers` passes different keywords per tool instead of
    one uniform call. It is recorded in CLAUDE.md rather than papered over here:
    this test asserts what is actually true, so it cannot quietly get worse.
    """
    import importlib

    module = importlib.import_module(f"actions.{module_name}")
    fn = getattr(module, module_name)
    sig = inspect.signature(fn)

    first = next(iter(sig.parameters))
    assert first == "parameters", \
        f"actions/{module_name}.py: first argument is {first!r}, not 'parameters'"
    assert sig.return_annotation in (str, "str"), \
        f"actions/{module_name}.py: does not declare -> str"

    for pname, param in list(sig.parameters.items())[1:]:
        assert param.default is not inspect.Parameter.empty, (
            f"actions/{module_name}.py: {pname} has no default, so the dispatch "
            f"cannot omit it")


# ── 2. file_controller — the module that gets R-05 right ─────────────────────

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point file_controller at a throwaway home, and make the trash a recorder.

    Both halves matter. Redirecting `_SAFE_ROOTS` without redirecting the
    shortcut folders would leave "desktop" pointing at the real Desktop; and a
    test that actually trashed a file would depend on the developer's recycle
    bin.
    """
    from actions import file_controller as fc
    from core import desktop

    home = tmp_path / "home"
    for sub in ("Desktop", "Downloads", "Documents", "Pictures", "Music", "Videos"):
        (home / sub).mkdir(parents=True)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(fc, "_SAFE_ROOTS", [home])

    trashed: list[Path] = []
    monkeypatch.setattr(desktop, "trash", lambda p: trashed.append(Path(p)) or True)

    return type("Sandbox", (), {"home": home, "desktop": home / "Desktop",
                                "trashed": trashed})()


def test_delete_moves_to_the_trash_and_never_unlinks(sandbox):
    """A voice assistant mishears. "Delete the report" must cost a trip to the
    trash, not the file."""
    from actions.file_controller import file_controller

    victim = sandbox.desktop / "report.txt"
    victim.write_text("months of work", encoding="utf-8")

    result = file_controller({"action": "delete", "path": str(sandbox.desktop),
                              "name": "report.txt"})

    assert "Trash" in result
    assert sandbox.trashed == [victim.resolve()], "desktop.trash was not called"
    assert victim.exists(), (
        "the file was removed as well as trashed — _safe_trash must never unlink")


def test_delete_pushes_an_undo(sandbox):
    from actions.file_controller import file_controller
    from core import undo

    (sandbox.desktop / "report.txt").write_text("x", encoding="utf-8")
    file_controller({"action": "delete", "path": str(sandbox.desktop),
                     "name": "report.txt"})

    assert any("report.txt" in entry for entry in undo.history()), (
        f"nothing undoable was recorded: {undo.history()}")


@pytest.mark.parametrize("folder", ["Desktop", "Downloads", "Documents",
                                    "Pictures", "Music", "Videos"])
def test_the_user_folders_themselves_cannot_be_deleted(sandbox, folder):
    """Deleting a *file* on the Desktop is routine. Deleting the Desktop is a
    misheard sentence."""
    from actions.file_controller import file_controller

    result = file_controller({"action": "delete", "path": str(sandbox.home / folder)})

    assert "Protected" in result
    assert sandbox.trashed == []


def test_home_itself_cannot_be_deleted(sandbox):
    from actions.file_controller import file_controller

    result = file_controller({"action": "delete", "path": str(sandbox.home)})
    assert "Protected" in result
    assert sandbox.trashed == []


def test_nothing_outside_home_can_be_touched(sandbox, tmp_path):
    """_SAFE_ROOTS is the whole confinement. A path that escapes it is refused
    before anything is opened."""
    from actions.file_controller import file_controller

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours", encoding="utf-8")

    for action in ("delete", "read", "list"):
        result = file_controller({"action": action, "path": str(outside),
                                  "name": "secret.txt" if action != "list" else ""})
        assert "Access denied" in result, f"{action} reached outside home: {result}"
    assert sandbox.trashed == []


def test_a_traversal_path_does_not_escape(sandbox):
    """"desktop/../../../etc" resolves out of home, and resolution happens
    before the check — which is the only order that works."""
    from actions.file_controller import file_controller

    escape = str(sandbox.desktop / ".." / ".." / ".." / "etc")
    result = file_controller({"action": "list", "path": escape})
    assert "Access denied" in result


def test_write_can_be_taken_back(sandbox):
    """Overwriting a file is reversible if the previous contents were kept, and
    R-05 says reversible means do it now and record the undo."""
    from actions.file_controller import file_controller
    from core import undo

    target = sandbox.desktop / "notes.txt"
    target.write_text("original", encoding="utf-8")

    file_controller({"action": "write", "path": str(sandbox.desktop),
                     "name": "notes.txt", "content": "replaced"})
    assert target.read_text(encoding="utf-8") == "replaced"

    undo.undo_last()
    assert target.read_text(encoding="utf-8") == "original", \
        "undo did not restore the previous contents"


def test_rename_can_be_taken_back(sandbox):
    from actions.file_controller import file_controller
    from core import undo

    original = sandbox.desktop / "before.txt"
    original.write_text("x", encoding="utf-8")

    file_controller({"action": "rename", "path": str(sandbox.desktop),
                     "name": "before.txt", "new_name": "after.txt"})
    assert (sandbox.desktop / "after.txt").exists()

    undo.undo_last()
    assert original.exists(), "undo did not put the name back"


def test_an_unknown_action_is_a_sentence_not_an_exception(sandbox):
    """The model writes this string. A malformed one must not take down the
    tool call."""
    from actions.file_controller import file_controller

    result = file_controller({"action": "obliterate", "path": str(sandbox.desktop)})
    assert isinstance(result, str)
    assert "Unknown action" in result


def test_no_parameters_at_all_is_survivable(sandbox):
    from actions.file_controller import file_controller

    assert isinstance(file_controller(None), str)
    assert isinstance(file_controller({}), str)


def test_permanent_deletion_only_ever_appears_inside_an_undo():
    """Read from the source: the guarantee is that no forward code path removes
    anything, which is stronger than any one call not doing so.

    `unlink` and `rmtree` do appear in this file, four times, and all four are
    correct: the undo for "create this file" is removing that file, and the undo
    for "copy this" is removing the copy — never the original. So the rule is
    not "the words must not appear", it is "they may only appear in code that is
    reversing something Alexio itself did". A blunter assertion would have had
    to be deleted the first time someone read it, which is how a safety test
    stops being one.
    """
    import ast

    path = ROOT / "actions" / "file_controller.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Nearest enclosing function for every node, so a closure inside copy_file
    # is judged by the closure's name and not by copy_file's.
    enclosing: dict[int, str] = {}

    def walk(node, current: str):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef) else current
            enclosing[id(child)] = name
            walk(child, name)

    walk(tree, "<module>")

    removers = {"unlink", "rmtree", "remove", "rmdir"}
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in removers:
            continue
        where = enclosing.get(id(node), "<module>")
        if where.startswith("_undo") or where == "_fn":
            continue
        offenders.append(f"line {node.lineno}: {node.func.attr}() in {where}()")

    assert not offenders, (
        "actions/file_controller.py removes something outside an undo — "
        "deletion must go through the trash and nowhere else:\n  "
        + "\n  ".join(offenders))


# ── 3. the pure resolvers ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("instagram",         "https://instagram.com"),
    ("instagram.com",     "https://instagram.com"),
    ("https://x.com/a",   "https://x.com/a"),
    ("http://x.com",      "http://x.com"),
    ("  github.com  ",    "https://github.com"),
    ("",                  "about:blank"),
])
def test_a_spoken_site_name_becomes_a_url(raw, expected):
    """The model hands over whatever the user said. "Open instagram" has no
    scheme and no dot, and the browser needs both."""
    from actions.browser_control import _normalize_url

    assert _normalize_url(raw) == expected


@pytest.mark.parametrize("said,action", [
    ("volume_up",          "volume_up"),      # already an action name
    ("turn the volume up", "volume_up"),      # alias phrase
    ("mute",               "mute"),
    ("brighter",           "brightness_up"),
    ("fullscren",          "fullscreen"),     # fuzzy: a real mishearing
    ("full screen",        "full_screen"),
])
def test_a_spoken_description_resolves_without_a_model(said, action):
    """This used to be a second Gemini call made *inside* the tool: every "turn
    it down" paid for two round trips, and the fallback when that call failed
    was `description.lower().replace(" ", "_")` — which turns any non-English
    phrasing straight into "Unknown action"."""
    from actions.computer_settings import _detect_action

    assert _detect_action(said)["action"] == action


def test_a_resolved_action_is_one_the_module_can_actually_run():
    """The resolver has five strategies, two of them fuzzy. A near-match that
    lands on a name with no handler would turn a mishearing into "Unknown
    action" — which is the dead end the resolver replaced."""
    from actions.computer_settings import (ACTION_MAP, _VALUE_ACTIONS,
                                           _detect_action)

    runnable = set(ACTION_MAP) | _VALUE_ACTIONS
    phrases = ["volume up", "turn it down", "mute", "brighter", "darker",
               "full screen", "fullscren", "close this window", "new tab",
               "copy", "paste", "select all", "save", "lock the screen"]

    for phrase in phrases:
        resolved = _detect_action(phrase)["action"]
        if resolved:
            assert resolved in runnable, (
                f"{phrase!r} resolved to {resolved!r}, which has no handler")


def test_a_number_next_to_a_volume_word_becomes_a_level():
    from actions.computer_settings import _detect_action

    assert _detect_action("set volume to 30") == {"action": "volume_set", "value": 30}
    assert _detect_action("sesi 70 yap")["value"] == 70


def test_a_volume_level_is_clamped():
    """The model can say 400. The mixer cannot."""
    from actions.computer_settings import _detect_action

    assert _detect_action("set the volume to 400")["value"] == 100


def test_nothing_recognisable_resolves_to_nothing():
    """An empty action is turned into a message naming real candidates, which is
    one round trip. Guessing would be zero round trips and the wrong action."""
    from actions.computer_settings import _detect_action

    assert _detect_action("")["action"] == ""
    assert _detect_action("qwertyuiop asdfgh")["action"] == ""


# ── 4. the irreversible settings still ask ───────────────────────────────────

@pytest.mark.parametrize("action", ["shutdown", "restart", "toggle_wifi"])
def test_the_dangerous_settings_wait_for_a_button(ui, action):
    """This is the one gate that already existed. It is pinned here because
    core/tool_policy.py now marks computer_settings as SELF-gated — that claim
    is only true while this keeps passing."""
    from actions.computer_settings import computer_settings
    from core import confirm

    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    result = computer_settings({"action": action})

    assert "CONFIRMATION_PENDING" in result
    assert len(ui.confirms) == 1
    assert confirm.pending_title()


def test_turning_the_wifi_off_is_gated_even_though_it_sounds_harmless(ui):
    """The one that was missed for exactly that reason: it cuts the assistant's
    own connection, so it cannot be asked to turn it back on."""
    from actions.computer_settings import computer_settings
    from core import confirm

    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    computer_settings({"action": "toggle_wifi"})

    title, detail = ui.confirms[0]
    assert "WiFi" in title
    assert "connection" in detail.lower()
