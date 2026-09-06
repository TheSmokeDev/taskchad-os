"""
Dream Cycle — Memory Consolidation for The Homie.

4-phase pipeline: Orient -> Gather Signal -> Consolidate -> Prune & Reindex.
Phases 1-2 are pure Python (no LLM). Phase 2 exits with DREAM_SILENT if no
signal found, skipping all LLM calls entirely.

Inspired by Claude Code Auto-Dream but built at the FRAMEWORK level -
provider-agnostic via the lane-first runtime router. Works with Claude, Codex,
Gemini, or any provider configured in the runtime.

Patterns borrowed from Hermes Agent cron scheduler:
- [SILENT] suppression (no signal → no LLM call)
- Crash-safe scheduling (advance state BEFORE execution)
- Cross-platform file locking (shared.file_lock)

Usage:
    uv run python memory_dream.py              # Run dream cycle
    uv run python memory_dream.py --test       # Dry run (no file edits)
    uv run python memory_dream.py --force      # Skip recency guard
    uv run python memory_dream.py --days 14    # Scan 14 days of logs

``--test`` means NO FILE EDITS, and that is a contract, not a description: no
dream-state write, no daily-log line, no belief decision artifact, no lock
sidecar, no directory creation. It is the only reason ``persona_dream_tick.py
--child-test`` is a safe probe, and the only reason a --test run cannot make
the 20-hour recency guard swallow that night's real dream.

``--test`` is still a real dry run: it CALLS the LLM so an operator can see what
the dream would write. Add ``--no-llm`` (requires ``--test``) to stop after the
free phases instead — same no-edit contract, zero tokens. That is what a
fan-out probe across a whole roster wants: proof the plumbing works, not a
night's bill.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

# M4 import-order pattern (PRD-8 Phase 2 WS3): inject .claude/chat onto sys.path
# AFTER apply_persona_override() boot-shim and BEFORE importing the new shim.
# Symmetrical with memory_reflect.py + memory_weekly.py.
_CHAT_DIR = Path(__file__).resolve().parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.amendments import (  # noqa: E402
    ProposalLedger,
    build_amendment_gate_section,
    ledger_file_lock,
    process_amendment_output,
)
from cognition.contradictions import build_drift_detection_section  # noqa: E402
from cognition.identity_payload import build_identity_payload  # noqa: E402
from cognition.proactive_brief import build_proactive_brief_section  # noqa: E402
from cognition.scheduled_payload import (  # noqa: E402
    build_scheduled_cognition_payload,
)
from cognition.status import collect_cognitive_loop_status  # noqa: E402

from config import (  # noqa: E402
    AMENDMENT_APPLY_LIMIT,
    AMENDMENT_LEDGER_FILE,
    AMENDMENT_SECTION_CAP,
    DAILY_DIR,
    DREAM_MIN_INTERVAL_HOURS,
    DREAM_SIGNAL_THRESHOLD,
    DREAM_STATE_FILE,
    GOALS_FILE,
    MEMORY_DIR,
    MEMORY_FILE,
    PROJECT_ROOT,
    SELF_FILE,
    STATE_DIR,
    ensure_directories,
    get_background_models,
    get_today_log_path,
    now_local,
)
from shared import append_to_daily_log, file_lock, load_state, save_state

# =============================================================================
# CONSTANTS
# =============================================================================

DREAM_SILENT = "DREAM_SILENT"
# A --test --no-llm probe that found signal and stopped before Phase 3. Distinct
# from DREAM_SILENT: silence means "nothing to consolidate", this means "there
# WAS something and we deliberately did not spend a token on it".
DREAM_NO_LLM = "DREAM_NO_LLM"
# Recorded in dream-state.json when there were no daily logs to scan. An honest
# "nothing to look at" is a completed cycle and still owes a receipt: without
# one, a persona fan-out cannot tell this apart from a child that died before
# writing anything, and the safe reading of "no receipt" is failure. NOT
# returned to callers — memory_reflect/memory_weekly treat any non-DREAM_SILENT
# truthy return as "consolidation ran", so this path still returns None.
DREAM_SKIPPED = "DREAM_SKIPPED"
# Env var by which a persona-dream parent hands this process a per-spawn nonce,
# echoed into dream-state.json so the parent can prove the receipt it reads back
# was written by the child it just launched (and not left over from last night).
SPAWN_ID_ENV = "HOMIE_DREAM_SPAWN_ID"
MAX_SIGNAL_CHARS = 5_000
MAX_LOG_CHARS_PER_FILE = 8_000

# Signal detection patterns (compiled once at module level)
_CORRECTION_RE = re.compile(
    r"\b(no,|don't|don't|wrong|stop doing|actually,|not that|"
    r"that's not|shouldn't have|mistake)\b",
    re.IGNORECASE,
)
_SAVE_RE = re.compile(
    r"\b(remember|important|key decision|lesson learned|lesson:|"
    r"note to self|note:|takeaway|never forget)\b",
    re.IGNORECASE,
)
_STALL_RE = re.compile(
    r"\b(stuck|blocked|failed|broke|broken|error|regression|"
    r"reverted|rolled back|can't figure)\b",
    re.IGNORECASE,
)

# Entity extraction patterns for frequency analysis
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


# =============================================================================
# DATA TYPES
# =============================================================================


@dataclass
class OrientResult:
    """Phase 1 output — orientation snapshot."""

    memory_lines: int = 0
    memory_size_chars: int = 0
    daily_logs: list[Path] = field(default_factory=list)
    concepts_count: int = 0
    self_exists: bool = False
    goals_exists: bool = False


@dataclass
class SignalResult:
    """Phase 2 output — gathered signal from logs."""

    found: bool = False
    digest: str = ""
    corrections: list[str] = field(default_factory=list)
    saves: list[str] = field(default_factory=list)
    stalls: list[str] = field(default_factory=list)
    repeated_entities: list[str] = field(default_factory=list)
    files_scanned: int = 0
    signal_score: int = 0
    # Living Mind Act 3 — open episodes scanned this run (additive field).
    episode_paths: list[Path] = field(default_factory=list)


# =============================================================================
# IDENTITY SECTION ASSEMBLY (PRD-8 Phase 2 WS3 — F2 post-build fix)
# =============================================================================


def _assemble_consolidate_identity_section(
    memory_dir: Path, memory_lines: int
) -> str:
    """Assemble the dream-consolidate identity section using the shim.

    Single source of truth for the consolidate-phase identity prologue —
    production code (``consolidate``) and parity tests both consume this
    helper, so any drift in headers or ordering breaks both at once.

    Order MEMORY/SELF/GOALS and the ``({N} lines)`` MEMORY-header annotation
    are contract-locked by ``tests/test_memory_dream.py``.
    """
    payload = build_scheduled_cognition_payload(memory_dir).identity
    memory_content = payload.get("MEMORY", "")
    self_content = payload.get("SELF", "")
    goals_content = payload.get("GOALS", "")

    return f"""## Current MEMORY.md ({memory_lines} lines)

{memory_content}

## Current SELF.md

{self_content}

## Current GOALS.md (read-only — reference only, do NOT edit)

{goals_content}"""


def _assemble_dream_cognition_section(
    memory_dir: Path,
    inference_state_file: Path | None = None,
) -> str:
    """Assemble the unified proactive brief for dream consolidation."""

    return build_proactive_brief_section(
        memory_dir,
        inference_state_file=inference_state_file,
        include_identity=False,
        header="## Scheduled Proactive Brief",
    )


def _assemble_dream_amendment_section(
    ledger_file: Path | None = None,
    *,
    source: str = "memory_dream",
) -> str:
    """Assemble the human-gated amendment proposal instructions.

    ``ledger_file`` is a ``None`` sentinel resolved to
    ``AMENDMENT_LEDGER_FILE`` at call time (Rule 1 — never bind tunable
    config in default args).
    """

    if ledger_file is None:
        ledger_file = AMENDMENT_LEDGER_FILE
    return build_amendment_gate_section(
        ledger_file,
        source=source,
        ledger=ProposalLedger(ledger_file),
    )


def _assemble_dream_drift_section(
    project_root: Path | None = None,
    cognitive_loop_status: dict | None = None,
) -> str:
    """Assemble deterministic contradiction/roadmap-drift findings."""

    if project_root is None:
        project_root = PROJECT_ROOT
    status = cognitive_loop_status or collect_cognitive_loop_status()
    return build_drift_detection_section(project_root, status)


def _assemble_episodes_section(episode_paths: list[Path]) -> str:
    """Assemble the consolidate-phase open-episodes digest section.

    Living Mind Act 3. Returns ``""`` for empty paths so the consolidate
    prompt stays byte-identical to pre-Act-3 assembly when no episodes
    exist. Digest caps come from ``config.get_episode_settings()`` (Rule 1,
    resolved inside ``render_episodes_digest``).
    """
    if not episode_paths:
        return ""
    from episodes import render_episodes_digest

    digest = render_episodes_digest(episode_paths)
    if not digest:
        return ""
    return "## Recent Episodes (open)\n\n" + digest


def _assemble_prune_memory_section(memory_dir: Path) -> str:
    """Assemble the dream-prune MEMORY-only section using the shim.

    Prune reads MEMORY only — the ``include=("MEMORY",)`` scoping is part
    of the contract. Header annotation ``({N} lines)`` is computed from the
    fetched content so the line count matches the rendered prompt body.
    """
    payload = build_identity_payload(memory_dir, include=("MEMORY",))
    memory_content = payload.get("MEMORY", "")
    memory_lines = len(memory_content.splitlines())

    return f"""## Current MEMORY.md ({memory_lines} lines)

{memory_content}"""


# =============================================================================
# PHASE 1: ORIENT (pure Python)
# =============================================================================


def orient(days: int = 7) -> OrientResult:
    """Load orientation data. No LLM call.

    Reads current memory state: line counts, recent logs, concept pages.
    """
    result = OrientResult()

    # MEMORY.md stats
    if MEMORY_FILE.exists():
        content = MEMORY_FILE.read_text(encoding="utf-8")
        result.memory_lines = len(content.splitlines())
        result.memory_size_chars = len(content)

    # Recent daily logs
    today = now_local().date()
    for i in range(1, days + 1):
        target_date = today - timedelta(days=i)
        log_path = DAILY_DIR / f"{target_date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            result.daily_logs.append(log_path)

    # Concept pages
    concepts_dir = MEMORY_DIR / "concepts"
    if concepts_dir.exists():
        result.concepts_count = len(list(concepts_dir.glob("*.md")))

    # Context files
    result.self_exists = SELF_FILE.exists()
    result.goals_exists = GOALS_FILE.exists()

    return result


# =============================================================================
# PHASE 2: GATHER SIGNAL (pure Python grep)
# =============================================================================


def _extract_matches(pattern: re.Pattern, text: str, context_chars: int = 80) -> list[str]:
    """Extract pattern matches with surrounding context."""
    matches = []
    for m in pattern.finditer(text):
        start = max(0, m.start() - context_chars)
        end = min(len(text), m.end() + context_chars)
        snippet = text[start:end].strip()
        # Clean up to single line
        snippet = " ".join(snippet.split())
        if snippet and snippet not in matches:
            matches.append(snippet)
    return matches


def _extract_entities(text: str) -> list[str]:
    """Extract bold terms and wikilinks as entity candidates."""
    entities = []
    for m in _BOLD_RE.finditer(text):
        term = m.group(1).strip()
        if len(term) > 2 and len(term) < 60:
            entities.append(term.lower())
    for m in _WIKILINK_RE.finditer(text):
        term = m.group(1).strip()
        if len(term) > 2:
            entities.append(term.lower())
    return entities


def gather_signal(
    daily_logs: list[Path], days: int = 7, memory_dir: Path | None = None
) -> SignalResult:
    """Scan logs for consolidation-worthy signal. No LLM call.

    Greps daily logs, session flush files, and open episodes for
    corrections, saves, stalls, and repeated entities. Returns a signal
    digest. ``memory_dir`` is a None sentinel resolved to the module's
    ``MEMORY_DIR`` inside the body (Rule 1 — existing call sites unchanged).
    """
    result = SignalResult()
    all_corrections: list[str] = []
    all_saves: list[str] = []
    all_stalls: list[str] = []
    entity_counter: Counter = Counter()
    entity_sources: dict[str, set[str]] = {}  # entity -> set of source files

    # Scan daily logs
    for log_path in daily_logs:
        try:
            content = log_path.read_text(encoding="utf-8")
            if len(content) > MAX_LOG_CHARS_PER_FILE:
                content = content[-MAX_LOG_CHARS_PER_FILE:]
            result.files_scanned += 1

            all_corrections.extend(_extract_matches(_CORRECTION_RE, content))
            all_saves.extend(_extract_matches(_SAVE_RE, content))
            all_stalls.extend(_extract_matches(_STALL_RE, content))

            # Entity frequency across files
            for entity in _extract_entities(content):
                entity_counter[entity] += 1
                entity_sources.setdefault(entity, set()).add(log_path.name)
        except Exception:
            continue

    # Scan session flush files (filtered by mtime — only recent ones).
    # This legacy scan mines raw leftovers of FAILED flushes (successful
    # flushes unlink their context file) — kept verbatim; episodes below are
    # the summary-grade source for successful flushes. Additive, not
    # replacement.
    flush_cutoff = datetime.now().timestamp() - (days * 86400)
    for flush_file in sorted(STATE_DIR.glob("session-flush-*.md")):
        try:
            if flush_file.stat().st_mtime < flush_cutoff:
                continue
            content = flush_file.read_text(encoding="utf-8")
            if len(content) > MAX_LOG_CHARS_PER_FILE:
                content = content[-MAX_LOG_CHARS_PER_FILE:]
            result.files_scanned += 1

            all_corrections.extend(_extract_matches(_CORRECTION_RE, content))
            all_saves.extend(_extract_matches(_SAVE_RE, content))
            all_stalls.extend(_extract_matches(_STALL_RE, content))

            for entity in _extract_entities(content):
                entity_counter[entity] += 1
                entity_sources.setdefault(entity, set()).add(flush_file.name)
        except Exception:
            continue

    # Living Mind Act 3 — scan open episodes (window + status filtered,
    # newest-first). Substantive episodes RAISE the weighted score so
    # sessions with real decisions make the dream fire.
    if memory_dir is None:
        memory_dir = MEMORY_DIR
    try:
        from episodes import list_open_episodes

        open_episodes = list_open_episodes(memory_dir, days=days)
    except Exception:
        open_episodes = []
    for episode_path in open_episodes:
        try:
            content = episode_path.read_text(encoding="utf-8")
            if len(content) > MAX_LOG_CHARS_PER_FILE:
                content = content[-MAX_LOG_CHARS_PER_FILE:]
            result.files_scanned += 1

            all_corrections.extend(_extract_matches(_CORRECTION_RE, content))
            all_saves.extend(_extract_matches(_SAVE_RE, content))
            all_stalls.extend(_extract_matches(_STALL_RE, content))

            for entity in _extract_entities(content):
                entity_counter[entity] += 1
                entity_sources.setdefault(entity, set()).add(episode_path.name)
            result.episode_paths.append(episode_path)
        except Exception:
            continue

    # Deduplicate and limit
    result.corrections = list(dict.fromkeys(all_corrections))[:10]
    result.saves = list(dict.fromkeys(all_saves))[:10]
    result.stalls = list(dict.fromkeys(all_stalls))[:10]

    # Entities appearing in 3+ different source files
    result.repeated_entities = [
        entity
        for entity, sources in entity_sources.items()
        if len(sources) >= 3
    ]

    # Weighted signal score — require minimum threshold to trigger LLM
    result.signal_score = (
        len(result.corrections) * 2
        + len(result.saves) * 2
        + len(result.stalls) * 1
        + len(result.repeated_entities) * 3
    )
    result.found = result.signal_score >= DREAM_SIGNAL_THRESHOLD

    # Build digest
    if result.found:
        parts = []
        if result.corrections:
            parts.append("## Corrections / Feedback\n")
            for c in result.corrections[:5]:
                parts.append(f"- {c}")
        if result.saves:
            parts.append("\n## Explicit Saves / Lessons\n")
            for s in result.saves[:5]:
                parts.append(f"- {s}")
        if result.stalls:
            parts.append("\n## Stalls / Failures\n")
            for st in result.stalls[:5]:
                parts.append(f"- {st}")
        if result.repeated_entities:
            parts.append("\n## Recurring Entities (3+ sources)\n")
            for e in result.repeated_entities[:10]:
                count = entity_counter[e]
                sources = entity_sources[e]
                parts.append(f"- **{e}** ({count}x across {len(sources)} files)")

        result.digest = "\n".join(parts)[:MAX_SIGNAL_CHARS]

    return result


# =============================================================================
# PHASE 3: CONSOLIDATE (LLM via run_with_runtime_lanes)
# =============================================================================


async def consolidate(
    signal: SignalResult,
    orientation: OrientResult,
    test_mode: bool = False,
    post_weekly: bool = False,
) -> str:
    """Merge signal into memory files via LLM. Provider-agnostic."""
    from claude_agent_sdk import HookMatcher

    from runtime.base import RuntimeRequest
    from runtime.capabilities import TOOL_REASONING
    from runtime.lane_router import run_with_runtime_lanes
    from shared import validate_bash_command

    # PRD-8 Phase 2 WS3: assemble identity section via the extracted helper.
    # Order MEMORY/SELF/GOALS + headers locked by parity tests in
    # tests/test_memory_dream.py — production helper is the test target.
    identity_section = _assemble_consolidate_identity_section(
        MEMORY_DIR, orientation.memory_lines
    )
    cognition_section = _assemble_dream_cognition_section(MEMORY_DIR)
    amendment_section = _assemble_dream_amendment_section()
    drift_section = _assemble_dream_drift_section()

    # Living Mind Act 3 — open-episodes digest. Both the section and its
    # instruction bullet are empty strings when no episodes exist, keeping
    # the assembled prompt byte-identical to pre-Act-3 for that case.
    episodes_section = _assemble_episodes_section(signal.episode_paths)
    episodes_instruction = ""
    if episodes_section:
        episodes_section = episodes_section + "\n"
        episodes_instruction = (
            "\n5. **Mine the open episodes**: recurring themes, durable lessons, "
            "decisions, or self-knowledge across episodes belong in the amendment "
            "proposals above. Do not copy episode text verbatim — distill.\n"
        )

    today_str = now_local().strftime("%Y-%m-%d")

    post_weekly_note = ""
    if post_weekly:
        post_weekly_note = (
            "\n## IMPORTANT: Weekly Synthesis Context\n\n"
            "Weekly synthesis JUST ran and may have already promoted some of these items "
            "into MEMORY.md. Check carefully for duplicates before adding anything. "
            "If an item from the signal digest is already present in MEMORY.md "
            "(even paraphrased), skip it.\n"
        )

    dry_run_note = (
        "\n\nDRY RUN: Do NOT edit any files. Describe what you would change.\n"
        if test_mode
        else ""
    )

    # Living Self Act 4 (#170) — the nightly belief-candidate harvest. This is
    # ADDITIVE to the prompt the model already receives (zero new LLM calls): the
    # SAME consolidation call may ALSO emit 0-N fenced `belief_candidate` JSON
    # blocks, parsed downstream by Phase 5's extract_belief_candidates(). Spliced
    # in as an unnumbered section (the numbered list ends at #4/#5-episodes) so it
    # never renumbers the byte-parity-tested identity/instruction blocks.
    from config import get_belief_evolve_settings  # Rule 1 — call-time resolve

    _bs = get_belief_evolve_settings()
    # Kimi gate MINOR on PR #181: don't ask the model for belief candidates the
    # rail will never consume. When autonomy is off (EVOLVE_ENABLED=false OR the
    # kill switch disabled), emit NO section — a true no-op, not dead prompt
    # weight that contradicts the "disablement is a true no-op" contract.
    _belief_rail_on = _bs.enabled
    try:
        from security import kill_switches
        _belief_rail_on = _belief_rail_on and not kill_switches.is_disabled("belief_autonomy")
    except Exception:
        pass
    if not _belief_rail_on:
        belief_candidate_section = ""
    else:
        belief_candidate_section = f"""
## Belief Candidates (optional, identity-grade only)

Scan the signal above for identity-grade patterns: a durable, EVIDENCE-BACKED
belief about the operator or about how The Homie itself works (NOT a routine
MEMORY.md lesson — those go through the numbered amendments above). Your job
here is GENERATION, not gatekeeping — every block you emit runs through a
separate evidence-read + LLM judge before anything is written to disk (aim for
confidence >= {_bs.candidate_min_confidence:.2f}), so when the same operator
preference, working pattern, or system truth recurs across the window, DO emit
it and let that gate decide. Zero is still the right answer on a quiet night.

Emit up to {_bs.max_candidates_per_night} candidate block(s). Each block must
be a fenced ```json block containing EXACTLY ONE compact JSON object on a
SINGLE line — no pretty-printing, shaped exactly like this example:

```json
{{"kind": "belief_candidate", "target_file": "SELF.md", "summary": "<short review title>", "rationale": "<why this belief is justified>", "evidence_paths": ["<vault-relative paths to REAL files with real, non-empty content>"], "proposed_content": "<the belief text, under 1200 chars>", "confidence_score": 0.85}}
```

`target_file` must be "SELF.md" or "USER.md"; `confidence_score` is 0.0-1.0.
"""

    prompt = f"""Memory dream consolidation. Merge recent signal into long-term memory.
{dry_run_note}
## Signal Digest (from last {len(signal.corrections) + len(signal.saves) + len(signal.stalls)} items)

{signal.digest}

{identity_section}
{cognition_section}
{amendment_section}
{drift_section}
{episodes_section}{post_weekly_note}
## Instructions

Today is {today_str}. Consolidate the signal above into memory:

1. **Propose MEMORY.md amendments** ({MEMORY_FILE}):
   - Propose new lessons, decisions, or important context from the signal
   - Do NOT duplicate items already present
   - Convert any relative dates ("yesterday", "last week") to absolute dates
   - Keep entries concise (1-2 lines each)
   - Do not edit MEMORY.md directly

2. **Propose SELF.md amendments** ({SELF_FILE}) ONLY if:
   - 2+ correction signals point to a recurring failure mode
   - A stall reveals a new area of low confidence
   - A repeated entity shows a new capability or pattern
   - Skip if no strong evidence
   - Do not edit SELF.md directly

3. **Resolve contradictions**: If a new signal contradicts an existing MEMORY.md entry,
   propose the replacement or deletion through the amendment ledger. Include source evidence.

4. Log a brief summary of changes to today's daily log ({get_today_log_path()}).
{episodes_instruction}{belief_candidate_section}
If nothing in the signal warrants changes, respond with exactly: CONSOLIDATION_OK
"""

    result = await run_with_runtime_lanes(
        RuntimeRequest(
            prompt=prompt,
            cwd=PROJECT_ROOT,
            task_name="memory_dream_consolidate",
            capability=TOOL_REASONING,
            # QUALITY background tier (sonnet) — deep consolidation that
            # rewrites durable memory. Cheap vs Opus; never the flagship.
            model=get_background_models()["quality"],
            setting_sources=["user", "project"],
            system_prompt={"type": "preset", "preset": "claude_code"},
            allowed_tools=["Read", "Edit", "Glob", "Grep", "Bash"],
            permission_mode="acceptEdits",
            max_turns=25,
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher="Bash",
                        hooks=[validate_bash_command],
                    )
                ]
            },
        )
    )

    print(
        f"[{now_local()}] Consolidation completed via {result.provider}:{result.model}"
        + (f" cost=${result.cost_usd:.4f}" if result.cost_usd else "")
    )
    if not test_mode:
        # Reentrant ledger lock — shared.file_lock here would deadlock
        # against the ledger mutations inside (per-handle OS locks).
        with ledger_file_lock(AMENDMENT_LEDGER_FILE):
            apply_results = process_amendment_output(
                result.text,
                ProposalLedger(AMENDMENT_LEDGER_FILE),
                MEMORY_DIR,
                default_source="memory_dream",
                apply_limit=AMENDMENT_APPLY_LIMIT,
                section_cap=AMENDMENT_SECTION_CAP,
                # Phase 5 owns `belief_candidate` blocks exclusively (evidence-gate
                # + regression floor + LLM judge + kill-switch + retry-budget). They
                # must never also be coerced into a regular AmendmentProposal here
                # and auto-applied through this module's weaker default policy.
                exclude_kinds=frozenset({"belief_candidate"}),
            )
        applied = [item for item in apply_results if item.status == "applied"]
        if applied:
            print(f"[{now_local()}] Auto-applied {len(applied)} dream amendment(s)")
    return result.text


# =============================================================================
# PHASE 4: PRUNE & REINDEX (LLM via run_with_runtime_lanes)
# =============================================================================


async def prune(orientation: OrientResult, test_mode: bool = False) -> str:
    """Prune stale entries and enforce limits on MEMORY.md. Provider-agnostic."""
    from claude_agent_sdk import HookMatcher

    from runtime.base import RuntimeRequest
    from runtime.capabilities import TOOL_REASONING
    from runtime.lane_router import run_with_runtime_lanes
    from shared import validate_bash_command

    # PRD-8 Phase 2 WS3: assemble MEMORY section via the extracted helper.
    # Header + content locked by parity tests in tests/test_memory_dream.py.
    memory_section = _assemble_prune_memory_section(MEMORY_DIR)
    amendment_section = _assemble_dream_amendment_section(source="memory_dream_prune")
    # Instruction #2 below references the line count separately; derive it
    # via the same shim so the count and the rendered body never drift.
    _payload = build_identity_payload(MEMORY_DIR, include=("MEMORY",))
    memory_lines = len(_payload.get("MEMORY", "").splitlines())

    dry_run_note = (
        "\n\nDRY RUN: Do NOT edit any files. Describe what you would change.\n"
        if test_mode
        else ""
    )

    prompt = f"""Memory dream pruning. Clean up and optimize MEMORY.md.
{dry_run_note}
{memory_section}
{amendment_section}

## Instructions

Today is {now_local().strftime('%Y-%m-%d')}.

1. **Propose stale-entry removals**: For completed work older than 30 days
   that has no ongoing relevance, propose a removal through the amendment ledger.
   Keep decisions and lessons even if old.

2. **Enforce 200-line limit**: If MEMORY.md exceeds 200 lines (currently {memory_lines}),
   propose cutting the oldest completed-work entries first. Preserve key decisions and lessons.

3. **Demote verbose entries**: If any entry is longer than 2 lines, condense it to 1-2 lines.
   Propose the concise replacement through the amendment ledger.

4. **Reorder sections**: If section order should change, propose the reorder through the amendment ledger.

5. **Verify pointers**: Check that all [[wikilink]] references in MEMORY.md point to files
   that actually exist in {MEMORY_DIR}. Propose broken-link removals.

Do not edit {MEMORY_FILE} directly.

If MEMORY.md is already clean and under 200 lines, respond with exactly: PRUNE_OK
"""

    result = await run_with_runtime_lanes(
        RuntimeRequest(
            prompt=prompt,
            cwd=PROJECT_ROOT,
            task_name="memory_dream_prune",
            capability=TOOL_REASONING,
            # QUALITY background tier (sonnet) — deep prune that rewrites
            # durable memory. Cheap vs Opus; never the flagship.
            model=get_background_models()["quality"],
            setting_sources=["user", "project"],
            system_prompt={"type": "preset", "preset": "claude_code"},
            allowed_tools=["Read", "Edit", "Glob", "Grep", "Bash"],
            permission_mode="acceptEdits",
            max_turns=15,
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher="Bash",
                        hooks=[validate_bash_command],
                    )
                ]
            },
        )
    )

    print(
        f"[{now_local()}] Pruning completed via {result.provider}:{result.model}"
        + (f" cost=${result.cost_usd:.4f}" if result.cost_usd else "")
    )
    if not test_mode:
        # Reentrant ledger lock — shared.file_lock here would deadlock
        # against the ledger mutations inside (per-handle OS locks).
        with ledger_file_lock(AMENDMENT_LEDGER_FILE):
            apply_results = process_amendment_output(
                result.text,
                ProposalLedger(AMENDMENT_LEDGER_FILE),
                MEMORY_DIR,
                default_source="memory_dream_prune",
                apply_limit=AMENDMENT_APPLY_LIMIT,
                section_cap=AMENDMENT_SECTION_CAP,
                # Security symmetry with consolidate (Kimi/independent gate F1
                # on PR #181): a belief_candidate block must NEVER be coerced
                # into a regular amendment and applied via the WEAK policy
                # (evidence_check=None -> no evidence gate/judge). Belief
                # candidates flow ONLY through propose_belief in Phase 5.
                exclude_kinds=frozenset({"belief_candidate"}),
            )
        applied = [item for item in apply_results if item.status == "applied"]
        if applied:
            print(f"[{now_local()}] Auto-applied {len(applied)} dream prune amendment(s)")
    return result.text


# =============================================================================
# PHASE 5: BELIEF EVOLUTION (identity rail — gated + throttled + retry-budgeted)
# =============================================================================


def _render_belief_receipt_line(receipt: dict[str, Any], *, lowercase: bool = False) -> str:
    """Render the Phase-5 receipt as one daily-log / vault-log line (#170).

    Returns "" for ``not_run`` (an early-return path that never reaches the
    success block anyway) so callers can skip appending. ``lowercase=True`` yields
    the vault-log bullet style ("beliefs: ...") vs the daily-log style ("Beliefs:").
    """
    result = receipt.get("result", "not_run")
    label = "beliefs" if lowercase else "Beliefs"
    skipped_corrupt = receipt.get("skipped_corrupt", 0)
    corrupt_suffix = f", {skipped_corrupt} corrupt-skipped" if skipped_corrupt else ""
    if result == "not_run":
        return ""
    if result == "EVOLVE_SILENT":
        return f"{label}: EVOLVE_SILENT (no new/retryable candidates){corrupt_suffix}"
    if result == "skipped_killswitch":
        return f"{label}: skipped (kill-switch disabled)"
    if result == "skipped_evolve_disabled":
        return f"{label}: skipped (EVOLVE_ENABLED=false)"
    if result == "failed":
        return f"{label}: phase failed (non-fatal): {receipt.get('error', '')}"
    if result == "ran":
        return (
            f"{label}: {receipt.get('proposed', 0)} proposed, "
            f"{receipt.get('adopted', 0)} adopted, "
            f"{receipt.get('rejected', 0)} rejected, "
            f"{receipt.get('error_retryable', 0)} retryable-error, "
            f"{receipt.get('error_terminal', 0)} terminal-error"
            f"{corrupt_suffix}"
        )
    return f"{label}: {result}"


async def _run_belief_evolution_phase(
    consolidation_result: str,
    test_mode: bool = False,
) -> dict[str, Any]:
    """Phase 5 (#170) — the identity rail, gated + throttled + retry-budgeted.

    Runs AFTER Phase 4, sourcing candidates from (a) the SAME Phase-3 consolidation
    LLM response (zero new LLM calls) and (b) the persistent retry queue of prior
    ``retryable`` decision artifacts. Each candidate routes through the UNCHANGED
    ``propose_belief`` chain (evidence-gate -> deterministic floor -> LLM judge ->
    amendment policy). Adoptions throttle to ``max_adoptions_per_night``.

    NEVER raises: a belief-evolution failure must not fail a dream cycle that
    already succeeded through Phase 4 (the ``receipt`` dict is built BEFORE the try
    body, so even a crash before any mutation returns a valid receipt). Gated by
    ``HOMIE_KILLSWITCH_BELIEF_AUTONOMY`` (ships enabled — unset != disabled); a
    disabled switch skips ONLY this phase, leaving the overall dream ``result``
    untouched (distinct from the Phase-3/4 ``llm`` kill-switch, which aborts the
    whole dream). Returns a receipt dict for the daily log / vault log / state file.

    Under ``test_mode`` the phase harvests (reads only) and reports what it WOULD
    propose as ``dry_run_preview``, then stops before ``propose_belief`` — which
    writes a decision artifact on every path, dry run included.
    """
    receipt: dict[str, Any] = {
        "result": "not_run",
        "candidates_seen": 0,
        "retried": 0,
        "proposed": 0,
        "adopted": 0,
        "rejected": 0,
        "error_retryable": 0,
        "error_terminal": 0,
        "skipped_corrupt": 0,
    }
    try:
        # Rule 3 — module-attribute lookup so tests can monkeypatch the switch.
        from security import kill_switches

        try:
            kill_switches.requireEnabled(
                "belief_autonomy", caller="memory_dream_belief_evolve"
            )
        except kill_switches.KillSwitchDisabled:
            # Operator intent — skip ONLY Phase 5; the dream itself still succeeds.
            receipt["result"] = "skipped_killswitch"
            return receipt

        from config import get_belief_evolve_settings
        from evolve.evolve_loop import (
            extract_belief_candidates,
            load_retryable_belief_candidates,
            propose_belief,
        )

        s = get_belief_evolve_settings()
        if not s.enabled:
            # A separate, independent gate from the kill switch — EVOLVE_ENABLED
            # covers the whole evolve rail (propose + propose-belief). Checked
            # before touching the retry queue/candidates so disablement is a
            # true no-op, not just an unreconciled zero-adds receipt.
            receipt["result"] = "skipped_evolve_disabled"
            return receipt
        retry_queue, skipped_corrupt = load_retryable_belief_candidates()
        receipt["skipped_corrupt"] = skipped_corrupt
        # Only FRESH LLM-authored candidates are capped; the retry queue is bounded
        # by max_attempts instead (a backlog must never be silently dropped).
        fresh = extract_belief_candidates(consolidation_result)[: s.max_candidates_per_night]
        # Retry candidates go FIRST — self-healing a stuck candidate outranks a
        # brand-new proposal for the adoption-throttle budget.
        combined = retry_queue + fresh
        receipt["candidates_seen"] = len(combined)
        receipt["retried"] = len(retry_queue)

        if not combined:
            # EVOLVE_SILENT — mirrors DREAM_SILENT: zero extra LLM calls, one line.
            receipt["result"] = "EVOLVE_SILENT"
            return receipt

        if test_mode:
            # --test = no file edits, and propose_belief writes a decision
            # artifact on EVERY path including its own dry-run preview (that
            # preview is deliberate — it belongs to the `evolve propose-belief
            # --dry-run` CLI, which is not bound by this file's contract). A
            # dream dry run reports what it WOULD propose and stops, the same
            # shape Phase 3 uses for the episode flip.
            print(
                f"[{now_local()}]   DRY RUN - would propose "
                f"{len(combined)} belief candidate(s) "
                f"({len(retry_queue)} retried, {len(fresh)} fresh)"
            )
            receipt["result"] = "dry_run_preview"
            return receipt

        adopted = 0
        for cand in combined:
            if adopted >= s.max_adoptions_per_night:
                # Throttle: remaining candidates wait for tomorrow (never judged
                # this run, so no attempts penalty — NOT counted as reject/error).
                break
            attempts = int(cand.pop("_attempts", 0))
            result = await propose_belief(
                cand, dry_run=test_mode, attempts=attempts
            )
            receipt["proposed"] += 1
            outcome = result.get("outcome", "")
            if outcome == "adopt":
                receipt["adopted"] += 1
                adopted += 1
            elif outcome == "reject":
                receipt["rejected"] += 1
            elif outcome == "error":
                if result.get("retryable"):
                    receipt["error_retryable"] += 1
                else:
                    receipt["error_terminal"] += 1
        receipt["result"] = "ran"
        return receipt
    except Exception as exc:  # noqa: BLE001 — Phase 5 never fails the dream cycle
        print(
            f"[{now_local()}]   WARNING: belief evolution phase failed "
            f"(non-fatal): {exc}"
        )
        receipt["result"] = "failed"
        receipt["error"] = str(exc)[:200]
        return receipt


# =============================================================================
# POST-STEPS (non-blocking)
# =============================================================================


def _run_entity_compilation() -> None:
    """Compile entities from recently updated memory files."""
    try:
        from entity_extractor import compile_single_log

        # Compile from MEMORY.md itself (it was just updated)
        report = compile_single_log(MEMORY_FILE, MEMORY_DIR)
        if report and (report.pages_created or report.pages_updated):
            print(
                f"[{now_local()}] Dream entity compilation: "
                f"+{len(report.pages_created)} created, ~{len(report.pages_updated)} updated"
            )
    except Exception as e:
        print(f"[{now_local()}] Entity compilation after dream failed (non-blocking): {e}")


def _run_reindex() -> None:
    """Reindex memory search database."""
    try:
        _chat_dir = Path(__file__).resolve().parent.parent / "chat"
        if str(_chat_dir) not in sys.path:
            sys.path.insert(0, str(_chat_dir))
        from recall_service import reindex_changed

        stats = reindex_changed(MEMORY_DIR)
        if stats["files_indexed"] > 0:
            print(f"[{now_local()}] Reindexed {stats['files_indexed']} memory files after dream")
    except Exception as e:
        print(f"[{now_local()}] Reindex after dream failed (non-blocking): {e}")


# =============================================================================
# MAIN DREAM FUNCTION
# =============================================================================


async def run_dream(
    test_mode: bool = False,
    force: bool = False,
    days: int = 7,
    post_weekly: bool = False,
    no_llm: bool = False,
) -> str | None:
    """Run dream consolidation cycle with concurrency guard.

    Returns:
        "DREAM_SILENT" if no signal found.
        "DREAM_NO_LLM" if ``no_llm`` stopped a signal-bearing run before Phase 3.
        Response text if consolidation ran.
        None if skipped (recency guard or lock).
    """
    if test_mode:
        # The lock is a physical write (a .lock sidecar plus its parent dirs) in
        # the profile tree, and --test promises none. A preview that mutates
        # nothing also needs no mutual exclusion, so it simply runs unlocked.
        return await _run_dream_inner(test_mode, force, days, post_weekly, no_llm)
    try:
        with file_lock(DREAM_STATE_FILE, timeout=5.0):
            result = await _run_dream_inner(test_mode, force, days, post_weekly, no_llm)
        try:
            from personas.learning import worker as learning_worker
            await learning_worker.wake_learning(test_mode=test_mode, max_stages=1)
        except Exception as exc:
            print(f"[{now_local()}] Learning queue wake failed (non-blocking): {type(exc).__name__}")
        return result
    except TimeoutError:
        print(f"[{now_local()}] Another dream cycle is already running, skipping")
        return None


async def _run_dream_inner(
    test_mode: bool = False,
    force: bool = False,
    days: int = 7,
    post_weekly: bool = False,
    no_llm: bool = False,
) -> str | None:
    """Inner dream cycle — all 4 phases."""
    print(f"[{now_local()}] Starting dream cycle (days={days}, test={test_mode}, force={force})...")

    # --test = no file edits. Every state/daily-log write in this function goes
    # through these two so a dry run cannot advance last_run (which would let
    # the recency guard swallow the next REAL dream) or leave a log line
    # claiming a cycle that never mutated anything.
    def _persist_state(payload: dict[str, Any]) -> None:
        if test_mode:
            return
        # Echo the parent's per-spawn nonce into every receipt this cycle
        # writes. One chokepoint, so no state-writing path can forget it and
        # look — correctly, from the parent's side — like a leftover file.
        spawn_id = os.environ.get(SPAWN_ID_ENV, "").strip()
        if spawn_id:
            payload["spawn_id"] = spawn_id
        save_state(payload, DREAM_STATE_FILE)

    def _log_daily(content: str, section: str = "Dream Cycle") -> None:
        if not test_mode:
            append_to_daily_log(content, section)

    # --- Recency guard ---
    if not force:
        state = load_state(DREAM_STATE_FILE)
        if "last_run" in state:
            # Allow immediate retry if the last run failed
            if state.get("result") == "failed":
                print(f"[{now_local()}] Last dream run failed, allowing retry")
            else:
                try:
                    last = datetime.fromisoformat(state["last_run"])
                    elapsed_h = (now_local() - last).total_seconds() / 3600
                    if elapsed_h < DREAM_MIN_INTERVAL_HOURS:
                        print(
                            f"[{now_local()}] Dream ran {elapsed_h:.1f}h ago, "
                            f"skipping (use --force to override)"
                        )
                        return None
                except (ValueError, TypeError):
                    pass  # Corrupted state — proceed

    # === PHASE 1: Orient ===
    print(f"[{now_local()}] Phase 1: Orient...")
    orientation = orient(days=days)
    print(
        f"[{now_local()}]   MEMORY.md: {orientation.memory_lines} lines, "
        f"{len(orientation.daily_logs)} daily logs, "
        f"{orientation.concepts_count} concept pages"
    )

    if not orientation.daily_logs:
        print(f"[{now_local()}] No daily logs found for last {days} days, skipping dream")
        # An honest receipt for an honest non-event. This path used to write
        # nothing at all, so a brand-new persona produced a clean exit and an
        # empty profile tree — which the fan-out could only read as "no proof
        # this ran". Saying so explicitly is what lets the parent record a real
        # outcome instead of guessing, and keeps a persona from being charged
        # for a night it cannot show a receipt for.
        state = load_state(DREAM_STATE_FILE)
        state["last_run"] = now_local().isoformat()
        state["days_scanned"] = days
        state["signal_found"] = False
        state["result"] = DREAM_SKIPPED
        state["phases_completed"] = ["orient"]
        state.pop("error", None)
        _persist_state(state)
        _log_daily(f"{DREAM_SKIPPED} - no logs for last {days} days")
        return None

    # === PHASE 2: Gather Signal ===
    print(f"[{now_local()}] Phase 2: Gather signal...")
    signal = gather_signal(orientation.daily_logs, days=days)
    print(
        f"[{now_local()}]   Scanned {signal.files_scanned} files — "
        f"score={signal.signal_score} (threshold={DREAM_SIGNAL_THRESHOLD}): "
        f"{len(signal.corrections)} corrections, "
        f"{len(signal.saves)} saves, "
        f"{len(signal.stalls)} stalls, "
        f"{len(signal.repeated_entities)} repeated entities, "
        f"{len(signal.episode_paths)} open episodes"
    )

    # === PHASE 2.5: Age working memory (always when live — maintenance, not
    # signal-gated, but still test_mode-gated like every other side-effecting
    # phase in this file) ===
    # Living Mind Phase 1: move stale bullets from WORKING.md active sections to
    # Archived (Cold). Insert-only — never deletes. Non-fatal on failure.
    # --test must mean "no file edits" (this file's own usage doc, and the
    # persona --child-test contract that promises "no vault edits") — a vault
    # mutation here would break that for both the main homie and any persona.
    if not test_mode:
        try:
            from living_memory import archive_stale_working_items  # noqa: WPS433

            _age_threshold = int(os.getenv("WORKING_MEMORY_AGE_DAYS", "7"))
            _archive_report = archive_stale_working_items(MEMORY_DIR, days=_age_threshold)
            if _archive_report.archived_count > 0:
                print(
                    f"[{now_local()}]   Archived {_archive_report.archived_count} stale "
                    f"working-memory items (>{_archive_report.days}d old) "
                    f"across {len(_archive_report.sections_touched)} sections"
                )
        except Exception as _wm_exc:  # noqa: BLE001
            # Dream cycle continues even if archiving fails.
            print(f"[{now_local()}]   WARNING: working memory archive failed: {_wm_exc}")

    # === PHASE 2.5b: Archive stale self-authored skill drafts (always when
    # live) ===
    # Skill-From-Experience loop (WS3): staged drafts that never recurred past
    # SKILL_STALE_DAYS are flipped to archived (+ one audit row each). This is
    # the production rail for skill_promotion.archive_stale() — same dream-prune
    # seam as working-memory archival. Lazy import via the chat-slice cognition
    # path (already on sys.path above). Fire-and-forget: a failure here NEVER
    # breaks the dream run. test_mode-gated for the same "--test = no file
    # edits" reason as Phase 2.5 above.
    if not test_mode:
        try:
            from cognition import skill_promotion  # noqa: WPS433

            _archived_skills = skill_promotion.archive_stale()
            if _archived_skills:
                print(
                    f"[{now_local()}]   Archived {len(_archived_skills)} stale skill "
                    f"draft(s): {', '.join(_archived_skills)}"
                )
        except Exception as _skill_exc:  # noqa: BLE001
            # Dream cycle continues even if skill archival fails.
            print(f"[{now_local()}]   WARNING: skill draft archive failed: {_skill_exc}")

    if not signal.found:
        print(f"[{now_local()}] No signal found — {DREAM_SILENT}")
        # Crash-safe: advance state even on silent
        state = load_state(DREAM_STATE_FILE)
        state["last_run"] = now_local().isoformat()
        state["days_scanned"] = days
        state["signal_found"] = False
        state["result"] = DREAM_SILENT
        state["phases_completed"] = ["orient", "gather"]
        state["signal_counts"] = {
            "corrections": 0,
            "saves": 0,
            "stalls": 0,
            "repeated_entities": 0,
        }
        _persist_state(state)
        _log_daily(
            f"DREAM_SILENT - scanned {signal.files_scanned} files, no consolidation signal"
        )
        return DREAM_SILENT

    if no_llm:
        # The free half of the cycle just proved everything a fan-out probe
        # needs: the boot-shim re-rooted, this profile's own daily logs were
        # found and scanned, and the signal scorer ran on them. Phases 3-5 are
        # the only ones that cost money, and a probe run across a whole roster
        # must not bill a real night's tokens to prove the plumbing.
        print(
            f"[{now_local()}] {DREAM_NO_LLM} — signal found "
            f"(score {signal.signal_score}, {signal.files_scanned} file(s) scanned) but "
            f"--no-llm stops before Phase 3; no LLM call, nothing written"
        )
        return DREAM_NO_LLM

    # --- Crash-safe: advance state BEFORE LLM phases ---
    state = load_state(DREAM_STATE_FILE)
    state["last_run"] = now_local().isoformat()
    state["days_scanned"] = days
    state["signal_found"] = True
    state["signal_counts"] = {
        "corrections": len(signal.corrections),
        "saves": len(signal.saves),
        "stalls": len(signal.stalls),
        "repeated_entities": len(signal.repeated_entities),
    }
    _persist_state(state)

    phases_completed = ["orient", "gather"]
    consolidation_result = ""
    prune_result = ""
    episodes_marked = 0
    belief_receipt = {"result": "not_run"}

    try:
        # === PHASE 3: Consolidate (LLM) ===
        print(f"[{now_local()}] Phase 3: Consolidate via LLM...")
        consolidation_result = await consolidate(
            signal, orientation, test_mode=test_mode, post_weekly=post_weekly
        )
        phases_completed.append("consolidate")

        # Living Mind Act 3 — deterministic episode flip. "Consolidated"
        # means "a successful dream Phase 3 reviewed it" (either LLM
        # outcome; reviewed-and-empty is still reviewed). Sits AFTER the
        # successful consolidate() return inside its OWN try/except so it
        # can NEVER reach the outer exception path: a consolidate() raise
        # leaves episodes open for the retry run; a flip raise is
        # warning-logged and the dream still reports success (R1 M1).
        if signal.episode_paths and not test_mode:
            try:
                from episodes import mark_episodes_consolidated

                episodes_marked = mark_episodes_consolidated(signal.episode_paths)
                if episodes_marked:
                    print(
                        f"[{now_local()}] Marked {episodes_marked} episode(s) consolidated"
                    )
            except Exception as e:
                # EpisodeFlipError carries the partial flip count (Rule 2:
                # those files physically say consolidated); any other raise
                # reports 0. Failed episodes stay open — next run re-feeds.
                episodes_marked = getattr(e, "flipped", 0)
                print(
                    f"[{now_local()}] WARNING: episode flip failed (non-fatal): {e}"
                )
        elif signal.episode_paths and test_mode:
            print(
                f"[{now_local()}] DRY RUN - would mark "
                f"{len(signal.episode_paths)} episode(s) consolidated"
            )

        # Re-read MEMORY.md line count after Phase 3 may have modified it
        if MEMORY_FILE.exists():
            orientation.memory_lines = len(
                MEMORY_FILE.read_text(encoding="utf-8").splitlines()
            )

        # === PHASE 4: Prune (LLM) ===
        if orientation.memory_lines > 150 or not test_mode:
            print(f"[{now_local()}] Phase 4: Prune & reindex via LLM...")
            prune_result = await prune(orientation, test_mode=test_mode)
            phases_completed.append("prune")
        else:
            print(f"[{now_local()}] Phase 4: Skipped (MEMORY.md under 150 lines in test mode)")

        # === PHASE 5: Belief Evolution (identity rail — #170) ===
        # Runs inside the outer try (so it's reached only on a successful Phase 3),
        # but its OWN internal try/except means it NEVER propagates to the outer
        # except below — a belief-evolution failure must not flip the dream to
        # "failed" after Phases 3/4 already succeeded.
        print(f"[{now_local()}] Phase 5: Belief evolution...")
        belief_receipt = await _run_belief_evolution_phase(
            consolidation_result, test_mode=test_mode
        )
        phases_completed.append("belief_evolve")
        print(f"[{now_local()}]   {belief_receipt}")

    except Exception as exc:
        # PRD-8 Phase 7a WS4 R2 NM2 — kill-switch is operator intent, not a
        # failure. Save state with result="skipped_killswitch" (NOT "failed")
        # so the recency guard does NOT force immediate retry — operator
        # deliberately disabled the feature. Late-bind import (defensive).
        try:
            from security.kill_switches import KillSwitchDisabled
        except ImportError:
            KillSwitchDisabled = ()  # type: ignore[assignment,misc]
        if isinstance(exc, KillSwitchDisabled):  # type: ignore[arg-type]
            switch_name = getattr(exc, "switch_name", "unknown")
            state["result"] = "skipped_killswitch"  # NOT "failed"
            state["phases_completed"] = phases_completed
            state["killswitch"] = switch_name
            _persist_state(state)
            print(f"[{now_local()}] Dream skipped: kill-switch '{switch_name}' disabled")
            return  # CLEAN exit — recency guard treats this as a successful skip

        # LLM failure — mark state as failed so recency guard allows retry
        state["result"] = "failed"
        state["phases_completed"] = phases_completed
        state["error"] = str(exc)[:200]
        _persist_state(state)
        print(f"[{now_local()}] Dream LLM phase failed: {exc}")
        raise

    # === Post-steps (non-blocking) ===
    if not test_mode:
        _run_entity_compilation()
        _run_reindex()

    # Final state update — success
    state["phases_completed"] = phases_completed
    state["result"] = "consolidated"
    state["belief_evolve"] = belief_receipt  # #170 — machine-readable Phase-5 receipt
    state.pop("error", None)  # Clear any previous error
    _persist_state(state)

    # Log summary
    summary_parts = [
        f"Dream cycle complete — {signal.files_scanned} files scanned",
        f"{len(signal.corrections)} corrections, {len(signal.saves)} saves, "
        f"{len(signal.stalls)} stalls, {len(signal.repeated_entities)} repeated entities",
    ]
    if signal.episode_paths:
        summary_parts.append(
            f"episodes: {len(signal.episode_paths)} reviewed, "
            f"{episodes_marked} consolidated"
        )
    if "CONSOLIDATION_OK" in consolidation_result:
        summary_parts.append("Consolidation: nothing to merge")
    else:
        summary_parts.append("Consolidation: merged signal into memory")
    if "PRUNE_OK" in prune_result:
        summary_parts.append("Pruning: MEMORY.md already clean")
    elif prune_result:
        summary_parts.append("Pruning: cleaned MEMORY.md")

    # #170 — belief-evolution (Phase 5) receipt line. Rendered for every result
    # except "not_run" (an early-return path that never reaches here anyway).
    belief_line = _render_belief_receipt_line(belief_receipt)
    if belief_line:
        summary_parts.append(belief_line)

    _log_daily("\n".join(summary_parts))
    print(f"[{now_local()}] Dream cycle finished.")

    # --- Vault log append (chronological wiki timeline, non-silent only) ---
    if not test_mode:
        try:
            from entity_extractor import append_vault_log

            bullets = [
                f"signal score: {signal.signal_score}",
                f"corrections: {len(signal.corrections)}, saves: {len(signal.saves)}, "
                f"stalls: {len(signal.stalls)}, repeated: {len(signal.repeated_entities)}",
            ]
            if signal.episode_paths:
                bullets.append(
                    f"episodes: {len(signal.episode_paths)} reviewed, "
                    f"{episodes_marked} consolidated"
                )
            if "CONSOLIDATION_OK" not in consolidation_result:
                bullets.append("consolidation: merged signal into memory")
            if "PRUNE_OK" not in (prune_result or ""):
                bullets.append("pruning: cleaned MEMORY.md")
            _belief_bullet = _render_belief_receipt_line(belief_receipt, lowercase=True)
            if _belief_bullet:
                bullets.append(_belief_bullet)

            append_vault_log(
                MEMORY_DIR,
                "dream",
                f"Dream cycle ({days} day scan)",
                bullets=bullets,
            )
        except Exception as exc:
            print(f"[{now_local()}] Vault log append failed (non-blocking): {exc}")

    return consolidation_result


# =============================================================================
# ENTRY POINT
# =============================================================================


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Memory dream consolidation cycle")
    parser.add_argument("--test", action="store_true", help="Dry run mode (no file edits)")
    parser.add_argument("--json", action="store_true", help="Emit validation probe JSON")
    parser.add_argument("--vault", type=Path, default=None, help="Override vault root for validation probe")
    parser.add_argument("--force", action="store_true", help="Skip recency guard")
    parser.add_argument("--days", type=int, default=7, help="Days of logs to scan (default: 7)")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="With --test: run the free phases and stop before Phase 3 (zero LLM calls)",
    )
    args = parser.parse_args()

    if args.no_llm and not args.test:
        # Without --test the free phases still advance last_run on their way
        # past, so a bare --no-llm run would spend the night's recency budget
        # on a cycle that never consolidated anything. The flag is a probe
        # modifier, not a cheap dream.
        parser.error("--no-llm requires --test (it is a probe, not a dream)")

    if args.json:
        from cognitive_loop_test_harness import build_scheduled_entrypoint_report

        report = build_scheduled_entrypoint_report(
            "memory_dream",
            args.vault or MEMORY_DIR,
            test_mode=args.test,
        )
        print(json.dumps(report, indent=2))
        return

    if not args.test:
        # Directory creation is a file edit too — a --test probe against a
        # persona profile must not leave a scaffolded tree behind.
        ensure_directories()

    if args.test:
        print("Running in TEST MODE (dry run, no file edits)")
        print(f"Project root: {PROJECT_ROOT}")
        print(f"Scanning last {args.days} day(s) of logs")

    result = asyncio.run(
        run_dream(
            test_mode=args.test,
            force=args.force,
            days=args.days,
            no_llm=args.no_llm,
        )
    )

    if result == DREAM_NO_LLM:
        print(f"\nDream result: {DREAM_NO_LLM} (signal found, stopped before Phase 3)")
    elif result == DREAM_SILENT:
        print(f"\nDream result: {DREAM_SILENT} (no signal, no LLM calls)")
    elif result:
        try:
            print(f"\nDream result:\n{result[:500]}")
        except UnicodeEncodeError:
            print(f"\nDream result:\n{result[:500].encode('ascii', 'replace').decode()}")
    else:
        print("\nDream complete: skipped or already running")


if __name__ == "__main__":
    main()
