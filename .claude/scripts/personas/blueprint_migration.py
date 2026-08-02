"""Read-only, preserve-first persona blueprint migration analysis."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from personas.blueprints import (
    ProvisionMode,
    build_builtin_blueprint,
    compile_blueprint,
    parse_blueprint,
)
from personas.capabilities import (
    resolve_profile_capability_entry,
    resolve_skill_allowlist,
)
from personas.discord_bindings import load_binding_document, parse_bindings
from personas.services import (
    PersonaToolScope,
    resolve_persona_tool_scope,
    validate_config_yaml_text,
)
from runtime.capabilities import resolve_toolset
from runtime.persona_tools import ensure_tools_registered
from runtime.tool_registry import list_registered, resolve_toolset_closure
from runtime.toolsets import TOOLSETS


@dataclass(frozen=True)
class ScopeAnalysis:
    source: str
    explicit: bool
    toolsets: tuple[str, ...]
    individual_tools: tuple[str, ...]
    unknown_toolsets: tuple[str, ...]
    structural_names: tuple[str, ...]
    offered_names: tuple[str, ...]
    callable_names: tuple[str, ...]
    unregistered_names: tuple[str, ...]
    uncallable_names: tuple[str, ...]


@dataclass(frozen=True)
class MigrationPreview:
    persona_id: str
    template: str
    protected: bool
    current_scope: ScopeAnalysis
    preserved_scope: ScopeAnalysis
    recommended_scope: ScopeAnalysis
    exact_intent_preserved: bool
    offered_names_preserved: bool
    reconcile_added: tuple[str, ...]
    reconcile_removed: tuple[str, ...]
    env_group_drift: tuple[str, ...]
    skill_drift: tuple[str, ...]
    findings: tuple[str, ...]


@dataclass(frozen=True)
class ProfileMigrationInventory:
    profiles: tuple[MigrationPreview, ...]
    dangling_binding_personas: tuple[str, ...]
    mismatched_config_ids: tuple[str, ...]
    dangling_capability_rows: tuple[str, ...]
    errors: tuple[str, ...] = ()


def analyze_scope(
    config: dict[str, Any],
    *,
    callable_inventory: Collection[str] | None = None,
    registered_inventory: Mapping[str, tuple[str, bool]] | None = None,
) -> ScopeAnalysis:
    """Resolve intent, structure, offered tools, and callable handlers."""

    scope = resolve_persona_tool_scope(config)
    source = _scope_source(config, scope)
    unknown_toolsets = tuple(
        name for name in scope.toolsets if name not in TOOLSETS
    )
    structural: set[str] = set(scope.tools)
    for toolset in scope.toolsets:
        structural.update(resolve_toolset(toolset, registry=TOOLSETS))

    if registered_inventory is None:
        ensure_tools_registered()
        registered = {
            entry.name: (entry.toolset, entry.handler is not None)
            for entry in list_registered()
        }
    else:
        registered = dict(registered_inventory)
    callable_override = (
        {str(name).strip() for name in callable_inventory if str(name).strip()}
        if callable_inventory is not None
        else None
    )
    granted = resolve_toolset_closure(
        enabled_toolsets=list(scope.toolsets) or None,
        registry=TOOLSETS,
    )
    offered: set[str] = set()
    for toolset in scope.toolsets:
        for name in resolve_toolset(toolset, registry=TOOLSETS):
            entry = registered.get(name)
            if entry is not None and entry[0] in granted:
                offered.add(name)
    for name in scope.tools:
        if name in registered:
            offered.add(name)
    callable_names = {
        name
        for name in offered
        if (
            name in callable_override
            if callable_override is not None
            else registered[name][1]
        )
    }
    return ScopeAnalysis(
        source=source,
        explicit=source != "absent",
        toolsets=scope.toolsets,
        individual_tools=scope.tools,
        unknown_toolsets=unknown_toolsets,
        structural_names=tuple(sorted(structural)),
        offered_names=tuple(sorted(offered)),
        callable_names=tuple(sorted(callable_names)),
        unregistered_names=tuple(sorted(structural - set(registered))),
        uncallable_names=tuple(sorted(offered - callable_names)),
    )


def preview_existing_profile(
    persona_id: str,
    raw_blueprint: dict[str, Any],
    *,
    profile_root: str | Path,
    callable_inventory: Collection[str] | None = None,
    registered_inventory: Mapping[str, tuple[str, bool]] | None = None,
    current_env_groups: Collection[str] | None = None,
    current_skills: Collection[str] | None = None,
) -> MigrationPreview:
    """Compare preserved migration intent with an explicit reconcile offer."""

    root = Path(profile_root)
    config_path = root / "config.yaml"
    current = (
        validate_config_yaml_text(config_path.read_text(encoding="utf-8"))
        if config_path.is_file()
        else {}
    )
    migration = compile_blueprint(
        raw_blueprint,
        mode=ProvisionMode.MIGRATE,
        current_config=current,
        callable_tools=callable_inventory,
    )
    reconcile = compile_blueprint(
        raw_blueprint,
        mode=ProvisionMode.RECONCILE,
        current_config=current,
        callable_tools=callable_inventory,
    )
    current_scope = analyze_scope(
        current,
        callable_inventory=callable_inventory,
        registered_inventory=registered_inventory,
    )
    preserved_config = {
        "toolsets": list(migration.applied_toolsets),
        "tools": list(migration.applied_tools),
    }
    preserved_scope = analyze_scope(
        preserved_config,
        callable_inventory=callable_inventory,
        registered_inventory=registered_inventory,
    )
    recommended_config = {
        "toolsets": list(reconcile.applied_toolsets),
        "tools": list(reconcile.applied_tools),
    }
    recommended_scope = analyze_scope(
        recommended_config,
        callable_inventory=callable_inventory,
        registered_inventory=registered_inventory,
    )
    compiled = current.get("capability_blueprint")
    compiled_env = (
        tuple(compiled.get("env_groups", []))
        if isinstance(compiled, dict)
        and isinstance(compiled.get("env_groups", []), list)
        else ()
    )
    compiled_skills = (
        tuple(compiled.get("skills", []))
        if isinstance(compiled, dict)
        and isinstance(compiled.get("skills", []), list)
        else ()
    )
    effective_env = (
        tuple(current_env_groups)
        if current_env_groups is not None
        else compiled_env
    )
    effective_skills = (
        tuple(current_skills)
        if current_skills is not None
        else compiled_skills
    )
    env_drift = tuple(sorted(set(migration.env_groups) ^ set(effective_env)))
    skill_drift = tuple(sorted(set(migration.skills) ^ set(effective_skills)))
    findings: list[str] = []
    if current_scope.unknown_toolsets:
        findings.append("unknown_toolsets")
    if current_scope.unregistered_names:
        findings.append("unregistered_tools")
    if current_scope.uncallable_names:
        findings.append("uncallable_tools")
    if env_drift:
        findings.append("env_group_drift")
    if skill_drift:
        findings.append("skill_drift")
    if persona_id == "repo-scout":
        findings.append("protected_repo_scout")
    return MigrationPreview(
        persona_id=persona_id,
        template=migration.template,
        protected=persona_id == "repo-scout",
        current_scope=current_scope,
        preserved_scope=preserved_scope,
        recommended_scope=recommended_scope,
        exact_intent_preserved=(
            current_scope.toolsets == preserved_scope.toolsets
            and current_scope.individual_tools
            == preserved_scope.individual_tools
        ),
        offered_names_preserved=(
            current_scope.offered_names == preserved_scope.offered_names
        ),
        reconcile_added=tuple(
            sorted(
                set(recommended_scope.offered_names)
                - set(current_scope.offered_names)
            )
        ),
        reconcile_removed=tuple(
            sorted(
                set(current_scope.offered_names)
                - set(recommended_scope.offered_names)
            )
        ),
        env_group_drift=env_drift,
        skill_drift=skill_drift,
        findings=tuple(findings),
    )


def inventory_profile_migrations(
    profiles_root: str | Path,
    *,
    bindings_file: str | Path | None = None,
    capability_matrix_file: str | Path | None = None,
    callable_inventory: Collection[str] | None = None,
    registered_inventory: Mapping[str, tuple[str, bool]] | None = None,
) -> ProfileMigrationInventory:
    """Inventory profile metadata without writing any file."""

    root = Path(profiles_root)
    existing = {
        path.name for path in root.iterdir() if path.is_dir()
    } if root.is_dir() else set()
    previews: list[MigrationPreview] = []
    mismatched: list[str] = []
    errors: list[str] = []
    matrix: dict[str, Any] = {}
    if capability_matrix_file is not None:
        matrix_path = Path(capability_matrix_file)
        if matrix_path.is_file():
            loaded = yaml.safe_load(matrix_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                matrix = loaded
    for persona_id in sorted(existing):
        profile_root = root / persona_id
        config_path = profile_root / "config.yaml"
        try:
            config = (
                validate_config_yaml_text(
                    config_path.read_text(encoding="utf-8")
                )
                if config_path.is_file()
                else {}
            )
        except Exception as exc:
            errors.append(f"{persona_id}: invalid config: {exc}")
            continue
        if not config_path.is_file():
            errors.append(f"{persona_id}: missing config.yaml")
        configured_id = str(
            (config.get("persona") or {}).get("id") or ""
        ).strip()
        if configured_id and configured_id != persona_id:
            mismatched.append(persona_id)
        try:
            blueprint_path = profile_root / "blueprint.yaml"
            if blueprint_path.is_file():
                loaded_blueprint = (
                    yaml.safe_load(
                        blueprint_path.read_text(encoding="utf-8")
                    )
                    or {}
                )
                parsed = parse_blueprint(loaded_blueprint)
                if parsed.persona_id != persona_id:
                    raise ValueError(
                        "blueprint persona id does not match profile directory"
                    )
                blueprint = loaded_blueprint
            else:
                template = (
                    persona_id
                    if persona_id in {"ai-engineer", "founder-operator"}
                    else "general-specialist"
                )
                display_name = str(
                    (config.get("persona") or {}).get("display_name")
                    or persona_id.replace("-", " ").title()
                )
                blueprint = build_builtin_blueprint(
                    template,
                    persona_id=persona_id,
                    display_name=display_name,
                )
            matrix_entry = resolve_profile_capability_entry(
                matrix,
                persona_id,
                profile_config=config,
            )
            current_env = tuple(matrix_entry.get("env_groups", []))
            skill_allowlist = resolve_skill_allowlist(
                persona_id,
                matrix=matrix,
                profile_config=config,
            )
            current_skills = (
                ("*",)
                if skill_allowlist is None
                else tuple(sorted(skill_allowlist))
            )
            previews.append(
                preview_existing_profile(
                    persona_id,
                    blueprint,
                    profile_root=profile_root,
                    callable_inventory=callable_inventory,
                    registered_inventory=registered_inventory,
                    current_env_groups=current_env,
                    current_skills=current_skills,
                )
            )
        except Exception as exc:
            errors.append(f"{persona_id}: migration preview failed: {exc}")

    dangling_bindings: set[str] = set()
    if bindings_file is not None:
        document = load_binding_document(bindings_file, strict=True)
        for binding in parse_bindings(document).values():
            if (
                binding.kind == "persona"
                and binding.persona_id not in existing
            ):
                dangling_bindings.add(binding.persona_id)

    dangling_rows: set[str] = set()
    matrix_profiles = matrix.get("profiles", {})
    if isinstance(matrix_profiles, dict):
        dangling_rows.update(
            name
            for name in matrix_profiles
            if name not in existing and name != "default"
        )
    return ProfileMigrationInventory(
        profiles=tuple(previews),
        dangling_binding_personas=tuple(sorted(dangling_bindings)),
        mismatched_config_ids=tuple(sorted(mismatched)),
        dangling_capability_rows=tuple(sorted(dangling_rows)),
        errors=tuple(errors),
    )


def _scope_source(
    config: dict[str, Any],
    scope: PersonaToolScope,
) -> str:
    if "toolsets" in config or "tools" in config:
        return "top-level"
    if scope.used_deprecated_alias:
        return "cabinet-alias"
    return "absent"


__all__ = [
    "MigrationPreview",
    "ProfileMigrationInventory",
    "ScopeAnalysis",
    "analyze_scope",
    "inventory_profile_migrations",
    "preview_existing_profile",
]
