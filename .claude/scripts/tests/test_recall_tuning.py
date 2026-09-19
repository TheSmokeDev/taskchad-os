"""Ground-truth tuning, durable recovery and call-time persona isolation proof."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from evolve import policy, relevance, tuning
from personas.learning.errors import LearningUnavailableError
from personas.learning.models import LearningError, LearningTarget
from personas.learning.service import LearningService

NOW = datetime(2026, 9, 10, tzinfo=UTC)
BASE = dict(zip(policy.KEYS, (0.7, 0.3, 0.3, 0.02), strict=True))


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLVE_ENABLED", "true")
    target = LearningTarget(
        "crypto", tmp_path / "memory", tmp_path / "data", tmp_path / "state", tmp_path / "skills"
    )
    target.memory_dir.mkdir()
    monkeypatch.setattr(policy, "operator_pins", lambda target=None: {})
    monkeypatch.setattr(policy, "configured_values", lambda: dict(BASE))
    return LearningService(target)


def evidence(service, name, text):
    path = service.target.memory_dir / name
    path.write_text(text, encoding="utf-8")
    return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "excerpt": text}


def make_cases(service):
    good = evidence(
        service, "relevant.md", "Verified original evidence supporting the desired answer."
    )
    bad = evidence(service, "irrelevant.md", "Original evidence about a different topic.")
    groups = {False: [], True: []}
    for index in range(100):
        family = f"family-{index}"
        heldout = int(hashlib.sha256(family.encode()).hexdigest(), 16) % 4 == 0
        groups[heldout].append(family)
    families = groups[False][:4] + groups[True][:2]
    result = []
    for family in families:
        source = evidence(
            service,
            f"{family}.md",
            f"Independently recorded original questions and outcomes for {family}.",
        )
        for index in range(10):
            result.append(
                {
                    "case_key": f"{family}-{index}",
                    "query": f"What did source {family} establish about question {index}?",
                    "source_family": family,
                    "source": source,
                    "labels": [
                        {
                            "evidence": good,
                            "relevance": 3,
                            "rationale": "Human judgment from the original outcome.",
                        },
                        {
                            "evidence": bad,
                            "relevance": 0,
                            "rationale": "Human judgment: unrelated to this question.",
                        },
                    ],
                    "validation": {
                        "method": "operator",
                        "actor": "test-ground-truth-reviewer",
                        "at": NOW.isoformat(),
                        "independent_of_retrieval_scores": True,
                    },
                    "request_budget": {
                        "max_results": 1,
                        "context_chars": 1000,
                        "search_mode": "hybrid",
                    },
                    "protected": False,
                    "forbidden_paths": ["other-persona.md"],
                }
            )
    return result


def admit(service):
    tuning.import_validated_cases(service, make_cases(service), source_key="operator-fixture")
    assert tuning.discover_tuning_work(service, NOW) == 1
    from personas.learning.queue import LearningQueue

    return next(job for job in LearningQueue(service).list() if job["kind"] == "tuning")


async def winner(case, values, target):
    return {
        "paths": ["relevant.md" if values[policy.KEYS[0]] > 0.7 else "irrelevant.md"],
        "latency_ms": 10.0,
        "context_chars": 100,
    }


async def complete(service, job, runner=winner):
    for _ in range(20):
        stage, payload = await tuning.process_tuning_stage(service, job, runner=runner)
        job = job | {"stage": stage, "payload": payload}
        if stage == "done":
            return service.store.get(payload["run_id"])
    raise AssertionError("tuning did not terminate")


@pytest.mark.asyncio
async def test_ground_truth_adoption_and_frozen_rollback(service):
    job = admit(service)
    run = await complete(service, job)
    assert run["status"] == "adopted"
    active = policy.active_policy(service)
    assert active["values"][policy.KEYS[0]] == 0.8
    assert active["comparison"]["paired_ci95"] == [1.0, 1.0]
    assert active["corpus_id"] and active["baseline_evaluation_id"]
    assert service.store.all("activation") == []
    assert service.store.all("candidate") == []
    assert (
        len(
            [
                event
                for event in service.store.events(run["id"])
                if event["event_type"] == "tuning_selection"
            ]
        )
        == 1
    )

    async def regressed(case, values, target):
        return {"paths": ["irrelevant.md"], "latency_ms": 10.0, "context_chars": 100}

    result = await tuning.monitor_active_policy(
        service, now=NOW + timedelta(days=1), runner=regressed
    )
    assert result["status"] == "rolled_back"
    assert policy.active_policy(service)["id"] == active["predecessor_id"]
    assert policy.effective_values(service) == BASE
    status = tuning.tuning_status(service)
    assert status["latest_evaluation"]["comparison"]["failures"] == ["frozen_relevance_regression"]


@pytest.mark.asyncio
async def test_heldout_rejected_once_without_selecting_another_neighbor(service):
    job = admit(service)
    corpus = service._owned(service._owned(job["payload"]["run_id"])["corpus_id"])
    seen = []

    async def overfit(case, values, target):
        seen.append((case["id"], values[policy.KEYS[0]]))
        correct = values[policy.KEYS[0]] > 0.7
        if case["id"] in corpus["heldout_ids"]:
            correct = not correct
        return {"paths": ["relevant.md" if correct else "irrelevant.md"], "latency_ms": 10.0}

    run = await complete(service, job, overfit)
    assert run["status"] == "no_change"
    assert run["status_reason"] == "heldout_rejected_no_second_selection"
    assert policy.active_policy(service) is None
    heldout_calls = [(case, value) for case, value in seen if case in corpus["heldout_ids"]]
    assert len(heldout_calls) == 2 * len(corpus["heldout_ids"])
    assert {value for _, value in heldout_calls} == {0.7, 0.8}
    assert set(case for case, _ in seen[: 40 * 7]).isdisjoint(corpus["heldout_ids"])


@pytest.mark.asyncio
async def test_raw_score_gaming_cannot_win(service):
    job = admit(service)

    async def gaming(case, values, target):
        return {
            "paths": ["irrelevant.md"],
            "scores": [10**9 * values[policy.KEYS[0]]],
            "latency_ms": 10.0,
        }

    result = await complete(service, job, gaming)
    assert result["status"] == "no_change"
    assert policy.active_policy(service) is None
    assert not any(e["role"].endswith("heldout") for e in service.store.all("tuning_evaluation"))


@pytest.mark.asyncio
async def test_outage_resumes_completed_cases_without_relabel_or_replay(service):
    job = admit(service)
    calls = []

    async def interrupted(case, values, target):
        calls.append(case["id"])
        if len(calls) == 3:
            raise LearningUnavailableError("provider unavailable")
        return await winner(case, values, target)

    with pytest.raises(LearningUnavailableError):
        await tuning.process_tuning_stage(service, job, runner=interrupted)
    persisted = service.store.all("tuning_evaluation")[0]
    assert len(tuning._evaluation_results(service, persisted)) == 2
    await tuning.process_tuning_stage(service, job, runner=interrupted)
    assert calls.count(calls[0]) == 1 and calls.count(calls[1]) == 1
    assert calls.count(calls[2]) == 2
    assert len(tuning._evaluation_results(service, persisted)) == 40


@pytest.mark.asyncio
async def test_daily_new_material_gate_and_pins_rechecked_before_activation(service, monkeypatch):
    job = admit(service)
    assert tuning.discover_tuning_work(service, NOW) == 0
    assert tuning.discover_tuning_work(service, NOW + timedelta(days=1)) == 0
    monkeypatch.setattr(policy, "operator_pins", lambda target=None: {policy.KEYS[0]: 0.6})
    result = await complete(service, job)
    assert result["status_reason"] == "operator_pins_changed"
    assert policy.effective_values(service)[policy.KEYS[0]] == 0.6


def test_readiness_never_turns_unlabeled_goldens_into_ground_truth(service):
    status = tuning.tuning_status(service)
    assert status["validated_cases"] == 0 and status["readiness"] == "not_ready"
    assert tuning.discover_tuning_work(service, NOW) == 0
    assert not service.store.path.exists()
    case = make_cases(service)[0]
    del case["validation"]
    with pytest.raises(LearningError, match="independent operator"):
        tuning.import_validated_cases(service, [case], source_key="unsafe")
    assert not service.store.path.exists()


def test_source_family_leakage_and_fabricated_evidence_denied(service):
    cases = make_cases(service)
    cases[0]["source_family"] = "same-source-in-other-family"
    with pytest.raises(LearningError, match="source-family leakage"):
        tuning.import_validated_cases(service, cases, source_key="leak")
    cases = make_cases(service)
    cases[0]["labels"][0]["evidence"] = dict(
        cases[0]["labels"][0]["evidence"], excerpt="fabricated evidence"
    )
    with pytest.raises(LearningError, match="excerpt not found"):
        tuning.import_validated_cases(service, cases, source_key="fabricated")


def test_family_partition_is_stable_when_new_material_arrives(service):
    cases = make_cases(service)
    development, heldout = relevance.split_cases(cases)
    extra = dict(cases[0], case_key="new", query="An independently prepared new question")
    new_dev, new_held = relevance.split_cases(cases + [extra])
    assert {c["case_key"] for c in heldout} <= {c["case_key"] for c in new_held}
    assert {c["case_key"] for c in development} <= {c["case_key"] for c in new_dev}


def test_six_bounded_neighbors_preserve_operator_pins():
    result = policy.neighbors(BASE)
    assert len(result) == 6
    for candidate in result:
        assert policy.validate_values(candidate) == candidate
    pinned = policy.neighbors(BASE, pins={policy.KEYS[0]: 0.7})
    assert len(pinned) == 4
    assert all(c[policy.KEYS[0]] == 0.7 for c in pinned)
    with pytest.raises(LearningError):
        policy.validate_values(BASE | {"model": "other"})
    with pytest.raises(LearningError):
        policy.validate_values(BASE | {policy.KEYS[2]: float("nan")})


@pytest.mark.parametrize(
    "change,failure",
    [
        ({"protected": True, "ndcg": 0.0}, "protected_regression"),
        ({"isolation_violation": True}, "isolation_violation"),
        ({"error": True}, "added_error"),
        ({"latency_ms": 12.51}, "p95_latency_regression"),
    ],
)
def test_protected_isolation_error_latency_gates(change, failure):
    base = {
        "case_id": "one",
        "ndcg": 0.2,
        "latency_ms": 10.0,
        "error": False,
        "protected": False,
        "isolation_violation": False,
    }
    candidate = base | {"ndcg": 1.0} | change
    comparison = relevance.compare([base], [candidate])
    assert comparison["accepted"] is False and failure in comparison["failures"]


@pytest.mark.asyncio
async def test_concurrent_policy_scope_propagates_to_search_threads(tmp_path, monkeypatch):
    import memory_search

    class DB:
        def keyword_search(self, *args, **kwargs):
            return [
                {
                    "file_path": "result.md",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "x",
                    "score": 0.2,
                }
            ]

        def vector_search(self, *args, **kwargs):
            return [
                {
                    "file_path": "result.md",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "x",
                    "score": 0.8,
                }
            ]

        def close(self):
            pass

    import embeddings

    monkeypatch.setattr(embeddings, "embed_text", lambda text: [1.0])
    monkeypatch.setattr(memory_search, "_open_search_db", lambda *args: DB())

    async def search(name, weight):
        root = tmp_path / name
        values = BASE | {policy.KEYS[0]: weight, policy.KEYS[1]: 1 - weight}
        with policy.policy_scope(root, values):
            await asyncio.sleep(0)
            result = await asyncio.to_thread(memory_search.search_hybrid, "query", memory_dir=root)
            return result[0].score

    scores = await asyncio.gather(search("default", 0.8), search("crypto", 0.2))
    assert scores == pytest.approx([0.68, 0.32])


def test_parent_operator_pins_never_leak_into_named_persona(tmp_path, monkeypatch):
    import personas

    monkeypatch.setattr(personas, "get_active_profile_name", lambda: "default")
    monkeypatch.setenv(policy.KEYS[0], ".9")
    target = LearningTarget(
        "crypto", tmp_path / "memory", tmp_path / "data", tmp_path / "state", tmp_path / "skills"
    )
    assert policy.operator_pins(target) == {}
    assert policy.effective_values(LearningService(target))[policy.KEYS[0]] == 0.7


def test_single_explicit_weight_pin_normalizes_before_config_validation(monkeypatch):
    import config

    for key in policy.KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(policy.KEYS[0], "0.9")
    monkeypatch.setattr(config, policy.KEYS[0], 0.9)
    monkeypatch.setattr(config, policy.KEYS[1], 0.3)
    values = policy.configured_values()
    assert values[policy.KEYS[0]] == 0.9
    assert values[policy.KEYS[1]] == 0.1


@pytest.mark.asyncio
async def test_changed_evidence_invalidates_case_and_finishes_no_change(service):
    job = admit(service)
    (service.target.memory_dir / "relevant.md").write_text(
        "Changed original evidence", encoding="utf-8"
    )
    stage, payload = await tuning.process_tuning_stage(service, job, runner=winner)
    assert stage == "done"
    assert payload["reason"] == "source_revision_changed_revalidation_required"
    assert service._owned(payload["run_id"])["status"] == "no_change"
    assert tuning.tuning_status(service)["validated_cases"] == 59


@pytest.mark.asyncio
async def test_runtime_pipeline_and_worker_routing_adopt_without_global_config_mutation(
    service, monkeypatch
):
    from cognition import recall as recall_module
    from cognition.graph import MemoryGraph
    from cognition.observability import RecallLogStore

    import config
    import embeddings
    import memory_search
    from personas.learning import worker

    class Database:
        def keyword_search(self, *args, **kwargs):
            return [self.row("relevant.md", 0.0), self.row("irrelevant.md", 0.014)]

        def vector_search(self, *args, **kwargs):
            return [self.row("relevant.md", 0.43), self.row("irrelevant.md", 0.425)]

        @staticmethod
        def row(path, score):
            return {
                "file_path": path,
                "start_line": 1,
                "end_line": 1,
                "content": "An original factual passage.",
                "score": score,
            }

        def close(self):
            pass

    monkeypatch.setattr(embeddings, "embed_text", lambda _: [1.0])
    monkeypatch.setattr(memory_search, "_open_search_db", lambda *_: Database())
    monkeypatch.setattr(recall_module, "get_cached_memory_graph", lambda *_: MemoryGraph())
    monkeypatch.setattr(config, "RECALL_ENABLED", True)
    monkeypatch.setattr(config, "RECALL_RERANK_ENABLED", False)
    persisted_logs = []
    monkeypatch.setattr(RecallLogStore, "append", lambda *args: persisted_logs.append(args))
    original = {key: getattr(config, key) for key in policy.KEYS}
    real_replay = tuning.replay_case

    async def measured_fixture(case, values, target):
        result = await real_replay(case, values, target)
        # Synthetic I/O has no real latency variance; keep the gate deterministic.
        result["latency_ms"] = 10.0
        return result

    monkeypatch.setattr(tuning, "replay_case", measured_fixture)
    job = admit(service)
    for _ in range(15):
        stage, payload = await worker.process_stage(service, job)
        job = job | {"stage": stage, "payload": payload}
        if stage == "done":
            break
    assert service._owned(payload["run_id"])["status"] == "adopted"
    assert policy.active_policy(service)["values"][policy.KEYS[0]] == 0.8
    assert {key: getattr(config, key) for key in policy.KEYS} == original
    assert persisted_logs == []
    monkeypatch.setattr(policy, "_target_for_memory", lambda _: service.target)
    from recall_service import SearchMode, recall

    ordinary = await recall(
        "Find the original factual passage",
        memory_dir=service.target.memory_dir,
        search_mode=SearchMode.HYBRID,
        max_results=1,
    )
    assert ordinary.results[0].path == "relevant.md"
    assert len(persisted_logs) == 1


@pytest.mark.asyncio
async def test_scheduled_legacy_propose_only_queues_shared_worker(service, monkeypatch, capsys):
    from types import SimpleNamespace

    import config
    from evolve import evolve_loop, replay

    monkeypatch.setattr(config, "get_belief_evolve_settings", lambda: SimpleNamespace(enabled=True))
    monkeypatch.setattr(LearningService, "for_persona", lambda _: service)

    async def forbidden_replay(*args, **kwargs):
        pytest.fail("scheduled evolve wake started inline replay")

    monkeypatch.setattr(replay, "run_replay", forbidden_replay)
    tuning.import_validated_cases(service, make_cases(service), source_key="operator-fixture")
    assert await evolve_loop.propose(dry_run=False) == 0
    assert len(service.store.all("tuning_run")) == 1
    assert not service.store.all("tuning_evaluation")
    assert '"operation": "recall_tuning_admission"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_changed_embedding_model_cannot_masquerade_as_policy_gain(service, monkeypatch):
    import config

    job = admit(service)
    await tuning.process_tuning_stage(service, job, runner=winner)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "operator-selected-other-model")
    stage, payload = await tuning.process_tuning_stage(service, job, runner=winner)
    assert stage == "done"
    assert service._owned(payload["run_id"])["status_reason"] == "runtime_contract_changed"
    assert policy.active_policy(service) is None
