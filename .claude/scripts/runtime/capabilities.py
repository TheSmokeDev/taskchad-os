"""Named capability tiers and capability/toolset aggregator.

This module serves a dual role:

1. Three legacy string constants (``TEXT_REASONING``, ``TOOL_REASONING``,
   ``VOICE_AUXILIARY``) used throughout the runtime for capability tier
   selection. These are the original contents of the file and remain
   importable unchanged — 16+ files in the codebase depend on them.

2. The capability aggregator — ``Capability`` dataclass, ``_AGGREGATORS``
   dispatch dict, ``register_aggregator()`` helper,
   ``_aggregate_chat_extensions()`` aggregator, ``list_capabilities()``
   entry point with the ``capabilities_resolved`` Langfuse span, and the
   Hermes-faithful ``resolve_toolset()`` recursive resolver.

PRP reference: ``PRPs/active/PRP-framework-capability-toolsets-1a.md``.

Hermes pattern source: ``~/hermes-agent/toolsets.py`` lines
504-554 (resolver) + the static ``TOOLSETS`` dict literal at lines 68+.
The Homie ports Hermes verbatim with one product-justified extension
(``live_source`` / ``live_filter`` for auto-discovery).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.toolsets import Toolset


# ---------------------------------------------------------------------------
# Legacy capability tier constants (pre-existing — DO NOT modify)
# ---------------------------------------------------------------------------

TEXT_REASONING = "text_reasoning"
TOOL_REASONING = "tool_reasoning"
VOICE_AUXILIARY = "voice_auxiliary"


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class CapabilityRegistryError(Exception):
    """Base exception for capability registry errors.

    Hermes-faithful semantics: ``resolve_toolset()`` returns ``[]`` silently
    on cycle and on missing toolset (matches "optional plugin not loaded"
    pattern). This base class is preserved for future loud-failure paths in
    higher-layer registry operations (e.g. registering an aggregator under a
    duplicate source name) and does NOT have any subclasses in PRP-1a.
    """


# ---------------------------------------------------------------------------
# Capability dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """A capability surfaced by an inner registry to the aggregator.

    Attributes:
        id: Dot-namespaced identifier. Format depends on the source:
            - ``chat.command.<command_name>`` for chat router commands.
            - ``chat.intent.<extension_id>.<command>`` for chat data intents
              (R1 B4 fix: namespaced by extension id so two extensions can
              route different intents to the same router command without
              colliding on capability id; the original draft used the
              non-namespaced form ``chat.intent.<command>`` and silently
              deduplicated cross-extension intents).
            - ``integration.<name>`` for native platform integrations
              (defined in PRP-1b — not produced in PRP-1a).
            - ``runtime.overlay.<name>`` for runtime profile overlays
              (defined in PRP-1c — not produced in PRP-1a).
        display_name: Short human-readable label (typically the command name).
        enabled: SNAPSHOT of the source's enabled state at the moment
            ``list_capabilities()`` was called. This field is NOT a live
            view. Callers that need live enabled-state for an integration
            must call the integration registry directly. Documented as
            snapshot-only to satisfy Rule 2 (meta is derived state, not
            source of truth — snapshot with documented semantics is
            acceptable; what is forbidden is trusting cached meta as the
            current state of a destructive guard).
        source: Producer label — ``"chat_extension"`` for chat,
            ``"integration"`` for PRP-1b, ``"runtime_overlay"`` for PRP-1c.
        extension_id: For chat-extension capabilities, the owning extension
            ID. None for non-extension sources.
        description: Human-readable description (default empty string).
    """

    id: str
    display_name: str
    enabled: bool
    source: str
    extension_id: str | None = None
    description: str = ""


# ---------------------------------------------------------------------------
# Aggregator dispatch dict + registration helper
# ---------------------------------------------------------------------------


def _aggregate_chat_extensions() -> list[Capability]:
    """Aggregate chat-extension commands and intents into ``Capability`` rows.

    Late-imports ``extension_manager`` because the chat slice and the runtime
    slice are siblings — importing at module top would force the runtime to
    unconditionally load the entire extension system (and could create a
    circular dependency on import order). Returns an empty list silently if
    the import fails (defensive — the caller must keep working even if the
    chat slice is unavailable).
    """
    try:
        from extension_manager import get_manager
    except ImportError:
        return []

    manager = get_manager()
    caps: list[Capability] = []
    for meta in manager.get_all_extensions():
        for cmd in meta.commands:
            caps.append(
                Capability(
                    id=f"chat.command.{cmd.name}",
                    display_name=cmd.name,
                    # Snapshot of the extension's enabled flag at this moment.
                    # See Capability docstring for the snapshot contract.
                    enabled=meta.enabled,
                    source="chat_extension",
                    extension_id=meta.id,
                    description=cmd.description,
                )
            )
        for intent in meta.intents:
            caps.append(
                Capability(
                    # R1 B4: namespace by extension_id so two extensions can
                    # both target the same router command without silent dedup.
                    id=f"chat.intent.{meta.id}.{intent.command}",
                    display_name=intent.command,
                    enabled=meta.enabled,
                    source="chat_extension",
                    extension_id=meta.id,
                )
            )
    return caps


# Module-level dispatch dict. PRP-1a registers ``chat_extensions`` here.
# PRP-1b registers ``"integrations"`` from ``integrations/registry.py``.
# PRP-1c registers ``"runtime_overlays"`` from ``runtime/profiles.py``.
# Each follow-on slice calls ``register_aggregator()`` from its own module so
# this file does not need to be re-touched.
_AGGREGATORS: dict[str, Callable[[], list[Capability]]] = {
    "chat_extensions": _aggregate_chat_extensions,
}


def register_aggregator(
    source: str, fn: Callable[[], list[Capability]],
) -> None:
    """Register a capability source aggregator.

    Called by PRP-1b/1c slices on import to add their inner registries onto
    the dispatch dict. Idempotent: re-registering the same source replaces
    the previous function (supports test override and slice reload). PRP-1a
    does not need to call this externally — ``chat_extensions`` is
    pre-registered.
    """
    _AGGREGATORS[source] = fn


# ---------------------------------------------------------------------------
# list_capabilities() — primary aggregator entry point
# ---------------------------------------------------------------------------


def list_capabilities(sources: list[str] | None = None) -> list[Capability]:
    """Aggregate capabilities from one or more source aggregators.

    Args:
        sources: Optional list of source names (Rule 1 sentinel — never
            bind ``_AGGREGATORS`` keys as default). Defaults to
            ``["chat_extensions"]`` when None.

    Returns:
        Flat list of ``Capability`` rows in source iteration order. Unknown
        sources are silently skipped (Hermes silent-on-missing pattern).

    Wraps the aggregation pass in the ``capabilities_resolved`` Langfuse
    span via ``orchestration.observability.orchestration_span``. Failure
    semantics:

    - Observability import failures fail open: returns the aggregated
      capabilities without a span (degrades to ``_list_capabilities_no_span``).
      The runtime contract is preserved when the orchestration slice is
      unavailable.
    - Aggregator exceptions DO propagate to the caller. Callers must handle
      source-specific errors (e.g. ImportError raised inside an aggregator
      other than ``_aggregate_chat_extensions`` — that one swallows
      ``extension_manager`` ImportError by design and returns ``[]``).
    """
    if sources is None:
        sources = ["chat_extensions"]

    # Rule 3: late module-attribute lookup. Direct ``from ... import`` would
    # cache the function reference at module-load time and break test
    # monkey-patches that target the source module.
    try:
        from orchestration import observability as _obs
    except ImportError:
        # Observability layer absent — degrade to no-span path; runtime
        # contract is preserved. This is the documented fail-open path
        # exercised by ``test_span_helper_import_error_does_not_crash``.
        return _list_capabilities_no_span(sources)

    with _obs.orchestration_span(
        "capabilities_resolved",
        metadata={"sources": list(sources)},
        tags=["capabilities"],
    ):
        result: list[Capability] = []
        for src in sources:
            agg = _AGGREGATORS.get(src)
            if agg is None:
                # Hermes silent-on-missing — unknown source is "optional
                # plugin not loaded", not an error condition.
                continue
            result.extend(agg())
        _obs.update_observation(
            metadata={
                "total": len(result),
                "enabled_count": sum(1 for c in result if c.enabled),
                "sources_resolved": list(sources),
            }
        )
        return result


def _list_capabilities_no_span(sources: list[str]) -> list[Capability]:
    """Fallback path when observability module fails to import.

    Behavior-equivalent to ``list_capabilities`` minus the Langfuse span.
    Defensive — should never trigger in normal runtime, but proves the
    documented fail-open contract under
    ``test_span_helper_import_error_does_not_crash``.
    """
    result: list[Capability] = []
    for src in sources:
        agg = _AGGREGATORS.get(src)
        if agg is None:
            continue
        result.extend(agg())
    return result


# ---------------------------------------------------------------------------
# resolve_toolset() — Hermes-faithful recursive resolver
# ---------------------------------------------------------------------------


def resolve_toolset(
    name: str,
    registry: "dict[str, Toolset] | None" = None,
    _visited: set[str] | None = None,
) -> list[str]:
    """Resolve a toolset name into a sorted, deduplicated list of capability ids.

    Hermes-faithful semantics (matches ``hermes-agent/toolsets.py``
    ``resolve_toolset`` lines 504-554):

    - Returns ``[]`` silently if ``name`` is in ``_visited`` (cycle or
      diamond-already-resolved). Cycles are not bugs — they are an
      inevitable consequence of allowing diamond composition. Silent return
      prevents false-positive errors.
    - Returns ``[]`` silently if ``name`` is not in ``registry`` (Hermes
      "optional plugin not loaded" pattern).
    - Returns ``sorted(list[str])`` (deterministic — Hermes pattern).
    - Each ``includes`` entry is resolved recursively, sharing the
      ``_visited`` set across siblings to prevent re-walking diamond-shared
      subtrees.

    The Homie's product-justified deviation: if a toolset declares a
    ``live_source`` field, ``resolve_toolset()`` calls
    ``list_capabilities(sources=[live_source])`` and includes any capability
    id starting with ``live_filter``. Bolted on cleanly between the
    tools-seed and the ``includes`` recursion, not woven through.

    Args:
        name: Toolset name to resolve.
        registry: Optional toolset registry (Rule 1 sentinel — never bind
            ``TOOLSETS`` as default arg, even though it is intended as
            static; sentinel keeps the function testable with custom
            registries). Defaults to ``runtime.toolsets.TOOLSETS`` via late
            import when None.
        _visited: Internal — set of toolset names already walked in this
            resolution. Sentinel-initialized on first call. The leading
            underscore signals "do not pass me" (Pythonic convention); the
            Hermes original parameter is named ``visited``.
    """
    # Late import: ``toolsets.py`` does not import this module at load time,
    # but this module deliberately late-imports the registry to avoid forcing
    # ``runtime.toolsets`` to load before ``runtime.capabilities`` is fully
    # initialized.
    if registry is None:
        from runtime.toolsets import TOOLSETS as _DEFAULT_REGISTRY
        registry = _DEFAULT_REGISTRY
    if _visited is None:
        _visited = set()

    # Silent on cycle / diamond (Hermes pattern). Diamonds are legal — the
    # shared visited set ensures the shared subtree is walked exactly once
    # across siblings.
    if name in _visited:
        return []
    _visited.add(name)

    toolset = registry.get(name)
    if not toolset:
        # Silent on missing (Hermes "optional plugin not loaded" pattern).
        return []

    tools: set[str] = set(toolset.get("tools", []))

    # Auto-discovery clause — the only piece of code with no Hermes analogue.
    # If the toolset declares ``live_source``, query the live aggregator and
    # include any capability id whose prefix matches ``live_filter`` (empty
    # filter = include all). This is Hermes' own late-lookup pattern from
    # ``get_toolset()`` lines 472-501, generalized for The Homie's adopter
    # story.
    live_source = toolset.get("live_source")
    if live_source:
        live_filter = toolset.get("live_filter", "")
        # Self-import is safe inside the function body — module-init order is
        # already complete by the time this branch runs.
        live_caps = list_capabilities(sources=[live_source])
        tools.update(c.id for c in live_caps if c.id.startswith(live_filter))

    # Recurse via includes; share the visited set across siblings so diamond
    # subtrees are walked exactly once.
    for included_name in toolset.get("includes", []):
        tools.update(resolve_toolset(included_name, registry, _visited))

    return sorted(tools)
