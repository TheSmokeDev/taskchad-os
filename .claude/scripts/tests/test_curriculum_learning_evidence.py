"""Curriculum evidence reaches the actual no-tools evaluator from temporary stores."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from curriculum.learning import complete_study, prepare_study
from personas.learning import evaluation, worker
from personas.learning.evaluation import runtime_reasoning as real_runtime_reasoning
from personas.learning.models import LearningTarget
from personas.learning.queue import LearningQueue
from personas.learning.service import LearningService


@pytest.fixture
def learning(tmp_path):
    return LearningService(
        LearningTarget(
            "sales",
            tmp_path / "memory",
            tmp_path / "data",
            tmp_path / "state",
            tmp_path / "skills",
        )
    )


def capture(learning, transcript):
    digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    video = {
        "video_id": "source-1",
        "url": "https://example.test/source",
        "title": "Sales discovery",
    }
    prepared = prepare_study(
        "sales",
        video,
        digest,
        service=learning,
        transcript=transcript,
        transcript_source="official_captions",
        source_timestamp="2026-08-31",
    )
    study = SimpleNamespace(
        markdown="[youtube:source-1 @ 01:02:03] Diagnose budget before discussing discount.",
        model="synthesis-model",
        provider="openai-compatible",
        runtime_lane="generic_runtime",
    )
    result = complete_study(
        prepared,
        video=video,
        transcript_digest=digest,
        dossier_path="unreadable-not-needed.md",
        study=study,
        dossier_text="# Actual persisted dossier\n" + study.markdown,
        proposals=[
            {"title": "Sales discovery", "body": "Diagnose budget before discussing discount."}
        ],
    )
    assert result["status"] == "recorded", result
    return result, prepared, video, study


@pytest.mark.asyncio
async def test_queue_support_evaluator_gets_literal_bounded_sources_without_file_tools(
    learning, monkeypatch
):
    from runtime import lane_router

    transcript = "[00:00:01] unrelated filler\n" * 1400
    transcript += (
        "[01:02:03] Ask the prospect which outcome justifies their budget "
        "before discussing discount.\n"
    )
    transcript = transcript.strip()
    result, prepared, video, study = capture(learning, transcript)
    observation = learning.get_record(result["observation_id"])
    evidence = observation["evidence"]
    excerpts = evidence["source_excerpts"]
    assert sum(len(row["text"]) for row in excerpts) <= 16000
    assert sum(len(row["text"]) for row in evidence["dossier_excerpts"]) <= 6000
    assert evidence["source_capture"]["complete"] is False
    assert any("which outcome justifies their budget" in row["text"] for row in excerpts)
    for row in excerpts:
        assert row["text"] == transcript[row["start_char"] : row["end_char"]]
        assert row["excerpt_sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()
        assert row["source_sha256"] == hashlib.sha256(transcript.encode()).hexdigest()
        assert row["source_timestamp"] == "2026-08-31"
        assert row["provenance"] == "official_captions"
    assert all(row["source_kind"] == "generated_dossier" for row in evidence["dossier_excerpts"])

    # Retry with the exact same physical source does not conflict with timestamps.
    repeated = complete_study(
        prepared,
        video=video,
        transcript_digest=hashlib.sha256(transcript.encode()).hexdigest(),
        dossier_path="unreadable-not-needed.md",
        study=study,
        dossier_text="# Actual persisted dossier\n" + study.markdown,
        proposals=[
            {"title": "Sales discovery", "body": "Diagnose budget before discussing discount."}
        ],
    )
    assert repeated["observation_id"] == result["observation_id"]

    jobs = LearningQueue(learning)
    queued = next(j for j in jobs.list() if j["kind"] == "candidate")
    supplied = []

    async def model(request):
        assert request.model_only is True
        assert request.allowed_tools == [] and request.disallowed_tools == ["*"]
        assert request.mcp_servers == [] and request.setting_sources == []
        payload = json.loads(request.prompt.split("\n", 1)[1])
        assert payload["mode"] == "support"
        supplied.append(payload)
        seen = payload["supporting"][result["observation_id"]]["evidence"]["source_excerpts"]
        assert seen == excerpts
        assert any("which outcome justifies their budget" in row["text"] for row in seen)
        # Reject after inspecting the evidence: this test needs no case inference.
        return SimpleNamespace(
            text=json.dumps(
                {
                    "supported": False,
                    "contradictions_addressed": True,
                    "changes_behavior": True,
                    "reason": "Further evidence required",
                }
            ),
            model="judge-model",
            provider="openai-codex",
            runtime_lane="generic_runtime",
            cost_usd=0.0,
            profile_key="test",
            tool_call_count=0,
            tool_calls=[],
            tool_names_used=[],
        )

    monkeypatch.setattr(evaluation, "runtime_reasoning", real_runtime_reasoning)
    monkeypatch.setattr(lane_router, "run_with_runtime_lanes", model)
    stage, payload = await worker.process_stage(learning, {**queued, "stage": "evaluate"})
    assert stage == "done" and payload["reason"] == "evidence_unsupported"
    assert len(supplied) == 1


def test_source_digest_mismatch_and_missing_source_are_explicit(learning):
    video = {"video_id": "v", "url": "https://example.test/v"}
    result = prepare_study("sales", video, "wrong", service=learning, transcript="source bytes")
    assert result == {"error_type": "ValueError"}
    assert not learning.store.all("experience")
    captured, _, _, _ = capture(learning, "")
    evidence = learning.get_record(captured["observation_id"])["evidence"]
    assert evidence["source_capture"]["status"] == "unavailable"
    assert evidence["source_excerpts"] == []


def test_study_context_is_prepared_until_runtime_returns(learning):
    from curriculum.learning import record_study_context, render_study_context

    prompt = render_study_context("sales", "Synthesize source", model="planned", service=learning)
    assert learning.store.all("context")[0]["phase"] == "prepared"
    record_study_context(prompt)
    assert {row["phase"] for row in learning.store.all("context")} == {"prepared", "submitted"}
    record_study_context(prompt, result=SimpleNamespace(success=False))
    assert all(row["phase"] != "executed" for row in learning.store.all("context"))
    record_study_context(
        prompt,
        result=SimpleNamespace(success=True, provider="real", model="actual", text="Synthesis"),
    )
    actual = next(row for row in learning.store.all("context") if row["phase"] == "executed")
    assert actual["model"] == "actual" and actual["provider"] == "real"


@pytest.mark.asyncio
async def test_producer_links_actual_synthesis_context_to_source_and_queues_reassessment(
    learning,
    monkeypatch,
):
    from dataclasses import replace

    from curriculum import study as study_module
    from personas.learning import service as facade
    from personas.learning.promotion import promote_candidate
    from video_learning.models import ExtractionResult, TranscriptSegment, VideoMetadata

    learning.target.memory_dir.mkdir(parents=True)
    (learning.target.memory_dir / "MEMORY.md").write_text("# Sales\n", encoding="utf-8")
    prior = learning.capture_experience("study-prior", "curriculum", "Source study")
    prior_observation = learning.record_observation(
        prior["id"],
        {
            "quality": "direct",
            "status": "resolved",
            "evidence": "Source claim lacked testing",
        },
        source_key="prior-source",
    )
    candidate = learning.propose_candidate(
        {
            "candidate_type": "procedure",
            "title": "Source study review",
            "content": (
                "During source study, separate direct testimony from experimental validation."
            ),
            "applicability": "Source study synthesis",
            "evidence_ids": [prior_observation["id"]],
            "domain": "curriculum",
            "baseline_version": "initial",
        },
        source_key="study-method",
    )
    manifest = evaluation.QualificationManifest(
        profile_id="sales",
        candidate_hash=candidate["content_hash"],
        baseline_content="",
        baseline_version="initial",
        model="old-study-model",
        provider="openai-compatible",
        cases=tuple(
            evaluation.QualificationCase(
                f"study-old-{i}",
                f"Source study synthesis review {i}",
                "Assess source validity",
                i < 8,
            )
            for i in range(12)
        ),
    )

    async def infer_case(case, content, manifest, **kwargs):
        return evaluation.CaseExecution(
            content, manifest.model, manifest.provider, manifest.runtime_lane
        )

    async def judge(payload, **kwargs):
        if payload["mode"] == "support":
            return {"supported": True, "contradictions_addressed": True, "changes_behavior": True}
        return {
            "score_a": 0.9 if "testimony" in payload["output_a"] else 0.2,
            "score_b": 0.9 if "testimony" in payload["output_b"] else 0.2,
            "failures_a": [],
            "failures_b": [],
        }

    qualified = await evaluation.evaluate_candidate(
        learning,
        candidate["id"],
        manifest=manifest,
        run_case=infer_case,
        judge=judge,
    )
    assert qualified["passed"], qualified
    activation = promote_candidate(learning, candidate["id"], qualified["id"])
    extraction = ExtractionResult(
        metadata=VideoMetadata(
            source="https://example.test/new-source",
            source_type="youtube",
            video_id="new-source",
            title="Source study",
            upload_date="20260901",
        ),
        segments=[
            TranscriptSegment(1, 4, "One salesperson's discovery anecdote, not a controlled test.")
        ],
        transcript_source="official_captions",
        artifact_dir=learning.target.data_dir,
    )
    transcript = extraction.transcript.strip()
    digest = hashlib.sha256(transcript.encode()).hexdigest()
    video = {"video_id": "new-source", "url": extraction.metadata.source, "title": "Source study"}
    prepared = prepare_study(
        "sales",
        video,
        digest,
        service=learning,
        transcript=transcript,
        transcript_source=extraction.transcript_source,
        source_timestamp=extraction.metadata.upload_date,
    )
    monkeypatch.setattr(facade, "get_learning_service", lambda _persona_id: learning)
    monkeypatch.setattr(
        study_module, "get_background_models", lambda: {"fast": "hint", "quality": "hint"}
    )
    calls = []

    async def study_inference(request):
        calls.append(request)
        if request.task_name == "curriculum_deep_study":
            assert candidate["content"] in request.prompt
            assert not [row for row in learning.store.all("context") if row["phase"] == "executed"]
            text = (
                "[youtube:new-source @ 00:00:01] This is anecdotal testimony; test its application."
            )
            model, cost = "synthesis-v2", 0.08
        else:
            assert candidate["content"] not in request.prompt
            text = "[00:00:01] Sales discovery anecdote without a controlled test."
            model, cost = "extraction-v1", 0.04
        return SimpleNamespace(
            text=text,
            success=True,
            provider="gemini",
            model=model,
            runtime_lane="generic_runtime",
            session_id=request.task_name,
            cost_usd=cost,
        )

    monkeypatch.setattr(study_module, "run_curriculum_model", study_inference)
    study = await study_module.study_extraction(
        extraction,
        persona_id="sales",
        persona_context="Sales",
        recalled_doctrine="",
        workspace=learning.target.memory_dir,
        study_model_tier="quality",
    )
    assert len(calls) == 2 and len(study.calls) == 2
    assert study.learning_receipt["parent_context_receipt_id"]
    completed = complete_study(
        prepared,
        video=video,
        transcript_digest=digest,
        dossier_path="source-dossier.md",
        dossier_text=study.markdown,
        study=study,
        proposals=[],
    )
    assert completed["status"] == "recorded", completed
    executions = learning.store.all("execution")
    parent = next(row for row in executions if row["stage"] == "study_synthesis")
    child = next(row for row in executions if row["stage"] == "dossier_written")
    assert parent["model_call_count"] == 1 and parent["cost_usd"] == 0.08
    assert child["model_call_count"] == 0 and "cost_usd" not in child
    assert "cost_usd" not in child["runtime"]
    assert child["reported_study_cost_usd"] == pytest.approx(0.12)
    assert child["attribution"] == "parent_inference"
    assert child["parent_execution_id"] == parent["id"]
    assert child["model"] == "synthesis-v2" and child["provider"] == "gemini"
    assert child["included_activation_ids"] == [activation["id"]]
    parent_context = learning.get_record(child["parent_context_receipt_id"])
    inherited = learning.get_record(child["context_receipt_id"])
    for key in ("included", "dropped", "rendered_prompt_hash", "context_hash", "model", "provider"):
        assert inherited[key] == parent_context[key]
    assert inherited["experience_id"] == completed["experience_id"]
    jobs = LearningQueue(learning).list(include_finished=True)
    requalification = next(job for job in jobs if job["kind"] == "requalification")
    assert requalification["payload"]["target_runtime"]["model"] == "synthesis-v2"
    assert requalification["payload"]["target_runtime"]["provider"] == "gemini"
    assert requalification["payload"]["experience_id"] == completed["experience_id"]
    reassessment = next(job for job in jobs if job["kind"] == "regression")
    assert reassessment["payload"]["observation_id"] == completed["observation_id"]
    assert reassessment["payload"]["activation_id"] == activation["id"]
    assert not [
        job for job in jobs if job["payload"].get("experience_id") == parent["experience_id"]
    ]

    rejected = complete_study(
        prepared,
        video=video,
        transcript_digest=digest,
        dossier_path="source-dossier.md",
        study=replace(study, markdown="Different unsourced output"),
        proposals=[],
    )
    assert rejected["status"] == "error" and rejected["error_type"] == "ValueError"
