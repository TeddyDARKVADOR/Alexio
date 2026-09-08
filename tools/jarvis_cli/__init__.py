"""
tools/jarvis_cli — one entry point for developing Alexio.

    ./jarvis help

Everything the project can do to itself lives behind one verb, so nobody has to
remember whether coverage is a pytest flag, a module, or a shell pipeline. The
underlying tools are still directly runnable — `python -m tools.jarvis_lint`,
`python -m tools.jarvis_health` — this is the front door, not a wrapper that
hides them.
"""
