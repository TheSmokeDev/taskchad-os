"""
Skills Sync Hook (project -> user)

SessionStart hook that copies project-level skills (the tracked source-of-truth
under thehomie/.claude/skills/) into the user-level ~/.claude/skills/ cache,
which is what Claude Code actually loads when slash-commands like /vault-ingest
fire from any directory.

Why this exists: project-level vs user-level skill drift was the root cause of
gap-5 raw-pipeline-audit (the user-level vault-ingest SKILL.md was missing
Step 2.5 - preserve_raw - for two weeks).

This hook is idempotent: it only writes when the project-level content differs
from the user-level content (SHA-256 compare). Logged via shared.log_hook_execution.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time as _time
from pathlib import Path

# Add scripts directory to path for shared imports
_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_scripts_dir))

from shared import log_hook_execution  # noqa: E402

# Skills to keep in sync. Conservative allow-list - do NOT auto-sync everything
# (some user-level skills are genuinely per-machine, e.g. private credentials).
# Add to this list when a project-level skill needs to be the source-of-truth.
SKILLS_TO_SYNC = [
    "vault-ingest",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sync_one(project_skill: Path, user_skill: Path) -> str:
    """Returns one of: 'copied', 'in-sync', 'project-missing'.

    Atomic write (R3-fix): writes to a temp file in the same target directory
    then os.replace(temp, dest). Prevents partial/truncated writes if the copy
    is interrupted (disk-full, permission flap, antivirus quarantine mid-copy).
    """
    if not project_skill.is_file():
        return "project-missing"
    user_skill.parent.mkdir(parents=True, exist_ok=True)
    if user_skill.is_file() and _sha256(user_skill) == _sha256(project_skill):
        return "in-sync"
    # Atomic replace: write to temp in same dir, then os.replace
    import os
    import tempfile
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{user_skill.name}.", suffix=".tmp",
        dir=str(user_skill.parent),
    )
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        shutil.copy2(project_skill, tmp)
        os.replace(tmp, user_skill)  # atomic on same filesystem
    except Exception:
        # Clean up temp on failure; don't leak debris
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    return "copied"


def main() -> None:
    _start = _time.time()

    # Read hook input from stdin (we don't use it but must consume cleanly)
    try:
        hook_input: dict[str, object] = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}
    source = hook_input.get("source", "startup")
    if not isinstance(source, str):
        source = "startup"

    project_root = Path(__file__).resolve().parent.parent.parent  # thehomie/
    project_skills_root = project_root / ".claude" / "skills"
    user_skills_root = Path.home() / ".claude" / "skills"

    # Fail-soft (R3-fix): SessionStart hooks must not break the session.
    # Wrap the sync loop and the log call so any single failure is reported
    # but doesn't bubble up. Worst case: stale user-level skill, same as today.
    results: list[str] = []
    status = "OK"
    error_summary = ""
    try:
        for skill_name in SKILLS_TO_SYNC:
            project_md = project_skills_root / skill_name / "SKILL.md"
            user_md = user_skills_root / skill_name / "SKILL.md"
            try:
                outcome = _sync_one(project_md, user_md)
            except Exception as e:  # noqa: BLE001 - fail-soft per R3
                outcome = f"error:{type(e).__name__}"
                if not error_summary:
                    error_summary = f"{skill_name}: {e}"
            results.append(f"{skill_name}={outcome}")
        if any("error:" in r for r in results):
            status = "ERROR"
    except Exception as e:  # noqa: BLE001 - outer guard
        status = "ERROR"
        results.append(f"loop-error:{type(e).__name__}")
        error_summary = str(e)

    summary = ", ".join(results) if results else "no-skills-configured"
    if error_summary:
        summary = f"{summary} | {error_summary}"
    try:
        log_hook_execution("sync-skills", source, status,
                           _time.time() - _start, summary)
    except Exception:  # noqa: BLE001 - even logging is fail-soft
        pass

    # SessionStart hooks may emit additionalContext to stdout; we don't need to.
    # Always exit 0 so the SessionStart chain continues even if sync failed.
    # The log line is the audit trail; ERROR status is visible via grep.
    sys.exit(0)


if __name__ == "__main__":
    main()
