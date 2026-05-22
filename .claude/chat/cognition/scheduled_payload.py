"""Shared read-only cognition payload for scheduled loop prompts.

This helper keeps scheduled entrypoints aligned with chat prompt assembly
without turning them into one generic prompt builder. Callers still own their
prompt ordering and task instructions; this module only gathers the shared
identity payload, active user inferences, and WORKING.md context.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Self-bootstrap like identity_payload.py: scheduled scripts import cognition
# from .claude/scripts, while chat imports it from .claude/chat.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from cognition.identity_payload import build_identity_payload
from cognition.self_model import InferenceRecord, InferenceTracker

DEFAULT_IDENTITY_ORDER: tuple[str, ...] = (
    "SOUL",
    "SELF",
    "USER",
    "MEMORY",
    "GOALS",
)


@dataclass(frozen=True)
class ScheduledCognitionPayload:
    """Read-only cognition context shared by scheduled entrypoints."""

    identity: dict[str, str]
    active_inferences: tuple[InferenceRecord, ...]
    active_inference_lines: tuple[str, ...]
    active_inference_section: str
    working_memory_section: str


def build_scheduled_cognition_payload(
    memory_dir: Path,
    *,
    inference_state_file: Path | None = None,
    min_confidence: float | None = None,
    cap: int | None = None,
) -> ScheduledCognitionPayload:
    """Build shared scheduled-loop context from caller-provided state.

    Missing identity files, absent inference state, and malformed inference
    state all degrade to empty sections instead of blocking scheduled jobs.
    """

    identity = build_identity_payload(memory_dir)
    state_file, resolved_min_confidence, resolved_cap = _resolve_inference_config(
        inference_state_file=inference_state_file,
        min_confidence=min_confidence,
        cap=cap,
    )
    active = _load_active_inferences(state_file, resolved_min_confidence)
    active = _sort_active_inferences(active)[:resolved_cap]
    active_lines = tuple(_format_inference_line(record) for record in active)
    active_section = (
        "## Active Beliefs About User\n" + "\n".join(active_lines)
        if active_lines
        else ""
    )
    working = identity.get("WORKING", "")
    working_section = f"## Current WORKING.md\n\n{working}" if working else ""

    return ScheduledCognitionPayload(
        identity=identity,
        active_inferences=tuple(active),
        active_inference_lines=active_lines,
        active_inference_section=active_section,
        working_memory_section=working_section,
    )


def render_identity_context(
    payload: ScheduledCognitionPayload,
    *,
    order: Iterable[str] = DEFAULT_IDENTITY_ORDER,
) -> str:
    """Render identity files with generic ``## Current X.md`` headers."""

    sections: list[str] = []
    for name in order:
        content = payload.identity.get(name, "")
        if content:
            sections.append(f"## Current {name}.md\n\n{content}")
    return "\n\n".join(sections)


def render_scheduled_cognition_context(
    payload: ScheduledCognitionPayload,
    *,
    include_identity: bool = False,
    identity_order: Iterable[str] = DEFAULT_IDENTITY_ORDER,
    header: str | None = None,
) -> str:
    """Render the shared scheduled cognition context deterministically."""

    sections: list[str] = []
    if include_identity:
        identity_context = render_identity_context(payload, order=identity_order)
        if identity_context:
            sections.append(identity_context)
    if payload.active_inference_section:
        sections.append(payload.active_inference_section)
    if payload.working_memory_section:
        sections.append(payload.working_memory_section)

    body = "\n\n".join(sections)
    if not body:
        return ""
    if header:
        return f"{header}\n\n{body}"
    return body


def _resolve_inference_config(
    *,
    inference_state_file: Path | None,
    min_confidence: float | None,
    cap: int | None,
) -> tuple[Path | None, float, int]:
    if inference_state_file is not None:
        state_file = Path(inference_state_file)
    else:
        state_file = None
        try:
            from config import INFERENCE_STATE_FILE

            state_file = INFERENCE_STATE_FILE
        except Exception:
            pass

    if min_confidence is None:
        min_confidence = 0.5
        try:
            from config import INFERENCE_PROMPT_MIN_CONFIDENCE

            min_confidence = INFERENCE_PROMPT_MIN_CONFIDENCE
        except Exception:
            pass

    if cap is None:
        cap = 10
        try:
            from config import INFERENCE_PROMPT_CAP

            cap = INFERENCE_PROMPT_CAP
        except Exception:
            pass

    return state_file, float(min_confidence), max(0, int(cap))


def _load_active_inferences(
    state_file: Path | None,
    min_confidence: float,
) -> list[InferenceRecord]:
    if state_file is None:
        return []
    try:
        return InferenceTracker(Path(state_file)).get_active(
            min_confidence=min_confidence,
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []


def _sort_active_inferences(records: list[InferenceRecord]) -> list[InferenceRecord]:
    active = list(records)
    active.sort(key=lambda r: r.last_updated or "", reverse=True)
    active.sort(key=lambda r: r.confidence, reverse=True)
    active.sort(key=lambda r: 0 if r.status == "confirmed" else 1)
    return active


def _format_inference_line(record: InferenceRecord) -> str:
    status_tag = (
        "confirmed"
        if record.status == "confirmed"
        else f"conf={record.confidence:.2f}"
    )
    return f"- [{status_tag}] {record.inference}"


__all__ = (
    "ScheduledCognitionPayload",
    "build_scheduled_cognition_payload",
    "render_identity_context",
    "render_scheduled_cognition_context",
)
