"""
Personal Gmail Read-Only Integration for The Homie.

Connects to owner6392lastname@gmail.com with gmail.readonly scope.
Uses a separate OAuth token (google_token_owner.json) — completely
independent of the AI account token (google_token.json).

No write operations. All content passes through email_sanitizer.

Usage:
    uv run python -m integrations.personal_gmail list --max 10
    uv run python -m integrations.personal_gmail unread
    uv run python -m integrations.personal_gmail read <message_id>
"""

from __future__ import annotations

import base64
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

from config import (  # noqa: E402
    INTEGRATIONS_DIR,
    LOCAL_TZ,
    PERSONAL_GMAIL_ACCOUNT,
    PERSONAL_GMAIL_SCOPES,
    PERSONAL_GMAIL_TOKEN_PATH,
    now_local,
)
from shared import with_retry  # noqa: E402


@dataclass
class PersonalEmail:
    """Represents a personal email message (read-only)."""

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


def _get_personal_gmail_credentials() -> Any:
    """Load personal Gmail OAuth credentials (readonly scope), refreshing if expired."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = Path(PERSONAL_GMAIL_TOKEN_PATH)

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(token_path), PERSONAL_GMAIL_SCOPES
        )

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")  # type: ignore[no-untyped-call]
            return creds
        except RefreshError as e:
            raise RuntimeError(
                f"Personal Gmail token refresh failed: {e}\n"
                "Run 'uv run python setup_auth.py --personal' to re-authenticate."
            ) from e

    if creds and creds.valid:
        return creds

    raise RuntimeError(
        f"No valid personal Gmail token found for {PERSONAL_GMAIL_ACCOUNT}.\n"
        "Run 'uv run python setup_auth.py --personal' to authenticate."
    )


def get_personal_gmail_session() -> Any:
    """Return an AuthorizedSession for the personal Gmail account (requests-based, no httplib2)."""
    from google.auth.transport.requests import AuthorizedSession

    creds = _get_personal_gmail_credentials()
    return AuthorizedSession(creds)


_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _gmail_get(session: Any, path: str, **params: Any) -> dict[str, Any]:
    """GET request against the Gmail REST API."""
    resp = session.get(f"{_GMAIL_BASE}/{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def is_personal_gmail_configured() -> bool:
    """Return True if personal Gmail token file exists."""
    return Path(PERSONAL_GMAIL_TOKEN_PATH).exists()


def _parse_sender(sender_full: str) -> tuple[str, str]:
    if "<" in sender_full:
        sender = sender_full.split("<")[0].strip().strip('"')
        sender_email = sender_full.split("<")[1].rstrip(">")
    else:
        sender = sender_full
        sender_email = sender_full
    return sender, sender_email


def _extract_body(payload: dict[str, Any]) -> str:
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


def _get_email_details(
    session: Any, msg_id: str, include_body: bool = False
) -> PersonalEmail | None:
    """Fetch details for a single personal email."""
    try:
        fmt = "full" if include_body else "metadata"
        params: dict[str, Any] = {
            "format": fmt,
            "metadataHeaders": ["From", "Subject", "Date"],
        }
        msg: dict[str, Any] = with_retry(
            lambda: _gmail_get(session, f"messages/{msg_id}", **params)
        )

        headers: dict[str, str] = {
            h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
        }

        sender, sender_email = _parse_sender(headers.get("From", ""))

        date_str = headers.get("Date", "")
        try:
            date = parsedate_to_datetime(date_str)
        except Exception:
            date = now_local()

        body = None
        if include_body:
            body = _extract_body(msg.get("payload", {}))

        label_ids: list[str] = msg.get("labelIds", [])

        return PersonalEmail(
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
        print(f"Error getting personal email {msg_id}: {e}")
        return None


def list_personal_emails(
    max_results: int = 20,
    query: str = "",
    unread_only: bool = False,
    hours_ago: int | None = None,
) -> list[PersonalEmail]:
    """List personal emails matching criteria."""
    session = get_personal_gmail_session()

    q_parts: list[str] = []
    if query:
        q_parts.append(query)
    if unread_only:
        q_parts.append("is:unread")
    if hours_ago:
        after_date = now_local() - timedelta(hours=hours_ago)
        q_parts.append(f"after:{after_date.strftime('%Y/%m/%d')}")

    params: dict[str, Any] = {"maxResults": max_results}
    if q_parts:
        params["q"] = " ".join(q_parts)

    result: dict[str, Any] = with_retry(lambda: _gmail_get(session, "messages", **params))
    messages: list[dict[str, str]] = result.get("messages", [])
    emails: list[PersonalEmail] = []

    for msg in messages:
        email = _get_email_details(session, msg["id"])
        if email:
            emails.append(email)

    return emails


def get_personal_unread_count() -> int:
    """Return count of unread messages in personal inbox."""
    session = get_personal_gmail_session()
    result: dict[str, Any] = with_retry(
        lambda: _gmail_get(session, "messages", q="is:unread in:inbox", maxResults=1)
    )
    count: int = result.get("resultSizeEstimate", 0)
    return count


def get_personal_email(msg_id: str) -> PersonalEmail | None:
    """Fetch a single personal email by message ID (includes body)."""
    session = get_personal_gmail_session()
    return _get_email_details(session, msg_id, include_body=True)


def format_personal_emails_for_context(
    emails: list[PersonalEmail], max_chars: int = 2000
) -> str:
    """Format personal emails for bot output. Sanitizes all external content."""
    if not emails:
        return "No emails found."

    try:
        from integrations.email_sanitizer import sanitize_external_text
    except ImportError:
        def sanitize_external_text(text: str) -> str:  # type: ignore[misc]
            return text[:500]

    output: list[str] = []
    chars = 0

    for email in emails:
        date_local = email.date.astimezone(LOCAL_TZ) if email.date.tzinfo else email.date
        safe_subject = sanitize_external_text(email.subject)
        safe_snippet = sanitize_external_text(email.snippet[:100])
        safe_sender = sanitize_external_text(email.sender)
        entry = (
            f"- **{safe_subject}** [id: {email.id}]\n"
            f"  From: {safe_sender} <{email.sender_email}>\n"
            f"  Date: {date_local.strftime('%Y-%m-%d %H:%M')}\n"
            f"  {'[UNREAD] ' if email.is_unread else ''}{safe_snippet}"
        )

        if chars + len(entry) > max_chars:
            remaining = len(emails) - len(output)
            output.append(f"\n... and {remaining} more emails")
            break

        output.append(entry)
        chars += len(entry)

    return "\n\n".join(output)


# ---------------------------------------------------------------------------
# CLI entry point (for direct testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Personal Gmail read-only integration")
    sub = parser.add_subparsers(dest="action")

    p_list = sub.add_parser("list", help="List recent emails")
    p_list.add_argument("--max", type=int, default=10)
    p_list.add_argument("--query", default="")
    p_list.add_argument("--hours", type=int, default=None)

    p_unread = sub.add_parser("unread", help="Unread count + list")
    p_unread.add_argument("--max", type=int, default=10)

    p_read = sub.add_parser("read", help="Read a specific email")
    p_read.add_argument("msg_id")

    args = parser.parse_args()

    if args.action == "list":
        emails = list_personal_emails(max_results=args.max, query=args.query, hours_ago=args.hours)
        print(format_personal_emails_for_context(emails))
    elif args.action == "unread":
        count = get_personal_unread_count()
        emails = list_personal_emails(max_results=args.max, unread_only=True)
        print(f"Unread: {count}\n")
        print(format_personal_emails_for_context(emails))
    elif args.action == "read":
        email = get_personal_email(args.msg_id)
        if email:
            print(f"Subject: {email.subject}")
            print(f"From: {email.sender} <{email.sender_email}>")
            print(f"Date: {email.date}")
            print(f"\n{email.body or '(no body)'}")
        else:
            print(f"Email {args.msg_id} not found")
    else:
        parser.print_help()
