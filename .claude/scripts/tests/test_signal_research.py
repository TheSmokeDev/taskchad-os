"""Tests for business_signal.research — URL enrichment stage."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_signal.models import SignalItem  # noqa: E402
from business_signal.research import _enrich_item, research_items  # noqa: E402


def _make_item(**overrides) -> SignalItem:
    defaults = dict(
        source="rss:hn",
        title="Test",
        url="http://example.com/article",
        summary="original summary",
        relevance_score=0.8,
        tags=["ai"],
    )
    defaults.update(overrides)
    return SignalItem(**defaults)


@dataclass
class _FakeFetchedContent:
    markdown: str


class TestResearchItems:
    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self):
        result = await research_items([])
        assert result == []

    @pytest.mark.asyncio
    async def test_enriches_summary_from_url(self):
        item = _make_item()
        fake_content = _FakeFetchedContent(markdown="Full article text about AI agents in insurance.")

        with patch("business_signal.research._sync_fetch", create=True):
            with patch(
                "business_signal.research.asyncio.to_thread",
                return_value=fake_content,
            ):
                result = await research_items([item])

        assert len(result) == 1
        assert result[0].summary == "Full article text about AI agents in insurance."

    @pytest.mark.asyncio
    async def test_enrichment_failure_preserves_original(self):
        item = _make_item(summary="keep this")

        with patch(
            "business_signal.research.asyncio.to_thread",
            side_effect=Exception("network error"),
        ):
            result = await research_items([item])

        assert len(result) == 1
        assert result[0].summary == "keep this"

    @pytest.mark.asyncio
    async def test_no_url_skips_enrichment(self):
        item = _make_item(url="")
        result = await research_items([item])
        assert result[0].summary == "original summary"

    @pytest.mark.asyncio
    async def test_empty_markdown_skips_enrichment(self):
        item = _make_item()
        fake_content = _FakeFetchedContent(markdown="")

        with patch(
            "business_signal.research.asyncio.to_thread",
            return_value=fake_content,
        ):
            result = await research_items([item])

        assert result[0].summary == "original summary"

    @pytest.mark.asyncio
    async def test_summary_capped_at_500(self):
        item = _make_item()
        long_text = "x" * 1000
        fake_content = _FakeFetchedContent(markdown=long_text)

        with patch(
            "business_signal.research.asyncio.to_thread",
            return_value=fake_content,
        ):
            result = await research_items([item])

        assert len(result[0].summary) == 500

    @pytest.mark.asyncio
    async def test_import_error_handled_gracefully(self):
        item = _make_item()

        with patch(
            "business_signal.research.asyncio.to_thread",
            side_effect=ImportError("url_fetch not available"),
        ):
            result = await research_items([item])

        assert result[0].summary == "original summary"


class TestEnrichItem:
    @pytest.mark.asyncio
    async def test_no_url_returns_none(self):
        item = _make_item(url="")
        result = await _enrich_item(item)
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        item = _make_item()
        fake_content = _FakeFetchedContent(markdown="Extracted content here.")

        with patch(
            "business_signal.research.asyncio.to_thread",
            return_value=fake_content,
        ):
            result = await _enrich_item(item)

        assert result == "Extracted content here."
