"""Due follow-ups overtake historical recovery without changing job state."""

import pytest

from personas.learning.cognition import discover_cognitive_work
from personas.learning.models import LearningTarget
from personas.learning.queue import LearningQueue, cognitive_cycle_priority
from personas.learning.service import LearningService


@pytest.fixture
def service(tmp_path):
    return LearningService(
        LearningTarget(
            "researcher",
            tmp_path / "memory",
            tmp_path / "data",
            tmp_path / "state",
            tmp_path / "skills",
        )
    )


def legacy_job(service, origin, phase="interpret"):
    cycle = service.store.put(
        "cognitive_cycle",
        {
            "origin_key": origin,
            "phase": phase,
            "evidence_ids": [],
            "metadata": {},
            "status": "pending",
        },
        key=origin,
    )
    # The old release assigned all cognition priority 15. Omitted priority retains
    # this compatible insertion default; discovery applies the new host policy.
    job = LearningQueue(service).enqueue(
        "cognition", cycle["id"], payload={"cycle_id": cycle["id"]}
    )
    return cycle, job


def rows(service):
    return {row["id"]: row for row in LearningQueue(service).list(include_finished=True)}


def test_provider_outage_does_not_starve_ready_work_after_restart(service):
    queue = LearningQueue(service)
    old = queue.enqueue("cognition", "unavailable-vision", now=100)
    fresh = queue.enqueue("cognition", "ready-numeric", now=101)
    claimed = queue.claim(now=102, ttl_seconds=100)
    assert claimed["id"] == old["id"]
    checkpoint = {"receipt": "completed-reasoning", "failure_class": "LearningUnavailableError"}
    deferred = queue.finish_stage(
        claimed, stage="cognitive_support", payload=checkpoint,
        status="deferred", delay_seconds=60, now=103,
    )
    # Even when a seven-persona rotation returns after the retry is due, the
    # previously queued numeric work gets its turn before another unavailable retry.
    restarted = LearningQueue(service)
    assert [row["id"] for row in restarted.list()] == [fresh["id"], old["id"]]
    next_job = restarted.claim(now=600, ttl_seconds=100)
    assert next_job["id"] == fresh["id"]
    restarted.finish_stage(next_job, stage="done", status="completed", now=601)
    resumed = restarted.claim(now=602)
    assert resumed["id"] == old["id"]
    assert resumed["payload"] == checkpoint and resumed["stage"] == "cognitive_support"
    assert resumed["failures"] == deferred["failures"] == 0


def test_due_revisit_overtakes_existing_sixty_cycle_backlog_idempotently(service):
    historical = [legacy_job(service, f"recover:historic-{index}")[1] for index in range(60)]
    _, fresh = legacy_job(service, "new-chart")
    _, due = legacy_job(service, "inquiry:already-due", "revisit")
    queue = LearningQueue(service)
    before = rows(service)
    assert queue.list()[-1]["id"] == due["id"]
    assert discover_cognitive_work(service, recover_sources=False) == 0
    order = queue.list()
    assert [row["id"] for row in order[:2]] == [due["id"], fresh["id"]]
    assert order[0]["priority"] == 11 and order[1]["priority"] == 15
    assert all(rows(service)[row["id"]]["priority"] == 55 for row in historical)
    after = rows(service)
    assert len(after) == len(before) == 62
    for identity, row in after.items():
        assert {k: v for k, v in row.items() if k != "priority"} == {
            k: v for k, v in before[identity].items() if k != "priority"
        }
    discover_cognitive_work(service, recover_sources=False)
    assert rows(service) == after
    claimed = queue.claim()
    assert claimed["id"] == due["id"]
    discover_cognitive_work(service, recover_sources=False)
    assert rows(service)[claimed["id"]] == claimed  # No duplicate or reset claim.


@pytest.mark.parametrize("status", ["deferred", "retry"])
def test_priority_refresh_preserves_backoff_failures_and_stage_checkpoint(service, status):
    _, original = legacy_job(service, "inquiry:awaiting-support", "revisit")
    queue = LearningQueue(service)
    claim = queue.claim()
    checkpoint = {
        **claim["payload"],
        "cached_reasoning_id": "persisted-model-receipt",
        "resume_cursor": 7,
    }
    before = queue.finish_stage(
        claim,
        stage="cognitive_support",
        payload=checkpoint,
        status=status,
        error="provider temporarily unavailable",
        delay_seconds=600,
        failed_attempt=True,
    )
    discover_cognitive_work(service, recover_sources=False)
    after = rows(service)[original["id"]]
    assert after == {**before, "priority": 11}
    assert queue.claim() is None  # The priority policy does not skip provider backoff.


@pytest.mark.parametrize("terminal", [False, True])
def test_priority_refresh_never_mutates_running_or_completed_jobs(service, terminal):
    _, original = legacy_job(service, "recover:already-claimed")
    queue = LearningQueue(service)
    claim = queue.claim()
    before = queue.finish_stage(claim, stage="done", status="completed") if terminal else claim
    discover_cognitive_work(service, recover_sources=False)
    assert rows(service)[original["id"]] == before


def test_notify_uses_same_policy_and_model_priority_metadata_is_ignored(service):
    experience = service.capture_experience("turn", "chat", "Observe actual work")
    for phase, origin, metadata, expected in (
        ("interpret", "fresh", {"priority": 11}, 15),
        ("interpret", "recover:old", {}, 55),
        ("interpret", "legacy-recovered", {"recovered_source_batch": True}, 55),
        ("revisit", "inquiry:due", {}, 11),
    ):
        cycle = service.enqueue_cognitive_cycle(
            phase, origin, [experience["id"]], experience_id=experience["id"], metadata=metadata
        )
        job = next(
            row for row in rows(service).values() if row["payload"]["cycle_id"] == cycle["id"]
        )
        assert job["priority"] == cognitive_cycle_priority(cycle) == expected


@pytest.mark.parametrize(
    "kind,priority",
    [
        ("cognition", True),
        ("cognition", 0),
        ("cognition", 11.0),
        ("cognition", -1),
        ("experience", 11),
    ],
)
def test_only_fixed_host_priority_values_are_accepted(service, kind, priority):
    with pytest.raises(ValueError, match="host policy"):
        LearningQueue(service).enqueue(kind, "invalid", priority=priority)
    assert not service.target.data_dir.exists()
