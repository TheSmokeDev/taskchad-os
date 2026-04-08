"""Mental process state machine — 5 modes with keyword-based detection.

Classifies incoming messages into conversation modes (planning, monitoring,
learning, execution, default) using regex/keyword signals. Each mode maps
to region weight multipliers that adjust prompt assembly emphasis.

CRITICAL: Detection is heuristic only — no LLM calls. Transitions are
implicit routing, never announced to the user.

Pattern: recall.py classify_tier() — regex pattern matching for classification.
Pattern: continuity.py — dataclass + state tracking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class MentalProcess(Enum):
    DEFAULT = "default"
    PLANNING = "planning"
    MONITORING = "monitoring"
    LEARNING = "learning"
    EXECUTION = "execution"


# Signal patterns — PATTERN: recall.py _TIER_0_PATTERNS style
_PLANNING_SIGNALS = re.compile(
    r"let's plan|let's design|how should we|what's the approach|"
    r"strategy for|roadmap|architecture|spec|prd|prp|blueprint",
    re.I,
)
_MONITORING_SIGNALS = re.compile(
    r"check on|status of|how are .+ doing|any alerts|"
    r"server health|uptime|error rate|lead count|metric",
    re.I,
)
_LEARNING_SIGNALS = re.compile(
    r"remember that|from now on|my .+ is|i prefer|"
    r"important to know|fyi|heads up|new info|update:",
    re.I,
)
_EXECUTION_SIGNALS = re.compile(
    r"do it|build (?:this|the)|fix (?:this|the)|implement|deploy|run|execute|"
    r"create the|write the|add a|remove the|change the",
    re.I,
)
_EXPLICIT_OVERRIDE = re.compile(
    r"switch to (\w+) mode|enter (\w+) mode|go to (\w+) mode",
    re.I,
)

# Map mode name strings to enum values for explicit override
_MODE_MAP: dict[str, MentalProcess] = {
    "default": MentalProcess.DEFAULT,
    "planning": MentalProcess.PLANNING,
    "plan": MentalProcess.PLANNING,
    "monitoring": MentalProcess.MONITORING,
    "monitor": MentalProcess.MONITORING,
    "learning": MentalProcess.LEARNING,
    "learn": MentalProcess.LEARNING,
    "execution": MentalProcess.EXECUTION,
    "execute": MentalProcess.EXECUTION,
    "exec": MentalProcess.EXECUTION,
}

# Per-process region weight multipliers (applied to DEFAULT_REGION_BUDGETS)
PROCESS_WEIGHTS: dict[MentalProcess, dict[str, float]] = {
    MentalProcess.DEFAULT: {},  # All regions at 1.0x (no adjustment)
    MentalProcess.PLANNING: {
        "durable_memory": 1.5,
        "continuity": 1.5,
        "recalled_memory": 1.3,
        "prefetched_context": 0.7,
    },
    MentalProcess.MONITORING: {
        "prefetched_context": 1.5,
        "recalled_memory": 0.7,
        "procedural_memory": 0.7,
    },
    MentalProcess.LEARNING: {
        "user_model": 1.5,
        "durable_memory": 1.3,
        "procedural_memory": 0.5,
    },
    MentalProcess.EXECUTION: {
        "continuity": 1.5,
        "procedural_memory": 1.5,
        "recalled_memory": 1.3,
        "prefetched_context": 0.5,
    },
}


@dataclass
class ProcessState:
    """Current mental process state for a session."""

    active: MentalProcess = MentalProcess.DEFAULT
    previous: MentalProcess = MentalProcess.DEFAULT
    transition_reason: str = ""
    activated_at: str = ""
    session_id: str = ""


def detect_process(
    text: str,
    current: MentalProcess = MentalProcess.DEFAULT,
) -> tuple[MentalProcess, str]:
    """Detect mental process from message text. Returns (process, reason).

    CRITICAL: No LLM call. Regex/keyword only.
    First match wins for signal patterns.
    Short messages (<15 chars) skip detection to avoid false positives.
    """
    # Import min length from config with fallback
    try:
        from config import PROCESS_DETECTION_MIN_LENGTH
        min_length = PROCESS_DETECTION_MIN_LENGTH
    except ImportError:
        min_length = 15

    text_stripped = text.strip()

    # Check explicit override first (any length)
    override = _EXPLICIT_OVERRIDE.search(text_stripped)
    if override:
        mode_name = (override.group(1) or override.group(2) or override.group(3)).lower()
        if mode_name in _MODE_MAP:
            return _MODE_MAP[mode_name], "explicit_override"

    # Short messages: skip detection to avoid false positives
    if len(text_stripped) < min_length:
        return current, "no_transition"

    # Check signal patterns — first match wins
    if _PLANNING_SIGNALS.search(text_stripped):
        return MentalProcess.PLANNING, "planning_signal"
    if _MONITORING_SIGNALS.search(text_stripped):
        return MentalProcess.MONITORING, "monitoring_signal"
    if _LEARNING_SIGNALS.search(text_stripped):
        return MentalProcess.LEARNING, "learning_signal"
    if _EXECUTION_SIGNALS.search(text_stripped):
        return MentalProcess.EXECUTION, "execution_signal"

    return current, "no_transition"


def get_process_weights(process: MentalProcess) -> dict[str, float]:
    """Return region weight multipliers for the given process."""
    return PROCESS_WEIGHTS.get(process, {})


# === Move 5b: Executable process functions ===
# MentalProcess Protocol: async (wm, params?) -> WM | (WM, next_process_fn)
# These are the executable versions of what MentalProcess enum describes.


async def default_process(wm, params=None):
    """Default conversation — respond to the user."""
    from cognition.steps import external_dialog

    new_wm, response = await external_dialog(wm)
    return new_wm


async def planning_process(wm, params=None):
    """Planning mode — think internally, then respond with a plan."""
    from cognition.steps import external_dialog, internal_monologue

    wm, _thought = await internal_monologue(wm, "What approach should we take?")
    wm, response = await external_dialog(wm)
    return wm


async def monitoring_process(wm, params=None):
    """Monitoring mode — summarize status, flag issues."""
    from cognition.steps import external_dialog

    wm, response = await external_dialog(wm, "Summarize current status and flag issues.")
    return wm


async def learning_process(wm, params=None):
    """Learning mode — acknowledge new info, update model."""
    from cognition.steps import external_dialog

    wm, response = await external_dialog(wm, "Acknowledge this new information.")
    return wm


async def execution_process(wm, params=None):
    """Execution mode — do the requested work."""
    from cognition.steps import external_dialog

    wm, response = await external_dialog(wm, "Execute the requested task.")
    return wm


# Map enum -> executable function
PROCESS_FUNCTIONS: dict[MentalProcess, object] = {
    MentalProcess.DEFAULT: default_process,
    MentalProcess.PLANNING: planning_process,
    MentalProcess.MONITORING: monitoring_process,
    MentalProcess.LEARNING: learning_process,
    MentalProcess.EXECUTION: execution_process,
}


def execute_process(process: MentalProcess):
    """Get the executable function for a mental process."""
    return PROCESS_FUNCTIONS.get(process, default_process)
