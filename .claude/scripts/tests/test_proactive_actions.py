"""Tests for proactive cognition action queue."""

from __future__ import annotations

import sys
from pathlib import Path

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.proactive_actions import (  # noqa: E402
    ProactiveAction,
    ProactiveActionQueue,
    evaluate_action_policy,
)


def test_proactive_action_queue_appends_and_dispatches_console(tmp_path: Path) -> None:
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    action = ProactiveAction(
        source="test",
        reason="Important follow-up",
        urgency=4,
        message="Review autonomous memory proof.",
        evidence_paths=["validation://future-behavior"],
    )

    assert queue.append(action) is True
    allowed, reason = evaluate_action_policy(action)
    assert allowed is True
    assert reason == "local_operator_notification"
    assert queue.dispatch_console(action.id) is True

    stored = queue.read_all()[0]
    assert stored.dispatch_status == "dispatched"
    assert stored.result == "console_operator_notification"


def test_proactive_action_queue_dedupes_active_actions(tmp_path: Path) -> None:
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    first = ProactiveAction(source="test", message="Same follow-up")
    duplicate = ProactiveAction(source="test", message="Same follow-up")

    assert queue.append(first) is True
    assert queue.append(duplicate) is False
    assert len(queue.read_queued()) == 1


def test_mark_holds_the_same_lock_as_append(tmp_path: Path) -> None:
    """Regression for issue #171's secondary finding: mark() must not race
    a concurrent append() with an unlocked read-then-rewrite."""
    import threading

    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    seed = ProactiveAction(source="test", message="seed action")
    queue.append(seed)

    errors: list[Exception] = []

    def _append_many() -> None:
        try:
            for i in range(20):
                queue.append(ProactiveAction(source="test", message=f"concurrent {i}"))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    def _mark_many() -> None:
        try:
            for _ in range(20):
                queue.mark(seed.id, dispatch_status="dispatched", result="test")
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    t1 = threading.Thread(target=_append_many)
    t2 = threading.Thread(target=_mark_many)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    all_actions = queue.read_all()
    # The seeded action plus all 20 concurrently-appended actions must
    # survive — an unlocked mark() rewrite racing append() would drop rows.
    assert len(all_actions) == 21
    seeded = next(a for a in all_actions if a.id == seed.id)
    assert seeded.dispatch_status == "dispatched"


def test_mark_expired_status_round_trips(tmp_path: Path) -> None:
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    action = ProactiveAction(source="test", message="stale action")
    queue.append(action)

    assert queue.mark(action.id, dispatch_status="expired", result="backlog_expired") is True
    stored = queue.read_all()[0]
    assert stored.dispatch_status == "expired"
    # expired actions must not resurface via read_queued()
    assert queue.read_queued() == []
