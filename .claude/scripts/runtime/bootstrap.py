"""Shared bootstrap/context builders for hooks and chat runtime."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from config import DAILY_DIR, MEMORY_DIR, now_local

MAX_DAILY_LOG_LINES = 30
MAX_CONTEXT_CHARS = 20_000
RESUME_MAX_CHARS = 20_000


def read_file_safe(path: Path) -> str:
    """Read a file, returning empty string if it doesn't exist."""

    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        return ""
    return ""


def get_recent_daily_log(
    *,
    daily_dir: Path = DAILY_DIR,
    max_lines: int = MAX_DAILY_LOG_LINES,
) -> str:
    """Read the tail of today's daily log, falling back to yesterday's."""

    today = now_local().strftime("%Y-%m-%d")
    today_log = daily_dir / f"{today}.md"

    content = read_file_safe(today_log)
    if content:
        lines = content.strip().splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "\n".join(lines)

    yesterday = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_log = daily_dir / f"{yesterday}.md"
    content = read_file_safe(yesterday_log)
    if content:
        lines = content.strip().splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "(Yesterday's log)\n" + "\n".join(lines)

    return ""


def _extract_capsule(content: str, max_chars: int = 1200) -> str:
    """Extract a compact capsule from a memory file.

    Takes the frontmatter + first two H2 sections, capped at max_chars.
    For SOUL.md this gives identity + core values (~1.2KB vs 6.4KB full).
    """
    lines = content.strip().splitlines()
    h2_count = 0
    cut_line = len(lines)
    in_frontmatter = False
    past_frontmatter = False

    for i, line in enumerate(lines):
        if i == 0 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter and line.strip() == "---":
            in_frontmatter = False
            past_frontmatter = True
            continue
        if past_frontmatter and line.startswith("## "):
            h2_count += 1
            if h2_count > 2:
                cut_line = i
                break

    capsule = "\n".join(lines[:cut_line]).strip()
    if len(capsule) > max_chars:
        capsule = capsule[:max_chars]
        last_nl = capsule.rfind("\n")
        if last_nl > 0:
            capsule = capsule[:last_nl]
    return capsule


def build_session_start_context(
    source: str,
    *,
    memory_dir: Path = MEMORY_DIR,
    daily_dir: Path = DAILY_DIR,
    max_context_chars: int = MAX_CONTEXT_CHARS,
    resume_max_chars: int = RESUME_MAX_CHARS,
) -> str:
    """Build the shared memory bootstrap context.

    Assembly order is priority-ranked so the most critical content survives
    the MAX_CONTEXT_CHARS truncation cap:
      1. MEMORY.md — behavioral rules, lessons, projects, urgents
      2. GOALS.md — quarterly objectives
      3. SOUL.md capsule — identity + core values (first 2 sections)
      4. USER.md capsule — profile + working style (first 2 sections)
      5. SELF.md capsule — capabilities overview (first 2 sections)
      6. Recent daily log tail
    """

    parts: list[str] = []

    bootstrap = read_file_safe(memory_dir / "BOOTSTRAP.md")
    if bootstrap:
        parts.append("## BOOTSTRAP (First-Run Onboarding)\n" + bootstrap.strip())

    # Priority 1: MEMORY.md — full content (already pruned by dream cycle)
    memory = read_file_safe(memory_dir / "MEMORY.md")
    if memory:
        parts.append("## Long-Term Memory\n" + memory.strip())

    # Priority 2: GOALS.md — full content (small file, ~1.7KB)
    goals = read_file_safe(memory_dir / "GOALS.md")
    if goals:
        parts.append("## Goals\n" + goals.strip())

    # Priority 3-5: Identity files as capsules (~1.2KB each vs 5-6KB full)
    soul = read_file_safe(memory_dir / "SOUL.md")
    if soul:
        parts.append("## Soul\n" + _extract_capsule(soul))

    user = read_file_safe(memory_dir / "USER.md")
    if user:
        parts.append("## User\n" + _extract_capsule(user))

    self_model = read_file_safe(memory_dir / "SELF.md")
    if self_model:
        parts.append("## Self-Model\n" + _extract_capsule(self_model))

    # Priority 6: Recent daily log
    daily = get_recent_daily_log(daily_dir=daily_dir)
    if daily:
        parts.append("## Recent Daily Log\n" + daily.strip())

    context = "\n\n---\n\n".join(parts)
    max_chars = resume_max_chars if source in {"resume", "compact"} else max_context_chars
    if len(context) > max_chars:
        context = context[:max_chars]
        last_newline = context.rfind("\n")
        if last_newline > 0:
            context = context[:last_newline]
    return context


# Function name kept as "second_brain" for backward compat
def build_second_brain_identity_context(project_root: Path, *, source: str = "startup") -> str:
    """Build the shared Homie system context for chat/runtime paths."""

    memory_dir = project_root / "TheHomie" / "Memory"
    daily_dir = memory_dir / "daily"
    return build_session_start_context(source, memory_dir=memory_dir, daily_dir=daily_dir)


