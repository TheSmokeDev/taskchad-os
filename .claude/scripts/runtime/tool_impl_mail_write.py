"""Outlook send proposals with host-owned learning attribution.

The catalog grants reach only. Sending still needs the dedicated exact-payload
operator approval gate; Gmail integrations remain read-only.
"""

from __future__ import annotations

import logging
from email.utils import parseaddr
from typing import Any

TOOL = "outlook_send_email"
TOOLSET = "mail_write"
_logger = logging.getLogger(__name__)


def _validate(to_email: Any, subject: Any, body: Any) -> dict[str, str]:
    if (
        not isinstance(to_email, str)
        or not to_email
        or len(to_email) > 254
        or parseaddr(to_email)[1] != to_email
        or "@" not in to_email
        or any(c in to_email for c in "\r\n,;")
    ):
        raise ValueError("to_email must be one plain email address")
    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > 200
        or any(c in subject for c in "\r\n")
    ):
        raise ValueError("subject must be a single line of at most 200 characters")
    if not isinstance(body, str) or not body.strip() or len(body) > 5000:
        raise ValueError("body must contain 1 to 5000 characters")
    return {"to_email": to_email, "subject": subject, "body": body}


def propose_email(to_email=None, subject=None, body=None, *, _persona_id="", **_):
    from integrations import outlook
    from personas import action_proposals
    from personas.learning import hooks

    if not _persona_id:
        return "error: host persona identity is required"
    try:
        arguments = _validate(to_email, subject, body)
    except ValueError as exc:
        return f"error: {exc}"
    if not outlook.GRAPH_USER_EMAIL:
        return "error: Outlook mailbox is not configured"
    arguments["mailbox_id"] = outlook.GRAPH_USER_EMAIL
    # The model cannot supply or override this private bundle. It originates in
    # the dispatcher's current action and is covered by the stored payload hash.
    context = hooks.current_action_learning_context(persona_id=_persona_id)
    if context is not None:
        arguments["_learning_context"] = context
    proposal = action_proposals.propose_action(
        _persona_id, TOOL, arguments, f"Send one email to {to_email}: {subject}"
    )
    if proposal is None:
        return "error: could not record mail approval proposal; nothing sent"
    return action_proposals.card_text(proposal)


def execute_email(
    *, persona_id: str, action_id: str, execution_token: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    from integrations import outlook
    from personas import action_proposals

    # Preserve the original deep-copied payload for the one-use binding check.
    fields = _validate(arguments.get("to_email"), arguments.get("subject"), arguments.get("body"))
    if (
        not outlook.GRAPH_USER_EMAIL
        or outlook.GRAPH_USER_EMAIL.casefold() != str(arguments.get("mailbox_id", "")).casefold()
    ):
        return {"ok": False, "status": "refused", "reason": "mailbox_changed"}
    if not action_proposals.consume_execution_token(
        persona_id, action_id, execution_token, arguments
    ):
        return {"ok": False, "status": "refused", "reason": "invalid_execution_token"}
    context = arguments.get("_learning_context")
    if context is not None and context.get("persona_id") != persona_id:
        # A changed or cross-persona bundle cannot be attached to the send.
        _logger.warning("mail learning attribution mismatch")
        context = None
    try:
        accepted = outlook.send_email(**fields, learning_context=context)
    except Exception as exc:
        # An ambiguous provider timeout must never be described as unsent and
        # retried. The consumed token remains consumed; the observer only reads.
        return {"ok": False, "status": "unknown", "reason": type(exc).__name__, "retry_send": False}
    return {"ok": accepted, "status": "accepted", "delivery_verified": False, "retry_send": False}


def register_tools() -> int:
    from personas import action_proposals
    from runtime import tool_registry

    tool_registry.register_tool(
        TOOL,
        "Propose sending one plain-text Outlook email. Returns exact content and an "
        "operator approval card; only /act approve executes the stored email once.",
        toolset=TOOLSET,
        effect="write",
        persona_scoped=True,
        dedicated_gate=True,
        parameters={
            "type": "object",
            "properties": {
                "to_email": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to_email", "subject", "body"],
            "additionalProperties": False,
        },
        handler=propose_email,
    )
    action_proposals.register_action_executor(TOOL, execute_email)
    return 1
