"""
tools/jarvis_lint/resource_rules.py — things acquired must be released, and work
handed to a thread must be collected.

Both rules here are about the same shape of mistake: the happy path releases the
resource and the failure path does not, so the *first* failure is permanent.
That is worse than a crash, because a crash is noticed.
"""

from __future__ import annotations

import ast
import re
from typing import Iterator

from .framework import (CRITICAL, HIGH, LOW, MEDIUM, Finding, Module,
                        rule)

# ── JAR005 ───────────────────────────────────────────────────────────────────

# Names that mean "something is in flight". Not a guess at intent: every one of
# these in this codebase gates an early return somewhere, which is what turns a
# leaked True into a feature that never works again.
_FLAG = re.compile(r"_(busy|active|running|in_flight|pending|locked|open)$")


def _lowered_in(body: list) -> set[str]:
    """Names assigned anywhere in a statement list — a finally that releases."""
    out: set[str] = set()
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, (ast.Name, ast.Attribute)):
                        out.add(ast.unparse(t))
    return out


def _flag_targets(stmt: ast.AST) -> list[tuple[str, int]]:
    """(dotted name, line) for each `<flag> = True` in this statement."""
    out = []
    for node in ast.walk(stmt):
        if not (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and node.value.value is True):
            continue
        for target in node.targets:
            if isinstance(target, (ast.Name, ast.Attribute)):
                name = ast.unparse(target)
                if _FLAG.search(name):
                    out.append((name, node.lineno))
    return out


@rule(
    "JAR005",
    "a state flag raised before a try is lowered in a finally",
    "main.py sets self._vision_busy = True and then captures the screen. If the "
    "capture raises — a refused Wayland portal, no camera — control reaches the "
    "except at the bottom of the dispatch, _pending_vision was never set, and the "
    "only two places that lower the flag are unreachable until the next reconnect. "
    "One denied portal dialog and vision answers 'still processing the previous "
    "request' for the rest of the session. The happy path lowers it; the failure "
    "path is the one that matters.",
    severity=HIGH,
)
def flag_without_finally(mod: Module) -> Iterator[Finding]:
    for node in mod.nodes:
        if not isinstance(node, ast.Try):
            continue

        raised: dict[str, int] = {}
        for stmt in node.body:
            for name, line in _flag_targets(stmt):
                raised.setdefault(name, line)
        if not raised:
            continue

        lowered = _lowered_in(node.finalbody)

        # A nested try/finally that covers the flag is the fix, and without this
        # the rule reported it: the outer try still *contains* the assignment,
        # so main.py's own correction for audit #5 kept firing. What matters is
        # whether some enclosing-or-inner handler releases it, not which one.
        # Any inner finally that lowers the name counts, wherever the flag was
        # raised: main.py raises _vision_busy just *before* the try that
        # releases it, which is the natural shape when the flag guards the
        # whole attempt. Requiring the raise to be inside the same try was
        # precise and wrong — it flagged the correction for audit #5.
        for inner in ast.walk(node):
            if isinstance(inner, ast.Try) and inner is not node:
                lowered |= _lowered_in(inner.finalbody)

        fn = mod.enclosing_function(node)
        for name, line in sorted(raised.items()):
            if name in lowered:
                continue
            yield Finding(
                rule="JAR005", path=mod.rel, line=line,
                symbol=f"{fn.name if fn else '<module>'}:{name}",
                message=(f"{name} is raised inside a try whose finally does not "
                         f"lower it, so any exception on the path below leaves it "
                         f"set for the life of the process. Clear it in a finally, "
                         f"or hold it with a contextmanager."),
            )


# ── JAR006 ───────────────────────────────────────────────────────────────────

@rule(
    "JAR006",
    "work handed to an executor is awaited or kept",
    "dashboard/server.py calls loop.run_in_executor(None, _ensure_crypto_js) and "
    "loop.run_in_executor(None, _firewall_hint, ...) as bare statements. The "
    "Futures are dropped on the floor, so if either raises — a read-only cache "
    "directory, a firewall probe that hangs — nothing reports it and nothing can. "
    "Not awaiting is the point (neither should block startup); discarding the "
    "handle is the mistake. Keep it, or attach a done-callback that logs.",
    severity=MEDIUM,
)
def discarded_executor_future(mod: Module) -> Iterator[Finding]:
    for node in mod.nodes:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "run_in_executor"):
            continue
        fn = mod.enclosing_function(node)
        target = ""
        if len(call.args) > 1:
            try:
                target = ast.unparse(call.args[1])
            except Exception:
                target = "?"
        yield Finding(
            rule="JAR006", path=mod.rel, line=node.lineno,
            symbol=f"{fn.name if fn else '<module>'}:{target}",
            message=(f"run_in_executor({target}) as a bare statement discards the "
                     f"Future, so an exception in {target or 'it'} is never "
                     f"retrieved and never logged. Keep the Future, or add "
                     f".add_done_callback(...) that surfaces the failure."),
        )
