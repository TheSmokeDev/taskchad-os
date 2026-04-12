from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from config import now_local
from runtime.bootstrap import (
    build_session_briefing,
    build_session_start_context,
    _extract_project_status,
    _extract_section,
    _extract_urgents,
    _extract_goal_names,
    _build_memory_index,
)


# ---------------------------------------------------------------------------
# Helpers to create realistic test fixtures
# ---------------------------------------------------------------------------

SAMPLE_SOUL = """\
---
tags: [system, identity]
---
# SOUL.md

## Core Identity
The Homie is a personal AI agent framework.

## Core Values
Authenticity, resourcefulness, direct communication.

## Boundaries
Never auto-apply changes without human approval.
"""

SAMPLE_SELF = """\
---
tags: [system, self-model]
---
# SELF.md

## Capabilities
Memory pipelines, recall, orchestration, finance, integrations.

## Patterns
Vertical slice architecture, provider-agnostic runtime.

## Growth Areas
Context compression, self-evolution.
"""

SAMPLE_USER = """\
---
tags: [system, user]
---
# USER.md

## Profile
owner — software engineer, insurance industry.

## Working Style
Direct, casual, no BS. Prefers plain-English breakdowns.

## Preferences
Sign as YourAgent. Browser testing: agent-browser only.
"""

SAMPLE_MEMORY = """\
---
tags: [system, memory]
---
# MEMORY.md

## Active Projects

- **YourBusiness** — Insurance lead gen. Monitoring dark since 03-29. Backend port 7888.
- **The Homie** — Telegram bot. Bot restart loop ongoing. 58 commands.
- **The Homie open-source** — New public repo. Need sanitize script.

## Key Decisions

- **Vertical slice**: Two surfaces — thehomie + MC GUI.
- **SQLite default**: db.prepare() IS the interface.

## Global Rules

- **Testing: map code paths first, one test per distinct path.**

## Lessons Learned

- **Bot caches SOUL/MEMORY/USER at startup**: Must restart bot after editing.

## Important Facts

- **Test suite**: 1,235 passing.
- **Langfuse**: Self-hosted localhost:3000.

## Finance Summary

Full details in `vault/memory/finances/BUDGET.md`. Paycheck $7,571 hits 15th.

## Upcoming Events

- ⚠️ **Car payment $1,633.22 due 2026-04-01 (PAST DUE)**
- **loan_provider loan #1 due 2026-04-16**: 0.03815 BTC
- **Something far away due 2026-12-25**: Not urgent at all
- **2025 taxes not started** — No date, undated urgent

## Preferences

Direct, casual, no BS. Sign as YourAgent.
"""

SAMPLE_GOALS = """\
---
tags: [system, goals]
---
# GOALS.md

## Q2 2026

### YourBusiness Revenue
Target: $10K MRR by June.

### The Homie System
Ship open-source framework.
"""


def _setup_memory_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Create a realistic memory dir with all files."""
    memory_dir = tmp_path / "Memory"
    daily_dir = memory_dir / "daily"
    daily_dir.mkdir(parents=True)
    (memory_dir / "concepts").mkdir()

    (memory_dir / "SOUL.md").write_text(SAMPLE_SOUL, encoding="utf-8")
    (memory_dir / "SELF.md").write_text(SAMPLE_SELF, encoding="utf-8")
    (memory_dir / "USER.md").write_text(SAMPLE_USER, encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text(SAMPLE_MEMORY, encoding="utf-8")
    (memory_dir / "GOALS.md").write_text(SAMPLE_GOALS, encoding="utf-8")

    today = now_local().strftime("%Y-%m-%d")
    (daily_dir / f"{today}.md").write_text(
        "# Daily Log\n\n## Sessions\n\n## Heartbeats\n\n"
        "### Heartbeat (08:08)\n\n"
        "- My Checking: $6.11 — essentially empty\n"
        "- Google OAuth expired\n",
        encoding="utf-8",
    )
    return memory_dir, daily_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_briefing_size_under_cap(tmp_path: Path) -> None:
    memory_dir, daily_dir = _setup_memory_dir(tmp_path)
    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)
    assert len(briefing) <= 6000, f"Briefing too large: {len(briefing)} chars"
    assert len(briefing) > 500, f"Briefing suspiciously small: {len(briefing)} chars"


def test_briefing_contains_identity(tmp_path: Path) -> None:
    memory_dir, daily_dir = _setup_memory_dir(tmp_path)
    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)
    assert "### Identity" in briefing
    assert "Core Identity" in briefing
    assert "Core Values" in briefing


def test_briefing_contains_capabilities(tmp_path: Path) -> None:
    memory_dir, daily_dir = _setup_memory_dir(tmp_path)
    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)
    assert "### Capabilities" in briefing
    assert "Memory pipelines" in briefing


def test_briefing_contains_user_model(tmp_path: Path) -> None:
    memory_dir, daily_dir = _setup_memory_dir(tmp_path)
    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)
    assert "### User" in briefing
    assert "owner" in briefing


def test_briefing_contains_rules(tmp_path: Path) -> None:
    memory_dir, daily_dir = _setup_memory_dir(tmp_path)
    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)
    assert "### Rules" in briefing
    assert "Testing: map code paths" in briefing
    assert "YourAgent" in briefing  # from Preferences


def test_urgents_date_filter(tmp_path: Path) -> None:
    """Past-due and near-term events included; far-future excluded."""
    memory_dir, daily_dir = _setup_memory_dir(tmp_path)
    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)
    assert "Car payment" in briefing  # past due 2026-04-01
    assert "2025 taxes" in briefing  # undated — always included
    assert "2026-12-25" not in briefing  # 8+ months away


def test_urgents_near_term_included(tmp_path: Path) -> None:
    """Events within 14 days are included."""
    # loan_provider loan is 2026-04-16, and today is 2026-04-08 = 8 days away
    urgents = _extract_urgents(SAMPLE_MEMORY)
    assert "loan_provider" in urgents


def test_project_status_extraction(tmp_path: Path) -> None:
    """Projects extracted with terse status, not just names."""
    projects = _extract_project_status(SAMPLE_MEMORY)
    assert "YourBusiness" in projects
    assert "Monitoring dark" in projects or "monitoring dark" in projects.lower()
    assert "The Homie" in projects
    # Should NOT contain full backend paths and other noise
    assert "Backend:" not in projects or len(projects) < len(SAMPLE_MEMORY)


def test_goal_names_extraction() -> None:
    names = _extract_goal_names(SAMPLE_GOALS)
    assert "YourBusiness Revenue" in names
    assert "The Homie System" in names
    assert "|" in names


def test_memory_index_has_paths(tmp_path: Path) -> None:
    memory_dir, _ = _setup_memory_dir(tmp_path)
    index = _build_memory_index(memory_dir)
    # Vault is outside PROJECT_ROOT in tests, so the absolute vault path is shown
    assert memory_dir.as_posix() in index
    assert "BUDGET.md" in index
    assert "GOALS.md" in index
    assert "concepts/" in index  # concepts dir exists in fixture


def test_bootstrap_override(tmp_path: Path) -> None:
    memory_dir, daily_dir = _setup_memory_dir(tmp_path)
    (memory_dir / "BOOTSTRAP.md").write_text("Welcome to The Homie!", encoding="utf-8")
    context = build_session_start_context(
        "startup", memory_dir=memory_dir, daily_dir=daily_dir
    )
    assert "BOOTSTRAP" in context
    assert "Welcome to The Homie!" in context
    # Should NOT contain briefing sections
    assert "### Identity" not in context


def test_failopen_on_empty_memory(tmp_path: Path) -> None:
    """When core files are missing, falls back to full dump (not empty briefing)."""
    memory_dir = tmp_path / "Memory"
    daily_dir = memory_dir / "daily"
    daily_dir.mkdir(parents=True)

    # Only provide MEMORY.md (no SOUL.md, no SELF.md → fail-open triggers)
    (memory_dir / "MEMORY.md").write_text(SAMPLE_MEMORY, encoding="utf-8")

    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)
    # Should fall back to full dump which includes "## Long-Term Memory"
    assert "## Long-Term Memory" in briefing
    # Should NOT have briefing format
    assert "## The Homie — Session Briefing" not in briefing


def test_no_primo_builder_exported() -> None:
    """Regression: build_primo_identity_context must not exist after identity unification."""
    import runtime.bootstrap as mod

    assert not hasattr(mod, "build_primo_identity_context")


def test_extract_section_returns_empty_on_miss() -> None:
    assert _extract_section("# No H2 here\nJust text.", "Missing") == ""


def test_briefing_has_finance_and_index(tmp_path: Path) -> None:
    memory_dir, daily_dir = _setup_memory_dir(tmp_path)
    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)
    assert "### Finance" in briefing
    assert "BUDGET.md" in briefing
    assert "### Memory Index" in briefing
