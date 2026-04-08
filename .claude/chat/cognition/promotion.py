"""Promotion pipeline: staging → durable memory files.

Loads unpromoted candidates, batch-distills via reasoning_step,
scores against quality gate, dedup-checks against existing file content,
and promotes to MEMORY.md/USER.md/SELF.md.

Pattern: memory_reflect.py async flow with file reads and writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cognition.staging import StagingCandidate, StagingStore


@dataclass
class PromotionResult:
    """Outcome of promoting a single candidate."""

    candidate_id: str
    action: str  # "promoted" | "rejected" | "deferred"
    target_file: str
    reason: str
    distilled_text: str
    original_observation: str


def _passes_quality_gate(c: StagingCandidate) -> bool:
    """Confidence > threshold AND evidence > minimum (self_model uses lower bar)."""
    from config import PROMOTION_CONFIDENCE_THRESHOLD, PROMOTION_EVIDENCE_MINIMUM, PROMOTION_SELF_MODEL_EVIDENCE_MINIMUM

    min_evidence = (
        PROMOTION_SELF_MODEL_EVIDENCE_MINIMUM if c.candidate_type == "self_model"
        else PROMOTION_EVIDENCE_MINIMUM
    )
    return (
        c.confidence >= PROMOTION_CONFIDENCE_THRESHOLD
        and c.evidence_count >= min_evidence
    )


def _rejection_reason(c: StagingCandidate) -> str:
    from config import PROMOTION_CONFIDENCE_THRESHOLD, PROMOTION_EVIDENCE_MINIMUM, PROMOTION_SELF_MODEL_EVIDENCE_MINIMUM

    min_evidence = (
        PROMOTION_SELF_MODEL_EVIDENCE_MINIMUM if c.candidate_type == "self_model"
        else PROMOTION_EVIDENCE_MINIMUM
    )
    if c.confidence < PROMOTION_CONFIDENCE_THRESHOLD:
        return f"low_confidence ({c.confidence:.2f} < {PROMOTION_CONFIDENCE_THRESHOLD})"
    if c.evidence_count < min_evidence:
        return f"low_evidence ({c.evidence_count} < {min_evidence})"
    return "unknown"


def _is_duplicate(text: str, existing: str) -> bool:
    """Check if text already appears in target file content."""
    normalized = text.strip().lower()
    if not normalized:
        return True  # Empty text is a no-op
    return normalized in existing.lower()


def _read_file(filepath: Path) -> str:
    """Read file content safely."""
    try:
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _append_to_file(filepath: Path, text: str) -> None:
    """Append a knowledge unit to a markdown file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"\n- {text}\n")


async def _batch_distill(candidates: list[StagingCandidate], cwd: Path) -> list[str]:
    """Distill all candidates in a single reasoning_step call.

    CRITICAL: one LLM call for ALL candidates, not per-candidate.
    Falls back to raw observations if distillation fails.
    """
    from cognition.steps import reasoning_step

    observations = [
        {"id": c.id, "type": c.candidate_type, "observation": c.observation}
        for c in candidates
    ]
    instruction = (
        "Distill each observation into a concise knowledge unit suitable for "
        "long-term memory. Keep facts precise. Remove conversation noise. "
        "Return a JSON array of strings, one per observation, in the same order."
    )
    context = (
        "You are distilling raw conversation captures into structured knowledge.\n"
        f"Candidates:\n{json.dumps(observations, indent=2)}"
    )

    try:
        result = await reasoning_step(
            context, instruction, output_schema={"type": "array"}, cwd=cwd
        )

        if result.parsed and isinstance(result.parsed, list):
            parsed = result.parsed
            # Pad if LLM returned fewer items
            while len(parsed) < len(candidates):
                parsed.append(candidates[len(parsed)].observation)
            return [str(item) for item in parsed]
    except Exception:
        pass

    # Fallback: use raw observations if distillation fails
    return [c.observation for c in candidates]


async def run_promotion_pipeline(
    staging_store: StagingStore,
    memory_dir: Path,
    cwd: Path,
    dry_run: bool = False,
) -> list[PromotionResult]:
    """Main promotion entry point. Called by daily reflection.

    Steps:
    1. Load unpromoted candidates
    2. Pre-filter by quality gate
    3. Batch distill via reasoning_step (one LLM call)
    4. Dedup against existing file content
    5. Promote to target files
    """
    results: list[PromotionResult] = []

    # Step 1: Load unpromoted candidates
    candidates = staging_store.read_unpromoted()
    if not candidates:
        return results

    # Step 2: Pre-filter by quality gate
    eligible = [c for c in candidates if _passes_quality_gate(c)]
    rejected = [c for c in candidates if not _passes_quality_gate(c)]

    # Mark rejected candidates
    for c in rejected:
        reason = _rejection_reason(c)
        staging_store.mark_rejected(c.id, reason)
        results.append(PromotionResult(
            candidate_id=c.id,
            action="rejected",
            target_file="",
            reason=reason,
            distilled_text="",
            original_observation=c.observation,
        ))

    if not eligible:
        return results

    # Step 3: Batch distillation via reasoning_step
    distilled = await _batch_distill(eligible, cwd)

    # Step 4: Load existing content for dedup
    existing_content: dict[str, str] = {
        "MEMORY.md": _read_file(memory_dir / "MEMORY.md"),
        "USER.md": _read_file(memory_dir / "USER.md"),
        "SELF.md": _read_file(memory_dir / "SELF.md"),
    }

    # Step 5: Promote each distilled candidate
    for candidate, distilled_text in zip(eligible, distilled):
        target = candidate.promotion_target
        if not target or target not in existing_content:
            target = "MEMORY.md"

        # Dedup: skip if distilled text already appears in target
        if _is_duplicate(distilled_text, existing_content[target]):
            staging_store.mark_rejected(candidate.id, "duplicate_in_target")
            results.append(PromotionResult(
                candidate_id=candidate.id,
                action="rejected",
                target_file=target,
                reason="duplicate_in_target",
                distilled_text=distilled_text,
                original_observation=candidate.observation,
            ))
            continue

        if not dry_run:
            _append_to_file(memory_dir / target, distilled_text)
            existing_content[target] += "\n" + distilled_text  # Update cache
            staging_store.mark_promoted(candidate.id, target)

        results.append(PromotionResult(
            candidate_id=candidate.id,
            action="promoted",
            target_file=target,
            reason="quality_gate_passed",
            distilled_text=distilled_text,
            original_observation=candidate.observation,
        ))

    return results
