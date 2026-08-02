"""Tool registry — OpenAI-format definitions, in-process handlers, toolset membership.

The security model is one sentence, ported verbatim from Hermes:

    **All tools must be part of a toolset to be accessible.**

That is default-deny *by construction*, not by policy. A tool registered
without toolset membership is unreachable — not merely undocumented — because
:func:`get_tool_definitions` only ever emits names that a toolset resolved to.
There is no "and also include these" escape hatch, and adding one would silently
delete the model.

Two registries, two jobs (do not conflate them):

* ``runtime.toolsets.TOOLSETS`` — *structure*. Which names belong to which
  toolset, and how toolsets compose. Already Hermes-faithful; this module does
  not duplicate or replace it.
* This module — *substance*. Name -> OpenAI-format schema + the callable that
  actually runs it.

``get_tool_definitions()`` is the join, and it is the only public read path.

Deliberate deviation from Hermes
--------------------------------
Hermes' ``_compute_tool_definitions`` treats ``enabled_toolsets=None`` as
"start with everything" (``model_tools.py`` lines 403-407). That is a CLI
convenience for a single-user harness. Porting it here would invert the model:
a persona whose config forgot to declare toolsets would silently receive the
entire catalog. This module fails CLOSED instead — ``None`` means *nothing*,
and the caller must pass toolset names to receive tools. This is the one place
where a faithful port would be the wrong port.

Statelessness
-------------
The catalog is rebuilt on every ``get_tool_definitions()`` call. There is no
memoization, no session-keyed cache, and no refresh API — deliberately.
OpenClaw's ``openclaw#84141`` regression came from a session-keyed catalog
drifting out of sync with the live registry, which produced *silent tool
dropouts*: the model simply stopped being offered tools it still had, with no
error anywhere. :data:`_GENERATION` exists so tests can PROVE freshness rather
than assert it in a comment.

Rule 1 compliance: every caller-facing default is a ``None`` sentinel resolved
inside the function body. Nothing that can be mutated at runtime is bound as a
default argument.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolRegistryError(Exception):
    """Raised on a registration that would corrupt the registry.

    Registration is a *developer-time* operation, so it fails LOUD — unlike
    resolution, which follows the Hermes silent-on-missing pattern because a
    missing toolset is routinely just "optional plugin not loaded".
    """


# ---------------------------------------------------------------------------
# Tool entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolEntry:
    """One registered tool: what the model sees, and what actually runs.

    Attributes:
        name: Tool name. Must match ``schema["function"]["name"]`` — the
            registry enforces this, because a mismatch means the model is
            told to call one name while dispatch looks up another, and the
            failure surfaces as an unexplained "no such tool" at call time.
        description: One-line summary. Mirrors the schema description and is
            what progressive-disclosure search indexes (see #245).
        schema: The full OpenAI-format entry —
            ``{"type": "function", "function": {name, description, parameters}}``.
            This exact dict is what goes on the wire. The format is the
            portability mechanism: the same bytes produced a structured call
            on Kimi K3 and were ignored by the Codex CLI, and neither outcome
            required a vendor SDK.
        handler: The in-process callable. ``None`` is legal and means
            "declared but not yet wired" — such a tool is still advertised to
            the model, so execution tickets (#239/#240) must treat a ``None``
            handler as a hard error at dispatch, never as a silent no-op.
        toolset: Owning toolset name. Non-empty is enforced at registration —
            this is the field that makes the default-deny invariant structural.
        effect: ``"read"`` or ``"write"``. Advisory metadata for audit rows
            (#242); it does NOT gate anything here. Real one-way doors (money,
            external posts) keep their existing dedicated gates.
        integration_action: Optional canonical direct-integration action ID
            implemented by this caller-tool wrapper, for example
            ``"sheets.read"``. Readiness uses this metadata together with
            handler and persona-scope checks; it never grants authorization.
        persona_scoped: Whether persona dispatch must inject the calling
            persona ID into the handler. This is structural metadata: a scoped
            handler that forgets to accept the identity fails loudly instead
            of silently falling through to operator-global state.
        dispatch_context_scoped: Whether persona dispatch must also inject the
            bounded origin/turn context. Only authorization meta-tools should
            need this; ordinary tools stay independent of chat transport.
        elevatable: Whether an out-of-scope persona may ask an operator for one
            exact, one-use call. Defaults false so a newly registered tool does
            not silently acquire an approval bypass surface.
        dedicated_gate: True when the tool owns a stronger authorization gate
            (money, external posts, profile mutation, browser writes, etc.). A
            dedicated-gate tool can never be one-time elevated.
    """

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[..., Any] | None = None
    toolset: str = ""
    effect: str = "read"
    integration_action: str | None = None
    persona_scoped: bool = False
    dispatch_context_scoped: bool = False
    elevatable: bool = False
    dedicated_gate: bool = False


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, ToolEntry] = {}

# Bumped on every mutation. Not a cache key — there is no cache. It exists so a
# test can prove a mutation is observable on the very next assembly, which is
# the openclaw#84141 invariant stated as an assertion instead of a comment.
_GENERATION: int = 0

# Reserved by the progressive-disclosure bridge (#245). Registering a tool under
# one of these names would let a plugin shadow the bridge and intercept every
# deferred call, so the names are refused up front rather than after the fact.
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {"tool_search", "tool_describe", "tool_call"}
)

# `execute` is its own class, not a synonym for `write`. Collapsing a shell
# into `write` would make any future policy that reasons over effects wrong in
# both directions: "allow writes" would silently grant arbitrary execution, and
# "deny writes" would read as if it had stopped a shell when a shell is exactly
# what it did not describe. The sibling contract in
# `integrations/capabilities.py` already carries richer effects
# (`external_post`, `archive`, `send`) and tests mutation as `effect != "read"`,
# so consumers written that idiomatic way treat `execute` as mutating for free.
VALID_EFFECTS: frozenset[str] = frozenset({"read", "write", "execute"})


def build_tool_schema(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one OpenAI-format tool definition.

    Kept as a helper so the wire shape is written in exactly one place. Every
    provider that speaks function calling accepts this shape, which is the
    entire portability argument — see the module docstring.

    Args:
        name: Tool name (must match the registered entry's name).
        description: What the tool does, in the model's terms.
        parameters: JSON Schema for the arguments. ``None`` (Rule 1 sentinel)
            resolves to a valid empty object schema rather than being omitted —
            some providers reject a function entry with no ``parameters`` key.
    """
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def _validate_schema_shape(schema: Any, name: str) -> None:
    """Reject anything that is not a well-formed OpenAI tool definition.

    Name-matching alone is not enough. A schema that passes a name check but is
    otherwise malformed reaches the provider and fails there — as an opaque
    400, at request time, on whichever lane happened to be selected. Validating
    the shape at REGISTRATION turns a runtime provider error into a loud
    developer error at import.
    """
    if not isinstance(schema, dict):
        raise ToolRegistryError(f"tool {name!r} schema must be a dict, got {type(schema).__name__}")
    if schema.get("type") != "function":
        raise ToolRegistryError(
            f"tool {name!r} schema must have type='function', got {schema.get('type')!r}"
        )
    fn = schema.get("function")
    if not isinstance(fn, dict):
        raise ToolRegistryError(f"tool {name!r} schema is missing a 'function' object")
    if fn.get("name") != name:
        raise ToolRegistryError(
            f"schema function name {fn.get('name')!r} does not match registered "
            f"name {name!r} — the model would be told to call a name that "
            "dispatch cannot resolve"
        )
    if not isinstance(fn.get("description"), str):
        raise ToolRegistryError(f"tool {name!r} schema needs a string description")
    params = fn.get("parameters")
    if not isinstance(params, dict):
        raise ToolRegistryError(
            f"tool {name!r} schema needs a 'parameters' object — some providers "
            "reject a function entry without one"
        )
    if params.get("type") != "object":
        raise ToolRegistryError(
            f"tool {name!r} parameters must be a JSON Schema object, got {params.get('type')!r}"
        )


def register_tool(
    name: str,
    description: str,
    *,
    toolset: str,
    parameters: dict[str, Any] | None = None,
    handler: Callable[..., Any] | None = None,
    effect: str = "read",
    integration_action: str | None = None,
    persona_scoped: bool = False,
    dispatch_context_scoped: bool = False,
    elevatable: bool = False,
    dedicated_gate: bool = False,
    schema: dict[str, Any] | None = None,
) -> ToolEntry:
    """Register a tool and return its entry.

    ``toolset`` is keyword-only and has no default on purpose. The one-line
    security model is "all tools must be part of a toolset to be accessible";
    a default here would let a caller register an unowned tool by omission,
    which is exactly the hole the model exists to close.

    Raises:
        ToolRegistryError: on an empty name, a reserved name, a blank toolset,
            an unknown ``effect``, a schema/name mismatch, or a duplicate
            registration under a different toolset.
    """
    global _GENERATION

    if not name or not name.strip():
        raise ToolRegistryError("tool name must be a non-empty string")
    name = name.strip()

    if name in RESERVED_TOOL_NAMES:
        raise ToolRegistryError(
            f"{name!r} is reserved for the progressive-disclosure bridge "
            "and cannot be registered as an ordinary tool"
        )

    if not toolset or not toolset.strip():
        raise ToolRegistryError(
            f"tool {name!r} must declare a non-empty toolset — all tools must "
            "be part of a toolset to be accessible"
        )
    toolset = toolset.strip()

    if effect not in VALID_EFFECTS:
        raise ToolRegistryError(
            f"tool {name!r} has unknown effect {effect!r} "
            f"(expected one of {sorted(VALID_EFFECTS)})"
        )
    if not isinstance(persona_scoped, bool):
        raise ToolRegistryError(
            f"tool {name!r} persona_scoped must be a boolean"
        )
    if not isinstance(dispatch_context_scoped, bool):
        raise ToolRegistryError(
            f"tool {name!r} dispatch_context_scoped must be a boolean"
        )
    if not isinstance(elevatable, bool) or not isinstance(dedicated_gate, bool):
        raise ToolRegistryError(
            f"tool {name!r} elevation flags must be booleans"
        )
    if elevatable and dedicated_gate:
        raise ToolRegistryError(
            f"tool {name!r} cannot be both elevatable and dedicated_gate"
        )
    if integration_action is not None:
        integration_action = integration_action.strip()
        if (
            not integration_action
            or integration_action.startswith(".")
            or integration_action.endswith(".")
            or "." not in integration_action
        ):
            raise ToolRegistryError(
                f"tool {name!r} integration_action must use "
                "'<integration>.<action>' form"
            )

    if schema is None:
        schema = build_tool_schema(name, description, parameters)

    _validate_schema_shape(schema, name)

    # Defensive copy. `@dataclass(frozen=True)` freezes the ATTRIBUTE BINDING,
    # not the nested dict — without this, a caller who mutates the dict they
    # passed in silently rewrites what the registry emits, INCLUDING the tool
    # name that was just validated. Storing a copy means validation describes
    # the stored object permanently, not just at the instant it ran.
    schema = copy.deepcopy(schema)

    existing = _REGISTRY.get(name)
    if existing is not None and existing.toolset != toolset:
        # Re-registering under the SAME toolset is a legal reload (test
        # override, module re-import). Silently moving a tool between toolsets
        # would change its reachability without anyone asking for it.
        raise ToolRegistryError(
            f"tool {name!r} is already registered in toolset "
            f"{existing.toolset!r}; refusing to move it to {toolset!r}"
        )

    entry = ToolEntry(
        name=name,
        description=description,
        schema=schema,
        handler=handler,
        toolset=toolset,
        effect=effect,
        integration_action=integration_action,
        persona_scoped=persona_scoped,
        dispatch_context_scoped=dispatch_context_scoped,
        elevatable=elevatable,
        dedicated_gate=dedicated_gate,
    )
    _REGISTRY[name] = entry
    _GENERATION += 1
    return entry


def unregister_tool(name: str) -> bool:
    """Remove a tool. Returns True if it was present.

    Exists for test isolation and plugin teardown. Removal bumps the generation
    for the same reason registration does: the next assembly must see it.
    """
    global _GENERATION
    if name in _REGISTRY:
        del _REGISTRY[name]
        _GENERATION += 1
        return True
    return False


def get_entry(name: str) -> ToolEntry | None:
    """Return the registered entry for ``name``, or None.

    Note this is a *registration* lookup, not an *authorization* lookup. It
    answers "does this tool exist", never "may this persona call it". Scoping
    is decided by :func:`get_tool_definitions` from toolset membership. Callers
    on an execution path must not use this as a permission check.
    """
    return _REGISTRY.get(name)


def get_generation() -> int:
    """Current registry generation. Increments on every mutation."""
    return _GENERATION


def list_registered() -> list[ToolEntry]:
    """Every registered entry, name-sorted. Diagnostics only.

    Deliberately NOT scoped — this bypasses toolset membership and must never
    be used to build a model-facing tools array. :func:`get_tool_definitions`
    is the only path that respects the default-deny invariant.
    """
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def list_registered_for_integration_action(action_id: str) -> list[ToolEntry]:
    """Return registered caller-tool wrappers for one integration action.

    This is registration metadata only. Consumers must separately verify that
    the entry has a handler and belongs to the persona's resolved scope.
    """

    normalized = action_id.strip()
    return [
        entry
        for entry in list_registered()
        if entry.integration_action == normalized
    ]


# ---------------------------------------------------------------------------
# The join: toolset structure -> tool substance
# ---------------------------------------------------------------------------


def resolve_tool_names(
    enabled_toolsets: list[str] | None = None,
    disabled_toolsets: list[str] | None = None,
    registry: dict[str, Any] | None = None,
) -> list[str]:
    """Resolve toolset names into the set of reachable tool names.

    Fails CLOSED: ``enabled_toolsets=None`` or ``[]`` resolves to ``[]``. See
    the module docstring for why this deviates from Hermes.

    ``disabled_toolsets`` subtracts *after* the union, so a name reachable
    through two enabled toolsets is removed only if it is also reachable
    through a disabled one. Subtraction is by resolved NAME, not by toolset
    label — disabling a toolset that shares members with an enabled one removes
    the shared members too. That is the conservative reading of "disabled", and
    the loud one: the operator sees fewer tools rather than more.

    Unknown toolset names resolve to nothing (Hermes silent-on-missing), so a
    typo yields no tools rather than an exception. Validating persona-declared
    toolset names loudly is #241's job, at config-load time, where the operator
    can actually see the error.
    """
    if not enabled_toolsets:
        return []

    # Rule 3: late module-attribute lookup so a test monkey-patching
    # ``runtime.capabilities.resolve_toolset`` is honored. A top-level
    # ``from runtime.capabilities import resolve_toolset`` would cache the
    # original function and silently ignore the patch.
    from runtime import capabilities as _caps

    enabled_names: set[str] = set()
    for toolset_name in enabled_toolsets:
        enabled_names.update(_caps.resolve_toolset(toolset_name, registry))

    if disabled_toolsets:
        for toolset_name in disabled_toolsets:
            enabled_names.difference_update(
                _caps.resolve_toolset(toolset_name, registry)
            )

    return sorted(enabled_names)


def get_tool_definitions(
    enabled_toolsets: list[str] | None = None,
    disabled_toolsets: list[str] | None = None,
    registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the OpenAI-format tools array for a given toolset scope.

    This is the ONLY public path from toolset scope to a model-facing tools
    array, and the only place the default-deny invariant is enforced. Rebuilt
    from scratch on every call — no cache, ever (openclaw#84141).

    A name that a toolset lists but nothing registered is skipped with a debug
    log rather than raising: toolsets legitimately reference tools whose module
    has not loaded yet, which is Hermes' "optional plugin not loaded" pattern.
    The inverse — a tool that IS registered but belongs to no enabled toolset —
    is what must be unreachable, and it is, because iteration is driven by the
    resolved names and never by the registry.

    Returns:
        List of OpenAI-format tool definitions, ordered by tool name for
        deterministic prompts (an unstable tools array would churn prompt-cache
        prefixes on every turn for no reason).
    """
    names = resolve_tool_names(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        registry=registry,
    )

    granted = resolve_toolset_closure(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        registry=registry,
    )

    definitions: list[dict[str, Any]] = []
    missing: list[str] = []
    disowned: list[str] = []
    for name in names:
        entry = _REGISTRY.get(name)
        if entry is None:
            missing.append(name)
            continue
        # OWNERSHIP CHECK — the two registries must AGREE.
        #
        # `runtime.toolsets` owns structure (which names a toolset claims);
        # this module owns substance (which toolset a tool was registered
        # under). Resolving names from the structural side and emitting the
        # schema without re-checking the substantive side trusts one registry
        # blindly: a toolset that merely LISTS `secret_tool` would hand it over
        # even though the tool declared a different owner.
        #
        # That is reachable without malice — a typo in a `tools:` list, a name
        # collision between a live-source toolset and a registered tool, or a
        # custom registry passed by a caller. Requiring the declared owner to
        # be inside the granted closure makes `ToolEntry.toolset` load-bearing
        # instead of decorative.
        if entry.toolset not in granted:
            disowned.append(f"{name}(owner={entry.toolset})")
            continue
        # Emit a COPY. The registry hands out schemas on every assembly; a
        # consumer that mutates one it received would poison every later
        # assembly for every persona (the catalog is rebuilt, but from these
        # same stored dicts).
        definitions.append(copy.deepcopy(entry.schema))

    if missing:
        _logger.debug(
            "toolset scope resolved %d name(s) with no registered tool: %s",
            len(missing),
            ", ".join(missing),
        )
    if disowned:
        # WARNING, not debug: a name resolving into scope while its owner is
        # out of scope means the two registries disagree, and the safe outcome
        # (refuse) hides a real misconfiguration. Say so.
        _logger.warning(
            "refused %d tool(s) whose declared toolset is not in the granted "
            "scope %s: %s",
            len(disowned),
            sorted(granted),
            ", ".join(disowned),
        )

    return definitions


def resolve_toolset_closure(
    enabled_toolsets: list[str] | None = None,
    disabled_toolsets: list[str] | None = None,
    registry: dict[str, Any] | None = None,
) -> frozenset[str]:
    """Return the set of toolset NAMES granted by this scope.

    Distinct from :func:`resolve_tool_names`, which returns tool names. This is
    the transitive closure over ``includes`` — granting ``browser`` (which
    includes ``research``, which includes ``core``) grants all three names, so a
    tool registered under ``core`` is legitimately reachable.

    Disabled toolsets are removed from the closure AFTER expansion, so disabling
    a toolset also disowns tools registered under it.

    Fails closed: no enabled toolsets means an empty closure.
    """
    if not enabled_toolsets:
        return frozenset()

    if registry is None:
        from runtime.toolsets import TOOLSETS as _DEFAULT_REGISTRY
        registry = _DEFAULT_REGISTRY

    def _expand(name: str, seen: set[str]) -> None:
        if name in seen:
            return
        seen.add(name)
        toolset = registry.get(name)
        if not toolset:
            return
        for included in toolset.get("includes", []):
            _expand(included, seen)

    granted: set[str] = set()
    for toolset_name in enabled_toolsets:
        _expand(toolset_name, granted)

    if disabled_toolsets:
        removed: set[str] = set()
        for toolset_name in disabled_toolsets:
            _expand(toolset_name, removed)
        granted -= removed

    return frozenset(granted)


__all__ = [
    "RESERVED_TOOL_NAMES",
    "VALID_EFFECTS",
    "ToolEntry",
    "ToolRegistryError",
    "build_tool_schema",
    "get_entry",
    "get_generation",
    "get_tool_definitions",
    "list_registered",
    "list_registered_for_integration_action",
    "register_tool",
    "resolve_tool_names",
    "resolve_toolset_closure",
    "unregister_tool",
]
