"""Physical-state-derived capability catalog and persona projection.

This module is a read model only. Existing registries remain the validation,
assignment, policy, lifecycle, and execution owners.
"""

from __future__ import annotations

import base64
import dataclasses
import os
import re
import shutil
import sys
from collections import defaultdict, deque
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
MAX_ITEMS = 10_000
MAX_PAGE_SIZE = 100
MAX_TEXT = 280
_SURFACES = ("cabinet", "direct_chat", "discord")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PRIVATE_PATH_RE = re.compile(
    r"(?i)(?:(?<![A-Za-z])[A-Z]:[\\/][^,;\"']*"
    r"|\\\\[^\\/\s]+[\\/][^,;\"']*"
    r"|(?<![A-Za-z0-9:/])/(?!/)[^,;\"']*)"
)
_PRIVATE_ID_RE = re.compile(
    r"(?<![A-Z0-9])(?:-100\d{8,18}|\d{15,22}|[BCDGUVW][A-Z0-9]{8,})(?![A-Z0-9])"
)
_URI_USERINFO_RE = re.compile(r"(?i)\b(https?://)[^/@\s]+@")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?(?:key|token)|access[_-]?token|token|secret|password|passwd|"
    r"credential|client[_-]?secret)\s*[:=]\s*[^\s,;]+"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+|\bbearer\s+[^\s,;]+"
)
_REQUIREMENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_REASON_ORDER = {
    "unavailable_owner": 10,
    "plugin_disabled": 20,
    "plugin_degraded": 30,
    "explicit_disabled": 40,
    "unassigned": 50,
    "missing_configuration": 60,
    "missing_handler": 70,
    "dependency_conflict": 80,
    "noncarrying_route": 90,
}


class CapabilityCatalogError(ValueError):
    """Invalid catalog input or contract."""


class CapabilityCatalogCollisionError(CapabilityCatalogError):
    """Two physical owners claimed one stable id."""


class CapabilityCatalogQueryError(CapabilityCatalogError):
    """A bounded query or cursor is invalid."""


class CapabilityKind(StrEnum):
    TOOL = "tool"
    TOOLSET = "toolset"
    SKILL = "skill"
    MCP = "mcp"
    INTEGRATION = "integration"
    PLUGIN = "plugin"


_CONTRIBUTION_KINDS = {
    "tool": CapabilityKind.TOOL,
    "toolset": CapabilityKind.TOOLSET,
    "skill": CapabilityKind.SKILL,
    "mcp": CapabilityKind.MCP,
    "mcp_server": CapabilityKind.MCP,
}


@dataclass(frozen=True, slots=True)
class BlockedReason:
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclass(frozen=True, slots=True)
class CatalogSourceError:
    source: str
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    id: str
    kind: CapabilityKind
    name: str
    display_name: str
    owner: str
    source: str
    version: str
    description: str
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    configuration_requirements: tuple[str, ...] = field(default_factory=tuple)
    effect_class: str = "read"
    supported_surfaces: tuple[str, ...] = field(default_factory=tuple)
    # Physical facts used only by the projection. They are deliberately absent
    # from ``as_dict`` so no raw transport/config representation can leak.
    physical_configured: bool = True
    physical_callable: bool = True
    # Dependency edges declared between persona-scoped plugin contributions.
    # Unlike physical owner/runtime edges, these require both endpoints to be
    # assigned to the projected persona. This stays internal to the read model.
    persona_dependencies: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "display_name": self.display_name,
            "owner": self.owner,
            "source": self.source,
            "version": self.version,
            "description": self.description,
            "dependencies": list(self.dependencies),
            "configuration_requirements": list(self.configuration_requirements),
            "effect_class": self.effect_class,
            "supported_surfaces": list(self.supported_surfaces),
        }


@dataclass(frozen=True, slots=True)
class PersonaCapabilityState:
    descriptor: CapabilityDescriptor
    available: bool
    assigned: bool
    configured: bool
    callable: bool
    enabled: bool
    readiness_axes: tuple[tuple[str, str], ...]
    surface_states: tuple[tuple[str, str], ...]
    blocked_reasons: tuple[BlockedReason, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.descriptor.as_dict(),
            "available": self.available,
            "assigned": self.assigned,
            "configured": self.configured,
            "callable": self.callable,
            "enabled": self.enabled,
            "readiness_axes": dict(self.readiness_axes),
            "surface_states": dict(self.surface_states),
            "blocked_reasons": [reason.as_dict() for reason in self.blocked_reasons],
        }


@dataclass(frozen=True, slots=True)
class CapabilityCatalogSnapshot:
    status: str
    items: tuple[CapabilityDescriptor, ...]
    generations: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    errors: tuple[CatalogSourceError, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "generations": dict(self.generations),
            "total_count": len(self.items),
            "items": [item.as_dict() for item in self.items],
            "errors": [error.as_dict() for error in self.errors],
        }


@dataclass(frozen=True, slots=True)
class PersonaCapabilityProjection:
    persona_id: str
    status: str
    states: tuple[PersonaCapabilityState, ...]
    catalog_errors: tuple[CatalogSourceError, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "persona_id": self.persona_id,
            "status": self.status,
            "total_count": len(self.states),
            "enabled_count": sum(state.enabled for state in self.states),
            "states": [state.as_dict() for state in self.states],
            "catalog_errors": [error.as_dict() for error in self.catalog_errors],
        }


@dataclass(frozen=True, slots=True)
class CapabilityCatalogQuery:
    search: str = ""
    kinds: tuple[CapabilityKind, ...] = field(default_factory=tuple)
    sources: tuple[str, ...] = field(default_factory=tuple)
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityCatalogPage:
    items: tuple[CapabilityDescriptor, ...]
    next_cursor: str | None
    matched_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.as_dict() for item in self.items],
            "next_cursor": self.next_cursor,
            "matched_count": self.matched_count,
        }


def collect_tool_descriptors(
    entries: Iterable[Any] | None = None,
) -> tuple[CapabilityDescriptor, ...]:
    """Read real tool entries; cached ``runtime.capabilities`` rows are ignored."""
    from runtime import tool_registry

    if entries is None:
        physical = tool_registry.list_registered()
    else:
        physical = tuple(entries)
    result: list[CapabilityDescriptor] = []
    for entry in physical:
        if not isinstance(entry, tool_registry.ToolEntry):
            raise CapabilityCatalogError("tool source requires ToolEntry rows")
        plugin_id = entry.plugin_id.strip()
        dependencies = [f"toolset.{entry.toolset}"]
        if entry.integration_action:
            dependencies.append(f"integration.{entry.integration_action.partition('.')[0]}")
        if plugin_id:
            dependencies.append(f"plugin.{plugin_id}")
        result.append(
            _descriptor(
                CapabilityKind.TOOL,
                entry.name,
                display_name=entry.name.replace("_", " ").title(),
                owner=f"plugin.{plugin_id}" if plugin_id else "runtime.tool_registry",
                source="runtime.tool_registry",
                version=entry.plugin_version or "builtin",
                description=entry.description,
                dependencies=dependencies,
                requirements=None,
                effect=entry.effect,
                surfaces=None,
            )
        )
    return _sorted(result)


def collect_toolset_descriptors(
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[CapabilityDescriptor, ...]:
    """Read structure and plugin ownership from the one physical toolset map."""
    from runtime import capabilities as runtime_capabilities
    from runtime import tool_registry, toolsets

    physical = toolsets.TOOLSETS if registry is None else registry
    result: list[CapabilityDescriptor] = []
    for name, spec in sorted(physical.items()):
        owner = toolsets.plugin_toolset_owner(name)
        dependencies = [f"toolset.{value}" for value in spec.get("includes", ())]
        dependencies.extend(f"tool.{value}" for value in spec.get("tools", ()))
        if owner:
            dependencies.append(f"plugin.{owner[0]}")
        resolved_tools = runtime_capabilities.resolve_toolset(name, registry=physical)
        dependencies.extend(_toolset_member_id(value) for value in resolved_tools)
        effects = [
            entry.effect
            for value in resolved_tools
            if (entry := tool_registry.get_entry(str(value))) is not None
        ]
        result.append(
            _descriptor(
                CapabilityKind.TOOLSET,
                name,
                display_name=name.replace("_", " ").title(),
                owner=f"plugin.{owner[0]}" if owner else "runtime.toolsets",
                source="runtime.toolsets",
                version=owner[1] if owner else "builtin",
                description=str(spec.get("description", "")),
                dependencies=dependencies,
                requirements=None,
                effect=_effect(
                    effects,
                    empty="unknown" if spec.get("live_source") else "read",
                ),
                surfaces=None,
                physical_callable=bool(
                    tool_registry.get_tool_definitions(
                        enabled_toolsets=[name],
                        registry=physical,
                    )
                ),
            )
        )
    return _sorted(result)


def collect_framework_descriptors(
    registry: Any | None = None,
    *,
    project_root: Any | None = None,
    mcp_config_path: Any | None = None,
) -> tuple[CapabilityDescriptor, ...]:
    """Read discovered skills and trusted MCP rows without exposing their paths."""
    from runtime import framework_registry

    physical = registry
    if physical is None:
        physical = framework_registry.discover_framework_registry(
            project_root,
            mcp_config_path=mcp_config_path,
        )
        # Runtime discovery is intentionally fenced because it feeds generic
        # lanes. This is the operator management catalog, which must inventory
        # persona-scoped promoted skills without granting them to anyone.
        management_skills = framework_registry.discover_skills(
            physical.project_root,
            fenced=False,
        )
        baseline_names = {entry.name for entry in management_skills}
        management_skills.extend(
            entry for entry in physical.skills if entry.name not in baseline_names
        )
        physical = dataclasses.replace(physical, skills=tuple(management_skills))
    if not isinstance(physical, framework_registry.FrameworkRegistry):
        raise CapabilityCatalogError("framework source requires FrameworkRegistry")
    owners = {
        (kind, name): (plugin_id, version)
        for kind, name, plugin_id, version in framework_registry.list_plugin_overlay_rows()
    }
    result: list[CapabilityDescriptor] = []
    for entry in physical.skills:
        owner = owners.get(("skill", entry.name))
        result.append(
            _descriptor(
                CapabilityKind.SKILL,
                entry.name,
                display_name=entry.name,
                owner=f"plugin.{owner[0]}" if owner else "runtime.framework_registry",
                source="runtime.framework_registry",
                version=owner[1] if owner else "builtin",
                description=entry.description,
                dependencies=[f"plugin.{owner[0]}"] if owner else None,
                requirements=None,
                effect="read",
                surfaces=None,
            )
        )
    for entry in physical.mcp_servers:
        owner = owners.get(("mcp_server", entry.name))
        configured = getattr(entry, "configured", None)
        callable_state = getattr(entry, "callable", None)
        if configured is None or callable_state is None:
            derived_configured, derived_callable = _mcp_physical_state(
                entry.transport,
                entry.config,
            )
            configured = derived_configured if configured is None else configured
            callable_state = derived_callable if callable_state is None else callable_state
        result.append(
            _descriptor(
                CapabilityKind.MCP,
                entry.name,
                display_name=entry.name,
                owner=f"plugin.{owner[0]}" if owner else "runtime.framework_registry",
                source="runtime.framework_registry",
                version=owner[1] if owner else "builtin",
                description=f"Trusted {entry.transport} MCP server",
                dependencies=[f"plugin.{owner[0]}"] if owner else None,
                requirements=_mcp_requirement_keys(entry.config),
                effect="external",
                surfaces=None,
                physical_configured=bool(configured),
                physical_callable=bool(callable_state),
            )
        )
    return _sorted(result)


def collect_integration_descriptors(
    integrations: Mapping[str, Any] | None = None,
) -> tuple[CapabilityDescriptor, ...]:
    """Read integration metadata and action effects; never read credential values."""
    from integrations import capabilities, registry

    physical = registry.get_all() if integrations is None else integrations
    result: list[CapabilityDescriptor] = []
    for name, info in sorted(physical.items()):
        if not isinstance(info, registry.IntegrationInfo):
            raise CapabilityCatalogError("integration source requires IntegrationInfo rows")
        actions = capabilities.get_integration_actions(name)
        requirements = list(info.required_config)
        for action in actions:
            requirements.extend(action.config_hints)
        exposures = {value for action in actions for value in action.exposures}
        surfaces = set(_SURFACES) if exposures & {"model", "operator_confirmed"} else set()
        if "internal" in exposures:
            surfaces.add("runtime")
        result.append(
            _descriptor(
                CapabilityKind.INTEGRATION,
                name,
                display_name=info.display_name,
                owner="integrations.registry",
                source=info.module_path or "integrations.registry",
                version="builtin",
                description=(
                    f"{len(actions)} declared actions; "
                    f"{sum(action.is_mutating for action in actions)} policy-gated mutations"
                ),
                dependencies=None,
                requirements=requirements,
                effect=_effect(action.effect for action in actions),
                surfaces=surfaces or None,
            )
        )
    return _sorted(result)


def collect_plugin_descriptors(
    plugin_views: Iterable[Any] | None = None,
) -> tuple[CapabilityDescriptor, ...]:
    """Read injected lifecycle views; discovery/load remains the kernel owner's job."""
    from runtime.capability_plugins import PluginInstanceView

    result: list[CapabilityDescriptor] = []
    for view in tuple(plugin_views or ()):
        if not isinstance(view, PluginInstanceView):
            raise CapabilityCatalogError("plugin source requires PluginInstanceView rows")
        result.append(
            _descriptor(
                CapabilityKind.PLUGIN,
                view.id,
                display_name=view.id.replace("-", " ").title(),
                owner="runtime.capability_plugins",
                source=str(getattr(view.source, "value", view.source)),
                version=view.version,
                description=f"Capability plugin with {len(view.contribution_ids)} contributions",
                dependencies=_plugin_dependencies(view),
                requirements=_plugin_requirements(view),
                effect=_effect(
                    (
                        str(row.get("effect"))
                        for row in view.contribution_inventory
                        if row.get("effect")
                    ),
                    empty="unknown",
                ),
                surfaces=("runtime",),
            )
        )
    return _sorted(result)


def collect_declared_plugin_contribution_descriptors(
    plugin_views: Iterable[Any],
) -> tuple[CapabilityDescriptor, ...]:
    """Keep unloaded typed manifest contributions visible without loading code.

    These rows come from the lifecycle owner's typed inventory. They are only
    used when the corresponding physical registry row is absent; once a plugin
    loads, its real registry descriptor wins.
    """
    result: list[CapabilityDescriptor] = []
    for view in tuple(plugin_views):
        contribution_ids = _plugin_contribution_ids(view)
        for row in view.contribution_inventory:
            kind = _CONTRIBUTION_KINDS.get(str(row.get("type") or ""))
            contribution_id = str(row.get("contribution_id") or "").strip()
            catalog_id = contribution_ids.get(contribution_id)
            if kind is None or catalog_id is None:
                continue
            _prefix, _separator, physical_name = catalog_id.partition(".")
            persona_dependencies = tuple(
                contribution_ids[dependency]
                for value in row.get("depends_on", ())
                if (dependency := str(value).strip()) in contribution_ids
            )
            dependencies = [f"plugin.{view.id}"]
            dependencies.extend(persona_dependencies)
            result.append(
                _descriptor(
                    kind,
                    physical_name,
                    display_name=physical_name.replace("_", " ").replace("-", " ").title(),
                    owner=f"plugin.{view.id}",
                    source="runtime.capability_plugins",
                    version=view.version,
                    description="Declared capability plugin contribution",
                    dependencies=dependencies,
                    requirements=_plugin_requirements(view),
                    effect=str(row.get("effect") or "unknown"),
                    surfaces=("runtime",),
                    physical_configured=True,
                    physical_callable=False,
                    persona_dependencies=persona_dependencies,
                )
            )
    return _sorted(result)


def collect_plugin_instance_views(
    project_root: Any | None = None,
) -> tuple[Any, ...]:
    """Discover trusted local plugin manifests without importing plugin code.

    The plugin kernel is not production-booted yet. Discovery is still physical
    availability, so disabled/unloaded candidates belong in the catalog even
    when no lifecycle kernel has been constructed.
    """
    from config import EXTENSIONS_EXTRA_PATH, PROJECT_ROOT
    from runtime.capability_plugin_manifest import (
        FilesystemPluginSource,
        ManifestSource,
        discover_capability_plugins,
    )
    from runtime.capability_plugins import (
        PluginDesiredState,
        PluginEffectiveState,
        PluginInstanceView,
        PluginLifecycleState,
    )

    root = Path(project_root or PROJECT_ROOT).resolve(strict=False)
    sources = [
        FilesystemPluginSource(ManifestSource.BUNDLED, root / ".claude" / "extensions")
    ]
    global_root = Path.home() / ".claude" / "extensions"
    if global_root.exists():
        sources.append(
            FilesystemPluginSource(ManifestSource.OPERATOR_GLOBAL, global_root)
        )
    extra = str(EXTENSIONS_EXTRA_PATH or "").strip()
    include_project = bool(extra)
    if extra:
        sources.append(FilesystemPluginSource(ManifestSource.PROJECT, Path(extra)))
    discovery = discover_capability_plugins(
        sources,
        include_project=include_project,
    )
    views = []
    for candidate in discovery.active_candidates:
        manifest = candidate.manifest
        inventory: list[Mapping[str, object]] = [
            {
                "contribution_id": contribution.id,
                "type": contribution.type.value,
                "depends_on": tuple(contribution.depends_on),
            }
            for contribution in manifest.contributions
        ]
        inventory.extend(
            {
                "contribution_id": f"requirement.env.{name}",
                "type": "config_requirement",
                "env_var": name,
                "required": True,
            }
            for name in manifest.requirements.env
        )
        inventory.extend(
            {
                "contribution_id": f"requirement.plugin.{name}",
                "type": "plugin_requirement",
                "plugin_id": name,
            }
            for name in manifest.requirements.plugins
        )
        enabled = manifest.enabled_by_default
        views.append(
            PluginInstanceView(
                id=manifest.id,
                version=manifest.version,
                source=manifest.source,
                desired_state=(
                    PluginDesiredState.ENABLED if enabled else PluginDesiredState.DISABLED
                ),
                effective_state=PluginEffectiveState.UNLOADED,
                lifecycle_state=(
                    PluginLifecycleState.ENABLED
                    if enabled
                    else PluginLifecycleState.DISCOVERED
                ),
                contribution_ids=manifest.contribution_ids,
                contribution_inventory=tuple(inventory),
                residual_contribution_ids=(),
                error_code="",
                detail="",
            )
        )
    return tuple(sorted(views, key=lambda view: view.id))


def _resolved_plugin_views(
    plugin_views: Iterable[Any] | None,
    project_root: Any | None,
) -> tuple[Any, ...]:
    discovered = {view.id: view for view in collect_plugin_instance_views(project_root)}
    for view in tuple(plugin_views or ()):
        discovered[view.id] = view
    return tuple(discovered[name] for name in sorted(discovered))


def build_capability_catalog(
    *,
    plugin_views: Iterable[Any] | None = None,
    project_root: Any | None = None,
    mcp_config_path: Any | None = None,
    source_rows: Mapping[str, Any] | None = None,
) -> CapabilityCatalogSnapshot:
    """Collect six owners. A broken source is explicit and cannot erase healthy rows."""
    rows = dict(source_rows or {})
    errors: list[CatalogSourceError] = []
    if source_rows is None:
        try:
            views = _resolved_plugin_views(plugin_views, project_root)
        except Exception as exc:
            views = tuple(plugin_views or ())
            error = CatalogSourceError(
                source="plugin_discovery",
                code="source_read_failed",
                detail=_exception_text(exc, 240),
            )
            errors.append(error)
            print(
                "CAPABILITY_CATALOG_SOURCE_ERROR "
                f"source={error.source} code={error.code} detail={error.detail}",
                file=sys.stderr,
            )
    else:
        views = tuple(rows.get("plugins", plugin_views or ()))
    calls = (
        ("tools", lambda: collect_tool_descriptors(rows.get("tools"))),
        ("toolsets", lambda: collect_toolset_descriptors(rows.get("toolsets"))),
        (
            "framework",
            lambda: collect_framework_descriptors(
                rows.get("framework"),
                project_root=project_root,
                mcp_config_path=mcp_config_path,
            ),
        ),
        ("integrations", lambda: collect_integration_descriptors(rows.get("integrations"))),
        ("plugins", lambda: collect_plugin_descriptors(views)),
    )
    collected: list[CapabilityDescriptor] = []
    for source, collect in calls:
        try:
            collected.extend(collect())
        except Exception as exc:  # source-local read model failure
            error = CatalogSourceError(
                source=source,
                code="source_read_failed",
                detail=_exception_text(exc, 240),
            )
            errors.append(error)
            print(
                "CAPABILITY_CATALOG_SOURCE_ERROR "
                f"source={source} code={error.code} detail={error.detail}",
                file=sys.stderr,
            )
    merged = _merge(collected)
    try:
        existing_ids = {item.id for item in merged}
        declared = tuple(
            item
            for item in collect_declared_plugin_contribution_descriptors(views)
            if item.id not in existing_ids
        )
        merged = _merge((*merged, *declared))
    except Exception as exc:
        error = CatalogSourceError(
            source="plugin_contributions",
            code="source_read_failed",
            detail=_exception_text(exc, 240),
        )
        errors.append(error)
        print(
            "CAPABILITY_CATALOG_SOURCE_ERROR "
            f"source={error.source} code={error.code} detail={error.detail}",
            file=sys.stderr,
        )
    requirements = {view.id: _plugin_requirements(view) for view in views}
    dependencies = {
        (view.id, capability_id): values
        for view in views
        for capability_id, values in _plugin_contribution_dependencies(view).items()
    }
    merged = tuple(
        _with_plugin_metadata(item, requirements, dependencies) for item in merged
    )
    return CapabilityCatalogSnapshot(
        status="partial" if errors else "ok",
        items=merged,
        generations=_generations(len(views)),
        errors=tuple(errors),
    )


def query_capabilities(
    snapshot: CapabilityCatalogSnapshot,
    query: CapabilityCatalogQuery | None = None,
) -> CapabilityCatalogPage:
    """Case-folded search with stable filters and an index-free last-id cursor."""
    if not isinstance(snapshot, CapabilityCatalogSnapshot):
        raise CapabilityCatalogQueryError("snapshot must be CapabilityCatalogSnapshot")
    resolved = CapabilityCatalogQuery() if query is None else query
    _validate_query(resolved)
    search = _safe_text(resolved.search, MAX_TEXT).casefold()
    kinds = {CapabilityKind(value) for value in resolved.kinds}
    sources = set(resolved.sources)
    matched = [
        item
        for item in snapshot.items
        if (not kinds or item.kind in kinds)
        and (not sources or item.source in sources)
        and (
            not search
            or search
            in " ".join(
                (item.id, item.display_name, item.description, item.owner, item.source)
            )
            .replace("_", " ")
            .replace("-", " ")
            .casefold()
        )
    ]
    total = len(matched)
    if resolved.cursor:
        material = _decode_cursor(resolved.cursor)
        if material not in {_cursor_material(item) for item in matched}:
            raise CapabilityCatalogQueryError("cursor is not present in this filtered snapshot")
        matched = [item for item in matched if _cursor_material(item) > material]
    items = tuple(matched[: resolved.limit])
    next_cursor = _encode_cursor(items[-1]) if len(matched) > resolved.limit and items else None
    return CapabilityCatalogPage(items=items, next_cursor=next_cursor, matched_count=total)


@dataclass(frozen=True, slots=True)
class _PersonaContext:
    tools: frozenset[str]
    toolsets: frozenset[str]
    skills: frozenset[str]
    mcp: frozenset[str]
    integration_actions: Mapping[str, frozenset[str]]
    policy_integration_actions: frozenset[str]
    callable_integration_actions: frozenset[str]
    configured_integrations: frozenset[str]
    entries: Mapping[str, Any]
    plugin_configured: Mapping[str, bool]
    tools_enabled: bool
    carrying: bool
    axes: tuple[tuple[str, str], ...]
    surfaces: tuple[tuple[str, str], ...]


def build_persona_capability_state(
    persona_id: str,
    *,
    catalog: CapabilityCatalogSnapshot | None = None,
    profile_config: Mapping[str, Any] | None = None,
    readiness_snapshot: Any | None = None,
    plugin_views: Iterable[Any] | None = None,
    explicit_disabled_ids: Collection[str] | None = None,
    configured_integration_ids: Collection[str] | None = None,
    configuration_requirement_statuses: Iterable[Any] | None = None,
) -> PersonaCapabilityProjection:
    """Join the catalog to exactly one passed persona grain."""
    resolved_persona_id = validate_persona_reference(persona_id)
    views = _resolved_plugin_views(plugin_views, None)
    physical_catalog = catalog or build_capability_catalog(plugin_views=views)
    if not isinstance(physical_catalog, CapabilityCatalogSnapshot):
        raise CapabilityCatalogError("catalog must be CapabilityCatalogSnapshot")
    if profile_config is None:
        from personas import services

        config = services.load_persona_config(resolved_persona_id)
    else:
        config = dict(profile_config)
    if readiness_snapshot is None:
        from personas import readiness

        readiness_snapshot = readiness.build_persona_readiness_snapshot(resolved_persona_id)
    from personas.readiness import PersonaReadinessSnapshot

    if not isinstance(readiness_snapshot, PersonaReadinessSnapshot):
        raise CapabilityCatalogError("readiness must be PersonaReadinessSnapshot")
    if readiness_snapshot.persona_id != resolved_persona_id:
        raise CapabilityCatalogError("readiness snapshot belongs to a different persona")
    disabled = {_validated_id(value) for value in explicit_disabled_ids or ()}
    context = _context(
        resolved_persona_id,
        config,
        readiness_snapshot,
        configured_integration_ids,
        configuration_requirement_statuses,
        views,
    )
    by_plugin = {view.id: view for view in views}
    assigned_plugins = {
        item.owner.removeprefix("plugin.")
        for item in physical_catalog.items
        if item.owner.startswith("plugin.") and _assigned(item, context, False)
    }
    base_states = tuple(
        _state(
            item,
            context,
            by_plugin,
            disabled,
            item.name in assigned_plugins,
        )
        for item in physical_catalog.items
    )
    states = _apply_dependency_state(base_states)
    return PersonaCapabilityProjection(
        persona_id=resolved_persona_id,
        status="partial" if physical_catalog.errors else "ok",
        states=states,
        catalog_errors=physical_catalog.errors,
    )


def _context(
    persona_id: str,
    config: Mapping[str, Any],
    readiness: Any,
    configured_ids: Collection[str] | None,
    requirement_statuses: Iterable[Any] | None,
    plugin_views: Iterable[Any],
) -> _PersonaContext:
    from integrations import capabilities as integration_policy
    from integrations import registry as integration_registry
    from personas import services, skill_assignment
    from runtime import (
        capabilities as runtime_capabilities,
    )
    from runtime import capability_contributions, persona_tools, tool_registry, toolsets

    scope = services.resolve_persona_tool_scope(dict(config))
    entries = {entry.name: entry for entry in tool_registry.list_registered()}
    assigned_toolsets = tool_registry.resolve_toolset_closure(
        enabled_toolsets=list(scope.toolsets) or None,
        registry=toolsets.TOOLSETS,
    )
    assigned_tools = set(scope.tools)
    for toolset in scope.toolsets:
        for name in runtime_capabilities.resolve_toolset(toolset, toolsets.TOOLSETS):
            entry = entries.get(name)
            if entry is not None and entry.toolset in assigned_toolsets:
                assigned_tools.add(name)
    blueprint = config.get("capability_blueprint")
    skills = set(_strings(blueprint.get("skills"))) if isinstance(blueprint, Mapping) else set()
    skills.update(skill_assignment.installed_skill_names(persona_id))
    mcp = _mcp_names(config)
    integration_actions: dict[str, set[str]] = defaultdict(set)
    blocked_config: set[str] = set()
    for row in readiness.capabilities:
        if row.kind != "integration":
            continue
        integration_id, _separator, action_name = row.id.partition(".")
        if action_name and row.axes.get("declared") == "READY":
            integration_actions[integration_id].add(action_name)
        if row.axes.get("configured") == "BLOCKED":
            blocked_config.add(integration_id)
    for name in assigned_tools:
        action = str(getattr(entries.get(name), "integration_action", "") or "")
        if action:
            integration_id, _separator, action_name = action.partition(".")
            if action_name:
                integration_actions[integration_id].add(action_name)
    assigned_action_ids = {
        f"{integration_id}.{action_name}"
        for integration_id, actions in integration_actions.items()
        for action_name in actions
    }
    policy_actions = {
        action_id
        for action_id in assigned_action_ids
        if integration_policy.is_integration_action_allowed(
            *action_id.partition(".")[::2],
            surface="model",
        )
    }
    try:
        from security import kill_switches

        tools_enabled = not kill_switches.is_disabled(persona_tools.KILL_SWITCH_NAME)
    except Exception:
        tools_enabled = True
    callable_actions = {
        entry.integration_action
        for name, entry in entries.items()
        if tools_enabled
        and name in assigned_tools
        and entry.integration_action in assigned_action_ids
        and callable(entry.handler)
    }
    configured = (
        set(integration_registry.get_enabled())
        if configured_ids is None
        else {str(value).strip() for value in configured_ids if str(value).strip()}
    )
    configured -= blocked_config
    statuses = (
        capability_contributions.evaluate_config_requirements()
        if requirement_statuses is None
        else tuple(requirement_statuses)
    )
    from runtime.capability_contributions import ConfigRequirementStatus

    plugin_configured: dict[str, bool] = {
        view.id: all(
            not bool(row.get("required", True))
            or not str(row.get("env_var") or "").strip()
            or bool(os.environ.get(str(row.get("env_var") or "").strip()))
            for row in view.contribution_inventory
            if str(row.get("type") or "") == "config_requirement"
        )
        for view in plugin_views
    }
    for status in statuses:
        if not isinstance(status, ConfigRequirementStatus):
            raise CapabilityCatalogError("config status must be ConfigRequirementStatus")
        plugin_configured[status.plugin_id] = plugin_configured.get(status.plugin_id, True) and (
            status.satisfied or not status.required
        )
    return _PersonaContext(
        tools=frozenset(assigned_tools),
        toolsets=frozenset(assigned_toolsets),
        skills=frozenset(skills),
        mcp=frozenset(mcp),
        integration_actions={
            name: frozenset(actions) for name, actions in integration_actions.items()
        },
        policy_integration_actions=frozenset(policy_actions),
        callable_integration_actions=frozenset(callable_actions),
        configured_integrations=frozenset(configured),
        entries=entries,
        plugin_configured=plugin_configured,
        tools_enabled=tools_enabled,
        carrying=readiness.axes["transportable"].status != "BLOCKED",
        axes=tuple((name, axis.status) for name, axis in sorted(readiness.axes.items())),
        surfaces=tuple(
            (name, surface.status) for name, surface in sorted(readiness.surfaces.items())
        ),
    )


def _state(
    descriptor: CapabilityDescriptor,
    context: _PersonaContext,
    plugins: Mapping[str, Any],
    disabled: set[str],
    plugin_assigned: bool,
) -> PersonaCapabilityState:
    assigned = _assigned(descriptor, context, plugin_assigned)
    configured = _configured(descriptor, context)
    handler = _handler(descriptor, context, plugins)
    route = context.carrying or descriptor.kind is CapabilityKind.PLUGIN
    switch_applies = descriptor.kind in {
        CapabilityKind.TOOL,
        CapabilityKind.TOOLSET,
        CapabilityKind.INTEGRATION,
    }
    switch_enabled = context.tools_enabled or not switch_applies
    callable_state = handler and route and switch_enabled
    reasons: list[BlockedReason] = []
    plugin_id = (
        descriptor.owner.removeprefix("plugin.")
        if descriptor.owner.startswith("plugin.")
        else descriptor.name
        if descriptor.kind is CapabilityKind.PLUGIN
        else ""
    )
    view = plugins.get(plugin_id)
    available = bool(not plugin_id or view is not None)
    if not available:
        reasons.append(
            _reason("unavailable_owner", f"owner plugin {plugin_id!r} is not discovered")
        )
    if view is not None:
        desired = str(getattr(view.desired_state, "value", view.desired_state))
        effective = str(getattr(view.effective_state, "value", view.effective_state))
        lifecycle = str(getattr(view.lifecycle_state, "value", view.lifecycle_state))
        if desired == "disabled":
            reasons.append(_reason("plugin_disabled", f"owner plugin {plugin_id!r} is disabled"))
        if effective != "loaded" or lifecycle in {"degraded", "failed", "restart_required"}:
            reasons.append(_reason("plugin_degraded", f"owner plugin {plugin_id!r} is {lifecycle}"))
    if descriptor.id in disabled:
        reasons.append(_reason("explicit_disabled", "profile equipment explicitly disables it"))
    if not switch_enabled:
        reasons.append(
            _reason("explicit_disabled", "persona tool kill switch disables execution")
        )
    if (
        descriptor.kind is CapabilityKind.INTEGRATION
        and assigned
        and _allowed_integration_actions(descriptor, context)
        != _assigned_integration_action_ids(descriptor, context)
    ):
        reasons.append(
            _reason("explicit_disabled", "integration action policy disables persona exposure")
        )
    if not assigned:
        reasons.append(_reason("unassigned", "capability is not assigned to this persona"))
    if not configured:
        detail = "missing required configuration"
        if descriptor.configuration_requirements:
            detail += ": " + ", ".join(descriptor.configuration_requirements)
        reasons.append(_reason("missing_configuration", detail))
    if not handler:
        detail = "no callable physical handler"
        if descriptor.kind is CapabilityKind.INTEGRATION:
            detail = "no callable persona wrapper is registered"
        elif descriptor.kind is CapabilityKind.PLUGIN:
            detail = "plugin is not physically loaded"
        reasons.append(_reason("missing_handler", detail))
    if not route:
        reasons.append(_reason("noncarrying_route", "selected runtime route carries no tools"))
    unique = {(reason.code, reason.detail): reason for reason in reasons}
    ordered = tuple(
        sorted(unique.values(), key=lambda value: (_REASON_ORDER[value.code], value.detail))
    )
    enabled = bool(available and assigned and configured and callable_state and not ordered)
    axes = dict(context.axes)
    axes.update(
        {
            "declared": "READY" if assigned else "BLOCKED",
            "configured": "READY" if configured else "BLOCKED",
            "callable": "READY" if callable_state else "BLOCKED",
        }
    )
    if descriptor.kind is CapabilityKind.PLUGIN:
        axes.update(
            {
                "transportable": "NOT_APPLICABLE",
                "channel-bound": "NOT_APPLICABLE",
                "scheduler-safe": "NOT_APPLICABLE",
            }
        )
        surfaces = (("runtime", "READY" if callable_state else "BLOCKED"),)
    else:
        supported = set(descriptor.supported_surfaces)
        surfaces = tuple(
            (
                name,
                "NOT_APPLICABLE"
                if name not in supported
                else "BLOCKED"
                if status == "READY" and not callable_state
                else status,
            )
            for name, status in context.surfaces
        )
    return PersonaCapabilityState(
        descriptor=descriptor,
        available=available,
        assigned=assigned,
        configured=configured,
        callable=callable_state,
        enabled=enabled,
        readiness_axes=tuple(sorted(axes.items())),
        surface_states=surfaces,
        blocked_reasons=ordered,
    )


def _apply_dependency_state(
    states: tuple[PersonaCapabilityState, ...],
) -> tuple[PersonaCapabilityState, ...]:
    """Propagate physical dependency failures in linear graph time."""
    by_id = {state.descriptor.id: state for state in states}
    reverse: dict[str, set[str]] = defaultdict(set)
    missing_by_id: dict[str, tuple[str, ...]] = {}
    blocked = {
        capability_id
        for capability_id, state in by_id.items()
        if not _dependency_operational(state)
    }
    for capability_id, state in by_id.items():
        missing = tuple(
            dependency
            for dependency in state.descriptor.dependencies
            if dependency not in by_id
        )
        if missing:
            missing_by_id[capability_id] = missing
            blocked.add(capability_id)
        if any(
            dependency in state.descriptor.persona_dependencies
            and dependency in by_id
            and not by_id[dependency].assigned
            for dependency in state.descriptor.dependencies
        ):
            blocked.add(capability_id)
        for dependency in state.descriptor.dependencies:
            if dependency in by_id:
                if (
                    state.descriptor.kind is CapabilityKind.TOOL
                    and dependency.startswith("toolset.")
                ):
                    # The tool registry's owner/toolset check already makes
                    # this edge load-bearing. Do not turn the reciprocal
                    # toolset -> tool inventory edge into a false cycle.
                    continue
                reverse[dependency].add(capability_id)

    queue = deque(blocked)
    while queue:
        dependency = queue.popleft()
        for dependent in reverse.get(dependency, ()):
            if dependent not in blocked:
                blocked.add(dependent)
                queue.append(dependent)

    result: list[PersonaCapabilityState] = []
    for state in states:
        capability_id = state.descriptor.id
        missing = missing_by_id.get(capability_id, ())
        failed = tuple(
            dependency
            for dependency in state.descriptor.dependencies
            if (
                dependency in blocked
                or (
                    dependency in state.descriptor.persona_dependencies
                    and dependency in by_id
                    and not by_id[dependency].assigned
                )
            )
            and not (
                state.descriptor.kind is CapabilityKind.TOOL
                and dependency.startswith("toolset.")
            )
        )
        reasons = list(state.blocked_reasons)
        if missing or failed:
            parts = []
            if missing:
                parts.append("missing dependencies: " + ", ".join(missing))
            if failed:
                parts.append("blocked dependencies: " + ", ".join(failed))
            reasons.append(_reason("dependency_conflict", "; ".join(parts)))
        unique = {(reason.code, reason.detail): reason for reason in reasons}
        ordered = tuple(
            sorted(unique.values(), key=lambda value: (_REASON_ORDER[value.code], value.detail))
        )
        result.append(
            dataclasses.replace(
                state,
                enabled=bool(state.enabled and capability_id not in blocked and not ordered),
                blocked_reasons=ordered,
            )
        )
    return tuple(result)


def _dependency_operational(state: PersonaCapabilityState) -> bool:
    blocking = {reason.code for reason in state.blocked_reasons} - {"unassigned"}
    return bool(
        state.available
        and state.configured
        and state.callable
        and not blocking
    )


def _assigned(
    descriptor: CapabilityDescriptor,
    context: _PersonaContext,
    plugin_assigned: bool,
) -> bool:
    assignments: dict[CapabilityKind, Collection[str]] = {
        CapabilityKind.TOOL: context.tools,
        CapabilityKind.TOOLSET: context.toolsets,
        CapabilityKind.SKILL: context.skills,
        CapabilityKind.MCP: context.mcp,
        CapabilityKind.INTEGRATION: context.integration_actions,
    }
    return (
        plugin_assigned
        if descriptor.kind is CapabilityKind.PLUGIN
        else (descriptor.name in assignments[descriptor.kind])
    )


def _configured(descriptor: CapabilityDescriptor, context: _PersonaContext) -> bool:
    if descriptor.kind is CapabilityKind.INTEGRATION:
        return descriptor.name in context.configured_integrations
    if descriptor.kind is CapabilityKind.MCP:
        return descriptor.physical_configured
    plugin_id = (
        descriptor.name
        if descriptor.kind is CapabilityKind.PLUGIN
        else descriptor.owner.removeprefix("plugin.")
    )
    if plugin_id and plugin_id != descriptor.owner:
        return context.plugin_configured.get(
            plugin_id,
            not descriptor.configuration_requirements,
        )
    return True


def _handler(
    descriptor: CapabilityDescriptor,
    context: _PersonaContext,
    plugins: Mapping[str, Any],
) -> bool:
    if descriptor.kind is CapabilityKind.TOOL:
        return callable(getattr(context.entries.get(descriptor.name), "handler", None))
    if descriptor.kind is CapabilityKind.TOOLSET:
        names = [
            value.removeprefix("tool.")
            for value in descriptor.dependencies
            if value.startswith("tool.")
        ]
        return descriptor.physical_callable and (
            not names
            or all(
                callable(getattr(context.entries.get(name), "handler", None))
                for name in names
            )
        )
    if descriptor.kind is CapabilityKind.INTEGRATION:
        assigned = _assigned_integration_action_ids(descriptor, context)
        return bool(assigned) and assigned.issubset(context.callable_integration_actions)
    if descriptor.kind is CapabilityKind.PLUGIN:
        view = plugins.get(descriptor.name)
        return bool(
            view and str(getattr(view.effective_state, "value", view.effective_state)) == "loaded"
        )
    if descriptor.kind is CapabilityKind.MCP:
        return descriptor.physical_callable
    return descriptor.physical_callable


def _assigned_integration_action_ids(
    descriptor: CapabilityDescriptor,
    context: _PersonaContext,
) -> frozenset[str]:
    return frozenset(
        f"{descriptor.name}.{action}"
        for action in context.integration_actions.get(descriptor.name, frozenset())
    )


def _allowed_integration_actions(
    descriptor: CapabilityDescriptor,
    context: _PersonaContext,
) -> frozenset[str]:
    assigned = context.integration_actions.get(descriptor.name, frozenset())
    return frozenset(
        f"{descriptor.name}.{action}"
        for action in assigned
        if f"{descriptor.name}.{action}" in context.policy_integration_actions
    )


def _descriptor(
    kind: CapabilityKind,
    name: str,
    *,
    display_name: str,
    owner: str,
    source: str,
    version: str,
    description: str,
    dependencies: Iterable[str] | None,
    requirements: Iterable[str] | None,
    effect: str | None,
    surfaces: Iterable[str] | None,
    physical_configured: bool = True,
    physical_callable: bool = True,
    persona_dependencies: Iterable[str] | None = None,
) -> CapabilityDescriptor:
    normalized = _name(name)
    dependency_tuple = _stable(dependencies, _validated_id)
    persona_dependency_tuple = _stable(persona_dependencies, _validated_id)
    if not set(persona_dependency_tuple).issubset(dependency_tuple):
        raise CapabilityCatalogError("persona dependencies must be catalog dependencies")
    requirement_tuple = _stable(requirements, _requirement)
    surface_tuple = _stable(surfaces or _SURFACES, _label)
    return CapabilityDescriptor(
        id=f"{kind.value}.{normalized}",
        kind=kind,
        name=normalized,
        display_name=_required_safe(display_name, 96),
        owner=_required_safe(owner, 96),
        source=_required_safe(source, 96),
        version=_required_safe(version, 96),
        description=_safe_text(description, MAX_TEXT),
        dependencies=dependency_tuple,
        configuration_requirements=requirement_tuple,
        effect_class=_required_safe(effect or "read", 32),
        supported_surfaces=surface_tuple,
        physical_configured=physical_configured,
        physical_callable=physical_callable,
        persona_dependencies=persona_dependency_tuple,
    )


def _merge(values: Iterable[CapabilityDescriptor]) -> tuple[CapabilityDescriptor, ...]:
    merged: dict[str, CapabilityDescriptor] = {}
    for value in values:
        previous = merged.get(value.id)
        if previous is not None and previous != value:
            raise CapabilityCatalogCollisionError(
                f"{value.id!r} claimed by {previous.owner}@{previous.version} "
                f"and {value.owner}@{value.version}"
            )
        merged[value.id] = value
    if len(merged) > MAX_ITEMS:
        raise CapabilityCatalogError("catalog exceeds bounded item limit")
    return _sorted(merged.values())


def _with_plugin_metadata(
    item: CapabilityDescriptor,
    requirements: Mapping[str, tuple[str, ...]],
    dependencies: Mapping[tuple[str, str], tuple[str, ...]],
) -> CapabilityDescriptor:
    plugin_id = ""
    if item.owner.startswith("plugin."):
        plugin_id = item.owner.removeprefix("plugin.")
    elif item.kind is CapabilityKind.PLUGIN:
        plugin_id = item.name
    extra = requirements.get(plugin_id, ())
    dependency_extra = dependencies.get((plugin_id, item.id), ())
    if not extra and not dependency_extra:
        return item
    return dataclasses.replace(
        item,
        configuration_requirements=tuple(sorted(set(item.configuration_requirements) | set(extra))),
        dependencies=tuple(sorted(set(item.dependencies) | set(dependency_extra))),
        persona_dependencies=tuple(
            sorted(set(item.persona_dependencies) | set(dependency_extra))
        ),
    )


def _plugin_requirements(view: Any) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(row.get("env_var") or row.get("config_requirement"))
                for row in view.contribution_inventory
                if str(row.get("type") or "") == "config_requirement"
                and _REQUIREMENT_RE.fullmatch(
                    str(row.get("env_var") or row.get("config_requirement") or "")
                )
            }
        )
    )


def _plugin_dependencies(view: Any) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                f"plugin.{row.get('plugin_id')}"
                for row in view.contribution_inventory
                if str(row.get("type") or "") == "plugin_requirement"
                and str(row.get("plugin_id") or "").strip()
            }
        )
    )


def _plugin_contribution_ids(view: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in view.contribution_inventory:
        kind = _CONTRIBUTION_KINDS.get(str(row.get("type") or ""))
        contribution_id = str(row.get("contribution_id") or "").strip()
        owner_field = _contribution_owner_field(str(row.get("type") or ""))
        physical_name = str(row.get(owner_field) or "").strip() if owner_field else ""
        if kind is not None and contribution_id and physical_name:
            result[contribution_id] = f"{kind.value}.{_name(physical_name)}"
    return result


def _plugin_contribution_dependencies(view: Any) -> dict[str, tuple[str, ...]]:
    contribution_ids = _plugin_contribution_ids(view)
    result: dict[str, tuple[str, ...]] = {}
    for row in view.contribution_inventory:
        contribution_id = str(row.get("contribution_id") or "").strip()
        catalog_id = contribution_ids.get(contribution_id)
        if catalog_id is None:
            continue
        result[catalog_id] = tuple(
            sorted(
                {
                    contribution_ids[dependency]
                    for value in row.get("depends_on", ())
                    if (dependency := str(value).strip()) in contribution_ids
                }
            )
        )
    return result


def _generations(plugin_count: int) -> tuple[tuple[str, int], ...]:
    try:
        from runtime import framework_registry, tool_registry, toolsets

        return tuple(
            sorted(
                (
                    ("framework_overlay", framework_registry.get_overlay_generation()),
                    ("plugins", plugin_count),
                    ("tools", tool_registry.get_generation()),
                    ("toolsets", toolsets.get_toolset_generation()),
                )
            )
        )
    except Exception as exc:
        detail = _exception_text(exc, 240)
        print(
            "CAPABILITY_CATALOG_GENERATION_ERROR "
            f"source=runtime code=generation_read_failed detail={detail}",
            file=sys.stderr,
        )
        return (("plugins", plugin_count),)


def _validate_query(query: CapabilityCatalogQuery) -> None:
    if not isinstance(query, CapabilityCatalogQuery):
        raise CapabilityCatalogQueryError("query must be CapabilityCatalogQuery")
    if type(query.limit) is not int or not 1 <= query.limit <= MAX_PAGE_SIZE:
        raise CapabilityCatalogQueryError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if len(query.kinds) != len(set(query.kinds)) or len(query.sources) != len(set(query.sources)):
        raise CapabilityCatalogQueryError("query filters contain duplicates")
    for source in query.sources:
        _label(source)
    if query.cursor:
        _decode_cursor(query.cursor)


def _reason(code: str, detail: str) -> BlockedReason:
    if code not in _REASON_ORDER:
        raise CapabilityCatalogError(f"unknown blocked reason {code!r}")
    rendered = _safe_text(detail, 240)
    if not rendered:
        raise CapabilityCatalogError("blocked reason detail is empty")
    return BlockedReason(code=code, detail=rendered)


def _sorted(values: Iterable[CapabilityDescriptor]) -> tuple[CapabilityDescriptor, ...]:
    return tuple(
        sorted(values, key=lambda value: (value.kind.value, value.id.casefold(), value.id))
    )


def _effect(values: Iterable[str], *, empty: str = "read") -> str:
    effects = {str(value or "read") for value in values}
    return next(iter(effects)) if len(effects) == 1 else "mixed" if effects else empty


def _contribution_owner_field(contribution_type: str) -> str:
    return {
        "tool": "tool",
        "toolset": "toolset",
        "skill": "skill",
        "mcp": "mcp_server",
        "mcp_server": "mcp_server",
    }.get(contribution_type, "")


def _toolset_member_id(value: Any) -> str:
    member = str(value).strip()
    prefix, separator, _name_value = member.partition(".")
    if separator and prefix in {kind.value for kind in CapabilityKind}:
        return _validated_id(member)
    return f"tool.{_name(member)}"


def _mcp_requirement_keys(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Return declared environment names only, never arbitrary config keys."""
    env = config.get("env")
    if not isinstance(env, Mapping):
        return ()
    return tuple(
        sorted(
            str(key)
            for key in env
            if _REQUIREMENT_RE.fullmatch(str(key))
        )
    )


def _mcp_names(config: Mapping[str, Any]) -> set[str]:
    mcp = config.get("mcp")
    if not isinstance(mcp, Mapping) or not isinstance(mcp.get("servers"), list):
        return set()
    result: set[str] = set()
    for item in mcp["servers"]:
        value = (
            item
            if isinstance(item, str)
            else (item.get("name") or item.get("id") or item.get("server") or "")
            if isinstance(item, Mapping)
            else ""
        )
        if str(value).strip():
            result.add(str(value).strip())
    return result


def _mcp_physical_state(
    transport: str,
    config: Mapping[str, Any],
) -> tuple[bool, bool]:
    """Evaluate only transport shape and key presence; never return values."""
    normalized_transport = str(transport or "").strip().casefold().replace("-", "_")
    env = config.get("env")
    env_ready = True
    if isinstance(env, Mapping):
        for value in env.values():
            text = _stringify(value).strip()
            match = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", text)
            if not text or (match and not os.environ.get(match.group(1))):
                env_ready = False
                break
    if normalized_transport == "stdio":
        command = _stringify(config.get("command")).strip()
        configured = bool(command and env_ready)
        command_path = Path(command).expanduser() if command else None
        callable_state = bool(
            configured
            and (
                shutil.which(command) is not None
                or (command_path is not None and command_path.is_file())
            )
        )
        return configured, callable_state
    if normalized_transport in {"http", "sse", "streamable_http"}:
        url = _stringify(config.get("url")).strip()
        configured = bool(_valid_http_url(url) and env_ready)
        return configured, configured
    return False, False


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme.casefold() in {"http", "https"}
        and parsed.netloc
        and parsed.hostname
        and not any(character.isspace() for character in value)
    )


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(item.strip() for item in value or () if isinstance(item, str) and item.strip())


def _stable(values: Iterable[Any] | None, validator: Any) -> tuple[str, ...]:
    result = tuple(validator(value) for value in values or ())
    if len(result) > 256:
        raise CapabilityCatalogError("catalog collection exceeds its bound")
    return tuple(sorted(set(result)))


def _name(value: Any) -> str:
    if type(value) is not str:
        raise CapabilityCatalogError("capability name must be a string")
    result = " ".join(value.strip().split())
    if (
        not result
        or len(result) > 96
        or _CONTROL_RE.search(result)
        or "/" in result
        or "\\" in result
        or result in {".", ".."}
    ):
        raise CapabilityCatalogError("capability name has an unsafe shape")
    return result


def validate_persona_reference(value: Any) -> str:
    """Validate a read target while preserving the built-in default profile."""
    if type(value) is not str or not value or value != value.strip():
        raise CapabilityCatalogQueryError("persona id has an invalid shape")
    if value == "default":
        return value
    from personas import core

    try:
        core.validate_persona_name(value)
    except ValueError as exc:
        raise CapabilityCatalogQueryError("persona id has an invalid shape") from exc
    return value


def _validated_id(value: Any) -> str:
    if type(value) is not str or "." not in value:
        raise CapabilityCatalogError("capability id must be namespaced")
    prefix, name = value.split(".", 1)
    try:
        kind = CapabilityKind(prefix)
    except ValueError as exc:
        raise CapabilityCatalogError("capability id has an unknown kind") from exc
    return f"{kind.value}.{_name(name)}"


def _requirement(value: Any) -> str:
    if type(value) is not str or not _REQUIREMENT_RE.fullmatch(value):
        raise CapabilityCatalogError("configuration requirement has an unsafe shape")
    return value


def _label(value: Any) -> str:
    return _required_safe(value, 96)


def _required_safe(value: Any, limit: int) -> str:
    try:
        original = str(value).strip()
    except Exception as exc:
        raise CapabilityCatalogError("operator-visible field cannot be rendered") from exc
    result = _safe_text(original, limit)
    if not result or result != original or _PRIVATE_PATH_RE.search(result):
        raise CapabilityCatalogError("operator-visible field has an unsafe shape")
    return result


def _safe_text(value: Any, limit: int) -> str:
    result = " ".join(_stringify(value).split())
    from runtime import capability_plugin_manifest

    result = capability_plugin_manifest.redact_detail(result)
    result = _URI_USERINFO_RE.sub(r"\1<redacted>@", result)
    result = _AUTHORIZATION_RE.sub("<redacted-secret>", result)
    result = _SECRET_ASSIGNMENT_RE.sub("<redacted-secret>", result)
    result = _PRIVATE_PATH_RE.sub("<private-path>", result)
    result = _PRIVATE_ID_RE.sub("<private-id>", result)
    result = _CONTROL_RE.sub("", result)
    return result if len(result) <= limit else result[: limit - 3] + "..."


def safe_operator_text(value: Any, limit: int = MAX_TEXT) -> str:
    """Public bounded serializer used by adjacent operator-facing read models."""
    if type(limit) is not int or limit < 1:
        raise CapabilityCatalogError("safe text limit must be a positive integer")
    return _safe_text(value, limit)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return f"<unprintable-{type(value).__name__}>"


def _exception_text(exc: BaseException, limit: int) -> str:
    return _safe_text(f"{type(exc).__name__}: {_stringify(exc)}", limit)


def _cursor_material(item: CapabilityDescriptor) -> str:
    return f"{item.kind.value}\0{item.id.casefold()}\0{item.id}"


def _encode_cursor(item: CapabilityDescriptor) -> str:
    return base64.urlsafe_b64encode(_cursor_material(item).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> str:
    if type(cursor) is not str or not cursor or len(cursor) > 512:
        raise CapabilityCatalogQueryError("cursor has an invalid shape")
    try:
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        ).decode()
        kind, folded_id, capability_id = raw.split("\0", 2)
        if _validated_id(capability_id).partition(".")[0] != kind:
            raise ValueError("cursor kind mismatch")
        if capability_id.casefold() != folded_id:
            raise ValueError("cursor sort material mismatch")
        return raw
    except (UnicodeError, ValueError, TypeError) as exc:
        raise CapabilityCatalogQueryError("cursor is malformed") from exc


__all__ = [
    "BlockedReason",
    "CapabilityCatalogCollisionError",
    "CapabilityCatalogError",
    "CapabilityCatalogPage",
    "CapabilityCatalogQuery",
    "CapabilityCatalogQueryError",
    "CapabilityCatalogSnapshot",
    "CapabilityDescriptor",
    "CapabilityKind",
    "CatalogSourceError",
    "PersonaCapabilityProjection",
    "PersonaCapabilityState",
    "build_capability_catalog",
    "build_persona_capability_state",
    "collect_framework_descriptors",
    "collect_integration_descriptors",
    "collect_plugin_instance_views",
    "collect_plugin_descriptors",
    "collect_tool_descriptors",
    "collect_toolset_descriptors",
    "query_capabilities",
    "safe_operator_text",
    "validate_persona_reference",
]
