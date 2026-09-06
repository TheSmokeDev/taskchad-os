"""Typed capability-plugin contributions and their owning-registry adapters.

Issue #530 gave the kernel a transactional lifecycle over GENERIC values: a
plugin could publish a callable, and the kernel could prove it was staged,
leased, and disposed. What it could not prove was that the plugin's *declared
behavior* existed — a manifest saying ``{"type": "tool"}`` produced no tool, and
unload could not remove owner rows that were never lifecycle-bound.

This module is the seam that closes that gap, and it is deliberately NOT a
second registry. Every contribution type here already has an owner:

======================  ==============================================
Contribution            Owning registry (validator AND executor)
======================  ==============================================
TOOL                    ``runtime.tool_registry``
TOOLSET                 ``runtime.toolsets``
SKILL / MCP_SERVER      ``runtime.framework_registry``
COMMAND / INTENT        ``chat.extension_manager``
PROMPT_HOOK etc.        this module (no prior owner existed)
======================  ==============================================

An adapter validates the payload shape, calls the owner's own narrow
register/unregister API, and hands back one disposer. Domain rules — the
default-deny toolset invariant, command collision classification, MCP config
redaction — stay where they already live and are not re-implemented here.

Three invariants carry the ticket:

1. **One owner per row.** Plugin id and version come from the manifest binding
   and can never be supplied by plugin code. Every removal is
   compare-and-remove on that pair, so one plugin can never dispose another's
   row and no plugin can shadow a baseline row.
2. **Hold or fail; never partially load.** Adapter resolution, payload
   validation, and service availability are ALL settled before the first owner
   mutation. A failure part-way through apply rolls the already-applied rows
   back in reverse order.
3. **Inventories carry no secrets.** ``catalog_record`` emits metadata only —
   never a callable, never an env value, never an MCP config value, never a
   filesystem path outside the repo-relative form the owner already publishes.

The module deliberately does not import ``runtime.capability_plugins``: the
kernel imports THIS module, converts :class:`ContributionError` at its own
seam, and keeps the dependency one-way.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from runtime.capability_plugin_manifest import ContributionType, redact_detail

_logger = logging.getLogger(__name__)

MAX_NAME_CHARS = 64
MAX_TEXT_CHARS = 400
MAX_COLLECTION_ITEMS = 128
MAX_DATA_DEPTH = 8
MAX_DATA_NODES = 512

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOOLSET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,63}$")
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOOK_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_RELATIVE_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,255}$")

#: Every contribution type this slice can install through a real owner.
#: ``GENERIC`` is intentionally absent — it keeps its #530 lease-only behavior
#: byte-for-byte. Any manifest type with no adapter fails its plugin loudly
#: rather than producing an inert row that claims loaded behavior.
TYPED_CONTRIBUTION_TYPES: frozenset[ContributionType] = frozenset(
    {
        ContributionType.TOOL,
        ContributionType.TOOLSET,
        ContributionType.SKILL,
        ContributionType.MCP,
        ContributionType.MCP_SERVER,
        ContributionType.COMMAND,
        ContributionType.INTENT,
        ContributionType.PROMPT_HOOK,
        ContributionType.CONTEXT_HOOK,
        ContributionType.HEALTH_PROBE,
        ContributionType.CONFIG_REQUIREMENT,
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ContributionError(ValueError):
    """A stable, operator-safe typed-contribution failure.

    Carries a ``code`` the kernel can copy into a lifecycle receipt verbatim
    and a ``detail`` that has already been through the manifest redactor, so a
    hostile payload cannot smuggle a credential into a receipt or a log line.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = redact_detail(detail)
        super().__init__(f"{code}: {self.detail}")


class ContributionApplyError(ContributionError):
    """Apply failed after zero or more owner rows were already installed.

    ``rollback`` is the receipt for every reverse-order disposal attempted, and
    ``residual_ids`` names the rows whose removal could NOT be proven. A
    non-empty ``residual_ids`` means physical owner state is unknown and the
    caller must degrade to restart-required rather than report a clean failure.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        rollback: Iterable[ContributionDisposal] = (),
        residual_ids: Iterable[str] = (),
        residual: Iterable[AppliedContribution] = (),
    ) -> None:
        super().__init__(code, detail)
        self.rollback = tuple(rollback)
        self.residual_ids = tuple(residual_ids)
        self.residual = tuple(residual)


# ---------------------------------------------------------------------------
# Binding and payload contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContributionOwner:
    """The manifest-derived identity of one contribution.

    Constructed by the kernel from the validated manifest. Plugin code never
    supplies these fields, which is what makes ``plugin_id``/``plugin_version``
    trustworthy as an ownership key rather than a self-reported label.
    """

    plugin_id: str
    plugin_version: str
    contribution_id: str
    type: ContributionType
    depends_on: tuple[str, ...] = ()

    @property
    def owner_label(self) -> str:
        return f"{self.plugin_id}@{self.plugin_version}"


@dataclass(frozen=True, slots=True)
class ContributionRequest:
    """One staged contribution awaiting owner application."""

    owner: ContributionOwner
    payload: object


@dataclass(frozen=True, slots=True)
class ToolContribution:
    """One caller-facing tool, applied through ``runtime.tool_registry``."""

    name: str
    description: str
    toolset: str
    parameters: Mapping[str, Any] | None = None
    handler: Callable[..., Any] | None = None
    effect: str = "read"
    integration_action: str | None = None
    persona_scoped: bool = False
    dispatch_context_scoped: bool = False
    elevatable: bool = False
    dedicated_gate: bool = False


@dataclass(frozen=True, slots=True)
class ToolModuleContribution:
    """Compatibility payload: register an EXISTING tool module unchanged.

    The current ``runtime.tool_impl*`` modules keep their business logic exactly
    where it is. A bundled compatibility plugin publishes this payload carrying
    that module's own ``register_tools`` callable; the adapter runs it inside
    ``tool_registry.plugin_owner_scope``, so every row it produces inherits
    plugin provenance and an exact disposer set without a single line of tool
    logic moving.

    ``register`` is a CALLABLE, never a module path string: resolving an
    import target from plugin-supplied data would turn a data contribution into
    an arbitrary-import primitive.
    """

    module: str
    register: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class ToolsetContribution:
    """One toolset structure row, applied through ``runtime.toolsets``."""

    name: str
    description: str
    tools: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillContribution:
    """One framework skill row, applied through ``runtime.framework_registry``."""

    name: str
    description: str
    path: str


@dataclass(frozen=True, slots=True)
class McpServerContribution:
    """One MCP server declaration. Config values are redacted by the owner."""

    name: str
    transport: str
    config: Mapping[str, Any] | None = None
    source: str = ""


@dataclass(frozen=True, slots=True)
class CommandContribution:
    """One chat command, applied through ``chat.extension_manager``."""

    name: str
    description: str
    command_type: str
    min_role: str
    handler: Callable[..., Any] | None = None
    handler_ref: str = ""
    category: str = ""


@dataclass(frozen=True, slots=True)
class IntentContribution:
    """One data-intent mapping, applied through ``chat.extension_manager``."""

    command: str
    keywords: tuple[str, ...]
    included_in_brief: bool = False


@dataclass(frozen=True, slots=True)
class PromptHookContribution:
    """One prompt-region contributor. Data-only registration; never invoked here."""

    name: str
    region: str
    render: Callable[..., Any]
    priority: int = 0


@dataclass(frozen=True, slots=True)
class ContextHookContribution:
    """One context-stage contributor. Data-only registration; never invoked here."""

    name: str
    stage: str
    provide: Callable[..., Any]
    priority: int = 0


@dataclass(frozen=True, slots=True)
class HealthProbeContribution:
    """One health probe. Registration is fail-closed; evaluation is fail-open."""

    name: str
    subject: str
    probe: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class ConfigRequirementContribution:
    """One declared configuration requirement.

    Carries the env variable NAME and never its value. Evaluation reports
    presence only, so neither the catalog nor a diagnostic can leak a secret
    that a plugin legitimately depends on.
    """

    key: str
    description: str
    env_var: str | None = None
    required: bool = True


# ---------------------------------------------------------------------------
# Payload validation helpers
# ---------------------------------------------------------------------------


def _require_payload(payload: object, expected: type | tuple[type, ...]) -> Any:
    expected_types = (expected,) if isinstance(expected, type) else expected
    if type(payload) not in expected_types:
        names = (
            expected.__name__
            if isinstance(expected, type)
            else "/".join(item.__name__ for item in expected)
        )
        raise ContributionError(
            "contribution_payload_type_mismatch",
            f"Contribution payload must be a {names}",
        )
    return payload


def _require_identifier(value: object, pattern: re.Pattern[str], code: str) -> str:
    if type(value) is not str:
        raise ContributionError(code, "Identifier must be an exact string")
    if not pattern.fullmatch(value):
        raise ContributionError(code, "Identifier does not match the required shape")
    return value


def _require_text(value: object, code: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ContributionError(code, "Text field must be an exact string")
    if not value and not allow_empty:
        raise ContributionError(code, "Text field must not be empty")
    if len(value) > MAX_TEXT_CHARS:
        raise ContributionError(code, "Text field exceeds the bounded length")
    # ``redact_detail`` normalizes whitespace, strips control characters, and
    # masks credential-shaped substrings. If it CHANGED anything beyond
    # whitespace normalization the field carried something that has no business
    # reaching a prompt, a catalog, or a receipt — refuse rather than store the
    # masked form, so the operator sees the real reason.
    if redact_detail(value) != " ".join(value.split()):
        raise ContributionError(code, "Text field carries control or secret-shaped content")
    return value


def _require_bool(value: object, code: str) -> bool:
    if type(value) is not bool:
        raise ContributionError(code, "Flag must be an exact boolean")
    return value


def _require_int(value: object, code: str, *, low: int, high: int) -> int:
    if type(value) is not int:
        raise ContributionError(code, "Value must be an exact integer")
    if not low <= value <= high:
        raise ContributionError(code, "Value is outside the bounded range")
    return value


def _require_sync_callable(value: object, code: str) -> Callable[..., Any]:
    if not callable(value):
        raise ContributionError(code, "Contribution requires a callable")
    if (
        inspect.iscoroutinefunction(value)
        or inspect.isgeneratorfunction(value)
        or inspect.isasyncgenfunction(value)
    ):
        raise ContributionError(code, "Contribution callables must be synchronous")
    call_method = getattr(type(value), "__call__", None)
    if call_method is not None and (
        inspect.iscoroutinefunction(call_method)
        or inspect.isgeneratorfunction(call_method)
        or inspect.isasyncgenfunction(call_method)
    ):
        raise ContributionError(code, "Contribution callables must be synchronous")
    return value


def _require_callable(value: object, code: str) -> Callable[..., Any]:
    """Require an execution callable without constraining its domain protocol."""

    if not callable(value):
        raise ContributionError(code, "Contribution requires a callable")
    return value


def _require_name_sequence(
    value: object,
    pattern: re.Pattern[str],
    code: str,
) -> tuple[str, ...]:
    if type(value) not in {tuple, list}:
        raise ContributionError(code, "Name collection must be a tuple or list")
    items = tuple(value)
    if len(items) > MAX_COLLECTION_ITEMS:
        raise ContributionError(code, "Name collection exceeds the bounded size")
    seen: set[str] = set()
    for item in items:
        name = _require_identifier(item, pattern, code)
        if name in seen:
            raise ContributionError(code, "Name collection contains duplicates")
        seen.add(name)
    return items


def _require_inert_data(value: object, code: str) -> Any:
    """Copy plugin-supplied structured data, refusing anything non-inert.

    Mirrors the kernel's own inert-data contract: exact built-in scalars,
    string-keyed mappings, and sequences only. A callable, a live object, or a
    secret-shaped string is refused rather than stored, because this data is
    rendered into schemas, prompts, and catalogs.
    """

    budget = [MAX_DATA_NODES]

    def _copy(node: object, depth: int) -> Any:
        if depth > MAX_DATA_DEPTH:
            raise ContributionError(code, "Structured data exceeds the safe depth")
        budget[0] -= 1
        if budget[0] < 0:
            raise ContributionError(code, "Structured data exceeds the bounded node count")
        if node is None or type(node) in {bool, int, float}:
            return node
        if type(node) is str:
            return _require_text(node, code, allow_empty=True)
        if type(node) in {list, tuple}:
            return [_copy(item, depth + 1) for item in node]
        if type(node) in {dict, MappingProxyType}:
            mapping = dict(node)
            if not all(type(key) is str for key in mapping):
                raise ContributionError(code, "Structured data mappings need string keys")
            return {key: _copy(item, depth + 1) for key, item in sorted(mapping.items())}
        raise ContributionError(code, "Structured data must be closed built-in values")

    return _copy(value, 0)


# ---------------------------------------------------------------------------
# Injected owner services
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContributionServices:
    """The owner services an adapter may reach.

    Injected rather than imported at adapter call sites so a missing owner is a
    typed ``dependency_service_unavailable`` BEFORE any mutation, and so a test
    can supply a fake owner without monkey-patching module globals.
    """

    services: Mapping[str, object]

    def available(self, name: str) -> bool:
        return self.services.get(name) is not None

    def optional(self, name: str) -> object | None:
        return self.services.get(name)

    def require(self, name: str) -> Any:
        service = self.services.get(name)
        if service is None:
            raise ContributionError(
                "dependency_service_unavailable",
                f"Owner service {name} is unavailable; refusing a partial load",
            )
        return service


def default_services(
    *,
    project_root: object | None = None,
    overrides: Mapping[str, object] | None = None,
) -> ContributionServices:
    """Resolve the real owner modules, tolerating an unavailable one.

    Rule 1: both arguments are ``None`` sentinels resolved here, never bound as
    defaults. Rule 3: owners are handed over as MODULE objects, so every adapter
    call is a late attribute lookup and a monkey-patched owner is honored.

    An owner that cannot be imported is recorded as absent rather than raising,
    which is what turns a broken optional owner into "this one plugin refuses to
    load" instead of "the kernel cannot start".
    """

    resolved: dict[str, object] = {}
    for name, module_path in (
        ("tool_registry", "runtime.tool_registry"),
        ("toolset_registry", "runtime.toolsets"),
        ("framework_registry", "runtime.framework_registry"),
        ("contribution_registry", __name__),
    ):
        try:
            import importlib

            resolved[name] = importlib.import_module(module_path)
        except Exception:  # an optional owner must not break the kernel
            _logger.warning("capability contribution owner %s unavailable", name)
    try:
        import importlib

        extension_manager = importlib.import_module("extension_manager")
        resolved["extension_manager"] = extension_manager.get_manager()
    except Exception:  # chat slice is optional in runtime-only processes
        _logger.warning("capability contribution owner extension_manager unavailable")

    if project_root is not None:
        resolved["project_root"] = project_root
    if overrides:
        for key, value in overrides.items():
            if value is None:
                resolved.pop(key, None)
            else:
                resolved[key] = value
    return ContributionServices(services=MappingProxyType(dict(resolved)))


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnerApplication:
    """What an adapter hands back after one successful owner mutation."""

    owner_key: str
    dispose: Callable[[], bool]


class ContributionAdapter:
    """Base adapter: validate a payload, apply it, describe it, dispose it.

    Subclasses implement ``_validate``, ``_apply``, and ``_catalog``. The base
    class owns the parts that must not vary: payload type enforcement and the
    declared service dependencies.
    """

    contribution_type: ContributionType
    owner: str
    payload_types: tuple[type, ...] = ()
    required_services: tuple[str, ...] = ()

    def validate(self, payload: object) -> None:
        self._validate(_require_payload(payload, self.payload_types))

    def apply(
        self,
        payload: object,
        owner: ContributionOwner,
        services: ContributionServices,
    ) -> OwnerApplication:
        return self._apply(_require_payload(payload, self.payload_types), owner, services)

    def catalog_record(
        self,
        payload: object,
        owner: ContributionOwner,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "contribution_id": owner.contribution_id,
            "type": owner.type.value,
            "owner": self.owner,
            "plugin_id": owner.plugin_id,
            "plugin_version": owner.plugin_version,
            "depends_on": list(owner.depends_on),
        }
        record.update(self._catalog(_require_payload(payload, self.payload_types)))
        return record

    # -- subclass hooks --------------------------------------------------

    def _validate(self, payload: Any) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _apply(
        self,
        payload: Any,
        owner: ContributionOwner,
        services: ContributionServices,
    ) -> OwnerApplication:  # pragma: no cover - abstract
        raise NotImplementedError

    def _catalog(self, payload: Any) -> dict[str, object]:  # pragma: no cover
        raise NotImplementedError


class ToolAdapter(ContributionAdapter):
    """Applies TOOL contributions through the tool registry's own validation."""

    contribution_type = ContributionType.TOOL
    owner = "runtime.tool_registry"
    payload_types = (ToolContribution, ToolModuleContribution)
    required_services = ("tool_registry",)

    def _validate(self, payload: Any) -> None:
        if isinstance(payload, ToolModuleContribution):
            _require_identifier(payload.module, _HOOK_NAME_RE, "invalid_tool_module_contribution")
            _require_sync_callable(payload.register, "invalid_tool_module_contribution")
            return
        _require_identifier(payload.name, _TOOL_NAME_RE, "invalid_tool_contribution")
        _require_text(payload.description, "invalid_tool_contribution")
        _require_identifier(payload.toolset, _TOOLSET_NAME_RE, "invalid_tool_contribution")
        _require_text(payload.effect, "invalid_tool_contribution")
        if payload.parameters is not None:
            _require_inert_data(payload.parameters, "invalid_tool_contribution")
        if payload.handler is not None:
            _require_sync_callable(payload.handler, "invalid_tool_contribution")
        if payload.integration_action is not None:
            _require_text(payload.integration_action, "invalid_tool_contribution")
        for flag in (
            payload.persona_scoped,
            payload.dispatch_context_scoped,
            payload.elevatable,
            payload.dedicated_gate,
        ):
            _require_bool(flag, "invalid_tool_contribution")

    def _apply(
        self,
        payload: Any,
        owner: ContributionOwner,
        services: ContributionServices,
    ) -> OwnerApplication:
        registry = services.require("tool_registry")
        if isinstance(payload, ToolModuleContribution):
            return self._apply_module(payload, owner, registry)

        with registry.plugin_owner_scope(owner.plugin_id, owner.plugin_version):
            registry.register_tool(
                payload.name,
                payload.description,
                toolset=payload.toolset,
                parameters=(dict(payload.parameters) if payload.parameters is not None else None),
                handler=payload.handler,
                effect=payload.effect,
                integration_action=payload.integration_action,
                persona_scoped=payload.persona_scoped,
                dispatch_context_scoped=payload.dispatch_context_scoped,
                elevatable=payload.elevatable,
                dedicated_gate=payload.dedicated_gate,
            )

        def dispose() -> bool:
            return _dispose_tool_names(
                registry, (payload.name,), owner.plugin_id, owner.plugin_version
            )

        return OwnerApplication(owner_key=payload.name, dispose=dispose)

    def _apply_module(
        self,
        payload: ToolModuleContribution,
        owner: ContributionOwner,
        registry: Any,
    ) -> OwnerApplication:
        with registry.plugin_owner_scope(owner.plugin_id, owner.plugin_version) as registered:
            payload.register()
            # Snapshot INSIDE the scope: the list is live and the scope closes
            # on exit, so a later read would describe a window that is gone.
            names = tuple(registered)

        def dispose() -> bool:
            return _dispose_tool_names(registry, names, owner.plugin_id, owner.plugin_version)

        return OwnerApplication(owner_key=payload.module, dispose=dispose)

    def _catalog(self, payload: Any) -> dict[str, object]:
        if isinstance(payload, ToolModuleContribution):
            return {"tool_module": payload.module}
        return {
            "tool": payload.name,
            "toolset": payload.toolset,
            "effect": payload.effect,
            "handler_wired": payload.handler is not None,
            "dedicated_gate": payload.dedicated_gate,
        }


def _dispose_tool_names(
    registry: Any,
    names: Sequence[str],
    plugin_id: str,
    plugin_version: str,
) -> bool:
    """Remove every owned tool name, proving physical absence at the end.

    Removal is attempted for ALL names even when one refuses, so a single bad
    row cannot strand the rest, and the return value is then derived from the
    registry itself rather than from whether the loop raised (Rule 2).
    """
    for name in names:
        try:
            registry.unregister_tool_for_owner(
                name, plugin_id=plugin_id, plugin_version=plugin_version
            )
        except Exception:
            _logger.warning(
                "capability plugin %s@%s could not unregister tool %s",
                plugin_id,
                plugin_version,
                name,
            )
    remaining = [
        entry.name
        for entry in registry.list_registered_for_owner(plugin_id, plugin_version)
        if entry.name in set(names)
    ]
    if remaining:
        _logger.warning(
            "capability plugin %s@%s left %d tool row(s) installed: %s",
            plugin_id,
            plugin_version,
            len(remaining),
            ", ".join(sorted(remaining)),
        )
        return False
    return True


class ToolsetAdapter(ContributionAdapter):
    """Applies TOOLSET contributions through the toolset structure registry."""

    contribution_type = ContributionType.TOOLSET
    owner = "runtime.toolsets"
    payload_types = (ToolsetContribution,)
    required_services = ("toolset_registry",)

    def _validate(self, payload: Any) -> None:
        _require_identifier(payload.name, _TOOLSET_NAME_RE, "invalid_toolset_contribution")
        _require_text(payload.description, "invalid_toolset_contribution")
        _require_name_sequence(payload.tools, _TOOL_NAME_RE, "invalid_toolset_contribution")
        includes = _require_name_sequence(
            payload.includes, _TOOLSET_NAME_RE, "invalid_toolset_contribution"
        )
        if payload.name in includes:
            raise ContributionError(
                "invalid_toolset_contribution",
                "A toolset may not include itself",
            )

    def _apply(
        self,
        payload: Any,
        owner: ContributionOwner,
        services: ContributionServices,
    ) -> OwnerApplication:
        registry = services.require("toolset_registry")
        registry.register_plugin_toolset(
            payload.name,
            {
                "description": payload.description,
                "tools": list(payload.tools),
                "includes": list(payload.includes),
            },
            plugin_id=owner.plugin_id,
            plugin_version=owner.plugin_version,
        )

        def dispose() -> bool:
            try:
                registry.unregister_plugin_toolset(
                    payload.name,
                    plugin_id=owner.plugin_id,
                    plugin_version=owner.plugin_version,
                )
            except Exception:
                _logger.warning(
                    "capability plugin %s could not unregister toolset %s",
                    owner.owner_label,
                    payload.name,
                )
            return registry.plugin_toolset_owner(payload.name) is None

        return OwnerApplication(owner_key=payload.name, dispose=dispose)

    def _catalog(self, payload: Any) -> dict[str, object]:
        return {
            "toolset": payload.name,
            "tools": list(payload.tools),
            "includes": list(payload.includes),
        }


class SkillAdapter(ContributionAdapter):
    """Applies SKILL contributions through framework discovery."""

    contribution_type = ContributionType.SKILL
    owner = "runtime.framework_registry"
    payload_types = (SkillContribution,)
    required_services = ("framework_registry",)

    def _validate(self, payload: Any) -> None:
        _require_identifier(payload.name, _SKILL_NAME_RE, "invalid_skill_contribution")
        _require_text(payload.description, "invalid_skill_contribution")
        path = _require_identifier(payload.path, _RELATIVE_PATH_RE, "invalid_skill_contribution")
        # Repo-relative only. An absolute path or a traversal would publish a
        # machine path into prompts and let a plugin point a runtime at a file
        # outside the framework tree.
        if path.startswith("/") or ".." in path.split("/"):
            raise ContributionError(
                "invalid_skill_contribution",
                "Skill path must be repo-relative without traversal",
            )

    def _apply(
        self,
        payload: Any,
        owner: ContributionOwner,
        services: ContributionServices,
    ) -> OwnerApplication:
        registry = services.require("framework_registry")
        registry.register_plugin_skill(
            registry.SkillEntry(
                name=payload.name,
                description=payload.description,
                path=payload.path,
            ),
            plugin_id=owner.plugin_id,
            plugin_version=owner.plugin_version,
            project_root=services.optional("project_root"),
        )

        def dispose() -> bool:
            try:
                registry.unregister_plugin_skill(
                    payload.name,
                    plugin_id=owner.plugin_id,
                    plugin_version=owner.plugin_version,
                )
            except Exception:
                _logger.warning(
                    "capability plugin %s could not unregister skill %s",
                    owner.owner_label,
                    payload.name,
                )
            return not any(
                kind == "skill"
                and name == payload.name
                and plugin_id == owner.plugin_id
                and plugin_version == owner.plugin_version
                for kind, name, plugin_id, plugin_version in registry.list_plugin_overlay_rows()
            )

        return OwnerApplication(owner_key=payload.name, dispose=dispose)

    def _catalog(self, payload: Any) -> dict[str, object]:
        return {"skill": payload.name, "path": payload.path}


class McpServerAdapter(ContributionAdapter):
    """Applies MCP / MCP_SERVER contributions through framework discovery."""

    owner = "runtime.framework_registry"
    payload_types = (McpServerContribution,)
    required_services = ("framework_registry",)

    def __init__(self, contribution_type: ContributionType) -> None:
        self.contribution_type = contribution_type

    def _validate(self, payload: Any) -> None:
        _require_identifier(payload.name, _MCP_NAME_RE, "invalid_mcp_contribution")
        _require_text(payload.transport, "invalid_mcp_contribution")
        _require_text(payload.source, "invalid_mcp_contribution", allow_empty=True)
        if payload.config is not None:
            _require_inert_data(payload.config, "invalid_mcp_contribution")

    def _apply(
        self,
        payload: Any,
        owner: ContributionOwner,
        services: ContributionServices,
    ) -> OwnerApplication:
        registry = services.require("framework_registry")
        registry.register_plugin_mcp_server(
            registry.McpServerEntry(
                name=payload.name,
                transport=payload.transport,
                config=dict(payload.config or {}),
                source=payload.source or owner.owner_label,
            ),
            plugin_id=owner.plugin_id,
            plugin_version=owner.plugin_version,
            project_root=services.optional("project_root"),
        )

        def dispose() -> bool:
            try:
                registry.unregister_plugin_mcp_server(
                    payload.name,
                    plugin_id=owner.plugin_id,
                    plugin_version=owner.plugin_version,
                )
            except Exception:
                _logger.warning(
                    "capability plugin %s could not unregister MCP server %s",
                    owner.owner_label,
                    payload.name,
                )
            return not any(
                kind == "mcp_server"
                and name == payload.name
                and plugin_id == owner.plugin_id
                and plugin_version == owner.plugin_version
                for kind, name, plugin_id, plugin_version in registry.list_plugin_overlay_rows()
            )

        return OwnerApplication(owner_key=payload.name, dispose=dispose)

    def _catalog(self, payload: Any) -> dict[str, object]:
        # Key NAMES only. An MCP config routinely carries a token in a value,
        # and this record is written into operator catalogs and receipts.
        return {
            "mcp_server": payload.name,
            "transport": payload.transport,
            "config_keys": sorted(str(key) for key in (payload.config or {})),
        }


class CommandAdapter(ContributionAdapter):
    """Applies COMMAND contributions through the chat extension manager."""

    contribution_type = ContributionType.COMMAND
    owner = "chat.extension_manager"
    payload_types = (CommandContribution,)
    required_services = ("extension_manager",)

    def _validate(self, payload: Any) -> None:
        _require_identifier(payload.name, _COMMAND_NAME_RE, "invalid_command_contribution")
        _require_text(payload.description, "invalid_command_contribution")
        _require_text(payload.category, "invalid_command_contribution", allow_empty=True)
        _require_text(payload.handler_ref, "invalid_command_contribution", allow_empty=True)
        if payload.command_type not in {"router", "engine"}:
            raise ContributionError(
                "invalid_command_contribution",
                "Command type must be router or engine",
            )
        if payload.handler is not None:
            # Router handlers are async in ExtensionManager; engine handlers
            # may be deferred elsewhere. Registration itself stays sync, while
            # the owning dispatcher retains execution-protocol authority.
            _require_callable(payload.handler, "invalid_command_contribution")

    def _apply(
        self,
        payload: Any,
        owner: ContributionOwner,
        services: ContributionServices,
    ) -> OwnerApplication:
        manager = services.require("extension_manager")
        extension_id = plugin_extension_id(owner)
        # Role vocabulary is the manager's, read at call time. Copying the
        # three role names here would let this module and the dispatcher drift.
        module = _extension_manager_module()
        if payload.min_role not in module.ROLE_LEVEL:
            raise ContributionError(
                "invalid_command_contribution",
                "Command min_role is not a known role",
            )
        spec = module.CommandSpec(
            name=payload.name,
            description=payload.description,
            type=payload.command_type,
            min_role=payload.min_role,
            handler=payload.handler,
            handler_ref=payload.handler_ref,
            extension_id=extension_id,
            category=payload.category,
        )
        manager.register_command(spec)

        def dispose() -> bool:
            try:
                manager.unregister_command(payload.name, extension_id=extension_id)
            except Exception:
                _logger.warning(
                    "capability plugin %s could not unregister /%s",
                    owner.owner_label,
                    payload.name,
                )
            return not any(
                registered.name == payload.name
                for registered in manager.list_commands_for_extension(extension_id)
            )

        return OwnerApplication(owner_key=payload.name, dispose=dispose)

    def _catalog(self, payload: Any) -> dict[str, object]:
        return {
            "command": payload.name,
            "command_type": payload.command_type,
            "min_role": payload.min_role,
            "handler_wired": payload.handler is not None,
        }


class IntentAdapter(ContributionAdapter):
    """Applies INTENT contributions through the chat extension manager."""

    contribution_type = ContributionType.INTENT
    owner = "chat.extension_manager"
    payload_types = (IntentContribution,)
    required_services = ("extension_manager",)

    def _validate(self, payload: Any) -> None:
        _require_identifier(payload.command, _COMMAND_NAME_RE, "invalid_intent_contribution")
        if type(payload.keywords) not in {tuple, list} or not payload.keywords:
            raise ContributionError(
                "invalid_intent_contribution",
                "Intent requires at least one keyword",
            )
        if len(payload.keywords) > MAX_COLLECTION_ITEMS:
            raise ContributionError(
                "invalid_intent_contribution",
                "Intent keyword collection exceeds the bounded size",
            )
        for keyword in payload.keywords:
            _require_text(keyword, "invalid_intent_contribution")
        _require_bool(payload.included_in_brief, "invalid_intent_contribution")

    def _apply(
        self,
        payload: Any,
        owner: ContributionOwner,
        services: ContributionServices,
    ) -> OwnerApplication:
        manager = services.require("extension_manager")
        extension_id = plugin_extension_id(owner)
        module = _extension_manager_module()
        # An intent whose target command is absent is a dead route: the router
        # would match keywords and dispatch into nothing. The extension loader
        # already refuses that case; a typed contribution gets the same rule.
        if payload.command not in set(manager.get_all_command_names()):
            raise ContributionError(
                "intent_target_command_missing",
                "Intent target command is not registered",
            )
        spec = module.IntentSpec(
            command=payload.command,
            keywords=list(payload.keywords),
            included_in_brief=payload.included_in_brief,
            extension_id=extension_id,
        )
        manager.register_intent(spec)

        def dispose() -> bool:
            try:
                manager.unregister_intent(spec, extension_id=extension_id)
            except Exception:
                _logger.warning(
                    "capability plugin %s could not unregister intent for /%s",
                    owner.owner_label,
                    payload.command,
                )
            return not any(
                registered is spec
                for registered in manager.list_intents_for_extension(extension_id)
            )

        return OwnerApplication(
            owner_key=f"{payload.command}:{owner.contribution_id}", dispose=dispose
        )

    def _catalog(self, payload: Any) -> dict[str, object]:
        return {
            "intent_command": payload.command,
            "keyword_count": len(payload.keywords),
            "included_in_brief": payload.included_in_brief,
        }


class _LocalRegistryAdapter(ContributionAdapter):
    """Shared apply/dispose for the four registries this module itself owns."""

    kind: str
    required_services = ("contribution_registry",)

    def _key(self, payload: Any) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def _apply(
        self,
        payload: Any,
        owner: ContributionOwner,
        services: ContributionServices,
    ) -> OwnerApplication:
        registry = services.require("contribution_registry")
        key = self._key(payload)
        registry.register_local_row(self.kind, key, payload, owner)

        def dispose() -> bool:
            try:
                registry.unregister_local_row(
                    self.kind,
                    key,
                    plugin_id=owner.plugin_id,
                    plugin_version=owner.plugin_version,
                )
            except Exception:
                _logger.warning(
                    "capability plugin %s could not unregister %s %s",
                    owner.owner_label,
                    self.kind,
                    key,
                )
            return registry.local_row_owner(self.kind, key) is None

        return OwnerApplication(owner_key=key, dispose=dispose)


class PromptHookAdapter(_LocalRegistryAdapter):
    contribution_type = ContributionType.PROMPT_HOOK
    owner = "runtime.capability_contributions"
    payload_types = (PromptHookContribution,)
    kind = "prompt_hook"

    def _key(self, payload: Any) -> str:
        return payload.name

    def _validate(self, payload: Any) -> None:
        _require_identifier(payload.name, _HOOK_NAME_RE, "invalid_prompt_hook_contribution")
        _require_identifier(payload.region, _HOOK_NAME_RE, "invalid_prompt_hook_contribution")
        _require_sync_callable(payload.render, "invalid_prompt_hook_contribution")
        _require_int(payload.priority, "invalid_prompt_hook_contribution", low=-999, high=999)

    def _catalog(self, payload: Any) -> dict[str, object]:
        return {
            "prompt_hook": payload.name,
            "region": payload.region,
            "priority": payload.priority,
        }


class ContextHookAdapter(_LocalRegistryAdapter):
    contribution_type = ContributionType.CONTEXT_HOOK
    owner = "runtime.capability_contributions"
    payload_types = (ContextHookContribution,)
    kind = "context_hook"

    def _key(self, payload: Any) -> str:
        return payload.name

    def _validate(self, payload: Any) -> None:
        _require_identifier(payload.name, _HOOK_NAME_RE, "invalid_context_hook_contribution")
        _require_identifier(payload.stage, _HOOK_NAME_RE, "invalid_context_hook_contribution")
        _require_sync_callable(payload.provide, "invalid_context_hook_contribution")
        _require_int(payload.priority, "invalid_context_hook_contribution", low=-999, high=999)

    def _catalog(self, payload: Any) -> dict[str, object]:
        return {
            "context_hook": payload.name,
            "stage": payload.stage,
            "priority": payload.priority,
        }


class HealthProbeAdapter(_LocalRegistryAdapter):
    contribution_type = ContributionType.HEALTH_PROBE
    owner = "runtime.capability_contributions"
    payload_types = (HealthProbeContribution,)
    kind = "health_probe"

    def _key(self, payload: Any) -> str:
        return payload.name

    def _validate(self, payload: Any) -> None:
        _require_identifier(payload.name, _HOOK_NAME_RE, "invalid_health_probe_contribution")
        _require_text(payload.subject, "invalid_health_probe_contribution")
        _require_sync_callable(payload.probe, "invalid_health_probe_contribution")

    def _catalog(self, payload: Any) -> dict[str, object]:
        return {"health_probe": payload.name, "subject": payload.subject}


class ConfigRequirementAdapter(_LocalRegistryAdapter):
    contribution_type = ContributionType.CONFIG_REQUIREMENT
    owner = "runtime.capability_contributions"
    payload_types = (ConfigRequirementContribution,)
    kind = "config_requirement"

    def _key(self, payload: Any) -> str:
        return payload.key

    def _validate(self, payload: Any) -> None:
        _require_identifier(payload.key, _HOOK_NAME_RE, "invalid_config_requirement")
        _require_text(payload.description, "invalid_config_requirement")
        _require_bool(payload.required, "invalid_config_requirement")
        if payload.env_var is not None:
            _require_identifier(payload.env_var, _ENV_NAME_RE, "invalid_config_requirement")

    def _catalog(self, payload: Any) -> dict[str, object]:
        # The env NAME is metadata; the value never leaves the process.
        return {
            "config_requirement": payload.key,
            "env_var": payload.env_var,
            "required": payload.required,
        }


def _extension_manager_module() -> Any:
    """Late module lookup so a patched ``extension_manager`` is honored (Rule 3)."""
    import extension_manager

    return extension_manager


def plugin_extension_id(owner: ContributionOwner) -> str:
    """Namespaced owner token used as the extension manager's ``extension_id``.

    Version is part of the token, so a reload under a new version cannot
    dispose the previous version's rows, and a legacy ``extension.json`` package
    can never collide with a capability plugin's token.
    """
    return f"capability-plugin:{owner.plugin_id}@{owner.plugin_version}"


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


class ContributionAdapterRegistry:
    """Explicit ``ContributionType`` -> adapter map with no implicit fallback."""

    def __init__(self, adapters: Iterable[ContributionAdapter] | None = None) -> None:
        self._adapters: dict[ContributionType, ContributionAdapter] = {}
        for adapter in adapters or ():
            self.register(adapter)

    def register(self, adapter: ContributionAdapter) -> None:
        contribution_type = adapter.contribution_type
        if contribution_type is ContributionType.GENERIC:
            raise ContributionError(
                "generic_adapter_not_allowed",
                "GENERIC contributions keep their lease-only lifecycle",
            )
        if contribution_type in self._adapters:
            raise ContributionError(
                "duplicate_adapter",
                f"An adapter for {contribution_type.value} is already registered",
            )
        self._adapters[contribution_type] = adapter

    def resolve(self, contribution_type: ContributionType) -> ContributionAdapter:
        adapter = self._adapters.get(contribution_type)
        if adapter is None:
            raise ContributionError(
                "adapter_not_registered",
                f"No owner adapter is registered for {contribution_type.value}",
            )
        return adapter

    def types(self) -> frozenset[ContributionType]:
        return frozenset(self._adapters)


def default_adapter_registry() -> ContributionAdapterRegistry:
    """Every adapter this slice ships. Built fresh per call (Rule 1/Rule 2)."""
    return ContributionAdapterRegistry(
        (
            ToolAdapter(),
            ToolsetAdapter(),
            SkillAdapter(),
            McpServerAdapter(ContributionType.MCP),
            McpServerAdapter(ContributionType.MCP_SERVER),
            CommandAdapter(),
            IntentAdapter(),
            PromptHookAdapter(),
            ContextHookAdapter(),
            HealthProbeAdapter(),
            ConfigRequirementAdapter(),
        )
    )


# ---------------------------------------------------------------------------
# Module-owned registries (prompt/context hooks, health probes, config needs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalContributionRow:
    """One row in a registry this module owns, with its plugin owner."""

    kind: str
    key: str
    payload: object = field(repr=False)
    owner: ContributionOwner


_LOCAL_KINDS = ("prompt_hook", "context_hook", "health_probe", "config_requirement")
_LOCAL_LOCK = threading.RLock()
_LOCAL_ROWS: dict[str, dict[str, LocalContributionRow]] = {kind: {} for kind in _LOCAL_KINDS}
_LOCAL_GENERATION: int = 0


class LocalRegistryError(ContributionError):
    """Raised on an ownership or shape violation in a module-owned registry."""


def get_local_generation() -> int:
    """Generation counter for the module-owned registries.

    Derived catalogs read this to prove an unload is observable rather than
    trusting a cached inventory.
    """
    return _LOCAL_GENERATION


def register_local_row(
    kind: str,
    key: str,
    payload: object,
    owner: ContributionOwner,
) -> None:
    """Install one row. Registration is fail-CLOSED: collisions refuse."""
    global _LOCAL_GENERATION

    if kind not in _LOCAL_ROWS:
        raise LocalRegistryError("unknown_local_registry", f"Unknown registry {kind}")
    with _LOCAL_LOCK:
        existing = _LOCAL_ROWS[kind].get(key)
        if existing is not None:
            raise LocalRegistryError(
                "local_row_conflict",
                f"{kind} {key} is already owned by {existing.owner.owner_label}",
            )
        _LOCAL_ROWS[kind][key] = LocalContributionRow(
            kind=kind, key=key, payload=payload, owner=owner
        )
        _LOCAL_GENERATION += 1


def unregister_local_row(
    kind: str,
    key: str,
    *,
    plugin_id: str,
    plugin_version: str,
) -> bool:
    """Compare-and-remove one owned row. True if it was removed."""
    global _LOCAL_GENERATION

    if kind not in _LOCAL_ROWS:
        raise LocalRegistryError("unknown_local_registry", f"Unknown registry {kind}")
    with _LOCAL_LOCK:
        existing = _LOCAL_ROWS[kind].get(key)
        if existing is None:
            return False
        if existing.owner.plugin_id != plugin_id or existing.owner.plugin_version != plugin_version:
            raise LocalRegistryError(
                "local_row_owner_mismatch",
                f"{kind} {key} is owned by {existing.owner.owner_label}",
            )
        del _LOCAL_ROWS[kind][key]
        _LOCAL_GENERATION += 1
        return True


def local_row_owner(kind: str, key: str) -> ContributionOwner | None:
    """Owner of one installed row, or None. Reads physical state."""
    if kind not in _LOCAL_ROWS:
        raise LocalRegistryError("unknown_local_registry", f"Unknown registry {kind}")
    with _LOCAL_LOCK:
        existing = _LOCAL_ROWS[kind].get(key)
    return existing.owner if existing is not None else None


def list_local_rows(kind: str) -> tuple[LocalContributionRow, ...]:
    """Every installed row of one kind, key-sorted."""
    if kind not in _LOCAL_ROWS:
        raise LocalRegistryError("unknown_local_registry", f"Unknown registry {kind}")
    with _LOCAL_LOCK:
        return tuple(row for _key, row in sorted(_LOCAL_ROWS[kind].items()))


def list_prompt_hooks() -> tuple[Mapping[str, object], ...]:
    """Prompt-hook metadata, region- then priority-ordered. No callables.

    Serialization NEVER invokes a hook: rendering is the executor's job, and a
    catalog read that could run plugin code would turn a diagnostics command
    into an execution surface.
    """
    return _hook_metadata("prompt_hook", "region")


def list_context_hooks() -> tuple[Mapping[str, object], ...]:
    """Context-hook metadata, stage- then priority-ordered. No callables."""
    return _hook_metadata("context_hook", "stage")


def _hook_metadata(kind: str, group_field: str) -> tuple[Mapping[str, object], ...]:
    rows = list_local_rows(kind)
    ordered = sorted(
        rows,
        key=lambda row: (
            getattr(row.payload, group_field, ""),
            -getattr(row.payload, "priority", 0),
            row.key,
        ),
    )
    return tuple(
        MappingProxyType(
            {
                "name": row.key,
                group_field: getattr(row.payload, group_field, ""),
                "priority": getattr(row.payload, "priority", 0),
                "plugin_id": row.owner.plugin_id,
                "plugin_version": row.owner.plugin_version,
            }
        )
        for row in ordered
    )


def resolve_prompt_hook(name: str) -> Callable[..., Any] | None:
    """Return one prompt hook's render callable for an executor, or None."""
    with _LOCAL_LOCK:
        row = _LOCAL_ROWS["prompt_hook"].get(name)
    return getattr(row.payload, "render", None) if row is not None else None


def resolve_context_hook(name: str) -> Callable[..., Any] | None:
    """Return one context hook's provide callable for an executor, or None."""
    with _LOCAL_LOCK:
        row = _LOCAL_ROWS["context_hook"].get(name)
    return getattr(row.payload, "provide", None) if row is not None else None


@dataclass(frozen=True, slots=True)
class HealthProbeResult:
    name: str
    subject: str
    plugin_id: str
    plugin_version: str
    status: str
    detail: str = ""


def evaluate_health_probes() -> tuple[HealthProbeResult, ...]:
    """Run every registered probe. Fail-OPEN: a bad probe never raises out.

    Registration validation is fail-closed (a probe must be a synchronous
    callable), but EVALUATION is diagnostics: one plugin's broken probe must
    degrade to ``unknown`` for that row, not take down the health report.
    """
    results: list[HealthProbeResult] = []
    for row in list_local_rows("health_probe"):
        payload = row.payload
        status = "unknown"
        detail = ""
        try:
            outcome = getattr(payload, "probe")()
        except BaseException:  # a probe must never break the report
            detail = "probe raised"
            _logger.warning(
                "capability health probe %s (%s) raised",
                row.key,
                row.owner.owner_label,
            )
        else:
            if outcome is True:
                status = "ok"
            elif outcome is False:
                status = "degraded"
            else:
                detail = "probe did not return an exact boolean"
        results.append(
            HealthProbeResult(
                name=row.key,
                subject=getattr(payload, "subject", ""),
                plugin_id=row.owner.plugin_id,
                plugin_version=row.owner.plugin_version,
                status=status,
                detail=detail,
            )
        )
    return tuple(results)


@dataclass(frozen=True, slots=True)
class ConfigRequirementStatus:
    key: str
    env_var: str | None
    required: bool
    satisfied: bool
    plugin_id: str
    plugin_version: str


def evaluate_config_requirements(
    environ: Mapping[str, str] | None = None,
) -> tuple[ConfigRequirementStatus, ...]:
    """Report presence of each declared requirement. Never reports a VALUE.

    ``environ`` is a Rule 1 sentinel resolved to the live ``os.environ`` here,
    so a test can inject an environment and a live caller always reads physical
    process state rather than a snapshot taken at import.
    """
    source = os.environ if environ is None else environ
    statuses: list[ConfigRequirementStatus] = []
    for row in list_local_rows("config_requirement"):
        payload = row.payload
        env_var = getattr(payload, "env_var", None)
        satisfied = True if env_var is None else bool(source.get(env_var))
        statuses.append(
            ConfigRequirementStatus(
                key=row.key,
                env_var=env_var,
                required=bool(getattr(payload, "required", True)),
                satisfied=satisfied,
                plugin_id=row.owner.plugin_id,
                plugin_version=row.owner.plugin_version,
            )
        )
    return tuple(statuses)


def reset_local_registries() -> None:
    """Drop every module-owned row. Test isolation only."""
    global _LOCAL_GENERATION

    with _LOCAL_LOCK:
        for kind in _LOCAL_ROWS:
            _LOCAL_ROWS[kind].clear()
        _LOCAL_GENERATION += 1


# ---------------------------------------------------------------------------
# Apply / dispose transaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppliedContribution:
    """One owner row that is physically installed, plus how to remove it."""

    contribution_id: str
    type: ContributionType
    owner: str
    owner_key: str
    plugin_id: str
    plugin_version: str
    record: Mapping[str, object]
    dispose: Callable[[], bool] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ContributionDisposal:
    contribution_id: str
    owner: str
    owner_key: str
    succeeded: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ContributionDisposalBatch:
    receipts: tuple[ContributionDisposal, ...]
    failed_ids: tuple[str, ...]


def typed_requests(requests: Iterable[ContributionRequest]) -> tuple[ContributionRequest, ...]:
    """Filter out GENERIC contributions, which keep their #530 behavior."""
    return tuple(
        request for request in requests if request.owner.type is not ContributionType.GENERIC
    )


def validate_contributions(
    requests: Sequence[ContributionRequest],
    *,
    adapters: ContributionAdapterRegistry | None = None,
    services: ContributionServices | None = None,
) -> None:
    """Settle adapters, payload shapes, and service availability. Mutates nothing.

    Every failure mode that can be known in advance is raised HERE, before the
    first owner row exists, which is what makes "hold or fail one plugin without
    partially loading it" achievable rather than aspirational.
    """
    registry = default_adapter_registry() if adapters is None else adapters
    for request in requests:
        adapter = registry.resolve(request.owner.type)
        adapter.validate(request.payload)
    if services is None:
        return
    for request in requests:
        adapter = registry.resolve(request.owner.type)
        for service_name in adapter.required_services:
            if not services.available(service_name):
                raise ContributionError(
                    "dependency_service_unavailable",
                    f"Owner service {service_name} is unavailable; refusing a partial load",
                )


def apply_contributions(
    requests: Sequence[ContributionRequest],
    *,
    adapters: ContributionAdapterRegistry | None = None,
    services: ContributionServices | None = None,
) -> tuple[AppliedContribution, ...]:
    """Apply typed contributions in the caller's dependency order.

    ``requests`` must already be topologically ordered by the manifest — the
    manifest owns dependency order and re-deriving it here would create a second
    truth. On any failure the rows applied so far are disposed in REVERSE order
    and :class:`ContributionApplyError` is raised carrying the rollback receipts
    plus any row whose removal could not be proven.
    """
    registry = default_adapter_registry() if adapters is None else adapters
    bundle = default_services() if services is None else services

    validate_contributions(requests, adapters=registry, services=bundle)

    applied: list[AppliedContribution] = []
    for request in requests:
        owner = request.owner
        adapter = registry.resolve(owner.type)
        try:
            record = adapter.catalog_record(request.payload, owner)
            application = adapter.apply(request.payload, owner, bundle)
        except KeyboardInterrupt:
            batch = dispose_contributions(tuple(applied))
            residual = tuple(
                item for item in applied if item.contribution_id in set(batch.failed_ids)
            )
            raise ContributionApplyError(
                "operator_interrupted",
                f"Contribution {owner.contribution_id} was interrupted during apply",
                rollback=batch.receipts,
                residual_ids=batch.failed_ids,
                residual=residual,
            ) from None
        except BaseException as exc:  # owner refusal or plugin-controlled raise
            batch = dispose_contributions(tuple(applied))
            residual = tuple(
                item for item in applied if item.contribution_id in set(batch.failed_ids)
            )
            code = (
                exc.code
                if type(exc) in {ContributionError, ContributionApplyError}
                else f"{owner.type.value}_owner_refused"
            )
            detail = (
                exc.detail
                if type(exc) in {ContributionError, ContributionApplyError}
                else "Owning registry refused the contribution"
            )
            raise ContributionApplyError(
                code,
                f"Contribution {owner.contribution_id} failed to apply: {detail}",
                rollback=batch.receipts,
                residual_ids=batch.failed_ids,
                residual=residual,
            ) from None
        applied.append(
            AppliedContribution(
                contribution_id=owner.contribution_id,
                type=owner.type,
                owner=adapter.owner,
                owner_key=application.owner_key,
                plugin_id=owner.plugin_id,
                plugin_version=owner.plugin_version,
                record=MappingProxyType(dict(record)),
                dispose=application.dispose,
            )
        )
    return tuple(applied)


def dispose_contributions(
    applied: Sequence[AppliedContribution],
) -> ContributionDisposalBatch:
    """Remove owner rows in reverse application order.

    Never raises: a disposer that fails is recorded as unproven so the caller
    can degrade to restart-required with honest residue, which is strictly more
    useful than an exception that loses the receipts for the rows that DID come
    out cleanly.
    """
    receipts: list[ContributionDisposal] = []
    failed_ids: list[str] = []
    for application in reversed(tuple(applied)):
        detail = ""
        try:
            proven = application.dispose() is True
        except BaseException:  # a disposer must not escape the batch
            proven = False
            detail = "disposer raised"
            _logger.warning(
                "capability contribution %s (%s@%s) disposer raised",
                application.contribution_id,
                application.plugin_id,
                application.plugin_version,
            )
        if not proven and not detail:
            detail = "disposer did not prove removal"
        if not proven:
            failed_ids.append(application.contribution_id)
        receipts.append(
            ContributionDisposal(
                contribution_id=application.contribution_id,
                owner=application.owner,
                owner_key=application.owner_key,
                succeeded=proven,
                detail=detail,
            )
        )
    return ContributionDisposalBatch(receipts=tuple(receipts), failed_ids=tuple(failed_ids))


def contribution_catalog(
    applied: Iterable[AppliedContribution],
) -> tuple[Mapping[str, object], ...]:
    """Redacted inventory of installed typed contributions.

    Every value here came from ``catalog_record``, which emits metadata only.
    No callable, env value, MCP config value, or absolute path is ever placed in
    a record, so the catalog is safe for receipts, diagnostics, and export.
    """
    return tuple(application.record for application in applied)


# ---------------------------------------------------------------------------
# Legacy extension.json compatibility
# ---------------------------------------------------------------------------


def legacy_extension_contributions(
    extension: Any,
    owner_plugin_id: str,
    owner_plugin_version: str,
) -> tuple[ContributionRequest, ...]:
    """Wrap a legacy ``extension.json`` package as typed contribution requests.

    Field-for-field: every ``CommandSpec``/``IntentSpec`` value the v1 loader
    produced is carried across unchanged, so dispatch, roles, help grouping,
    intent keywords, and brief membership behave exactly as they do today. The
    only thing that changes is WHO owns the row — a lifecycle-bound plugin
    instead of an untracked package — which is what makes unload able to remove
    it.

    Contribution ids are derived from the legacy names (``command.<name>`` /
    ``intent.<n>.<command>``) so a receipt reads back to the surface it created.
    """
    requests: list[ContributionRequest] = []
    for spec in getattr(extension, "commands", ()) or ():
        owner = ContributionOwner(
            plugin_id=owner_plugin_id,
            plugin_version=owner_plugin_version,
            contribution_id=f"command.{spec.name}",
            type=ContributionType.COMMAND,
        )
        requests.append(
            ContributionRequest(
                owner=owner,
                payload=CommandContribution(
                    name=spec.name,
                    description=spec.description,
                    command_type=spec.type,
                    min_role=spec.min_role,
                    handler=_legacy_command_handler(extension, spec),
                    handler_ref=spec.handler_ref,
                    category=spec.category,
                ),
            )
        )
    for index, intent in enumerate(getattr(extension, "intents", ()) or ()):
        owner = ContributionOwner(
            plugin_id=owner_plugin_id,
            plugin_version=owner_plugin_version,
            contribution_id=f"intent.{index}.{intent.command}",
            type=ContributionType.INTENT,
        )
        requests.append(
            ContributionRequest(
                owner=owner,
                payload=IntentContribution(
                    command=intent.command,
                    keywords=tuple(intent.keywords),
                    included_in_brief=intent.included_in_brief,
                ),
            )
        )
    return tuple(requests)


def _legacy_command_handler(extension: Any, spec: Any) -> Callable[..., Any] | None:
    """Preserve ExtensionManager's lazy handler loading for compatibility rows."""

    if spec.handler is not None or not spec.handler_ref:
        return spec.handler

    module = _extension_manager_module()
    loader = module.ExtensionManager()
    loader._extensions[extension.id] = extension

    async def dispatch(
        adapter: object,
        incoming: object,
        args: str,
        *,
        collect_only: bool = False,
    ) -> Any:
        handler = loader._load_handler(spec.handler_ref, extension.id)
        return await handler(
            adapter,
            incoming,
            args,
            collect_only=collect_only,
        )

    return dispatch


def bundled_tool_module_contribution(
    contribution_id: str,
    module: str,
    register: Callable[[], Any],
    *,
    plugin_id: str,
    plugin_version: str,
) -> ContributionRequest:
    """Build the compatibility request that adopts an existing tool module.

    The module's own ``register_tools()`` runs unchanged inside the owner scope;
    no business logic moves and no ``tool_impl`` module learns about plugins.
    """
    return ContributionRequest(
        owner=ContributionOwner(
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            contribution_id=contribution_id,
            type=ContributionType.TOOL,
        ),
        payload=ToolModuleContribution(module=module, register=register),
    )


__all__ = [
    "TYPED_CONTRIBUTION_TYPES",
    "AppliedContribution",
    "CommandContribution",
    "ConfigRequirementContribution",
    "ConfigRequirementStatus",
    "ContextHookContribution",
    "ContributionAdapter",
    "ContributionAdapterRegistry",
    "ContributionApplyError",
    "ContributionDisposal",
    "ContributionDisposalBatch",
    "ContributionError",
    "ContributionOwner",
    "ContributionRequest",
    "ContributionServices",
    "HealthProbeContribution",
    "HealthProbeResult",
    "IntentContribution",
    "LocalContributionRow",
    "LocalRegistryError",
    "McpServerContribution",
    "OwnerApplication",
    "PromptHookContribution",
    "SkillContribution",
    "ToolContribution",
    "ToolModuleContribution",
    "ToolsetContribution",
    "apply_contributions",
    "bundled_tool_module_contribution",
    "contribution_catalog",
    "default_adapter_registry",
    "default_services",
    "dispose_contributions",
    "evaluate_config_requirements",
    "evaluate_health_probes",
    "get_local_generation",
    "legacy_extension_contributions",
    "list_context_hooks",
    "list_local_rows",
    "list_prompt_hooks",
    "local_row_owner",
    "plugin_extension_id",
    "register_local_row",
    "reset_local_registries",
    "resolve_context_hook",
    "resolve_prompt_hook",
    "typed_requests",
    "unregister_local_row",
    "validate_contributions",
]
