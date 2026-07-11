"""Durable, exact-byte compensating rollback for one applied amendment.

This module is the rollback domain service that sits beside
``cognition.amendments``: it lists eligible applied-amendment snapshots
(non-healing, read-only) and performs a single compensating rollback with
durable intent (written before any target mutation), crash reconciliation,
and cooperative ledger+target locking. Rollback/rescue bytes are always
handled as raw ``bytes`` — never decoded/re-encoded — so legacy snapshot
content survives round-trip exactly.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cognition.amendments import (
    AMENDMENT_TARGETS,
    ProposalLedger,
    _ledger_lock,
    _parse_record_line,
    target_file_lock,
)

_SUCCESS_REASONS = frozenset({
    "rollback_completed",
    "rollback_reconciled_after_crash",
    "rollback_already_completed",
})
_REFUSAL_REASONS = frozenset({
    "invalid_proposal_id",
    "proposal_not_found",
    "duplicate_proposal_id",
    "proposal_not_applied",
    "missing_apply_hashes",
    "invalid_actor",
    "invalid_reason",
    "target_not_allowed",
    "target_path_invalid",
    "target_missing",
    "target_unreadable",
    "snapshot_path_invalid",
    "snapshot_missing",
    "snapshot_unreadable",
    "snapshot_hash_mismatch",
})
_CONFLICT_REASONS = frozenset({"target_hash_conflict"})
_FAILURE_REASONS = frozenset({
    "lock_timeout",
    "ledger_read_failed",
    "rescue_snapshot_failed",
    "ledger_prepare_failed",
    "target_restore_failed",
    "target_verify_failed",
    "ledger_finalize_failed",
})

_ACTOR_MAX_CHARS = 200
_REASON_MAX_CHARS = 2000


@dataclass(frozen=True)
class AmendmentSnapshot:
    """One ledger row's rollback-listing view: identity, hashes, eligibility."""

    proposal_id: str
    target_file: str
    status: str
    created_at: str
    applied_at: str | None
    before_hash: str
    after_hash: str
    snapshot_path: str
    eligible: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class AmendmentRollbackResult:
    """Result of one ``rollback_amendment`` attempt."""

    proposal_id: str
    status: Literal["rolled_back", "conflict", "refused", "failed"]
    reason: str
    rollback_before_hash: str = ""
    rollback_after_hash: str = ""
    rollback_rescue_snapshot_path: str = ""


# =============================================================================
# Byte helpers — exact-byte, never decode/re-encode.
# =============================================================================


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes durably via a sibling tempfile + fsync + os.replace.

    Errors from the write/fsync/replace steps propagate to the caller; on
    any such failure the sibling tempfile is best-effort removed (its own
    cleanup failure is swallowed) so no residue survives. The parent
    directory fsync after a successful replace is POSIX-only best-effort
    (a no-op path on win32, which has no directory-fsync primitive).
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        delete=False,
        suffix=".tmp",
    )
    tmp_name = handle.name
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    except OSError:
        try:
            handle.close()
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    if sys.platform != "win32":
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


# =============================================================================
# Path confinement — never trust a caller/ledger-supplied path outside its
# confined root; symlinks are rejected (not followed).
# =============================================================================


def _is_link_or_reparse(path: Path) -> bool:
    """Reject symlinks and Windows reparse points before resolution."""
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
        reparse = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attrs & reparse)
    except OSError:
        return False


def _has_link_component(path: Path) -> bool:
    current = path
    while True:
        if _is_link_or_reparse(current):
            return True
        if current == current.parent:
            return False
        current = current.parent


def _lockable_target_path(
    memory_dir: Path | str, target_file: str
) -> tuple[Path | None, str | None]:
    """Confine (but do not require existence of) a proposal's target path.

    Used before acquiring the target lock, when the file may legitimately
    be missing (that becomes ``target_missing`` once we validate further
    under the lock).
    """

    if target_file not in AMENDMENT_TARGETS:
        return None, "target_not_allowed"
    try:
        supplied_root = Path(memory_dir)
        candidate = supplied_root / target_file
        if _has_link_component(supplied_root) or _is_link_or_reparse(candidate):
            return None, "target_path_invalid"
        memory_root = supplied_root.resolve(strict=True)
        resolved = candidate.resolve()
    except OSError:
        return None, "target_path_invalid"
    if resolved.parent != memory_root:
        return None, "target_path_invalid"
    return resolved, None


def _read_target_bytes(target_path: Path) -> tuple[bytes | None, str | None]:
    try:
        # Revalidate the original path and every ancestor immediately before
        # each locked read. Advisory locking cannot constrain an uncooperative
        # external process, but cooperative writers cannot race this check.
        if _has_link_component(target_path):
            return None, "target_path_invalid"
        if not target_path.exists():
            return None, "target_missing"
        if not target_path.is_file():
            return None, "target_path_invalid"
        return target_path.read_bytes(), None
    except OSError:
        return None, "target_unreadable"


def _read_snapshot_bytes(snapshot_path: Path) -> tuple[bytes | None, str | None]:
    try:
        if _has_link_component(snapshot_path):
            return None, "snapshot_path_invalid"
        if not snapshot_path.exists():
            return None, "snapshot_missing"
        if not snapshot_path.is_file():
            return None, "snapshot_path_invalid"
        return snapshot_path.read_bytes(), None
    except OSError:
        return None, "snapshot_unreadable"


def _classify_target(
    memory_dir: Path | str, target_file: str
) -> tuple[Path | None, bytes | None, str | None]:
    resolved, reason = _lockable_target_path(memory_dir, target_file)
    if resolved is None:
        return None, None, reason
    data, read_reason = _read_target_bytes(resolved)
    if read_reason is not None:
        return None, None, read_reason
    return resolved, data, None


def _classify_snapshot(
    ledger_path: Path | str, snapshot_path_value: str
) -> tuple[Path | None, bytes | None, str | None]:
    value = str(snapshot_path_value or "").strip()
    if not value:
        return None, None, "snapshot_path_invalid"
    try:
        ledger_parent = Path(ledger_path).parent
        supplied = Path(value)
        if not supplied.is_absolute():
            supplied = ledger_parent / supplied
        if _has_link_component(ledger_parent) or _has_link_component(supplied):
            return None, None, "snapshot_path_invalid"
        rollback_root = (ledger_parent / "rollback").resolve()
        resolved = supplied.resolve()
    except OSError:
        return None, None, "snapshot_path_invalid"
    try:
        resolved.relative_to(rollback_root)
    except ValueError:
        return None, None, "snapshot_path_invalid"
    data, read_reason = _read_snapshot_bytes(resolved)
    if read_reason is not None:
        return None, None, read_reason
    return resolved, data, None


def _resolve_target_path(memory_dir: Path | str, target_file: str) -> Path | None:
    resolved, _data, reason = _classify_target(memory_dir, target_file)
    return resolved if reason is None else None


def _resolve_snapshot_path(
    ledger_path: Path | str, snapshot_path: str
) -> Path | None:
    resolved, _data, reason = _classify_snapshot(ledger_path, snapshot_path)
    return resolved if reason is None else None


def _rescue_snapshot_path(
    ledger_path: Path, target_path: Path, proposal_id: str
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_id = str(proposal_id).replace("-", "")[:12]
    rollback_dir = ledger_path.parent / "rollback"
    return rollback_dir / f"{target_path.name}.rollback.{timestamp}.{safe_id}.rescue"


# =============================================================================
# Listing — non-healing, read-only, never touches the ledger file.
# =============================================================================


def list_amendment_snapshots(
    ledger: ProposalLedger,
    memory_dir: Path | str,
    *,
    proposal_id: str | None = None,
) -> list[AmendmentSnapshot]:
    """Return rollback-eligible-or-not snapshot rows, newest-first.

    Reads only via the ledger's raw-line/parse path — never
    ``ProposalLedger.read_all()`` (which heals and can write), never
    acquires a mutation lock, never creates the ledger file or its parent
    directories. An absent ledger returns ``[]``.
    """

    records: list[dict[str, Any]] = []
    id_counts: dict[str, int] = {}
    try:
        raw_lines = ledger._read_raw_lines()
    except Exception:
        return []
    for line in raw_lines:
        record = _parse_record_line(line)
        if record is None:
            continue
        rid = record.get("id")
        if rid:
            id_counts[rid] = id_counts.get(rid, 0) + 1
        records.append(record)

    snapshots: list[AmendmentSnapshot] = []
    for record in records:
        snapshot_path = str(record.get("rollback_snapshot_path") or "")
        if not snapshot_path:
            continue
        rid = record.get("id")
        if proposal_id is not None and rid != proposal_id:
            continue
        duplicate = id_counts.get(rid, 0) > 1
        eligible, reason = _snapshot_eligibility(
            record, ledger.path, memory_dir, duplicate
        )
        snapshots.append(
            AmendmentSnapshot(
                proposal_id=str(rid or ""),
                target_file=str(record.get("target_file") or ""),
                status=str(record.get("status") or ""),
                created_at=str(record.get("created_at") or ""),
                applied_at=record.get("applied_at"),
                before_hash=str(record.get("before_hash") or ""),
                after_hash=str(record.get("after_hash") or ""),
                snapshot_path=snapshot_path,
                eligible=eligible,
                reason=reason,
            )
        )
    snapshots.sort(
        key=lambda snap: (snap.applied_at or "", snap.created_at or "", snap.proposal_id),
        reverse=True,
    )
    return snapshots


def _snapshot_eligibility(
    record: dict[str, Any],
    ledger_path: Path,
    memory_dir: Path | str,
    duplicate: bool,
) -> tuple[bool, str | None]:
    if duplicate:
        return False, "duplicate_proposal_id"
    if record.get("status") != "applied":
        return False, "proposal_not_applied"
    before_hash = str(record.get("before_hash") or "")
    after_hash = str(record.get("after_hash") or "")
    if not before_hash or not after_hash:
        return False, "missing_apply_hashes"
    target_file = str(record.get("target_file") or "")
    _target_path, target_data, target_reason = _classify_target(memory_dir, target_file)
    if target_reason is not None:
        return False, target_reason
    if _sha256_bytes(target_data) != after_hash:
        return False, "target_hash_conflict"
    snapshot_value = str(record.get("rollback_snapshot_path") or "")
    _snap_path, snap_data, snap_reason = _classify_snapshot(ledger_path, snapshot_value)
    if snap_reason is not None:
        return False, snap_reason
    if _sha256_bytes(snap_data) != before_hash:
        return False, "snapshot_hash_mismatch"
    return True, None


# =============================================================================
# Rollback — durable intent before mutation, crash reconciliation.
# =============================================================================


def rollback_amendment(
    proposal_id: str,
    actor: str,
    reason: str,
    *,
    ledger: ProposalLedger,
    memory_dir: Path | str,
) -> AmendmentRollbackResult:
    """Compensate one applied amendment with an exact-byte target restore.

    Always acquires the ledger lock, then the target lock (never inverted).
    Durable intent (``rollback_pending``) is written and confirmed BEFORE
    any target mutation; recovery of an interrupted prior attempt is
    handled transparently on the next call for the same id.
    """

    raw_id = str(proposal_id or "").strip()
    if not raw_id:
        return AmendmentRollbackResult(
            proposal_id=str(proposal_id or ""),
            status="refused",
            reason="invalid_proposal_id",
        )

    actor_text = str(actor or "").strip()
    if not actor_text or len(actor_text) > _ACTOR_MAX_CHARS:
        return AmendmentRollbackResult(
            proposal_id=raw_id, status="refused", reason="invalid_actor"
        )

    reason_text = str(reason or "").strip()
    if not reason_text or len(reason_text) > _REASON_MAX_CHARS:
        return AmendmentRollbackResult(
            proposal_id=raw_id, status="refused", reason="invalid_reason"
        )

    try:
        with _ledger_lock(ledger.path):
            record, refusal = _find_unique_raw_record(ledger, raw_id)
            if refusal is not None:
                return AmendmentRollbackResult(
                    proposal_id=raw_id, status="refused", reason=refusal
                )

            target_file = str(record.get("target_file") or "")
            target_path, target_reason = _lockable_target_path(memory_dir, target_file)
            if target_path is None:
                return AmendmentRollbackResult(
                    proposal_id=raw_id, status="refused", reason=target_reason
                )

            with target_file_lock(target_path):
                return _rollback_locked(
                    raw_id, actor_text, reason_text, record, target_path, ledger
                )
    except TimeoutError:
        return AmendmentRollbackResult(
            proposal_id=raw_id, status="failed", reason="lock_timeout"
        )
    except Exception:
        return AmendmentRollbackResult(
            proposal_id=raw_id, status="failed", reason="ledger_read_failed"
        )


def _find_unique_raw_record(
    ledger: ProposalLedger, proposal_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    matches: list[dict[str, Any]] = []
    for line in ledger._read_raw_lines():
        record = _parse_record_line(line)
        if record is not None and record.get("id") == proposal_id:
            matches.append(record)
    if not matches:
        return None, "proposal_not_found"
    if len(matches) > 1:
        return None, "duplicate_proposal_id"
    return matches[0], None


def _rollback_locked(
    proposal_id: str,
    actor: str,
    reason_text: str,
    record: dict[str, Any],
    target_path: Path,
    ledger: ProposalLedger,
) -> AmendmentRollbackResult:
    status = str(record.get("status") or "")

    if status == "rolled_back":
        return _recover_rolled_back(proposal_id, record, target_path)

    if status == "rollback_pending":
        return _recover_pending(proposal_id, record, target_path, ledger)

    if status != "applied":
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="refused", reason="proposal_not_applied"
        )

    before_hash = str(record.get("before_hash") or "")
    after_hash = str(record.get("after_hash") or "")
    if not before_hash or not after_hash:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="refused", reason="missing_apply_hashes"
        )

    target_data, target_reason = _read_target_bytes(target_path)
    if target_reason is not None:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="refused", reason=target_reason
        )
    if _sha256_bytes(target_data) != after_hash:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="conflict", reason="target_hash_conflict"
        )

    snapshot_value = str(record.get("rollback_snapshot_path") or "")
    _snap_path, snap_data, snap_reason = _classify_snapshot(ledger.path, snapshot_value)
    if snap_reason is not None:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="refused", reason=snap_reason
        )
    if _sha256_bytes(snap_data) != before_hash:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="refused", reason="snapshot_hash_mismatch"
        )

    rescue_path = _rescue_snapshot_path(ledger.path, target_path, proposal_id)
    try:
        # Keep path substitution detection mechanically adjacent to replacement.
        if _has_link_component(rescue_path):
            raise OSError("rescue path substituted")
        _atomic_write_bytes(rescue_path, target_data)
        rescue_check = rescue_path.read_bytes()
    except OSError:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="failed", reason="rescue_snapshot_failed"
        )
    if _sha256_bytes(rescue_check) != after_hash:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="failed", reason="rescue_snapshot_failed"
        )

    requested_at = datetime.now(UTC).isoformat()
    try:
        outcome = ledger._update_record_unique(
            proposal_id,
            {
                "status": "rollback_pending",
                "rollback_actor": actor,
                "rollback_reason": reason_text,
                "rollback_requested_at": requested_at,
                "rollback_before_hash": after_hash,
                "rollback_rescue_snapshot_path": str(rescue_path),
                "rollback_error": None,
            },
        )
    except Exception:
        outcome = "io_error"
    if outcome != "updated":
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="failed", reason="ledger_prepare_failed"
        )

    return _restore_and_finalize(
        proposal_id, before_hash, after_hash, target_path, snap_data, rescue_path, ledger
    )


def _restore_and_finalize(
    proposal_id: str,
    before_hash: str,
    after_hash: str,
    target_path: Path,
    restore_bytes: bytes,
    rescue_path: Path,
    ledger: ProposalLedger,
) -> AmendmentRollbackResult:
    """Re-read target, replace with the ORIGINAL pre-apply snapshot bytes.

    ``restore_bytes`` is the validated ``before_hash`` snapshot content —
    NOT the ``rescue_path`` safety copy (which holds ``after_hash`` bytes
    purely as a recovery aid and is never itself written back to target).
    """

    current_data, current_reason = _read_target_bytes(target_path)
    if current_reason is not None:
        return AmendmentRollbackResult(
            proposal_id=proposal_id,
            status="failed",
            reason="target_restore_failed",
            rollback_before_hash=after_hash,
            rollback_rescue_snapshot_path=str(rescue_path),
        )
    if _sha256_bytes(current_data) != after_hash:
        return AmendmentRollbackResult(
            proposal_id=proposal_id,
            status="conflict",
            reason="target_hash_conflict",
            rollback_before_hash=after_hash,
            rollback_rescue_snapshot_path=str(rescue_path),
        )

    try:
        # Revalidate the original target path and every ancestor immediately
        # before every replacement, not merely at lock acquisition/read time.
        if _has_link_component(target_path):
            raise OSError("target path substituted")
        _atomic_write_bytes(target_path, restore_bytes)
    except OSError:
        return AmendmentRollbackResult(
            proposal_id=proposal_id,
            status="failed",
            reason="target_restore_failed",
            rollback_before_hash=after_hash,
            rollback_rescue_snapshot_path=str(rescue_path),
        )

    verify_data, verify_reason = _read_target_bytes(target_path)
    if verify_reason is not None or _sha256_bytes(verify_data) != before_hash:
        # Verification observed an unknown third state.  Never use the rescue
        # copy as authority over that drift; leave rollback_pending durable for
        # explicit human resolution and perform no further target mutation.
        return AmendmentRollbackResult(
            proposal_id=proposal_id,
            status="failed",
            reason="target_verify_failed",
            rollback_before_hash=after_hash,
            rollback_rescue_snapshot_path=str(rescue_path),
        )

    return _finalize_rolled_back(
        proposal_id,
        after_hash,
        before_hash,
        str(rescue_path),
        ledger,
        success_reason="rollback_completed",
    )


def _finalize_rolled_back(
    proposal_id: str,
    rollback_before_hash: str,
    rollback_after_hash: str,
    rescue_snapshot_path: str,
    ledger: ProposalLedger,
    *,
    success_reason: str,
) -> AmendmentRollbackResult:
    completed_at = datetime.now(UTC).isoformat()
    try:
        outcome = ledger._update_record_unique(
            proposal_id,
            {
                "status": "rolled_back",
                "rolled_back_at": completed_at,
                "rollback_after_hash": rollback_after_hash,
                "rollback_error": None,
            },
        )
    except Exception:
        outcome = "io_error"
    if outcome != "updated":
        return AmendmentRollbackResult(
            proposal_id=proposal_id,
            status="failed",
            reason="ledger_finalize_failed",
            rollback_before_hash=rollback_before_hash,
            rollback_after_hash=rollback_after_hash,
            rollback_rescue_snapshot_path=rescue_snapshot_path,
        )
    return AmendmentRollbackResult(
        proposal_id=proposal_id,
        status="rolled_back",
        reason=success_reason,
        rollback_before_hash=rollback_before_hash,
        rollback_after_hash=rollback_after_hash,
        rollback_rescue_snapshot_path=rescue_snapshot_path,
    )


def _recover_pending(
    proposal_id: str,
    record: dict[str, Any],
    target_path: Path,
    ledger: ProposalLedger,
) -> AmendmentRollbackResult:
    before_hash = str(record.get("before_hash") or "")
    rollback_before_hash = str(record.get("rollback_before_hash") or "")
    rescue_value = str(record.get("rollback_rescue_snapshot_path") or "")
    original_snapshot_value = str(record.get("rollback_snapshot_path") or "")
    if not before_hash or not rollback_before_hash:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="failed", reason="ledger_prepare_failed"
        )

    current_data, current_reason = _read_target_bytes(target_path)
    if current_reason is not None:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="refused", reason=current_reason
        )
    current_hash = _sha256_bytes(current_data)

    if current_hash == before_hash:
        # Target already restored (crash after write, before finalize) —
        # finalize only, no second physical write.
        return _finalize_rolled_back(
            proposal_id,
            rollback_before_hash,
            before_hash,
            rescue_value,
            ledger,
            success_reason="rollback_reconciled_after_crash",
        )

    if current_hash != rollback_before_hash:
        # Unknown third-state bytes belong to somebody else. Rescue is only for
        # recovery of the known after_hash state, never authority to overwrite drift.
        return AmendmentRollbackResult(
            proposal_id=proposal_id,
            status="conflict",
            reason="target_hash_conflict",
            rollback_before_hash=rollback_before_hash,
            rollback_rescue_snapshot_path=rescue_value,
        )

    # current == rollback_before_hash (== original after_hash): re-validate
    # the recorded rescue snapshot's integrity (it should still hold the
    # after_hash bytes it was written with), then restore from the ORIGINAL
    # apply-time snapshot — never the rescue copy, which is a safety net,
    # not the restore source.
    _rescue_path, rescue_data, rescue_reason = _classify_snapshot(
        ledger.path, rescue_value
    )
    if rescue_reason is not None:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="refused", reason=rescue_reason
        )
    if _sha256_bytes(rescue_data) != rollback_before_hash:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="failed", reason="target_restore_failed"
        )

    _orig_path, orig_data, orig_reason = _classify_snapshot(
        ledger.path, original_snapshot_value
    )
    if orig_reason is not None:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="refused", reason=orig_reason
        )
    if _sha256_bytes(orig_data) != before_hash:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="failed", reason="target_restore_failed"
        )

    try:
        # Recovery has the same last-moment substitution boundary as a fresh
        # rollback: validate immediately adjacent to the target replacement.
        if _has_link_component(target_path):
            raise OSError("target path substituted")
        _atomic_write_bytes(target_path, orig_data)
    except OSError:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="failed", reason="target_restore_failed"
        )

    verify_data, verify_reason = _read_target_bytes(target_path)
    if verify_reason is not None or _sha256_bytes(verify_data) != before_hash:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="failed", reason="target_verify_failed"
        )

    return _finalize_rolled_back(
        proposal_id,
        rollback_before_hash,
        before_hash,
        rescue_value,
        ledger,
        success_reason="rollback_reconciled_after_crash",
    )


def _recover_rolled_back(
    proposal_id: str,
    record: dict[str, Any],
    target_path: Path,
) -> AmendmentRollbackResult:
    rollback_before_hash = str(record.get("rollback_before_hash") or "")
    rollback_after_hash = str(record.get("rollback_after_hash") or "")
    rescue_value = str(record.get("rollback_rescue_snapshot_path") or "")
    expected = rollback_after_hash or str(record.get("before_hash") or "")

    current_data, current_reason = _read_target_bytes(target_path)
    if current_reason is not None:
        return AmendmentRollbackResult(
            proposal_id=proposal_id, status="refused", reason=current_reason
        )
    if _sha256_bytes(current_data) == expected:
        return AmendmentRollbackResult(
            proposal_id=proposal_id,
            status="rolled_back",
            reason="rollback_already_completed",
            rollback_before_hash=rollback_before_hash,
            rollback_after_hash=rollback_after_hash,
            rollback_rescue_snapshot_path=rescue_value,
        )
    return AmendmentRollbackResult(
        proposal_id=proposal_id,
        status="conflict",
        reason="target_hash_conflict",
        rollback_before_hash=rollback_before_hash,
        rollback_rescue_snapshot_path=rescue_value,
    )


__all__ = (
    "AmendmentRollbackResult",
    "AmendmentSnapshot",
    "list_amendment_snapshots",
    "rollback_amendment",
)
