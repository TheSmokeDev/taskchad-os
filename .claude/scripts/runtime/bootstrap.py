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


def build_session_start_context(
    source: str,
    *,
    memory_dir: Path = MEMORY_DIR,
    daily_dir: Path = DAILY_DIR,
    max_context_chars: int = MAX_CONTEXT_CHARS,
    resume_max_chars: int = RESUME_MAX_CHARS,
) -> str:
    """Build the shared memory bootstrap context."""

    parts: list[str] = []

    bootstrap = read_file_safe(memory_dir / "BOOTSTRAP.md")
    if bootstrap:
        parts.append("## BOOTSTRAP (First-Run Onboarding)\n" + bootstrap.strip())

    soul = read_file_safe(memory_dir / "SOUL.md")
    if soul:
        parts.append("## Soul\n" + soul.strip())

    self_model = read_file_safe(memory_dir / "SELF.md")
    if self_model:
        parts.append("## Self-Model\n" + self_model.strip())

    user = read_file_safe(memory_dir / "USER.md")
    if user:
        parts.append("## User\n" + user.strip())

    memory = read_file_safe(memory_dir / "MEMORY.md")
    if memory:
        parts.append("## Long-Term Memory\n" + memory.strip())

    goals = read_file_safe(memory_dir / "GOALS.md")
    if goals:
        parts.append("## Goals\n" + goals.strip())

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


