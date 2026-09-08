"""
core/paths.py — joining a path to a name somebody else chose.

WHY A HELPER AND NOT A LINE OF CODE AT EACH SITE
    `project_dir / file_path` looks safe and is not, in two separate ways that
    both bite silently:

      · pathlib DISCARDS the left side when the right is absolute.
        Path("/home/u/Desktop/proj") / "/etc/cron.d/x"  ==  Path("/etc/cron.d/x")
        No error, no warning — the base simply stops existing.

      · `..` resolves after the join, so "a/../../../../tmp/x" leaves the root
        by arithmetic rather than by trickery.

    actions/dev_agent.py had both, at two sites, on paths read straight out of
    the planner model's JSON. actions/file_controller.py got it right at fifteen
    sites with its own private `_is_safe_path`. One of those is a rule; the
    other is a habit that held.

WHAT `resolve()` ALONE DOES NOT DO
    Resolving a traversal produces a perfectly valid path outside the root —
    that is what the attack *is*. The check that matters is the comparison
    afterwards, and it has to happen on the resolved form, because comparing
    before resolving compares two different things.

    Symlinks are covered by the same call: `resolve()` follows them, so a link
    inside the root pointing out of it fails the comparison like any other
    escape.
"""

from __future__ import annotations

from pathlib import Path


class PathEscape(ValueError):
    """A name tried to leave the directory it was supposed to stay in.

    Carries both halves, because "invalid path" tells the person reading the log
    nothing about which of the two was wrong.
    """

    def __init__(self, root: Path, requested: str, resolved: Path) -> None:
        self.root = root
        self.requested = requested
        self.resolved = resolved
        super().__init__(
            f"{requested!r} resolves to {resolved} which is outside {root}. "
            f"Paths must stay inside the directory they were given."
        )


def safe_join(root: Path | str, *parts: str) -> Path:
    """`root / parts…`, or raise. Never returns a path outside `root`.

    Use this wherever the tail comes from a model, a tool parameter, an upload
    or the network. Where the tail is a literal written in the file, plain `/`
    is clearer and cannot escape.

        safe_join(project_dir, plan["path"])        # checked
        project_dir / ".venv"                        # a literal; fine as is
    """
    base = Path(root).resolve()
    candidate = base
    for part in parts:
        text = str(part)
        # `~` is refused rather than resolved. Path.resolve() leaves it alone,
        # so "~/x" would land in a directory literally named "~" inside the
        # root — safe here, and an escape the moment anything downstream calls
        # .expanduser() on the result. A tail whose meaning depends on who
        # expands it has no business being a project-relative path.
        if text.startswith("~"):
            raise PathEscape(base, text, Path(text).expanduser())
        candidate = candidate / text
    resolved = candidate.resolve()
    if resolved != base and not resolved.is_relative_to(base):
        raise PathEscape(base, "/".join(str(p) for p in parts), resolved)
    return resolved


def is_inside(root: Path | str, candidate: Path | str) -> bool:
    """The predicate form, for callers that would rather branch than catch."""
    try:
        base = Path(root).resolve()
        target = Path(candidate).resolve()
    except OSError:
        return False
    return target == base or target.is_relative_to(base)
