"""
The tools the model is told about must all exist, and must all be handled.

This is a regression test for a bug that shipped: core/prompt.txt instructed the
model to call `agent_task`, an assistant-wide planner that had been renamed to
`dev_agent`. The model dutifully tried, got "Unknown tool", and the user saw the
assistant fail at the one thing the prompt had just promised it could do.

Nothing catches that class of mistake at runtime — the model's tool name is a
string, matched against strings. So it gets caught here.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def declared_names() -> set[str]:
    import main
    return {t["name"] for t in main.TOOL_DECLARATIONS}


@pytest.fixture(scope="module")
def handled_names() -> set[str]:
    """Tool names the dispatch actually reaches.

    Two places since the confirmation gate went in: most tools are entries in
    `_sync_handlers`, a table whose values are callables precisely so
    core/confirm.py can hold one and run it later; the rest are still `name ==`
    branches because they are async, or touch state on JarvisLive.

    Both are read, and the table is read by *calling* it rather than by parsing
    it — a dict of lambdas keyed by tool name is exactly the thing a regex over
    source would get subtly wrong.
    """
    import main

    src = inspect.getsource(main.JarvisLive._execute_tool)
    branches = set(re.findall(r'name\s*==\s*"([a-z_]+)"', src))

    table = main.JarvisLive._sync_handlers(_StubLive(), {})
    return branches | set(table)


class _StubLive:
    """Enough of JarvisLive for `_sync_handlers` to build its closures.

    It never runs them — the point is the set of keys, and building the table
    must not need a microphone, a Live session or a UI.
    """
    ui = None

    def speak(self, *_a, **_kw):    # pragma: no cover - never called
        raise AssertionError("the handler table must not run anything")


def test_every_declared_tool_has_a_handler(declared_names, handled_names):
    missing = declared_names - handled_names
    assert not missing, (
        f"declared to the model but not handled in _execute_tool: {sorted(missing)}"
    )


def test_every_handler_is_declared(declared_names, handled_names):
    orphan = handled_names - declared_names
    assert not orphan, (
        f"handled in _execute_tool but never declared, so unreachable: {sorted(orphan)}"
    )


def test_prompt_only_names_tools_that_exist(declared_names):
    """The TOOL ROUTING section of prompt.txt lists `tool_name: when to use it`.

    Any lowercase snake_case token in that position is the model being told a
    tool exists.
    """
    prompt = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
    referenced = set(re.findall(r"^([a-z][a-z0-9_]{2,}):", prompt, re.MULTILINE))

    # Words that sit in that position but name a concept, not a tool.
    concepts = {"note", "example", "warning"}
    referenced -= concepts

    unknown = referenced - declared_names
    assert not unknown, (
        f"core/prompt.txt tells the model to call tools that do not exist: "
        f"{sorted(unknown)}"
    )


def test_declarations_are_well_formed(declared_names):
    """Each declaration must carry the three fields the Live API requires, and a
    description long enough to actually route on."""
    import main

    for tool in main.TOOL_DECLARATIONS:
        name = tool.get("name", "<unnamed>")
        assert isinstance(tool.get("description"), str) and len(tool["description"]) > 20, \
            f"{name}: description missing or too short to route on"
        params = tool.get("parameters")
        assert isinstance(params, dict), f"{name}: parameters must be a dict"
        assert params.get("type") == "OBJECT", f"{name}: parameters.type must be OBJECT"
        for req in params.get("required", []):
            assert req in params.get("properties", {}), \
                f"{name}: required field '{req}' is not among its properties"


def test_tool_names_are_unique():
    import main
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate tool names shadow each other: {sorted(dupes)}"


# Rule R-01: only core/ai/ and core/voice/ import a provider SDK.
#
# core/ai   is plane B — one-shot requests.
# core/voice is plane A — the conversational session.
#
# This set was {main.py, actions/screen_processor.py} until phase 02. It is now
# empty, and the second test below keeps it that way: it fails if a name is left
# here that no longer needs it, so the list can never quietly become history.
_PLANE_A_EXEMPT: set[str] = set()

_PROVIDER_SDKS = {"anthropic", "openai", "google", "google_genai", "litellm", "cohere", "mistralai"}


def _sdk_imports(path: Path) -> list[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    lines = []
    for node in ast.walk(tree):
        mods: set[str] = set()
        if isinstance(node, ast.Import):
            mods = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = {node.module.split(".")[0]}
        if mods & _PROVIDER_SDKS:
            lines.append(node.lineno)
    return lines


def _project_files():
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).parts
        if parts[0] in (".venv", "tests", "__pycache__", "logs"):
            continue
        yield path


def test_no_provider_sdk_imported_outside_the_ai_layer():
    offenders: list[str] = []
    for path in _project_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("core/ai/", "core/voice/")) or rel in _PLANE_A_EXEMPT:
            continue
        offenders += [f"{rel}:{n}" for n in _sdk_imports(path)]

    assert not offenders, (
        "A provider SDK is imported outside core/ai/. Route the call through "
        f"core.ai.generate() instead: {offenders}"
    )


def test_the_plane_a_exemption_list_does_not_grow_stale():
    """An exempt file that no longer imports an SDK should leave the list, so
    the list always names real remaining work rather than old history."""
    stale = [rel for rel in _PLANE_A_EXEMPT if not _sdk_imports(ROOT / rel)]
    assert not stale, (
        f"these no longer import a provider SDK — drop them from "
        f"_PLANE_A_EXEMPT: {stale}"
    )
