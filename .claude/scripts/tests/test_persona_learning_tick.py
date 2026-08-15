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


# ── Eligibility check (issue #422) ───────────────────────────────────────────


class TestIsLearningEligible:
    """Unit coverage for the extracted admission check every creation-door
    regression test (CLI, dashboard, lifecycle) drives against a real
    newborn config — see ``tests/test_persona_creation_surfaces.py``,
    ``tests/test_dashboard_api.py``, and ``tests/test_persona_lifecycle.py``.
    """

    def test_enabled_true_is_eligible(self) -> None:
        from persona_learning_tick import is_learning_eligible

        assert is_learning_eligible({"learning": {"enabled": True}}) is True

    def test_enabled_false_is_ineligible(self) -> None:
        from persona_learning_tick import is_learning_eligible

        assert is_learning_eligible({"learning": {"enabled": False}}) is False

    def test_absent_learning_key_is_ineligible(self) -> None:
        from persona_learning_tick import is_learning_eligible

        assert is_learning_eligible({}) is False

    def test_non_dict_learning_is_ineligible(self) -> None:
        from persona_learning_tick import is_learning_eligible

        assert is_learning_eligible({"learning": "oops"}) is False


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


# ── Composed gate: chat rows OR fresh notes (issue #425) ───────────────────


def _seed_note(profile_root: Path, rel: str, *, age_hours: float = 1.0) -> Path:
    """Write a note under the profile's memory tree with a controlled mtime."""
    path = profile_root / "memory" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntags: [system, persona, experience]\n---\n"
        "# Experience Notes\n\n## 09:14 - a-1 (build -> done)\n\n- Outcome: shipped\n",
        encoding="utf-8",
    )
    when = (datetime.now() - timedelta(hours=age_hours)).timestamp()
    os.utime(path, (when, when))
    return path


class TestFreshNoteCount:
    def test_counts_notes_newer_than_stamp(self, tmp_path: Path) -> None:
        from persona_learning_tick import _count_fresh_notes_since

        _seed_note(tmp_path, "experience/2026-08-13.md", age_hours=1)
        _seed_note(tmp_path, "market/2026-08-13.md", age_hours=2)
        stamp = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()

        count = _count_fresh_notes_since(
            "crypto", stamp, tmp_path, silent_skip_window_hours=24.0
        )

        assert count == 2

    def test_notes_older_than_stamp_are_not_counted(self, tmp_path: Path) -> None:
        from persona_learning_tick import _count_fresh_notes_since

        _seed_note(tmp_path, "experience/old.md", age_hours=48)
        stamp = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()

        count = _count_fresh_notes_since(
            "crypto", stamp, tmp_path, silent_skip_window_hours=24.0
        )

        assert count == 0

    def test_cold_start_uses_the_silent_skip_window(self, tmp_path: Path) -> None:
        """No stamp: the notes half shares the row half's cold-start boundary,
        so a first-ever tick cannot distil an unbounded backlog."""
        from persona_learning_tick import _count_fresh_notes_since

        _seed_note(tmp_path, "experience/recent.md", age_hours=2)
        _seed_note(tmp_path, "experience/ancient.md", age_hours=100)

        assert (
            _count_fresh_notes_since(
                "crypto", None, tmp_path, silent_skip_window_hours=24.0
            )
            == 1
        )

    def test_dirs_outside_the_registry_are_not_counted(self, tmp_path: Path) -> None:
        """episodes/ and daily/ have their own consumers — counting them would
        trigger a distillation on a corpus this gate does not feed."""
        from persona_learning_tick import _count_fresh_notes_since

        _seed_note(tmp_path, "episodes/2026-08-13-x.md", age_hours=1)
        _seed_note(tmp_path, "daily/2026-08-13.md", age_hours=1)

        count = _count_fresh_notes_since(
            "crypto", None, tmp_path, silent_skip_window_hours=24.0
        )

        assert count == 0

    def test_fail_open_on_internal_exception(self, tmp_path: Path) -> None:
        from persona_learning_tick import _count_fresh_notes_since

        with patch(
            "personas.experience.count_fresh_notes", side_effect=RuntimeError("boom")
        ):
            count = _count_fresh_notes_since(
                "crypto", None, tmp_path, silent_skip_window_hours=24.0
            )

        assert count == 0


class TestSharedBoundary:
    def test_stamp_wins_over_window(self) -> None:
        from persona_learning_tick import _resolve_since_boundary

        stamp = "2026-08-12T00:00:00"

        assert _resolve_since_boundary(
            stamp, silent_skip_window_hours=24.0
        ) == datetime(2026, 8, 12, 0, 0, 0)

    def test_absent_and_corrupt_stamps_share_the_window_fallback(self) -> None:
        from persona_learning_tick import _resolve_since_boundary

        absent = _resolve_since_boundary(None, silent_skip_window_hours=24.0)
        corrupt = _resolve_since_boundary(
            "not-a-timestamp", silent_skip_window_hours=24.0
        )

        assert abs((absent - corrupt).total_seconds()) < 5
        assert timedelta(hours=23) < (datetime.now() - absent) < timedelta(hours=25)

    def test_rows_and_notes_halves_share_one_boundary(self, tmp_path: Path) -> None:
        """The two halves must never disagree about 'fresh': a note written at
        the same instant as the boundary the row count used is excluded by
        both, not one."""
        from persona_learning_tick import (
            _count_fresh_notes_since,
            _resolve_since_boundary,
        )

        boundary = _resolve_since_boundary(None, silent_skip_window_hours=24.0)
        note = _seed_note(tmp_path, "experience/edge.md")
        os.utime(note, (boundary.timestamp(), boundary.timestamp()))

        assert (
            _count_fresh_notes_since(
                "crypto", None, tmp_path, silent_skip_window_hours=24.0
            )
            == 0
        )


class TestComposedGate:
    """The ticket's headline behaviour change, driven through run_tick."""

    def _run(
        self,
        tmp_path: Path,
        *,
        row_count: int,
        seed_notes: bool,
        test_mode: bool = False,
    ):
        profile_root = tmp_path / "crypto"
        profile_root.mkdir()
        if seed_notes:
            _seed_note(profile_root, "market/2026-08-13.md", age_hours=1)

        install = tmp_path / "install"
        install.mkdir()
        (install / "chat.db").touch()

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        p = MagicMock()
        p.name = "crypto"
        p.path = profile_root
        p.is_default = False
        default_p = MagicMock()
        default_p.is_default = True

        spawn = MagicMock(return_value=(True, "success"))

        with patch("persona_learning_tick.is_active_default_profile", return_value=True), \
             patch("persona_learning_tick.get_default_paths", return_value={"data": install}), \
             patch("persona_learning_tick.list_profiles", return_value=[default_p, p]), \
             patch("persona_learning_tick.load_persona_config",
                   return_value={"learning": {"enabled": True}}), \
             patch("persona_learning_tick._count_attributed_rows_since",
                   return_value=row_count), \
             patch("persona_learning_tick._spawn_persona_pipeline", spawn), \
             patch("persona_learning_tick.STATE_DIR", state_dir), \
             patch("persona_learning_tick._persona_state_file",
                   side_effect=lambda n: state_dir / f"persona-learning-{n}-state.json"):
            import io
            from contextlib import redirect_stdout

            from persona_learning_tick import run_tick

            buf = io.StringIO()
            with redirect_stdout(buf):
                run_tick(test_mode=test_mode)
        return spawn, buf.getvalue(), state_dir / "persona-learning-crypto-state.json"

    def test_fresh_notes_with_zero_chat_rows_no_longer_silent_skipped(
        self, tmp_path: Path
    ) -> None:
        """FAIL-WITHOUT-FIX lock. A persona whose work leaves NOTES but who
        never held a chat turn (worktick assignments, crypto market rounds)
        used to be skipped forever with a rich corpus unread on disk."""
        spawn, out, state_file = self._run(tmp_path, row_count=0, seed_notes=True)

        assert "PERSONA_REFLECT_SILENT" not in out
        assert "0 attributed rows, 1 fresh notes" in out
        assert spawn.call_count == 1
        assert json.loads(state_file.read_text(encoding="utf-8"))["notes_found"] == 1

    def test_no_rows_and_no_notes_still_silent_with_no_spawn(
        self, tmp_path: Path
    ) -> None:
        """The gate only ADDS a trigger — the zero-cost skip is preserved."""
        spawn, out, _state = self._run(tmp_path, row_count=0, seed_notes=False)

        assert "PERSONA_REFLECT_SILENT" in out
        assert "0 new rows, 0 fresh notes" in out
        assert spawn.call_count == 0

    def test_chat_rows_alone_still_trigger(self, tmp_path: Path) -> None:
        spawn, out, _state = self._run(tmp_path, row_count=3, seed_notes=False)

        assert "3 attributed rows, 0 fresh notes" in out
        assert spawn.call_count == 1

    def test_child_receives_the_same_boundary_the_gate_used(
        self, tmp_path: Path
    ) -> None:
        """Parent and child STATE_DIRs differ — the child cannot read the
        parent's last_run stamp, so the boundary MUST be threaded explicitly or
        the child distils notes the gate never counted."""
        from persona_learning_tick import _resolve_since_boundary

        spawn, _out, _state = self._run(tmp_path, row_count=0, seed_notes=True)

        notes_since = spawn.call_args.kwargs["notes_since"]
        expected = _resolve_since_boundary(None, silent_skip_window_hours=24.0)

        assert notes_since, "--notes-since boundary was not passed to the child"
        assert abs(
            (datetime.fromisoformat(notes_since) - expected).total_seconds()
        ) < 5

    def test_notes_since_reaches_the_child_argv(self, tmp_path: Path) -> None:
        """The boundary is a real CLI arg on the spawned memory_reflect.py."""
        import persona_learning_tick as tick

        captured: dict[str, list[str]] = {}

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _Result()

        with patch.object(tick.subprocess, "run", _fake_run), \
             patch.object(tick, "build_capability_scoped_env", return_value={}):
            ok, msg = tick._spawn_persona_pipeline(
                "crypto", tmp_path, notes_since="2026-08-12T00:00:00"
            )

        assert ok, msg
        cmd = captured["cmd"]
        assert "--notes-since" in cmd
        assert cmd[cmd.index("--notes-since") + 1] == "2026-08-12T00:00:00"
        assert cmd[cmd.index("-p") + 1] == "crypto"

    def test_absent_boundary_omits_the_flag(self, tmp_path: Path) -> None:
        """No boundary -> no flag, so the child falls back to its configured
        window instead of receiving an empty string."""
        import persona_learning_tick as tick

        captured: dict[str, list[str]] = {}

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _Result()

        with patch.object(tick.subprocess, "run", _fake_run), \
             patch.object(tick, "build_capability_scoped_env", return_value={}):
            tick._spawn_persona_pipeline("crypto", tmp_path, notes_since=None)

        assert "--notes-since" not in captured["cmd"]


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


# ── Reconcile round: shared boundary reaches BOTH counters (MAJOR) ─────────


class TestSharedBoundaryReachesBothCounters:
    """FAIL-WITHOUT-FIX lock. run_tick used to pass the RAW last_run stamp
    to both counters; each one independently recomputes its own fallback
    boundary via _resolve_since_boundary, so on a cold start (last_run=None)
    or a corrupted stamp the two calls to datetime.now() land at different
    instants. The fix threads the ALREADY-RESOLVED boundary (notes_since)
    into both calls instead."""

    def test_both_counters_receive_the_identical_resolved_boundary(
        self, tmp_path: Path
    ) -> None:
        import persona_learning_tick as tick

        profile_root = tmp_path / "crypto"
        profile_root.mkdir()
        install = tmp_path / "install"
        install.mkdir()
        (install / "chat.db").touch()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        p = MagicMock()
        p.name = "crypto"
        p.path = profile_root
        p.is_default = False
        default_p = MagicMock()
        default_p.is_default = True

        real_rows = tick._count_attributed_rows_since
        real_notes = tick._count_fresh_notes_since
        captured: dict[str, list] = {"rows": [], "notes": []}

        def _spy_rows(persona_id, since_iso, *a, **k):
            captured["rows"].append(since_iso)
            return real_rows(persona_id, since_iso, *a, **k)

        def _spy_notes(persona_id, since_iso, *a, **k):
            captured["notes"].append(since_iso)
            return real_notes(persona_id, since_iso, *a, **k)

        with patch("persona_learning_tick.is_active_default_profile", return_value=True), \
             patch("persona_learning_tick.get_default_paths", return_value={"data": install}), \
             patch("persona_learning_tick.list_profiles", return_value=[default_p, p]), \
             patch("persona_learning_tick.load_persona_config",
                   return_value={"learning": {"enabled": True}}), \
             patch("persona_learning_tick._count_attributed_rows_since", _spy_rows), \
             patch("persona_learning_tick._count_fresh_notes_since", _spy_notes), \
             patch("persona_learning_tick._spawn_persona_pipeline",
                   return_value=(True, "success")), \
             patch("persona_learning_tick.STATE_DIR", state_dir), \
             patch("persona_learning_tick._persona_state_file",
                   side_effect=lambda n: state_dir / f"persona-learning-{n}-state.json"):
            tick.run_tick()

        assert captured["rows"], "row counter was never called"
        assert captured["notes"], "note counter was never called"
        assert captured["rows"][0] is not None, (
            "counter received the raw (None) last_run instead of the "
            "already-resolved boundary — the cold-start case the bug hit"
        )
        assert captured["rows"][0] == captured["notes"][0], (
            "row and note counters received DIFFERENT since_iso boundaries — "
            f"rows={captured['rows'][0]!r} notes={captured['notes'][0]!r}"
        )


# ── Reconcile round: dry-run must not advance the watermark (MAJOR) ────────


class TestDryRunDoesNotAdvanceWatermark:
    """A --test tick must report what it saw without mutating persistent
    state — writing last_run here would make a subsequent REAL tick treat
    the dry run's timestamp as the last real run, silently and permanently
    skipping notes/rows the dry run only reported on."""

    def test_test_mode_leaves_no_state_file(self, tmp_path: Path) -> None:
        spawn, out, state_file = TestComposedGate()._run(
            tmp_path, row_count=0, seed_notes=True, test_mode=True
        )

        assert spawn.call_count == 0
        assert "--test mode" in out
        assert not state_file.exists(), (
            "a --test run wrote a persona-learning state file — the "
            "production watermark was advanced by a dry run"
        )


# ── Reconcile round: fail-open handlers survive a hostile __str__ (MINOR) ──


class _HostileStrError(Exception):
    """An exception whose ``__str__`` itself raises — the pathological case
    a plain ``f"...{exc}..."`` cannot survive."""

    def __str__(self) -> str:
        raise RuntimeError("str() itself raises")


class TestFailOpenSurvivesHostileExceptionStr:
    def test_count_fresh_notes_survives_hostile_exception_str(
        self, tmp_path: Path
    ) -> None:
        from persona_learning_tick import _count_fresh_notes_since

        with patch(
            "personas.experience.count_fresh_notes",
            side_effect=_HostileStrError("boom"),
        ):
            count = _count_fresh_notes_since(
                "crypto", None, tmp_path, silent_skip_window_hours=24.0
            )
        assert count == 0

    def test_count_attributed_rows_survives_hostile_exception_str(
        self, tmp_path: Path
    ) -> None:
        from persona_learning_tick import _count_attributed_rows_since

        with patch(
            "persona_learning_tick.get_session_store",
            side_effect=_HostileStrError("boom"),
        ):
            count = _count_attributed_rows_since(
                "sales", None, tmp_path / "chat.db", silent_skip_window_hours=24.0
            )
        assert count == 0
