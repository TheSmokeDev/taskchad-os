"""Canonical persona blueprint surface adapter.

CLI, FastAPI, Hono, and dashboard callers supply the same typed blueprint
intent. This module turns that intent into one strict blueprint, delegates
create/reconcile preview and apply to the atomic provisioner, and returns
JSON-safe typed receipts. It does not duplicate lifecycle, config,
channel-binding, or provisioning mutations.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Collection
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from personas.blueprints import (
    BUILTIN_TEMPLATES,
    BlueprintError,
    BlueprintPlan,
    ProvisionMode,
    build_builtin_blueprint,
    compile_blueprint,
    template_catalog,
)
from personas.core import validate_persona_name
from personas.provisioning import (
    ProvisionConflictError,
    ProvisionPaths,
    ProvisionPreview,
    ProvisionResult,
    apply_provision,
    preview_provision,
)
from personas.services import validate_config_yaml_text

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PersonaCreationSpec:
    """Operator intent shared verbatim by every creation surface."""

    persona_id: str
    template_id: str | None = None
    display_name: str | None = None
    role: str | None = None
    model: str | None = None
    domain: str | None = None
    discord_channel_id: str | None = None
    discord_channel_name: str | None = None
    operator_exec: bool = False


@dataclass(frozen=True)
class PersonaCreationPreview:
    """Read-only preview with both plan and physical-state CAS hashes."""

    persona_id: str
    preview_hash: str
    state_hash: str
    plan: BlueprintPlan
    changed_paths: tuple[str, ...]
    env_summary: dict[str, Any]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "preview_hash": self.preview_hash,
            "state_hash": self.state_hash,
            "plan": self.plan.as_dict(),
            "changed_paths": list(self.changed_paths),
            "env_summary": self.env_summary,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PersonaCreationReceipt:
    """Typed result returned by create and reconcile apply surfaces."""

    schema_version: int
    persona_id: str
    outcome: str
    preview_hash: str
    state_before_hash: str
    state_after_hash: str
    transaction_id: str
    profile_path: str
    receipt_path: str
    changed_paths: tuple[str, ...]
    alias_paths: tuple[str, ...]
    warnings: tuple[str, ...]
    plan: BlueprintPlan

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "persona_id": self.persona_id,
            "outcome": self.outcome,
            "preview_hash": self.preview_hash,
            "state_before_hash": self.state_before_hash,
            "state_after_hash": self.state_after_hash,
            "transaction_id": self.transaction_id,
            "profile_path": self.profile_path,
            "receipt_path": self.receipt_path,
            "changed_paths": list(self.changed_paths),
            "alias_paths": list(self.alias_paths),
            "warnings": list(self.warnings),
            "plan": self.plan.as_dict(),
        }


def get_creation_catalog() -> tuple[dict[str, Any], ...]:
    """Return the single catalog consumed by CLI, API, and dashboard."""

    return template_catalog()


def build_creation_blueprint(spec: PersonaCreationSpec) -> dict[str, Any]:
    """Normalize a surface spec into the strict compiler document."""

    if not isinstance(spec, PersonaCreationSpec):
        raise BlueprintError("creation spec must be PersonaCreationSpec")
    validate_persona_name(spec.persona_id)
    template_id = spec.template_id if spec.template_id is not None else "general-specialist"
    if not isinstance(template_id, str) or not template_id.strip():
        raise BlueprintError("template_id must be a non-empty string")
    template_id = template_id.strip()
    template = BUILTIN_TEMPLATES.get(template_id)
    if template is None:
        raise BlueprintError(
            f"unknown persona template {template_id!r}; "
            f"known: {', '.join(sorted(BUILTIN_TEMPLATES))}"
        )
    if not isinstance(spec.operator_exec, bool):
        raise BlueprintError("operator_exec must be a boolean")

    display_name = (
        spec.display_name
        if spec.display_name is not None
        else template.display_name
    )
    role = spec.role if spec.role is not None else template.default_role
    model = spec.model if spec.model is not None else template.default_model
    domain = spec.domain if spec.domain is not None else template.domain
    return build_builtin_blueprint(
        template_id,
        persona_id=spec.persona_id,
        display_name=display_name,
        role=role,
        model=model,
        domain=domain,
        channel_id=spec.discord_channel_id,
        channel_name=spec.discord_channel_name,
        operator_exec=spec.operator_exec,
    )


def compile_creation_plan(
    spec: PersonaCreationSpec,
    *,
    callable_tools: Collection[str] | None = None,
) -> BlueprintPlan:
    """Pure compiler entrypoint used for cross-surface parity checks."""

    return compile_blueprint(
        build_creation_blueprint(spec),
        mode=ProvisionMode.CREATE,
        callable_tools=callable_tools,
    )


def build_reconcile_blueprint(
    spec: PersonaCreationSpec,
    *,
    current_config: dict[str, Any],
) -> dict[str, Any]:
    """Build reviewed reconcile intent without erasing authored identity.

    Reconcile is intentionally stricter than create: the template and Discord
    channel must both be explicit. Omitted identity/model fields preserve the
    physical profile values rather than resetting a live persona to template
    defaults.
    """

    if not isinstance(spec, PersonaCreationSpec):
        raise BlueprintError("reconcile spec must be PersonaCreationSpec")
    if spec.template_id is None:
        raise BlueprintError("template_id is required for reconcile")
    if spec.discord_channel_id is None:
        raise BlueprintError("discord_channel_id is required for reconcile")
    if not isinstance(current_config, dict):
        raise BlueprintError("current profile config must be a mapping")

    current_persona = current_config.get("persona")
    if not isinstance(current_persona, dict):
        current_persona = {}
    configured_id = str(current_persona.get("id") or "").strip()
    if configured_id and configured_id != spec.persona_id:
        raise BlueprintError(
            f"physical config belongs to {configured_id!r}, not "
            f"{spec.persona_id!r}"
        )

    raw = build_creation_blueprint(spec)
    raw_persona = raw["persona"]
    for field_name in ("display_name", "role", "domain"):
        if getattr(spec, field_name) is not None:
            continue
        current_value = current_persona.get(field_name)
        if isinstance(current_value, str) and current_value.strip():
            raw_persona[field_name] = current_value
        elif field_name == "role":
            raw_persona.pop(field_name, None)
        else:
            raise BlueprintError(
                f"current profile persona.{field_name} is missing; "
                f"supply --{field_name.replace('_', '-')} explicitly "
                "instead of restoring a template default"
            )

    if spec.model is None:
        current_model = current_config.get("model")
        preferred = (
            current_model.get("preferred")
            if isinstance(current_model, dict)
            else None
        )
        if isinstance(preferred, str) and preferred.strip():
            raw_persona["model"] = preferred
        else:
            raw_persona.pop("model", None)
    return raw


def preview_persona_creation(
    spec: PersonaCreationSpec,
    *,
    paths: ProvisionPaths | None = None,
    callable_tools: Collection[str] | None = None,
) -> PersonaCreationPreview:
    """Read physical state and return a zero-write provisioning preview."""

    resolved_paths = paths if paths is not None else ProvisionPaths.defaults()
    preview = preview_provision(
        build_creation_blueprint(spec),
        mode=ProvisionMode.CREATE,
        paths=resolved_paths,
        callable_tools=callable_tools,
    )
    return _surface_preview(preview)


def preview_persona_reconcile(
    spec: PersonaCreationSpec,
    *,
    paths: ProvisionPaths | None = None,
    callable_tools: Collection[str] | None = None,
) -> PersonaCreationPreview:
    """Read a live profile and return a zero-write reconcile preview."""

    resolved_paths = paths if paths is not None else ProvisionPaths.defaults()
    current_config = _read_reconcile_config(resolved_paths, spec.persona_id)
    preview = preview_provision(
        build_reconcile_blueprint(spec, current_config=current_config),
        mode=ProvisionMode.RECONCILE,
        paths=resolved_paths,
        callable_tools=callable_tools,
    )
    return _surface_preview(preview)


def apply_persona_creation(
    spec: PersonaCreationSpec,
    *,
    actor: str,
    expected_preview_hash: str | None = None,
    expected_state_hash: str | None = None,
    paths: ProvisionPaths | None = None,
    callable_tools: Collection[str] | None = None,
    create_alias: bool = False,
    best_effort_alias: bool = False,
    registered_subcommands: Collection[str] | None = None,
    install_launchd: bool = False,
    install_systemd: bool = False,
) -> PersonaCreationReceipt:
    """Apply one create preview through the atomic provisioner.

    Wrapper aliases are created before the profile commit and removed if the
    commit refuses. Service-manager installation is intentionally excluded
    from this transaction because it starts external processes.
    """

    if (expected_preview_hash is None) != (expected_state_hash is None):
        raise BlueprintError(
            "expected_preview_hash and expected_state_hash must be supplied together"
        )
    _validate_hash(expected_preview_hash, "expected_preview_hash")
    _validate_hash(expected_state_hash, "expected_state_hash")
    clean_actor = _validated_actor(actor)
    if install_launchd or install_systemd:
        requested_services = ", ".join(
            name
            for enabled, name in (
                (install_launchd, "launchd"),
                (install_systemd, "systemd"),
            )
            if enabled
        )
        raise BlueprintError(
            f"{requested_services} service installation is not part of atomic "
            "blueprint creation; create the profile first"
        )
    if registered_subcommands is not None:
        validate_persona_name(
            spec.persona_id,
            registered_subcommands=frozenset(registered_subcommands),
        )

    resolved_paths = paths if paths is not None else ProvisionPaths.defaults()
    raw_blueprint = build_creation_blueprint(spec)
    preview = preview_provision(
        raw_blueprint,
        mode=ProvisionMode.CREATE,
        paths=resolved_paths,
        callable_tools=callable_tools,
    )
    plan_hash = expected_preview_hash or preview.plan_sha256
    state_hash = expected_state_hash or preview.state.token_sha256
    if plan_hash != preview.plan_sha256:
        raise ProvisionConflictError(
            "creation input no longer matches the expected preview hash"
        )
    if state_hash != preview.state.token_sha256:
        raise ProvisionConflictError(
            "physical state no longer matches the expected preview hash"
        )

    from security import kill_switches

    kill_switches.requireEnabled(
        "persona_mutation",
        caller="persona_creation_surface",
    )

    wrapper_paths: tuple[str, ...] = ()
    alias_created = False
    alias_warnings: list[str] = []
    if create_alias:
        from personas import wrappers

        try:
            created = wrappers.create_wrapper_alias(
                spec.persona_id,
                resolved_paths.profiles_root / spec.persona_id,
            )
            wrapper_paths = _created_wrapper_paths(created)
            alias_created = True
        except Exception as exc:
            wrappers.remove_wrapper_alias(spec.persona_id)
            if not best_effort_alias:
                raise
            receipt = (
                f"persona_creation alias_warning persona={spec.persona_id} "
                f"error={type(exc).__name__}: {exc}"
            )
            print(receipt, file=sys.stderr)
            alias_warnings.append(receipt)

    try:
        result = apply_provision(
            raw_blueprint,
            mode=ProvisionMode.CREATE,
            expected_plan_sha256=plan_hash,
            expected_state_sha256=state_hash,
            actor=clean_actor,
            paths=resolved_paths,
            callable_tools=callable_tools,
        )
    except Exception:
        if alias_created:
            from personas import wrappers

            wrappers.remove_wrapper_alias(spec.persona_id)
        raise

    return _surface_receipt(
        result,
        preview,
        paths=resolved_paths,
        alias_paths=wrapper_paths,
        extra_warnings=tuple(alias_warnings),
    )


def apply_persona_reconcile(
    spec: PersonaCreationSpec,
    *,
    actor: str,
    expected_preview_hash: str,
    expected_state_hash: str,
    reconcile_approved: bool = False,
    paths: ProvisionPaths | None = None,
    callable_tools: Collection[str] | None = None,
) -> PersonaCreationReceipt:
    """Apply one explicitly approved reconcile through the provisioner.

    No bot lifecycle, scheduler installation, provider call, or Discord send is
    part of this surface. Both hashes are mandatory so the reviewed plan and
    physical state are the exact inputs that reach the provisioner.
    """

    if reconcile_approved is not True:
        raise BlueprintError("reconcile requires explicit approval")
    if expected_preview_hash is None:
        raise BlueprintError("expected_preview_hash is required for reconcile")
    if expected_state_hash is None:
        raise BlueprintError("expected_state_hash is required for reconcile")
    _validate_hash(expected_preview_hash, "expected_preview_hash")
    _validate_hash(expected_state_hash, "expected_state_hash")
    clean_actor = _validated_actor(actor)

    from security import kill_switches

    kill_switches.requireEnabled(
        "persona_mutation",
        caller="persona_reconcile_surface",
    )

    resolved_paths = paths if paths is not None else ProvisionPaths.defaults()
    current_config = _read_reconcile_config(resolved_paths, spec.persona_id)
    raw_blueprint = build_reconcile_blueprint(
        spec,
        current_config=current_config,
    )
    preview = preview_provision(
        raw_blueprint,
        mode=ProvisionMode.RECONCILE,
        paths=resolved_paths,
        callable_tools=callable_tools,
    )
    if expected_preview_hash != preview.plan_sha256:
        raise ProvisionConflictError(
            "reconcile input no longer matches the expected preview hash"
        )
    if expected_state_hash != preview.state.token_sha256:
        raise ProvisionConflictError(
            "physical state no longer matches the expected preview hash"
        )

    result = apply_provision(
        raw_blueprint,
        mode=ProvisionMode.RECONCILE,
        expected_plan_sha256=expected_preview_hash,
        expected_state_sha256=expected_state_hash,
        actor=clean_actor,
        paths=resolved_paths,
        callable_tools=callable_tools,
        reconcile_approved=True,
    )
    return _surface_receipt(result, preview, paths=resolved_paths)


def resolve_callable_tool_inventory() -> tuple[str, ...]:
    """Return the live registered handler inventory at call time."""

    from runtime import persona_tools, tool_registry

    persona_tools.ensure_tools_registered()
    return tuple(
        sorted(
            entry.name
            for entry in tool_registry.list_registered()
            if entry.handler is not None
        )
    )


def read_provisioning_readiness(
    persona_id: str,
    *,
    paths: ProvisionPaths | None = None,
) -> dict[str, Any]:
    """Report physical blueprint-provisioning state without claiming #301 axes."""

    validate_persona_name(persona_id)
    resolved_paths = paths if paths is not None else ProvisionPaths.defaults()
    profile_root = resolved_paths.profiles_root / persona_id
    if not profile_root.is_dir():
        raise FileNotFoundError(f"profile {persona_id!r} does not exist")

    required = {
        "blueprint": profile_root / "blueprint.yaml",
        "config": profile_root / "config.yaml",
        "readiness_receipt": (
            profile_root / "data" / "persona-provisioning-readiness.json"
        ),
    }
    physical = {name: path.is_file() for name, path in required.items()}
    receipt: dict[str, Any] = {}
    receipt_path = required["readiness_receipt"]
    if receipt_path.is_file():
        try:
            loaded = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BlueprintError(f"invalid provisioning readiness receipt: {exc}") from exc
        if not isinstance(loaded, dict):
            raise BlueprintError("provisioning readiness receipt must be an object")
        if loaded.get("persona_id") != persona_id:
            raise BlueprintError("provisioning readiness receipt persona mismatch")
        receipt = loaded

    complete = all(physical.values())
    return {
        "schema_version": 1,
        "persona_id": persona_id,
        "status": "PROVISIONED" if complete else "PARTIAL",
        "physical": physical,
        "plan_sha256": str(receipt.get("plan_sha256") or ""),
        "missing_tools": [
            str(name)
            for name in receipt.get("missing_tools", [])
            if isinstance(name, str)
        ],
        "scheduled_model_only": receipt.get("scheduled_model_only")
        if isinstance(receipt.get("scheduled_model_only"), bool)
        else None,
        "scope": "blueprint-provisioning",
        "six_axis_owner": "issue-301",
    }


def _surface_preview(preview: ProvisionPreview) -> PersonaCreationPreview:
    return PersonaCreationPreview(
        persona_id=preview.plan.persona_id,
        preview_hash=preview.plan_sha256,
        state_hash=preview.state.token_sha256,
        plan=preview.plan,
        changed_paths=preview.changed_paths,
        env_summary=preview.env_summary,
        warnings=preview.warnings,
    )


def _surface_receipt(
    result: ProvisionResult,
    preview: ProvisionPreview,
    *,
    paths: ProvisionPaths,
    alias_paths: tuple[str, ...] = (),
    extra_warnings: tuple[str, ...] = (),
) -> PersonaCreationReceipt:
    return PersonaCreationReceipt(
        schema_version=1,
        persona_id=result.persona_id,
        outcome=result.outcome,
        preview_hash=result.plan_sha256,
        state_before_hash=result.state_before_sha256,
        state_after_hash=result.state_after_sha256,
        transaction_id=result.transaction_id,
        profile_path=str(paths.profiles_root / result.persona_id),
        receipt_path=result.receipt_path,
        changed_paths=result.changed_paths,
        alias_paths=alias_paths,
        warnings=tuple(preview.warnings) + extra_warnings,
        plan=preview.plan,
    )


def _read_reconcile_config(
    paths: ProvisionPaths,
    persona_id: str,
) -> dict[str, Any]:
    validate_persona_name(persona_id)
    profile_root = paths.profiles_root / persona_id
    if not profile_root.is_dir():
        raise FileNotFoundError(f"profile {persona_id!r} does not exist")
    config_path = profile_root / "config.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(
            f"profile {persona_id!r} has no physical config.yaml"
        )
    return validate_config_yaml_text(config_path.read_text(encoding="utf-8"))


def _validate_hash(value: str | None, field_name: str) -> None:
    if value is not None and (
        not isinstance(value, str) or _HASH_RE.fullmatch(value) is None
    ):
        raise BlueprintError(f"{field_name} must be a lowercase SHA-256 hex string")


def _validated_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise BlueprintError("actor must be a non-empty string")
    cleaned = actor.strip()
    if len(cleaned) > 128 or "\x00" in cleaned:
        raise BlueprintError("actor must be at most 128 characters without NUL")
    return cleaned


def _created_wrapper_paths(wrapper_paths: Any) -> tuple[str, ...]:
    created: list[str] = []
    for field in fields(wrapper_paths):
        value = getattr(wrapper_paths, field.name)
        if isinstance(value, Path):
            created.append(str(value))
    return tuple(created)


__all__ = [
    "PersonaCreationPreview",
    "PersonaCreationReceipt",
    "PersonaCreationSpec",
    "apply_persona_reconcile",
    "apply_persona_creation",
    "build_reconcile_blueprint",
    "build_creation_blueprint",
    "compile_creation_plan",
    "get_creation_catalog",
    "preview_persona_reconcile",
    "preview_persona_creation",
    "read_provisioning_readiness",
    "resolve_callable_tool_inventory",
]
