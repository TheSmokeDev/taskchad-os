"""Linked beliefs appear in one selected region without losing authority."""

from __future__ import annotations

import engine as engine_module
from cognition.self_model import InferenceRecord, InferenceTracker
from engine import ConversationEngine

import config
from personas.learning.context import compile_cognitive_context
from personas.learning.legacy_beliefs import partition_legacy_context, sync_legacy_beliefs
from personas.learning.models import LearningContext, content_hash
from personas.learning.service import get_learning_service


def _setup(monkeypatch):
    service = get_learning_service("default")
    path = service.target.state_dir / "self-model-inferences.json"
    tracker = InferenceTracker(path)
    tracker.save(
        [
            InferenceRecord(
                "inferred", "Concise answers preference", "source", 0.9, source="reflection"
            ),
            InferenceRecord(
                "explicit", "Never send without approval", "operator", 0.8, source="explicit"
            ),
        ]
    )
    sync_legacy_beliefs(service)
    context = compile_cognitive_context(
        "Concise answers and never send without approval",
        service.store.all("understanding"),
        [],
        LearningContext(),
        max_chars=8000,
    )
    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", path)
    monkeypatch.setattr(config, "MEMORY_DIR", service.target.memory_dir)
    monkeypatch.setattr(config, "INFERENCE_PROMPT_CAP", 10)
    monkeypatch.setattr(engine_module, "_PROCESSES_AVAILABLE", False)
    return service, tracker, context, ConversationEngine.__new__(ConversationEngine)


def _legacy_text(memory):
    return "\n".join(row.content for row in memory.memories if row.region == "user_inferences")


def test_selected_inferred_uses_canonical_once_explicit_stays_authoritative(monkeypatch):
    _service, tracker, context, engine = _setup(monkeypatch)
    memory, filtered = engine._build_base_working_memory(
        retained_context=context,
        return_context=True,
    )
    legacy = _legacy_text(memory)
    assert "Concise answers preference" not in legacy
    assert "Never send without approval" in legacy
    assert "Concise answers preference" in filtered.text
    assert "Never send without approval" not in filtered.text
    linked = {row.id: row.journal_record_id for row in tracker.load()}
    assert [row["record_id"] for row in filtered.versions] == [linked["inferred"]]
    assert filtered.context_hash == content_hash(filtered.text)
    assert context.text != filtered.text  # the frozen input object was not mutated


def test_paused_or_unselected_context_keeps_legacy_beliefs(monkeypatch):
    service, _tracker, _context, engine = _setup(monkeypatch)
    service.set_paused(True)
    empty = service.render_cognitive_context("Concise answers")
    assert not empty.text
    for context in (None, empty, LearningContext("Unrelated context", (), "hash")):
        memory, filtered = engine._build_base_working_memory(
            retained_context=context,
            return_context=True,
        )
        legacy = _legacy_text(memory)
        assert "Concise answers preference" in legacy
        assert "Never send without approval" in legacy
        assert filtered is context


def test_cap_omitted_explicit_does_not_remove_its_canonical_block(monkeypatch):
    _service, tracker, context, engine = _setup(monkeypatch)
    monkeypatch.setattr(config, "INFERENCE_PROMPT_CAP", 1)
    memory, filtered = engine._build_base_working_memory(
        retained_context=context,
        return_context=True,
    )
    assert not _legacy_text(memory)  # only inferred was selected, then deduplicated
    explicit = next(row for row in tracker.load() if row.source == "explicit")
    assert any(row["record_id"] == explicit.journal_record_id for row in filtered.versions)
    assert "Never send without approval" in filtered.text


def test_identity_and_revision_required_never_fuzzy_text_dedup(monkeypatch):
    _service, tracker, context, _engine = _setup(monkeypatch)
    records = tracker.load()
    inferred = next(row for row in records if row.id == "inferred")
    old_ref = inferred.journal_record_id
    inferred.journal_record_id = "different-canonical-id"
    kept, unchanged = partition_legacy_context([inferred], context)
    assert kept == [inferred]
    assert unchanged is context
    inferred.journal_record_id = old_ref
    inferred.inference = "Manual correction to the original preference"
    kept, unchanged = partition_legacy_context([inferred], context)
    assert kept == [inferred]
    assert unchanged is context


def test_manifest_without_actual_rendered_block_cannot_hide_legacy(monkeypatch):
    _service, tracker, context, _engine = _setup(monkeypatch)
    missing_text = LearningContext("unrelated text", context.versions, "not-submitted")
    records = tracker.load()
    kept, unchanged = partition_legacy_context(records, missing_text)
    assert kept == records
    assert unchanged is missing_text
