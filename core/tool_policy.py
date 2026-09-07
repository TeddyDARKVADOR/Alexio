"""
core/tool_policy.py — which tool calls need a human, decided in one place.

THE HOLE THIS FILLS
    Two mechanisms existed and were never connected to each other.

    `core/confirm.py` issues a confirmation token the model cannot forge — the
    right design, used in exactly one place in the whole repository
    (actions/computer_settings.py, for restart / shutdown / toggle_wifi).

    `ToolSpec` carries `irreversible` and `needs_confirmation` — the right
    fields, populated by `adopt_gemini_declarations(...)`, which production code
    never calls. Only a test does.

    In between sits `JarvisLive._execute_tool`: 24 `elif` branches that consult
    neither. So `dev_agent` wrote model-generated code to disk, pip-installed
    packages named by that code's own tracebacks, and executed the result — with
    no undo and no question. `shutdown_jarvis` called `os._exit(0)` the moment
    the model asked. Neither was a decision anyone made; they were what happens
    when the gate and the dispatch never meet.

THE CRITERION IS REVERSIBILITY, NOT ALARM (R-05)
    Not "does this sound dangerous". Deleting a file goes to the trash and is
    reversible, so it happens at once with an undo pushed. Turning WiFi off
    sounds mild and cuts the assistant's own connection so it cannot be asked to
    turn it back on — that one waits for a button.

    Asking about everything is how an assistant becomes one nobody uses, and
    every question costs a round trip. The CONFIRM list below is deliberately
    short, and each entry says what cannot be taken back.

WHY THE VERDICT DEPENDS ON THE ARGUMENTS
    Reversibility is a property of the call, not of the tool. `file_controller`
    reading a file and `file_controller` deleting a folder are the same tool.
    `game_updater` is harmless until `shutdown_when_done` is true. A table keyed
    on the tool name alone would either gate far too much or miss the case that
    matters, so a rule is a function of the arguments.

THREE VERDICTS, AND WHY "SELF" IS NOT "RUN"
    A module that already runs its own gate must not be gated twice — two
    banners for one action is worse than none, because the user answers the
    first and nothing happens. SELF records that the module gates itself, and
    `tests/test_tool_policy.py` checks the claim against the module's source
    rather than trusting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

RUN = "run"          # reversible or harmless — do it now, push undo if it can be
CONFIRM = "confirm"  # cannot be taken back — park behind core/confirm.py (R-04)
SELF = "self"        # the action module runs its own gate; do not ask twice

VERDICTS = (RUN, CONFIRM, SELF)


@dataclass(frozen=True)
class Verdict:
    """What to do with one tool call before running it."""

    gate:   str
    title:  str = ""       # the banner headline, if it is going on screen
    detail: str = ""       # what the user is agreeing to, in one or two lines
    reason: str = ""       # why this rule exists — for the log and for humans

    def __bool__(self) -> bool:
        """True when the call may proceed without asking."""
        return self.gate == RUN

    def __str__(self) -> str:
        return f"{self.gate:<8} {self.title or self.reason}"


# A rule sees the call's arguments and returns a Verdict.
Rule = Callable[[dict], Verdict]

_RULES: dict[str, Rule] = {}


def rule(name: str) -> Callable[[Rule], Rule]:
    def _register(fn: Rule) -> Rule:
        _RULES[name] = fn
        return fn
    return _register


def register(name: str, fn: Rule) -> None:
    """Declare a rule for a tool this module does not know about.

    Plugins load after this file, so they cannot use the decorator. Without
    this they would silently inherit the RUN default, which is the correct
    default for the tools shipped here and not a promise about someone else's.
    """
    _RULES[name] = fn


def _always(gate: str, title: str, detail: str, reason: str) -> Rule:
    verdict = Verdict(gate, title, detail, reason)
    return lambda _args: verdict


# ── the table ────────────────────────────────────────────────────────────────

_RULES["shutdown_jarvis"] = _always(
    CONFIRM,
    "Shut Alexio down",
    "The conversation ends and the session summary is written. Anything you "
    "were in the middle of is lost.",
    "os._exit(0). It was ungated: the model asked, and Alexio was gone before "
    "anyone could disagree.",
)

_RULES["dev_agent"] = _always(
    CONFIRM,
    "Build and run a generated project",
    "Alexio will write code it has generated to your Desktop, install the "
    "packages that code asks for, and run it.",
    "Three irreversible things at once: files on disk, packages in an "
    "environment, and execution of code no human has read.",
)

_RULES["computer_settings"] = _always(
    SELF,
    "", "",
    "actions/computer_settings.py::_IRREVERSIBLE gates restart, shutdown and "
    "toggle_wifi itself, after resolving the spoken description to an action. "
    "Gating here as well would put two banners on screen for one request.",
)


@rule("code_helper")
def _code_helper(args: dict) -> Verdict:
    """Writing and explaining code is free. Running it is not.

    `auto` is not gated here because it has not resolved to anything yet — the
    intent is detected inside the module. actions/code_helper.py re-enters this
    same gate once it knows, which is why `auto` is safe to let through.
    """
    action = str(args.get("action", "auto")).lower().strip()
    if action in ("run", "build"):
        return Verdict(
            CONFIRM,
            "Run generated code",
            f"Alexio will execute code with `{action}` on this machine. "
            f"It has not been read by a human.",
            "subprocess.run on model-authored code.",
        )
    return Verdict(RUN, reason="writing, editing and explaining touch files "
                               "that file_controller can put back")


@rule("game_updater")
def _game_updater(args: dict) -> Verdict:
    """Updating a game is a download. Powering the machine off afterwards is
    not, and it hides behind a boolean nobody reads out loud."""
    flag = args.get("shutdown_when_done")
    if str(flag).lower() in ("true", "1", "yes"):
        return Verdict(
            CONFIRM,
            "Shut the computer down when the update finishes",
            "The update runs, then this computer powers off. Anything unsaved "
            "at that point is lost.",
            "schedules a real machine shutdown from a parameter, not a verb.",
        )
    return Verdict(RUN, reason="a download, and the launcher can cancel it")


# ── the tools that run, and why each one is allowed to ───────────────────────
#
# Written out rather than left to the default. A shipped tool inheriting RUN by
# omission and a shipped tool judged reversible look identical in the code and
# are completely different claims — and it is the second one that has to survive
# somebody adding a `delete` verb to an existing tool two years from now.
#
# The reason is the load-bearing part. "Reversible" alone is an assertion; the
# sentence says what makes it true, so the next person can check whether it
# still is.
_REVERSIBLE: dict[str, str] = {
    "open_app":         "launching an application; the user can close it",
    "web_search":       "reads the network, writes nothing",
    "system_status":    "reads counters",
    "weather_report":   "reads an API",
    "flight_finder":    "searches and optionally saves a file the file tools can remove",
    "send_message":     "a sent message cannot be unsent — but a gate here would "
                        "ask before every reply, and the user dictated the text "
                        "and heard it read back before it went",
    "reminder":         "writes a scheduled entry the same tool can delete",
    "youtube_video":    "plays or downloads; the file is a file",
    "screen_process":   "captures a frame into the conversation",
    "close_camera":     "stops a preview",
    "browser_control":  "navigation and clicks in a browser that has its own back button",
    "file_controller":  "nine push_undo calls, including delete — which trashes "
                        "rather than unlinks (see actions/file_controller.py)",
    "desktop_control":  "the generated code runs in a sandbox with no deletion, "
                        "no rmtree and no subprocess (actions/desktop.py::_build_sandbox)",
    "computer_control": "types and clicks; a wrong keystroke is an annoyance, and "
                        "gating it would mean a banner per word",
    "file_processor":   "converts and edits documents into an output path",
    "manage_monitor":   "adds and removes a topic from a list",
    "save_memory":      "one key in long_term.json, overwritable",
    "recall_memory":    "a dictionary scan",
    "undo":             "it is the reversal",
}

for _name, _why in _REVERSIBLE.items():
    _RULES[_name] = _always(RUN, "", "", _why)


_ALLOWED_BY_DEFAULT = Verdict(
    RUN, reason="not a tool this build ships; unknown tools are allowed (R-05)")


def classify(name: str, args: Optional[dict] = None) -> Verdict:
    """The verdict for one tool call. Unknown tools are allowed.

    RUN is the default on purpose. A tool nobody classified is far more likely
    to be a new reversible one than a new destructive one, and a gate that
    defaults to asking trains the user to press CONFIRM without reading — which
    is worse than no gate, because it looks like consent.

    `tests/test_tool_policy.py` fails if a *declared* tool has no rule, so the
    default covers plugins and nothing that ships here.
    """
    fn = _RULES.get(name)
    if fn is None:
        return _ALLOWED_BY_DEFAULT
    return fn(dict(args or {}))


def gate(name: str, args: dict, run: Callable[[], str]) -> Optional[str]:
    """One call site for the whole rule, used by main.py and by action modules.

    Returns the sentence to hand back to the model when the call was parked, and
    None when it may run now. Returning None rather than running it keeps this
    module free of any opinion about threads — `_execute_tool` needs its handler
    in an executor, and actions/code_helper.py is already on one.
    """
    from core import confirm

    verdict = classify(name, args)
    if verdict.gate != CONFIRM:
        return None

    if confirm.pending_title():
        return ("There is already a confirmation waiting on screen. Ask the "
                "user to answer that one before I do anything else.")

    return confirm.request(key=name, title=verdict.title,
                           detail=verdict.detail, run=run)


def describe(declarations: Optional[list[dict]] = None) -> str:
    """The gate matrix, for a human.

    `.venv/bin/python -c "from core import tool_policy; import main;
     print(tool_policy.describe(main.TOOL_DECLARATIONS))"`
    """
    names = ([d["name"] for d in declarations] if declarations
             else sorted(_RULES))
    lines = ["tool                 gate      why"]
    for name in sorted(names):
        v = classify(name, {})
        lines.append(f"  {name:<20} {v.gate:<9} {v.reason or v.title}")
    return "\n".join(lines)
