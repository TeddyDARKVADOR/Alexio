"""
The guarantees, tested as guarantees.

WHY THIS FILE IS NOT test_ai_routing.py OR test_dashboard_security.py
    Those test units. This tests the sentences the documentation makes to the
    user — "a private budget does not leave the machine", "revoking a device
    stops it" — because the 2026-09-08 audit found several of those sentences
    false in modules with excellent line coverage. core/ai/router.py measured
    97% with `Budget.private` at 100% while the private-budget guarantee was
    broken, and the raise it emits was swallowed one module up.

    Every test here names the audit finding it closes. If one starts failing,
    something that was true for the user has stopped being true — which is a
    different kind of alarm from a unit test going red, and worth reading
    differently.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import ai                                    # noqa: E402
from core.paths import PathEscape, safe_join           # noqa: E402


# ── #1 · a budget is a constraint, not a preference (R-12) ───────────────────

REMOTE_ONLY = {"gemini", "anthropic", "openai"}


def test_a_private_budget_refuses_rather_than_going_to_a_vendor():
    """Audit #1, the worst of the twenty-five.

    `_resolve` caught NoModelFits, set `ranked = []`, and fell through to a loop
    that appends every reachable provider. So Budget.private() — documented as
    "ne quitte pas la machine" — shipped the prompt to Gemini, Anthropic and
    OpenAI in exactly the situation the guarantee exists for: no local model
    installed. The guarantee did not merely fail, it inverted.
    """
    with mock.patch.object(ai, "reachable_providers", lambda: REMOTE_ONLY):
        with pytest.raises(ai.NoModelFits):
            ai._resolve({"text"}, ai.Tier.FAST, ai.Budget.private())


def test_an_impossible_latency_budget_raises():
    with mock.patch.object(ai, "reachable_providers", lambda: REMOTE_ONLY):
        with pytest.raises(ai.NoModelFits):
            ai._resolve({"text"}, ai.Tier.FAST,
                        ai.Budget(tier=ai.Tier.FAST, max_latency_ms=1))


def test_an_impossible_cost_budget_raises():
    with mock.patch.object(ai, "reachable_providers", lambda: REMOTE_ONLY):
        with pytest.raises(ai.NoModelFits):
            ai._resolve({"text"}, ai.Tier.FAST,
                        ai.Budget(tier=ai.Tier.FAST, max_cost_usd=0.0))


def test_an_unconstrained_request_still_falls_back():
    """The other half of the fix, and the reason it is not just `raise`.

    A caller who asked only for a tier has asserted nothing the catalogue could
    violate, so a provider the catalogue has not caught up with may still
    answer. Removing that would trade a broken promise for a broken assistant.
    """
    with mock.patch.object(ai, "reachable_providers", lambda: REMOTE_ONLY):
        got = ai._resolve({"text"}, ai.Tier.FAST, None)
    assert [m.NAME for m in got], "an unconstrained request must still resolve"


def test_the_cost_shape_a_caller_described_survives_resolution():
    """Audit #12. _resolve rebuilt the Budget field by field and dropped
    est_tokens_in / est_tokens_out / est_cached_in, so a caller's max_cost_usd
    was checked against the median-request defaults instead of their numbers."""
    seen = {}
    real = ai.router.candidates

    def spy(budget, **kw):
        seen.update(tokens_in=budget.est_tokens_in,
                    tokens_out=budget.est_tokens_out,
                    cached_in=budget.est_cached_in)
        return real(budget, **kw)

    asked = ai.Budget(tier=ai.Tier.FAST, est_tokens_in=99,
                      est_tokens_out=7, est_cached_in=3)
    with mock.patch.object(ai.router, "candidates", spy):
        ai._resolve({"text"}, ai.Tier.FAST, asked)

    assert seen == {"tokens_in": 99, "tokens_out": 7, "cached_in": 3}


# ── #2 · a model-authored path stays in the project ──────────────────────────

@pytest.mark.parametrize("hostile", [
    "../../.bashrc",
    "/etc/cron.d/x",                    # pathlib DISCARDS the base for these
    "a/../../../../tmp/pwned.py",
    "~/outside.py",                     # meaning depends on who expands it
    "../" * 12 + "etc/passwd",
])
def test_a_hostile_path_cannot_leave_the_project(tmp_path, hostile):
    """Audit #2. `project_dir / file_path` was the entire check, and it is not
    one: pathlib returns the tail unchanged when the tail is absolute, so a plan
    naming "/etc/cron.d/x" wrote to /etc/cron.d/x with no error at all."""
    with pytest.raises(PathEscape):
        safe_join(tmp_path, hostile)


def test_an_ordinary_project_path_still_works(tmp_path):
    assert safe_join(tmp_path, "utils/helpers.py").relative_to(tmp_path) \
        == Path("utils/helpers.py")


def test_a_symlink_out_of_the_project_is_refused(tmp_path):
    """resolve() follows links, so a link inside the root pointing out of it
    fails the same comparison as any other escape. Worth its own test because
    the traversal cases all fail on string arithmetic and this one does not."""
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "project"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathEscape):
        safe_join(root, "link/stolen.py")


def test_dev_agent_writes_through_the_confinement_helper():
    """The unit above proves safe_join refuses; this proves dev_agent uses it.

    Checked against the source because the alternative is running the planner,
    and a guarantee that can only be verified by calling a model is a guarantee
    nobody verifies.
    """
    src = (ROOT / "actions" / "dev_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    joins = [n for n in ast.walk(tree)
             if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)
             and isinstance(n.right, ast.Name)
             and n.right.id in ("file_path", "fix_path")]
    assert not joins, (
        "actions/dev_agent.py joins a planner-supplied path with `/` again — "
        "use core.paths.safe_join(project_dir, …)"
    )
    assert src.count("safe_join(project_dir") >= 2


# ── #3 · no tool executes without a verdict (R-18) ───────────────────────────

def test_a_plugin_classified_confirm_does_not_run_on_the_models_word(ui):
    """Audit #3. core/tool_policy.register() exists *only* so a plugin can
    declare a CONFIRM rule — plugins load after the table, so they cannot use
    the decorator — and the plugin branch of the dispatch consulted nothing, so
    a plugin that registered one ran anyway.

    Driven through tool_policy.gate() rather than through JarvisLive, which
    needs a microphone and a Live session to construct.
    """
    from core import confirm, tool_policy

    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    ran: list[int] = []
    tool_policy.register("evil_plugin", lambda _a: tool_policy.Verdict(
        tool_policy.CONFIRM, "Do the irreversible thing", "detail", "a test"))
    try:
        parked = tool_policy.gate("evil_plugin", {}, lambda: ran.append(1) or "done")
        assert parked is not None, "a CONFIRM plugin was allowed to run"
        assert not ran, "the plugin ran before anyone confirmed"

        confirm.resolve(True)
        import time
        for _ in range(100):
            if ran:
                break
            time.sleep(0.01)
        assert ran, "the plugin did not run after the user confirmed"
    finally:
        tool_policy._RULES.pop("evil_plugin", None)


def test_the_dispatch_gates_the_plugin_branch():
    """The wiring, checked against main.py's source.

    tests/test_tool_policy.py already checks that every CONFIRM *core* tool has
    a gate in its branch; nothing checked the branch that has no `elif name ==`
    to look at.
    """
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    body = src.split("async def _execute_tool")[1].split("\n    async def ")[0]
    plugin_branch = body.split("_plugin_registry.has(name)")[1]
    assert "tool_policy.gate(" in plugin_branch, (
        "the plugin branch of _execute_tool runs a tool without a verdict (R-18)"
    )


# ── #22 · a tool parameter never reaches a shell ─────────────────────────────

def test_open_app_never_builds_a_shell_command_line():
    """Audit #22, found while writing JAR012 rather than during the audit.

    `shutil.which(app_name.split(".")[0])` answers yes for "notepad.exe & calc",
    and the Popen below it ran the whole string through cmd. The guard validated
    a prefix; the call executed the lot. app_name is a tool parameter, so the
    model wrote it, and open_app is classified RUN, so nothing asked first.
    """
    tree = ast.parse((ROOT / "actions" / "open_app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if any(kw.arg == "shell" and getattr(kw.value, "value", None) is True
               for kw in node.keywords):
            pytest.fail(f"actions/open_app.py:{node.lineno} still uses shell=True")


@pytest.mark.parametrize("hostile", [
    "notepad.exe & calc",
    "notepad.exe && calc",
    "notepad.exe | calc",
    "ms-settings: & calc",
    'x" & calc & "',
])
def test_a_hostile_app_name_launches_nothing(hostile, monkeypatch):
    """The behaviour, not the shape: whatever open_app does with a hostile name,
    it must not produce a command line with a shell metacharacter in it."""
    import actions.open_app as oa

    launched: list = []
    monkeypatch.setattr(oa.subprocess, "Popen",
                        lambda *a, **kw: launched.append((a, kw)) or None)
    monkeypatch.setattr(oa.shutil, "which", lambda name: None)
    monkeypatch.setattr(oa.time, "sleep", lambda _s: None)

    # The last-resort branch drives the Start menu through core/desktop, which
    # on this machine means the Wayland RemoteDesktop portal — a real D-Bus call
    # that sits out its timeout. The suite runs with no screen (pytest.ini), and
    # without this the five parametrised cases cost thirty seconds each.
    from core import desktop as real_desktop
    for name in ("key", "type_text"):
        monkeypatch.setattr(real_desktop, name, lambda *a, **kw: None)

    try:
        oa._launch_windows(hostile)
    except Exception:
        pass

    for args, kwargs in launched:
        assert kwargs.get("shell") is not True, "a shell was involved"
        argv = args[0] if args else []
        assert not isinstance(argv, str), f"a command *string* was built: {argv!r}"
        joined = " ".join(str(a) for a in argv)
        assert "&" not in joined and "|" not in joined, \
            f"a shell metacharacter survived into argv: {joined!r}"


def test_a_real_executable_is_launched_by_resolved_path(monkeypatch):
    """The fix must not break the feature it protects."""
    import actions.open_app as oa

    launched: list = []
    monkeypatch.setattr(oa.shutil, "which",
                        lambda name: "/usr/bin/notepad" if name == "notepad" else None)
    monkeypatch.setattr(oa.subprocess, "Popen",
                        lambda *a, **kw: launched.append(a[0]) or None)
    monkeypatch.setattr(oa.time, "sleep", lambda _s: None)

    assert oa._launch_windows("notepad") is True
    assert launched == [["/usr/bin/notepad"]]


# ── #7 · revoking a device stops what it opened ──────────────────────────────

def test_revoking_devices_also_kills_their_live_sessions():
    """Audit #7, and the clearest case in the repo of a green test that does not
    check the guarantee.

    test_revoking_devices_actually_revokes_them passed throughout: it asserts the
    device dictionary is emptied. It does not assert that the bearer token the
    phone is holding stops working — which is what "revoke" means to the person
    pressing the button. A stolen handset kept full API access for up to seven
    days, and every use slid the window forward.
    """
    from dashboard.auth import CredentialStore

    store = CredentialStore()
    _bearer, _nav, session = store.open_session()
    device_token = store.pair_device(session)

    reconnected = store.open_session_for_device(device_token)
    assert reconnected is not None
    phone_bearer, phone_nav, _ = reconnected
    assert store.session_for_bearer(phone_bearer) is not None

    store.revoke_devices()

    assert store.session_for_bearer(phone_bearer) is None, \
        "the phone's bearer token still works after revocation"
    assert store.session_for_nav(phone_nav) is None, \
        "the phone's navigation cookie still serves the page after revocation"
    assert store.open_session_for_device(device_token) is None


def test_revoking_devices_leaves_the_desktop_session_alone():
    """The near miss: revocation must cut the phones, not log the user out of
    the browser they are pressing the button in."""
    from dashboard.auth import CredentialStore

    store = CredentialStore()
    desk_bearer, _nav, _session = store.open_session()
    _b2, _n2, phone_session = store.open_session()
    store.pair_device(phone_session)

    store.revoke_devices()
    assert store.session_for_bearer(desk_bearer) is not None


# ── #13 · the failure map cannot grow from the network ───────────────────────

def test_merely_asking_leaves_nothing_behind():
    """Audit #13. retry_after wrote `self._fails[who] = hits` unconditionally,
    so every address that only *asked* left an entry, on an unauthenticated
    path, and nothing removed it."""
    from dashboard.auth import Throttle

    t = Throttle()
    for i in range(200):
        t.retry_after(f"10.0.0.{i}")
    assert not t._fails, "querying the throttle populated it"


def test_the_failure_map_is_capped():
    from dashboard.auth import MAX_TRACKED, Throttle

    t = Throttle()
    now = 1000.0
    for i in range(MAX_TRACKED * 2):
        t.fail(f"10.0.{i // 256}.{i % 256}", now=now + i * 0.001)
    assert len(t._fails) <= MAX_TRACKED


def test_throttling_still_works_after_the_cap(ui):
    """The cap must not cost the feature: an address that keeps failing is still
    refused."""
    from dashboard.auth import FAIL_LIMIT, Throttle

    t = Throttle()
    now = 1000.0
    for i in range(FAIL_LIMIT):
        t.fail("10.0.0.1", now=now + i)
    assert t.retry_after("10.0.0.1", now=now + FAIL_LIMIT) > 0


# ── #19 · a GET cannot spend a credential ────────────────────────────────────

def test_looking_at_the_qr_link_does_not_burn_the_key():
    """Audit #19. /auto-login redeemed the PIN, minted a session and paired a
    device on a GET, so a browser prefetch, a crawler or a link preview spent a
    live single-use credential just by looking at it."""
    from dashboard.server import DashboardServer
    fastapi_testclient = pytest.importorskip("fastapi.testclient")

    server = DashboardServer()
    key = server.new_key()
    client = fastapi_testclient.TestClient(server.app)

    first = client.get(f"/auto-login?key={key}")
    assert first.status_code == 200

    # The key must still be spendable afterwards — that is the whole point.
    paired = client.post("/auto-login", json={"key": key})
    assert paired.status_code == 200, "the GET consumed the pairing key"
    assert paired.json()["ok"] is True


def test_the_pairing_key_is_still_single_use_on_post():
    from dashboard.server import DashboardServer
    fastapi_testclient = pytest.importorskip("fastapi.testclient")

    server = DashboardServer()
    key = server.new_key()
    client = fastapi_testclient.TestClient(server.app)

    assert client.post("/auto-login", json={"key": key}).status_code == 200
    assert client.post("/auto-login", json={"key": key}).status_code == 401


# ── #11 · an expired confirmation takes its own banner down ──────────────────

def test_an_abandoned_confirmation_removes_its_banner(ui, monkeypatch):
    """Audit #11. The timeout was consulted only inside resolve(), so nothing
    ever called the hide callback: the banner stayed up, and a user who came
    back and pressed CONFIRM got a log line and no action."""
    from core import confirm

    confirm.bind(show=ui.show_confirm, hide=ui.hide_confirm, log=ui.write_log)
    confirm.request("shutdown", "Shut down", "the computer", lambda: "done")
    assert confirm.pending_title() == "Shut down"

    clock = [0.0]
    monkeypatch.setattr(confirm.time, "monotonic", lambda: clock[0])
    confirm.request("shutdown", "Shut down", "the computer", lambda: "done")
    clock[0] = confirm.TIMEOUT_SECONDS + 1

    hidden_before = ui.confirm_hides
    assert confirm.pending_title() == ""
    assert ui.confirm_hides > hidden_before, \
        "the expired banner was never taken off the screen"


# ── #4 · camera detection does not raise NameError ───────────────────────────

def test_camera_probing_does_not_reach_for_an_import_that_is_not_there():
    """Audit #4. `np.mean(frame)` with numpy never imported in that file: every
    camera probe that actually got a frame raised NameError, so the branch died
    precisely when it was working. pyflakes reported it all along."""
    import actions.screen_processor as sp

    # Against the AST, not the text: the explanation of this very bug lives in
    # a comment two lines above the fix, and a substring search finds it there.
    tree = ast.parse((ROOT / "actions" / "screen_processor.py")
                     .read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id == "np"], \
        "screen_processor references np without importing it"

    class _Cap:
        def isOpened(self): return True
        def read(self): return True, _Frame()
        def release(self): return None

    class _Frame:
        def mean(self): return 42.0

    sp._CV2 = True
    with mock.patch.object(sp, "cv2", mock.Mock(VideoCapture=lambda *a: _Cap()),
                           create=True):
        assert sp._probe_camera(0, 0, warmup=0) is True


# ── #8 · the routing path does not read the whole log ────────────────────────

def test_the_router_reads_a_window_not_the_whole_history(tmp_path, monkeypatch):
    """Audit #8. read_rows applied `limit` after parsing, so summarise() — which
    registry.load() calls on every routing decision — parsed both generations in
    full: 491 ms measured at the rotation threshold, against a 900 ms
    conversation budget."""
    import json as _json
    from core import telemetry

    log = tmp_path / "ai_calls.jsonl"
    row = _json.dumps({"provider": "gemini", "model": "m", "ok": True,
                       "latency_ms": 10.0}) + "\n"
    log.write_text(row * 40_000, encoding="utf-8")
    monkeypatch.setattr(telemetry, "LOG_PATH", log)
    monkeypatch.setattr(telemetry, "LOG_DIR", tmp_path)

    rows = telemetry.read_rows(limit=telemetry.ROUTING_WINDOW)
    assert len(rows) == telemetry.ROUTING_WINDOW, \
        "read_rows ignored its limit and read everything"


# ── #9 · the refusal names the real cause ────────────────────────────────────

def test_a_machine_with_no_api_key_is_told_so():
    """Audit #9. Providers absent from `available` were skipped with no reason
    recorded, so a fresh install with no key was told "the catalogue is empty" —
    the wrong cause on the most common failure there is."""
    from core.ai import router

    with pytest.raises(router.NoModelFits) as excinfo:
        router.candidates(router.Budget(), available=set())
    message = str(excinfo.value)
    assert "catalogue is empty" not in message
    assert "credentials" in message and "api_keys.json" in message


# ── #20 · a dialect cannot mutate the spec it came from ──────────────────────

def test_rendering_a_tool_twice_gives_the_same_document():
    """Audit #20. to_anthropic and to_openai returned self.parameters by
    reference, so an adapter that normalised the schema it was given edited the
    ToolSpec every other adapter reads next."""
    from core.ai.tools import ToolSpec

    spec = ToolSpec(name="t", description="d",
                    parameters={"type": "object",
                                "properties": {"a": {"type": "string"}}})
    first = spec.to_anthropic()
    first["input_schema"]["properties"]["a"]["type"] = "vandalised"
    first["input_schema"]["required"] = ["a"]

    assert spec.to_anthropic()["input_schema"]["properties"]["a"]["type"] == "string"
    assert "required" not in spec.to_openai()["function"]["parameters"]


# ── R-02 · a tool never names a model ────────────────────────────────────────

def test_no_action_module_pins_a_model():
    """Found while writing JAR002, not by the audit: dev_agent and code_helper
    each pinned "gemini-flash-latest" at module level. Both were **dead** — the
    shims ignored the argument and routed on the tier — so they pinned nothing
    while looking exactly like configuration, and the router could not see those
    modules at all."""
    from tools import jarvis_lint

    offenders = [f.render() for f in jarvis_lint.run(ROOT)
                 if f.rule == "JAR002"]
    assert not offenders, f"a module names a model outside core/ai (R-02): {offenders}"


# ── #24 · the sandbox blocks what its RUN verdict claims it blocks ───────────

SANDBOX_MUST_REFUSE = [
    pytest.param("Path('/tmp/x').unlink()", id="unlink"),
    pytest.param("shutil.rmtree('/tmp')", id="rmtree"),
    pytest.param("import subprocess", id="import subprocess"),
    pytest.param("__import__('subprocess').run(['id'])", id="__import__"),
    pytest.param("open('/etc/passwd').read()", id="open"),
    pytest.param("eval('1+1')", id="eval"),
    pytest.param("exec('x=1')", id="exec"),
    pytest.param("__builtins__['__import__']('os')", id="builtins __import__"),
]


@pytest.mark.parametrize("hostile", SANDBOX_MUST_REFUSE)
def test_the_desktop_sandbox_refuses_what_it_says_it_refuses(hostile):
    """Audit #24, found by writing JAR020 rather than by the audit.

    core/tool_policy.py lets desktop_control run ungated on the strength of one
    sentence — "the generated code runs in a sandbox with no deletion, no rmtree
    and no subprocess". That sentence is the entire justification for
    exec()-ing model-authored code with no human in the loop, and nothing in the
    suite checked any of the three. A claim load-bearing enough to skip a
    confirmation is load-bearing enough to test.
    """
    from actions.desktop import _build_sandbox

    sandbox = _build_sandbox()
    with pytest.raises(Exception):
        exec(compile(hostile, "<probe>", "exec"), sandbox)


def test_the_sandbox_still_allows_what_the_feature_needs():
    """The near miss: a sandbox that refuses everything is not a sandbox, it is
    a broken feature. desktop_control exists to read the desktop and copy files."""
    from actions.desktop import _build_sandbox

    sandbox = _build_sandbox()
    seen: list = []
    sandbox["__builtins__"]["print"] = lambda *a: seen.append(" ".join(map(str, a)))
    exec(compile("print(len(sorted([3, 1, 2])))", "<probe>", "exec"), sandbox)
    assert seen == ["3"]


def test_tool_policy_still_claims_a_sandbox():
    """If the claim is ever dropped from core/tool_policy.py the tests above stop
    guarding anything, and would keep passing while doing so."""
    from core import tool_policy

    assert "sandbox" in tool_policy.classify("desktop_control", {}).reason


# ── #18 · a pinned static asset is verified once, not per request ────────────

def test_the_crypto_bundle_is_not_rehashed_on_every_request(tmp_path, monkeypatch):
    """Audit #18. serve_crypto re-read and re-hashed 50 kB of JavaScript on every
    page load. A file that changed would change its mtime too, so the stat is
    enough — and a replacement is still caught, which the test below proves."""
    from dashboard import server

    bundle = tmp_path / "crypto-js.min.js"
    bundle.write_bytes(b"x" * 1024)
    calls = {"n": 0}
    real = server._digest_ok

    def counting(data):
        calls["n"] += 1
        return True

    monkeypatch.setattr(server, "_digest_ok", counting)
    monkeypatch.setattr(server, "_verified_stat", None)

    assert server._cached_digest_ok(bundle) is True
    assert server._cached_digest_ok(bundle) is True
    assert server._cached_digest_ok(bundle) is True
    assert calls["n"] == 1, "the bundle was hashed more than once"


def test_a_replaced_bundle_is_hashed_again(tmp_path, monkeypatch):
    from dashboard import server

    bundle = tmp_path / "crypto-js.min.js"
    bundle.write_bytes(b"x" * 1024)
    monkeypatch.setattr(server, "_verified_stat", None)
    monkeypatch.setattr(server, "_digest_ok", lambda data: data == b"x" * 1024)

    assert server._cached_digest_ok(bundle) is True
    bundle.write_bytes(b"malicious payload")
    assert server._cached_digest_ok(bundle) is False, \
        "a swapped bundle was served from the cached verdict"


# ── #25 · a destructive action registers its reversal (R-05) ─────────────────

def test_organizing_the_desktop_can_be_undone(tmp_path, monkeypatch):
    """Audit #25. core/tool_policy.py lets desktop_control run ungated *because*
    it is reversible — a claim about a module that never called push_undo, so
    `undo` could not reach a single one of these moves.
    actions/file_controller.py::organize_desktop has journalled them all along:
    same work, same desktop, one of the two keeping the promise.
    """
    import actions.desktop as ad
    from core import undo

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "photo.png").write_text("i", encoding="utf-8")
    (desktop / "notes.txt").write_text("n", encoding="utf-8")
    monkeypatch.setattr(ad, "_get_desktop", lambda: desktop)

    ad.organize_desktop()
    assert not (desktop / "photo.png").exists(), "nothing was organised"
    assert undo.history(), "the reorganisation registered no undo"

    undo.undo_last()
    assert (desktop / "photo.png").exists(), "undo did not put the file back"
    assert (desktop / "notes.txt").exists()


def test_archiving_the_desktop_can_be_undone(tmp_path, monkeypatch):
    import actions.desktop as ad
    from core import undo

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "old.txt").write_text("o", encoding="utf-8")
    monkeypatch.setattr(ad, "_get_desktop", lambda: desktop)

    ad.clean_desktop()
    assert not (desktop / "old.txt").exists()
    assert undo.history()

    undo.undo_last()
    assert (desktop / "old.txt").exists(), "undo did not put the file back"
