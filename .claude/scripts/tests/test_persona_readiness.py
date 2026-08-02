from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from curriculum.model_runtime import secure_curriculum_request
from personas.blueprints import (
    ProvisionMode,
    build_builtin_blueprint,
    compile_blueprint,
)
from personas.lifecycle import ProfileInfo
from personas.readiness import (
    AXIS_NAMES,
    ReadinessPaths,
    build_persona_readiness_snapshot,
    collect_persona_readiness_inventory,
)
from runtime import base as runtime_base
from runtime import lane_router
from runtime.base import RuntimeRequest
from runtime.profiles import RuntimeProfile

PERSONA_ID = "ai-engineer"
DISCORD_CHANNEL_ID = "222222222222222222"
SECRET_VALUE = "must-never-enter-readiness-json"


def _write_compiled_profile(
    tmp_path: Path,
    *,
    persona_id: str = PERSONA_ID,
    template: str = "ai-engineer",
    channel_id: str = DISCORD_CHANNEL_ID,
    binding_persona: str | None = None,
    binding_enabled: bool = True,
    scheduled_authorities: tuple[str, ...] | None = None,
) -> ReadinessPaths:
    profile_root = tmp_path / persona_id
    profile_root.mkdir()
    raw = build_builtin_blueprint(
        template,
        persona_id=persona_id,
        channel_id=channel_id,
    )
    if scheduled_authorities is not None:
        raw["scheduled"]["authorities"] = list(scheduled_authorities)
    plan = compile_blueprint(
        raw,
        mode=ProvisionMode.RECONCILE,
        current_config={},
    )
    (profile_root / "blueprint.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    (profile_root / "config.yaml").write_text(
        yaml.safe_dump(plan.config_patch, sort_keys=False),
        encoding="utf-8",
    )
    (profile_root / ".env").write_text(
        f"OPENAI_API_KEY={SECRET_VALUE}\n",
        encoding="utf-8",
    )
    bindings_file = tmp_path / "discord-channel-bindings.json"
    bindings_file.write_text(
        json.dumps(
            {
                "guild_id": "guild-1",
                "channels": {
                    channel_id: {
                        "kind": "persona",
                        "persona": binding_persona or persona_id,
                        "name": persona_id,
                        "enabled": binding_enabled,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    master_env_file = tmp_path / "master.env"
    master_env_file.write_text(
        f"OPENAI_API_KEY={SECRET_VALUE}\n",
        encoding="utf-8",
    )
    return ReadinessPaths(
        profile_root=profile_root,
        bindings_file=bindings_file,
        capability_matrix_file=tmp_path / "missing-capability-matrix.yaml",
        master_env_file=master_env_file,
    )


def _select_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: str,
) -> None:
    profile = RuntimeProfile(
        key=f"readiness-{provider}",
        provider=provider,
        model="test-model",
    )
    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [profile],
    )


def _enable_sheets(monkeypatch: pytest.MonkeyPatch) -> None:
    from integrations import registry as integration_registry

    sheets = integration_registry.get_all()["sheets"]
    monkeypatch.setattr(
        integration_registry,
        "get_enabled",
        lambda: {"sheets": sheets},
    )


def _activate_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    from personas import activity

    monkeypatch.setattr(
        activity,
        "get_active_profile_name",
        lambda: PERSONA_ID,
    )


def _install_ready_safe_core_handlers() -> dict[str, object | None]:
    from runtime import persona_tools, tool_registry

    persona_tools.ensure_tools_registered()
    previous: dict[str, object | None] = {}
    for name in ("recall", "todo"):
        entry = tool_registry.get_entry(name)
        if entry is not None and entry.handler is not None:
            continue
        previous[name] = entry
        tool_registry.register_tool(
            name,
            entry.description if entry is not None else f"Test handler for {name}.",
            toolset=entry.toolset if entry is not None else "safe_core",
            handler=lambda **_kwargs: {},
            effect=entry.effect if entry is not None else "read",
            integration_action=(
                entry.integration_action if entry is not None else None
            ),
            schema=entry.schema if entry is not None else None,
        )
    return previous


def _restore_tool_entries(previous: dict[str, object | None]) -> None:
    from runtime import tool_registry

    for name, entry in previous.items():
        tool_registry.unregister_tool(name)
        if entry is not None:
            tool_registry.register_tool(
                entry.name,
                entry.description,
                toolset=entry.toolset,
                handler=entry.handler,
                effect=entry.effect,
                integration_action=entry.integration_action,
                schema=entry.schema,
            )


def test_snapshot_keeps_six_axes_and_exact_declared_handler_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_compiled_profile(tmp_path)
    _select_runtime(monkeypatch, provider="claude")
    _enable_sheets(monkeypatch)
    _activate_persona(monkeypatch)

    snapshot = build_persona_readiness_snapshot(PERSONA_ID, paths=paths)

    assert tuple(snapshot.axes) == AXIS_NAMES
    assert snapshot.axes["declared"].status == "READY"
    assert snapshot.axes["transportable"].status == "READY"
    assert snapshot.axes["callable"].status == "PARTIAL"
    assert snapshot.axes["configured"].status == "READY"
    assert snapshot.axes["channel-bound"].status == "READY"
    assert snapshot.axes["scheduler-safe"].status == "READY"
    assert set(snapshot.surfaces) == {
        "discord",
        "direct_chat",
        "cabinet",
        "web",
        "scheduled",
    }
    assert snapshot.surfaces["web"].status == "NOT_APPLICABLE"
    assert snapshot.surfaces["scheduled"].caller_tools is False

    capabilities = {row.id: row for row in snapshot.capabilities}
    for tool_name in ("firecrawl_scrape", "gh_issue_view"):
        row = capabilities[tool_name]
        assert row.status == "PARTIAL"
        assert row.axes["declared"] == "READY"
        assert row.axes["callable"] == "BLOCKED"
        assert row.axes["scheduler-safe"] == "NOT_APPLICABLE"
        assert row.surfaces["web"] == "NOT_APPLICABLE"
        assert row.surfaces["scheduled"] == "NOT_APPLICABLE"
        assert any(
            f"declared tool {tool_name!r} has no registered handler" in reason
            for reason in row.reasons
        )
    sheets = capabilities["sheets.read"]
    assert sheets.kind == "integration"
    assert sheets.axes["configured"] == "READY"
    assert sheets.axes["callable"] == "PARTIAL"
    assert any("no persona caller-tool handler" in reason for reason in sheets.reasons)

    serialized = json.dumps(snapshot.as_dict(), sort_keys=True)
    assert SECRET_VALUE not in serialized
    assert "OPENAI_API_KEY" in serialized


def test_fully_provisioned_persona_can_reach_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persona_id = "general-specialist"
    paths = _write_compiled_profile(
        tmp_path,
        persona_id=persona_id,
        template="general-specialist",
        channel_id="1532418792234291372",
    )
    _select_runtime(monkeypatch, provider="claude")

    previous = _install_ready_safe_core_handlers()
    try:
        snapshot = build_persona_readiness_snapshot(persona_id, paths=paths)
    finally:
        _restore_tool_entries(previous)

    assert snapshot.status == "READY"
    assert snapshot.surfaces["web"].status == "NOT_APPLICABLE"
    assert snapshot.surfaces["scheduled"].status == "NOT_APPLICABLE"
    assert snapshot.capabilities
    assert all(row.status == "READY" for row in snapshot.capabilities)
    assert all(
        row.surfaces["web"] == "NOT_APPLICABLE"
        and row.surfaces["scheduled"] == "NOT_APPLICABLE"
        for row in snapshot.capabilities
    )


def test_direct_chat_readiness_is_selectability_not_active_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personas import activity

    persona_id = "general-specialist"
    paths = _write_compiled_profile(
        tmp_path,
        persona_id=persona_id,
        template="general-specialist",
        channel_id="1532418792234291373",
    )
    _select_runtime(monkeypatch, provider="claude")
    monkeypatch.setattr(activity, "get_active_profile_name", lambda: "smoke")

    previous = _install_ready_safe_core_handlers()
    try:
        snapshot = build_persona_readiness_snapshot(persona_id, paths=paths)
    finally:
        _restore_tool_entries(previous)

    assert snapshot.surfaces["direct_chat"].status == "READY"
    assert all(
        "currently selects physical profile" not in reason
        for reason in snapshot.surfaces["direct_chat"].reasons
    )


def test_transport_probe_uses_fixed_runtime_root_across_invocation_cwds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persona_id = "general-specialist"
    paths = _write_compiled_profile(
        tmp_path,
        persona_id=persona_id,
        template="general-specialist",
        channel_id="1532418792234291374",
    )
    observed_cwds: list[Path] = []

    def probe(request: RuntimeRequest) -> lane_router.CallerToolTransportProbe:
        observed_cwds.append(request.cwd)
        return lane_router.CallerToolTransportProbe(
            lane="claude_native",
            candidates=(
                lane_router.CallerToolTransportCandidate(
                    provider="claude",
                    carries_caller_tools=True,
                ),
            ),
        )

    monkeypatch.setattr(lane_router, "probe_caller_tool_transport", probe)
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    build_persona_readiness_snapshot(persona_id, paths=paths)
    monkeypatch.chdir(second_cwd)
    build_persona_readiness_snapshot(persona_id, paths=paths)

    expected = Path(__file__).resolve().parents[1]
    assert observed_cwds == [expected, expected]


def test_readiness_consumes_public_lane_transport_probe_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persona_id = "general-specialist"
    paths = _write_compiled_profile(
        tmp_path,
        persona_id=persona_id,
        template="general-specialist",
        channel_id="1532418792234291375",
    )
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
    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: pytest.fail("readiness must not consume private lane helpers"),
    )
    monkeypatch.setattr(
        lane_router,
        "_adapter_for",
        lambda _profile: pytest.fail("readiness must not construct adapters directly"),
    )

    snapshot = build_persona_readiness_snapshot(persona_id, paths=paths)

    assert snapshot.axes["transportable"].status == "READY"


def test_integration_callable_state_tracks_registered_scoped_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime import persona_tools, tool_registry

    paths = _write_compiled_profile(tmp_path)
    _select_runtime(monkeypatch, provider="claude")
    _enable_sheets(monkeypatch)
    persona_tools.ensure_tools_registered()
    wrapper_name = "persona_sheets_read"
    tool_registry.register_tool(
        wrapper_name,
        "Read one configured spreadsheet.",
        toolset="safe_core",
        handler=lambda **_kwargs: {},
        integration_action="sheets.read",
    )
    try:
        snapshot = build_persona_readiness_snapshot(PERSONA_ID, paths=paths)
    finally:
        tool_registry.unregister_tool(wrapper_name)

    sheets = next(row for row in snapshot.capabilities if row.id == "sheets.read")
    assert sheets.axes["callable"] == "READY"
    assert all("no persona caller-tool handler" not in reason for reason in sheets.reasons)


def test_scheduler_readiness_uses_registered_authority_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from curriculum import model_runtime

    persona_id = "reflection-specialist"
    paths = _write_compiled_profile(
        tmp_path,
        persona_id=persona_id,
        template="general-specialist",
        channel_id="1532418792234291376",
        scheduled_authorities=("persona_reflection",),
    )
    _select_runtime(monkeypatch, provider="claude")
    monkeypatch.setattr(
        model_runtime,
        "get_scheduled_runtime_contracts",
        lambda: {"persona_reflection": model_runtime.secure_curriculum_request},
    )

    snapshot = build_persona_readiness_snapshot(persona_id, paths=paths)

    assert snapshot.axes["scheduler-safe"].status == "READY"
    assert snapshot.axes["scheduler-safe"].evidence["contracts"] == [
        {
            "authority": "persona_reflection",
            "model_only": True,
            "enabled": False,
        }
    ]


def test_selected_noncarrying_lane_blocks_transport_without_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_compiled_profile(tmp_path)
    monkeypatch.setattr(
        lane_router,
        "probe_caller_tool_transport",
        lambda _request: lane_router.CallerToolTransportProbe(
            lane="generic_runtime",
            candidates=(
                lane_router.CallerToolTransportCandidate(
                    provider="gemini",
                    carries_caller_tools=False,
                ),
            ),
        ),
    )
    _enable_sheets(monkeypatch)
    _activate_persona(monkeypatch)

    snapshot = build_persona_readiness_snapshot(PERSONA_ID, paths=paths)

    assert snapshot.selected_providers == ("gemini",)
    assert snapshot.axes["transportable"].status == "BLOCKED"
    assert any(
        "gemini cannot execute caller-supplied tool definitions" in reason
        for reason in snapshot.axes["transportable"].reasons
    )
    assert snapshot.surfaces["discord"].status == "BLOCKED"


def test_mixed_fallback_transport_is_partial_not_false_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_compiled_profile(tmp_path)
    monkeypatch.setattr(
        lane_router,
        "probe_caller_tool_transport",
        lambda _request: lane_router.CallerToolTransportProbe(
            lane="generic_runtime",
            candidates=(
                lane_router.CallerToolTransportCandidate(
                    provider="claude",
                    carries_caller_tools=True,
                ),
                lane_router.CallerToolTransportCandidate(
                    provider="gemini",
                    carries_caller_tools=False,
                ),
            ),
        ),
    )
    _enable_sheets(monkeypatch)
    _activate_persona(monkeypatch)

    snapshot = build_persona_readiness_snapshot(PERSONA_ID, paths=paths)

    assert snapshot.selected_providers == ("claude", "gemini")
    assert snapshot.axes["transportable"].status == "PARTIAL"
    assert snapshot.axes["transportable"].evidence["carrying_count"] == 1
    assert any(
        "gemini cannot execute caller-supplied tool definitions" in reason
        for reason in snapshot.axes["transportable"].reasons
    )


def test_wrong_physical_discord_owner_is_reported_and_document_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_compiled_profile(
        tmp_path,
        binding_persona="founder-operator",
    )
    before = paths.bindings_file.read_bytes()
    _select_runtime(monkeypatch, provider="claude")
    _enable_sheets(monkeypatch)
    _activate_persona(monkeypatch)

    snapshot = build_persona_readiness_snapshot(PERSONA_ID, paths=paths)

    assert snapshot.axes["channel-bound"].status == "BLOCKED"
    assert any(
        "resolves to 'founder-operator', not 'ai-engineer'" in reason
        for reason in snapshot.axes["channel-bound"].reasons
    )
    assert snapshot.surfaces["discord"].status == "BLOCKED"
    assert paths.bindings_file.read_bytes() == before


def test_derived_green_receipt_cannot_override_unsafe_physical_scheduler_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_compiled_profile(tmp_path)
    receipt_path = (
        paths.profile_root / "data" / "persona-provisioning-readiness.json"
    )
    receipt_path.parent.mkdir()
    receipt_path.write_text(
        json.dumps(
            {
                "status": "READY",
                "scheduled_model_only": True,
                "missing_tools": [],
            }
        ),
        encoding="utf-8",
    )
    config_path = paths.profile_root / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["capability_blueprint"]["scheduled_authorities"] = []
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    receipt_before = receipt_path.read_bytes()
    _select_runtime(monkeypatch, provider="claude")
    _enable_sheets(monkeypatch)
    _activate_persona(monkeypatch)

    snapshot = build_persona_readiness_snapshot(PERSONA_ID, paths=paths)

    assert snapshot.axes["scheduler-safe"].status == "BLOCKED"
    assert any(
        "physical scheduled_authorities do not match blueprint intent" in reason
        for reason in snapshot.axes["scheduler-safe"].reasons
    )
    assert receipt_path.read_bytes() == receipt_before


def test_curriculum_contract_probe_uses_real_runtime_request_model() -> None:
    hostile = RuntimeRequest(
        prompt="study",
        cwd=Path.cwd(),
        task_name="curriculum",
        allowed_tools=["Bash"],
        disallowed_tools=None,
        mcp_servers=["external"],
        hooks={"PostToolUse": []},
        setting_sources=["user"],
        tool_defs=[
            {
                "type": "function",
                "function": {
                    "name": "unsafe",
                    "description": "unsafe",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_dispatch=lambda *_args: "unsafe",
        read_only_tools=True,
        workspace_write_tools=True,
    )

    secured = secure_curriculum_request(hostile)

    assert isinstance(secured, RuntimeRequest)
    runtime_base.assert_model_only_contract(secured)
    assert secured.allowed_tools == []
    assert secured.disallowed_tools == ["*"]
    assert secured.mcp_servers == []
    assert secured.tool_defs is None
    assert secured.tool_dispatch is None
    assert secured.hooks is None
    assert secured.setting_sources == []


def test_inventory_redacts_credential_values_from_hostile_collector_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_compiled_profile(tmp_path)
    profile = ProfileInfo(
        name=PERSONA_ID,
        path=paths.profile_root,
        is_default=False,
        bot_running=False,
        has_env=True,
        skill_count=0,
    )
    import personas.readiness as readiness

    monkeypatch.setattr(readiness, "list_profiles", lambda: [profile])
    monkeypatch.setattr(
        readiness,
        "_build_persona_readiness_snapshot",
        lambda _persona_id, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(f"OPENAI_API_KEY={SECRET_VALUE}")
        ),
    )

    inventory = collect_persona_readiness_inventory()

    serialized = json.dumps(inventory)
    assert inventory[PERSONA_ID]["status"] == "ERROR"
    assert SECRET_VALUE not in serialized
    assert "OPENAI_API_KEY=<redacted>" in serialized


def test_inventory_reuses_one_global_transport_probe_for_all_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import personas.readiness as readiness

    first_id = "general-alpha"
    second_id = "general-beta"
    first_paths = _write_compiled_profile(
        tmp_path,
        persona_id=first_id,
        template="general-specialist",
        channel_id="1532418792234291377",
    )
    second_paths = _write_compiled_profile(
        tmp_path,
        persona_id=second_id,
        template="general-specialist",
        channel_id="1532418792234291378",
    )
    profiles = [
        ProfileInfo(
            name=persona_id,
            path=paths.profile_root,
            is_default=False,
            bot_running=False,
            has_env=True,
            skill_count=0,
        )
        for persona_id, paths in (
            (first_id, first_paths),
            (second_id, second_paths),
        )
    ]
    paths_by_persona = {
        first_id: first_paths,
        second_id: second_paths,
    }
    probe_calls = 0

    def probe(_request: RuntimeRequest) -> lane_router.CallerToolTransportProbe:
        nonlocal probe_calls
        probe_calls += 1
        return lane_router.CallerToolTransportProbe(
            lane="claude_native",
            candidates=(
                lane_router.CallerToolTransportCandidate(
                    provider="claude",
                    carries_caller_tools=True,
                ),
            ),
        )

    monkeypatch.setattr(readiness, "list_profiles", lambda: profiles)
    monkeypatch.setattr(
        readiness.ReadinessPaths,
        "defaults",
        classmethod(lambda _cls, persona_id: paths_by_persona[persona_id]),
    )
    monkeypatch.setattr(lane_router, "probe_caller_tool_transport", probe)

    inventory = collect_persona_readiness_inventory()

    assert set(inventory) == {first_id, second_id}
    assert probe_calls == 1


def test_snapshot_does_not_mutate_runtime_request_used_by_scheduler_probe() -> None:
    request = RuntimeRequest(
        prompt="unchanged",
        cwd=Path.cwd(),
        task_name="curriculum",
    )
    original = replace(request)

    secure_curriculum_request(request)

    assert request == original
