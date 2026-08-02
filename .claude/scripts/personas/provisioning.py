"""Crash-recoverable persona blueprint provisioning.

Preview is pure/read-only. Apply re-reads and recompiles under advisory locks,
checks both plan and physical-state hashes, stages a private journal, commits
only compiler-owned files plus Discord bindings, and performs conservative
rollback when a commit step fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from personas.blueprints import (
    BlueprintPlan,
    ProvisionMode,
    compile_blueprint,
    parse_blueprint,
)
from personas.capabilities import (
    build_env_sync_plan,
    render_profile_env,
    safe_env_sync_summary,
)
from personas.core import get_default_homie_root, get_default_paths
from personas.discord_bindings import (
    dump_binding_document,
    load_binding_document,
    reconcile_persona_bindings,
)
from personas.lifecycle import initialize_staged_profile_inventory
from personas.services import (
    ConfigShapeError,
    dump_config_yaml,
    merge_config_patch,
    validate_config_yaml_text,
)
from shared import atomic_write_text, file_lock


class ProvisioningError(RuntimeError):
    """Base error for a refused or failed provisioning operation."""


class ProvisionConflictError(ProvisioningError):
    """The preview no longer matches physical state."""


class ProvisionRecoveryRequiredError(ProvisioningError):
    """A prior transaction cannot be safely recovered automatically."""


@dataclass(frozen=True)
class ProvisionPaths:
    homie_root: Path
    bindings_file: Path
    capability_matrix_file: Path
    master_env_file: Path

    @classmethod
    def defaults(cls) -> ProvisionPaths:
        root = get_default_homie_root()
        configured_bindings = os.environ.get(
            "DISCORD_CHANNEL_BINDINGS_FILE", ""
        ).strip()
        default_paths = get_default_paths()
        return cls(
            homie_root=root,
            bindings_file=(
                Path(configured_bindings).expanduser()
                if configured_bindings
                else default_paths["data"] / "discord-channel-bindings.json"
            ),
            capability_matrix_file=(
                default_paths["data"] / "persona-capability-matrix.yaml"
            ),
            master_env_file=default_paths["env_file"],
        )

    @property
    def profiles_root(self) -> Path:
        return self.homie_root / "profiles"

    @property
    def transactions_root(self) -> Path:
        return self.homie_root / "run" / "persona-provisioning" / "transactions"

    @property
    def locks_root(self) -> Path:
        return self.homie_root / "run" / "persona-provisioning" / "locks"

    @property
    def receipts_root(self) -> Path:
        return self.homie_root / "data" / "persona-provisioning" / "receipts"


@dataclass(frozen=True)
class FileFingerprint:
    relative_path: str
    exists: bool
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ProvisionStateToken:
    persona_id: str
    profile_exists: bool
    files: tuple[FileFingerprint, ...]
    bindings_sha256: str
    matrix_sha256: str
    master_env_sha256: str
    token_sha256: str


@dataclass(frozen=True)
class ProvisionPreview:
    plan: BlueprintPlan
    plan_sha256: str
    state: ProvisionStateToken
    changed_paths: tuple[str, ...]
    env_summary: dict[str, Any]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ProvisionResult:
    persona_id: str
    outcome: Literal["created", "reconciled", "unchanged"]
    transaction_id: str
    plan_sha256: str
    state_before_sha256: str
    state_after_sha256: str
    changed_paths: tuple[str, ...]
    receipt_path: str


_PROFILE_MANAGED = (
    "blueprint.yaml",
    "config.yaml",
    ".env",
    "data/persona-provisioning-readiness.json",
    ".persona-provision-transaction",
)
_WINDOWS_DEVICES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def preview_provision(
    raw_blueprint: dict[str, Any],
    *,
    mode: ProvisionMode | str,
    paths: ProvisionPaths | None = None,
    callable_tools: Collection[str] | None = None,
) -> ProvisionPreview:
    """Compile a secret-free, no-write provisioning preview."""

    resolved_paths = paths or ProvisionPaths.defaults()
    blueprint = parse_blueprint(raw_blueprint)
    _validate_physical_targets(resolved_paths, blueprint.persona_id)
    profile_root = _profile_root(resolved_paths, blueprint.persona_id)
    current_config = _read_config(profile_root / "config.yaml")
    resolved_mode = (
        mode if isinstance(mode, ProvisionMode) else ProvisionMode(str(mode))
    )
    if resolved_mode is ProvisionMode.CREATE and profile_root.exists():
        marker = profile_root / ".persona-provision-transaction"
        marker_data = _read_bytes(marker)
        if marker_data is None:
            raise ProvisioningError(
                f"profile {blueprint.persona_id!r} already exists and was not "
                "created by the blueprint provisioner; use reconcile"
            )
    if resolved_mode is ProvisionMode.RECONCILE and not profile_root.is_dir():
        raise ProvisioningError(
            f"profile {blueprint.persona_id!r} does not exist; use create"
        )
    plan = compile_blueprint(
        blueprint,
        mode=resolved_mode,
        current_config=current_config if profile_root.exists() else None,
        callable_tools=callable_tools,
    )
    plan_sha = _canonical_hash(plan.as_dict())
    rendered, env_summary = _render_managed_files(
        raw_blueprint,
        plan,
        current_config,
        resolved_paths,
    )
    binding_document = load_binding_document(
        resolved_paths.bindings_file, strict=True
    )
    desired_binding = dump_binding_document(
        reconcile_persona_bindings(
            binding_document,
            persona_id=plan.persona_id,
            channels=plan.channels,
        )
    )
    state = _state_token(resolved_paths, plan.persona_id)
    changed: list[str] = []
    for relative_path, desired in rendered.items():
        target = profile_root / relative_path
        if _read_bytes(target) != desired.encode("utf-8"):
            changed.append(str(target))
    if _read_bytes(resolved_paths.bindings_file) != desired_binding.encode(
        "utf-8"
    ):
        changed.append(str(resolved_paths.bindings_file))
    return ProvisionPreview(
        plan=plan,
        plan_sha256=plan_sha,
        state=state,
        changed_paths=tuple(changed),
        env_summary=env_summary,
        warnings=plan.warnings,
    )


def apply_provision(
    raw_blueprint: dict[str, Any],
    *,
    mode: ProvisionMode | str,
    expected_plan_sha256: str,
    expected_state_sha256: str,
    actor: str,
    paths: ProvisionPaths | None = None,
    callable_tools: Collection[str] | None = None,
    reconcile_approved: bool = False,
) -> ProvisionResult:
    """Apply a preview after re-reading and re-compiling physical state."""

    from security import kill_switches

    kill_switches.requireEnabled(
        "persona_mutation", caller="persona_blueprint_provision"
    )
    resolved_paths = paths or ProvisionPaths.defaults()
    blueprint = parse_blueprint(raw_blueprint)
    resolved_mode = (
        mode if isinstance(mode, ProvisionMode) else ProvisionMode(str(mode))
    )
    if resolved_mode is ProvisionMode.MIGRATE:
        raise ProvisioningError("migration is preview-only; use explicit reconcile")
    if resolved_mode is ProvisionMode.RECONCILE and not reconcile_approved:
        raise ProvisioningError("reconcile requires explicit approval")
    _validate_physical_targets(resolved_paths, blueprint.persona_id)
    resolved_paths.locks_root.mkdir(parents=True, exist_ok=True)
    persona_lock = resolved_paths.locks_root / blueprint.persona_id
    with file_lock(persona_lock):
        _validate_physical_targets(resolved_paths, blueprint.persona_id)
        binding_lock = resolved_paths.locks_root / "discord-bindings-global"
        # A crashed transaction may contain a shared binding write even when
        # the new preview does not. Recovery therefore always takes the global
        # binding lock using the same persona -> global order as live apply.
        with file_lock(binding_lock):
            _recover_incomplete_transactions(
                resolved_paths,
                blueprint.persona_id,
            )
        preview = preview_provision(
            raw_blueprint,
            mode=resolved_mode,
            paths=resolved_paths,
            callable_tools=callable_tools,
        )
        _require_preview_match(
            preview,
            expected_plan_sha256=expected_plan_sha256,
            expected_state_sha256=expected_state_sha256,
        )
        _require_create_is_idempotent(preview)
        binding_changed = str(resolved_paths.bindings_file) in set(
            preview.changed_paths
        )
        if binding_changed:
            with file_lock(binding_lock):
                locked_preview = preview_provision(
                    raw_blueprint,
                    mode=resolved_mode,
                    paths=resolved_paths,
                    callable_tools=callable_tools,
                )
                _require_preview_match(
                    locked_preview,
                    expected_plan_sha256=expected_plan_sha256,
                    expected_state_sha256=expected_state_sha256,
                )
                _require_create_is_idempotent(locked_preview)
                return _commit_preview(
                    raw_blueprint,
                    locked_preview,
                    actor=actor,
                    paths=resolved_paths,
                )
        return _commit_preview(
            raw_blueprint,
            preview,
            actor=actor,
            paths=resolved_paths,
        )


def _commit_preview(
    raw_blueprint: dict[str, Any],
    preview: ProvisionPreview,
    *,
    actor: str,
    paths: ProvisionPaths,
) -> ProvisionResult:
    transaction_id = uuid.uuid4().hex
    if not preview.changed_paths:
        return _finalize_result(
            preview,
            actor=actor,
            paths=paths,
            transaction_id=transaction_id,
            outcome="unchanged",
        )
    transaction_root = (
        paths.transactions_root / preview.plan.persona_id / transaction_id
    )
    profile_root = _profile_root(paths, preview.plan.persona_id)
    current_config = _read_config(profile_root / "config.yaml")
    rendered, _summary = _render_managed_files(
        raw_blueprint,
        preview.plan,
        current_config,
        paths,
    )
    current_binding = load_binding_document(paths.bindings_file, strict=True)
    binding_text = dump_binding_document(
        reconcile_persona_bindings(
            current_binding,
            persona_id=preview.plan.persona_id,
            channels=preview.plan.channels,
        )
    )
    if _state_token(paths, preview.plan.persona_id).token_sha256 != (
        preview.state.token_sha256
    ):
        raise ProvisionConflictError(
            "physical state changed while preparing the transaction"
        )
    _validate_physical_targets(paths, preview.plan.persona_id)
    transaction_root.mkdir(parents=True, exist_ok=False)
    is_create = (
        preview.plan.mode == ProvisionMode.CREATE.value
        and not profile_root.exists()
    )
    try:
        entries = _prepare_entries(
            transaction_root,
            profile_root,
            rendered,
            paths.bindings_file,
            binding_text,
            changed_paths=preview.changed_paths,
        )
        if _state_token(paths, preview.plan.persona_id).token_sha256 != (
            preview.state.token_sha256
        ):
            raise ProvisionConflictError(
                "physical state changed while snapshotting the transaction"
            )
    except Exception:
        shutil.rmtree(transaction_root)
        raise
    journal = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "persona_id": preview.plan.persona_id,
        "mode": preview.plan.mode,
        "state": "prepared",
        "profile_root": str(profile_root),
        "created_profile": is_create,
        "entries": entries,
    }
    journal_path = transaction_root / "journal.json"
    _write_json(journal_path, journal)
    writes_started = False
    try:
        staged_profile: Path | None = None
        if is_create:
            staged_profile = _stage_created_profile(
                transaction_root,
                preview.plan.persona_id,
                rendered,
            )
            created_files, created_dirs = _tree_manifest(staged_profile)
            journal["created_files"] = created_files
            journal["created_dirs"] = created_dirs
            _write_json(journal_path, journal)
        journal["state"] = "committing"
        _write_json(journal_path, journal)
        _assert_transaction_preconditions(entries)
        writes_started = True
        if is_create:
            assert staged_profile is not None
            profile_root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_profile, profile_root)
        else:
            _commit_reconciled_profile(
                profile_root,
                rendered,
                entries=entries,
            )
        if str(paths.bindings_file) in set(preview.changed_paths):
            binding_entry = _entry_for_target(entries, paths.bindings_file)
            _assert_entry_precondition(binding_entry)
            atomic_write_text(paths.bindings_file, binding_text)
        journal["state"] = "committed"
        _write_json(journal_path, journal)
    except Exception:
        if not writes_started:
            shutil.rmtree(transaction_root)
            raise
        journal["state"] = "rolling_back"
        _write_json(journal_path, journal)
        _rollback_journal(
            journal_path,
            expected_profile_root=profile_root,
            expected_bindings_file=paths.bindings_file,
            expected_persona_id=preview.plan.persona_id,
        )
        recovered = json.loads(journal_path.read_text(encoding="utf-8"))
        if recovered.get("state") == "rolled_back":
            shutil.rmtree(transaction_root)
        raise

    outcome: Literal["created", "reconciled", "unchanged"] = (
        "created" if is_create else "reconciled"
    )
    try:
        return _finalize_result(
            preview,
            actor=actor,
            paths=paths,
            transaction_id=transaction_id,
            outcome=outcome,
        )
    finally:
        # Backups may include a derived env file. They are recovery state only
        # and must not remain after a completed transaction, even if the
        # receipt sink itself fails.
        if transaction_root.is_dir():
            shutil.rmtree(transaction_root)


def _finalize_result(
    preview: ProvisionPreview,
    *,
    actor: str,
    paths: ProvisionPaths,
    transaction_id: str,
    outcome: Literal["created", "reconciled", "unchanged"],
) -> ProvisionResult:
    state_after = _state_token(paths, preview.plan.persona_id)
    receipt = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "persona_id": preview.plan.persona_id,
        "actor": actor,
        "outcome": outcome,
        "plan_sha256": preview.plan_sha256,
        "state_before_sha256": preview.state.token_sha256,
        "state_after_sha256": state_after.token_sha256,
        "changed_paths": list(preview.changed_paths),
        "created_at": datetime.now(UTC).isoformat(),
        "env": preview.env_summary,
    }
    receipt_path = (
        paths.receipts_root
        / preview.plan.persona_id
        / f"{transaction_id}.json"
    )
    _validate_physical_targets(paths, preview.plan.persona_id)
    _write_json(receipt_path, receipt)
    _best_effort_audit(actor, preview.plan.persona_id, outcome, receipt)
    return ProvisionResult(
        persona_id=preview.plan.persona_id,
        outcome=outcome,
        transaction_id=transaction_id,
        plan_sha256=preview.plan_sha256,
        state_before_sha256=preview.state.token_sha256,
        state_after_sha256=state_after.token_sha256,
        changed_paths=preview.changed_paths,
        receipt_path=str(receipt_path),
    )


def _render_managed_files(
    raw_blueprint: dict[str, Any],
    plan: BlueprintPlan,
    current_config: dict[str, Any],
    paths: ProvisionPaths,
) -> tuple[dict[str, str], dict[str, Any]]:
    merged = merge_config_patch(current_config, plan.config_patch)
    config_text = dump_config_yaml(merged)
    validate_config_yaml_text(config_text)
    env_plan = build_env_sync_plan(
        plan.persona_id,
        matrix_path=paths.capability_matrix_file,
        master_env_path=paths.master_env_file,
        env_groups=plan.env_groups,
        profile_config=merged,
        profile_env_path=_profile_root(paths, plan.persona_id) / ".env",
    )
    readiness = {
        "schema_version": 1,
        "persona_id": plan.persona_id,
        "plan_sha256": _canonical_hash(plan.as_dict()),
        "status": "provisioned",
        "scheduled_model_only": all(
            item.model_only and not item.toolsets and not item.tools
            for item in plan.scheduled
        ),
        "missing_tools": list(plan.missing_tools or ()),
    }
    marker = {
        "schema_version": 1,
        "persona_id": plan.persona_id,
        "plan_sha256": _canonical_hash(plan.as_dict()),
    }
    return (
        {
            "blueprint.yaml": yaml.safe_dump(
                raw_blueprint,
                sort_keys=False,
                allow_unicode=False,
            ),
            "config.yaml": config_text,
            ".env": render_profile_env(env_plan),
            "data/persona-provisioning-readiness.json": (
                json.dumps(readiness, indent=2, sort_keys=True) + "\n"
            ),
            ".persona-provision-transaction": (
                json.dumps(marker, sort_keys=True) + "\n"
            ),
        },
        safe_env_sync_summary(env_plan),
    )


def _prepare_entries(
    transaction_root: Path,
    profile_root: Path,
    rendered: dict[str, str],
    bindings_file: Path,
    binding_text: str,
    *,
    changed_paths: Collection[str],
) -> list[dict[str, Any]]:
    backups = transaction_root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    changed = {
        os.path.normcase(str(_absolute_lexical(Path(path))))
        for path in changed_paths
    }
    targets = [
        (profile_root / relative, content)
        for relative, content in rendered.items()
        if os.path.normcase(
            str(_absolute_lexical(profile_root / relative))
        ) in changed
    ]
    if os.path.normcase(str(_absolute_lexical(bindings_file))) in changed:
        targets.append((bindings_file, binding_text))
    for index, (target, content) in enumerate(targets):
        before = _read_bytes(target)
        backup_rel = ""
        if before is not None:
            backup = backups / f"{index}.bin"
            backup.write_bytes(before)
            backup_rel = str(backup.relative_to(transaction_root))
        entries.append(
            {
                "target": str(target),
                "before_exists": before is not None,
                "before_sha256": _hash_bytes(before or b""),
                "post_sha256": _hash_bytes(content.encode("utf-8")),
                "backup": backup_rel,
            }
        )
    return entries


def _assert_transaction_preconditions(entries: list[dict[str, Any]]) -> None:
    """Refuse to overwrite a target that changed after the locked preview."""

    for entry in entries:
        _assert_entry_precondition(entry)


def _assert_entry_precondition(entry: dict[str, Any]) -> None:
    target = Path(str(entry["target"]))
    current = _read_bytes(target)
    if (current is not None) != bool(entry["before_exists"]):
        raise ProvisionConflictError(
            f"transaction target existence changed: {target}"
        )
    if _hash_bytes(current or b"") != entry["before_sha256"]:
        raise ProvisionConflictError(
            f"transaction target changed while preparing: {target}"
        )


def _entry_for_target(
    entries: list[dict[str, Any]],
    target: Path,
) -> dict[str, Any]:
    expected = os.path.normcase(str(_absolute_lexical(target)))
    for entry in entries:
        candidate = os.path.normcase(
            str(_absolute_lexical(Path(str(entry["target"]))))
        )
        if candidate == expected:
            return entry
    raise ProvisionRecoveryRequiredError(
        f"transaction write target has no recovery entry: {target}"
    )


def _stage_created_profile(
    transaction_root: Path,
    persona_id: str,
    rendered: dict[str, str],
) -> Path:
    staged_profile = transaction_root / "staged-profile"
    initialize_staged_profile_inventory(staged_profile, persona_id)
    for relative_path, content in rendered.items():
        atomic_write_text(staged_profile / relative_path, content)
    return staged_profile


def _commit_reconciled_profile(
    profile_root: Path,
    rendered: dict[str, str],
    *,
    entries: list[dict[str, Any]],
) -> None:
    for relative_path, content in rendered.items():
        target = profile_root / relative_path
        try:
            entry = _entry_for_target(entries, target)
        except ProvisionRecoveryRequiredError:
            continue
        _assert_entry_precondition(entry)
        atomic_write_text(target, content)


def _recover_incomplete_transactions(
    paths: ProvisionPaths,
    persona_id: str,
) -> None:
    root = paths.transactions_root / persona_id
    if not root.is_dir():
        return
    for transaction in sorted(root.iterdir()):
        journal_path = transaction / "journal.json"
        if not journal_path.is_file():
            raise ProvisionRecoveryRequiredError(
                f"transaction {transaction.name} has no recovery journal"
            )
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        state = str(journal.get("state") or "")
        if state == "committed":
            shutil.rmtree(transaction)
            continue
        if state == "rolled_back":
            shutil.rmtree(transaction)
            continue
        _rollback_journal(
            journal_path,
            expected_profile_root=_profile_root(paths, persona_id),
            expected_bindings_file=paths.bindings_file,
            expected_persona_id=persona_id,
        )
        recovered = json.loads(journal_path.read_text(encoding="utf-8"))
        if recovered.get("state") != "rolled_back":
            raise ProvisionRecoveryRequiredError(
                f"transaction {transaction.name} needs operator attention"
            )
        shutil.rmtree(transaction)


def _rollback_journal(
    journal_path: Path,
    *,
    expected_profile_root: Path,
    expected_bindings_file: Path,
    expected_persona_id: str,
) -> None:
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    transaction_root = journal_path.parent
    profile_root = _validate_recovery_journal(
        journal,
        transaction_root=transaction_root,
        expected_profile_root=expected_profile_root,
        expected_bindings_file=expected_bindings_file,
        expected_persona_id=expected_persona_id,
    )
    if journal.get("created_profile") and profile_root.exists():
        marker = profile_root / ".persona-provision-transaction"
        marker_data = _read_bytes(marker)
        expected_marker = next(
            (
                entry["post_sha256"]
                for entry in journal["entries"]
                if Path(entry["target"]) == marker
            ),
            None,
        )
        if marker_data is None or _hash_bytes(marker_data) != expected_marker:
            journal["state"] = "needs_attention"
            _write_json(journal_path, journal)
            return
        expected_files = journal.get("created_files", {})
        expected_dirs = journal.get("created_dirs", [])
        actual_files, actual_dirs = _tree_manifest(profile_root)
        if (
            not isinstance(expected_files, dict)
            or not isinstance(expected_dirs, list)
            or actual_files != expected_files
            or actual_dirs != expected_dirs
        ):
            journal["state"] = "needs_attention"
            _write_json(journal_path, journal)
            return
        shutil.rmtree(profile_root)

    for entry in reversed(journal.get("entries", [])):
        target = Path(str(entry["target"]))
        if journal.get("created_profile") and _is_within(target, profile_root):
            continue
        current = _read_bytes(target)
        current_hash = _hash_bytes(current or b"")
        if current is not None and current_hash == entry["post_sha256"]:
            if entry["before_exists"]:
                backup = transaction_root / str(entry["backup"])
                backup_bytes = backup.read_bytes()
                if _hash_bytes(backup_bytes) != entry["before_sha256"]:
                    journal["state"] = "needs_attention"
                    _write_json(journal_path, journal)
                    return
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(target, backup_bytes)
            else:
                target.unlink()
        elif entry["before_exists"] and current_hash == entry["before_sha256"]:
            continue
        elif not entry["before_exists"] and current is None:
            continue
        else:
            journal["state"] = "needs_attention"
            _write_json(journal_path, journal)
            return
    journal["state"] = "rolled_back"
    _write_json(journal_path, journal)


def _validate_recovery_journal(
    journal: dict[str, Any],
    *,
    transaction_root: Path,
    expected_profile_root: Path,
    expected_bindings_file: Path,
    expected_persona_id: str,
) -> Path:
    """Fail closed before trusting paths from a persisted recovery journal."""

    if journal.get("persona_id") != expected_persona_id:
        raise ProvisionRecoveryRequiredError(
            "transaction journal persona does not match its lock scope"
        )
    profile_root = Path(str(journal.get("profile_root") or ""))
    _reject_reparse_chain(transaction_root)
    if not _same_physical_path(profile_root, expected_profile_root):
        raise ProvisionRecoveryRequiredError(
            "transaction journal profile root does not match the expected target"
        )
    allowed_profile_targets = {
        (expected_profile_root / relative).resolve(strict=False)
        for relative in _PROFILE_MANAGED
    }
    expected_binding = expected_bindings_file.resolve(strict=False)
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise ProvisionRecoveryRequiredError("transaction journal entries are invalid")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProvisionRecoveryRequiredError(
                "transaction journal entry is invalid"
            )
        target_lexical = _absolute_lexical(
            Path(str(entry.get("target") or ""))
        )
        _reject_reparse_chain(target_lexical)
        target = target_lexical.resolve(strict=False)
        if target != expected_binding and target not in allowed_profile_targets:
            raise ProvisionRecoveryRequiredError(
                f"transaction journal target is outside managed state: {target}"
            )
        backup_name = str(entry.get("backup") or "")
        if entry.get("before_exists") and not backup_name:
            raise ProvisionRecoveryRequiredError(
                "transaction journal is missing a required backup"
            )
        if backup_name:
            backup = _absolute_lexical(transaction_root / backup_name)
            backup_root = _absolute_lexical(transaction_root / "backups")
            if not _is_lexically_within(backup, backup_root):
                raise ProvisionRecoveryRequiredError(
                    "transaction journal backup escapes its private directory"
                )
            _reject_reparse_chain(backup)
            if not backup.is_file():
                raise ProvisionRecoveryRequiredError(
                    "transaction journal backup is missing"
                )
        for hash_field in ("before_sha256", "post_sha256"):
            value = entry.get(hash_field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ProvisionRecoveryRequiredError(
                    f"transaction journal {hash_field} is invalid"
                )
    return expected_profile_root


def _tree_manifest(root: Path) -> tuple[dict[str, str], list[str]]:
    files: dict[str, str] = {}
    directories: list[str] = []
    if not root.is_dir():
        return files, directories
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        is_junction = getattr(path, "is_junction", None)
        if (
            stat.S_ISLNK(info.st_mode)
            or (callable(is_junction) and is_junction())
            or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
        ):
            raise ProvisionRecoveryRequiredError(
                f"transaction profile contains a reparse entry: {relative}"
            )
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            files[relative] = _hash_bytes(path.read_bytes())
        else:
            raise ProvisionRecoveryRequiredError(
                f"transaction profile contains an unsupported entry: {relative}"
            )
    return files, directories


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return validate_config_yaml_text(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigShapeError(f"read: {path}: {exc}") from exc


def _state_token(paths: ProvisionPaths, persona_id: str) -> ProvisionStateToken:
    profile_root = _profile_root(paths, persona_id)
    fingerprints = tuple(
        _fingerprint(profile_root / relative, relative)
        for relative in _PROFILE_MANAGED
    )
    bindings = _fingerprint(paths.bindings_file, "discord-channel-bindings.json")
    matrix = _fingerprint(
        paths.capability_matrix_file, "persona-capability-matrix.yaml"
    )
    master_env = _fingerprint(paths.master_env_file, "master.env")
    payload = {
        "persona_id": persona_id,
        "profile_exists": profile_root.is_dir(),
        "files": [asdict(item) for item in fingerprints],
        "bindings": asdict(bindings),
        "matrix": asdict(matrix),
        "master_env": asdict(master_env),
    }
    return ProvisionStateToken(
        persona_id=persona_id,
        profile_exists=profile_root.is_dir(),
        files=fingerprints,
        bindings_sha256=bindings.sha256,
        matrix_sha256=matrix.sha256,
        master_env_sha256=master_env.sha256,
        token_sha256=_canonical_hash(payload),
    )


def _fingerprint(path: Path, label: str) -> FileFingerprint:
    data = _read_bytes(path)
    return FileFingerprint(
        relative_path=label,
        exists=data is not None,
        sha256=_hash_bytes(data or b""),
        size_bytes=len(data or b""),
    )


def _validate_physical_targets(paths: ProvisionPaths, persona_id: str) -> None:
    if persona_id.casefold() in _WINDOWS_DEVICES:
        raise ProvisioningError(
            f"persona id {persona_id!r} is a reserved Windows device name"
        )
    root = _absolute_lexical(paths.homie_root)
    profiles_root = _absolute_lexical(paths.profiles_root)
    profile_root = _absolute_lexical(_profile_root(paths, persona_id))
    if not _is_lexically_within(profile_root, profiles_root):
        raise ProvisioningError("profile target escapes the Homie profiles root")
    for candidate in (
        root,
        profiles_root,
        profile_root,
        _absolute_lexical(paths.transactions_root),
        _absolute_lexical(paths.transactions_root / persona_id),
        _absolute_lexical(paths.locks_root),
        _absolute_lexical(paths.locks_root / persona_id),
        _absolute_lexical(
            (paths.locks_root / persona_id).with_suffix(".lock")
        ),
        _absolute_lexical(
            paths.locks_root / "discord-bindings-global"
        ),
        _absolute_lexical(
            (
                paths.locks_root / "discord-bindings-global"
            ).with_suffix(".lock")
        ),
        _absolute_lexical(paths.receipts_root),
        _absolute_lexical(paths.receipts_root / persona_id),
        _absolute_lexical(paths.bindings_file),
        _absolute_lexical(paths.capability_matrix_file),
        _absolute_lexical(paths.master_env_file),
    ):
        _reject_reparse_chain(candidate)


def _reject_reparse_chain(path: Path) -> None:
    cursor = path.expanduser()
    parents = list(reversed(cursor.parents)) + [cursor]
    for part in parents:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ProvisioningError(f"symlink target is not allowed: {part}")
        is_junction = getattr(part, "is_junction", None)
        if callable(is_junction) and is_junction():
            raise ProvisioningError(f"junction target is not allowed: {part}")
        if getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ProvisioningError(f"reparse-point target is not allowed: {part}")


def _require_preview_match(
    preview: ProvisionPreview,
    *,
    expected_plan_sha256: str,
    expected_state_sha256: str,
) -> None:
    if preview.plan_sha256 != expected_plan_sha256:
        raise ProvisionConflictError("compiled plan changed after preview")
    if preview.state.token_sha256 != expected_state_sha256:
        raise ProvisionConflictError("physical state changed after preview")


def _require_create_is_idempotent(preview: ProvisionPreview) -> None:
    if (
        preview.plan.mode == ProvisionMode.CREATE.value
        and preview.state.profile_exists
        and preview.changed_paths
    ):
        raise ProvisioningError(
            "existing managed profile has drift; use reconcile with explicit approval"
        )


def _profile_root(paths: ProvisionPaths, persona_id: str) -> Path:
    return paths.profiles_root / persona_id


def _absolute_lexical(path: Path) -> Path:
    """Return an absolute path without resolving symlinks or junctions."""

    return Path(os.path.abspath(str(path.expanduser())))


def _is_lexically_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return _hash_bytes(payload)


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _same_physical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(
        str(right.resolve(strict=False))
    )


def _best_effort_audit(
    actor: str,
    persona_id: str,
    outcome: str,
    receipt: dict[str, Any],
) -> None:
    try:
        from dashboard_api import _audit_write

        _audit_write(
            operator_id=actor,
            action="persona_blueprint_provision",
            target_persona_id=persona_id,
            outcome=outcome,
            detail={
                "transaction_id": receipt["transaction_id"],
                "plan_sha256": receipt["plan_sha256"],
                "changed_paths": receipt["changed_paths"],
            },
        )
    except Exception:
        pass


__all__ = [
    "FileFingerprint",
    "ProvisionConflictError",
    "ProvisionPaths",
    "ProvisionPreview",
    "ProvisionRecoveryRequiredError",
    "ProvisionResult",
    "ProvisionStateToken",
    "ProvisioningError",
    "apply_provision",
    "preview_provision",
]
