"""Recovery uses actual runtime receipts and exact owned source revisions."""

from datetime import UTC, datetime

import pytest

from personas.learning import cognition
from personas.learning.investigation_sources import collect_journal_source
from personas.learning.service import LearningService
from tests.test_continuous_cognition_core import (
    install_model,
    job_for,
    observed,
    response,
)
from tests.test_continuous_cognition_core import (
    service as _service_fixture,
)

service = _service_fixture


@pytest.mark.asyncio
async def test_invalid_response_retry_requires_persisted_feedback(service, monkeypatch):
    exp, row = observed(service)
    cycle = service.enqueue_cognitive_cycle(
        "interpret", "feedback", [row["id"]], experience_id=exp["id"]
    )
    calls = []

    def infer(request):
        value = response(row, inquiries=False)
        assert "CITABLE EVIDENCE IDS" in request.prompt
        if "PREVIOUS OUTPUT VALIDATION" not in request.prompt:
            value["understanding"][0]["evidence_ids"] = ["nested-source-not-a-citation"]
        else:
            assert "out_of_snapshot" in request.prompt
            assert "understanding[0].evidence_ids" in request.prompt
            assert "nested-source-not-a-citation" not in request.prompt
        return value

    install_model(monkeypatch, infer, calls)
    job = job_for(service, "cognition", cycle_id=cycle["id"])
    with pytest.raises(cognition.CognitiveReferenceError):
        await cognition.process_cognitive_stage(service, job)
    assert not service.store.all("understanding")
    execution = service.store.all("execution")[-1]
    assert execution["output_status"] == "invalid_contract"
    assert execution["model"] == "test-model"
    assert execution["source_inputs"][0]["included"]
    restarted = LearningService(service.target)
    await cognition.process_cognitive_stage(restarted, job)
    assert len(calls) == 2 and len(restarted.store.all("understanding")) == 1
    await cognition.process_cognitive_stage(restarted, job)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "refs,code",
    [
        ([], "missing_evidence_ids"),
        (None, "malformed_evidence_ids"),
        ("an-id", "malformed_evidence_ids"),
        ([{}], "malformed_evidence_ids"),
        (["other-persona-id"], "out_of_snapshot"),
    ],
)
def test_citation_diagnostics_keep_missing_and_invalid_distinct(service, refs, code):
    _, row = observed(service)
    output = response(row, inquiries=False)
    output["understanding"][0]["evidence_ids"] = refs
    with pytest.raises(cognition.CognitiveReferenceError) as caught:
        cognition._validate_output(service, {"evidence_ids": [row["id"]]}, output)
    assert caught.value.code == code


def source(service, key, revision, *, generated=False):
    exp = service.capture_experience(
        f"captured:{key}:{revision}",
        "source_capture",
        "Observed source",
        metadata={"domain": "social", "cognitive_generated": generated},
    )
    return service.record_observation(
        exp["id"],
        {
            "status": "partial",
            "quality": "direct",
            "evidence": {"source_id": key, "revision": revision, "content": "observed"},
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        source_key=f"{key}:{revision}",
    )


@pytest.mark.asyncio
async def test_source_followup_ignores_domain_spelling_and_reuses_real_observation(service):
    first = source(service, "post:123", "r1")
    inquiry = service.open_investigation(
        {
            "question": "Did the same post receive a revision?",
            "why": "Compare actual revisions",
            "domain": "linkedin-editorial-analysis",
            "evidence_ids": [first["id"]],
            "trigger": {"type": "source_update", "source_id": "post:123", "revision": "r1"},
        },
        source_key="question",
    )
    source(service, "post:999", "r2")
    source(service, "post:123", "generated", generated=True)
    assert not collect_journal_source(service, inquiry)["available"]
    second = source(service, "post:123", "r2")
    restarted = LearningService(service.target)
    job = job_for(restarted, "investigation", investigation_id=inquiry["id"])
    stage, payload = await cognition.process_cognitive_stage(restarted, job)
    assert stage == "done" and payload["observation_id"] == second["id"]
    assert len(restarted.store.all("observation")) == 4  # No synthetic duplicate.
    latest = restarted.store.get(inquiry["id"])
    assert not collect_journal_source(restarted, latest)["available"]
    cycle = restarted.store.get(payload["cycle_id"])
    assert set(cycle["evidence_ids"]) == {first["id"], second["id"]}


def test_source_observer_keeps_registered_acquisition_and_missing_evidence_pending(
    service, monkeypatch
):
    def marker(*_):
        return None

    monkeypatch.setitem(cognition._observers, "fixture-host", marker)
    trigger = {"type": "source_update", "source_id": "post:1", "revision": "r1"}
    assert cognition._observer("fixture-host", trigger) is marker
    assert cognition._observer("any descriptive topic", trigger) is collect_journal_source
    assert cognition._observer("unknown", {"type": "crossing"}) is None


@pytest.mark.asyncio
async def test_pending_revisit_never_replays_older_or_consumed_revision(service):
    first = source(service, "post:123", "r1")
    inquiry = service.open_investigation(
        {
            "question": "Did the post change?",
            "why": "Follow original revision",
            "domain": "social",
            "evidence_ids": [first["id"]],
            "trigger": {"type": "source_update", "source_id": "post:123", "revision": "r1"},
        },
        source_key="monotonic",
    )
    source(service, "post:123", "r2")
    newest = source(service, "post:123", "r3")
    job = job_for(service, "investigation", investigation_id=inquiry["id"])
    _, payload = await cognition.process_cognitive_stage(service, job)
    assert payload["observation_id"] == newest["id"]
    # Real pending transitions replace the latest list. Immutable history still
    # prevents an older r2 from becoming the next supposed fresh observation.
    service.transition_investigation(
        inquiry["id"], "pending", source_key="wait", reason="Need another actual source revision"
    )
    restarted = LearningService(service.target)
    for _ in range(3):
        assert not collect_journal_source(restarted, restarted.store.get(inquiry["id"]))[
            "available"
        ]
    fourth = source(restarted, "post:123", "r4")
    assert (
        collect_journal_source(restarted, restarted.store.get(inquiry["id"]))["observation_id"]
        == fourth["id"]
    )


@pytest.mark.asyncio
async def test_host_domain_selects_collector_despite_descriptive_model_domain(service, monkeypatch):
    first = source(service, "post:123", "r1")
    inquiry = service.open_investigation(
        {
            "question": "Did the post change?",
            "why": "Compare observations",
            "domain": "unregistered editorial topic",
            "evidence_ids": [first["id"]],
            "trigger": {"type": "source_update", "source_id": "post:123", "revision": "r1"},
        },
        source_key="host-domain",
    )
    seen = []

    def host_observer(svc, row):
        seen.append(row["id"])
        return {"available": False, "reason": "No remote revision"}

    monkeypatch.setitem(cognition._observers, "social", host_observer)
    second = source(service, "post:123", "r2")
    _, payload = await cognition.process_cognitive_stage(
        service, job_for(service, "investigation", investigation_id=inquiry["id"])
    )
    assert seen == [inquiry["id"]]
    assert payload["observation_id"] == second["id"]
