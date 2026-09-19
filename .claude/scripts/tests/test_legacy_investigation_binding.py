"""Historical questions acquire only real, newer, source-matched anchors."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personas.learning import cognition, worker
from personas.learning.errors import LearningUnavailableError
from personas.learning.models import LearningError, LearningTarget
from personas.learning.queue import LearningQueue
from personas.learning.service import LearningService
from runtime import registry
from runtime.base import RuntimeResult


@pytest.fixture
def service(tmp_path):
    target = LearningTarget(
        "researcher",
        tmp_path / "memory",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "skills",
    )
    target.memory_dir.mkdir()
    return LearningService(target)


def install_runtime(monkeypatch, output, calls):
    async def run(request):
        calls.append(request)
        assert (
            request.model_only and request.allowed_tools == [] and request.disallowed_tools == ["*"]
        )
        return RuntimeResult(
            text=json.dumps(output),
            model="test-model",
            provider="test-provider",
            runtime_lane="generic_runtime",
        )

    monkeypatch.setattr(registry, "run_with_fallback", run)


def inquiry(service):
    text = "An unresolved historical source question"
    return service.open_investigation(
        {
            "question": "Did the source confirm the proposed interpretation?",
            "why": "The old note has no independently attributable evidence",
            "domain": "fixture",
            "trigger": {"type": "source_update", "source_id": "report", "revision": "v1"},
            "evidence_ids": [],
            "origin": "synthesis",
            "source_manifest": [
                {
                    "ref": "legacy:old-note",
                    "revision": "old",
                    "kind": "legacy_note",
                    "text": text,
                    "start": 0,
                    "end": len(text),
                }
            ],
        },
        source_key="historical-question",
    )


def fresh(service, *, source_id="report", domain="fixture", older=False, generated=False):
    exp = service.capture_experience(
        "fresh:" + source_id + domain,
        "test",
        "An actual newer report",
        mode="practice" if generated else "real",
        metadata={"domain": domain, **({"learning_role": "propose"} if generated else {})},
    )
    observed = datetime.now(UTC) - timedelta(days=int(older))
    observation = service.record_observation(
        exp["id"],
        {
            "quality": "direct",
            "status": "resolved",
            "occurred_at": observed.isoformat(),
            "evidence": {
                "source_id": source_id,
                "revision": "v2",
                "domain": domain,
                "finding": "The actual updated report rejects the earlier interpretation",
            },
        },
        source_key="actual-v2",
    )
    return exp, observation


def observe_job(service, inquiry_id):
    return next(
        row
        for row in LearningQueue(service).list()
        if row["kind"] == "investigation" and row["payload"]["investigation_id"] == inquiry_id
    )


@pytest.mark.asyncio
async def test_historical_question_binds_actual_new_source_and_completes_revisit(
    service, monkeypatch
):
    import recall_service

    from runtime import function_hooks

    monkeypatch.setattr(
        cognition,
        "_observers",
        {"fixture": lambda *_: pytest.fail("existing original source needs no repeated fetch")},
    )
    monkeypatch.setattr(function_hooks, "_HANDLERS", {})
    old = inquiry(service)
    exp, observation = fresh(service)
    cognition.discover_cognitive_work(service, recover_sources=False)
    stage, receipt = await worker.process_stage(service, observe_job(service, old["id"]))
    assert stage == "done" and receipt["observation_id"] == observation["id"]
    bound = service.get_record(old["id"])
    assert bound["experience_id"] == exp["id"] and bound["anchor_evidence_ids"] == [
        observation["id"]
    ]
    assert bound["evidence_ids"] == [] and bound["source_manifest"] == old["source_manifest"]
    assert bound["historical_evidence_count"] is None
    assert len(service.store.all("observation")) == 1

    async def recall(*_, **__):
        return SimpleNamespace(formatted_text="", results=[])

    monkeypatch.setattr(recall_service, "recall", recall)
    calls = []
    install_runtime(
        monkeypatch,
        {
            "conclusion": "The updated report resolves the historical uncertainty.",
            "understanding": [],
            "investigations": [],
            "investigation_result": {
                "status": "completed",
                "conclusion": "The newer report rejects the earlier interpretation.",
            },
        },
        calls,
    )
    cycle_job = next(
        row
        for row in LearningQueue(service).list()
        if row["payload"].get("cycle_id") == receipt["cycle_id"]
    )
    next_stage, payload = await worker.process_stage(service, cycle_job)
    assert next_stage == "cognitive_support"
    assert (
        await worker.process_stage(service, {**cycle_job, "stage": next_stage, "payload": payload})
    )[0] == "done"
    completed = service.get_record(old["id"])
    assert completed["status"] == "completed" and completed["historical_evidence_count"] is None
    assert completed["latest_evidence_ids"] == [observation["id"]]
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["source", "domain", "old", "generated"])
async def test_irrelevant_or_generated_anchor_remains_blocked_and_deferred(
    service, monkeypatch, wrong
):
    monkeypatch.setattr(
        cognition,
        "_observers",
        {"fixture": lambda *_: {"available": False, "reason": "Awaiting actual source"}},
    )
    old = inquiry(service)
    fresh(
        service,
        source_id="unrelated" if wrong == "source" else "report",
        domain="other" if wrong == "domain" else "fixture",
        older=wrong == "old",
        generated=wrong == "generated",
    )
    cognition.discover_cognitive_work(service, recover_sources=False)
    with pytest.raises(LearningUnavailableError):
        await worker.process_stage(service, observe_job(service, old["id"]))
    current = service.get_record(old["id"])
    assert current["status"] == "blocked" and current["experience_id"] is None
    assert current["evidence_ids"] == [] and current["historical_evidence_count"] is None
    assert not service.store.all("cognitive_cycle")


@pytest.mark.asyncio
async def test_registered_observer_can_bind_actual_owned_experience(service, monkeypatch):
    old = inquiry(service)
    exp = service.capture_experience(
        "actual-event", "host_observer", "The report was fetched", metadata={"domain": "fixture"}
    )
    monkeypatch.setattr(
        cognition,
        "_observers",
        {
            "fixture": lambda *_: {
                "available": True,
                "experience_id": exp["id"],
                "source_key": "report:v2",
                "occurred_at": datetime.now(UTC).isoformat(),
                "quality": "direct",
                "evidence": {
                    "source_id": "report",
                    "revision": "v2",
                    "domain": "fixture",
                    "content": "Actual fetched report",
                },
            }
        },
    )
    cognition.discover_cognitive_work(service, recover_sources=False)
    _, result = await worker.process_stage(service, observe_job(service, old["id"]))
    current = service.get_record(old["id"])
    assert current["experience_id"] == exp["id"] and current["evidence_ids"] == []
    assert current["latest_evidence_ids"] == [result["observation_id"]]
    assert service.get_record(result["cycle_id"])["evidence_ids"] == [result["observation_id"]]


@pytest.mark.asyncio
async def test_retired_question_cannot_bind_or_resume_pending_job(service, monkeypatch):
    monkeypatch.setattr(
        cognition,
        "_observers",
        {"fixture": lambda *_: pytest.fail("retired question must not observe")},
    )
    old = inquiry(service)
    exp, observation = fresh(service)
    cognition.discover_cognitive_work(service, recover_sources=False)
    queued = observe_job(service, old["id"])
    service.set_status(old["id"], "needs_reassessment", reason="Retired ancestry", key="retired")
    assert (await worker.process_stage(service, queued))[0] == "done"
    with pytest.raises(LearningError, match="retired"):
        service.transition_investigation(
            old["id"],
            "due",
            source_key="cannot-revive",
            evidence_ids=[observation["id"]],
            experience_id=exp["id"],
        )
    assert service.get_record(old["id"])["status"] == "needs_reassessment"
