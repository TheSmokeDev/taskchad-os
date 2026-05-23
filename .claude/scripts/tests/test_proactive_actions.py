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
