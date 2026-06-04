"""Read-only Capability Gateway status collector."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any


def collect_capability_gateway_status() -> dict[str, Any]:
    """Return operator-safe runtime, toolset, integration, and policy status."""
    capabilities, toolsets = _collect_capabilities_and_toolsets()
    integrations = _collect_integrations()
    runtime = _collect_runtime()
    browserops = _collect_browserops()
    outbound_actions = [
        action
        for item in integrations["items"]
        for action in item["actions"]
        if action["effect"] in {"send", "external_post"}
    ]

    return {
        "status": "ok",
        "timestamp": _utc_timestamp(),
        "runtime": runtime,
        "capabilities": capabilities,
        "toolsets": toolsets,
        "integrations": integrations,
        "browserops": browserops,
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


def _collect_capabilities_and_toolsets() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        import integrations.registry  # noqa: F401
        import runtime.overlays  # noqa: F401
        from runtime.capabilities import list_capabilities, resolve_toolset
        from runtime.toolsets import TOOLSETS

        rows = list_capabilities(sources=["chat_extensions", "integrations", "runtime_overlays"])
        capabilities = [
            {
                "id": row.id,
                "display_name": row.display_name,
                "enabled": row.enabled,
                "source": row.source,
                "description": row.description,
            }
            for row in rows
        ]
        source_counts: dict[str, int] = {}
        for row in capabilities:
            source = str(row.get("source") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
        toolsets = [
            {
                "name": name,
                "description": spec.get("description", ""),
                "capability_ids": resolve_toolset(name, registry=TOOLSETS),
            }
            for name, spec in TOOLSETS.items()
        ]
        for item in toolsets:
            item["capability_count"] = len(item["capability_ids"])
        return (
            {
                "total_count": len(capabilities),
                "enabled_count": sum(1 for row in capabilities if row["enabled"]),
                "sources": source_counts,
                "items": capabilities,
            },
            toolsets,
        )
    except Exception as exc:  # noqa: BLE001 - gateway must degrade read-only.
        return (
            {
                "total_count": 0,
                "enabled_count": 0,
                "sources": {},
                "items": [],
                "error": _short_error(exc),
            },
            [],
        )


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
    value = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return value[:-6] + "Z" if value.endswith("+00:00") else value


def _short_error(exc: Exception, *, max_chars: int = 220) -> str:
    text = " ".join(str(exc).strip().split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."
