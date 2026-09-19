"""Regressions for concrete live type, source-size and silent-poll failures."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from personas.learning import cognition, worker
from personas.learning.errors import LearningOutputError, LearningUnavailableError
from personas.learning.models import LearningTarget, canonical_json, content_hash
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


def source_payload():
    return {
        "source_id": "exchange:BTC:5m",
        "revision": "closed-v2",
        "asset": "BTC",
        "venue": "exchange",
        "timeframe": "5m",
        "metrics": {"rsi": 66.25, "close": 123.45, "volume": 501.0},
        "closed_candles": [
            {
                "closed_at": f"2026-09-10T12:{index:02}:00Z",
                "close": index + 100.25,
                "report": (f"literal-{index}\\quoted\n" * 600),
            }
            for index in range(48)
        ],
        "analysis_inputs": {
            f"note_{index}": "historical source prose " * 400 for index in range(6)
        },
    }


def observe(service, key, evidence=None):
    exp = service.capture_experience(key, "test", "Assess a chart observation")
    return service.record_observation(
        exp["id"],
        {
            "quality": "direct",
            "status": "partial",
            "occurred_at": datetime.now(UTC).isoformat(),
            "evidence": evidence or {"source_id": key, "revision": "v1", "close": 123},
        },
        source_key=key,
    )


def mock_model(monkeypatch, output, calls):
    import recall_service

    async def recall(*_, **__):
        return SimpleNamespace(formatted_text="", results=[])

    async def model(request):
        calls.append(request)
        assert (
            request.model_only and request.allowed_tools == [] and request.disallowed_tools == ["*"]
        )
        return RuntimeResult(
            text=json.dumps(output),
            model="actual-test-model",
            provider="test-provider",
            runtime_lane="generic_runtime",
        )

    monkeypatch.setattr(recall_service, "recall", recall)
    monkeypatch.setattr(registry, "run_with_fallback", model)


def test_retained_type_is_supplied_and_predecessor_rule_is_explicit(service):
    observation = observe(service, "type")
    retained = service.record_understanding(
        {
            "understanding_type": "belief",
            "title": "Chart interpretation",
            "content": "The chart is uncertain",
            "scope": "all tasks",
            "uncertainty": "One observation",
            "evidence_ids": [observation["id"]],
        },
        source_key="belief",
    )
    cycle = service.enqueue_cognitive_cycle("interpret", "type", [observation["id"]])
    prompt, _, context = cognition._prompt(service, cycle)
    assert "Type: belief" in context.text and "different type requires a new record" in prompt
    assert (
        next(row for row in context.versions if row.get("record_id") == retained["id"])[
            "understanding_type"
        ]
        == "belief"
    )
    row = {
        key: retained[key]
        for key in (
            "understanding_type",
            "title",
            "content",
            "scope",
            "uncertainty",
            "evidence_ids",
        )
    }
    row["predecessor_id"] = retained["id"]
    output = {"conclusion": "Refined belief", "understanding": [row], "investigations": []}
    cognition._validate_output(service, cycle, output)
    row["understanding_type"] = "interpretation"
    with pytest.raises(LearningOutputError, match="changed its type"):
        cognition._validate_output(service, cycle, output)


def test_adaptive_nested_source_fits_and_reports_original_array_indexes():
    original = {
        "id": "f7cdbae2-350f-5a5a-854a-bb5c52f0ce21",
        "kind": "observation",
        "created_at": "2026-09-10T12:00:00Z",
        "evidence": source_payload(),
    }
    assert len(canonical_json(original)) > 24000
    bounded = cognition._bounded_source(original)
    assert len(canonical_json(bounded)) <= 24000 and bounded["id"] == original["id"]
    assert bounded["evidence"]["metrics"] == original["evidence"]["metrics"]
    manifest = bounded["input_excerpt_manifest"]
    assert manifest["original_record_hash"] == content_hash(original)
    spans = manifest["omissions"]
    candles = next(row for row in spans if row["path"] == "/evidence/closed_candles")
    assert candles["start"] > 0 and candles["end"] == 48
    string = next(
        row for row in spans if row["path"] == f"/evidence/closed_candles/{candles['start']}/report"
    )
    assert (
        bounded["evidence"]["closed_candles"][0]["report"]
        == original["evidence"]["closed_candles"][candles["start"]]["report"][
            string["start"] : string["end"]
        ]
    )


@pytest.mark.asyncio
async def test_oversized_live_shaped_sources_reach_inference_with_exact_receipts(
    service, monkeypatch
):
    observations = [observe(service, f"large:{index}", source_payload()) for index in range(6)]
    cycle = service.enqueue_cognitive_cycle(
        "interpret", "large-sources", [row["id"] for row in observations]
    )
    calls = []
    mock_model(
        monkeypatch,
        {
            "conclusion": "No additional claim is justified.",
            "understanding": [],
            "investigations": [],
        },
        calls,
    )
    saved = await cognition._reason(service, cycle)
    assert len(calls) == 1
    supplied = json.loads(calls[0].prompt.split("\nACTUAL EVIDENCE:\n", 1)[1])
    assert len(canonical_json(supplied)) <= 48000
    assert all(len(canonical_json(row)) <= 24000 for row in supplied)
    assert len(saved["source_inputs"]) == len(observations)
    actual = {row["id"]: row for row in supplied}
    for receipt in saved["source_inputs"]:
        assert receipt["included"] is True
        assert receipt["excerpt"] == actual[receipt["record_id"]]
        assert receipt["excerpt_hash"] == content_hash(receipt["excerpt"])
        assert receipt["original_record_hash"] == content_hash(
            service.store.get(receipt["record_id"])
        )
    assert service.get_record(saved["execution_id"])["source_inputs"] == saved["source_inputs"]


@pytest.mark.asyncio
async def test_wholly_omitted_record_cannot_be_cited_as_delivered(service, monkeypatch):
    observations = [observe(service, f"many:{index}") for index in range(34)]
    cycle = service.enqueue_cognitive_cycle(
        "interpret", "many", [row["id"] for row in observations]
    )
    selected = service.cognitive_evidence_records(cycle["evidence_ids"])
    omitted = selected[32]
    receipts = {}
    prompt, _, _ = cognition._prompt(service, cycle, context_provenance=receipts)
    assert omitted["id"] not in prompt
    assert (
        next(row for row in receipts["source_inputs"] if row["record_id"] == omitted["id"])[
            "included"
        ]
        is False
    )
    mock_model(
        monkeypatch,
        {
            "conclusion": "Unsupported reference",
            "investigations": [],
            "understanding": [
                {
                    "understanding_type": "belief",
                    "title": "Claim",
                    "content": "Claim",
                    "scope": "chart",
                    "uncertainty": "unknown",
                    "evidence_ids": [omitted["id"]],
                }
            ],
        },
        [],
    )
    with pytest.raises(LearningOutputError, match="outside"):
        await cognition._reason(service, cycle)


@pytest.mark.asyncio
async def test_unavailable_inquiry_does_not_spend_ready_model_stage(service, tmp_path):
    queue = LearningQueue(service)
    queue.enqueue("investigation", "old-inquiry", payload={"investigation_id": "old"})
    queue.enqueue("cognition", "ready", payload={"cycle_id": "ready"})
    visits = []

    async def process(_, job):
        visits.append(job["stage"])
        if job["stage"] == "cognitive_observe":
            raise cognition.InvestigationEvidencePendingError("No new evidence")
        return "done", job["payload"]

    result = await worker.run_worker(
        service, max_stages=1, processor=process, activity_path=tmp_path / "activity.db"
    )
    assert result["stages"] == 1 and visits == ["cognitive_observe", "cognitive_reason"]
    polling = next(row for row in queue.list() if row["kind"] == "investigation")
    assert polling["status"] == "deferred" and polling["failures"] == 0
    assert polling["payload"]["evidence_poll_count"] == 1


@pytest.mark.asyncio
async def test_poll_batch_is_bounded_and_provider_failure_still_stops(service, tmp_path):
    queue = LearningQueue(service)
    for index in range(9):
        queue.enqueue("investigation", f"poll:{index}")
    queue.enqueue("cognition", "ready")
    visits = []

    async def process(_, job):
        visits.append(job["kind"])
        if job["kind"] == "investigation":
            raise cognition.InvestigationEvidencePendingError("No new evidence")
        raise LearningUnavailableError("Inference provider unavailable")

    first = await worker.run_worker(
        service, max_stages=1, processor=process, activity_path=tmp_path / "activity.db"
    )
    assert first["poll_deferrals"] == 8 and len(visits) == 8 and first["stages"] == 0
    second = await worker.run_worker(
        service, max_stages=1, processor=process, activity_path=tmp_path / "activity.db"
    )
    assert second == {"status": "deferred", "stages": 0}
    assert visits[-2:] == ["investigation", "cognition"]
