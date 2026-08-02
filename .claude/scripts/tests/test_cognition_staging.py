"""Tests for cognition.staging — JSONL staging store."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cognition import staging as staging_mod
from cognition.staging import StagingCandidate, StagingStore


def test_append_and_read(tmp_path: Path):
    """Write candidate -> read it back."""
    store = StagingStore(tmp_path / "staging.jsonl")
    candidate = StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="The server is running on port 7888",
        dedupe_key="server running port 7888",
        promotion_target="MEMORY.md",
    )

    assert store.append(candidate) is True
    results = store.read_recent(hours=1)
    assert len(results) == 1
    assert results[0].observation == "The server is running on port 7888"
    assert results[0].candidate_type == "fact"


def test_dedup_exact_key(tmp_path: Path):
    """Same dedupe_key -> merged into the existing record (evidence_count++), not a new row."""
    store = StagingStore(tmp_path / "staging.jsonl")
    c1 = StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="Fact A",
        dedupe_key="fact-a",
        promotion_target="MEMORY.md",
    )
    c2 = StagingCandidate(
        source_turn="test:2",
        candidate_type="fact",
        observation="Fact A again",
        dedupe_key="fact-a",  # Same key
        promotion_target="MEMORY.md",
    )

    assert store.append(c1) is True
    assert store.append(c2) is False  # merged, not a new row
    assert store.count() == 1

    merged = store.read_unpromoted()
    assert len(merged) == 1
    assert merged[0].id == c1.id
    assert merged[0].evidence_count == 2


def test_empty_dedupe_key_rejected(tmp_path: Path):
    """Empty dedupe_key -> rejected."""
    store = StagingStore(tmp_path / "staging.jsonl")
    c = StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="Something",
        dedupe_key="",
        promotion_target="MEMORY.md",
    )
    assert store.append(c) is False


def test_count(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    assert store.count() == 0

    for i in range(3):
        store.append(StagingCandidate(
            source_turn=f"test:{i}",
            candidate_type="fact",
            observation=f"Fact {i}",
            dedupe_key=f"fact-{i}",
            promotion_target="MEMORY.md",
        ))
    assert store.count() == 3


def test_cleanup_expired(tmp_path: Path):
    """Old entries removed."""
    store = StagingStore(tmp_path / "staging.jsonl")

    # Write an already-expired entry
    expired = StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="Old fact",
        dedupe_key="old-fact",
        promotion_target="MEMORY.md",
    )
    expired.decay_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    # Write directly since append would set decay_at fresh
    import json
    from dataclasses import asdict

    with open(tmp_path / "staging.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(expired)) + "\n")

    # Write a fresh entry
    store.append(StagingCandidate(
        source_turn="test:2",
        candidate_type="fact",
        observation="Fresh fact",
        dedupe_key="fresh-fact",
        promotion_target="MEMORY.md",
    ))

    assert store.count() == 2
    removed = store.cleanup_expired()
    assert removed == 1
    assert store.count() == 1


def test_read_recent_filter(tmp_path: Path):
    """Only returns candidates within time window."""
    store = StagingStore(tmp_path / "staging.jsonl")
    store.append(StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="Recent fact",
        dedupe_key="recent",
        promotion_target="MEMORY.md",
    ))

    # Should find it within 1 hour
    assert len(store.read_recent(hours=1)) == 1


def test_nonexistent_file(tmp_path: Path):
    """Store handles missing file gracefully."""
    store = StagingStore(tmp_path / "nonexistent.jsonl")
    assert store.count() == 0
    assert store.read_recent() == []


def test_dedup_merge_keeps_max_confidence(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    c1 = StagingCandidate(
        source_turn="t:1", candidate_type="fact", observation="A",
        dedupe_key="k", promotion_target="MEMORY.md", confidence=0.6,
    )
    c2 = StagingCandidate(
        source_turn="t:2", candidate_type="fact", observation="A again",
        dedupe_key="k", promotion_target="MEMORY.md", confidence=0.8,
    )
    store.append(c1)
    store.append(c2)

    merged = store.read_unpromoted()[0]
    assert merged.confidence == 0.8

    # Lower second observation must NOT drag confidence down
    store2 = StagingStore(tmp_path / "staging2.jsonl")
    store2.append(StagingCandidate(
        source_turn="t:1", candidate_type="fact", observation="B",
        dedupe_key="k2", promotion_target="MEMORY.md", confidence=0.9,
    ))
    store2.append(StagingCandidate(
        source_turn="t:2", candidate_type="fact", observation="B again",
        dedupe_key="k2", promotion_target="MEMORY.md", confidence=0.5,
    ))
    assert store2.read_unpromoted()[0].confidence == 0.9


def test_dedup_merge_three_observations(tmp_path: Path):
    """Three repeats of the same dedupe_key -> evidence_count == 3."""
    store = StagingStore(tmp_path / "staging.jsonl")
    for i in range(3):
        store.append(StagingCandidate(
            source_turn=f"t:{i}", candidate_type="fact", observation=f"obs {i}",
            dedupe_key="same-key", promotion_target="MEMORY.md",
        ))
    assert store.count() == 1
    assert store.read_unpromoted()[0].evidence_count == 3


def test_concurrent_merge_no_lost_updates(tmp_path: Path):
    """N threads append() the SAME dedupe_key concurrently.

    If the lock did not serialize the read-check-write, threads would read a
    stale record before writing back and the final evidence_count would be
    < N (lost updates). Asserting == N proves the lock is load-bearing.
    """
    store = StagingStore(tmp_path / "staging.jsonl")
    store.append(StagingCandidate(
        source_turn="seed", candidate_type="fact", observation="seed obs",
        dedupe_key="hot-key", promotion_target="MEMORY.md",
    ))

    n_threads = 20
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker(i: int):
        try:
            barrier.wait()
            store.append(StagingCandidate(
                source_turn=f"t:{i}", candidate_type="fact", observation=f"obs {i}",
                dedupe_key="hot-key", promotion_target="MEMORY.md",
            ))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker errors: {errors!r}"
    assert store.count() == 1
    assert store.read_unpromoted()[0].evidence_count == n_threads + 1


def test_concurrent_merge_requires_lock(tmp_path: Path, monkeypatch):
    """Lock-spy: with the lock NEUTERED, a forced stale-snapshot interleave
    deterministically loses an update (no probabilistic contention — the old
    20-thread version could serialize under the GIL and false-fail, and a
    crashed worker could make it pass for the wrong reason).
    """
    import contextlib

    @contextlib.contextmanager
    def _noop_lock(_path):
        yield

    monkeypatch.setattr(staging_mod, "_file_lock", _noop_lock, raising=False)

    store = StagingStore(tmp_path / "staging.jsonl")
    store.append(StagingCandidate(
        source_turn="seed", candidate_type="fact", observation="seed obs",
        dedupe_key="hot-key", promotion_target="MEMORY.md",
    ))

    orig_iter = StagingStore._iter_records
    read_taken = threading.Event()
    release_write = threading.Event()

    def gated_iter(self):
        records = orig_iter(self)
        if threading.current_thread().name == "racer-a":
            read_taken.set()
            assert release_write.wait(timeout=5), "gate never released"
        return records

    monkeypatch.setattr(StagingStore, "_iter_records", gated_iter)

    errors: list[BaseException] = []

    def racer_a():
        try:
            store.append(StagingCandidate(
                source_turn="t:a", candidate_type="fact", observation="obs a",
                dedupe_key="hot-key", promotion_target="MEMORY.md",
            ))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=racer_a, name="racer-a")
    t.start()
    assert read_taken.wait(timeout=5), "racer-a never read"
    # While racer-a holds its stale snapshot (evidence_count=1), land a full
    # merge from this thread (ungated) — evidence_count becomes 2 on disk.
    store.append(StagingCandidate(
        source_turn="t:b", candidate_type="fact", observation="obs b",
        dedupe_key="hot-key", promotion_target="MEMORY.md",
    ))
    release_write.set()
    t.join(timeout=5)

    assert not errors, f"racer errors: {errors!r}"
    # racer-a's write was based on the stale snapshot: it clobbers b's merge,
    # leaving 2 (seed + a) instead of 3 — a deterministic lost update.
    assert store.read_unpromoted()[0].evidence_count == 2


def test_update_record_requires_lock_against_append(tmp_path: Path, monkeypatch):
    """Lock-neutered counter-test for the append-vs-_update_record race: with
    the lock removed and the promoter frozen on its stale snapshot, a row
    appended mid-flight is deterministically erased by the snapshot rewrite —
    proving the shared lock is load-bearing for #166 Finding 2.
    """
    import contextlib

    @contextlib.contextmanager
    def _noop_lock(_path):
        yield

    monkeypatch.setattr(staging_mod, "_file_lock", _noop_lock, raising=False)

    store = StagingStore(tmp_path / "staging.jsonl")
    pre_existing = StagingCandidate(
        source_turn="pre", candidate_type="fact", observation="pre-existing",
        dedupe_key="pre-existing-key", promotion_target="MEMORY.md",
    )
    store.append(pre_existing)

    orig_iter = StagingStore._iter_records
    read_taken = threading.Event()
    release_write = threading.Event()

    def gated_iter(self):
        records = orig_iter(self)
        if threading.current_thread().name == "promoter":
            read_taken.set()
            assert release_write.wait(timeout=5), "gate never released"
        return records

    monkeypatch.setattr(StagingStore, "_iter_records", gated_iter)

    errors: list[BaseException] = []

    def promoter():
        try:
            store.mark_promoted(pre_existing.id, "MEMORY.md")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=promoter, name="promoter")
    t.start()
    assert read_taken.wait(timeout=5), "promoter never read"
    # Land a live-capture append while the promoter holds its stale snapshot.
    store.append(StagingCandidate(
        source_turn="live:0", candidate_type="fact", observation="live capture",
        dedupe_key="live-0", promotion_target="MEMORY.md",
    ))
    release_write.set()
    t.join(timeout=5)

    assert not errors, f"promoter errors: {errors!r}"
    # The unlocked snapshot rewrite erased the appended row: only the
    # pre-existing record survives. With the real lock this is count == 2
    # (proven by test_concurrent_append_survives_update_record).
    assert store.count() == 1


def test_concurrent_append_survives_update_record(tmp_path: Path):
    """One thread appends N new candidates while another concurrently marks
    a pre-existing candidate promoted (_update_record). Without the shared
    lock, _update_record's stale-snapshot rewrite can silently drop rows
    appended mid-flight (#166 Finding 2).
    """
    store = StagingStore(tmp_path / "staging.jsonl")
    pre_existing = StagingCandidate(
        source_turn="pre", candidate_type="fact", observation="pre-existing",
        dedupe_key="pre-existing-key", promotion_target="MEMORY.md",
    )
    store.append(pre_existing)

    n_appends = 30
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def appender():
        try:
            barrier.wait()
            for i in range(n_appends):
                store.append(StagingCandidate(
                    source_turn=f"live:{i}", candidate_type="fact",
                    observation=f"live capture {i}", dedupe_key=f"live-{i}",
                    promotion_target="MEMORY.md",
                ))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def promoter():
        try:
            barrier.wait()
            for _ in range(n_appends):
                store.mark_promoted(pre_existing.id, "MEMORY.md")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=appender)
    t2 = threading.Thread(target=promoter)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"worker errors: {errors!r}"
    # 1 pre-existing + n_appends distinct live rows, none lost.
    assert store.count() == n_appends + 1

    import json
    records = [
        json.loads(line)
        for line in (tmp_path / "staging.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pre = next(r for r in records if r["id"] == pre_existing.id)
    assert pre["promoted"] is True


def test_unreject_low_evidence_migration(tmp_path: Path):
    """Legacy rows rejected as low_evidence (pre-#166) flip back to pending."""
    import json
    from dataclasses import asdict

    path = tmp_path / "staging.jsonl"
    legacy = StagingCandidate(
        source_turn="test:1", candidate_type="fact", observation="stuck fact",
        dedupe_key="stuck", promotion_target="MEMORY.md",
        evidence_count=1, rejected=True, rejected_reason="low_evidence (1 < 2)",
    )
    still_rejected = StagingCandidate(
        source_turn="test:2", candidate_type="fact", observation="bad fact",
        dedupe_key="bad", promotion_target="MEMORY.md",
        confidence=0.1, rejected=True, rejected_reason="low_confidence (0.10 < 0.70)",
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(legacy)) + "\n")
        f.write(json.dumps(asdict(still_rejected)) + "\n")

    store = StagingStore(path)
    assert store.read_unpromoted() == []  # both look rejected pre-migration

    flipped = store.unreject_low_evidence()
    assert flipped == 1

    unpromoted = store.read_unpromoted()
    assert len(unpromoted) == 1
    assert unpromoted[0].dedupe_key == "stuck"

    # Idempotent — second call flips nothing.
    assert store.unreject_low_evidence() == 0


def test_concurrent_read_survives_locked_rewrite(tmp_path: Path):
    """Unlocked-looking read methods must not observe a torn file mid-rewrite.

    One thread repeatedly forces a real _write_all() rewrite (mark_promoted /
    _update_record, as memory_reflect.py's cron pass does) while another
    thread hammers count() (as engine.py's hot path and the cron pass do).
    Every observed count must equal N — never a torn intermediate value.
    """
    store = StagingStore(tmp_path / "staging.jsonl")
    n = 50
    ids = []
    for i in range(n):
        c = StagingCandidate(
            source_turn=f"t:{i}", candidate_type="fact", observation=f"obs {i}",
            dedupe_key=f"key-{i}", promotion_target="MEMORY.md",
        )
        store.append(c)
        ids.append(c.id)

    stop = threading.Event()
    bad_counts: list[int] = []

    def writer():
        while not stop.is_set():
            store._update_record(ids[0], {"promoted": True})
            store._update_record(ids[0], {"promoted": False})

    def reader():
        for _ in range(500):
            observed = store.count()
            if observed != n:
                bad_counts.append(observed)

    t_w = threading.Thread(target=writer)
    t_r = threading.Thread(target=reader)
    t_w.start()
    t_r.start()
    t_r.join()
    stop.set()
    t_w.join()

    assert not bad_counts, f"torn reads observed: {bad_counts[:10]} (of {len(bad_counts)})"


def test_locked_fail_open_when_file_lock_unavailable(tmp_path: Path, monkeypatch):
    """_locked()'s is-None fallback still produces correct (if unlocked) behavior."""
    monkeypatch.setattr(staging_mod, "_file_lock", None)

    store = StagingStore(tmp_path / "staging.jsonl")
    c1 = StagingCandidate(
        source_turn="t:1", candidate_type="fact", observation="A",
        dedupe_key="k", promotion_target="MEMORY.md",
    )
    c2 = StagingCandidate(
        source_turn="t:2", candidate_type="fact", observation="A again",
        dedupe_key="k", promotion_target="MEMORY.md",
    )
    assert store.append(c1) is True
    assert store.append(c2) is False  # merge still works without a lock
    assert store.read_unpromoted()[0].evidence_count == 2


def test_merge_refreshes_decay_at(tmp_path: Path):
    """An actively re-observed candidate earns a fresh expiry — without the
    refresh, cleanup_expired() can purge a slow accumulator mid-climb
    (Codex gate MAJOR on PR #176)."""
    import json as _json

    path = tmp_path / "staging.jsonl"
    store = StagingStore(path)
    store.append(StagingCandidate(
        source_turn="t:1", candidate_type="fact", observation="slow burner",
        dedupe_key="slow-key", promotion_target="MEMORY.md",
    ))
    # Backdate the stored decay_at to simulate a near-expiry record.
    rows = [_json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["decay_at"] = "2020-01-01T00:00:00"
    path.write_text(
        "".join(_json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )

    fresh = StagingCandidate(
        source_turn="t:2", candidate_type="fact", observation="slow burner",
        dedupe_key="slow-key", promotion_target="MEMORY.md",
    )
    store.append(fresh)

    merged = [_json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()][0]
    assert merged["evidence_count"] == 2
    assert merged["decay_at"] == fresh.decay_at
    assert merged["decay_at"] > "2020-01-01T00:00:00"
