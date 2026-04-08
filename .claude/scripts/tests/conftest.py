"""Shared fixtures for The Homie tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure scripts dir is on path for imports
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    """Provide a temporary state directory for PID files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def tmp_pid_file(tmp_state_dir: Path) -> Path:
    """Provide a temporary PID file path."""
    return tmp_state_dir / "bot.pid"


@pytest.fixture
def tmp_env_file(tmp_path: Path) -> Path:
    """Provide a temporary .env file for config reload tests."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CHAT_MAX_TURNS=25\n"
        "CHAT_MAX_BUDGET_USD=2.0\n"
        "OPENAI_API_KEY=sk-test-key\n"
        "VOICE_TTS_ENGINE=edge\n",
        encoding="utf-8",
    )
    return env_file
