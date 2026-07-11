"""Tests for the durable amendment rollback domain service (PRP-001A).

Exercises exact-byte compensating rollback for one applied amendment:
durable intent (rollback_pending) before target mutation, crash
reconciliation, cooperative target-lock serialization, and non-healing
snapshot listing. Every test is tmp_path-isolated; none touches the live
ledger or vault.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import cognition.amendment_rollback as rollback_module  # noqa: E402
from cognition.amendment_rollback import (  # noqa: E402
    _CONFLICT_REASONS,
    _FAILURE_REASONS,
    _REFUSAL_REASONS,
    _atomic_write_bytes,
    _resolve_snapshot_path,
    _resolve_target_path,
    _sha256_bytes,
    list_amendment_snapshots,
    rollback_amendment,
)
from cognition.amendments import (  # noqa: E402
    AmendmentProposal,
    ProposalLedger,
    _target_lock,
    target_file_lock,
)


def _raw_llm_record(
    content: str,
    *,
    target: str = "MEMORY.md",
    summary: str = "Raw lesson",
) -> dict[str, Any]:
    return {
        "source": "memory_reflect",
        "target_file": target,
        "summary": summary,
        "rationale": "Seen repeatedly in daily logs.",
        "evidence_paths": ["daily/2026-06-09.md"],
        "proposed_content": content,
        "confidence_score": 0.9,
        "status": "pending",
    }


def _write_raw_ledger(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _seed_applied_row(
    tmp_path: Path,
    *,
    before_bytes: bytes = b"# SELF\n\n- original\n",
    after_bytes: bytes = b"# SELF\n\n- original\n\n## Autonomous Amendments\n\n- new\n",
    target_name: str = "SELF.md",
    proposal_id: str | None = None,
) -> dict[str, Any]:
    """Build a tmp memory dir + ledger with one valid applied+rollback-ready row.

    Returns a dict of everything a rollback test needs: ledger, memory_dir,
    target_path, snapshot_path, proposal_id, before_hash, after_hash.
    """

    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir(exist_ok=True)
    target_path = memory_dir / target_name
    target_path.write_bytes(after_bytes)

    ledger_path = tmp_path / "state" / "amendments.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_dir = ledger_path.parent / "rollback"
    rollback_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = rollback_dir / f"{target_name}.snapshot.bak"
    snapshot_path.write_bytes(before_bytes)

    pid = proposal_id or str(uuid.uuid4())
    before_hash = _sha256_bytes(before_bytes)
    after_hash = _sha256_bytes(after_bytes)
    now = "2026-06-09T00:00:00+00:00"
    row = {
        "id": pid,
        "created_at": now,
        "source": "test",
        "target_file": target_name,
        "summary": "Seeded applied row",
        "rationale": "Fixture.",
        "evidence_paths": ["daily/2026-06-09.md"],
        "proposed_content": "Seeded content.",
        "status": "applied",
        "reviewer": "machine_policy",
        "reviewed_at": now,
        "dedupe_key": "seed-key",
        "confidence_score": 0.9,
        "policy_decision": "apply",
        "policy_reason": "policy_allowed",
        "before_hash": before_hash,
        "after_hash": after_hash,
        "rollback_snapshot_path": str(snapshot_path),
        "applied_at": now,
    }
    _write_raw_ledger(ledger_path, [row])
    ledger = ProposalLedger(ledger_path)
    return {
        "ledger": ledger,
        "memory_dir": memory_dir,
        "target_path": target_path,
        "snapshot_path": snapshot_path,
        "proposal_id": pid,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
    }


def _patch_read_bytes_raises_for(monkeypatch: pytest.MonkeyPatch, victim: Path) -> None:
    """Make ``Path.read_bytes`` raise OSError only for ``victim``."""

    original = Path.read_bytes

    def patched(self: Path, *a: Any, **k: Any) -> bytes:
        if self == victim:
            raise OSError("simulated permission denied")
        return original(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", patched)


# =============================================================================
# Target lock primitive
# =============================================================================


def test_target_lock_is_public_alias() -> None:
    assert target_file_lock is _target_lock


def test_target_lock_timeout(tmp_path: Path) -> None:
    target = tmp_path / "SELF.md"
    target.write_text("content", encoding="utf-8")
    lock_file = target.with_suffix(target.suffix + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_file, "w", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(TimeoutError):
            with _target_lock(target, timeout=0.3):
                pass
    finally:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_target_lock_same_thread_reentrant(tmp_path: Path) -> None:
    target = tmp_path / "SELF.md"
    target.write_text("content", encoding="utf-8")
    start = time.monotonic()
    with _target_lock(target):
        with _target_lock(target):  # nested same-thread call is a no-op
            assert True
    assert time.monotonic() - start < 4.0  # never hit the 5s timeout spin


def test_target_lock_two_threads_serialize(tmp_path: Path) -> None:
    target = tmp_path / "SELF.md"
    target.write_text("content", encoding="utf-8")
    events: list[str] = []
    first_holding = threading.Event()
    release_first = threading.Event()

    def hold_first() -> None:
        with _target_lock(target):
            events.append("first_acquired")
            first_holding.set()
            release_first.wait(timeout=5.0)
            events.append("first_released")

    def acquire_second() -> None:
        first_holding.wait(timeout=5.0)
        with _target_lock(target, timeout=5.0):
            events.append("second_acquired")

    t1 = threading.Thread(target=hold_first)
    t2 = threading.Thread(target=acquire_second)
    t1.start()
    t2.start()
    time.sleep(0.2)
    assert events == ["first_acquired"]  # second is genuinely blocked
    release_first.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert events == ["first_acquired", "first_released", "second_acquired"]


def test_target_lock_portable_two_process_style_contention(tmp_path: Path) -> None:
    """Simulates concurrent processes: second acquisition times out cleanly."""

    target = tmp_path / "SELF.md"
    target.write_text("content", encoding="utf-8")
    lock_file = target.with_suffix(target.suffix + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    # A separate handle simulates a different process holding the OS lock —
    # this process's own reentrancy registry does not cover it.
    handle = open(lock_file, "w", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        restores = 0
        with pytest.raises(TimeoutError):
            with _target_lock(target, timeout=0.3):
                restores += 1  # never reached
        assert restores == 0  # exactly one physical restore never happens here
    finally:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# =============================================================================
# Byte helpers
# =============================================================================


@pytest.mark.parametrize(
    "data",
    [
        b"line one\r\nline two\r\n",  # CRLF
        b"line one\nline two\n",  # LF
        "héllo wörld — ünïcödé".encode(),  # non-ASCII
        bytes(range(256)),  # arbitrary bytes, including NUL
    ],
    ids=["crlf", "lf", "non_ascii", "arbitrary_bytes"],
)
def test_sha256_bytes_and_atomic_write_bytes_roundtrip(
    tmp_path: Path, data: bytes
) -> None:
    target = tmp_path / "roundtrip.bin"
    _atomic_write_bytes(target, data)

    assert target.read_bytes() == data
    assert _sha256_bytes(target.read_bytes()) == _sha256_bytes(data)


def test_atomic_write_bytes_fsync_failure_propagates_no_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sub" / "fsync_fail.bin"

    def raising_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(rollback_module.os, "fsync", raising_fsync)

    with pytest.raises(OSError, match="simulated fsync failure"):
        _atomic_write_bytes(target, b"data")

    assert not target.exists()
    leftover = list(target.parent.glob("*.tmp")) if target.parent.exists() else []
    assert leftover == []  # best-effort cleanup left no residue


def test_atomic_write_bytes_replace_failure_propagates_no_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sub" / "replace_fail.bin"

    def raising_replace(src: str, dst: Any) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(rollback_module.os, "replace", raising_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        _atomic_write_bytes(target, b"data")

    assert not target.exists()
    leftover = list(target.parent.glob("*.tmp")) if target.parent.exists() else []
    assert leftover == []


# =============================================================================
# Path confinement
# =============================================================================


def test_resolve_target_path_rejects_disallowed_basename(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    (memory_dir / "NOTALLOWED.md").write_text("x", encoding="utf-8")

    assert _resolve_target_path(memory_dir, "NOTALLOWED.md") is None


def test_resolve_target_path_rejects_traversal_outside_memory_root(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    nested = memory_dir / "nested"
    nested.mkdir()
    # A target_file resolving relative to the WRONG root (nested, not
    # memory_dir) must fail confinement even though "SELF.md" is allowed.
    assert _resolve_target_path(nested, "SELF.md") is None


def test_resolve_target_path_rejects_directory(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    (memory_dir / "SELF.md").mkdir()

    assert _resolve_target_path(memory_dir, "SELF.md") is None


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privilege on win32"
)
def test_resolve_target_path_rejects_symlink(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    real = tmp_path / "real_self.md"
    real.write_text("real", encoding="utf-8")
    (memory_dir / "SELF.md").symlink_to(real)

    assert _resolve_target_path(memory_dir, "SELF.md") is None


def test_resolve_target_path_accepts_confined_regular_file(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "SELF.md"
    target.write_text("ok", encoding="utf-8")

    resolved = _resolve_target_path(memory_dir, "SELF.md")

    assert resolved == target.resolve()


def test_resolve_snapshot_path_rejects_outside_rollback_dir(tmp_path: Path) -> None:
    ledger_path = tmp_path / "state" / "amendments.jsonl"
    ledger_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.bak"
    outside.write_bytes(b"data")

    assert _resolve_snapshot_path(ledger_path, str(outside)) is None


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privilege on win32"
)
def test_resolve_snapshot_path_rejects_symlink(tmp_path: Path) -> None:
    ledger_path = tmp_path / "state" / "amendments.jsonl"
    ledger_path.parent.mkdir(parents=True)
    rollback_dir = ledger_path.parent / "rollback"
    rollback_dir.mkdir()
    real = tmp_path / "real_snap.bak"
    real.write_bytes(b"data")
    link = rollback_dir / "link.bak"
    link.symlink_to(real)

    assert _resolve_snapshot_path(ledger_path, str(link)) is None


def test_resolve_snapshot_path_accepts_confined_file(tmp_path: Path) -> None:
    ledger_path = tmp_path / "state" / "amendments.jsonl"
    ledger_path.parent.mkdir(parents=True)
    rollback_dir = ledger_path.parent / "rollback"
    rollback_dir.mkdir()
    snap = rollback_dir / "snap.bak"
    snap.write_bytes(b"data")

    resolved = _resolve_snapshot_path(ledger_path, str(snap))

    assert resolved == snap.resolve()


def test_resolve_snapshot_path_rejects_empty(tmp_path: Path) -> None:
    ledger_path = tmp_path / "state" / "amendments.jsonl"
    ledger_path.parent.mkdir(parents=True)

    assert _resolve_snapshot_path(ledger_path, "") is None


# =============================================================================
# Non-healing listing
# =============================================================================


def test_list_absent_ledger_returns_empty_no_create(tmp_path: Path) -> None:
    ledger_path = tmp_path / "state" / "amendments.jsonl"
    ledger = ProposalLedger(ledger_path)
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()

    result = list_amendment_snapshots(ledger, memory_dir)

    assert result == []
    assert not ledger_path.exists()
    assert not ledger_path.parent.exists()


def test_list_never_calls_read_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)

    def boom(self: ProposalLedger) -> list[AmendmentProposal]:
        raise AssertionError("list_amendment_snapshots must never call read_all()")

    monkeypatch.setattr(ProposalLedger, "read_all", boom)

    result = list_amendment_snapshots(fixture["ledger"], fixture["memory_dir"])

    assert len(result) == 1
    assert result[0].eligible is True


def test_list_malformed_and_missing_id_rows_byte_identical(tmp_path: Path) -> None:
    ledger_path = tmp_path / "amendments.jsonl"
    malformed_line = '{"source": "memory_reflect", "proposed'
    idless = _raw_llm_record("Id-less row, no snapshot path, must be skipped.")
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    ledger_path.write_text(
        malformed_line + "\n" + json.dumps(idless, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    before_bytes = ledger_path.read_bytes()
    ledger = ProposalLedger(ledger_path)

    result = list_amendment_snapshots(ledger, memory_dir)

    assert result == []  # no snapshot path on either row
    assert ledger_path.read_bytes() == before_bytes  # byte-identical, no heal


def test_list_orders_newest_first_by_applied_created_id(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    ledger_path = tmp_path / "amendments.jsonl"
    rollback_dir = ledger_path.parent / "rollback"
    rollback_dir.mkdir(parents=True)
    rows = []
    for index, applied_at in enumerate(
        ["2026-06-01T00:00:00+00:00", "2026-06-03T00:00:00+00:00", "2026-06-02T00:00:00+00:00"]
    ):
        snap = rollback_dir / f"snap{index}.bak"
        snap.write_bytes(b"before")
        row = _raw_llm_record(f"Row {index}")
        row["id"] = str(uuid.uuid4())
        row["created_at"] = applied_at
        row["applied_at"] = applied_at
        row["status"] = "applied"
        row["before_hash"] = _sha256_bytes(b"before")
        row["after_hash"] = "deadbeef"
        row["rollback_snapshot_path"] = str(snap)
        rows.append(row)
    _write_raw_ledger(ledger_path, rows)
    ledger = ProposalLedger(ledger_path)

    result = list_amendment_snapshots(ledger, memory_dir)

    assert [snap.applied_at for snap in result] == [
        "2026-06-03T00:00:00+00:00",
        "2026-06-02T00:00:00+00:00",
        "2026-06-01T00:00:00+00:00",
    ]


def test_list_exact_id_filter(tmp_path: Path) -> None:
    fixture_a = _seed_applied_row(tmp_path, proposal_id=str(uuid.uuid4()))
    ledger = fixture_a["ledger"]
    memory_dir = fixture_a["memory_dir"]
    second_target = memory_dir / "USER.md"
    second_before = b"# USER\n"
    second_after = b"# USER\n\nmore\n"
    second_target.write_bytes(second_after)
    rollback_dir = ledger.path.parent / "rollback"
    second_snap = rollback_dir / "USER.md.snap2.bak"
    second_snap.write_bytes(second_before)
    second_id = str(uuid.uuid4())
    second_row = _raw_llm_record("Second row")
    second_row["id"] = second_id
    second_row["created_at"] = "2026-06-09T00:00:00+00:00"
    second_row["applied_at"] = "2026-06-09T00:00:00+00:00"
    second_row["status"] = "applied"
    second_row["target_file"] = "USER.md"
    second_row["before_hash"] = _sha256_bytes(second_before)
    second_row["after_hash"] = _sha256_bytes(second_after)
    second_row["rollback_snapshot_path"] = str(second_snap)
    existing_text = ledger.path.read_text(encoding="utf-8")
    existing_rows = [json.loads(line) for line in existing_text.splitlines()]
    _write_raw_ledger(ledger.path, existing_rows + [second_row])

    result = list_amendment_snapshots(ledger, memory_dir, proposal_id=second_id)

    assert len(result) == 1
    assert result[0].proposal_id == second_id


def test_list_duplicate_id_visible_but_ineligible(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    ledger_path = tmp_path / "amendments.jsonl"
    rollback_dir = ledger_path.parent / "rollback"
    rollback_dir.mkdir(parents=True)
    dup_id = str(uuid.uuid4())
    snap = rollback_dir / "dup.bak"
    snap.write_bytes(b"before")
    rows = []
    for _ in range(2):
        row = _raw_llm_record("Duplicate id row")
        row["id"] = dup_id
        row["created_at"] = "2026-06-09T00:00:00+00:00"
        row["applied_at"] = "2026-06-09T00:00:00+00:00"
        row["status"] = "applied"
        row["before_hash"] = _sha256_bytes(b"before")
        row["after_hash"] = "deadbeef"
        row["rollback_snapshot_path"] = str(snap)
        rows.append(row)
    _write_raw_ledger(ledger_path, rows)
    ledger = ProposalLedger(ledger_path)

    result = list_amendment_snapshots(ledger, memory_dir)

    assert len(result) == 2
    assert all(snap.proposal_id == dup_id for snap in result)
    assert all(snap.eligible is False for snap in result)
    assert all(snap.reason == "duplicate_proposal_id" for snap in result)


@pytest.mark.parametrize(
    "mutate,expected_reason",
    [
        (lambda f: f.__setitem__("status", "pending"), "proposal_not_applied"),
        (lambda f: f.__setitem__("before_hash", ""), "missing_apply_hashes"),
        (lambda f: f.__setitem__("after_hash", ""), "missing_apply_hashes"),
        (lambda f: f.__setitem__("target_file", "NOTALLOWED.md"), "target_not_allowed"),
        (lambda f: f.__setitem__("after_hash", "0" * 64), "target_hash_conflict"),
    ],
)
def test_list_row_level_refusal_reasons(tmp_path, mutate, expected_reason) -> None:
    fixture = _seed_applied_row(tmp_path)
    ledger_path = fixture["ledger"].path
    row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    mutate(row)
    _write_raw_ledger(ledger_path, [row])
    ledger = ProposalLedger(ledger_path)

    result = list_amendment_snapshots(ledger, fixture["memory_dir"])

    assert len(result) == 1
    assert result[0].eligible is False
    assert result[0].reason == expected_reason


def test_list_target_missing_reason(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    fixture["target_path"].unlink()

    result = list_amendment_snapshots(fixture["ledger"], fixture["memory_dir"])

    assert result[0].eligible is False
    assert result[0].reason == "target_missing"


def test_list_target_unreadable_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)
    _patch_read_bytes_raises_for(monkeypatch, fixture["target_path"].resolve())

    result = list_amendment_snapshots(fixture["ledger"], fixture["memory_dir"])

    assert result[0].eligible is False
    assert result[0].reason == "target_unreadable"


def test_list_snapshot_missing_reason(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    fixture["snapshot_path"].unlink()

    result = list_amendment_snapshots(fixture["ledger"], fixture["memory_dir"])

    assert result[0].eligible is False
    assert result[0].reason == "snapshot_missing"


def test_list_snapshot_unreadable_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)
    _patch_read_bytes_raises_for(monkeypatch, fixture["snapshot_path"].resolve())

    result = list_amendment_snapshots(fixture["ledger"], fixture["memory_dir"])

    assert result[0].eligible is False
    assert result[0].reason == "snapshot_unreadable"


def test_list_snapshot_hash_mismatch_reason(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    fixture["snapshot_path"].write_bytes(b"corrupted snapshot bytes")

    result = list_amendment_snapshots(fixture["ledger"], fixture["memory_dir"])

    assert result[0].eligible is False
    assert result[0].reason == "snapshot_hash_mismatch"


def test_list_eligible_row_has_no_reason(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)

    result = list_amendment_snapshots(fixture["ledger"], fixture["memory_dir"])

    assert len(result) == 1
    assert result[0].eligible is True
    assert result[0].reason is None


# =============================================================================
# Happy path
# =============================================================================


@pytest.mark.parametrize(
    "before_bytes,after_bytes",
    [
        (b"line one\r\nline two\r\n", b"line one\r\nline two\r\nAMENDED\r\n"),
        (b"line one\nline two\n", b"line one\nline two\nAMENDED\n"),
        (
            "héllo wörld — ünïcödé".encode(),
            "héllo wörld — ünïcödé AMENDED".encode(),
        ),
        (bytes(range(0, 200)), bytes(range(0, 200)) + b"\x00\x01\xff"),
    ],
    ids=["crlf", "lf", "non_ascii", "arbitrary_bytes"],
)
def test_rollback_restores_exact_bytes(
    tmp_path: Path, before_bytes: bytes, after_bytes: bytes
) -> None:
    fixture = _seed_applied_row(tmp_path, before_bytes=before_bytes, after_bytes=after_bytes)

    result = rollback_amendment(
        fixture["proposal_id"],
        "operator",
        "manual rollback test",
        ledger=fixture["ledger"],
        memory_dir=fixture["memory_dir"],
    )

    assert result.status == "rolled_back"
    assert result.reason == "rollback_completed"
    assert fixture["target_path"].read_bytes() == before_bytes
    assert result.rollback_before_hash == fixture["after_hash"]
    assert result.rollback_after_hash == fixture["before_hash"]
    assert Path(result.rollback_rescue_snapshot_path).read_bytes() == after_bytes
    stored = json.loads(fixture["ledger"].path.read_text(encoding="utf-8").splitlines()[0])
    assert stored["status"] == "rolled_back"
    assert stored["rollback_actor"] == "operator"
    assert stored["rollback_reason"] == "manual rollback test"
    assert stored["rollback_requested_at"]
    assert stored["rolled_back_at"]
    assert stored["rollback_before_hash"] == fixture["after_hash"]
    assert stored["rollback_after_hash"] == fixture["before_hash"]
    assert stored["rollback_rescue_snapshot_path"]


def test_rollback_happy_path_pending_precedes_target_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)
    observed: dict[str, Any] = {}
    real_atomic_write_bytes = rollback_module._atomic_write_bytes

    def spy_atomic_write_bytes(path: Path, data: bytes) -> None:
        if Path(path) == fixture["target_path"].resolve():
            # At the moment the TARGET write happens, the ledger row must
            # already be durably rollback_pending on disk.
            on_disk = json.loads(
                fixture["ledger"].path.read_text(encoding="utf-8").splitlines()[0]
            )
            observed["status_at_target_write"] = on_disk["status"]
            observed["target_bytes_before_write"] = fixture["target_path"].read_bytes()
        real_atomic_write_bytes(path, data)

    monkeypatch.setattr(rollback_module, "_atomic_write_bytes", spy_atomic_write_bytes)

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "prove ordering",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert observed["status_at_target_write"] == "rollback_pending"
    assert observed["target_bytes_before_write"] == fixture["after_bytes"]
    assert result.status == "rolled_back"


# =============================================================================
# Validation / state — one test per refusal/conflict reason code
# =============================================================================


def test_rollback_invalid_proposal_id_empty(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)

    result = rollback_amendment(
        "   ", "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "invalid_proposal_id"


@pytest.mark.parametrize("bad_actor", ["", "   ", "x" * 201])
def test_rollback_invalid_actor(tmp_path: Path, bad_actor: str) -> None:
    fixture = _seed_applied_row(tmp_path)

    result = rollback_amendment(
        fixture["proposal_id"], bad_actor, "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "invalid_actor"


@pytest.mark.parametrize("bad_reason", ["", "   ", "x" * 2001])
def test_rollback_invalid_reason(tmp_path: Path, bad_reason: str) -> None:
    fixture = _seed_applied_row(tmp_path)

    result = rollback_amendment(
        fixture["proposal_id"], "operator", bad_reason,
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "invalid_reason"


def test_rollback_proposal_not_found(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)

    result = rollback_amendment(
        str(uuid.uuid4()), "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "proposal_not_found"


def test_rollback_duplicate_id_refuses_zero_mutation(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    ledger_path = fixture["ledger"].path
    row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    _write_raw_ledger(ledger_path, [row, dict(row)])
    before = ledger_path.read_bytes()

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=ProposalLedger(ledger_path), memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "duplicate_proposal_id"
    assert ledger_path.read_bytes() == before
    assert fixture["target_path"].read_bytes() == fixture["after_bytes"]


def test_rollback_proposal_not_applied(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    ledger_path = fixture["ledger"].path
    row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    row["status"] = "pending"
    _write_raw_ledger(ledger_path, [row])

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=ProposalLedger(ledger_path), memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "proposal_not_applied"


def test_rollback_missing_apply_hashes(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    ledger_path = fixture["ledger"].path
    row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    row["before_hash"] = ""
    _write_raw_ledger(ledger_path, [row])

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=ProposalLedger(ledger_path), memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "missing_apply_hashes"


def test_rollback_target_not_allowed(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    ledger_path = fixture["ledger"].path
    row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    row["target_file"] = "NOTALLOWED.md"
    _write_raw_ledger(ledger_path, [row])

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=ProposalLedger(ledger_path), memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "target_not_allowed"


def test_rollback_target_missing(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    fixture["target_path"].unlink()

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "target_missing"


def test_rollback_target_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)
    _patch_read_bytes_raises_for(monkeypatch, fixture["target_path"].resolve())

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "target_unreadable"


def test_rollback_target_hash_conflict(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    fixture["target_path"].write_bytes(b"drifted content nobody expected")

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "conflict"
    assert result.reason == "target_hash_conflict"
    assert fixture["target_path"].read_bytes() == b"drifted content nobody expected"


def test_rollback_snapshot_missing(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    fixture["snapshot_path"].unlink()

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "snapshot_missing"


def test_rollback_snapshot_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)
    _patch_read_bytes_raises_for(monkeypatch, fixture["snapshot_path"].resolve())

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "snapshot_unreadable"


def test_rollback_snapshot_hash_mismatch(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    fixture["snapshot_path"].write_bytes(b"corrupted snapshot bytes")

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "snapshot_hash_mismatch"
    assert fixture["target_path"].read_bytes() == fixture["after_bytes"]


def test_rollback_snapshot_path_invalid(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    ledger_path = fixture["ledger"].path
    row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    row["rollback_snapshot_path"] = str(tmp_path / "outside_rollback_dir.bak")
    (tmp_path / "outside_rollback_dir.bak").write_bytes(fixture["before_bytes"])
    _write_raw_ledger(ledger_path, [row])

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=ProposalLedger(ledger_path), memory_dir=fixture["memory_dir"],
    )

    assert result.status == "refused"
    assert result.reason == "snapshot_path_invalid"


def test_all_refusal_and_conflict_reasons_are_covered_by_status(tmp_path: Path) -> None:
    """Sanity: every refusal/conflict test above used a reason from the spec set."""

    covered = {
        "invalid_proposal_id", "invalid_actor", "invalid_reason",
        "proposal_not_found", "duplicate_proposal_id", "proposal_not_applied",
        "missing_apply_hashes", "target_not_allowed", "target_missing",
        "target_unreadable", "target_hash_conflict", "snapshot_missing",
        "snapshot_unreadable", "snapshot_hash_mismatch", "snapshot_path_invalid",
    }
    assert covered <= (_REFUSAL_REASONS | _CONFLICT_REASONS)


# =============================================================================
# Failure injection
# =============================================================================


def test_rollback_rescue_snapshot_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)

    def raising_write(path: Path, data: bytes) -> None:
        raise OSError("simulated rescue write failure")

    monkeypatch.setattr(rollback_module, "_atomic_write_bytes", raising_write)

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "failed"
    assert result.reason == "rescue_snapshot_failed"
    assert result.reason in _FAILURE_REASONS
    stored = json.loads(fixture["ledger"].path.read_text(encoding="utf-8").splitlines()[0])
    assert stored["status"] == "applied"  # never transitioned
    assert fixture["target_path"].read_bytes() == fixture["after_bytes"]


def test_rollback_ledger_prepare_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)

    def raising_update(self: ProposalLedger, proposal_id: str, updates: dict) -> str:
        return "not_found"  # simulate a lost row without raising

    monkeypatch.setattr(ProposalLedger, "_update_record_unique", raising_update)

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "failed"
    assert result.reason == "ledger_prepare_failed"
    assert fixture["target_path"].read_bytes() == fixture["after_bytes"]


def test_rollback_target_restore_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)
    real_atomic_write_bytes = rollback_module._atomic_write_bytes

    def selective_failure(path: Path, data: bytes) -> None:
        if Path(path) == fixture["target_path"].resolve():
            raise OSError("simulated target restore failure")
        real_atomic_write_bytes(path, data)

    monkeypatch.setattr(rollback_module, "_atomic_write_bytes", selective_failure)

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "failed"
    assert result.reason == "target_restore_failed"
    stored = json.loads(fixture["ledger"].path.read_text(encoding="utf-8").splitlines()[0])
    assert stored["status"] == "rollback_pending"  # durable intent survives


def test_rollback_target_verify_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)
    real_atomic_write_bytes = rollback_module._atomic_write_bytes
    write_count = {"target": 0}

    def corrupt_second_target_write(path: Path, data: bytes) -> None:
        if Path(path) == fixture["target_path"].resolve():
            write_count["target"] += 1
            real_atomic_write_bytes(path, data + b"CORRUPTED")
            return
        real_atomic_write_bytes(path, data)

    monkeypatch.setattr(rollback_module, "_atomic_write_bytes", corrupt_second_target_write)

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "failed"
    assert result.reason == "target_verify_failed"
    assert write_count["target"] == 1  # unknown verification state is never rescued over
    assert fixture["target_path"].read_bytes().endswith(b"CORRUPTED")
    stored = json.loads(fixture["ledger"].path.read_text(encoding="utf-8").splitlines()[0])
    assert stored["status"] == "rollback_pending"


def test_rollback_ledger_finalize_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)
    real_update = ProposalLedger._update_record_unique
    call_count = {"n": 0}

    def fail_second_call(self: ProposalLedger, proposal_id: str, updates: dict) -> str:
        call_count["n"] += 1
        if call_count["n"] == 2:
            return "not_found"  # simulate the finalize update losing its row
        return real_update(self, proposal_id, updates)

    monkeypatch.setattr(ProposalLedger, "_update_record_unique", fail_second_call)

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reason",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "failed"
    assert result.reason == "ledger_finalize_failed"
    assert fixture["target_path"].read_bytes() == fixture["before_bytes"]  # restore DID happen
    assert result.rollback_before_hash == fixture["after_hash"]
    assert result.rollback_after_hash == fixture["before_hash"]


# =============================================================================
# Recovery
# =============================================================================


def _convert_to_pending(fixture: dict[str, Any]) -> dict[str, Any]:
    """Mutate a fresh applied-row fixture into rollback_pending + rescue snapshot."""

    ledger_path = fixture["ledger"].path
    rollback_dir = ledger_path.parent / "rollback"
    rescue_path = rollback_dir / "rescue.bin"
    rescue_path.write_bytes(fixture["after_bytes"])
    row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    row.update({
        "status": "rollback_pending",
        "rollback_actor": "operator",
        "rollback_reason": "prior attempt",
        "rollback_requested_at": "2026-06-09T00:00:00+00:00",
        "rollback_before_hash": fixture["after_hash"],
        "rollback_rescue_snapshot_path": str(rescue_path),
        "rollback_error": None,
    })
    _write_raw_ledger(ledger_path, [row])
    fixture["ledger"] = ProposalLedger(ledger_path)
    fixture["rescue_path"] = rescue_path
    return fixture


def test_recovery_pending_post_hash_restores(tmp_path: Path) -> None:
    fixture = _convert_to_pending(_seed_applied_row(tmp_path))
    # Target still holds the post-apply bytes (crash happened before restore).
    fixture["target_path"].write_bytes(fixture["after_bytes"])

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reconcile",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "rolled_back"
    assert result.reason == "rollback_reconciled_after_crash"
    assert fixture["target_path"].read_bytes() == fixture["before_bytes"]
    stored = json.loads(fixture["ledger"].path.read_text(encoding="utf-8").splitlines()[0])
    assert stored["status"] == "rolled_back"


def test_recovery_pending_pre_hash_finalizes_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _convert_to_pending(_seed_applied_row(tmp_path))
    # Target ALREADY restored (crash happened after write, before finalize).
    fixture["target_path"].write_bytes(fixture["before_bytes"])
    write_calls = []
    real_write = rollback_module._atomic_write_bytes

    def spy_write(path: Path, data: bytes) -> None:
        write_calls.append(Path(path))
        real_write(path, data)

    monkeypatch.setattr(rollback_module, "_atomic_write_bytes", spy_write)

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reconcile",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "rolled_back"
    assert result.reason == "rollback_reconciled_after_crash"
    assert write_calls == []  # zero writes — finalize only
    assert fixture["target_path"].read_bytes() == fixture["before_bytes"]


def test_recovery_pending_unknown_hash_conflicts_with_zero_mutation(tmp_path: Path) -> None:
    fixture = _convert_to_pending(_seed_applied_row(tmp_path))
    drift = b"neither before nor after bytes"
    fixture["target_path"].write_bytes(drift)
    ledger_before = fixture["ledger"].path.read_bytes()
    rescue = Path(
        json.loads(fixture["ledger"].path.read_text(encoding="utf-8").splitlines()[0])[
            "rollback_rescue_snapshot_path"
        ]
    )
    rescue_before = rescue.read_bytes()

    result = rollback_amendment(
        fixture["proposal_id"], "operator", "reconcile",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert result.status == "conflict"
    assert result.reason == "target_hash_conflict"
    assert fixture["target_path"].read_bytes() == drift
    assert fixture["ledger"].path.read_bytes() == ledger_before
    assert rescue.read_bytes() == rescue_before


def test_rollback_already_completed_zero_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_applied_row(tmp_path)
    first = rollback_amendment(
        fixture["proposal_id"], "operator", "first rollback",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )
    assert first.status == "rolled_back"

    write_calls = []
    real_write = rollback_module._atomic_write_bytes

    def spy_write(path: Path, data: bytes) -> None:
        write_calls.append(Path(path))
        real_write(path, data)

    ledger_before = fixture["ledger"].path.read_bytes()
    monkeypatch.setattr(rollback_module, "_atomic_write_bytes", spy_write)

    second = rollback_amendment(
        fixture["proposal_id"], "operator", "second rollback",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert second.status == "rolled_back"
    assert second.reason == "rollback_already_completed"
    assert write_calls == []
    assert fixture["ledger"].path.read_bytes() == ledger_before  # zero ledger writes


def test_rollback_already_completed_drift_conflicts(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    first = rollback_amendment(
        fixture["proposal_id"], "operator", "first rollback",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )
    assert first.status == "rolled_back"
    fixture["target_path"].write_bytes(b"drifted after rollback completed")

    second = rollback_amendment(
        fixture["proposal_id"], "operator", "second rollback",
        ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
    )

    assert second.status == "conflict"
    assert second.reason == "target_hash_conflict"


def test_rollback_lock_timeout(tmp_path: Path) -> None:
    fixture = _seed_applied_row(tmp_path)
    lock_file = fixture["target_path"].resolve().with_suffix(
        fixture["target_path"].suffix + ".lock"
    )
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_file, "w", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        import cognition.amendments as amendments_module

        original_timeout = amendments_module._LEDGER_LOCK_TIMEOUT_S
        amendments_module._LEDGER_LOCK_TIMEOUT_S = 0.3
        try:
            result = rollback_amendment(
                fixture["proposal_id"], "operator", "reason",
                ledger=fixture["ledger"], memory_dir=fixture["memory_dir"],
            )
        finally:
            amendments_module._LEDGER_LOCK_TIMEOUT_S = original_timeout
    finally:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    assert result.status == "failed"
    assert result.reason == "lock_timeout"
