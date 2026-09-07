"""
The confirmation gate is wired to the dispatch, and stays wired.

WHAT WAS BROKEN
    core/confirm.py issued a token the model cannot forge — used in one place.
    ToolSpec carried `irreversible` — populated only by a test. And
    `JarvisLive._execute_tool` consulted neither, so `shutdown_jarvis` reached
    `os._exit(0)` on the model's word alone, and `dev_agent` wrote generated
    code, pip-installed packages that code's own tracebacks named, and ran the
    result.

    Every piece was correct. Nothing joined them. That is the failure this file
    exists to notice, because it is invisible in every individual file.

THE TWO THINGS IT PINS
    1. Coverage — every declared tool has a verdict, so a tool added next month
       cannot arrive unclassified and inherit a default nobody thought about.
    2. Restraint — the gate stays *small*. A test that only checked "dangerous
       things are gated" would be satisfied by gating everything, which trains
       the user to press CONFIRM without reading. That is not consent, it just
       looks like it, so the reversible tools are asserted un-gated too.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import confirm, tool_policy  # noqa: E402


@pytest.fixture(scope="module")
def declared_names() -> set[str]:
    import main
    return {t["name"] for t in main.TOOL_DECLARATIONS}


# ── 1. coverage ──────────────────────────────────────────────────────────────

def test_every_declared_tool_has_a_verdict(declared_names):
    """RUN is the default for *unknown* tools — plugins, mostly — and that is
    deliberate. It must not be how a shipped tool gets classified."""
    unruled = sorted(n for n in declared_names if n not in tool_policy._RULES)
    assert not unruled, (
        "these tools are declared to the model but have no rule in "
        f"core/tool_policy.py, so they silently inherit RUN: {unruled}"
    )


def test_no_rule_names_a_tool_that_does_not_exist(declared_names):
    """A rule for a renamed tool is worse than no rule: it reads as coverage."""
    orphan = sorted(n for n in tool_policy._RULES if n not in declared_names)
    assert not orphan, (
        f"rules for tools that are not declared anywhere: {orphan}"
    )


def test_every_verdict_is_one_of_the_three(declared_names):
    for name in declared_names:
        verdict = tool_policy.classify(name, {})
        assert verdict.gate in tool_policy.VERDICTS, \
            f"{name}: unknown gate {verdict.gate!r}"


def test_every_rule_explains_itself(declared_names):
    """A gate nobody can explain is a gate nobody will maintain correctly. This
    is the same discipline as Capability.detail in core/desktop."""
    for name in declared_names:
        verdict = tool_policy.classify(name, {})
        assert verdict.reason, f"{name}: no reason given for gate {verdict.gate}"
        if verdict.gate == tool_policy.CONFIRM:
            assert verdict.title, f"{name}: CONFIRM with no banner title"
            assert verdict.detail, (
                f"{name}: CONFIRM with no detail — the user would be asked to "
                f"agree to a headline")


# ── 2. what is gated, and what deliberately is not ───────────────────────────

def test_shutting_alexio_down_asks_first():
    """It ends in os._exit(0). It used to fire the moment the model asked."""
    assert tool_policy.classify("shutdown_jarvis", {}).gate == tool_policy.CONFIRM


def test_building_and_running_a_generated_project_asks_first():
    assert tool_policy.classify("dev_agent", {"description": "a todo app"}).gate \
        == tool_policy.CONFIRM


@pytest.mark.parametrize("action", ["run", "build"])
def test_executing_generated_code_asks_first(action):
    assert tool_policy.classify("code_helper", {"action": action}).gate \
        == tool_policy.CONFIRM


@pytest.mark.parametrize("action", ["write", "edit", "explain", "optimize"])
def test_writing_and_reading_code_does_not(action):
    """These touch files, and files come back. Asking here would be the start of
    asking about everything."""
    assert tool_policy.classify("code_helper", {"action": action}).gate \
        == tool_policy.RUN


@pytest.mark.parametrize("flag", [True, "true", "True", "yes", "1"])
def test_an_update_that_powers_the_machine_off_asks_first(flag):
    """It hides behind a boolean nobody says out loud, which is exactly why it
    was never gated."""
    verdict = tool_policy.classify("game_updater",
                                   {"action": "update", "shutdown_when_done": flag})
    assert verdict.gate == tool_policy.CONFIRM


@pytest.mark.parametrize("args", [{}, {"action": "update"},
                                  {"shutdown_when_done": False},
                                  {"shutdown_when_done": "false"}])
def test_an_ordinary_update_does_not(args):
    assert tool_policy.classify("game_updater", args).gate == tool_policy.RUN


@pytest.mark.parametrize("name", [
    "open_app", "web_search", "weather_report", "send_message", "reminder",
    "youtube_video", "browser_control", "file_controller", "desktop_control",
    "computer_control", "flight_finder", "file_processor", "system_status",
    "manage_monitor", "save_memory", "recall_memory", "undo",
    "screen_process", "close_camera",
])
def test_the_reversible_tools_are_not_gated(name):
    """The restraint half.

    file_controller deletes to the trash and pushes nine undos; computer_control
    types and clicks, which is an annoyance rather than a loss. R-05 says the
    criterion is reversibility, not how alarming the verb sounds — and a gate on
    all of these is how an assistant becomes one nobody uses.
    """
    assert tool_policy.classify(name, {}).gate == tool_policy.RUN


def test_the_confirm_list_stays_short(declared_names):
    """A ratchet, not a rule. If a third of the tools end up gated, the design
    has drifted and the banner has stopped meaning anything."""
    gated = [n for n in declared_names
             if tool_policy.classify(n, {}).gate == tool_policy.CONFIRM]
    assert len(gated) <= 4, (
        f"{len(gated)} of {len(declared_names)} tools ask by default: {sorted(gated)}. "
        f"Every question costs a round trip and teaches the user to click yes.")


# ── 3. the modules that gate themselves really do ────────────────────────────

def test_a_self_gated_module_actually_calls_the_gate():
    """SELF says "this module asks on its own, do not ask twice". That is a
    claim about someone else's source, so it is checked against it — otherwise
    a refactor that removed the module's own gate would leave the tool
    completely open while the policy still said it was covered."""
    for name in tool_policy._RULES:
        if tool_policy.classify(name, {}).gate != tool_policy.SELF:
            continue
        source = (ROOT / "actions" / f"{name}.py").read_text(encoding="utf-8")
        assert "confirm.request(" in source, (
            f"core/tool_policy.py claims actions/{name}.py gates itself, and it "
            f"does not call confirm.request()")


def test_code_helper_re_enters_the_gate_after_resolving_auto():
    """`auto` cannot be classified — the intent is detected inside the module.
    If it did not come back to the gate once it knew, "run this" phrased as a
    description would be the one way around the rule."""
    source = (ROOT / "actions" / "code_helper.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "code_helper")
    body = ast.get_source_segment(source, fn) or ""

    assert "tool_policy.gate(" in body, (
        "actions/code_helper.py resolves `auto` to an action and never asks the "
        "policy about the result")
    assert body.index("_detect_intent") < body.index("tool_policy.gate("), (
        "the gate must be consulted *after* the intent is known, or it is "
        "classifying `auto` again")


# ── 4. the gate helper ───────────────────────────────────────────────────────

def test_a_run_verdict_does_not_touch_the_interface(ui):
    """`gate` returns None and the caller runs the handler itself. Anything else
    would put core/tool_policy.py in charge of threads, which it has no business
    knowing about."""
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    ran = []

    assert tool_policy.gate("open_app", {"app_name": "x"},
                            lambda: ran.append(1) or "ok") is None
    assert ran == [], "gate must not run the handler; the caller does"
    assert ui.confirms == []


def test_a_confirm_verdict_parks_the_call_and_runs_nothing(ui):
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    ran = []

    parked = tool_policy.gate("shutdown_jarvis", {},
                              lambda: ran.append(1) or "done")

    assert parked and "CONFIRMATION_PENDING" in parked
    assert ran == [], "the action ran before anyone agreed to it"
    assert confirm.pending_title() == "Shut Alexio down"


def test_the_handler_runs_only_after_a_human_presses_confirm(ui):
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    ran = []

    tool_policy.gate("shutdown_jarvis", {}, lambda: ran.append(1) or "done")
    assert ran == []

    confirm.resolve(True)
    _join_confirm_workers()
    assert ran == [1]


def test_cancelling_runs_nothing(ui):
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    ran = []

    tool_policy.gate("dev_agent", {"description": "x"},
                     lambda: ran.append(1) or "done")
    confirm.resolve(False)
    _join_confirm_workers()
    assert ran == []


def test_without_an_interface_nothing_irreversible_runs():
    """R-04's floor. Headless, or a call that arrives before the UI is bound:
    refuse, do not perform."""
    confirm.bind(show=None, hide=None, log=None)
    ran = []

    parked = tool_policy.gate("shutdown_jarvis", {},
                              lambda: ran.append(1) or "done")
    assert parked and "not available" in parked
    assert ran == []


def test_a_second_confirmation_does_not_stack_a_second_banner(ui):
    """Two banners for two requests means the user answers one and the other is
    silently still armed."""
    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)

    first = tool_policy.gate("shutdown_jarvis", {}, lambda: "a")
    second = tool_policy.gate("dev_agent", {"description": "x"}, lambda: "b")

    assert "CONFIRMATION_PENDING" in first
    assert "already a confirmation waiting" in second
    assert confirm.pending_title() == "Shut Alexio down"


def _join_confirm_workers() -> None:
    """core/confirm.py runs the callable on a daemon thread so the Qt thread is
    never blocked by a shutdown. Tests have to wait for it."""
    import threading

    for thread in threading.enumerate():
        if thread.name.startswith("confirm-"):
            thread.join(timeout=5)


# ── 5. the dispatch really consults it ───────────────────────────────────────

def test_execute_tool_consults_the_policy():
    """The whole point. A dispatch that does not ask is where this started."""
    import main

    source = inspect.getsource(main.JarvisLive._execute_tool)
    assert "tool_policy.gate(" in source, (
        "JarvisLive._execute_tool does not consult core/tool_policy.py — which "
        "is exactly the state this module was written to end")


def test_every_gated_tool_is_reachable_through_the_gate(declared_names):
    """A CONFIRM verdict is worthless if that tool's branch bypasses the table.

    `shutdown_jarvis` is the case: it cannot live in `_sync_handlers` because
    the work is a coroutine, so it calls the gate by hand — and this is what
    notices if that hand-written call is ever dropped.
    """
    import main

    table = set(main.JarvisLive._sync_handlers(_StubLive(), {}))
    source = inspect.getsource(main.JarvisLive._execute_tool)

    for name in declared_names:
        if tool_policy.classify(name, {}).gate != tool_policy.CONFIRM:
            continue
        if name in table:
            continue
        branch = source.split(f'name == "{name}"')
        assert len(branch) > 1, f"{name} is gated but has no dispatch at all"
        assert "tool_policy.gate(" in branch[1].split("elif name ==")[0], (
            f"{name} needs confirmation, is not in the handler table, and its "
            f"own branch never calls the gate — so it runs unasked")


class _StubLive:
    """Enough of JarvisLive to build the handler table without a session."""
    ui = None

    def speak(self, *_a, **_kw):   # pragma: no cover - never called
        raise AssertionError("the handler table must not run anything")
