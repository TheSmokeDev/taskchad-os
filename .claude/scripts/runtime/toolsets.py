"""Static toolset registry — Hermes shape (dict-of-dicts, not dataclass).

Auto-discovery extension: toolsets carrying ``live_source`` and ``live_filter``
resolve their contents at every ``resolve_toolset()`` call via
``list_capabilities()``. No cache — the registry captures structural intent
only; the actual tools come from the live aggregator surface.

The static dict literal below is the single source of truth for toolset
structure. There is no build function, no cache variable, and no refresh API.
This is the Hermes-faithful pivot: data-shape parity with
``hermes-agent/toolsets.py`` (lines 68+ for the literal, lines 504-554 for the
resolver). The single deviation is the optional ``live_source`` /
``live_filter`` pair, which generalizes Hermes' own plugin late-lookup pattern
(``get_toolset()`` lines 472-501) for The Homie's adopter story.

Modules in this package never import from ``runtime.capabilities`` here at
load time — both modules late-import each other inside functions, so this file
remains a leaf module.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class Toolset(TypedDict):
    """Toolset shape (Hermes-faithful, with optional auto-discovery extension).

    Required fields match Hermes verbatim. ``live_source`` and ``live_filter``
    are NotRequired and are The Homie's product-justified extension for
    auto-discovery (no analogue in Hermes).
    """

    description: str
    tools: list[str]
    includes: list[str]
    # The Homie's auto-discovery extension (not in Hermes):
    live_source: NotRequired[str]
    live_filter: NotRequired[str]


# Static module-level registry. Hermes shape: dict of dicts.
#
# Auto-discovery toolsets (those carrying ``live_source``) resolve their
# contents by calling ``list_capabilities(sources=[live_source])`` on every
# ``resolve_toolset()`` call. There is no cache layer between the registry
# and the live aggregator — staleness window is zero.
TOOLSETS: dict[str, Toolset] = {
    "chat_commands": {
        "description": "All registered chat commands (auto-discovered from extension manager)",
        # No hand-listed tools — auto-discovery via live_source.
        "tools": [],
        "includes": [],
        "live_source": "chat_extensions",
        "live_filter": "chat.command.",
    },
    "chat_intents": {
        "description": "All registered chat intent detectors (auto-discovered)",
        "tools": [],
        "includes": [],
        "live_source": "chat_extensions",
        "live_filter": "chat.intent.",
    },
    "chat_all": {
        "description": "All chat capabilities (commands + intents)",
        "tools": [],
        # NOTE (R2 Minor 2): each child toolset declares
        # ``live_source="chat_extensions"``, so resolving ``chat_all`` calls
        # ``list_capabilities(sources=["chat_extensions"])`` TWICE — once for
        # each child. The same source is aggregated twice on every resolve.
        # Acceptable for cold-path callers (admin / diagnostics); do not call
        # ``resolve_toolset("chat_all")`` from hot paths. See
        # ``capabilities.resolve_toolset`` for the resolver implementation.
        "includes": ["chat_commands", "chat_intents"],
    },
}
