"""A provider outage must not permanently retire a learner's pending work."""

import time

import pytest

from personas.learning.models import LearningTarget
from personas.learning.queue import LearningQueue
from personas.learning.service import LearningService
from personas.learning.worker import run_worker
from runtime.errors import RuntimeExecutionError


@pytest.mark.asyncio
async def test_repeated_provider_outages_remain_deferred_until_recovery(tmp_path, monkeypatch):
    service = LearningService(LearningTarget("sales", tmp_path / "memory", tmp_path / "data",
                                            tmp_path / "state", tmp_path / "skills"))
    record = service.capture_experience("outage", "test", "Learn from price objections")
    queue = LearningQueue(service)
    queue.enqueue("experience", "outage", payload={"experience_id": record["id"]})
    clock = [time.time() + 1]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    async def offline(service, job):
        raise RuntimeExecutionError("Provider quota temporarily exhausted")

    for _ in range(4):
        result = await run_worker(service, max_stages=1, processor=offline,
                                  activity_path=tmp_path / "activity.db")
        assert result["status"] == "deferred"
        job = queue.list(include_finished=True)[0]
        assert job["status"] == "deferred" and job["failures"] == 0
        clock[0] += 601

    async def recovered(service, job):
        return "done", job["payload"]

    await run_worker(service, max_stages=1, processor=recovered,
                     activity_path=tmp_path / "activity.db")
    assert queue.list(include_finished=True)[0]["status"] == "completed"
