"""Test PRD-8 Phase 5a / WS1.13 (B10) — sanitizer cabinet coverage.

Lightweight version: instead of running the full sanitizer subprocess
(which expects the public-export repo to exist locally), assert the cabinet
files do NOT match the sanitizer's deny patterns and DO contain only
public-safe content.

Production sanitizer dry-run is gated behind the `--sanitizer` flag in CI.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_CABINET_DIR = Path(__file__).resolve().parent.parent / "cabinet"
_DASHBOARD_SERVER_ROUTES = (
    Path(__file__).resolve().parent.parent.parent.parent / "dashboard" / "server" / "src" / "routes"
)
_DASHBOARD_WEB_PAGES = (
    Path(__file__).resolve().parent.parent.parent.parent / "dashboard" / "web" / "src" / "pages"
)
_DASHBOARD_WEB_LIB = (
    Path(__file__).resolve().parent.parent.parent.parent / "dashboard" / "web" / "src" / "lib"
)


_CABINET_FILES = [
    _CABINET_DIR / "meeting_channel.py",
    _CABINET_DIR / "text_orchestrator.py",
    _CABINET_DIR / "text_router.py",
    _CABINET_DIR / "tool_policy.py",
    _CABINET_DIR / "title.py",
    _DASHBOARD_SERVER_ROUTES / "cabinet.ts",
    _DASHBOARD_WEB_PAGES / "Cabinet.tsx",
    _DASHBOARD_WEB_LIB / "cabinet-stream.ts",
]


# Patterns the sanitizer would flag as personal data — must not appear in cabinet code.
_FORBIDDEN_PATTERNS = [
    "your-calendar@gmail.com",
    "your-email@example.com",
    "TELEGRAM_BOT_TOKEN=",
    "SLACK_BOT_TOKEN=",
    "BEGIN PRIVATE KEY",
    "TELLER_ACCESS_TOKEN=",
    # API key prefixes — would only be a leak if a literal value follows.
    "sk-ant-",
    "sk-or-v1-",
]


@pytest.mark.parametrize("path", _CABINET_FILES, ids=[p.name for p in _CABINET_FILES])
def test_cabinet_file_has_no_personal_secrets(path: Path) -> None:
    """B10 — cabinet code must be sanitizer-safe (no personal data, no real tokens)."""
    assert path.is_file(), f"missing cabinet file: {path}"
    text = path.read_text(encoding="utf-8")
    for pattern in _FORBIDDEN_PATTERNS:
        # The sanitizer denies ACTUAL secret values, not the pattern names
        # used in code (e.g. `TELEGRAM_BOT_TOKEN` as an env-var REFERENCE
        # is fine; `TELEGRAM_BOT_TOKEN=...real value...` would be a leak).
        # Check for `<pattern>...` as a literal substring with a value.
        if pattern.endswith("="):
            # `=` patterns: only fail if followed by non-empty content within 64 chars.
            idx = text.find(pattern)
            if idx >= 0:
                tail = text[idx + len(pattern): idx + len(pattern) + 64]
                stripped = tail.strip().strip('"').strip("'")
                if stripped and not stripped.startswith(("os.environ", "process.env", "sk-test")):
                    pytest.fail(f"{path.name}: literal secret pattern {pattern!r} appears with value")
        else:
            assert pattern not in text, f"{path.name}: forbidden pattern {pattern!r}"


def test_cabinet_files_exist_and_nonempty() -> None:
    """Smoke test — every WS1+WS2+WS3+WS4 cabinet file is present + non-empty."""
    for path in _CABINET_FILES:
        assert path.is_file(), f"missing: {path}"
        size = path.stat().st_size
        assert size > 100, f"{path.name} suspiciously small ({size} bytes)"
