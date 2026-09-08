"""
Alexio's invariants are enforced, not merely written down.

WHY THIS FILE IS NOT JUST "RUN THE LINTER"
    A linter that has never said no to anything has proved nothing. Half of the
    twenty-one defects the 2026-09-08 audit found were places where CLAUDE.md
    stated a rule (R-02, R-12, R-18) and no code checked it, so the failure mode
    to guard against here is a *rule* that looks present and is not — the same
    shape, one level up.

    So every rule in tools/jarvis_lint gets two tests:

      · a positive — the defect it was written for, reduced to a few lines.
        This is what stops someone "simplifying" the rule into a no-op.
      · a near miss — code that looks like the defect and is correct. This is
        what stops the rule from being widened until it flags everything and
        everyone learns to ignore it.

    The near miss is the load-bearing one. JAR009 first flagged a `__repr__` and
    a boolean return; JAR004 first flagged the gateway's own documented
    provider fallback; JAR001 first flagged fourteen ordinary file writes. A
    rule with no negative test is a rule nobody has checked for false positives.

THE BASELINE
    The defects are still in the tree — this landed before the fixes, on
    purpose, so the rules could be written against real code rather than against
    a memory of it. tools/jarvis_lint/baseline.json records them; the gate below
    fails on anything outside it. See tools/jarvis_lint/framework.py.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import jarvis_lint                                    # noqa: E402
from tools.jarvis_lint.framework import Module, _RULES           # noqa: E402


def check(source: str, rule: str, rel: str = "actions/probe.py") -> list:
    """Run one rule over a snippet and return its findings."""
    code = textwrap.dedent(source)
    mod = Module(path=ROOT / rel, rel=rel, source=code, tree=ast.parse(code))
    return list(_RULES[rule].fn(mod))


def fires(source: str, rule: str, rel: str = "actions/probe.py") -> bool:
    return bool(check(source, rule, rel))


# ── the gate ─────────────────────────────────────────────────────────────────

def test_no_violation_outside_the_baseline():
    """The rule that actually protects the repository.

    Everything else in this file protects *this* rule.
    """
    findings = jarvis_lint.run(ROOT)
    new, _ = jarvis_lint.partition(findings, jarvis_lint.load_baseline())
    assert not new, (
        "new architecture/safety violations — each message says what would "
        "clear it:\n  " + "\n  ".join(f.render() for f in new)
    )


def test_the_baseline_names_only_defects_that_still_exist():
    """A baseline entry that no longer reproduces is history pretending to be
    debt. It also means the next real occurrence of that exact defect would be
    silently forgiven, which is the opposite of what this file is for."""
    findings = jarvis_lint.run(ROOT)
    _, stale = jarvis_lint.partition(findings, jarvis_lint.load_baseline())
    assert not stale, (
        "these baseline entries no longer reproduce — delete them from "
        f"tools/jarvis_lint/baseline.json: {stale}"
    )


# ── JAR001 · a model-controlled path is confined before it is written ────────

def test_jar001_fires_on_a_path_read_out_of_a_model_document():
    assert fires("""
        def _write_file(file_info, project_dir):
            file_path = file_info["path"]
            full_path = project_dir / file_path
            full_path.write_text("code", encoding="utf-8")
    """, "JAR001")


def test_jar001_follows_taint_through_a_list_and_a_loop():
    """actions/dev_agent.py::_fix_files is the same defect as _write_file with
    two extra hops. A single-pass, assignment-only version of this rule caught
    one and missed the other — a path rule a `for` loop walks around is not a
    path rule."""
    assert fires("""
        def _fix_files(all_files, project_dir):
            todo = []
            for fi in all_files:
                todo.append(fi["path"])
            for fix_path in todo:
                full_path = project_dir / fix_path
                full_path.write_text("fixed", encoding="utf-8")
    """, "JAR001")


def test_jar001_accepts_a_write_that_checks_containment():
    assert not fires("""
        def _write_file(file_info, project_dir):
            full_path = (project_dir / file_info["path"]).resolve()
            if not full_path.is_relative_to(project_dir.resolve()):
                raise ValueError("outside the project")
            full_path.write_text("code", encoding="utf-8")
    """, "JAR001")


def test_jar001_accepts_a_name_that_has_been_laundered():
    """`re.sub` to a safe alphabet is what actions/dev_agent.py already does to
    the project name, and it is why that line is not a finding."""
    assert not fires("""
        def _build(plan, root):
            name = re.sub(r"[^\\w\\-]", "_", plan["project_name"])
            (root / name).mkdir(parents=True, exist_ok=True)
    """, "JAR001")


def test_jar001_ignores_a_path_the_function_built_itself():
    """The near miss that matters: almost every file write in this codebase
    joins a non-literal. Flagging those is how the rule became noise the first
    time it ran — fourteen findings, thirteen of them ordinary."""
    assert not fires("""
        def _save_to_desktop(content):
            desktop = Path.home() / "Desktop"
            stamp = datetime.now().strftime("%Y%m%d")
            filepath = desktop / f"flights_{stamp}.txt"
            filepath.write_text(content, encoding="utf-8")
    """, "JAR001")


# ── JAR002 · a model identifier lives only in core/ai ────────────────────────

def test_jar002_fires_on_a_pinned_model_in_an_action():
    assert fires('MODEL_WRITER = "gemini-flash-latest"', "JAR002")


def test_jar002_allows_the_gateway_to_name_models():
    assert not fires('MODEL = "claude-opus-5"', "JAR002", rel="core/ai/anthropic.py")


def test_jar002_does_not_flag_a_transport_or_product_name():
    """core/voice/gemini_live.py sets `name = "gemini-live"`, which is a
    transport. A rule that cannot tell a model from a product name gets an
    exemption list on day one."""
    assert not fires('name = "gemini-live"', "JAR002")
    assert not fires('tool = "claude-code"', "JAR002")


# ── JAR003 · executing a tool requires a verdict ─────────────────────────────

def test_jar003_fires_when_a_plugin_runs_with_no_gate():
    assert fires("""
        async def _execute_tool(self, name, args):
            if self._plugin_registry.has(name):
                return self._plugin_registry.run(name, args)
    """, "JAR003", rel="main.py")


def test_jar003_accepts_a_gate_that_dominates_the_call():
    assert not fires("""
        async def _execute_tool(self, name, args):
            parked = tool_policy.gate(name, args, handler)
            if parked is None:
                return self._plugin_registry.run(name, args)
    """, "JAR003", rel="main.py")


def test_jar003_rejects_a_gate_that_only_guards_a_sibling_branch():
    """The exact shape of the bug. main.py's gate sits inside
    `if handler is not None:`, and the plugin branch is reached precisely when
    handler *is* None — so the gate is present in the function, present above
    the call, and guards nothing. Presence is not dominance."""
    assert fires("""
        async def _execute_tool(self, name, args):
            handler = self._sync_handlers(args).get(name)
            if handler is not None:
                parked = tool_policy.gate(name, args, handler)
            else:
                return self._plugin_registry.run(name, args)
    """, "JAR003", rel="main.py")


# ── JAR004 · an unmet constraint is not swallowed ────────────────────────────

def test_jar004_fires_when_nomodelfits_widens_the_provider_list():
    assert fires("""
        def _resolve(needs, tier, budget):
            try:
                ranked = router.candidates(budget, available=reachable)
            except NoModelFits:
                ranked = []
            for name in _PROVIDERS:
                ranked.append(name)
            return ranked
    """, "JAR004", rel="core/ai/__init__.py")


def test_jar004_accepts_reporting_the_refusal():
    """core/ai/router.py::explain catches NoModelFits and returns str(e). It
    reports the refusal instead of working around it, which is the whole
    distinction the rule is drawing."""
    assert not fires("""
        def explain(budget):
            try:
                rows = candidates(budget)
            except NoModelFits as e:
                return str(e)
            return rows
    """, "JAR004", rel="core/ai/router.py")


def test_jar004_does_not_flag_the_documented_provider_fallback():
    """QuotaExceeded means 'this provider failed', not 'what you asked for is
    impossible'. Trying the next one is the gateway's documented behaviour, and
    an early version of this rule flagged it."""
    assert not fires("""
        def generate(prompt):
            for provider in candidates:
                try:
                    return provider.generate(prompt)
                except (ProviderUnavailable, QuotaExceeded) as e:
                    last = e
                    continue
    """, "JAR004", rel="core/ai/__init__.py")


# ── JAR005 · a raised flag is lowered in a finally ───────────────────────────

def test_jar005_fires_when_a_busy_flag_can_leak():
    assert fires("""
        async def _execute_tool(self):
            try:
                self._vision_busy = True
                data = capture()
            except Exception:
                pass
    """, "JAR005", rel="main.py")


def test_jar005_accepts_a_flag_cleared_in_a_finally():
    assert not fires("""
        async def _execute_tool(self):
            try:
                self._vision_busy = True
                data = capture()
            finally:
                self._vision_busy = False
    """, "JAR005", rel="main.py")


# ── JAR006 · executor work is collected ──────────────────────────────────────

def test_jar006_fires_on_a_discarded_future():
    assert fires("""
        async def serve(self):
            loop.run_in_executor(None, _ensure_crypto_js)
    """, "JAR006", rel="dashboard/server.py")


def test_jar006_accepts_a_future_that_is_kept():
    assert not fires("""
        async def serve(self):
            fut = loop.run_in_executor(None, _ensure_crypto_js)
            fut.add_done_callback(_report)
    """, "JAR006", rel="dashboard/server.py")


# ── JAR007 · a GET does not change state ─────────────────────────────────────

def test_jar007_fires_on_a_get_that_spends_a_credential():
    assert fires("""
        @app.get("/auto-login")
        async def auto_login(req, key=""):
            if not self._creds.redeem_pairing_key(key):
                return error()
    """, "JAR007", rel="dashboard/server.py")


def test_jar007_accepts_the_same_work_behind_post():
    assert not fires("""
        @app.post("/login")
        async def login(req):
            if not self._creds.redeem_pairing_key(pin):
                return error()
    """, "JAR007", rel="dashboard/server.py")


def test_jar007_does_not_flag_a_get_that_only_reads():
    assert not fires("""
        @app.get("/api/files")
        async def list_files(req):
            return JSONResponse({"files": self._listing()})
    """, "JAR007", rel="dashboard/server.py")


# ── JAR008 · a request-indexed map declares a cap ────────────────────────────

def test_jar008_fires_on_a_map_with_no_length_test():
    assert fires("""
        class Throttle:
            def retry_after(self, who):
                hits = [t for t in self._fails.get(who, [])]
                self._fails[who] = hits

            def clear(self, who):
                self._fails.pop(who, None)
    """, "JAR008", rel="dashboard/auth.py")


def test_jar008_accepts_a_map_that_refuses_to_grow():
    """CredentialStore._devices, which is correct — and note that Throttle's
    `.pop()` above is NOT accepted. Popping on the success path is cleanup;
    the question is whether any input makes the dict refuse to grow."""
    assert not fires("""
        class CredentialStore:
            def pair_device(self, token, session):
                while len(self._devices) >= MAX_DEVICES:
                    del self._devices[oldest]
                self._devices[token] = session
    """, "JAR008", rel="dashboard/auth.py")


def test_jar008_is_scoped_to_the_network_facing_surface():
    """Everything else in Alexio grows a dict at the speed a human types."""
    assert not fires("""
        class SystemMonitor:
            def alert(self, key):
                self._last_alert[key] = time.time()
    """, "JAR008", rel="actions/system_monitor.py")


# ── JAR009 · a spec does not hand out its own state ──────────────────────────

def test_jar009_fires_when_a_dialect_returns_the_schema_by_reference():
    assert fires("""
        class ToolSpec:
            parameters: dict

            def to_anthropic(self):
                return {"name": self.name, "input_schema": self.parameters}
    """, "JAR009", rel="core/ai/tools.py")


def test_jar009_accepts_a_dialect_that_builds_a_new_document():
    assert not fires("""
        class ToolSpec:
            parameters: dict

            def to_gemini(self):
                return {"parameters": _convert_types(self.parameters, TABLE)}
    """, "JAR009", rel="core/ai/tools.py")


def test_jar009_does_not_flag_reading_the_field():
    """An early version flagged a __repr__ and a boolean. Mentioning the field
    is not handing it out — and a rule that flags __repr__ is a rule that gets
    switched off."""
    assert not fires("""
        class VoiceEvent:
            calls: list

            def __repr__(self):
                return f"VoiceEvent({self.calls})"

            def is_full(self):
                return len(self.calls) >= 5
    """, "JAR009", rel="core/voice/types.py")


# ── JAR010 · the routing path does not parse the whole log ───────────────────

def test_jar010_fires_on_an_unbounded_log_read():
    assert fires("""
        def summarise(min_samples=5):
            for r in read_rows():
                pass
    """, "JAR010", rel="core/telemetry.py")


def test_jar010_accepts_a_bounded_read():
    assert not fires("""
        def summarise(min_samples=5):
            for r in read_rows(limit=5000):
                pass
    """, "JAR010", rel="core/telemetry.py")


# ── JAR011 · an optional native import is guarded with except Exception ──────

def test_jar011_fires_on_a_narrow_guard_around_a_native_package():
    assert fires("""
        try:
            import psutil
            _PSUTIL = True
        except ImportError:
            _PSUTIL = False
    """, "JAR011")


def test_jar011_accepts_except_exception():
    assert not fires("""
        try:
            import psutil
            _PSUTIL = True
        except Exception:
            _PSUTIL = False
    """, "JAR011")


def test_jar011_leaves_pure_python_guards_alone():
    """`except ImportError` around a pure-Python parser is honest and stays
    legal — the rule is about packages that reach for the machine."""
    assert not fires("""
        try:
            import docx
        except ImportError:
            docx = None
    """, "JAR011")


# ── the meta-rules ───────────────────────────────────────────────────────────

def test_every_rule_says_what_it_is_for():
    """A rule whose reason is not written down is a rule the next person
    deletes when it becomes inconvenient — and they will be right to."""
    for meta in jarvis_lint.all_rules():
        assert len(meta.why) > 120, (
            f"{meta.code} does not say which defect it came from. A one-line "
            f"`why` is how a guardrail becomes cargo cult."
        )
        assert meta.title and meta.title[0].islower()


def test_every_rule_has_a_positive_and_a_negative_test():
    """A new rule cannot land with only the happy case checked.

    Counted from this file's own source: a rule needs at least one test naming
    it that asserts `fires(...)` and at least one that asserts `not fires(...)`.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    positive: set[str] = set()
    negative: set[str] = set()

    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_"):
            continue
        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "fires"):
                continue
            codes = [a.value for a in call.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)
                     and a.value.startswith("JAR")]
            parent_is_not = any(
                isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not)
                and call in ast.walk(n)
                for n in ast.walk(fn)
            )
            for code in codes:
                (negative if parent_is_not else positive).add(code)

    for meta in jarvis_lint.all_rules():
        assert meta.code in positive, (
            f"{meta.code} has no test proving it fires on the defect it was "
            f"written for. Add one to tests/test_architecture_rules.py."
        )
        assert meta.code in negative, (
            f"{meta.code} has no test proving it stays quiet on correct code. "
            f"A rule with no negative test is a rule nobody has checked for "
            f"false positives."
        )


@pytest.fixture(scope="module")
def every_module() -> list:
    """The whole tree, parsed once.

    Parsing 103 files inside a test parametrised over 20 rules means parsing
    them 2 060 times. Module-scoped so the parametrisation stays readable and
    the cost does not.
    """
    from tools.jarvis_lint.framework import load_module, project_files
    return [m for m in (load_module(p, ROOT) for p in project_files(ROOT)) if m]


@pytest.mark.parametrize("meta", jarvis_lint.all_rules(), ids=lambda m: m.code)
def test_every_rule_survives_the_whole_tree(meta, every_module):
    """Each rule must run over every project file without raising.

    A rule that crashes on one unusual file is a rule that silently stops
    protecting everything after it in the walk.
    """
    for mod in every_module:
        list(meta.fn(mod))


# ═══════════════════════════════════════════════════════════════════════════
# The execution family — what may become a command, and what may become a file
# ═══════════════════════════════════════════════════════════════════════════

# ── JAR012 · shell=True takes a literal command ──────────────────────────────

def test_jar012_fires_on_a_tool_parameter_reaching_the_shell():
    """actions/open_app.py:84. The `which()` above it validates a prefix and the
    Popen below runs the whole string, so "notepad.exe & calc" resolves and then
    executes both halves."""
    assert fires("""
        def _launch_windows(app_name):
            if shutil.which(app_name) or shutil.which(app_name.split(".")[0]):
                subprocess.Popen(app_name, shell=True)
    """, "JAR012", rel="actions/open_app.py")


def test_jar012_fires_on_an_interpolated_command():
    assert fires("""
        def _launch_windows(app_name):
            if ":" in app_name:
                subprocess.Popen(f"start {app_name}", shell=True)
    """, "JAR012", rel="actions/open_app.py")


def test_jar012_fires_on_a_list_with_shell_true():
    """Its own bug, unrelated to taint: POSIX hands sh -c only the first element
    and drops the rest, so the arguments silently vanish."""
    assert fires("""
        def _open_vscode(project_dir):
            subprocess.Popen([cmd, str(project_dir)], shell=True)
    """, "JAR012", rel="actions/dev_agent.py")


def test_jar012_allows_a_command_written_in_the_file():
    assert not fires("""
        def _probe():
            subprocess.run("wmic path Win32_VideoController get name", shell=True)
    """, "JAR012", rel="core/desktop/windows.py")


def test_jar012_ignores_subprocess_without_a_shell():
    """An argument list without shell=True is the fix, not the defect — the rule
    must not push people away from it."""
    assert not fires("""
        def _launch(app_name):
            subprocess.Popen(["open", "-a", app_name])
    """, "JAR012", rel="actions/open_app.py")


# ── JAR013 · a command line is not produced by .split() ──────────────────────

def test_jar013_fires_on_a_command_split_on_whitespace():
    assert fires("""
        def _run_project(run_command, project_dir):
            parts = run_command.split()
            result = subprocess.run(parts, cwd=str(project_dir))
    """, "JAR013", rel="actions/dev_agent.py")


def test_jar013_accepts_shlex():
    assert not fires("""
        def _run_project(run_command, project_dir):
            parts = shlex.split(run_command)
            result = subprocess.run(parts, cwd=str(project_dir))
    """, "JAR013", rel="actions/dev_agent.py")


def test_jar013_ignores_a_split_that_never_reaches_a_process():
    assert not fires("""
        def _first_word(line):
            words = line.split()
            return words[0]
    """, "JAR013", rel="actions/dev_agent.py")


# ── JAR014 · a temporary file is created, not merely named ───────────────────

def test_jar014_fires_on_mktemp():
    assert fires("""
        def screenshot():
            out = Path(tempfile.mktemp(suffix=".png"))
    """, "JAR014", rel="core/desktop/macos.py")


def test_jar014_accepts_mkstemp():
    assert not fires("""
        def screenshot():
            fd, name = tempfile.mkstemp(suffix=".png")
            out = Path(name)
    """, "JAR014", rel="core/desktop/macos.py")


# ── JAR019 · TLS verification stays on ───────────────────────────────────────

def test_jar019_fires_on_verify_false():
    assert fires("""
        def fetch(url):
            return requests.get(url, verify=False)
    """, "JAR019", rel="actions/web_search.py")


def test_jar019_fires_on_an_unverified_context():
    assert fires("""
        def fetch(url):
            ctx = ssl._create_unverified_context()
            return urlopen(url, context=ctx)
    """, "JAR019", rel="actions/web_search.py")


def test_jar019_allows_pinning_the_certificate():
    assert not fires("""
        def fetch(url):
            return requests.get(url, verify="config/certs/jarvis.crt")
    """, "JAR019", rel="actions/web_search.py")


# ═══════════════════════════════════════════════════════════════════════════
# The policy family — the gate, the undo, and the claims that justify RUN
# ═══════════════════════════════════════════════════════════════════════════

# ── JAR015 · the verdict is read ─────────────────────────────────────────────

def test_jar015_fires_when_the_gate_is_called_for_its_side_effect():
    """The bug that comes after JAR003 is fixed: a gate that is consulted,
    ignored, and then reads as a gate in review."""
    assert fires("""
        async def _execute_tool(self, name, args):
            tool_policy.gate(name, args, handler)
            return await loop.run_in_executor(None, handler)
    """, "JAR015", rel="main.py")


def test_jar015_accepts_a_gate_whose_answer_decides():
    assert not fires("""
        async def _execute_tool(self, name, args):
            parked = tool_policy.gate(name, args, handler)
            if parked is not None:
                return parked
            return await loop.run_in_executor(None, handler)
    """, "JAR015", rel="main.py")


def test_jar015_leaves_the_suite_alone():
    """A test drives the gate for its side effect and then resolves the
    confirmation itself — the one caller with nothing to read."""
    assert not fires("""
        def test_cancelling_runs_nothing():
            tool_policy.gate("dev_agent", {"description": "x"}, lambda: "b")
            confirm.resolve(False)
    """, "JAR015", rel="tests/test_tool_policy.py")


# ── JAR016 · destroying something registers an undo (R-05) ───────────────────

def test_jar016_fires_on_a_delete_with_no_undo():
    assert fires("""
        def clean_desktop(target):
            target.unlink()
    """, "JAR016", rel="actions/desktop.py")


def test_jar016_accepts_a_module_that_registers_reversals():
    assert not fires("""
        def delete(target):
            push_undo(f"deleted {target.name}", _restore(target))
            target.unlink()
    """, "JAR016", rel="actions/file_controller.py")


def test_jar016_does_not_confuse_str_replace_with_path_replace():
    """`"a b".replace(" ", "_")` is a sanitiser and `Path.replace(target)` is an
    overwrite. They share a name and nothing else — before the arity check this
    rule reported ten sanitisers as data loss, including one literally called
    _sanitise."""
    assert not fires("""
        def _sanitise(text):
            return text.replace(" ", "_").replace("/", "-")
    """, "JAR016", rel="actions/reminder.py")


def test_jar016_ignores_cleaning_up_its_own_temporary_file():
    assert not fires("""
        def set_wallpaper_from_url(url):
            tmp = Path(tempfile.mktemp(suffix=".png"))
            tmp.write_bytes(download(url))
            apply(tmp)
            tmp.unlink()
    """, "JAR016", rel="actions/desktop.py")


# ── JAR017 · secrets compare in constant time ────────────────────────────────

def test_jar017_fires_on_an_equality_check_between_secrets():
    assert fires("""
        def redeem(entered_token, stored_token):
            if entered_token == stored_token:
                return True
    """, "JAR017", rel="dashboard/auth.py")


def test_jar017_accepts_compare_digest():
    assert not fires("""
        def redeem(entered_token, stored_token):
            if secrets.compare_digest(entered_token, stored_token):
                return True
    """, "JAR017", rel="dashboard/auth.py")


def test_jar017_ignores_a_presence_check():
    """`if token == ""` asks whether anything was sent at all. Timing tells an
    attacker nothing they did not already know."""
    assert not fires("""
        def resolve(token):
            if token == "":
                return None
    """, "JAR017", rel="dashboard/auth.py")


# ── JAR018 · secrets stay out of logs ────────────────────────────────────────

def test_jar018_fires_when_a_token_is_printed():
    assert fires("""
        def open_session(self):
            bearer = secrets.token_urlsafe(32)
            print(f"[Dashboard] issued {bearer}")
    """, "JAR018", rel="dashboard/server.py")


def test_jar018_accepts_logging_around_a_secret():
    assert not fires("""
        def open_session(self):
            bearer = secrets.token_urlsafe(32)
            print(f"[Dashboard] issued a session token ({len(bearer)} chars)")
    """, "JAR018", rel="dashboard/server.py")


# ── JAR020 · a sandbox claim is backed by a test ─────────────────────────────

def test_jar020_fires_on_an_untested_sandbox_justification(tmp_path, monkeypatch):
    """The justification for executing model-authored code with no human in the
    loop is one sentence in a dict. It should cost a test.

    Pointed at an empty tests directory: the real one now contains that test —
    which is the rule having worked — so without this the positive case could
    only be demonstrated by deleting the fix.
    """
    from tools.jarvis_lint import architecture_rules
    monkeypatch.setattr(architecture_rules, "_TESTS_DIR", tmp_path)
    assert fires('''
        _REVERSIBLE = {
            "desktop_control": "the generated code runs in a sandbox with no "
                               "deletion, no rmtree and no subprocess",
        }
    ''', "JAR020", rel="core/tool_policy.py")


def test_jar020_ignores_rationales_that_claim_no_sandbox():
    assert not fires('''
        _REVERSIBLE = {
            "web_search": "reads the network, writes nothing",
        }
    ''', "JAR020", rel="core/tool_policy.py")


def test_jar003_accepts_a_closure_handed_to_the_gate():
    """The idiom the design asks for, and the one the line-number heuristic got
    wrong: the callable must be defined *before* the gate that takes it, so the
    gate always sits textually below the sink. core/confirm.py stores exactly
    such a callable to run later if a human presses CONFIRM."""
    assert not fires("""
        async def _execute_tool(self, name, args):
            if self._plugin_registry.has(name):
                def _run_plugin(n=name, a=args):
                    return self._plugin_registry.run(n, a, player=self.ui)
                parked = tool_policy.gate(name, args, _run_plugin)
                if parked is not None:
                    return parked
                return await loop.run_in_executor(None, _run_plugin)
    """, "JAR003", rel="main.py")


def test_jar005_accepts_a_nested_try_that_releases_the_flag():
    """The fix for audit #5 kept firing the rule that found it: main.py's inner
    try/finally releases the flag, and the outer try still lexically contains
    the assignment. What matters is that *some* handler releases it."""
    assert not fires("""
        async def _execute_tool(self):
            try:
                if ok:
                    self._vision_busy = True
                    try:
                        data = capture()
                        handed = True
                    finally:
                        if not handed:
                            self._vision_busy = False
            except Exception:
                pass
    """, "JAR005", rel="main.py")
