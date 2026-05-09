"""Security slice — module-only re-exports (Rule 3 enforcement).

Consumers MUST import the module, NEVER the function:

    # CORRECT
    from security import kill_switches
    kill_switches.requireEnabled("llm")

    from security import redact
    redact.redact_sensitive_text(msg)
    # or the Homie alias:
    redact.redact(msg)

    # WRONG — defeats monkeypatch (Rule 3, see CLAUDE.md:124-144)
    from security import requireEnabled  # forbidden
    from security.kill_switches import requireEnabled  # forbidden
    from security.redact import redact_sensitive_text  # forbidden

R1 B4 fix (Phase 7a): re-exporting callables would create a Rule 3 escape hatch —
top-level `from security import requireEnabled` defeats monkeypatch propagation
in tests. A grep gate AND an AST gate enforce that production code uses the
module-attribute pattern.

PRD-8 Phase 7b (WS1.5): added ``redact`` module re-export.

PRD-8 Phase 7b R4 (codex R3 NM1): the ``redact`` re-export is LAZY via PEP 562
``__getattr__``. Eagerly importing ``redact`` here would force every consumer
of ``kill_switches`` (lane_router, cabinet text_router/text_orchestrator) to
ALSO load ``config`` (because ``redact.py`` imports ``config`` at module top
for the NB2 fix) and snapshot ``_REDACT_ENABLED`` against the wrong profile
env if security is imported before the profile boot finalizes ``HOMIE_HOME``.
By eagerly loading only ``kill_switches`` + ``patterns`` and deferring
``redact`` to attribute access, we keep the NB2 contract for redact consumers
(``from security import redact`` still triggers the config-import precondition)
without paying that cost on the kill-switch import path.
"""

import importlib

from . import kill_switches, patterns

__all__ = ["kill_switches", "patterns", "redact"]


def __getattr__(name: str):
    """PEP 562 module-level lazy attribute resolution.

    ``from security import redact`` (or any attribute access of ``redact`` on
    this package) triggers this function, which imports the ``redact`` submodule
    on first access via ``importlib.import_module`` and binds it onto this
    package so subsequent accesses go through the normal attribute path.

    Why ``importlib.import_module`` instead of ``from . import redact``:
    Python's ``from . import redact`` invokes ``security.__getattr__("redact")``
    when ``redact`` isn't already bound — recursing into this function. The
    ``importlib.import_module`` call performs the actual submodule load (which
    populates ``sys.modules['security.redact']``) without going through
    attribute access, breaking the recursion.

    Importing ``kill_switches`` or ``patterns`` does NOT trigger this — those
    are eagerly bound at the top of this file.

    Phase 7b WS1.5 R4 fix (codex R3 NM1).
    """
    if name == "redact":
        module = importlib.import_module(".redact", __name__)
        # Bind onto this package so subsequent accesses skip __getattr__.
        globals()["redact"] = module
        return module
    raise AttributeError(f"module 'security' has no attribute {name!r}")
