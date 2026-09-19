"""Session clear remains recoverable when learning storage or acknowledgement fails."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from personas.learning import hooks, lifecycle_outbox, worker
from personas.learning.models import LearningError, LearningTarget
from personas.learning.service import LearningService


def service(tmp_path, name="default"):
    root = tmp_path / name
    return LearningService(
        LearningTarget(name, root / "memory", root / "data", root / "state", root / "skills")
    )


def values():
    return dict(
        persona_id="default",
        session_id="turn-1",
        surface="session_clear",
        transcript="User: Why did it reverse?\nHomie: Another candle is needed.",
        reason="clear",
    )


def pending(svc):
    return list((svc.target.state_dir / "learning-lifecycle-outbox").glob("*.json"))


def unavailable(*a, **kw):
    raise LearningError("simulated store unavailable")


@pytest.mark.parametrize(
    "failing_method",
    ["enabled", "capture_experience", "record_observation", "enqueue_cognitive_cycle"],
)
def test_capture_failure_is_durable_before_the_first_database_attempt(
    tmp_path, monkeypatch, failing_method
):
    svc = service(tmp_path)
    monkeypatch.setattr(svc, failing_method, unavailable)
    result = hooks.enqueue_session_debrief(service=svc, **values())
    assert result["status"] == "deferred"
    assert len(pending(svc)) == 1
    if failing_method in {"enabled", "capture_experience"}:
        assert not svc.store.path.exists()
    restored = LearningService(svc.target)
    worker.discover_work(restored)
    assert len(restored.store.all("cognitive_cycle")) == 1
    assert len(restored.store.all("experience")) == 1
    assert len(restored.store.all("observation")) == 1
    assert not pending(restored)
    worker.discover_work(restored)
    assert len(restored.store.all("cognitive_cycle")) == 1


def test_paused_outbox_waits_for_resume_without_ingesting(tmp_path):
    svc = service(tmp_path)
    svc.set_paused(True)
    assert hooks.enqueue_session_debrief(service=svc, **values())["status"] == "disabled"
    assert len(pending(svc)) == 1
    assert worker.discover_work(svc) == 0
    assert not svc.store.all("experience")
    svc.set_paused(False)
    worker.discover_work(svc)
    assert len(svc.store.all("cognitive_cycle")) == 1
    assert not pending(svc)


def test_missing_acknowledgement_replays_the_same_committed_cycle(tmp_path, monkeypatch):
    svc = service(tmp_path)
    original = Path.unlink

    def refuse_ack(path, *a, **kw):
        if path.parent.name == "learning-lifecycle-outbox" and path.suffix == ".json":
            raise OSError("simulated acknowledgement outage")
        return original(path, *a, **kw)

    monkeypatch.setattr(Path, "unlink", refuse_ack)
    first = hooks.enqueue_session_debrief(service=svc, **values())
    assert first["status"] == "queued" and len(pending(svc)) == 1
    monkeypatch.setattr(Path, "unlink", original)
    restored = LearningService(svc.target)
    assert lifecycle_outbox.replay_pending(restored)["delivered"] == 1
    assert len(restored.store.all("cognitive_cycle")) == 1
    assert restored.store.all("cognitive_cycle")[0]["id"] == first["cognitive_cycle_id"]


def test_wrong_persona_and_tampered_secret_fields_never_enter_learning(tmp_path):
    svc = service(tmp_path)
    other = service(tmp_path, "crypto")
    with pytest.raises(LearningError, match="another persona"):
        hooks.enqueue_session_debrief(service=other, **values())
    assert not other.target.state_dir.exists()
    saved = lifecycle_outbox.persist_session_debrief(svc, **values())
    destination = lifecycle_outbox._directory(other)
    destination.mkdir(parents=True)
    (destination / saved.name).write_bytes(saved.read_bytes())
    assert lifecycle_outbox.replay_pending(other)["rejected"] == 1
    assert not other.store.path.exists()
    entry = json.loads(saved.read_text())
    entry["api_key"] = "untrusted-secret"
    saved.write_text(json.dumps(entry))
    assert lifecycle_outbox.replay_pending(svc)["rejected"] == 1
    assert not svc.store.path.exists()


def test_outbox_redacts_source_credentials_before_any_persistence(tmp_path, monkeypatch):
    svc = service(tmp_path)
    import security.redact as redactor

    monkeypatch.setattr(redactor, "_REDACT_ENABLED", True)
    data = values() | {
        "transcript": "Observe candle. OPENAI_API_KEY=<REDACTED-openai>"
    }
    monkeypatch.setattr(svc, "capture_experience", unavailable)
    hooks.enqueue_session_debrief(service=svc, **data)
    serialized = pending(svc)[0].read_text()
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in serialized
    restored = LearningService(svc.target)
    worker.discover_work(restored)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in str(restored.store.all())
    assert restored.store.all("observation")[0]["evidence"][
        "transcript_hash"
    ] == lifecycle_outbox._hash(data["transcript"])


def seed_chat(tmp_path):
    from session import Session, SQLiteSessionStore
    from session_keys import build_session_key

    now = datetime.now()
    store = SQLiteSessionStore(tmp_path / "chat.db")
    session = Session(
        session_id=build_session_key("cli", "channel", "thread"),
        agent_session_id="runtime",
        platform="cli",
        channel_id="channel",
        thread_id="thread",
        user_id="user",
        created_at=now,
        updated_at=now,
        message_count=2,
    )
    store.create(session)
    store.add_message(session.session_id, "user", "Examine the chart", now)
    store.add_message(session.session_id, "assistant", "Need to revisit the reversal", now)
    return store, session


def test_clear_deletes_session_then_restart_replays_exactly_one_debrief(tmp_path, monkeypatch):
    import session_lifecycle_hooks as lifecycle

    import personas

    svc = service(tmp_path)
    store, session = seed_chat(tmp_path)
    monkeypatch.setattr(lifecycle, "get_state_dir", lambda: svc.target.state_dir)
    monkeypatch.setattr(
        lifecycle, "run_hook_script", lambda name, *a, **kw: lifecycle.HookInvocation(name, 0)
    )
    monkeypatch.setattr(personas, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(hooks, "_service_for", lambda _: svc)
    monkeypatch.setattr(svc, "capture_experience", unavailable)
    result = lifecycle.clear_session_with_lifecycle(
        store=store, session=session, platform="cli", channel_id="channel", thread_id="thread"
    )
    assert not result.session_retained
    assert store.get("cli", "channel", "thread") is None
    assert result.transcript_path.exists() and len(pending(svc)) == 1
    restarted = LearningService(svc.target)
    worker.discover_work(restarted)
    worker.discover_work(restarted)
    assert len(restarted.store.all("cognitive_cycle")) == 1
    assert not pending(restarted)


def test_outbox_filesystem_failure_can_still_acknowledge_durable_database(tmp_path, monkeypatch):
    svc = service(tmp_path)
    monkeypatch.setattr(
        lifecycle_outbox,
        "persist_session_debrief",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("outbox down")),
    )
    result = hooks.enqueue_session_debrief(service=svc, **values())
    assert result["status"] == "queued"
    assert len(svc.store.all("cognitive_cycle")) == 1


def test_losing_outbox_and_database_preserves_clear_session(tmp_path, monkeypatch):
    import session_lifecycle_hooks as lifecycle

    import personas

    svc = service(tmp_path)
    store, session = seed_chat(tmp_path)
    monkeypatch.setattr(lifecycle, "get_state_dir", lambda: svc.target.state_dir)
    monkeypatch.setattr(personas, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(hooks, "_service_for", lambda _: svc)
    monkeypatch.setattr(svc, "capture_experience", unavailable)
    monkeypatch.setattr(
        lifecycle_outbox,
        "persist_session_debrief",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("outbox down")),
    )
    result = lifecycle.clear_session_with_lifecycle(
        store=store, session=session, platform="cli", channel_id="channel", thread_id="thread"
    )
    assert result.session_retained
    assert store.get("cli", "channel", "thread") is not None
    assert "cognitive_debrief" in result.warning_summary()


@pytest.mark.asyncio
async def test_clear_command_does_not_claim_success_when_both_destinations_fail(
    tmp_path, monkeypatch
):
    import core_handlers
    import session_lifecycle_hooks as lifecycle

    import personas

    svc = service(tmp_path)
    store, session = seed_chat(tmp_path)
    monkeypatch.setattr(lifecycle, "get_state_dir", lambda: svc.target.state_dir)
    monkeypatch.setattr(personas, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(hooks, "_service_for", lambda _: svc)
    monkeypatch.setattr(svc, "capture_experience", unavailable)
    monkeypatch.setattr(
        lifecycle_outbox,
        "persist_session_debrief",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("outbox down")),
    )
    monkeypatch.setattr(
        core_handlers, "_get_session", lambda _: (store, session, "cli", "channel", "thread")
    )
    response = await core_handlers.handle_clear(None, object(), "")
    assert response.startswith("Session was not cleared")
    assert store.get("cli", "channel", "thread") is not None
