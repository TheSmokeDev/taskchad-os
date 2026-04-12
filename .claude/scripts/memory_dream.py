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
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from config import (
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
    get_today_log_path,
    now_local,
)
from shared import append_to_daily_log, file_lock, load_state, save_state

# =============================================================================
# CONSTANTS
# =============================================================================

DREAM_SILENT = "DREAM_SILENT"
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


def gather_signal(daily_logs: list[Path], days: int = 7) -> SignalResult:
    """Scan logs for consolidation-worthy signal. No LLM call.

    Greps daily logs and session flush files for corrections, saves,
    stalls, and repeated entities. Returns a signal digest.
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

    # Scan session flush files (filtered by mtime — only recent ones)
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

    # Load current file contents for context
    memory_content = MEMORY_FILE.read_text(encoding="utf-8") if MEMORY_FILE.exists() else ""
    self_content = SELF_FILE.read_text(encoding="utf-8") if SELF_FILE.exists() else ""
    goals_content = GOALS_FILE.read_text(encoding="utf-8") if GOALS_FILE.exists() else ""

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

    prompt = f"""Memory dream consolidation. Merge recent signal into long-term memory.
{dry_run_note}
## Signal Digest (from last {len(signal.corrections) + len(signal.saves) + len(signal.stalls)} items)

{signal.digest}

## Current MEMORY.md ({orientation.memory_lines} lines)

{memory_content}

## Current SELF.md

{self_content}

## Current GOALS.md (read-only — reference only, do NOT edit)

{goals_content}
{post_weekly_note}
## Instructions

Today is {today_str}. Consolidate the signal above into memory:

1. **Merge into MEMORY.md** ({MEMORY_FILE}):
   - Add new lessons, decisions, or important context from the signal
   - Do NOT duplicate items already present
   - Convert any relative dates ("yesterday", "last week") to absolute dates
   - Keep entries concise (1-2 lines each)

2. **Update SELF.md** ({SELF_FILE}) ONLY if:
   - 2+ correction signals point to a recurring failure mode
   - A stall reveals a new area of low confidence
   - A repeated entity shows a new capability or pattern
   - Skip if no strong evidence

3. **Resolve contradictions**: If a new signal contradicts an existing MEMORY.md entry,
   update the entry to reflect the latest truth. Delete the old version.

4. Log a brief summary of changes to today's daily log ({get_today_log_path()}).

If nothing in the signal warrants changes, respond with exactly: CONSOLIDATION_OK
"""

    result = await run_with_runtime_lanes(
        RuntimeRequest(
            prompt=prompt,
            cwd=PROJECT_ROOT,
            task_name="memory_dream_consolidate",
            capability=TOOL_REASONING,
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

    memory_content = MEMORY_FILE.read_text(encoding="utf-8") if MEMORY_FILE.exists() else ""
    memory_lines = len(memory_content.splitlines())

    dry_run_note = (
        "\n\nDRY RUN: Do NOT edit any files. Describe what you would change.\n"
        if test_mode
        else ""
    )

    prompt = f"""Memory dream pruning. Clean up and optimize MEMORY.md.
{dry_run_note}
## Current MEMORY.md ({memory_lines} lines)

{memory_content}

## Instructions

Today is {now_local().strftime('%Y-%m-%d')}.

1. **Remove stale entries**: Delete entries about completed work older than 30 days
   that have no ongoing relevance. Keep decisions and lessons even if old.

2. **Enforce 200-line limit**: If MEMORY.md exceeds 200 lines (currently {memory_lines}),
   cut the oldest completed-work entries first. Preserve key decisions and lessons.

3. **Demote verbose entries**: If any entry is longer than 2 lines, condense it to 1-2 lines.
   Move detail to the linked topic file if one exists.

4. **Reorder sections**: Active projects first, then recent decisions, then reference.

5. **Verify pointers**: Check that all [[wikilink]] references in MEMORY.md point to files
   that actually exist in {MEMORY_DIR}. Remove broken links.

Use the Edit tool to modify {MEMORY_FILE}.

If MEMORY.md is already clean and under 200 lines, respond with exactly: PRUNE_OK
"""

    result = await run_with_runtime_lanes(
        RuntimeRequest(
            prompt=prompt,
            cwd=PROJECT_ROOT,
            task_name="memory_dream_prune",
            capability=TOOL_REASONING,
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
    return result.text


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
) -> str | None:
    """Run dream consolidation cycle with concurrency guard.

    Returns:
        "DREAM_SILENT" if no signal found.
        Response text if consolidation ran.
        None if skipped (recency guard or lock).
    """
    try:
        with file_lock(DREAM_STATE_FILE, timeout=5.0):
            return await _run_dream_inner(test_mode, force, days, post_weekly)
    except TimeoutError:
        print(f"[{now_local()}] Another dream cycle is already running, skipping")
        return None


async def _run_dream_inner(
    test_mode: bool = False,
    force: bool = False,
    days: int = 7,
    post_weekly: bool = False,
) -> str | None:
    """Inner dream cycle — all 4 phases."""
    print(f"[{now_local()}] Starting dream cycle (days={days}, test={test_mode}, force={force})...")

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
        append_to_daily_log(f"DREAM_SKIPPED - no logs for last {days} days", "Dream Cycle")
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
        f"{len(signal.repeated_entities)} repeated entities"
    )

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
        save_state(state, DREAM_STATE_FILE)
        append_to_daily_log(
            f"DREAM_SILENT - scanned {signal.files_scanned} files, no consolidation signal",
            "Dream Cycle",
        )
        return DREAM_SILENT

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
    save_state(state, DREAM_STATE_FILE)

    phases_completed = ["orient", "gather"]
    consolidation_result = ""
    prune_result = ""

    try:
        # === PHASE 3: Consolidate (LLM) ===
        print(f"[{now_local()}] Phase 3: Consolidate via LLM...")
        consolidation_result = await consolidate(
            signal, orientation, test_mode=test_mode, post_weekly=post_weekly
        )
        phases_completed.append("consolidate")

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

    except Exception as exc:
        # LLM failure — mark state as failed so recency guard allows retry
        state["result"] = "failed"
        state["phases_completed"] = phases_completed
        state["error"] = str(exc)[:200]
        save_state(state, DREAM_STATE_FILE)
        print(f"[{now_local()}] Dream LLM phase failed: {exc}")
        raise

    # === Post-steps (non-blocking) ===
    if not test_mode:
        _run_entity_compilation()
        _run_reindex()

    # Final state update — success
    state["phases_completed"] = phases_completed
    state["result"] = "consolidated"
    state.pop("error", None)  # Clear any previous error
    save_state(state, DREAM_STATE_FILE)

    # Log summary
    summary_parts = [
        f"Dream cycle complete — {signal.files_scanned} files scanned",
        f"{len(signal.corrections)} corrections, {len(signal.saves)} saves, "
        f"{len(signal.stalls)} stalls, {len(signal.repeated_entities)} repeated entities",
    ]
    if "CONSOLIDATION_OK" in consolidation_result:
        summary_parts.append("Consolidation: nothing to merge")
    else:
        summary_parts.append("Consolidation: merged signal into memory")
    if "PRUNE_OK" in prune_result:
        summary_parts.append("Pruning: MEMORY.md already clean")
    elif prune_result:
        summary_parts.append("Pruning: cleaned MEMORY.md")

    append_to_daily_log("\n".join(summary_parts), "Dream Cycle")
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
            if "CONSOLIDATION_OK" not in consolidation_result:
                bullets.append("consolidation: merged signal into memory")
            if "PRUNE_OK" not in (prune_result or ""):
                bullets.append("pruning: cleaned MEMORY.md")

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
    ensure_directories()

    parser = argparse.ArgumentParser(description="Memory dream consolidation cycle")
    parser.add_argument("--test", action="store_true", help="Dry run mode (no file edits)")
    parser.add_argument("--force", action="store_true", help="Skip recency guard")
    parser.add_argument("--days", type=int, default=7, help="Days of logs to scan (default: 7)")
    args = parser.parse_args()

    if args.test:
        print("Running in TEST MODE (dry run, no file edits)")
        print(f"Project root: {PROJECT_ROOT}")
        print(f"Scanning last {args.days} day(s) of logs")

    result = asyncio.run(run_dream(test_mode=args.test, force=args.force, days=args.days))

    if result == DREAM_SILENT:
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
