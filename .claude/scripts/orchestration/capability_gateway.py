"""Read-only Capability Gateway status collector."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any


def collect_capability_gateway_status(
    *,
    query: Any | None = None,
    persona_id: str | None = None,
    search: str = "",
    kinds: tuple[str, ...] = (),
    sources: tuple[str, ...] = (),
    limit: int = 50,
    cursor: str | None = None,
    plugin_views: Any | None = None,
) -> dict[str, Any]:
    """Return operator-safe runtime, toolset, integration, and policy status."""
    if query is None:
        from personas.capability_catalog import CapabilityCatalogQuery, CapabilityKind

        query = CapabilityCatalogQuery(
            search=search,
            kinds=tuple(CapabilityKind(value) for value in kinds),
            sources=sources,
            limit=limit,
            cursor=cursor,
        )
    capabilities, toolsets = _collect_capabilities_and_toolsets(
        query=query,
        persona_id=persona_id,
        plugin_views=plugin_views,
    )
    integrations = _collect_integrations()
    runtime = _collect_runtime()
    browserops = _collect_browserops()
    try:
        from buzz_status import read_buzz_status

        buzz = read_buzz_status()
    except Exception as exc:  # noqa: BLE001 - read-only gateway degrades.
        buzz = {
            "enabled": False,
            "state": "failed",
            "active_transport": "none",
            "last_error": _short_error(exc),
        }
    outbound_actions = [
        action
        for item in integrations["items"]
        for action in item["actions"]
        if action["effect"] in {"send", "external_post"}
    ]

    return {
        "status": "partial" if capabilities.get("status") != "ok" else "ok",
        "timestamp": _utc_timestamp(),
        "runtime": runtime,
        "capabilities": capabilities,
        "toolsets": toolsets,
        "integrations": integrations,
        "browserops": browserops,
        "collaboration": {
            "buzz": buzz,
            "buzz_approval_authority": False,
            "approval_surface": "homie_only",
        },
        "outbound_messaging": {
            "status": "policy_gated" if outbound_actions else "none_declared",
            "actions": outbound_actions,
            "requires_operator_confirmation": True,
        },
        "approval_policy": {
            "default_deny": True,
            "mutating_actions_require_operator_confirmation": True,
            "model_exposed_mutating_actions": [
                action
                for item in integrations["items"]
                for action in item["actions"]
                if action["is_mutating"] and "model" in action["exposures"]
            ],
            "dashboard_mode": "read_only",
        },
    }


def _collect_capabilities_and_toolsets(
    *,
    query: Any | None = None,
    persona_id: str | None = None,
    plugin_views: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from personas import capability_catalog

    views = None if plugin_views is None else tuple(plugin_views)
    safe_persona_id = (
        capability_catalog.validate_persona_reference(persona_id)
        if persona_id is not None
        else None
    )
    catalog_errors: list[dict[str, str]] = []
    catalog_payload: dict[str, Any]
    snapshot = None
    page = None
    try:
        snapshot = capability_catalog.build_capability_catalog(plugin_views=views)
        resolved_query = query or capability_catalog.CapabilityCatalogQuery()
        page = capability_catalog.query_capabilities(snapshot, resolved_query)
        normalized = [{**item.as_dict(), "available": True} for item in page.items]
        source_counts: dict[str, int] = {}
        for row in snapshot.items:
            source = row.source or "unknown"
            source_counts[source] = source_counts.get(source, 0) + 1
        catalog_errors.extend(error.as_dict() for error in snapshot.errors)
        catalog_payload = {
            "schema_version": snapshot.schema_version,
            "status": snapshot.status,
            "persona_id": safe_persona_id,
            "total_count": len(snapshot.items),
            "matched_count": page.matched_count,
            "available_count": len(snapshot.items),
            "enabled_count": None,
            "sources": source_counts,
            "items": normalized,
            "next_cursor": page.next_cursor,
            "errors": list(catalog_errors),
        }
        if safe_persona_id:
            try:
                projection_kwargs = {"catalog": snapshot}
                if views is not None:
                    projection_kwargs["plugin_views"] = views
                projection = capability_catalog.build_persona_capability_state(
                    safe_persona_id,
                    **projection_kwargs,
                )
            except capability_catalog.CapabilityCatalogQueryError:
                raise
            except Exception as exc:  # noqa: BLE001 - preserve the healthy page.
                detail = _short_error(exc)
                print(
                    "CAPABILITY_GATEWAY_PROJECTION_ERROR "
                    f"code=persona_projection_failed detail={detail}"
                )
                projection_error = {
                    "source": "persona_projection",
                    "code": "source_read_failed",
                    "detail": detail,
                }
                catalog_errors.append(projection_error)
                catalog_payload["status"] = "partial"
                catalog_payload["errors"] = list(catalog_errors)
                catalog_payload["enabled_count"] = 0
            else:
                states = {state.descriptor.id: state for state in projection.states}
                catalog_payload.update(
                    {
                        "status": projection.status,
                        "persona_id": projection.persona_id,
                        "available_count": sum(state.available for state in projection.states),
                        "enabled_count": sum(state.enabled for state in projection.states),
                        "items": [states[item.id].as_dict() for item in page.items],
                    }
                )
    except capability_catalog.CapabilityCatalogQueryError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalized catalog degrades alone.
        detail = _short_error(exc)
        print(
            "CAPABILITY_GATEWAY_CATALOG_ERROR "
            f"code=normalized_catalog_failed detail={detail}"
        )
        catalog_errors.append(
            {
                "source": "normalized_catalog",
                "code": "source_read_failed",
                "detail": detail,
            }
        )
        catalog_payload = {
            "schema_version": 1,
            "status": "partial",
            "persona_id": safe_persona_id,
            "total_count": 0,
            "matched_count": 0,
            "available_count": 0,
            "enabled_count": 0 if persona_id else None,
            "sources": {},
            "items": [],
            "next_cursor": None,
            "errors": list(catalog_errors),
        }

    legacy_errors: list[dict[str, str]] = []
    legacy_error_detail: str | None = None
    toolsets: list[dict[str, Any]] = []
    try:
        import integrations.registry  # noqa: F401
        import runtime.overlays  # noqa: F401
        from runtime import capabilities as runtime_capabilities
        from runtime import toolsets as runtime_toolsets

        legacy_rows = runtime_capabilities.list_capabilities(
            sources=["chat_extensions", "integrations", "runtime_overlays"]
        )
        legacy = [
            {
                "id": row.id,
                "display_name": row.display_name,
                "enabled": row.enabled,
                "source": row.source,
                "description": row.description,
            }
            for row in legacy_rows
        ]
        source_counts: dict[str, int] = {}
        for row in legacy:
            source = str(row.get("source") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
        toolsets = [
            {
                "name": name,
                "description": spec.get("description", ""),
                "capability_ids": runtime_capabilities.resolve_toolset(
                    name,
                    registry=runtime_toolsets.TOOLSETS,
                ),
            }
            for name, spec in runtime_toolsets.TOOLSETS.items()
        ]
        for item in toolsets:
            item["capability_count"] = len(item["capability_ids"])
    except Exception as exc:  # noqa: BLE001 - legacy envelope degrades alone.
        detail = _short_error(exc)
        legacy_error_detail = detail
        print(
            "CAPABILITY_GATEWAY_LEGACY_ERROR "
            f"code=legacy_capabilities_failed detail={detail}"
        )
        legacy = []
        source_counts = {}
        legacy_errors.append(
            {
                "source": "legacy_capabilities",
                "code": "source_read_failed",
                "detail": detail,
            }
        )

    all_errors = [*catalog_errors, *legacy_errors]
    status = (
        "partial"
        if catalog_payload["status"] != "ok" or legacy_errors
        else "ok"
    )
    capabilities_payload = {
            # Preserve the original Gateway envelope exactly for old clients.
            "total_count": len(legacy),
            "available_count": len(legacy),
            "enabled_count": sum(1 for row in legacy if row["enabled"]),
            "sources": source_counts,
            "items": legacy,
            "legacy_items": legacy,
            # New consumers adopt the bounded normalized projection here.
            "schema_version": catalog_payload["schema_version"],
            "status": status,
            "catalog": catalog_payload,
            "errors": all_errors,
        }
    if legacy_error_detail is not None:
        capabilities_payload["error"] = legacy_error_detail
    return capabilities_payload, toolsets


def _collect_integrations() -> dict[str, Any]:
    try:
        from integrations.capabilities import get_integration_actions
        from integrations.registry import get_all, get_enabled

        all_integrations = get_all()
        enabled = set(get_enabled().keys())
        items = []
        for name, info in all_integrations.items():
            actions = [
                _action_to_dict(action)
                for action in get_integration_actions(name)
            ]
            items.append(
                {
                    "id": name,
                    "display_name": info.display_name,
                    "auth_type": info.auth_type,
                    "enabled": name in enabled,
                    "action_count": len(actions),
                    "mutating_action_count": sum(1 for action in actions if action["is_mutating"]),
                    "actions": actions,
                }
            )
        return {
            "enabled_count": len(enabled),
            "total_count": len(items),
            "items": items,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled_count": 0,
            "total_count": 0,
            "items": [],
            "error": _short_error(exc),
        }


def _collect_runtime() -> dict[str, Any]:
    try:
        from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RUNTIME_LANE_GENERIC
        from runtime.model_control import (
            configured_runtime_models,
            runtime_model_warnings,
            selected_runtime_model,
        )
        from runtime.profiles import GENERIC_PROVIDER_REGISTRY
        from runtime.routing import GENERIC_TEXT_ROUTE, GENERIC_TOOL_ROUTE
        from runtime.selection import resolve_runtime_selection

        selection = resolve_runtime_selection()
        return {
            "selected_lane": selection.lane or "auto",
            "selected_generic_provider": selection.generic_provider,
            "selected_model": selected_runtime_model(selection),
            "configured_models": configured_runtime_models(),
            "model_warnings": runtime_model_warnings(selection),
            "lanes": [RUNTIME_LANE_CLAUDE_NATIVE, RUNTIME_LANE_GENERIC],
            "generic_providers": sorted(GENERIC_PROVIDER_REGISTRY.keys()),
            "generic_text_route": list(GENERIC_TEXT_ROUTE),
            "generic_tool_route": list(GENERIC_TOOL_ROUTE),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "selected_lane": "unknown",
            "selected_generic_provider": None,
            "selected_model": None,
            "configured_models": {},
            "model_warnings": [_short_error(exc)],
            "lanes": [],
            "generic_providers": [],
            "generic_text_route": [],
            "generic_tool_route": [],
        }


def _collect_browserops() -> dict[str, Any]:
    try:
        from browser_control import browser_readiness

        return browser_readiness()
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": False,
            "status": "attention",
            "reason": _short_error(exc),
        }


def _action_to_dict(action: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(action):
        raw = dataclasses.asdict(action)
    else:
        raw = dict(action)
    raw["id"] = getattr(action, "id", f"{raw.get('integration')}.{raw.get('action')}")
    raw["is_mutating"] = bool(getattr(action, "is_mutating", raw.get("effect") != "read"))
    raw["exposures"] = list(raw.get("exposures") or [])
    raw["required_scopes"] = list(raw.get("required_scopes") or [])
    raw["config_hints"] = list(raw.get("config_hints") or [])
    return raw


def _utc_timestamp() -> str:
    value = datetime.now(UTC).replace(microsecond=0).isoformat()
    return value[:-6] + "Z" if value.endswith("+00:00") else value


def _short_error(exc: Exception, *, max_chars: int = 220) -> str:
    from personas.capability_catalog import safe_operator_text

    try:
        rendered = str(exc)
    except Exception:
        rendered = f"<unprintable-{type(exc).__name__}>"
    return safe_operator_text(f"{type(exc).__name__}: {rendered}", max_chars)
