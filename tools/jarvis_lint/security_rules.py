"""
tools/jarvis_lint/security_rules.py — the four rules about damage.

Each one is written against a defect that shipped. The comment above each rule
names it, because a rule whose origin is forgotten is a rule that gets relaxed
by someone who has only ever seen it be inconvenient.
"""

from __future__ import annotations

import ast
import re
from typing import Iterator

from .framework import (CRITICAL, HIGH, LOW, MEDIUM, Finding, Module,
                        rule)

# ── JAR001 ───────────────────────────────────────────────────────────────────

# Writing through a path someone else chose the tail of.
_WRITE_METHODS = {"write_text", "write_bytes", "mkdir", "touch",
                  "rename", "replace", "unlink", "rmdir", "symlink_to"}

# What counts as having thought about containment. `resolve()` alone does not:
# resolving a traversal produces a perfectly valid path outside the root, which
# is the whole point of the attack. The check that matters is the comparison.
_CONFINEMENT = re.compile(r"is_relative_to|_is_safe_path|safe_join|_inside_|"
                          r"confine|SafePath|ProjectPath|relative_to")

# Calls that turn a hostile string into a harmless one. `Path(x).name` strips
# every directory component; `re.sub` here is always the "keep only these
# characters" shape. A value that has been through one of these is no longer
# the model's to steer.
_SANITISERS = re.compile(r"^(_safe_filename|_sanitize|_slug|sub|name|stem|basename)$")


def _is_literal_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _reads_a_mapping(node: ast.AST) -> bool:
    """`d["k"]` or `d.get("k")` — how a model-authored JSON document enters.

    This, and not "is a parameter", is the seed of the taint. Every helper in
    this codebase takes a path argument; almost none of them read one out of a
    document the model wrote. Seeding on parameters flags the whole tree and
    teaches everyone to ignore the rule.
    """
    if isinstance(node, ast.Subscript) and isinstance(node.value, (ast.Name, ast.Attribute)):
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get")


def _is_sanitised(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if _SANITISERS.match(name or ""):
            return True
    return isinstance(node, ast.Attribute) and _SANITISERS.match(node.attr or "")


_CONTAINER_ADDS = {"append", "add", "extend", "insert", "update"}


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _tainted_names(fn: ast.AST) -> set[str]:
    """Names in `fn` carrying a value that was read out of a mapping.

    Run to a fixed point, and through three carriers rather than one:

        p = fi["path"]                 # the seed: a mapping read
        files_to_fix.append(p)         # a container inherits its contents
        for fix_path in files_to_fix:  # a loop target inherits its iterable

    Those exact three hops are actions/dev_agent.py::_fix_files, which a
    single-pass assignment-only version missed while catching the identical
    defect in _write_file two hundred lines above. A path rule that a `for`
    loop walks around is not a path rule.

    Still deliberately shallow — no interprocedural anything. The claim is
    only that taint survives the shapes this codebase actually writes.
    """
    tainted: set[str] = set()
    for _ in range(6):                                 # converges in 3 here
        before = set(tainted)

        for node in ast.walk(fn):
            # a = <expr>
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                if value is None:
                    continue
                if _is_sanitised(value):
                    for t in targets:                  # laundering clears it
                        if isinstance(t, ast.Name):
                            tainted.discard(t.id)
                    continue
                if _reads_a_mapping(value) or (_names_in(value) & tainted):
                    for t in targets:
                        if isinstance(t, ast.Name):
                            tainted.add(t.id)

            # for x in <expr>
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                if _reads_a_mapping(node.iter) or (_names_in(node.iter) & tainted):
                    for t in ast.walk(node.target):
                        if isinstance(t, ast.Name):
                            tainted.add(t.id)

            # container.append(x) / .add / .extend / .update
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if (isinstance(call.func, ast.Attribute)
                        and call.func.attr in _CONTAINER_ADDS
                        and isinstance(call.func.value, ast.Name)):
                    if any(_reads_a_mapping(a) or (_names_in(a) & tainted)
                           for a in call.args):
                        tainted.add(call.func.value.id)

        if tainted == before:
            break
    return tainted


def _unconfined_join(node: ast.AST, tainted: set[str]) -> bool:
    """`base / x` where x carries something read out of a mapping."""
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
        return False
    if _is_literal_str(node.right):
        return False                                   # `root / ".venv"` cannot escape
    return _reads_a_mapping(node.right) or any(
        isinstance(n, ast.Name) and n.id in tainted for n in ast.walk(node.right)
    )


@rule(
    "JAR001",
    "a path joined from a non-literal must be confined before it is written",
    "actions/dev_agent.py built `project_dir / file_path` from the planner's own "
    "JSON and wrote to it. pathlib discards the left side entirely when the right "
    "is absolute, so a plan naming \"/etc/cron.d/x\" wrote to /etc/cron.d/x. "
    "actions/file_controller.py already does this correctly at fifteen call sites; "
    "the rule is that everyone does.",
    severity=CRITICAL,
)
def unconfined_write(mod: Module) -> Iterator[Finding]:
    for fn in mod.nodes:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Does this function contain the check anywhere at all? Intraprocedural
        # and deliberately generous: the goal is to catch functions that never
        # considered containment, not to audit ones that did.
        #
        # By walking for names rather than searching ast.dump(fn): dump
        # serialises the whole subtree to a string for every function in the
        # tree, which cost five seconds a run on ui.py and main.py alone and
        # made `jarvis check` too slow to run before a commit.
        if any(_CONFINEMENT.search(n.id if isinstance(n, ast.Name) else n.attr)
               for n in ast.walk(fn)
               if isinstance(n, (ast.Name, ast.Attribute))):
            continue

        tainted = _tainted_names(fn)

        # Names bound to an unconfined join, and the join expressions themselves.
        joined: dict[str, int] = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and _unconfined_join(node.value, tainted):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        joined[t.id] = node.lineno

        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _WRITE_METHODS):
                continue
            recv = node.func.value
            origin = None
            if isinstance(recv, ast.Name) and recv.id in joined:
                origin = recv.id
            elif _unconfined_join(recv, tainted):
                origin = "<inline join>"
            elif isinstance(recv, ast.Attribute) and _unconfined_join(recv.value, tainted):
                origin = "<inline join>"
            if origin is None:
                continue
            yield Finding(
                rule="JAR001", path=mod.rel, line=node.lineno, symbol=fn.name,
                message=(f".{node.func.attr}() writes through a path joined from a "
                         f"value this function did not choose ({origin}). Resolve it "
                         f"and compare against the root — `is_relative_to` — before "
                         f"writing, the way actions/file_controller.py::_is_safe_path "
                         f"does. An absolute tail silently discards the base."),
            )


# ── JAR004 ───────────────────────────────────────────────────────────────────

# Errors that mean "a constraint the caller asked for cannot be met". Catching
# one and carrying on is not error handling — it is deciding, on the caller's
# behalf and without telling them, that the constraint was optional.
#
# Deliberately NOT here: QuotaExceeded and ProviderUnavailable. Those say "this
# provider failed", not "what you asked for is impossible", and falling through
# to the next provider is the documented, correct behaviour of the gateway.
# PermissionError is out for the same reason — skipping a directory you may not
# read while listing is not a widened permission, it is a listing.
_CONSTRAINT_ERRORS = {"NoModelFits", "CapabilityUnavailable", "ConfirmationRequired"}


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    t = handler.type
    if t is None:
        return set()
    nodes = t.elts if isinstance(t, ast.Tuple) else [t]
    out = set()
    for n in nodes:
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


@rule(
    "JAR004",
    "an unmet constraint may not be swallowed into a wider permission",
    "core/ai/__init__.py caught NoModelFits, set `ranked = []`, and fell through "
    "to a loop that appended every reachable provider. Budget.private() — "
    "documented as 'ne quitte pas la machine' — therefore sent the prompt to "
    "Gemini, Anthropic and OpenAI in exactly the case the guarantee was for. "
    "R-12: a budget that cannot be met raises; it never degrades quietly.",
    severity=CRITICAL,
)
def swallowed_constraint(mod: Module) -> Iterator[Finding]:
    for handler in mod.nodes:
        if not isinstance(handler, ast.ExceptHandler):
            continue
        caught = _handler_names(handler) & _CONSTRAINT_ERRORS
        if not caught:
            continue
        # Re-raising is the correct shape. So is answering *about* the failure
        # and stopping — core/ai/router.py::explain returns str(e), which
        # reports the refusal rather than working around it.
        body = handler.body
        if any(isinstance(n, (ast.Raise, ast.Return)) for s in body
               for n in ast.walk(s)):
            continue
        fn = mod.enclosing_function(handler)
        yield Finding(
            rule="JAR004", path=mod.rel, line=handler.lineno,
            symbol=f"{fn.name if fn else '<module>'}:{'/'.join(sorted(caught))}",
            message=(f"catches {'/'.join(sorted(caught))} and continues without "
                     f"re-raising or returning. The caller asked for a constraint; "
                     f"carrying on grants more than they asked for. Re-raise, or "
                     f"return an answer that says the constraint was not met."),
        )


# ── JAR007 ───────────────────────────────────────────────────────────────────

# Things that consume, mint or destroy a credential or a file. A browser
# prefetch, a link-preview bot and a security scanner all issue GETs; anything
# on this list behind one happens without a human deciding it did.
_STATE_CHANGING = re.compile(
    r"^(redeem_|revoke_|burn_|pair_device|open_session|_enqueue|unlink|rmtree|"
    r"rmdir|write_text|write_bytes|new_pairing_key|new_ticket)"
)


def _route_methods(fn: ast.AST) -> set[str]:
    """{"get", "post", …} for a FastAPI-decorated handler."""
    out = set()
    for dec in getattr(fn, "decorator_list", []):
        call = dec if isinstance(dec, ast.Call) else None
        target = call.func if call else dec
        if isinstance(target, ast.Attribute):
            out.add(target.attr)
    return out


@rule(
    "JAR007",
    "a GET route may not change state",
    "dashboard/server.py's /auto-login consumes the pairing PIN, mints a session "
    "and pairs a device — all behind a GET. A browser prefetching the QR link, or "
    "any crawler that sees it, burns a live credential. GET is the one verb the "
    "network issues without being asked.",
    severity=HIGH,
)
def get_route_changes_state(mod: Module) -> Iterator[Finding]:
    for fn in mod.nodes:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "get" not in _route_methods(fn):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else "")
            if name and _STATE_CHANGING.match(name):
                yield Finding(
                    rule="JAR007", path=mod.rel, line=node.lineno,
                    symbol=f"{fn.name}:{name}",
                    message=(f"GET handler calls {name}(), which changes state. "
                             f"Move it behind POST, or make the GET idempotent — a "
                             f"prefetch must not be able to spend a credential."),
                )


# ── JAR008 ───────────────────────────────────────────────────────────────────

# A cap, not a cleanup. `.pop()` and `del` are eviction, and eviction on the
# success path is exactly what Throttle already has — it is why the defect
# survived review. The question this rule asks is the other one: is there any
# input for which this dictionary refuses to grow? Only a length test answers it.
_BOUND_EVIDENCE = re.compile(r"len\(self\.{attr}\)|maxsize|maxlen")

# Scoped to the one surface that is reachable by someone who is not already at
# the keyboard. Everything else in Alexio grows a dict at the speed a human
# types; the dashboard grows one at the speed a network can ask.
_REMOTE_SURFACE = ("dashboard/",)


@rule(
    "JAR008",
    "a dashboard map keyed on caller-supplied input must declare a size cap",
    "dashboard/auth.py::Throttle.retry_after does `self._fails[who] = hits` on "
    "every call, including when `hits` is empty — so every address that merely "
    "*asks* leaves an entry, on an unauthenticated path. clear() pops on success "
    "and prune() does not touch it, so the only inputs that shrink it are the ones "
    "that were never the problem. CredentialStore gets this right next door: "
    "`while len(self._devices) >= MAX_DEVICES`. A cap, not a cleanup.",
    severity=HIGH,
)
def unbounded_request_map(mod: Module) -> Iterator[Finding]:
    if not mod.is_under(*_REMOTE_SURFACE):
        return
    for cls in mod.nodes:
        if not isinstance(cls, ast.ClassDef):
            continue
        dumped = ast.unparse(cls)
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                    continue
                target = node.targets[0]
                if not (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and isinstance(target.value.value, ast.Name)
                        and target.value.value.id == "self"):
                    continue
                idx = target.slice
                if not (isinstance(idx, ast.Name) and idx.id in params):
                    continue
                attr = target.value.attr
                # Does the class ever refuse to let this dictionary grow?
                if re.search(_BOUND_EVIDENCE.pattern.format(attr=re.escape(attr)),
                             dumped):
                    continue
                yield Finding(
                    rule="JAR008", path=mod.rel, line=node.lineno,
                    symbol=f"{cls.name}.{attr}",
                    message=(f"self.{attr}[{idx.id}] is keyed on a caller-supplied "
                             f"value and nothing in {cls.name} ever tests "
                             f"len(self.{attr}). Popping on the success path is not "
                             f"a cap. Add a maximum and evict, the way "
                             f"CredentialStore does for _devices — a dict that grows "
                             f"on an unauthenticated path is a leak with a remote "
                             f"trigger."),
                )


# ── JAR017 ───────────────────────────────────────────────────────────────────

# Names that hold something an attacker would like to guess one character at a
# time. `api_key` is deliberately absent: it is read from a config file, not
# submitted by a remote party, and including it would flag every presence check
# in the tree.
_SECRET_NAME = re.compile(
    r"(^|_)(token|secret|bearer|pin|passphrase|password|digest|signature|hmac|nonce)"
    r"(_|$)", re.IGNORECASE)


def _looks_secret(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and _SECRET_NAME.search(node.id):
        return node.id
    if isinstance(node, ast.Attribute) and _SECRET_NAME.search(node.attr or ""):
        return node.attr
    return None


@rule(
    "JAR017",
    "a secret is compared in constant time",
    "dashboard/auth.py already gets this right once — _digest_ok uses "
    "secrets.compare_digest for the CryptoJS hash — so the idiom is present and "
    "the discipline is not. `==` on a string returns as soon as two bytes "
    "differ, which leaks the length of the matching prefix through timing. The "
    "dashboard is the one part of Alexio reachable by someone who is not at the "
    "keyboard, and the pairing PIN is a thirty-bit secret; a prefix oracle turns "
    "that into six six-bit searches.",
    severity=HIGH,
)
def timing_unsafe_comparison(mod: Module) -> Iterator[Finding]:
    # A test asserting that two secrets are equal is making a claim about
    # storage, not authenticating anybody; there is no attacker to time it.
    if mod.is_under("tests/"):
        return
    for node in mod.nodes:
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            continue
        left, right = node.left, node.comparators[0]
        # A presence check against a literal is not a secret comparison:
        # `if token == ""` asks whether anything was sent at all.
        if any(isinstance(side, ast.Constant) for side in (left, right)):
            continue
        name = _looks_secret(left) or _looks_secret(right)
        if not name:
            continue
        fn = mod.enclosing_function(node)
        yield Finding(
            rule="JAR017", path=mod.rel, line=node.lineno,
            symbol=f"{fn.name if fn else '<module>'}:{name}",
            message=(f"`{name}` is compared with == , which returns early on the "
                     f"first differing byte. Use secrets.compare_digest(a, b), "
                     f"the way dashboard/auth.py::_digest_ok already does."),
        )


# ── JAR018 ───────────────────────────────────────────────────────────────────

_LOG_CALLS = {"print", "write_log", "debug", "info", "warning", "error",
              "exception", "_log", "_warn", "_notify"}

# Calls that consume a secret without revealing it. Logging how long a token is,
# or the first eight characters of its hash, is exactly the compromise this rule
# recommends — so it must not flag it. The negative test found this, not review.
_SAFE_WRAPPERS = {"len", "bool", "type", "hash", "sha256", "md5", "_hash",
                  "compare_digest", "id"}


def _leaked_names(node: ast.AST) -> Iterator[str]:
    """Secret-looking names in `node`, not counting those already neutralised.

    Walks by hand rather than with ast.walk so a subtree under len()/_hash()
    can be pruned instead of inspected.
    """
    if isinstance(node, ast.Call):
        fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                 else getattr(node.func, "id", ""))
        if fname in _SAFE_WRAPPERS:
            return
    name = _looks_secret(node)
    if name:
        yield name
        return
    for child in ast.iter_child_nodes(node):
        yield from _leaked_names(child)


@rule(
    "JAR018",
    "a secret is never written to a log",
    "Nothing in the tree does this today. The dashboard mints bearer tokens, "
    "256-bit session secrets, device tokens and WebSocket tickets, and "
    "dashboard/auth.py goes out of its way to store all of them hashed precisely "
    "so that 'a credential store dumped by a traceback' hands nobody a working "
    "token. A print() during the next debugging session undoes that in one line, "
    "and stdout on this project is a HUD the user reads.",
    severity=HIGH,
)
def secret_in_a_log(mod: Module) -> Iterator[Finding]:
    for node in mod.nodes:
        if not isinstance(node, ast.Call):
            continue
        fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                 else node.func.id if isinstance(node.func, ast.Name) else "")
        if fname not in _LOG_CALLS:
            continue
        for arg in node.args:
            for name in _leaked_names(arg):
                fn = mod.enclosing_function(node)
                yield Finding(
                    rule="JAR018", path=mod.rel, line=node.lineno,
                    symbol=f"{fn.name if fn else '<module>'}:{name}",
                    message=(f"{fname}() is handed `{name}`. Tokens are stored "
                             f"hashed so a dump cannot hand anyone a working "
                             f"credential; a log line gives back what the hashing "
                             f"took away. Log a prefix, a hash, or nothing."),
                )
                break
