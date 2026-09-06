from __future__ import annotations

import pytest

from orchestration import capability_gateway
from personas import capability_catalog, readiness, services, skill_assignment
from runtime import capabilities as runtime_capabilities
from runtime.capability_plugin_manifest import ManifestSource
from runtime.capability_plugins import (
    PluginDesiredState,
    PluginEffectiveState,
    PluginInstanceView,
    PluginLifecycleState,
)


def test_gateway_adopts_normalized_catalog_and_preserves_legacy_envelope() -> None:
    payload = capability_gateway.collect_capability_gateway_status()

    capabilities = payload["capabilities"]
    assert payload["status"] == "ok"
    assert capabilities["status"] == "ok"
    assert capabilities["schema_version"] == 1
    assert capabilities["available_count"] == capabilities["total_count"]
    assert capabilities["items"]
    assert capabilities["legacy_items"]
    assert all(
        {"id", "display_name", "enabled", "source", "description"} <= item.keys()
        for item in capabilities["items"]
    )
    normalized = capabilities["catalog"]
    assert normalized["items"]
    assert len(normalized["items"]) <= capability_catalog.MAX_PAGE_SIZE
    assert all(
        item["id"].startswith(f"{item['kind']}.")
        and item["available"] is True
        for item in normalized["items"]
    )
    assert payload["approval_policy"]["dashboard_mode"] == "read_only"


def test_gateway_sources_the_bundled_disabled_plugin_without_loading_it() -> None:
    payload = capability_gateway.collect_capability_gateway_status(
        kinds=("plugin",),
        limit=100,
    )

    plugin = next(
        item
        for item in payload["capabilities"]["catalog"]["items"]
        if item["id"] == "plugin.homie.capability-fixture"
    )
    assert plugin["kind"] == "plugin"
    assert plugin["source"] == "bundled"
    assert plugin["version"] == "1.0.0"


def test_gateway_catalog_exception_is_partial_and_prints_receipt(
    monkeypatch,
    capsys,
) -> None:
    def fail_catalog():
        raise RuntimeError("catalog fixture failed")

    monkeypatch.setattr(
        capability_catalog,
        "build_capability_catalog",
        fail_catalog,
    )

    payload = capability_gateway.collect_capability_gateway_status()

    assert payload["status"] == "partial"
    assert payload["capabilities"]["status"] == "partial"
    assert payload["capabilities"]["items"]
    assert payload["capabilities"]["catalog"]["items"] == []
    assert payload["capabilities"]["errors"][0]["code"] == "source_read_failed"
    assert "CAPABILITY_GATEWAY_CATALOG_ERROR" in capsys.readouterr().out


def test_gateway_query_is_bounded_searchable_and_uses_persona_projection(
    monkeypatch,
) -> None:
    called: list[str] = []

    def project(persona_id, *, catalog):
        called.append(persona_id)
        states = tuple(
            capability_catalog.PersonaCapabilityState(
                descriptor=item,
                available=True,
                assigned=False,
                configured=True,
                callable=True,
                enabled=False,
                readiness_axes=(
                    ("declared", "BLOCKED"),
                    ("configured", "READY"),
                    ("transportable", "READY"),
                    ("callable", "READY"),
                    ("channel-bound", "READY"),
                    ("scheduler-safe", "NOT_APPLICABLE"),
                ),
                surface_states=(),
                blocked_reasons=(
                    capability_catalog.BlockedReason("unassigned", "not assigned"),
                ),
            )
            for item in catalog.items
        )
        return capability_catalog.PersonaCapabilityProjection(
            persona_id=persona_id,
            status="ok",
            states=states,
        )

    monkeypatch.setattr(capability_catalog, "build_persona_capability_state", project)
    payload = capability_gateway.collect_capability_gateway_status(
        query=capability_catalog.CapabilityCatalogQuery(
            search="capability fixture",
            limit=1,
        ),
        persona_id="persona-a",
    )

    normalized = payload["capabilities"]["catalog"]
    assert called == ["persona-a"]
    assert normalized["persona_id"] == "persona-a"
    assert normalized["matched_count"] == 1
    assert len(normalized["items"]) == 1
    assert normalized["items"][0]["id"] == "plugin.homie.capability-fixture"
    assert normalized["items"][0]["enabled"] is False


def test_legacy_failure_preserves_a_healthy_normalized_catalog(monkeypatch) -> None:
    def fail_legacy(*_args, **_kwargs):
        raise RuntimeError("legacy collector failed")

    monkeypatch.setattr(runtime_capabilities, "list_capabilities", fail_legacy)

    payload = capability_gateway.collect_capability_gateway_status()

    capabilities = payload["capabilities"]
    assert payload["status"] == "partial"
    assert capabilities["items"] == []
    assert capabilities["catalog"]["items"]
    assert capabilities["errors"][-1]["source"] == "legacy_capabilities"
    assert "legacy collector failed" in capabilities["error"]


def test_gateway_failure_receipt_redacts_secrets_paths_and_ids(monkeypatch) -> None:
    def fail_catalog():
        raise RuntimeError(
            "API_TOKEN=synthetic-secret "
            "https://alice:password@example.invalid "
            "D:\\private\\manifest.json "
            "discord_channel_999999999999999999"
        )

    monkeypatch.setattr(capability_catalog, "build_capability_catalog", fail_catalog)

    payload = capability_gateway.collect_capability_gateway_status()
    detail = payload["capabilities"]["catalog"]["errors"][0]["detail"]

    assert "synthetic-secret" not in detail
    assert "alice" not in detail
    assert "password" not in detail
    assert "D:\\private" not in detail
    assert "999999999999999999" not in detail


def test_gateway_consumes_injected_live_plugin_lifecycle_view(monkeypatch) -> None:
    plugin_id = "gateway-live-plugin-571"
    view = PluginInstanceView(
        id=plugin_id,
        version="1.0.0",
        source=ManifestSource.BUNDLED,
        desired_state=PluginDesiredState.ENABLED,
        effective_state=PluginEffectiveState.LOADED,
        lifecycle_state=PluginLifecycleState.LOADED,
        contribution_ids=("gateway-live-tool-571",),
        contribution_inventory=(
            {
                "contribution_id": "gateway-live-tool-571",
                "type": "tool",
                "tool": "gateway-live-tool-571",
                "depends_on": (),
            },
        ),
        residual_contribution_ids=(),
        error_code="",
        detail="",
    )
    axes = {name: readiness.AxisReadiness("READY") for name in readiness.AXIS_NAMES}
    surfaces = {
        name: readiness.SurfaceReadiness("READY", (), True)
        for name in readiness.SURFACE_NAMES
    }
    snapshot = readiness.PersonaReadinessSnapshot(
        schema_version=1,
        persona_id="persona-a",
        status="READY",
        selected_lane="generic",
        selected_providers=("test",),
        axes=axes,
        surfaces=surfaces,
        capabilities=(),
    )
    monkeypatch.setattr(
        services,
        "load_persona_config",
        lambda _persona: {"tools": ["gateway-live-tool-571"], "toolsets": []},
    )
    monkeypatch.setattr(
        readiness,
        "build_persona_readiness_snapshot",
        lambda _persona: snapshot,
    )
    monkeypatch.setattr(skill_assignment, "installed_skill_names", lambda _persona: ())

    payload = capability_gateway.collect_capability_gateway_status(
        kinds=("plugin",),
        limit=100,
        persona_id="persona-a",
        plugin_views=(view,),
    )

    plugin = next(
        item
        for item in payload["capabilities"]["catalog"]["items"]
        if item["id"] == f"plugin.{plugin_id}"
    )
    assert plugin["enabled"] is True
    assert not {
        "plugin_disabled",
        "plugin_degraded",
        "missing_handler",
    } & {reason["code"] for reason in plugin["blocked_reasons"]}


def test_projection_failure_preserves_healthy_catalog_page(monkeypatch) -> None:
    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("projection failed")

    monkeypatch.setattr(
        capability_catalog,
        "build_persona_capability_state",
        fail_projection,
    )

    payload = capability_gateway.collect_capability_gateway_status(
        search="capability fixture",
        kinds=("plugin",),
        limit=1,
        persona_id="persona-a",
    )
    normalized = payload["capabilities"]["catalog"]

    assert normalized["status"] == "partial"
    assert normalized["items"][0]["id"] == "plugin.homie.capability-fixture"
    assert normalized["errors"][-1]["source"] == "persona_projection"


def test_gateway_rejects_hostile_persona_id_without_echoing_it() -> None:
    hostile = "../../Authorization: Bearer synthetic-secret"

    with pytest.raises(capability_catalog.CapabilityCatalogQueryError) as error:
        capability_gateway.collect_capability_gateway_status(persona_id=hostile)

    assert hostile not in str(error.value)
    assert "synthetic-secret" not in str(error.value)
