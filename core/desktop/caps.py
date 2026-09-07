"""
core/desktop/caps.py — what this machine can actually do, and by what means.

A capability is not a boolean. "Can Alexio take a screenshot?" has three useful
answers: yes via the portal, yes via X11, or no and here is the one command that
would fix it. Collapsing that to True/False is how you get an assistant that
says "done" and changed nothing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    """One thing the desktop layer can be asked for."""

    name:      str
    available: bool
    backend:   str = "none"
    detail:    str = ""

    def __bool__(self) -> bool:
        return self.available

    def __str__(self) -> str:
        mark = "yes" if self.available else "NO "
        line = f"{mark}  {self.name:<16} {self.backend}"
        return f"{line}  — {self.detail}" if self.detail else line


class UnsupportedOnThisPlatform(RuntimeError):
    """The operation has no implementation here.

    Always carries what to do about it: a missing package, a permission to
    grant, a session type to switch. An error that only says "unsupported"
    makes the user guess, and they will guess wrong.
    """
