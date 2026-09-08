"""
tools/jarvis_lint — Alexio's own invariants, enforced.

    .venv/bin/python -m tools.jarvis_lint            # what is new since the baseline
    .venv/bin/python -m tools.jarvis_lint --all      # everything, baseline included
    .venv/bin/python -m tools.jarvis_lint --rules    # what each rule is for
    .venv/bin/python -m tools.jarvis_lint --write-baseline

The rules live in three files by what they protect: security_rules (damage),
architecture_rules (where things may live and which door they go through), and
resource_rules (things acquired must be released). Importing this package
registers all of them; see framework.py for why the baseline is not an
ignore-list.
"""

from . import (architecture_rules, execution_rules, resource_rules,  # noqa: F401
               security_rules)
from .framework import (
    BASELINE_PATH,
    Finding,
    Module,
    all_rules,
    load_baseline,
    partition,
    run,
    write_baseline,
)

__all__ = ["Finding", "Module", "all_rules", "run", "load_baseline",
           "write_baseline", "partition", "BASELINE_PATH"]
