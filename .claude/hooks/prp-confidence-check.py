"""
PostToolUse Hook: PRP Confidence Self-Improvement

Fires after any Write to a PRP file. Checks the confidence score and injects
context nudging Claude to research gaps and improve the PRP toward 9.5+/10.

Tracks iteration count via a temp file to cap at 2 improvement passes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

# Pattern: PRPs/PRP-*.md (works with both / and \ separators)
PRP_PATTERN = re.compile(r"PRPs[/\\]PRP-.*\.md$")
CONFIDENCE_PATTERN = re.compile(r"##\s*Confidence\s+Score:\s*(\d+\.?\d*)\s*/\s*10", re.IGNORECASE)
THRESHOLD = 9.5
MAX_ITERATIONS = 2


def get_iteration_file(file_path: str) -> Path:
    """Get the temp file path for tracking improvement iterations."""
    file_hash = hashlib.md5(file_path.encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"prp-improve-{file_hash}.count"


def get_iteration_count(iter_file: Path) -> int:
    """Read current iteration count from temp file."""
    try:
        return int(iter_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def increment_iteration(iter_file: Path, current: int) -> None:
    """Increment and save iteration count."""
    iter_file.write_text(str(current + 1))


def main() -> None:
    # Read hook input from stdin
    try:
        hook_input: dict = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Extract file_path from tool_input
    tool_input = hook_input.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, ValueError):
            sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path:
        sys.exit(0)

    # Check if this is a PRP file
    if not PRP_PATTERN.search(file_path):
        sys.exit(0)

    # Check iteration count
    iter_file = get_iteration_file(file_path)
    iteration = get_iteration_count(iter_file)
    if iteration >= MAX_ITERATIONS:
        sys.exit(0)

    # Read the PRP file and extract confidence score
    prp_path = Path(file_path)
    if not prp_path.exists():
        sys.exit(0)

    try:
        content = prp_path.read_text(encoding="utf-8")
    except Exception:
        sys.exit(0)

    match = CONFIDENCE_PATTERN.search(content)
    if not match:
        # No confidence score found — don't trigger
        sys.exit(0)

    score = float(match.group(1))
    if score >= THRESHOLD:
        # Good enough — clean up and exit
        if iter_file.exists():
            iter_file.unlink()
        sys.exit(0)

    # Score is below threshold — inject improvement context
    increment_iteration(iter_file, iteration)
    remaining = MAX_ITERATIONS - iteration - 1

    result = {
        "additionalContext": (
            f"SELF-IMPROVE TRIGGER: The PRP you just wrote has a confidence score of {score}/10 "
            f"(iteration {iteration + 1}/{MAX_ITERATIONS}).\n"
            f"Before proceeding, you MUST:\n"
            f"1. Read the 'Why not 10' or gaps/risks section in the PRP\n"
            f"2. For each gap listed: research it (read files, search codebase, check docs)\n"
            f"3. Update the PRP with your findings — fill in line numbers, verify patterns, confirm APIs\n"
            f"4. Re-evaluate and update the confidence score\n"
            f"{'Stop at 9.5+ or when remaining gaps are runtime-only.' if remaining == 0 else f'{remaining} improvement iteration(s) remaining.'}\n"
            f"Do NOT proceed to execution until this improvement pass is complete."
        )
    }

    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
