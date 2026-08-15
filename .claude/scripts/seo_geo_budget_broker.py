"""Fail-closed monthly spend broker for SEO/GEO provider research.

This module is intentionally the only supported paid DataForSEO path for the
SEO/GEO fleet loop.  It reserves a conservative estimate before a provider
subprocess starts, keeps ambiguous calls charged, and records durable local
receipts.  It never calls a provider itself and it never changes a website.

The ledger is local operational state, not a billing system.  DataForSEO's
account billing remains the source of truth; the broker exists to stop the
scheduled loop from accidentally overspending its approved ceiling.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo" / "data" / "fleet-budget"
_MONEY_QUANTUM = Decimal("0.0001")


class BudgetBrokerError(RuntimeError):
    """Base error for a fail-closed budget decision."""


class BudgetLockError(BudgetBrokerError):
    """A concurrent process owns the monthly ledger lock."""


class BudgetExceededError(BudgetBrokerError):
    """A reservation would exceed a hard broker cap."""


class PolicyViolationError(BudgetBrokerError):
    """A provider or operation is outside the approved policy."""


class DuplicateRunError(BudgetBrokerError):
    """An idempotency key already exists for the ledger month."""


DEFAULT_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "approval": {
        "approved_at": "2026-08-12",
        "scope": "YourBusiness fleet GEO research only",
        "monthly_hard_cap_usd": 25.0,
        "billing_note": (
            "Provider billing is authoritative; broker estimates reserve conservatively."
        ),
    },
    "allocatable_cap_usd": 22.5,
    "uncertainty_reserve_usd": 2.5,
    "providers": {
        "dataforseo": {
            "enabled": True,
            "monthly_cap_usd": 22.5,
            "per_run_cap_usd": 0.5,
            "operations": {
                "geo-mentions": {"max_units": 25, "estimated_unit_usd": 0.002},
                "serp": {"max_units": 25, "estimated_unit_usd": 0.002},
                "keywords": {"max_units": 25, "estimated_unit_usd": 0.05},
                "backlinks-summary": {"max_units": 5, "estimated_unit_usd": 0.02},
                "site-audit-lite": {"max_units": 5, "estimated_unit_usd": 0.0125},
            },
        },
        "firecrawl": {
            "enabled": False,
            "monthly_cap_usd": 0.0,
            "per_run_cap_usd": 0.0,
            "reason": "No verified dollar-cap or isolated account boundary is configured.",
        },
        "openseo_paid": {
            "enabled": False,
            "monthly_cap_usd": 0.0,
            "per_run_cap_usd": 0.0,
            "reason": "Paid OpenSEO runs are not brokered in this control lane.",
        },
    },
}


def _now() -> datetime:
    return datetime.now(UTC)


def _month_key(now: datetime | None = None) -> str:
    instant = now or _now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(PACIFIC).strftime("%Y-%m")


def _as_money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise PolicyViolationError("estimated_usd must be a finite positive amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise PolicyViolationError("estimated_usd must be a finite positive amount")
    return amount


def _money_float(value: Decimal | float | int | str) -> float:
    return float(Decimal(str(value)).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def _paths(root: Path, month: str) -> dict[str, Path]:
    return {
        "policy": root / "policy.json",
        "ledger": root / f"{month}-ledger.json",
        "lock": root / "locks" / f"{month}.lock",
        "receipts": root / "receipts",
    }


def _ensure_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "locks").mkdir(parents=True, exist_ok=True)
    (root / "receipts").mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        raise BudgetBrokerError(f"could not read {path.name}") from exc
    if not isinstance(payload, dict):
        raise BudgetBrokerError(f"{path.name} must contain a JSON object")
    return payload


def _policy(root: Path) -> dict[str, Any]:
    _ensure_root(root)
    path = root / "policy.json"
    if not path.exists():
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(DEFAULT_POLICY, handle, indent=2, ensure_ascii=False, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                path.unlink(missing_ok=True)
                raise
    policy = _read_json(path)
    if policy.get("schema_version") != 1:
        raise BudgetBrokerError("unsupported or missing broker policy schema")
    return policy


def _empty_ledger(month: str) -> dict[str, Any]:
    return {"schema_version": 1, "month": month, "entries": []}


def _ledger(root: Path, month: str) -> dict[str, Any]:
    data = _read_json(_paths(root, month)["ledger"])
    if not data:
        return _empty_ledger(month)
    if (
        data.get("schema_version") != 1
        or data.get("month") != month
        or not isinstance(data.get("entries"), list)
    ):
        raise BudgetBrokerError("invalid monthly budget ledger")
    return data


class _MonthlyLock:
    def __init__(self, root: Path, month: str) -> None:
        self.path = _paths(root, month)["lock"]
        self.handle: int | None = None

    def __enter__(self) -> _MonthlyLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise BudgetLockError(
                "monthly budget lock exists for "
                f"{self.path.stem}; stop and inspect it rather than bypassing the ledger"
            ) from exc
        try:
            os.write(
                self.handle, f"pid={os.getpid()} created_at={_now().isoformat()}\n".encode()
            )
        except OSError:
            pass
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is not None:
            try:
                os.close(self.handle)
            finally:
                self.handle = None
        self.path.unlink(missing_ok=True)


def _charged(entries: list[dict[str, Any]], *, provider: str | None = None) -> Decimal:
    total = Decimal("0")
    for entry in entries:
        if provider and entry.get("provider") != provider:
            continue
        if entry.get("status") not in {"reserved", "settled", "unknown"}:
            continue
        try:
            total += Decimal(str(entry.get("charged_usd", entry.get("estimated_usd", 0))))
        except InvalidOperation:
            raise BudgetBrokerError("ledger contains an invalid charged amount")
    return total.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _budget_snapshot(policy: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    entries = ledger.get("entries", [])
    if not isinstance(entries, list):
        raise BudgetBrokerError("ledger entries are invalid")
    overall_cap = _as_money(policy.get("allocatable_cap_usd"))
    overall_charged = _charged(entries)
    providers: dict[str, Any] = {}
    for name, config in policy.get("providers", {}).items():
        if not isinstance(config, dict):
            continue
        cap = Decimal(str(config.get("monthly_cap_usd", 0))).quantize(_MONEY_QUANTUM)
        used = _charged(entries, provider=name)
        providers[name] = {
            "enabled": bool(config.get("enabled")),
            "monthly_cap_usd": _money_float(cap),
            "charged_usd": _money_float(used),
            "remaining_usd": _money_float(max(Decimal("0"), cap - used)),
            "per_run_cap_usd": _money_float(Decimal(str(config.get("per_run_cap_usd", 0)))),
        }
    statuses = {"reserved": 0, "settled": 0, "unknown": 0}
    for entry in entries:
        status = str(entry.get("status", ""))
        if status in statuses:
            statuses[status] += 1
    return {
        "month": ledger["month"],
        "monthly_hard_cap_usd": _money_float(
            _as_money(policy.get("approval", {}).get("monthly_hard_cap_usd"))
        ),
        "allocatable_cap_usd": _money_float(overall_cap),
        "uncertainty_reserve_usd": _money_float(
            Decimal(str(policy.get("uncertainty_reserve_usd", 0)))
        ),
        "charged_usd": _money_float(overall_charged),
        "remaining_allocatable_usd": _money_float(max(Decimal("0"), overall_cap - overall_charged)),
        "entry_status_counts": statuses,
        "providers": providers,
    }


def _write_entry_receipt(
    root: Path, month: str, entry: dict[str, Any], budget: dict[str, Any]
) -> Path:
    path = _paths(root, month)["receipts"] / f"{entry['run_id']}.json"
    _atomic_write_json(
        path,
        {
            "schema_version": 1,
            "generated_at": _now().isoformat(),
            "month": month,
            "entry": entry,
            "budget": budget,
            "provider_billing_note": (
                "Provider billing is authoritative; this is a conservative local control receipt."
            ),
        },
    )
    return path


def _find_entry(ledger: dict[str, Any], run_id: str) -> dict[str, Any]:
    for entry in ledger["entries"]:
        if isinstance(entry, dict) and entry.get("run_id") == run_id:
            return entry
    raise BudgetBrokerError(f"unknown budget run_id: {run_id}")


def _validate_reservation(
    policy: dict[str, Any],
    *,
    provider: str,
    operation: str,
    estimated: Decimal,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    providers = policy.get("providers")
    if not isinstance(providers, dict) or not isinstance(providers.get(provider), dict):
        raise PolicyViolationError(f"provider is not approved by budget policy: {provider}")
    provider_config = providers[provider]
    if not provider_config.get("enabled"):
        raise PolicyViolationError(f"provider is disabled by budget policy: {provider}")
    operations = provider_config.get("operations")
    if not isinstance(operations, dict) or not isinstance(operations.get(operation), dict):
        raise PolicyViolationError(
            f"operation is not approved by budget policy: {provider}/{operation}"
        )
    operation_config = operations[operation]
    try:
        units = int(metadata.get("units", 1))
    except (TypeError, ValueError) as exc:
        raise PolicyViolationError("metadata.units must be a positive integer") from exc
    maximum_units = int(operation_config.get("max_units", 0))
    if units < 1 or units > maximum_units:
        raise PolicyViolationError(
            f"{provider}/{operation} permits 1..{maximum_units} units per run"
        )
    expected_floor = Decimal(str(operation_config.get("estimated_unit_usd", 0))) * units
    if estimated < expected_floor:
        raise PolicyViolationError(
            "estimated "
            f"${estimated} is below the policy floor ${expected_floor} for "
            f"{units} {provider}/{operation} units"
        )
    per_run = _as_money(provider_config.get("per_run_cap_usd"))
    if estimated > per_run:
        raise BudgetExceededError(
            f"estimated ${estimated} exceeds {provider} per-run cap ${per_run}"
        )
    return provider_config, operation_config


def reserve(
    *,
    provider: str,
    operation: str,
    cohort: str,
    idempotency_key: str,
    estimated_usd: Any,
    metadata: dict[str, Any] | None = None,
    root: Path = DEFAULT_ROOT,
) -> dict[str, Any]:
    """Reserve a paid operation before a subprocess/provider call starts.

    A duplicate key, unknown provider, cap breach, or existing lock always
    fails before the caller can spend.  The caller must subsequently settle or
    mark the reservation unknown; unknown calls remain charged.
    """
    if not cohort.strip() or not idempotency_key.strip():
        raise PolicyViolationError("cohort and idempotency_key are required")
    safe_metadata = dict(metadata or {})
    estimated = _as_money(estimated_usd)
    month = _month_key()
    policy = _policy(root)
    provider_config, _ = _validate_reservation(
        policy,
        provider=provider,
        operation=operation,
        estimated=estimated,
        metadata=safe_metadata,
    )
    paths = _paths(root, month)
    with _MonthlyLock(root, month):
        ledger = _ledger(root, month)
        for entry in ledger["entries"]:
            if isinstance(entry, dict) and entry.get("idempotency_key") == idempotency_key:
                raise DuplicateRunError(
                    f"idempotency key already recorded for {month}: {idempotency_key}"
                )
        overall_cap = _as_money(policy.get("allocatable_cap_usd"))
        provider_cap = _as_money(provider_config.get("monthly_cap_usd"))
        overall_after = _charged(ledger["entries"]) + estimated
        provider_after = _charged(ledger["entries"], provider=provider) + estimated
        if overall_after > overall_cap:
            raise BudgetExceededError(
                f"reservation would exceed ${overall_cap} allocatable monthly cap"
            )
        if provider_after > provider_cap:
            raise BudgetExceededError(
                f"reservation would exceed {provider} monthly cap ${provider_cap}"
            )
        entry = {
            "run_id": uuid.uuid4().hex,
            "provider": provider,
            "operation": operation,
            "cohort": cohort,
            "idempotency_key": idempotency_key,
            "estimated_usd": _money_float(estimated),
            "charged_usd": _money_float(estimated),
            "status": "reserved",
            "created_at": _now().isoformat(),
            "updated_at": _now().isoformat(),
            "metadata": safe_metadata,
            "artifact_paths": [],
            "provider_status": "not_started",
        }
        ledger["entries"].append(entry)
        _atomic_write_json(paths["ledger"], ledger)
        budget = _budget_snapshot(policy, ledger)
        receipt_path = _write_entry_receipt(root, month, entry, budget)
    return {
        "run_id": entry["run_id"],
        "status": entry["status"],
        "budget": budget,
        "receipt_path": str(receipt_path),
    }


def _finalize(
    *,
    run_id: str,
    status: str,
    provider_status: str,
    artifact_paths: list[str] | None,
    details: dict[str, Any] | None,
    reason: str | None = None,
    actual_usd: Any | None = None,
    root: Path = DEFAULT_ROOT,
) -> dict[str, Any]:
    if status not in {"settled", "unknown"}:
        raise BudgetBrokerError(f"unsupported final status: {status}")
    month = _month_key()
    policy = _policy(root)
    paths = _paths(root, month)
    with _MonthlyLock(root, month):
        ledger = _ledger(root, month)
        entry = _find_entry(ledger, run_id)
        if entry.get("status") != "reserved":
            raise BudgetBrokerError(f"run {run_id} is already {entry.get('status')}")
        entry["status"] = status
        entry["updated_at"] = _now().isoformat()
        entry["provider_status"] = provider_status
        entry["artifact_paths"] = [str(path) for path in (artifact_paths or [])]
        entry["details"] = dict(details or {})
        if reason:
            entry["reason"] = reason
        # Use a reported actual only when it is non-negative and never release
        # the original conservative reservation automatically.
        if actual_usd is not None:
            actual = _as_money(actual_usd)
            entry["reported_actual_usd"] = _money_float(actual)
            entry["charged_usd"] = _money_float(max(actual, Decimal(str(entry["estimated_usd"]))))
        _atomic_write_json(paths["ledger"], ledger)
        budget = _budget_snapshot(policy, ledger)
        receipt_path = _write_entry_receipt(root, month, entry, budget)
    return {"run_id": run_id, "status": status, "budget": budget, "receipt_path": str(receipt_path)}


def settle(
    *,
    run_id: str,
    artifact_paths: list[str],
    provider_status: str,
    details: dict[str, Any] | None = None,
    actual_usd: Any | None = None,
    root: Path = DEFAULT_ROOT,
) -> dict[str, Any]:
    """Settle a completed provider call without automatically refunding it."""
    return _finalize(
        run_id=run_id,
        status="settled",
        provider_status=provider_status,
        artifact_paths=artifact_paths,
        details=details,
        actual_usd=actual_usd,
        root=root,
    )


def mark_unknown(
    *,
    run_id: str,
    reason: str,
    artifact_paths: list[str] | None = None,
    details: dict[str, Any] | None = None,
    root: Path = DEFAULT_ROOT,
) -> dict[str, Any]:
    """Fail closed after a timeout/crash/malformed result; reservation stays charged."""
    return _finalize(
        run_id=run_id,
        status="unknown",
        provider_status="unknown",
        artifact_paths=artifact_paths,
        details=details,
        reason=reason,
        root=root,
    )


def budget_status(*, root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Return local policy/ledger status without calling any provider."""
    month = _month_key()
    policy = _policy(root)
    ledger = _ledger(root, month)
    result = _budget_snapshot(policy, ledger)
    result["policy_path"] = str(root / "policy.json")
    result["ledger_path"] = str(_paths(root, month)["ledger"])
    result["lock_present"] = _paths(root, month)["lock"].exists()
    return result


def main() -> int:
    """Print the current no-spend local budget status for operators."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Inspect the local SEO/GEO research budget ledger."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    policy_existed = (args.root / "policy.json").is_file()
    print(json.dumps(budget_status(root=args.root), indent=2, ensure_ascii=False))
    print("PROVIDER_CALLS=0")
    print(f"LOCAL_CONTROL_STATE={'read' if policy_existed else 'initialized'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
