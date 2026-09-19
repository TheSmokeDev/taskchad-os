"""Physical source identity and cross-representation belief lifecycle proofs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cognition import operator_beliefs
from cognition.belief_conflicts import _decide_loser
from cognition.self_model import InferenceRecord, InferenceTracker
from session import PostgresSessionStore, Session, SQLiteSessionStore, read_operator_user_turns

from personas.learning.legacy_beliefs import legacy_understanding_current, sync_legacy_beliefs
from personas.learning.models import LearningTarget
from personas.learning.service import LearningService


@pytest.fixture(autouse=True)
def exact_beliefs(monkeypatch):
    monkeypatch.setattr(
        InferenceTracker,
        "_find_similar_active",
        staticmethod(lambda text, rows: next((r for r in rows if r.inference == text), None)),
    )


def _source(ref="message-1", revision="v1"):
    return {"ref": ref, "revision": revision}


def _service(tmp_path):
    return LearningService(
        LearningTarget(
            "default",
            tmp_path / "memory",
            tmp_path / "data",
            tmp_path / "state",
            tmp_path / "skills",
        )
    )


def test_sqlite_physical_ids_survive_overlapping_windows_and_correction(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    now = datetime.now()
    store.create(Session("cli:test:1", "runtime", "cli", "test", "1", "operator", now, now))
    store.add_message("cli:test:1", "user", "I prefer short answers", now)
    store.add_message("cli:test:1", "user", "I prefer short answers", now)
    first = read_operator_user_turns(now - timedelta(days=1), store=store, include_provenance=True)
    again = read_operator_user_turns(now - timedelta(days=2), store=store, include_provenance=True)
    assert first == again
    assert first[0]["message_id"] != first[1]["message_id"]
    assert first[0]["source_ref"] != first[1]["source_ref"]
    assert first[0]["source_revision"] == first[1]["source_revision"]
    with store._connect() as conn:
        conn.execute(
            "UPDATE chat_messages SET content=? WHERE id=?",
            ("I prefer detailed answers", first[0]["message_id"]),
        )
    revised = read_operator_user_turns(
        now - timedelta(days=1), store=store, include_provenance=True
    )
    assert revised[0]["source_ref"] == first[0]["source_ref"]
    assert revised[0]["source_revision"] != first[0]["source_revision"]


def test_postgres_uses_physical_ids_and_content_revisions(monkeypatch):
    now = datetime.now(UTC)
    physical = [(71, "s", "user", "I prefer short answers", now, "[]")]
    cursor = SimpleNamespace(execute=lambda *_: None, fetchall=lambda: physical)
    store = object.__new__(PostgresSessionStore)
    store._conn = SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(
        store, "list_active", lambda **_: [SimpleNamespace(session_id="s", updated_at=now)]
    )
    first = read_operator_user_turns(now - timedelta(days=1), store=store, include_provenance=True)
    assert first[0]["message_id"] == 71
    assert first[0]["source_ref"] == "chat-message:s:71"
    assert first[0]["source_backend"] == "postgres"
    assert store.list_messages("s")[0].source_ref == first[0]["source_ref"]
    assert store.list_messages("s")[0].source_revision == first[0]["source_revision"]
    physical[0] = (71, "s", "user", "I prefer detailed answers", now, "[]")
    revised = read_operator_user_turns(
        now - timedelta(days=2), store=store, include_provenance=True
    )
    assert first[0]["source_ref"] == revised[0]["source_ref"]
    assert first[0]["source_revision"] != revised[0]["source_revision"]


def test_extraction_rejects_fabricated_ids_and_overlapping_windows_do_not_reinforce(tmp_path):
    turns = [
        {"text": "I prefer concise replies", "source_ref": "one", "source_revision": "v1"},
        {"text": "I prefer concise replies", "source_ref": "two", "source_revision": "v1"},
    ]

    async def extract(_context, _instruction, **_):
        return SimpleNamespace(
            parsed=[
                {
                    "claim": "operator prefers concise replies",
                    "kind": "explicit",
                    "confidence": 0.5,
                    "source_refs": ["one", "fabricated", "one"],
                }
            ]
        )

    path = tmp_path / "inferences.json"
    for window in (turns[:1], turns, turns[:1]):
        claims = asyncio.run(
            operator_beliefs.extract_operator_beliefs(window, tmp_path, reasoning=extract)
        )
        assert claims[0]["source_evidence"] == [_source("one")]
        asyncio.run(operator_beliefs.apply_operator_beliefs(claims, path, write_time_enabled=False))
    record = InferenceTracker(path).load()[0]
    assert record.known_evidence_count == record.evidence_count == 1
    assert record.confidence == 0.5
    assert record.status == "active"


def test_same_wording_distinct_originals_reinforces_but_revision_does_not(tmp_path):
    tracker = InferenceTracker(tmp_path / "inferences.json")
    first = tracker.add_inference(
        "concise", "operator text", 0.5, source="explicit", source_evidence=[_source()]
    )
    again = tracker.add_inference(
        "concise", "operator text", 0.5, source="explicit", source_evidence=[_source()]
    )
    assert again.confidence == first.confidence
    second = tracker.add_inference(
        "concise", "operator text", 0.5, source="explicit", source_evidence=[_source("message-2")]
    )
    assert second.confidence > first.confidence
    revised = tracker.add_inference(
        "concise",
        "corrected text",
        0.5,
        source="explicit",
        source_evidence=[_source("message-2", "v2")],
    )
    assert revised.confidence == second.confidence
    assert revised.known_evidence_count == 2


def test_message_correction_retires_old_claim_without_creating_independent_support(tmp_path):
    tracker = InferenceTracker(tmp_path / "inferences.json")
    old = tracker.add_inference(
        "prefers brief", "brief", 0.5, source="explicit", source_evidence=[_source()]
    )
    new = tracker.add_inference(
        "prefers detail", "detail", 0.5, source="explicit", source_evidence=[_source(revision="v2")]
    )
    assert new.confidence == 0.5
    assert [r.id for r in tracker.get_active()] == [new.id]
    assert next(r for r in tracker.load() if r.id == old.id).status == "superseded"


def test_embedding_neighbor_does_not_hide_corrected_claim(tmp_path, monkeypatch):
    tracker = InferenceTracker(tmp_path / "inferences.json")
    old = tracker.add_inference(
        "prefers brief", "brief", 0.5, source="explicit", source_evidence=[_source()]
    )
    monkeypatch.setattr(
        InferenceTracker,
        "_find_similar_active",
        staticmethod(lambda _text, rows: rows[0] if rows else None),
    )
    new = tracker.add_inference(
        "prefers detailed",
        "detail",
        0.5,
        source="explicit",
        source_evidence=[_source(revision="v2")],
    )
    assert old.id != new.id
    assert [row.inference for row in tracker.get_active()] == ["prefers detailed"]
    assert new.confidence == 0.5


def test_missing_source_claims_cannot_strengthen_or_confirm(tmp_path):
    path = tmp_path / "inferences.json"
    for _ in range(4):
        asyncio.run(
            operator_beliefs.apply_operator_beliefs(
                [{"claim": "operator likes clean designs", "confidence": 0.4}],
                path,
                write_time_enabled=False,
            )
        )
    row = InferenceTracker(path).load()[0]
    assert row.confidence == 0.4
    assert row.known_evidence_count == 0
    assert row.historical_evidence_count is None
    assert row.status == "active"


def test_legacy_add_api_without_sources_cannot_manufacture_confirmation(tmp_path):
    tracker = InferenceTracker(tmp_path / "inferences.json")
    for _ in range(4):
        record = tracker.add_inference("concise", "same observation", 0.4)
    assert record.confidence == 0.4
    assert record.evidence_count == 0
    assert record.historical_evidence_count is None
    assert record.status == "active"


def test_first_source_link_does_not_retroactively_reinforce_unknown_history(tmp_path):
    tracker = InferenceTracker(tmp_path / "inferences.json")
    tracker.save(
        [InferenceRecord("old", "concise", "unknown", 0.6, evidence_count=18, source="reflection")]
    )
    linked = tracker.add_inference(
        "concise", "message", 0.6, source="reflection", source_evidence=[_source()]
    )
    assert linked.confidence == 0.6
    assert linked.evidence_count == 18
    assert linked.historical_evidence_count is None
    assert linked.known_evidence_count == 1
    fresh = tracker.add_inference(
        "concise", "new message", 0.6, source="reflection", source_evidence=[_source("message-2")]
    )
    assert fresh.confidence > linked.confidence
    assert fresh.evidence_count == 18
    assert fresh.known_evidence_count == 2


def test_import_is_additive_unknown_counts_remain_unknown_and_links_are_idempotent(tmp_path):
    service = _service(tmp_path)
    path = service.target.state_dir / "self-model-inferences.json"
    tracker = InferenceTracker(path)
    legacy = InferenceRecord(
        "old", "prefers concise", "missing source", 0.8, evidence_count=37, source="reflection"
    )
    tracker.save([legacy])
    original = path.read_bytes()
    first = sync_legacy_beliefs(service)
    assert first["imported"] == 1
    from pathlib import Path

    assert Path(first["backup_path"]).read_bytes() == original
    second = sync_legacy_beliefs(service)
    assert second == {"imported": 0, "unchanged": 1, "backup_path": None}
    rows = service.store.all("understanding")
    assert len(rows) == 1
    assert rows[0]["historical_evidence_count"] is None
    assert rows[0]["known_evidence_count"] == 0
    assert rows[0]["reported_legacy_evidence_count"] == 37
    assert rows[0]["evidence_ids"] == []
    assert tracker.load()[0].id == "old"


def test_canonical_invalidation_suppresses_legacy_but_manual_edit_and_explicit_win(tmp_path):
    service = _service(tmp_path)
    path = service.target.state_dir / "self-model-inferences.json"
    tracker = InferenceTracker(path)
    tracker.save(
        [
            InferenceRecord("inferred", "concise replies", "unknown", 0.8, source="reflection"),
            InferenceRecord("explicit", "do not send messages", "unknown", 0.8, source="explicit"),
        ]
    )
    sync_legacy_beliefs(service)
    for row in service.store.all("understanding"):
        service.set_status(row["id"], "invalidated", reason="new counterevidence")
    assert [r.id for r in tracker.get_active(learning_service=service)] == ["explicit"]
    physical = tracker.load()
    physical[0].inference = "operator manually corrected this preference"
    tracker.save(physical)
    assert len(tracker.get_active(learning_service=service)) == 2
    prior = next(r for r in service.store.all("understanding") if r["legacy_id"] == "inferred")
    assert not legacy_understanding_current(prior, service)
    sync_legacy_beliefs(service)
    assert len(service.store.all("understanding")) == 3


def test_physical_contradiction_invalidates_canonical_copy_and_all_context(tmp_path):
    service = _service(tmp_path)
    path = service.target.state_dir / "self-model-inferences.json"
    tracker = InferenceTracker(path)
    record = tracker.add_inference(
        "concise replies", "operator", 0.8, source="reflection", source_evidence=[_source()]
    )
    sync_legacy_beliefs(service)
    old = service.store.all("understanding")[0]
    tracker.contradict(record.id, by="winner:explicit>reflection")
    assert not tracker.get_active(learning_service=service)
    assert not legacy_understanding_current(old, service)
    sync_legacy_beliefs(service)
    assert {r["status"] for r in service.store.all("understanding")} == {
        "superseded",
        "invalidated",
    }


def test_provenance_priority_and_unknown_counter_do_not_manufacture_authority():
    explicit = InferenceRecord("e", "do not send", "operator", 0.5, source="explicit")
    inferred = InferenceRecord(
        "r", "send automatically", "unknown", 0.9, evidence_count=999, source="reflection"
    )
    settings = SimpleNamespace(allow_explicit_vs_explicit=False)
    loser, winner, _, held = _decide_loser(explicit, inferred, settings)
    assert (loser.id, winner.id, held) == ("r", "e", False)
    known = InferenceRecord(
        "k", "known event", "message", 0.5, source="reflection", source_evidence=[_source()]
    )
    loser, winner, reason, _ = _decide_loser(known, inferred, settings)
    assert loser.id == "r"
    assert winner.id == "k"
    assert reason == "evidence 1>0"


def test_automatic_counter_change_cannot_resurrect_canonical_invalidation(tmp_path):
    service = _service(tmp_path)
    tracker = InferenceTracker(service.target.state_dir / "self-model-inferences.json")
    tracker.save([InferenceRecord("old", "concise", "unknown", 0.8, source="reflection")])
    sync_legacy_beliefs(service)
    row = service.store.all("understanding")[0]
    service.set_status(row["id"], "invalidated", reason="contradiction")
    physical = tracker.load()
    physical[0].confidence = 0.75
    tracker.save(physical)
    assert not tracker.get_active(learning_service=service)
    sync_legacy_beliefs(service)
    assert not tracker.get_active(learning_service=service)
    assert all(
        r["status"] not in {"tentative", "supported"} for r in service.store.all("understanding")
    )
