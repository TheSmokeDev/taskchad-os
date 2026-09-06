"""Queue/lease tests use only explicitly supplied temporary profile data."""

import asyncio
from types import SimpleNamespace
import json

import pytest

from personas.learning import queue, worker
from personas.learning.worker import _runtime_role as production_runtime_role
from runtime import activity


class StubService:
    def __init__(self, root, name="sales"):
        self.target = SimpleNamespace(persona_id=name, data_dir=root / name)
        self.paused = False

    def enabled(self):
        return not self.paused


def test_inspecting_absent_queue_does_not_create_profile(tmp_path):
    service = StubService(tmp_path)
    assert queue.LearningQueue(service).list() == []
    assert not service.target.data_dir.exists()


def test_idempotent_enqueue_and_profile_isolation(tmp_path):
    sales = queue.LearningQueue(StubService(tmp_path, "sales"))
    crypto = queue.LearningQueue(StubService(tmp_path, "crypto"))
    first = sales.enqueue("experience", "message:1", payload={"experience_id": "e1"}, now=10)
    assert sales.enqueue("experience", "message:1", now=11)["id"] == first["id"]
    assert len(sales.list()) == 1
    assert crypto.list() == []
    assert crypto.enqueue("experience", "message:1", now=12)["id"] != first["id"]


def test_due_priority_crash_recovery_and_stale_checkpoint_refusal(tmp_path):
    jobs = queue.LearningQueue(StubService(tmp_path))
    jobs.enqueue("experience", "e", now=10)
    jobs.enqueue("observation", "due", available_at=100, now=10)
    first = jobs.claim(now=11, ttl_seconds=10)
    assert first["kind"] == "experience"
    assert jobs.claim(now=12) is None
    recovered = jobs.claim(now=22, ttl_seconds=10)
    assert recovered["id"] == first["id"]
    assert recovered["token"] != first["token"]
    with pytest.raises(RuntimeError, match="claim lost"):
        jobs.finish_stage(first, now=23, status="completed")
    jobs.finish_stage(recovered, now=23, status="completed")
    assert jobs.claim(now=99) is None
    assert jobs.claim(now=100)["kind"] == "observation"


def test_failures_are_bounded_and_deferral_does_not_spend_attempt(tmp_path):
    jobs = queue.LearningQueue(StubService(tmp_path))
    jobs.enqueue("experience", "e", now=10)
    claimed = jobs.claim(now=11)
    deferred = jobs.finish_stage(claimed, now=12, status="deferred", delay_seconds=30)
    assert deferred["failures"] == 0
    assert jobs.claim(now=41) is None
    for attempt in range(3):
        claimed = jobs.claim(now=42 + attempt)
        result = jobs.finish_stage(claimed, now=42 + attempt, status="retry", failed_attempt=True)
    assert result["status"] == "failed"
    assert jobs.claim(now=100) is None


def test_foreground_leases_expire_and_background_lock_is_install_wide(tmp_path):
    path = tmp_path / "activity.db"
    first = activity.acquire_lease("foreground", owner="sales", ttl_seconds=10, now=10, path=path)
    assert activity.foreground_active(now=11, path=path)
    assert not activity.foreground_active(now=20, path=path)
    assert not activity.renew_lease(first, now=21, path=path)
    one = activity.acquire_lease("learning-worker", owner="sales", exclusive=True, now=30, path=path)
    assert one
    assert activity.acquire_lease("learning-worker", owner="crypto", exclusive=True, now=31, path=path) is None
    assert activity.acquire_lease("learning-worker", owner="crypto", exclusive=True, now=121, path=path)


def test_worker_yields_between_stages_and_resumes_checkpoint(tmp_path):
    service = StubService(tmp_path)
    jobs = queue.LearningQueue(service)
    jobs.enqueue("experience", "e")
    path = tmp_path / "activity.db"
    visits = []
    foreground = []
    async def process(_service, job):
        visits.append(job["stage"])
        if job["stage"] == "propose":
            foreground.append(activity.acquire_lease("foreground", owner="interactive", path=path))
            return "evaluate", {"candidate_id": "persisted"}
        assert job["payload"]["candidate_id"] == "persisted"
        return "done", job["payload"]
    first = asyncio.run(worker.run_worker(service, processor=process, activity_path=path))
    assert first == {"status": "deferred", "stages": 1}
    activity.release_lease(foreground[0], path=path)
    second = asyncio.run(worker.run_worker(service, processor=process, activity_path=path))
    assert second == {"status": "idle", "stages": 1}
    assert visits == ["propose", "evaluate"]
    assert jobs.list() == []


def test_paused_and_empty_workers_never_call_processor(tmp_path):
    service = StubService(tmp_path)
    async def fail(*args):
        raise AssertionError("no model work expected")
    assert asyncio.run(worker.run_worker(service, processor=fail))["status"] == "idle"
    service.paused = True
    assert asyncio.run(worker.run_worker(service, processor=fail))["status"] == "disabled"


def test_foreground_wrapper_releases_on_cancellation_and_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_ACTIVITY_DB", str(tmp_path / "activity.db"))
    request = SimpleNamespace(conversational=True, workload="auto", task_name="chat")
    async def run():
        with pytest.raises(asyncio.CancelledError):
            async with activity.foreground_request(request):
                assert activity.foreground_active()
                raise asyncio.CancelledError()
        assert not activity.foreground_active()
        monkeypatch.setattr(activity, "acquire_lease", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
        async with activity.foreground_request(request):
            return "ordinary request still executes"
    assert asyncio.run(run()) == "ordinary request still executes"


def test_dry_run_wakes_never_resolve_profiles_or_write(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "discover_work", lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert asyncio.run(worker.wake_learning(test_mode=True))["status"] == "dry_run"
    assert worker.run_pending_profiles(test_mode=True)["status"] == "dry_run"


def test_simulation_execution_notification_never_reinforces_real_work(tmp_path):
    service = StubService(tmp_path)
    service.get_record = lambda _: {"id": "practice", "mode": "practice"}
    queue.notify_record(service, {"kind": "execution", "id": "result", "experience_id": "practice"})
    assert queue.LearningQueue(service).list() == []


def real_service(tmp_path):
    from personas.learning.models import LearningTarget
    from personas.learning.service import LearningService
    target = LearningTarget("sales", tmp_path / "memory", tmp_path / "data",
                            tmp_path / "state", tmp_path / "skills")
    target.memory_dir.mkdir()
    (target.memory_dir / "MEMORY.md").write_text("# Knowledge\n", encoding="utf-8")
    return LearningService(target)


def test_real_worker_proposes_evaluates_promotes_without_reinforcing_practice(tmp_path, monkeypatch):
    """Only model inference is substituted; queue, records and application are real."""
    from personas.learning import evaluation
    from runtime import registry

    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_ACTIVITY_DB", str(tmp_path / "activity.db"))
    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "true")
    service = real_service(tmp_path)
    experience = service.capture_experience("real-conversation", "test", "Understand a prospect's objection")
    observation = service.record_observation(experience["id"], {
        "status": "resolved", "quality": "direct", "evidence": "Prospect clarified value after discovery.",
    }, source_key="inbound")
    roles = []
    async def model(request):
        assert request.model_only and request.disallowed_tools == ["*"] and request.workload == "learning"
        roles.append(request.task_name)
        if request.task_name.endswith("propose"):
            value = {"candidate": {"candidate_type": "procedure", "title": "Value discovery",
                     "content": "Ask a diagnostic question before offering a discount.",
                     "applicability": "Prospect raises price before explaining expected value",
                     "evidence_ids": [observation["id"]], "counterevidence_ids": [],
                     "changes_behavior": True, "uncertainty": "one observed conversation", "domain": "sales"}}
        else:
            value = {"cases": [{"id": f"heldout-{i}", "prompt": f"Prospect scenario {i}",
                     "expected": "Discover unclear value while respecting already stated needs",
                     "applicable": i < 8} for i in range(12)]}
        return SimpleNamespace(text=json.dumps(value), model="model-a", provider="test-model",
            runtime_lane="generic_runtime", profile_key="test-profile", cost_usd=0,
            tool_calls=[], tool_call_count=0, tool_names_used=[])
    async def run_case(case, content, manifest, *, cwd):
        return evaluation.CaseExecution(content, "model-a", "test-model", "generic_runtime")
    async def judge(payload, **kwargs):
        if payload["mode"] == "support":
            return {"supported": True, "contradictions_addressed": True,
                    "changes_behavior": True, "reason": "supported by provided conversation"}
        return {"score_a": 0.9 if "diagnostic" in payload["output_a"] else 0.2,
                "score_b": 0.9 if "diagnostic" in payload["output_b"] else 0.2,
                "failures_a": [], "failures_b": [], "reason": "domain rubric"}
    monkeypatch.setattr(registry, "run_with_fallback", model)
    monkeypatch.setattr(worker, "_runtime_role", production_runtime_role)
    monkeypatch.setattr(evaluation, "runtime_case", run_case)
    monkeypatch.setattr(evaluation, "runtime_judge", judge)
    result = asyncio.run(worker.run_worker(service))
    assert result["status"] == "idle"
    assert roles == ["persona_learning_propose", "persona_learning_design"]
    activations = service.store.all("activation")
    assert len(activations) == 1
    assert activations[0]["method_status"] == "active_provisional"
    assert "diagnostic" in service.render_context("A prospect objects to price").text
    assert len(service.store.all("candidate")) == 1
    worker.discover_work(service)
    assert queue.LearningQueue(service).list() == []


def test_observation_stage_persists_and_deduplicates_collection_timestamp(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from personas.learning import observers

    service = real_service(tmp_path)
    experience = service.capture_experience("mail", "test", "Await a prospect reply")
    expectation = service.commit_expectation(experience["id"], {"claim": "Prospect replies",
        "check_by": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "resolution_rule": "A verified inbound reply", "situation": {"domain": "sales"}})
    calls = []
    async def collect(_service, expected):
        calls.append(1)
        stamp = (datetime.now(timezone.utc) + timedelta(seconds=len(calls))).isoformat()
        return {"quality": "direct", "status": "resolved", "expectation_id": expected["id"],
                "occurred_at": stamp, "evidence": {"status": "no_reply", "collected_at": stamp}}
    monkeypatch.setattr(observers, "collect_due_observation", collect)
    job = {"id": "observe", "kind": "observation", "stage": "observe", "payload": {
        "experience_id": experience["id"], "expectation_id": expectation["id"]}}
    one = asyncio.run(worker.process_stage(service, job))
    two = asyncio.run(worker.process_stage(service, job))
    assert one == two
    assert len(service.store.all("observation")) == 1
    earlier = service.store.all("observation")[0]
    cutoff = datetime.fromisoformat(earlier["evidence"]["collected_at"])
    async def delayed_reply(_service, expected):
        return {"quality": "direct", "status": "resolved", "expectation_id": expected["id"],
                "evidence": {"status": "replied", "collected_at": (cutoff + timedelta(minutes=2)).isoformat(),
                    "messages": [{"id": "late", "occurred_at": (cutoff + timedelta(minutes=1)).isoformat()}]}}
    monkeypatch.setattr(observers, "collect_due_observation", delayed_reply)
    asyncio.run(worker.process_stage(service, job))
    assert len(service.store.all("observation")) == 2
    assert service.get_record(earlier["id"])["status"] == "resolved"
    assert "supersedes" not in service.store.all("observation")[0]


def test_worker_time_budget_cancels_safely_and_keeps_stage_resumable(tmp_path):
    service = StubService(tmp_path)
    jobs = queue.LearningQueue(service)
    jobs.enqueue("experience", "slow")
    cancelled = []
    async def slow(*args):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    result = asyncio.run(worker.run_worker(service, processor=slow,
        activity_path=tmp_path / "activity.db", stage_timeout_seconds=0.01))
    assert result["status"] == "deferred"
    assert cancelled == [True]
    assert jobs.list()[0]["failures"] == 0
    assert jobs.list()[0]["stage"] == "propose"


def test_pause_during_a_stage_does_not_spend_a_retry(tmp_path):
    service = StubService(tmp_path)
    jobs = queue.LearningQueue(service)
    jobs.enqueue("experience", "pause")
    async def pause(*args):
        service.paused = True
        raise ValueError("write refused by paused service")
    result = asyncio.run(worker.run_worker(service, processor=pause, activity_path=tmp_path / "activity.db"))
    assert result["status"] == "deferred"
    assert jobs.list()[0]["failures"] == 0


def test_actual_runtime_switch_queues_requalification_once(tmp_path):
    service = StubService(tmp_path)
    records = {"experience": {"id": "experience", "mode": "real"},
               "active": {"id": "active", "status": "active_provisional", "candidate_id": "method"}}
    service.get_record = lambda key: records.get(key)
    service.store = SimpleNamespace(all=lambda kind: [
        {"candidate_id": "method", "passed": True, "model": "old-model", "provider": "old-provider"}])
    execution = {"kind": "execution", "id": "exec-1", "experience_id": "experience",
                 "model": "new-model", "provider": "new-provider", "runtime_lane": "generic_runtime",
                 "included_activation_ids": ["active"]}
    queue.notify_record(service, execution)
    queue.notify_record(service, {**execution, "id": "exec-2"})
    requal = [j for j in queue.LearningQueue(service).list() if j["kind"] == "requalification"]
    assert len(requal) == 1
    assert requal[0]["payload"]["target_runtime"]["model"] == "new-model"


def test_completed_reflection_survives_learning_seam_failure(tmp_path, monkeypatch):
    import memory_reflect
    from contextlib import nullcontext
    async def reflection(*args):
        return "existing reflection completed"
    async def unavailable(**kwargs):
        raise RuntimeError("learning package unavailable")
    monkeypatch.setattr(memory_reflect, "file_lock", lambda *args, **kw: nullcontext())
    monkeypatch.setattr(memory_reflect, "_run_reflection_inner", reflection)
    monkeypatch.setattr(worker, "wake_learning", unavailable)
    assert asyncio.run(memory_reflect.run_reflection()) == "existing reflection completed"


def test_existing_queue_inspection_is_read_only_and_validates_owner_and_job_keys(tmp_path):
    import sqlite3
    from personas.learning.models import LearningError
    jobs = queue.LearningQueue(StubService(tmp_path))
    job = jobs.enqueue("experience", "event", payload={"experience_id": "experience"})
    before = (jobs.path.read_bytes(), jobs.path.stat().st_mtime_ns,
              sorted(p.name for p in jobs.path.parent.iterdir()))
    assert jobs.list()[0]["id"] == job["id"]
    assert before == (jobs.path.read_bytes(), jobs.path.stat().st_mtime_ns,
                      sorted(p.name for p in jobs.path.parent.iterdir()))
    foreign = StubService(tmp_path, "crypto")
    foreign.target.data_dir = jobs.root
    with pytest.raises(LearningError, match="another profile"):
        queue.LearningQueue(foreign).list()
    with sqlite3.connect(jobs.path) as db:
        db.execute("UPDATE learning_jobs SET source_key='tampered'")
    with pytest.raises(LearningError, match="job identity"):
        jobs.list()


def test_existing_unknown_database_is_not_initialized_by_inspection(tmp_path):
    import sqlite3
    from personas.learning.models import LearningError
    jobs = queue.LearningQueue(StubService(tmp_path))
    jobs.path.parent.mkdir(parents=True)
    with sqlite3.connect(jobs.path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
    original = jobs.path.read_bytes()
    with pytest.raises(LearningError, match="schema"):
        jobs.list()
    assert jobs.path.read_bytes() == original


def test_queue_rejects_credential_payload_before_creating_files(tmp_path):
    from personas.learning.models import LearningError
    jobs = queue.LearningQueue(StubService(tmp_path))
    with pytest.raises(LearningError, match="credentials"):
        jobs.enqueue("experience", "event", payload={"nested": {"access_token": "secret"}})
    assert not jobs.path.exists()


def test_host_observed_paper_settlement_learns_but_generated_practice_does_not(tmp_path):
    service = real_service(tmp_path)
    paper = service.capture_experience("call:1", "crypto_paper", "Paper decision", mode="practice",
        metadata={"practice_origin": "host_observed", "source_receipt_id": "call-1"})
    service.record_execution(paper["id"], {"success": True}, attempt_key="accepted")
    worker.discover_work(service)
    assert queue.LearningQueue(service).list() == []
    service.record_observation(paper["id"], {"status": "resolved", "quality": "direct",
        "evidence": {"call_id": "call-1", "simulated": True, "settlement": "market_closed"}}, source_key="market")
    jobs = queue.LearningQueue(service).list()
    assert len(jobs) == 1 and jobs[0]["payload"]["experience_id"] == paper["id"]
    assert any(r["kind"] == "observation" for r in worker._evidence(service, paper["id"]))
    assert all(r.get("mode", "practice") == "practice" for r in worker._evidence(service, paper["id"]))
    simulated = service.capture_experience("case:1", "learning_worker", "Generated task", mode="practice",
        metadata={"learning_role": "propose", "practice_origin": "host_observed", "source_receipt_id": "fake"})
    service.record_observation(simulated["id"], {"status": "resolved", "quality": "direct",
        "evidence": {"call_id": "fake", "simulated": True}}, source_key="synthetic")
    worker.discover_work(service)
    assert not any(j["payload"].get("experience_id") == simulated["id"] for j in queue.LearningQueue(service).list())
    assert worker._evidence(service, simulated["id"]) == []


@pytest.mark.parametrize("maximum", [0, -1, 1.5, True, 65])
def test_worker_rejects_unbounded_stage_work(tmp_path, maximum):
    with pytest.raises(ValueError, match="stage count"):
        asyncio.run(worker.run_worker(StubService(tmp_path), max_stages=maximum))


def _fake_learning_models(monkeypatch, service):
    """Substitute inference only; proposals, qualification and adoption remain real."""
    from personas.learning import evaluation
    from runtime import registry
    state = {"designs": 0, "model": "model-a", "provider": "test-model", "budgets": [], "design_inputs": []}
    async def model(request):
        state["budgets"].append(request.max_budget_usd)
        if request.task_name.endswith("propose"):
            evidence = json.loads(request.prompt.split("\n", 1)[1])["evidence"]
            observed = next(r for r in evidence if r["kind"] == "observation")
            value = {"candidate": {"candidate_type": "procedure", "title": "Value discovery",
                "content": "Ask a diagnostic question before offering a discount.",
                "applicability": "Prospect raises price before explaining expected value",
                "evidence_ids": [observed["id"]], "counterevidence_ids": [],
                "changes_behavior": True, "uncertainty": "one observation", "domain": "sales"}}
        else:
            state["designs"] += 1
            state["design_inputs"].append(json.loads(request.prompt.split("\n", 1)[1]))
            value = {"cases": [{"id": f"fresh-{state['designs']}-{i}",
                "prompt": f"Prospect price objection batch {state['designs']} scenario {i}",
                "expected": "Discover unclear value respecting prior needs", "applicable": i < 8} for i in range(12)]}
        return SimpleNamespace(text=json.dumps(value), model=state["model"], provider=state["provider"],
            runtime_lane="generic_runtime", profile_key="test-profile", cost_usd=0,
            tool_calls=[], tool_call_count=0, tool_names_used=[])
    async def run_case(case, content, manifest, *, cwd):
        return evaluation.CaseExecution(content, manifest.model, manifest.provider, manifest.runtime_lane)
    async def judge(payload, **kwargs):
        if payload["mode"] == "support":
            return {"supported": True, "contradictions_addressed": True,
                    "changes_behavior": True, "reason": "backed by source"}
        return {"score_a": .9 if "diagnostic" in payload["output_a"] else .2,
                "score_b": .9 if "diagnostic" in payload["output_b"] else .2,
                "failures_a": [], "failures_b": [], "reason": "task quality"}
    monkeypatch.setattr(registry, "run_with_fallback", model)
    monkeypatch.setattr(worker, "_runtime_role", production_runtime_role)
    monkeypatch.setattr(evaluation, "runtime_case", run_case)
    monkeypatch.setattr(evaluation, "runtime_judge", judge)
    return state


@pytest.mark.parametrize("repeat_used_cases", [False, True])
def test_adoption_context_drift_redesigns_and_eventually_adopts(tmp_path, monkeypatch, repeat_used_cases):
    """Real queue, inference cache, frozen trials and ledgers; only models are fake."""
    from personas.learning import evaluation, promotion

    service = real_service(tmp_path)
    state = _fake_learning_models(monkeypatch, service)
    exp = service.capture_experience("fresh-sale", "sales", "Handle a price objection")
    observed = service.record_observation(exp["id"], {
        "quality": "direct", "status": "resolved", "evidence": "Prospect clarified expected value."
    }, source_key="reply")
    first = asyncio.run(worker.run_worker(service, max_stages=3))
    assert first["stages"] == 3
    pending = queue.LearningQueue(service).list()[0]
    assert pending["stage"] == "adopt"
    original_evaluation = pending["payload"]["evaluation_id"]
    candidate_id = pending["payload"]["candidate_id"]

    incumbent = service.propose_candidate({
        "candidate_type": "knowledge", "title": "Price delivery evidence",
        "content": "The prospect confirmed delivery dates separately.", "applicability": "price",
        "changes_behavior": False, "evidence_ids": [observed["id"]],
        "counterevidence_ids": [], "worker_job_id": "independent-learning-worker",
    }, source_key="independent-incumbent")
    async def source_judge(payload, **kwargs):
        return {"supported": True, "contradictions_addressed": True, "changes_behavior": False}
    support = asyncio.run(evaluation.evaluate_candidate(service, incumbent["id"], judge=source_judge))
    promotion.promote_candidate(service, incumbent["id"], support["id"])

    retried = asyncio.run(worker.run_worker(service, max_stages=1))
    assert retried["stages"] == 1
    redesign = queue.LearningQueue(service).list()[0]
    assert redesign["stage"] == "design" and redesign["payload"]["design_revision"] == 1
    assert redesign["payload"]["candidate_id"] == candidate_id
    assert redesign["payload"]["design_reason"] == "qualification_deployed_context_changed"
    assert "manifest" not in redesign["payload"] and "evaluation_id" not in redesign["payload"]
    assert not any(a["candidate_id"] == candidate_id for a in service.store.all("activation"))

    if repeat_used_cases:
        # Simulate a designer ignoring the exclusion list once; host validation
        # must retire that cache result and request a fresh design automatically.
        state["designs"] = 0
        repeated = asyncio.run(worker.run_worker(service, max_stages=1))
        assert repeated["stages"] == 1
        next_design = queue.LearningQueue(service).list()[0]
        assert next_design["stage"] == "design"
        assert next_design["payload"]["design_revision"] == 2
        assert next_design["payload"]["design_reason"] == "qualification_case_previously_exposed"

    finished = asyncio.run(worker.run_worker(service))
    assert finished["status"] == "idle"
    activated = next(a for a in service.store.all("activation") if a["candidate_id"] == candidate_id)
    assert activated["evaluation_id"] != original_evaluation
    assert state["designs"] == 2
    assert len(state["design_inputs"]) == (3 if repeat_used_cases else 2)
    assert len(state["design_inputs"][-1]["excluded_case_inputs"]) == 12
    assert "diagnostic" in service.render_context("Prospect raises price").text
    assert incumbent["content"] in service.render_context("Prospect raises price").text


def test_due_observation_to_adoption_and_model_requalification_use_real_pipeline(tmp_path, monkeypatch):
    """Local mail fixture substitutes the provider read; no inbox/provider is accessed."""
    from integrations import gmail
    service = real_service(tmp_path)
    state = _fake_learning_models(monkeypatch, service)
    monkeypatch.setenv("PERSONA_LEARNING_MODEL_BUDGET_USD", "0.04")
    experience = service.capture_experience("historical-message", "sales", "Understand a price objection", mode="backfill")
    service.commit_expectation(experience["id"], {"phase": "retrospective", "domain": "sales",
        "claim": "Prospect replies", "check_by": "2020-01-01T00:00:00+00:00",
        "resolution_rule": "Verified inbound response", "situation": {"observer": {
            "provider": "gmail", "thread_id": "thread", "outbound_id": "message",
            "recipient_email": "prospect@example.invalid", "mailbox_id": "sales@example.invalid"}}})
    monkeypatch.setattr(gmail, "observe_inbound_response", lambda **kw: {
        "status": "replied", "complete": True, "messages": [{"id": "reply", "snippet": "Now I understand the value"}],
        "collected_at": kw["collected_at"]})
    result = asyncio.run(worker.run_worker(service))
    assert result["status"] == "idle"
    active = service.store.all("activation")[0]
    assert active["status"] == "active_provisional"
    assert state["budgets"] == [.04, .04]
    current = service.capture_experience("fresh-message", "sales", "Discover value")
    service.record_execution(current["id"], {"success": True, "model": "model-b", "provider": "new-model",
        "runtime_lane": "generic_runtime", "included_activation_ids": [active["id"]]}, attempt_key="actual-new-model")
    # Stop before unrelated ordinary-experience proposal work.
    jobs = queue.LearningQueue(service)
    requal = next(j for j in jobs.list() if j["kind"] == "requalification")
    state.update(model="model-b", provider="new-model")
    # Requalification reuses the physical method in its evaluation checkpoint.
    result = asyncio.run(worker.run_worker(service, max_stages=2))
    assert result["stages"] == 2
    updated = [a for a in service.store.all("activation") if a["status"] == "active_provisional"]
    assert len(updated) == 1
    assert set(updated[0]["qualified_models"]) == {"model-a", "model-b"}
    assert service.get_record(active["id"])["status"] == "superseded"
    assert next(j for j in jobs.list(include_finished=True) if j["id"] == requal["id"])["status"] == "completed"


def test_corrected_outcome_retires_method_and_preserves_revision_lineage(tmp_path, monkeypatch):
    service = real_service(tmp_path)
    _fake_learning_models(monkeypatch, service)
    experience = service.capture_experience("case", "sales", "Understand a price objection")
    original = service.record_observation(experience["id"], {"quality": "direct", "status": "resolved",
        "evidence": "Prospect replied positively"}, source_key="reply")
    asyncio.run(worker.run_worker(service))
    active = service.store.all("activation")[0]
    correction = service.record_observation(experience["id"], {"quality": "direct", "status": "resolved",
        "evidence": "The reply belonged to another conversation", "supersedes": original["id"]}, source_key="correction")
    # First checkpoint retires the invalid physical method; second creates revision.
    result = asyncio.run(worker.run_worker(service, max_stages=2))
    assert result["stages"] == 2
    assert service.get_record(active["id"])["status"] == "rolled_back"
    assert "diagnostic" not in service.render_context("price objection").text
    revision = next(c for c in service.store.all("candidate") if c["id"] != active["candidate_id"])
    assert revision["prior_candidate_id"] == active["candidate_id"]
    assert revision["evidence_ids"] == [correction["id"]]
    assert "diagnostic" not in revision["baseline_content"]
    before = queue.LearningQueue(service).list(include_finished=True)
    worker.discover_work(service)
    after = queue.LearningQueue(service).list(include_finished=True)
    assert {j["id"] for j in before} == {j["id"] for j in after}


def test_profile_child_preserves_operator_learning_budgets(tmp_path, monkeypatch):
    import subprocess
    from personas import activity as persona_activity, lifecycle, capabilities
    from personas.learning import service as learning_service
    service = StubService(tmp_path)
    queue.LearningQueue(service).enqueue("experience", "pending")
    monkeypatch.setattr(persona_activity, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(lifecycle, "list_profiles", lambda: [SimpleNamespace(name="sales", path=tmp_path / "sales")])
    monkeypatch.setattr(learning_service, "get_learning_service", lambda *_: service)
    monkeypatch.setattr(worker, "discover_work", lambda *_: 0)
    monkeypatch.setattr(capabilities, "build_capability_scoped_env", lambda *a, **kw: {})
    monkeypatch.setenv("PERSONA_LEARNING_MODEL_BUDGET_USD", ".03")
    captured = []
    def child(command, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", child)
    assert worker.run_pending_profiles(once=True)["attempted"] == ["sales"]
    assert captured[0]["env"]["PERSONA_LEARNING_MODEL_BUDGET_USD"] == ".03"
