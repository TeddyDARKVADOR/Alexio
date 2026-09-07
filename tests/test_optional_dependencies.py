"""
Alexio starts on a machine where the optional packages are not installed.

THE BUG THIS EXISTS FOR
  `main.py` imports `actions/browser_control.py`, which did `from
  playwright.async_api import …` with no guard. On a fresh install — no
  playwright, or playwright present but its browsers not downloaded — the
  ImportError propagated all the way out of `import main` and the application
  did not start. Not a degraded browser: no Alexio at all.

  Phase 00 fixed five of these, all of them `pyautogui`. It missed this one
  because the audit looked for guards that were *too narrow* (`except
  ImportError` where the package raises something else), not for imports with
  no guard at all. Different shape, same failure.

WHY A STRUCTURAL TEST AND A BEHAVIOURAL ONE
  The AST test states the rule and catches the next unguarded import the moment
  it is written, in any file, for any package. The behavioural test proves the
  rule is worth something: it actually removes each package and imports the
  whole application, which is the only way to catch a guard that exists but sets
  a flag nobody reads, or a module-level `pyautogui.FAILSAFE = False` sitting
  outside the try.

  Neither replaces the other. The first is the specification; the second is the
  measurement (R-06: a capability is measured, not declared).
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── What is optional, and what each absence costs ────────────────────────────
#
# Every one of these buys a capability. None of them buys the right to start.
# The comment is the message the user should see when it is missing — a "no"
# has to say what would fix it.
OPTIONAL_DEPENDENCIES: dict[str, str] = {
    "playwright":             "interactive browser control (click / type / read a page)",
    "pyautogui":              "keyboard and mouse injection",
    "pygetwindow":            "window enumeration and focus",
    "mss":                    "screen capture on X11 and Windows",
    "pycaw":                  "volume control on Windows",
    "comtypes":               "the COM bridge pycaw sits on",
    "send2trash":             "trash on Windows and macOS",
    "cv2":                    "camera capture and image processing",
    "PIL":                    "image encoding",
    "pyperclip":              "clipboard",
    "jeepney":                "D-Bus, hence every Wayland portal",
    "win10toast":             "notifications on Windows",
    "bs4":                    "HTML parsing for web search",
    "ddgs":                   "the web search backend",
    "qrcode":                 "the dashboard pairing QR code",
    "fastapi":                "the remote dashboard",
    "uvicorn":                "the dashboard's HTTP server",
    "docx":                   ".docx reading",
    "pypdf":                  ".pdf text extraction",
    "pdfplumber":             ".pdf extraction with layout",
    "pandas":                 ".csv / .xlsx analysis",
    "openpyxl":               ".xlsx reading",
    "pydub":                  "audio slicing and conversion",
    "pptx":                   ".pptx reading",
    "youtube_transcript_api": "YouTube transcript fetching",
    "wmi":                    "brightness on Windows",
    "win32com":               "the Windows shell bridge",
    "pywinauto":              "Windows UI automation",
}

# Not optional: without one of these Alexio has no interface, no audio, no HTTP.
# Listed so the two sets are visibly exhaustive rather than accidentally so.
REQUIRED_DEPENDENCIES = {"PyQt6", "numpy", "sounddevice", "requests", "psutil"}


# ── The modules that must survive each absence ───────────────────────────────

def _module_names() -> list[str]:
    """Every module `main.py` reaches at import time, discovered rather than
    listed — a new file under actions/ is covered the day it is written."""
    names = ["main"]
    for path in sorted(ROOT.glob("actions/*.py")):
        if path.name != "__init__.py":
            names.append(f"actions.{path.stem}")
    for path in sorted(ROOT.rglob("core/**/*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT)
        parts = rel.with_suffix("").parts
        names.append(".".join(parts[:-1] if path.name == "__init__.py" else parts))
    return sorted(set(names))


MODULES = _module_names()


class _Blocked:
    """A meta-path finder that makes one package look uninstalled.

    Raising from `find_spec` rather than returning None is deliberate: returning
    None lets the *next* finder answer, and the real package is still on disk.
    ModuleNotFoundError is what a genuinely absent package raises, so a guard
    written as `except ImportError` is exercised exactly as it would be in the
    field.
    """

    def __init__(self, package: str):
        self.package = package

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.package or fullname.startswith(self.package + "."):
            raise ModuleNotFoundError(
                f"No module named {fullname!r} (removed by "
                f"tests/test_optional_dependencies.py)", name=fullname)
        return None


_WATCHED = ("main", "actions", "core", "ui", "memory", "dashboard")


def _import_everything_without(packages: list[str]) -> list[str]:
    """Import every module of the app with `packages` unavailable.

    Returns the modules that refused. The whole module table is snapshotted and
    put back, for two independent reasons:

      · Reimporting `core.desktop` replaces the module object other tests
        already hold, so `desktop.backend_for("Windows") is windows` starts
        failing for reasons that have nothing to do with the code — the trap
        test_windows_compat.py's pristine_module_cache exists for.
      · Several C extensions are not import-safe twice in one process. Dropping
        `cv2` and letting it be imported again raises
        `module 'cv2.dnn' has no attribute 'DictValue'` — a real property of
        opencv-python, and nothing to do with what is being tested here.

    `main.py` also wraps subprocess.Popen at import time on Windows by
    subclassing whatever Popen currently is, so a run of this would otherwise
    leave a stack of nested wrappers behind for the rest of the session.
    """
    saved_modules = dict(sys.modules)
    saved_popen = subprocess.Popen
    saved_meta = list(sys.meta_path)
    blockers = [_Blocked(p) for p in packages]

    try:
        sys.meta_path[0:0] = blockers
        for name in list(sys.modules):
            root = name.split(".")[0]
            if root in packages or root in _WATCHED:
                del sys.modules[name]

        broken = []
        for name in MODULES:
            try:
                importlib.import_module(name)
            except Exception as exc:      # noqa: BLE001 — the failure is the result
                broken.append(f"{name}: {type(exc).__name__}: {exc}")
        return broken
    finally:
        sys.meta_path[:] = saved_meta
        subprocess.Popen = saved_popen
        # Only Alexio's own modules are dropped. A third-party package pulled in
        # for the first time during this run stays: `sounddevice` and `cv2` both
        # bind a C extension that raises "cannot load module more than once per
        # process" if the Python wrapper is discarded and imported again.
        for name in [k for k in sys.modules
                     if k not in saved_modules and k.split(".")[0] in _WATCHED]:
            del sys.modules[name]
        sys.modules.update(saved_modules)


# ── 1. behaviour: remove the package, start anyway ───────────────────────────

@pytest.mark.parametrize("package", sorted(OPTIONAL_DEPENDENCIES))
def test_the_app_imports_without(package):
    """The test the playwright bug needed and did not have.

    It runs for packages that are not installed here either — that is not a
    weakness, it is the same assertion under a stricter starting point.
    """
    broken = _import_everything_without([package])
    assert not broken, (
        f"removing {package!r} — which only buys "
        f"{OPTIONAL_DEPENDENCIES[package]} — stops Alexio from starting:\n  "
        + "\n  ".join(broken)
    )


def test_removing_them_all_at_once_still_starts():
    """A fresh clone with `pip install` never run at all.

    Each package on its own is the common case; all of them together is the
    first five minutes on a new machine, and it is where guards that quietly
    depend on each other show up.
    """
    broken = _import_everything_without(sorted(OPTIONAL_DEPENDENCIES))
    assert not broken, (
        "with no optional package installed at all, Alexio does not start:\n  "
        + "\n  ".join(broken))


# ── 2. structure: the rule that keeps it from coming back ────────────────────

def _project_files():
    for path in sorted(ROOT.rglob("*.py")):
        if any(p in path.parts for p in (".venv", "__pycache__", "logs", "build",
                                         "tests")):
            continue
        yield path


def _guarded_import_nodes(tree: ast.Module) -> set[int]:
    """Import nodes that sit inside a `try`. Only the try *body* counts: an
    import in the `except` branch runs precisely when the guard has already
    fired."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        guarded.add(id(sub))
    return guarded


def test_no_optional_package_is_imported_unguarded_at_module_level():
    """The specification, stated once.

    Inside a function is always fine — the cost is paid by whoever calls it. At
    the top of a file the cost is paid by everyone who starts the program, so it
    has to be behind a try.
    """
    offenders = []
    for path in _project_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        guarded = _guarded_import_nodes(tree)
        for node in tree.body:                    # module level only
            packages: set[str] = set()
            if isinstance(node, ast.Import):
                packages = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                packages = {node.module.split(".")[0]}
            hits = packages & set(OPTIONAL_DEPENDENCIES)
            if hits and id(node) not in guarded:
                rel = path.relative_to(ROOT).as_posix()
                offenders.append(f"{rel}:{node.lineno} {sorted(hits)}")

    assert not offenders, (
        "optional packages imported at module level with no try/except — one "
        "missing package stops the whole application from starting:\n  "
        + "\n  ".join(offenders)
    )


def test_optional_guards_do_not_catch_importerror_only():
    """`except ImportError` is the guard that looks right and is not.

    pyautogui opens an X display when it is imported and raises
    DisplayConnectionError over SSH, in a TTY, under a systemd unit, in CI.
    playwright raises its own Error when the browsers were never downloaded.
    Neither is an ImportError, so the narrow guard lets it through and the
    module dies anyway — which is the phase 00 bug with one extra step.

    Only the packages that reach for the machine are checked. `except
    ImportError` around a pure-Python parser is honest and stays legal.
    """
    reaches_for_the_machine = {"pyautogui", "pygetwindow", "playwright", "mss",
                               "pycaw", "comtypes", "win32com", "pywinauto",
                               "wmi", "cv2", "pyperclip"}
    offenders = []
    for path in _project_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            packages: set[str] = set()
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Import):
                        packages |= {a.name.split(".")[0] for a in sub.names}
                    elif isinstance(sub, ast.ImportFrom) and sub.module and sub.level == 0:
                        packages.add(sub.module.split(".")[0])
            hits = packages & reaches_for_the_machine
            if not hits:
                continue
            caught = set()
            for handler in node.handlers:
                for name in ast.walk(handler.type) if handler.type else []:
                    if isinstance(name, ast.Name):
                        caught.add(name.id)
                if handler.type is None:
                    caught.add("Exception")       # bare except catches it too
            if "Exception" not in caught and "BaseException" not in caught:
                rel = path.relative_to(ROOT).as_posix()
                offenders.append(f"{rel}:{node.lineno} {sorted(hits)} "
                                 f"caught only {sorted(caught)}")

    assert not offenders, (
        "these guards catch ImportError but the package raises something else "
        "when it cannot reach the display or its browsers — use "
        "`except Exception`:\n  " + "\n  ".join(offenders)
    )


def test_the_two_dependency_lists_do_not_overlap():
    """A package cannot be both optional and required. If one moves, it moves
    once — otherwise the behavioural test above silently stops asserting
    anything about it while still appearing to."""
    both = set(OPTIONAL_DEPENDENCIES) & REQUIRED_DEPENDENCIES
    assert not both, f"listed as both optional and required: {sorted(both)}"
