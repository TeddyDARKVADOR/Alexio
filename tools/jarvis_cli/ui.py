"""
tools/jarvis_cli/ui.py — how the commands look.

One place, so `jarvis check`, `jarvis test` and `jarvis health` cannot drift into
three different dialects of the same output. Colour is switched off whenever the
stream is not a terminal (a pipe, a CI log, a file), because escape codes in a
build log are noise nobody asked for.
"""

from __future__ import annotations

import os
import shutil
import sys

_COLOUR = (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
           and os.environ.get("TERM") != "dumb")

_C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "cyan": "\033[36m", "grey": "\033[90m",
}

OK, WARN, BAD, SKIP = "✓", "⚠", "✗", "○"


def c(text: str, colour: str) -> str:
    if not _COLOUR or colour not in _C:
        return text
    return f"{_C[colour]}{text}{_C['reset']}"


def width(default: int = 78) -> int:
    try:
        return min(shutil.get_terminal_size().columns, 100)
    except Exception:
        return default


def title(text: str) -> str:
    return f"\n{c(text.upper(), 'bold')}\n{c('━' * min(width(), 62), 'grey')}"


def rule_line() -> str:
    return c("─" * min(width(), 62), "grey")


def bar(pct: float, size: int = 20, colour: str | None = None) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(size * pct / 100))
    body = "█" * filled + c("░" * (size - filled), "grey")
    return c(body, colour) if colour else body


def verdict_colour(pct: float) -> str:
    return "red" if pct < 60 else ("yellow" if pct < 85 else "green")


def mark(state: str) -> str:
    return {
        "ok":   c(OK, "green"),
        "warn": c(WARN, "yellow"),
        "bad":  c(BAD, "red"),
        "skip": c(SKIP, "grey"),
    }.get(state, " ")


def line(state: str, label: str, detail: str = "") -> str:
    tail = f"   {c(detail, 'grey')}" if detail else ""
    return f"  {mark(state)} {label}{tail}"


def kv(label: str, value: str, pad: int = 20) -> str:
    return f"  {label:<{pad}} {value}"


def hint(text: str) -> str:
    return f"\n{c('→', 'cyan')} {text}"
