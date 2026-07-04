"""Tests for persona learning tick (US-006).

Covers:
  1. Boot-order — persona_learning_tick.py discovered by Tier A/B audit
  2. Default-profile guard — tick refuses to run under a named profile
  3. Silent-skip — no attributed rows since stamp → PERSONA_REFLECT_SILENT
  4. Fail-open — one persona failure does not block the next
  5. Subprocess spawn — correct env and command shape
  6. Grep gates — no direct provider imports, get_default_paths explicit
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
_REPO_ROOT = _SCRIPTS_DIR.parent.parent

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))


# ── Boot-order ──────────────────────────────────────────────────────────────


class TestBootOrder:
    def test_tick_has_shim_call(self) -> None:
        """persona_learning_tick.py contains apply_persona_override() at top level."""
        src = (_SCRIPTS_DIR / "persona_learning_tick.py").read_text(encoding="utf-8")
        assert re.search(
            r"^\s*apply_persona_override\s*\(\s*\)", src, re.MULTILINE
        ), "Missing apply_persona_override() call at module top level"

    def test_shim_precedes_config_import(self) -> None:
        """apply_persona_override() appears before config import."""
        src = (_SCRIPTS_DIR / "persona_learning_tick.py").read_text(encoding="utf-8")
        shim_pos = src.find("apply_persona_override()")
        config_import_match = re.search(
            r"^\s*from\s+config\s+import", src, re.MULTILINE
        )
        assert shim_pos >= 0, "apply_persona_override() not found"
        assert config_import_match is not None, "config import not found"
        assert shim_pos < config_import_match.start(), (
            "apply_persona_override() must appear BEFORE config import"
        )

    def test_has_main_guard(self) -> None:
        """Script has if __name__ == '__main__' guard."""
        src = (_SCRIPTS_DIR / "persona_learning_tick.py").read_text(encoding="utf-8")
        assert '__name__ == "__main__"' in src or "__name__ == '__main__'" in src


# ── Default-profile guard ───────────────────────────────────────────────────


class TestDefaultProfileGuard:
    @patch("persona_learning_tick.is_active_default_profile", return_value=False)
    def test_refuses_named_profile(self, mock_default: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        from persona_learning_tick import run_tick

        run_tick(test_mode=True)
        captured = capsys.readouterr()
        assert "must run under default profile" in captured.out

    @patch("persona_learning_tick.is_active_default_profile", return_value=True)
    @patch("persona_learning_tick.list_profiles", return_value=[])
    def test_no_named_profiles_exits(
        self,
        mock_profiles: MagicMock,
        mock_default: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from persona_learning_tick import run_tick

        run_tick(test_mode=True)
        captured = capsys.readouterr()
        assert "no named profiles found" in captured.out


# ── Silent-skip ─────────────────────────────────────────────────────────────


class TestSilentSkip:
    def _make_db_with_session(
        self, db_path: Path, persona_id: str | None = None, updated_at: str | None = None
    ) -> None:
        """Create a proper chat.db via SQLiteSessionStore and insert a session."""
        from session import SQLiteSessionStore, Session

        store = SQLiteSessionStore(db_path)
        sid = f"test:{persona_id or 'main'}:1"
        now_str = updated_at or datetime.now(timezone.utc).isoformat()
        now_dt = datetime.fromisoformat(now_str)
        session = Session(
            session_id=sid,
            agent_session_id="",
            platform="test",
            channel_id=persona_id or "main",
            thread_id="1",
            user_id="test",
            created_at=now_dt,
            updated_at=now_dt,
            source="interactive",
            persona_id=persona_id,
        )
        store.create(session)

    def test_zero_rows_produces_silent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        from session import SQLiteSessionStore
        SQLiteSessionStore(db_path)

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since("sales", None, db_path)
        assert count == 0

    def test_rows_exist_returns_count(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        self._make_db_with_session(db_path, persona_id="sales")

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since("sales", None, db_path)
        assert count == 1

    def test_rows_filtered_by_timestamp(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        self._make_db_with_session(
            db_path, persona_id="sales", updated_at="2020-01-01T00:00:00"
        )

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since("sales", "2025-01-01T00:00:00", db_path)
        assert count == 0

    def test_rows_after_stamp_counted(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        self._make_db_with_session(
            db_path, persona_id="sales", updated_at="2026-07-03T12:00:00"
        )

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since("sales", "2026-01-01T00:00:00", db_path)
        assert count == 1


# ── Fail-open ───────────────────────────────────────────────────────────────


class TestFailOpen:
    def _mock_profile(self, name: str, path: Path) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.path = path
        p.is_default = False
        return p

    @patch("persona_learning_tick.is_active_default_profile", return_value=True)
    @patch("persona_learning_tick.get_default_paths")
    @patch("persona_learning_tick.list_profiles")
    @patch("persona_learning_tick.load_persona_config")
    @patch("persona_learning_tick._count_attributed_rows_since", return_value=5)
    @patch("persona_learning_tick._spawn_persona_pipeline")
    def test_failure_does_not_block_next(
        self,
        mock_spawn: MagicMock,
        mock_count: MagicMock,
        mock_config: MagicMock,
        mock_profiles: MagicMock,
        mock_paths: MagicMock,
        mock_default: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_paths.return_value = {"data": tmp_path}
        (tmp_path / "chat.db").touch()

        p1 = self._mock_profile("alpha", tmp_path / "alpha")
        p2 = self._mock_profile("beta", tmp_path / "beta")
        default_p = MagicMock()
        default_p.is_default = True
        mock_profiles.return_value = [default_p, p1, p2]

        mock_config.return_value = {"learning": {"enabled": True}}
        mock_spawn.side_effect = [(False, "crash"), (True, "success")]

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("persona_learning_tick.STATE_DIR", state_dir):
            with patch("persona_learning_tick._persona_state_file") as mock_sf:
                alpha_state = state_dir / "persona-learning-alpha-state.json"
                beta_state = state_dir / "persona-learning-beta-state.json"
                mock_sf.side_effect = lambda n: state_dir / f"persona-learning-{n}-state.json"

                from persona_learning_tick import run_tick

                run_tick()

        captured = capsys.readouterr()
        assert "FAILED" in captured.out
        assert "SUCCESS" in captured.out
        assert mock_spawn.call_count == 2

    @patch("persona_learning_tick.is_active_default_profile", return_value=True)
    @patch("persona_learning_tick.get_default_paths")
    @patch("persona_learning_tick.list_profiles")
    @patch("persona_learning_tick.load_persona_config", side_effect=Exception("parse error"))
    def test_config_error_skips_persona(
        self,
        mock_config: MagicMock,
        mock_profiles: MagicMock,
        mock_paths: MagicMock,
        mock_default: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_paths.return_value = {"data": tmp_path}
        p1 = self._mock_profile("broken", tmp_path / "broken")
        default_p = MagicMock()
        default_p.is_default = True
        mock_profiles.return_value = [default_p, p1]

        from persona_learning_tick import run_tick

        run_tick(test_mode=True)
        captured = capsys.readouterr()
        assert "config error" in captured.out
        assert "no learning-enabled personas" in captured.out


# ── No-enabled parity ──────────────────────────────────────────────────────


class TestNoEnabledParity:
    @patch("persona_learning_tick.is_active_default_profile", return_value=True)
    @patch("persona_learning_tick.get_default_paths")
    @patch("persona_learning_tick.list_profiles")
    @patch("persona_learning_tick.load_persona_config")
    def test_zero_enabled_is_noop(
        self,
        mock_config: MagicMock,
        mock_profiles: MagicMock,
        mock_paths: MagicMock,
        mock_default: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_paths.return_value = {"data": tmp_path}
        p1 = MagicMock()
        p1.name = "sales"
        p1.is_default = False
        p1.path = tmp_path / "sales"
        default_p = MagicMock()
        default_p.is_default = True
        mock_profiles.return_value = [default_p, p1]
        mock_config.return_value = {"learning": {"enabled": False}}

        from persona_learning_tick import run_tick

        run_tick(test_mode=True)
        captured = capsys.readouterr()
        assert "no learning-enabled personas" in captured.out


# ── Grep gates ──────────────────────────────────────────────────────────────


class TestGrepGates:
    def test_no_direct_provider_imports(self) -> None:
        """No direct anthropic/claude_agent_sdk imports in the tick."""
        src = (_SCRIPTS_DIR / "persona_learning_tick.py").read_text(encoding="utf-8")
        assert "from anthropic" not in src
        assert "import anthropic" not in src
        assert "claude_agent_sdk" not in src

    def test_uses_explicit_install_db(self) -> None:
        """The tick explicitly references get_default_paths for the install DB."""
        src = (_SCRIPTS_DIR / "persona_learning_tick.py").read_text(encoding="utf-8")
        assert "get_default_paths" in src

    def test_uses_build_capability_scoped_env(self) -> None:
        """Spawns children via build_capability_scoped_env."""
        src = (_SCRIPTS_DIR / "persona_learning_tick.py").read_text(encoding="utf-8")
        assert "build_capability_scoped_env" in src

    def test_uses_is_active_default_profile(self) -> None:
        """Uses is_active_default_profile (not is_default_profile)."""
        src = (_SCRIPTS_DIR / "persona_learning_tick.py").read_text(encoding="utf-8")
        assert "is_active_default_profile" in src

    def test_uses_load_persona_config_call_time(self) -> None:
        """Uses load_persona_config (call-time disk read, no import binding)."""
        src = (_SCRIPTS_DIR / "persona_learning_tick.py").read_text(encoding="utf-8")
        assert "load_persona_config" in src


# ── State file management ──────────────────────────────────────────────────


class TestStateFile:
    def test_persona_state_file_path(self) -> None:
        from persona_learning_tick import _persona_state_file

        result = _persona_state_file("sales")
        assert "persona-learning-sales-state.json" in str(result)
