"""Tests for business_signal.output — digest writing, draft creation, daily log."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_signal.config import SignalSettings  # noqa: E402
from business_signal.models import SignalDigest, SignalItem  # noqa: E402
from business_signal.output import _create_drafts, _slugify, _write_digest  # noqa: E402


class TestSlugify:
    def test_basic(self):
        assert _slugify("AI Agents for Insurance!") == "ai-agents-for-insurance"

    def test_truncates(self):
        long = "a" * 100
        assert len(_slugify(long)) <= 40

    def test_strips_special(self):
        assert _slugify("Hello@World#2026") == "hello-world-2026"


class TestWriteDigest:
    def test_writes_with_frontmatter(self, tmp_path):
        with patch("business_signal.output.MEMORY_DIR", tmp_path, create=True):
            digest = SignalDigest(
                date="2026-06-17",
                items=[
                    SignalItem(
                        source="rss:hn",
                        title="Test Item",
                        url="http://a",
                        summary="test summary",
                        relevance_score=0.8,
                        tags=["ai agent"],
                    )
                ],
                sources_checked=3,
                sources_failed=0,
                total_fetched=10,
                total_triaged=1,
                markdown_body="Executive summary.",
            )
            path = _write_digest(digest)
            content = path.read_text(encoding="utf-8")
            assert "tags: [signal, business-intel, auto-generated]" in content
            assert "date: 2026-06-17" in content
            assert "sources_checked: 3" in content
            assert "items_triaged: 1" in content
            assert "Executive summary." in content
            assert "### 1. Test Item" in content

    def test_empty_digest_has_signal_items_header(self, tmp_path):
        with patch("business_signal.output.MEMORY_DIR", tmp_path, create=True):
            digest = SignalDigest(date="2026-06-17")
            path = _write_digest(digest)
            content = path.read_text(encoding="utf-8")
            assert "## Signal Items" in content

    def test_filename_is_business_signal_digest(self, tmp_path):
        with patch("business_signal.output.MEMORY_DIR", tmp_path, create=True):
            digest = SignalDigest(date="2026-06-17")
            path = _write_digest(digest)
            assert path.name == "BUSINESS_SIGNAL_DIGEST.md"


class TestCreateDrafts:
    def _make_settings(self, threshold: float = 0.7) -> SignalSettings:
        return SignalSettings(
            enabled=True,
            triage_threshold=0.3,
            max_items_per_run=30,
            draft_threshold=threshold,
            rss_feeds=[],
        )

    @pytest.mark.asyncio
    async def test_creates_draft_for_high_signal(self, tmp_path):
        drafts_dir = tmp_path / "drafts" / "active"
        settings = self._make_settings(threshold=0.7)
        with (
            patch("business_signal.output.get_signal_settings", return_value=settings),
            patch("business_signal.output.generate_draft_copy", new_callable=AsyncMock, return_value="AI is reshaping insurance — here's what founders should watch."),
            patch("business_signal.output.DRAFTS_ACTIVE_DIR", drafts_dir),
        ):
            digest = SignalDigest(
                date="2026-06-17",
                items=[
                    SignalItem(
                        source="rss:hn",
                        title="High Signal",
                        url="http://a",
                        summary="important",
                        relevance_score=0.8,
                        tags=["ai"],
                        content_angle="AI disruption angle",
                    ),
                ],
            )
            drafts = await _create_drafts(digest)
            assert len(drafts) == 1
            draft_path = drafts_dir / drafts[0]
            content = draft_path.read_text(encoding="utf-8")
            assert "tags: [draft, signal, content]" in content
            assert "status: draft" in content
            assert "AI disruption angle" in content
            assert "source_url: http://a" in content
            assert "AI is reshaping insurance" in content

    @pytest.mark.asyncio
    async def test_skips_low_signal(self, tmp_path):
        drafts_dir = tmp_path / "drafts" / "active"
        settings = self._make_settings(threshold=0.7)
        with (
            patch("business_signal.output.get_signal_settings", return_value=settings),
            patch("business_signal.output.DRAFTS_ACTIVE_DIR", drafts_dir),
        ):
            digest = SignalDigest(
                date="2026-06-17",
                items=[
                    SignalItem(
                        source="rss:tc",
                        title="Low Signal",
                        url="http://b",
                        summary="meh",
                        relevance_score=0.3,
                        tags=["startup"],
                    ),
                ],
            )
            drafts = await _create_drafts(digest)
            assert len(drafts) == 0

    @pytest.mark.asyncio
    async def test_no_duplicate_drafts(self, tmp_path):
        drafts_dir = tmp_path / "drafts" / "active"
        settings = self._make_settings(threshold=0.7)
        with (
            patch("business_signal.output.get_signal_settings", return_value=settings),
            patch("business_signal.output.generate_draft_copy", new_callable=AsyncMock, return_value="Draft copy here."),
            patch("business_signal.output.DRAFTS_ACTIVE_DIR", drafts_dir),
        ):
            digest = SignalDigest(
                date="2026-06-17",
                items=[
                    SignalItem(
                        source="rss:hn",
                        title="Unique",
                        url="http://a",
                        summary="test",
                        relevance_score=0.9,
                        tags=["ai"],
                    ),
                ],
            )
            drafts1 = await _create_drafts(digest)
            drafts2 = await _create_drafts(digest)
            assert len(drafts1) == 1
            assert len(drafts2) == 0  # already exists
