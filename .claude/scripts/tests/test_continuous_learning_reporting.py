"""Operator reports use temporary ledgers and fake inference, never live personas."""

from datetime import UTC, datetime

import pytest

from personas.learning import reporting
from personas.learning.models import LearningTarget, LearningValidationError
from personas.learning.operator import LearningOperator
from personas.learning.service import LearningService


@pytest.fixture
def services(tmp_path, monkeypatch):
    import config
    from personas.learning import store

    monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "true")
    monkeypatch.setenv("HOMIE_KILLSWITCH_HARNESS_LEARNING", "enabled")
    monkeypatch.setenv("DESK_WAKING_START", "08:00")
    monkeypatch.setenv("DESK_WAKING_END", "22:00")
    monkeypatch.setattr(config, "LOCAL_TZ", UTC)
    monkeypatch.setattr(config, "HEARTBEAT_ACTIVE_START", "08:00")
    monkeypatch.setattr(config, "HEARTBEAT_ACTIVE_END", "22:00")
    monkeypatch.setattr(store, "utc_now", lambda: "2026-09-10T17:00:00+00:00")
    result = []
    for name in ("default", "crypto"):
        root = tmp_path / name
        root.mkdir()
        result.append(
            LearningService(
                LearningTarget(
                    name, root / "memory", root / "data", root / "state", root / "skills"
                )
            )
        )
    return result


def retain(service, key="idea", **extra):
    return service.store.put(
        "understanding",
        {
            "understanding_type": "belief",
            "scope": "chart interpretation",
            "content": "A failed breakout needs follow-up candles.",
            "title": "Follow the breakout",
            "status": "tentative",
            "evidence_ids": [],
            "uncertainty": "One observed case",
            **extra,
        },
        key=key,
    )


def report(service):
    return reporting.build_learning_report(
        service, since="2026-09-10T00:00:00Z", until="2026-09-11T00:00:00Z"
    )


def test_empty_report_is_read_only_and_period_validation(services):
    service = services[0]
    result = report(service)
    assert not result["has_activity"]
    assert not service.target.data_dir.exists()
    for start, end in (
        ("nonsense", None),
        ("2026-01-01", None),
        ("2026-09-11T00:00:00Z", "2026-09-10T00:00:00Z"),
    ):
        with pytest.raises(LearningValidationError):
            reporting.build_learning_report(service, since=start, until=end)
    assert not service.target.data_dir.exists()


def test_counts_distinguish_conclusions_changes_trials_and_real_qualification(services):
    service = services[1]
    first = retain(service)
    retain(service, "duplicate")
    retain(
        service,
        "revision",
        content="The breakout held after the next candle closed.",
        predecessor_id=first["id"],
    )
    for mode in ("manifest", "case", "trial", "support", "qualification"):
        service.store.put(
            "evaluation", {"mode": mode, "candidate_id": "candidate", "passed": True}, key=mode
        )
    service.store.put(
        "evaluation",
        {"mode": "knowledge_support", "understanding_id": first["id"], "passed": True},
        key="understanding-support",
    )
    service.store.put(
        "context",
        {
            "phase": "prepared",
            "included": [{"record_kind": "understanding", "record_id": first["id"]}],
        },
        key="prepared",
    )
    service.store.put(
        "context",
        {
            "phase": "executed",
            "included": [{"record_kind": "understanding", "record_id": first["id"]}],
        },
        key="executed",
    )
    result = report(service)
    assert result["counts"]["understanding_changes"] == 3
    assert result["counts"]["distinct_conclusions"] == 2
    assert result["counts"]["qualification_attempts"] == 1
    assert result["counts"]["understanding_support_checks"] == 1
    assert result["counts"]["delivered_contexts"] == 1
    assert result["counts"]["methods_adopted"] == 0


def test_links_include_versions_and_never_cross_personas(services):
    own = retain(services[0])
    foreign = retain(services[1])
    context = services[0].store.put(
        "context",
        {
            "phase": "executed",
            "included": [
                {"record_id": own["id"], "record_kind": "understanding"},
                {"record_id": foreign["id"], "record_kind": "understanding"},
            ],
        },
        key="context",
    )
    result = LearningOperator(services[0]).get_record(context["id"])
    assert [link["id"] for link in result["links"]] == [own["id"]]
    assert (
        LearningOperator(services[0]).list_records("understanding")["records"][0]["id"] == own["id"]
    )


@pytest.mark.asyncio
async def test_explanation_reuses_completed_receipt_and_cannot_change_host_counts(services):
    service = services[0]
    retain(service)
    calls = []

    async def infer(_service, snapshot):
        calls.append(snapshot)
        return {
            "narrative": "One tentative conclusion was recorded.",
            "provider": "fake",
            "model": "fake",
            "counts": {"distinct_conclusions": 999},
        }

    first = await reporting.explain_learning_report(service, report(service), runtime_fn=infer)
    second = await reporting.explain_learning_report(service, report(service), runtime_fn=infer)
    assert first == second
    assert len(calls) == 1
    assert first["counts"]["distinct_conclusions"] == 1
    assert not service.store.all("experience")


@pytest.mark.asyncio
async def test_empty_explanation_spends_no_tokens(services):
    async def never(*args):
        raise AssertionError("Empty reports must not call a model")

    result = await reporting.explain_learning_report(
        services[0], report(services[0]), runtime_fn=never
    )
    assert result["narrative_status"] == "empty"
    assert not services[0].target.data_dir.exists()


@pytest.mark.asyncio
async def test_daily_recap_is_combined_due_quiet_aware_and_dedupes_after_delivery(services):
    from cognition.proactive_actions import ProactiveActionQueue

    retain(services[1])
    calls = []

    async def infer(_owner, snapshot):
        calls.append(snapshot)
        return {
            "narrative": "Crypto retained a tentative chart interpretation.",
            "provider": "fake",
            "model": "fake",
        }

    assert (
        await reporting.dispatch_learning_reports(
            services, now=datetime(2026, 9, 10, 17, tzinfo=UTC), runtime_fn=infer
        )
    )["status"] == "empty"
    assert (
        await reporting.dispatch_learning_reports(
            services, now=datetime(2026, 9, 10, 23, tzinfo=UTC), runtime_fn=infer
        )
    )["status"] == "quiet_hours"
    result = await reporting.dispatch_learning_reports(
        services, now=datetime(2026, 9, 10, 18, 1, tzinfo=UTC), runtime_fn=infer
    )
    assert result["queued"] == 1
    assert calls[0]["personas"][0]["persona_id"] == "crypto"
    queue = ProactiveActionQueue(services[0].target.state_dir / "proactive-actions.jsonl")
    action = queue.read_all()[0]
    assert queue.mark(action.id, dispatch_status="dispatched")
    # A crash after queue publication but before the settings receipt must not resend.
    services[0].store.set_setting("learning:daily:2026-09-10", None)
    await reporting.dispatch_learning_reports(
        services, now=datetime(2026, 9, 10, 18, 2, tzinfo=UTC), runtime_fn=infer
    )
    assert len(queue.read_all()) == len(calls) == 1


@pytest.mark.asyncio
async def test_provider_outage_keeps_daily_report_retryable(services):
    retain(services[0])

    async def unavailable(*args):
        raise TimeoutError("Provider unavailable")

    with pytest.raises(TimeoutError):
        await reporting.dispatch_learning_reports(
            services, now=datetime(2026, 9, 10, 18, tzinfo=UTC), runtime_fn=unavailable
        )
    assert not services[0].store.setting("learning:daily:2026-09-10")


@pytest.mark.asyncio
async def test_morning_wake_delivers_previous_evening_recap_after_quiet_hours(services):
    retain(services[1])

    async def infer(*args):
        return {
            "narrative": "Recorded change with uncertainty.",
            "provider": "fake",
            "model": "fake",
        }

    assert (
        await reporting.dispatch_learning_reports(
            services, now=datetime(2026, 9, 10, 23, tzinfo=UTC), runtime_fn=infer
        )
    )["status"] == "quiet_hours"
    result = await reporting.dispatch_learning_reports(
        services, now=datetime(2026, 9, 11, 9, tzinfo=UTC), runtime_fn=infer
    )
    assert result["queued"] == 1
    assert services[0].store.setting("learning:daily:2026-09-10")


@pytest.mark.asyncio
async def test_revised_understanding_queues_one_important_update(services):
    old = retain(services[1])
    retain(
        services[1],
        "changed",
        content="The later evidence contradicts that idea.",
        predecessor_id=old["id"],
    )
    deliveries = []

    def enqueue(owner, **data):
        deliveries.append((owner.target.persona_id, data))
        return True

    for _ in range(2):
        await reporting.dispatch_learning_reports(
            services, now=datetime(2026, 9, 10, 12, tzinfo=UTC), enqueue_fn=enqueue
        )
    assert len(deliveries) == 1
    assert deliveries[0][0] == "default"
    assert deliveries[0][1]["urgency"] == 3


def test_old_investigation_completed_in_period_is_reported_once(services, monkeypatch):
    from personas.learning import store

    service = services[0]
    monkeypatch.setattr(store, "utc_now", lambda: "2026-09-09T10:00:00+00:00")
    inquiry = service.store.put(
        "investigation", {"status": "open", "question": "What followed?"}, key="older-inquiry"
    )
    monkeypatch.setattr(store, "utc_now", lambda: "2026-09-10T10:00:00+00:00")
    for _ in range(2):
        service.store.event(
            inquiry["id"],
            "investigation_transition",
            {"status": "completed", "conclusion": "The next observation resolved the question."},
            key="completion",
        )
    result = report(service)
    assert result["counts"]["investigations_opened"] == 0
    assert result["counts"]["investigations_completed"] == 1
    assert [row["id"] for row in result["records"]] == [inquiry["id"]]
