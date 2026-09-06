"""Controlled qualification/adoption proof; never calls providers or live profiles."""
from dataclasses import asdict, replace
import json

import pytest

from personas.learning import evaluation as ev
from personas.learning.evaluation import runtime_reasoning as real_runtime_reasoning
from personas.learning.models import LearningError, LearningTarget
from personas.learning.promotion import promote_candidate, rollback_activation, reassess_activation
from personas.learning.service import LearningService


@pytest.fixture
def service(tmp_path, monkeypatch):
    target = LearningTarget("sales", tmp_path / "memory", tmp_path / "data",
                            tmp_path / "state", tmp_path / "skills")
    target.memory_dir.mkdir()
    (target.memory_dir / "MEMORY.md").write_text("# Knowledge\n", encoding="utf-8")
    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "true")
    monkeypatch.delenv("HOMIE_KILLSWITCH_SKILL_PROMOTION", raising=False)
    return LearningService(target)


def candidate(service, *, kind="procedure", changes_behavior=True, content="Ask a diagnostic question before discussing a discount.", prior_candidate_id=None, baseline_version="initial"):
    experience = service.capture_experience("sales-1", "test", "Handle a price objection")
    observation = service.record_observation(experience["id"], {
        "evidence": "Prospect explained their needs after a diagnostic question.",
        "quality": "direct", "status": "resolved", "held": True,
    }, source_key="reply-1")
    return service.propose_candidate({"candidate_type": kind, "title": "Price discovery",
        "content": content, "applicability": "price objection before value is established",
        "changes_behavior": changes_behavior, "evidence_ids": [observation["id"]],
        "counterevidence_ids": [], "baseline_version": baseline_version, "domain": "sales",
        "prior_candidate_id": prior_candidate_id,
    }, source_key=kind + content)


def manifest(c, **kwargs):
    return ev.QualificationManifest(profile_id="sales", candidate_hash=c["content_hash"],
        baseline_content="Offer a discount immediately.", baseline_version="initial",
        cases=tuple(ev.QualificationCase(f"q-{i}", f"Prospect {i} asks about price.",
                    "Explore value before discussing discount; respect already known needs.", i < 8)
                    for i in range(12)), model="model-a", provider="openai-compatible", **kwargs)


async def execute(case, content, manifest, *, cwd):
    return ev.CaseExecution(content, "model-a", "openai-compatible", "generic_runtime")


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_frozen_trials_match_actual_deployed_bundle_with_incumbents(service, monkeypatch, replacement):
    """Real ledgers and promotion; only model inference is faked."""
    async def seed(content):
        c = candidate(service, content=content)
        original = manifest(c)
        m = replace(original, cases=tuple(replace(case, id=content[:4] + case.id,
            prompt=content[:4] + " " + case.prompt) for case in original.cases))
        async def seed_judge(payload, **kwargs):
            if payload["mode"] == "support":
                return {"supported": True, "contradictions_addressed": True, "changes_behavior": True}
            return {"score_a": .9 if content in payload["output_a"] else .1,
                "score_b": .9 if content in payload["output_b"] else .1,
                "failures_a": [], "failures_b": []}
        result = await ev.evaluate_candidate(service, c["id"], manifest=m, run_case=execute, judge=seed_judge)
        assert result["passed"]
        promote_candidate(service, c["id"], result["id"])
        return c

    # Identical labels do not establish replacement authority.
    incumbent = await seed("Offer a discount immediately.")
    unrelated = await seed("Confirm the promised delivery date.")
    c = candidate(service, prior_candidate_id=incumbent["id"] if replacement else None)
    m = ev.freeze_context_bundles(service, c, manifest(c))
    snapshots = {b["case_id"]: b for b in m.context_bundles}
    for b in m.context_bundles:
        assert incumbent["content"] in b["baseline"]["text"]
        assert unrelated["content"] in b["baseline"]["text"]
        assert unrelated["content"] in b["candidate"]["text"]
        assert (incumbent["content"] in b["candidate"]["text"]) is not replacement
        assert len(b["candidate"]["text"]) <= m.context_max_chars == 2000

    calls = []
    async def inference(case, content, manifest, **kwargs):
        assert content in {snapshots[case.id][v]["text"] for v in ("baseline", "candidate")}
        calls.append((case.id, content))
        return await execute(case, content, manifest, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(service, "preview_context", lambda *a, **kw: pytest.fail("live context lookup after freeze"))
        result = await ev.evaluate_candidate(service, c["id"], manifest=m, run_case=inference, judge=judge)
    assert result["passed"] and len(calls) == 24
    activation = promote_candidate(service, c["id"], result["id"])
    assert activation["method_status"] == "active_provisional"
    for case in m.cases:
        assert service.render_context(case.prompt, max_chars=m.context_max_chars).text == snapshots[case.id]["candidate"]["text"]
    assert {row["context_bundle_hash"] for row in result["comparisons"]} == {
        ev.canonical_hash(b) for b in m.context_bundles}


@pytest.mark.asyncio
async def test_context_budget_rejects_candidate_that_deployment_cannot_deliver(service):
    c = candidate(service, content="Ask a diagnostic question. " * 40)
    m = replace(manifest(c), context_max_chars=300)
    result = await ev.evaluate_candidate(service, c["id"], manifest=m, run_case=execute, judge=judge)
    assert not result["passed"] and result["reason"] == "no_primary_improvement"
    assert result["baseline_score"] == result["candidate_score"]


@pytest.mark.asyncio
async def test_case_selection_uses_visible_context_but_never_hidden_rubric(service):
    c = candidate(service)
    original = manifest(c)
    cases = tuple(replace(case, prompt=f"Handle request {i}",
        context="The prospect raises a price objection." if i < 8 else "The appointment date is confirmed.",
        expected="Ask diagnostic price discovery questions when appropriate.")
        for i, case in enumerate(original.cases))
    m = ev.freeze_context_bundles(service, c, replace(original, cases=cases))
    assert all(c["content"] in b["candidate"]["text"] for b in m.context_bundles[:8])
    assert all(not b["candidate"]["text"] for b in m.context_bundles[8:])
    result = await ev.evaluate_candidate(service, c["id"], manifest=m, run_case=execute, judge=judge)
    assert result["passed"]
    promote_candidate(service, c["id"], result["id"])
    for b in m.context_bundles:
        assert service.render_context(b["task"]).text == b["candidate"]["text"]


@pytest.mark.asyncio
async def test_bundle_tampering_and_context_drift_cannot_authorize_promotion(service):
    c = candidate(service)
    m = ev.freeze_context_bundles(service, c, manifest(c))
    changed = json.loads(json.dumps(asdict(m)))
    changed["context_bundles"][0]["candidate"]["text"] = c["content"]
    rejected = await ev.evaluate_candidate(service, c["id"], manifest=changed, run_case=execute, judge=judge)
    assert not rejected["passed"] and "context_bundle_render_mismatch" in rejected["errors"][0]
    # Fresh cases for a valid qualification, then a real intervening activation.
    fresh = replace(manifest(c), cases=tuple(replace(case, id="fresh-" + case.id,
        prompt="Fresh " + case.prompt) for case in manifest(c).cases))
    result = await ev.evaluate_candidate(service, c["id"], manifest=fresh, run_case=execute, judge=judge)
    later = candidate(service, kind="knowledge", changes_behavior=False, content="Delivery dates are discussed before price.")
    support = await ev.evaluate_candidate(service, later["id"], judge=judge)
    promote_candidate(service, later["id"], support["id"])
    with pytest.raises(LearningError, match="qualification_deployed_context_changed"):
        promote_candidate(service, c["id"], result["id"])


@pytest.mark.asyncio
async def test_background_native_quality_hint_preserves_generic_configured_model(service, monkeypatch):
    from types import SimpleNamespace
    from runtime import lane_router
    requests = []
    async def fake(request):
        requests.append(request)
        return SimpleNamespace(text="ok", model="observed-model", provider="test",
            runtime_lane="generic_runtime", cost_usd=0, profile_key="test",
            tool_calls=[], tool_call_count=0, tool_names_used=[])
    monkeypatch.setenv("SECOND_BRAIN_BACKGROUND_QUALITY_MODEL", "quality-model")
    monkeypatch.setattr(lane_router, "run_with_runtime_lanes", fake)
    await real_runtime_reasoning("task", cwd=service.target.memory_dir, runtime_lane="claude_native", provider="claude")
    await real_runtime_reasoning("task", cwd=service.target.memory_dir, runtime_lane="generic_runtime", provider="kimi")
    await real_runtime_reasoning("task", cwd=service.target.memory_dir, runtime_lane="generic_runtime", provider="kimi", model="pinned-model")
    assert [r.model for r in requests] == ["quality-model", None, "pinned-model"]


async def judge(payload, **kwargs):
    if payload["mode"] == "support":
        return {"supported": True, "contradictions_addressed": True,
                "changes_behavior": payload["candidate"]["changes_behavior"],
                "reason": "direct conversation evidence"}
    return {"score_a": 0.9 if "diagnostic" in payload["output_a"] else 0.3,
            "score_b": 0.9 if "diagnostic" in payload["output_b"] else 0.3,
            "failures_a": [], "failures_b": [], "reason": "consistent rubric"}


@pytest.mark.asyncio
async def test_qualifies_adopts_reuses_and_rolls_back_real_skill(service):
    c = candidate(service)
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=execute, judge=judge)
    assert result["passed"] is True
    assert len(result["comparisons"]) == 12
    activation = promote_candidate(service, c["id"], result["id"])
    assert activation["method_status"] == "active_provisional"
    promoted = service.target.skills_dir / "promoted" / activation["skill_name"] / "SKILL.md"
    assert promoted.is_file()
    assert "diagnostic" in promoted.read_text()
    assert c["content"] in service.render_context("A prospect has a price objection", model="model-a").text
    from cognition.skill_usage import get_usage
    usage = get_usage(activation["skill_name"], sidecar_path=service.target.data_dir / "skill_usage.json")
    assert usage.recurrence_count == 1
    assert usage.assigned_personas == ["sales"]
    assert usage.state == "promoted"
    again = promote_candidate(service, c["id"], result["id"])
    assert again["id"] == activation["id"]
    undone = rollback_activation(service, activation["id"], reason="new comparable performance regressed")
    assert undone["status"] == "rolled_back"
    assert not promoted.exists()
    assert not service.render_context("price objection").text


@pytest.mark.asyncio
async def test_supported_knowledge_uses_existing_amendment_ledger_and_low_confidence_is_irrelevant(service):
    c = candidate(service, kind="knowledge", changes_behavior=False,
                  content="This prospect described unclear value as their price concern.")
    result = await ev.evaluate_candidate(service, c["id"], judge=judge)
    assert result["mode"] == "knowledge_support" and result["passed"] is True
    before = (service.target.memory_dir / "MEMORY.md").read_bytes()
    activation = promote_candidate(service, c["id"], result["id"])
    from cognition.amendments import ProposalLedger
    ledger = ProposalLedger(service.target.state_dir / "amendment-proposals.jsonl")
    row = ledger.read_all()[0]
    assert row.confidence_score == 0 and row.status == "applied"
    assert row.source == "harness_learning"
    assert c["content"] in (service.target.memory_dir / "MEMORY.md").read_text()
    assert rollback_activation(service, activation["id"], reason="source corrected")["status"] == "rolled_back"
    assert (service.target.memory_dir / "MEMORY.md").read_bytes() == before


def test_manifest_requires_default_twelve_and_countercases(service):
    c = candidate(service)
    m = manifest(c)
    with pytest.raises(ValueError, match="insufficient"):
        replace(m, cases=m.cases[:2])
    with pytest.raises(ValueError, match="counterexample"):
        replace(m, cases=tuple(replace(case, applicable=True) for case in m.cases))
    with pytest.raises(ValueError, match="overlaps"):
        replace(m, proposal_case_ids=("q-0",))
    with pytest.raises(ValueError, match="distinct"):
        replace(m, cases=(m.cases[0], replace(m.cases[0], id="renamed"), *m.cases[2:]))


@pytest.mark.asyncio
async def test_behavioral_self_change_cannot_hide_under_knowledge_support(service):
    c = candidate(service, kind="self_model", changes_behavior=False)
    async def skeptical(payload, **kwargs):
        return {"supported": True, "contradictions_addressed": True,
                "changes_behavior": True, "reason": "This changes what the persona does."}
    result = await ev.evaluate_candidate(service, c["id"], judge=skeptical)
    assert result["reason"] == "behavior_requires_qualification"
    with pytest.raises(LearningError):
        promote_candidate(service, c["id"], result["id"])


@pytest.mark.asyncio
async def test_unsupported_source_and_string_boolean_are_rejected(service):
    c = candidate(service)
    async def unsupported(payload, **kwargs):
        return {"supported": False, "contradictions_addressed": True, "changes_behavior": True}
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), judge=unsupported, run_key="first")
    assert result["reason"] == "evidence_unsupported"
    async def malformed(payload, **kwargs):
        return {"supported": "true", "contradictions_addressed": True, "changes_behavior": True}
    result = await ev.evaluate_candidate(service, c["id"], judge=malformed, run_key="second")
    assert result["reason"] == "evaluation_incomplete"


@pytest.mark.asyncio
async def test_runtime_change_cannot_pass_paired_evaluation(service):
    c = candidate(service)
    async def drift(case, content, manifest, **kwargs):
        return ev.CaseExecution(content, "model-b" if "diagnostic" in content else "model-a",
                                "openai-compatible", "generic_runtime")
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=drift, judge=judge)
    assert result["reason"] == "evaluation_incomplete"
    assert "trial_model_drift" in result["errors"][0]


@pytest.mark.asyncio
async def test_primary_improvement_cannot_override_new_hard_failure(service):
    c = candidate(service)
    m = manifest(c)
    m = replace(m, cases=(replace(m.cases[0], forbidden_substrings=("diagnostic",)), *m.cases[1:]))
    result = await ev.evaluate_candidate(service, c["id"], manifest=m, run_case=execute, judge=judge)
    assert result["candidate_score"] > result["baseline_score"]
    assert result["reason"] == "new_hard_failure" and result["passed"] is False


@pytest.mark.asyncio
async def test_retry_reuses_frozen_receipt_and_exposed_cases_cannot_be_retuned(service):
    c = candidate(service)
    m = manifest(c)
    one = await ev.evaluate_candidate(service, c["id"], manifest=m, run_case=execute, judge=judge)
    async def must_not_call(*args, **kwargs):
        raise AssertionError("retry repeated provider spend")
    two = await ev.evaluate_candidate(service, c["id"], manifest=m, run_case=must_not_call, judge=must_not_call)
    assert two["id"] == one["id"]
    changed = candidate(service, content="Ask another diagnostic question before discussing discounts.")
    with pytest.raises(LearningError, match="previously_exposed"):
        await ev.evaluate_candidate(service, changed["id"], manifest=manifest(changed), run_case=execute, judge=judge)


@pytest.mark.asyncio
async def test_checkpoint_resumes_without_repeating_completed_pairs(service):
    c = candidate(service)
    seen = []
    class LearningDeferred(Exception):
        pass
    async def defer(item):
        if item.get("case_id") == "q-1":
            raise LearningDeferred("foreground")
    async def track(case, content, manifest, **kwargs):
        seen.append(case.id)
        return await execute(case, content, manifest, **kwargs)
    with pytest.raises(LearningDeferred):
        await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=track, judge=judge, checkpoint=defer)
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=track, judge=judge)
    assert result["passed"] and len(seen) == 24
    assert seen.count("q-0") == 2 and seen.count("q-1") == 2


@pytest.mark.asyncio
async def test_hash_binding_and_cross_profile_receipt_rejected(service, tmp_path):
    c = candidate(service)
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=execute, judge=judge)
    forged = dict(result["receipt"], candidate_score=1.0)
    changed = service.record_evaluation(c["id"], {"passed": True, "mode": "qualification", "receipt": forged,
        "receipt_hash": result["receipt_hash"]}, run_key="tampered")
    with pytest.raises(LearningError, match="integrity"):
        promote_candidate(service, c["id"], changed["id"])
    other = LearningService(LearningTarget("crypto", tmp_path / "other-memory", tmp_path / "other-data",
                                           tmp_path / "other-state", tmp_path / "other-skills"))
    with pytest.raises(LearningError, match="candidate_not_found"):
        promote_candidate(other, c["id"], result["id"])


@pytest.mark.asyncio
async def test_corrected_evidence_invalidates_old_adoption_authority(service):
    c = candidate(service)
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=execute, judge=judge)
    old = service.get_record(c["evidence_ids"][0])
    service.record_observation(old["experience_id"], {"evidence": "Reply belonged to a different prospect.",
        "quality": "direct", "status": "resolved", "supersedes": old["id"]}, source_key="correction")
    with pytest.raises(LearningError, match="superseded"):
        promote_candidate(service, c["id"], result["id"])


@pytest.mark.asyncio
async def test_rollback_preserves_later_unrelated_edits(service):
    c = candidate(service, kind="knowledge", changes_behavior=False, content="The prospect asked about value.")
    result = await ev.evaluate_candidate(service, c["id"], judge=judge)
    activation = promote_candidate(service, c["id"], result["id"])
    memory = service.target.memory_dir / "MEMORY.md"
    memory.write_text(memory.read_text() + "\nUnrelated operator addition\n", encoding="utf-8")
    outcome = rollback_activation(service, activation["id"], reason="reassess")
    assert outcome["status"] == "rolled_back"
    assert "Unrelated operator addition" in memory.read_text()
    assert c["content"] not in memory.read_text()


@pytest.mark.asyncio
async def test_machine_skill_promotion_refuses_dangerous_content(service):
    c = candidate(service, content="Run rm -rf / to improve sales results.")
    async def generous(payload, **kwargs):
        if payload["mode"] == "support":
            return {"supported": True, "contradictions_addressed": True, "changes_behavior": True}
        return {"score_a": 0.9 if "rm -rf" in payload["output_a"] else 0.3,
                "score_b": 0.9 if "rm -rf" in payload["output_b"] else 0.3,
                "failures_a": [], "failures_b": []}
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=execute, judge=generous)
    with pytest.raises(LearningError, match="scan_dangerous"):
        promote_candidate(service, c["id"], result["id"])
    assert not list((service.target.skills_dir / "promoted").glob("*/SKILL.md"))


@pytest.mark.asyncio
async def test_provider_failure_retries_without_repeating_completed_pairs(service):
    c = candidate(service)
    visited = []
    async def unreliable(case, content, manifest, **kwargs):
        visited.append(case.id)
        if case.id == "q-2":
            raise RuntimeError("quota unavailable")
        return await execute(case, content, manifest, **kwargs)
    first = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=unreliable, judge=judge)
    assert first["reason"] == "evaluation_incomplete"
    async def reliable(case, content, manifest, **kwargs):
        visited.append(case.id)
        return await execute(case, content, manifest, **kwargs)
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=reliable, judge=judge)
    assert result["passed"] and result["id"] != first["id"]
    assert visited.count("q-0") == 2 and visited.count("q-1") == 2


@pytest.mark.asyncio
async def test_changed_owned_amendment_is_conflict_and_not_physically_active(service):
    from personas.learning.promotion import activation_is_applied
    c = candidate(service, kind="knowledge", changes_behavior=False)
    result = await ev.evaluate_candidate(service, c["id"], judge=judge)
    activation = promote_candidate(service, c["id"], result["id"])
    assert activation_is_applied(service, activation)
    memory = service.target.memory_dir / "MEMORY.md"
    memory.write_bytes(memory.read_bytes().replace(b"diagnostic", b"different"))
    assert not activation_is_applied(service, activation)
    assert rollback_activation(service, activation["id"], reason="edited")["status"] == "conflict"
    assert b"different" in memory.read_bytes()


@pytest.mark.asyncio
async def test_selective_rollback_recovers_after_write_before_finalize(service, monkeypatch):
    from cognition import amendment_rollback
    from cognition.amendments import ProposalLedger
    c = candidate(service, kind="knowledge", changes_behavior=False)
    result = await ev.evaluate_candidate(service, c["id"], judge=judge)
    activation = promote_candidate(service, c["id"], result["id"])
    memory = service.target.memory_dir / "MEMORY.md"
    memory.write_bytes(memory.read_bytes() + b"\nNew unrelated note\n")
    original = ProposalLedger._update_record_unique
    def fail_finalize(self, proposal_id, changes):
        if changes.get("status") == "rolled_back":
            return "io_error"
        return original(self, proposal_id, changes)
    monkeypatch.setattr(ProposalLedger, "_update_record_unique", fail_finalize)
    first = rollback_activation(service, activation["id"], reason="regressed")
    assert first["status"] == "failed"
    assert c["content"] not in memory.read_text()
    monkeypatch.setattr(ProposalLedger, "_update_record_unique", original)
    second = rollback_activation(service, activation["id"], reason="regressed")
    assert second["status"] == "rolled_back"
    assert "New unrelated note" in memory.read_text()


@pytest.mark.asyncio
async def test_learning_survives_second_provider_qualification(service):
    c = candidate(service)
    first = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=execute, judge=judge)
    a = promote_candidate(service, c["id"], first["id"])
    m = replace(manifest(c), model="model-b", provider="kimi",
                cases=tuple(replace(case, id="b-" + case.id, prompt=case.prompt + " New setting.") for case in manifest(c).cases))
    async def second_provider(case, content, manifest, **kwargs):
        return ev.CaseExecution(content, "model-b", "kimi", "generic_runtime")
    second = await ev.evaluate_candidate(service, c["id"], manifest=m, run_case=second_provider, judge=judge)
    b = promote_candidate(service, c["id"], second["id"])
    assert b["id"] != a["id"] and b["qualified_models"] == ["model-a", "model-b"]
    assert b["skill_name"] == a["skill_name"]
    assert service.get_record(a["id"])["status"] == "superseded"
    assert c["content"] in service.render_context("price objection", model="model-b").text


@pytest.mark.asyncio
async def test_observational_support_requires_actual_use_and_real_origin(service):
    c = candidate(service)
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=execute, judge=judge)
    activation = promote_candidate(service, c["id"], result["id"])
    included = []
    for index, mode in enumerate(("real", "practice", "real")):
        experience = service.capture_experience(f"future-{index}", "test", "price objection", mode=mode)
        if index != 2:
            context = service.render_context("price objection")
            service.record_context_receipt(experience["id"], context, context.text, attempt_key="run")
        obs = service.record_observation(experience["id"], {"evidence": "Prospect explained the value concern.",
            "quality": "direct", "status": "resolved"}, source_key=f"future-{index}")
        included.append(obs["id"])
    reassess_activation(service, activation["id"], result["id"], observation_ids=included)
    event = [event for event in service.store.events(activation["id"]) if event["event_type"] == "reassessment"][0]
    assert event["payload"]["real_observation_ids"] == [included[0]]
    assert event["payload"]["claim_scope"] == "observational_support_only"


@pytest.mark.asyncio
async def test_runtime_reasoner_is_strictly_toolless_and_provider_pin_is_not_auth(tmp_path, monkeypatch):
    from runtime import lane_router
    from runtime.base import RuntimeResult, assert_model_only_contract
    captured = []
    async def runtime(request):
        assert_model_only_contract(request)
        captured.append(request)
        return RuntimeResult("{}", "generic_runtime", "kimi", "model-k", profile_key="primary-kimi")
    monkeypatch.setattr(lane_router, "run_with_runtime_lanes", runtime)
    result = await real_runtime_reasoning("grade this task", cwd=tmp_path, provider="kimi",
            runtime_lane="generic_runtime", model="model-k", allow_fallback=False)
    assert result.profile_key == "primary-kimi"
    assert captured[0].preferred_provider == "kimi" and captured[0].auth_profile is None
    assert captured[0].workload == "background" and captured[0].model_only


def test_provider_bound_model_override_preserves_unpinned_behavior(tmp_path, monkeypatch):
    from runtime import profiles, routing, lane_router
    from runtime.base import RuntimeRequest
    fake = profiles.RuntimeProfile(key="primary-kimi", provider="kimi", model="default-model")
    monkeypatch.setattr(profiles, "_kimi_profile", lambda **kwargs: fake)
    request = RuntimeRequest("test", tmp_path, "qualification", preferred_provider="kimi", model="tested-model", allow_fallback=False)
    pinned = profiles.build_profile_for_provider("kimi", key_prefix="primary", request=request)
    assert pinned.model == "tested-model" and pinned.candidate_models == ("tested-model",)
    request.preferred_provider = None
    assert profiles.build_profile_for_provider("kimi", key_prefix="primary", request=request).model == "default-model"
    request.preferred_provider = "kimi"
    assert routing._preferred_generic_provider(request) == "kimi"
    request.model_only = True
    request.runtime_lane = "generic_runtime"
    seen = []
    monkeypatch.setattr(lane_router, "_lane_profiles", lambda lane, req: seen.append(lane) or [fake])
    assert lane_router._resolve_lane_profiles(request) == [fake]
    assert seen == ["generic_runtime"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["knowledge", "procedure"])
async def test_activation_write_failure_compensates_physical_content_and_retry_succeeds(service, monkeypatch, kind):
    c = candidate(service, kind=kind, changes_behavior=kind == "procedure")
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c) if kind == "procedure" else None,
                                         run_case=execute, judge=judge)
    original = service.record_activation
    def fail(*args, **kwargs):
        raise OSError("simulated disk full")
    monkeypatch.setattr(service, "record_activation", fail)
    with pytest.raises(OSError):
        promote_candidate(service, c["id"], result["id"])
    assert not service.store.all("activation")
    assert not list((service.target.skills_dir / "promoted").glob("*/SKILL.md"))
    assert c["content"] not in (service.target.memory_dir / "MEMORY.md").read_text()
    monkeypatch.setattr(service, "record_activation", original)
    activation = promote_candidate(service, c["id"], result["id"])
    assert activation["status"] == "active_provisional"


@pytest.mark.asyncio
async def test_missing_requalification_result_does_not_freeze_current_method(service):
    c = candidate(service)
    result = await ev.evaluate_candidate(service, c["id"], manifest=manifest(c), run_case=execute, judge=judge)
    promote_candidate(service, c["id"], result["id"])
    m = replace(manifest(c), cases=tuple(replace(case, id="new-" + case.id, prompt=case.prompt + " In a new market.")
                                       for case in manifest(c).cases))
    async def unavailable(*args, **kwargs):
        raise RuntimeError("provider unavailable")
    failed = await ev.evaluate_candidate(service, c["id"], manifest=m, run_case=unavailable, judge=judge)
    assert failed["errors"]
    assert service.get_record(c["id"])["status"] == "active_provisional"
    assert c["content"] in service.render_context("price objection").text


@pytest.mark.asyncio
async def test_judge_uses_actual_producer_vendor_and_configured_fallback_order(tmp_path, monkeypatch):
    from runtime import routing
    monkeypatch.setattr(routing, "_generic_fallback_route_for_request", lambda *args, **kwargs: ("kimi", "openai-compatible"))
    attempts = []
    async def reason(prompt, **kwargs):
        attempts.append(kwargs["provider"])
        if kwargs["provider"] == "openai-codex":
            raise RuntimeError("unavailable")
        return ev.CaseExecution('{"supported":true,"contradictions_addressed":true,"changes_behavior":false}',
                                "model-k", kwargs["provider"], kwargs["runtime_lane"])
    result = await ev.runtime_judge({"mode": "support", "candidate": "claim"}, cwd=tmp_path,
                                    producer_provider="claude", reasoning=reason)
    assert attempts == ["openai-codex", "kimi"]
    assert result["grader"]["independent_vendor"] is True
    assert result["grader"]["attempts"][0]["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_actual_widened_lane_can_be_frozen_for_strict_paired_trials(service, monkeypatch):
    from runtime import lane_router
    from runtime.base import RuntimeResult
    from runtime.profiles import RuntimeProfile
    unsafe = RuntimeProfile("codex", "openai-codex", "code-model")
    safe = RuntimeProfile("claude", "claude", "model-c")
    monkeypatch.setattr(lane_router, "_lane_profiles", lambda lane, req: [unsafe] if lane == "generic_runtime" else [safe])
    class Adapter:
        def __init__(self, profile):
            self.profile = profile
        def supports_model_only(self):
            return self.profile.provider == "claude"
        def supports(self, request):
            return True
        async def run(self, request):
            return RuntimeResult(request.prompt, "claude_native", "claude", "model-c")
    monkeypatch.setattr(lane_router, "_adapter_for", Adapter)
    first = await real_runtime_reasoning("design qualification", cwd=service.target.memory_dir, runtime_lane="generic_runtime")
    assert first.provider == "claude" and first.runtime_lane == "claude_native"
    c = candidate(service)
    m = replace(manifest(c), model=first.model, provider=first.provider, runtime_lane=first.runtime_lane)
    monkeypatch.setattr(ev, "runtime_reasoning", real_runtime_reasoning)
    result = await ev.evaluate_candidate(service, c["id"], manifest=m, judge=judge)
    assert result["passed"] and result["provider"] == "claude"
    assert {r["candidate"]["runtime_lane"] for r in result["comparisons"]} == {"claude_native"}


@pytest.mark.asyncio
async def test_successor_retires_prior_from_real_skill_index_and_rollback_restores_prior(service):
    from cognition.skills import build_skill_index
    first = candidate(service)
    first_eval = await ev.evaluate_candidate(service, first["id"], manifest=manifest(first), run_case=execute, judge=judge)
    prior = promote_candidate(service, first["id"], first_eval["id"])
    second = candidate(service, content="Ask a diagnostic question about the budget owner before discounting.",
                        prior_candidate_id=first["id"], baseline_version=first["content_hash"])
    m = replace(manifest(second), baseline_content=first["content"], baseline_version=first["content_hash"],
                cases=tuple(replace(case, id="next-" + case.id, prompt=case.prompt + " A budget owner is involved.") for case in manifest(second).cases))
    async def improved(payload, **kwargs):
        if payload["mode"] == "support":
            return await judge(payload, **kwargs)
        return {"score_a": 0.9 if "budget owner" in payload["output_a"] else 0.6,
                "score_b": 0.9 if "budget owner" in payload["output_b"] else 0.6,
                "failures_a": [], "failures_b": []}
    second_eval = await ev.evaluate_candidate(service, second["id"], manifest=m, run_case=execute, judge=improved)
    successor = promote_candidate(service, second["id"], second_eval["id"])
    # The profile-local extra-dir path bypasses ambient global sidecar scope,
    # exactly as the real persona runtime builds its own local skill index.
    index = build_skill_index(service.target.skills_dir / "unused-central", extra_skill_dirs=[service.target.skills_dir], reader_persona="sales")
    assert prior["skill_name"] not in index and successor["skill_name"] in index
    assert service.get_record(prior["id"])["status"] == "rolled_back"
    undone = rollback_activation(service, successor["id"], reason="successor underperformed")
    assert undone["restored_previous_activation_id"]
    index = build_skill_index(service.target.skills_dir / "unused-central", extra_skill_dirs=[service.target.skills_dir], reader_persona="sales")
    assert prior["skill_name"] in index and successor["skill_name"] not in index
    assert first["content"] in service.render_context("price objection").text


@pytest.mark.asyncio
async def test_prior_retirement_conflict_compensates_successor(service):
    first = candidate(service)
    first_eval = await ev.evaluate_candidate(service, first["id"], manifest=manifest(first), run_case=execute, judge=judge)
    prior = promote_candidate(service, first["id"], first_eval["id"])
    file = service.target.skills_dir / "promoted" / prior["skill_name"] / "SKILL.md"
    file.write_bytes(file.read_bytes() + b"\nOperator edited this method\n")
    second = candidate(service, content="Ask a diagnostic question with an example.", prior_candidate_id=first["id"])
    m = replace(manifest(second), cases=tuple(replace(case, id="next-" + case.id, prompt=case.prompt + " Another situation.") for case in manifest(second).cases))
    second_eval = await ev.evaluate_candidate(service, second["id"], manifest=m, run_case=execute, judge=judge)
    with pytest.raises(LearningError, match="retirement_conflict"):
        promote_candidate(service, second["id"], second_eval["id"])
    assert len(list((service.target.skills_dir / "promoted").glob("*/SKILL.md"))) == 1
    assert b"Operator edited" in file.read_bytes()
    assert not any(item["candidate_id"] == second["id"] for item in service.store.all("activation"))
