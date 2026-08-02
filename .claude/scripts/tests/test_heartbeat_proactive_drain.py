"""Tests for the proactive-action drain (Living Mind Act 3 closer, issue #171).

Test design mirrors test_heartbeat_blockers.py / test_heartbeat_observations.py:
  1. Delivery — a queued operator_notification action is dispatched via the
     notifier and marked dispatched (mock notifier, injected queue/clock).
  2. Backlog expiry — actions older than max_age_days are marked expired,
     never dispatched, and never printed as an alert.
  3. Per-run cap — more eligible actions than max_dispatch_per_run leaves
     the rest queued for the next run.
  4. Policy routing — a non-operator_notification action is routed through
     evaluate_action_policy() (mocked to deny) and marked policy_rejected,
     never dispatched directly (no mutation bypass).
  5. Fail-open — a notifier exception is caught, the action lands in
     report["failed"], and the drain continues to the next action.
  6. run_heartbeat() ordering — the drain call site is wrapped in a fail-open
     try/except so its failure does not abort the heartbeat run.
"""

from __future__ import annotations

import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cognition.proactive_actions import ProactiveAction, ProactiveActionQueue  # noqa: E402

import heartbeat  # noqa: E402

TZ = timezone(timedelta(hours=-5))


def _dt(year, month, day, hour=12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=TZ)


def test_drain_dispatches_queued_notification_and_marks_dispatched(tmp_path, monkeypatch):
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    action = ProactiveAction(
        source="cognitive_pass", message="Review the drafted reply.",
        created_at=_dt(2026, 7, 20).isoformat(),
    )
    queue.append(action)

    sent = []
    monkeypatch.setattr(
        heartbeat, "send_toast_notification",
        lambda title, message, **kw: sent.append((title, message)) or None,
    )

    report = heartbeat.drain_proactive_actions(queue=queue, now=_dt(2026, 7, 20, 14))

    assert report["dispatched"] == [action.id]
    assert sent and sent[0][1] == action.message
    assert queue.read_all()[0].dispatch_status == "dispatched"


def test_drain_expires_backlog_without_dispatching(tmp_path, monkeypatch):
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    stale = ProactiveAction(
        source="cognitive_pass", message="Old proposal",
        created_at=_dt(2026, 7, 1).isoformat(),
    )
    queue.append(stale)

    sent = []
    monkeypatch.setattr(
        heartbeat, "send_toast_notification",
        lambda *a, **kw: sent.append(a) or None,
    )

    report = heartbeat.drain_proactive_actions(
        queue=queue, max_age_days=7, now=_dt(2026, 7, 20)
    )

    assert report["expired"] == [stale.id]
    assert not sent
    assert queue.read_all()[0].dispatch_status == "expired"


def test_drain_respects_per_run_dispatch_cap(tmp_path, monkeypatch):
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    actions = [
        ProactiveAction(
            source="cognitive_pass", message=f"action {i}",
            created_at=_dt(2026, 7, 20, 10 + i).isoformat(),
        )
        for i in range(5)
    ]
    for a in actions:
        queue.append(a)

    monkeypatch.setattr(heartbeat, "send_toast_notification", lambda *a, **kw: None)

    report = heartbeat.drain_proactive_actions(
        queue=queue, max_dispatch_per_run=2, now=_dt(2026, 7, 20, 16)
    )

    assert len(report["dispatched"]) == 2
    remaining_queued = [a for a in queue.read_all() if a.dispatch_status == "queued"]
    assert len(remaining_queued) == 3


def test_drain_routes_non_notification_channel_through_policy_gate(tmp_path, monkeypatch):
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    action = ProactiveAction(
        source="cognitive_pass", channel="integration_action",
        integration="slack", action="post",
        message="Would post to Slack.",
        created_at=_dt(2026, 7, 20, 10).isoformat(),
    )
    queue.append(action)

    monkeypatch.setattr(
        "cognition.proactive_actions.require_integration_action",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("denied")),
    )
    sent = []
    monkeypatch.setattr(heartbeat, "send_toast_notification", lambda *a, **kw: sent.append(a))

    report = heartbeat.drain_proactive_actions(queue=queue, now=_dt(2026, 7, 20, 16))

    assert report["policy_rejected"] == [action.id]
    assert not sent
    assert queue.read_all()[0].dispatch_status == "policy_rejected"


def test_drain_notifier_failure_is_fail_open_and_continues(tmp_path, monkeypatch):
    """Scenario 5: a notifier exception on one action is caught (action lands
    in report['failed']), and the drain continues to dispatch the next one."""
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    boom = ProactiveAction(
        source="cognitive_pass", message="first — notifier throws",
        created_at=_dt(2026, 7, 20, 10).isoformat(),
    )
    ok = ProactiveAction(
        source="cognitive_pass", message="second — should still dispatch",
        created_at=_dt(2026, 7, 20, 11).isoformat(),
    )
    queue.append(boom)
    queue.append(ok)

    calls = {"n": 0}

    def _flaky(title, message, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("toast pipe broke")
        return None

    monkeypatch.setattr(heartbeat, "send_toast_notification", _flaky)

    report = heartbeat.drain_proactive_actions(queue=queue, now=_dt(2026, 7, 20, 16))

    assert report["failed"] == [boom.id]
    assert report["dispatched"] == [ok.id]
    statuses = {a.id: a.dispatch_status for a in queue.read_all()}
    # The failed action stays queued (never marked) so a later run can retry;
    # the second action was still delivered despite the first one's failure.
    assert statuses[boom.id] == "queued"
    assert statuses[ok.id] == "dispatched"


def test_drain_mark_failure_after_successful_notify_stays_queued_for_retry(
    tmp_path, monkeypatch, capsys
):
    """A queue.mark() failure AFTER the notification already went out (e.g. a
    lock timeout or disk error on the .jsonl rewrite) must not be treated the
    same as a notify failure: the notification already reached the operator,
    so the log must say so distinctly (not just 'dispatch failed'), and the
    action stays queued so it is retried (accepted duplicate-over-drop
    tradeoff) rather than being silently lost."""
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    action = ProactiveAction(
        source="cognitive_pass", message="notify ok, mark() blows up",
        created_at=_dt(2026, 7, 20, 10).isoformat(),
    )
    queue.append(action)

    sent = []
    monkeypatch.setattr(
        heartbeat, "send_toast_notification",
        lambda title, message, **kw: sent.append((title, message)) or None,
    )
    monkeypatch.setattr(
        queue, "mark",
        lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("lock wait exceeded")),
    )

    report = heartbeat.drain_proactive_actions(queue=queue, now=_dt(2026, 7, 20, 14))

    assert sent  # the notification was actually delivered
    assert report["failed"] == [action.id]
    assert report["dispatched"] == []
    assert queue.read_all()[0].dispatch_status == "queued"
    out = capsys.readouterr().out
    assert "ALREADY NOTIFIED" in out


def test_drain_cap_counts_notifies_not_mark_success(tmp_path, monkeypatch):
    """Codex gate MINOR on PR #177: the per-run anti-burst cap must count the
    OPERATOR-VISIBLE notify, not mark() success. With every mark() failing but
    every notify succeeding, the old behavior (increment on mark success) left
    the counter at 0 and toasted the whole backlog in one run — defeating the
    cap exactly when it matters. The cap must still hold at max_dispatch_per_run
    operator-visible toasts."""
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    for i in range(5):
        queue.append(ProactiveAction(
            source="cognitive_pass", message=f"action {i}",
            created_at=_dt(2026, 7, 20, 10 + i).isoformat(),
        ))

    sent = []
    monkeypatch.setattr(
        heartbeat, "send_toast_notification",
        lambda title, message, **kw: sent.append(message) or None,
    )
    # Every mark() fails — the old cap (increment on mark success) would never
    # trip and all 5 would be toasted.
    monkeypatch.setattr(
        queue, "mark",
        lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("lock wait exceeded")),
    )

    heartbeat.drain_proactive_actions(
        queue=queue, max_dispatch_per_run=2, now=_dt(2026, 7, 20, 16)
    )

    # Exactly the cap number of operator-visible toasts, no burst.
    assert len(sent) == 2


def test_run_heartbeat_wraps_drain_call_in_fail_open_guard():
    """Scenario 6: the run_heartbeat() call site guards drain_proactive_actions
    in a try/except so a drain failure is non-fatal to the heartbeat run."""
    src = inspect.getsource(heartbeat.run_heartbeat)
    lines = [ln.strip() for ln in src.splitlines()]
    assert "drain_proactive_actions(test_mode=test_mode)" in lines
    call_idx = lines.index("drain_proactive_actions(test_mode=test_mode)")
    # The line immediately above the call is the `try:` opening the fail-open
    # guard — the same shape as the blocker/observation pipelines above it.
    assert lines[call_idx - 1] == "try:"
    assert "Proactive action drain error (non-fatal)" in src


def test_drain_reads_env_knobs_when_kwargs_omitted(tmp_path, monkeypatch):
    """Rule 1: _proactive_drain_settings() must resolve os.getenv() at call
    time — no bound default arg, no module-load-time snapshot."""
    monkeypatch.setenv("HEARTBEAT_PROACTIVE_DRAIN_MAX_PER_RUN", "1")
    monkeypatch.setenv("HEARTBEAT_PROACTIVE_DRAIN_MAX_AGE_DAYS", "3")
    monkeypatch.setattr(heartbeat, "send_toast_notification", lambda *a, **kw: None)

    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    for i in range(3):
        queue.append(ProactiveAction(
            source="cognitive_pass", message=f"action {i}",
            created_at=_dt(2026, 7, 20, 10 + i).isoformat(),
        ))

    # No max_dispatch_per_run kwarg — must resolve from the env var just set.
    report = heartbeat.drain_proactive_actions(queue=queue, now=_dt(2026, 7, 20, 16))

    assert len(report["dispatched"]) == 1


def test_drain_does_not_report_policy_rejected_when_mark_fails_silently(tmp_path, monkeypatch):
    """If queue.mark() returns False (record not found) for a policy-rejected
    action, the drain must not claim it as handled — else the on-disk record
    stays 'queued' while the report says otherwise, and the same action gets
    re-processed (and re-reported) on the next run."""
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    action = ProactiveAction(
        source="cognitive_pass", channel="integration_action",
        integration="slack", action="post", message="Would post to Slack.",
        created_at=_dt(2026, 7, 20, 10).isoformat(),
    )
    queue.append(action)

    monkeypatch.setattr(
        "cognition.proactive_actions.require_integration_action",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("denied")),
    )
    real_mark = queue.mark
    monkeypatch.setattr(
        queue, "mark",
        lambda *a, **kw: False if a and a[0] == action.id else real_mark(*a, **kw),
    )

    report = heartbeat.drain_proactive_actions(queue=queue, now=_dt(2026, 7, 20, 16))

    assert action.id not in report["policy_rejected"]
    assert action.id in report["failed"]


def test_drain_default_queue_wires_to_config_path(tmp_path, monkeypatch):
    """The sole production call site (run_heartbeat) never passes `queue=` —
    this proves the queue=None default actually resolves to a working queue
    via config.PROACTIVE_ACTION_QUEUE_FILE."""
    import config as scripts_config

    queue_path = tmp_path / "actions.jsonl"
    monkeypatch.setattr(scripts_config, "PROACTIVE_ACTION_QUEUE_FILE", queue_path)

    seed_queue = ProactiveActionQueue(queue_path)
    seed_queue.append(ProactiveAction(
        source="cognitive_pass", message="via default queue wiring",
        created_at=_dt(2026, 7, 20, 10).isoformat(),
    ))

    monkeypatch.setattr(heartbeat, "send_toast_notification", lambda *a, **kw: None)

    report = heartbeat.drain_proactive_actions(now=_dt(2026, 7, 20, 16))  # queue=None

    assert len(report["dispatched"]) == 1


def test_drain_dispatches_action_with_unparseable_created_at(tmp_path, monkeypatch):
    """A corrupted/hand-edited created_at must not silently drop the action
    or crash the drain — age becomes unknown, so it falls through to normal
    dispatch/policy handling instead of expiring."""
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    action = ProactiveAction(source="cognitive_pass", message="corrupted timestamp")
    action.created_at = "not-a-real-timestamp"
    queue.append(action)

    monkeypatch.setattr(heartbeat, "send_toast_notification", lambda *a, **kw: None)
    report = heartbeat.drain_proactive_actions(queue=queue, now=_dt(2026, 7, 20, 16))

    assert report["dispatched"] == [action.id]


def test_drain_expires_full_backlog_independent_of_dispatch_cap(tmp_path, monkeypatch):
    """Expiry must not be starved or capped by max_dispatch_per_run — all
    eligible-for-expiry actions expire regardless of how many eligible
    actions are waiting behind the dispatch cap."""
    queue = ProactiveActionQueue(tmp_path / "actions.jsonl")
    stale = [
        ProactiveAction(
            source="cognitive_pass", message=f"stale {i}",
            created_at=_dt(2026, 7, 1, 10 + i).isoformat(),
        )
        for i in range(2)
    ]
    fresh = [
        ProactiveAction(
            source="cognitive_pass", message=f"fresh {i}",
            created_at=_dt(2026, 7, 20, 10 + i).isoformat(),
        )
        for i in range(5)
    ]
    for a in stale + fresh:
        queue.append(a)

    monkeypatch.setattr(heartbeat, "send_toast_notification", lambda *a, **kw: None)

    report = heartbeat.drain_proactive_actions(
        queue=queue, max_age_days=7, max_dispatch_per_run=2, now=_dt(2026, 7, 20, 16)
    )

    assert len(report["expired"]) == 2
    assert len(report["dispatched"]) == 2
    remaining_queued = [a for a in queue.read_all() if a.dispatch_status == "queued"]
    assert len(remaining_queued) == 3
