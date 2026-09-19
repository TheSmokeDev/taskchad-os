"""Deterministic lifecycle scheduling: real ledgers and fake execution only."""

import asyncio

import pytest

from personas.learning import dispatcher, hooks
from personas.learning.models import LearningTarget
from personas.learning.service import LearningService
from runtime import activity
from runtime.base import RuntimeRequest, RuntimeResult


def service(tmp_path, name="default"):
    root = tmp_path / name
    return LearningService(
        LearningTarget(name, root / "memory", root / "data", root / "state", root / "skills")
    )


def test_turn_retains_cognitive_events_without_trade(tmp_path):
    svc = service(tmp_path)

    def execute(name, args):
        return {"rsi": 44, "kind": "observed_chart", "cutoff": "2026-09-10T10:00:00Z"}

    request = RuntimeRequest(
        prompt="Review the observed reversal", cwd=".", task_name="review", tool_dispatch=execute
    )
    turn = hooks.prepare_turn(
        request, persona_id="default", surface="chat_engine", origin_id="message-1", service=svc
    )
    turn.request.tool_dispatch("inspect_chart", {})
    turn.complete(
        RuntimeResult(
            text="The reversal remains uncertain.",
            provider="fake",
            model="test",
            runtime_lane="generic_runtime",
        )
    )
    cycles = svc.store.all("cognitive_cycle")
    assert {c["phase"] for c in cycles} == {"reorient", "interpret", "reflect"}
    assert all(c["evidence_ids"] for c in cycles)
    assert not svc.store.all("expectation")
    assert not svc.store.all("activation")
    # Duplicate end callbacks do not duplicate the durable transition.
    turn.complete(
        RuntimeResult(
            text="The reversal remains uncertain.",
            provider="fake",
            model="test",
            runtime_lane="generic_runtime",
        )
    )
    assert len(svc.store.all("cognitive_cycle")) == len(cycles)


def test_learning_role_does_not_recursively_capture(tmp_path):
    svc = service(tmp_path)
    request = RuntimeRequest(
        prompt="Interpret previous work",
        cwd=".",
        task_name="reflection",
        metadata={"cognitive_generated": True},
    )
    turn = hooks.prepare_turn(
        request, persona_id="default", surface="chat_engine", origin_id="generated", service=svc
    )
    assert turn.experience is None
    assert turn.request.metadata["learning"]["coverage"] == "generated_learning_excluded"
    assert not svc.store.path.exists()


def test_session_debrief_is_durable_and_duplicate_safe(tmp_path):
    svc = service(tmp_path)
    kwargs = dict(
        persona_id="default",
        session_id="session-1",
        surface="talk",
        transcript="Operator: Why did price reject?\nHomie: Need another candle.",
        service=svc,
    )
    first = hooks.enqueue_session_debrief(**kwargs)
    second = hooks.enqueue_session_debrief(**kwargs)
    assert first == second
    restored = LearningService(svc.target)
    assert restored.get_record(first["cognitive_cycle_id"])["phase"] == "reflect"
    assert len(restored.store.all("observation")) == 1


@pytest.mark.asyncio
async def test_cancel_queues_debrief_and_flushes_tool_evidence(tmp_path):
    svc = service(tmp_path)
    turn = hooks.prepare_turn(
        RuntimeRequest(
            prompt="Examine the market",
            cwd=".",
            task_name="review",
            tool_dispatch=lambda *a: {"value": 4},
        ),
        persona_id="default",
        surface="chat_engine",
        origin_id="cancelled",
        service=svc,
    )
    turn.request.tool_dispatch("inspect_chart", {})
    await turn.afailed(asyncio.CancelledError())
    phases = {row["phase"] for row in svc.store.all("cognitive_cycle")}
    assert phases == {"reorient", "interpret", "reflect"}


@pytest.mark.asyncio
async def test_dispatcher_election_and_fair_rotation(tmp_path, monkeypatch):
    path = tmp_path / "activity.db"
    services = [service(tmp_path, name) for name in ("default", "crypto", "sales")]
    selected = []

    async def child(svc):
        selected.append(svc.target.persona_id)
        return {"status": "checkpointed"}

    async def report(services):
        return {"status": "idle"}

    monkeypatch.setattr(dispatcher, "_discover", lambda values: (values, []))
    first = dispatcher.CognitiveDispatcher(
        path=path, service_loader=lambda: services, child_runner=child, report_runner=report
    )
    second = dispatcher.CognitiveDispatcher(
        path=path, service_loader=lambda: services, child_runner=child, report_runner=report
    )
    for _ in range(4):
        await first.tick()
        await asyncio.sleep(0)
    assert selected == ["crypto", "default", "sales", "crypto"]
    assert await second.tick() == {"state": "standby"}
    assert dispatcher.dispatcher_status(path=path)["tick_count"] == 4
    activity.release_lease(first.lease, path=path)
    assert await second.elect()
    activity.release_lease(second.lease, path=path)


@pytest.mark.asyncio
async def test_foreground_defers_without_dropping_queue(tmp_path, monkeypatch):
    path = tmp_path / "activity.db"
    svc = service(tmp_path)
    called = []

    async def child(value):
        called.append(value)

    async def report(values):
        called.append("report")

    monkeypatch.setattr(dispatcher, "_discover", lambda values: (values, []))
    active = activity.acquire_lease("foreground", owner="actual-turn", path=path)
    runner = dispatcher.CognitiveDispatcher(
        path=path, service_loader=lambda: [svc], child_runner=child, report_runner=report
    )
    await runner.tick()
    assert runner.state["state"] == "foreground"
    assert called == []
    activity.release_lease(active, path=path)
    await runner.tick()
    await asyncio.sleep(0)
    assert svc in called
    activity.release_lease(runner.lease, path=path)


def test_diagnostics_read_does_not_initialize_ledger(tmp_path):
    path = tmp_path / "absent.db"
    assert dispatcher.dispatcher_status(path=path)["state"] == "not_started"
    assert not path.exists()


def test_child_preserves_installation_paths(tmp_path, monkeypatch):
    svc = service(tmp_path, "sales")
    from personas import capabilities, core

    monkeypatch.setattr(
        capabilities,
        "build_capability_scoped_env",
        lambda *a, **k: {"HOMIE_HOME": str(k["profile_root"])},
    )
    monkeypatch.setattr(core, "get_default_paths", lambda: {"workspace": tmp_path / "canonical"})
    monkeypatch.setenv("HOMIE_DEFAULT_PROFILE_ROOT", str(tmp_path / "canonical"))
    monkeypatch.setenv("ORCHESTRATION_DB_PATH", str(tmp_path / "orchestration.db"))
    env = dispatcher._child_env(svc)
    assert env["HOMIE_HOME"] == str(svc.target.memory_dir.parent)
    assert env["HOMIE_DEFAULT_PROFILE_ROOT"] == str(tmp_path / "canonical")
    assert env["ORCHESTRATION_DB_PATH"] == str(tmp_path / "orchestration.db")
    assert env["SECOND_BRAIN_RUNTIME_ACTIVITY_DB"] == str(activity.activity_db_path())


def test_prefetch_is_retained_as_observation_not_recalled_memory(tmp_path):
    svc = service(tmp_path)
    turn = hooks.prepare_turn(
        RuntimeRequest(prompt="Analyze the chart", cwd=".", task_name="review"),
        persona_id="default",
        surface="chat_engine",
        origin_id="chart-prefetch",
        service=svc,
    )
    turn.capture_sources(
        {"prefetched_context": "At 10:00 UTC the closed candle rejected 120.", "empty": ""}
    )
    observation = svc.store.all("observation")[0]
    assert observation["evidence"]["text"].startswith("At 10:00")
    assert observation["evidence"]["freshness"] == "as_reported_by_source"
    assert observation["domain_outcome_observed"] is False
    cycle = next(row for row in svc.store.all("cognitive_cycle") if row["phase"] == "interpret")
    assert observation["id"] in cycle["evidence_ids"]


def test_talk_mint_context_is_prepared_not_falsely_executed(tmp_path, monkeypatch):
    import personas
    import talk_session

    svc = service(tmp_path)
    experience = svc.capture_experience("source", "chat_engine", "Compare reversal evidence")
    understanding = svc.record_understanding(
        {
            "understanding_type": "belief",
            "title": "Reversal evidence",
            "content": "A reversal still needs a subsequent close.",
            "scope": "voice conversation reversal",
            "uncertainty": "Tentative; one chart",
            "evidence_ids": [experience["id"]],
        },
        source_key="belief-1",
    )
    monkeypatch.setattr(personas, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(hooks, "_service_for", lambda _: svc)
    prompt, state = talk_session._prepare_learning_session("Voice identity", "fake-model")
    assert "subsequent close" in prompt and state is not None
    receipt = svc.store.all("context")[0]
    assert receipt["phase"] == "prepared"
    assert receipt["status"] != "delivered"
    assert any(row.get("record_id") == understanding["id"] for row in receipt["included"])


def test_one_broken_profile_does_not_stop_other_discovery(tmp_path):
    from types import SimpleNamespace

    bad = SimpleNamespace(target=SimpleNamespace(persona_id="broken"))
    bad.enabled = lambda: (_ for _ in ()).throw(ValueError("malformed profile"))
    good = service(tmp_path)
    pending, errors = dispatcher._discover([bad, good])
    assert pending == []
    assert errors == [{"persona_id": "broken", "error_type": "ValueError"}]


@pytest.mark.asyncio
async def test_supervised_loop_retries_failures_with_backoff(tmp_path, monkeypatch):
    from personas.learning.errors import LearningUnavailableError

    runner = dispatcher.CognitiveDispatcher(path=tmp_path / "activity.db")
    calls, waits = [], []

    async def tick():
        calls.append(1)
        if len(calls) <= 2:
            raise LearningUnavailableError("temporary model outage")
        raise asyncio.CancelledError()

    async def no_renew():
        await asyncio.Event().wait()

    real_sleep = asyncio.sleep

    async def sleep(delay):
        waits.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(runner, "tick", tick)
    monkeypatch.setattr(runner, "renew", no_renew)
    monkeypatch.setattr(dispatcher.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await runner.run()
    assert len(calls) == 3 and waits == [10, 20]
    assert runner.state["consecutive_failures"] == 2


@pytest.mark.asyncio
async def test_expired_election_cancels_and_joins_previous_child_before_standby(
    tmp_path, monkeypatch
):
    import time

    path = tmp_path / "activity.db"
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    runner = dispatcher.CognitiveDispatcher(path=path)
    assert await runner.elect()
    child_started = asyncio.Event()
    child_stopped = asyncio.Event()

    async def child():
        child_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # A real child_runner terminates its provider process in this cleanup.
            await asyncio.sleep(0)
            child_stopped.set()

    running_child = asyncio.create_task(child())
    runner.worker = running_child
    await child_started.wait()
    clock[0] += dispatcher.LEASE_SECONDS + 1
    successor = activity.acquire_lease(
        "learning_dispatcher", owner="successor", exclusive=True, path=path,
        ttl_seconds=dispatcher.LEASE_SECONDS,
    )
    assert successor
    assert await runner.elect() is False
    assert child_stopped.is_set() and running_child.cancelled()
    assert runner.worker is None and runner.lease is None
    assert await runner.tick() == {"state": "standby"}
    activity.release_lease(successor, path=path)
