"""Promotion pipeline: staging → durable memory files.

Loads unpromoted candidates, batch-distills via reasoning_step,
scores against quality gate, dedup-checks against existing file content,
and promotes to MEMORY.md/USER.md/SELF.md.

Pattern: memory_reflect.py async flow with file reads and writes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from cognition.staging import (
    LOW_EVIDENCE_REASON_PREFIX,
    StagingCandidate,
    StagingStore,
    is_low_evidence_reason,
)


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
        return f"{LOW_EVIDENCE_REASON_PREFIX} ({c.evidence_count} < {min_evidence})"
    return "unknown"


def _normalize_line(line: str) -> str:
    """Fold a bullet/text line to a comparable form: strip bullet markers
    (``-``/``*``/``+``, ordered ``N.``/``N)``, and ``[ ]``/``[x]`` task boxes),
    trailing punctuation, and case/whitespace variance."""
    line = line.strip()
    line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
    line = re.sub(r"^\[[ xX]?\]\s+", "", line)
    line = line.rstrip(".!?,;:")
    line = re.sub(r"\s+", " ", line)
    return line.lower()


def _is_duplicate(text: str, existing: str) -> bool:
    """Check if text matches existing LINE(s) in target file content.

    Line-level comparison (not whole-file substring) so a short new fact
    that happens to be a contiguous substring of an unrelated, longer
    existing sentence is never mistaken for a duplicate. BOTH sides are
    split into physical lines and normalized identically (Codex gate MAJOR
    on PR #180 — the old code collapsed the new text's newlines to spaces
    while splitting existing per line, giving both false negatives and false
    positives on multi-line units). A multi-line unit is a duplicate only
    when every one of its lines already exists.
    """
    new_lines = [_normalize_line(ln) for ln in text.splitlines() if ln.strip()]
    if not new_lines:
        return True  # Empty text is a no-op
    existing_lines = {_normalize_line(line) for line in existing.splitlines() if line.strip()}
    return all(nl in existing_lines for nl in new_lines)


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


async def _batch_distill(candidates: list[StagingCandidate], cwd: Path) -> dict[str, str]:
    """Distill all candidates in a single reasoning_step call.

    CRITICAL: one LLM call for ALL candidates, not per-candidate.

    Returns a dict keyed by candidate.id (never positional). Each item in the
    LLM's response round-trips the "i" index it was given so a dropped or
    reordered item can never shift a later candidate's text onto the wrong
    id — a missing/invalid "i" just falls back to that one candidate's own
    raw observation instead of corrupting every candidate after it.
    """
    from cognition.steps import reasoning_step

    observations = [
        {"i": i, "type": c.candidate_type, "observation": c.observation}
        for i, c in enumerate(candidates)
    ]
    instruction = (
        "Distill each observation into a concise knowledge unit suitable for "
        "long-term memory. Keep facts precise. Remove conversation noise. "
        'Return a JSON array of objects: [{"i": <index>, "text": <distilled '
        'text>}], echoing back the same "i" index each observation was given.'
    )
    context = (
        "You are distilling raw conversation captures into structured knowledge.\n"
        f"Candidates:\n{json.dumps(observations, indent=2)}"
    )

    # Default: every candidate falls back to its own raw observation.
    result_map: dict[str, str] = {c.id: c.observation for c in candidates}

    try:
        # Item schema mirrors the round-trip contract so strict structured-output
        # providers enforce the {"i": int, "text": str} shape at the boundary,
        # not just via prompt prose (Kimi gate MINOR). Response-side validation
        # below still handles lenient providers.
        result = await reasoning_step(
            context,
            instruction,
            output_schema={
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                    "required": ["i", "text"],
                },
            },
            cwd=cwd,
        )

        if result.parsed and isinstance(result.parsed, list):
            seen: set[int] = set()
            for item in result.parsed:
                if not isinstance(item, dict):
                    continue
                idx = item.get("i")
                text = item.get("text")
                if not isinstance(idx, int) or isinstance(idx, bool):
                    continue
                if not (0 <= idx < len(candidates)):
                    continue
                cid = candidates[idx].id
                if idx in seen:
                    # Duplicate index — the LLM mislabeled at least one item, so
                    # DISTRUST BOTH: revert this id to its raw observation rather
                    # than let a later "i" silently overwrite an earlier
                    # candidate's text onto the wrong id (the exact
                    # cross-contamination this fix exists to kill). Idempotent
                    # for 3rd+ duplicates. (Codex gate BLOCKER on PR #180.)
                    result_map[cid] = candidates[idx].observation
                    continue
                seen.add(idx)
                # Keep the raw-observation fallback (already in result_map) for
                # empty/whitespace/non-string text: persisting "" makes the
                # downstream _is_duplicate("") return True -> mark_rejected ->
                # PERMANENT loss of a valid candidate; str()-ing a list/dict
                # pollutes durable memory. (Codex gate BLOCKER + MAJOR.)
                if not isinstance(text, str) or not text.strip():
                    continue
                result_map[cid] = text
    except Exception:
        pass

    return result_map


async def run_promotion_pipeline(
    staging_store: StagingStore,
    memory_dir: Path,
    cwd: Path,
    dry_run: bool = False,
) -> list[PromotionResult]:
    """Main promotion entry point. Called by daily reflection.

    Steps:
    0. Migrate legacy low_evidence rejections back to pending (#166)
    1. Load unpromoted candidates
    2. Pre-filter by quality gate
    3. Batch distill via reasoning_step (one LLM call)
    4. Dedup against existing file content
    5. Promote to target files
    """
    results: list[PromotionResult] = []

    # Migrate legacy low_evidence rejections back to pending before loading
    # unpromoted candidates, so previously-stuck rows re-enter this run (#166).
    # Best-effort: a migration failure (e.g. a lock timeout racing the live
    # bot's append()) must not abort the whole promotion pass. NEVER under
    # dry_run — a --test reflection must not mutate the staging file.
    if not dry_run:
        try:
            staging_store.unreject_low_evidence()
        except Exception as e:
            print(f"[promotion] unreject_low_evidence migration failed (non-blocking): {e}")

    # Step 1: Load unpromoted candidates
    candidates = staging_store.read_unpromoted()
    if not candidates:
        return results

    # Step 2: Pre-filter by quality gate. low_evidence candidates are
    # DEFERRED (left pending — they can still accumulate evidence via
    # StagingStore.append()'s merge path and re-qualify on a future run);
    # only a real quality failure (low_confidence) is permanently rejected.
    eligible: list[StagingCandidate] = []
    for c in candidates:
        if _passes_quality_gate(c):
            eligible.append(c)
            continue

        reason = _rejection_reason(c)
        action = "deferred" if is_low_evidence_reason(reason) else "rejected"
        if action == "rejected" and not dry_run:
            staging_store.mark_rejected(c.id, reason)
        results.append(PromotionResult(
            candidate_id=c.id,
            action=action,
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
    for candidate in eligible:
        distilled_text = distilled.get(candidate.id, candidate.observation)
        target = candidate.promotion_target
        if not target or target not in existing_content:
            target = "MEMORY.md"

        # Dedup: skip if distilled text already appears in target
        if _is_duplicate(distilled_text, existing_content[target]):
            if not dry_run:
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
