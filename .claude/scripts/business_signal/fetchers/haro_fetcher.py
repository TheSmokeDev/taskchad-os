"""HARO email scanner — extracts relevant journalist queries from Outlook.

Refactored from ``heartbeat.py:396-585``. The heartbeat still owns the
pitch-drafting LLM pass; this fetcher only handles the email scanning and
keyword-matching stage, returning ``SignalItem`` instances.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from business_signal.models import SignalItem

logger = logging.getLogger(__name__)

INSURANCE_KEYWORDS: list[str] = [
    "insurance", "auto", "car", "vehicle", "driver", "coverage",
    "policy", "premium", "deductible", "liability", "sr-22",
    "finance", "loan", "mortgage", "credit", "debt", "savings",
    "accident", "claim", "personal finance", "budget",
]

AI_TECH_KEYWORDS: list[str] = [
    "artificial intelligence", "machine learning", "automation",
    "startup", "entrepreneur", "founder", "small business", "insurtech",
    "fintech", "saas", "technology", "software",
]


class HAROFetcher:
    """Fetch signal items from HARO emails via Outlook/Graph integration."""

    @property
    def name(self) -> str:
        return "haro"

    async def fetch(self) -> list[SignalItem]:
        return scan_haro_emails()


def scan_haro_emails(
    max_results: int = 5,
    hours_ago: int = 4,
) -> list[SignalItem]:
    """Scan recent HARO emails and return matched queries as SignalItems.

    This is the pure-Python extraction pass (zero LLM cost). The heartbeat
    can import and call this directly instead of duplicating the keyword
    matching logic.
    """
    try:
        from integrations.outlook import get_email_body, list_emails
    except ImportError:
        logger.info("Outlook integration not available — HARO fetcher skipped")
        return []

    try:
        from integrations.outlook import is_configured

        if not is_configured():
            logger.info("Outlook not configured — HARO fetcher skipped")
            return []
    except ImportError:
        pass

    try:
        emails = list_emails(max_results=max_results, hours_ago=hours_ago, unread_only=True)
    except Exception:
        logger.exception("Failed to list emails for HARO scan")
        return []

    haro_emails = [e for e in emails if "haro@helpareporter.com" in e.sender_email.lower()]
    if not haro_emails:
        logger.info("No HARO emails found")
        return []

    items: list[SignalItem] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    for email in haro_emails:
        try:
            body = get_email_body(email.id)
        except Exception:
            logger.exception("Failed to read HARO email body (non-fatal)")
            continue

        chunks = [q.strip() for q in body.split("\n\n") if q.strip() and len(q.strip()) > 40]
        for q_text in chunks:
            q_lower = q_text.lower()

            non_ascii = sum(1 for c in q_text if ord(c) > 127)
            if non_ascii / max(len(q_text), 1) > 0.15:
                continue
            if q_text.count("&") > 3:
                continue

            angle: str | None = None
            if any(kw in q_lower for kw in AI_TECH_KEYWORDS):
                angle = "ai"
            elif any(kw in q_lower for kw in INSURANCE_KEYWORDS):
                angle = "insurance"

            if angle is None:
                continue

            title_words = q_text.split()[:10]
            title = " ".join(title_words)
            if len(title) > 80:
                title = title[:77] + "..."

            items.append(
                SignalItem(
                    source="haro",
                    title=title,
                    url=f"haro:email:{email.id}",
                    summary=q_text[:500],
                    fetched_at=fetched_at,
                    tags=[angle],
                )
            )

    logger.info(
        "HARO scan: %d email(s), %d relevant queries",
        len(haro_emails),
        len(items),
    )
    return items
