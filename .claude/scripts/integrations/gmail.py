"""
Gmail Direct Integration for The Homie.

Read-only access to Gmail via Google API. Shares OAuth token with Calendar.

Usage:
    uv run python -m integrations.gmail list --max 5
    uv run python -m integrations.gmail unread
    uv run python -m integrations.gmail urgent --hours 2
    uv run python -m integrations.gmail search --query "from:someone"
"""

from __future__ import annotations

import base64
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LOCAL_TZ, now_local  # noqa: E402
from shared import with_retry  # noqa: E402


@dataclass
class Email:
    """Represents an email message."""

    id: str
    thread_id: str
    subject: str
    sender: str
    sender_email: str
    date: datetime
    snippet: str
    body: str | None = None
    labels: list[str] = field(default_factory=list)
    is_unread: bool = False


def get_gmail_service() -> Any:
    """Build authenticated Gmail API service."""
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    from integrations.auth import get_google_credentials

    creds = get_google_credentials()
    service: Any = build("gmail", "v1", credentials=creds)
    return service


def _parse_sender(sender_full: str) -> tuple[str, str]:
    """Parse 'Name <email>' format into (name, email)."""
    if "<" in sender_full:
        sender = sender_full.split("<")[0].strip().strip('"')
        sender_email = sender_full.split("<")[1].rstrip(">")
    else:
        sender = sender_full
        sender_email = sender_full
    return sender, sender_email


def _extract_body(payload: dict[str, Any]) -> str:
    """Extract email body text from payload (handles multipart MIME)."""
    body_data = payload.get("body", {}).get("data")
    if body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    parts = payload.get("parts", [])
    for part in parts:
        mime_type = part.get("mimeType", "")
        if mime_type == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        elif mime_type in ("multipart/alternative", "multipart/mixed"):
            result = _extract_body(part)
            if result:
                return result

    return ""


def get_email_details(service: Any, msg_id: str, include_body: bool = False) -> Email | None:
    """Get details for a single email."""
    try:
        fmt = "full" if include_body else "metadata"
        msg: dict[str, Any] = with_retry(
            lambda: service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format=fmt,
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )

        headers: dict[str, str] = {
            h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
        }

        sender, sender_email = _parse_sender(headers.get("From", ""))

        # Parse date robustly
        date_str = headers.get("Date", "")
        try:
            date = parsedate_to_datetime(date_str)
        except Exception:
            date = now_local()

        body = None
        if include_body:
            body = _extract_body(msg.get("payload", {}))

        label_ids: list[str] = msg.get("labelIds", [])

        return Email(
            id=msg["id"],
            thread_id=msg["threadId"],
            subject=headers.get("Subject", "(no subject)"),
            sender=sender,
            sender_email=sender_email,
            date=date,
            snippet=msg.get("snippet", ""),
            body=body,
            labels=label_ids,
            is_unread="UNREAD" in label_ids,
        )
    except Exception as e:
        print(f"Error getting email {msg_id}: {e}")
        return None


def list_emails(
    max_results: int = 10,
    query: str = "",
    unread_only: bool = False,
    hours_ago: int | None = None,
) -> list[Email]:
    """
    List emails matching criteria.

    Args:
        max_results: Maximum emails to return
        query: Gmail search query (e.g. "from:someone subject:important")
        unread_only: Only return unread emails
        hours_ago: Only emails from last N hours
    """
    service = get_gmail_service()

    q_parts: list[str] = []
    if query:
        q_parts.append(query)
    if unread_only:
        q_parts.append("is:unread")
    if hours_ago:
        after_date = now_local() - timedelta(hours=hours_ago)
        q_parts.append(f"after:{after_date.strftime('%Y/%m/%d')}")

    full_query = " ".join(q_parts) if q_parts else None

    result: dict[str, Any] = with_retry(
        lambda: service.users()
        .messages()
        .list(userId="me", maxResults=max_results, q=full_query)
        .execute()
    )

    messages: list[dict[str, str]] = result.get("messages", [])
    emails: list[Email] = []

    for msg in messages:
        email = get_email_details(service, msg["id"])
        if email:
            emails.append(email)

    return emails


def get_unread_count() -> int:
    """Get count of unread emails in inbox."""
    service = get_gmail_service()

    result: dict[str, Any] = with_retry(
        lambda: service.users()
        .messages()
        .list(userId="me", q="is:unread in:inbox", maxResults=1)
        .execute()
    )

    count: int = result.get("resultSizeEstimate", 0)
    return count


def check_for_urgent_emails(
    important_senders: list[str] | None = None,
    hours_ago: int = 2,
) -> list[Email]:
    """
    Check for urgent emails that need attention.

    Flags emails from important senders or with urgent keywords in subject.
    """
    recent = list_emails(max_results=20, unread_only=True, hours_ago=hours_ago)

    urgent_keywords = ["urgent", "asap", "important", "action required", "deadline"]
    urgent: list[Email] = []

    for email in recent:
        reason = ""

        # Check important senders
        if important_senders:
            for sender in important_senders:
                if sender.lower() in email.sender_email.lower():
                    reason = f"From important sender: {email.sender}"
                    break

        # Check urgent keywords in subject
        if not reason:
            subject_lower = email.subject.lower()
            for keyword in urgent_keywords:
                if keyword in subject_lower:
                    reason = f"Urgent keyword: {keyword}"
                    break

        if reason:
            email.body = reason
            urgent.append(email)

    return urgent


def get_thread_id(msg_id: str) -> str | None:
    """Resolve a Gmail message ID to its thread ID."""
    service = get_gmail_service()
    try:
        msg: dict[str, Any] = with_retry(
            lambda: service.users()
            .messages()
            .get(userId="me", id=msg_id, format="minimal")
            .execute()
        )
        return msg.get("threadId")
    except Exception:
        return None


def check_sent_reply(thread_id: str, after_timestamp: str) -> str | None:
    """
    Check if Cole sent a reply in a Gmail thread after a given time.

    Args:
        thread_id: The Gmail thread ID to check
        after_timestamp: ISO format timestamp — only look for replies after this time

    Returns:
        The reply text if Cole sent one, None otherwise.
    """
    service = get_gmail_service()

    try:
        thread_data: dict[str, Any] = with_retry(
            lambda: service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
    except Exception as e:
        print(f"Error fetching thread {thread_id}: {e}")
        return None

    after_dt = datetime.fromisoformat(after_timestamp)

    messages: list[dict[str, Any]] = thread_data.get("messages", [])
    for msg in messages:
        label_ids: list[str] = msg.get("labelIds", [])
        # Only look at messages Cole sent (in SENT label)
        if "SENT" not in label_ids:
            continue

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        date_str = headers.get("Date", "")
        try:
            msg_date = parsedate_to_datetime(date_str)
        except Exception:
            continue

        # Check if this sent message is after our timestamp
        if msg_date.replace(tzinfo=None) > after_dt.replace(tzinfo=None):
            body = _extract_body(msg.get("payload", {}))
            if body:
                return body

    return None


def get_important_unreplied_emails(
    hours_ago: int = 4,
    max_results: int = 10,
) -> list[Email]:
    """
    Get recent emails that Cole hasn't replied to yet.

    Returns emails from the inbox that are:
    - Received in the last N hours
    - Not from Cole himself
    - In threads where Cole's last message is NOT the most recent

    Importance filtering is done by Claude based on USER.md criteria.
    """
    service = get_gmail_service()

    after_date = now_local() - timedelta(hours=hours_ago)
    q = f"in:inbox after:{after_date.strftime('%Y/%m/%d')} -from:me"

    try:
        result: dict[str, Any] = with_retry(
            lambda: service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q=q)
            .execute()
        )
    except Exception as e:
        print(f"Error listing unreplied emails: {e}")
        return []

    messages_list: list[dict[str, str]] = result.get("messages", [])
    emails: list[Email] = []

    # Track threads we've already seen to avoid duplicates
    seen_threads: set[str] = set()

    for msg_ref in messages_list:
        email = get_email_details(service, msg_ref["id"], include_body=True)
        if not email:
            continue

        # Skip if we already have a message from this thread
        if email.thread_id in seen_threads:
            continue
        seen_threads.add(email.thread_id)

        emails.append(email)

    return emails


_INJECTION_PATTERNS = re.compile(
    r"(?i)"
    r"(?:ignore|disregard|forget|override)\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?|context)"
    r"|(?:you\s+are\s+now|act\s+as|pretend\s+(?:to\s+be|you\s+are)|new\s+instructions?)"
    r"|(?:system\s*(?:prompt|message|instruction)|<\s*/?system\s*>)"
    r"|(?:do\s+not\s+follow|stop\s+following)\s+(?:your|the)\s+(?:instructions?|rules?)"
    r"|(?:execute|run)\s+(?:this\s+)?(?:command|code|script)"
    r"|(?:forward|send|email|exfiltrate)\s+(?:all|my|the)\s+(?:emails?|data|messages?|files?|credentials?)\s+to"
    r"|(?:delete|remove|drop|destroy)\s+(?:all|every|the)\s+(?:files?|data|memory|emails?)"
)


def sanitize_external_text(text: str) -> str:
    """Sanitize text from external sources (email, etc.) to mitigate prompt injection.

    Replaces suspicious instruction-like patterns with a redaction marker.
    """
    return _INJECTION_PATTERNS.sub("[REDACTED-SUSPICIOUS-CONTENT]", text)


def format_emails_for_context(emails: list[Email], max_chars: int = 2000) -> str:
    """Format emails for inclusion in Claude's context prompt.

    All email content is sanitized to mitigate prompt injection attacks.
    """
    if not emails:
        return "No emails found."

    output: list[str] = []
    chars = 0

    for email in emails:
        date_cst = email.date.astimezone(LOCAL_TZ) if email.date.tzinfo else email.date
        safe_subject = sanitize_external_text(email.subject)
        safe_snippet = sanitize_external_text(email.snippet[:100])
        safe_sender = sanitize_external_text(email.sender)
        entry = (
            f"- **{safe_subject}** [thread_id: {email.thread_id}]\n"
            f"  From: {safe_sender} <{email.sender_email}>\n"
            f"  Date: {date_cst.strftime('%Y-%m-%d %H:%M')}\n"
            f"  {'[UNREAD] ' if email.is_unread else ''}{safe_snippet}"
        )

        if chars + len(entry) > max_chars:
            remaining = len(emails) - len(output)
            output.append(f"\n... and {remaining} more emails")
            break

        output.append(entry)
        chars += len(entry)

    return "\n\n".join(output)


def _load_env_set(*names: str) -> set[str]:
    values: set[str] = set()
    for name in names:
        raw = os.getenv(name, "")
        for item in raw.split(","):
            cleaned = item.strip().lower()
            if cleaned:
                values.add(cleaned)
    return values


def _load_protected_addresses() -> set[str]:
    protected = _load_env_set("PROTECTED_ADDRESSES")
    protected |= {
        value.strip().lower()
        for value in (
            os.getenv("BUSINESS_EMAIL", ""),
            os.getenv("GRAPH_USER_EMAIL", ""),
            os.getenv("GOOGLE_CALENDAR_ID", ""),
            os.getenv("CIRCLE_MEMBER_EMAIL", ""),
        )
        if "@" in value
    }
    return protected


# Email addresses owned by the user or deployment that cleanup should never archive.
PROTECTED_ADDRESSES = _load_protected_addresses()


def _is_protected_sender(sender_email: str) -> bool:
    """Check if an email is from one of the user's own addresses.

    Fails closed: if no protected addresses are configured, treats ALL
    senders as protected to prevent accidental archival.
    """
    if not PROTECTED_ADDRESSES:
        return True
    return sender_email.lower().strip() in {a.lower() for a in PROTECTED_ADDRESSES}


def archive_emails(msg_ids: list[str]) -> dict[str, int]:
    """Archive emails by removing the INBOX label (moves to All Mail).

    Returns counts of archived and skipped messages.
    """
    service = get_gmail_service()
    archived = 0
    skipped = 0

    for msg_id in msg_ids:
        try:
            with_retry(
                lambda mid=msg_id: service.users()
                .messages()
                .modify(
                    userId="me",
                    id=mid,
                    body={"removeLabelIds": ["INBOX"]},
                )
                .execute()
            )
            archived += 1
        except Exception as e:
            print(f"Error archiving {msg_id}: {e}")
            skipped += 1

    return {"archived": archived, "skipped": skipped}


def batch_archive_emails(msg_ids: list[str], batch_size: int = 1000) -> dict[str, int]:
    """Archive emails in bulk using Gmail batchModify API.

    Much faster than one-by-one — processes up to 1000 IDs per API call.
    """
    service = get_gmail_service()
    archived = 0
    skipped = 0

    for i in range(0, len(msg_ids), batch_size):
        chunk = msg_ids[i : i + batch_size]
        try:
            with_retry(
                lambda ids=chunk: service.users()
                .messages()
                .batchModify(
                    userId="me",
                    body={
                        "ids": ids,
                        "removeLabelIds": ["INBOX"],
                    },
                )
                .execute()
            )
            archived += len(chunk)
            print(f"  Archived batch {i // batch_size + 1}: {len(chunk)} emails")
        except Exception as e:
            print(f"  Error on batch {i // batch_size + 1}: {e}")
            skipped += len(chunk)

    return {"archived": archived, "skipped": skipped}


def bulk_archive_by_query(query: str, protect_senders: bool = True) -> dict[str, int]:
    """Archive all emails matching a Gmail query, with sender protection.

    Paginates through all results and batch-archives them.
    """
    service = get_gmail_service()
    all_ids: list[str] = []
    protected_count = 0
    page_token = None

    print(f"Scanning: {query}")
    while True:
        kwargs: dict[str, Any] = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token

        result: dict[str, Any] = with_retry(
            lambda kw=kwargs: service.users().messages().list(**kw).execute()
        )

        msg_refs = result.get("messages", [])
        if not msg_refs:
            break

        if protect_senders:
            for msg_ref in msg_refs:
                email = get_email_details(service, msg_ref["id"])
                if email and (_is_protected_sender(email.sender_email) or _is_keep_sender(email.sender_email)):
                    protected_count += 1
                    continue
                all_ids.append(msg_ref["id"])
        else:
            all_ids.extend(m["id"] for m in msg_refs)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

        print(f"  Scanned {len(all_ids)} emails so far ({protected_count} protected)...")

    if not all_ids:
        return {"archived": 0, "skipped": 0, "protected": protected_count}

    print(f"Archiving {len(all_ids)} emails ({protected_count} protected, skipped)...")
    result = batch_archive_emails(all_ids)
    result["protected"] = protected_count
    return result


def find_promo_emails(max_results: int = 100) -> list[Email]:
    """Find promotional/spam emails in inbox, excluding protected senders."""
    service = get_gmail_service()

    # Gmail's built-in category queries
    queries = [
        "category:promotions in:inbox",
        "category:social in:inbox",
        "category:updates in:inbox",
    ]

    seen_ids: set[str] = set()
    promo_emails: list[Email] = []

    for q in queries:
        try:
            result: dict[str, Any] = with_retry(
                lambda query=q: service.users()
                .messages()
                .list(userId="me", maxResults=max_results, q=query)
                .execute()
            )
        except Exception as e:
            print(f"Error querying '{q}': {e}")
            continue

        for msg_ref in result.get("messages", []):
            if msg_ref["id"] in seen_ids:
                continue
            seen_ids.add(msg_ref["id"])

            email = get_email_details(service, msg_ref["id"])
            if not email:
                continue

            # Skip emails from the user's own addresses
            if _is_protected_sender(email.sender_email):
                continue

            promo_emails.append(email)

            if len(promo_emails) >= max_results:
                break

    return promo_emails


# Senders to keep in inbox even if Gmail categorizes as promo/updates
KEEP_SENDERS = {
    "turbotax",
    "sdge",
    "capitalone",
    "stripe",
    "coinbase",
    "cash.app",
    "paypal",
    "chime",
    "irs.gov",
    "edd.ca.gov",
}


def _is_keep_sender(sender_email: str) -> bool:
    """Check if a sender is on the keep list (financial/important)."""
    lower = sender_email.lower()
    return any(k in lower for k in KEEP_SENDERS)


def cleanup_inbox(dry_run: bool = True, max_results: int = 100) -> str:
    """Find and optionally archive promo/spam emails.

    Args:
        dry_run: If True, only list what would be archived. If False, archive them.
        max_results: Max emails to process.

    Returns:
        Summary string of results.
    """
    promos = find_promo_emails(max_results=max_results)

    if not promos:
        return "No promo/spam emails found in inbox."

    # Split into archive vs keep
    to_archive: list[Email] = []
    kept: list[Email] = []
    for email in promos:
        if _is_keep_sender(email.sender_email):
            kept.append(email)
        else:
            to_archive.append(email)

    # Group by sender for summary
    by_sender: dict[str, list[Email]] = {}
    for email in to_archive:
        key = email.sender_email
        by_sender.setdefault(key, []).append(email)

    lines = [f"Found {len(to_archive)} emails to archive from {len(by_sender)} senders:\n"]
    for sender, emails in sorted(by_sender.items(), key=lambda x: -len(x[1])):
        lines.append(f"  {sender}: {len(emails)} emails")

    if kept:
        lines.append(f"\nKept {len(kept)} emails from important senders:")
        for email in kept:
            lines.append(f"  {email.sender_email}: {email.subject[:60]}")

    if dry_run:
        lines.append("\nDry run — nothing archived. Run with --execute to archive.")
    else:
        result = archive_emails([e.id for e in to_archive])
        lines.append(f"\nArchived {result['archived']} emails. Skipped {result['skipped']}.")

    return "\n".join(lines)


# CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail integration")
    parser.add_argument("command", choices=["auth", "list", "unread", "urgent", "search", "cleanup", "bulk-cleanup"])
    parser.add_argument("--max", type=int, default=10)
    parser.add_argument("--query", default="")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--unread", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Actually archive (default is dry run)")

    args = parser.parse_args()

    if args.command == "auth":
        service = get_gmail_service()
        print("Authentication successful!")

    elif args.command == "list":
        result_emails = list_emails(
            max_results=args.max, query=args.query, unread_only=args.unread, hours_ago=args.hours
        )
        print(format_emails_for_context(result_emails))

    elif args.command == "unread":
        count = get_unread_count()
        print(f"Unread emails: {count}")

    elif args.command == "urgent":
        urgent_emails = check_for_urgent_emails(hours_ago=args.hours)
        if urgent_emails:
            print(f"Found {len(urgent_emails)} potentially urgent emails:")
            print(format_emails_for_context(urgent_emails))
        else:
            print("No urgent emails found")

    elif args.command == "search":
        if not args.query:
            print("--query required for search command")
            sys.exit(1)
        result_emails = list_emails(max_results=args.max, query=args.query)
        print(format_emails_for_context(result_emails))

    elif args.command == "cleanup":
        print(cleanup_inbox(dry_run=not args.execute, max_results=args.max))

    elif args.command == "bulk-cleanup":
        categories = ["category:promotions in:inbox", "category:social in:inbox"]
        if not args.execute:
            print("Dry run — showing what would be archived per category.")
            print("Run with --execute to actually archive.\n")
        for cat in categories:
            print(f"\n{'='*50}")
            if args.execute:
                result = bulk_archive_by_query(cat, protect_senders=True)
                print(f"  Result: archived={result['archived']}, skipped={result.get('skipped',0)}, protected={result.get('protected',0)}")
            else:
                from integrations.gmail import get_gmail_service as _gs
                svc = _gs()
                r = with_retry(lambda q=cat: svc.users().messages().list(userId='me', q=q, maxResults=1).execute())
                print(f"  {cat}: ~{r.get('resultSizeEstimate', 0)} emails")
