from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from cli import main
from personas.blueprints import BlueprintError
from personas.creation import (
    PersonaCreationSpec,
    apply_persona_creation,
    build_creation_blueprint,
    compile_creation_plan,
    get_creation_catalog,
    preview_persona_creation,
    read_provisioning_readiness,
)
from personas.provisioning import (
    ProvisionConflictError,
    ProvisionPaths,
)

CHANNEL_ID = "123456789012345678"


def _paths(tmp_path: Path) -> ProvisionPaths:
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "env_groups": {
                    "runtime_core": ["OPENAI_API_KEY"],
                    "vault_memory": ["HOMIE_VAULT_DIR"],
                    "business_profile": ["BUSINESS_EMAIL"],
                },
                "skill_groups": {},
                "profile_defaults": {
                    "env_groups": ["runtime_core", "vault_memory"],
                    "skill_groups": [],
                    "skills": [],
                },
                "profiles": {},
            }
        ),
        encoding="utf-8",
    )
    master_env = tmp_path / "master.env"
    master_env.write_text(
        "OPENAI_API_KEY=test-secret\n"
        "HOMIE_VAULT_DIR=C:/vault\n"
        "BUSINESS_EMAIL=ops@example.com\n",
        encoding="utf-8",
    )
    bindings = tmp_path / "discord-bindings.json"
    bindings.write_text(
        json.dumps({"guild_id": "test", "channels": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    return ProvisionPaths(
        homie_root=tmp_path / "homie",
        bindings_file=bindings,
        capability_matrix_file=matrix,
        master_env_file=master_env,
    )


@pytest.fixture
def creation_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_sinks: Path,
) -> ProvisionPaths:
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda *_args, **_kwargs: None,
    )
    # Issue #422: apply_persona_creation appends a persona-learning audit
    # row on create, resolved from config.DATA_DIR at CALL time. The shared
    # ``isolated_operator_sinks`` fixture (conftest.py) redirects that and
    # the kill-switch audit DB into the tmp tree, so this suite exercises
    # the real write paths without touching the checkout's live data dir.
    return paths


def _learning_audit_rows(persona_id: str) -> list[dict]:
    """Every persona-learning audit row for ``persona_id`` (call-time read)."""
    import config as _config

    ledger = Path(_config.DATA_DIR) / "persona_learning_audit.jsonl"
    if not ledger.is_file():
        return []
    rows = (json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines())
    return [row for row in rows if row["persona_id"] == persona_id]


def test_catalog_and_plan_are_deterministic_and_safe(
    creation_paths: ProvisionPaths,
) -> None:
    catalog = get_creation_catalog()
    assert [item["id"] for item in catalog] == [
        "general-specialist",
        "ai-engineer",
        "founder-operator",
    ]
    assert all(item["operator_exec_default"] is False for item in catalog)

    spec = PersonaCreationSpec(
        persona_id="surface-parity",
        template_id="ai-engineer",
        display_name="Surface Parity",
        role="Inspect repositories and propose bounded work.",
        model="claude-sonnet-4-7",
        domain="platform-engineering",
        discord_channel_id=CHANNEL_ID,
        discord_channel_name="surface-parity",
    )
    pure_plan = compile_creation_plan(spec)
    preview = preview_persona_creation(spec, paths=creation_paths)

    assert preview.plan.as_dict() == pure_plan.as_dict()
    assert preview.preview_hash == preview_persona_creation(
        spec, paths=creation_paths
    ).preview_hash
    assert pure_plan.applied_toolsets == ("safe_core", "ai_engineering")
    assert "operator_exec" not in pure_plan.applied_toolsets
    assert pure_plan.display_name == "Surface Parity"
    assert pure_plan.role == "Inspect repositories and propose bounded work."
    assert pure_plan.model == "claude-sonnet-4-7"
    assert pure_plan.domain == "platform-engineering"
    assert pure_plan.channels[0].channel_id == CHANNEL_ID


def test_apply_returns_typed_receipt_and_persists_every_surface_field(
    creation_paths: ProvisionPaths,
) -> None:
    spec = PersonaCreationSpec(
        persona_id="api-engineer",
        template_id="ai-engineer",
        display_name="API Engineer",
        role="Own API architecture and implementation.",
        model="claude-opus-4-7",
        domain="api-engineering",
        discord_channel_id=CHANNEL_ID,
    )
    preview = preview_persona_creation(spec, paths=creation_paths)
    receipt = apply_persona_creation(
        spec,
        actor="test-operator",
        expected_preview_hash=preview.preview_hash,
        expected_state_hash=preview.state_hash,
        paths=creation_paths,
    )

    assert receipt.outcome == "created"
    assert receipt.preview_hash == preview.preview_hash
    assert receipt.transaction_id
    assert receipt.receipt_path.endswith(".json")
    profile = creation_paths.profiles_root / "api-engineer"
    config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    blueprint = yaml.safe_load((profile / "blueprint.yaml").read_text(encoding="utf-8"))
    bindings = json.loads(creation_paths.bindings_file.read_text(encoding="utf-8"))

    assert config["persona"] == {
        "id": "api-engineer",
        "display_name": "API Engineer",
        "domain": "api-engineering",
        "role": "Own API architecture and implementation.",
    }
    assert config["model"]["preferred"] == "claude-opus-4-7"
    assert config["toolsets"] == ["safe_core", "ai_engineering"]
    assert blueprint["template"] == "ai-engineer"
    assert blueprint["persona"]["model"] == "claude-opus-4-7"
    assert blueprint["channels"][0]["channel_id"] == CHANNEL_ID
    assert bindings["channels"][CHANNEL_ID]["persona"] == "api-engineer"
    assert bindings["channels"][CHANNEL_ID]["enabled"] is False
    assert "test-secret" not in json.dumps(receipt.as_dict())

    # Issue #422 — this IS the shared seam behind `thehomie profile create`
    # (non-clone) and dashboard `POST /api/agents`: a persona compiled and
    # applied through `apply_persona_creation` must be born learning with
    # an audit row, exactly like personas.lifecycle.create_profile's clone
    # path already was.
    assert config["learning"]["enabled"] is True
    rows = _learning_audit_rows("api-engineer")
    assert len(rows) == 1
    assert rows[0]["enabled"] is True
    assert rows[0]["action"] == "enable"
    assert rows[0]["actor"] == "persona_creation_surface"

    # Reconcile round 2 — this is the shared function BOTH the CLI
    # non-clone door and the dashboard POST /api/agents door call, so
    # proving the tick's real admission check accepts its output here
    # covers the seam both adapters sit on top of.
    from persona_learning_tick import is_learning_eligible

    assert is_learning_eligible(config) is True


def test_stale_preview_refuses_without_creating_profile(
    creation_paths: ProvisionPaths,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    alias_root = tmp_path / "bin"
    monkeypatch.setenv("HOMIE_BIN_DIR", str(alias_root))
    spec = PersonaCreationSpec(persona_id="stale-preview")
    preview = preview_persona_creation(spec, paths=creation_paths)
    before = creation_paths.bindings_file.read_bytes()
    creation_paths.bindings_file.write_text(
        json.dumps({"guild_id": "changed", "channels": {}}) + "\n",
        encoding="utf-8",
    )
    changed = creation_paths.bindings_file.read_bytes()

    with pytest.raises(ProvisionConflictError):
        apply_persona_creation(
            spec,
            actor="test-operator",
            expected_preview_hash=preview.preview_hash,
            expected_state_hash=preview.state_hash,
            paths=creation_paths,
            create_alias=True,
        )

    assert changed != before
    assert creation_paths.bindings_file.read_bytes() == changed
    assert not (creation_paths.profiles_root / "stale-preview").exists()
    assert not alias_root.exists()


def test_operator_exec_and_hostile_fields_fail_closed(
    creation_paths: ProvisionPaths,
) -> None:
    default_plan = compile_creation_plan(
        PersonaCreationSpec(persona_id="safe-default")
    )
    elevated_plan = compile_creation_plan(
        PersonaCreationSpec(persona_id="explicit-exec", operator_exec=True)
    )
    assert "operator_exec" not in default_plan.applied_toolsets
    assert "operator_exec" in elevated_plan.applied_toolsets

    with pytest.raises(BlueprintError, match="operator_exec"):
        build_creation_blueprint(
            PersonaCreationSpec(
                persona_id="bad-bool",
                operator_exec="false",  # type: ignore[arg-type]
            )
        )
    with pytest.raises(BlueprintError, match="model"):
        compile_creation_plan(
            PersonaCreationSpec(persona_id="bad-model", model="model; rm -rf")
        )
    with pytest.raises(BlueprintError, match="channel_id"):
        preview_persona_creation(
            PersonaCreationSpec(
                persona_id="bad-channel",
                discord_channel_id="123abc",
            ),
            paths=creation_paths,
        )


def test_resolver_sentinel_is_refused_at_every_creation_door(
    creation_paths: ProvisionPaths,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Issue #422 round 3 — ``custom`` is a resolver sentinel, not a persona.

    ``get_persona_paths("custom")`` roots the profile at ``HOMIE_HOME``
    itself, so the atomic provisioner would stage
    ``profiles/custom/config.yaml`` while the learning tick keeps reading
    ``HOMIE_HOME/config.yaml``: create returns success and the newborn's
    born-learning config is invisible to the runtime that must honor it.
    Every creation door has to refuse BEFORE staging.
    """
    monkeypatch.setattr(
        ProvisionPaths,
        "defaults",
        classmethod(lambda cls: creation_paths),
    )
    homie_home = tmp_path / "operator-homie-home"
    homie_home.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(homie_home))

    spec = PersonaCreationSpec(persona_id="custom")

    # The pure compiler seam both atomic doors sit on.
    with pytest.raises(BlueprintError, match="sentinel"):
        compile_creation_plan(spec)

    # The dashboard door: POST /api/agents/preview and POST /api/agents call
    # these two functions verbatim.
    with pytest.raises(BlueprintError, match="sentinel"):
        preview_persona_creation(spec, paths=creation_paths)
    with pytest.raises(BlueprintError, match="sentinel"):
        apply_persona_creation(
            spec,
            actor="test-operator",
            paths=creation_paths,
        )

    # The CLI door: plain non-clone `thehomie profile create`.
    created = CliRunner().invoke(
        main,
        ["profile", "create", "custom", "--no-alias"],
    )
    assert created.exit_code == 1, created.output
    assert "sentinel" in created.output

    # No door staged anything.
    assert not (creation_paths.profiles_root / "custom").exists()

    # This is WHY: the tick's own read path resolves somewhere else entirely,
    # and no door was allowed to touch the operator's live config either.
    from personas.services import get_profile_config_path

    runtime_config = get_profile_config_path("custom")
    assert runtime_config == homie_home / "config.yaml"
    assert (
        runtime_config
        != creation_paths.profiles_root / "custom" / "config.yaml"
    )
    assert not runtime_config.exists()


def test_readiness_uses_physical_files_and_stays_scoped_to_provisioning(
    creation_paths: ProvisionPaths,
) -> None:
    spec = PersonaCreationSpec(persona_id="ready-persona")
    apply_persona_creation(spec, actor="test", paths=creation_paths)
    ready = read_provisioning_readiness("ready-persona", paths=creation_paths)
    assert ready["status"] == "PROVISIONED"
    assert ready["scope"] == "blueprint-provisioning"
    assert ready["six_axis_owner"] == "issue-301"

    (creation_paths.profiles_root / "ready-persona" / "config.yaml").unlink()
    partial = read_provisioning_readiness(
        "ready-persona",
        paths=creation_paths,
    )
    assert partial["status"] == "PARTIAL"
    assert partial["physical"]["config"] is False


def test_cli_blueprint_readiness_delegates_to_six_axis_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personas import readiness

    snapshot = readiness.build_persona_readiness_error_snapshot(
        "cli-engineer",
        "test readiness receipt",
    )
    monkeypatch.setattr(
        readiness,
        "build_persona_readiness_snapshot",
        lambda persona_id: snapshot,
    )

    result = CliRunner().invoke(
        main,
        ["profile", "blueprint", "readiness", "cli-engineer", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["persona_id"] == "cli-engineer"
    assert payload["schema_version"] == readiness.READINESS_SCHEMA_VERSION
    assert tuple(payload["axes"]) == readiness.AXIS_NAMES


def test_cli_blueprint_commands_and_profile_create_share_the_adapter(
    creation_paths: ProvisionPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ProvisionPaths,
        "defaults",
        classmethod(lambda cls: creation_paths),
    )
    runner = CliRunner()

    listed = runner.invoke(main, ["profile", "blueprint", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)[0]["id"] == "general-specialist"

    planned = runner.invoke(
        main,
        [
            "profile",
            "blueprint",
            "plan",
            "cli-engineer",
            "--template",
            "ai-engineer",
            "--display-name",
            "CLI Engineer",
            "--role",
            "Inspect CLI architecture.",
            "--model",
            "claude-sonnet-4-7",
            "--domain",
            "cli-engineering",
            "--channel",
            CHANNEL_ID,
            "--json",
        ],
    )
    assert planned.exit_code == 0, planned.output
    plan_payload = json.loads(planned.output)

    applied = runner.invoke(
        main,
        [
            "profile",
            "blueprint",
            "apply",
            "cli-engineer",
            "--template",
            "ai-engineer",
            "--display-name",
            "CLI Engineer",
            "--role",
            "Inspect CLI architecture.",
            "--model",
            "claude-sonnet-4-7",
            "--domain",
            "cli-engineering",
            "--channel",
            CHANNEL_ID,
            "--preview-hash",
            plan_payload["preview_hash"],
            "--state-hash",
            plan_payload["state_hash"],
            "--no-alias",
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.output)
    assert applied_payload["plan"] == plan_payload["plan"]

    created = runner.invoke(
        main,
        ["profile", "create", "safe-default-cli", "--no-alias"],
    )
    assert created.exit_code == 0, created.output
    default_config = yaml.safe_load(
        (
            creation_paths.profiles_root
            / "safe-default-cli"
            / "config.yaml"
        ).read_text(encoding="utf-8")
    )
    assert default_config["toolsets"] == ["safe_core"]
    assert default_config["persona"]["role"]
    assert default_config["model"]["preferred"]

    # Issue #422 R2 blocker — the ordinary non-clone `thehomie profile
    # create` door (this exact invocation) bypasses
    # personas.lifecycle.create_profile entirely and used to leave newborn
    # personas without learning enabled or an audit row.
    assert default_config["learning"]["enabled"] is True
    rows = _learning_audit_rows("safe-default-cli")
    assert len(rows) == 1
    assert rows[0]["enabled"] is True
    assert rows[0]["actor"] == "persona_creation_surface"

    # Reconcile round 2 — prove the tick's REAL admission check (not a
    # re-derived stand-in) accepts this exact newborn config.
    from persona_learning_tick import is_learning_eligible

    assert is_learning_eligible(default_config) is True
