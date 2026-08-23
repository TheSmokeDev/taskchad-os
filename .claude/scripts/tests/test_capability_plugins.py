from __future__ import annotations

import builtins
import importlib.abc
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from extension_manager import ExtensionManager

import config
import runtime.capability_plugin_journal as capability_plugin_journal_module
import runtime.capability_plugins as capability_plugins_module
from runtime.capability_plugin_journal import (
    JournalAppendResult,
    JournalOwnershipError,
    LockedLifecycleJournal,
    ReceiptPersistenceError,
)
from runtime.capability_plugin_manifest import (
    CapabilityPluginArtifact,
    CapabilityPluginCandidate,
    FilesystemPluginSource,
    ManifestSource,
    discover_capability_plugins,
    parse_capability_manifest,
)
from runtime.capability_plugins import (
    CapabilityNotFoundError,
    CapabilityPluginError,
    CapabilityPluginKernel,
    LifecycleEvent,
    LifecycleOutcome,
    LifecyclePhase,
    LifecycleTransition,
    PluginDesiredState,
    PluginEffectiveState,
    PluginLifecycleReceipt,
    PluginLifecycleState,
)


def raw_manifest(
    plugin_id: str = "fixture.plugin",
    *,
    source: str = "bundled",
    contributions: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("fixture.base", ()),
        ("fixture.dependent", ("fixture.base",)),
    ),
    enabled_by_default: bool = False,
    env: tuple[str, ...] = (),
    plugin_dependencies: tuple[str, ...] = (),
    entrypoint: str = "plugin:register",
    core_requirement: str = ">=1.6,<2",
) -> dict[str, object]:
    return {
        "manifestVersion": 2,
        "id": plugin_id,
        "name": f"{plugin_id} fixture",
        "version": "1.0.0",
        "description": "A deterministic test-only capability plugin.",
        "source": source,
        "entrypoint": entrypoint,
        "requirements": {
            "coreVersion": core_requirement,
            "env": list(env),
            "plugins": list(plugin_dependencies),
        },
        "contributions": [
            {"id": item, "type": "generic", "dependsOn": list(depends_on)}
            for item, depends_on in contributions
        ],
        "enabledByDefault": enabled_by_default,
        "replaces": None,
        "contractVersion": 1,
        "export": "public",
    }


SUCCESS_PLUGIN = """
import builtins

EVENTS = []
REGISTRATION_RECEIPTS = []

def base_value():
    return "fixture-base"

def dependent_value():
    return {
        "value": "fixture-ok",
        "disposals": tuple(EVENTS),
        "registrations": tuple(item.contribution_id for item in REGISTRATION_RECEIPTS),
    }

def dispose_base():
    EVENTS.append("fixture.base")
    if hasattr(builtins, "CAPABILITY_SUCCESS_EVENTS"):
        builtins.CAPABILITY_SUCCESS_EVENTS.append("fixture.base")
    return True

def dispose_dependent():
    EVENTS.append("fixture.dependent")
    if hasattr(builtins, "CAPABILITY_SUCCESS_EVENTS"):
        builtins.CAPABILITY_SUCCESS_EVENTS.append("fixture.dependent")
    return True

def register(registrar):
    REGISTRATION_RECEIPTS.append(registrar.publish(
        "fixture.base", base_value, disposer=dispose_base, depends_on=(),
    ))
    REGISTRATION_RECEIPTS.append(registrar.publish(
        "fixture.dependent",
        dependent_value,
        disposer=dispose_dependent,
        depends_on=("fixture.base",),
    ))
"""


def write_plugin(root: Path, directory: str, raw: dict[str, object], body: str) -> Path:
    plugin_dir = root / directory
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "extension.json").write_text(json.dumps(raw), encoding="utf-8")
    (plugin_dir / "plugin.py").write_text(body, encoding="utf-8")
    return plugin_dir


def discover(root: Path) -> tuple[CapabilityPluginCandidate, ...]:
    result = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, root)]
    )
    assert result.errors == ()
    return result.active_candidates


def receipt_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def retained_exception_strings(exc: BaseException) -> tuple[str, ...]:
    """Collect exact string args from the exception graph and traceback locals."""

    found: list[str] = []
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.extend(arg for arg in current.args if type(arg) is str)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        traceback = current.__traceback__
        while traceback is not None:
            for value in list(traceback.tb_frame.f_locals.values()):
                if isinstance(value, BaseException):
                    pending.append(value)
            traceback = traceback.tb_next
    return tuple(found)


def journal_payload(command_id: str, *, phase: str = "request") -> dict[str, object]:
    return {
        "command_id": command_id,
        "plugin_id": "fixture.plugin",
        "command_transition": "enable",
        "phase": phase,
    }


def valid_lifecycle_record() -> dict[str, object]:
    return {
        "schema_version": 2,
        "command_id": "enable",
        "event_id": 1,
        "plugin_id": "fixture.plugin",
        "plugin_version": "1.0.0",
        "plugin_provenance_id": "bundled:00000000000000000000",
        "source": "bundled",
        "command_transition": "enable",
        "requested_transition": "enable",
        "phase": "request",
        "event": "enabled",
        "desired_state": "enabled",
        "effective_state": "unloaded",
        "lifecycle_state": "enabled",
        "generation_before": 0,
        "generation_after": 0,
        "contribution_ids": ["fixture.base"],
        "outcome": "accepted",
        "restart_required": False,
        "timestamp": "2026-08-22T00:00:00+00:00",
        "detail_code": "",
        "detail": "",
        "disposals": [],
        "journal_owner_id": "owner",
    }


def test_enable_load_execute_disable_unload_is_transactional_and_durable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(builtins, "CAPABILITY_SUCCESS_EVENTS", [], raising=False)
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(
        discover(root),
        receipt_path=receipts,
        clock=lambda: datetime(2026, 8, 21, 12, tzinfo=UTC),
    )

    enable = kernel.request_enable("fixture.plugin", command_id="enable-fixture")
    assert enable.event is LifecycleEvent.ENABLED
    assert enable.outcome is LifecycleOutcome.ACCEPTED
    assert enable.desired_state is PluginDesiredState.ENABLED
    assert enable.effective_state is PluginEffectiveState.UNLOADED
    with kernel.snapshot() as empty:
        assert empty.contributions == {}

    (loaded,) = kernel.apply_turn_boundary()
    assert loaded.event is LifecycleEvent.LOADED
    assert loaded.generation_before == 0
    assert loaded.generation_after == 1
    held = kernel.snapshot()
    assert held.resolve("fixture.base")() == "fixture-base"
    assert held.resolve("fixture.dependent")() == {
        "value": "fixture-ok",
        "disposals": (),
        "registrations": ("fixture.base", "fixture.dependent"),
    }

    disable = kernel.request_disable("fixture.plugin", command_id="disable-fixture")
    assert disable.event is LifecycleEvent.UNLOAD_REQUESTED
    with kernel.snapshot() as same_turn_generation:
        assert same_turn_generation.resolve("fixture.dependent")()["value"] == "fixture-ok"
    assert held.resolve("fixture.base")() == "fixture-base"

    (draining,) = kernel.apply_turn_boundary()
    assert draining.event is LifecycleEvent.DRAINING
    assert draining.phase is LifecyclePhase.PROGRESS
    assert draining.disposals == ()
    assert builtins.CAPABILITY_SUCCESS_EVENTS == []
    assert held.resolve("fixture.dependent")()["disposals"] == ()
    with kernel.snapshot() as current:
        assert current.generation == 2
        assert current.plugins == ()
        assert current.contributions == {}
        with pytest.raises(CapabilityNotFoundError):
            current.resolve("fixture.base")

    (unloaded,) = held.close()
    assert unloaded.event is LifecycleEvent.UNLOADED
    assert unloaded.phase is LifecyclePhase.TERMINAL
    assert [item.contribution_id for item in unloaded.disposals] == [
        "fixture.dependent",
        "fixture.base",
    ]
    assert unloaded.generation_before == 1
    assert unloaded.generation_after == 2
    assert builtins.CAPABILITY_SUCCESS_EVENTS == ["fixture.dependent", "fixture.base"]
    with pytest.raises(CapabilityPluginError) as released:
        held.resolve("fixture.base")
    assert released.value.code == "snapshot_released"

    rows = receipt_rows(receipts)
    assert [row["event"] for row in rows] == [
        "enabled",
        "loaded",
        "unload_requested",
        "draining",
        "unloaded",
    ]
    assert [row["event_id"] for row in rows] == [1, 2, 3, 4, 5]
    assert [row["phase"] for row in rows] == [
        "request",
        "terminal",
        "request",
        "progress",
        "terminal",
    ]
    assert [row["command_id"] for row in rows] == [
        "enable-fixture",
        "enable-fixture",
        "disable-fixture",
        "disable-fixture",
        "disable-fixture",
    ]
    assert [row["detail_code"] for row in rows] == [
        "",
        "",
        "",
        "snapshot_leases_draining",
        "",
    ]
    assert all(row["timestamp"] == "2026-08-21T12:00:00+00:00" for row in rows)


def test_exact_registration_set_mismatch_rolls_back_without_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(builtins, "CAPABILITY_TEST_EVENTS", [], raising=False)
    body = """
import builtins

def dispose_base():
    builtins.CAPABILITY_TEST_EVENTS.append("fixture.base")
    return True

def register(registrar):
    registrar.publish("fixture.base", "base", disposer=dispose_base, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(root, "mismatch", raw_manifest(), body)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")

    kernel.request_enable("fixture.plugin", command_id="mismatch")
    (failed,) = kernel.apply_turn_boundary()

    assert failed.event is LifecycleEvent.FAILED
    assert failed.detail_code == "registration_set_mismatch"
    assert kernel.generation == 0
    with kernel.snapshot() as current:
        assert current.contributions == {}
    assert builtins.CAPABILITY_TEST_EVENTS == ["fixture.base"]
    assert kernel.state("fixture.plugin").lifecycle_state is PluginLifecycleState.FAILED


def test_partial_registration_exception_rolls_back_in_reverse_dependency_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(builtins, "CAPABILITY_TEST_EVENTS", [], raising=False)
    body = """
import builtins

def dispose_base():
    builtins.CAPABILITY_TEST_EVENTS.append("fixture.base")
    return True

def dispose_dependent():
    builtins.CAPABILITY_TEST_EVENTS.append("fixture.dependent")
    return True

def register(registrar):
    registrar.publish("fixture.base", "base", disposer=dispose_base, depends_on=())
    registrar.publish(
        "fixture.dependent", "dependent", disposer=dispose_dependent,
        depends_on=("fixture.base",),
    )
    raise RuntimeError("registration exploded")
"""
    root = tmp_path / "extensions"
    write_plugin(root, "broken", raw_manifest(), body)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")

    kernel.request_enable("fixture.plugin", command_id="rollback")
    (failed,) = kernel.apply_turn_boundary()

    assert failed.event is LifecycleEvent.FAILED
    assert builtins.CAPABILITY_TEST_EVENTS == ["fixture.dependent", "fixture.base"]
    with kernel.snapshot() as current:
        assert current.contributions == {}
    assert kernel.generation == 0


def test_failed_load_rollback_requires_restart_when_disposal_is_unproven(
    tmp_path: Path,
) -> None:
    body = """
def dispose_base():
    return False

def register(registrar):
    registrar.publish("fixture.base", "base", disposer=dispose_base, depends_on=())
    raise RuntimeError("load failed")
"""
    raw = raw_manifest(contributions=(("fixture.base", ()),))
    root = tmp_path / "extensions"
    write_plugin(root, "broken", raw, body)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")

    kernel.request_enable("fixture.plugin", command_id="rollback-fails")
    (failed,) = kernel.apply_turn_boundary()

    assert failed.event is LifecycleEvent.RESTART_REQUIRED
    assert failed.restart_required is True
    with kernel.snapshot() as current:
        assert current.contributions == {}
    state = kernel.state("fixture.plugin")
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.residual_contribution_ids == ("fixture.base",)

    refused = kernel.request_disable("fixture.plugin", command_id="disable-after-failure")

    assert refused.event is LifecycleEvent.FAILED
    assert refused.outcome is LifecycleOutcome.FAILED
    assert refused.detail_code == "restart_required_disable_refused"
    assert refused.desired_state is PluginDesiredState.ENABLED
    assert kernel.state("fixture.plugin").desired_state is PluginDesiredState.ENABLED


def test_failed_unload_never_claims_unloaded_and_removes_new_turn_authority(
    tmp_path: Path,
) -> None:
    body = """
def value():
    return "still-held"

def dispose():
    return False

def register(registrar):
    registrar.publish("fixture.base", value, disposer=dispose, depends_on=())
"""
    raw = raw_manifest(contributions=(("fixture.base", ()),))
    root = tmp_path / "extensions"
    write_plugin(root, "bad-dispose", raw, body)
    receipts = tmp_path / "receipts.jsonl"
    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, root)]
    )
    assert discovery.errors == ()
    kernel = CapabilityPluginKernel(discovery.active_candidates, receipt_path=receipts)
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()
    kernel.request_disable("fixture.plugin", command_id="disable")

    (draining,) = kernel.apply_turn_boundary()

    assert draining.event is LifecycleEvent.DRAINING
    assert kernel.state("fixture.plugin").effective_state is PluginEffectiveState.DRAINING
    with kernel.snapshot() as current:
        assert current.contributions == {}
    assert held.resolve("fixture.base")() == "still-held"
    (terminal,) = held.close()
    assert terminal.event is LifecycleEvent.RESTART_REQUIRED
    assert terminal.restart_required is True
    assert terminal.generation_after == 2
    with kernel.snapshot() as current:
        assert current.contributions == {}
    assert kernel.state("fixture.plugin").effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert "\"event\":\"unloaded\"" not in receipts.read_text(encoding="utf-8")


def test_repeated_requests_are_durable_no_ops_without_duplicate_disposal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(discover(root), receipt_path=receipts)
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()

    already_enabled = kernel.request_enable("fixture.plugin", command_id="enable-again")
    first_disable = kernel.request_disable("fixture.plugin", command_id="disable")
    second_disable = kernel.request_disable("fixture.plugin", command_id="disable-again")
    (draining,) = kernel.apply_turn_boundary()
    assert kernel.apply_turn_boundary() == ()

    assert already_enabled.outcome is LifecycleOutcome.NO_OP
    assert first_disable.outcome is LifecycleOutcome.ACCEPTED
    assert second_disable.outcome is LifecycleOutcome.NO_OP
    assert draining.event is LifecycleEvent.DRAINING
    assert held.resolve("fixture.dependent")()["disposals"] == ()
    (unloaded,) = held.close()
    assert unloaded.generation_after == 2
    assert [item.contribution_id for item in unloaded.disposals] == [
        "fixture.dependent",
        "fixture.base",
    ]
    assert [row["event"] for row in receipt_rows(receipts)].count("unloaded") == 1


def test_snapshot_is_frozen_and_survives_concurrent_boundary_change(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()
    observed: list[tuple[int, tuple[str, ...], str]] = []
    released = []
    ready = threading.Event()
    proceed = threading.Event()

    def hold_snapshot() -> None:
        ready.set()
        proceed.wait(timeout=5)
        observed.append(
            (
                held.generation,
                tuple(held.contributions),
                held.resolve("fixture.dependent")()["value"],
            )
        )
        released.extend(held.close())

    thread = threading.Thread(target=hold_snapshot)
    thread.start()
    assert ready.wait(timeout=5)
    kernel.request_disable("fixture.plugin", command_id="disable")
    (draining,) = kernel.apply_turn_boundary()
    assert draining.event is LifecycleEvent.DRAINING
    with kernel.snapshot() as current:
        assert current.generation == 2
        assert current.contributions == {}
    with pytest.raises(TypeError):
        held.contributions["other"] = held.contributions["fixture.base"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        held.generation = 99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        held._lease = object()  # type: ignore[assignment]
    proceed.set()
    thread.join(timeout=5)

    assert observed == [(1, ("fixture.base", "fixture.dependent"), "fixture-ok")]
    assert [item.event for item in released] == [LifecycleEvent.UNLOADED]


def test_dependency_disable_is_rejected_until_loaded_dependent_is_unloaded(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    dependency_body = """
def register(registrar):
    registrar.publish("dependency.value", "dependency", disposer=lambda: True, depends_on=())
"""
    dependent_body = """
def register(registrar):
    registrar.publish("dependent.value", "dependent", disposer=lambda: True, depends_on=())
"""
    write_plugin(
        root,
        "dependency",
        raw_manifest(
            "dependency.plugin",
            contributions=(("dependency.value", ()),),
        ),
        dependency_body,
    )
    write_plugin(
        root,
        "dependent",
        raw_manifest(
            "dependent.plugin",
            contributions=(("dependent.value", ()),),
            plugin_dependencies=("dependency.plugin",),
        ),
        dependent_body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("dependency.plugin", command_id="enable-dependency")
    kernel.request_enable("dependent.plugin", command_id="enable-dependent")
    assert [item.event for item in kernel.apply_turn_boundary()] == [
        LifecycleEvent.LOADED,
        LifecycleEvent.LOADED,
    ]

    rejected = kernel.request_disable(
        "dependency.plugin", command_id="disable-dependency-too-early"
    )

    assert rejected.event is LifecycleEvent.FAILED
    assert rejected.detail_code == "loaded_dependents_present"
    assert "dependent.plugin" in rejected.detail
    assert rejected.desired_state is PluginDesiredState.ENABLED
    with kernel.snapshot() as current:
        assert set(current.contributions) == {"dependency.value", "dependent.value"}

    kernel.request_disable("dependent.plugin", command_id="disable-dependent")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    accepted = kernel.request_disable(
        "dependency.plugin", command_id="disable-dependency-after-dependent"
    )
    assert accepted.outcome is LifecycleOutcome.ACCEPTED
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED


def test_dependency_disable_is_rejected_while_dependent_retains_ownership(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_cleanup = capability_plugins_module._LoaderOwnership.cleanup
    interrupted = False

    def interrupt_dependent_cleanup(ownership) -> bool:
        nonlocal interrupted
        if (
            not interrupted
            and ownership.namespace is not None
            and "dependent_plugin" in ownership.namespace
        ):
            interrupted = True
            raise KeyboardInterrupt()
        return original_cleanup(ownership)

    monkeypatch.setattr(
        capability_plugins_module._LoaderOwnership,
        "cleanup",
        interrupt_dependent_cleanup,
    )
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "dependency",
        raw_manifest(
            "dependency.plugin",
            contributions=(("dependency.value", ()),),
        ),
        "def register(registrar):\n"
        "    registrar.publish(\n"
        "        'dependency.value', 'dependency', disposer=lambda: True, depends_on=(),\n"
        "    )\n",
    )
    write_plugin(
        root,
        "dependent",
        raw_manifest(
            "dependent.plugin",
            contributions=(("dependent.value", ()),),
            plugin_dependencies=("dependency.plugin",),
        ),
        "def register(registrar):\n"
        "    registrar.publish(\n"
        "        'dependent.value', 'dependent', disposer=lambda: True, depends_on=(),\n"
        "    )\n",
    )
    kernel = CapabilityPluginKernel(
        discover(root),
        receipt_writer=lambda _receipt: None,
    )
    kernel.request_enable("dependency.plugin", command_id="enable-dependency")
    kernel.request_enable("dependent.plugin", command_id="enable-dependent")
    assert [item.event for item in kernel.apply_turn_boundary()] == [
        LifecycleEvent.LOADED,
        LifecycleEvent.LOADED,
    ]
    kernel.request_disable("dependent.plugin", command_id="disable-dependent")

    with pytest.raises(KeyboardInterrupt):
        kernel.apply_turn_boundary()

    dependent = kernel.state("dependent.plugin")
    assert dependent.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert dependent.contribution_ids == ()
    assert dependent.residual_contribution_ids == ()
    assert kernel._instances["dependent.plugin"].ownership is not None

    rejected = kernel.request_disable(
        "dependency.plugin",
        command_id="disable-dependency-with-undrained-dependent",
    )

    assert rejected.event is LifecycleEvent.FAILED
    assert rejected.detail_code == "loaded_dependents_present"
    assert "dependent.plugin" in rejected.detail
    assert kernel.state("dependency.plugin").effective_state is PluginEffectiveState.LOADED


def test_core_and_environment_requirements_fail_before_any_import(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    core_tripwire = tmp_path / "CORE_IMPORTED"
    env_tripwire = tmp_path / "ENV_IMPORTED"
    write_plugin(
        root,
        "core",
        raw_manifest(
            "core.plugin",
            contributions=(("core.value", ()),),
            core_requirement=">=9.0.0",
        ),
        f"""
from pathlib import Path
Path({str(core_tripwire)!r}).write_text("imported", encoding="utf-8")
def register(registrar):
    registrar.publish("core.value", "bad", disposer=lambda: True, depends_on=())
""",
    )
    write_plugin(
        root,
        "env",
        raw_manifest(
            "env.plugin",
            contributions=(("env.value", ()),),
            env=("REQUIRED_PLUGIN_SECRET",),
        ),
        f"""
from pathlib import Path
Path({str(env_tripwire)!r}).write_text("imported", encoding="utf-8")
def register(registrar):
    registrar.publish("env.value", "bad", disposer=lambda: True, depends_on=())
""",
    )
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(
        discover(root),
        receipt_path=receipts,
        core_version="1.6.0",
        environ={},
    )
    kernel.request_enable("core.plugin", command_id="core-command")
    kernel.request_enable("env.plugin", command_id="env-command")

    terminal = kernel.apply_turn_boundary()

    assert [item.detail_code for item in terminal] == [
        "core_version_incompatible",
        "environment_requirement_missing",
    ]
    assert not core_tripwire.exists()
    assert not env_tripwire.exists()
    serialized = receipts.read_text(encoding="utf-8")
    assert "REQUIRED_PLUGIN_SECRET=" not in serialized
    with kernel.snapshot() as current:
        assert "core.value" not in current.contributions
        assert "env.value" not in current.contributions


def test_filesystem_loader_owns_relative_import_namespace_until_lease_release(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    plugin_dir = root / "relative"
    package_dir = plugin_dir / "pkg"
    package_dir.mkdir(parents=True)
    raw = raw_manifest(
        "relative.plugin",
        contributions=(("relative.value", ()),),
        entrypoint="pkg.entry:register",
    )
    (plugin_dir / "extension.json").write_text(json.dumps(raw), encoding="utf-8")
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "helper.py").write_text(
        "VALUE = 'relative-ok'\n",
        encoding="utf-8",
    )
    (package_dir / "entry.py").write_text(
        "from .helper import VALUE\n"
        "def value():\n    return VALUE\n"
        "def register(registrar):\n"
        "    registrar.publish('relative.value', value, disposer=lambda: True, depends_on=())\n",
        encoding="utf-8",
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    before_owned_modules = {
        name for name in sys.modules if name.startswith("_homie_capability_")
    }
    kernel.request_enable("relative.plugin", command_id="relative-enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()
    value = held.resolve("relative.value")
    new_owned_modules = {
        name
        for name in sys.modules
        if name.startswith("_homie_capability_") and name not in before_owned_modules
    }
    helper_module = next(name for name in new_owned_modules if name.endswith(".pkg.helper"))
    namespace = helper_module.removesuffix(".pkg.helper")

    assert value() == "relative-ok"
    assert f"{namespace}.pkg.helper" in sys.modules
    kernel.request_disable("relative.plugin", command_id="relative-disable")
    (draining,) = kernel.apply_turn_boundary()
    assert draining.event is LifecycleEvent.DRAINING
    assert f"{namespace}.pkg.helper" in sys.modules

    (unloaded,) = held.close()

    assert unloaded.event is LifecycleEvent.UNLOADED
    assert not any(
        name == namespace or name.startswith(f"{namespace}.") for name in sys.modules
    )


def test_broken_plugin_is_redacted_and_does_not_poison_unrelated_load(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    secret = "PLAIN_ENV_SECRET_987654"
    monkeypatch.setenv("PLUGIN_SECRET", secret)
    broken_body = """
import os

def register(registrar):
    raise RuntimeError("plugin exploded " + os.environ["PLUGIN_SECRET"])
"""
    good_body = """
def value():
    return "good"

def dispose():
    return True

def register(registrar):
    registrar.publish("good.value", value, disposer=dispose, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "a-broken",
        raw_manifest(
            "broken.plugin",
            contributions=(("broken.value", ()),),
            env=("PLUGIN_SECRET",),
        ),
        broken_body,
    )
    write_plugin(
        root,
        "b-good",
        raw_manifest("good.plugin", contributions=(("good.value", ()),)),
        good_body,
    )
    receipts = tmp_path / "receipts.jsonl"
    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, root)]
    )
    assert discovery.errors == ()
    kernel = CapabilityPluginKernel(discovery.active_candidates, receipt_path=receipts)
    kernel.request_enable("broken.plugin", command_id="broken")
    kernel.request_enable("good.plugin", command_id="good")

    terminal = kernel.apply_turn_boundary()

    assert [item.event for item in terminal] == [LifecycleEvent.FAILED, LifecycleEvent.LOADED]
    with kernel.snapshot() as current:
        assert current.resolve("good.value")() == "good"
        assert "broken.value" not in current.contributions
    serialized = (
        receipts.read_text(encoding="utf-8")
        + caplog.text
        + str(discovery.catalog())
        + repr(kernel.state("good.plugin"))
    )
    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert secret not in kernel.state("broken.plugin").detail


def test_hostile_exception_stringification_is_isolated_without_pending_retry(
    tmp_path: Path,
) -> None:
    body = """
class HostileMeta(type):
    def __getattribute__(cls, name):
        if name == "__name__":
            raise SystemExit("hostile exception metadata escaped")
        return super().__getattribute__(name)

class HostileError(Exception, metaclass=HostileMeta):
    def __str__(self):
        raise SystemExit("hostile exception formatting escaped")

def register(_registrar):
    raise HostileError()
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")

    (failed,) = kernel.apply_turn_boundary()

    assert failed.event is LifecycleEvent.FAILED
    assert failed.detail_code == "plugin_registration_failed"
    assert "Unprintable detail" in failed.detail
    assert kernel.state("fixture.plugin").effective_state is PluginEffectiveState.UNLOADED
    assert kernel._pending == {}
    kernel.close()


def test_plugin_controlled_error_code_cannot_expose_required_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "<REDACTED-openai>"
    monkeypatch.setenv("PLUGIN_SECRET", secret)
    body = """
import os
from runtime.capability_plugins import CapabilityPluginError

def register(_registrar):
    raise CapabilityPluginError(os.environ["PLUGIN_SECRET"], "safe detail")
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(
            contributions=(("fixture.base", ()),),
            env=("PLUGIN_SECRET",),
        ),
        body,
    )
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(discover(root), receipt_path=receipts)
    kernel.request_enable("fixture.plugin", command_id="enable")

    (failed,) = kernel.apply_turn_boundary()

    assert failed.detail_code == "plugin_error"
    assert kernel.state("fixture.plugin").error_code == "plugin_error"
    assert secret not in receipts.read_text(encoding="utf-8")
    kernel.close()


@pytest.mark.parametrize(
    "broken_body",
    [
        "raise SystemExit('import exit')\n",
        "def register(registrar):\n    raise SystemExit('register exit')\n",
    ],
)
def test_system_exit_during_import_or_register_isolated_from_unrelated_plugin(
    tmp_path: Path,
    broken_body: str,
) -> None:
    good_body = """
def value():
    return "good"

def register(registrar):
    registrar.publish("good.value", value, disposer=lambda: True, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "a-broken",
        raw_manifest("broken.plugin", contributions=(("broken.value", ()),)),
        broken_body,
    )
    write_plugin(
        root,
        "b-good",
        raw_manifest("good.plugin", contributions=(("good.value", ()),)),
        good_body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("broken.plugin", command_id="broken-enable")
    kernel.request_enable("good.plugin", command_id="good-enable")

    terminals = kernel.apply_turn_boundary()

    assert [item.event for item in terminals] == [LifecycleEvent.FAILED, LifecycleEvent.LOADED]
    broken = kernel.state("broken.plugin")
    assert broken.lifecycle_state is PluginLifecycleState.FAILED
    assert broken.effective_state is PluginEffectiveState.UNLOADED
    assert broken.contribution_ids == ()
    with kernel.snapshot() as current:
        assert current.resolve("good.value")() == "good"
        assert "broken.value" not in current.contributions
    kernel.request_disable("good.plugin", command_id="good-disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    kernel.close()


def test_filesystem_entrypoint_attribute_failure_cleans_owned_namespace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        "def __getattr__(_name):\n    raise SystemExit('attribute lookup exit')\n",
    )
    before_owned_modules = {
        name for name in sys.modules if name.startswith("_homie_capability_")
    }
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")

    (failed,) = kernel.apply_turn_boundary()

    assert failed.event is LifecycleEvent.FAILED
    state = kernel.state("fixture.plugin")
    assert state.effective_state is PluginEffectiveState.UNLOADED
    assert state.lifecycle_state is PluginLifecycleState.FAILED
    assert before_owned_modules == {
        name for name in sys.modules if name.startswith("_homie_capability_")
    }
    kernel.close()


def test_filesystem_entrypoint_rejects_ambiguous_module_and_package_before_import(
    tmp_path: Path,
) -> None:
    tripwire = tmp_path / "PACKAGE_IMPORTED"
    root = tmp_path / "extensions"
    plugin_dir = write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        "def register(registrar):\n    return None\n",
    )
    package_dir = plugin_dir / "plugin"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(tripwire)!r}).write_text('imported', encoding='utf-8')\n"
        "def register(registrar):\n    return None\n",
        encoding="utf-8",
    )
    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, root)]
    )

    assert discovery.active_candidates == ()
    assert [item.code for item in discovery.errors] == [
        "entrypoint_module_ambiguous"
    ]
    assert not tripwire.exists()


def test_filesystem_entrypoint_executes_exact_resolved_artifact_not_meta_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        (
            "def register(_registrar):\n    return None\n"
            "register.origin = 'resolved-file'\n"
        ),
    )
    candidate = discover(root)[0]

    class AlternateLoader(importlib.abc.Loader):
        def create_module(self, _spec):
            return None

        def exec_module(self, module) -> None:
            def register(_registrar):
                return None

            register.origin = "meta-path"  # type: ignore[attr-defined]
            module.register = register

    class AlternateFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, _path, _target=None):
            if fullname.startswith("_homie_capability_") and fullname.endswith(".plugin"):
                return importlib.util.spec_from_loader(
                    fullname,
                    AlternateLoader(),
                    origin="alternate://artifact",
                )
            return None

    finder = AlternateFinder()
    sys.meta_path.insert(0, finder)
    loaded = None
    try:
        loaded = capability_plugins_module._load_frozen_entrypoint(candidate)
        assert loaded.register.origin == "resolved-file"  # type: ignore[attr-defined]
    finally:
        sys.meta_path.remove(finder)
        if loaded is not None:
            assert loaded.ownership.cleanup() is True


def test_discovery_authorizes_exact_code_bytes_across_boundary_and_recovery(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    plugin_dir = write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        "def register(registrar):\n"
        "    registrar.publish(\n"
        "        'fixture.base', 'artifact-a', disposer=lambda: True, depends_on=(),\n"
        "    )\n",
    )
    candidate_a = discover(root)[0]
    receipts = tmp_path / "receipts.jsonl"
    first = CapabilityPluginKernel((candidate_a,), receipt_path=receipts)
    first.request_enable("fixture.plugin", command_id="enable-a")

    (plugin_dir / "plugin.py").write_text(
        "def register(registrar):\n"
        "    registrar.publish(\n"
        "        'fixture.base', 'artifact-b', disposer=lambda: True, depends_on=(),\n"
        "    )\n",
        encoding="utf-8",
    )
    candidate_b = discover(root)[0]

    assert candidate_a.provenance_id != candidate_b.provenance_id
    assert candidate_a.artifact_fingerprint != candidate_b.artifact_fingerprint
    assert first.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    with first.snapshot() as current:
        assert current.resolve("fixture.base").read() == "artifact-a"
    first.request_disable("fixture.plugin", command_id="disable-a")
    assert first.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    first.close()

    pending_receipts = tmp_path / "pending.jsonl"
    pending = CapabilityPluginKernel((candidate_a,), receipt_path=pending_receipts)
    pending.request_enable("fixture.plugin", command_id="pending-a")
    assert pending._journal is not None
    pending._journal.close()
    pending._journal = None
    pending._closed = True

    with pytest.raises(ReceiptPersistenceError):
        CapabilityPluginKernel((candidate_b,), receipt_path=pending_receipts)
    proof = LockedLifecycleJournal(pending_receipts)
    proof.close()


def test_absolute_and_dynamic_self_imports_resolve_only_frozen_artifacts() -> None:
    raw = raw_manifest(
        "entry.plugin",
        source="python_entry_point",
        contributions=(("entry.value", ()),),
        entrypoint="entry_plugin:register",
    )
    manifest = parse_capability_manifest(
        raw,
        physical_source=ManifestSource.PYTHON_ENTRY_POINT,
    )
    candidate = CapabilityPluginCandidate(
        manifest=manifest,
        location_key="entry-distribution:absolute-import-proof",
        artifacts=(
            CapabilityPluginArtifact(
                "entry_plugin/__init__.py",
                b"import entry_plugin.helper\n"
                b"from entry_plugin.helper import VALUE as STATIC_VALUE\n"
                b"import importlib\n"
                b"from importlib import import_module\n"
                b"PLAIN_VALUE = entry_plugin.helper.VALUE\n"
                b"DYNAMIC_VALUE = importlib.import_module(\n"
                b"    'entry_plugin.dynamic'\n"
                b").VALUE\n"
                b"ALIASED_VALUE = import_module('entry_plugin.dynamic').VALUE\n"
                b"def register(registrar):\n"
                b"    registrar.publish(\n"
                b"        'entry.value', (\n"
                b"            PLAIN_VALUE, STATIC_VALUE, DYNAMIC_VALUE, ALIASED_VALUE,\n"
                b"        ),\n"
                b"        disposer=lambda: True, depends_on=(),\n"
                b"    )\n",
            ),
            CapabilityPluginArtifact(
                "entry_plugin/helper.py",
                b"VALUE = 'FROZEN_STATIC'\n",
            ),
            CapabilityPluginArtifact(
                "entry_plugin/dynamic.py",
                b"VALUE = 'FROZEN_DYNAMIC'\n",
            ),
        ),
    )
    live_package = ModuleType("entry_plugin")
    live_package.__path__ = []  # type: ignore[attr-defined]
    live_helper = ModuleType("entry_plugin.helper")
    live_helper.VALUE = "LIVE_STATIC"  # type: ignore[attr-defined]
    live_dynamic = ModuleType("entry_plugin.dynamic")
    live_dynamic.VALUE = "LIVE_DYNAMIC"  # type: ignore[attr-defined]
    prior = {
        name: sys.modules.get(name)
        for name in ("entry_plugin", "entry_plugin.helper", "entry_plugin.dynamic")
    }
    sys.modules["entry_plugin"] = live_package
    sys.modules["entry_plugin.helper"] = live_helper
    sys.modules["entry_plugin.dynamic"] = live_dynamic
    loaded = None
    try:
        loaded = capability_plugins_module._load_frozen_entrypoint(candidate)
        registrar = capability_plugins_module.StagedRegistrar(manifest)
        loaded.register(registrar)

        assert registrar.exact_registrations()["entry.value"].value == (
            "FROZEN_STATIC",
            "FROZEN_STATIC",
            "FROZEN_DYNAMIC",
            "FROZEN_DYNAMIC",
        )
        assert sys.modules["entry_plugin.helper"] is live_helper
        assert sys.modules["entry_plugin.dynamic"] is live_dynamic
    finally:
        if loaded is not None:
            assert loaded.ownership.cleanup() is True
        for name, previous in prior.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def test_import_interrupt_propagates_cleanly_when_namespace_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        "def register(registrar):\n    return None\n",
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")

    def interrupt_import(*_args, **_kwargs):
        raise KeyboardInterrupt("PLUGIN_IMPORT_SECRET")

    def fail_cleanup(_ownership) -> bool:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(
        capability_plugins_module._FrozenArtifactLoader,
        "exec_module",
        interrupt_import,
    )
    monkeypatch.setattr(capability_plugins_module._LoaderOwnership, "cleanup", fail_cleanup)

    with pytest.raises(KeyboardInterrupt) as interrupted:
        kernel.apply_turn_boundary()

    assert interrupted.value.args == ()
    assert interrupted.value.__cause__ is None
    assert interrupted.value.__context__ is None
    assert "PLUGIN_IMPORT_SECRET" not in retained_exception_strings(interrupted.value)
    state = kernel.state("fixture.plugin")
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.error_code == "module_cleanup_unproven"
    assert kernel._pending == {}
    assert kernel._journal is not None
    kernel._journal.close()


def test_system_exit_during_disposal_becomes_restart_required_without_poisoning_other_plugin(
    tmp_path: Path,
) -> None:
    broken_body = """
def dispose():
    raise SystemExit("dispose exit")

def register(registrar):
    registrar.publish("broken.value", "broken", disposer=dispose, depends_on=())
"""
    good_body = """
def value():
    return "good"

def register(registrar):
    registrar.publish("good.value", value, disposer=lambda: True, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "a-broken",
        raw_manifest("broken.plugin", contributions=(("broken.value", ()),)),
        broken_body,
    )
    write_plugin(
        root,
        "b-good",
        raw_manifest("good.plugin", contributions=(("good.value", ()),)),
        good_body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("broken.plugin", command_id="broken-enable")
    kernel.request_enable("good.plugin", command_id="good-enable")
    assert [item.event for item in kernel.apply_turn_boundary()] == [
        LifecycleEvent.LOADED,
        LifecycleEvent.LOADED,
    ]
    kernel.request_disable("broken.plugin", command_id="broken-disable")

    (terminal,) = kernel.apply_turn_boundary()

    assert terminal.event is LifecycleEvent.RESTART_REQUIRED
    assert terminal.detail_code == "disposal_unproven"
    broken = kernel.state("broken.plugin")
    assert broken.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert broken.residual_contribution_ids == ("broken.value",)
    with kernel.snapshot() as current:
        assert current.resolve("good.value")() == "good"
        assert "broken.value" not in current.contributions
    kernel.request_disable("good.plugin", command_id="good-disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    assert kernel._journal is not None
    kernel._journal.close()


def test_keyboard_interrupt_propagates_after_registration_state_is_quarantined(
    tmp_path: Path,
) -> None:
    body = """
def register(registrar):
    raise KeyboardInterrupt("PLUGIN_INTERRUPT_SECRET")
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")

    with pytest.raises(KeyboardInterrupt) as interrupted:
        kernel.apply_turn_boundary()

    assert interrupted.value.args == ()
    assert interrupted.value.__cause__ is None
    assert interrupted.value.__context__ is None
    assert "PLUGIN_INTERRUPT_SECRET" not in retained_exception_strings(interrupted.value)
    state = kernel.state("fixture.plugin")
    assert state.lifecycle_state is PluginLifecycleState.FAILED
    assert state.effective_state is PluginEffectiveState.UNLOADED
    assert state.contribution_ids == ()
    kernel.close()


def test_keyboard_interrupt_during_unload_quarantines_residual_without_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"base": 0, "dependent": 0}
    monkeypatch.setattr(builtins, "INTERRUPT_DISPOSAL_COUNTS", counts, raising=False)
    body = """
import builtins

def dispose_base():
    builtins.INTERRUPT_DISPOSAL_COUNTS["base"] += 1
    raise KeyboardInterrupt()

def dispose_dependent():
    builtins.INTERRUPT_DISPOSAL_COUNTS["dependent"] += 1
    return True

def register(registrar):
    registrar.publish("fixture.base", "base", disposer=dispose_base, depends_on=())
    registrar.publish(
        "fixture.dependent",
        "dependent",
        disposer=dispose_dependent,
        depends_on=("fixture.base",),
    )
"""
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), body)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    kernel.request_disable("fixture.plugin", command_id="disable")

    with pytest.raises(KeyboardInterrupt):
        kernel.apply_turn_boundary()

    assert counts == {"base": 1, "dependent": 1}
    state = kernel.state("fixture.plugin")
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.contribution_ids == ("fixture.base",)
    assert state.residual_contribution_ids == ("fixture.base",)
    assert kernel.apply_turn_boundary() == ()
    assert counts == {"base": 1, "dependent": 1}
    refused = kernel.request_disable("fixture.plugin", command_id="disable-again")
    assert refused.event is LifecycleEvent.FAILED
    assert refused.detail_code == "restart_required_disable_refused"
    with pytest.raises(CapabilityPluginError) as close_refused:
        kernel.close()
    assert close_refused.value.code == "plugins_not_drained"
    ownership = kernel._instances["fixture.plugin"].ownership
    assert ownership is not None
    assert ownership.cleanup() is True
    assert kernel._journal is not None
    kernel._journal.close()


def test_keyboard_interrupt_during_failed_load_rollback_quarantines_residual(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"base": 0}
    monkeypatch.setattr(builtins, "INTERRUPT_ROLLBACK_COUNTS", counts, raising=False)
    body = """
import builtins

def dispose_base():
    builtins.INTERRUPT_ROLLBACK_COUNTS["base"] += 1
    raise KeyboardInterrupt()

def register(registrar):
    registrar.publish("fixture.base", "base", disposer=dispose_base, depends_on=())
    raise RuntimeError("RAW_PLUGIN_SECRET_123")
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")

    with pytest.raises(KeyboardInterrupt) as interrupted:
        kernel.apply_turn_boundary()

    assert interrupted.value.args == ()
    assert interrupted.value.__cause__ is None
    assert interrupted.value.__context__ is None
    assert counts == {"base": 1}
    state = kernel.state("fixture.plugin")
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.contribution_ids == ("fixture.base",)
    assert state.residual_contribution_ids == ("fixture.base",)
    assert kernel.apply_turn_boundary() == ()
    assert counts == {"base": 1}
    with pytest.raises(CapabilityPluginError) as close_refused:
        kernel.close()
    assert close_refused.value.code == "plugins_not_drained"
    ownership = kernel._instances["fixture.plugin"].ownership
    assert ownership is not None
    assert ownership.cleanup() is True
    assert kernel._journal is not None
    kernel._journal.close()


def test_keyboard_interrupt_during_load_receipt_write_revokes_published_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"dispose": 0}
    monkeypatch.setattr(builtins, "INTERRUPT_LOAD_RECEIPT_COUNTS", counts, raising=False)
    body = """
import builtins

def dispose():
    builtins.INTERRUPT_LOAD_RECEIPT_COUNTS["dispose"] += 1
    return True

def register(registrar):
    registrar.publish("fixture.base", "value", disposer=dispose, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )

    def writer(receipt) -> None:
        if receipt.event is LifecycleEvent.LOADED:
            raise KeyboardInterrupt("PERSISTENCE_SECRET")

    kernel = CapabilityPluginKernel(discover(root), receipt_writer=writer)
    kernel.request_enable("fixture.plugin", command_id="enable")

    with pytest.raises(KeyboardInterrupt) as interrupted:
        kernel.apply_turn_boundary()

    assert interrupted.value.args == ()
    assert interrupted.value.__cause__ is None
    assert interrupted.value.__context__ is None
    assert "PERSISTENCE_SECRET" not in retained_exception_strings(interrupted.value)
    state = kernel.state("fixture.plugin")
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.contribution_ids == ()
    assert state.residual_contribution_ids == ()
    assert counts == {"dispose": 1}
    assert kernel._bindings == {}
    assert kernel._pending == {}
    assert kernel.apply_turn_boundary() == ()
    with kernel.snapshot() as current:
        assert current.contributions == {}


def test_interrupt_after_successful_load_cannot_leave_retryable_pending_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        """
def register(registrar):
    registrar.publish("fixture.base", "value", disposer=lambda: True, depends_on=())
""",
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    original_load = kernel._load_instance

    def interrupt_after_success(*args, **kwargs):
        original_load(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(kernel, "_load_instance", interrupt_after_success)

    with pytest.raises(KeyboardInterrupt):
        kernel.apply_turn_boundary()

    assert kernel._pending == {}
    assert kernel.state("fixture.plugin").effective_state is PluginEffectiveState.LOADED
    assert kernel.apply_turn_boundary() == ()
    with kernel.snapshot() as current:
        assert current.resolve("fixture.base").read() == "value"
    monkeypatch.setattr(kernel, "_load_instance", original_load)
    kernel.request_disable("fixture.plugin", command_id="disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    kernel.close()


def test_keyboard_interrupt_during_draining_receipt_write_quarantines_without_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"dispose": 0}
    monkeypatch.setattr(builtins, "INTERRUPT_DRAINING_RECEIPT_COUNTS", counts, raising=False)
    body = """
import builtins

def dispose():
    builtins.INTERRUPT_DRAINING_RECEIPT_COUNTS["dispose"] += 1
    return True

def register(registrar):
    registrar.publish("fixture.base", "value", disposer=dispose, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )

    def writer(receipt) -> None:
        if receipt.event is LifecycleEvent.DRAINING:
            raise KeyboardInterrupt()

    kernel = CapabilityPluginKernel(discover(root), receipt_writer=writer)
    kernel.request_enable("fixture.plugin", command_id="enable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    held = kernel.snapshot()
    kernel.request_disable("fixture.plugin", command_id="disable")

    with pytest.raises(KeyboardInterrupt):
        kernel.apply_turn_boundary()

    state = kernel.state("fixture.plugin")
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.contribution_ids == ("fixture.base",)
    assert state.residual_contribution_ids == ("fixture.base",)
    assert counts == {"dispose": 0}
    assert kernel._pending == {}
    assert kernel.apply_turn_boundary() == ()
    assert held.close() == ()
    assert counts == {"dispose": 0}
    ownership = kernel._instances["fixture.plugin"].ownership
    assert ownership is not None
    assert ownership.cleanup() is True


def test_keyboard_interrupt_during_module_cleanup_never_retries_successful_disposer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"dispose": 0, "cleanup": 0}
    monkeypatch.setattr(builtins, "INTERRUPT_CLEANUP_COUNTS", counts, raising=False)
    original_cleanup = capability_plugins_module._LoaderOwnership.cleanup

    def interrupt_cleanup(ownership) -> bool:
        counts["cleanup"] += 1
        if counts["cleanup"] == 1:
            raise KeyboardInterrupt()
        return original_cleanup(ownership)

    monkeypatch.setattr(
        capability_plugins_module._LoaderOwnership,
        "cleanup",
        interrupt_cleanup,
    )
    body = """
import builtins

def dispose():
    builtins.INTERRUPT_CLEANUP_COUNTS["dispose"] += 1
    return True

def register(registrar):
    registrar.publish("fixture.base", "value", disposer=dispose, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_writer=lambda _receipt: None)
    kernel.request_enable("fixture.plugin", command_id="enable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    kernel.request_disable("fixture.plugin", command_id="disable")

    with pytest.raises(KeyboardInterrupt):
        kernel.apply_turn_boundary()

    state = kernel.state("fixture.plugin")
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.contribution_ids == ()
    assert state.residual_contribution_ids == ()
    assert counts == {"dispose": 1, "cleanup": 1}
    assert kernel._pending == {}
    assert kernel.apply_turn_boundary() == ()
    assert counts == {"dispose": 1, "cleanup": 1}
    ownership = kernel._instances["fixture.plugin"].ownership
    assert ownership is not None
    assert ownership.cleanup() is True


def test_keyboard_interrupt_during_unloaded_receipt_write_does_not_retry_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"dispose": 0}
    monkeypatch.setattr(builtins, "INTERRUPT_UNLOADED_RECEIPT_COUNTS", counts, raising=False)
    body = """
import builtins

def dispose():
    builtins.INTERRUPT_UNLOADED_RECEIPT_COUNTS["dispose"] += 1
    return True

def register(registrar):
    registrar.publish("fixture.base", "value", disposer=dispose, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )

    def writer(receipt) -> None:
        if receipt.event is LifecycleEvent.UNLOADED:
            raise KeyboardInterrupt()

    kernel = CapabilityPluginKernel(discover(root), receipt_writer=writer)
    kernel.request_enable("fixture.plugin", command_id="enable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    kernel.request_disable("fixture.plugin", command_id="disable")

    with pytest.raises(KeyboardInterrupt):
        kernel.apply_turn_boundary()

    state = kernel.state("fixture.plugin")
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.contribution_ids == ()
    assert counts == {"dispose": 1}
    assert kernel._pending == {}
    assert kernel.apply_turn_boundary() == ()
    assert counts == {"dispose": 1}


def test_receipt_failure_before_effects_does_not_queue_or_import(tmp_path: Path) -> None:
    tripwire = tmp_path / "IMPORTED"
    body = f"""
from pathlib import Path
Path({str(tripwire)!r}).write_text("imported", encoding="utf-8")

def register(registrar):
    raise AssertionError("must not load")
"""
    raw = raw_manifest(contributions=(("fixture.base", ()),))
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw, body)

    def fail_writer(_receipt) -> None:
        raise OSError("sink unavailable")

    kernel = CapabilityPluginKernel(discover(root), receipt_writer=fail_writer)
    failed = kernel.request_enable("fixture.plugin", command_id="no-receipt")

    assert failed.outcome is LifecycleOutcome.FAILED
    assert failed.detail_code == "receipt_write_failed"
    assert kernel.apply_turn_boundary() == ()
    assert kernel.generation == 0
    assert not tripwire.exists()
    state = kernel.state("fixture.plugin")
    assert state.desired_state is PluginDesiredState.DISABLED
    assert state.lifecycle_state is PluginLifecycleState.DISCOVERED


def test_load_receipt_failure_rolls_back_and_requires_restart(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    seen: list[LifecycleTransition] = []

    def writer(receipt) -> None:
        seen.append(receipt.requested_transition)
        if receipt.requested_transition is LifecycleTransition.LOAD:
            raise OSError("load receipt failed")

    kernel = CapabilityPluginKernel(discover(root), receipt_writer=writer)
    kernel.request_enable("fixture.plugin", command_id="enable")
    (terminal,) = kernel.apply_turn_boundary()

    assert seen == [LifecycleTransition.ENABLE, LifecycleTransition.LOAD]
    assert terminal.event is LifecycleEvent.RESTART_REQUIRED
    assert terminal.detail_code == "receipt_write_failed"
    with kernel.snapshot() as current:
        assert current.contributions == {}
    assert kernel.state("fixture.plugin").lifecycle_state is PluginLifecycleState.RESTART_REQUIRED


def test_terminal_receipt_failure_after_plugin_rollback_requires_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"dispose": 0}
    monkeypatch.setattr(builtins, "FAILED_LOAD_ROLLBACK_COUNTS", counts, raising=False)
    body = """
import builtins

def dispose():
    builtins.FAILED_LOAD_ROLLBACK_COUNTS["dispose"] += 1
    return True

def register(registrar):
    registrar.publish("fixture.base", "value", disposer=dispose, depends_on=())
    raise RuntimeError("registration failed")
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )

    def writer(receipt) -> None:
        if receipt.requested_transition is LifecycleTransition.LOAD:
            raise OSError("terminal receipt failed")

    kernel = CapabilityPluginKernel(discover(root), receipt_writer=writer)
    kernel.request_enable("fixture.plugin", command_id="enable")

    (terminal,) = kernel.apply_turn_boundary()

    assert terminal.event is LifecycleEvent.RESTART_REQUIRED
    assert terminal.outcome is LifecycleOutcome.FAILED
    assert terminal.restart_required is True
    assert terminal.detail_code == "receipt_write_failed"
    assert counts == {"dispose": 1}
    state = kernel.state("fixture.plugin")
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.contribution_ids == ()
    assert kernel._bindings == {}


def test_receipt_failure_after_failed_disposer_stays_restart_required(tmp_path: Path) -> None:
    body = """
def dispose():
    return False

def register(registrar):
    registrar.publish("fixture.base", "value", disposer=dispose, depends_on=())
"""
    raw = raw_manifest(contributions=(("fixture.base", ()),))
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw, body)

    def writer(receipt) -> None:
        if receipt.requested_transition is LifecycleTransition.UNLOAD:
            raise OSError("terminal receipt failed")

    kernel = CapabilityPluginKernel(discover(root), receipt_writer=writer)
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    kernel.request_disable("fixture.plugin", command_id="disable")

    (terminal,) = kernel.apply_turn_boundary()

    assert terminal.event is LifecycleEvent.RESTART_REQUIRED
    assert terminal.outcome is LifecycleOutcome.FAILED
    assert terminal.detail_code == "receipt_write_failed"
    with kernel.snapshot() as current:
        assert current.contributions == {}
    assert kernel.state("fixture.plugin").lifecycle_state is PluginLifecycleState.RESTART_REQUIRED


def test_default_receipt_path_resolves_config_at_call_time(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    kernel = CapabilityPluginKernel(discover(root))
    redirected = tmp_path / "late-data"
    monkeypatch.setattr(config, "DATA_DIR", redirected)

    kernel.request_enable("fixture.plugin", command_id="late-config")

    path = redirected / "capability_plugin_lifecycle.jsonl"
    assert path.is_file()
    assert receipt_rows(path)[0]["command_id"] == "late-config"


def test_jsonl_success_flushes_through_fsync(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    fsync_calls: list[int] = []
    monkeypatch.setattr(
        capability_plugin_journal_module.os,
        "fsync",
        lambda file_descriptor: fsync_calls.append(file_descriptor),
    )
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(discover(root), receipt_path=receipts)

    result = kernel.request_enable("fixture.plugin", command_id="fsync-proof")

    assert result.outcome is LifecycleOutcome.ACCEPTED
    assert fsync_calls
    assert receipt_rows(receipts)[0]["command_id"] == "fsync-proof"


def test_relative_journal_path_is_frozen_absolute_before_cwd_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_cwd = tmp_path / "original"
    later_cwd = tmp_path / "later"
    original_cwd.mkdir()
    later_cwd.mkdir()
    monkeypatch.chdir(original_cwd)
    journal = LockedLifecycleJournal(Path("state") / "receipts.jsonl")
    frozen_path = journal.path

    monkeypatch.chdir(later_cwd)
    journal.append_request(journal_payload("stable-path"))

    assert frozen_path.is_absolute()
    assert frozen_path == original_cwd / "state" / "receipts.jsonl"
    assert frozen_path.is_file()
    assert not (later_cwd / "state" / "receipts.jsonl").exists()
    journal.close()


def test_lifecycle_receipt_schema_rejects_coercion_before_recovery_authority() -> None:
    valid = valid_lifecycle_record()
    assert PluginLifecycleReceipt.from_dict(valid).restart_required is False
    invalid_records: list[dict[str, object]] = []
    for key, value in (
        ("command_id", 456),
        ("event_id", 1.9),
        ("generation_after", 1.9),
        ("contribution_ids", "ab"),
        ("restart_required", "false"),
        ("timestamp", "2026-08-22T00:00:00"),
    ):
        record = dict(valid)
        record[key] = value
        invalid_records.append(record)
    record = dict(valid)
    record["unexpected"] = True
    invalid_records.append(record)
    record = dict(valid)
    record.pop("journal_owner_id")
    invalid_records.append(record)
    for key, value in (
        ("requested_transition", "unload"),
        ("event", "loaded"),
        ("desired_state", "disabled"),
        ("effective_state", "loaded"),
        ("lifecycle_state", "restart_required"),
        ("restart_required", True),
    ):
        record = dict(valid)
        record[key] = value
        invalid_records.append(record)

    for record in invalid_records:
        with pytest.raises(ReceiptPersistenceError):
            PluginLifecycleReceipt.from_dict(record)


def test_invalid_receipt_enum_erases_secret_bearing_validation_exception() -> None:
    secret = "<REDACTED-openai>"
    payload = valid_lifecycle_record()
    payload["source"] = secret

    with pytest.raises(ReceiptPersistenceError) as rejected:
        PluginLifecycleReceipt.from_dict(payload)

    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert secret not in retained_exception_strings(rejected.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_id", "<REDACTED-openai>"),
        ("plugin_id", "<REDACTED-openai>"),
        ("plugin_version", "<REDACTED-openai>"),
        ("contribution_ids", ["<REDACTED-openai>"]),
        ("journal_owner_id", "<REDACTED-openai>"),
    ],
)
def test_historical_receipt_rejects_secret_shaped_identifiers_without_retention(
    field: str,
    value: object,
) -> None:
    secret = (
        value[0]
        if type(value) is list
        else value
    )
    assert type(secret) is str
    payload = valid_lifecycle_record()
    payload[field] = value

    with pytest.raises(ReceiptPersistenceError) as rejected:
        PluginLifecycleReceipt.from_dict(payload)

    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert secret not in retained_exception_strings(rejected.value)


def test_secret_shaped_command_id_is_rejected_before_receipt_persistence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(discover(root), receipt_path=receipts)
    secret = "<REDACTED-openai>"

    with pytest.raises(CapabilityPluginError) as rejected:
        kernel.request_enable("fixture.plugin", command_id=secret)

    assert rejected.value.code == "invalid_command_id"
    assert secret not in str(rejected.value)
    assert not receipts.exists()
    kernel.close()


@pytest.mark.parametrize(
    "corruption",
    [
        {"schema_version": 3, "phase": "terminal", "event": "failed", "outcome": "failed"},
        {"plugin_version": "9.9.9"},
        {"source": "project"},
        {"contribution_ids": ["wrong.value"]},
        {"plugin_provenance_id": "bundled:11111111111111111111"},
    ],
)
def test_recovery_rejects_corrupt_or_wrong_manifest_authority(
    tmp_path: Path,
    corruption: dict[str, object],
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    request = valid_lifecycle_record()
    request["journal_owner_id"] = "prior-owner"
    request["plugin_provenance_id"] = candidates[0].provenance_id
    rows = [request]
    if "schema_version" in corruption:
        terminal = dict(request)
        terminal.update(corruption)
        terminal["event_id"] = 2
        rows.append(terminal)
    else:
        request.update(corruption)
    receipts.write_text(
        "".join(f"{json.dumps(row, sort_keys=True)}\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(ReceiptPersistenceError):
        CapabilityPluginKernel(candidates, receipt_path=receipts)

    proof = LockedLifecycleJournal(receipts)
    proof.close()


def test_recovery_rejects_command_history_identity_changes(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    request = valid_lifecycle_record()
    request["journal_owner_id"] = "prior-owner"
    request["plugin_provenance_id"] = candidates[0].provenance_id
    terminal = dict(request)
    terminal.update(
        {
            "event_id": 2,
            "plugin_version": "9.9.9",
            "phase": "terminal",
            "event": "failed",
            "outcome": "failed",
            "lifecycle_state": "failed",
        }
    )
    receipts.write_text(
        f"{json.dumps(request, sort_keys=True)}\n{json.dumps(terminal, sort_keys=True)}\n",
        encoding="utf-8",
    )
    with pytest.raises(ReceiptPersistenceError):
        CapabilityPluginKernel(candidates, receipt_path=receipts)

    proof = LockedLifecycleJournal(receipts)
    proof.close()


def test_recovery_rejects_same_owner_records_after_terminal(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    request = valid_lifecycle_record()
    request["plugin_provenance_id"] = candidates[0].provenance_id
    request["journal_owner_id"] = "same-owner"
    terminal = dict(request)
    terminal.update(
        {
            "event_id": 1,
            "requested_transition": "load",
            "phase": "terminal",
            "event": "loaded",
            "effective_state": "loaded",
            "lifecycle_state": "loaded",
            "generation_after": 1,
            "outcome": "succeeded",
        }
    )
    request["event_id"] = 2
    request["generation_before"] = 1
    request["generation_after"] = 1
    receipts.write_text(
        f"{json.dumps(terminal, sort_keys=True)}\n{json.dumps(request, sort_keys=True)}\n",
        encoding="utf-8",
    )

    with pytest.raises(ReceiptPersistenceError):
        CapabilityPluginKernel(candidates, receipt_path=receipts)

    proof = LockedLifecycleJournal(receipts)
    proof.close()


def test_recovery_fence_terminalizes_all_incomplete_claims_for_plugin(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        SUCCESS_PLUGIN,
    )
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    enable = valid_lifecycle_record()
    enable["plugin_provenance_id"] = candidates[0].provenance_id
    enable["journal_owner_id"] = "prior-enable-owner"
    fenced_disable = dict(enable)
    fenced_disable.update(
        {
            "command_id": "fenced-disable",
            "event_id": 2,
            "command_transition": "disable",
            "requested_transition": "disable",
            "event": "unload_requested",
            "desired_state": "disabled",
            "lifecycle_state": "unloaded",
            "detail_code": "supersession_replacement",
            "detail": "Atomic replacement request; incomplete recovery must fail closed",
            "journal_owner_id": "prior-disable-owner",
        }
    )
    receipts.write_text(
        f"{json.dumps(enable, sort_keys=True)}\n"
        f"{json.dumps(fenced_disable, sort_keys=True)}\n",
        encoding="utf-8",
    )

    recovered = CapabilityPluginKernel(candidates, receipt_path=receipts)

    state = recovered.state("fixture.plugin")
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.error_code == "recovered_supersession_fence"
    assert recovered._pending == {}
    assert recovered.apply_turn_boundary() == ()
    rows = receipt_rows(receipts)
    assert [row["event"] for row in rows[-2:]] == [
        "restart_required",
        "restart_required",
    ]
    assert recovered._journal is not None
    recovered._journal.close()


def test_persisted_fence_remains_dominant_after_partial_recovery_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        SUCCESS_PLUGIN,
    )
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    fenced = valid_lifecycle_record()
    fenced.update(
        {
            "plugin_provenance_id": candidates[0].provenance_id,
            "command_id": "fenced-disable",
            "command_transition": "disable",
            "requested_transition": "disable",
            "event": "unload_requested",
            "desired_state": "disabled",
            "lifecycle_state": "unloaded",
            "detail_code": "supersession_replacement",
            "detail": "Atomic replacement request; incomplete recovery must fail closed",
            "journal_owner_id": "prior-fence-owner",
        }
    )
    later = valid_lifecycle_record()
    later.update(
        {
            "plugin_provenance_id": candidates[0].provenance_id,
            "command_id": "later-enable",
            "event_id": 2,
            "journal_owner_id": "prior-enable-owner",
        }
    )
    receipts.write_text(
        f"{json.dumps(fenced, sort_keys=True)}\n{json.dumps(later, sort_keys=True)}\n",
        encoding="utf-8",
    )
    interrupted_recovery = CapabilityPluginKernel(
        candidates,
        receipt_writer=lambda _receipt: None,
    )
    interrupted_recovery._receipt_writer = None
    journal = LockedLifecycleJournal(receipts)
    interrupted_recovery._journal = journal
    interrupted_recovery._recovered_journal_path = None
    real_append = journal.append_event
    calls = 0

    def fail_second_terminal(payload, *, unique_terminal):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ReceiptPersistenceError("injected second fence terminal failure")
        return real_append(payload, unique_terminal=unique_terminal)

    monkeypatch.setattr(journal, "append_event", fail_second_terminal)
    with pytest.raises(ReceiptPersistenceError):
        interrupted_recovery._recover_journal(journal.path)
    journal.close()

    successor = CapabilityPluginKernel(candidates, receipt_path=receipts)
    state = successor.state("fixture.plugin")
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert state.error_code == "recovered_supersession_fence"
    assert successor._pending == {}
    assert successor.apply_turn_boundary() == ()
    assert successor._journal is not None
    successor._journal.close()


def test_historical_recovery_fence_does_not_poison_future_command_epoch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        "def register(registrar):\n"
        "    registrar.publish(\n"
        "        'fixture.base', 'future-ok', disposer=lambda: True, depends_on=(),\n"
        "    )\n",
    )
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    fenced = valid_lifecycle_record()
    fenced.update(
        {
            "plugin_provenance_id": candidates[0].provenance_id,
            "command_id": "fenced-disable",
            "command_transition": "disable",
            "requested_transition": "disable",
            "event": "unload_requested",
            "desired_state": "disabled",
            "lifecycle_state": "unloaded",
            "detail_code": "supersession_replacement",
            "detail": "Atomic replacement request; incomplete recovery must fail closed",
            "journal_owner_id": "prior-fence-owner",
        }
    )
    receipts.write_text(f"{json.dumps(fenced, sort_keys=True)}\n", encoding="utf-8")

    fenced_recovery = CapabilityPluginKernel(candidates, receipt_path=receipts)
    assert (
        fenced_recovery.state("fixture.plugin").lifecycle_state
        is PluginLifecycleState.RESTART_REQUIRED
    )
    assert fenced_recovery._journal is not None
    fenced_recovery._journal.close()
    fenced_recovery._journal = None
    fenced_recovery._closed = True

    future = CapabilityPluginKernel(candidates, receipt_path=receipts)
    assert future.state("fixture.plugin").lifecycle_state is PluginLifecycleState.DISCOVERED
    future.request_enable("fixture.plugin", command_id="future-enable")
    assert future._journal is not None
    future._journal.close()
    future._journal = None
    future._closed = True

    recovered = CapabilityPluginKernel(candidates, receipt_path=receipts)
    assert recovered.state("fixture.plugin").lifecycle_state is PluginLifecycleState.ENABLED
    assert recovered.state("fixture.plugin").error_code == ""
    assert recovered.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    recovered.request_disable("fixture.plugin", command_id="future-disable")
    assert recovered.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    recovered.close()


def test_incomplete_command_recovers_but_prior_owner_terminal_is_not_local_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    first = CapabilityPluginKernel(candidates, receipt_path=receipts)

    requested = first.request_enable("fixture.plugin", command_id="recover-enable")
    assert requested.phase is LifecyclePhase.REQUEST
    assert requested.event_id == 1
    assert first._journal is not None
    first._journal.close()
    first._journal = None
    first._closed = True

    recovered = CapabilityPluginKernel(candidates, receipt_path=receipts)
    assert recovered.state("fixture.plugin").desired_state is PluginDesiredState.ENABLED
    (terminal,) = recovered.apply_turn_boundary()
    assert terminal.event is LifecycleEvent.LOADED
    assert terminal.event_id == 3
    recovered.request_disable("fixture.plugin", command_id="recover-disable")
    assert recovered.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    recovered.close()

    replay_kernel = CapabilityPluginKernel(candidates, receipt_path=receipts)
    replay = replay_kernel.request_enable("fixture.plugin", command_id="recover-enable")

    assert replay.event is LifecycleEvent.ENABLED
    assert replay.phase is LifecyclePhase.REQUEST
    assert replay.event_id == 6
    assert replay.effective_state is PluginEffectiveState.UNLOADED
    assert replay_kernel.state("fixture.plugin").effective_state is PluginEffectiveState.UNLOADED
    (replayed_terminal,) = replay_kernel.apply_turn_boundary()
    assert replayed_terminal.event is LifecycleEvent.LOADED
    assert replayed_terminal.event_id == 7
    rows = receipt_rows(receipts)
    assert rows[0]["journal_owner_id"] != rows[1]["journal_owner_id"]
    assert rows[1]["journal_owner_id"] != rows[5]["journal_owner_id"]
    replay_kernel.request_disable("fixture.plugin", command_id="replay-disable")
    replay_kernel.apply_turn_boundary()
    replay_kernel.close()


def test_failed_lazy_recovery_is_not_latched_and_retries_adoption(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        "def register(registrar):\n    return None\n",
    )
    kernel = CapabilityPluginKernel(
        discover(root),
        receipt_path=tmp_path / "initial.jsonl",
    )
    assert kernel._journal is not None
    kernel._journal.close()
    instance = kernel._instances["fixture.plugin"]
    prior_request = kernel._make_receipt(
        instance,
        command_id="recover-enable",
        transition=LifecycleTransition.ENABLE,
        event=LifecycleEvent.ENABLED,
        outcome=LifecycleOutcome.ACCEPTED,
        desired_state=PluginDesiredState.ENABLED,
        lifecycle_state=PluginLifecycleState.ENABLED,
    ).to_dict()
    prior_request["event_id"] = 1
    prior_request["journal_owner_id"] = "prior-owner"

    class RetryableJournal:
        path = tmp_path / "recovery.jsonl"
        owner_id = "current-owner"
        attempts = 0
        closed = False

        def records(self):
            return (prior_request,)

        def append_request(self, payload):
            self.attempts += 1
            if self.attempts == 1:
                raise ReceiptPersistenceError("injected recovery adoption failure")
            record = dict(payload)
            record["event_id"] = 2
            record["journal_owner_id"] = self.owner_id
            return JournalAppendResult(record=record, replayed=False)

        def close(self) -> None:
            self.closed = True

    journal = RetryableJournal()
    kernel._journal = journal  # type: ignore[assignment]
    kernel._recovered_journal_path = None

    with pytest.raises(ReceiptPersistenceError, match="injected recovery adoption failure"):
        kernel._ensure_journal_recovered()

    assert kernel._recovered_journal_path is None
    assert kernel._pending == {}
    kernel._ensure_journal_recovered()
    assert journal.attempts == 2
    assert kernel._recovered_journal_path == journal.path
    assert kernel.state("fixture.plugin").desired_state is PluginDesiredState.ENABLED
    assert kernel._pending["fixture.plugin"].transition is LifecycleTransition.ENABLE
    journal.close()
    assert journal.closed is True


def test_one_in_process_kernel_exclusively_owns_a_journal(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    first = CapabilityPluginKernel(candidates, receipt_path=receipts)

    with pytest.raises(CapabilityPluginError) as contended:
        CapabilityPluginKernel(candidates, receipt_path=receipts)
    assert contended.value.code == "journal_owner_unavailable"

    first.close()
    successor = CapabilityPluginKernel(candidates, receipt_path=receipts)
    accepted = successor.request_enable("fixture.plugin", command_id="successor")
    assert accepted.event is LifecycleEvent.ENABLED
    successor.request_disable("fixture.plugin", command_id="successor-cancel")
    assert successor.apply_turn_boundary() == ()
    successor.close()


def test_keyboard_interrupt_during_journal_owner_acquisition_releases_process_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipts = (tmp_path / "receipts.jsonl").resolve()
    identity = capability_plugin_journal_module._path_identity(receipts)

    def interrupt_open(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(builtins, "open", interrupt_open)

    with pytest.raises(KeyboardInterrupt):
        LockedLifecycleJournal(receipts)

    assert identity not in capability_plugin_journal_module._PROCESS_OWNERS


def test_interrupted_owner_handle_cleanup_cannot_retain_process_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipts = (tmp_path / "receipts.jsonl").resolve()
    identity = capability_plugin_journal_module._path_identity(receipts)

    class InterruptingHandle:
        def seek(self, *_args) -> None:
            return None

        def tell(self) -> int:
            return 1

        def close(self) -> None:
            raise KeyboardInterrupt()

    monkeypatch.setattr(builtins, "open", lambda *_args, **_kwargs: InterruptingHandle())
    monkeypatch.setattr(
        LockedLifecycleJournal,
        "_lock_byte_zero",
        staticmethod(lambda _handle: (_ for _ in ()).throw(KeyboardInterrupt())),
    )

    with pytest.raises(KeyboardInterrupt):
        LockedLifecycleJournal(receipts)

    assert identity not in capability_plugin_journal_module._PROCESS_OWNERS


def test_owner_acquisition_interrupt_dominates_non_interrupting_cleanup_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipts = (tmp_path / "receipts.jsonl").resolve()
    identity = capability_plugin_journal_module._path_identity(receipts)

    class CleanupFailingHandle:
        def seek(self, *_args) -> None:
            return None

        def tell(self) -> int:
            return 1

        def close(self) -> None:
            raise OSError("injected cleanup failure")

    monkeypatch.setattr(builtins, "open", lambda *_args, **_kwargs: CleanupFailingHandle())
    monkeypatch.setattr(
        LockedLifecycleJournal,
        "_lock_byte_zero",
        staticmethod(
            lambda _handle: (_ for _ in ()).throw(
                KeyboardInterrupt("ACQUISITION_INTERRUPT_SECRET")
            )
        ),
    )

    with pytest.raises(KeyboardInterrupt) as interrupted:
        LockedLifecycleJournal(receipts)

    assert interrupted.value.args == ()
    assert interrupted.value.__cause__ is None
    assert interrupted.value.__context__ is None
    assert "ACQUISITION_INTERRUPT_SECRET" not in retained_exception_strings(interrupted.value)
    assert identity not in capability_plugin_journal_module._PROCESS_OWNERS


def test_initial_recovery_interrupt_dominates_journal_close_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class InterruptingRecoveryJournal:
        def __init__(self, path: Path) -> None:
            self.path = Path(path).resolve(strict=False)
            self.owner_id = "fake-owner"
            self.closed = False

        def records(self):
            raise KeyboardInterrupt("RECOVERY_INTERRUPT_SECRET")

        def close(self) -> None:
            raise OSError("injected close failure")

    monkeypatch.setattr(
        capability_plugins_module,
        "LockedLifecycleJournal",
        InterruptingRecoveryJournal,
    )

    with pytest.raises(KeyboardInterrupt) as interrupted:
        CapabilityPluginKernel((), receipt_path=tmp_path / "receipts.jsonl")

    assert interrupted.value.args == ()
    assert interrupted.value.__cause__ is None
    assert interrupted.value.__context__ is None
    assert "RECOVERY_INTERRUPT_SECRET" not in retained_exception_strings(interrupted.value)


def test_interrupted_journal_close_fails_closed_when_descriptor_outcome_is_unknown() -> None:
    identity = "in-memory-journal-owner"

    class InterruptingHandle:
        def close(self) -> None:
            raise KeyboardInterrupt()

    handle = InterruptingHandle()
    journal = object.__new__(LockedLifecycleJournal)
    journal.path = Path("in-memory-journal.jsonl")
    journal.owner_id = "owner"
    journal._identity = identity
    journal._thread_lock = threading.RLock()
    journal._owner_handle = handle
    journal._closed = False
    with capability_plugin_journal_module._PROCESS_OWNERS_GUARD:
        capability_plugin_journal_module._PROCESS_OWNERS[identity] = journal.owner_id
    with pytest.raises(KeyboardInterrupt):
        journal.close()

    assert journal.closed is True
    assert journal._owner_handle is None
    assert identity not in capability_plugin_journal_module._PROCESS_OWNERS
    with pytest.raises(JournalOwnershipError):
        journal.records()
    journal.close()


def test_subprocess_cannot_claim_a_live_journal_owner(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts.jsonl"
    helper = Path(__file__).parent / "_holders" / "hold_capability_journal_owner.py"
    scripts_dir = Path(__file__).parent.parent
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(scripts_dir), process_env.get("PYTHONPATH", "")))
    )
    process = subprocess.Popen(
        [sys.executable, str(helper), str(receipts)],
        cwd=scripts_dir,
        env=process_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        with pytest.raises(JournalOwnershipError):
            LockedLifecycleJournal(receipts)
    finally:
        if process.stdin is not None:
            process.stdin.write("release\n")
            process.stdin.flush()
        process.wait(timeout=10)
    assert process.returncode == 0
    successor = LockedLifecycleJournal(receipts)
    successor.close()


def test_supersession_batch_failure_keeps_old_pending_command_authoritative(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(discover(root), receipt_path=receipts)
    kernel.request_enable("fixture.plugin", command_id="old-enable")
    assert kernel._journal is not None

    def fail_batch(_old, _new):
        raise OSError("injected replacement failure")

    monkeypatch.setattr(kernel._journal, "append_supersession", fail_batch)
    refused = kernel.request_disable("fixture.plugin", command_id="new-disable")

    assert refused.outcome is LifecycleOutcome.FAILED
    assert refused.detail_code == "receipt_write_failed"
    assert kernel.state("fixture.plugin").desired_state is PluginDesiredState.ENABLED
    assert [row["event"] for row in receipt_rows(receipts)] == ["enabled"]
    (loaded,) = kernel.apply_turn_boundary()
    assert loaded.event is LifecycleEvent.LOADED
    kernel.request_disable("fixture.plugin", command_id="cleanup-disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    kernel.close()


def test_supersession_terminal_and_replacement_request_commit_as_one_batch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    candidates = discover(root)
    kernel = CapabilityPluginKernel(candidates, receipt_path=receipts)
    kernel.request_enable("fixture.plugin", command_id="old-enable")

    replacement = kernel.request_disable("fixture.plugin", command_id="new-disable")

    assert replacement.outcome is LifecycleOutcome.ACCEPTED
    rows = receipt_rows(receipts)
    assert [row["event"] for row in rows] == [
        "enabled",
        "superseded",
        "unload_requested",
        "unloaded",
    ]
    assert [row["event_id"] for row in rows] == [1, 2, 3, 4]
    assert kernel.apply_turn_boundary() == ()
    assert kernel.state("fixture.plugin").effective_state is PluginEffectiveState.UNLOADED
    kernel.close()
    successor = CapabilityPluginKernel(candidates, receipt_path=receipts)
    assert successor.state("fixture.plugin").lifecycle_state is PluginLifecycleState.DISCOVERED
    assert successor._pending == {}
    successor.close()


def test_reenable_before_disable_boundary_cancels_unload_without_authority_split(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    candidates = discover(root)
    kernel = CapabilityPluginKernel(candidates, receipt_path=receipts)
    kernel.request_enable("fixture.plugin", command_id="enable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    kernel.request_disable("fixture.plugin", command_id="disable")

    reenabled = kernel.request_enable("fixture.plugin", command_id="reenable")

    assert reenabled.event is LifecycleEvent.ENABLED
    assert reenabled.effective_state is PluginEffectiveState.LOADED
    assert reenabled.lifecycle_state is PluginLifecycleState.LOADED
    assert kernel._pending == {}
    assert kernel.apply_turn_boundary() == ()
    with kernel.snapshot() as current:
        assert current.resolve("fixture.base")() == "fixture-base"
    assert [row["event"] for row in receipt_rows(receipts)][-4:] == [
        "unload_requested",
        "superseded",
        "enabled",
        "loaded",
    ]
    kernel.request_disable("fixture.plugin", command_id="cleanup-disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    kernel.close()
    successor = CapabilityPluginKernel(candidates, receipt_path=receipts)
    assert successor.state("fixture.plugin").lifecycle_state is PluginLifecycleState.DISCOVERED
    assert successor._pending == {}
    successor.close()


def test_ambiguous_reenable_supersession_is_fenced_across_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"register": 0}
    monkeypatch.setattr(builtins, "FENCED_SUPERSESSION_COUNTS", counts, raising=False)
    body = """
import builtins

def register(registrar):
    builtins.FENCED_SUPERSESSION_COUNTS["register"] += 1
    registrar.publish("fixture.base", "value", disposer=lambda: True, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    first = CapabilityPluginKernel(candidates, receipt_path=receipts)
    first.request_enable("fixture.plugin", command_id="enable")
    assert first.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    first.request_disable("fixture.plugin", command_id="disable")
    assert first._journal is not None

    def fail_after_replace() -> None:
        raise OSError("injected parent sync failure")

    monkeypatch.setattr(first._journal, "_fsync_parent_directory", fail_after_replace)
    refused = first.request_enable("fixture.plugin", command_id="reenable")

    assert refused.event is LifecycleEvent.RESTART_REQUIRED
    assert first._pending == {}
    assert counts == {"register": 1}
    first._journal.close()
    first._journal = None
    first._closed = True
    successor = CapabilityPluginKernel(candidates, receipt_path=receipts)
    successor_state = successor.state("fixture.plugin")
    assert successor_state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert successor_state.error_code == "recovered_supersession_fence"
    assert successor._pending == {}
    assert successor.apply_turn_boundary() == ()
    assert counts == {"register": 1}
    assert successor._journal is not None
    successor._journal.close()


def test_hostile_capability_error_subclass_cannot_escape_cleanup_or_close_guard(
    tmp_path: Path,
) -> None:
    body = """
from runtime.capability_plugins import CapabilityPluginError

class HostileError(CapabilityPluginError):
    def __getattribute__(self, name):
        if name == "code":
            raise SystemExit("hostile code access escaped")
        return super().__getattribute__(name)

def register(_registrar):
    raise HostileError("safe_code", "safe detail")
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")

    (failed,) = kernel.apply_turn_boundary()

    assert failed.event is LifecycleEvent.FAILED
    assert failed.detail_code == "plugin_registration_failed"
    assert kernel.state("fixture.plugin").lifecycle_state is PluginLifecycleState.FAILED
    assert kernel._pending == {}
    kernel.close()


def test_kernel_close_refuses_nonquiescent_lifecycle_without_visible_registrations() -> None:
    kernel = CapabilityPluginKernel((), receipt_writer=lambda _receipt: None)
    candidate = CapabilityPluginCandidate(
        manifest=parse_capability_manifest(
            raw_manifest(contributions=(("fixture.base", ()),)),
            physical_source=ManifestSource.BUNDLED,
        ),
        location_key="memory",
    )
    kernel._candidates = (candidate,)
    kernel._instances[candidate.manifest.id] = capability_plugins_module._PluginInstance(
        candidate=candidate,
        desired_state=PluginDesiredState.ENABLED,
        effective_state=PluginEffectiveState.UNLOADED,
        lifecycle_state=PluginLifecycleState.LOADING,
    )

    with pytest.raises(CapabilityPluginError) as refused:
        kernel.close()

    assert refused.value.code == "plugins_not_drained"
    kernel._instances[candidate.manifest.id].lifecycle_state = PluginLifecycleState.FAILED
    kernel.close()


def test_post_replace_fsync_failure_quarantines_supersession_authority(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(discover(root), receipt_path=receipts)
    kernel.request_enable("fixture.plugin", command_id="old-enable")
    assert kernel._journal is not None

    def fail_after_replace() -> None:
        raise OSError("injected parent sync failure")

    monkeypatch.setattr(kernel._journal, "_fsync_parent_directory", fail_after_replace)
    replacement = kernel.request_disable("fixture.plugin", command_id="new-disable")

    assert replacement.event is LifecycleEvent.RESTART_REQUIRED
    assert replacement.outcome is LifecycleOutcome.FAILED
    assert replacement.detail_code == "journal_commit_ambiguous"
    assert [row["event"] for row in receipt_rows(receipts)] == [
        "enabled",
        "superseded",
        "unload_requested",
    ]
    state = kernel.state("fixture.plugin")
    assert state.desired_state is PluginDesiredState.DISABLED
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert kernel.apply_turn_boundary() == ()
    with kernel.snapshot() as current:
        assert current.contributions == {}
    kernel._journal.close()


def test_post_replace_keyboard_interrupt_reconciles_quarantines_and_propagates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(discover(root), receipt_path=receipts)
    kernel.request_enable("fixture.plugin", command_id="old-enable")
    assert kernel._journal is not None
    atomic_replace = kernel._journal._atomic_replace_unlocked

    def replace_then_interrupt(expected) -> None:
        atomic_replace(expected)
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        kernel._journal,
        "_atomic_replace_unlocked",
        replace_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        kernel.request_disable("fixture.plugin", command_id="new-disable")

    assert [row["event"] for row in receipt_rows(receipts)] == [
        "enabled",
        "superseded",
        "unload_requested",
    ]
    state = kernel.state("fixture.plugin")
    assert state.desired_state is PluginDesiredState.DISABLED
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert kernel._pending == {}
    assert kernel.apply_turn_boundary() == ()
    with kernel.snapshot() as current:
        assert current.contributions == {}
    kernel._journal.close()


def test_impossible_post_replace_image_quarantines_old_pending_authority(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(discover(root), receipt_path=receipts)
    kernel.request_enable("fixture.plugin", command_id="old-enable")
    real_replace = capability_plugin_journal_module.os.replace

    def expose_partial_image(source: Path, target: Path) -> None:
        real_replace(source, target)
        committed = Path(target).read_bytes().splitlines(keepends=True)
        Path(target).write_bytes(b"".join(committed[:2]))
        raise OSError("injected impossible post-replace image")

    monkeypatch.setattr(
        capability_plugin_journal_module.os,
        "replace",
        expose_partial_image,
    )
    refused = kernel.request_disable("fixture.plugin", command_id="new-disable")

    assert refused.event is LifecycleEvent.RESTART_REQUIRED
    assert refused.detail_code == "journal_commit_ambiguous"
    state = kernel.state("fixture.plugin")
    assert state.desired_state is PluginDesiredState.DISABLED
    assert state.effective_state is PluginEffectiveState.RESTART_REQUIRED
    assert state.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
    assert kernel.apply_turn_boundary() == ()
    with kernel.snapshot() as current:
        assert current.contributions == {}
    assert kernel._journal is not None
    kernel._journal.close()


def test_atomic_rewrite_failure_preserves_last_committed_journal(
    tmp_path: Path, monkeypatch
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    journal = LockedLifecycleJournal(receipts)
    journal.append_request(journal_payload("committed"))
    committed = receipts.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("injected pre-replace crash")

    monkeypatch.setattr(capability_plugin_journal_module.os, "replace", fail_replace)
    with pytest.raises(ReceiptPersistenceError):
        journal.append_request(journal_payload("not-committed"))

    assert receipts.read_bytes() == committed
    assert [item["command_id"] for item in journal.records()] == ["committed"]
    journal.close()


def test_pre_replace_persistence_interrupt_is_argument_and_traceback_clean(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    journal = LockedLifecycleJournal(receipts)
    journal.append_request(journal_payload("committed"))

    def interrupt_before_replace(_expected) -> None:
        raise KeyboardInterrupt("PERSISTENCE_SECRET")

    monkeypatch.setattr(journal, "_atomic_replace_unlocked", interrupt_before_replace)

    with pytest.raises(KeyboardInterrupt) as interrupted:
        journal.append_request(journal_payload("interrupted"))

    assert interrupted.value.args == ()
    assert interrupted.value.__cause__ is None
    assert interrupted.value.__context__ is None
    assert "PERSISTENCE_SECRET" not in retained_exception_strings(interrupted.value)
    assert [item["command_id"] for item in journal.records()] == ["committed"]
    journal.close()


def test_temporary_write_interrupt_dominates_file_handle_cleanup_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = LockedLifecycleJournal(tmp_path / "receipts.jsonl")
    events: list[str] = []

    class InterruptingHandle:
        def write(self, _value: str) -> None:
            raise KeyboardInterrupt("TEMP_WRITE_INTERRUPT_SECRET")

        def flush(self) -> None:
            raise AssertionError("flush must not follow an interrupted write")

        def fileno(self) -> int:
            return 12345

        def close(self) -> None:
            events.append("handle.close")
            raise OSError("injected file handle cleanup failure")

    monkeypatch.setattr(
        capability_plugin_journal_module,
        "_open_private_text_file",
        lambda _path: InterruptingHandle(),
    )

    with pytest.raises(KeyboardInterrupt) as interrupted:
        journal._atomic_replace_unlocked(({"event_id": 1},))

    assert events == ["handle.close"]
    assert interrupted.value.args == ()
    assert interrupted.value.__cause__ is None
    assert interrupted.value.__context__ is None
    assert "TEMP_WRITE_INTERRUPT_SECRET" not in retained_exception_strings(
        interrupted.value
    )
    journal.close()


def test_interrupt_after_atomic_file_open_closes_only_the_file_object(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = LockedLifecycleJournal(tmp_path / "receipts.jsonl")
    close_count = 0

    class OwnedHandle:
        def write(self, _value: str) -> None:
            raise AssertionError("line-level interrupt must precede writes")

        def flush(self) -> None:
            raise AssertionError("line-level interrupt must precede flush")

        def fileno(self) -> int:
            return 12345

        def close(self) -> None:
            nonlocal close_count
            close_count += 1

    monkeypatch.setattr(
        capability_plugin_journal_module,
        "_open_private_text_file",
        lambda _path: OwnedHandle(),
    )
    real_close = capability_plugin_journal_module.os.close

    def unexpected_raw_close(_descriptor: int) -> None:
        raise AssertionError("raw descriptor cleanup survived the atomic open handoff")

    monkeypatch.setattr(
        capability_plugin_journal_module.os,
        "close",
        unexpected_raw_close,
    )
    source_lines, start_line = inspect.getsourcelines(
        LockedLifecycleJournal._atomic_replace_unlocked
    )
    interrupt_line = start_line + next(
        index for index, line in enumerate(source_lines) if "for record in records" in line
    )
    target_code = LockedLifecycleJournal._atomic_replace_unlocked.__code__

    def trace(frame, event, _arg):
        if frame.f_code is target_code and event == "line" and frame.f_lineno == interrupt_line:
            raise KeyboardInterrupt("ATOMIC_OPEN_HANDOFF_SECRET")
        return trace

    try:
        sys.settrace(trace)
        with pytest.raises(KeyboardInterrupt) as interrupted:
            journal._atomic_replace_unlocked(())
    finally:
        sys.settrace(None)
        monkeypatch.setattr(capability_plugin_journal_module.os, "close", real_close)

    assert close_count == 1
    assert interrupted.value.args == ()
    assert interrupted.value.__cause__ is None
    assert interrupted.value.__context__ is None
    assert "ATOMIC_OPEN_HANDOFF_SECRET" not in retained_exception_strings(
        interrupted.value
    )
    journal.close()


def test_parent_fsync_interrupt_dominates_close_failure_and_marks_ambiguity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    journal = LockedLifecycleJournal(receipts)
    journal.append_request(journal_payload("first"))
    real_open = capability_plugin_journal_module.os.open
    real_fsync = capability_plugin_journal_module.os.fsync
    real_close = capability_plugin_journal_module.os.close
    parent_descriptor = 12345

    def open_path(path, flags, mode=0o777):
        if os.fspath(path) == os.fspath(receipts.parent):
            return parent_descriptor
        return real_open(path, flags, mode)

    def fsync_descriptor(descriptor: int) -> None:
        if descriptor == parent_descriptor:
            raise KeyboardInterrupt("PARENT_FSYNC_INTERRUPT_SECRET")
        real_fsync(descriptor)

    def close_descriptor(descriptor: int) -> None:
        if descriptor == parent_descriptor:
            raise OSError("injected parent descriptor cleanup failure")
        real_close(descriptor)

    monkeypatch.setattr(capability_plugin_journal_module.os, "name", "posix")
    monkeypatch.setattr(capability_plugin_journal_module.os, "open", open_path)
    monkeypatch.setattr(capability_plugin_journal_module.os, "fsync", fsync_descriptor)
    monkeypatch.setattr(capability_plugin_journal_module.os, "close", close_descriptor)

    with pytest.raises(
        capability_plugin_journal_module.JournalCommitAmbiguousError
    ) as ambiguous:
        journal.append_request(journal_payload("second"))

    assert ambiguous.value.interrupted is True
    assert [item["command_id"] for item in journal.records()] == ["first", "second"]
    journal.close()


def test_ordinary_append_reports_ambiguity_after_expected_image_is_visible(
    tmp_path: Path, monkeypatch
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    journal = LockedLifecycleJournal(receipts)
    journal.append_request(journal_payload("first"))

    def fail_after_replace() -> None:
        raise OSError("injected parent sync failure")

    monkeypatch.setattr(journal, "_fsync_parent_directory", fail_after_replace)
    with pytest.raises(capability_plugin_journal_module.JournalCommitAmbiguousError):
        journal.append_request(journal_payload("second"))

    assert [item["command_id"] for item in journal.records()] == ["first", "second"]
    journal.close()


def test_unterminated_tail_and_mid_file_corruption_both_fail_closed(
    tmp_path: Path,
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    journal = LockedLifecycleJournal(receipts)
    journal.append_request(journal_payload("first"))
    committed = receipts.read_bytes()
    with receipts.open("ab") as handle:
        handle.write(b'{"event_id":2')

    with pytest.raises(ReceiptPersistenceError):
        journal.records()
    with pytest.raises(ReceiptPersistenceError):
        journal.append_request(journal_payload("not-authoritative"))

    receipts.write_bytes(committed)
    second = journal.append_request(journal_payload("second"))
    assert second.record["event_id"] == 2
    committed_lines = receipts.read_bytes().splitlines(keepends=True)
    receipts.write_bytes(committed_lines[0] + b"{broken}\n" + committed_lines[1])
    with pytest.raises(ReceiptPersistenceError):
        journal.records()
    journal.close()


def test_truncated_real_terminal_cannot_replay_plugin_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"register": 0}
    monkeypatch.setattr(builtins, "TRUNCATED_TERMINAL_COUNTS", counts, raising=False)
    body = """
import builtins

def register(registrar):
    builtins.TRUNCATED_TERMINAL_COUNTS["register"] += 1
    registrar.publish("fixture.base", "value", disposer=lambda: True, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    candidates = discover(root)
    receipts = tmp_path / "receipts.jsonl"
    first = CapabilityPluginKernel(candidates, receipt_path=receipts)
    first.request_enable("fixture.plugin", command_id="enable")
    assert first.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    assert counts == {"register": 1}
    assert first._journal is not None
    first._journal.close()
    first._journal = None
    first._closed = True
    committed = receipts.read_bytes()
    assert committed.endswith(b"\n")
    receipts.write_bytes(committed[:-1])

    with pytest.raises(ReceiptPersistenceError):
        CapabilityPluginKernel(candidates, receipt_path=receipts)

    assert counts == {"register": 1}
    proof = LockedLifecycleJournal(receipts)
    proof.close()


@pytest.mark.parametrize(
    "committed",
    [
        b"\n",
        b'{"event_id":1}\n\n',
        b'{"event_id":2}\n',
        b'{"event_id":1}\n{"event_id":3}\n',
    ],
)
def test_committed_blank_frames_and_noncontiguous_event_ids_fail_closed(
    tmp_path: Path,
    committed: bytes,
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    journal = LockedLifecycleJournal(receipts)
    receipts.write_bytes(committed)

    with pytest.raises(ReceiptPersistenceError):
        journal.records()

    journal.close()


def test_plugin_callbacks_reject_reentrancy_but_snapshot_does_not_take_global_lock(
    tmp_path: Path, monkeypatch
) -> None:
    results: dict[str, object] = {}
    monkeypatch.setattr(builtins, "CALLBACK_RESULTS", results, raising=False)
    body = """
import builtins
import threading

def dispose():
    try:
        builtins.CALLBACK_KERNEL.request_enable("fixture.plugin", command_id="from-dispose")
    except Exception as exc:
        builtins.CALLBACK_RESULTS["dispose_reentry"] = getattr(exc, "code", "")
    return True

def register(registrar):
    try:
        builtins.CALLBACK_KERNEL.request_disable("fixture.plugin", command_id="from-register")
    except Exception as exc:
        builtins.CALLBACK_RESULTS["register_reentry"] = getattr(exc, "code", "")

    def inspect_snapshot():
        with builtins.CALLBACK_KERNEL.snapshot() as current:
            builtins.CALLBACK_RESULTS["snapshot_generation"] = current.generation
            builtins.CALLBACK_RESULTS["snapshot_contributions"] = tuple(current.contributions)

    worker = threading.Thread(target=inspect_snapshot)
    worker.start()
    worker.join(timeout=2)
    builtins.CALLBACK_RESULTS["snapshot_nonblocking"] = not worker.is_alive()
    registrar.publish("fixture.base", "value", disposer=dispose, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    monkeypatch.setattr(builtins, "CALLBACK_KERNEL", kernel, raising=False)
    kernel.request_enable("fixture.plugin", command_id="enable")

    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    assert results == {
        "register_reentry": "lifecycle_callback_active",
        "snapshot_generation": 0,
        "snapshot_contributions": (),
        "snapshot_nonblocking": True,
    }
    kernel.request_disable("fixture.plugin", command_id="disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    assert results["dispose_reentry"] == "lifecycle_callback_active"
    kernel.close()


def test_cached_callable_handle_is_revoked_and_close_waits_for_active_invocation(
    tmp_path: Path, monkeypatch
) -> None:
    started = threading.Event()
    release_call = threading.Event()
    events: list[str] = []
    monkeypatch.setattr(builtins, "HANDLE_STARTED", started, raising=False)
    monkeypatch.setattr(builtins, "HANDLE_RELEASE", release_call, raising=False)
    monkeypatch.setattr(builtins, "HANDLE_EVENTS", events, raising=False)
    body = """
import builtins

def value():
    builtins.HANDLE_STARTED.set()
    builtins.HANDLE_RELEASE.wait(timeout=5)
    builtins.HANDLE_EVENTS.append("call-finished")
    return "done"

def dispose():
    builtins.HANDLE_EVENTS.append("disposed")
    return True

def register(registrar):
    registrar.publish("fixture.base", value, disposer=dispose, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()
    handle = held.contributions["fixture.base"]
    assert not hasattr(handle, "value")
    call_results: list[str] = []
    close_receipts = []
    caller = threading.Thread(target=lambda: call_results.append(handle()))
    caller.start()
    assert started.wait(timeout=2)
    kernel.request_disable("fixture.plugin", command_id="disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.DRAINING
    closer = threading.Thread(target=lambda: close_receipts.extend(held.close()))
    closer.start()

    closer.join(timeout=0.2)
    assert closer.is_alive()
    assert events == []
    release_call.set()
    caller.join(timeout=5)
    closer.join(timeout=5)

    assert call_results == ["done"]
    assert events == ["call-finished", "disposed"]
    assert [item.event for item in close_receipts] == [LifecycleEvent.UNLOADED]
    with pytest.raises(CapabilityPluginError) as revoked:
        handle()
    assert revoked.value.code == "snapshot_released"
    kernel.close()


def test_contribution_cannot_close_its_own_authorizing_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    body = """
import builtins

def value():
    try:
        builtins.SELF_CLOSE_SNAPSHOT.close()
    except Exception as exc:
        return (getattr(exc, "code", ""), builtins.SELF_CLOSE_SNAPSHOT.closed)
    return ("unexpected-success", True)

def register(registrar):
    registrar.publish("fixture.base", value, disposer=lambda: True, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()
    monkeypatch.setattr(builtins, "SELF_CLOSE_SNAPSHOT", held, raising=False)
    observed: list[tuple[str, bool]] = []
    caller = threading.Thread(
        target=lambda: observed.append(held.resolve("fixture.base")()),
        daemon=True,
    )

    caller.start()
    caller.join(timeout=2)

    assert not caller.is_alive(), "self-close deadlocked inside its active contribution"
    assert observed == [("snapshot_close_from_active_invocation", False)]
    assert held.close() == ()
    kernel.request_disable("fixture.plugin", command_id="disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    kernel.close()


def test_concurrent_snapshot_close_callers_receive_the_same_completed_receipts(
    tmp_path: Path, monkeypatch
) -> None:
    disposer_started = threading.Event()
    finish_disposer = threading.Event()
    monkeypatch.setattr(builtins, "CLOSE_DISPOSER_STARTED", disposer_started, raising=False)
    monkeypatch.setattr(builtins, "CLOSE_DISPOSER_FINISH", finish_disposer, raising=False)
    body = """
import builtins

def value():
    return "value"

def dispose():
    builtins.CLOSE_DISPOSER_STARTED.set()
    builtins.CLOSE_DISPOSER_FINISH.wait(timeout=5)
    return True

def register(registrar):
    registrar.publish("fixture.base", value, disposer=dispose, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()
    kernel.request_disable("fixture.plugin", command_id="disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.DRAINING
    results: dict[str, tuple[object, ...]] = {}

    first = threading.Thread(target=lambda: results.__setitem__("first", held.close()))
    second = threading.Thread(target=lambda: results.__setitem__("second", held.close()))
    first.start()
    assert disposer_started.wait(timeout=2)
    second.start()
    second.join(timeout=0.2)
    assert second.is_alive()
    finish_disposer.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert results["first"] == results["second"]
    assert [item.event for item in results["first"]] == [LifecycleEvent.UNLOADED]
    kernel.close()


def test_failed_snapshot_release_is_visible_to_waiters_and_explicitly_retryable(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()
    handle = held.resolve("fixture.base")
    kernel.request_disable("fixture.plugin", command_id="disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.DRAINING
    release_entered = threading.Event()
    fail_release = threading.Event()
    original_complete = kernel._complete_unload
    attempts = 0

    def fail_once(instance, pending, *, generation_before, transaction):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            release_entered.set()
            fail_release.wait(timeout=5)
            raise OSError("injected release failure")
        return original_complete(
            instance,
            pending,
            generation_before=generation_before,
            transaction=transaction,
        )

    monkeypatch.setattr(kernel, "_complete_unload", fail_once)
    errors: list[BaseException] = []

    def close_and_capture() -> None:
        try:
            held.close()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=close_and_capture)
    second = threading.Thread(target=close_and_capture)
    first.start()
    assert release_entered.wait(timeout=2)
    second.start()
    second.join(timeout=0.2)
    assert second.is_alive()
    fail_release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert len(errors) == 2
    assert errors[0] is errors[1]
    diagnostics = kernel.outstanding_leases()
    assert len(diagnostics) == 1
    assert diagnostics[0].lease_id == held.lease_id
    assert diagnostics[0].closing is True
    assert diagnostics[0].release_state == "release_failed"
    assert diagnostics[0].release_error_code == "snapshot_release_failed"
    with pytest.raises(CapabilityPluginError) as revoked:
        handle()
    assert revoked.value.code == "snapshot_released"

    retried = held.close()

    assert [item.event for item in retried] == [LifecycleEvent.UNLOADED]
    assert held.close() == retried
    assert kernel.outstanding_leases() == ()
    kernel.close()


def test_partial_multi_plugin_release_returns_prior_completed_receipts_on_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    counts = {"a": 0, "b": 0}
    monkeypatch.setattr(builtins, "MULTI_RELEASE_COUNTS", counts, raising=False)
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "a-plugin",
        raw_manifest("a.plugin", contributions=(("a.value", ()),)),
        """
import builtins

def dispose():
    builtins.MULTI_RELEASE_COUNTS["a"] += 1
    return True

def register(registrar):
    registrar.publish("a.value", "a", disposer=dispose, depends_on=())
""",
    )
    write_plugin(
        root,
        "b-plugin",
        raw_manifest("b.plugin", contributions=(("b.value", ()),)),
        """
import builtins

def dispose():
    builtins.MULTI_RELEASE_COUNTS["b"] += 1
    raise KeyboardInterrupt()

def register(registrar):
    registrar.publish("b.value", "b", disposer=dispose, depends_on=())
""",
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("a.plugin", command_id="enable-a")
    kernel.request_enable("b.plugin", command_id="enable-b")
    assert [item.event for item in kernel.apply_turn_boundary()] == [
        LifecycleEvent.LOADED,
        LifecycleEvent.LOADED,
    ]
    held = kernel.snapshot()
    kernel.request_disable("a.plugin", command_id="disable-a")
    kernel.request_disable("b.plugin", command_id="disable-b")
    assert [item.event for item in kernel.apply_turn_boundary()] == [
        LifecycleEvent.DRAINING,
        LifecycleEvent.DRAINING,
    ]

    with pytest.raises(KeyboardInterrupt):
        held.close()

    assert counts == {"a": 1, "b": 1}
    assert kernel.state("a.plugin").effective_state is PluginEffectiveState.UNLOADED
    assert kernel.state("b.plugin").effective_state is PluginEffectiveState.RESTART_REQUIRED
    retried = held.close()
    assert [(item.plugin_id, item.event) for item in retried] == [
        ("a.plugin", LifecycleEvent.UNLOADED)
    ]
    assert held.close() == retried
    assert counts == {"a": 1, "b": 1}
    ownership = kernel._instances["b.plugin"].ownership
    assert ownership is not None
    assert ownership.cleanup() is True
    assert kernel._journal is not None
    kernel._journal.close()


def test_overlapping_disjoint_snapshot_releases_require_retry_without_stranding_drain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    started = threading.Event()
    finish = threading.Event()
    counts = {"a": 0, "b": 0}
    monkeypatch.setattr(builtins, "OVERLAP_RELEASE_STARTED", started, raising=False)
    monkeypatch.setattr(builtins, "OVERLAP_RELEASE_FINISH", finish, raising=False)
    monkeypatch.setattr(builtins, "OVERLAP_RELEASE_COUNTS", counts, raising=False)
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "a-plugin",
        raw_manifest("a.plugin", contributions=(("a.value", ()),)),
        """
import builtins

def dispose():
    builtins.OVERLAP_RELEASE_COUNTS["a"] += 1
    builtins.OVERLAP_RELEASE_STARTED.set()
    builtins.OVERLAP_RELEASE_FINISH.wait(timeout=5)
    return True

def register(registrar):
    registrar.publish("a.value", "a", disposer=dispose, depends_on=())
""",
    )
    write_plugin(
        root,
        "b-plugin",
        raw_manifest("b.plugin", contributions=(("b.value", ()),)),
        """
import builtins

def dispose():
    builtins.OVERLAP_RELEASE_COUNTS["b"] += 1
    return True

def register(registrar):
    registrar.publish("b.value", "b", disposer=dispose, depends_on=())
""",
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("a.plugin", command_id="enable-a")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    snapshot_a = kernel.snapshot()
    kernel.request_disable("a.plugin", command_id="disable-a")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.DRAINING
    kernel.request_enable("b.plugin", command_id="enable-b")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    snapshot_b = kernel.snapshot()
    kernel.request_disable("b.plugin", command_id="disable-b")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.DRAINING
    a_receipts: list[object] = []
    a_errors: list[BaseException] = []

    def close_a() -> None:
        try:
            a_receipts.extend(snapshot_a.close())
        except BaseException as exc:
            a_errors.append(exc)

    thread = threading.Thread(target=close_a)
    thread.start()
    assert started.wait(timeout=2)

    with pytest.raises(CapabilityPluginError) as deferred:
        snapshot_b.close()

    assert deferred.value.code == "snapshot_release_deferred"
    assert kernel.state("b.plugin").effective_state is PluginEffectiveState.DRAINING
    assert kernel._instances["b.plugin"].lease_count == 0
    finish.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert a_errors == []
    assert [item.event for item in a_receipts] == [LifecycleEvent.UNLOADED]

    b_receipts = snapshot_b.close()
    assert [item.event for item in b_receipts] == [LifecycleEvent.UNLOADED]
    assert counts == {"a": 1, "b": 1}
    assert kernel.state("b.plugin").effective_state is PluginEffectiveState.UNLOADED
    kernel.close()


def test_snapshot_release_retry_history_is_bounded_without_waiters() -> None:
    attempts = 0

    def fail_release(_lease_id: str) -> tuple[object, ...]:
        nonlocal attempts
        attempts += 1
        raise OSError("injected release failure")

    lease = capability_plugins_module._SnapshotLease("lease", {}, fail_release)

    for _ in range(64):
        with pytest.raises(OSError, match="injected release failure"):
            lease.close()
        assert lease._attempt_outcomes == {}

    lease._release = lambda _lease_id: ()
    assert lease.close() == ()
    assert lease._attempt_outcomes == {}
    assert attempts == 64


def test_snapshot_release_success_is_published_before_finalize_interrupt_propagates() -> None:
    released: list[str] = []

    class InterruptSecondEnter:
        def __init__(self) -> None:
            self.real = threading.Condition(threading.Lock())
            self.enters = 0

        def __enter__(self):
            self.enters += 1
            if self.enters in {2, 3}:
                raise KeyboardInterrupt()
            return self.real.__enter__()

        def __exit__(self, *args):
            return self.real.__exit__(*args)

        def wait(self, *args, **kwargs):
            return self.real.wait(*args, **kwargs)

        def notify_all(self) -> None:
            self.real.notify_all()

    lease = capability_plugins_module._SnapshotLease(
        "lease",
        {},
        lambda lease_id: (released.append(lease_id) or ()),
    )
    lease._condition = InterruptSecondEnter()

    with pytest.raises(KeyboardInterrupt):
        lease.close()

    assert released == ["lease"]
    assert lease.closed is True
    assert lease.close() == ()


def test_hostile_returned_object_metadata_becomes_stable_non_inert_failure() -> None:
    class HostileMeta(type):
        def __getattribute__(cls, name):
            if name in {"__mro__", "__dict__"}:
                raise SystemExit("hostile returned-object metadata escaped")
            return super().__getattribute__(name)

    class HostileResult(metaclass=HostileMeta):
        pass

    lease = capability_plugins_module._SnapshotLease(
        "lease",
        {"fixture.value": lambda: HostileResult()},
        lambda _lease_id: (),
    )

    with pytest.raises(CapabilityPluginError) as rejected:
        lease.invoke("fixture.value")

    assert rejected.value.code == "non_inert_contribution_result"
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert lease.close() == ()


def test_hostile_non_callable_metadata_becomes_stable_non_inert_read_failure() -> None:
    class HostileMeta(type):
        def __hash__(cls) -> int:
            raise SystemExit("hostile non-callable metadata escaped")

    class HostileValue(metaclass=HostileMeta):
        pass

    lease = capability_plugins_module._SnapshotLease(
        "lease",
        {"fixture.value": HostileValue()},
        lambda _lease_id: (),
    )

    with pytest.raises(CapabilityPluginError) as rejected:
        lease.read("fixture.value")

    assert rejected.value.code == "non_inert_contribution"
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert lease.close() == ()


def test_non_callable_handles_return_only_detached_inert_data(tmp_path: Path) -> None:
    body = """
def register(registrar):
    registrar.publish(
        "fixture.safe", {"items": ["a", "b"]}, disposer=lambda: True, depends_on=()
    )
    registrar.publish(
        "fixture.unsafe", object(), disposer=lambda: True, depends_on=()
    )
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(
            contributions=(("fixture.safe", ()), ("fixture.unsafe", ())),
        ),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()
    safe = held.resolve("fixture.safe")
    unsafe = held.resolve("fixture.unsafe")

    detached = safe.read()
    assert detached["items"] == ("a", "b")
    with pytest.raises(TypeError):
        detached["items"] = ()
    with pytest.raises(CapabilityPluginError) as rejected:
        unsafe.read()
    assert rejected.value.code == "non_inert_contribution"
    held.close()
    with pytest.raises(CapabilityPluginError) as revoked:
        safe.read()
    assert revoked.value.code == "snapshot_released"
    kernel.request_disable("fixture.plugin", command_id="disable")
    kernel.apply_turn_boundary()
    kernel.close()


@pytest.mark.parametrize(
    "value_definition",
    [
        "async def value():\n    return 'deferred'\n",
        "def value():\n    yield 'deferred'\n",
        "async def value():\n    yield 'deferred'\n",
    ],
)
def test_deferred_contribution_functions_are_rejected_before_publication(
    tmp_path: Path,
    value_definition: str,
) -> None:
    body = (
        value_definition
        + "\ndef register(registrar):\n"
        + "    registrar.publish('fixture.base', value, disposer=lambda: True, depends_on=())\n"
    )
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")

    (terminal,) = kernel.apply_turn_boundary()

    assert terminal.event is LifecycleEvent.FAILED
    assert terminal.detail_code == "deferred_contribution_not_supported"
    assert kernel.state("fixture.plugin").effective_state is PluginEffectiveState.UNLOADED
    with kernel.snapshot() as current:
        assert current.contributions == {}
    kernel.close()


def test_invocation_rejects_and_closes_deferred_or_authority_bearing_results(
    tmp_path: Path, monkeypatch
) -> None:
    returned: dict[str, object] = {}
    monkeypatch.setattr(builtins, "RETURNED_AUTHORITIES", returned, raising=False)
    body = """
import builtins

async def coroutine_body():
    return "deferred"

def coroutine_value():
    value = coroutine_body()
    builtins.RETURNED_AUTHORITIES["coroutine"] = value
    return value

def generator_value():
    def deferred():
        yield "deferred"
    value = deferred()
    builtins.RETURNED_AUTHORITIES["generator"] = value
    return value

def async_generator_value():
    async def deferred():
        yield "deferred"
    value = deferred()
    builtins.RETURNED_AUTHORITIES["async_generator"] = value
    return value

def callable_value():
    return lambda: "escaped"

class Authority:
    pass

def custom_value():
    return Authority()

def register(registrar):
    registrar.publish("result.coroutine", coroutine_value, disposer=lambda: True, depends_on=())
    registrar.publish("result.generator", generator_value, disposer=lambda: True, depends_on=())
    registrar.publish(
        "result.async-generator", async_generator_value, disposer=lambda: True, depends_on=()
    )
    registrar.publish("result.callable", callable_value, disposer=lambda: True, depends_on=())
    registrar.publish("result.custom", custom_value, disposer=lambda: True, depends_on=())
"""
    contributions = tuple(
        (item, ())
        for item in (
            "result.coroutine",
            "result.generator",
            "result.async-generator",
            "result.callable",
            "result.custom",
        )
    )
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(contributions=contributions), body)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()
    held = kernel.snapshot()

    for contribution_id in (
        "result.coroutine",
        "result.generator",
        "result.async-generator",
    ):
        with pytest.raises(CapabilityPluginError) as rejected:
            held.resolve(contribution_id)()
        assert rejected.value.code == "deferred_contribution_result"
    with pytest.raises(CapabilityPluginError) as callable_rejected:
        held.resolve("result.callable")()
    assert callable_rejected.value.code == "authority_contribution_result"
    with pytest.raises(CapabilityPluginError) as custom_rejected:
        held.resolve("result.custom")()
    assert custom_rejected.value.code == "non_inert_contribution_result"
    assert returned["coroutine"].cr_frame is None  # type: ignore[union-attr]
    assert returned["generator"].gi_frame is None  # type: ignore[union-attr]
    assert returned["async_generator"].ag_frame is None  # type: ignore[union-attr]

    held.close()
    kernel.request_disable("fixture.plugin", command_id="disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    kernel.close()


def test_contribution_execution_failure_isolated_without_secret_or_process_exit(
    tmp_path: Path,
) -> None:
    body = """
def value():
    raise SystemExit("api_key=CONTRIBUTION_SECRET_123")

def register(registrar):
    registrar.publish("fixture.base", value, disposer=lambda: True, depends_on=())
"""
    root = tmp_path / "extensions"
    write_plugin(
        root,
        "fixture",
        raw_manifest(contributions=(("fixture.base", ()),)),
        body,
    )
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    held = kernel.snapshot()

    with pytest.raises(CapabilityPluginError) as isolated:
        held.resolve("fixture.base")()

    assert isolated.value.code == "contribution_execution_failed"
    assert "CONTRIBUTION_SECRET_123" not in str(isolated.value)
    assert isolated.value.__cause__ is None
    assert isolated.value.__context__ is None
    held.close()
    kernel.request_disable("fixture.plugin", command_id="disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    kernel.close()


def test_ambiguous_journal_quarantine_advances_generation_when_revoking_live_binding(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.LOADED
    held = kernel.snapshot()
    generation_before = kernel.generation
    assert kernel._journal is not None

    def ambiguous(_payload):
        raise capability_plugin_journal_module.JournalCommitAmbiguousError(
            "injected ambiguous append"
        )

    monkeypatch.setattr(kernel._journal, "append_request", ambiguous)
    refused = kernel.request_disable("fixture.plugin", command_id="disable")

    assert refused.event is LifecycleEvent.RESTART_REQUIRED
    assert refused.detail_code == "journal_commit_ambiguous"
    assert kernel.generation == generation_before + 1
    with kernel.snapshot() as current:
        assert current.contributions == {}
    assert held.resolve("fixture.base")() == "fixture-base"
    held.close()
    kernel._journal.close()


def test_outstanding_lease_diagnostics_make_forgotten_close_and_shutdown_refusal_visible(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel(discover(root), receipt_path=receipts)
    forgotten = kernel.snapshot()

    diagnostics = kernel.outstanding_leases()
    assert len(diagnostics) == 1
    assert diagnostics[0].lease_id == forgotten.lease_id
    assert diagnostics[0].generation == 0
    assert diagnostics[0].active_invocations == 0
    with pytest.raises(CapabilityPluginError) as refused:
        kernel.close()
    assert refused.value.code == "outstanding_snapshot_leases"
    with pytest.raises(CapabilityPluginError) as owner_still_held:
        CapabilityPluginKernel(discover(root), receipt_path=receipts)
    assert owner_still_held.value.code == "journal_owner_unavailable"

    forgotten.close()
    assert kernel.outstanding_leases() == ()
    kernel.close()
    with pytest.raises(CapabilityPluginError) as closed:
        kernel.snapshot()
    assert closed.value.code == "kernel_closed"


def test_keyboard_interrupt_during_kernel_close_restores_open_state(
    monkeypatch,
) -> None:
    kernel = CapabilityPluginKernel((), receipt_writer=lambda _receipt: None)
    held = kernel.snapshot()

    def interrupt_wait(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(kernel._lease_condition, "wait", interrupt_wait)

    with pytest.raises(KeyboardInterrupt):
        kernel.close(timeout=1.0)

    assert kernel._closing is False
    fresh = kernel.snapshot()
    assert fresh.close() == ()
    assert held.close() == ()
    kernel.close()


def test_keyboard_interrupt_during_journal_close_retains_open_kernel_for_retry() -> None:
    class InterruptingJournal:
        def __init__(self) -> None:
            self.closed = False
            self.interrupt = True

        def close(self) -> None:
            if self.interrupt:
                raise KeyboardInterrupt()
            self.closed = True

    journal = InterruptingJournal()
    kernel = CapabilityPluginKernel((), receipt_writer=lambda _receipt: None)
    kernel._journal = journal  # type: ignore[assignment]

    with pytest.raises(KeyboardInterrupt):
        kernel.close()

    assert kernel._closed is False
    assert kernel._closing is False
    assert kernel._journal is journal
    with kernel.snapshot() as current:
        assert current.contributions == {}
    journal.interrupt = False
    kernel.close()
    assert journal.closed is True


def test_descriptor_close_interrupt_permanently_closes_kernel_and_releases_other_process(
    tmp_path: Path,
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    kernel = CapabilityPluginKernel((), receipt_path=receipts)
    journal = kernel._journal_for_current_path()
    real_handle = journal._owner_handle
    assert real_handle is not None

    class CloseThenInterrupt:
        def close(self) -> None:
            real_handle.close()
            raise KeyboardInterrupt()

    journal._owner_handle = CloseThenInterrupt()  # type: ignore[assignment]

    with pytest.raises(KeyboardInterrupt):
        kernel.close()

    assert journal.closed is True
    assert kernel._closed is True
    assert kernel._journal is None
    with pytest.raises(CapabilityPluginError) as rejected:
        kernel.snapshot()
    assert rejected.value.code == "kernel_closed"

    helper = Path(__file__).parent / "_holders" / "hold_capability_journal_owner.py"
    scripts_dir = Path(__file__).parent.parent
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(scripts_dir), process_env.get("PYTHONPATH", "")))
    )
    process = subprocess.Popen(
        [sys.executable, str(helper), str(receipts)],
        cwd=scripts_dir,
        env=process_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
    finally:
        if process.stdin is not None:
            process.stdin.write("release\n")
            process.stdin.flush()
        process.wait(timeout=5)


def test_kernel_close_refuses_loaded_physical_state_until_explicit_drain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw_manifest(), SUCCESS_PLUGIN)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")
    kernel.request_enable("fixture.plugin", command_id="enable")
    kernel.apply_turn_boundary()

    with pytest.raises(CapabilityPluginError) as refused:
        kernel.close()
    assert refused.value.code == "plugins_not_drained"
    kernel.request_disable("fixture.plugin", command_id="disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    kernel.close()


class LoadStageEntryPoint:
    def __init__(self) -> None:
        self.load_count = 0

    def load(self):
        self.load_count += 1

        def register(registrar) -> None:
            registrar.publish("entry.value", "entry-ok", disposer=lambda: True, depends_on=())

        return register


class ResidueFailingEntryPoint:
    def __init__(self, sentinel: str) -> None:
        self.sentinel = sentinel
        self.load_count = 0

    def load(self):
        self.load_count += 1
        sys.modules[self.sentinel] = ModuleType(self.sentinel)
        raise RuntimeError("installed entry point failed after import residue")


def test_approved_entrypoint_executes_captured_bytes_without_calling_load(
    tmp_path: Path,
) -> None:
    sentinel = "_capability_entrypoint_residue_sentinel"
    raw = raw_manifest(
        "entry.plugin",
        source="python_entry_point",
        contributions=(("entry.value", ()),),
        entrypoint="entry_plugin:register",
    )
    entry_point = ResidueFailingEntryPoint(sentinel)
    candidate = CapabilityPluginCandidate(
        manifest=parse_capability_manifest(
            raw, physical_source=ManifestSource.PYTHON_ENTRY_POINT
        ),
        location_key="entry-distribution:residue-plugin",
        artifacts=(
            CapabilityPluginArtifact(
                "entry_plugin.py",
                b"def register(registrar):\n"
                b"    registrar.publish(\n"
                b"        'entry.value', 'entry-ok', disposer=lambda: True, depends_on=(),\n"
                b"    )\n",
            ),
        ),
        entry_point=entry_point,
    )
    kernel = CapabilityPluginKernel((candidate,), receipt_path=tmp_path / "receipts.jsonl")

    kernel.request_enable("entry.plugin", command_id="entry-enable")
    (loaded,) = kernel.apply_turn_boundary()

    assert loaded.event is LifecycleEvent.LOADED
    assert entry_point.load_count == 0
    assert sentinel not in sys.modules
    with kernel.snapshot() as current:
        assert current.resolve("entry.value").read() == "entry-ok"
    kernel.request_disable("entry.plugin", command_id="entry-disable")
    assert kernel.apply_turn_boundary()[0].event is LifecycleEvent.UNLOADED
    kernel.close()


def test_approved_python_entry_point_loads_only_at_turn_boundary(tmp_path: Path) -> None:
    raw = raw_manifest(
        "entry.plugin",
        source="python_entry_point",
        contributions=(("entry.value", ()),),
        entrypoint="entry_plugin:register",
    )
    parsed = parse_capability_manifest(
        raw, physical_source=ManifestSource.PYTHON_ENTRY_POINT
    )
    entry_point = LoadStageEntryPoint()
    candidate = CapabilityPluginCandidate(
        manifest=parsed,
        location_key="entry-distribution:entry-plugin",
        artifacts=(
            CapabilityPluginArtifact(
                "entry_plugin.py",
                b"def register(registrar):\n"
                b"    registrar.publish(\n"
                b"        'entry.value', 'entry-ok', disposer=lambda: True, depends_on=(),\n"
                b"    )\n",
            ),
        ),
        entry_point=entry_point,
    )
    kernel = CapabilityPluginKernel((candidate,), receipt_path=tmp_path / "receipts.jsonl")

    kernel.request_enable("entry.plugin", command_id="entry-enable")
    assert entry_point.load_count == 0
    kernel.apply_turn_boundary()

    assert entry_point.load_count == 0
    with kernel.snapshot() as current:
        assert current.resolve("entry.value").read() == "entry-ok"

    kernel.request_disable("entry.plugin", command_id="entry-disable")
    (terminal,) = kernel.apply_turn_boundary()

    assert terminal.event is LifecycleEvent.UNLOADED
    assert terminal.detail_code == ""
    assert kernel.state("entry.plugin").effective_state is PluginEffectiveState.UNLOADED
    kernel.close()


def test_enabled_by_default_loads_at_first_boundary_without_boot_import(tmp_path: Path) -> None:
    tripwire = tmp_path / "IMPORTED"
    body = f"""
from pathlib import Path
Path({str(tripwire)!r}).write_text("imported", encoding="utf-8")

def register(registrar):
    registrar.publish("fixture.base", "value", disposer=lambda: True, depends_on=())
"""
    raw = raw_manifest(
        contributions=(("fixture.base", ()),),
        enabled_by_default=True,
    )
    root = tmp_path / "extensions"
    write_plugin(root, "fixture", raw, body)
    kernel = CapabilityPluginKernel(discover(root), receipt_path=tmp_path / "receipts.jsonl")

    assert not tripwire.exists()
    with kernel.snapshot() as initial:
        assert initial.contributions == {}
    bootstrap, loaded = kernel.apply_turn_boundary()

    assert bootstrap.phase is LifecyclePhase.REQUEST
    assert bootstrap.outcome is LifecycleOutcome.ACCEPTED
    assert loaded.event is LifecycleEvent.LOADED
    assert tripwire.exists()
    with kernel.snapshot() as current:
        assert current.resolve("fixture.base").read() == "value"


def test_bundled_fixture_is_reversible_with_zero_stale_new_turn_state(tmp_path: Path) -> None:
    bundled = Path(__file__).resolve().parents[2] / "extensions"
    before_modules = {
        name for name in tuple(__import__("sys").modules) if name.startswith("_homie_capability_")
    }
    discovery = discover_capability_plugins(
        [FilesystemPluginSource(ManifestSource.BUNDLED, bundled)]
    )
    candidate = next(
        item
        for item in discovery.active_candidates
        if item.manifest.id == "homie.capability-fixture"
    )
    assert candidate.manifest.enabled_by_default is False
    assert {item.location for item in discovery.legacy} >= {"_example", "blog"}
    assert before_modules == {
        name for name in tuple(__import__("sys").modules) if name.startswith("_homie_capability_")
    }

    receipts = tmp_path / "fixture-receipts.jsonl"
    kernel = CapabilityPluginKernel((candidate,), receipt_path=receipts)
    enabled = kernel.request_enable(
        "homie.capability-fixture", command_id="fixture-enable"
    )
    assert enabled.event is LifecycleEvent.ENABLED
    (loaded,) = kernel.apply_turn_boundary()
    assert loaded.event is LifecycleEvent.LOADED
    held = kernel.snapshot()
    assert held.resolve("homie.fixture.dependent")() == {
        "result": "capability-fixture-ok",
        "disposal_order": (),
    }

    disabled = kernel.request_disable(
        "homie.capability-fixture", command_id="fixture-disable"
    )
    assert disabled.event is LifecycleEvent.UNLOAD_REQUESTED
    assert held.resolve("homie.fixture.base")() == "capability-fixture-base"
    (draining,) = kernel.apply_turn_boundary()
    assert draining.event is LifecycleEvent.DRAINING
    assert held.resolve("homie.fixture.dependent")()["disposal_order"] == ()
    with kernel.snapshot() as current:
        assert current.contributions == {}
        assert current.plugins == ()
    (unloaded,) = held.close()
    assert unloaded.event is LifecycleEvent.UNLOADED
    assert [item.contribution_id for item in unloaded.disposals] == [
        "homie.fixture.dependent",
        "homie.fixture.base",
    ]
    assert [row["event"] for row in receipt_rows(receipts)] == [
        "enabled",
        "loaded",
        "unload_requested",
        "draining",
        "unloaded",
    ]


def test_bundled_v2_fixture_is_harmless_to_legacy_extension_manager() -> None:
    bundled = Path(__file__).resolve().parents[2] / "extensions"
    manager = ExtensionManager()

    discovered = manager.discover([bundled])
    fixture = next(item for item in discovered if item.id == "homie.capability-fixture")

    assert fixture.enabled is False
    assert fixture.status == "disabled"
    assert fixture.commands == []
    assert fixture.intents == []
