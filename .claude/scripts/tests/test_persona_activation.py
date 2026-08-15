from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import cli
from cli import main
from personas.blueprints import BlueprintError
from personas.creation import (
    PersonaCreationSpec,
    apply_persona_reconcile,
    preview_persona_reconcile,
    resolve_callable_tool_inventory,
)
from personas.lifecycle import ProfileInfo, initialize_staged_profile_inventory
from personas.provisioning import ProvisionPaths
from personas.readiness import ReadinessPaths, build_persona_readiness_snapshot
from personas.services import dump_config_yaml
from runtime import lane_router
from runtime.base import RuntimeResult, RuntimeToolCall

AI_ENGINEER_CHANNEL = "222222222222222222"
FOUNDER_OPERATOR_CHANNEL = "1532418846600859658"
REPO_ROOT = Path(__file__).resolve().parents[3]

TARGETS = (
    PersonaCreationSpec(
        persona_id="ai-engineer",
        template_id="ai-engineer",
        discord_channel_id=AI_ENGINEER_CHANNEL,
        discord_channel_name="ai-engineer",
    ),
    PersonaCreationSpec(
        persona_id="founder-operator",
        template_id="founder-operator",
        discord_channel_id=FOUNDER_OPERATOR_CHANNEL,
        discord_channel_name="founder-operator",
    ),
)


@pytest.fixture
def activation_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ProvisionPaths:
    homie_root = tmp_path / "homie"
    matrix = tmp_path / "persona-capability-matrix.yaml"
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
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    master_env = tmp_path / "master.env"
    master_env.write_text(
        "OPENAI_API_KEY=test-only-secret\n"
        "HOMIE_VAULT_DIR=C:/test-vault\n"
        "BUSINESS_EMAIL=founder@example.test\n",
        encoding="utf-8",
    )
    bindings = tmp_path / "discord-channel-bindings.json"
    bindings.write_text(
        json.dumps(
            {
                "guild_id": "test-guild",
                "channels": {
                    AI_ENGINEER_CHANNEL: {
                        "kind": "persona",
                        "persona": "ai-engineer",
                        "name": "ai-engineer",
                        "enabled": True,
                    },
                    FOUNDER_OPERATOR_CHANNEL: {
                        "kind": "persona",
                        "persona": "founder-operator",
                        "name": "founder-operator",
                        "enabled": True,
                    },
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    paths = ProvisionPaths(
        homie_root=homie_root,
        bindings_file=bindings,
        capability_matrix_file=matrix,
        master_env_file=master_env,
    )
    for spec, enabled, domain in (
        (TARGETS[0], True, "ai-engineering"),
        (TARGETS[1], False, "founder-operations"),
    ):
        profile_root = paths.profiles_root / spec.persona_id
        initialize_staged_profile_inventory(profile_root, spec.persona_id)
        config = {
            "persona": {
                "id": spec.persona_id,
                "display_name": spec.persona_id.replace("-", " ").title(),
                "role": f"AUTHORED_{spec.persona_id.upper()}_ROLE",
                "domain": domain,
            },
            "model": {"preferred": "claude-sonnet-4-7"},
            "toolsets": [],
            "tools": [],
            "curriculum": {
                "enabled": enabled,
                "domain": domain,
                "sources": [],
                "schedule_hours": 6,
            },
            "operator_notes": {"preserve": spec.persona_id},
        }
        (profile_root / "config.yaml").write_text(
            dump_config_yaml(config),
            encoding="utf-8",
        )

    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    monkeypatch.setenv("HOMIE_PERSONA_CAPABILITY_MATRIX", str(matrix))
    monkeypatch.setenv("DISCORD_CHANNEL_BINDINGS_FILE", str(bindings))
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda *_args, **_kwargs: None,
    )
    return paths


def _reconcile_targets(paths: ProvisionPaths) -> dict[str, object]:
    callable_tools = resolve_callable_tool_inventory()
    receipts = {}
    for spec in TARGETS:
        preview = preview_persona_reconcile(
            spec,
            paths=paths,
            callable_tools=callable_tools,
        )
        receipts[spec.persona_id] = apply_persona_reconcile(
            spec,
            actor="test-operator",
            expected_preview_hash=preview.preview_hash,
            expected_state_hash=preview.state_hash,
            reconcile_approved=True,
            paths=paths,
            callable_tools=callable_tools,
        )
    return receipts


def test_exact_targets_reconcile_to_safe_packs_and_unique_active_channels(
    activation_paths: ProvisionPaths,
) -> None:
    receipts = _reconcile_targets(activation_paths)
    expected_toolsets = {
        "ai-engineer": ["safe_core", "ai_engineering"],
        "founder-operator": ["safe_core", "founder_operations"],
    }
    expected_curriculum = {
        "ai-engineer": True,
        "founder-operator": False,
    }

    for spec in TARGETS:
        receipt = receipts[spec.persona_id]
        profile_root = activation_paths.profiles_root / spec.persona_id
        config = yaml.safe_load(
            (profile_root / "config.yaml").read_text(encoding="utf-8")
        )
        blueprint = yaml.safe_load(
            (profile_root / "blueprint.yaml").read_text(encoding="utf-8")
        )

        assert receipt.outcome == "reconciled"
        assert Path(receipt.receipt_path).is_file()
        assert config["toolsets"] == expected_toolsets[spec.persona_id]
        assert config["tools"] == []
        assert "operator_exec" not in config["toolsets"]
        assert config["capability_blueprint"]["operator_exec"] is False
        assert config["curriculum"]["enabled"] is expected_curriculum[spec.persona_id]
        assert config["operator_notes"] == {"preserve": spec.persona_id}
        assert config["persona"]["role"] == (
            f"AUTHORED_{spec.persona_id.upper()}_ROLE"
        )
        assert blueprint["capabilities"]["operator_exec"] is False
        assert blueprint["channels"] == [
            {
                "kind": "discord",
                "channel_id": spec.discord_channel_id,
                "name": spec.persona_id,
            }
        ]
        assert not list((profile_root / "run").glob("*.pid"))

    binding_document = json.loads(
        activation_paths.bindings_file.read_text(encoding="utf-8")
    )
    target_rows = {
        channel_id: row
        for channel_id, row in binding_document["channels"].items()
        if channel_id in {AI_ENGINEER_CHANNEL, FOUNDER_OPERATOR_CHANNEL}
    }
    assert {
        channel_id: row["persona"] for channel_id, row in target_rows.items()
    } == {
        AI_ENGINEER_CHANNEL: "ai-engineer",
        FOUNDER_OPERATOR_CHANNEL: "founder-operator",
    }
    assert all(row["enabled"] is True for row in target_rows.values())


def test_reconcile_refusal_leaves_both_persona_rows_unchanged(
    activation_paths: ProvisionPaths,
) -> None:
    spec = TARGETS[0]
    callable_tools = resolve_callable_tool_inventory()
    preview = preview_persona_reconcile(
        spec,
        paths=activation_paths,
        callable_tools=callable_tools,
    )
    watched = (
        activation_paths.profiles_root / "ai-engineer" / "config.yaml",
        activation_paths.profiles_root / "founder-operator" / "config.yaml",
        activation_paths.bindings_file,
    )
    before = {path: path.read_bytes() for path in watched}

    with pytest.raises(BlueprintError, match="explicit approval"):
        apply_persona_reconcile(
            spec,
            actor="test-operator",
            expected_preview_hash=preview.preview_hash,
            expected_state_hash=preview.state_hash,
            reconcile_approved=False,
            paths=activation_paths,
            callable_tools=callable_tools,
        )

    assert {path: path.read_bytes() for path in watched} == before


def test_persona_mutation_kill_switch_blocks_reconcile_before_write(
    activation_paths: ProvisionPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from security.kill_switches import KillSwitchDisabled

    spec = TARGETS[0]
    callable_tools = resolve_callable_tool_inventory()
    preview = preview_persona_reconcile(
        spec,
        paths=activation_paths,
        callable_tools=callable_tools,
    )
    watched = (
        activation_paths.profiles_root / "ai-engineer" / "config.yaml",
        activation_paths.bindings_file,
    )
    before = {path: path.read_bytes() for path in watched}
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")

    with pytest.raises(KillSwitchDisabled, match="persona_mutation"):
        apply_persona_reconcile(
            spec,
            actor="test-operator",
            expected_preview_hash=preview.preview_hash,
            expected_state_hash=preview.state_hash,
            reconcile_approved=True,
            paths=activation_paths,
            callable_tools=callable_tools,
        )

    assert {path: path.read_bytes() for path in watched} == before


def test_cli_reconcile_requires_reviewed_hashes_and_explicit_approval(
    activation_paths: ProvisionPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reconciliation contract is machine-readable JSON.  Keep this test
    # independent of the process-wide release-check cache, whose banner is
    # intentionally written to stderr by the real CLI.
    monkeypatch.setattr(cli, "check_for_update", lambda: None)
    monkeypatch.setattr(
        ProvisionPaths,
        "defaults",
        classmethod(lambda cls: activation_paths),
    )
    runner = CliRunner()
    base_args = [
        "profile",
        "blueprint",
        "reconcile-plan",
        "founder-operator",
        "--template",
        "founder-operator",
        "--channel",
        FOUNDER_OPERATOR_CHANNEL,
        "--json",
    ]
    planned = runner.invoke(main, base_args)
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["plan"]["applied_toolsets"] == [
        "safe_core",
        "founder_operations",
    ]
    assert "operator_exec" not in plan["plan"]["applied_toolsets"]
    assert "recall" not in plan["plan"]["missing_tools"]

    config_path = (
        activation_paths.profiles_root
        / "founder-operator"
        / "config.yaml"
    )
    before = config_path.read_bytes()
    apply_args = [
        "profile",
        "blueprint",
        "reconcile",
        "founder-operator",
        "--template",
        "founder-operator",
        "--channel",
        FOUNDER_OPERATOR_CHANNEL,
        "--preview-hash",
        plan["preview_hash"],
        "--state-hash",
        plan["state_hash"],
        "--json",
    ]
    refused = runner.invoke(main, apply_args)
    assert refused.exit_code != 0
    assert "reconcile requires explicit approval" in refused.output
    assert config_path.read_bytes() == before

    applied = runner.invoke(main, [*apply_args, "--approve-reconcile"])
    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["outcome"] == "reconciled"
    assert receipt["preview_hash"] == plan["preview_hash"]


def test_cross_persona_config_mismatch_refuses_and_other_row_is_unchanged(
    activation_paths: ProvisionPaths,
) -> None:
    ai_config_path = (
        activation_paths.profiles_root / "ai-engineer" / "config.yaml"
    )
    founder_config_path = (
        activation_paths.profiles_root / "founder-operator" / "config.yaml"
    )
    ai_config = yaml.safe_load(ai_config_path.read_text(encoding="utf-8"))
    ai_config["persona"]["id"] = "founder-operator"
    ai_config_path.write_text(dump_config_yaml(ai_config), encoding="utf-8")
    before_ai = ai_config_path.read_bytes()
    before_founder = founder_config_path.read_bytes()

    with pytest.raises(BlueprintError, match="physical config belongs"):
        preview_persona_reconcile(TARGETS[0], paths=activation_paths)

    assert ai_config_path.read_bytes() == before_ai
    assert founder_config_path.read_bytes() == before_founder


def test_reconcile_refuses_to_restore_missing_identity_from_template(
    activation_paths: ProvisionPaths,
) -> None:
    config_path = (
        activation_paths.profiles_root / "ai-engineer" / "config.yaml"
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["persona"].pop("display_name")
    config["persona"].pop("role")
    config["persona"].pop("domain")
    config_path.write_text(dump_config_yaml(config), encoding="utf-8")
    before = config_path.read_bytes()

    with pytest.raises(
        BlueprintError,
        match=r"persona\.display_name is missing",
    ):
        preview_persona_reconcile(
            TARGETS[0],
            paths=activation_paths,
            callable_tools=resolve_callable_tool_inventory(),
        )

    assert config_path.read_bytes() == before


def test_declared_unavailable_repo_tool_reports_exact_readiness_failure(
    activation_paths: ProvisionPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reconcile_targets(activation_paths)
    monkeypatch.setattr(
        lane_router,
        "probe_caller_tool_transport",
        lambda _request: lane_router.CallerToolTransportProbe(
            lane="claude_native",
            candidates=(
                lane_router.CallerToolTransportCandidate(
                    provider="claude",
                    carries_caller_tools=True,
                ),
            ),
        ),
    )
    paths = ReadinessPaths(
        profile_root=activation_paths.profiles_root / "ai-engineer",
        bindings_file=activation_paths.bindings_file,
        capability_matrix_file=activation_paths.capability_matrix_file,
        master_env_file=activation_paths.master_env_file,
    )

    snapshot = build_persona_readiness_snapshot("ai-engineer", paths=paths)
    github_tool = next(row for row in snapshot.capabilities if row.id == "gh_issue_view")

    assert snapshot.axes["channel-bound"].status == "READY"
    assert github_tool.axes["declared"] == "READY"
    assert github_tool.axes["callable"] == "BLOCKED"
    assert github_tool.reasons == (
        "declared tool 'gh_issue_view' has no registered handler",
    )
    assert "test-only-secret" not in json.dumps(snapshot.as_dict())


@pytest.mark.asyncio
async def test_each_channel_executes_real_safe_tool_with_source_audit_and_runtime_receipt(
    activation_paths: ProvisionPaths,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import dashboard_api
    from discord_channel_bindings import load_discord_channel_bindings
    from discord_persona_runtime import run_discord_persona_channel_turn
    from models import Channel, IncomingMessage, Platform, Thread, User
    from recall_service import RecallResponse, _FallbackLog
    from session import get_session_store

    _reconcile_targets(activation_paths)
    audit_rows: list[dict[str, object]] = []
    monkeypatch.setattr(
        dashboard_api,
        "_audit_write",
        lambda **kwargs: audit_rows.append(kwargs),
    )

    async def no_recall(**_kwargs) -> RecallResponse:
        return RecallResponse(
            results=[],
            formatted_text="",
            log=_FallbackLog(),
        )

    monkeypatch.setattr("recall_service.recall", no_recall)
    runtime_requests = []

    async def execute_safe_tool(request):
        runtime_requests.append(request)
        assert request.tool_dispatch is not None
        names = {
            definition["function"]["name"]
            for definition in (request.tool_defs or [])
        }
        assert "skills_list" in names
        assert "terminal" not in names
        source_text = request.tool_dispatch("skills_list", {})
        return RuntimeResult(
            text=source_text,
            runtime_lane="claude_native",
            provider="claude",
            model="test-model",
            profile_key=f"test-{request.metadata['persona_id']}",
            session_id=f"runtime-{request.metadata['persona_id']}",
            tool_call_count=1,
            tool_names_used=["skills_list"],
            tool_calls=[
                RuntimeToolCall(
                    id=f"call-{request.metadata['persona_id']}",
                    name="skills_list",
                    arguments={},
                    provider_type="caller_tool",
                    status="completed",
                )
            ],
        )

    monkeypatch.setattr(
        "runtime.lane_router.run_with_runtime_lanes",
        execute_safe_tool,
    )
    bindings = load_discord_channel_bindings(
        path=activation_paths.bindings_file
    )
    store = get_session_store(tmp_path / "persona-channel-acceptance.db")

    for spec in TARGETS:
        channel_id = str(spec.discord_channel_id)
        incoming = IncomingMessage(
            text="List the skills you can inspect.",
            user=User(Platform.DISCORD, "operator-1", "Operator"),
            channel=Channel(Platform.DISCORD, channel_id, is_dm=False),
            platform=Platform.DISCORD,
            thread=Thread(channel_id),
            raw_event={"guild": "test-guild"},
        )
        outgoing = await run_discord_persona_channel_turn(
            incoming=incoming,
            binding=bindings[channel_id],
            session_store=store,
            project_root=REPO_ROOT,
        )

        assert "agent-browser" in outgoing.text
        session = store.get("discord", channel_id, channel_id)
        assert session is not None
        assert session.persona_id == spec.persona_id
        assert session.runtime_provider == "claude"
        assert session.runtime_model == "test-model"
        assert session.runtime_profile_key == f"test-{spec.persona_id}"
        assert session.tool_call_count == 1
        assert session.runtime_tool_calls[0]["name"] == "skills_list"

    assert [request.metadata["discord_channel_id"] for request in runtime_requests] == [
        AI_ENGINEER_CHANNEL,
        FOUNDER_OPERATOR_CHANNEL,
    ]
    assert {
        row["target_persona_id"] for row in audit_rows
    } == {"ai-engineer", "founder-operator"}
    assert all(row["outcome"] == "completed" for row in audit_rows)
    assert all(row["detail"]["tool"] == "skills_list" for row in audit_rows)


def test_disabled_founder_curriculum_makes_zero_provider_calls(
    activation_paths: ProvisionPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import curriculum_tick

    founder_root = activation_paths.profiles_root / "founder-operator"
    founder = ProfileInfo(
        name="founder-operator",
        path=founder_root,
        is_default=False,
        bot_running=False,
        has_env=False,
        skill_count=0,
    )
    monkeypatch.setattr(curriculum_tick, "is_active_default_profile", lambda: True)
    monkeypatch.setattr(curriculum_tick, "list_profiles", lambda: [founder])
    monkeypatch.setattr(
        curriculum_tick.kill_switches,
        "is_disabled",
        lambda _name: False,
    )

    def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("disabled founder must not spawn a provider child")

    monkeypatch.setattr(curriculum_tick, "_spawn", forbidden_spawn)

    assert curriculum_tick.run_parent() == 0
    assert not (
        founder_root / "data" / "curricula" / "curriculum.db"
    ).exists()


def test_curriculum_proposal_starts_no_work_before_or_after_mailbox_route(
    activation_paths: ProvisionPaths,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import config
    from curriculum.ledger import CurriculumLedger
    from curriculum.paths import resolve_curriculum_paths
    from curriculum.service import CurriculumService
    from orchestration.db import OrchestrationDB

    orchestration_db = tmp_path / "orchestration.db"
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", orchestration_db)
    curriculum_paths = resolve_curriculum_paths(
        "ai-engineer",
        "ai-engineering",
    )
    ledger = CurriculumLedger(
        curriculum_paths.ledger_path,
        "ai-engineer",
    )
    ledger.upsert_source(
        "source",
        kind="youtube_channel",
        url="https://youtube.com/@example",
        policy="full",
    )
    ledger.discover_video(
        {
            "video_id": "video-1",
            "source_id": "source",
            "url": "https://youtube.com/watch?v=video-1",
            "title": "Evidence",
        }
    )
    proposal_id = ledger.add_proposal(
        "video-1",
        title="Evaluate a bounded harness",
        body="Internal proposal only.",
    )

    assert not orchestration_db.exists()
    result = CurriculumService("ai-engineer").route(proposal_id)
    assert result["work_started"] is False

    db = OrchestrationDB(orchestration_db)
    try:
        assert db.conn.execute(
            "SELECT COUNT(*) FROM convoys"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM subtasks"
        ).fetchone()[0] == 0
        assert [
            row["msg_type"]
            for row in db.conn.execute(
                "SELECT msg_type FROM agent_messages"
            ).fetchall()
        ] == ["curriculum_proposal"]
    finally:
        db.close()
