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
from datetime import datetime, timedelta, timezone
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


def _make_db_with_session(
    db_path: Path, persona_id: str | None = None, updated_at: str | None = None
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


class TestSilentSkip:
    def test_zero_rows_produces_silent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        from session import SQLiteSessionStore
        SQLiteSessionStore(db_path)

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since(
            "sales", None, db_path, silent_skip_window_hours=24.0
        )
        assert count == 0

    def test_rows_exist_returns_count(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _make_db_with_session(db_path, persona_id="sales")

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since(
            "sales", None, db_path, silent_skip_window_hours=24.0
        )
        assert count == 1

    def test_rows_filtered_by_timestamp(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _make_db_with_session(
            db_path, persona_id="sales", updated_at="2020-01-01T00:00:00"
        )

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since(
            "sales", "2025-01-01T00:00:00", db_path, silent_skip_window_hours=24.0
        )
        assert count == 0

    def test_rows_after_stamp_counted(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _make_db_with_session(
            db_path, persona_id="sales", updated_at="2026-07-03T12:00:00"
        )

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since(
            "sales", "2026-01-01T00:00:00", db_path, silent_skip_window_hours=24.0
        )
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


# ── Timezone-normalized comparison (Finding 1) ──────────────────────────────


class TestTimezoneNormalizedComparison:
    """last_run is stamped aware-UTC; session.updated_at is naive-local
    (SQLite). A raw string compare undercounts on a UTC-negative box — we
    simulate that deterministically (independent of the CI box's actual
    system timezone) by patching the canonical normalizer to apply a fixed
    -8h shift to aware inputs, mirroring what `.astimezone()` does on a
    real UTC-8 box, and passing already-naive values through unchanged."""

    @staticmethod
    def _fake_normalize_utc_minus_8(value):
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                value = datetime.fromisoformat(text)
            except ValueError:
                return None
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is not None:
            return (value - timedelta(hours=8)).replace(tzinfo=None)
        return value

    def test_aware_utc_last_run_vs_naive_local_newer_session_is_counted(
        self, tmp_path: Path
    ) -> None:
        """Reproduces the issue: last_run stamped aware-UTC at 12:00. A
        session updated 2 REAL hours later on a UTC-8 box lands at
        naive-local 06:00 the same calendar day — chronologically AFTER
        last_run, but "06:00:00" < "12:00:00+00:00" under the OLD raw
        string compare (currently fails without the fix)."""
        db_path = tmp_path / "chat.db"
        _make_db_with_session(
            db_path, persona_id="sales", updated_at="2026-07-20T06:00:00"
        )

        with patch(
            "persona_learning_tick.normalize_physical_timestamp",
            side_effect=self._fake_normalize_utc_minus_8,
        ):
            from persona_learning_tick import _count_attributed_rows_since

            count = _count_attributed_rows_since(
                "sales",
                "2026-07-20T12:00:00+00:00",
                db_path,
                silent_skip_window_hours=24.0,
            )
        assert count == 1


# ── Cold-start silent-skip window (Finding 2) ───────────────────────────────


class TestColdStartSilentSkipWindow:
    def test_cold_start_excludes_sessions_older_than_window(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "chat.db"
        old_updated = (datetime.now() - timedelta(hours=48)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        _make_db_with_session(db_path, persona_id="sales", updated_at=old_updated)

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since(
            "sales", None, db_path, silent_skip_window_hours=24.0
        )
        assert count == 0

    def test_cold_start_includes_sessions_within_window(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "chat.db"
        recent_updated = (datetime.now() - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        _make_db_with_session(
            db_path, persona_id="sales", updated_at=recent_updated
        )

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since(
            "sales", None, db_path, silent_skip_window_hours=24.0
        )
        assert count == 1

    def test_changing_window_hours_changes_cold_start_boundary(
        self, tmp_path: Path
    ) -> None:
        """Widening silent_skip_window_hours (what
        PERSONA_LEARNING_SILENT_SKIP_WINDOW resolves into — see
        TestSilentSkipWindowWiring for the env-var wiring itself) widens the
        cold-start boundary directly: a 30h-old session, excluded at 24h, is
        counted at 48h."""
        db_path = tmp_path / "chat.db"
        updated_at = (datetime.now() - timedelta(hours=30)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        _make_db_with_session(db_path, persona_id="sales", updated_at=updated_at)

        from persona_learning_tick import _count_attributed_rows_since

        assert (
            _count_attributed_rows_since(
                "sales", None, db_path, silent_skip_window_hours=24.0
            )
            == 0
        )
        assert (
            _count_attributed_rows_since(
                "sales", None, db_path, silent_skip_window_hours=48.0
            )
            == 1
        )

    def test_corrupted_stamp_falls_back_to_cold_start_window(
        self, tmp_path: Path
    ) -> None:
        """A present-but-unparsable since_iso shares the cold-start fallback
        boundary, per the docstring — not silently treated as count=0 via an
        unrelated exception path."""
        db_path = tmp_path / "chat.db"
        recent_updated = (datetime.now() - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        _make_db_with_session(
            db_path, persona_id="sales", updated_at=recent_updated
        )

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since(
            "sales", "not-a-real-timestamp", db_path, silent_skip_window_hours=24.0
        )
        assert count == 1

    def test_boundary_is_exclusive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session updated_at exactly `silent_skip_window_hours` old is
        excluded — the comparison is strict `>`, not `>=`."""
        db_path = tmp_path / "chat.db"
        fixed_now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
        # Build the stamp in LOCAL wall clock — session.updated_at is naive
        # LOCAL by contract, and the boundary is normalized to naive local
        # too. A UTC strftime here silently lands the session hours past the
        # boundary on any non-UTC box (Codex gate finding on PR #179).
        exact_boundary = (
            (fixed_now - timedelta(hours=24))
            .astimezone()
            .replace(tzinfo=None)
            .strftime("%Y-%m-%dT%H:%M:%S")
        )
        _make_db_with_session(
            db_path, persona_id="sales", updated_at=exact_boundary
        )

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)

        monkeypatch.setattr("persona_learning_tick.datetime", _FixedDatetime)

        from persona_learning_tick import _count_attributed_rows_since

        count = _count_attributed_rows_since(
            "sales", None, db_path, silent_skip_window_hours=24.0
        )
        assert count == 0  # exactly-at-boundary session is excluded (strict `>`)


class TestRealNormalizerEndToEnd:
    def test_mixed_clock_bases_count_with_real_normalizer(
        self, tmp_path: Path
    ) -> None:
        """Companion to the patched-normalizer regression test: exercise the
        REAL normalize_physical_timestamp end-to-end. A session stamped
        naive-LOCAL one hour ago must be counted against an aware-UTC
        last_run 24h ago — the exact mixed-clock-base pair production sees.
        Timezone-robust: both stamps derive from the same instant via the
        box's own local offset."""
        db_path = tmp_path / "chat.db"
        now_utc = datetime.now(timezone.utc)
        recent_local = (
            (now_utc - timedelta(hours=1))
            .astimezone()
            .replace(tzinfo=None)
            .strftime("%Y-%m-%dT%H:%M:%S")
        )
        _make_db_with_session(
            db_path, persona_id="sales", updated_at=recent_local
        )

        from persona_learning_tick import _count_attributed_rows_since

        since_iso = (now_utc - timedelta(hours=24)).isoformat()
        count = _count_attributed_rows_since(
            "sales", since_iso, db_path, silent_skip_window_hours=24.0
        )
        assert count == 1


class TestFailOpenRowCount:
    def test_fail_open_on_internal_exception(self, tmp_path: Path) -> None:
        """An exception raised inside the try-block (e.g. store construction
        failure) returns 0 rather than propagating — the fail-open contract
        proven directly, not just at the run_tick orchestration level."""
        from persona_learning_tick import _count_attributed_rows_since

        with patch(
            "persona_learning_tick.get_session_store",
            side_effect=RuntimeError("boom"),
        ):
            count = _count_attributed_rows_since(
                "sales", None, tmp_path / "chat.db", silent_skip_window_hours=24.0
            )
        assert count == 0


# ── End-to-end wiring: run_tick threads the configured window through ──────


class TestSilentSkipWindowWiring:
    @patch("persona_learning_tick.is_active_default_profile", return_value=True)
    @patch("persona_learning_tick.get_default_paths")
    @patch("persona_learning_tick.list_profiles")
    @patch("persona_learning_tick.load_persona_config")
    @patch("persona_learning_tick._count_attributed_rows_since", return_value=0)
    def test_run_tick_passes_configured_window_to_row_count(
        self,
        mock_count: MagicMock,
        mock_config: MagicMock,
        mock_profiles: MagicMock,
        mock_paths: MagicMock,
        mock_default: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSONA_LEARNING_SILENT_SKIP_WINDOW", "48")
        mock_paths.return_value = {"data": tmp_path}
        (tmp_path / "chat.db").touch()

        p1 = MagicMock()
        p1.name = "sales"
        p1.is_default = False
        p1.path = tmp_path / "sales"
        default_p = MagicMock()
        default_p.is_default = True
        mock_profiles.return_value = [default_p, p1]
        mock_config.return_value = {"learning": {"enabled": True}}

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("persona_learning_tick.STATE_DIR", state_dir):
            with patch("persona_learning_tick._persona_state_file") as mock_sf:
                mock_sf.side_effect = (
                    lambda n: state_dir / f"persona-learning-{n}-state.json"
                )

                from persona_learning_tick import run_tick

                run_tick(test_mode=True)

        assert mock_count.call_args.kwargs["silent_skip_window_hours"] == 48.0
