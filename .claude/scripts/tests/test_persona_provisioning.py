from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from personas.blueprints import ProvisionMode, build_builtin_blueprint
from personas.provisioning import (
    ProvisionConflictError,
    ProvisioningError,
    ProvisionPaths,
    ProvisionRecoveryRequiredError,
    apply_provision,
    preview_provision,
)

DEV_CHANNEL = "123456789012345678"
BUSINESS_CHANNEL = "987654321098765432"


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
        "OPENAI_API_KEY=top-secret\n"
        "HOMIE_VAULT_DIR=C:/vault\n"
        "BUSINESS_EMAIL=ops@example.com\n",
        encoding="utf-8",
    )
    bindings = tmp_path / "discord-channel-bindings.json"
    bindings.write_bytes(
        (
            json.dumps(
                {"guild_id": "g1", "channels": {}},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    )
    return ProvisionPaths(
        homie_root=tmp_path / "homie",
        bindings_file=bindings,
        capability_matrix_file=matrix,
        master_env_file=master_env,
    )


def _blueprint(channel_id: str | None = DEV_CHANNEL):
    return build_builtin_blueprint(
        "ai-engineer",
        channel_id=channel_id,
    )


def _redirect_learning_audit_ledger(monkeypatch, paths: ProvisionPaths) -> None:
    """Keep the persona-learning audit ledger off the live repo data dir.

    Issue #422: a CREATE-mode apply now appends a row via
    ``append_persona_learning_audit``, resolved from ``config.DATA_DIR`` at
    CALL time (same pattern as ``_isolate_runtime_health_file`` in
    conftest.py for ``RUNTIME_HEALTH_FILE``). Without this, every CREATE
    test in this suite would append a real row to the checkout's live
    ``persona_learning_audit.jsonl``.
    """
    import config as _config

    data_dir = paths.homie_root.parent / "learning-audit-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_config, "DATA_DIR", data_dir)


def _apply(
    blueprint,
    preview,
    paths,
    monkeypatch,
    *,
    mode=ProvisionMode.CREATE,
    reconcile_approved=False,
):
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda *_args, **_kwargs: None,
    )
    _redirect_learning_audit_ledger(monkeypatch, paths)
    return apply_provision(
        blueprint,
        mode=mode,
        expected_plan_sha256=preview.plan_sha256,
        expected_state_sha256=preview.state.token_sha256,
        actor="test-operator",
        paths=paths,
        reconcile_approved=reconcile_approved,
    )


def test_create_is_transactional_secret_free_and_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint()
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    assert "top-secret" not in repr(preview)
    result = _apply(blueprint, preview, paths, monkeypatch)
    profile = paths.profiles_root / "ai-engineer"
    assert result.outcome == "created"
    assert (profile / "memory" / "SOUL.md").is_file()
    assert "top-secret" in (profile / ".env").read_text(encoding="utf-8")
    config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert config["toolsets"] == ["safe_core", "ai_engineering"]
    assert config["tools"] == []
    assert config["capability_blueprint"]["env_groups"] == [
        "runtime_core",
        "vault_memory",
    ]
    receipt_text = Path(result.receipt_path).read_text(encoding="utf-8")
    assert "top-secret" not in receipt_text
    binding = json.loads(paths.bindings_file.read_text(encoding="utf-8"))
    assert (
        binding["channels"][DEV_CHANNEL]["persona"]
        == "ai-engineer"
    )
    assert binding["channels"][DEV_CHANNEL]["enabled"] is False

    mtimes = {
        name: (profile / name).stat().st_mtime_ns
        for name in (
            "blueprint.yaml",
            "config.yaml",
            ".env",
            "data/persona-provisioning-readiness.json",
        )
    }
    second_preview = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    assert second_preview.changed_paths == ()
    second = _apply(blueprint, second_preview, paths, monkeypatch)
    assert second.outcome == "unchanged"
    assert mtimes == {
        name: (profile / name).stat().st_mtime_ns for name in mtimes
    }


def test_apply_refuses_stale_state(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint()
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    paths.bindings_file.write_text(
        json.dumps({"guild_id": "changed", "channels": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(ProvisionConflictError, match="physical state changed"):
        apply_provision(
            blueprint,
            mode=ProvisionMode.CREATE,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test",
            paths=paths,
        )
    assert not paths.profiles_root.exists()


def test_apply_refuses_changed_master_env_after_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint()
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    paths.master_env_file.write_text(
        "OPENAI_API_KEY=replaced-after-preview\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(ProvisionConflictError, match="physical state changed"):
        apply_provision(
            blueprint,
            mode=ProvisionMode.CREATE,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test",
            paths=paths,
        )
    assert not paths.profiles_root.exists()


def test_reconcile_preserves_authored_sections_and_clears_stale_tools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    profile = paths.profiles_root / "ai-engineer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "persona": {"id": "ai-engineer", "role": "authored"},
                "tools": ["terminal_exec"],
                "curriculum": {"enabled": False, "sources": []},
                "operator_notes": {"keep": True},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    blueprint = _blueprint(channel_id=None)
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.RECONCILE,
        paths=paths,
    )
    result = _apply(
        blueprint,
        preview,
        paths,
        monkeypatch,
        mode=ProvisionMode.RECONCILE,
        reconcile_approved=True,
    )
    assert result.outcome == "reconciled"
    config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert config["tools"] == []
    assert config["persona"]["role"] == "authored"
    assert config["curriculum"]["enabled"] is False
    assert config["operator_notes"] == {"keep": True}
    # Issue #422 — born-learning is a CREATE-only compiler patch. A profile
    # with no authored `learning` section must stay absent after reconcile,
    # not retroactively opt an existing profile in.
    assert "learning" not in config


def test_create_refuses_existing_unmanaged_profile(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    profile = paths.profiles_root / "ai-engineer"
    profile.mkdir(parents=True)
    with pytest.raises(ProvisioningError, match="was not created"):
        preview_provision(
            _blueprint(),
            mode=ProvisionMode.CREATE,
            paths=paths,
        )


def test_create_refuses_managed_drift_until_approved_reconcile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint(channel_id=None)
    created = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    _apply(blueprint, created, paths, monkeypatch)
    config_path = paths.profiles_root / "ai-engineer" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["toolsets"] = ["operator_exec"]
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    drifted = config_path.read_bytes()

    for approved in (False, True):
        preview = preview_provision(
            blueprint,
            mode=ProvisionMode.CREATE,
            paths=paths,
        )
        with pytest.raises(ProvisioningError, match="use reconcile"):
            _apply(
                blueprint,
                preview,
                paths,
                monkeypatch,
                reconcile_approved=approved,
            )
        assert config_path.read_bytes() == drifted

    reconcile = preview_provision(
        blueprint,
        mode=ProvisionMode.RECONCILE,
        paths=paths,
    )
    result = _apply(
        blueprint,
        reconcile,
        paths,
        monkeypatch,
        mode=ProvisionMode.RECONCILE,
        reconcile_approved=True,
    )
    assert result.outcome == "reconciled"
    repaired = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert repaired["toolsets"] == ["safe_core", "ai_engineering"]


def test_preview_refuses_binding_owned_by_other_persona(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.bindings_file.write_text(
        json.dumps(
            {
                "channels": {
                    DEV_CHANNEL: {
                        "kind": "persona",
                        "persona": "other",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="already bound"):
        preview_provision(
            _blueprint(),
            mode=ProvisionMode.CREATE,
            paths=paths,
        )


def test_preview_refuses_legacy_binding_owned_by_other_persona(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.bindings_file.write_text(
        json.dumps(
            {
                "channels": {
                    DEV_CHANNEL: {
                        "persona": "other",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="already bound"):
        preview_provision(
            _blueprint(),
            mode=ProvisionMode.CREATE,
            paths=paths,
        )


def test_windows_device_persona_is_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    blueprint = build_builtin_blueprint(
        "general-specialist",
        persona_id="con",
    )
    with pytest.raises(ProvisioningError, match="reserved Windows"):
        preview_provision(
            blueprint,
            mode=ProvisionMode.CREATE,
            paths=paths,
        )


def test_failed_reconcile_restores_all_prior_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    profile = paths.profiles_root / "ai-engineer"
    profile.mkdir(parents=True)
    config_path = profile / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "persona": {"id": "ai-engineer", "role": "keep"},
                "toolsets": [],
                "tools": ["terminal_exec"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    before = config_path.read_bytes()
    blueprint = _blueprint(channel_id=None)
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.RECONCILE,
        paths=paths,
    )
    from personas import provisioning

    real_commit = provisioning._commit_reconciled_profile
    unrelated_binding = json.dumps(
        {
            "guild_id": "g1",
            "channels": {
                BUSINESS_CHANNEL: {
                    "kind": "persona",
                    "persona": "founder-operator",
                }
            },
        },
        sort_keys=True,
    ).encode("utf-8")

    def fail_after_profile_write(profile_root, rendered, **kwargs):
        real_commit(profile_root, rendered, **kwargs)
        paths.bindings_file.write_bytes(unrelated_binding)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(
        provisioning,
        "_commit_reconciled_profile",
        fail_after_profile_write,
    )
    monkeypatch.setattr(
        provisioning,
        "_best_effort_audit",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        apply_provision(
            blueprint,
            mode=ProvisionMode.RECONCILE,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test",
            paths=paths,
            reconcile_approved=True,
        )
    assert config_path.read_bytes() == before
    for relative in (
        "blueprint.yaml",
        ".env",
        "data/persona-provisioning-readiness.json",
        ".persona-provision-transaction",
    ):
        assert not (profile / relative).exists()
    assert paths.bindings_file.read_bytes() == unrelated_binding


def test_reconcile_journal_contains_only_the_actual_write_set(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint(channel_id=None)
    created = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    _apply(blueprint, created, paths, monkeypatch)
    profile = paths.profiles_root / "ai-engineer"
    config_path = profile / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["tools"] = ["terminal_exec"]
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.RECONCILE,
        paths=paths,
    )
    assert str(profile / ".env") not in preview.changed_paths
    assert str(paths.bindings_file) not in preview.changed_paths

    from personas import provisioning

    captured: list[dict[str, object]] = []
    real_prepare = provisioning._prepare_entries

    def capture_entries(*args, **kwargs):
        entries = real_prepare(*args, **kwargs)
        captured.extend(entries)
        return entries

    monkeypatch.setattr(provisioning, "_prepare_entries", capture_entries)
    _apply(
        blueprint,
        preview,
        paths,
        monkeypatch,
        mode=ProvisionMode.RECONCILE,
        reconcile_approved=True,
    )
    assert {str(entry["target"]) for entry in captured} == set(
        preview.changed_paths
    )


def test_apply_detects_noncooperating_writer_while_snapshotting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint(channel_id=None)
    created = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    _apply(blueprint, created, paths, monkeypatch)
    profile = paths.profiles_root / "ai-engineer"
    config_path = profile / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["tools"] = ["terminal_exec"]
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.RECONCILE,
        paths=paths,
    )

    from personas import provisioning

    real_prepare = provisioning._prepare_entries
    external_bytes = b"persona:\n  id: ai-engineer\noperator_notes:\n  concurrent: true\n"

    def race_after_snapshot(*args, **kwargs):
        entries = real_prepare(*args, **kwargs)
        config_path.write_bytes(external_bytes)
        return entries

    monkeypatch.setattr(
        provisioning,
        "_prepare_entries",
        race_after_snapshot,
    )
    with pytest.raises(ProvisionConflictError, match="snapshotting"):
        _apply(
            blueprint,
            preview,
            paths,
            monkeypatch,
            mode=ProvisionMode.RECONCILE,
            reconcile_approved=True,
        )
    assert config_path.read_bytes() == external_bytes
    transaction_parent = paths.transactions_root / "ai-engineer"
    assert not transaction_parent.exists() or not any(
        transaction_parent.iterdir()
    )


def test_reconcile_never_writes_an_unchanged_target_that_changes_mid_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint(channel_id=None)
    created = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    _apply(blueprint, created, paths, monkeypatch)
    profile = paths.profiles_root / "ai-engineer"
    config_path = profile / "config.yaml"
    env_path = profile / ".env"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["tools"] = ["terminal_exec"]
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.RECONCILE,
        paths=paths,
    )
    assert str(env_path) not in preview.changed_paths

    from personas import provisioning

    real_atomic_write = provisioning.atomic_write_text
    concurrent_env = b"OPENAI_API_KEY=concurrent-value\n"
    raced = False

    def race_during_commit(path, content):
        nonlocal raced
        if Path(path) == config_path and not raced:
            raced = True
            env_path.write_bytes(concurrent_env)
        return real_atomic_write(path, content)

    monkeypatch.setattr(
        provisioning,
        "atomic_write_text",
        race_during_commit,
    )
    result = _apply(
        blueprint,
        preview,
        paths,
        monkeypatch,
        mode=ProvisionMode.RECONCILE,
        reconcile_approved=True,
    )
    assert result.outcome == "reconciled"
    assert raced
    assert env_path.read_bytes() == concurrent_env


def test_corrupt_recovery_backup_never_overwrites_managed_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint(channel_id=None)
    created = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    _apply(blueprint, created, paths, monkeypatch)
    config_path = paths.profiles_root / "ai-engineer" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["tools"] = ["terminal_exec"]
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.RECONCILE,
        paths=paths,
    )

    from personas import provisioning

    real_commit = provisioning._commit_reconciled_profile

    def corrupt_backup_after_write(profile_root, rendered, **kwargs):
        real_commit(profile_root, rendered, **kwargs)
        journal_path = next(paths.transactions_root.rglob("journal.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        config_entry = next(
            entry
            for entry in journal["entries"]
            if Path(entry["target"]) == config_path
        )
        backup = journal_path.parent / config_entry["backup"]
        backup.write_bytes(b"corrupted-backup")
        raise RuntimeError("injected failure")

    monkeypatch.setattr(
        provisioning,
        "_commit_reconciled_profile",
        corrupt_backup_after_write,
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        _apply(
            blueprint,
            preview,
            paths,
            monkeypatch,
            mode=ProvisionMode.RECONCILE,
            reconcile_approved=True,
        )
    assert config_path.read_bytes() != b"corrupted-backup"
    journal_path = next(paths.transactions_root.rglob("journal.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "needs_attention"


@pytest.mark.parametrize(
    "target_name",
    (
        "transaction-persona",
        "receipt-persona",
        "persona-lock-file",
        "binding-lock-file",
        "configured-binding",
    ),
)
def test_reparse_in_dynamic_write_target_is_rejected_before_preview(
    tmp_path: Path,
    monkeypatch,
    target_name: str,
) -> None:
    paths = _paths(tmp_path)
    blocked = {
        "transaction-persona": paths.transactions_root / "ai-engineer",
        "receipt-persona": paths.receipts_root / "ai-engineer",
        "persona-lock-file": (
            paths.locks_root / "ai-engineer"
        ).with_suffix(".lock"),
        "binding-lock-file": (
            paths.locks_root / "discord-bindings-global"
        ).with_suffix(".lock"),
        "configured-binding": paths.bindings_file,
    }[target_name]
    if target_name in {"transaction-persona", "receipt-persona"}:
        blocked.mkdir(parents=True)
    elif target_name != "configured-binding":
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_bytes(b"lock")
    path_type = type(paths.transactions_root)
    real_is_junction = getattr(path_type, "is_junction", lambda _self: False)

    def fake_is_junction(path):
        return path == blocked or real_is_junction(path)

    monkeypatch.setattr(
        path_type,
        "is_junction",
        fake_is_junction,
        raising=False,
    )
    with pytest.raises(ProvisioningError, match="junction"):
        preview_provision(
            _blueprint(),
            mode=ProvisionMode.CREATE,
            paths=paths,
        )


def test_failed_create_removes_exact_staged_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint()
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    from personas import provisioning

    real_atomic_write = provisioning.atomic_write_text

    def fail_binding_write(path, content):
        if Path(path) == paths.bindings_file:
            raise RuntimeError("binding commit failed")
        return real_atomic_write(path, content)

    monkeypatch.setattr(provisioning, "atomic_write_text", fail_binding_write)
    monkeypatch.setattr(
        provisioning,
        "_best_effort_audit",
        lambda *_args, **_kwargs: None,
    )
    before_binding = paths.bindings_file.read_bytes()
    with pytest.raises(RuntimeError, match="binding commit failed"):
        apply_provision(
            blueprint,
            mode=ProvisionMode.CREATE,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test",
            paths=paths,
        )
    assert not (paths.profiles_root / "ai-engineer").exists()
    assert paths.bindings_file.read_bytes() == before_binding
    transaction_parent = (
        paths.transactions_root / "ai-engineer"
    )
    assert not transaction_parent.exists() or not any(
        transaction_parent.iterdir()
    )


def test_kill_switch_refuses_before_staging(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    blueprint = _blueprint()
    preview = preview_provision(
        blueprint,
        mode=ProvisionMode.CREATE,
        paths=paths,
    )

    def refuse(_name, *, caller=""):
        raise RuntimeError(f"disabled:{caller}")

    monkeypatch.setattr("security.kill_switches.requireEnabled", refuse)
    with pytest.raises(RuntimeError, match="persona_blueprint_provision"):
        apply_provision(
            blueprint,
            mode=ProvisionMode.CREATE,
            expected_plan_sha256=preview.plan_sha256,
            expected_state_sha256=preview.state.token_sha256,
            actor="test",
            paths=paths,
        )
    assert not paths.homie_root.exists()


def test_concurrent_personas_never_lose_shared_binding_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        "personas.provisioning._best_effort_audit",
        lambda *_args, **_kwargs: None,
    )
    _redirect_learning_audit_ledger(monkeypatch, paths)
    blueprints = {
        "ai-engineer": build_builtin_blueprint(
            "ai-engineer",
            channel_id=DEV_CHANNEL,
        ),
        "founder-operator": build_builtin_blueprint(
            "founder-operator",
            channel_id=BUSINESS_CHANNEL,
        ),
    }
    previews = {
        persona_id: preview_provision(
            blueprint,
            mode=ProvisionMode.CREATE,
            paths=paths,
        )
        for persona_id, blueprint in blueprints.items()
    }

    def apply_one(persona_id):
        preview = previews[persona_id]
        try:
            return apply_provision(
                blueprints[persona_id],
                mode=ProvisionMode.CREATE,
                expected_plan_sha256=preview.plan_sha256,
                expected_state_sha256=preview.state.token_sha256,
                actor="test",
                paths=paths,
            ).outcome
        except ProvisionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(apply_one, blueprints))

    assert sorted(outcomes) == ["conflict", "created"]
    first_document = json.loads(paths.bindings_file.read_text(encoding="utf-8"))
    assert len(first_document["channels"]) == 1
    missing = next(
        persona_id
        for persona_id in blueprints
        if not (paths.profiles_root / persona_id).exists()
    )
    retry = preview_provision(
        blueprints[missing],
        mode=ProvisionMode.CREATE,
        paths=paths,
    )
    result = apply_provision(
        blueprints[missing],
        mode=ProvisionMode.CREATE,
        expected_plan_sha256=retry.plan_sha256,
        expected_state_sha256=retry.state.token_sha256,
        actor="test",
        paths=paths,
    )
    assert result.outcome == "created"
    final_document = json.loads(paths.bindings_file.read_text(encoding="utf-8"))
    assert {
        row["persona"] for row in final_document["channels"].values()
    } == {"ai-engineer", "founder-operator"}


def test_recovery_journal_cannot_target_an_unmanaged_file(
    tmp_path: Path,
) -> None:
    from personas import provisioning

    paths = _paths(tmp_path)
    external = tmp_path / "valuable.txt"
    external.write_text("keep", encoding="utf-8")
    transaction = paths.transactions_root / "ai-engineer" / "hostile"
    transaction.mkdir(parents=True)
    journal = {
        "schema_version": 1,
        "transaction_id": "hostile",
        "persona_id": "ai-engineer",
        "mode": "create",
        "state": "rolling_back",
        "profile_root": str(paths.profiles_root / "ai-engineer"),
        "created_profile": False,
        "entries": [
            {
                "target": str(external),
                "before_exists": False,
                "before_sha256": "0" * 64,
                "post_sha256": "0" * 64,
                "backup": "",
            }
        ],
    }
    journal_path = transaction / "journal.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(
        ProvisionRecoveryRequiredError,
        match="outside managed state",
    ):
        provisioning._rollback_journal(
            journal_path,
            expected_profile_root=paths.profiles_root / "ai-engineer",
            expected_bindings_file=paths.bindings_file,
            expected_persona_id="ai-engineer",
        )
    assert external.read_text(encoding="utf-8") == "keep"
