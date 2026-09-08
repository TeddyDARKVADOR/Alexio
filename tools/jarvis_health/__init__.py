"""
tools/jarvis_health — three different questions about the same tree.

    Code coverage      was this line executed by a test?
    Behaviour coverage is this guarantee actually checked?
    Rule coverage      is this invariant mechanically enforced?

The three are not substitutes. core/ai/router.py measures 97% line coverage and
93% branch coverage; `Budget.private` itself measures 100%; and the guarantee
that a private budget never leaves the machine was broken the entire time,
because the raise happens in router.py and is swallowed one module up. A number
that cannot see that is a number that can be farmed.

    .venv/bin/python -m tools.jarvis_health --measure   # refresh coverage
    .venv/bin/python -m tools.jarvis_health             # the score
    .venv/bin/python -m tools.jarvis_health --tree      # per module
    .venv/bin/python -m tools.jarvis_health --risk      # what to fix first
    .venv/bin/python -m tools.jarvis_health core/ai/router.py
"""

from .behaviors import BEHAVIOURS, Behaviour
from .report import BY_RULE, TESTED, UNTESTED, VIOLATED, Report, build

__all__ = ["BEHAVIOURS", "Behaviour", "Report", "build",
           "TESTED", "BY_RULE", "UNTESTED", "VIOLATED"]
