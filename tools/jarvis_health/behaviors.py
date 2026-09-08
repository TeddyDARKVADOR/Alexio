"""
tools/jarvis_health/behaviors.py — the guarantees Alexio claims to offer, one
line each, with the test that proves it.

WHY THIS FILE HAS TO BE WRITTEN BY HAND
    Line coverage is derived. Branch coverage is derived. Behaviour coverage
    cannot be: no tool can look at `core/ai/__init__.py` and work out that the
    interesting question is "does Budget.private() still refuse when the local
    model is missing". That is a claim about intent, and intent has to be
    declared.

    The proof that this distinction is real, and not bookkeeping:

      · core/ai/router.py is at 97% line coverage and 93% branch coverage, and
        `Budget.private` measures 100%. The private-budget guarantee is broken
        anyway — the raise happens in router.py and is swallowed one module up.
      · tests/test_dashboard_security.py::test_revoking_devices_actually_revokes_them
        exists, passes, and asserts that the device dictionary is emptied. It
        does not assert that the sessions those devices opened stop working,
        which is what "revoke" means to the person pressing the button.

    Both files would show green on any coverage report ever written. So the
    dashboard tracks three different things and never lets one stand in for
    another: code coverage (was the line run), behaviour coverage (is the
    guarantee checked), rule coverage (is the invariant mechanically enforced).

HOW A ROW STAYS HONEST
    Status is derived, never typed in:

      VIOLATED  a jarvis_lint rule reports this module right now
      UNTESTED  no test is named, or a named test does not exist
      TESTED    every named test exists and the suite is green

    `audit` records where the claim came from. `open_since` marks a guarantee
    the 2026-09-08 audit found broken and which nobody has fixed yet — and
    tools/jarvis_health/report.py *reconciles* it: a row marked open that is now
    both tested and unviolated is an error, exactly like a stale lint baseline.
    A ledger that can drift is a ledger nobody believes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CRITICAL = "critical"
HIGH     = "high"
MEDIUM   = "medium"
LOW      = "low"

# What each axis is worth. Security and architecture outweigh raw coverage on
# purpose — see tools/jarvis_health/score.py for the cap that stops 99% line
# coverage from burying a critical violation.
WEIGHTS = {
    "lines":        0.10,
    "branches":     0.10,
    "functions":    0.10,
    "behaviour":    0.25,
    "security":     0.25,
    "architecture": 0.20,
}


@dataclass(frozen=True)
class Behaviour:
    """One guarantee, and the evidence for it."""

    id:        str
    module:    str                       # repo-relative path this is about
    what:      str                       # the guarantee, in one sentence
    severity:  str
    tests:     tuple[str, ...] = ()      # pytest node ids that prove it
    rule:      str | None = None         # the jarvis_lint rule that also guards it
    audit:     str | None = None         # audit finding number, if it came from one
    open_since: str | None = None        # date the audit found it broken
    note:      str = ""                  # why the obvious test is not the real test

    @property
    def area(self) -> str:
        return self.module.split("/")[0]


# ── the manifest ─────────────────────────────────────────────────────────────
#
# Ordered by area. Every row that carries an `audit` reference is a defect the
# 2026-09-08 audit found; every row without one is a guarantee that predates it.

BEHAVIOURS: list[Behaviour] = [

    # ── core/ai — the model gateway ──────────────────────────────────────
    Behaviour(
        id="BEH-AI-01", module="core/ai/__init__.py", severity=CRITICAL,
        what="Budget.private() refuses rather than falling back to a remote provider",
        rule="JAR004", audit="#1",
        tests=("tests/test_security_invariants.py::test_a_private_budget_refuses_rather_than_going_to_a_vendor",
               "tests/test_security_invariants.py::test_an_unconstrained_request_still_falls_back"),
        note="_resolve() catches NoModelFits and the loop below then appends every "
             "reachable provider, so the guarantee inverts in exactly the case it "
             "exists for: no local model available.",
    ),
    Behaviour(
        id="BEH-AI-02", module="core/ai/__init__.py", severity=CRITICAL,
        what="an impossible max_latency_ms raises instead of degrading (R-12)",
        rule="JAR004", audit="#1",
        tests=("tests/test_security_invariants.py::test_an_impossible_latency_budget_raises",),
    ),
    Behaviour(
        id="BEH-AI-03", module="core/ai/__init__.py", severity=CRITICAL,
        what="an impossible max_cost_usd raises instead of degrading (R-12)",
        rule="JAR004", audit="#1",
        tests=("tests/test_security_invariants.py::test_an_impossible_cost_budget_raises",),
    ),
    Behaviour(
        id="BEH-AI-04", module="core/ai/__init__.py", severity=MEDIUM,
        what="a caller's Budget survives _resolve() with all its fields intact",
        audit="#12",
        tests=("tests/test_security_invariants.py::test_the_cost_shape_a_caller_described_survives_resolution",),
        note="est_tokens_in/out/cached_in are dropped when the Budget is rebuilt, "
             "so cost filtering silently uses the defaults.",
    ),
    Behaviour(
        id="BEH-AI-05", module="core/ai/router.py", severity=HIGH,
        what="latency filtering compares the same statistic before and after measurement",
        tests=("tests/test_ai_routing.py::test_measured_latency_wins_once_there_are_enough_samples",
               "tests/test_ai_routing.py::test_the_tail_is_still_available_to_anyone_who_asks_for_it",),
        audit="#6",
        note="declared typical_latency_ms is a median; measured latency_ms is a p95. "
             "The fifth call flips Budget.conversation() from working to NoModelFits "
             "on a model whose median is well inside the budget.",
    ),
    Behaviour(
        id="BEH-AI-06", module="core/ai/router.py", severity=MEDIUM,
        what="NoModelFits names the real cause when no provider has credentials",
        tests=("tests/test_security_invariants.py::test_a_machine_with_no_api_key_is_told_so",),
        audit="#9",
        note="providers absent from `available` are skipped with no reason recorded, "
             "so a fresh install is told 'the catalogue is empty'.",
    ),
    Behaviour(
        id="BEH-AI-07", module="core/ai/router.py", severity=MEDIUM,
        what="a provider observed to fail is dropped while a healthy one exists",
        tests=("tests/test_ai_routing.py",),
    ),
    Behaviour(
        id="BEH-AI-08", module="core/ai/tools.py", severity=MEDIUM,
        what="a rendered dialect cannot mutate the ToolSpec it came from",
        tests=("tests/test_security_invariants.py::test_rendering_a_tool_twice_gives_the_same_document",),
        rule="JAR009", audit="#20",
    ),
    Behaviour(
        id="BEH-AI-09", module="core/ai/tools.py", severity=MEDIUM,
        what="schema type conversion recurses into arrays of objects",
        tests=("tests/test_ai_routing.py",),
    ),
    Behaviour(
        id="BEH-AI-10", module="core/telemetry.py", severity=MEDIUM,
        what="a routing decision costs no more than a few milliseconds",
        tests=("tests/test_security_invariants.py::test_the_router_reads_a_window_not_the_whole_history",),
        rule="JAR010", audit="#8",
        note="measured 491 ms at the 16 MB rotation threshold, ~1 s once the rotated "
             "generation is full too. Budget.conversation() allows 900 ms in total.",
    ),
    Behaviour(
        id="BEH-AI-11", module="core/telemetry.py", severity=LOW,
        what="every model call writes one JSONL row, failures included (R-11)",
        tests=("tests/test_telemetry.py",),
    ),

    # ── the safety gates ─────────────────────────────────────────────────
    Behaviour(
        id="BEH-GATE-01", module="main.py", severity=CRITICAL,
        what="no tool executes without a verdict from core/tool_policy (R-18)",
        rule="JAR003", audit="#3",
        tests=("tests/test_security_invariants.py::test_a_plugin_classified_confirm_does_not_run_on_the_models_word",
               "tests/test_security_invariants.py::test_the_dispatch_gates_the_plugin_branch"),
        note="the plugin branch of _execute_tool calls nothing. tool_policy.register() "
             "exists only for plugins and is read by nobody.",
    ),
    Behaviour(
        id="BEH-GATE-02", module="core/confirm.py", severity=CRITICAL,
        what="an irreversible action runs only after a human presses CONFIRM (R-04)",
        tests=("tests/test_safety.py::test_the_action_runs_only_after_the_user_presses_confirm",
               "tests/test_safety.py::test_cancelling_runs_nothing",
               "tests/test_safety.py::test_without_an_interface_nothing_irreversible_runs"),
    ),
    Behaviour(
        id="BEH-GATE-03", module="core/confirm.py", severity=MEDIUM,
        what="an expired confirmation takes its own banner off the screen",
        tests=("tests/test_security_invariants.py::test_an_abandoned_confirmation_removes_its_banner",),
        audit="#11",
        note="the TTL is only consulted inside resolve(). Nothing calls _hide_cb on "
             "expiry, so the button stays up and does nothing when pressed.",
    ),
    Behaviour(
        id="BEH-GATE-04", module="core/confirm.py", severity=HIGH,
        what="a second request cannot swap the action behind a banner already shown",
        tests=("tests/test_tool_policy.py",),
        note="both call sites check pending_title() first; request() itself does not "
             "enforce it, so the guarantee rests on caller discipline.",
    ),
    Behaviour(
        id="BEH-GATE-05", module="core/tool_policy.py", severity=HIGH,
        what="every shipped tool has an explicit rule, never the default",
        tests=("tests/test_tool_policy.py",),
    ),
    Behaviour(
        id="BEH-GATE-06", module="actions/code_helper.py", severity=HIGH,
        what="'auto' re-enters the gate once it has resolved to run or build",
        tests=("tests/test_tool_policy.py",),
    ),

    # ── the filesystem ───────────────────────────────────────────────────
    Behaviour(
        id="BEH-FS-01", module="actions/dev_agent.py", severity=CRITICAL,
        what="a model-authored path cannot escape the project directory",
        rule="JAR001", audit="#2",
        tests=("tests/test_security_invariants.py::test_a_hostile_path_cannot_leave_the_project",
               "tests/test_security_invariants.py::test_a_symlink_out_of_the_project_is_refused",
               "tests/test_security_invariants.py::test_dev_agent_writes_through_the_confinement_helper"),
        note="`project_dir / file_path` with no containment check. pathlib discards "
             "the base entirely when the tail is absolute.",
    ),
    Behaviour(
        id="BEH-FS-02", module="actions/file_controller.py", severity=CRITICAL,
        what="a file operation cannot climb out of the allowed roots",
        tests=("tests/test_actions.py",),
    ),
    Behaviour(
        id="BEH-FS-03", module="actions/file_controller.py", severity=HIGH,
        what="a delete goes to the trash and can be undone (R-05)",
        tests=("tests/test_actions.py",),
    ),
    Behaviour(
        id="BEH-FS-04", module="dashboard/server.py", severity=HIGH,
        what="an upload or download cannot leave the uploads folder",
        tests=("tests/test_dashboard_security.py::test_a_download_cannot_climb_out_of_the_uploads_folder",),
    ),

    # ── the dashboard, the only thing on a socket ────────────────────────
    Behaviour(
        id="BEH-NET-01", module="dashboard/auth.py", severity=HIGH,
        what="revoking devices also stops the sessions those devices opened",
        tests=("tests/test_security_invariants.py::test_revoking_devices_also_kills_their_live_sessions",
               "tests/test_security_invariants.py::test_revoking_devices_leaves_the_desktop_session_alone",),
        audit="#7",
        note="test_revoking_devices_actually_revokes_them exists and passes — it "
             "asserts the device dict is emptied, not that a live bearer token stops "
             "working. The bearer survives up to 7 days. This is the clearest case in "
             "the repo of a green test that does not check the guarantee.",
    ),
    Behaviour(
        id="BEH-NET-02", module="dashboard/auth.py", severity=HIGH,
        what="a failure-tracking map cannot grow without bound from the network",
        tests=("tests/test_security_invariants.py::test_merely_asking_leaves_nothing_behind",
               "tests/test_security_invariants.py::test_the_failure_map_is_capped",
               "tests/test_security_invariants.py::test_throttling_still_works_after_the_cap",),
        rule="JAR008", audit="#13",
    ),
    Behaviour(
        id="BEH-NET-03", module="dashboard/server.py", severity=HIGH,
        what="a GET cannot spend a pairing credential",
        tests=("tests/test_security_invariants.py::test_looking_at_the_qr_link_does_not_burn_the_key",
               "tests/test_security_invariants.py::test_the_pairing_key_is_still_single_use_on_post",),
        rule="JAR007", audit="#19",
    ),
    Behaviour(
        id="BEH-NET-04", module="dashboard/server.py", severity=CRITICAL,
        what="a cookie drives navigation and never the API (R-16)",
        tests=("tests/test_dashboard_security.py::test_the_navigation_cookie_cannot_drive_the_api",
               "tests/test_dashboard_security.py::test_the_navigation_cookie_is_httponly_and_samesite_strict"),
    ),
    Behaviour(
        id="BEH-NET-05", module="dashboard/server.py", severity=CRITICAL,
        what="the application page is gated on the server, not in JavaScript",
        tests=("tests/test_dashboard_security.py::test_the_app_page_is_not_served_to_an_anonymous_visitor",),
    ),
    Behaviour(
        id="BEH-NET-06", module="dashboard/auth.py", severity=HIGH,
        what="a pairing PIN works once and dies with its QR code",
        tests=("tests/test_dashboard_security.py::test_the_pairing_key_works_once_and_then_never_again",
               "tests/test_dashboard_security.py::test_the_pairing_key_dies_with_the_qr_code"),
    ),
    Behaviour(
        id="BEH-NET-07", module="dashboard/auth.py", severity=HIGH,
        what="a burst of wrong guesses burns every pending PIN",
        tests=("tests/test_dashboard_security.py::test_a_burst_of_wrong_guesses_takes_the_pin_away",),
    ),
    Behaviour(
        id="BEH-NET-08", module="dashboard/server.py", severity=HIGH,
        what="nothing asks for elevation at startup (R-17)",
        tests=("tests/test_dashboard_security.py::test_the_dashboard_never_asks_for_elevation",),
    ),
    Behaviour(
        id="BEH-NET-09", module="dashboard/server.py", severity=MEDIUM,
        what="the pinned CryptoJS bundle is verified before it is served",
        tests=("tests/test_dashboard_security.py::test_the_crypto_bundle_is_pinned_and_verified",),
    ),
    Behaviour(
        id="BEH-NET-10", module="dashboard/server.py", severity=LOW,
        what="a static asset is not re-hashed on every request",
        tests=("tests/test_security_invariants.py::test_the_crypto_bundle_is_not_rehashed_on_every_request",
               "tests/test_security_invariants.py::test_a_replaced_bundle_is_hashed_again",),
        audit="#18",
        note="held by the (mtime, size) cache in _cached_digest_ok.",
    ),

    # ── the conversation ─────────────────────────────────────────────────
    Behaviour(
        id="BEH-VOICE-01", module="main.py", severity=HIGH,
        what="a failed capture cannot disable vision for the rest of the session",
        rule="JAR005", audit="#5", open_since="2026-09-08",
    ),
    Behaviour(
        id="BEH-VOICE-02", module="core/voice/gemini_live.py", severity=MEDIUM,
        what="a rejected audio feature does not cost the conversation its resume handle",
        audit="#10", open_since="2026-09-08",
        note="both predicates match INVALID_ARGUMENT and the handle is tested first, "
             "so an enhanced-audio rejection is diagnosed as a dead handle and the "
             "conversation is thrown away to fix something else.",
    ),
    Behaviour(
        id="BEH-VOICE-03", module="core/voice/gemini_live.py", severity=MEDIUM,
        what="a go-away warning carries the number of seconds left",
        tests=("tests/test_voice_disconnect.py",),
    ),
    Behaviour(
        id="BEH-VOICE-04", module="core/voice/gemini_live.py", severity=MEDIUM,
        what="a disconnection says who hung up and why",
        tests=("tests/test_voice_disconnect.py",),
    ),

    # ── executing things ─────────────────────────────────────────────────
    Behaviour(
        id="BEH-EXEC-01", module="actions/open_app.py", severity=CRITICAL,
        what="a tool parameter never reaches a shell command line",
        rule="JAR012", audit="#22",
        tests=("tests/test_security_invariants.py::test_open_app_never_builds_a_shell_command_line",
               "tests/test_security_invariants.py::test_a_hostile_app_name_launches_nothing",
               "tests/test_security_invariants.py::test_a_real_executable_is_launched_by_resolved_path"),
        note="_launch_windows validates a PREFIX and then executes the whole "
             "string: shutil.which(app_name.split('.')[0]) answers yes for "
             "\"notepad.exe & calc\", and Popen(app_name, shell=True) runs both. "
             "Line 97 has the same shape behind an `if ':' in app_name` test. "
             "app_name is a model-supplied tool parameter and open_app is "
             "classified RUN. Windows-only, which is why nothing has hit it.",
    ),
    Behaviour(
        id="BEH-EXEC-02", module="actions/dev_agent.py", severity=HIGH,
        what="a command line is never assembled by splitting a model's string",
        rule="JAR013", audit="known debt",
        note="Held by JAR013 since the fix: _run_project now uses shlex.split, which is a parser, and refuses an empty command instead of raising IndexError. _run_project does plan['run_command'].split() and hands the result "
             "to subprocess. Confirmed and run in a per-project venv now, but the "
             "content is still chosen by the model.",
    ),
    Behaviour(
        id="BEH-EXEC-03", module="core/tool_policy.py", severity=HIGH,
        what="the desktop_control sandbox actually blocks what it claims to block",
        tests=("tests/test_security_invariants.py::test_the_desktop_sandbox_refuses_what_it_says_it_refuses",
               "tests/test_security_invariants.py::test_the_sandbox_still_allows_what_the_feature_needs",
               "tests/test_security_invariants.py::test_tool_policy_still_claims_a_sandbox",),
        rule="JAR020", audit="#24",
        note="core/tool_policy.py justifies RUN for desktop_control with 'no "
             "deletion, no rmtree and no subprocess in the sandbox'. That is an "
             "assertion about _build_sandbox with no test behind it, guarding an "
             "exec() of model-authored code.",
    ),
    Behaviour(
        id="BEH-FS-05", module="actions/desktop.py", severity=MEDIUM,
        what="a temporary file is created atomically, not merely named",
        rule="JAR014", audit="#23",
        note="held by JAR014: all five sites now use mkstemp(). tempfile.mktemp() at five sites across core/desktop and actions. "
             "It returns a name and leaves the create to you, so anything can win "
             "the race in between. Python's own docs say use mkstemp().",
    ),

    Behaviour(
        id="BEH-FS-06", module="actions/reminder.py", severity=HIGH,
        what="an action that deletes registers the reversal (R-05)",
        rule="JAR016", audit="#25", open_since="2026-09-08",
        note="core/tool_policy.py classifies reminder, file_processor, "
             "code_helper and desktop_control as RUN because they are reversible, "
             "and only actions/file_controller.py calls push_undo. Seven "
             "destructive calls across four modules have nothing behind them, so "
             "`undo` cannot reach what they did.",
    ),
    Behaviour(
        id="BEH-FS-07", module="actions/desktop.py", severity=HIGH,
        what="moving a user's files registers the reversal (R-05)",
        tests=("tests/test_security_invariants.py::test_organizing_the_desktop_can_be_undone",
               "tests/test_security_invariants.py::test_archiving_the_desktop_can_be_undone",),
        rule="JAR016", audit="#25",
        note="organize_desktop and clean_desktop shutil.move the user's files. "
             "actions/file_controller.py::organize_desktop does the same work and "
             "journals every move for undo; this one does not.",
    ),

    # ── portability and startup ──────────────────────────────────────────
    Behaviour(
        id="BEH-BOOT-01", module="actions/screen_processor.py", severity=HIGH,
        what="camera detection does not raise NameError on a machine with a camera",
        tests=("tests/test_security_invariants.py::test_camera_probing_does_not_reach_for_an_import_that_is_not_there",),
        audit="#4",
        note="_probe_camera calls np.mean and numpy is never imported in this file. "
             "pyflakes reports it; nothing in the suite runs the line.",
    ),
    Behaviour(
        id="BEH-BOOT-02", module="main.py", severity=HIGH,
        what="the application imports with every optional package absent",
        tests=("tests/test_optional_dependencies.py",),
    ),
    Behaviour(
        id="BEH-BOOT-03", module="dashboard/server.py", severity=MEDIUM,
        what="a broken native package does not stop the application from starting",
        rule="JAR011", audit="#15",
        note="Held by JAR011 since the fix: all eleven narrow guards are now `except Exception`, so a broken native package degrades a capability instead of stopping the application.",
    ),
    Behaviour(
        id="BEH-BOOT-04", module="core/plugin_loader.py", severity=MEDIUM,
        what="a plugin receives every argument R-03 promises it",
        audit="#17", open_since="2026-09-08",
        note="_call_run knows about player and session_memory only; response and "
             "speak are silently left at their defaults.",
    ),
    Behaviour(
        id="BEH-BOOT-05", module="actions/dev_agent.py", severity=HIGH,
        what="model identifiers do not appear outside core/ai (R-02)",
        tests=("tests/test_security_invariants.py::test_no_action_module_pins_a_model",),
        rule="JAR002", audit="new",
        note="found by building the rule, not by the audit: tests/test_tool_wiring.py "
             "enforces the SDK-import half of R-02 and never the identifier half.",
    ),
    Behaviour(
        id="BEH-BOOT-06", module="actions/code_helper.py", severity=HIGH,
        what="model identifiers do not appear outside core/ai (R-02)",
        tests=("tests/test_security_invariants.py::test_no_action_module_pins_a_model",),
        rule="JAR002", audit="new",
    ),
    Behaviour(
        id="BEH-BOOT-07", module="dashboard/server.py", severity=MEDIUM,
        what="a failure in background startup work is reported, not dropped",
        rule="JAR006", audit="#16",
        note="held by JAR006: the Futures are kept and a done-callback reports.",
    ),
]


def by_module() -> dict[str, list[Behaviour]]:
    out: dict[str, list[Behaviour]] = {}
    for b in BEHAVIOURS:
        out.setdefault(b.module, []).append(b)
    return out
