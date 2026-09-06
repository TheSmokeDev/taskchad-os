"""Microsoft Graph API integration for a configured Outlook mailbox.

Uses client credentials flow (application permissions) — no user login needed.
Requires: GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_TENANT_ID, GRAPH_USER_EMAIL in .env.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

# Importing config triggers persona-aware load_dotenv from config.ENV_FILE.
# Replaces the prior bare ``load_dotenv()`` call, which always loaded the
# install-dir .env regardless of HOMIE_HOME.
import config  # noqa: E402, F401
from integrations.capabilities import require_integration_action  # noqa: E402

GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET", "")
GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID", "")
GRAPH_USER_EMAIL = os.getenv("GRAPH_USER_EMAIL", "")

_token_cache: dict[str, Any] = {}
_logger = logging.getLogger(__name__)


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


def observe_inbound_response(
    *, thread_id: str, outbound_id: str, recipient_email: str, mailbox_id: str,
    collected_at: str, deadline: str | None = None, session: Any = None,
    mailbox_email: str | None = None, max_pages: int = 100,
) -> dict[str, Any]:
    """Read a conversation with stable Graph IDs across moves and all pages.

    An interrupted or bounded page scan can establish a reply but cannot prove
    its absence. Every request carries the immutable ID preference.
    """
    from urllib.parse import quote, urlparse

    from personas.learning.observers import (
        observe_mail_response,
        outlook_messages,
        unavailable_observation,
    )

    require_integration_action("outlook", "read")
    try:
        address = mailbox_email or GRAPH_USER_EMAIL
        if not address:
            return unavailable_observation("outlook", "mailbox_not_configured")
        if address.casefold() != mailbox_id.casefold():
            return unavailable_observation("outlook", "mailbox_identity_mismatch")
        headers = {"Prefer": 'IdType="ImmutableId"'}
        if session is None:
            headers["Authorization"] = f"Bearer {_get_access_token()}"
        client = session if session is not None else requests
        url = f"https://graph.microsoft.com/v1.0/users/{quote(address, safe='')}/messages"
        params = {"$filter": "conversationId eq '" + thread_id.replace("'", "''") + "'",
                  "$select": ("id,conversationId,internetMessageId,from,toRecipients,ccRecipients,"
                              "receivedDateTime,sentDateTime,isDraft,bodyPreview"),
                  "$top": "100"}
        messages: list[dict[str, Any]] = []
        complete = False
        for _ in range(max(1, max_pages)):
            response = client.get(url, params=params, headers=headers, timeout=15)
            response.raise_for_status()
            page = response.json()
            messages.extend(page.get("value", []))
            next_url = page.get("@odata.nextLink")
            if not next_url:
                complete = True
                break
            # Never forward Graph credentials to a URL supplied by a foreign host.
            parsed = urlparse(str(next_url))
            if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com":
                raise ValueError("invalid Graph pagination URL")
            url, params = str(next_url), None
        return observe_mail_response(
            provider="outlook", mailbox_id=mailbox_id, outbound_id=outbound_id,
            recipient_email=recipient_email, messages=outlook_messages(messages, address),
            collected_at=collected_at, deadline=deadline, complete=complete,
        )
    except Exception as exc:
        return unavailable_observation("outlook", type(exc).__name__)


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
    require_integration_action(
        "outlook",
        "archive",
        surface="operator_confirmed",
        caller="integrations.outlook.archive_emails",
    )
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


SIGNATURE_PATH_ENV = "OUTREACH_SIGNATURE_HTML"
_DEFAULT_SIGNATURE_PATH = Path(r"C:\Users\YourUser\YourBusiness\.claude\email-signature.html")


def load_signature_html(path: str | Path | None = None) -> str:
    """Return the branded outreach signature HTML, or '' if unavailable.

    Resolved at call time (never bound as a default arg) so the path can be
    overridden per run. Fail-open: a missing file yields an empty string and the
    caller still sends, it just sends unsigned.
    """
    if path is None:
        path = os.getenv(SIGNATURE_PATH_ENV) or _DEFAULT_SIGNATURE_PATH
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def send_email(
    to_email: str,
    subject: str,
    body: str,
    content_type: str = "Text",
    append_signature: bool = False,
    *,
    learning_context: dict[str, str] | None = None,
) -> bool:
    """Send an email via Microsoft Graph API.

    Defaults are byte-identical to the original plain-text behavior. Pass
    ``content_type="HTML"`` with ``append_signature=True`` for branded outreach
    so the canonical signature block renders instead of a hand-typed stub.
    """
    require_integration_action(
        "outlook",
        "send_email",
        surface="operator_confirmed",
        caller="integrations.outlook.send_email",
    )
    if append_signature:
        sig = load_signature_html()
        if sig:
            if content_type == "HTML":
                body = f"{body}{sig}"
            else:
                raise ValueError(
                    "append_signature=True requires content_type='HTML'; the "
                    "signature is an HTML table and will not render as text."
                )
    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": content_type,
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
    learning = None
    if learning_context is not None:
        from personas.learning import observers

        send_id = uuid.uuid4().hex
        try:
            learning = observers.begin_mail_send(
                learning_context, mailbox_id=GRAPH_USER_EMAIL, to_email=to_email,
                subject=subject, body=body, content_type=content_type, send_id=send_id,
            )
        except Exception as exc:
            _logger.warning("mail learning capture unavailable: %s", type(exc).__name__)
        # Opaque host correlation only: never put persona IDs or credentials in
        # outbound headers. Graph supports custom x- headers on sendMail.
        if learning is not None:
            payload["message"]["internetMessageHeaders"] = [
                {"name": "x-homie-send-id", "value": send_id}
            ]
    try:
        _graph_post("/sendMail", payload)
    except Exception:
        # The POST may have reached Graph. Never repeat it to obtain IDs. The
        # durable intent remains unknown and the scheduled collector reads it.
        raise
    if learning is not None:
        try:
            observers.resolve_mail_outbound(*learning)
        except Exception as exc:
            # Preserve sender compatibility: provider acceptance cannot become
            # an apparent send failure that tempts a caller to send again.
            _logger.warning("mail learning readback unavailable: %s", type(exc).__name__)
    return True


def find_sent_learning_message(intent: dict[str, Any], *, session: Any = None,
                               max_pages: int = 10) -> dict[str, Any]:
    """Verify Sent Items evidence for one prior host send, without any POST.

    Microsoft documents sendMail's 202 as acceptance, not completion:
    https://learn.microsoft.com/en-us/graph/api/user-sendmail
    ID linkage requires the opaque send header plus exact account, recipient,
    content and bounded server timestamp. Ambiguity is unknown, never guessed.
    """
    from urllib.parse import quote, urlparse

    from personas.learning.observers import (
        _time,
        mail_content_hash,
        outlook_messages,
        unavailable_observation,
    )

    require_integration_action("outlook", "read")
    try:
        address = GRAPH_USER_EMAIL
        if not address or address.casefold() != str(intent.get("mailbox_id", "")).casefold():
            return unavailable_observation("outlook", "mailbox_identity_mismatch")
        started, latest = _time(intent["started_at"]), _time(intent["send_not_after"])
        # Graph timestamps may carry seconds only. The unique host header and
        # exact content still bind the send; record the server precision below.
        earliest = started.replace(microsecond=0)
        headers = {
            "Prefer": 'IdType="ImmutableId", outlook.body-content-type="'
                      + str(intent["content_type"]) + '"',
        }
        client = session if session is not None else requests
        if session is None:
            headers["Authorization"] = f"Bearer {_get_access_token()}"
        root = f"https://graph.microsoft.com/v1.0/users/{quote(address, safe='')}"
        url = root + "/mailFolders/sentitems/messages"
        params = {
            "$filter": "sentDateTime ge " + earliest.isoformat().replace("+00:00", "Z")
                       + " and sentDateTime le " + latest.isoformat().replace("+00:00", "Z"),
            "$select": "id,conversationId,internetMessageId,internetMessageHeaders,from,"
                       "toRecipients,ccRecipients,bccRecipients,sentDateTime,receivedDateTime,"
                       "isDraft,subject,body,bodyPreview", "$top": "100",
        }
        matches = {}
        complete = False
        for _ in range(max(1, max_pages)):
            response = client.get(url, params=params, headers=headers, timeout=15)
            response.raise_for_status()
            page = response.json()
            for item in page.get("value", []):
                marker = [h.get("value") for h in item.get("internetMessageHeaders", [])
                          if str(h.get("name", "")).casefold() == "x-homie-send-id"]
                if marker != [intent["send_id"]]:
                    continue
                message = outlook_messages([item], address)[0]
                stamp = _time(message["occurred_at"])
                recipients = [str(r.get("emailAddress", {}).get("address", "")).casefold()
                              for r in item.get("toRecipients", [])]
                if (not message["sent"] or message["draft"] or not earliest <= stamp <= latest
                    or recipients != [intent["recipient_email"]]
                    or item.get("ccRecipients") or item.get("bccRecipients")
                    or mail_content_hash(recipients[0], item.get("subject", ""),
                        item.get("body", {}).get("content", ""),
                        item.get("body", {}).get("contentType", "")) != intent["content_hash"]):
                    continue
                message["host_send_started_at"] = intent["started_at"]
                message["send_id"] = intent["send_id"]
                matches[message["id"]] = message
            next_url = page.get("@odata.nextLink")
            if not next_url:
                complete = True
                break
            parsed = urlparse(str(next_url))
            if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com":
                raise ValueError("invalid Graph pagination URL")
            if not str(next_url).startswith(root + "/"):
                raise ValueError("Graph pagination account mismatch")
            url, params = str(next_url), None
        if not complete:
            return unavailable_observation("outlook", "sent_scan_incomplete")
        if len(matches) != 1:
            return unavailable_observation(
                "outlook", "sent_match_ambiguous" if matches else "sent_not_observed"
            )
        return {"status": "sent_observed", "outbound": next(iter(matches.values()))}
    except Exception as exc:
        return unavailable_observation("outlook", type(exc).__name__)
