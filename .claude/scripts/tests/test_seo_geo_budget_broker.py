"""Focused fail-closed tests for the SEO/GEO paid-research budget broker."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import seo_geo_budget_broker as broker  # noqa: E402


@pytest.fixture(autouse=True)
def _fixed_month(monkeypatch):
    monkeypatch.setattr(broker, "_month_key", lambda now=None: "2026-08")


def _reserve(root: Path, *, key: str = "cohort-hash:geo-mentions", estimate: float = 0.01):
    return broker.reserve(
        provider="dataforseo",
        operation="geo-mentions",
        cohort="five-brand-geo-v1",
        idempotency_key=key,
        estimated_usd=estimate,
        metadata={"units": 5},
        root=root,
    )


def test_reservation_initializes_policy_and_charges_before_provider_work(tmp_path):
    result = _reserve(tmp_path)

    policy = json.loads((tmp_path / "policy.json").read_text(encoding="utf-8"))
    ledger = json.loads((tmp_path / "2026-08-ledger.json").read_text(encoding="utf-8"))
    receipt = Path(result["receipt_path"])
    assert policy["approval"]["monthly_hard_cap_usd"] == 25.0
    assert policy["allocatable_cap_usd"] == 22.5
    assert ledger["entries"][0]["status"] == "reserved"
    assert ledger["entries"][0]["charged_usd"] == 0.01
    assert receipt.is_file()
    assert result["budget"]["charged_usd"] == 0.01
    assert result["budget"]["remaining_allocatable_usd"] == 22.49


def test_duplicate_key_is_blocked_before_a_second_call(tmp_path):
    _reserve(tmp_path)

    with pytest.raises(broker.DuplicateRunError, match="idempotency key"):
        _reserve(tmp_path)


def test_unknown_call_stays_charged_and_cannot_be_released(tmp_path):
    reservation = _reserve(tmp_path)
    result = broker.mark_unknown(
        run_id=reservation["run_id"],
        reason="subprocess_timeout",
        artifact_paths=["C:/temporary/provider-output.json"],
        details={"timeout_seconds": 90},
        root=tmp_path,
    )

    status = broker.budget_status(root=tmp_path)
    assert result["status"] == "unknown"
    assert status["charged_usd"] == 0.01
    assert status["entry_status_counts"] == {"reserved": 0, "settled": 0, "unknown": 1}
    with pytest.raises(broker.BudgetBrokerError, match="already unknown"):
        broker.settle(
            run_id=reservation["run_id"],
            provider_status="ok",
            artifact_paths=[],
            root=tmp_path,
        )


def test_settlement_retains_conservative_charge_and_writes_receipt(tmp_path):
    reservation = _reserve(tmp_path)
    result = broker.settle(
        run_id=reservation["run_id"],
        provider_status="ok",
        artifact_paths=["C:/temporary/research.json"],
        details={"tasks": 5},
        root=tmp_path,
    )

    assert result["status"] == "settled"
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["entry"]["status"] == "settled"
    assert receipt["entry"]["charged_usd"] == 0.01


def test_policy_denies_unapproved_provider_operation_and_underestimate(tmp_path):
    with pytest.raises(broker.PolicyViolationError, match="provider is disabled"):
        broker.reserve(
            provider="firecrawl",
            operation="map",
            cohort="owned-surfaces",
            idempotency_key="blocked-firecrawl",
            estimated_usd=0.01,
            metadata={"units": 1},
            root=tmp_path,
        )
    with pytest.raises(broker.PolicyViolationError, match="operation is not approved"):
        broker.reserve(
            provider="dataforseo",
            operation="fleet-report",
            cohort="wrong-roster",
            idempotency_key="blocked-fleet-report",
            estimated_usd=0.01,
            metadata={"units": 1},
            root=tmp_path,
        )
    with pytest.raises(broker.PolicyViolationError, match="below the policy floor"):
        broker.reserve(
            provider="dataforseo",
            operation="geo-mentions",
            cohort="five-brand-geo-v1",
            idempotency_key="underestimate",
            estimated_usd=0.002,
            metadata={"units": 2},
            root=tmp_path,
        )


def test_cap_and_existing_lock_fail_closed(tmp_path):
    with pytest.raises(broker.BudgetExceededError, match="per-run cap"):
        broker.reserve(
            provider="dataforseo",
            operation="geo-mentions",
            cohort="too-many",
            idempotency_key="too-many",
            estimated_usd=0.5001,
            metadata={"units": 25},
            root=tmp_path,
        )

    lock = tmp_path / "locks" / "2026-08.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("operator inspection required\n", encoding="utf-8")
    with pytest.raises(broker.BudgetLockError, match="lock exists"):
        _reserve(tmp_path)
