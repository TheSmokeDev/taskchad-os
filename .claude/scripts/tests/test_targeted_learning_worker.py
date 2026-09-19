"""Exact-job selection narrows existing work without bypassing worker policy."""

import importlib
import sys

import pytest

from personas.learning import worker
from personas.learning.errors import LearningUnavailableError
from personas.learning.models import LearningTarget
from personas.learning.queue import LearningQueue
from personas.learning.service import LearningService
from runtime import activity


def make_service(tmp_path, name="researcher"):
    base = tmp_path / name
    return LearningService(
        LearningTarget(name, base / "memory", base / "data", base / "state", base / "skills")
    )


def test_target_missing_notready_and_foreign_are_idle_without_new_jobs(tmp_path):
    service = make_service(tmp_path)
    queue = LearningQueue(service)
    assert queue.claim(job_id="missing") is None
    assert not queue.path.exists()
    urgent = queue.enqueue("investigation", "urgent", now=10)
    target = queue.enqueue("dream", "target", now=10, available_at=100)
    foreign = LearningQueue(make_service(tmp_path, "other")).enqueue("dream", "target", now=10)
    before = queue.list(include_finished=True)
    assert queue.claim(job_id=target["id"], now=11) is None
    assert queue.claim(job_id=foreign["id"], now=11) is None
    assert queue.claim(job_id="not-there", now=11) is None
    assert queue.list(include_finished=True) == before
    assert queue.claim(now=11)["id"] == urgent["id"]  # Default priority remains unchanged.


def test_target_respects_leases_and_recovers_only_its_own_expiry(tmp_path):
    queue = LearningQueue(make_service(tmp_path))
    unrelated = queue.enqueue("investigation", "old", now=10)
    target = queue.enqueue("dream", "target", now=10)
    queue.claim(job_id=unrelated["id"], now=11, ttl_seconds=10)
    first = queue.claim(job_id=target["id"], now=30, ttl_seconds=10)
    assert first["id"] == target["id"]
    assert queue.claim(job_id=target["id"], now=31) is None
    other = next(row for row in queue.list() if row["id"] == unrelated["id"])
    assert other["status"] == "running" and other["expires_at"] == 21
    resumed = queue.claim(job_id=target["id"], now=41)
    assert resumed["id"] == first["id"] and resumed["token"] != first["token"]
    with pytest.raises(RuntimeError, match="claim lost"):
        queue.finish_stage(first, status="completed", now=42)
    queue.finish_stage(resumed, status="deferred", delay_seconds=100, now=42)
    assert queue.claim(job_id=target["id"], now=43) is None


@pytest.mark.asyncio
async def test_target_runs_same_stages_without_drain_or_priority_mutation(tmp_path):
    service = make_service(tmp_path)
    queue = LearningQueue(service)
    unrelated = queue.enqueue("investigation", "urgent")
    target = queue.enqueue("dream", "admitted-dream", payload={"cycle_id": "persisted"})
    visits = []

    async def process(_service, job):
        worker._check_stage_boundary()
        visits.append((job["id"], job["stage"]))
        return ("synthesis_retain" if job["stage"] == "synthesis_reason" else "done"), job[
            "payload"
        ]

    result = await worker.run_worker(
        service,
        job_id=target["id"],
        processor=process,
        max_stages=4,
        activity_path=tmp_path / "activity.db",
    )
    assert result == {"status": "idle", "stages": 2}
    assert visits == [(target["id"], "synthesis_reason"), (target["id"], "synthesis_retain")]
    assert next(row for row in queue.list() if row["id"] == unrelated["id"]) == unrelated
    assert (
        next(row for row in queue.list(include_finished=True) if row["id"] == target["id"])[
            "priority"
        ]
        == target["priority"]
    )


@pytest.mark.asyncio
async def test_target_cannot_bypass_foreground_pause_exclusive_lease_or_outage(tmp_path):
    service = make_service(tmp_path)
    queue = LearningQueue(service)
    target = queue.enqueue("dream", "target")
    path = tmp_path / "activity.db"
    calls = []

    async def unavailable(_service, _job):
        worker._check_stage_boundary()
        calls.append("called")
        raise LearningUnavailableError("Actual inference provider unavailable")

    options = {"job_id": target["id"], "processor": unavailable, "activity_path": path}
    foreground = activity.acquire_lease("foreground", owner="interactive", path=path)
    assert (await worker.run_worker(service, **options))["status"] == "deferred"
    activity.release_lease(foreground, path=path)
    service.set_paused(True)
    assert (await worker.run_worker(service, **options))["status"] == "disabled"
    service.set_paused(False)
    other_worker = activity.acquire_lease(
        "learning-worker", owner="other", exclusive=True, path=path
    )
    assert (await worker.run_worker(service, **options))["status"] == "busy"
    activity.release_lease(other_worker, path=path)
    assert calls == []
    assert (await worker.run_worker(service, **options))["status"] == "deferred"
    assert calls == ["called"]
    assert (await worker.run_worker(service, **options))["status"] == "idle"
    assert calls == ["called"]  # The existing retry backoff still applies.


@pytest.mark.asyncio
async def test_targeted_wake_skips_discovery_and_uses_the_shared_processor(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    target = LearningQueue(service).enqueue("dream", "target")
    monkeypatch.setattr(
        worker, "discover_work", lambda _: pytest.fail("targeted wake must not admit new jobs")
    )
    visits = []

    async def process(_service, job):
        worker._check_stage_boundary()
        visits.append(job["id"])
        return "done", job["payload"]

    monkeypatch.setattr(worker, "process_stage", process)
    assert (await worker.wake_learning(service=service, job_id=target["id"], max_stages=1))[
        "stages"
    ] == 1
    assert visits == [target["id"]]
    assert (await worker.wake_learning(service=service, job_id="missing"))["status"] == "idle"


def test_child_cli_forwards_optional_job_id(monkeypatch, capsys):
    import personas

    monkeypatch.setattr(personas, "apply_persona_override", lambda: None)
    module = importlib.import_module("persona_learning_worker")
    received = []

    async def wake(**kwargs):
        received.append(kwargs)
        return {"status": "idle", "stages": 0}

    monkeypatch.setattr(worker, "wake_learning", wake)
    monkeypatch.setattr(
        sys, "argv", ["persona_learning_worker.py", "--job-id", "existing-job", "--max-stages", "4"]
    )
    assert module.main() == 0
    assert received == [{"test_mode": False, "max_stages": 4, "job_id": "existing-job"}]
    assert '"status": "idle"' in capsys.readouterr().out
