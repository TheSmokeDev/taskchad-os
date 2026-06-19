"""Research stage — content enrichment and validation.

Takes triaged signal items and enriches them with additional context
from source URLs (full-text extraction) and validates relevance.
Bridges the triage and analysis stages.
"""

from __future__ import annotations

import asyncio
import logging

from business_signal.models import SignalItem

logger = logging.getLogger(__name__)


async def research_items(items: list[SignalItem]) -> list[SignalItem]:
    """Enrich triaged items with full-text content from source URLs.

    Returns enriched items (mutated in place). If URL fetch fails,
    items are returned unchanged. Non-blocking: individual fetch
    failures don't stop the pipeline.
    """
    if not items:
        return items

    for item in items:
        try:
            enriched = await _enrich_item(item)
            if enriched:
                item.summary = enriched[:500]
        except Exception as exc:
            logger.debug("research: item enrichment failed for %s: %s", item.url, exc)

    return items


async def _enrich_item(item: SignalItem) -> str | None:
    """Fetch and extract full-text content from item URL.

    Uses url_fetch.fetch() (sync, trafilatura + Firecrawl fallback)
    via asyncio.to_thread for non-blocking execution.
    Returns the extracted markdown (first ~500 chars) or None on failure.
    """
    if not item.url:
        return None

    try:
        from url_fetch import fetch as _sync_fetch

        result = await asyncio.to_thread(_sync_fetch, item.url)
        if result and result.markdown:
            return result.markdown[:500]
    except ImportError:
        logger.debug("research: url_fetch not available")
    except Exception as exc:
        logger.debug("research: fetch failed for %s: %s", item.url, exc)

    return None
