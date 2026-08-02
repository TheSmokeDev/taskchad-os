from __future__ import annotations

import json
import sqlite3

import pytest

from buzz_signals import RECEIPT_TYPES, enqueue_work_receipt, render_work_receipt
from buzz_state import BuzzStateStore


def test_cursor_persists_same_second_overlap_without_duplicates(tmp_path) -> None:
    path = tmp_path / "buzz.db"
    first = BuzzStateStore(path, max_seen_ids=128)

    assert first.record_event_if_new("scope", "room", "a" * 64, 100) is True
    assert first.record_event_if_new("scope", "room", "a" * 64, 100) is False

    restarted = BuzzStateStore(path, max_seen_ids=128)
    assert restarted.record_event_if_new("scope", "room", "b" * 64, 100) is True
    assert restarted.record_event_if_new("scope", "room", "c" * 64, 99) is False
    assert restarted.cursor("scope", "room") == (100, ("a" * 64, "b" * 64))


def test_first_run_seed_suppresses_history(tmp_path) -> None:
    store = BuzzStateStore(tmp_path / "buzz.db")
    assert store.seed_cursor("scope", "room", 50, ["a" * 64]) is True
    assert store.seed_cursor("scope", "room", 80, ["z" * 64]) is False
    assert store.record_event_if_new("scope", "room", "b" * 64, 49) is False
    assert store.record_event_if_new("scope", "room", "c" * 64, 51) is True


def test_state_connections_release_windows_file_lock(tmp_path) -> None:
    path = tmp_path / "buzz.db"
    store = BuzzStateStore(path)

    store.seed_cursor("scope", "room", 100, ["a" * 64])
    assert store.cursor("scope", "room") == (100, ("a" * 64,))

    path.unlink()
    assert not path.exists()


@pytest.mark.parametrize("receipt_type", sorted(RECEIPT_TYPES))
def test_receipts_are_redacted_bounded_and_idempotent(tmp_path, receipt_type) -> None:
    path = tmp_path / "buzz.db"
    kwargs = {
        "work_id": "42",
        "work_type": "convoy",
        "summary": "Deploy API_KEY=top-secret nsec1shouldneverleave " + "x" * 400,
        "status": receipt_type.removeprefix("work."),
        "dashboard_path": "/mission/convoys/42",
        "idempotency_key": f"42:{receipt_type}",
        "state_path": path,
        "profile": "pilot",
        "timestamp": "2026-07-31T00:00:00+00:00",
    }
    assert enqueue_work_receipt(receipt_type, **kwargs) is True
    assert enqueue_work_receipt(receipt_type, **kwargs) is False

    claimed = BuzzStateStore(path).claim_receipts()
    assert len(claimed) == 1
    payload = claimed[0]["payload"]
    serialized = json.dumps(payload)
    assert "top-secret" not in serialized
    assert "nsec1" not in serialized
    assert len(payload["summary"]) <= 240
    assert set(payload) == {
        "receipt_type",
        "work_id",
        "work_type",
        "profile",
        "summary",
        "status",
        "timestamp",
        "dashboard_path",
    }
    assert "Open in Homie Dashboard: /mission/convoys/42" in render_work_receipt(payload)


def test_receipt_retry_and_completion_ledger(tmp_path) -> None:
    path = tmp_path / "buzz.db"
    store = BuzzStateStore(path)
    assert store.enqueue_receipt("one", {"receipt_type": "work.started"}) is True
    row = store.claim_receipts()[0]
    store.release_receipt(row["id"], "offline", 1)

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE buzz_receipt_outbox SET next_attempt_at = 0")
    retried = store.claim_receipts()[0]
    store.mark_receipt_sent(retried["id"], "e" * 64)

    with sqlite3.connect(path) as conn:
        status, event_id = conn.execute(
            "SELECT status, platform_event_id FROM buzz_receipt_outbox"
        ).fetchone()
    assert (status, event_id) == ("sent", "e" * 64)


def test_receipt_dashboard_path_refuses_external_or_query_data(tmp_path) -> None:
    with pytest.raises(ValueError):
        enqueue_work_receipt(
            "work.failed",
            work_id="1",
            work_type="task",
            summary="failed",
            status="failed",
            dashboard_path="https://attacker.invalid/?token=secret",
            idempotency_key="bad",
            state_path=tmp_path / "buzz.db",
        )
