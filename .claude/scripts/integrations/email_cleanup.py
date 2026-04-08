"""Unified email cleanup orchestrator for Gmail + Outlook.

Categorizes junk, runs dry runs, executes cleanup across both inboxes.
All operations archive (never delete). Protected senders are never touched.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JunkCategory(Enum):
    PROMO = "Promos"
    SOCIAL = "Social"
    NEWSLETTER = "Newsletters"
    SCAM = "Scam/Phishing"


@dataclass
class EmailSummary:
    id: str
    sender: str
    sender_email: str
    subject: str
    snippet: str
    category: JunkCategory


@dataclass
class CleanupReport:
    source: str  # "Gmail" or "Outlook"
    by_category: dict[JunkCategory, list[EmailSummary]] = field(default_factory=dict)
    protected_count: int = 0
    total_scanned: int = 0


# ── Protected senders (never touch) ──────────────────────────────

def _load_env_set(*names: str) -> set[str]:
    values: set[str] = set()
    for name in names:
        raw = os.getenv(name, "")
        for item in raw.split(","):
            cleaned = item.strip().lower()
            if cleaned:
                values.add(cleaned)
    return values


PROTECTED_ADDRESSES = _load_env_set("PROTECTED_ADDRESSES")
PROTECTED_ADDRESSES |= {
    value.strip().lower()
    for value in (
        os.getenv("BUSINESS_EMAIL", ""),
        os.getenv("GRAPH_USER_EMAIL", ""),
        os.getenv("GOOGLE_CALENDAR_ID", ""),
        os.getenv("CIRCLE_MEMBER_EMAIL", ""),
    )
    if "@" in value
}

# Important senders — keep even if categorized as promo
DEFAULT_KEEP_DOMAINS = {
    "turbotax", "sdge", "capitalone", "stripe", "coinbase",
    "cash.app", "paypal", "chime", "irs.gov", "edd.ca.gov",
    "supabase", "vercel", "github", "cloudflare", "google.com",
    "hostinger", "sentry", "anthropic", "openai",
}
KEEP_DOMAINS = DEFAULT_KEEP_DOMAINS | _load_env_set("KEEP_DOMAINS")


def _is_protected(sender_email: str) -> bool:
    lower = sender_email.lower().strip()
    if lower in {a.lower() for a in PROTECTED_ADDRESSES}:
        return True
    return any(d in lower for d in KEEP_DOMAINS)


# ── Categorization engine ────────────────────────────────────────

SOCIAL_DOMAINS = {
    "facebook.com", "facebookmail.com", "linkedin.com", "twitter.com",
    "x.com", "instagram.com", "tiktok.com", "reddit.com", "discord.com",
    "pinterest.com", "snapchat.com", "youtube.com", "medium.com",
    "quora.com", "nextdoor.com",
}

PROMO_SENDER_PATTERNS = re.compile(
    r"(marketing|promo|deals|offers|sales|campaign|newsletter|digest|"
    r"noreply|no-reply|donotreply|notifications?|updates?|info@|hello@|"
    r"news@|announce|bulk|mass|blast)@",
    re.IGNORECASE,
)

PROMO_SUBJECT_PATTERNS = re.compile(
    r"(\b\d+%\s*off\b|flash sale|limited time|exclusive offer|"
    r"don.t miss|act now|hurry|last chance|free shipping|"
    r"save \$|deal of|black friday|cyber monday|clearance|"
    r"unsubscribe|weekly digest|daily digest)",
    re.IGNORECASE,
)

COLD_OUTREACH_PATTERNS = re.compile(
    r"(send.{0,10}screenshots?|check(ed)? your (site|website)|"
    r"i.ve been (looking|checking)|"
    r"quick question about your|boost your (seo|traffic|ranking)|"
    r"link.?building|guest.?post|backlink|"
    r"web.?authority|real.?growth|grow your|"
    r"google reviews.*permanent|rank.{0,10}(higher|first page))",
    re.IGNORECASE,
)

SCAM_PATTERNS = re.compile(
    r"(verify your (account|identity|email)|account (has been |was )?(suspended|compromised|locked)|"
    r"click here immediately|urgent.{0,10}action required|"
    r"you.ve (won|been selected)|lottery|inheritance|"
    r"wire transfer|western union|bitcoin.*send|"
    r"nigerian|prince|million dollars|"
    r"password.{0,10}expired|security alert.*click|"
    r"confirm your (payment|identity)|unusual (sign|activity))",
    re.IGNORECASE,
)

NEWSLETTER_SIGNALS = re.compile(
    r"(unsubscribe|view in browser|email preferences|manage subscriptions|"
    r"weekly roundup|daily brief|morning brew|digest|newsletter)",
    re.IGNORECASE,
)


def categorize_email(sender_email: str, subject: str, snippet: str = "") -> JunkCategory | None:
    """Classify an email into a junk category, or None if it seems legitimate."""
    if _is_protected(sender_email):
        return None

    combined = f"{subject} {snippet}"

    # Check scam first (highest priority)
    if SCAM_PATTERNS.search(combined):
        return JunkCategory.SCAM

    # Check social media
    domain = sender_email.split("@")[-1].lower() if "@" in sender_email else ""
    if domain in SOCIAL_DOMAINS:
        return JunkCategory.SOCIAL

    # Check newsletters (snippet often has "unsubscribe" etc.)
    if NEWSLETTER_SIGNALS.search(combined):
        return JunkCategory.NEWSLETTER

    # Check cold outreach / SEO spam
    if COLD_OUTREACH_PATTERNS.search(combined):
        return JunkCategory.SCAM

    # Check promos
    if PROMO_SENDER_PATTERNS.search(sender_email) or PROMO_SUBJECT_PATTERNS.search(subject):
        return JunkCategory.PROMO

    return None


# ── Scanning ─────────────────────────────────────────────────────

def scan_gmail(max_results: int = 100) -> CleanupReport:
    """Scan Gmail inbox for junk emails."""
    from integrations.gmail import find_promo_emails

    report = CleanupReport(source="Gmail")
    promos = find_promo_emails(max_results=max_results)
    report.total_scanned = len(promos)

    for email in promos:
        if _is_protected(email.sender_email):
            report.protected_count += 1
            continue

        cat = categorize_email(email.sender_email, email.subject, email.snippet)
        if not cat:
            # Gmail already flagged it as promo/social — trust that
            cat = JunkCategory.PROMO

        summary = EmailSummary(
            id=email.id,
            sender=email.sender,
            sender_email=email.sender_email,
            subject=email.subject,
            snippet=email.snippet[:80],
            category=cat,
        )
        report.by_category.setdefault(cat, []).append(summary)

    return report


def scan_outlook(max_results: int = 50) -> CleanupReport:
    """Scan Outlook inbox for junk emails."""
    from integrations.outlook import is_configured, list_emails

    report = CleanupReport(source="Outlook")

    if not is_configured():
        return report

    emails = list_emails(max_results=max_results)
    report.total_scanned = len(emails)

    for email in emails:
        if _is_protected(email.sender_email):
            report.protected_count += 1
            continue

        cat = categorize_email(email.sender_email, email.subject, email.snippet)
        if not cat:
            continue  # Looks legit — skip

        summary = EmailSummary(
            id=email.id,
            sender=email.sender,
            sender_email=email.sender_email,
            subject=email.subject,
            snippet=email.snippet[:80],
            category=cat,
        )
        report.by_category.setdefault(cat, []).append(summary)

    return report


# ── Formatting ───────────────────────────────────────────────────

def _format_report(report: CleanupReport) -> str:
    """Format a single inbox report."""
    total = sum(len(emails) for emails in report.by_category.values())
    if total == 0:
        return f"*{report.source}* — clean (scanned {report.total_scanned})"

    lines = [f"*{report.source}* ({total} to archive, {report.protected_count} protected)"]

    for cat in JunkCategory:
        emails = report.by_category.get(cat, [])
        if not emails:
            continue

        # Group by sender
        by_sender: dict[str, int] = {}
        for e in emails:
            by_sender[e.sender_email] = by_sender.get(e.sender_email, 0) + 1

        lines.append(f"  {cat.value} ({len(emails)}):")
        for sender, count in sorted(by_sender.items(), key=lambda x: -x[1])[:8]:
            lines.append(f"    {sender}: {count}")
        if len(by_sender) > 8:
            lines.append(f"    ... and {len(by_sender) - 8} more senders")

    return "\n".join(lines)


def format_dry_run(gmail_report: CleanupReport, outlook_report: CleanupReport) -> str:
    """Format both inbox reports as a dry-run summary."""
    gmail_total = sum(len(e) for e in gmail_report.by_category.values())
    outlook_total = sum(len(e) for e in outlook_report.by_category.values())
    grand_total = gmail_total + outlook_total

    parts = ["*Inbox Cleanup — Dry Run*\n"]
    parts.append(_format_report(gmail_report))
    parts.append("")
    parts.append(_format_report(outlook_report))
    parts.append(f"\n*Total: {grand_total} emails to archive*")

    if grand_total > 0:
        parts.append(f"\nType /cleanup go to archive all {grand_total}. Nothing has been touched yet — this is just a preview.")
    else:
        parts.append("\nBoth inboxes are clean. Nothing to do.")

    return "\n".join(parts)


# ── Execution ────────────────────────────────────────────────────

def execute_cleanup(
    gmail_report: CleanupReport, outlook_report: CleanupReport,
) -> str:
    """Archive all categorized junk from both inboxes."""
    results: list[str] = ["*Cleanup Results*\n"]

    # Gmail
    gmail_ids = [e.id for emails in gmail_report.by_category.values() for e in emails]
    if gmail_ids:
        from integrations.gmail import batch_archive_emails
        r = batch_archive_emails(gmail_ids)
        results.append(f"*Gmail*: archived {r['archived']}, skipped {r['skipped']}")
    else:
        results.append("*Gmail*: nothing to archive")

    # Outlook
    outlook_ids = [e.id for emails in outlook_report.by_category.values() for e in emails]
    if outlook_ids:
        from integrations.outlook import archive_emails
        r = archive_emails(outlook_ids)
        results.append(f"*Outlook*: archived {r['archived']}, skipped {r['skipped']}")
    else:
        results.append("*Outlook*: nothing to archive")

    return "\n".join(results)
