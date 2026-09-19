"""v3 proof through the real queue worker and evaluator; only inference is faked."""

import asyncio
import time
from dataclasses import asdict, replace

import pytest

from personas.learning import evaluation as ev
from personas.learning import worker
from personas.learning.errors import LearningDeferredError, LearningOutputError
from personas.learning.models import LearningTarget
from personas.learning.queue import LearningQueue
from personas.learning.service import LearningService
from personas.learning.worker import _runtime_role as production_runtime_role
from runtime import activity
from runtime.errors import RuntimeExecutionError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "true")
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_ACTIVITY_DB", str(tmp_path / "activity.db"))
    target = LearningTarget(
        "crypto", tmp_path / "memory", tmp_path / "data", tmp_path / "state", tmp_path / "skills"
    )
    target.memory_dir.mkdir()
    service = LearningService(target)
    experience = service.capture_experience("chart-1", "test", "Discuss chart risk")
    observation = service.record_observation(
        experience["id"],
        {
            "evidence": "The proposed target did not account for costs.",
            "quality": "direct",
            "status": "resolved",
            "held": False,
        },
        source_key="chart-after",
    )
    candidate = service.propose_candidate(
        {
            "candidate_type": "procedure",
            "title": "Costs before chart risk",
            "content": "Include costs in a chart risk estimate.",
            "applicability": "chart risk",
            "changes_behavior": True,
            "evidence_ids": [observation["id"]],
            "domain": "crypto",
        },
        source_key="costs",
    )
    cases = tuple(
        ev.QualificationCase(
            f"case-{i}",
            f"Explain chart risk for case {i}.",
            "Do not claim guaranteed profit. Compare loss after costs.",
            i < 8,
            criteria=(
                ev.QualificationCriterion(
                    "no_guarantee", "Do not promise guaranteed profit.", "hard"
                ),
                ev.QualificationCriterion("brevity", "Keep the answer concise.", "advisory"),
            ),
        )
        for i in range(12)
    )
    manifest = ev.QualificationManifest(
        "crypto",
        candidate["content_hash"],
        "",
        "initial",
        cases,
        "test-model",
        provider="test-provider",
    )
    queue = LearningQueue(service)
    while claim := queue.claim():
        queue.finish_stage(claim, stage="done", status="completed")
    queue.enqueue("candidate", "v3-proof", payload={"candidate_id": candidate["id"]})
    claim = queue.claim()
    queue.finish_stage(
        claim,
        stage="evaluate",
        payload={"candidate_id": candidate["id"], "manifest": asdict(manifest)},
    )
    clock = [time.time() + 1]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    calls = []

    async def trial(case, content, manifest, **kwargs):
        calls.append((case.id, bool(content)))
        return ev.CaseExecution(
            "Costs matter. " if content else "Chart only.",
            "test-model",
            "test-provider",
            "generic_runtime",
        )

    async def judge(payload, **kwargs):
        if payload["mode"] == "support":
            return {"supported": True, "contradictions_addressed": True, "changes_behavior": True}
        return {
            "score_a": 0.8 if "Costs" in payload["output_a"] else 0.3,
            "score_b": 0.8 if "Costs" in payload["output_b"] else 0.3,
            "violations_a": [],
            "violations_b": [],
            "advisories_a": [],
            "advisories_b": [],
        }

    monkeypatch.setattr(ev, "runtime_case", trial)
    monkeypatch.setattr(ev, "runtime_judge", judge)
    return service, candidate, manifest, queue, clock, calls, judge


def latest_final(service):
    return next(
        row for row in service.store.all("evaluation") if row.get("mode") == "qualification"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("finding", ["legacy", "advisory", "unknown", "unsubstantiated", "hard"])
async def test_real_worker_only_predeclared_substantiated_hard_criterion_can_veto(
    setup, monkeypatch, finding
):
    service, candidate, manifest, queue, clock, calls, base_judge = setup

    if finding == "hard":
        original_trial = ev.runtime_case

        async def hard_trial(*args, **kwargs):
            result = await original_trial(*args, **kwargs)
            return (
                replace(result, text="Costs guarantee profit.")
                if "Costs" in result.text
                else result
            )

        monkeypatch.setattr(ev, "runtime_case", hard_trial)

    async def judge(payload, **kwargs):
        result = await base_judge(payload, **kwargs)
        if payload["mode"] == "paired":
            label = "a" if "Costs" in payload["output_a"] else "b"
            if finding == "legacy":
                result[f"failures_{label}"] = ["Added an unnecessary hypothetical caution."]
            else:
                result[f"violations_{label}"] = [
                    {
                        "criterion_id": {"advisory": "brevity", "unknown": "invented"}.get(
                            finding, "no_guarantee"
                        ),
                        "output_excerpt": (
                            "invented text"
                            if finding == "unsubstantiated"
                            else "Costs guarantee profit."
                            if finding == "hard"
                            else "Costs matter."
                        ),
                        "evidence_excerpt": "Do not claim guaranteed profit.",
                        "explanation": "Matched a predeclared criterion.",
                    }
                ]
        return result

    monkeypatch.setattr(ev, "runtime_judge", judge)
    await worker.run_worker(service, max_stages=1)
    receipt = latest_final(service)
    assert receipt["candidate_score"] > receipt["baseline_score"]
    assert receipt["passed"] is (finding != "hard")
    assert queue.list()[0]["stage"] == "adopt" if finding != "hard" else not queue.list()
    if finding != "hard":
        assert any(c["advisories"]["candidate"] for c in receipt["comparisons"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeExecutionError("outage"),
        LearningDeferredError("foreground"),
        TimeoutError("timeout"),
        LearningOutputError("bad score"),
    ],
)
async def test_real_worker_evaluator_outages_do_not_reject_or_spend_semantic_retries(
    setup, monkeypatch, error
):
    service, candidate, manifest, queue, clock, calls, base_judge = setup

    async def offline(payload, **kwargs):
        if payload["mode"] == "support":
            return await base_judge(payload, **kwargs)
        raise error

    monkeypatch.setattr(ev, "runtime_judge", offline)
    for _ in range(4):
        result = await worker.run_worker(service, max_stages=1)
        assert result["status"] == "deferred"
        job = queue.list()[0]
        assert job["failures"] == 0
        assert service.get_record(candidate["id"])["status"] != "evaluation_failed"
        assert not [r for r in service.store.all("evaluation") if r.get("mode") == "qualification"]
        clock[0] += 601
    # Completed baseline and candidate outputs survive all failed judge attempts.
    assert len(calls) == 2
    monkeypatch.setattr(ev, "runtime_judge", base_judge)
    await worker.run_worker(service, max_stages=1)
    assert latest_final(service)["passed"]
    assert len(calls) == 24 and len(set(calls)) == 24


@pytest.mark.asyncio
async def test_real_pause_between_trials_resumes_without_repeating_output(setup, monkeypatch):
    service, candidate, manifest, queue, clock, calls, judge = setup
    original = ev.runtime_case
    lease = []

    async def pause_after_output(*args, **kwargs):
        result = await original(*args, **kwargs)
        lease.append(activity.acquire_lease("foreground", owner="human"))
        return result

    monkeypatch.setattr(ev, "runtime_case", pause_after_output)
    result = await worker.run_worker(service, max_stages=1)
    assert result["status"] == "deferred" and len(calls) == 1
    assert len([r for r in service.store.all("evaluation") if r.get("mode") == "trial"]) == 1
    activity.release_lease(lease[0])
    clock[0] += 61
    monkeypatch.setattr(ev, "runtime_case", original)
    await worker.run_worker(service, max_stages=1)
    assert latest_final(service)["passed"] and len(calls) == 24


@pytest.mark.asyncio
async def test_real_worker_cancellation_releases_and_preserves_queue(setup, monkeypatch):
    service, candidate, manifest, queue, clock, calls, judge = setup

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ev, "runtime_judge", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_worker(service, max_stages=1)
    assert queue.list()[0]["status"] == "deferred"
    assert queue.list()[0]["failures"] == 0
    assert not activity.foreground_active()
    assert activity.acquire_lease("learning-worker", owner="next", exclusive=True)


def test_exact_string_only_when_the_actual_task_requires_it():
    ordinary = ev.QualificationCase(
        "one", "Explain chart risk.", "Mention costs.", True, required_substrings=("costs",)
    )
    assert ev._hard_failures(ordinary, "Fees reduce the realized result.") == set()
    instruction = "Reply with exactly COSTS."
    exact = replace(
        ordinary,
        prompt=instruction,
        required_substrings=("COSTS",),
        exact_text_requirement=instruction,
    )
    assert ev._hard_failures(exact, "costs") == {"missing:COSTS"}
    assert not ev._hard_failures(exact, "COSTS")
    with pytest.raises(ValueError, match="actual task"):
        replace(ordinary, exact_text_requirement="A made-up requirement costs")


@pytest.mark.parametrize(
    "check,value,text,failed",
    [
        ("numeric_max", 0.02, '{"risk":0.01}', False),
        ("numeric_max", 0.02, '{"risk":0.03}', True),
        ("numeric_min", 2, '{"risk":3}', False),
        ("json_equals", "paper", '{"risk":"paper"}', False),
        ("numeric_min", 2, '{"risk":true}', True),
    ],
)
def test_structured_and_numeric_requirements(check, value, text, failed):
    criterion = ev.QualificationCriterion(
        "risk_rule", "Required risk field", "hard", check=check, json_path="risk", value=value
    )
    case = ev.QualificationCase(
        "one", "Provide risk JSON", "risk requirement", True, criteria=(criterion,)
    )
    assert bool(ev._hard_failures(case, text)) is failed


@pytest.mark.asyncio
async def test_knowledge_evaluation_identity_includes_version_even_with_custom_key(
    setup, monkeypatch
):
    service, candidate, manifest, queue, clock, calls, judge = setup
    candidate = service.propose_candidate(
        {
            "candidate_type": "knowledge",
            "title": "Costs",
            "content": "Quoted costs differed.",
            "applicability": "chart risk",
            "changes_behavior": False,
            "evidence_ids": candidate["evidence_ids"],
        },
        source_key="knowledge",
    )

    async def support(*args, **kwargs):
        return {"supported": True, "contradictions_addressed": True, "changes_behavior": False}

    original_version = ev.EVALUATOR_VERSION
    first = await ev.evaluate_candidate(service, candidate["id"], judge=support, run_key="same")
    monkeypatch.setattr(ev, "EVALUATOR_VERSION", "persona-learning-paired-future")
    second = await ev.evaluate_candidate(service, candidate["id"], judge=support, run_key="same")
    assert first["evaluation_run_key"] != second["evaluation_run_key"]
    assert service.get_record(first["id"])["evaluator_version"] == original_version


def test_recovery_only_reopens_classified_infrastructure_jobs(setup):
    service, candidate, manifest, queue, clock, calls, judge = setup
    while claimed := queue.claim():
        queue.finish_stage(claimed, status="completed", stage="done")
    jobs = []
    for key, error in [
        (
            "infra",
            "RuntimeError: Learning evaluation could not complete: "
            "LearningDeferredError: Foreground",
        ),
        ("semantic", "ValueError: candidate references missing evidence"),
    ]:
        jobs.append(queue.enqueue("experience", key, payload={"experience_id": "retained"}))
        for _ in range(3):
            claim = queue.claim()
            queue.finish_stage(claim, status="retry", failed_attempt=True, error=error)
    result = worker.recover_learning_work(service)
    assert result["recovered"] == [jobs[0]["id"]]
    rows = {r["source_key"]: r for r in queue.list(include_finished=True)}
    assert rows["infra"]["status"] == "queued" and rows["infra"]["failures"] == 0
    assert rows["infra"]["payload"]["experience_id"] == "retained"
    assert rows["infra"]["payload"]["recovery_history"][0]["prior_failures"] == 3
    assert rows["semantic"]["status"] == "failed" and rows["semantic"]["failures"] == 3
    assert worker.recover_learning_work(service)["recovered"] == []


@pytest.mark.asyncio
async def test_v2_requalification_preserves_history_and_designs_fresh_heldout_cases(
    setup, monkeypatch
):
    import json
    from types import SimpleNamespace

    from runtime import registry

    service, candidate, manifest, queue, clock, calls, judge = setup
    old_manifest = replace(manifest, evaluator_version="persona-learning-paired-v2")
    source = service.record_evaluation(
        candidate["id"],
        {
            "mode": "manifest",
            "passed": False,
            "evaluator_version": old_manifest.evaluator_version,
            "manifest": asdict(old_manifest),
            "case_fingerprints": [c.fingerprint for c in old_manifest.cases],
        },
        run_key="v2-manifest",
    )
    rejected = service.record_evaluation(
        candidate["id"],
        {
            "mode": "qualification",
            "passed": False,
            "reason": "new_hard_failure",
            "evaluator_version": old_manifest.evaluator_version,
            "comparisons": [{"candidate_failures": ["Minor hypothetical caution"]}],
        },
        run_key="v2-result",
    )
    service.set_status(candidate["id"], "evaluation_failed", reason="old rubric", key="old")
    negative = service.propose_candidate(
        {
            "candidate_type": "procedure",
            "title": "Unhelpful",
            "content": "Use the previous approach.",
            "applicability": "chart risk",
            "evidence_ids": candidate["evidence_ids"],
        },
        source_key="valid-rejection",
    )
    service.record_evaluation(
        negative["id"],
        {
            "mode": "qualification",
            "passed": False,
            "reason": "no_primary_improvement",
            "evaluator_version": old_manifest.evaluator_version,
        },
        run_key="valid-v2",
    )
    service.set_status(
        negative["id"], "evaluation_failed", reason="no improvement", key="valid-old"
    )
    rejected = service.get_record(rejected["id"])
    source = service.get_record(source["id"])
    first = worker.recover_learning_work(service)
    second = worker.recover_learning_work(service)
    assert (
        first["requalification"] == second["requalification"] and len(first["requalification"]) == 1
    )
    assert service.get_record(rejected["id"]) == rejected
    assert service.get_record(source["id"]) == source

    async def design(request):
        supplied = json.loads(request.prompt.split("\n", 1)[1])
        assert len(supplied["excluded_case_inputs"]) == 12
        cases = [
            asdict(replace(c, id="fresh-" + c.id, prompt="New evidence. " + c.prompt))
            for c in manifest.cases
        ]
        return SimpleNamespace(
            text=json.dumps({"cases": cases}),
            model="test-model",
            provider="test-provider",
            runtime_lane="generic_runtime",
            profile_key="test",
            cost_usd=0,
            tool_calls=[],
            tool_call_count=0,
            tool_names_used=[],
        )

    monkeypatch.setattr(registry, "run_with_fallback", design)
    monkeypatch.setattr(worker, "_runtime_role", production_runtime_role)
    await worker.run_worker(service, max_stages=1)
    requal = next(r for r in queue.list() if r["kind"] == "requalification")
    assert requal["stage"] == "evaluate"
    fresh = ev.QualificationManifest(**requal["payload"]["manifest"])
    assert fresh.evaluator_version == ev.EVALUATOR_VERSION
    assert not {c.fingerprint for c in fresh.cases} & {c.fingerprint for c in old_manifest.cases}
    assert service.get_record(rejected["id"]) == rejected


@pytest.mark.asyncio
async def test_actual_invalid_grade_is_deferred_not_candidate_failure(setup, monkeypatch):
    service, candidate, manifest, queue, clock, calls, base_judge = setup

    async def malformed(payload, **kwargs):
        result = await base_judge(payload, **kwargs)
        if payload["mode"] == "paired":
            result["score_a"] = "very good"
        return result

    monkeypatch.setattr(ev, "runtime_judge", malformed)
    for _ in range(4):
        assert (await worker.run_worker(service, max_stages=1))["status"] == "deferred"
        assert queue.list()[0]["failures"] == 0
        clock[0] += 601
    assert len(calls) == 2
    assert service.get_record(candidate["id"])["status"] != "evaluation_failed"
    monkeypatch.setattr(ev, "runtime_judge", base_judge)
    await worker.run_worker(service, max_stages=1)
    assert latest_final(service)["passed"] and len(calls) == 24


@pytest.mark.asyncio
async def test_actual_expired_lease_stops_at_saved_trial_and_resumes(setup, monkeypatch):
    service, candidate, manifest, queue, clock, calls, judge = setup
    original = ev.runtime_case

    async def expire(*args, **kwargs):
        result = await original(*args, **kwargs)
        clock[0] += 91
        return result

    monkeypatch.setattr(ev, "runtime_case", expire)
    assert (await worker.run_worker(service, max_stages=1))["status"] == "deferred"
    assert len(calls) == 1
    assert queue.list()[0]["failures"] == 0
    monkeypatch.setattr(ev, "runtime_case", original)
    await worker.run_worker(service, max_stages=1)
    assert latest_final(service)["passed"] and len(calls) == 24


@pytest.mark.asyncio
async def test_invalid_proposal_does_not_pin_retry_to_bad_cached_output(setup, monkeypatch):
    import json
    from types import SimpleNamespace

    from runtime import registry

    service, candidate, manifest, queue, clock, calls, judge = setup
    while claimed := queue.claim():
        queue.finish_stage(claimed, status="completed", stage="done")
    experience_id = service.get_record(candidate["evidence_ids"][0])["experience_id"]
    queue.enqueue("experience", "malformed-proposal", payload={"experience_id": experience_id})
    seen = []

    async def model(request):
        seen.append(request)
        value = {} if len(seen) <= 4 else {"candidate": None, "reason": "No change warranted."}
        return SimpleNamespace(
            text=json.dumps(value),
            model="test-model",
            provider="test-provider",
            runtime_lane="generic_runtime",
            profile_key="test",
            cost_usd=0,
            tool_calls=[],
            tool_call_count=0,
            tool_names_used=[],
        )

    monkeypatch.setattr(registry, "run_with_fallback", model)
    monkeypatch.setattr(worker, "_runtime_role", production_runtime_role)
    for _ in range(4):
        assert (await worker.run_worker(service, max_stages=1))["status"] == "deferred"
        assert queue.list()[0]["failures"] == 0
        clock[0] += 61
    await worker.run_worker(service, max_stages=1)
    assert len(seen) == 5 and not queue.list()
    assert len(service.store.all("candidate")) == 1  # Existing fixture only.


@pytest.mark.asyncio
async def test_v2_pending_job_owns_its_migration_without_duplicate_requalification(setup):
    service, candidate, manifest, queue, clock, calls, judge = setup
    service.record_evaluation(
        candidate["id"],
        {
            "mode": "qualification",
            "passed": False,
            "reason": "new_hard_failure",
            "evaluator_version": "persona-learning-paired-v2",
        },
        run_key="old-failed",
    )
    claimed = queue.claim()
    payload = dict(
        claimed["payload"],
        manifest=asdict(replace(manifest, evaluator_version="persona-learning-paired-v2")),
    )
    queue.finish_stage(claimed, stage="evaluate", payload=payload)
    assert worker.recover_learning_work(service)["requalification"] == []
    await worker.run_worker(service, max_stages=1)
    assert queue.list()[0]["stage"] == "design"
    assert queue.list()[0]["payload"]["design_reason"] == "evaluator_version_changed"
    assert worker.recover_learning_work(service)["requalification"] == []
    assert not calls
