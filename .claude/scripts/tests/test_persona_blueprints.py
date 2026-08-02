"""Persona blueprint compiler and capability-class foundation."""

from __future__ import annotations

import copy

import pytest

from personas.blueprints import (
    BlueprintError,
    CapabilityClass,
    ProvisionMode,
    build_builtin_blueprint,
    compile_blueprint,
    parse_blueprint,
)
from runtime.capabilities import resolve_toolset
from runtime.toolsets import (
    _HOMIE_OPERATOR_EXEC_TOOLS,
    _HOMIE_SAFE_CORE_TOOLS,
    TOOLSETS,
)

DEV_CHANNEL = "123456789012345678"
BUSINESS_CHANNEL = "987654321098765432"


def test_ai_engineer_builtin_compiles_to_useful_safe_domain_pack():
    raw = build_builtin_blueprint("ai-engineer", channel_id=DEV_CHANNEL)

    plan = compile_blueprint(raw)

    assert plan.persona_id == "ai-engineer"
    assert plan.applied_toolsets == ("safe_core", "ai_engineering")
    assert "operator_exec" not in plan.applied_toolsets
    assert plan.capability_classes == (
        CapabilityClass.SAFE_CORE.value,
        CapabilityClass.DOMAIN_PACK.value,
        CapabilityClass.SCHEDULED_STUDY.value,
    )
    assert plan.channels[0].channel_id == DEV_CHANNEL
    assert plan.integration_requirements == ("sheets.read",)
    assert plan.proposal_authorities == ("mailbox.propose",)


def test_founder_operator_builtin_is_safe_and_has_no_scheduled_curriculum():
    raw = build_builtin_blueprint(
        "founder-operator",
        channel_id=BUSINESS_CHANNEL,
    )

    plan = compile_blueprint(raw)

    assert plan.applied_toolsets == ("safe_core", "founder_operations")
    assert plan.scheduled == ()
    assert CapabilityClass.OPERATOR_EXEC.value not in plan.capability_classes


def test_general_specialist_never_compiles_to_empty_scope():
    plan = compile_blueprint(
        build_builtin_blueprint(
            "general-specialist",
            persona_id="research-assistant",
        )
    )

    assert plan.applied_toolsets == ("safe_core",)
    assert plan.declared_tools


def test_operator_exec_requires_explicit_manifest_boolean():
    raw = build_builtin_blueprint("ai-engineer")
    raw["capabilities"]["operator_exec"] = True

    plan = compile_blueprint(raw)

    assert plan.applied_toolsets[-1] == "operator_exec"
    assert CapabilityClass.OPERATOR_EXEC.value in plan.capability_classes


def test_scheduled_authority_is_model_only_and_inherits_zero_chat_tools():
    plan = compile_blueprint(build_builtin_blueprint("ai-engineer"))

    assert len(plan.scheduled) == 1
    scheduled = plan.scheduled[0]
    assert scheduled.authority == "curriculum_study"
    assert scheduled.model_only is True
    assert scheduled.toolsets == ()
    assert scheduled.tools == ()


def test_migration_preserves_existing_scope_and_reports_recommendation():
    current = {
        "persona": {"id": "ai-engineer"},
        "toolsets": ["core", "repo"],
        "tools": ["custom_reader"],
    }

    plan = compile_blueprint(
        build_builtin_blueprint("ai-engineer"),
        mode=ProvisionMode.MIGRATE,
        current_config=current,
    )

    assert plan.preserved_existing_scope is True
    assert plan.applied_toolsets == ("core", "repo")
    assert plan.applied_tools == ("custom_reader",)
    assert plan.recommended_toolsets == ("safe_core", "ai_engineering")
    assert "existing tool scope preserved" in plan.warnings[0]


def test_migration_preserves_explicit_empty_scope_without_widening():
    plan = compile_blueprint(
        build_builtin_blueprint("ai-engineer"),
        mode="migrate",
        current_config={"toolsets": []},
    )

    assert plan.preserved_existing_scope is True
    assert plan.applied_toolsets == ()
    assert plan.recommended_toolsets == ("safe_core", "ai_engineering")


def test_migration_preserves_absent_scope_without_widening():
    plan = compile_blueprint(
        build_builtin_blueprint("ai-engineer"),
        mode="migrate",
        current_config={"persona": {"id": "ai-engineer"}},
    )

    assert plan.preserved_existing_scope is True
    assert plan.applied_toolsets == ()
    assert plan.applied_tools == ()
    assert plan.applied_declared_tools == ()
    assert plan.declared_tools
    assert plan.config_patch["toolsets"] == []
    assert plan.config_patch["tools"] == []


def test_migration_preserves_deprecated_cabinet_tools_as_individual_grants():
    plan = compile_blueprint(
        build_builtin_blueprint(
            "general-specialist",
            persona_id="legacy-specialist",
        ),
        mode="migrate",
        current_config={"cabinet": {"tools": ["Read", "Bash"]}},
    )

    assert plan.applied_toolsets == ()
    assert plan.applied_tools == ("Read", "Bash")
    assert plan.preserved_existing_scope is True


def test_reconcile_applies_blueprint_instead_of_legacy_scope():
    plan = compile_blueprint(
        build_builtin_blueprint("founder-operator"),
        mode="reconcile",
        current_config={"toolsets": ["core"]},
    )

    assert plan.preserved_existing_scope is False
    assert plan.applied_toolsets == ("safe_core", "founder_operations")
    assert plan.config_patch["tools"] == []
    assert plan.config_patch["capability_blueprint"]["env_groups"] == [
        "business_profile",
        "runtime_core",
        "vault_memory",
    ]


def test_callable_inventory_keeps_readiness_partial_when_handlers_are_missing():
    plan = compile_blueprint(
        build_builtin_blueprint("ai-engineer"),
        callable_tools={"memory_search", "skills_list"},
    )

    assert plan.callable_tools == ("memory_search", "skills_list")
    assert "firecrawl_scrape" in plan.missing_tools
    assert "gh_issue_view" in plan.missing_tools
    assert any("readiness must stay partial" in warning for warning in plan.warnings)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda raw: raw["capabilities"].update({"safe_core": False}),
            "safe_core must be true",
        ),
        (
            lambda raw: raw["capabilities"].update({"domain_packs": ["unknown"]}),
            "unknown domain pack",
        ),
        (
            lambda raw: raw["scheduled"].update({"authorities": ["shell"]}),
            "unknown scheduled authority",
        ),
        (
            lambda raw: raw["channels"].append(
                {"kind": "discord", "channel_id": "not-a-number", "name": "dev"}
            ),
            "digits only",
        ),
        (
            lambda raw: raw.update({"surprise": True}),
            "unknown field",
        ),
    ],
)
def test_blueprint_validation_fails_closed(mutator, message):
    raw = build_builtin_blueprint("ai-engineer")
    mutator(raw)

    with pytest.raises(BlueprintError, match=message):
        parse_blueprint(raw)


def test_parse_does_not_mutate_operator_document():
    raw = build_builtin_blueprint("ai-engineer", channel_id=DEV_CHANNEL)
    original = copy.deepcopy(raw)

    parse_blueprint(raw)

    assert raw == original


def test_safe_core_and_operator_exec_are_structurally_disjoint():
    safe = set(resolve_toolset("safe_core", TOOLSETS))
    operator = set(resolve_toolset("operator_exec", TOOLSETS))

    assert safe == set(_HOMIE_SAFE_CORE_TOOLS)
    assert set(_HOMIE_OPERATOR_EXEC_TOOLS).issubset(operator)
    assert safe.isdisjoint(_HOMIE_OPERATOR_EXEC_TOOLS)


@pytest.mark.parametrize("domain_pack", ["ai_engineering", "founder_operations"])
def test_new_domain_packs_never_inherit_operator_exec(domain_pack):
    resolved = set(resolve_toolset(domain_pack, TOOLSETS))

    assert resolved
    assert resolved.isdisjoint(_HOMIE_OPERATOR_EXEC_TOOLS)


def test_legacy_core_preserves_wide_effective_grant():
    resolved = set(resolve_toolset("core", TOOLSETS))

    assert resolved == set(_HOMIE_SAFE_CORE_TOOLS) | set(_HOMIE_OPERATOR_EXEC_TOOLS)
    assert {"terminal", "write_file", "memory_search"}.issubset(resolved)


def test_real_tool_owners_match_the_new_capability_classes():
    from runtime import tool_impl, tool_impl_exec, tool_impl_eyes

    safe_owners = {
        name: owner for name, owner, _description, _parameters, _handler in tool_impl._SPECS
    }
    exec_owners = {
        name: owner
        for name, owner, _description, _parameters, _handler, _effect in tool_impl_exec._SPECS
    }
    eye_owners = {
        name: owner for name, owner, _description, _parameters, _handler in tool_impl_eyes._SPECS
    }

    assert safe_owners["memory_search"] == "safe_core"
    assert safe_owners["read_file"] == "operator_exec"
    assert all(owner == "operator_exec" for owner in exec_owners.values())
    assert eye_owners["x_search"] == "research_read"
    assert eye_owners["browser_snapshot"] == "browser_read"
