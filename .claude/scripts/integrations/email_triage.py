"""Email triage — prioritized inbox briefing across Gmail + Outlook.

Fetches unread/recent emails, filters out junk, categorizes by priority,
and formats a TL;DR briefing. No LLM needed — rules-based triage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Priority(IntEnum):
    """Email priority levels, ordered by urgency."""
    ACTION = 1      # Needs you to do something
    REPLY = 2       # Someone's waiting for a response
    MONEY = 3       # Financial — billing, payments, invoices
    FYI = 4         # Good to know, no action needed
    LOW = 5         # Can wait


PRIORITY_LABELS = {
    Priority.ACTION: "ACTION REQUIRED",
    Priority.REPLY: "Needs Reply",
    Priority.MONEY: "Money / Billing",
    Priority.FYI: "FYI",
    Priority.LOW: "Low Priority",
}

PRIORITY_ICONS = {
    Priority.ACTION: "!!",
    Priority.REPLY: "->",
    Priority.MONEY: "$$",
    Priority.FYI: "--",
    Priority.LOW: "..",
}


@dataclass
class TriagedEmail:
    source: str  # "Gmail" or "Outlook"
    sender: str
    sender_email: str
    subject: str
    snippet: str
    priority: Priority
    reason: str  # Why this priority
    is_unread: bool = True


@dataclass
class InboxBriefing:
    emails: list[TriagedEmail] = field(default_factory=list)
    gmail_total: int = 0
    gmail_unread: int = 0
    outlook_total: int = 0
    outlook_unread: int = 0
    junk_filtered: int = 0


# ── Priority detection patterns ──────────────────────────────────

ACTION_PATTERNS = re.compile(
    r"(action required|action needed|response (required|needed)|"
    r"please (confirm|verify|review|approve|sign|complete|update|submit)|"
    r"deadline|due (today|tomorrow|by)|expir(es?|ing|ation)|"
    r"your (order|shipment|package|delivery)|"
    r"appointment|scheduled for|reminder:|"
    r"document.{0,10}(ready|sign|review)|"
    r"invitation to|you.re invited|rsvp)",
    re.IGNORECASE,
)

MONEY_PATTERNS = re.compile(
    r"(invoice|payment|billing|receipt|statement|"
    r"charge|refund|subscription|renewal|"
    r"your (bill|payment|plan)|amount due|"
    r"credit card|bank account|transaction|"
    r"suspended.*billing|overdue|past due|"
    r"tax (return|document|form)|w-2|1099)",
    re.IGNORECASE,
)

# Senders that indicate a real person (not automated)
HUMAN_SENDER_SIGNALS = re.compile(
    r"^[a-z]+(\.[a-z]+)?@",  # firstname@, first.last@
    re.IGNORECASE,
)

# Automated/no-reply senders
AUTO_SENDER_PATTERNS = re.compile(
    r"(noreply|no-reply|donotreply|notifications?|alerts?|"
    r"updates?|mailer|daemon|system|automated|bounce|"
    r"postmaster|info@|hello@|support@|team@|news@)",
    re.IGNORECASE,
)

# Junk — skip these entirely (handled by /cleanup)
JUNK_DOMAINS = {
    "facebookmail.com", "linkedin.com", "twitter.com", "x.com",
    "instagram.com", "tiktok.com", "reddit.com", "pinterest.com",
    "nextdoor.com", "quora.com", "medium.com",
}

JUNK_SENDER_PATTERNS = re.compile(
    r"(marketing|promo|deals|offers|sales|campaign|"
    r"newsletter|digest|bulk|blast)@",
    re.IGNORECASE,
)

# Cold outreach / spam disguised as personal emails
COLD_OUTREACH_PATTERNS = re.compile(
    r"(send.{0,10}screenshots?|check(ed)? your (site|website)|"
    r"i.ve been (looking|checking)|hope you.re having|"
    r"quick question about your|boost your (seo|traffic|ranking)|"
    r"link.?building|guest.?post|backlink|"
    r"web.?authority|real.?growth|grow your|"
    r"google reviews.*permanent|rank.{0,10}(higher|first page))",
    re.IGNORECASE,
)


def _is_junk(sender_email: str, subject: str, snippet: str = "") -> bool:
    """Quick check if email is junk (skip in triage, leave for /cleanup)."""
    domain = sender_email.split("@")[-1].lower() if "@" in sender_email else ""
    if domain in JUNK_DOMAINS:
        return True
    if JUNK_SENDER_PATTERNS.search(sender_email):
        return True
    # Cold outreach disguised as personal emails
    combined = f"{subject} {snippet}"
    if COLD_OUTREACH_PATTERNS.search(combined):
        return True
    return False


def triage_email(
    sender: str, sender_email: str, subject: str, snippet: str, is_unread: bool,
) -> tuple[Priority, str] | None:
    """Determine priority for an email. Returns (priority, reason) or None if junk."""
    if _is_junk(sender_email, subject, snippet):
        return None

    combined = f"{subject} {snippet}"

    # Money first — always important
    if MONEY_PATTERNS.search(combined):
        return Priority.MONEY, "billing/financial"

    # Action required
    if ACTION_PATTERNS.search(combined):
        return Priority.ACTION, "action requested"

    # Human sender asking something? Likely needs reply
    if (
        HUMAN_SENDER_SIGNALS.match(sender_email)
        and not AUTO_SENDER_PATTERNS.search(sender_email)
        and "?" in combined
    ):
        return Priority.REPLY, "question from a person"

    # Unread from non-automated sender
    if is_unread and not AUTO_SENDER_PATTERNS.search(sender_email):
        return Priority.FYI, "unread"

    # Everything else
    return Priority.LOW, "informational"


# ── Scanning ─────────────────────────────────────────────────────

def scan_inbox(max_per_source: int = 20, unread_only: bool = True) -> InboxBriefing:
    """Scan both inboxes and triage all emails."""
    briefing = InboxBriefing()

    # --- Gmail ---
    try:
        from integrations.gmail import get_unread_count as gmail_unread_count, list_emails as gmail_list

        briefing.gmail_unread = gmail_unread_count()
        emails_g = gmail_list(max_results=max_per_source, unread_only=unread_only)
        briefing.gmail_total = len(emails_g)

        for e in emails_g:
            result = triage_email(e.sender, e.sender_email, e.subject, e.snippet, e.is_unread)
            if result is None:
                briefing.junk_filtered += 1
                continue
            priority, reason = result
            briefing.emails.append(TriagedEmail(
                source="Gmail", sender=e.sender, sender_email=e.sender_email,
                subject=e.subject, snippet=e.snippet[:120],
                priority=priority, reason=reason, is_unread=e.is_unread,
            ))
    except Exception as ex:
        briefing.emails.append(TriagedEmail(
            source="Gmail", sender="Error", sender_email="",
            subject=f"Gmail scan failed: {ex}", snippet="",
            priority=Priority.FYI, reason="error",
        ))

    # --- Outlook ---
    try:
        from integrations.outlook import (
            get_unread_count as outlook_unread_count,
            is_configured,
            list_emails as outlook_list,
        )

        if is_configured():
            briefing.outlook_unread = outlook_unread_count()
            emails_o = outlook_list(max_results=max_per_source, unread_only=unread_only)
            briefing.outlook_total = len(emails_o)

            for e in emails_o:
                result = triage_email(e.sender, e.sender_email, e.subject, e.snippet, e.is_unread)
                if result is None:
                    briefing.junk_filtered += 1
                    continue
                priority, reason = result
                briefing.emails.append(TriagedEmail(
                    source="Outlook", sender=e.sender, sender_email=e.sender_email,
                    subject=e.subject, snippet=e.snippet[:120],
                    priority=priority, reason=reason, is_unread=e.is_unread,
                ))
    except Exception as ex:
        briefing.emails.append(TriagedEmail(
            source="Outlook", sender="Error", sender_email="",
            subject=f"Outlook scan failed: {ex}", snippet="",
            priority=Priority.FYI, reason="error",
        ))

    # Sort by priority (ACTION first, LOW last)
    briefing.emails.sort(key=lambda e: e.priority)
    return briefing


# ── Formatting ───────────────────────────────────────────────────

def format_briefing(briefing: InboxBriefing) -> str:
    """Format the triage as a readable briefing."""
    lines = [
        f"*Inbox Briefing*",
        f"Gmail: {briefing.gmail_unread} unread | Outlook: {briefing.outlook_unread} unread",
        f"({briefing.junk_filtered} junk filtered out)\n",
    ]

    if not briefing.emails:
        lines.append("Nothing important right now. Inbox is chill.")
        return "\n".join(lines)

    current_priority: Priority | None = None

    for email in briefing.emails:
        # Section header when priority changes
        if email.priority != current_priority:
            current_priority = email.priority
            label = PRIORITY_LABELS[current_priority]
            lines.append(f"*{label}*")

        icon = PRIORITY_ICONS[email.priority]
        source_tag = f"[{email.source[0]}]"  # [G] or [O]
        unread_tag = " NEW" if email.is_unread else ""

        # TL;DR line: subject + who + why
        subj = email.subject[:60]
        sender_short = email.sender.split()[0] if email.sender else email.sender_email.split("@")[0]

        lines.append(f"  {icon} {source_tag}{unread_tag} {subj}")
        lines.append(f"     From: {sender_short} — {email.reason}")

        # Show snippet for high-priority items
        if email.priority <= Priority.MONEY and email.snippet:
            snippet = email.snippet[:100].replace("\n", " ")
            lines.append(f"     > {snippet}")

        lines.append("")

    return "\n".join(lines).strip()
