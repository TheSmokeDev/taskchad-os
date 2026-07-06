"""Tests for the cofounder v2 WS5 reporting loop (cofounder/report.py).

Path map (one test per distinct path, adversarial first):
  Gates
  - cofounder_delegation kill switch = refused + counted, zero services
  - COFOUNDER_REPORT_ENABLED default false = disabled
  - nothing to do = idle, no card
  Ingestion
  - done result flips the agenda line delegated->done (summary +
    deliverable stamped), delivery acked, audit row written
  - failed result flips ->failed AND fails the convoy subtask
  - dispatched result flips ->dispatched and stamps run_id/branch/subtask_id
  - garbage result body = ingested as failed-shape record, still acked,
    pass continues
  - dry run never claims (delivery stays pending), no card
  Polling
  - finished archon run (completed) flips dispatched->done + completes the
    subtask; failed run flips ->failed + fails the subtask
  - unknown/unreadable run row = conservatively left dispatched
  Cards
  - intraday pulse sent once per tick with changes; muted when
    COFOUNDER_REPORT_NOTIFY=false; global mute (empty COFOUNDER_NOTIFY_LEVELS)
    wins
  - EOD checkout: hour-gated, once daily (state marker), deterministic
    render carries per-line marks + delegations-spent
  Config
  - Rule-1 env round-trip
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import config
from cofounder import delegate as delegate_mod
from cofounder import report as report_mod
from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import (
    CofounderResultPayload,
    CreateConvoyInput,
    CreateSubtaskInput,
)
from security import kill_switches

TODAY = "2026-07-05"
EVENING = datetime(2026, 7, 5, 19, 0)
MORNING = datetime(2026, 7, 5, 10, 0)

ENV_KEYS = (
    "HOMIE_KILLSWITCH_COFOUNDER_DELEGATION",
    "COFOUNDER_REPORT_ENABLED",
    "COFOUNDER_REPORT_NOTIFY",
    "COFOUNDER_CHECKOUT_HOUR",
    "COFOUNDER_REPORT_POLL_DAYS",
    "COFOUNDER_NOTIFY_LEVELS",
    "COFOUNDER_PROJECTS_DIR",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def reset_counters():
    kill_switches._REFUSAL_COUNTERS.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()


@pytest.fixture
def services():
    db = OrchestrationDB(":memory:")
    return ConvoyService(db), MailboxService(db)


@pytest.fixture(autouse=True)
def isolated_audit(tmp_path, monkeypatch):
    path = tmp_path / "delegation-audit.jsonl"
    monkeypatch.setattr(
        delegate_mod,
        "_resolve_audit_path",
        lambda audit_path=None: Path(audit_path) if audit_path else path,
    )
    return path


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    from cofounder import state as state_mod

    path = tmp_path / "report-state.json"
    monkeypatch.setattr(
        state_mod,
        "_resolve_state_file",
        lambda sf: Path(sf) if sf is not None else path,
    )
    return path


def _agenda(tmp_path: Path, items: list[dict]) -> Path:
    agendas = tmp_path / "cofounder" / "agendas"
    agendas.mkdir(parents=True, exist_ok=True)
    path = agendas / f"AGENDA-{TODAY}.json"
    path.write_text(
        json.dumps({"date": TODAY, "summary": "s", "items": items}), encoding="utf-8"
    )
    return path


def _item(n=1, status="delegated", **kw):
    base = {
        "n": n,
        "persona": "sales",
        "repo": "YourProduct",
        "task": "close the leads",
        "why": "w",
        "priority": 1,
        "mode": "draft",
        "status": status,
    }
    base.update(kw)
    return base


def _subtask(services, persona="sales"):
    convoy_service, _ = services
    created = convoy_service.create_convoy(
        CreateConvoyInput(
            title="[cofounder] t",
            created_by="cofounder",
            subtasks=[CreateSubtaskInput(title="t", assigned_agent_id=persona)],
        )
    )
    return created.convoy.id, created.subtasks[0].id


def _send_result(services, *, status="done", n=1, subtask_id=0, convoy_id=None, **kw):
    _, mailbox_service = services
    payload = CofounderResultPayload(
        subtask_id=subtask_id,
        agenda_ref=f"AGENDA-{TODAY}.md#{n}",
        status=status,
        summary=kw.pop("summary", f"{status} summary"),
        **kw,
    )
    return mailbox_service.send_cofounder_result(
        "sales", "cofounder", payload, convoy_id=convoy_id
    )


def _pass(tmp_path, services, **kwargs):
    kwargs.setdefault(
        "report_settings", config.get_cofounder_report_settings(enabled=True)
    )
    kwargs.setdefault(
        "settings",
        config.get_cofounder_settings(projects_dir=tmp_path / "cofounder"),
    )
    kwargs.setdefault("services", services)
    kwargs.setdefault("now", MORNING)
    kwargs.setdefault("notify", lambda *a, **k: True)
    kwargs.setdefault("fetch_run_row", lambda run_id: None)
    return report_mod.run_report_pass(**kwargs)


def _recorder():
    calls = []

    def notify(project, text, level, *, settings=None, with_buttons=True):
        calls.append({"slug": project.slug, "text": text, "level": level,
                      "with_buttons": with_buttons})
        return True

    return calls, notify


# =============================================================================
# Gates
# =============================================================================


def test_kill_switch_refuses_and_counts(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER_DELEGATION", "disabled")
    monkeypatch.setattr(
        report_mod, "_build_services", lambda: pytest.fail("services built")
    )
    result = report_mod.run_report_pass(
        report_settings=config.get_cofounder_report_settings(enabled=True)
    )
    assert result.outcome == report_mod.OUTCOME_REFUSED
    assert kill_switches.get_refusal_counters()["cofounder_delegation"] == 1


def test_disabled_by_default(tmp_path):
    result = report_mod.run_report_pass()
    assert result.outcome == report_mod.OUTCOME_DISABLED


def test_nothing_to_do_is_idle_no_card(tmp_path, services):
    calls, notify = _recorder()
    result = _pass(tmp_path, services, notify=notify)
    assert result.outcome == report_mod.OUTCOME_IDLE
    assert calls == []


# =============================================================================
# Ingestion
# =============================================================================


def test_done_result_flips_line_and_acks(tmp_path, services, isolated_audit):
    agenda_path = _agenda(tmp_path, [_item()])
    _send_result(
        services, status="done", deliverable_path="vault/d.md", summary="checklist done"
    )
    result = _pass(tmp_path, services)
    assert result.outcome == report_mod.OUTCOME_COMPLETED
    assert result.ingested[0]["status"] == "done"

    data = json.loads(agenda_path.read_text(encoding="utf-8"))
    item = data["items"][0]
    assert item["status"] == "done"
    assert item["result_summary"] == "checklist done"
    assert item["deliverable_path"] == "vault/d.md"

    _, mailbox_service = services
    assert mailbox_service.get_inbox("cofounder", msg_type=report_mod.MSG_TYPE_RESULT) == []
    rows = [json.loads(l) for l in isolated_audit.read_text().splitlines()]
    assert rows[-1]["outcome"] == "report-done"


def test_failed_result_fails_the_subtask(tmp_path, services):
    convoy_id, subtask_id = _subtask(services)
    _agenda(tmp_path, [_item()])
    _send_result(services, status="failed", subtask_id=subtask_id, convoy_id=convoy_id)
    _pass(tmp_path, services)
    convoy_service, _ = services
    subtask = convoy_service.get_subtask(convoy_id, subtask_id)
    assert subtask.status == "failed"


def test_dispatched_result_stamps_run_metadata(tmp_path, services):
    convoy_id, subtask_id = _subtask(services)
    agenda_path = _agenda(tmp_path, [_item()])
    _send_result(
        services,
        status="dispatched",
        subtask_id=subtask_id,
        convoy_id=convoy_id,
        run_id="run-9",
        branch="cofounder/assign-x",
    )
    _pass(tmp_path, services)
    item = json.loads(agenda_path.read_text(encoding="utf-8"))["items"][0]
    assert item["status"] == "dispatched"
    assert item["run_id"] == "run-9"
    assert item["subtask_id"] == subtask_id


def test_garbage_result_body_is_contained_and_acked(tmp_path, services):
    _agenda(tmp_path, [_item()])
    _, mailbox_service = services
    from orchestration.models import SendMessageInput

    mailbox_service.send_message(
        SendMessageInput(
            from_agent="sales",
            recipients=["cofounder"],
            body="{not json",
            msg_type=report_mod.MSG_TYPE_RESULT,
        )
    )
    result = _pass(tmp_path, services)
    assert result.outcome == report_mod.OUTCOME_COMPLETED
    assert mailbox_service.get_inbox("cofounder", msg_type=report_mod.MSG_TYPE_RESULT) == []


def test_dry_run_never_claims_and_never_cards(tmp_path, services):
    _agenda(tmp_path, [_item()])
    _send_result(services)
    calls, notify = _recorder()
    result = _pass(tmp_path, services, dry_run=True, notify=notify)
    assert result.ingested[0]["status"] == "dry-run"
    assert calls == []
    _, mailbox_service = services
    statuses = [
        d.status
        for m in mailbox_service.get_inbox("cofounder", msg_type=report_mod.MSG_TYPE_RESULT)
        for d in m.deliveries
    ]
    assert statuses == ["pending"]


# =============================================================================
# Polling
# =============================================================================


def test_completed_run_flips_dispatched_to_done(tmp_path, services):
    convoy_id, subtask_id = _subtask(services)
    agenda_path = _agenda(
        tmp_path,
        [_item(status="dispatched", run_id="run-9", subtask_id=subtask_id)],
    )
    result = _pass(
        tmp_path, services, fetch_run_row=lambda run_id: {"status": "completed"}
    )
    assert result.polled[0]["status"] == "done"
    item = json.loads(agenda_path.read_text(encoding="utf-8"))["items"][0]
    assert item["status"] == "done"
    convoy_service, _ = services
    assert convoy_service.get_subtask(convoy_id, subtask_id).status == "completed"


def test_failed_run_flips_dispatched_to_failed(tmp_path, services):
    convoy_id, subtask_id = _subtask(services)
    _agenda(
        tmp_path,
        [_item(status="dispatched", run_id="run-9", subtask_id=subtask_id)],
    )
    result = _pass(
        tmp_path, services, fetch_run_row=lambda run_id: {"status": "failed"}
    )
    assert result.polled[0]["status"] == "failed"
    convoy_service, _ = services
    assert convoy_service.get_subtask(convoy_id, subtask_id).status == "failed"


def test_unknown_run_stays_dispatched(tmp_path, services):
    agenda_path = _agenda(tmp_path, [_item(status="dispatched", run_id="run-9")])
    result = _pass(tmp_path, services, fetch_run_row=lambda run_id: None)
    assert result.polled == []
    item = json.loads(agenda_path.read_text(encoding="utf-8"))["items"][0]
    assert item["status"] == "dispatched"


# =============================================================================
# Cards
# =============================================================================


def test_pulse_card_sent_once_with_changes(tmp_path, services):
    _agenda(tmp_path, [_item()])
    _send_result(services, status="done", summary="the checklist is drafted")
    calls, notify = _recorder()
    _pass(tmp_path, services, notify=notify)
    assert len(calls) == 1
    card = calls[0]
    assert card["level"] == report_mod.REPORT_LEVEL
    assert card["with_buttons"] is False
    assert "Portfolio pulse" in card["text"]
    assert "the checklist is drafted" in card["text"]


def test_pulse_muted_by_report_notify_false(tmp_path, services):
    _agenda(tmp_path, [_item()])
    _send_result(services)
    calls, notify = _recorder()
    _pass(
        tmp_path,
        services,
        notify=notify,
        report_settings=config.get_cofounder_report_settings(
            enabled=True, notify=False
        ),
    )
    assert calls == []


def test_global_mute_wins(tmp_path, services):
    _agenda(tmp_path, [_item()])
    _send_result(services)
    calls, notify = _recorder()
    _pass(
        tmp_path,
        services,
        notify=notify,
        settings=config.get_cofounder_settings(
            projects_dir=tmp_path / "cofounder", notify_levels=""
        ),
    )
    assert calls == []


def test_checkout_retries_when_send_not_confirmed(tmp_path, services, isolated_state):
    """A failed/unconfirmed card must NOT stamp the day (review finding 1):
    the next tick retries; a later confirmed send stamps once."""
    _agenda(tmp_path, [_item(status="done")])
    outcomes = iter([False, True])  # first send fails, second confirms
    calls = []

    def flaky_notify(project, text, level, *, settings=None, with_buttons=True):
        calls.append(text)
        return next(outcomes)

    first = _pass(tmp_path, services, notify=flaky_notify, now=EVENING)
    assert first.checkout_sent is False
    second = _pass(tmp_path, services, notify=flaky_notify, now=EVENING)
    assert second.checkout_sent is True
    third = _pass(tmp_path, services, notify=flaky_notify, now=EVENING)
    assert third.checkout_sent is False  # stamped now — once daily holds
    assert sum("End-of-day checkout" in c for c in calls) == 2  # fail + success


def test_poll_stamp_failure_claims_nothing(tmp_path, services, monkeypatch, isolated_audit):
    """When the agenda flip does not land, the poll must not advance the
    convoy, audit, or pulse — the next tick retries (review finding 2)."""
    convoy_id, subtask_id = _subtask(services)
    _agenda(
        tmp_path,
        [_item(status="dispatched", run_id="run-9", subtask_id=subtask_id)],
    )
    monkeypatch.setattr(report_mod, "_stamp_agenda_item", lambda *a, **k: False)
    calls, notify = _recorder()
    result = _pass(
        tmp_path,
        services,
        notify=notify,
        fetch_run_row=lambda run_id: {"status": "completed"},
    )
    assert result.polled == []
    assert calls == []  # no pulse card
    convoy_service, _ = services
    assert convoy_service.get_subtask(convoy_id, subtask_id).status != "completed"
    assert not isolated_audit.exists()  # no audit row claimed


def test_checkout_hour_gated_once_daily(tmp_path, services, isolated_state):
    _agenda(tmp_path, [_item(status="done"), _item(n=2, status="proposed", task="t2")])
    calls, notify = _recorder()

    # Before the hour: no checkout.
    result = _pass(tmp_path, services, notify=notify, now=MORNING)
    assert result.checkout_sent is False

    # Evening: one checkout with the day summary.
    result = _pass(tmp_path, services, notify=notify, now=EVENING)
    assert result.checkout_sent is True
    card = calls[-1]
    assert "End-of-day checkout" in card["text"]
    assert "1 done" in card["text"] and "1 proposed" in card["text"]
    assert "Delegations spent: 0/5" in card["text"]

    # Same evening again: state marker blocks a second checkout.
    result = _pass(tmp_path, services, notify=notify, now=EVENING)
    assert result.checkout_sent is False
    assert sum("End-of-day checkout" in c["text"] for c in calls) == 1


# =============================================================================
# Config
# =============================================================================


def test_report_settings_resolve_env_at_call_time(monkeypatch):
    defaults = config.get_cofounder_report_settings()
    assert defaults == config.CofounderReportSettings(
        enabled=False, notify=True, checkout_hour=18, poll_days=7
    )
    monkeypatch.setenv("COFOUNDER_REPORT_ENABLED", "true")
    monkeypatch.setenv("COFOUNDER_REPORT_NOTIFY", "false")
    monkeypatch.setenv("COFOUNDER_CHECKOUT_HOUR", "20")
    monkeypatch.setenv("COFOUNDER_REPORT_POLL_DAYS", "3")
    live = config.get_cofounder_report_settings()
    assert live == config.CofounderReportSettings(
        enabled=True, notify=False, checkout_hour=20, poll_days=3
    )
