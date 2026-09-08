"""
tools/jarvis_lint/architecture_rules.py — the rules about where things may live
and which door they must go through.

R-01 (no provider SDK outside core/ai and core/voice) is NOT here: it is already
enforced by tests/test_tool_wiring.py, with an exemption list that test keeps
honest. Duplicating it would give two places to relax it. What that test does
*not* cover is the other half of the same sentence — the model identifiers —
which is JAR002 below, and which two action modules were quietly breaking.
"""

from __future__ import annotations

import ast
import re
import sys
from typing import Iterator

from .framework import (CRITICAL, HIGH, LOW, MEDIUM, Finding, Module,
                        rule)

# ── JAR002 ───────────────────────────────────────────────────────────────────

# A model id is a vendor prefix *plus a version*. The version is what makes it
# an identifier rather than a name: "gemini-flash-latest", "claude-opus-5",
# "gpt-5.6-luna", "qwen3:1.7b" all pin something, while "gemini-live" (the
# transport in core/voice) and "claude-code" (a product) pin nothing.
#
# The vendor prefix alone was the first version of this rule, and the negative
# test in tests/test_architecture_rules.py rejected it — correctly. A rule that
# cannot tell a model from a product name earns an exemption list on day one,
# and an exemption list is prose again.
_MODEL_ID = re.compile(
    r"^(gemini|claude|gpt|qwen|llama|mistral)[-:.]?[a-z0-9][a-z0-9.\-:]*$"
)
_HAS_VERSION = re.compile(r"\d|latest|preview|snapshot")

_MODEL_ID_HOMES = ("core/ai/", "core/voice/", "tests/", "tools/")


@rule(
    "JAR002",
    "a model identifier appears only in core/ai/",
    "R-02: a tool declares what the work deserves (a tier), never who answers. "
    "actions/dev_agent.py and actions/code_helper.py each pinned "
    "\"gemini-flash-latest\" at module level, so the router could not route them "
    "and a model rename would have to be found by grep. "
    "tests/test_tool_wiring.py enforces the import half of R-01/R-02; this is the "
    "identifier half, which nothing enforced.",
    severity=HIGH,
)
def model_id_outside_gateway(mod: Module) -> Iterator[Finding]:
    if mod.is_under(*_MODEL_ID_HOMES):
        return
    for node in mod.nodes:
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value.strip()
        if not (_MODEL_ID.match(text) and _HAS_VERSION.search(text)):
            continue
        # A docstring naming a model in an example is documentation, not wiring.
        parent = mod.parents.get(node)
        if isinstance(parent, ast.Expr) and isinstance(parent.value, ast.Constant):
            continue
        fn = mod.enclosing_function(node)
        yield Finding(
            rule="JAR002", path=mod.rel, line=node.lineno,
            symbol=f"{fn.name if fn else '<module>'}:{node.value}",
            message=(f"names the model {node.value!r} outside core/ai/. Ask for a "
                     f"tier instead — ai.generate(..., tier=ai.Tier.FAST, "
                     f"task='...') — so the router can choose and the telemetry "
                     f"can price it (R-02)."),
        )


# ── JAR003 ───────────────────────────────────────────────────────────────────

_SINK = re.compile(r"registry|plugin")


def _is_gate_call(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "gate"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "tool_policy"):
            return True
    return False


def _enclosing_bodies(mod: Module, node: ast.AST):
    """Every statement list that lexically contains `node`, innermost first."""
    chain = {id(node)} | {id(a) for a in mod.ancestors(node)}
    for anc in mod.ancestors(node):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(anc, field, None)
            if isinstance(body, list) and any(id(s) in chain for s in body):
                yield body, chain


@rule(
    "JAR003",
    "executing a tool requires a verdict from core/tool_policy",
    "main.py's dispatch consults tool_policy.gate() for the handler table and for "
    "shutdown, and the plugin branch — `self._plugin_registry.run(...)` — consults "
    "nothing. tool_policy.register() exists solely so a plugin can declare a "
    "CONFIRM rule, and is called by nobody and read by nobody: a plugin that "
    "registered one would run anyway. R-18 says no tool executes without a "
    "verdict; this is the branch where that was false.",
    severity=CRITICAL,
)
def ungated_tool_execution(mod: Module) -> Iterator[Finding]:
    for node in mod.nodes:
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Attribute)
                and _SINK.search(node.func.value.attr)):
            continue

        # A sink inside a closure that is *handed to* the gate is guarded, and
        # the line numbers say the opposite: the callable has to be defined
        # before the gate that takes it, so `gate` always sits below the sink.
        # core/confirm.py stores exactly such a callable to run later if a
        # human presses CONFIRM — this is the shape the design asks for, not a
        # loophole.
        holder = mod.enclosing_function(node)
        if holder is not None:
            outer = mod.enclosing_function(holder)
            scope = outer if outer is not None else mod.tree
            handed_over = any(
                isinstance(a, ast.Name) and a.id == holder.name
                for c in ast.walk(scope)
                if isinstance(c, ast.Call) and _is_gate_call(c)
                for a in c.args
            )
            if handed_over:
                continue

        # Dominance, not mere presence. A gate call sitting inside a *sibling*
        # `if` does not guard this branch — that is exactly the shape of the
        # bug: the gate at main.py:1193 lives inside `if handler is not None:`,
        # and the plugin branch is reached precisely when handler *is* None.
        # So only straight-line statements of an enclosing block count.
        guarded = False
        for body, chain in _enclosing_bodies(mod, node):
            for stmt in body:
                if id(stmt) in chain:
                    continue                       # an ancestor of the sink
                if isinstance(stmt, (ast.If, ast.Try, ast.While, ast.For,
                                     ast.AsyncFor, ast.With, ast.AsyncWith)):
                    continue                       # a branch we may not have taken
                if stmt.lineno < node.lineno and _is_gate_call(stmt):
                    guarded = True
                    break
            if guarded:
                break

        if guarded:
            continue

        fn = mod.enclosing_function(node)
        yield Finding(
            rule="JAR003", path=mod.rel, line=node.lineno,
            symbol=f"{fn.name if fn else '<module>'}:{node.func.value.attr}.run",
            message=("executes a tool without a verdict from core/tool_policy. Call "
                     "tool_policy.gate(name, args, handler) first and park the call "
                     "when it returns a sentence — otherwise a plugin that declared "
                     "itself CONFIRM runs anyway (R-18)."),
        )


# ── JAR009 ───────────────────────────────────────────────────────────────────

_MUTABLE_ANNOTATIONS = {"dict", "list", "set", "Dict", "List", "Set"}


def _mutable_fields(cls: ast.ClassDef) -> set[str]:
    out = set()
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            ann = stmt.annotation
            name = (ann.id if isinstance(ann, ast.Name)
                    else ann.value.id if isinstance(ann, ast.Subscript)
                    and isinstance(ann.value, ast.Name) else "")
            if name in _MUTABLE_ANNOTATIONS:
                out.add(stmt.target.id)
    return out


@rule(
    "JAR009",
    "a spec does not hand out its own mutable state",
    "core/ai/tools.py::to_anthropic and to_openai return `self.parameters` by "
    "reference, so a provider adapter that normalises the schema it was given "
    "edits the ToolSpec every other adapter will read next. The dialects are "
    "documented as pure functions of the neutral form; a shared dict makes them "
    "anything but.",
    severity=MEDIUM,
)
def mutable_spec_leak(mod: Module) -> Iterator[Finding]:
    for cls in mod.nodes:
        if not isinstance(cls, ast.ClassDef):
            continue
        fields = _mutable_fields(cls)
        if not fields:
            continue
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for ret in ast.walk(fn):
                if not (isinstance(ret, ast.Return) and ret.value is not None):
                    continue
                # `return self.x` or `return {"k": self.x}` — the attribute
                # itself escapes. NOT `return f"...{self.x}"` or
                # `return len(self.x) >= N`: those read it and build something
                # new, which is the whole difference. Checking "mentions
                # self.x anywhere in the return expression" flagged an
                # __repr__ and a bool, and a rule that flags __repr__ is a
                # rule that gets switched off.
                escaping = []
                if isinstance(ret.value, ast.Attribute):
                    escaping = [ret.value]
                elif isinstance(ret.value, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
                    values = (ret.value.values if isinstance(ret.value, ast.Dict)
                              else ret.value.elts)
                    escaping = [v for v in values if isinstance(v, ast.Attribute)]
                for n in escaping:
                    if (n.attr in fields and isinstance(n.value, ast.Name)
                            and n.value.id == "self"):
                        yield Finding(
                            rule="JAR009", path=mod.rel, line=ret.lineno,
                            symbol=f"{cls.name}.{fn.name}:{n.attr}",
                            message=(f"returns self.{n.attr} by reference; the caller "
                                     f"can mutate {cls.name}'s own state. Return "
                                     f"copy.deepcopy(self.{n.attr}) — a dialect is "
                                     f"documented as a pure function of the spec."),
                        )


# ── JAR010 ───────────────────────────────────────────────────────────────────

@rule(
    "JAR010",
    "the routing path does not parse the whole telemetry log",
    "core/ai/registry.load() runs on every routing decision and calls "
    "telemetry.summarise() -> read_rows(), which parses both log generations in "
    "full — `limit` is applied after the parsing, not before. Measured at the 16 MB "
    "rotation threshold: 491 ms per ai.generate(), and roughly a second once the "
    "rotated file is full too. The docstring promises 'well under a millisecond', "
    "which is true only on a fresh install; Budget.conversation() allows 900 ms in "
    "total.",
    severity=MEDIUM,
)
def unbounded_log_read(mod: Module) -> Iterator[Finding]:
    for node in mod.nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "read_rows"):
            continue
        if any(kw.arg == "limit" for kw in node.keywords) or node.args:
            continue
        fn = mod.enclosing_function(node)
        if fn is not None and fn.name.startswith("test"):
            continue
        yield Finding(
            rule="JAR010", path=mod.rel, line=node.lineno,
            symbol=f"{fn.name if fn else '<module>'}:read_rows",
            message=("read_rows() with no limit parses every line of both log "
                     "generations, on the hot path of every model call. Pass a "
                     "limit, or read the tail — the router only ever looks at "
                     "recent behaviour."),
        )


# ── JAR011 ───────────────────────────────────────────────────────────────────

# Packages that are pure Python and import nothing native, so absence really is
# the only way they fail and `except ImportError` really is honest.
#
# This is an ALLOWLIST on purpose. tests/test_optional_dependencies.py has the
# same rule keyed on a hand-written list of eleven packages that "reach for the
# machine" — which is a denylist, and a denylist is a claim about every package
# nobody has thought of yet. PIL, psutil, numpy and uvicorn are not on it.
# dashboard/server.py guards fastapi/uvicorn with `except ImportError` eight
# lines above a `except Exception` for python-multipart, in the same file.
_PURE_PYTHON = {
    "docx", "openpyxl", "pptx", "PyPDF2", "pypdf", "markdown", "bs4", "ddgs",
    "yaml", "dateutil", "send2trash", "jeepney", "qrcode", "dotenv", "chardet",
    "youtube_transcript_api", "requests", "urllib3", "certifi", "idna",
    "charset_normalizer", "packaging", "typing_extensions", "distro",
}

_NARROW = {"ImportError", "ModuleNotFoundError"}


@rule(
    "JAR011",
    "an optional import that touches native code is guarded with except Exception",
    "Phase 00 fixed five `except ImportError` guards around pyautogui, which opens "
    "an X display at import and raises DisplayConnectionError — not an ImportError "
    "— over SSH, in a TTY, under a systemd unit, in CI. The narrow guard lets it "
    "through and the module dies anyway. Any package with a compiled extension or "
    "a runtime dependency on the machine fails the same way.",
    severity=LOW,
)
def narrow_import_guard(mod: Module) -> Iterator[Finding]:
    for node in mod.nodes:
        if not isinstance(node, ast.Try):
            continue
        packages: set[str] = set()
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Import):
                    packages |= {a.name.split(".")[0] for a in sub.names}
                elif isinstance(sub, ast.ImportFrom) and sub.module and sub.level == 0:
                    packages.add(sub.module.split(".")[0])

        risky = {p for p in packages
                 if p not in _PURE_PYTHON
                 and p not in sys.stdlib_module_names
                 and not (mod.path.parent.parent / p).exists()      # project-local
                 and p not in {"core", "actions", "memory", "dashboard", "config",
                               "plugins", "tools", "main", "ui", "tests"}}
        if not risky:
            continue

        caught: set[str] = set()
        for handler in node.handlers:
            if handler.type is None:
                caught.add("Exception")
            else:
                for n in ast.walk(handler.type):
                    if isinstance(n, ast.Name):
                        caught.add(n.id)
        if caught - _NARROW:
            continue

        fn = mod.enclosing_function(node)
        yield Finding(
            rule="JAR011", path=mod.rel, line=node.lineno,
            symbol=f"{fn.name if fn else '<module>'}:{'/'.join(sorted(risky))}",
            message=(f"guards {sorted(risky)} with `except {'/'.join(sorted(caught))}` "
                     f"only. These reach for the machine at import time and raise "
                     f"other things when it is not there. Use `except Exception`."),
        )


# ── JAR015 ───────────────────────────────────────────────────────────────────

@rule(
    "JAR015",
    "the verdict from core/tool_policy is read, not merely obtained",
    "Nothing in the tree does this today — it is the bug that comes *after* "
    "JAR003 is fixed. gate() returns the sentence to hand back when a call was "
    "parked, and returns None when it may run; classify() returns a Verdict whose "
    "__bool__ is True only for RUN. Both are easy to call for their side effect "
    "and then run the tool anyway, which reads as a gate in review, passes "
    "JAR003, and asks the user a question whose answer is discarded. That is "
    "worse than no gate: it manufactures consent.",
    severity=CRITICAL,
)
def discarded_verdict(mod: Module) -> Iterator[Finding]:
    # A test drives the gate for its side effect and then resolves the
    # confirmation itself; that is the one caller for which the return value is
    # genuinely uninteresting.
    if mod.is_under("tests/"):
        return
    for node in mod.nodes:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in ("gate", "classify")
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "tool_policy"):
            continue
        fn = mod.enclosing_function(node)
        yield Finding(
            rule="JAR015", path=mod.rel, line=node.lineno,
            symbol=f"{fn.name if fn else '<module>'}:{call.func.attr}",
            message=(f"tool_policy.{call.func.attr}() is called and its result "
                     f"thrown away. gate() returns None only when the call may "
                     f"proceed; anything else is the sentence to hand back "
                     f"instead of running the tool (R-18)."),
        )


# ── JAR016 ───────────────────────────────────────────────────────────────────

# Operations that destroy or displace something that already existed. Creating
# an output file is not on this list: R-05 is about what cannot be taken back,
# not about everything that touches a disk.
_DESTRUCTIVE = {"unlink", "rmtree", "rmdir", "rename", "replace", "move"}


@rule(
    "JAR016",
    "an action that destroys something registers an undo (R-05)",
    "R-05 is the load-bearing half of the tool policy: reversible work happens at "
    "once and pushes an undo, and only the irreversible waits for a button. "
    "core/tool_policy.py classifies file_processor, code_helper, desktop_control "
    "and reminder as RUN on the strength of being reversible — and only "
    "actions/file_controller.py actually calls push_undo. The others delete, move "
    "and rename with nothing behind them, so 'reversible' is a claim the code "
    "does not keep and `undo` cannot reach them.",
    severity=HIGH,
)
def destructive_without_undo(mod: Module) -> Iterator[Finding]:
    if not mod.is_under("actions/"):
        return
    if "push_undo" in mod.source:
        return
    seen: set[str] = set()
    for node in mod.nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _DESTRUCTIVE):
            continue
        # `"a b".replace(" ", "_")` is string work and `Path.replace(target)` is
        # an overwrite. They share a name and nothing else; the arity separates
        # them, and without that this rule reported ten sanitisers as data loss.
        if node.func.attr == "replace" and len(node.args) != 1:
            continue
        if node.func.attr == "move" and not (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "shutil"):
            continue
        # `Path.rename` on a temp file the same function just made is bookkeeping,
        # not destruction; requiring an undo for it would make the rule noise.
        fn = mod.enclosing_function(node)
        name = fn.name if fn else "<module>"
        if name.startswith("_undo") or name in seen:
            continue
        # Removing a temporary file this same function created is bookkeeping,
        # not data loss. Undo has nothing to restore and asking for one would
        # make the rule argue with correct code.
        if fn is not None and isinstance(node.func.value, ast.Name):
            target = node.func.value.id
            # Two passes, because mkstemp() returns (fd, name): the path is born
            # on a *second* line — `_fd, _name = mkstemp(...)` then
            # `tmp = Path(_name)`. A single hop saw only the tuple and reported
            # the cleanup as data loss. Found by this rule firing on the fix for
            # audit #23, which is the loop working.
            # Indexed once, then two cheap passes over the index. Doing
            # ast.dump() inside the loop reintroduced exactly the cost that was
            # taken out of JAR001 for the same reason: it serialises a subtree
            # per assignment, and it took the suite from 48 s to 198 s.
            assigns = []
            for a in ast.walk(fn):
                if isinstance(a, ast.Assign):
                    names = {n.id for n in ast.walk(a.value) if isinstance(n, ast.Name)}
                    seeded = any(
                        isinstance(n, ast.Attribute) and n.attr in
                        ("mkstemp", "mkdtemp", "mktemp", "NamedTemporaryFile",
                         "TemporaryDirectory")
                        for n in ast.walk(a.value)
                    ) or "tempfile" in names
                    bound = {t2.id for t1 in a.targets for t2 in ast.walk(t1)
                             if isinstance(t2, ast.Name)}
                    assigns.append((seeded, names, bound))

            temps: set[str] = set()
            for _ in range(2):
                for seeded, names, bound in assigns:
                    if seeded or (names & temps):
                        temps |= bound
            if target in temps:
                continue
        seen.add(name)
        yield Finding(
            rule="JAR016", path=mod.rel, line=node.lineno,
            symbol=f"{name}:{node.func.attr}",
            message=(f".{node.func.attr}() destroys or displaces something and "
                     f"this module never calls push_undo. Either register the "
                     f"reversal (core/undo.py), or say so in core/tool_policy.py "
                     f"and gate the tool — R-05 has no third option."),
        )


# ── JAR020 ───────────────────────────────────────────────────────────────────

# Overridden by tests/test_architecture_rules.py; None means "the real one".
_TESTS_DIR = None


@rule(
    "JAR020",
    "a RUN verdict that rests on a sandbox is backed by a test of that sandbox",
    "core/tool_policy.py lets desktop_control run ungated because 'the generated "
    "code runs in a sandbox with no deletion, no rmtree and no subprocess'. That "
    "sentence is the entire justification for executing model-authored code with "
    "no human in the loop — actions/desktop.py does exec(compile(code, ...), "
    "sandbox) — and nothing in the suite checks that the sandbox refuses any of "
    "the three. A claim load-bearing enough to skip a confirmation is load-bearing "
    "enough to test.",
    severity=HIGH,
)
def untested_sandbox_claim(mod: Module) -> Iterator[Finding]:
    if mod.rel != "core/tool_policy.py":
        return
    from .framework import ROOT

    tests = ""
    # Injectable so the rule's own positive test can point it at an empty
    # directory. Once the real sandbox test existed the rule stopped firing on
    # its synthetic snippet — correct behaviour, and it made the test that
    # proves the rule works unwritable. A rule that can only be demonstrated
    # while the defect is live is a rule nobody can keep honest.
    tests_dir = _TESTS_DIR or (ROOT / "tests")
    if tests_dir.is_dir():
        for p in sorted(tests_dir.glob("test_*.py")):
            try:
                tests += p.read_text(encoding="utf-8")
            except OSError:
                pass

    for node in mod.nodes:
        if not (isinstance(node, ast.Dict)):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            try:
                rationale = ast.literal_eval(value)
            except Exception:
                continue
            if not isinstance(rationale, str) or "sandbox" not in rationale.lower():
                continue
            if "_build_sandbox" in tests:
                continue
            yield Finding(
                rule="JAR020", path=mod.rel, line=key.lineno,
                symbol=f"_REVERSIBLE:{key.value}",
                message=(f"'{key.value}' is allowed to RUN because a sandbox is "
                         f"said to block deletion, rmtree and subprocess, and no "
                         f"test names _build_sandbox. Write the test that tries "
                         f"all three, or gate the tool."),
            )
