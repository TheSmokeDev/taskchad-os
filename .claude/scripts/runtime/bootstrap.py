"""Shared bootstrap/context builders for hooks and chat runtime."""

from __future__ import annotations

import re
import sys
from datetime import timedelta
from pathlib import Path

from config import DAILY_DIR, MEMORY_DIR, PROJECT_ROOT, now_local
from repository_config import build_repository_config_briefing

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

MAX_DAILY_LOG_LINES = 30
MAX_CONTEXT_CHARS = 20_000
RESUME_MAX_CHARS = 20_000
MAX_BRIEFING_CHARS = 6000


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


def _extract_section(content: str, heading: str) -> str:
    """Extract body of an H2 section by name from markdown content."""
    pattern = rf"^## {re.escape(heading)}\s*\n(.*?)(?=\n## |\Z)"
    m = re.search(pattern, content, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extract_project_status(memory: str) -> str:
    """Extract terse project status lines from Active Projects section.

    Keeps project name + key status indicator (not full text).
    E.g.: "YourBusiness — monitoring dark since 03-29"
    """
    section = _extract_section(memory, "Active Projects")
    if not section:
        return ""
    lines = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("- **"):
            continue
        # Extract "**Name** — rest..." and truncate rest to ~80 chars
        m = re.match(r"- \*\*(.+?)\*\*\s*[—–-]\s*(.*)", line)
        if m:
            name, detail = m.group(1), m.group(2)
            # Keep first ~80 chars to preserve key status info
            if len(detail) > 80:
                detail = detail[:77] + "..."
            lines.append(f"- **{name}** — {detail}")
        else:
            lines.append(line[:100])
    return "\n".join(lines)


def _extract_urgents(memory: str) -> str:
    """Extract date-filtered urgents from Upcoming Events section.

    Includes items that are past-due or due within 14 days.
    """
    section = _extract_section(memory, "Upcoming Events")
    if not section:
        return ""
    today = now_local().date()
    lines = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        # Check for ISO date (YYYY-MM-DD) in the line
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", line)
        if date_match:
            try:
                from datetime import date as date_cls

                event_date = date_cls.fromisoformat(date_match.group(1))
                days_until = (event_date - today).days
                if days_until <= 14:  # past-due or within 14 days
                    lines.append(line)
            except ValueError:
                lines.append(line)  # can't parse date, include anyway
        else:
            # No date found — include it (undated urgents are always relevant)
            lines.append(line)
    return "\n".join(lines)


def _extract_last_session(daily_dir: Path) -> str:
    """Extract first substantive content block from today's daily log.

    Skips empty headers, returns up to 300 chars.
    """
    today = now_local().strftime("%Y-%m-%d")
    content = read_file_safe(daily_dir / f"{today}.md")
    if not content:
        yesterday = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d")
        content = read_file_safe(daily_dir / f"{yesterday}.md")
        if not content:
            return ""

    # Find first block with actual content (not just headers)
    lines = content.strip().splitlines()
    block_lines: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if in_block and block_lines:
                break  # end of first substantive block
            in_block = False
            continue
        if stripped:
            in_block = True
            block_lines.append(stripped)
        elif in_block and block_lines:
            break  # blank line ends block

    result = "\n".join(block_lines)
    if len(result) > 300:
        result = result[:297] + "..."
    return result


def _extract_goal_names(goals: str) -> str:
    """Extract H3 heading names from GOALS.md, joined with ' | '."""
    headings = re.findall(r"^### (.+)", goals, re.MULTILINE)
    return " | ".join(h.strip() for h in headings) if headings else ""


def _extract_working_memory(memory_dir: Path) -> str:
    """Build a compact `### Working Memory` block from WORKING.md.

    Living Mind Phase 1: surfaces cross-session open threads, active hypotheses,
    and unresolved questions. Returns empty string if WORKING.md is missing or
    all active sections are empty. Fail-open: any error yields "" and the
    briefing proceeds without the section.
    """
    try:
        from living_memory import build_briefing_section  # noqa: WPS433

        block = build_briefing_section(memory_dir)
        if not block:
            return ""
        # build_briefing_section returns `## Working Memory\n...`; rewrite as H3
        # so it nests under the briefing's H2 like the other sections.
        return block.replace("## Working Memory", "### Working Memory", 1)
    except Exception:
        return ""


def _build_proactive_brief(memory_dir: Path, daily_dir: Path) -> str:
    """Build the unified proactive brief for session startup."""

    try:
        from cognition.proactive_brief import build_proactive_brief_section

        return build_proactive_brief_section(
            memory_dir,
            daily_dir=daily_dir,
            include_identity=False,
            header="### Proactive Brief",
        )
    except Exception:
        return _extract_working_memory(memory_dir)


def _build_memory_index(memory_dir: Path) -> str:
    """Build a topic → path mapping for the memory index."""
    # When the vault lives inside the repo, show repo-relative paths so any
    # provider can locate files. When HOMIE_VAULT_DIR points outside the repo,
    # fall back to the absolute vault path so the AI sees the real location.
    try:
        base = memory_dir.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        base = memory_dir.as_posix()
    entries = [
        f"- finance: {base}/finances/BUDGET.md",
        f"- lessons: {base}/MEMORY.md (## Lessons Learned)",
        f"- decisions: {base}/MEMORY.md (## Key Decisions)",
        f"- facts: {base}/MEMORY.md (## Important Facts)",
        f"- goals detail: {base}/GOALS.md",
        f"- soul full: {base}/SOUL.md",
        f"- user profile: {base}/USER.md",
        f"- self-model: {base}/SELF.md",
    ]
    # Add concepts dir if it exists
    concepts = memory_dir / "concepts"
    if concepts.exists() and concepts.is_dir():
        entries.append(f"- concepts: {base}/concepts/")
    return "\n".join(entries)


def build_session_briefing(
    *,
    memory_dir: Path = MEMORY_DIR,
    daily_dir: Path = DAILY_DIR,
) -> str:
    """Build a compact, self-contained session briefing (~4.5KB).

    This is the framework-level orientation injected at session start,
    regardless of which provider is running. Designed to be self-contained
    — no assumptions about CLAUDE.md, Read tool, or provider features.

    Assembly order is priority-ranked:
      1. SOUL.md capsule — identity + core values
      2. SELF.md capsule — capabilities + patterns
      3. USER.md capsule — profile + operating instructions
      4. Global Rules + Preferences verbatim
      5. Active Projects with terse status
      6. Profile-owned repository config (only when enabled and valid)
      7. Urgents (date-filtered)
      8. Last session context
      9. Goals snapshot
      10. Finance summary verbatim
      11. Memory index with repo-relative paths

    Fail-open: if required sections (identity, capabilities, rules) are
    missing, falls back to the full-dump behavior.
    """
    parts: list[str] = []

    # --- Required sections (fail-open guard checks these) ---

    # 1. Identity (SOUL.md capsule)
    soul = read_file_safe(memory_dir / "SOUL.md")
    identity = _extract_capsule(soul) if soul else ""
    if identity:
        parts.append("### Identity\n" + identity)

    # 2. Capabilities (SELF.md capsule)
    self_model = read_file_safe(memory_dir / "SELF.md")
    capabilities = _extract_capsule(self_model) if self_model else ""
    if capabilities:
        parts.append("### Capabilities\n" + capabilities)

    # 3. User model (USER.md capsule)
    user = read_file_safe(memory_dir / "USER.md")
    user_model = _extract_capsule(user) if user else ""
    if user_model:
        parts.append("### User\n" + user_model)

    # 4. Rules (Global Rules + Preferences from MEMORY.md)
    memory = read_file_safe(memory_dir / "MEMORY.md")
    rules = _extract_section(memory, "Global Rules") if memory else ""
    prefs = _extract_section(memory, "Preferences") if memory else ""
    rules_block = ""
    if rules:
        rules_block += rules
    if prefs:
        rules_block += ("\n\n" if rules_block else "") + prefs
    if rules_block:
        parts.append("### Rules\n" + rules_block)

    # --- Fail-open guard: required sections must be present ---
    if not identity or not capabilities or not rules_block:
        # Fall back to full dump — extractor failure
        return _build_full_dump(memory_dir=memory_dir, daily_dir=daily_dir)

    # --- Optional sections (graceful degradation) ---

    # 4.5. Unified proactive brief (active inferences + working memory).
    proactive_brief = _build_proactive_brief(memory_dir, daily_dir)
    if proactive_brief:
        parts.append(proactive_brief)

    # 5. Active Projects (terse status)
    projects = _extract_project_status(memory) if memory else ""
    if projects:
        parts.append("### Active Projects\n" + projects)

    # 6. Profile-owned repository runtime config (validation/briefing only).
    repository_config = build_repository_config_briefing()
    if repository_config:
        parts.append(repository_config)

    # 7. Urgents (date-filtered)
    urgents = _extract_urgents(memory) if memory else ""
    if urgents:
        parts.append("### Urgents\n" + urgents)

    # 7. Last session
    last_session = _extract_last_session(daily_dir=daily_dir)
    if last_session:
        parts.append("### Last Session\n" + last_session)

    # 8. Goals
    goals = read_file_safe(memory_dir / "GOALS.md")
    goal_names = _extract_goal_names(goals) if goals else ""
    if goal_names:
        parts.append("### Goals\n" + goal_names)

    # 9. Finance summary
    finance = _extract_section(memory, "Finance Summary") if memory else ""
    if finance:
        parts.append("### Finance\n" + finance)

    # 10. Memory index
    index = _build_memory_index(memory_dir)
    if index:
        parts.append(
            "### Memory Index\nFull memory available. To load a topic, read the file:\n"
            + index
        )

    briefing = "## The Homie — Session Briefing\n\n" + "\n\n".join(parts)

    if len(briefing) > MAX_BRIEFING_CHARS:
        briefing = briefing[:MAX_BRIEFING_CHARS]
        last_nl = briefing.rfind("\n")
        if last_nl > 0:
            briefing = briefing[:last_nl]

    return briefing


def _build_full_dump(
    *,
    memory_dir: Path = MEMORY_DIR,
    daily_dir: Path = DAILY_DIR,
) -> str:
    """Legacy full-dump context builder. Used as fail-open fallback."""
    parts: list[str] = []

    memory = read_file_safe(memory_dir / "MEMORY.md")
    if memory:
        parts.append("## Long-Term Memory\n" + memory.strip())

    goals = read_file_safe(memory_dir / "GOALS.md")
    if goals:
        parts.append("## Goals\n" + goals.strip())

    soul = read_file_safe(memory_dir / "SOUL.md")
    if soul:
        parts.append("## Soul\n" + _extract_capsule(soul))

    user = read_file_safe(memory_dir / "USER.md")
    if user:
        parts.append("## User\n" + _extract_capsule(user))

    self_model = read_file_safe(memory_dir / "SELF.md")
    if self_model:
        parts.append("## Self-Model\n" + _extract_capsule(self_model))

    daily = get_recent_daily_log(daily_dir=daily_dir)
    if daily:
        parts.append("## Recent Daily Log\n" + daily.strip())

    context = "\n\n---\n\n".join(parts)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS]
        last_newline = context.rfind("\n")
        if last_newline > 0:
            context = context[:last_newline]
    return context


def build_session_start_context(
    source: str,
    *,
    memory_dir: Path = MEMORY_DIR,
    daily_dir: Path = DAILY_DIR,
    max_context_chars: int = MAX_CONTEXT_CHARS,
    resume_max_chars: int = RESUME_MAX_CHARS,
) -> str:
    """Build the shared memory bootstrap context.

    Delegates to build_session_briefing() for a compact ~4.5KB orientation.
    Falls back to full dump if briefing extractors fail (fail-open).

    BOOTSTRAP.md override: if present, returns bootstrap content directly
    (first-run onboarding takes priority over normal briefing).

    The source and max_*_chars params are kept for backward compat but
    are vestigial — the briefing engine manages its own budget.
    """
    # First-run onboarding override
    bootstrap = read_file_safe(memory_dir / "BOOTSTRAP.md")
    if bootstrap:
        return "## BOOTSTRAP (First-Run Onboarding)\n" + bootstrap.strip()

    return build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)


# Function name kept as "second_brain" for backward compat
def build_second_brain_identity_context(project_root: Path, *, source: str = "startup") -> str:
    """Build the shared Homie system context for chat/runtime paths.

    The ``project_root`` parameter is retained for backward compat but is no
    longer used to locate the vault — ``config.MEMORY_DIR`` is the canonical
    source and respects the ``HOMIE_VAULT_DIR`` env override.
    """

    return build_session_start_context(source, memory_dir=MEMORY_DIR, daily_dir=DAILY_DIR)


