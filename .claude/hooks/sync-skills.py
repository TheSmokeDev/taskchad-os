"""
Skills Sync Hook (project -> user)

SessionStart hook that copies project-level skills (the tracked source-of-truth
under thehomie/.claude/skills/) into the user-level ~/.claude/skills/ cache,
which is what Claude Code actually loads when slash-commands like /vault-ingest
fire from any directory.

Why this exists: project-level vs user-level skill drift was the root cause of
gap-5 raw-pipeline-audit (the user-level vault-ingest SKILL.md was missing
Step 2.5 - preserve_raw - for two weeks).

This hook is idempotent: it only writes when the project-level skill directory
differs from the user-level content (SHA-256 tree compare). Logged via
shared.log_hook_execution.
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

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

from shared import log_hook_execution  # noqa: E402

# Skills to keep in sync. Conservative allow-list - do NOT auto-sync everything
# (some user-level skills are genuinely per-machine, e.g. private credentials).
# Add to this list when a project-level skill needs to be the source-of-truth.
SKILLS_TO_SYNC = [
    # Vault skills consolidated into the single vault-ops skill (2026-07-11);
    # the 14 atomic vault-* skills were archived to .claude/_archive/skills/.
    "vault-ops",
    # CLUTCH v3 — added 2026-04-29 alongside adversarial-review reference + templates.
    # Project-level clutch is the source of truth; user-level is the cache Claude Code loads.
    "clutch",
    # Video pipeline skills — added 2026-06-11. homie-video = house loop
    # (sanitizer-denied, private); video-director = generic public capability
    # (ships via the export). Project-level is the source of truth.
    "homie-video",
    "video-director",
]


def _tree_digest(path: Path) -> str:
    """Hash a file or directory, including relative file names for directories."""
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.name.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        return h.hexdigest()

    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = child.relative_to(path).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(child.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _sync_one(project_skill: Path, user_skill: Path) -> str:
    """Returns one of: 'copied', 'in-sync', 'project-missing'.

    Copies the whole skill directory, not just SKILL.md. This matters for skills
    with scripts, references, or evals.
    """
    if not project_skill.is_dir():
        return "project-missing"
    user_skill.parent.mkdir(parents=True, exist_ok=True)
    if user_skill.is_dir() and _tree_digest(user_skill) == _tree_digest(project_skill):
        return "in-sync"

    import tempfile

    tmp_path = tempfile.mkdtemp(
        prefix=f".{user_skill.name}.", suffix=".tmp",
        dir=str(user_skill.parent),
    )
    tmp = Path(tmp_path)
    try:
        shutil.rmtree(tmp)
        shutil.copytree(project_skill, tmp)
        if user_skill.exists():
            shutil.rmtree(user_skill)
        tmp.replace(user_skill)
    except Exception:
        # Clean up temp on failure; don't leak debris
        if tmp.exists():
            try:
                shutil.rmtree(tmp)
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
            project_skill = project_skills_root / skill_name
            user_skill = user_skills_root / skill_name
            try:
                outcome = _sync_one(project_skill, user_skill)
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
