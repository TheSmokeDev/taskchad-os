"""Cognitive architecture module for The Homie chat engine.

Provides tiered recall, structured prompt regions, auto-capture,
injection defense, recall observability, promotion pipeline,
continuity tracking, and graph intelligence.
"""

from __future__ import annotations

from cognition.capture import auto_capture_from_turn
from cognition.identity_payload import build_identity_payload
from cognition.injection import sanitize_recalled_content
from cognition.observability import (
    CompactionEvent,
    RecallLog,
    log_compaction_event,
    log_recall_event,
)
from cognition.recall import RecallTier, classify_tier, run_recall_pipeline
from cognition.regions import PromptRegion, assemble_regions
from cognition.scheduled_payload import (
    ScheduledCognitionPayload,
    build_scheduled_cognition_payload,
    render_scheduled_cognition_context,
)
from cognition.status import collect_cognitive_loop_status

# Move 2 modules — guarded import (require runtime layer)
try:
    from cognition.continuity import (
        ContinuityState,
        load_continuity,
        save_continuity,
        update_continuity_from_turn,
    )
    from cognition.promotion import PromotionResult, run_promotion_pipeline
    from cognition.steps import (
        CognitiveContext,
        CognitiveStepResult,
        ReasoningStepResult,
        reasoning_step,
    )
except ImportError:
    pass  # Optional — promotion/continuity require runtime layer

# Move 3 modules — guarded import
try:
    from cognition.processes import (
        MentalProcess,
        ProcessState,
        detect_process,
        get_process_weights,
    )
    from cognition.self_model import InferenceRecord, InferenceTracker
    from cognition.skills import (
        SkillSpec,
        build_skill_index,
        propose_skill,
        write_skill,
    )
except ImportError:
    pass  # Optional — Move 3 modules

__all__ = [
    # Move 1
    "RecallTier",
    "classify_tier",
    "run_recall_pipeline",
    "assemble_regions",
    "PromptRegion",
    "auto_capture_from_turn",
    "sanitize_recalled_content",
    "RecallLog",
    "log_recall_event",
    # PRD-8 Phase 2 — identity payload shim (WS2)
    "build_identity_payload",
    "ScheduledCognitionPayload",
    "build_scheduled_cognition_payload",
    "render_scheduled_cognition_context",
    # Cognitive-loop truth surface
    "collect_cognitive_loop_status",
    # Move 2 — observability
    "CompactionEvent",
    "log_compaction_event",
    # Move 2 — promotion
    "run_promotion_pipeline",
    "PromotionResult",
    # Move 2 — continuity
    "ContinuityState",
    "load_continuity",
    "save_continuity",
    "update_continuity_from_turn",
    # Move 2 — steps
    "reasoning_step",
    "ReasoningStepResult",
    # Move 3 — steps (extended)
    "CognitiveContext",
    "CognitiveStepResult",
    # Move 3 — processes
    "MentalProcess",
    "ProcessState",
    "detect_process",
    "get_process_weights",
    # Move 3 — skills
    "SkillSpec",
    "build_skill_index",
    "propose_skill",
    "write_skill",
    # Move 3 — self-model
    "InferenceRecord",
    "InferenceTracker",
]
