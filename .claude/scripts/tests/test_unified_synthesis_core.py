"""Shared synthesis crosses real journal/queue stages with only inference faked."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from personas.learning import cognition, synthesis, synthesis_sources, worker
from personas.learning.errors import LearningOutputError
from personas.learning.models import LearningError, LearningTarget, content_hash
from personas.learning.queue import LearningQueue
from personas.learning.service import LearningService
from runtime import registry
from runtime.base import RuntimeResult


@pytest.fixture
def service(tmp_path):
    base = tmp_path / "researcher"
    target = LearningTarget(
        "researcher", base / "memory", base / "data", base / "state", base / "skills"
    )
    target.memory_dir.mkdir(parents=True)
    return LearningService(target)


def original(service, key="one"):
    exp = service.capture_experience(key, "chat", "Understand the observed reversal")
    obs = service.record_observation(
        exp["id"],
        {
            "quality": "direct",
            "status": "resolved",
            "evidence": {"source_id": key, "price": 123, "actual_observation": True},
        },
        source_key=key,
    )
    return exp, obs


def understanding(service, obs, *, key="understanding", **overrides):
    return service.record_understanding(
        {
            "understanding_type": "interpretation",
            "title": "Reversal evidence",
            "content": "A price observation is insufficient to establish reversal",
            "scope": "reversal",
            "uncertainty": "Only one independent observation",
            "evidence_ids": [obs["id"]],
            **overrides,
        },
        source_key=key,
    )


def nochange():
    return {
        "conclusion": "No additional conclusion is supported.",
        "understanding": [],
        "investigations": [],
        "proposals": [],
    }


@pytest.mark.asyncio
async def test_reflection_does_not_reconsume_status_updates_or_root_equivalent_cognition(
    service, monkeypatch
):
    _, obs = original(service)
    retained = understanding(service, obs)
    inquiry = service.open_investigation(
        {
            "question": "Does reversal persist?",
            "why": "Test duration",
            "domain": "fixture",
            "evidence_ids": [obs["id"]],
            "trigger": {"type": "deadline", "at": "2030-01-01T00:00:00Z"},
        },
        source_key="stable-question",
    )
    calls = []
    install_runtime(monkeypatch, nochange(), calls)
    first = synthesis.request_synthesis(service, "reflection", source_key="initial")
    await complete(service, job(service, first["cycle_id"]))
    service.set_status(retained["id"], "supported", reason="Support checked", key="checked")
    service.transition_investigation(
        inquiry["id"],
        "blocked",
        source_key="unavailable",
        reason="Observer unavailable",
        next_check_at="2030-01-02T00:00:00Z",
    )
    understanding(service, obs, key="cognitive-restatement")
    assert (
        synthesis.request_synthesis(service, "reflection", source_key="repeat", force=True)[
            "status"
        ]
        == "no_signal"
    )
    assert len(calls) == 1
    _, fresh = original(service, "fresh")
    next_cycle = synthesis.request_synthesis(
        service, "reflection", source_key="new-evidence", force=True
    )
    assert next_cycle["status"] == "queued"
    assert fresh["id"] in service.get_record(next_cycle["cycle_id"])["evidence_ids"]


@pytest.mark.asyncio
async def test_dream_status_only_is_quiet_but_changed_understanding_is_admitted(
    service, monkeypatch
):
    _, obs = original(service)
    prior = understanding(service, obs)
    calls = []
    install_runtime(monkeypatch, nochange(), calls)
    first = synthesis.request_synthesis(service, "dream", source_key="first")
    await complete(service, job(service, first["cycle_id"]))
    service.set_status(prior["id"], "supported", reason="Support receipt", key="support")
    assert (
        synthesis.request_synthesis(service, "dream", source_key="status")["status"] == "no_signal"
    )
    revised = understanding(
        service,
        obs,
        key="changed",
        predecessor_id=prior["id"],
        content="Duration now provides a distinct interpretation",
    )
    next_cycle = synthesis.request_synthesis(service, "dream", source_key="changed")
    assert next_cycle["status"] == "queued"
    assert revised["id"] in service.get_record(next_cycle["cycle_id"])["derived_input_ids"]


@pytest.mark.asyncio
async def test_status_transition_preserves_saved_reasoning_and_exact_journal_tail(
    service, monkeypatch
):
    _, obs = original(service)
    retained = understanding(service, obs, content="Long original interpretation " * 240)
    calls = []
    install_runtime(monkeypatch, nochange(), calls)
    first = synthesis.request_synthesis(service, "dream", source_key="partial")
    current = job(service, first["cycle_id"])
    stage, payload = await worker.process_stage(service, current)
    service.set_status(retained["id"], "supported", reason="Check completed", key="checked")
    await complete(service, {**current, "stage": stage, "payload": payload})
    assert len(calls) == 1
    first_source = next(
        row
        for row in service.get_record(first["cycle_id"])["input_manifest"]
        if row["metadata"].get("record_id") == retained["id"]
    )
    assert not first_source["complete"]
    second = synthesis.request_synthesis(service, "dream", source_key="tail")
    assert second["status"] == "queued"
    source = next(
        row
        for row in service.get_record(second["cycle_id"])["input_manifest"]
        if row["metadata"].get("record_id") == retained["id"]
    )
    assert source["revision"] == first_source["revision"]
    assert source["start"] == first_source["end"]
    full = synthesis.canonical_json(synthesis._journal_snapshot(service.get_record(retained["id"])))
    assert source["text"] == full[source["start"] : source["end"]]


def test_identical_active_inquiries_coalesce_without_merging_distinct_questions(service):
    _, obs = original(service)
    data = {
        "question": "Does reversal persist?",
        "why": "Test duration",
        "domain": "fixture",
        "evidence_ids": [obs["id"]],
        "trigger": {"type": "deadline", "at": "2030-01-01T00:00:00Z"},
    }
    first = service.open_investigation(data, source_key="cycle-one:0")
    assert service.open_investigation(data, source_key="cycle-two:0")["id"] == first["id"]
    other = service.open_investigation(
        data | {"question": "Does volume confirm reversal?"}, source_key="cycle-two:1"
    )
    assert other["id"] != first["id"]
    service.transition_investigation(
        first["id"], "completed", source_key="done", conclusion="Resolved"
    )
    assert service.open_investigation(data, source_key="cycle-three:0")["id"] != first["id"]


@pytest.mark.asyncio
async def test_completed_v1_source_coverage_upgrades_without_reconsumption(service, monkeypatch):
    _, obs = original(service)
    calls = []
    install_runtime(monkeypatch, nochange(), calls)
    original_sources = synthesis._journal_sources

    def old_sources(service, kind):
        result = original_sources(service, kind)
        for row in result:
            record = service.store.get(row["metadata"]["record_id"])
            text = synthesis.canonical_json(record)
            row.update(revision=content_hash(record), text=text, end=len(text))
            row["metadata"]["source_projection"] = "journal-record-v1"
        return result

    monkeypatch.setattr(synthesis, "_journal_sources", old_sources)
    first = synthesis.request_synthesis(service, "reflection", source_key="old")
    await complete(service, job(service, first["cycle_id"]))
    monkeypatch.setattr(synthesis, "_journal_sources", original_sources)
    assert (
        synthesis.request_synthesis(service, "reflection", source_key="upgraded")["status"]
        == "no_signal"
    )


def test_fresh_understanding_leads_legacy_backlog_without_consuming_omissions(service, monkeypatch):
    def source(ref, origin, time):
        text = ref + " context" * 600
        return {
            "ref": ref,
            "revision": content_hash(text),
            "kind": "understanding",
            "text": text,
            "start": 0,
            "end": len(text),
            "complete": True,
            "evidence_ids": [],
            "source_time": time,
            "metadata": {"origin": origin, "status": "tentative", "derived": True},
        }

    sources = [source(f"legacy:{i}", "legacy_import", "2026-09-10T23:00:00Z") for i in range(12)]
    sources += [source("current", "cognition", "2026-09-10T22:00:00Z")]
    monkeypatch.setattr(synthesis, "_journal_sources", lambda *a: sources)
    monkeypatch.setattr(synthesis_sources, "collect_sources", lambda *a, **kw: [])
    included, omitted = synthesis._freeze_inputs(service, "dream")
    assert included[0]["ref"] == "current"
    assert omitted and all(row["ref"].startswith("legacy:") for row in omitted)
    assert not synthesis._coverage(service, "dream")


def install_runtime(monkeypatch, output, calls):
    async def run(request):
        calls.append(request)
        assert (
            request.model_only and request.allowed_tools == [] and request.disallowed_tools == ["*"]
        )
        assert request.mcp_servers == [] and request.max_turns == 1 and request.hooks is None
        result = output(request) if callable(output) else output
        return RuntimeResult(
            text=json.dumps(result),
            model="fixture-model",
            provider="fixture-provider",
            runtime_lane="generic_runtime",
        )

    monkeypatch.setattr(registry, "run_with_fallback", run)
    # conftest disables automatic providers. This tests the production no-tools
    # inference seam while substituting only registry.run_with_fallback.
    from personas.learning.worker import _runtime_role

    monkeypatch.setattr(worker, "_runtime_role", _PRODUCTION_ROLE)
    assert _runtime_role


_PRODUCTION_ROLE = worker._runtime_role


def job(service, cycle_id):
    return next(
        row
        for row in LearningQueue(service).list(include_finished=True)
        if row["payload"].get("cycle_id") == cycle_id
    )


async def complete(service, current):
    for _ in range(6):
        stage, payload = await worker.process_stage(service, current)
        if stage == "done":
            return
        current = {**current, "stage": stage, "payload": payload}
    pytest.fail("synthesis did not finish its bounded stages")


@pytest.mark.asyncio
async def test_changed_understanding_questions_roots_and_nochange_are_one_real_request(
    service, monkeypatch
):
    exp, obs = original(service)
    retained = understanding(service, obs)
    inquiry = service.open_investigation(
        {
            "question": "Does the reversal persist?",
            "why": "Test the hypothesis",
            "domain": "fixture",
            "evidence_ids": [obs["id"]],
            "trigger": {
                "type": "deadline",
                "at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        },
        source_key="question",
    )
    calls = []
    install_runtime(monkeypatch, nochange(), calls)
    admitted = synthesis.request_synthesis(service, "dream", source_key="nightly")
    assert admitted["status"] == "queued"
    assert (
        synthesis.request_synthesis(service, "dream", source_key="another-wake")["cycle_id"]
        == admitted["cycle_id"]
    )
    cycle = service.get_record(admitted["cycle_id"])
    assert set(cycle["derived_input_ids"]) == {retained["id"], inquiry["id"]}
    assert set(cycle["evidence_ids"]) == {exp["id"], obs["id"]}
    await complete(service, job(service, cycle["id"]))
    assert len(calls) == 1
    for marker in (retained["content"], inquiry["question"], "actual_observation"):
        assert marker in calls[0].prompt
    assert (
        synthesis.request_synthesis(service, "dream", source_key="nightly-next")["status"]
        == "no_signal"
    )
    assert len(service.store.all("synthesis_cycle")) == 1
    assert service.get_record(cycle["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_inference_saved_before_reasoning_checkpoint_is_reused(service, monkeypatch):
    original(service)
    calls = []
    install_runtime(monkeypatch, nochange(), calls)
    admitted = synthesis.request_synthesis(service, "reflection", source_key="retry")
    current = job(service, admitted["cycle_id"])
    store_event = service.store.event
    crash = [True]

    def event(record_id, event_type, payload, **kwargs):
        if event_type == "synthesis_reasoning" and crash[0]:
            crash[0] = False
            raise OSError("interrupted after actual inference")
        return store_event(record_id, event_type, payload, **kwargs)

    monkeypatch.setattr(service.store, "event", event)
    with pytest.raises(OSError):
        await worker.process_stage(service, current)
    assert len(calls) == 1
    await complete(service, current)
    assert len(calls) == 1
    assert (
        len(
            [
                row
                for row in service.store.all("execution")
                if row.get("learning_role", "").startswith("synthesis_")
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_exact_partial_ranges_remain_eligible_and_identity_is_not_fresh_signal(
    service, monkeypatch
):
    source_text = "observed episode body " * 1000
    source = {
        "ref": "memory:episode",
        "revision": content_hash(source_text),
        "kind": "legacy_episode",
        "text": source_text,
        "start": 0,
        "end": len(source_text),
        "complete": True,
        "metadata": {},
        "evidence_ids": [],
        "source_time": None,
    }
    identity = {
        **source,
        "ref": "identity:SOUL",
        "text": "An explicit operator instruction",
        "end": 32,
        "metadata": {"always_context": True},
    }
    identity["end"] = len(identity["text"])
    monkeypatch.setattr(synthesis_sources, "collect_sources", lambda *a, **k: [source, identity])
    monkeypatch.setattr(synthesis_sources, "project_completed_synthesis", lambda *a: {})
    calls = []
    install_runtime(monkeypatch, nochange(), calls)
    first = synthesis.request_synthesis(service, "dream", source_key="first", force=True)
    await complete(service, job(service, first["cycle_id"]))
    previous_end = service.get_record(first["cycle_id"])["input_manifest"][0]["end"]
    second = synthesis.request_synthesis(service, "dream", source_key="second")
    assert second["status"] == "queued"  # Tail bypasses interval/signal threshold.
    cycle = service.get_record(second["cycle_id"])
    segment = next(row for row in cycle["input_manifest"] if row["ref"] == source["ref"])
    assert segment["start"] == previous_end
    assert segment["text"] == source_text[segment["start"] : segment["end"]]
    assert not segment["complete"]
    assert identity["text"] in synthesis._prompt(cycle)


@pytest.mark.asyncio
async def test_legacy_conclusion_stays_tentative_and_proposal_pending_without_invented_roots(
    service, monkeypatch
):
    (service.target.memory_dir / "daily").mkdir()
    (service.target.memory_dir / "daily" / "note.md").write_text(
        "Historical report of reversal uncertainty.", encoding="utf-8"
    )
    admitted = synthesis.request_synthesis(service, "reflection", source_key="legacy")
    cycle = service.get_record(admitted["cycle_id"])
    ref = cycle["input_manifest"][0]["ref"]
    output = nochange()
    output["understanding"] = [
        {
            "understanding_type": "interpretation",
            "title": "Historical uncertainty",
            "content": "The historical note records uncertainty.",
            "scope": "reversal",
            "uncertainty": "Unknown original counts",
            "evidence_ids": [],
            "source_refs": [ref],
        }
    ]
    output["proposals"] = [
        {
            "candidate_type": "procedure",
            "title": "Reversal method",
            "content": "Check reversal duration",
            "applicability": "reversal study",
            "uncertainty": "Unqualified",
            "changes_behavior": True,
            "target_file": "SELF.md",
            "evidence_ids": [],
            "source_refs": [ref],
        }
    ]
    calls = []
    install_runtime(monkeypatch, output, calls)
    await complete(service, job(service, cycle["id"]))
    retained = service.store.all("understanding")[0]
    assert retained["status"] == "tentative" and retained["historical_evidence_count"] is None
    proposal = service.store.all("change_proposal")[0]
    assert proposal["status"] == "pending" and proposal["reason"] == "missing_supporting_evidence"
    assert not service.store.all("candidate") and not service.store.all("activation")
    assert "historical note" in service.render_cognitive_context("reversal").text


@pytest.mark.asyncio
async def test_revised_observation_invalidates_pending_batch_before_inference(service, monkeypatch):
    exp, obs = original(service)
    understanding(service, obs)
    admitted = synthesis.request_synthesis(service, "dream", source_key="before-correction")
    service.record_observation(
        exp["id"],
        {
            "quality": "direct",
            "status": "resolved",
            "evidence": "corrected price",
            "supersedes": obs["id"],
        },
        source_key="correction",
    )
    calls = []
    install_runtime(monkeypatch, nochange(), calls)
    await complete(service, job(service, admitted["cycle_id"]))
    assert not calls and service.get_record(admitted["cycle_id"])["status"] == "superseded"
    assert "insufficient" not in service.render_cognitive_context("reversal").text
    assert (
        synthesis.request_synthesis(service, "dream", source_key="after-correction")["status"]
        == "queued"
    )


def test_unseen_evidence_or_predecessor_cannot_be_cited(service):
    _, obs = original(service)
    admitted = synthesis.request_synthesis(service, "reflection", source_key="sources")
    cycle = service.get_record(admitted["cycle_id"])
    result = nochange()
    result["understanding"] = [
        {
            "understanding_type": "belief",
            "title": "Fact",
            "content": "A fact",
            "scope": "all tasks",
            "uncertainty": "tentative",
            "source_refs": [cycle["input_manifest"][0]["ref"]],
            "evidence_ids": ["invented"],
        }
    ]
    with pytest.raises(LearningOutputError, match="outside"):
        synthesis._validate_output(service, cycle, result)
    result["understanding"][0]["evidence_ids"] = [obs["id"]]
    result["understanding"][0]["source_refs"] = ["not-in-snapshot"]
    with pytest.raises(LearningOutputError, match="outside"):
        synthesis._validate_output(service, cycle, result)


def test_testmode_disabled_persona_and_dream_switch(service, monkeypatch):
    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "false")
    assert (
        synthesis.request_synthesis(service, "dream", source_key="test", test_mode=True)["status"]
        == "test_mode"
    )
    assert not service.store.path.exists()
    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "true")
    original(service)
    monkeypatch.setenv("PERSONA_DREAM_ENABLED", "false")
    assert (
        synthesis.request_synthesis(service, "dream", source_key="off", force=True)["status"]
        == "disabled"
    )
    assert not service.store.all("synthesis_cycle")


def test_v2_migration_backup_preserves_unknown_counts_and_tuning_is_separate(service):
    exp, _ = original(service)
    with sqlite3.connect(service.store.path) as connection:
        connection.execute("PRAGMA user_version=2")
    service.store.put(
        "tuning_run", {"status": "no_change", "historical_evidence_count": None}, key="tune:one"
    )
    backups = list(service.store.directory.glob("learning-v2-*.backup.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT id FROM records WHERE id=?", (exp["id"],)).fetchone()
    service.store.set_setting("probe", True)
    assert len(list(service.store.directory.glob("learning-v2-*.backup.db"))) == 1
    assert service.store.all("tuning_run")[0]["historical_evidence_count"] is None
    assert service.summary()["counts"]["candidate"] == 0


@pytest.mark.asyncio
async def test_procedure_proposal_receives_investigation_conclusion_as_derived_context(
    service, monkeypatch
):
    exp, obs = original(service)
    inquiry = service.open_investigation(
        {
            "question": "Was reversal sustained?",
            "why": "Test duration",
            "domain": "fixture",
            "evidence_ids": [obs["id"]],
            "trigger": {"type": "deadline", "at": datetime.now(UTC).isoformat()},
        },
        source_key="inquiry",
    )
    service.transition_investigation(
        inquiry["id"],
        "completed",
        source_key="conclusion",
        conclusion="Duration refuted the initial reversal",
        evidence_ids=[obs["id"]],
    )
    calls = []
    install_runtime(monkeypatch, {"candidate": None, "reason": "No qualified improvement"}, calls)
    current = {"id": "procedure-job", "payload": {"experience_id": exp["id"]}}
    stage, _ = await worker._propose(service, current)
    assert stage == "done" and len(calls) == 1
    payload = json.loads(calls[0].prompt.split("\n", 1)[1])
    assert payload["derived_context"][0]["conclusion"] == "Duration refuted the initial reversal"
    assert inquiry["id"] not in {row["id"] for row in payload["evidence"]}


def test_foreground_reorientation_records_actual_runtime_once_without_queue(service):
    exp = service.capture_experience("turn", "chat", "Understand reversal")
    receipt = {"success": True, "provider": "fixture", "model": "reasoner", "output_hash": "abc"}
    record = service.record_reorientation(exp["id"], "turn", receipt)
    assert record["execution_kind"] == "reasoning" and record["model_call_count"] == 1
    assert service.record_reorientation(exp["id"], "turn", receipt)["id"] == record["id"]
    assert not LearningQueue(service).list()
    context = service.record_reorientation(exp["id"], "other")
    assert context["execution_kind"] == "context_only" and context["model_call_count"] == 0
    with pytest.raises(LearningError):
        service.record_reorientation(exp["id"], "invalid", {"success": True, "provider": "guessed"})


def test_explicit_operator_understanding_cannot_be_automatically_superseded(service):
    _, obs = original(service)
    explicit = understanding(service, obs, explicit_operator_authority=True)
    with pytest.raises(LearningError, match="explicit operator"):
        understanding(service, obs, key="automatic", predecessor_id=explicit["id"])


def test_retiring_understanding_invalidates_transitive_conclusions_but_not_replacement(service):
    _, obs = original(service)
    first = understanding(service, obs, key="first")
    second = understanding(service, obs, key="second", derived_input_ids=[first["id"]])
    third = understanding(service, obs, key="third", derived_input_ids=[second["id"]])
    replacement = understanding(
        service, obs, key="replacement", predecessor_id=first["id"], derived_input_ids=[first["id"]]
    )
    assert service.get_record(first["id"])["status"] == "superseded"
    assert service.get_record(second["id"])["status"] == "needs_reassessment"
    assert service.get_record(third["id"])["status"] == "needs_reassessment"
    assert service.get_record(replacement["id"])["status"] == "tentative"
    included = {
        row.get("record_id")
        for row in service.render_cognitive_context("reversal", max_chars=12000).versions
    }
    assert replacement["id"] in included and not included.intersection(
        {first["id"], second["id"], third["id"]}
    )


def test_overlapping_transcript_turn_and_revision_share_one_physical_root():
    from personas.learning.sources import independent_sources

    one = {"source_ref": "chat-message:session:17", "source_revision": "v1"}
    revised = {**one, "source_revision": "v2"}
    proof = independent_sources(
        [
            {"id": "turn", "metadata": {"source_evidence": [one]}},
            {"id": "transcript", "evidence": {"source_evidence": [one]}},
            {"id": "revision", "metadata": {"source_evidence": [revised]}},
        ]
    )
    assert proof["independent_source_count"] == 1 and proof["record_count"] == 3
    assert proof["sources"][0]["revisions"] == ["v1", "v2"]
    assert independent_sources([{"id": "historical"}])["independent_source_count"] is None
    grouped = independent_sources(
        [
            {
                "id": "container",
                "metadata": {"evidence_role": "source_container", "source_evidence": []},
            },
            {"id": "chunk", "evidence": {"source_evidence": [one]}},
        ]
    )
    assert grouped["independent_source_count"] == 1
    assert grouped["context_only_record_ids"] == ["container"]


def test_corrected_understanding_can_retain_superseded_counterevidence(service):
    exp, obs = original(service)
    prior = understanding(service, obs)
    corrected = service.record_observation(
        exp["id"],
        {
            "quality": "direct",
            "status": "resolved",
            "evidence": "Corrected price",
            "supersedes": obs["id"],
        },
        source_key="corrected",
    )
    revised = understanding(
        service,
        corrected,
        key="revised",
        predecessor_id=prior["id"],
        counterevidence_ids=[obs["id"]],
    )
    included = {
        row.get("record_id") for row in service.render_cognitive_context("reversal").versions
    }
    assert revised["id"] in included and prior["id"] not in included


def test_legacy_only_question_waits_without_observer_or_fabricated_experience(service):
    text = "A historical question"
    inquiry = service.open_investigation(
        {
            "question": "Was the signal reliable?",
            "why": "Historical unresolved question",
            "domain": "fixture",
            "trigger": {"type": "source_update", "source_id": "legacy", "revision": "1"},
            "evidence_ids": [],
            "origin": "synthesis",
            "source_manifest": [
                {
                    "ref": "legacy:question",
                    "revision": "1",
                    "kind": "legacy_note",
                    "text": text,
                    "start": 0,
                    "end": len(text),
                }
            ],
        },
        source_key="legacy-question",
    )
    assert inquiry["status"] == "blocked" and inquiry["experience_id"] is None
    cognition.discover_cognitive_work(service)
    assert not LearningQueue(service).list()
    assert not service.store.all("experience")


def test_reorientation_distinguishes_prepared_and_executed_context(service):
    from personas.learning.models import LearningContext

    exp = service.capture_experience("context-turn", "chat", "reversal")
    context = LearningContext(
        "Retained context",
        ({"record_id": "known", "rendered_block": "Retained context"},),
        "ctx-hash",
    )
    prepared = service.record_reorientation(
        exp["id"], "prepared", {"context_hash": context.context_hash}, context=context
    )
    assert prepared["context_delivery"] == "prepared" and prepared["model_call_count"] == 0
    actual = service.record_reorientation(
        exp["id"],
        "actual",
        {
            "success": True,
            "provider": "fixture",
            "model": "reasoner",
            "response_hash": "response",
            "prompt_hash": "prompt",
            "retained_context_hash": context.context_hash,
        },
        context=context,
    )
    assert (
        actual["context_delivery"] == "executed"
        and actual["input_versions"][0]["record_id"] == "known"
    )
    assert actual["foreground_receipt"]["prompt_hash"] == "prompt"
    omitted = service.record_reorientation(
        exp["id"],
        "omitted",
        {
            "success": True,
            "provider": "fixture",
            "model": "reasoner",
            "response_hash": "response",
            "retained_context_hash": "different-context",
        },
        context=context,
    )
    assert omitted["model_call_count"] == 1 and omitted["context_delivery"] == "prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("after_retention", [False, True])
async def test_retired_derived_input_cannot_resurrect_at_later_synthesis_stage(
    service, monkeypatch, after_retention
):
    _, obs = original(service)
    retained = understanding(service, obs)
    admitted = synthesis.request_synthesis(service, "dream", source_key="derived-snapshot")
    cycle = service.get_record(admitted["cycle_id"])
    output = nochange()
    output["understanding"] = [
        {
            "understanding_type": "interpretation",
            "title": "Deeper conclusion",
            "content": "A derived interpretation",
            "scope": "reversal",
            "uncertainty": "Still uncertain",
            "evidence_ids": [obs["id"]],
            "source_refs": ["learning:" + retained["id"]],
        }
    ]
    install_runtime(monkeypatch, output, [])
    current = job(service, cycle["id"])
    stage, payload = await worker.process_stage(service, current)
    current = {**current, "stage": stage, "payload": payload}
    if after_retention:
        stage, payload = await worker.process_stage(service, current)
        current = {**current, "stage": stage, "payload": payload}
    service.set_status(
        retained["id"], "invalidated", reason="Contradicted by new evidence", key="retired"
    )
    assert (await worker.process_stage(service, current))[0] == "done"
    assert service.get_record(cycle["id"])["status"] == "superseded"
    events = service.store.events(cycle["id"])
    assert any(row["event_type"] == "synthesis_reasoning" for row in events)
    assert not any(row["event_type"] == "synthesis_consumed" for row in events)
    assert "derived interpretation" not in service.render_cognitive_context("reversal").text
    assert (
        synthesis.request_synthesis(service, "dream", source_key="refreshed")["status"] == "queued"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_unknown_private_reasoning_fields_are_not_persisted(service, monkeypatch, nested):
    _, obs = original(service)
    admitted = synthesis.request_synthesis(service, "reflection", source_key="privacy")
    output = nochange()
    secret_trace = "PRIVATE_INTERNAL_MONOLOGUE_SHOULD_NEVER_PERSIST"
    if nested:
        output["understanding"] = [
            {
                "understanding_type": "interpretation",
                "title": "Fact",
                "content": "A concise fact",
                "scope": "reversal",
                "uncertainty": "Unknown",
                "evidence_ids": [obs["id"]],
                "source_refs": ["learning:" + obs["id"]],
                "thinking": secret_trace,
            }
        ]
    else:
        output["thinking"] = secret_trace
    install_runtime(monkeypatch, output, [])
    stage, _ = await worker.process_stage(service, job(service, admitted["cycle_id"]))
    assert stage == "synthesis_retain"
    with sqlite3.connect(service.store.path) as db:
        persisted = repr(db.execute("SELECT payload FROM records").fetchall()) + repr(
            db.execute("SELECT payload FROM events").fetchall()
        )
    assert secret_trace not in persisted
    execution = service.store.all("execution")[0]
    assert execution["output_status"] == "valid"
    assert "thinking" not in execution["response_text"]
    assert json.loads(execution["response_text"])["conclusion"] == output["conclusion"]
    assert execution["model"] == "fixture-model" and execution["response_hash"]


def test_invalidating_an_applied_candidate_schedules_physical_owner_regression(service):
    _, obs = original(service)
    candidate = service.propose_candidate(
        {
            "candidate_type": "procedure",
            "title": "Bound method",
            "content": "Check the observation",
            "applicability": "reversal",
            "evidence_ids": [obs["id"]],
        },
        source_key="applied",
    )
    activation = service.store.put(
        "activation",
        {
            "candidate_id": candidate["id"],
            "status": "active_provisional",
        },
        key="physical-owner-fixture",
    )
    service.set_status(
        candidate["id"], "needs_reassessment", reason="Derived input changed", key="invalidated"
    )
    regressions = [row for row in LearningQueue(service).list() if row["kind"] == "regression"]
    assert len(regressions) == 1
    assert regressions[0]["payload"]["activation_id"] == activation["id"]
