"""Compatibility admissions for old scheduled commands; no provider or writers.

Only the shared dispatcher/worker may execute admitted work. These adapters
keep scheduled entrypoints useful without reviving independent cognition loops.
"""

from __future__ import annotations

from datetime import UTC, datetime


def request_active_synthesis(kind, *, source_key, test_mode=False, force=False):
    from personas import activity

    from . import service, synthesis

    target = service.get_learning_service(activity.get_active_profile_name())
    return synthesis.request_synthesis(
        target, kind, source_key=source_key, force=force, test_mode=test_mode
    )


def admit_profile_synthesis(kind, *, profiles, test_mode=False, once=False) -> dict:
    """Admit named profiles through the same pause, signal and interval policy."""
    from . import service, synthesis

    attempted, failed, receipts = [], [], []
    slot = datetime.now(UTC).strftime("%Y-%m-%d")
    for profile in profiles:
        if profile.is_default:
            continue
        admitted = False
        try:
            target = service.get_learning_service(profile.name)
            receipt = synthesis.request_synthesis(
                target,
                kind,
                source_key=f"legacy-{kind}-tick:{slot}",
                test_mode=test_mode,
            )
            receipts.append({"persona_id": profile.name, **receipt})
            print(f"[{kind}] {profile.name}: {receipt.get('status', 'unknown')}")
            if receipt.get("status") in {"queued", "coalesced"}:
                attempted.append(profile.name)
                admitted = True
            elif receipt.get("status") == "test_mode":
                admitted = True
        except Exception as exc:
            failed.append(profile.name)
            print(f"[{kind}] {profile.name}: admission failed ({type(exc).__name__})")
        if once and admitted:
            break
    return {"attempted": attempted, "failed": failed, "receipts": receipts}
