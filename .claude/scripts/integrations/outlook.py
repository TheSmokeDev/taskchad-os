"""Microsoft Graph API integration for a configured Outlook mailbox.

Uses client credentials flow (application permissions) — no user login needed.
Requires: GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_TENANT_ID, GRAPH_USER_EMAIL in .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET", "")
GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID", "")
GRAPH_USER_EMAIL = os.getenv("GRAPH_USER_EMAIL", "")

_token_cache: dict[str, Any] = {}


@dataclass
class OutlookEmail:
    """Represents an Outlook email message."""

    id: str
    subject: str
    sender: str
    sender_email: str
    snippet: str
    date: datetime
    is_unread: bool
    has_attachments: bool = False
    importance: str = "normal"
    categories: list[str] = field(default_factory=list)


def _get_access_token() -> str:
    """Get an access token using client credentials flow."""
    if _token_cache.get("token") and _token_cache.get("expires_at", 0) > datetime.now().timestamp():
        return _token_cache["token"]

    url = f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": GRAPH_CLIENT_ID,
        "client_secret": GRAPH_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    }
    resp = requests.post(url, data=data, timeout=10)
    resp.raise_for_status()
    result = resp.json()

    _token_cache["token"] = result["access_token"]
    _token_cache["expires_at"] = datetime.now().timestamp() + result.get("expires_in", 3600) - 60
    return result["access_token"]


def _graph_get(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make an authenticated GET request to the Graph API."""
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER_EMAIL}{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _parse_message(msg: dict[str, Any]) -> OutlookEmail:
    """Parse a Graph API message into an OutlookEmail."""
    sender_info = msg.get("from", {}).get("emailAddress", {})
    received = msg.get("receivedDateTime", "")
    dt = datetime.fromisoformat(received.replace("Z", "+00:00")) if received else datetime.now(timezone.utc)

    return OutlookEmail(
        id=msg.get("id", ""),
        subject=msg.get("subject", "(no subject)"),
        sender=sender_info.get("name", "Unknown"),
        sender_email=sender_info.get("address", ""),
        snippet=msg.get("bodyPreview", "")[:200],
        date=dt,
        is_unread=not msg.get("isRead", True),
        has_attachments=msg.get("hasAttachments", False),
        importance=msg.get("importance", "normal"),
        categories=msg.get("categories", []),
    )


def get_email_body(message_id: str) -> str:
    """Fetch the plain-text body of a specific message."""
    import html as html_lib
    import re

    result = _graph_get(f"/messages/{message_id}", {"$select": "body"})
    body = result.get("body", {})
    # Prefer text/plain
    if body.get("contentType") == "text":
        return body.get("content", "")
    # Strip HTML: tags → whitespace, then decode entities, then collapse whitespace
    raw = body.get("content", "")
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_lib.unescape(text)
    # Remove zero-width / non-printable unicode junk
    text = re.sub(r"[\u200b-\u200f\u00ad\ufeff\u2028\u2029]+", "", text)
    # Collapse runs of whitespace / blank lines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _graph_post(endpoint: str, json_body: dict[str, Any]) -> dict[str, Any]:
    """Make an authenticated POST request to the Graph API."""
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER_EMAIL}{endpoint}"
    resp = requests.post(url, headers=headers, json=json_body, timeout=15)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def is_configured() -> bool:
    """Check if Graph API credentials are present."""
    return bool(GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET and GRAPH_TENANT_ID and GRAPH_USER_EMAIL)


def list_emails(
    max_results: int = 10,
    query: str = "",
    unread_only: bool = False,
    hours_ago: int | None = None,
) -> list[OutlookEmail]:
    """List emails from the Outlook inbox."""
    params: dict[str, Any] = {
        "$top": max_results,
        "$select": "id,subject,from,receivedDateTime,isRead,bodyPreview,hasAttachments,importance,categories",
        "$orderby": "receivedDateTime desc",
    }

    # Build filter
    filters: list[str] = []
    if unread_only:
        filters.append("isRead eq false")
    if hours_ago:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filters.append(f"receivedDateTime ge {since}")

    if filters:
        params["$filter"] = " and ".join(filters)

    # $search is separate from $filter — can't combine with $filter in Graph API
    if query and not filters:
        params["$search"] = f'"{query}"'
    elif query and filters:
        # Fallback: add subject contains to filter (less powerful than $search)
        filters.append(f"contains(subject, '{query}')")
        params["$filter"] = " and ".join(filters)

    result = _graph_get("/messages", params)
    messages = result.get("value", [])
    return [_parse_message(m) for m in messages]


def get_unread_count() -> int:
    """Get count of unread emails."""
    result = _graph_get("/mailFolders/inbox")
    return result.get("unreadItemCount", 0)


def format_emails_for_context(emails: list[OutlookEmail], max_chars: int = 2000) -> str:
    """Format Outlook emails for display."""
    if not emails:
        return "No emails found."

    try:
        from integrations.gmail import LOCAL_TZ
    except ImportError:
        from datetime import timezone as _tz
        LOCAL_TZ = _tz(timedelta(hours=-7))  # PST fallback

    output: list[str] = []
    chars = 0

    for email in emails:
        dt = email.date.astimezone(LOCAL_TZ) if email.date.tzinfo else email.date
        entry = (
            f"- *{email.subject}*\n"
            f"  From: {email.sender} <{email.sender_email}>\n"
            f"  Date: {dt.strftime('%Y-%m-%d %H:%M')}\n"
            f"  {'[UNREAD] ' if email.is_unread else ''}{email.snippet[:100]}"
        )

        if chars + len(entry) > max_chars:
            remaining = len(emails) - len(output)
            output.append(f"\n... and {remaining} more emails")
            break

        output.append(entry)
        chars += len(entry)

    return "\n\n".join(output)


def archive_emails(msg_ids: list[str]) -> dict[str, int]:
    """Move messages to the Archive folder. Returns archived/skipped counts."""
    archived = 0
    skipped = 0
    for msg_id in msg_ids:
        try:
            _graph_post(f"/messages/{msg_id}/move", {"destinationId": "archive"})
            archived += 1
        except Exception as e:
            print(f"[Outlook] Error archiving {msg_id}: {e}")
            skipped += 1
    return {"archived": archived, "skipped": skipped}


def send_email(to_email: str, subject: str, body: str) -> bool:
    """Send an email via Microsoft Graph API."""
    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "Text",
                "content": body
            },
            "toRecipients": [
                {
                    "emailAddress": {
                        "address": to_email
                    }
                }
            ]
        },
        "saveToSentItems": "true"
    }
    try:
        _graph_post("/sendMail", payload)
        return True
    except Exception as e:
        # Re-raise to let caller handle error reporting
        raise e
