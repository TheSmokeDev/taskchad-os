from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import MappingProxyType

import pytest

from integrations.registry import IntegrationInfo
from personas import capability_catalog as catalog
from personas import readiness, skill_assignment
from personas.readiness import (
    AXIS_NAMES,
    AxisReadiness,
    CapabilityReadiness,
    PersonaReadinessSnapshot,
    SurfaceReadiness,
)
from runtime import tool_registry
from runtime.capability_plugin_manifest import ManifestSource
from runtime.capability_plugins import (
    PluginDesiredState,
    PluginEffectiveState,
    PluginInstanceView,
    PluginLifecycleState,
)
from runtime.framework_registry import FrameworkRegistry, McpServerEntry, SkillEntry

TOOL_NAME = "piv_catalog_tool"
MISSING_HANDLER_TOOL = "piv_catalog_missing_handler"
SECRET = "<REDACTED-openai>"
PRIVATE_PATH = "C:\\Users\\private-user\\private\\SKILL.md"
CHANNEL_ID = "999999999999999999"


def _schema(name: str) -> dict[str, object]:
    return tool_registry.build_tool_schema(name, f"{name} test tool")


def _handler(**_kwargs: object) -> dict[str, bool]:
    return {"ok": True}


def _entry(
    name: str,
    *,
    handler: object = _handler,
    plugin_id: str = "",
    plugin_version: str = "",
    integration_action: str | None = None,
    toolset: str = "safe_core",
) -> tool_registry.ToolEntry:
    return tool_registry.ToolEntry(
        name=name,
        description=(
            f"Operator safe description API_KEY={SECRET} {PRIVATE_PATH} {CHANNEL_ID}"
        ),
        schema=_schema(name),
        handler=handler if callable(handler) else None,
        toolset=toolset,
        effect="read",
        integration_action=integration_action,
        plugin_id=plugin_id,
        plugin_version=plugin_version,
    )


def _framework(tmp_path: Path) -> FrameworkRegistry:
    return FrameworkRegistry(
        project_root=tmp_path,
        skills=(
            SkillEntry(
                name="private_skill",
                description=f"Skill description token={SECRET} {PRIVATE_PATH}",
                path=".claude/skills/private/SKILL.md",
            ),
        ),
        mcp_servers=(
            McpServerEntry(
                name="trusted_mcp",
                transport="stdio",
                config={"command": "python", "env": {"TOKEN": SECRET}},
                source=PRIVATE_PATH,
            ),
        ),
        mcp_config_path=tmp_path / "private-mcp.json",
    )


def _plugin_view(
    *,
    desired: PluginDesiredState = PluginDesiredState.ENABLED,
    effective: PluginEffectiveState = PluginEffectiveState.LOADED,
    lifecycle: PluginLifecycleState = PluginLifecycleState.LOADED,
) -> PluginInstanceView:
    return PluginInstanceView(
        id="piv-catalog-plugin",
        version="1.2.3",
        source=ManifestSource.BUNDLED,
        desired_state=desired,
        effective_state=effective,
        lifecycle_state=lifecycle,
        contribution_ids=("plugin.tool",),
        contribution_inventory=(
            MappingProxyType(
                {
                    "contribution_id": "plugin.config",
                    "type": "config_requirement",
                    "config_requirement": "plugin.api",
                    "env_var": "PLUGIN_API_TOKEN",
                    "required": True,
                }
            ),
        ),
        residual_contribution_ids=(),
        error_code="",
        detail="",
    )


def _source_rows(
    tmp_path: Path,
    *,
    entries: tuple[tool_registry.ToolEntry, ...] | None = None,
    plugin_view: PluginInstanceView | None = None,
) -> dict[str, object]:
    return {
        "tools": entries or (_entry(TOOL_NAME),),
        "toolsets": {
            "safe_core": {
                "description": "Scoped safe core",
                "tools": [entry.name for entry in entries or (_entry(TOOL_NAME),)],
                "includes": [],
            }
        },
        "framework": _framework(tmp_path),
        "integrations": {
            "sheets": IntegrationInfo(
                name="sheets",
                display_name="Google Sheets",
                auth_type="google_oauth",
                required_config=[],
                module_path="integrations.sheets_api",
            )
        },
        "plugins": (plugin_view or _plugin_view(),),
    }


def _readiness(
    *,
    transport: str = "READY",
    integration_config: str = "READY",
    integration_action: str = "sheets.read",
) -> PersonaReadinessSnapshot:
    axes = {
        name: AxisReadiness(status=transport if name == "transportable" else "READY")
        for name in AXIS_NAMES
    }
    surfaces = {
        "discord": SurfaceReadiness(status="READY", reasons=(), caller_tools=True),
        "direct_chat": SurfaceReadiness(status="READY", reasons=(), caller_tools=True),
        "cabinet": SurfaceReadiness(status="READY", reasons=(), caller_tools=True),
        "web": SurfaceReadiness(
            status="NOT_APPLICABLE", reasons=(), caller_tools=False
        ),
        "scheduled": SurfaceReadiness(
            status="NOT_APPLICABLE", reasons=(), caller_tools=False
        ),
    }
    integration = CapabilityReadiness(
        id=integration_action,
        kind="integration",
        status="READY" if integration_config == "READY" else "BLOCKED",
        axes={
            "declared": "READY",
            "transportable": transport,
            "callable": "READY",
            "configured": integration_config,
            "channel-bound": "READY",
            "scheduler-safe": "NOT_APPLICABLE",
        },
        surfaces={name: value.status for name, value in surfaces.items()},
        reasons=(),
    )
    return PersonaReadinessSnapshot(
        schema_version=1,
        persona_id="persona-a",
        status="READY",
        selected_lane="generic",
        selected_providers=("test",),
        axes=axes,
        surfaces=surfaces,
        capabilities=(integration,),
    )


@pytest.fixture(autouse=True)
def _physical_tools() -> None:
    for name in (TOOL_NAME, MISSING_HANDLER_TOOL, "piv_catalog_sheets"):
        tool_registry.unregister_tool(name)
    tool_registry.register_tool(
        TOOL_NAME,
        "Catalog test tool",
        toolset="safe_core",
        handler=_handler,
    )
    tool_registry.register_tool(
        MISSING_HANDLER_TOOL,
        "Catalog missing-handler tool",
        toolset="safe_core",
        handler=None,
    )
    tool_registry.register_tool(
        "piv_catalog_sheets",
        "Catalog Sheets wrapper",
        toolset="safe_core",
        handler=_handler,
        integration_action="sheets.read",
    )
    yield
    for name in (TOOL_NAME, MISSING_HANDLER_TOOL, "piv_catalog_sheets"):
        tool_registry.unregister_tool(name)


def _projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: tuple[tool_registry.ToolEntry, ...] | None = None,
    config: dict[str, object] | None = None,
    readiness_snapshot: PersonaReadinessSnapshot | None = None,
    plugin_view: PluginInstanceView | None = None,
    disabled: tuple[str, ...] = (),
    configured_integrations: tuple[str, ...] = ("sheets",),
) -> catalog.PersonaCapabilityProjection:
    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())
    view = plugin_view or _plugin_view()
    snapshot = catalog.build_capability_catalog(
        plugin_views=(view,),
        source_rows=_source_rows(
            tmp_path,
            entries=entries,
            plugin_view=view,
        ),
    )
    return catalog.build_persona_capability_state(
        "persona-a",
        catalog=snapshot,
        profile_config=config
        or {
            "tools": [TOOL_NAME, "piv_catalog_sheets"],
            "toolsets": [],
            "capability_blueprint": {"skills": ["private_skill"]},
            "mcp": {"servers": ["trusted_mcp"]},
        },
        readiness_snapshot=readiness_snapshot or _readiness(),
        plugin_views=(view,),
        explicit_disabled_ids=disabled,
        configured_integration_ids=configured_integrations,
        configuration_requirement_statuses=(),
    )


def _state_by_id(
    projection: catalog.PersonaCapabilityProjection,
    capability_id: str,
) -> catalog.PersonaCapabilityState:
    return next(state for state in projection.states if state.descriptor.id == capability_id)


def test_catalog_normalizes_all_six_kinds_and_redacts_operator_output(
    tmp_path: Path,
) -> None:
    view = _plugin_view()
    snapshot = catalog.build_capability_catalog(
        plugin_views=(view,),
        source_rows=_source_rows(
            tmp_path,
            entries=(
                _entry(
                    TOOL_NAME,
                    plugin_id=view.id,
                    plugin_version=view.version,
                ),
            ),
            plugin_view=view,
        ),
    )

    assert snapshot.status == "ok"
    assert {item.kind for item in snapshot.items} == set(catalog.CapabilityKind)
    assert all(item.id == f"{item.kind.value}.{item.name}" for item in snapshot.items)
    assert all(item.owner and item.source and item.version for item in snapshot.items)
    plugin_tool = next(item for item in snapshot.items if item.id == f"tool.{TOOL_NAME}")
    assert plugin_tool.owner == f"plugin.{view.id}"
    assert plugin_tool.version == view.version
    assert f"plugin.{view.id}" in plugin_tool.dependencies
    serialized = json.dumps(snapshot.as_dict(), sort_keys=True)
    assert SECRET not in serialized
    assert PRIVATE_PATH not in serialized
    assert CHANNEL_ID not in serialized
    assert "private-mcp.json" not in serialized
    assert "PLUGIN_API_TOKEN" in serialized


def test_catalog_refuses_cross_owner_stable_id_collision(tmp_path: Path) -> None:
    baseline = _entry(TOOL_NAME)
    plugin = _entry(
        TOOL_NAME,
        plugin_id="other-plugin",
        plugin_version="9.9.9",
    )

    with pytest.raises(catalog.CapabilityCatalogCollisionError):
        catalog.build_capability_catalog(
            source_rows=_source_rows(tmp_path, entries=(baseline, plugin))
        )


def test_broken_source_is_partial_keeps_healthy_rows_and_prints_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = _source_rows(tmp_path)
    rows["tools"] = (
        IntegrationInfo("wrong", "Wrong", "token"),
    )

    snapshot = catalog.build_capability_catalog(source_rows=rows)

    assert snapshot.status == "partial"
    assert snapshot.errors[0].source == "tools"
    assert any(item.kind is catalog.CapabilityKind.SKILL for item in snapshot.items)
    assert "CAPABILITY_CATALOG_SOURCE_ERROR source=tools" in capsys.readouterr().err


def test_query_is_deterministic_searchable_bounded_and_cursor_safe(tmp_path: Path) -> None:
    snapshot = catalog.build_capability_catalog(source_rows=_source_rows(tmp_path))
    first = catalog.query_capabilities(
        snapshot,
        catalog.CapabilityCatalogQuery(limit=2),
    )
    second = catalog.query_capabilities(
        snapshot,
        catalog.CapabilityCatalogQuery(limit=2, cursor=first.next_cursor),
    )
    searched = catalog.query_capabilities(
        snapshot,
        catalog.CapabilityCatalogQuery(search="trusted mcp", limit=10),
    )

    assert first.next_cursor
    assert {item.id for item in first.items}.isdisjoint(item.id for item in second.items)
    assert [item.id for item in first.items] == sorted(
        [item.id for item in first.items],
        key=lambda value: (value.partition(".")[0], value.casefold(), value),
    )
    assert [item.id for item in searched.items] == ["mcp.trusted_mcp"]
    with pytest.raises(catalog.CapabilityCatalogQueryError):
        catalog.query_capabilities(
            snapshot,
            catalog.CapabilityCatalogQuery(limit=101),
        )
    with pytest.raises(catalog.CapabilityCatalogQueryError):
        catalog.query_capabilities(
            snapshot,
            catalog.CapabilityCatalogQuery(cursor="not-a-real-cursor"),
        )


def test_assigned_tool_is_enabled_only_with_handler_and_carrying_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(tmp_path, monkeypatch)

    state = _state_by_id(projection, f"tool.{TOOL_NAME}")
    assert state.available is True
    assert state.assigned is True
    assert state.configured is True
    assert state.callable is True
    assert state.enabled is True
    assert state.blocked_reasons == ()


def test_green_looking_tool_with_missing_handler_is_visible_and_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(MISSING_HANDLER_TOOL, handler=None)
    projection = _projection(
        tmp_path,
        monkeypatch,
        entries=(entry,),
        config={"tools": [MISSING_HANDLER_TOOL], "toolsets": []},
    )

    state = _state_by_id(projection, f"tool.{MISSING_HANDLER_TOOL}")
    assert (state.available, state.assigned, state.configured) == (True, True, True)
    assert state.callable is False
    assert state.enabled is False
    assert [reason.code for reason in state.blocked_reasons] == ["missing_handler"]


def test_noncarrying_route_and_explicit_disable_are_ordered_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(
        tmp_path,
        monkeypatch,
        readiness_snapshot=_readiness(transport="BLOCKED"),
        disabled=(f"tool.{TOOL_NAME}",),
    )

    state = _state_by_id(projection, f"tool.{TOOL_NAME}")
    assert state.callable is False
    assert state.enabled is False
    assert [reason.code for reason in state.blocked_reasons] == [
        "explicit_disabled",
        "noncarrying_route",
    ]


def test_missing_integration_config_keeps_item_callable_but_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(
        tmp_path,
        monkeypatch,
        configured_integrations=(),
        readiness_snapshot=_readiness(integration_config="BLOCKED"),
    )

    state = _state_by_id(projection, "integration.sheets")
    assert state.available is True
    assert state.assigned is True
    assert state.configured is False
    assert state.callable is True
    assert state.enabled is False
    assert [reason.code for reason in state.blocked_reasons] == [
        "missing_configuration"
    ]


def test_live_integration_policy_disable_blocks_without_hiding_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integrations import capabilities as integration_policy

    def deny_policy(_integration: str, _action: str, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        integration_policy,
        "is_integration_action_allowed",
        deny_policy,
    )

    state = _state_by_id(
        _projection(tmp_path, monkeypatch),
        "integration.sheets",
    )

    assert state.available is True
    assert state.assigned is True
    assert state.callable is True
    assert state.enabled is False
    assert [reason.code for reason in state.blocked_reasons] == ["explicit_disabled"]


def test_disabled_degraded_plugin_remains_visible_without_auto_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _plugin_view(
        desired=PluginDesiredState.DISABLED,
        effective=PluginEffectiveState.UNLOADED,
        lifecycle=PluginLifecycleState.DEGRADED,
    )
    projection = _projection(tmp_path, monkeypatch, plugin_view=view)

    state = _state_by_id(projection, "plugin.piv-catalog-plugin")
    assert state.available is True
    assert state.assigned is False
    assert state.enabled is False
    assert [reason.code for reason in state.blocked_reasons] == [
        "plugin_disabled",
        "plugin_degraded",
        "unassigned",
        "missing_configuration",
        "missing_handler",
    ]


def test_missing_dependency_is_an_exact_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _source_rows(tmp_path)
    rows["toolsets"] = {}
    snapshot = catalog.build_capability_catalog(source_rows=rows)
    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())

    projection = catalog.build_persona_capability_state(
        "persona-a",
        catalog=snapshot,
        profile_config={"tools": [TOOL_NAME], "toolsets": []},
        readiness_snapshot=_readiness(),
        plugin_views=(_plugin_view(),),
        configured_integration_ids=("sheets",),
        configuration_requirement_statuses=(),
    )

    state = _state_by_id(projection, f"tool.{TOOL_NAME}")
    assert [reason.code for reason in state.blocked_reasons] == [
        "dependency_conflict"
    ]
    assert "toolset.safe_core" in state.blocked_reasons[0].detail


def test_two_profiles_cannot_infer_each_others_private_skill_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = catalog.build_capability_catalog(source_rows=_source_rows(tmp_path))
    monkeypatch.setattr(
        skill_assignment,
        "installed_skill_names",
        lambda persona_id: ("private_skill",) if persona_id == "persona-a" else (),
    )

    def project(persona_id: str) -> catalog.PersonaCapabilityProjection:
        ready = dataclasses.replace(_readiness(), persona_id=persona_id)
        return catalog.build_persona_capability_state(
            persona_id,
            catalog=snapshot,
            profile_config={"tools": [], "toolsets": []},
            readiness_snapshot=ready,
            configured_integration_ids=(),
            configuration_requirement_statuses=(),
        )

    import dataclasses

    first = _state_by_id(project("persona-a"), "skill.private_skill")
    second = _state_by_id(project("persona-b"), "skill.private_skill")
    assert first.assigned is True
    assert second.assigned is False
    assert [reason.code for reason in second.blocked_reasons] == ["unassigned"]


def test_readiness_adapter_preserves_six_axes_and_all_six_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(tmp_path, monkeypatch)

    rows = readiness.capability_readiness_from_projection(projection)

    assert {row.kind for row in rows} == {kind.value for kind in catalog.CapabilityKind}
    assert all(set(row.axes) == set(AXIS_NAMES) for row in rows)
    assert all(row.id.startswith(f"{row.kind}.") for row in rows)


def test_toolset_structure_cannot_assign_a_tool_owned_by_an_ungranted_toolset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime import toolsets

    name = "piv_cross_owner_tool"
    tool_registry.unregister_tool(name)
    tool_registry.register_tool(
        name,
        "Cross-owner assignment probe",
        toolset="private_pack",
        handler=_handler,
    )
    registry = {
        "safe_core": {"description": "safe", "tools": [name], "includes": []},
        "private_pack": {"description": "private", "tools": [], "includes": []},
    }
    monkeypatch.setattr(toolsets, "TOOLSETS", registry)
    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())
    try:
        rows = _source_rows(tmp_path, entries=(tool_registry.get_entry(name),))
        rows["toolsets"] = registry
        snapshot = catalog.build_capability_catalog(source_rows=rows)
        projection = catalog.build_persona_capability_state(
            "persona-a",
            catalog=snapshot,
            profile_config={"tools": [], "toolsets": ["safe_core"]},
            readiness_snapshot=_readiness(),
            plugin_views=(_plugin_view(),),
            configured_integration_ids=("sheets",),
            configuration_requirement_statuses=(),
        )
    finally:
        tool_registry.unregister_tool(name)

    state = _state_by_id(projection, f"tool.{name}")
    assert state.assigned is False
    assert state.enabled is False
    assert "unassigned" in {reason.code for reason in state.blocked_reasons}


def test_integration_callable_ignores_an_unassigned_global_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integrations import capabilities as integration_policy

    monkeypatch.setattr(
        integration_policy,
        "is_integration_action_allowed",
        lambda _integration, _action, **_kwargs: True,
    )
    projection = _projection(
        tmp_path,
        monkeypatch,
        config={"tools": [], "toolsets": []},
        readiness_snapshot=_readiness(integration_action="sheets.write"),
    )

    state = _state_by_id(projection, "integration.sheets")
    assert state.assigned is True
    assert state.callable is False
    assert state.enabled is False
    assert [reason.code for reason in state.blocked_reasons] == ["missing_handler"]


def test_blocked_integration_dependency_disables_its_assigned_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(
        tmp_path,
        monkeypatch,
        entries=(
            _entry(
                "piv_catalog_sheets",
                integration_action="sheets.read",
            ),
        ),
        configured_integrations=(),
        readiness_snapshot=_readiness(integration_config="BLOCKED"),
    )

    state = _state_by_id(projection, "tool.piv_catalog_sheets")
    assert state.assigned is True
    assert state.callable is True
    assert state.enabled is False
    assert [reason.code for reason in state.blocked_reasons] == ["dependency_conflict"]
    assert "integration.sheets" in state.blocked_reasons[0].detail


def test_mcp_requires_a_real_transport_command_and_resolved_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = McpServerEntry(
        name="broken_mcp",
        transport="stdio",
        config={
            "command": "piv-command-that-does-not-exist",
            "env": {"TOKEN": "${PIV_UNSET_MCP_TOKEN}"},
        },
        source="fixture",
    )
    framework = dataclasses.replace(_framework(tmp_path), mcp_servers=(server,))
    rows = _source_rows(tmp_path)
    rows["framework"] = framework
    monkeypatch.delenv("PIV_UNSET_MCP_TOKEN", raising=False)
    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())
    snapshot = catalog.build_capability_catalog(source_rows=rows)
    projection = catalog.build_persona_capability_state(
        "persona-a",
        catalog=snapshot,
        profile_config={"tools": [], "toolsets": [], "mcp": {"servers": ["broken_mcp"]}},
        readiness_snapshot=_readiness(),
        plugin_views=(_plugin_view(),),
        configured_integration_ids=(),
        configuration_requirement_statuses=(),
    )

    state = _state_by_id(projection, "mcp.broken_mcp")
    assert state.assigned is True
    assert state.configured is False
    assert state.callable is False
    assert [reason.code for reason in state.blocked_reasons] == [
        "missing_configuration",
        "missing_handler",
    ]


def test_projection_rejects_a_readiness_snapshot_from_another_persona(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())
    snapshot = catalog.build_capability_catalog(source_rows=_source_rows(tmp_path))
    wrong = dataclasses.replace(_readiness(), persona_id="persona-b")

    with pytest.raises(catalog.CapabilityCatalogError, match="different persona"):
        catalog.build_persona_capability_state(
            "persona-a",
            catalog=snapshot,
            profile_config={"tools": [], "toolsets": []},
            readiness_snapshot=wrong,
            plugin_views=(_plugin_view(),),
            configured_integration_ids=(),
            configuration_requirement_statuses=(),
        )


def test_operator_serialization_redacts_uri_userinfo_all_drive_unc_and_prefixed_ids() -> None:
    description = (
        "https://alice:synthetic-pass@example.invalid "
        "D:\\synthetic-private\\config.yaml "
        "\\\\fileserver\\profiles\\synthetic\\token.txt "
        "discord_channel_999999999999999999"
    )
    entry = tool_registry.ToolEntry(
        name="piv_redaction_probe",
        description=description,
        schema=_schema("piv_redaction_probe"),
        handler=_handler,
        toolset="safe_core",
        effect="read",
    )

    serialized = json.dumps(catalog.collect_tool_descriptors((entry,))[0].as_dict())
    assert "alice" not in serialized
    assert "synthetic-pass" not in serialized
    assert "D:\\\\synthetic-private" not in serialized
    assert "fileserver" not in serialized
    assert "999999999999999999" not in serialized


def test_management_catalog_uses_unfenced_skill_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime import framework_registry

    empty = FrameworkRegistry(project_root=tmp_path, skills=(), mcp_servers=())
    calls: list[bool] = []
    monkeypatch.setattr(
        framework_registry,
        "discover_framework_registry",
        lambda *_args, **_kwargs: empty,
    )

    def discover(_root: Path, *, fenced: bool = True) -> list[SkillEntry]:
        calls.append(fenced)
        return [SkillEntry("persona_private", "Private promoted skill", "private/SKILL.md")]

    monkeypatch.setattr(framework_registry, "discover_skills", discover)

    descriptors = catalog.collect_framework_descriptors(project_root=tmp_path)

    assert calls == [False]
    assert [item.id for item in descriptors] == ["skill.persona_private"]


def test_hostile_exception_string_cannot_escape_partial_catalog_collection(
    tmp_path: Path,
) -> None:
    class BadTextError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("secondary string failure")

    class BadMap(dict):
        def items(self):
            raise BadTextError()

    rows = _source_rows(tmp_path)
    rows["toolsets"] = BadMap()

    snapshot = catalog.build_capability_catalog(source_rows=rows)

    assert snapshot.status == "partial"
    assert snapshot.errors[0].source == "toolsets"
    assert "unprintable-BadTextError" in snapshot.errors[0].detail


def test_readiness_adapter_never_reports_an_explicitly_disabled_state_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(
        tmp_path,
        monkeypatch,
        disabled=(f"tool.{TOOL_NAME}",),
    )

    row = next(
        item
        for item in readiness.capability_readiness_from_projection(projection)
        if item.id == f"tool.{TOOL_NAME}"
    )
    assert row.status == "BLOCKED"
    assert "explicitly disables" in row.reasons[0]


def test_cursor_uses_the_same_casefolded_order_as_catalog_sorting(tmp_path: Path) -> None:
    entries = (_entry("a"), _entry("Z"))
    snapshot = catalog.build_capability_catalog(
        source_rows=_source_rows(tmp_path, entries=entries)
    )
    first = catalog.query_capabilities(
        snapshot,
        catalog.CapabilityCatalogQuery(
            kinds=(catalog.CapabilityKind.TOOL,),
            limit=1,
        ),
    )
    second = catalog.query_capabilities(
        snapshot,
        catalog.CapabilityCatalogQuery(
            kinds=(catalog.CapabilityKind.TOOL,),
            limit=1,
            cursor=first.next_cursor,
        ),
    )

    assert [item.id for item in first.items] == ["tool.a"]
    assert [item.id for item in second.items] == ["tool.Z"]


def test_projection_evaluates_each_state_once_before_linear_dependency_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = catalog._state

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog, "_state", counted)
    projection = _projection(tmp_path, monkeypatch)

    assert calls == len(projection.states)


def test_persona_tool_kill_switch_blocks_catalog_callable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_TOOLS", "disabled")

    state = _state_by_id(_projection(tmp_path, monkeypatch), f"tool.{TOOL_NAME}")

    assert state.callable is False
    assert state.enabled is False
    assert any(
        reason.code == "explicit_disabled" and "kill switch" in reason.detail
        for reason in state.blocked_reasons
    )


def test_plugin_requirements_are_evaluated_when_status_registry_has_no_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _plugin_view()
    monkeypatch.delenv("PLUGIN_API_TOKEN", raising=False)

    projection = _projection(
        tmp_path,
        monkeypatch,
        entries=(
            _entry(
                TOOL_NAME,
                plugin_id=view.id,
                plugin_version=view.version,
            ),
        ),
        config={"tools": [TOOL_NAME], "toolsets": []},
        plugin_view=view,
    )
    state = _state_by_id(projection, f"tool.{TOOL_NAME}")

    assert state.assigned is True
    assert state.configured is False
    assert state.enabled is False
    assert [reason.code for reason in state.blocked_reasons].count(
        "missing_configuration"
    ) == 1


def test_recursive_toolset_effect_includes_nested_execute_tools() -> None:
    tool_name = "piv_nested_execute_571"
    tool_registry.unregister_tool(tool_name)
    tool_registry.register_tool(
        tool_name,
        "Nested execute fixture",
        toolset="piv_nested_child_571",
        handler=_handler,
        effect="execute",
    )
    try:
        descriptors = catalog.collect_toolset_descriptors(
            {
                "piv_nested_child_571": {
                    "description": "child",
                    "tools": [tool_name],
                    "includes": [],
                },
                "piv_nested_parent_571": {
                    "description": "parent",
                    "tools": [],
                    "includes": ["piv_nested_child_571"],
                },
            }
        )
    finally:
        tool_registry.unregister_tool(tool_name)

    parent = next(item for item in descriptors if item.id == "toolset.piv_nested_parent_571")
    assert parent.effect_class == "execute"


def test_real_live_integration_toolset_reports_membership_and_empty_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from integrations import registry as integration_registry
    from runtime import toolsets

    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())
    rows = _source_rows(tmp_path)
    rows["toolsets"] = toolsets.TOOLSETS
    rows["integrations"] = integration_registry.get_all()
    snapshot = catalog.build_capability_catalog(source_rows=rows)
    descriptor = next(item for item in snapshot.items if item.id == "toolset.integrations")

    assert "integration.sheets" in descriptor.dependencies
    assert tool_registry.get_tool_definitions(enabled_toolsets=["integrations"]) == []

    projection = catalog.build_persona_capability_state(
        "persona-a",
        catalog=snapshot,
        profile_config={"tools": [], "toolsets": ["integrations"]},
        readiness_snapshot=_readiness(),
        plugin_views=(_plugin_view(),),
        configured_integration_ids=("sheets",),
        configuration_requirement_statuses=(),
    )
    state = _state_by_id(projection, "toolset.integrations")
    assert state.assigned is True
    assert state.callable is False
    assert state.enabled is False
    assert any(reason.code == "missing_handler" for reason in state.blocked_reasons)


def test_integration_requires_wrappers_for_every_assigned_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _readiness()
    missing_wrapper = dataclasses.replace(
        base.capabilities[0],
        id="sheets.info",
    )
    projection = _projection(
        tmp_path,
        monkeypatch,
        readiness_snapshot=dataclasses.replace(
            base,
            capabilities=(*base.capabilities, missing_wrapper),
        ),
    )

    state = _state_by_id(projection, "integration.sheets")
    assert state.assigned is True
    assert state.callable is False
    assert state.enabled is False
    assert any(reason.code == "missing_handler" for reason in state.blocked_reasons)


def test_operator_serializer_redacts_posix_paths_bearers_and_short_channel_ids(
    tmp_path: Path,
) -> None:
    rendered = catalog.safe_operator_text(
        "Authorization: Bearer synthetic-bearer /root/profile /opt/homie "
        "-1001234567890 C0123456789"
    )
    assert "synthetic-bearer" not in rendered
    assert "/root" not in rendered
    assert "/opt" not in rendered
    assert "-1001234567890" not in rendered
    assert "C0123456789" not in rendered

    framework = FrameworkRegistry(
        project_root=tmp_path,
        skills=(),
        mcp_servers=(
            McpServerEntry(
                name="safe_mcp",
                transport="http",
                config={
                    "url": "https://example.invalid/mcp",
                    "api_token_synthetic_secret": "redacted-value",
                },
                source="fixture",
                configured=True,
                callable=True,
            ),
        ),
    )
    serialized = json.dumps(catalog.collect_framework_descriptors(framework)[0].as_dict())
    assert "api_token_synthetic_secret" not in serialized
    assert "redacted-value" not in serialized


def test_default_persona_is_a_valid_projection_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())
    snapshot = catalog.build_capability_catalog(source_rows=_source_rows(tmp_path))

    projection = catalog.build_persona_capability_state(
        "default",
        catalog=snapshot,
        profile_config={"tools": [TOOL_NAME], "toolsets": []},
        readiness_snapshot=dataclasses.replace(_readiness(), persona_id="default"),
        plugin_views=(_plugin_view(),),
        configured_integration_ids=(),
        configuration_requirement_statuses=(),
    )

    assert projection.persona_id == "default"
    assert _state_by_id(projection, f"tool.{TOOL_NAME}").assigned is True


def test_catalog_read_does_not_initialize_or_mutate_the_tool_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime import persona_tools

    before_generation = tool_registry.get_generation()
    before_names = tuple(entry.name for entry in tool_registry.list_registered())

    def forbidden_registration(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("read model attempted tool registration")

    monkeypatch.setattr(persona_tools, "ensure_tools_registered", forbidden_registration)
    catalog.collect_tool_descriptors()

    assert tool_registry.get_generation() == before_generation
    assert tuple(entry.name for entry in tool_registry.list_registered()) == before_names


def test_profile_skill_assignment_uses_runtime_frontmatter_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills_root = tmp_path / "skills"
    skill_file = skills_root / "My-Useful-Skill" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: My Useful Skill\ndescription: Useful.\n---\n\n# Skill\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_assignment, "persona_skill_dir", lambda _persona: skills_root)
    rows = _source_rows(tmp_path)
    rows["framework"] = dataclasses.replace(
        _framework(tmp_path),
        skills=(SkillEntry("My Useful Skill", "Useful.", "skills/My-Useful-Skill/SKILL.md"),),
    )
    snapshot = catalog.build_capability_catalog(source_rows=rows)

    projection = catalog.build_persona_capability_state(
        "persona-a",
        catalog=snapshot,
        profile_config={"tools": [], "toolsets": []},
        readiness_snapshot=_readiness(),
        plugin_views=(_plugin_view(),),
        configured_integration_ids=(),
        configuration_requirement_statuses=(),
    )

    assert _state_by_id(projection, "skill.My Useful Skill").assigned is True


def test_typed_plugin_dependency_edges_block_physical_contributions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime import framework_registry

    plugin_id = "piv-dependency-plugin-571"
    version = "1.0.0"
    skill = SkillEntry("dependent-skill", "Depends on MCP.", "plugin/SKILL.md")
    server = McpServerEntry(
        "physical-mcp",
        "stdio",
        {"command": "python"},
        "fixture",
        configured=True,
        callable=True,
    )
    view = PluginInstanceView(
        id=plugin_id,
        version=version,
        source=ManifestSource.BUNDLED,
        desired_state=PluginDesiredState.ENABLED,
        effective_state=PluginEffectiveState.LOADED,
        lifecycle_state=PluginLifecycleState.LOADED,
        contribution_ids=("skill-graph-key", "mcp-graph-key"),
        contribution_inventory=(
            MappingProxyType(
                {
                    "contribution_id": "skill-graph-key",
                    "type": "skill",
                    "skill": skill.name,
                    "depends_on": ("mcp-graph-key",),
                }
            ),
            MappingProxyType(
                {
                    "contribution_id": "mcp-graph-key",
                    "type": "mcp_server",
                    "mcp_server": server.name,
                    "depends_on": (),
                }
            ),
        ),
        residual_contribution_ids=(),
        error_code="",
        detail="",
    )
    framework_registry.register_plugin_skill(
        skill,
        plugin_id=plugin_id,
        plugin_version=version,
        project_root=tmp_path,
    )
    framework_registry.register_plugin_mcp_server(
        server,
        plugin_id=plugin_id,
        plugin_version=version,
        project_root=tmp_path,
    )
    try:
        rows = _source_rows(tmp_path, plugin_view=view)
        rows["framework"] = dataclasses.replace(
            _framework(tmp_path),
            skills=(skill,),
            mcp_servers=(server,),
        )
        snapshot = catalog.build_capability_catalog(source_rows=rows)
    finally:
        framework_registry.unregister_plugin_skill(
            skill.name,
            plugin_id=plugin_id,
            plugin_version=version,
        )
        framework_registry.unregister_plugin_mcp_server(
            server.name,
            plugin_id=plugin_id,
            plugin_version=version,
        )
    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())
    projection = catalog.build_persona_capability_state(
        "persona-a",
        catalog=snapshot,
        profile_config={
            "tools": [],
            "toolsets": [],
            "capability_blueprint": {"skills": [skill.name]},
        },
        readiness_snapshot=_readiness(),
        plugin_views=(view,),
        configured_integration_ids=(),
        configuration_requirement_statuses=(),
    )

    state = _state_by_id(projection, "skill.dependent-skill")
    assert "mcp.physical-mcp" in state.descriptor.dependencies
    assert "mcp.mcp-graph-key" not in state.descriptor.dependencies
    assert _state_by_id(projection, "mcp.physical-mcp").assigned is False
    assert any(reason.code == "dependency_conflict" for reason in state.blocked_reasons)
    assert state.enabled is False


def test_plugin_discovery_failure_is_source_local_and_keeps_healthy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        catalog,
        "collect_plugin_instance_views",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("discovery failed")),
    )

    snapshot = catalog.build_capability_catalog()

    assert snapshot.status == "partial"
    assert any(error.source == "plugin_discovery" for error in snapshot.errors)
    assert any(item.kind is catalog.CapabilityKind.TOOL for item in snapshot.items)


def test_declared_plugin_rows_use_physical_owner_keys_without_ghosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_view = PluginInstanceView(
        id="disabled-plugin-571",
        version="1.0.0",
        source=ManifestSource.BUNDLED,
        desired_state=PluginDesiredState.DISABLED,
        effective_state=PluginEffectiveState.UNLOADED,
        lifecycle_state=PluginLifecycleState.DISCOVERED,
        contribution_ids=("declared-tool-graph-571",),
        contribution_inventory=(
            MappingProxyType(
                {
                    "contribution_id": "declared-tool-graph-571",
                    "type": "tool",
                    "tool": "declared-tool-physical-571",
                    "depends_on": (),
                }
            ),
        ),
        residual_contribution_ids=(),
        error_code="",
        detail="",
    )
    rows = _source_rows(tmp_path, plugin_view=disabled_view)
    visible = catalog.build_capability_catalog(source_rows=rows)
    declared = next(
        item for item in visible.items if item.id == "tool.declared-tool-physical-571"
    )
    assert all(item.id != "tool.declared-tool-graph-571" for item in visible.items)
    assert declared.effect_class == "unknown"

    orphan_rows = _source_rows(
        tmp_path,
        entries=(
            _entry(
                TOOL_NAME,
                plugin_id="orphan-plugin-571",
                plugin_version="1.0.0",
            ),
        ),
    )
    orphan_rows["plugins"] = ()
    snapshot = catalog.build_capability_catalog(source_rows=orphan_rows)
    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())
    projection = catalog.build_persona_capability_state(
        "persona-a",
        catalog=snapshot,
        profile_config={"tools": [TOOL_NAME], "toolsets": []},
        readiness_snapshot=_readiness(),
        plugin_views=(),
        configured_integration_ids=(),
        configuration_requirement_statuses=(),
    )
    orphan = _state_by_id(projection, f"tool.{TOOL_NAME}")
    assert orphan.available is False
    assert orphan.enabled is False
    assert any(reason.code == "unavailable_owner" for reason in orphan.blocked_reasons)


@pytest.mark.parametrize(
    "private_path",
    (
        '"C:\\Users\\Private User\\App Data\\settings.json"',
        "'\\\\fileserver\\Private Share\\Team Folder\\token.txt'",
        "/home/private user/project notes/config.yaml",
    ),
)
def test_operator_serialization_redacts_complete_spaced_private_paths(
    private_path: str,
) -> None:
    rendered = catalog.safe_operator_text(f"failed at {private_path}, retry is safe")

    assert "<private-path>" in rendered
    assert "Private User" not in rendered
    assert "Private Share" not in rendered
    assert "private user" not in rendered
