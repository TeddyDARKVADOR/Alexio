"""
tools/jarvis_lint/execution_rules.py — what may become a command, and what may
become a file.

The family here shares one shape: a value the model chose reaches a primitive
that does not care where the value came from. JAR001 already covers the
filesystem version of that; these cover the process version, plus two smaller
rules about primitives that are simply the wrong ones to reach for.
"""

from __future__ import annotations

import ast
import re
from typing import Iterator

from .framework import CRITICAL, HIGH, LOW, MEDIUM, Finding, Module, rule

_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}


def _is_subprocess(call: ast.Call) -> bool:
    """`subprocess.run(...)` / `subprocess.Popen(...)`, however imported."""
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr in _SUBPROCESS_CALLS:
        base = f.value
        return isinstance(base, ast.Name) and base.id in ("subprocess", "sp")
    return isinstance(f, ast.Name) and f.id in ("Popen", "check_output")


def _literal_command(node: ast.AST) -> bool:
    """A command written here, in this file, by a person.

    A string literal, or a list of them. Anything else — a name, an f-string, a
    concatenation, a call — was assembled at runtime out of something, and on
    this codebase "something" is routinely a tool parameter.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                   for e in node.elts)
    return False


# ── JAR012 ───────────────────────────────────────────────────────────────────

@rule(
    "JAR012",
    "shell=True takes a command written in this file, never one assembled at runtime",
    "actions/open_app.py::_launch_windows validates a PREFIX and executes the "
    "whole string: shutil.which(app_name.split('.')[0]) answers yes for "
    "\"notepad.exe & calc\", and subprocess.Popen(app_name, shell=True) then runs "
    "both halves. app_name is a model-supplied tool parameter and open_app is "
    "classified RUN, so nothing asks first. Line 97 repeats it as an f-string "
    "behind an `if ':' in app_name` test, which is a shape check, not a "
    "sanitiser. A list with shell=True is its own bug: POSIX passes only the "
    "first element to sh -c and silently drops the rest.",
    severity=CRITICAL,
)
def tainted_shell(mod: Module) -> Iterator[Finding]:
    for node in mod.nodes:
        if not isinstance(node, ast.Call):
            continue
        shell_true = any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant)
            and kw.value.value is True for kw in node.keywords
        )
        if not shell_true or not node.args:
            continue
        cmd = node.args[0]
        if _literal_command(cmd) and not isinstance(cmd, (ast.List, ast.Tuple)):
            continue
        fn = mod.enclosing_function(node)
        try:
            shown = ast.unparse(cmd)
        except Exception:
            shown = "<expr>"
        listy = isinstance(cmd, (ast.List, ast.Tuple))
        why = ("a list with shell=True: POSIX hands only the first element to "
               "sh -c and drops the rest"
               if listy else
               "the command is assembled at runtime, so anything that reaches it "
               "reaches the shell")
        yield Finding(
            rule="JAR012", path=mod.rel, line=node.lineno,
            symbol=f"{fn.name if fn else '<module>'}:{shown[:44]}",
            message=(f"shell=True with `{shown[:60]}` — {why}. Drop shell=True and "
                     f"pass an argument list, or resolve the value against an "
                     f"allow-list of commands this file names itself."),
        )


# ── JAR013 ───────────────────────────────────────────────────────────────────

@rule(
    "JAR013",
    "a command line is not produced by splitting a string on whitespace",
    "actions/dev_agent.py::_run_project does plan['run_command'].split() and hands "
    "the result to subprocess. The string is written by the planner model; "
    ".split() is not a parser, so quoting, escaping and embedded arguments all "
    "mean whatever the whitespace happens to say. Confirmed and run in a "
    "per-project venv now, but the content is still the model's to choose.",
    severity=HIGH,
)
def split_command(mod: Module) -> Iterator[Finding]:
    for fn in mod.nodes:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # names bound to <something>.split(...)
        split_names: dict[str, int] = {}
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "split"
                    # shlex.split() is the fix this rule exists to push people
                    # towards; flagging it would make the rule argue with its
                    # own advice. Caught by the negative test, not by review.
                    and not (isinstance(node.value.func.value, ast.Name)
                             and node.value.func.value.id == "shlex")):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        split_names[t.id] = node.lineno
        if not split_names:
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and _is_subprocess(node) and node.args):
                continue
            first = node.args[0]
            names = {n.id for n in ast.walk(first) if isinstance(n, ast.Name)}
            hit = names & set(split_names)
            if not hit:
                continue
            name = sorted(hit)[0]
            yield Finding(
                rule="JAR013", path=mod.rel, line=node.lineno,
                symbol=f"{fn.name}:{name}",
                message=(f"`{name}` came from .split() on line {split_names[name]} "
                         f"and is used as a command line. Whitespace is not a "
                         f"grammar — use shlex.split() for a human-written string, "
                         f"or build the list yourself from named parts."),
            )


# ── JAR014 ───────────────────────────────────────────────────────────────────

@rule(
    "JAR014",
    "a temporary file is created, not merely named",
    "tempfile.mktemp() returns a path and leaves the creating to you, so anything "
    "on the machine can win the race between the two — which is why Python's own "
    "documentation has said 'use mkstemp() instead' since 2.3. Five sites: "
    "core/desktop/macos.py, core/desktop/windows.py, actions/file_processor.py "
    "and actions/desktop.py twice, three of them writing a file that is then "
    "handed to another program.",
    severity=MEDIUM,
)
def insecure_tempfile(mod: Module) -> Iterator[Finding]:
    for node in mod.nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "mktemp"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "tempfile"):
            continue
        fn = mod.enclosing_function(node)
        yield Finding(
            rule="JAR014", path=mod.rel, line=node.lineno,
            symbol=f"{fn.name if fn else '<module>'}:mktemp",
            message=("tempfile.mktemp() only invents a name — the file is created "
                     "later, and whoever creates it first wins. Use "
                     "tempfile.mkstemp() (or NamedTemporaryFile), which creates "
                     "and opens it atomically."),
        )


# ── JAR019 ───────────────────────────────────────────────────────────────────

_TLS_OFF = re.compile(r"^(CERT_NONE|_create_unverified_context)$")


@rule(
    "JAR019",
    "certificate verification is never switched off",
    "Nothing in the tree does this today, which is the moment to write the rule "
    "down. The dashboard generates its own TLS pair on first run and the phone "
    "accepts it once; the tempting fix for any certificate error it later throws "
    "is verify=False, and that turns the pairing — the one exchange where the "
    "256-bit device secret crosses the network — back into plaintext for anyone "
    "on the LAN. A rule costs nothing while nobody breaks it.",
    severity=CRITICAL,
)
def disabled_tls(mod: Module) -> Iterator[Finding]:
    for node in mod.nodes:
        if (isinstance(node, ast.keyword) and node.arg == "verify"
                and isinstance(node.value, ast.Constant)
                and node.value.value is False):
            fn = mod.enclosing_function(node.value)
            yield Finding(
                rule="JAR019", path=mod.rel, line=node.value.lineno,
                symbol=f"{fn.name if fn else '<module>'}:verify=False",
                message=("verify=False disables certificate checking, which turns "
                         "TLS into obfuscation. Point the client at the "
                         "certificate instead (verify='/path/to/jarvis.crt')."),
            )
        if isinstance(node, ast.Attribute) and _TLS_OFF.match(node.attr or ""):
            fn = mod.enclosing_function(node)
            yield Finding(
                rule="JAR019", path=mod.rel, line=node.lineno,
                symbol=f"{fn.name if fn else '<module>'}:{node.attr}",
                message=(f"{node.attr} disables certificate checking. Trust the "
                         f"specific certificate rather than every certificate."),
            )
