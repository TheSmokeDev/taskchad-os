"""Code-backed cognitive-loop status collection.

This is an operator truth surface, not a roadmap summary. Each subsystem is
reported from importability and current source wiring so planned self-evolution
features are not accidentally presented as live.
"""

import importlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LIVE = "live"
SHADOW_ONLY = "shadow_only"
PLANNED = "planned"
MISSING = "missing"
DRIFT = "drift"
UNKNOWN = "unknown"
PARTIAL = "partial"

STATE_VALUES = frozenset({
    LIVE,
    SHADOW_ONLY,
    PLANNED,
    MISSING,
    DRIFT,
    UNKNOWN,
    PARTIAL,
})


def collect_cognitive_loop_status(
    *,
    chat_dir: Path | None = None,
    scripts_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable status report for the cognitive loop."""

    root = Path(__file__).resolve().parents[2]
    chat_root = chat_dir or root / "chat"
    scripts_root = scripts_dir or root / "scripts"

    paths = {
        "engine": chat_root / "engine.py",
        "working_memory": chat_root / "cognition" / "working_memory.py",
        "steps": chat_root / "cognition" / "steps.py",
        "processes": chat_root / "cognition" / "processes.py",
        "self_model": chat_root / "cognition" / "self_model.py",
        "amendments": chat_root / "cognition" / "amendments.py",
        "contradictions": chat_root / "cognition" / "contradictions.py",
        "identity_payload": chat_root / "cognition" / "identity_payload.py",
        "scheduled_payload": chat_root / "cognition" / "scheduled_payload.py",
        "reflect": scripts_root / "memory_reflect.py",
        "weekly": scripts_root / "memory_weekly.py",
        "dream": scripts_root / "memory_dream.py",
        "heartbeat": scripts_root / "heartbeat.py",
        "bootstrap": scripts_root / "runtime" / "bootstrap.py",
    }
    source = {name: _read_text(path) for name, path in paths.items()}

    subsystems = {
        "working_memory": _working_memory_status(source),
        "cognitive_steps": _import_status(
            "cognition.steps",
            ("reasoning_step", "create_cognitive_step"),
            LIVE,
            (
                ".claude/chat/cognition/steps.py exposes cognitive step "
                "wrappers and the WorkingMemory step factory."
            ),
        ),
        "mental_processes": _mental_process_status(source),
        "identity_payload": _identity_payload_status(source),
        "active_inferences": _active_inference_status(source),
        "scheduled_cognition_payload": _scheduled_payload_status(source),
        "reflection_identity": _scheduled_identity_status(
            source, "reflect", "memory_reflect.py"
        ),
        "weekly_identity": _scheduled_identity_status(
            source, "weekly", "memory_weekly.py"
        ),
        "dream_identity": _scheduled_identity_status(
            source, "dream", "memory_dream.py"
        ),
        "heartbeat_identity": _heartbeat_identity_status(source),
        "self_amendment": _self_amendment_status(source),
        "contradiction_detection": _contradiction_status(source),
        "proactive_brief": _proactive_brief_status(source),
    }

    state_counts = Counter(
        item["state"] for item in subsystems.values()
        if item.get("state") in STATE_VALUES
    )

    return {
        "overall": _overall_state(state_counts),
        "generated_at": datetime.now(UTC).isoformat(),
        "state_counts": dict(sorted(state_counts.items())),
        "subsystems": subsystems,
        "next_actions": _next_actions(subsystems),
    }


def _working_memory_status(source: dict[str, str]) -> dict[str, Any]:
    importable = _can_import("cognition.working_memory", ("WorkingMemory", "Memory"))
    engine_uses_prompt_regions = "PromptRegion(" in source["engine"]
    engine_uses_working_file = 'payload.get("WORKING"' in source["engine"]
    engine_owns_with_wm = "WorkingMemory(" in source["engine"]

    if importable and engine_uses_prompt_regions and not engine_owns_with_wm:
        return _status(
            SHADOW_ONLY,
            (
                "WorkingMemory is importable, but ConversationEngine still "
                "owns chat turns through PromptRegion assembly; WORKING.md is "
                "injected as a prompt region."
            ),
            importable=True,
            working_file_region=engine_uses_working_file,
            production_owner=False,
        )
    if importable and engine_owns_with_wm:
        return _status(
            LIVE,
            "WorkingMemory is importable and ConversationEngine source references it directly.",
            importable=True,
            production_owner=True,
        )
    if importable:
        return _status(
            SHADOW_ONLY,
            (
                "WorkingMemory is importable, but production ownership was "
                "not detected in ConversationEngine."
            ),
            importable=True,
            production_owner=False,
        )
    return _status(MISSING, "cognition.working_memory is not importable.")


def _mental_process_status(source: dict[str, str]) -> dict[str, Any]:
    importable = _can_import(
        "cognition.processes",
        ("MentalProcess", "detect_process", "get_process_weights"),
    )
    engine_wired = all(
        token in source["engine"]
        for token in ("detect_process", "get_process_weights", "apply_process_weights")
    )
    if importable and engine_wired:
        return _status(
            LIVE,
            (
                "ConversationEngine imports process detection and applies "
                "process weights during prompt assembly."
            ),
            importable=True,
            engine_wired=True,
        )
    if importable:
        return _status(
            SHADOW_ONLY,
            "Mental process primitives are importable, but engine wiring was not fully detected.",
            importable=True,
            engine_wired=False,
        )
    return _status(MISSING, "cognition.processes is not importable.")


def _identity_payload_status(source: dict[str, str]) -> dict[str, Any]:
    importable = _can_import("cognition.identity_payload", ("build_identity_payload",))
    consumers = [
        name for name in ("engine", "reflect", "weekly", "dream")
        if (
            "build_identity_payload" in source[name]
            or "build_scheduled_cognition_payload" in source[name]
        )
    ]
    if importable and {"engine", "reflect", "weekly", "dream"}.issubset(consumers):
        return _status(
            LIVE,
            (
                "Chat, reflection, weekly, and dream code all consume the "
                "canonical identity payload path."
            ),
            importable=True,
            consumers=consumers,
        )
    if importable:
        return _status(
            PARTIAL,
            (
                "The identity payload helper is importable, but not all "
                "expected consumers were detected."
            ),
            importable=True,
            consumers=consumers,
        )
    return _status(MISSING, "cognition.identity_payload is not importable.")


def _active_inference_status(source: dict[str, str]) -> dict[str, Any]:
    importable = _can_import(
        "cognition.self_model",
        ("InferenceTracker", "InferenceRecord"),
    )
    engine_wired = all(
        token in source["engine"]
        for token in ("InferenceTracker", "get_active", "user_inferences")
    )
    if importable and engine_wired:
        return _status(
            LIVE,
            (
                "ConversationEngine builds a user_inferences PromptRegion "
                "from InferenceTracker.get_active()."
            ),
            importable=True,
            engine_wired=True,
        )
    if importable:
        return _status(
            SHADOW_ONLY,
            (
                "InferenceTracker is importable, but active inference prompt "
                "injection was not fully detected."
            ),
            importable=True,
            engine_wired=False,
        )
    return _status(MISSING, "cognition.self_model is not importable.")


def _scheduled_identity_status(
    source: dict[str, str],
    source_key: str,
    filename: str,
) -> dict[str, Any]:
    scheduled_helper_used = "build_scheduled_cognition_payload" in source[source_key]
    identity_helper_used = "build_identity_payload" in source[source_key]
    if scheduled_helper_used:
        return _status(
            LIVE,
            f"{filename} assembles scheduled context through build_scheduled_cognition_payload().",
            helper="build_scheduled_cognition_payload",
        )
    if identity_helper_used:
        return _status(
            LIVE,
            f"{filename} assembles identity context through build_identity_payload().",
            helper="build_identity_payload",
        )
    return _status(
        DRIFT,
        (
            f"{filename} does not call build_identity_payload(); scheduled "
            "identity assembly can drift."
        ),
        helper="not_detected",
    )


def _heartbeat_identity_status(source: dict[str, str]) -> dict[str, Any]:
    scheduled_helper_used = "build_scheduled_cognition_payload" in source["heartbeat"]
    identity_helper_used = "build_identity_payload" in source["heartbeat"]
    recall_used = "caller=\"heartbeat\"" in source["heartbeat"]
    if scheduled_helper_used:
        return _status(
            LIVE,
            (
                "heartbeat.py assembles identity, active inferences, and "
                "WORKING.md through build_scheduled_cognition_payload()."
            ),
            helper="build_scheduled_cognition_payload",
            recall_context=recall_used,
        )
    if identity_helper_used:
        return _status(
            LIVE,
            "heartbeat.py calls build_identity_payload() for its prompt identity context.",
            helper="build_identity_payload",
            recall_context=recall_used,
        )
    return _status(
        DRIFT,
        (
            "heartbeat.py uses direct integration context and recall, but its "
            "main prompt does not share the canonical identity payload helper."
        ),
        helper="not_detected",
        recall_context=recall_used,
    )


def _scheduled_payload_status(source: dict[str, str]) -> dict[str, Any]:
    importable = _can_import(
        "cognition.scheduled_payload",
        (
            "build_scheduled_cognition_payload",
            "render_scheduled_cognition_context",
        ),
    )
    source_text = source["scheduled_payload"]
    has_identity = "build_identity_payload" in source_text
    has_inferences = "InferenceTracker" in source_text
    has_working = "WORKING" in source_text
    consumers = {
        name: "build_scheduled_cognition_payload" in source[name]
        for name in ("reflect", "weekly", "dream", "heartbeat")
    }
    if importable and has_identity and has_inferences and has_working and all(consumers.values()):
        return _status(
            LIVE,
            (
                "Scheduled cognition payload helper is importable and all "
                "scheduled loop entrypoints consume it."
            ),
            importable=True,
            identity_payload=True,
            active_inferences=True,
            working_memory_context=True,
            consumers=consumers,
        )
    return _status(
        PARTIAL,
        "Scheduled cognition payload helper is not fully wired across scheduled loops.",
        importable=importable,
        identity_payload=has_identity,
        active_inferences=has_inferences,
        working_memory_context=has_working,
        consumers=consumers,
    )


def _self_amendment_status(source: dict[str, str]) -> dict[str, Any]:
    importable = _can_import(
        "cognition.amendments",
        (
            "AmendmentProposal",
            "ProposalLedger",
            "build_amendment_gate_section",
        ),
    )
    self_prompt_updates = all(
        "SELF.md" in source[name] for name in ("reflect", "weekly", "dream")
    )
    consumers = {
        name: "build_amendment_gate_section" in source[name]
        for name in ("reflect", "weekly", "dream")
    }
    if importable and all(consumers.values()):
        return _status(
            LIVE,
            (
                "Human-gated amendment proposal ledger is importable and "
                "reflection, weekly, and dream prompts consume the gate."
            ),
            self_update_prompts=self_prompt_updates,
            proposal_ledger=True,
            human_gate=True,
            auto_apply=False,
            consumers=consumers,
        )
    if importable:
        return _status(
            PARTIAL,
            (
                "Amendment proposal ledger is importable, but not every "
                "scheduled memory loop consumes the human gate."
            ),
            self_update_prompts=self_prompt_updates,
            proposal_ledger=True,
            human_gate=False,
            auto_apply=False,
            consumers=consumers,
        )
    return _status(
        PLANNED,
        (
            "Reflection, weekly, and dream prompts can update SELF.md, but no "
            "human-gated amendment proposal ledger was detected."
        ),
        self_update_prompts=self_prompt_updates,
        proposal_ledger=False,
        human_gate=False,
        auto_apply=False,
    )


def _contradiction_status(source: dict[str, str]) -> dict[str, Any]:
    importable = _can_import(
        "cognition.contradictions",
        (
            "DriftFinding",
            "DriftLedger",
            "detect_cognitive_loop_drift",
            "build_drift_detection_section",
        ),
    )
    primitive = "def contradict(" in source["self_model"]
    dream_prompt_mentions = "Resolve contradictions" in source["dream"]
    consumers = {
        name: "build_drift_detection_section" in source[name]
        for name in ("weekly", "dream")
    }
    if importable and all(consumers.values()):
        return _status(
            LIVE,
            (
                "Bounded contradiction/roadmap-drift detector is importable "
                "and weekly/dream prompts consume deterministic findings."
            ),
            primitive=primitive,
            dream_prompt_mentions=dream_prompt_mentions,
            detector=True,
            bounded=True,
            source_paths=True,
            consumers=consumers,
        )
    if importable:
        return _status(
            PARTIAL,
            (
                "Contradiction/roadmap-drift detector is importable, but "
                "weekly/dream scheduled consumption is incomplete."
            ),
            primitive=primitive,
            dream_prompt_mentions=dream_prompt_mentions,
            detector=True,
            bounded=True,
            source_paths=True,
            consumers=consumers,
        )
    return _status(
        PLANNED,
        (
            "Inference contradiction primitives and dream prompt guidance "
            "exist, but no bounded contradiction/drift detector or findings "
            "ledger was detected."
        ),
        primitive=primitive,
        dream_prompt_mentions=dream_prompt_mentions,
        detector=False,
        bounded=False,
        source_paths=False,
    )


def _proactive_brief_status(source: dict[str, str]) -> dict[str, Any]:
    briefing = "build_session_briefing" in source["bootstrap"]
    working_memory = "_extract_working_memory" in source["bootstrap"]
    heartbeat = "run_heartbeat" in source["heartbeat"]
    if briefing and working_memory and heartbeat:
        return _status(
            PARTIAL,
            (
                "Session briefing injects working memory and heartbeat can "
                "notify, but there is no unified proactive brief builder yet."
            ),
            session_briefing=True,
            working_memory_section=True,
            heartbeat=True,
        )
    return _status(
        PLANNED,
        "A complete proactive brief path was not detected.",
        session_briefing=briefing,
        working_memory_section=working_memory,
        heartbeat=heartbeat,
    )


def _import_status(
    module_name: str,
    required_attrs: tuple[str, ...],
    live_state: str,
    evidence: str,
) -> dict[str, Any]:
    importable = _can_import(module_name, required_attrs)
    if importable:
        return _status(live_state, evidence, importable=True)
    return _status(MISSING, f"{module_name} is not importable.", importable=False)


def _can_import(module_name: str, attrs: tuple[str, ...] = ()) -> bool:
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return False
    return all(hasattr(module, attr) for attr in attrs)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _status(state: str, evidence: str, **details: Any) -> dict[str, Any]:
    if state not in STATE_VALUES:
        state = UNKNOWN
    return {
        "state": state,
        "evidence": evidence,
        "details": details,
    }


def _overall_state(state_counts: Counter[str]) -> str:
    if state_counts.get(DRIFT) or state_counts.get(MISSING):
        return PARTIAL
    if state_counts.get(PLANNED) or state_counts.get(SHADOW_ONLY):
        return PARTIAL
    if state_counts and set(state_counts) == {LIVE}:
        return LIVE
    return UNKNOWN


def _next_actions(subsystems: dict[str, dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    if subsystems["heartbeat_identity"]["state"] == DRIFT:
        actions.append(
            "Unify heartbeat prompt identity/cognition assembly with "
            "build_scheduled_cognition_payload()."
        )
    if subsystems["working_memory"]["state"] == SHADOW_ONLY:
        actions.append(
            "Keep WorkingMemory shadow-only until a dedicated production-owner "
            "cutover PRP is executed."
        )
    if subsystems["self_amendment"]["state"] == PLANNED:
        actions.append(
            "Add a human-gated self-amendment proposal ledger before applying "
            "SELF/SOUL/USER/MEMORY edits."
        )
    if subsystems["contradiction_detection"]["state"] == PLANNED:
        actions.append(
            "Add bounded contradiction/roadmap-drift findings with source "
            "paths and caps."
        )
    return actions


__all__ = (
    "collect_cognitive_loop_status",
    "STATE_VALUES",
    "LIVE",
    "SHADOW_ONLY",
    "PLANNED",
    "MISSING",
    "DRIFT",
    "UNKNOWN",
    "PARTIAL",
)
