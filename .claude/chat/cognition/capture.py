"""Post-response auto-capture with regex triggers.

Scans user messages and assistant responses for capturable content
(facts, preferences, decisions, entities). Fire-and-forget — must
never block response delivery. Max 3 captures per turn.

Pattern: OpenClaw auto-capture triggers from RESEARCH-cognitive-memory-architecture.md
"""

from __future__ import annotations

import re

from cognition.staging import StagingCandidate, StagingStore

# Capture triggers: (pattern, candidate_type)
_CAPTURE_TRIGGERS: list[tuple[re.Pattern, str]] = [  # type: ignore[type-arg]
    (re.compile(r"remember|remind|don't forget", re.I), "fact"),
    (re.compile(r"prefer|like|love|hate|want|always|never", re.I), "preference"),
    (re.compile(r"decided|agreed|let's go with|locked", re.I), "decision"),
    (re.compile(r"\+\d{10,}"), "entity"),  # Phone numbers
    (re.compile(r"[\w.-]+@[\w.-]+\.\w+"), "entity"),  # Email addresses
    (re.compile(r"\bmy\s+\w+\s+is\b|important to me", re.I), "fact"),
    (re.compile(r"I learned that .{15,}|I realized that .{15,}|I keep (making|doing|forgetting) .{10,}|I struggle with .{10,}", re.I), "self_model"),
    (re.compile(r"mistake I made.{10,}|pattern I('ve)? noticed.{10,}|turns out I .{10,}|I'm (good|bad|better|worse) at .{5,}", re.I), "self_model"),
]

_MIN_LENGTH = 10
_MAX_LENGTH = 500
_MAX_CAPTURES_PER_TURN = 3

# Reject XML/system markup
_SYSTEM_MARKUP = re.compile(r"</?system|</?recalled-memory|</?untrusted", re.I)


def _normalize_dedupe_key(text: str) -> str:
    """Create a normalized key for exact dedup."""
    # Lowercase, strip whitespace, collapse spaces
    return re.sub(r"\s+", " ", text.strip().lower())


def extract_candidates(
    user_message: str,
    assistant_response: str,
    session_id: str = "",
    turn_number: int = 0,
) -> list[StagingCandidate]:
    """Scan user message + response for capturable content.

    Returns up to MAX_CAPTURES_PER_TURN candidates.
    """
    candidates: list[StagingCandidate] = []

    # Scan both user message and assistant response
    texts_to_scan = [
        (user_message, "user"),
        (assistant_response, "assistant"),
    ]

    for text, source in texts_to_scan:
        if len(candidates) >= _MAX_CAPTURES_PER_TURN:
            break

        for pattern, candidate_type in _CAPTURE_TRIGGERS:
            if len(candidates) >= _MAX_CAPTURES_PER_TURN:
                break

            match = pattern.search(text)
            if not match:
                continue

            # Extract the sentence containing the match
            start = max(0, text.rfind(".", 0, match.start()) + 1)
            end = text.find(".", match.end())
            if end == -1:
                end = min(len(text), match.end() + 200)
            else:
                end += 1

            observation = text[start:end].strip()

            # Length filter
            if len(observation) < _MIN_LENGTH or len(observation) > _MAX_LENGTH:
                continue

            # Reject system markup
            if _SYSTEM_MARKUP.search(observation):
                continue

            dedupe_key = _normalize_dedupe_key(observation)

            # Determine promotion target
            if candidate_type == "preference":
                target = "USER.md"
            elif candidate_type == "self_model":
                target = "SELF.md"
            else:
                target = "MEMORY.md"

            candidates.append(
                StagingCandidate(
                    source_turn=f"{session_id}:{turn_number}",
                    candidate_type=candidate_type,
                    observation=observation,
                    inference="",
                    confidence=0.7,  # Default confidence for regex-triggered captures
                    evidence_count=1,
                    dedupe_key=dedupe_key,
                    promotion_target=target,
                )
            )

    return candidates[:_MAX_CAPTURES_PER_TURN]


def auto_capture_from_turn(
    user_message: str,
    assistant_response: str,
    staging_store: StagingStore,
    session_id: str = "",
    turn_number: int = 0,
) -> int:
    """Extract + dedup + write to staging. Returns count written.

    Fire-and-forget — caller wraps in try/except.
    """
    candidates = extract_candidates(
        user_message, assistant_response, session_id, turn_number
    )

    written = 0
    for candidate in candidates:
        if staging_store.append(candidate):
            written += 1

    # Move 5a: Feed preference/pattern captures to InferenceTracker
    if written > 0:
        try:
            from cognition.self_model import InferenceTracker

            from config import INFERENCE_STATE_FILE

            tracker = InferenceTracker(INFERENCE_STATE_FILE)
            for candidate in candidates:
                if candidate.candidate_type in ("preference", "decision"):
                    tracker.add_inference(
                        inference=candidate.observation[:200],
                        observation=candidate.observation,
                        confidence=0.5,
                        source="auto_capture",
                    )
        except (ImportError, Exception):
            pass  # Non-blocking — inference tracking is optional

    return written
