"""
One-off backfill: distill Lessons Learned from weekly summaries W10-W13 into SELF.md.

Reads 4 weeks of weekly summaries, extracts their "Lessons Learned" sections,
and uses the AI runtime to distill them into SELF.md's four sections
(Capabilities, Patterns, Failure Modes, Confidence Notes).

Usage:
    cd .claude/scripts && uv run python seed_self_model.py              # Run
    cd .claude/scripts && uv run python seed_self_model.py --dry-run    # Print only
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from config import (
    MEMORY_DIR,
    PROJECT_ROOT,
    SELF_FILE,
    WEEKLY_DIR,
    ensure_directories,
    now_local,
)
from runtime.base import RuntimeRequest
from runtime.capabilities import TEXT_REASONING
from runtime.registry import run_with_fallback

# Weeks to backfill
WEEKLY_FILES = [
    WEEKLY_DIR / "2026-W10.md",
    WEEKLY_DIR / "2026-W11.md",
    WEEKLY_DIR / "2026-W12.md",
    WEEKLY_DIR / "2026-W13.md",
]

SELF_MD_SCAFFOLD = """\
# Self-Model

What I know about how I operate — built from evidence, not assumption.

## Capabilities
<!-- Tools, techniques, and approaches confirmed to work -->

## Patterns
<!-- Recurring successful behaviors -->

## Failure Modes
<!-- Mistakes that have recurred -->

## Confidence Notes
<!-- Assumptions corrected or known uncertain areas -->
"""


def extract_lessons(content: str, filename: str) -> str | None:
    """Extract the 'Lessons Learned' section from a weekly summary."""
    pattern = r"(## (?:Lessons Learned|Key Lessons|Lessons))\s*\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return f"### {filename}\n\n{match.group(2).strip()}"
    return None


def gather_lessons() -> tuple[str, list[str], list[str]]:
    """Read weekly files and extract lessons. Returns (combined_text, found, skipped)."""
    sections: list[str] = []
    found: list[str] = []
    skipped: list[str] = []

    for path in WEEKLY_FILES:
        name = path.name
        if not path.exists():
            print(f"  SKIP {name} (not found)")
            skipped.append(name)
            continue

        content = path.read_text(encoding="utf-8")
        lessons = extract_lessons(content, name)
        if lessons:
            sections.append(lessons)
            found.append(name)
            print(f"  FOUND lessons in {name}")
        else:
            print(f"  SKIP {name} (no lessons section)")
            skipped.append(name)

    return "\n\n---\n\n".join(sections), found, skipped


async def seed_self_model(dry_run: bool = False) -> str | None:
    """Main backfill: extract lessons from weeklies and distill into SELF.md."""
    print(f"[{now_local()}] Seed self-model — scanning weekly files...")

    lessons_text, found, skipped = gather_lessons()

    if not found:
        print("No lessons found in any weekly file. Nothing to do.")
        return None

    # Ensure SELF.md exists with scaffold
    if SELF_FILE.exists():
        current_self = SELF_FILE.read_text(encoding="utf-8")
        print(f"  Current SELF.md: {len(current_self)} chars")
    else:
        current_self = SELF_MD_SCAFFOLD
        if not dry_run:
            SELF_FILE.write_text(current_self, encoding="utf-8")
        print("  Created SELF.md scaffold")

    distill_prompt = f"""You are updating a self-model file (SELF.md) with lessons learned
from 4 weeks of weekly summaries.

## Current SELF.md

{current_self}

## Extracted Lessons (W10-W13)

{lessons_text}

## Instructions

Update SELF.md at: {SELF_FILE}

Given the lessons above and the current SELF.md, update the four sections:
- **Capabilities** — tools, techniques, approaches confirmed to work
- **Patterns** — recurring successful behaviors
- **Failure Modes** — mistakes that have recurred
- **Confidence Notes** — assumptions corrected or known uncertain areas

Rules:
1. 1-2 sentences per new entry, prefixed with a dash and bold keyword
2. Do NOT duplicate existing content — check before adding
3. Only add what is clearly new and evidenced in the lessons
4. Keep the existing structure and HTML comments
5. Use the Edit tool to update SELF.md directly

{"DRY RUN: Do NOT edit any files. Just describe what you would add." if dry_run else ""}

After updating, respond with a brief summary of what was added (section: count).
"""

    try:
        result = await run_with_fallback(
            RuntimeRequest(
                prompt=distill_prompt,
                cwd=PROJECT_ROOT,
                task_name="seed_self_model",
                capability=TEXT_REASONING,
                setting_sources=["user", "project"],
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=["Read", "Edit", "Glob", "Grep"],
                permission_mode="acceptEdits",
                max_turns=10,
            )
        )
        response = result.text.strip()
        print(
            f"[{now_local()}] Completed via {result.provider}:{result.model}"
            + (f" cost=${result.cost_usd:.4f}" if result.cost_usd else "")
        )
        return response

    except Exception as e:
        print(f"[{now_local()}] ERROR: {e}")
        return None


def main() -> None:
    ensure_directories()

    parser = argparse.ArgumentParser(description="Backfill SELF.md from weekly lessons")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing")
    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN MODE ===\n")

    result = asyncio.run(seed_self_model(dry_run=args.dry_run))

    if result:
        print(f"\n--- Summary ---\n{result[:800]}")
    else:
        print("\nNo changes made.")


if __name__ == "__main__":
    main()
