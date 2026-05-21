"""Validation-only helpers for cognitive-loop E2E probes.

These helpers are deliberately small and side-effect-light. They prove which
entrypoints currently consume the shared identity payload from a caller-provided
vault root, and they report missing/drift states instead of papering over them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import sys

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

    try:
        from cognition.identity_payload import build_identity_payload

        payload = build_identity_payload(vault)
    except Exception as exc:  # pragma: no cover - defensive reporting path
        payload = {}
        errors.append(f"identity_payload_error: {exc}")

    try:
        if entrypoint == "memory_reflect":
            from memory_reflect import _assemble_reflect_identity_section

            prompt_sections["identity"] = _assemble_reflect_identity_section(vault)
            identity_payload_present = _contains_all(
                prompt_sections["identity"],
                ("SOUL", "SELF", "USER", "MEMORY", "GOALS"),
            )
        elif entrypoint == "memory_weekly":
            from memory_weekly import _assemble_weekly_identity_section

            prompt_sections["identity"] = _assemble_weekly_identity_section(vault)
            identity_payload_present = _contains_all(
                prompt_sections["identity"],
                ("SOUL", "SELF", "USER", "MEMORY", "GOALS"),
            )
        elif entrypoint == "memory_dream":
            from memory_dream import (
                _assemble_consolidate_identity_section,
                _assemble_prune_memory_section,
            )

            memory_lines = len(payload.get("MEMORY", "").splitlines())
            prompt_sections["consolidate_identity"] = (
                _assemble_consolidate_identity_section(vault, memory_lines)
            )
            prompt_sections["prune_memory"] = _assemble_prune_memory_section(vault)
            identity_payload_present = _contains_all(
                prompt_sections["consolidate_identity"],
                ("SELF", "MEMORY", "GOALS"),
            )
        elif entrypoint == "heartbeat":
            heartbeat_source = (Path(__file__).resolve().parent / "heartbeat.py").read_text(
                encoding="utf-8",
            )
            prompt_sections["source_probe"] = heartbeat_source
            identity_payload_present = "build_identity_payload" in heartbeat_source
        else:
            errors.append(f"unknown_entrypoint: {entrypoint}")
    except Exception as exc:  # pragma: no cover - defensive reporting path
        errors.append(f"entrypoint_probe_error: {exc}")

    prompt_text = "\n\n".join(prompt_sections.values())
    active_inferences_present = (
        "InferenceTracker" in prompt_text or "user_inferences" in prompt_text
    )
    working_memory_present = (
        "WORKING" in prompt_text or IDENTITY_SENTINELS["WORKING"] in prompt_text
    )
    heartbeat_drift = entrypoint == "heartbeat" and not identity_payload_present

    missing = []
    if not identity_payload_present:
        missing.append("canonical_identity_payload")
    if not active_inferences_present:
        missing.append("active_inferences")
    if not working_memory_present:
        missing.append("working_memory_context")
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
        "runtime_mode": "fake_deterministic_probe",
        "external_sends": [],
        "errors": errors,
        "state": "drift" if heartbeat_drift else ("partial" if missing else "live"),
        "missing": missing,
        "test_mode": test_mode,
        "prompt_capture": {
            "scope": (
                "source_probe"
                if entrypoint == "heartbeat"
                else "scheduled_identity_sections"
            ),
            "sections": sorted(prompt_sections.keys()),
            "chars": len(prompt_text),
            "contains_seeded_identity": {
                name: sentinel in prompt_text
                for name, sentinel in IDENTITY_SENTINELS.items()
            },
        },
    }


def _contains_all(text: str, names: tuple[str, ...]) -> bool:
    return all(IDENTITY_SENTINELS[name] in text for name in names)
