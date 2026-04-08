"""
Weekly Synthesis Script for The Homie

Reviews the last 7 days of daily logs and creates a weekly summary in
vault/memory/weekly/YYYY-WNN.md. Also updates MEMORY.md and GOALS.md
status fields. Runs Sunday 8 PM via OS scheduler.

Usage:
    uv run python memory_weekly.py              # Run weekly synthesis
    uv run python memory_weekly.py --test       # Dry run (no file edits)
    uv run python memory_weekly.py --days 14    # Two-week lookback
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from config import (
    DAILY_DIR,
    GOALS_FILE,
    MEMORY_DIR,
    MEMORY_FILE,
    PROJECT_ROOT,
    SELF_FILE,
    SOUL_FILE,
    USER_FILE,
    WEEKLY_DIR,
    WEEKLY_STATE_FILE,
    ensure_directories,
    get_today_log_path,
    now_local,
)
from runtime.base import RuntimeRequest
from runtime.capabilities import TOOL_REASONING
from runtime.registry import run_with_fallback
from shared import append_to_daily_log, file_lock, load_state, save_state, validate_bash_command

# =============================================================================
# LOG HELPERS
# =============================================================================

MAX_LOG_CHARS = 60_000  # Higher than daily reflection — covers a full week


def get_weekly_logs(days: int = 7) -> list[tuple[str, str]]:
    """Read the last N days of daily logs.

    Returns list of (date_str, content) tuples, most recent first.
    """
    logs: list[tuple[str, str]] = []
    today = now_local().date()

    for i in range(1, days + 1):
        target_date = today - timedelta(days=i)
        date_str = target_date.strftime("%Y-%m-%d")
        log_path = DAILY_DIR / f"{date_str}.md"

        if log_path.exists():
            content = log_path.read_text(encoding="utf-8")
            if len(content) > MAX_LOG_CHARS // days:
                content = "... (truncated)\n\n" + content[-(MAX_LOG_CHARS // days) :]
            logs.append((date_str, content))

    return logs


def load_file_safe(path) -> str:
    """Read a file, returning empty string if it doesn't exist."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def load_self_file() -> str:
    """Read current SELF.md content."""
    return load_file_safe(SELF_FILE)


def get_previous_weekly(n: int = 1) -> str:
    """Read the most recent N weekly review files for continuity."""
    if not WEEKLY_DIR.exists():
        return ""

    files = sorted(WEEKLY_DIR.glob("*.md"), reverse=True)
    parts: list[str] = []
    for f in files[:n]:
        content = f.read_text(encoding="utf-8")
        if len(content) > 3000:
            content = content[:3000] + "\n... (truncated)"
        parts.append(f"### {f.stem}\n\n{content}")

    return "\n\n---\n\n".join(parts)


# =============================================================================
# MAIN WEEKLY SYNTHESIS
# =============================================================================


async def run_weekly(test_mode: bool = False, days: int = 7) -> str | None:
    """Run weekly synthesis with concurrency guard."""
    try:
        with file_lock(WEEKLY_STATE_FILE, timeout=5.0):
            return await _run_weekly_inner(test_mode, days)
    except TimeoutError:
        print(f"[{now_local()}] Another weekly synthesis is already running, skipping")
        return None


async def _run_weekly_inner(test_mode: bool = False, days: int = 7) -> str | None:
    """Run weekly synthesis using Agent SDK.

    Reviews recent daily logs and creates a weekly summary file,
    updates MEMORY.md, and updates GOALS.md status fields.

    Args:
        test_mode: If True, run in dry-run mode (no file edits).
        days: Number of days of logs to review (default: 7).

    Returns:
        Response summary, or None if WEEKLY_OK.
    """
    from claude_agent_sdk import HookMatcher

    print(f"[{now_local()}] Running weekly synthesis (days={days}, test={test_mode})...")

    # Load recent logs
    logs = get_weekly_logs(days=days)
    if not logs:
        msg = f"No daily logs found for the last {days} day(s), skipping weekly synthesis"
        print(f"[{now_local()}] {msg}")
        append_to_daily_log(f"WEEKLY_SKIPPED - {msg}", "Weekly Synthesis")
        return None

    # Build log context
    log_sections: list[str] = []
    for date_str, content in logs:
        log_sections.append(f"### Daily Log: {date_str}\n\n{content}")
    log_context = "\n\n---\n\n".join(log_sections)

    # Proactive recall — search memory for context related to this week's logs
    recalled_section = ""
    try:
        _chat_dir = Path(__file__).resolve().parent.parent / "chat"
        if str(_chat_dir) not in sys.path:
            sys.path.insert(0, str(_chat_dir))
        from recall_service import recall as recall_fn

        from config import RECALL_BACKGROUND_MAX_CHARS, RECALL_BACKGROUND_MAX_RESULTS

        log_summary = log_context[:300] if log_context else ""
        if log_summary:
            recall_resp = await recall_fn(
                query=log_summary,
                memory_dir=MEMORY_DIR,
                caller="weekly",
                max_results=RECALL_BACKGROUND_MAX_RESULTS,
            )
            if recall_resp.formatted_text:
                recalled_section = (
                    "\n\n## Recalled Context (from memory search)\n\n"
                    "Related content found in memory. Check for duplicates.\n\n"
                    + recall_resp.formatted_text[:RECALL_BACKGROUND_MAX_CHARS]
                )
                print(f"[{now_local()}] Recalled {len(recalled_section)} chars for weekly")
    except Exception as e:
        print(f"[{now_local()}] Recall for weekly failed (non-blocking): {e}")

    # Load current files
    current_memory = load_file_safe(MEMORY_FILE)
    current_goals = load_file_safe(GOALS_FILE)
    current_soul = load_file_safe(SOUL_FILE)
    current_user = load_file_safe(USER_FILE)
    current_self = load_self_file()
    previous_weekly = get_previous_weekly(n=1)

    # Determine the week number for the output file
    today = now_local()
    iso_year, iso_week, _ = today.isocalendar()
    weekly_filename = f"{iso_year}-W{iso_week:02d}.md"
    weekly_path = WEEKLY_DIR / weekly_filename

    dry_run_note = (
        "\n\nDRY RUN: Do NOT edit any files. Just describe what you would change.\n"
        if test_mode
        else ""
    )

    synthesis_prompt = f"""Weekly memory synthesis. Review the past {days} days of daily logs \
and produce a weekly summary.
{dry_run_note}
## Current MEMORY.md

{current_memory}

## Current GOALS.md

{current_goals}

## Current USER.md

{current_user}

## Current SOUL.md

{current_soul}

## Current SELF.md

{current_self}

## Previous Weekly Review (for continuity)

{previous_weekly or "(No previous weekly review found)"}

## Daily Logs ({days} days)

{log_context}
{recalled_section}
## Instructions

Perform these steps:

### 1. Create Weekly Summary
Create the file `{weekly_path}` with these sections:
- **What Moved Forward** — projects, features, or decisions that progressed
- **What Stalled** — things that were blocked, delayed, or abandoned
- **Recurring Patterns** — habits, workflows, or issues that appeared multiple times
- **Key Decisions** — important choices made during the week
- **Lessons Learned** — new insights or mistakes to remember
- **Goals Progress** — how each goal in GOALS.md progressed this week

### 2. Update MEMORY.md ({MEMORY_FILE})
If daily reflection missed any important items, add them now.
Remove outdated entries that are no longer relevant.

### 3. Update GOALS.md ({GOALS_FILE})
Update the **Status** field for each goal based on this week's progress.
Keep status updates concise (1-2 sentences).
If a goal needs new key metrics or active projects, update those too.

### 4. Log Summary
Append a brief summary (2-3 sentences) to today's daily log ({get_today_log_path()}).

### 5. Update SELF.md ({SELF_FILE})
Distill this week's Lessons Learned into the four sections.
Only update a section if there is clear, new evidence. Do NOT duplicate existing entries.

- **Capabilities** — A new tool, method, or integration confirmed to work this week
- **Patterns** — A recurring successful approach that appeared 2+ times
- **Failure Modes** — A mistake or struggle from Lessons Learned or What Stalled
- **Confidence Notes** — An assumption corrected, or a recurring area of uncertainty
- **System Observations** — A meta-observation about the framework's own improvement process (e.g., a gap discovered and fixed, a pipeline that proved its value, a feedback loop that worked or failed)

1-2 sentences per entry. If nothing new, skip this step entirely.

**Rules:**
- Use the Edit tool to update existing files, Write tool only for the new weekly file
- Do NOT duplicate items already present in MEMORY.md
- Keep entries concise and actionable
- Reference specific dates and events from the logs
- If nothing meaningful happened this week, respond with exactly: WEEKLY_OK
"""

    try:
        result = await run_with_fallback(
            RuntimeRequest(
                prompt=synthesis_prompt,
                cwd=PROJECT_ROOT,
                task_name="memory_weekly",
                capability=TOOL_REASONING,
                setting_sources=["user", "project"],
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=[
                    "Read",
                    "Write",
                    "Edit",
                    "Glob",
                    "Grep",
                    "Bash",
                ],
                permission_mode="acceptEdits",
                max_turns=30,
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
        response_text = result.text
        print(
            f"[{now_local()}] Weekly synthesis completed via {result.provider}:{result.model}"
            + (f" cost=${result.cost_usd:.4f}" if result.cost_usd else "")
        )

    except Exception as e:
        print(f"[{now_local()}] Weekly synthesis error: {e}")
        append_to_daily_log(f"**ERROR**: Weekly synthesis failed - {e}", "Weekly Synthesis")
        return None

    # --- Move 5a: Emergent connections ---
    try:
        import sys as _sys
        _chat_dir = PROJECT_ROOT / ".claude" / "chat"
        if str(_chat_dir) not in _sys.path:
            _sys.path.insert(0, str(_chat_dir))
        from cognition.connections import find_emergent_connections

        from config import MEMORY_DIR

        connections = await find_emergent_connections(
            MEMORY_DIR, similarity_threshold=0.75, max_results=10,
        )
        if connections:
            conn_text = "\n## Suggested Connections\n\n"
            for c in connections:
                conn_text += (
                    f"- **{c.note_a}** ↔ **{c.note_b}** "
                    f"(similarity: {c.similarity:.2f})\n"
                )
            # Append to weekly file if it exists
            if weekly_path.exists() and not test_mode:
                existing = weekly_path.read_text(encoding="utf-8")
                weekly_path.write_text(
                    existing + "\n" + conn_text, encoding="utf-8"
                )
            print(f"[{now_local()}] Found {len(connections)} emergent connections")
            append_to_daily_log(
                f"Found {len(connections)} emergent connections between vault notes",
                "Weekly Synthesis",
            )
    except ImportError:
        pass
    except Exception as e:
        print(f"[{now_local()}] Emergent connections error (non-blocking): {e}")

    # Update state
    state = load_state(WEEKLY_STATE_FILE)
    state["last_run"] = now_local().isoformat()
    state["days_reviewed"] = days
    state["logs_found"] = len(logs)
    state["week"] = weekly_filename
    state["result"] = "WEEKLY_OK" if "WEEKLY_OK" in response_text else "synthesized"
    save_state(state, WEEKLY_STATE_FILE)

    response_text = response_text.strip()

    if "WEEKLY_OK" in response_text:
        append_to_daily_log("WEEKLY_OK - Nothing meaningful to synthesize", "Weekly Synthesis")
        print(f"[{now_local()}] Weekly OK - nothing to synthesize")
    else:
        append_to_daily_log(
            f"Weekly synthesis complete — created {weekly_filename}", "Weekly Synthesis"
        )

        if test_mode:
            print(f"[{now_local()}] DRY RUN - would have created:\n{response_text[:500]}")
        else:
            print(f"[{now_local()}] Weekly synthesis created {weekly_filename}")

    # Reindex AFTER all daily log appends + state saves — catches everything
    try:
        _chat_dir_ri = Path(__file__).resolve().parent.parent / "chat"
        if str(_chat_dir_ri) not in sys.path:
            sys.path.insert(0, str(_chat_dir_ri))
        from recall_service import reindex_changed

        stats = reindex_changed(MEMORY_DIR)
        if stats["files_indexed"] > 0:
            print(f"[{now_local()}] Reindexed {stats['files_indexed']} memory files after weekly")
    except Exception as e:
        print(f"[{now_local()}] Reindex after weekly failed (non-blocking): {e}")

    # Entity compilation: compile concepts from the new weekly note
    if not test_mode and "WEEKLY_OK" not in response_text:
        try:
            from entity_extractor import compile_single_log

            weekly_note = WEEKLY_DIR / weekly_filename
            report = compile_single_log(weekly_note, MEMORY_DIR)
            if report and (report.pages_created or report.pages_updated):
                print(
                    f"[{now_local()}] Compiled entities from {weekly_filename}: "
                    f"+{len(report.pages_created)} created, ~{len(report.pages_updated)} updated"
                )
        except Exception as e:
            print(f"[{now_local()}] Entity compilation after weekly failed (non-blocking): {e}")

    # --- Dream consolidation post-step ---
    if not test_mode:
        try:
            from memory_dream import run_dream

            dream_result = await run_dream(test_mode=False, force=True, days=days, post_weekly=True)
            if dream_result and dream_result != "DREAM_SILENT":
                print(f"[{now_local()}] Dream consolidation completed post-weekly")
                append_to_daily_log("Dream consolidation ran as weekly post-step", "Weekly Synthesis")
            elif dream_result == "DREAM_SILENT":
                print(f"[{now_local()}] Dream post-weekly: no signal (SILENT)")
        except Exception as e:
            print(f"[{now_local()}] Dream post-weekly failed (non-blocking): {e}")

    # --- Hermes Scout post-step ---
    if not test_mode:
        try:
            from example_scout import run_example_scout

            scout_result = await run_example_scout(test_mode=False, days=days)
            if scout_result and scout_result != "HERMES_SILENT":
                print(f"[{now_local()}] Hermes Scout completed post-weekly")
                append_to_daily_log("Hermes Scout ran as weekly post-step", "Weekly Synthesis")
            elif scout_result == "HERMES_SILENT":
                print(f"[{now_local()}] Hermes Scout: no upstream activity (SILENT)")
        except Exception as exc:
            print(f"[{now_local()}] Hermes Scout post-weekly failed (non-blocking): {exc}")

    if "WEEKLY_OK" in response_text:
        return None
    return response_text


# =============================================================================
# ENTRY POINT
# =============================================================================


def main() -> None:
    """Main entry point."""
    ensure_directories()

    parser = argparse.ArgumentParser(description="Weekly memory synthesis")
    parser.add_argument("--test", action="store_true", help="Dry run mode")
    parser.add_argument("--days", type=int, default=7, help="Days of logs to review (default: 7)")
    args = parser.parse_args()

    if args.test:
        print("Running in TEST MODE (dry run, no file edits)")
        print(f"Project root: {PROJECT_ROOT}")
        print(f"Reviewing last {args.days} day(s) of logs")

    result = asyncio.run(run_weekly(test_mode=args.test, days=args.days))

    if result:
        try:
            print(f"\nWeekly synthesis result:\n{result[:500]}")
        except UnicodeEncodeError:
            print(f"\nWeekly synthesis result:\n{result[:500].encode('ascii', 'replace').decode()}")
    else:
        print("\nWeekly synthesis complete: OK or skipped")


if __name__ == "__main__":
    main()
