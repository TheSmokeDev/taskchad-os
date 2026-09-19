from __future__ import annotations

import asyncio
import builtins
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pytest
from extension_manager import CommandSpec, ExtensionManager, ExtensionMeta, IntentSpec

from runtime import capability_contributions as contributions
from runtime import framework_registry, tool_registry, toolsets
from runtime.capability_plugin_manifest import (
    CapabilityPluginCandidate,
    ContributionType,
    FilesystemPluginSource,
    ManifestSource,
    discover_capability_plugins,
)
from runtime.capability_plugins import (
    CapabilityPluginKernel,
    DisposalOutcome,
    LifecycleEvent,
    PluginEffectiveState,
    PluginLifecycleState,
)

TOOL_NAME = "piv_typed_fixture_tool"
TOOLSET_NAME = "piv_typed_fixture_bundle"
COMMAND_NAME = "piv-typed-fixture"
SKILL_NAME = "PIV Typed Fixture"
MCP_NAME = "piv-typed-fixture"
PROMPT_HOOK_NAME = "piv.typed.prompt"
CONTEXT_HOOK_NAME = "piv.typed.context"
HEALTH_NAME = "piv.typed.health"
CONFIG_NAME = "piv.typed.config"


def _raw_manifest(
    plugin_id: str,
    declarations: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> dict[str, object]:
    return {
        "manifestVersion": 2,
        "id": plugin_id,
        "name": f"{plugin_id} fixture",
        "version": "1.0.0",
        "description": "A typed capability contribution fixture.",
        "source": "bundled",
        "entrypoint": "plugin:register",
        "requirements": {"coreVersion": ">=1.6,<2", "env": [], "plugins": []},
        "contributions": [
            {"id": item, "type": kind, "dependsOn": list(depends_on)}
            for item, kind, depends_on in declarations
        ],
        "enabledByDefault": False,
        "replaces": None,
        "contractVersion": 1,
        "export": "public",
    }


def _write_plugin(
    root: Path,
    directory: str,
    plugin_id: str,
    declarations: tuple[tuple[str, str, tuple[str, ...]], ...],
    body: str,
) -> None:
    plugin_dir = root / directory
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "extension.json").write_text(
        json.dumps(_raw_manifest(plugin_id, declarations)), encoding="utf-8"
    )
    (plugin_dir / "plugin.py").write_text(body, encoding="utf-8")


def _discover(root: Path) -> tuple[CapabilityPluginCandidate, ...]:
    result = discover_capability_plugins([FilesystemPluginSource(ManifestSource.BUNDLED, root)])
    assert result.errors == ()
    return result.active_candidates


def _services(
    project_root: Path,
    manager: ExtensionManager | None = None,
    *,
    include_tool_registry: bool = True,
) -> contributions.ContributionServices:
    services: dict[str, object] = {
        "toolset_registry": toolsets,
        "framework_registry": framework_registry,
        "contribution_registry": contributions,
        "project_root": project_root,
    }
    if include_tool_registry:
        services["tool_registry"] = tool_registry
    if manager is not None:
        services["extension_manager"] = manager
    return contributions.ContributionServices(MappingProxyType(services))


def _cleanup_named_rows() -> None:
    for name in (
        TOOL_NAME,
        "piv_collision_tool",
        "piv_rollback_tool",
        "piv_module_tool",
        "piv_thread_plugin_tool",
        "piv_thread_baseline_tool",
        "piv_owner_tool",
    ):
        tool_registry.unregister_tool(name)

    for name, plugin_id, plugin_version in toolsets.list_plugin_toolsets():
        if name.startswith("piv_"):
            toolsets.unregister_plugin_toolset(
                name, plugin_id=plugin_id, plugin_version=plugin_version
            )

    for kind, name, plugin_id, plugin_version in framework_registry.list_plugin_overlay_rows():
        if not name.startswith("PIV") and not name.startswith("piv-"):
            continue
        if kind == "skill":
            framework_registry.unregister_plugin_skill(
                name, plugin_id=plugin_id, plugin_version=plugin_version
            )
        else:
            framework_registry.unregister_plugin_mcp_server(
                name, plugin_id=plugin_id, plugin_version=plugin_version
            )
    contributions.reset_local_registries()


@pytest.fixture(autouse=True)
def _isolate_owner_registries() -> None:
    _cleanup_named_rows()
    yield
    _cleanup_named_rows()


ALL_TYPED_DECLARATIONS = (
    ("typed.tool", "tool", ()),
    ("typed.toolset", "toolset", ("typed.tool",)),
    ("typed.skill", "skill", ()),
    ("typed.mcp", "mcp_server", ()),
    ("typed.command", "command", ()),
    ("typed.intent", "intent", ("typed.command",)),
    ("typed.prompt", "prompt_hook", ()),
    ("typed.context", "context_hook", ()),
    ("typed.health", "health_probe", ()),
    ("typed.config", "config_requirement", ()),
)


ALL_TYPED_PLUGIN = f'''
from runtime.capability_contributions import (
    CommandContribution,
    ConfigRequirementContribution,
    ContextHookContribution,
    HealthProbeContribution,
    IntentContribution,
    McpServerContribution,
    PromptHookContribution,
    SkillContribution,
    ToolContribution,
    ToolsetContribution,
)

def dispose():
    return True

def tool_handler(**kwargs):
    return {{"ok": True, "kwargs": kwargs}}

async def command_handler(adapter, incoming, args, *, collect_only=False):
    return "typed:" + args

def prompt_render():
    return "prompt"

def context_provide():
    return {{"context": True}}

def health_probe():
    return True

def register(registrar):
    registrar.publish(
        "typed.tool",
        ToolContribution(
            name="{TOOL_NAME}",
            description="Read the typed fixture.",
            toolset="{TOOLSET_NAME}",
            parameters={{"type": "object", "properties": {{}}}},
            handler=tool_handler,
        ),
        disposer=dispose,
    )
    registrar.publish(
        "typed.toolset",
        ToolsetContribution(
            name="{TOOLSET_NAME}",
            description="Typed fixture tools.",
            tools=("{TOOL_NAME}",),
        ),
        disposer=dispose,
        depends_on=("typed.tool",),
    )
    registrar.publish(
        "typed.skill",
        SkillContribution(
            name="{SKILL_NAME}",
            description="Typed fixture skill.",
            path="skills/piv-typed-fixture/SKILL.md",
        ),
        disposer=dispose,
    )
    registrar.publish(
        "typed.mcp",
        McpServerContribution(
            name="{MCP_NAME}",
            transport="stdio",
            config={{"command": "fixture-mcp"}},
            source="typed fixture",
        ),
        disposer=dispose,
    )
    registrar.publish(
        "typed.command",
        CommandContribution(
            name="{COMMAND_NAME}",
            description="Run the typed fixture.",
            command_type="router",
            min_role="viewer",
            handler=command_handler,
            category="Fixture",
        ),
        disposer=dispose,
    )
    registrar.publish(
        "typed.intent",
        IntentContribution(
            command="{COMMAND_NAME}",
            keywords=("typed fixture",),
            included_in_brief=True,
        ),
        disposer=dispose,
        depends_on=("typed.command",),
    )
    registrar.publish(
        "typed.prompt",
        PromptHookContribution(
            name="{PROMPT_HOOK_NAME}", region="system", render=prompt_render
        ),
        disposer=dispose,
    )
    registrar.publish(
        "typed.context",
        ContextHookContribution(
            name="{CONTEXT_HOOK_NAME}", stage="before_turn", provide=context_provide
        ),
        disposer=dispose,
    )
    registrar.publish(
        "typed.health",
        HealthProbeContribution(
            name="{HEALTH_NAME}", subject="typed fixture", probe=health_probe
        ),
        disposer=dispose,
    )
    registrar.publish(
        "typed.config",
        ConfigRequirementContribution(
            key="{CONFIG_NAME}",
            description="Typed fixture configuration.",
            env_var="PIV_TYPED_FIXTURE_SECRET",
        ),
        disposer=dispose,
    )
'''


def test_kernel_loads_and_unloads_every_typed_owner_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "extensions"
    _write_plugin(
        root,
        "typed",
        "typed.fixture",
        ALL_TYPED_DECLARATIONS,
        ALL_TYPED_PLUGIN,
    )
    manager = ExtensionManager()
    kernel = CapabilityPluginKernel(
        _discover(root),
        receipt_path=tmp_path / "receipts.jsonl",
        contribution_services=_services(tmp_path, manager),
    )
    generations_before = (
        tool_registry.get_generation(),
        toolsets.get_toolset_generation(),
        framework_registry.get_overlay_generation(),
        contributions.get_local_generation(),
    )

    kernel.request_enable("typed.fixture", command_id="enable-typed")
    (loaded,) = kernel.apply_turn_boundary()
    assert loaded.event is LifecycleEvent.LOADED
    assert kernel.state("typed.fixture").effective_state is PluginEffectiveState.LOADED

    (tool_entry,) = [entry for entry in tool_registry.list_registered() if entry.name == TOOL_NAME]
    assert (tool_entry.plugin_id, tool_entry.plugin_version) == (
        "typed.fixture",
        "1.0.0",
    )
    assert toolsets.plugin_toolset_owner(TOOLSET_NAME) == ("typed.fixture", "1.0.0")
    discovered = framework_registry.discover_framework_registry(tmp_path)
    assert any(entry.name == SKILL_NAME for entry in discovered.skills)
    assert any(entry.name == MCP_NAME for entry in discovered.mcp_servers)
    extension_id = "capability-plugin:typed.fixture@1.0.0"
    assert [item.name for item in manager.list_commands_for_extension(extension_id)] == [
        COMMAND_NAME
    ]
    assert len(manager.list_intents_for_extension(extension_id)) == 1
    assert [row.key for row in contributions.list_local_rows("prompt_hook")] == [PROMPT_HOOK_NAME]
    assert [row.key for row in contributions.list_local_rows("context_hook")] == [CONTEXT_HOOK_NAME]
    assert contributions.evaluate_health_probes()[0].status == "ok"
    monkeypatch.setenv("PIV_TYPED_FIXTURE_SECRET", "do-not-serialize-this-value")
    assert contributions.evaluate_config_requirements()[0].satisfied is True

    inventory = kernel.contribution_inventory("typed.fixture")
    assert len(inventory) == len(ALL_TYPED_DECLARATIONS)
    assert all(row["plugin_id"] == "typed.fixture" for row in inventory)
    assert all(row["plugin_version"] == "1.0.0" for row in inventory)
    assert all(row["owner_state"] == "registered" for row in inventory)
    assert all(row["disposer_registered"] is True for row in inventory)
    serialized = json.dumps([dict(row) for row in inventory], sort_keys=True)
    assert "do-not-serialize-this-value" not in serialized
    assert "fixture-mcp" not in serialized

    generations_loaded = (
        tool_registry.get_generation(),
        toolsets.get_toolset_generation(),
        framework_registry.get_overlay_generation(),
        contributions.get_local_generation(),
    )
    assert all(after > before for before, after in zip(generations_before, generations_loaded))

    kernel.request_disable("typed.fixture", command_id="disable-typed")
    (unloaded,) = kernel.apply_turn_boundary()
    assert unloaded.event is LifecycleEvent.UNLOADED
    assert len(unloaded.disposals) == len(ALL_TYPED_DECLARATIONS)
    assert all(item.outcome is DisposalOutcome.SUCCEEDED for item in unloaded.disposals)
    assert kernel.contribution_inventory("typed.fixture") == ()
    assert not any(entry.name == TOOL_NAME for entry in tool_registry.list_registered())
    assert TOOLSET_NAME not in toolsets.TOOLSETS
    assert framework_registry.list_plugin_overlay_rows() == ()
    assert manager.list_commands_for_extension(extension_id) == []
    assert manager.list_intents_for_extension(extension_id) == []
    assert all(
        contributions.list_local_rows(kind) == ()
        for kind in (
            "prompt_hook",
            "context_hook",
            "health_probe",
            "config_requirement",
        )
    )
    generations_unloaded = (
        tool_registry.get_generation(),
        toolsets.get_toolset_generation(),
        framework_registry.get_overlay_generation(),
        contributions.get_local_generation(),
    )
    assert all(after > before for before, after in zip(generations_loaded, generations_unloaded))
    kernel.close()


TOOL_ONLY_PLUGIN = f'''
from runtime.capability_contributions import ToolContribution

def dispose():
    return True

def register(registrar):
    registrar.publish(
        "typed.tool",
        ToolContribution(
            name="{TOOL_NAME}",
            description="Read the typed fixture.",
            toolset="safe_core",
        ),
        disposer=dispose,
    )
'''


def test_missing_owner_service_fails_one_plugin_before_owner_mutation(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_plugin(
        root,
        "missing-service",
        "missing.service",
        (("typed.tool", "tool", ()),),
        TOOL_ONLY_PLUGIN,
    )
    kernel = CapabilityPluginKernel(
        _discover(root),
        receipt_path=tmp_path / "receipts.jsonl",
        contribution_services=_services(tmp_path, include_tool_registry=False),
    )
    generation_before = tool_registry.get_generation()

    kernel.request_enable("missing.service", command_id="enable-missing")
    (failed,) = kernel.apply_turn_boundary()

    assert failed.event is LifecycleEvent.FAILED
    state = kernel.state("missing.service")
    assert state.lifecycle_state is PluginLifecycleState.FAILED
    assert state.effective_state is PluginEffectiveState.UNLOADED
    assert state.error_code == "dependency_service_unavailable"
    assert tool_registry.get_generation() == generation_before
    assert not any(entry.name == TOOL_NAME for entry in tool_registry.list_registered())


def test_typed_owner_rows_remain_for_held_turn_then_dispose_on_lease_release(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    _write_plugin(
        root,
        "leased",
        "leased.plugin",
        (("typed.tool", "tool", ()),),
        TOOL_ONLY_PLUGIN,
    )
    kernel = CapabilityPluginKernel(
        _discover(root),
        receipt_path=tmp_path / "receipts.jsonl",
        contribution_services=_services(tmp_path),
    )
    kernel.request_enable("leased.plugin", command_id="enable-leased")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()

    kernel.request_disable("leased.plugin", command_id="disable-leased")
    (draining,) = kernel.apply_turn_boundary()
    assert draining.event is LifecycleEvent.DRAINING
    assert any(entry.name == TOOL_NAME for entry in tool_registry.list_registered())
    with kernel.snapshot() as current:
        assert "typed.tool" not in current.contributions

    (unloaded,) = held.close()
    assert unloaded.event is LifecycleEvent.UNLOADED
    assert not any(entry.name == TOOL_NAME for entry in tool_registry.list_registered())
    assert kernel.contribution_inventory("leased.plugin") == ()
    kernel.close()


def test_unproven_owner_cleanup_keeps_residual_row_and_restart_required_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "extensions"
    _write_plugin(
        root,
        "residual",
        "residual.plugin",
        (("typed.tool", "tool", ()),),
        TOOL_ONLY_PLUGIN,
    )
    kernel = CapabilityPluginKernel(
        _discover(root),
        receipt_path=tmp_path / "receipts.jsonl",
        contribution_services=_services(tmp_path),
    )
    kernel.request_enable("residual.plugin", command_id="enable-residual")
    kernel.apply_turn_boundary()

    def refuse_cleanup(name: str, *, plugin_id: str, plugin_version: str) -> bool:
        raise tool_registry.ToolRegistryError("cleanup refused")

    monkeypatch.setattr(tool_registry, "unregister_tool_for_owner", refuse_cleanup)
    kernel.request_disable("residual.plugin", command_id="disable-residual")
    (failed,) = kernel.apply_turn_boundary()

    assert failed.event is LifecycleEvent.RESTART_REQUIRED
    assert failed.disposals[0].outcome is DisposalOutcome.FAILED
    state = kernel.state("residual.plugin")
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.residual_contribution_ids == ("typed.tool",)
    assert state.contribution_inventory[0]["owner_state"] == "residual"
    (physical_row,) = [
        entry for entry in tool_registry.list_registered() if entry.name == TOOL_NAME
    ]
    assert physical_row.plugin_id == "residual.plugin"


def _collision_plugin(contribution_id: str) -> str:
    return f'''
from runtime.capability_contributions import ToolContribution

def dispose():
    return True

def register(registrar):
    registrar.publish(
        "{contribution_id}",
        ToolContribution(
            name="piv_collision_tool",
            description="Collision fixture.",
            toolset="safe_core",
        ),
        disposer=dispose,
    )
'''


def test_cross_plugin_domain_collision_has_zero_shadow_registration(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_plugin(
        root,
        "alpha",
        "alpha.plugin",
        (("alpha.tool", "tool", ()),),
        _collision_plugin("alpha.tool"),
    )
    _write_plugin(
        root,
        "beta",
        "beta.plugin",
        (("beta.tool", "tool", ()),),
        _collision_plugin("beta.tool"),
    )
    kernel = CapabilityPluginKernel(
        _discover(root),
        receipt_path=tmp_path / "receipts.jsonl",
        contribution_services=_services(tmp_path),
    )
    kernel.request_enable("alpha.plugin", command_id="enable-alpha")
    kernel.request_enable("beta.plugin", command_id="enable-beta")
    receipts = kernel.apply_turn_boundary()

    assert {item.event for item in receipts} == {LifecycleEvent.LOADED, LifecycleEvent.FAILED}
    (installed,) = [
        item for item in tool_registry.list_registered() if item.name == "piv_collision_tool"
    ]
    assert installed.plugin_id == "alpha.plugin"
    assert kernel.state("alpha.plugin").effective_state is PluginEffectiveState.LOADED
    beta_state = kernel.state("beta.plugin")
    assert beta_state.effective_state is PluginEffectiveState.UNLOADED
    assert beta_state.error_code == "tool_owner_refused"
    assert kernel.contribution_inventory("beta.plugin") == ()

    kernel.request_disable("alpha.plugin", command_id="disable-alpha")
    kernel.apply_turn_boundary()
    assert not any(item.name == "piv_collision_tool" for item in tool_registry.list_registered())


ROLLBACK_PLUGIN = f'''
from runtime.capability_contributions import CommandContribution, ToolContribution

def dispose():
    return True

async def handler(adapter, incoming, args, *, collect_only=False):
    return args

def register(registrar):
    registrar.publish(
        "rollback.tool",
        ToolContribution(
            name="piv_rollback_tool",
            description="Rollback fixture.",
            toolset="safe_core",
        ),
        disposer=dispose,
    )
    registrar.publish(
        "rollback.command",
        CommandContribution(
            name="{COMMAND_NAME}",
            description="Colliding command.",
            command_type="router",
            min_role="viewer",
            handler=handler,
        ),
        disposer=dispose,
        depends_on=("rollback.tool",),
    )
'''


def test_late_owner_failure_rolls_back_prior_rows_and_keeps_original_command(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    _write_plugin(
        root,
        "rollback",
        "rollback.plugin",
        (
            ("rollback.tool", "tool", ()),
            ("rollback.command", "command", ("rollback.tool",)),
        ),
        ROLLBACK_PLUGIN,
    )
    manager = ExtensionManager()

    async def baseline_handler(adapter: object, incoming: object, args: str, **_: object) -> str:
        return f"baseline:{args}"

    baseline = CommandSpec(
        name=COMMAND_NAME,
        description="Baseline command.",
        type="router",
        min_role="viewer",
        handler=baseline_handler,
        extension_id="baseline.extension",
    )
    manager.register_command(baseline)
    kernel = CapabilityPluginKernel(
        _discover(root),
        receipt_path=tmp_path / "receipts.jsonl",
        contribution_services=_services(tmp_path, manager),
    )
    generation_before = tool_registry.get_generation()

    kernel.request_enable("rollback.plugin", command_id="enable-rollback")
    (failed,) = kernel.apply_turn_boundary()

    assert failed.event is LifecycleEvent.FAILED
    assert kernel.generation == 0
    assert tool_registry.get_generation() == generation_before + 2
    assert not any(entry.name == "piv_rollback_tool" for entry in tool_registry.list_registered())
    assert manager._commands[COMMAND_NAME] is baseline
    assert kernel.contribution_inventory("rollback.plugin") == ()


REVERSE_PLUGIN = """
import builtins
from runtime.capability_contributions import ContextHookContribution, PromptHookContribution

def prompt():
    return "prompt"

def context():
    return "context"

def dispose_prompt():
    builtins.PIV_DISPOSAL_EVENTS.append("plugin:prompt")
    return True

def dispose_context():
    builtins.PIV_DISPOSAL_EVENTS.append("plugin:context")
    return True

def register(registrar):
    registrar.publish(
        "reverse.prompt",
        PromptHookContribution(name="piv.reverse.prompt", region="system", render=prompt),
        disposer=dispose_prompt,
    )
    registrar.publish(
        "reverse.context",
        ContextHookContribution(name="piv.reverse.context", stage="turn", provide=context),
        disposer=dispose_context,
        depends_on=("reverse.prompt",),
    )
"""


def test_unload_runs_owner_then_plugin_disposers_in_reverse_dependency_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "extensions"
    _write_plugin(
        root,
        "reverse",
        "reverse.plugin",
        (
            ("reverse.prompt", "prompt_hook", ()),
            ("reverse.context", "context_hook", ("reverse.prompt",)),
        ),
        REVERSE_PLUGIN,
    )
    events: list[str] = []
    monkeypatch.setattr(builtins, "PIV_DISPOSAL_EVENTS", events, raising=False)
    original_unregister = contributions.unregister_local_row

    def traced_unregister(kind: str, key: str, *, plugin_id: str, plugin_version: str) -> bool:
        events.append(f"owner:{kind}")
        return original_unregister(
            kind,
            key,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
        )

    monkeypatch.setattr(contributions, "unregister_local_row", traced_unregister)
    kernel = CapabilityPluginKernel(
        _discover(root),
        receipt_path=tmp_path / "receipts.jsonl",
        contribution_services=_services(tmp_path),
    )
    kernel.request_enable("reverse.plugin", command_id="enable-reverse")
    kernel.apply_turn_boundary()
    kernel.request_disable("reverse.plugin", command_id="disable-reverse")
    (receipt,) = kernel.apply_turn_boundary()

    assert events == [
        "owner:context_hook",
        "plugin:context",
        "owner:prompt_hook",
        "plugin:prompt",
    ]
    assert [item.contribution_id for item in receipt.disposals] == [
        "reverse.context",
        "reverse.prompt",
    ]


def test_wrong_owner_removals_leave_every_physical_row_unchanged(tmp_path: Path) -> None:
    owner_a = contributions.ContributionOwner(
        plugin_id="owner.alpha",
        plugin_version="1.0.0",
        contribution_id="owner.prompt",
        type=ContributionType.PROMPT_HOOK,
    )
    payload = contributions.PromptHookContribution(
        name="piv.owner.prompt", region="system", render=lambda: "prompt"
    )
    contributions.register_local_row("prompt_hook", payload.name, payload, owner_a)
    local_before = contributions.list_local_rows("prompt_hook")[0]
    with pytest.raises(contributions.LocalRegistryError):
        contributions.unregister_local_row(
            "prompt_hook",
            payload.name,
            plugin_id="owner.beta",
            plugin_version="1.0.0",
        )
    assert contributions.list_local_rows("prompt_hook")[0] is local_before

    with tool_registry.plugin_owner_scope("owner.alpha", "1.0.0"):
        tool_before = tool_registry.register_tool(
            "piv_owner_tool", "Owner fixture.", toolset="safe_core"
        )
    with pytest.raises(tool_registry.ToolRegistryError):
        tool_registry.unregister_tool_for_owner(
            "piv_owner_tool", plugin_id="owner.beta", plugin_version="1.0.0"
        )
    assert (
        next(item for item in tool_registry.list_registered() if item.name == "piv_owner_tool")
        is tool_before
    )

    toolsets.register_plugin_toolset(
        "piv_owner_bundle",
        {"description": "Owner fixture.", "tools": [], "includes": []},
        plugin_id="owner.alpha",
        plugin_version="1.0.0",
    )
    toolset_before = toolsets.TOOLSETS["piv_owner_bundle"]
    with pytest.raises(toolsets.ToolsetRegistryError):
        toolsets.unregister_plugin_toolset(
            "piv_owner_bundle", plugin_id="owner.beta", plugin_version="1.0.0"
        )
    assert toolsets.TOOLSETS["piv_owner_bundle"] is toolset_before

    skill = framework_registry.SkillEntry(
        name="PIV Owner Skill", description="Owner fixture.", path="skills/piv-owner/SKILL.md"
    )
    framework_registry.register_plugin_skill(
        skill,
        plugin_id="owner.alpha",
        plugin_version="1.0.0",
        project_root=tmp_path,
    )
    overlay_before = framework_registry.list_plugin_overlay_rows()
    with pytest.raises(framework_registry.FrameworkOverlayError):
        framework_registry.unregister_plugin_skill(
            skill.name, plugin_id="owner.beta", plugin_version="1.0.0"
        )
    assert framework_registry.list_plugin_overlay_rows() == overlay_before

    manager = ExtensionManager()
    command = CommandSpec(
        name="piv-owner-command",
        description="Owner fixture.",
        type="engine",
        min_role="viewer",
        extension_id="owner.alpha",
    )
    manager.register_command(command)
    with pytest.raises(ValueError):
        manager.unregister_command(command.name, extension_id="owner.beta")
    assert manager._commands[command.name] is command


def test_tool_owner_scope_does_not_leak_into_another_thread() -> None:
    worker_errors: list[BaseException] = []

    def register_baseline() -> None:
        try:
            tool_registry.register_tool(
                "piv_thread_baseline_tool",
                "Thread baseline fixture.",
                toolset="safe_core",
            )
        except BaseException as exc:
            worker_errors.append(exc)

    with tool_registry.plugin_owner_scope("thread.plugin", "1.0.0"):
        worker = threading.Thread(target=register_baseline)
        worker.start()
        worker.join()
        tool_registry.register_tool(
            "piv_thread_plugin_tool", "Thread plugin fixture.", toolset="safe_core"
        )

    assert worker_errors == []
    rows = {item.name: item for item in tool_registry.list_registered()}
    assert rows["piv_thread_baseline_tool"].plugin_id == ""
    assert rows["piv_thread_plugin_tool"].plugin_id == "thread.plugin"


def test_toolset_owner_validates_closed_shape_and_dependencies() -> None:
    generation_before = toolsets.get_toolset_generation()
    with pytest.raises(toolsets.ToolsetRegistryError):
        toolsets.register_plugin_toolset(
            "piv_invalid_bundle",
            {
                "description": "Invalid fixture.",
                "tools": [],
                "includes": ["piv_missing_bundle"],
            },
            plugin_id="invalid.plugin",
            plugin_version="1.0.0",
        )
    assert "piv_invalid_bundle" not in toolsets.TOOLSETS
    assert toolsets.get_toolset_generation() == generation_before


@dataclass(frozen=True)
class _Incoming:
    user_role: str = "viewer"


def test_legacy_extension_compatibility_preserves_command_and_intent_behavior(
    tmp_path: Path,
) -> None:
    manager = ExtensionManager()

    async def handler(
        adapter: object,
        incoming: object,
        args: str,
        *,
        collect_only: bool = False,
    ) -> str:
        return f"legacy:{args}:{collect_only}"

    command = CommandSpec(
        name="piv-legacy-command",
        description="Legacy fixture.",
        type="router",
        min_role="viewer",
        handler=handler,
        handler_ref="",
        extension_id="legacy.extension",
        category="Legacy",
    )
    intent = IntentSpec(
        command=command.name,
        keywords=["legacy fixture"],
        included_in_brief=True,
        extension_id="legacy.extension",
    )
    extension = ExtensionMeta(
        id="legacy.extension",
        name="Legacy fixture",
        version="2.4.1",
        description="Legacy compatibility fixture.",
        path=tmp_path,
        source="bundled",
        commands=[command],
        intents=[intent],
    )
    requests = contributions.legacy_extension_contributions(extension, "compat.legacy", "2.4.1")
    applied = contributions.apply_contributions(
        requests,
        services=contributions.ContributionServices(
            MappingProxyType({"extension_manager": manager})
        ),
    )

    extension_id = "capability-plugin:compat.legacy@2.4.1"
    (installed_command,) = manager.list_commands_for_extension(extension_id)
    (installed_intent,) = manager.list_intents_for_extension(extension_id)
    assert (
        installed_command.name,
        installed_command.description,
        installed_command.type,
        installed_command.min_role,
        installed_command.handler,
        installed_command.handler_ref,
        installed_command.category,
    ) == (
        command.name,
        command.description,
        command.type,
        command.min_role,
        command.handler,
        command.handler_ref,
        command.category,
    )
    assert (
        installed_intent.command,
        installed_intent.keywords,
        installed_intent.included_in_brief,
    ) == (intent.command, intent.keywords, intent.included_in_brief)
    assert (
        asyncio.run(
            manager.dispatch(
                command.name,
                adapter=None,
                incoming=_Incoming(),
                args="hello",
                collect_only=True,
            )
        )
        == "legacy:hello:True"
    )

    batch = contributions.dispose_contributions(applied)
    assert batch.failed_ids == ()
    assert manager.list_commands_for_extension(extension_id) == []
    assert manager.list_intents_for_extension(extension_id) == []


def test_real_extension_json_lazy_handler_runs_through_compatibility_adapter(
    tmp_path: Path,
) -> None:
    extensions_root = tmp_path / "extensions"
    package = extensions_root / "legacy-package"
    package.mkdir(parents=True)
    (package / "extension.json").write_text(
        json.dumps(
            {
                "id": "legacy.package",
                "name": "Legacy Package",
                "version": "3.2.1",
                "commands": [
                    {
                        "name": "piv-real-legacy",
                        "description": "Real legacy package fixture.",
                        "type": "router",
                        "minRole": "viewer",
                        "handler": "handler:run",
                    }
                ],
                "dataIntents": [
                    {
                        "command": "piv-real-legacy",
                        "keywords": ["real legacy fixture"],
                        "includedInBrief": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (package / "handler.py").write_text(
        "async def run(adapter, incoming, args, *, collect_only=False):\n"
        "    return f'real-legacy:{args}:{collect_only}'\n",
        encoding="utf-8",
    )
    source_manager = ExtensionManager()
    (extension,) = source_manager.discover([extensions_root])
    assert extension.commands[0].handler is None

    target_manager = ExtensionManager()
    applied = contributions.apply_contributions(
        contributions.legacy_extension_contributions(
            extension, "compat.real-legacy", extension.version
        ),
        services=contributions.ContributionServices(
            MappingProxyType({"extension_manager": target_manager})
        ),
    )

    assert (
        asyncio.run(
            target_manager.dispatch(
                "piv-real-legacy",
                adapter=None,
                incoming=_Incoming(),
                args="hello",
                collect_only=True,
            )
        )
        == "real-legacy:hello:True"
    )
    extension_id = "capability-plugin:compat.real-legacy@3.2.1"
    assert target_manager.list_intents_for_extension(extension_id)[0].keywords == [
        "real legacy fixture"
    ]
    assert contributions.dispose_contributions(applied).failed_ids == ()
    assert target_manager.list_commands_for_extension(extension_id) == []
    assert target_manager.list_intents_for_extension(extension_id) == []


def test_existing_tool_module_registers_unchanged_through_compatibility_scope(
    tmp_path: Path,
) -> None:
    def handler(**_: object) -> str:
        return "module-ok"

    def register_tools() -> int:
        tool_registry.register_tool(
            "piv_module_tool",
            "Module compatibility fixture.",
            toolset="safe_core",
            handler=handler,
        )
        return 1

    request = contributions.bundled_tool_module_contribution(
        "compat.module",
        "runtime.tool_impl_fixture",
        register_tools,
        plugin_id="compat.tools",
        plugin_version="1.0.0",
    )
    (applied,) = contributions.apply_contributions((request,), services=_services(tmp_path))

    (installed,) = [
        item for item in tool_registry.list_registered() if item.name == "piv_module_tool"
    ]
    assert installed.handler is handler
    assert (installed.plugin_id, installed.plugin_version) == ("compat.tools", "1.0.0")
    assert contributions.dispose_contributions((applied,)).failed_ids == ()
    assert not any(item.name == "piv_module_tool" for item in tool_registry.list_registered())


def test_health_and_config_evaluation_fail_open_without_serializing_values(
    tmp_path: Path,
) -> None:
    secret = "never-print-this-secret-value"

    def broken_probe() -> bool:
        raise RuntimeError(secret)

    owner_health = contributions.ContributionOwner(
        plugin_id="diagnostic.plugin",
        plugin_version="1.0.0",
        contribution_id="diagnostic.health",
        type=ContributionType.HEALTH_PROBE,
    )
    owner_config = contributions.ContributionOwner(
        plugin_id="diagnostic.plugin",
        plugin_version="1.0.0",
        contribution_id="diagnostic.config",
        type=ContributionType.CONFIG_REQUIREMENT,
    )
    applied = contributions.apply_contributions(
        (
            contributions.ContributionRequest(
                owner_health,
                contributions.HealthProbeContribution(
                    name="piv.diagnostic.health",
                    subject="diagnostic fixture",
                    probe=broken_probe,
                ),
            ),
            contributions.ContributionRequest(
                owner_config,
                contributions.ConfigRequirementContribution(
                    key="piv.diagnostic.config",
                    description="Diagnostic configuration.",
                    env_var="PIV_DIAGNOSTIC_SECRET",
                ),
            ),
        ),
        services=_services(tmp_path),
    )

    (health,) = contributions.evaluate_health_probes()
    assert health.status == "unknown"
    assert health.detail == "probe raised"
    (config_status,) = contributions.evaluate_config_requirements({"PIV_DIAGNOSTIC_SECRET": secret})
    assert config_status.satisfied is True
    serialized = json.dumps(
        [dict(item) for item in contributions.contribution_catalog(applied)],
        sort_keys=True,
    )
    assert secret not in serialized
    assert contributions.dispose_contributions(applied).failed_ids == ()


def test_adapter_registry_is_closed_and_rejects_derived_payloads() -> None:
    registry = contributions.default_adapter_registry()
    assert registry.types() == contributions.TYPED_CONTRIBUTION_TYPES
    with pytest.raises(contributions.ContributionError, match="duplicate_adapter"):
        registry.register(contributions.ToolAdapter())
    with pytest.raises(contributions.ContributionError, match="adapter_not_registered"):
        registry.resolve(ContributionType.PROVIDER_ADAPTER)

    class DerivedTool(contributions.ToolContribution):
        pass

    request = contributions.ContributionRequest(
        contributions.ContributionOwner(
            plugin_id="derived.plugin",
            plugin_version="1.0.0",
            contribution_id="derived.tool",
            type=ContributionType.TOOL,
        ),
        DerivedTool(
            name="piv_derived_tool",
            description="Derived fixture.",
            toolset="safe_core",
        ),
    )
    with pytest.raises(contributions.ContributionError, match="contribution_payload_type_mismatch"):
        contributions.validate_contributions((request,), adapters=registry)
