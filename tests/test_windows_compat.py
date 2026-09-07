"""
Phase 07 — Alexio still runs on Windows.

Alexio was a Windows program before it was anything else, and phases 00-05
added roughly 3 500 lines written and tested on a Fedora laptop. None of it was
run on Windows. This file is the substitute for that: a set of properties that,
if they hold, mean the new code cannot have broken the Windows path — checked
by reading the source, so they hold on every machine including CI.

It is not a claim that Alexio *works* on Windows. It is a claim that nothing
here makes it impossible, and that the specific mistakes a Linux-only session
makes have not been made.

WHAT ACTUALLY BREAKS PORTABILITY, IN ORDER OF HOW OFTEN
  1. `open(path)` with no `encoding=`. On Windows this uses the locale code
     page — cp1252 on a French install — so reading a UTF-8 file with an accent
     in it raises UnicodeDecodeError. Silent on Linux, fatal there.
  2. A POSIX-only module imported at the top of a file (`fcntl`, `pwd`, `termios`).
     Not a bug on Linux, an ImportError on Windows.
  3. `subprocess.CREATE_NO_WINDOW` referenced outside a platform guard. The
     constant does not exist on Linux; the mirror mistake is a Linux-only path
     reached on Windows.
  4. Paths built by string concatenation with "/".
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Everything phases 00-05 added, plus what they rewired.
NEW_MODULES = [
    "core.ai", "core.ai.registry", "core.ai.router", "core.ai.tools",
    "core.ai.gemini", "core.ai.local", "core.ai.anthropic", "core.ai.openai",
    "core.voice", "core.voice.gemini_live",
    "core.desktop", "core.desktop.linux", "core.desktop.windows",
    "core.desktop.macos", "core.desktop.portal",
    "core.local", "core.local.reflex", "core.local.intents", "core.local.speech",
    "core.telemetry",
]

POSIX_ONLY_MODULES = {"fcntl", "termios", "pwd", "grp", "tty", "pty",
                      "resource", "syslog", "crypt", "posix"}

WINDOWS_ONLY_MODULES = {"winreg", "msvcrt", "winsound", "pycaw", "comtypes",
                        "win10toast", "win32com", "win32api", "pywinauto", "wmi"}


def _project_files():
    for path in sorted(ROOT.rglob("*.py")):
        if any(p in path.parts for p in (".venv", "__pycache__", "logs", "build")):
            continue
        yield path


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# ── 1. imports ───────────────────────────────────────────────────────────────

def test_no_posix_only_module_is_imported_at_load_time():
    """These do not exist on Windows. Inside a function is fine; at the top of a
    file it makes the module unimportable there."""
    offenders = []
    for path in _project_files():
        for node in _tree(path).body:
            names: set[str] = set()
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            if names & POSIX_ONLY_MODULES:
                offenders.append(f"{_rel(path)}:{node.lineno} "
                                 f"{sorted(names & POSIX_ONLY_MODULES)}")
    assert not offenders, f"POSIX-only imports at module level: {offenders}"


def test_no_windows_only_module_is_imported_at_load_time():
    """The mirror rule, which is what keeps this very test suite runnable."""
    offenders = []
    for path in _project_files():
        for node in _tree(path).body:
            names: set[str] = set()
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            if names & WINDOWS_ONLY_MODULES:
                offenders.append(f"{_rel(path)}:{node.lineno} "
                                 f"{sorted(names & WINDOWS_ONLY_MODULES)}")
    assert not offenders, f"Windows-only imports at module level: {offenders}"


@pytest.fixture
def pristine_module_cache():
    """Reimporting core.* under a fake platform replaces the module objects
    other tests already hold, so `desktop.backend_for("Windows") is windows`
    starts failing for reasons that have nothing to do with the code. Snapshot
    and restore."""
    saved = {k: v for k, v in sys.modules.items() if k.startswith("core")}
    try:
        yield
    finally:
        for k in [k for k in sys.modules if k.startswith("core")]:
            del sys.modules[k]
        sys.modules.update(saved)


@pytest.mark.parametrize("module", NEW_MODULES)
@pytest.mark.parametrize("platform_name", ["win32", "darwin", "linux"])
def test_every_new_module_imports_on_every_platform(module, platform_name,
                                                    pristine_module_cache):
    """The strongest single guarantee available without a Windows machine.

    Faking sys.platform is enough because none of these import a platform
    package at load time — which is exactly what the two tests above enforce.
    """
    with mock.patch.object(sys, "platform", platform_name):
        for name in [k for k in sys.modules if k.startswith("core.")]:
            del sys.modules[name]
        importlib.import_module(module)


# ── 2. text files ────────────────────────────────────────────────────────────

def test_every_text_open_declares_its_encoding():
    """cp1252 is the default on a French Windows. `json.load(open(p))` on a
    config file containing "é" raises UnicodeDecodeError there and nowhere else.
    """
    offenders = []
    for path in _project_files():
        for node in ast.walk(_tree(path)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "open"):
                continue
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if "b" in mode:
                continue
            if not any(kw.arg == "encoding" for kw in node.keywords):
                offenders.append(f"{_rel(path)}:{node.lineno}")
    assert not offenders, (
        f"open() without encoding= — reads UTF-8 as cp1252 on Windows: {offenders}"
    )


def test_pathlib_text_helpers_declare_their_encoding():
    """Path.read_text() and write_text() have the same default as open()."""
    offenders = []
    for path in _project_files():
        for node in ast.walk(_tree(path)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in ("read_text", "write_text"):
                continue
            if not any(kw.arg == "encoding" for kw in node.keywords):
                offenders.append(f"{_rel(path)}:{node.lineno} .{node.func.attr}()")
    assert not offenders, f"read_text/write_text without encoding=: {offenders}"


# ── 3. platform-specific constants ───────────────────────────────────────────

def _module_has_platform_guard(tree: ast.Module) -> bool:
    """True if the file tests the platform anywhere. Deliberately coarse: the
    point is to catch a file that reaches for a Windows constant while having no
    idea what it is running on."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("system", "platform"):
            return True
        if isinstance(node, ast.Name) and node.id in ("_OS", "_WIN_HIDE", "_NO_WINDOW"):
            return True
    return False


def test_windows_only_subprocess_flags_never_appear_in_unguarded_files():
    """CREATE_NO_WINDOW and DETACHED_PROCESS do not exist on Linux or macOS.

    Reaching for one in a file that never checks the platform is an
    AttributeError waiting for the first non-Windows user.
    """
    flags = {"CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_CONSOLE",
             "STARTF_USESHOWWINDOW"}
    offenders = []
    for path in _project_files():
        tree = _tree(path)
        uses = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr in flags]
        if uses and not _module_has_platform_guard(tree):
            offenders.append(f"{_rel(path)}: lines {uses}")
    assert not offenders, f"Windows subprocess flags in unguarded files: {offenders}"


# ── 4. paths ─────────────────────────────────────────────────────────────────

def test_absolute_posix_paths_only_appear_in_platform_aware_files():
    """"/sys/class/backlight" is correct in core/desktop/linux.py and a bug
    anywhere that does not know which OS it is on."""
    prefixes = ("/tmp/", "/usr/", "/etc/", "/var/", "/home/", "/proc/", "/sys/",
                "/Applications/", "/Library/")
    offenders = []
    for path in _project_files():
        tree = _tree(path)
        if _module_has_platform_guard(tree) or path.name in ("linux.py", "macos.py"):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value.startswith(prefixes) and len(node.value) > 6):
                offenders.append(f"{_rel(path)}:{node.lineno} {node.value[:40]}")
    assert not offenders, f"absolute POSIX paths in platform-blind files: {offenders}"


def test_paths_are_not_built_by_string_concatenation():
    """"dir" + "/" + name gives a broken path on Windows. pathlib does not."""
    import re
    pattern = re.compile(r'"[^"]*/"\s*\+|\+\s*"/[A-Za-z_]')
    offenders = []
    for path in _project_files():
        # This file defines the pattern it searches for, so it matches itself.
        if path.name == "test_windows_compat.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "http" in line or "://" in line:
                continue
            if pattern.search(line):
                offenders.append(f"{_rel(path)}:{i}")
    assert not offenders, f"paths built with string concatenation: {offenders}"


# ── 5. the new subsystems behave on Windows ──────────────────────────────────

def test_the_desktop_facade_selects_the_windows_backend():
    """Compared by NAME, not by object identity.

    The platform-simulation tests above reimport core.*, so two equally valid
    module objects for core.desktop.windows can be alive at once. Identity would
    then fail for a reason that has nothing to do with dispatch.
    """
    from core import desktop

    assert desktop.backend_for("Windows").NAME == "windows"
    assert desktop.backend_for("win32").NAME == "windows"
    assert "desktop backend: windows" in desktop.report("Windows")


def test_the_windows_backend_uses_a_literal_for_the_no_window_flag():
    """subprocess.CREATE_NO_WINDOW cannot even be *named* on Linux, so
    core/desktop/windows.py spells the value out — which is what lets this file
    be imported and inspected here."""
    from core.desktop import windows

    source = (ROOT / "core" / "desktop" / "windows.py").read_text(encoding="utf-8")
    assert "CREATE_NO_WINDOW" not in source
    assert windows._NO_WINDOW == ({} if sys.platform != "win32"
                                  else {"creationflags": 0x08000000})


def test_telemetry_writes_and_rotates_without_posix_assumptions(temp_telemetry):
    """Log rotation renames a file. On Windows a rename onto an existing name
    fails, and renaming an open file fails — so the old generation is unlinked
    first and nothing is held open across the rename."""
    from core import telemetry

    source = (ROOT / "core" / "telemetry.py").read_text(encoding="utf-8")
    assert "prev.unlink()" in source, "rotation must clear the target first"

    for _ in range(3):
        with telemetry.record(plane="B", provider="p", model="m", task="t"):
            pass
    assert len(telemetry.read_rows()) == 3


def test_the_reflex_layer_is_pure_python():
    """No subprocess, no platform branch: it must behave identically everywhere,
    because it is the one component that acts without the model.

    Checked against the imports rather than the text — the module's docstring
    legitimately contains the word "subprocess" while explaining what it saves.
    """
    tree = _tree(ROOT / "core" / "local" / "reflex.py")

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not (imported & {"subprocess", "platform", "os", "sys", "ctypes"}), (
        f"reflex.py reaches for the system: {sorted(imported)}"
    )


def test_the_ai_gateway_has_no_platform_branch():
    """Plane B is HTTP and JSON. A platform check in there would mean something
    is wrong."""
    for name in ("__init__", "registry", "router", "tools", "types",
                 "errors", "anthropic", "openai", "gemini"):
        source = (ROOT / "core" / "ai" / f"{name}.py").read_text(encoding="utf-8")
        assert "platform.system()" not in source, f"core/ai/{name}.py branches on OS"


def test_local_ai_provider_guards_its_windows_flag():
    """core/ai/local.py starts `ollama serve`, which is the one place in the
    gateway that touches a process."""
    source = (ROOT / "core" / "ai" / "local.py").read_text(encoding="utf-8")
    assert 'sys.platform == "win32"' in source
    idx_guard = source.index('sys.platform == "win32"')
    idx_flag  = source.index("CREATE_NO_WINDOW")
    assert idx_guard < idx_flag, "the flag is used before the platform is checked"


def test_the_portal_client_refuses_politely_off_linux():
    """xdg-desktop-portal is a Linux mechanism. On Windows the calls must raise
    a clear PortalError, and `available()` must simply say no."""
    from core.desktop import portal

    with mock.patch.object(sys, "platform", "win32"):
        assert portal.available() is False
        assert portal.interface_version("org.freedesktop.portal.Screenshot") is None
        with pytest.raises(portal.PortalError, match="Linux"):
            portal.call("Screenshot", "Screenshot", "sa{sv}", ("", {}))
        with pytest.raises(portal.PortalError, match="Linux"):
            portal.trash("C:/tmp/x")


def test_jeepney_is_declared_linux_only():
    """It is a D-Bus library; installing it on Windows is noise at best."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    line = next((l for l in text.splitlines() if l.strip().startswith("jeepney")), "")
    assert line, "jeepney must be declared in requirements.txt"
    assert 'sys_platform == "linux"' in line, (
        f"jeepney must carry a Linux marker, got: {line!r}"
    )
