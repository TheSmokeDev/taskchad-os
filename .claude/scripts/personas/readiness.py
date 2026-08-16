"""Read-only, six-axis readiness truth for compiled persona capabilities.

The provisioner writes a useful receipt, but that receipt is derived history.
This module deliberately ignores it and rebuilds readiness from physical
profile/config/channel state plus the live runtime registries on every call.
No provider is invoked and no credential value is serialized.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from personas.blueprints import ProvisionMode, compile_blueprint, parse_blueprint
from personas.core import validate_persona_name
from personas.discord_bindings import load_binding_document, parse_bindings
from personas.lifecycle import list_profiles, resolve_profile_root
from personas.services import (
    resolve_persona_tool_scope,
    validate_config_yaml_text,
)
from runtime.base import RuntimeRequest
from runtime.capabilities import TEXT_REASONING

READINESS_SCHEMA_VERSION = 1
AXIS_NAMES: tuple[str, ...] = (
    "declared",
    "transportable",
    "callable",
    "configured",
    "channel-bound",
    "scheduler-safe",
)
SURFACE_NAMES: tuple[str, ...] = (
    "discord",
    "direct_chat",
    "cabinet",
    "web",
    "scheduled",
)

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)"
    r"\s*[:=]\s*([^\s,;]+)"
)


class PersonaReadinessError(RuntimeError):
    """A physical persona state cannot produce a trustworthy snapshot."""


@dataclass(frozen=True)
class ReadinessPaths:
    profile_root: Path
    bindings_file: Path
    capability_matrix_file: Path
    master_env_file: Path

    @classmethod
    def defaults(cls, persona_id: str) -> ReadinessPaths:
        """Resolve every mutable/configured path at call time."""

        from personas.provisioning import ProvisionPaths

        provision_paths = ProvisionPaths.defaults()
        return cls(
            profile_root=resolve_profile_root(persona_id),
            bindings_file=provision_paths.bindings_file,
            capability_matrix_file=provision_paths.capability_matrix_file,
            master_env_file=provision_paths.master_env_file,
        )


@dataclass(frozen=True)
class AxisReadiness:
    status: str
    reasons: tuple[str, ...] = ()
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class SurfaceReadiness:
    status: str
    reasons: tuple[str, ...]
    caller_tools: bool


@dataclass(frozen=True)
class CapabilityReadiness:
    id: str
    kind: str
    status: str
    axes: dict[str, str]
    surfaces: dict[str, str]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PersonaReadinessSnapshot:
    schema_version: int
    persona_id: str
    status: str
    selected_lane: str
    selected_providers: tuple[str, ...]
    axes: dict[str, AxisReadiness]
    surfaces: dict[str, SurfaceReadiness]
    capabilities: tuple[CapabilityReadiness, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, credential-value-free representation."""

        return asdict(self)


def build_persona_readiness_snapshot(
    persona_id: str,
    *,
    paths: ReadinessPaths | None = None,
) -> PersonaReadinessSnapshot:
    """Rebuild one persona's readiness vector from current physical state."""

    return _build_persona_readiness_snapshot(
        persona_id,
        paths=paths,
        transport_snapshot=None,
    )


def _build_persona_readiness_snapshot(
    persona_id: str,
    *,
    paths: ReadinessPaths | None,
    transport_snapshot: tuple[AxisReadiness, str, tuple[str, ...]] | None,
) -> PersonaReadinessSnapshot:
    validate_persona_name(persona_id)
    resolved_paths = paths if paths is not None else ReadinessPaths.defaults(persona_id)
    profile_root = resolved_paths.profile_root
    if not profile_root.is_dir():
        raise PersonaReadinessError(
            f"physical profile directory does not exist for {persona_id!r}"
        )

    raw_blueprint = _load_blueprint(profile_root / "blueprint.yaml", persona_id)
    config = _load_config(profile_root / "config.yaml", persona_id)
    plan = compile_blueprint(
        raw_blueprint,
        mode=ProvisionMode.RECONCILE,
        current_config=config,
    )

    declared_axis, expected_names, actual_names = _declared_axis(plan, config)
    tool_rows, callable_axis = _tool_capabilities(
        expected_names=expected_names,
        actual_names=actual_names,
        config=config,
    )
    if transport_snapshot is None:
        transport_snapshot = _transport_axis()
    transport_axis, selected_lane, selected_providers = transport_snapshot
    integration_rows, configured_axis = _configuration_capabilities(
        persona_id=persona_id,
        profile_root=profile_root,
        config=config,
        plan=plan,
        paths=resolved_paths,
    )
    channel_axis, discord_surface = _channel_axis(
        persona_id=persona_id,
        profile_root=profile_root,
        expected_channels=plan.channels,
        bindings_file=resolved_paths.bindings_file,
    )
    scheduler_axis, scheduled_surface = _scheduler_axis(
        persona_id=persona_id,
        profile_root=profile_root,
        plan=plan,
        config=config,
    )

    axes = {
        "declared": declared_axis,
        "transportable": transport_axis,
        "callable": callable_axis,
        "configured": configured_axis,
        "channel-bound": channel_axis,
        "scheduler-safe": scheduler_axis,
    }
    surfaces = _surface_readiness(
        persona_id=persona_id,
        profile_root=profile_root,
        transport_axis=transport_axis,
        callable_axis=callable_axis,
        configured_axis=configured_axis,
        discord_surface=discord_surface,
        scheduled_surface=scheduled_surface,
    )
    capabilities = _finish_capability_rows(
        [*tool_rows, *integration_rows],
        axes=axes,
        surfaces=surfaces,
    )
    status = _aggregate_status(
        [axis.status for axis in axes.values()]
        + [surface.status for surface in surfaces.values()]
        + [capability.status for capability in capabilities]
    )
    return PersonaReadinessSnapshot(
        schema_version=READINESS_SCHEMA_VERSION,
        persona_id=persona_id,
        status=status,
        selected_lane=selected_lane,
        selected_providers=selected_providers,
        axes=axes,
        surfaces=surfaces,
        capabilities=capabilities,
    )


def collect_persona_readiness_inventory() -> dict[str, dict[str, Any]]:
    """Collect compiled named profiles only; keep per-profile errors visible."""

    inventory: dict[str, dict[str, Any]] = {}
    transport_snapshot: tuple[AxisReadiness, str, tuple[str, ...]] | None = None
    for profile in list_profiles():
        if profile.is_default:
            continue
        blueprint_path = profile.path / "blueprint.yaml"
        config_path = profile.path / "config.yaml"
        compiled = False
        try:
            if config_path.is_file():
                config = validate_config_yaml_text(
                    config_path.read_text(encoding="utf-8")
                )
                compiled = isinstance(config.get("capability_blueprint"), dict)
        except Exception:
            compiled = blueprint_path.is_file()
        if not blueprint_path.is_file() and not compiled:
            continue
        try:
            if transport_snapshot is None:
                transport_snapshot = _transport_axis()
            inventory[profile.name] = _build_persona_readiness_snapshot(
                profile.name,
                paths=None,
                transport_snapshot=transport_snapshot,
            ).as_dict()
        except Exception as exc:
            inventory[profile.name] = build_persona_readiness_error_snapshot(
                profile.name,
                str(exc),
            ).as_dict()
    return inventory


def render_persona_readiness(snapshot: Mapping[str, Any]) -> str:
    """Render a compact human view without flattening the readiness vector."""

    persona_id = str(snapshot.get("persona_id") or "unknown")
    lines = [
        f"{persona_id}: {str(snapshot.get('status') or 'ERROR').upper()}",
    ]
    axes = snapshot.get("axes")
    if isinstance(axes, Mapping):
        for axis_name in AXIS_NAMES:
            axis = axes.get(axis_name)
            if not isinstance(axis, Mapping):
                continue
            reasons = axis.get("reasons")
            reason = ""
            if isinstance(reasons, (list, tuple)) and reasons:
                reason = f" - {_safe_reason(str(reasons[0]))}"
            lines.append(
                f"  {axis_name}: {str(axis.get('status') or 'ERROR').upper()}{reason}"
            )
    surfaces = snapshot.get("surfaces")
    if isinstance(surfaces, Mapping):
        rendered_surfaces = [
            f"{name}={str((surfaces.get(name) or {}).get('status') or 'ERROR').upper()}"
            for name in SURFACE_NAMES
            if isinstance(surfaces.get(name), Mapping)
        ]
        if rendered_surfaces:
            lines.append("  surfaces: " + ", ".join(rendered_surfaces))
    gaps: list[str] = []
    capabilities = snapshot.get("capabilities")
    if isinstance(capabilities, (list, tuple)):
        for capability in capabilities:
            if not isinstance(capability, Mapping):
                continue
            if capability.get("status") == "READY":
                continue
            reasons = capability.get("reasons")
            if isinstance(reasons, (list, tuple)) and reasons:
                gaps.append(
                    f"{capability.get('id')}: {_safe_reason(str(reasons[0]))}"
                )
    for gap in gaps[:8]:
        lines.append(f"  gap: {gap}")
    if len(gaps) > 8:
        lines.append(f"  gap: ... {len(gaps) - 8} more")
    return "\n".join(lines)


def _load_blueprint(path: Path, persona_id: str) -> dict[str, Any]:
    if not path.is_file():
        raise PersonaReadinessError("physical blueprint.yaml is missing")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PersonaReadinessError(f"invalid blueprint.yaml: {exc}") from exc
    if not isinstance(raw, dict):
        raise PersonaReadinessError("blueprint.yaml top-level must be a mapping")
    parsed = parse_blueprint(raw)
    if parsed.persona_id != persona_id:
        raise PersonaReadinessError(
            "blueprint persona id does not match the physical profile directory"
        )
    return raw


def _load_config(path: Path, persona_id: str) -> dict[str, Any]:
    if not path.is_file():
        raise PersonaReadinessError("physical config.yaml is missing")
    try:
        config = validate_config_yaml_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PersonaReadinessError(f"invalid config.yaml: {exc}") from exc
    configured_id = str((config.get("persona") or {}).get("id") or "").strip()
    if configured_id != persona_id:
        raise PersonaReadinessError(
            "config persona id does not match the physical profile directory"
        )
    return config


def _declared_axis(
    plan: Any,
    config: dict[str, Any],
) -> tuple[AxisReadiness, set[str], set[str]]:
    from personas import toolset_grants as _toolset_grants  # noqa: PLC0415 — late, Rule 3
    from runtime import capabilities as runtime_capabilities
    from runtime import toolsets as runtime_toolsets

    scope = resolve_persona_tool_scope(config)
    actual_names = set(scope.tools)
    unknown_toolsets: list[str] = []
    for toolset in scope.toolsets:
        if toolset not in runtime_toolsets.TOOLSETS:
            unknown_toolsets.append(toolset)
        actual_names.update(
            runtime_capabilities.resolve_toolset(
                toolset,
                registry=runtime_toolsets.TOOLSETS,
            )
        )
    expected_names = set(plan.applied_declared_tools)
    # Ledger-granted single tools (#465 1c) are legitimate reach, not
    # blueprint drift: an operator turn put them in `tools:` through the
    # grant executor, and the compiled plan predates that turn. They join the
    # EXPECTED side here so readiness stays READY over an honest grant.
    persona_id = str((config.get("persona") or {}).get("id") or "").strip()
    ledger_tools = (
        set(_toolset_grants.active_tool_grants(persona_id)) if persona_id else set()
    )
    expected_names |= ledger_tools
    reasons: list[str] = []
    if tuple(scope.toolsets) != tuple(plan.applied_toolsets):
        reasons.append(
            "physical config toolsets do not match the compiled blueprint "
            f"(expected {list(plan.applied_toolsets)}, found {list(scope.toolsets)})"
        )
    expected_tools = set(plan.applied_tools) | ledger_tools
    # Set compare, not tuple: the written order carries no meaning here, and
    # the ledger union is sorted while the config keeps grant order.
    if set(scope.tools) != expected_tools:
        reasons.append(
            "physical individual-tool grants do not match the compiled blueprint"
        )
    expected_metadata = plan.config_patch.get("capability_blueprint")
    if config.get("capability_blueprint") != expected_metadata:
        reasons.append(
            "physical capability_blueprint metadata does not match blueprint.yaml"
        )
    if unknown_toolsets:
        reasons.append(
            "physical config declares unknown toolsets: "
            + ", ".join(sorted(unknown_toolsets))
        )
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    if missing:
        reasons.append("compiled tools absent from runtime config: " + ", ".join(missing))
    if extra:
        reasons.append("runtime config has undeclared tools: " + ", ".join(extra))
    return (
        AxisReadiness(
            status="READY" if not reasons else "PARTIAL",
            reasons=tuple(reasons),
            evidence={
                "expected_toolsets": list(plan.applied_toolsets),
                "physical_toolsets": list(scope.toolsets),
                "expected_tool_count": len(expected_names),
                "physical_tool_count": len(actual_names),
            },
        ),
        expected_names,
        actual_names,
    )


def _tool_capabilities(
    *,
    expected_names: set[str],
    actual_names: set[str],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], AxisReadiness]:
    from runtime import persona_tools, tool_registry
    from runtime import toolsets as runtime_toolsets

    persona_tools.ensure_tools_registered()
    scope = resolve_persona_tool_scope(config)
    granted_toolsets = tool_registry.resolve_toolset_closure(
        enabled_toolsets=list(scope.toolsets) or None,
        registry=runtime_toolsets.TOOLSETS,
    )
    individual_grants = set(scope.tools)
    rows: list[dict[str, Any]] = []
    callable_count = 0
    gap_reasons: list[str] = []
    for name in sorted(expected_names | actual_names):
        reasons: list[str] = []
        declared_status = "READY"
        if name not in expected_names:
            declared_status = "PARTIAL"
            reasons.append(f"runtime config grants {name!r} outside blueprint intent")
        elif name not in actual_names:
            declared_status = "PARTIAL"
            reasons.append(f"compiled tool {name!r} is absent from runtime config")

        entry = tool_registry.get_entry(name)
        callable_status = "READY"
        if name not in actual_names:
            callable_status = "BLOCKED"
        elif entry is None:
            callable_status = "BLOCKED"
            reasons.append(f"declared tool {name!r} has no registered handler")
        elif (
            name not in individual_grants
            and entry.toolset not in granted_toolsets
        ):
            callable_status = "BLOCKED"
            reasons.append(
                f"declared tool {name!r} is owned by toolset "
                f"{entry.toolset!r}, outside the granted toolset closure"
            )
        elif entry.handler is None:
            callable_status = "BLOCKED"
            reasons.append(f"registered tool {name!r} has no callable handler")
        else:
            callable_count += 1

        gap_reasons.extend(reasons)
        rows.append(
            {
                "id": name,
                "kind": "tool",
                "declared": declared_status,
                "callable": callable_status,
                "configured": "NOT_APPLICABLE",
                "reasons": reasons,
            }
        )
    expected_count = len(expected_names)
    if expected_count == 0:
        status = "NOT_APPLICABLE"
    elif callable_count == expected_count:
        status = "READY"
    elif callable_count:
        status = "PARTIAL"
    else:
        status = "BLOCKED"
    return rows, AxisReadiness(
        status=status,
        reasons=tuple(dict.fromkeys(gap_reasons)),
        evidence={
            "expected_count": expected_count,
            "callable_count": callable_count,
            "registered_count": len(tool_registry.list_registered()),
            "granted_toolsets": sorted(granted_toolsets),
        },
    )


def _transport_axis() -> tuple[AxisReadiness, str, tuple[str, ...]]:
    from runtime import lane_router, tool_registry

    schema = tool_registry.build_tool_schema(
        "persona_readiness_probe",
        "Readiness-only caller-tool transport probe.",
    )
    request = RuntimeRequest(
        prompt="",
        # Transport is process-global, not persona- or invocation-relative.
        # Anchor resolution to the installed runtime package root so `doctor`
        # reports the same lane facts from a repo root, profile, or worktree.
        cwd=Path(__file__).resolve().parents[1],
        task_name="persona_readiness_probe",
        capability=TEXT_REASONING,
        tool_defs=[schema],
        allow_fallback=True,
    )
    try:
        probe = lane_router.probe_caller_tool_transport(request)
    except Exception as exc:
        reason = _safe_reason(f"selected runtime lane could not resolve: {exc}")
        return (
            AxisReadiness(status="BLOCKED", reasons=(reason,), evidence={}),
            "unknown",
            (),
        )

    candidates: list[dict[str, Any]] = []
    carrying = 0
    reasons: list[str] = []
    providers: list[str] = []
    for candidate in probe.candidates:
        provider = str(candidate.provider)
        providers.append(provider)
        carries = candidate.carries_caller_tools
        if candidate.error:
            reasons.append(
                _safe_reason(
                    f"provider {provider} caller-tool probe failed: "
                    f"{candidate.error}"
                )
            )
        if carries:
            carrying += 1
        else:
            reasons.append(
                f"provider {provider} cannot execute caller-supplied tool definitions"
            )
        candidates.append(
            {
                "provider": provider,
                "carries_caller_tools": carries,
            }
        )
    if not probe.candidates:
        reasons.append(
            f"selected lane {probe.lane} has no configured runtime profile"
        )
    if probe.candidates and carrying == len(probe.candidates):
        status = "READY"
    elif carrying:
        status = "PARTIAL"
    else:
        status = "BLOCKED"
    return (
        AxisReadiness(
            status=status,
            reasons=tuple(dict.fromkeys(reasons)),
            evidence={
                "selected_lane": probe.lane,
                "candidate_count": len(candidates),
                "carrying_count": carrying,
                "candidates": candidates,
            },
        ),
        probe.lane,
        tuple(dict.fromkeys(providers)),
    )


def _configuration_capabilities(
    *,
    persona_id: str,
    profile_root: Path,
    config: dict[str, Any],
    plan: Any,
    paths: ReadinessPaths,
) -> tuple[list[dict[str, Any]], AxisReadiness]:
    from integrations import registry as integration_registry
    from personas import capabilities as persona_capabilities
    from runtime import tool_registry
    from runtime import toolsets as runtime_toolsets

    env_plan = persona_capabilities.build_env_sync_plan(
        persona_id,
        matrix_path=paths.capability_matrix_file,
        master_env_path=paths.master_env_file,
        env_groups=plan.env_groups,
        profile_config=config,
        profile_env_path=profile_root / ".env",
    )
    profile_env = persona_capabilities.read_env_values(profile_root / ".env")
    profile_present = {key for key, value in profile_env.items() if value}
    all_integrations = integration_registry.get_all()
    enabled_integrations = integration_registry.get_enabled()
    scope = resolve_persona_tool_scope(config)
    granted_toolsets = tool_registry.resolve_toolset_closure(
        enabled_toolsets=list(scope.toolsets) or None,
        registry=runtime_toolsets.TOOLSETS,
    )
    individual_grants = set(scope.tools)
    rows: list[dict[str, Any]] = []
    configured_count = 0
    reasons: list[str] = []
    for requirement in plan.integration_requirements:
        integration_id, _, action = requirement.partition(".")
        info = all_integrations.get(integration_id)
        configuration_reasons: list[str] = []
        configured = False
        required_keys: list[str] = []
        if info is None:
            configuration_reasons.append(
                f"integration requirement {requirement!r} is not registered"
            )
        else:
            required_keys = list(info.required_config)
            missing_profile_keys = [
                key for key in required_keys if key not in profile_present
            ]
            missing_source_keys = [
                key for key in required_keys if key not in env_plan.present_keys
            ]
            if integration_id not in enabled_integrations:
                configuration_reasons.append(
                    f"direct integration {integration_id!r} is not configured"
                )
            if missing_source_keys:
                configuration_reasons.append(
                    "required env keys are absent from the configured source: "
                    + ", ".join(missing_source_keys)
                )
            if missing_profile_keys:
                configuration_reasons.append(
                    "required env keys are absent from the physical profile env: "
                    + ", ".join(missing_profile_keys)
                )
            configured = (
                integration_id in enabled_integrations
                and not missing_source_keys
                and not missing_profile_keys
            )
        if configured:
            configured_count += 1
        row_reasons = list(configuration_reasons)
        wrappers = tool_registry.list_registered_for_integration_action(requirement)
        callable_wrappers = [
            entry
            for entry in wrappers
            if entry.handler is not None
            and (
                entry.name in individual_grants
                or entry.toolset in granted_toolsets
            )
        ]
        if callable_wrappers:
            callable_status = "READY"
        else:
            callable_status = "PARTIAL"
            if not wrappers:
                row_reasons.append(
                    f"direct integration {integration_id!r} action {action!r} "
                    "has no persona caller-tool handler"
                )
            elif not any(entry.handler is not None for entry in wrappers):
                row_reasons.append(
                    f"direct integration {integration_id!r} action {action!r} "
                    "has registered caller-tool wrappers without callable handlers"
                )
            else:
                row_reasons.append(
                    f"direct integration {integration_id!r} action {action!r} "
                    "has caller-tool handlers outside the persona's resolved scope"
                )
        reasons.extend(configuration_reasons)
        rows.append(
            {
                "id": requirement,
                "kind": "integration",
                "declared": "READY",
                "callable": callable_status,
                "configured": "READY" if configured else "BLOCKED",
                "reasons": row_reasons,
                "required_env_keys": required_keys,
            }
        )
    required_count = len(plan.integration_requirements)
    if required_count == 0:
        status = "NOT_APPLICABLE"
    elif configured_count == required_count:
        status = "READY"
    elif configured_count:
        status = "PARTIAL"
    else:
        status = "BLOCKED"
    return rows, AxisReadiness(
        status=status,
        reasons=tuple(dict.fromkeys(reasons)),
        evidence={
            "requirements": list(plan.integration_requirements),
            "configured_count": configured_count,
            "delegated_env_keys": list(env_plan.allowed_keys),
            "source_present_env_keys": list(env_plan.present_keys),
            "source_missing_env_keys": list(env_plan.missing_keys),
            "profile_present_env_keys": sorted(profile_present),
            "profile_missing_env_keys": sorted(
                set(env_plan.allowed_keys) - profile_present
            ),
        },
    )


def _channel_axis(
    *,
    persona_id: str,
    profile_root: Path,
    expected_channels: tuple[Any, ...],
    bindings_file: Path,
) -> tuple[AxisReadiness, SurfaceReadiness]:
    reasons: list[str] = []
    ready_count = 0
    expected_discord = [
        channel for channel in expected_channels if channel.kind == "discord"
    ]
    try:
        bindings = parse_bindings(
            load_binding_document(bindings_file, strict=True)
        )
    except Exception as exc:
        reason = _safe_reason(f"Discord binding document is invalid: {exc}")
        return (
            AxisReadiness(status="BLOCKED", reasons=(reason,), evidence={}),
            SurfaceReadiness(
                status="BLOCKED",
                reasons=(reason,),
                caller_tools=True,
            ),
        )

    evidence_rows: list[dict[str, Any]] = []
    for channel in expected_discord:
        binding = bindings.get(channel.channel_id)
        channel_reasons: list[str] = []
        if binding is None:
            channel_reasons.append(
                f"Discord channel {channel.channel_id} has no physical binding"
            )
        elif binding.kind != "persona":
            channel_reasons.append(
                f"Discord channel {channel.channel_id} is not a persona binding"
            )
        elif binding.persona_id != persona_id:
            channel_reasons.append(
                f"Discord channel {channel.channel_id} resolves to "
                f"{binding.persona_id!r}, not {persona_id!r}"
            )
        elif not binding.enabled:
            channel_reasons.append(
                f"Discord channel {channel.channel_id} binding is disabled"
            )
        elif not profile_root.is_dir():
            channel_reasons.append("bound physical profile directory is missing")
        else:
            ready_count += 1
        reasons.extend(channel_reasons)
        evidence_rows.append(
            {
                "kind": "discord",
                "channel_id": channel.channel_id,
                "bound": not channel_reasons,
            }
        )
    expected_count = len(expected_discord)
    if expected_count == 0:
        axis_status = "NOT_APPLICABLE"
        surface_status = "NOT_APPLICABLE"
        surface_reasons = ("blueprint declares no Discord channel",)
    elif ready_count == expected_count:
        axis_status = "READY"
        surface_status = "READY"
        surface_reasons = ()
    elif ready_count:
        axis_status = "PARTIAL"
        surface_status = "PARTIAL"
        surface_reasons = tuple(dict.fromkeys(reasons))
    else:
        axis_status = "BLOCKED"
        surface_status = "BLOCKED"
        surface_reasons = tuple(dict.fromkeys(reasons))
    return (
        AxisReadiness(
            status=axis_status,
            reasons=tuple(dict.fromkeys(reasons)),
            evidence={
                "expected_count": expected_count,
                "bound_count": ready_count,
                "bindings": evidence_rows,
            },
        ),
        SurfaceReadiness(
            status=surface_status,
            reasons=surface_reasons,
            caller_tools=True,
        ),
    )


def _scheduler_axis(
    *,
    persona_id: str,
    profile_root: Path,
    plan: Any,
    config: dict[str, Any],
) -> tuple[AxisReadiness, SurfaceReadiness]:
    from curriculum import model_runtime
    from runtime import base as runtime_base

    runtime_contracts = model_runtime.get_scheduled_runtime_contracts()
    expected = tuple(item.authority for item in plan.scheduled)
    compiled = config.get("capability_blueprint")
    physical = tuple(
        compiled.get("scheduled_authorities", [])
        if isinstance(compiled, dict)
        and isinstance(compiled.get("scheduled_authorities", []), list)
        else ()
    )
    curriculum = config.get("curriculum")
    curriculum_enabled = bool(
        curriculum.get("enabled")
        if isinstance(curriculum, dict)
        else False
    )
    reasons: list[str] = []
    contracts: list[dict[str, Any]] = []
    if physical != expected:
        reasons.append(
            "physical scheduled_authorities do not match blueprint intent "
            f"(expected {list(expected)}, found {list(physical)})"
        )
    for authority in expected:
        secure_request = runtime_contracts.get(authority)
        if secure_request is None:
            reasons.append(
                f"scheduled authority {authority!r} has no registered "
                "zero-tool runtime contract"
            )
            contracts.append({"authority": authority, "model_only": False})
            continue
        probe = RuntimeRequest(
            prompt="",
            cwd=profile_root,
            task_name="persona_readiness_scheduler_probe",
            capability=TEXT_REASONING,
        )
        try:
            secured = secure_request(probe)
            runtime_base.assert_model_only_contract(secured)
            safe = True
        except Exception as exc:
            safe = False
            reasons.append(
                _safe_reason(
                    f"curriculum scheduled runtime contract failed: {exc}"
                )
            )
        if not safe:
            reasons.append(
                "curriculum scheduled runtime is not model_only with zero tools"
            )
        contracts.append(
            {
                "authority": authority,
                "model_only": safe,
                "enabled": curriculum_enabled,
            }
        )
    if curriculum_enabled and "curriculum_study" not in expected:
        reasons.append(
            "curriculum is physically enabled without compiled curriculum_study authority"
        )
    if not expected and not curriculum_enabled:
        status = "NOT_APPLICABLE"
        surface_status = "NOT_APPLICABLE"
        surface_reasons = ("blueprint declares no scheduled authority",)
    elif reasons:
        status = "BLOCKED"
        surface_status = "BLOCKED"
        surface_reasons = tuple(dict.fromkeys(reasons))
    else:
        status = "READY"
        surface_status = "READY"
        surface_reasons = (
            "scheduled authority is isolated to model_only with zero tools",
        )
    return (
        AxisReadiness(
            status=status,
            reasons=tuple(dict.fromkeys(reasons)),
            evidence={
                "expected_authorities": list(expected),
                "physical_authorities": list(physical),
                "contracts": contracts,
            },
        ),
        SurfaceReadiness(
            status=surface_status,
            reasons=surface_reasons,
            caller_tools=False,
        ),
    )


def _surface_readiness(
    *,
    persona_id: str,
    profile_root: Path,
    transport_axis: AxisReadiness,
    callable_axis: AxisReadiness,
    configured_axis: AxisReadiness,
    discord_surface: SurfaceReadiness,
    scheduled_surface: SurfaceReadiness,
) -> dict[str, SurfaceReadiness]:
    interactive_dependency_status = _aggregate_status(
        [
            transport_axis.status,
            callable_axis.status,
            configured_axis.status,
        ]
    )

    def interactive(
        *,
        binding_status: str,
        reasons: tuple[str, ...],
    ) -> SurfaceReadiness:
        if binding_status == "NOT_APPLICABLE":
            status = "NOT_APPLICABLE"
        elif (
            binding_status == "BLOCKED"
            or transport_axis.status == "BLOCKED"
            or callable_axis.status == "BLOCKED"
        ):
            status = "BLOCKED"
        else:
            status = _aggregate_status(
                [interactive_dependency_status, binding_status]
            )
        return SurfaceReadiness(
            status=status,
            reasons=tuple(
                dict.fromkeys(
                    [
                        *reasons,
                        *transport_axis.reasons,
                        *callable_axis.reasons,
                        *configured_axis.reasons,
                    ]
                )
            ),
            caller_tools=True,
        )

    profile_status = "READY" if profile_root.is_dir() else "BLOCKED"
    return {
        "discord": interactive(
            binding_status=discord_surface.status,
            reasons=discord_surface.reasons,
        ),
        "direct_chat": interactive(
            binding_status=profile_status,
            reasons=(
                ()
                if profile_status == "READY"
                else (f"physical persona profile {persona_id!r} is missing",)
            ),
        ),
        "cabinet": interactive(
            binding_status=profile_status,
            reasons=(
                "Cabinet text resolves the physical persona profile; "
                "Cabinet voice remains text-only",
            ),
        ),
        "web": SurfaceReadiness(
            status="NOT_APPLICABLE",
            reasons=(
                "web persona runtime is explicitly text-only and supplies no "
                "persona caller-tool definitions; caller-tool capabilities do "
                "not target this surface",
            ),
            caller_tools=False,
        ),
        "scheduled": scheduled_surface,
    }


def _finish_capability_rows(
    rows: list[dict[str, Any]],
    *,
    axes: dict[str, AxisReadiness],
    surfaces: dict[str, SurfaceReadiness],
) -> tuple[CapabilityReadiness, ...]:
    finished: list[CapabilityReadiness] = []
    for row in rows:
        capability_axes = {
            "declared": str(row["declared"]),
            "transportable": axes["transportable"].status,
            "callable": str(row["callable"]),
            "configured": str(row["configured"]),
            "channel-bound": axes["channel-bound"].status,
            # Scheduled authorities are model-only and zero-tool by contract.
            # Interactive caller-tool capabilities never target that surface.
            "scheduler-safe": "NOT_APPLICABLE",
        }
        capability_surfaces: dict[str, str] = {}
        for name, surface in surfaces.items():
            status = surface.status
            if name in {"web", "scheduled"}:
                status = "NOT_APPLICABLE"
            if (
                status == "READY"
                and (
                    row["callable"] != "READY"
                    or row["configured"] == "BLOCKED"
                )
            ):
                status = "PARTIAL"
            capability_surfaces[name] = status
        status = _aggregate_status(
            [
                *capability_axes.values(),
                *capability_surfaces.values(),
            ]
        )
        finished.append(
            CapabilityReadiness(
                id=str(row["id"]),
                kind=str(row["kind"]),
                status=status,
                axes=capability_axes,
                surfaces=capability_surfaces,
                reasons=tuple(dict.fromkeys(str(value) for value in row["reasons"])),
            )
        )
    return tuple(finished)


def _aggregate_status(statuses: list[str]) -> str:
    relevant = [
        status
        for status in statuses
        if status not in {"NOT_APPLICABLE", "READY"}
    ]
    if not relevant:
        return "READY"
    ready_count = sum(status == "READY" for status in statuses)
    if ready_count:
        return "PARTIAL"
    if all(status == "BLOCKED" for status in relevant):
        return "BLOCKED"
    return "PARTIAL"


def build_persona_readiness_error_snapshot(
    persona_id: str,
    reason: str,
) -> PersonaReadinessSnapshot:
    """Build the canonical secret-safe collector failure vector."""

    reason = _safe_reason(reason)
    axis = AxisReadiness(status="BLOCKED", reasons=(reason,), evidence={})
    return PersonaReadinessSnapshot(
        schema_version=READINESS_SCHEMA_VERSION,
        persona_id=persona_id,
        status="ERROR",
        selected_lane="unknown",
        selected_providers=(),
        axes={name: axis for name in AXIS_NAMES},
        surfaces={},
        capabilities=(),
    )


def _safe_reason(value: str) -> str:
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1=<redacted>", value)
    return " ".join(redacted.strip().split())[:500]


__all__ = [
    "AXIS_NAMES",
    "READINESS_SCHEMA_VERSION",
    "CapabilityReadiness",
    "PersonaReadinessError",
    "PersonaReadinessSnapshot",
    "ReadinessPaths",
    "SurfaceReadiness",
    "build_persona_readiness_snapshot",
    "build_persona_readiness_error_snapshot",
    "collect_persona_readiness_inventory",
    "render_persona_readiness",
]
