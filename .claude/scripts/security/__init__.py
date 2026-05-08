"""Security slice — module-only re-exports (Rule 3 enforcement).

Consumers MUST import the module, NEVER the function:

    # CORRECT
    from security import kill_switches
    kill_switches.requireEnabled("llm")

    # WRONG — defeats monkeypatch (Rule 3, see CLAUDE.md:124-144)
    from security import requireEnabled  # forbidden
    from security.kill_switches import requireEnabled  # forbidden

R1 B4 fix: re-exporting callables would create a Rule 3 escape hatch — top-level
`from security import requireEnabled` defeats monkeypatch propagation in tests.
A grep gate AND an AST gate enforce that production code uses the module-attribute
pattern.
"""

from . import kill_switches, patterns

__all__ = ["kill_switches", "patterns"]
