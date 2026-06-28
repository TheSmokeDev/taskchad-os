"""Continuity state tracking across session boundaries.

Extracts current focus, open loops, pending commitments, and recent
decisions from conversation turns using lightweight heuristics (no LLM).
Persists as JSON per session. Formats for injection into continuity
prompt region.

Pattern: cognition/staging.py — dataclass + JSON file persistence.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Defaults — overridden by config.py when available
_MAX_OPEN_LOOPS = 5
_MAX_DECISIONS = 5
_LOW_SIGNAL_PHRASES = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "ok",
    "okay",
    "cool",
    "got it",
    "sounds good",
    "sound good",
    "right",
    "right right",
    "yoo",
    "yo",
    "still cooking",
    "how we looking",
    "how we looking still cooking",
    "we good",
    "any update",
    "do that",
    "lets do that",
    "let s do that",
}
_DIRECTIVE_RE = re.compile(
    r"\b("
    r"please|need|we need|i need|i want|go ahead|pull|deploy|set up|setup|"
    r"create|fix|run|show|implement|wire|push|restart|download|build|"
    r"make|update|ship"
    r")\b",
    re.I,
)


def _get_limits() -> tuple[int, int]:
    """Load limits from config, with fallback defaults."""
    try:
        from config import CONTINUITY_MAX_DECISIONS, CONTINUITY_MAX_OPEN_LOOPS

        return CONTINUITY_MAX_OPEN_LOOPS, CONTINUITY_MAX_DECISIONS
    except ImportError:
        return _MAX_OPEN_LOOPS, _MAX_DECISIONS


def _normalize_signal(text: str) -> str:
    """Normalize a short utterance for low-signal/follow-up detection."""

    normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return " ".join(normalized.split())


def _is_low_signal(text: str) -> bool:
    """Return True for acknowledgements/status pings that should not replace focus."""

    normalized = _normalize_signal(text)
    if not normalized:
        return True
    if normalized in _LOW_SIGNAL_PHRASES:
        return True
    if normalized.startswith("yes exactly") and len(normalized) <= 32:
        return True
    if normalized.startswith("right right") and len(normalized) <= 32:
        return True
    return False


def _first_substantive_sentence(text: str) -> str:
    """Pick the first non-acknowledgement sentence from a user message."""

    for match in re.finditer(r"[^.!?\n]+", text):
        sentence = match.group(0).strip()
        if not sentence or _is_low_signal(sentence):
            continue
        return sentence[:240].strip()
    stripped = text.strip()
    if stripped and not _is_low_signal(stripped):
        return stripped[:240].strip()
    return ""


def _looks_like_directive(text: str) -> bool:
    """Detect messages that should update the durable active task/goal."""

    if not text:
        return False
    return bool(_DIRECTIVE_RE.search(text))


@dataclass
class ContinuityState:
    """Tracks active context across session boundaries."""

    active_goal: str = ""
    current_focus: str = ""
    open_loops: list[str] = field(default_factory=list)
    pending_commitments: list[str] = field(default_factory=list)
    recent_decisions: list[str] = field(default_factory=list)
    session_id: str = ""
    turn_count: int = 0
    updated_at: str = ""

    def to_region_text(self) -> str:
        """Format for injection into continuity prompt region."""
        parts: list[str] = []
        if self.active_goal:
            parts.append(f"**Active Goal**: {self.active_goal}")
        if self.current_focus:
            parts.append(f"**Current Focus**: {self.current_focus}")
        if self.open_loops:
            parts.append(
                "**Open Loops**:\n" + "\n".join(f"- {item}" for item in self.open_loops[-5:])
            )
        if self.pending_commitments:
            parts.append(
                "**Pending**:\n"
                + "\n".join(f"- {c}" for c in self.pending_commitments[-5:])
            )
        if self.recent_decisions:
            parts.append(
                "**Recent Decisions**:\n"
                + "\n".join(f"- {d}" for d in self.recent_decisions[-5:])
            )
        return "\n\n".join(parts) if parts else ""


def save_continuity(state: ContinuityState, continuity_dir: Path) -> None:
    """Persist state to JSON file keyed by session_id."""
    continuity_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^\w\-.]", "_", state.session_id)
    filepath = continuity_dir / f"{safe_id}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, ensure_ascii=False, indent=2)


def load_continuity(session_id: str, continuity_dir: Path) -> ContinuityState:
    """Load persisted state. Returns empty state if not found."""
    safe_id = re.sub(r"[^\w\-.]", "_", session_id)
    filepath = continuity_dir / f"{safe_id}.json"
    if filepath.exists():
        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            return ContinuityState(**data)
        except Exception:
            pass
    return ContinuityState(session_id=session_id)


def update_continuity_from_turn(
    state: ContinuityState,
    user_message: str,
    assistant_response: str,
) -> ContinuityState:
    """Lightweight heuristic extraction from a single turn.

    CRITICAL: No LLM call — must be instant. Regex/keyword only.
    """
    max_loops, max_decisions = _get_limits()

    state.turn_count += 1
    state.updated_at = datetime.now(UTC).isoformat()

    # Update focus/task from the first substantive sentence, skipping short
    # acknowledgements and status pings that would otherwise erase the real work.
    substantive = _first_substantive_sentence(user_message)
    if substantive:
        state.current_focus = substantive[:180].strip()
        if _looks_like_directive(substantive):
            state.active_goal = substantive[:240].strip()

    # Detect open loops: questions in user message
    questions = re.findall(r"[^.!]*\?", user_message)
    for q in questions[:2]:
        q = q.strip()
        if len(q) > 15 and q not in state.open_loops:
            state.open_loops.append(q)
            state.open_loops = state.open_loops[-max_loops:]

    # Detect commitments: "I'll", "I will", "let me" in assistant
    commitment_patterns = re.findall(
        r"(?:I'll|I will|let me|I'm going to)\s+([^.!]{10,80})",
        assistant_response,
        re.I,
    )
    for c in commitment_patterns[:2]:
        c = c.strip()
        if c not in state.pending_commitments:
            state.pending_commitments.append(c)
            state.pending_commitments = state.pending_commitments[-max_decisions:]

    # Detect decisions: "decided", "let's go with", "agreed"
    decision_patterns = re.findall(
        r"(?:decided|let's go with|agreed|locked in)\s+([^.!]{10,80})",
        user_message + " " + assistant_response,
        re.I,
    )
    for d in decision_patterns[:2]:
        d = d.strip()
        if d not in state.recent_decisions:
            state.recent_decisions.append(d)
            state.recent_decisions = state.recent_decisions[-max_decisions:]

    return state


def cleanup_old_continuity(continuity_dir: Path, max_age_days: int = 7) -> int:
    """Remove continuity files older than max_age_days. Returns count removed."""
    if not continuity_dir.exists():
        return 0
    now = datetime.now().timestamp()
    removed = 0
    for f in continuity_dir.glob("*.json"):
        age_days = (now - f.stat().st_mtime) / 86400
        if age_days > max_age_days:
            f.unlink()
            removed += 1
    return removed
