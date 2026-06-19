"""Tests for business_signal.signal_engine — pipeline integration, state, status."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_signal.config import SignalSettings  # noqa: E402
from business_signal.models import SignalDigest, SignalItem  # noqa: E402
from business_signal.signal_engine import _save_run_state, get_latest_status  # noqa: E402


def _make_settings(**overrides) -> SignalSettings:
    defaults = dict(
        enabled=True,
        triage_threshold=0.3,
        max_items_per_run=30,
        draft_threshold=0.7,
        rss_feeds=["https://test.com/feed"],
    )
    defaults.update(overrides)
    return SignalSettings(**defaults)


class TestSaveRunState:
    def test_saves_to_state_file(self, tmp_path):
        state_file = tmp_path / "signal-state.json"
        state_file.write_text("{}", encoding="utf-8")
        with (
            patch("business_signal.signal_engine.load_state", side_effect=lambda f: {}),
            patch("business_signal.signal_engine.save_state") as mock_save,
            patch("business_signal.signal_engine.SIGNAL_STATE_FILE", state_file),
        ):
            _save_run_state("2026-06-17T00:00:00Z", "success", 5, 2)
            assert mock_save.called
            saved = mock_save.call_args[0][0]
            assert saved["last_run"] == "2026-06-17T00:00:00Z"
            assert saved["last_result"] == "success"
            assert saved["items_count"] == 5
            assert saved["drafts_count"] == 2


class TestGetLatestStatus:
    def test_no_previous_run(self):
        with patch("business_signal.signal_engine.load_state", return_value={}):
            result = get_latest_status()
            assert "has not run yet" in result

    def test_with_previous_run(self):
        state = {
            "last_run": "2026-06-17T00:00:00Z",
            "last_result": "success",
            "items_count": 10,
            "drafts_count": 3,
        }
        with patch("business_signal.signal_engine.load_state", return_value=state):
            result = get_latest_status()
            assert "success" in result
            assert "10" in result
            assert "3" in result

    def test_silent_result(self):
        state = {
            "last_run": "2026-06-17T00:00:00Z",
            "last_result": "silent",
            "items_count": 0,
            "drafts_count": 0,
        }
        with patch("business_signal.signal_engine.load_state", return_value=state):
            result = get_latest_status()
            assert "silent" in result


class TestRunPipeline:
    @pytest.mark.asyncio
    async def test_disabled_returns_disabled(self):
        settings = _make_settings(enabled=False)
        with (
            patch("business_signal.signal_engine.get_signal_settings", return_value=settings),
        ):
            from business_signal.signal_engine import run_signal_engine

            result = await run_signal_engine(test_mode=True)
            assert result == "disabled"

    @pytest.mark.asyncio
    async def test_empty_fetch_returns_silent(self, tmp_path):
        state_file = tmp_path / "signal-state.json"
        state_file.write_text("{}", encoding="utf-8")
        settings = _make_settings()

        mock_registry = MagicMock()
        mock_registry.fetch_all = AsyncMock(return_value=([], 3, 0))

        with (
            patch("business_signal.signal_engine.get_signal_settings", return_value=settings),
            patch("business_signal.signal_engine.SIGNAL_STATE_FILE", state_file),
            patch("business_signal.signal_engine.file_lock", MagicMock()),
            patch("business_signal.signal_engine.load_state", return_value={}),
            patch("business_signal.signal_engine.save_state"),
            patch("business_signal.signal_engine.append_to_daily_log"),
            patch("business_signal.fetchers.default_registry", return_value=mock_registry),
            patch("business_signal.triage.triage_items", return_value=[]),
        ):
            from business_signal.signal_engine import _run_pipeline

            result = await _run_pipeline(test_mode=False, days=7)
            assert result == "SIGNAL_SILENT"

    @pytest.mark.asyncio
    async def test_test_mode_skips_llm_and_output(self, tmp_path):
        state_file = tmp_path / "signal-state.json"
        state_file.write_text("{}", encoding="utf-8")
        settings = _make_settings()

        items = [
            SignalItem(
                source="rss:hn",
                title="Test",
                url="http://a",
                summary="test",
                relevance_score=0.8,
                tags=["ai"],
            ),
        ]

        mock_registry = MagicMock()
        mock_registry.fetch_all = AsyncMock(return_value=(items, 1, 0))

        with (
            patch("business_signal.signal_engine.get_signal_settings", return_value=settings),
            patch("business_signal.signal_engine.SIGNAL_STATE_FILE", state_file),
            patch("business_signal.signal_engine.file_lock", MagicMock()),
            patch("business_signal.signal_engine.load_state", return_value={}),
            patch("business_signal.signal_engine.save_state"),
            patch("business_signal.fetchers.default_registry", return_value=mock_registry),
            patch("business_signal.triage.triage_items", return_value=items),
        ):
            from business_signal.signal_engine import _run_pipeline

            result = await _run_pipeline(test_mode=True, days=7)
            assert result == "success"
