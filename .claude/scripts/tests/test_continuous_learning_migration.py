"""Split learning-store reconciliation preserves history and never copies claims."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from personas.learning.migration import inventory_learning_store, reconcile_learning_stores
from personas.learning.models import LearningError, LearningTarget
from personas.learning.queue import LearningQueue
from personas.learning.store import LearningStore


def target(path, persona="default"):
    return LearningTarget(persona, path / "memory", path / "data", path / "state", path / "skills")


def record(t, key, content="observed"):
    return LearningStore(t).put("experience", {"content": content}, key=key)


def test_inventory_missing_is_strictly_readonly(tmp_path):
    t = target(tmp_path / "missing")
    assert inventory_learning_store(t)["records"] == 0
    assert not t.data_dir.exists()


def test_disjoint_records_events_evidence_queue_backed_up_and_idempotent(tmp_path):
    dst, src = target(tmp_path / "dst"), target(tmp_path / "src")
    original = record(dst, "existing")
    fresh = record(src, "new")
    source_store = LearningStore(src)
    event = source_store.event(fresh["id"], "status", {"status": "complete"}, key="end")
    evidence = source_store.directory / "evidence/chart.json"
    evidence.parent.mkdir()
    evidence.write_text('{"close":100}')
    source_queue = LearningQueue(SimpleNamespace(target=src))
    job = source_queue.enqueue(
        "experience", fresh["id"], payload={"experience_id": fresh["id"]}, now=1
    )
    before = inventory_learning_store(src)
    plan = reconcile_learning_stores(dst, [src])
    assert plan["added"]["records"] == 1
    assert LearningStore(dst).get(fresh["id"]) is None
    result = reconcile_learning_stores(
        dst, [src], backup_dir=tmp_path / "backups", apply=True, quiesced=True
    )
    assert result["applied"] and len(result["backups"]) == 2
    assert all((Path(path) / "learning.db").exists() for path in result["backups"])
    assert LearningStore(dst).get(original["id"]) is not None
    assert LearningStore(dst).events(fresh["id"])[0]["id"] == event["id"]
    assert (
        LearningStore(dst).directory / "evidence/chart.json"
    ).read_bytes() == evidence.read_bytes()
    dest_queue = LearningQueue(SimpleNamespace(target=dst))
    claimed = dest_queue.claim()
    assert claimed["id"] == job["id"]
    dest_queue.finish_stage(claimed, status="completed")
    again = reconcile_learning_stores(
        dst, [src], backup_dir=tmp_path / "backups", apply=True, quiesced=True
    )
    assert again["added"] == {"records": 0, "events": 0, "settings": 0, "jobs": 0, "files": 0}
    assert dest_queue.list(include_finished=True)[0]["status"] == "completed"
    assert inventory_learning_store(src) == before


def test_conflicting_immutable_id_refuses_before_target_or_backup_mutation(tmp_path):
    dst, src = target(tmp_path / "dst"), target(tmp_path / "src")
    original = record(dst, "same", "first")
    record(src, "same", "different")
    before = LearningStore(dst).path.read_bytes()
    with pytest.raises(LearningError, match="conflicting learning records id"):
        reconcile_learning_stores(
            dst, [src], backup_dir=tmp_path / "backup", apply=True, quiesced=True
        )
    assert LearningStore(dst).path.read_bytes() == before
    assert LearningStore(dst).get(original["id"])["content"] == "first"
    assert not (tmp_path / "backup").exists()


def test_same_record_imported_once_preserves_id(tmp_path):
    src, dst = target(tmp_path / "src"), target(tmp_path / "dst")
    row = record(src, "same")
    reconcile_learning_stores(dst, [src], backup_dir=tmp_path / "backup", apply=True, quiesced=True)
    assert reconcile_learning_stores(dst, [src])["added"]["records"] == 0
    assert LearningStore(dst).get(row["id"])["created_at"] == row["created_at"]


def test_expired_queue_claim_resumes_checkpoint_without_live_tokens(tmp_path):
    src, dst = target(tmp_path / "src"), target(tmp_path / "dst")
    row = record(src, "old")
    store = LearningStore(src)
    token = store.claim(row["id"], "test", ttl_seconds=30)
    queue = LearningQueue(SimpleNamespace(target=src))
    queue.enqueue("experience", row["id"], payload={"experience_id": row["id"]}, now=1)
    queue.claim(now=1, ttl_seconds=1)
    with pytest.raises(LearningError, match="active claims"):
        reconcile_learning_stores(
            dst, [src], backup_dir=tmp_path / "backup", apply=True, quiesced=True
        )
    store.release_claim(row["id"], "test", token)
    reconcile_learning_stores(dst, [src], backup_dir=tmp_path / "backup", apply=True, quiesced=True)
    job = LearningQueue(SimpleNamespace(target=dst)).list()[0]
    assert job["status"] == "queued" and job["token"] is None and job["expires_at"] is None
    with sqlite3.connect(LearningStore(dst).path) as db:
        assert db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_cross_persona_and_conflicting_evidence_refused(tmp_path):
    src, dst = target(tmp_path / "src"), target(tmp_path / "dst", "crypto")
    with pytest.raises(LearningError, match="different learning personas"):
        reconcile_learning_stores(dst, [src])
    dst = target(tmp_path / "dst")
    for t, content in [(src, b"first"), (dst, b"other")]:
        record(t, str(t.data_dir))
        (LearningStore(t).directory / "evidence.json").write_bytes(content)
    with pytest.raises(LearningError, match="conflicting learning evidence"):
        reconcile_learning_stores(dst, [src])


def test_old_source_transition_cannot_overwrite_newer_canonical_projection(tmp_path):
    src, dst = target(tmp_path / "src"), target(tmp_path / "dst")
    row = record(src, "same")
    reconcile_learning_stores(dst, [src], backup_dir=tmp_path / "backup", apply=True, quiesced=True)
    old = LearningStore(src).event(row["id"], "status", {"status": "old"}, key="old")
    new = LearningStore(dst).event(row["id"], "status", {"status": "new"}, key="new")
    assert new["created_at"] >= old["created_at"]
    with pytest.raises(LearningError, match="conflicting learning event order"):
        reconcile_learning_stores(dst, [src])


def test_legacy_v1_source_stays_readonly_and_destination_uses_current_schema(tmp_path):
    from personas.learning.store import SCHEMA_VERSION

    src, dst = target(tmp_path / "src"), target(tmp_path / "dst")
    record(src, "legacy")
    with sqlite3.connect(LearningStore(src).path) as db:
        db.execute("PRAGMA user_version=1")
    reconcile_learning_stores(dst, [src], backup_dir=tmp_path / "backup", apply=True, quiesced=True)
    assert inventory_learning_store(src)["schema_version"] == 1
    assert inventory_learning_store(dst)["schema_version"] == SCHEMA_VERSION


def test_orphan_evidence_reference_is_refused_before_destination_creation(tmp_path):
    src, dst = target(tmp_path / "src"), target(tmp_path / "dst")
    LearningStore(src).put("candidate", {"evidence_ids": ["missing-record"]}, key="bad")
    with pytest.raises(LearningError, match="orphan record reference"):
        reconcile_learning_stores(
            dst, [src], backup_dir=tmp_path / "backup", apply=True, quiesced=True
        )
    assert not dst.data_dir.exists()


def test_source_writer_during_backup_is_detected_before_target_changes(tmp_path, monkeypatch):
    from personas.learning import migration

    src, dst = target(tmp_path / "src"), target(tmp_path / "dst")
    record(src, "first")
    evidence = LearningStore(src).directory / "evidence.json"
    evidence.write_text("{}")
    copy = migration.shutil.copy2

    def concurrent_writer(source, destination):
        result = copy(source, destination)
        record(src, "arrived-during-backup")
        return result

    monkeypatch.setattr(migration.shutil, "copy2", concurrent_writer)
    with pytest.raises(LearningError, match="source changed during backup"):
        reconcile_learning_stores(
            dst, [src], backup_dir=tmp_path / "backup", apply=True, quiesced=True
        )
    assert not dst.data_dir.exists()
