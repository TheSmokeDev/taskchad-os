"""Validation-only helpers for cognitive-loop E2E probes.

These helpers are deliberately small and side-effect-light. They prove which
entrypoints currently consume the shared identity payload from a caller-provided
vault root, and they report missing/drift states instead of papering over them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_CHAT_DIR = Path(__file__).resolve().parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

IDENTITY_SENTINELS = {
    "SOUL": "COG_E2E_SOUL_SENTINEL",
    "SELF": "COG_E2E_SELF_SENTINEL",
    "USER": "COG_E2E_USER_SENTINEL",
    "MEMORY": "COG_E2E_MEMORY_SENTINEL",
    "GOALS": "COG_E2E_GOALS_SENTINEL",
    "WORKING": "COG_E2E_WORKING_SENTINEL",
}
ACTIVE_INFERENCE_SENTINEL = "COG_E2E_ACTIVE_INFERENCE_SENTINEL"
FUTURE_BEHAVIOR_SENTINEL = "COG_E2E_FUTURE_BEHAVIOR_SENTINEL"


def get_cognitive_loop_inference_state_file(vault_root: Path) -> Path:
    """Return the temp inference-state file used by validation probes."""

    return Path(vault_root) / ".validation" / "self-model-inferences.json"


def seed_cognitive_loop_temp_vault(vault_root: Path) -> Path:
    """Create a deterministic temp vault for validation tests."""

    vault = Path(vault_root)
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "daily").mkdir(exist_ok=True)
    (vault / "weekly").mkdir(exist_ok=True)
    (vault / "concepts").mkdir(exist_ok=True)
    (vault / "drafts" / "active").mkdir(parents=True, exist_ok=True)
    (vault / "drafts" / "sent").mkdir(parents=True, exist_ok=True)
    (vault / "drafts" / "expired").mkdir(parents=True, exist_ok=True)

    for name, sentinel in IDENTITY_SENTINELS.items():
        (vault / f"{name}.md").write_text(
            f"# {name}\n\n- {sentinel}\n",
            encoding="utf-8",
        )

    inference_state = get_cognitive_loop_inference_state_file(vault)
    inference_state.parent.mkdir(parents=True, exist_ok=True)
    inference_state.write_text(
        json.dumps(
            [
                {
                    "id": "validation-active-inference",
                    "inference": ACTIVE_INFERENCE_SENTINEL,
                    "observation": "Seeded by cognitive-loop E2E harness.",
                    "confidence": 0.95,
                    "evidence_count": 3,
                    "contradiction_count": 0,
                    "first_seen": "2026-05-21T00:00:00+00:00",
                    "last_updated": "2026-05-21T00:00:00+00:00",
                    "source": "validation_harness",
                    "status": "confirmed",
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    (vault / "HEARTBEAT.md").write_text(
        "# HEARTBEAT\n\n- Validation heartbeat checklist\n",
        encoding="utf-8",
    )
    (vault / "HABITS.md").write_text(
        "# HABITS\n\nToday: validation\n",
        encoding="utf-8",
    )
    (vault / "daily" / "2026-05-20.md").write_text(
        "# Daily Log\n\nKey decision: validate the cognitive loop with temp state.\n",
        encoding="utf-8",
    )
    return vault


def build_scheduled_entrypoint_report(
    entrypoint: str,
    vault_root: Path,
    *,
    test_mode: bool = True,
) -> dict[str, Any]:
    """Return a machine-readable scheduled-loop validation probe."""

    vault = Path(vault_root).resolve()
    errors: list[str] = []
    prompt_sections: dict[str, str] = {}
    identity_payload_present = False
    inference_state_file = get_cognitive_loop_inference_state_file(vault)

    try:
        from cognition.identity_payload import build_identity_payload

        payload = build_identity_payload(vault)
    except Exception as exc:  # pragma: no cover - defensive reporting path
        payload = {}
        errors.append(f"identity_payload_error: {exc}")

    try:
        if entrypoint == "memory_reflect":
            from memory_reflect import (
                _assemble_reflect_amendment_section,
                _assemble_reflect_cognition_section,
                _assemble_reflect_identity_section,
            )

            prompt_sections["identity"] = _assemble_reflect_identity_section(vault)
            prompt_sections["scheduled_cognition"] = _assemble_reflect_cognition_section(
                vault,
                inference_state_file,
            )
            prompt_sections["amendment_gate"] = _assemble_reflect_amendment_section(
                vault / ".validation" / "amendment-proposals.jsonl",
            )
            identity_payload_present = _contains_all(
                prompt_sections["identity"],
                ("SOUL", "SELF", "USER", "MEMORY", "GOALS"),
            )
        elif entrypoint == "memory_weekly":
            from memory_weekly import (
                _assemble_weekly_amendment_section,
                _assemble_weekly_cognition_section,
                _assemble_weekly_drift_section,
                _assemble_weekly_identity_section,
            )

            prompt_sections["identity"] = _assemble_weekly_identity_section(vault)
            prompt_sections["scheduled_cognition"] = _assemble_weekly_cognition_section(
                vault,
                inference_state_file,
            )
            prompt_sections["amendment_gate"] = _assemble_weekly_amendment_section(
                vault / ".validation" / "amendment-proposals.jsonl",
            )
            prompt_sections["drift_detection"] = _assemble_weekly_drift_section()
            identity_payload_present = _contains_all(
                prompt_sections["identity"],
                ("SOUL", "SELF", "USER", "MEMORY", "GOALS"),
            )
        elif entrypoint == "memory_dream":
            from memory_dream import (
                _assemble_consolidate_identity_section,
                _assemble_dream_amendment_section,
                _assemble_dream_cognition_section,
                _assemble_dream_drift_section,
                _assemble_prune_memory_section,
            )

            memory_lines = len(payload.get("MEMORY", "").splitlines())
            prompt_sections["consolidate_identity"] = (
                _assemble_consolidate_identity_section(vault, memory_lines)
            )
            prompt_sections["prune_memory"] = _assemble_prune_memory_section(vault)
            prompt_sections["scheduled_cognition"] = _assemble_dream_cognition_section(
                vault,
                inference_state_file,
            )
            prompt_sections["amendment_gate"] = _assemble_dream_amendment_section(
                vault / ".validation" / "amendment-proposals.jsonl",
            )
            prompt_sections["prune_amendment_gate"] = _assemble_dream_amendment_section(
                vault / ".validation" / "amendment-proposals.jsonl",
                source="memory_dream_prune",
            )
            prompt_sections["drift_detection"] = _assemble_dream_drift_section()
            identity_payload_present = _contains_all(
                prompt_sections["consolidate_identity"],
                ("SELF", "MEMORY", "GOALS"),
            )
        elif entrypoint == "heartbeat":
            from heartbeat import _assemble_heartbeat_cognition_section

            prompt_sections["scheduled_cognition"] = (
                _assemble_heartbeat_cognition_section(
                    vault,
                    inference_state_file,
                )
            )
            identity_payload_present = _contains_all(
                prompt_sections["scheduled_cognition"],
                ("SOUL", "SELF", "USER", "MEMORY", "GOALS"),
            )
        else:
            errors.append(f"unknown_entrypoint: {entrypoint}")
    except Exception as exc:  # pragma: no cover - defensive reporting path
        errors.append(f"entrypoint_probe_error: {exc}")

    prompt_text = "\n\n".join(prompt_sections.values())
    active_inferences_present = ACTIVE_INFERENCE_SENTINEL in prompt_text
    working_memory_present = (
        "WORKING" in prompt_text or IDENTITY_SENTINELS["WORKING"] in prompt_text
    )
    amendment_gate_present = (
        "Human-Gated Durable Memory Amendments" in prompt_text
        or "Policy-Gated Durable Memory Amendments" in prompt_text
    )
    proactive_brief_present = "Proactive Brief" in prompt_text
    auto_apply_enabled = (
        "policy engine may automatically apply" in prompt_text.lower()
        or "machine policy gate" in prompt_text.lower()
    )
    auto_apply_disabled = (
        "proposal-only" in prompt_text
        or "Never mark a proposal approved, rejected, or\napplied yourself." in prompt_text
    )
    drift_detection_present = "Cognitive Loop Drift Findings" in prompt_text
    heartbeat_drift = entrypoint == "heartbeat" and not identity_payload_present

    missing = []
    if not identity_payload_present:
        missing.append("canonical_identity_payload")
    if not active_inferences_present:
        missing.append("active_inferences")
    if not working_memory_present:
        missing.append("working_memory_context")
    if not proactive_brief_present:
        missing.append("unified_proactive_brief")
    if entrypoint in {"memory_reflect", "memory_weekly", "memory_dream"}:
        if not amendment_gate_present:
            missing.append("policy_gated_amendment_proposals")
        if not auto_apply_enabled:
            missing.append("durable_memory_auto_apply_enabled")
    if entrypoint in {"memory_weekly", "memory_dream"} and not drift_detection_present:
        missing.append("contradiction_drift_detector")
    if heartbeat_drift:
        missing.append("heartbeat_identity_unification")

    return {
        "success": not errors,
        "entrypoint": entrypoint,
        "vault_root": str(vault),
        "writes": [],
        "identity_payload_present": identity_payload_present,
        "active_inferences_present": active_inferences_present,
        "working_memory_present": working_memory_present,
        "proactive_brief_present": proactive_brief_present,
        "amendment_gate_present": amendment_gate_present,
        "auto_apply_enabled": auto_apply_enabled,
        "auto_apply_disabled": auto_apply_disabled,
        "drift_detection_present": drift_detection_present,
        "runtime_mode": "fake_deterministic_probe",
        "external_sends": [],
        "errors": errors,
        "state": "drift" if heartbeat_drift else ("partial" if missing else "live"),
        "missing": missing,
        "test_mode": test_mode,
        "prompt_capture": {
            "scope": (
                "scheduled_cognition_sections"
                if "scheduled_cognition" in prompt_sections
                else "scheduled_identity_sections"
            ),
            "sections": sorted(prompt_sections.keys()),
            "chars": len(prompt_text),
            "contains_seeded_identity": {
                name: sentinel in prompt_text
                for name, sentinel in IDENTITY_SENTINELS.items()
            },
            "contains_active_inference": ACTIVE_INFERENCE_SENTINEL in prompt_text,
            "contains_amendment_gate": amendment_gate_present,
            "contains_auto_apply_policy": auto_apply_enabled,
            "contains_drift_detection": drift_detection_present,
            "contains_proactive_brief": proactive_brief_present,
        },
    }


def build_future_behavior_autonomy_report(vault_root: Path) -> dict[str, Any]:
    """Prove amendment apply changes future prompt-visible behavior."""

    vault = seed_cognitive_loop_temp_vault(Path(vault_root))
    ledger_path = vault / ".validation" / "amendment-proposals.jsonl"
    action_queue_path = vault / ".validation" / "proactive-actions.jsonl"
    before = (vault / "SELF.md").read_text(encoding="utf-8")
    before_contains = FUTURE_BEHAVIOR_SENTINEL in before

    try:
        from cognition.amendments import (
            AmendmentProposal,
            ProposalLedger,
            apply_policy_approved_amendments,
        )
        from cognition.identity_payload import build_identity_payload
        from cognition.proactive_actions import ProactiveAction, ProactiveActionQueue

        ledger = ProposalLedger(ledger_path)
        proposal = AmendmentProposal(
            source="validation_harness",
            target_file="SELF.md",
            summary="Prove future behavior loading",
            rationale="Validation harness needs a deterministic before/after marker.",
            evidence_paths=["validation://seeded-turn"],
            proposed_content=FUTURE_BEHAVIOR_SENTINEL,
            confidence_score=0.95,
        )
        ledger.append(proposal)
        proposals = ledger.read_all()
        applied = apply_policy_approved_amendments(ledger, vault)
        payload = build_identity_payload(vault)
        after_self = payload.get("SELF", "")
        after_contains = FUTURE_BEHAVIOR_SENTINEL in after_self

        queue = ProactiveActionQueue(action_queue_path)
        action = ProactiveAction(
            source="validation_harness",
            reason="Auto-applied amendment changed future self-model context.",
            urgency=3,
            message="Future behavior proof is ready for operator review.",
            evidence_paths=["validation://future-behavior"],
        )
        queued = queue.append(action)
        dispatched = queue.dispatch_console(action.id)
        errors: list[str] = []
    except Exception as exc:  # pragma: no cover - defensive report path
        proposals = []
        applied = []
        after_contains = False
        queued = False
        dispatched = False
        errors = [str(exc)]

    writes = [
        str(vault / "SELF.md"),
        str(ledger_path),
        str(action_queue_path),
    ]
    rollback_paths = [
        item.rollback_snapshot_path for item in applied
        if item.rollback_snapshot_path
    ]
    writes.extend(rollback_paths)

    return {
        "success": not errors,
        "entrypoint": "future_behavior_autonomy",
        "vault_root": str(vault.resolve()),
        "before_contains_directive": before_contains,
        "after_contains_directive": after_contains,
        "future_behavior_changed": (not before_contains) and after_contains,
        "amendments_seen": len(proposals),
        "applied_count": len([item for item in applied if item.status == "applied"]),
        "rollback_paths": rollback_paths,
        "proactive_action_queued": queued,
        "proactive_action_dispatched": dispatched,
        "writes": writes,
        "external_sends": [],
        "errors": errors,
        "state": "live" if (after_contains and dispatched and not errors) else "partial",
    }


def _contains_all(text: str, names: tuple[str, ...]) -> bool:
    return all(IDENTITY_SENTINELS[name] in text for name in names)
