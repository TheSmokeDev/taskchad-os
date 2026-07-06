"""Terminal-flip Telegram notifications for the co-founder orchestrator.

Cloned from the proven ``social/notify.py`` shape: direct Bot API call from
the cron/heartbeat process (cross-process safe; the chat bot keeps the single
getUpdates poller, sendMessage from another process is fine), plain text,
token-redacting error handling, fail-open bool returns.

Default-deny (Global Invariant 1): every send attempt is gated by the
``cofounder`` kill switch (refusal counted via ``security.kill_switches``)
AND ``require_integration_action("cofounder", "notify")``, with one
append-only audit row per attempt at ``DATA_DIR/cofounder_notify.jsonl``.

Level filter: only levels in ``COFOUNDER_NOTIFY_LEVELS`` (default
``done | blocked | awaiting-human``) ever reach the gates. Other levels
return False without HTTP and without an audit row: a filtered level is not
a send attempt (routine progress is Activity Log only).

On a confirmed send the Telegram ``message_id`` is stamped back to the
project file as ``chat_thread`` (reply anchoring) via the US-003
``update_frontmatter`` writer. The stamp is best-effort: a stamp failure
never un-sends the message, so ``notify`` still returns True.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Telegram hard limit for sendMessage text.
_TG_TEXT_LIMIT = 4096
# Telegram hard limit for inline-button callback_data (bytes). A cron-process
# send cannot reach the running bot's hashed-callback map (it lives in the
# bot's memory), so ids must fit RAW or the buttons are dropped.
_TG_CALLBACK_DATA_LIMIT = 64


def _redact(text: str, token: str) -> str:
    """Strip the bot token from any string before it is logged or audited.
    urllib exceptions embed the request URL (which carries the token)."""
    if token and token in text:
        text = text.replace(token, "***")
    return text


def _telegram_credentials() -> tuple[str, str] | None:
    """Return (token, chat_id) from env, or None when not configured.

    Same env pathway as ``social/notify.py``: token from TELEGRAM_BOT_TOKEN,
    chat id = first entry of TELEGRAM_ALLOWED_USER_IDS.
    """
    import os

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    user_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not token or not user_ids:
        return None
    chat_id = user_ids.split(",")[0].strip()
    if not chat_id:
        return None
    return token, chat_id


def _build_text(slug: str, text: str, level: str) -> str:
    """Compose the plain-text notification. No parse_mode, so arbitrary
    project text can never break Telegram entity parsing."""
    header = f"[co-founder] {slug} - {level}"
    body = text or ""
    budget = _TG_TEXT_LIMIT - len(header) - 2
    if budget > 0 and len(body) > budget:
        body = body[: budget - 1].rstrip() + "…"
    return f"{header}\n\n{body}"[:_TG_TEXT_LIMIT]


def _build_reply_markup(slug: str) -> dict | None:
    """Inline pause/approve buttons for the notify card (US-016).

    The callback ids ride the bot's EXISTING ``__button:`` pipeline
    (``cofounder:pause:<slug>`` / ``cofounder:approve:<slug>``) and execute
    the same code path as ``/cofounder pause|approve``. Telegram rejects the
    ENTIRE sendMessage when any callback_data exceeds 64 bytes, so an
    overlong slug drops the buttons (returns None) instead of the card —
    slash commands still steer.
    """
    buttons = [
        {"text": "⏸ Pause", "callback_data": f"cofounder:pause:{slug}"},
        {"text": "✅ Approve", "callback_data": f"cofounder:approve:{slug}"},
    ]
    for button in buttons:
        if len(button["callback_data"].encode("utf-8")) > _TG_CALLBACK_DATA_LIMIT:
            logger.warning(
                "cofounder.notify: slug %r too long for inline buttons; "
                "card sent without them",
                slug,
            )
            return None
    return {"inline_keyboard": [buttons]}


def append_notify_audit_record(
    *,
    project: str,
    level: str,
    outcome: str,
    text_preview: str = "",
    message_id: int | None = None,
    error: str = "",
    audit_path: Path | str | None = None,
) -> str:
    """Append one audit row (append-only JSONL, ``social/audit.py`` shape).

    ``audit_path`` is a None-sentinel resolved at call time to
    ``config.DATA_DIR / "cofounder_notify.jsonl"`` (Rule 1).
    """
    if audit_path is None:
        import config

        audit_path = config.DATA_DIR / "cofounder_notify.jsonl"
    audit_path = Path(audit_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    preview = text_preview[:80].replace("\n", " ") if text_preview else ""
    record = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "integration": "cofounder",
        "action": "notify",
        "project": project,
        "level": level,
        "outcome": outcome,
        "message_id": message_id,
        "text_preview": preview,
        "error": error,
    }
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return f"{record['timestamp']}:{project}:{level}:{outcome}"


def _audit(
    slug: str,
    level: str,
    outcome: str,
    text: str,
    *,
    message_id: int | None = None,
    error: str = "",
    audit_path: Path | str | None = None,
) -> None:
    """Best-effort audit append (kill_switches precedent: a failed audit
    write never blocks the gate outcome and never breaks the notify path)."""
    try:
        append_notify_audit_record(
            project=slug,
            level=level,
            outcome=outcome,
            text_preview=text,
            message_id=message_id,
            error=error,
            audit_path=audit_path,
        )
    except Exception as exc:
        logger.warning("cofounder.notify: audit write failed (%s)", exc)


def _stamp_chat_thread(path: Any, message_id: int | None, slug: str) -> None:
    """Store the sent message_id back to the project file as ``chat_thread``.

    Best-effort: the message is already delivered, so a stamp failure only
    warns (the next successful send re-stamps).
    """
    if path is None or message_id is None:
        return
    try:
        from cofounder import project_model

        project_model.update_frontmatter(Path(path), chat_thread=message_id)
    except Exception as exc:
        logger.warning(
            "cofounder.notify: chat_thread stamp failed for %s (%s)", slug, exc
        )


def notify(
    project: Any,
    text: str,
    level: str,
    *,
    settings=None,
    audit_path: Path | str | None = None,
    with_buttons: bool = True,
) -> bool:
    """Send one terminal-flip notification. Returns True only on a confirmed
    send; False on every filtered, refused, denied, or failed path. Never
    raises (Invariant 6: a notify failure never breaks a pass).

    Gate order: level filter (silent False, not a send attempt) -> kill
    switch (refusal counted, audit row) -> capability gate (audit row, no
    HTTP on deny) -> HTTP send (audit row either way).

    ``with_buttons=False`` drops the inline pause/approve buttons — for cards
    that carry no steerable project (the v2 agenda card), where a button
    press would hit ``/cofounder pause`` on a non-project slug.
    """
    slug = str(getattr(project, "slug", None) or "unknown")
    path = getattr(project, "path", None)
    normalized_level = str(level or "").strip().lower()
    try:
        import config

        if settings is None:
            settings = config.get_cofounder_settings()  # Rule 1: call time
        if normalized_level not in settings.notify_levels:
            return False

        from security import kill_switches  # Rule 3: module-attribute lookup

        try:
            kill_switches.requireEnabled("cofounder", caller="cofounder.notify")
        except kill_switches.KillSwitchDisabled:
            _audit(
                slug,
                normalized_level,
                "refused_killswitch",
                text,
                audit_path=audit_path,
            )
            logger.warning("cofounder.notify: refused by kill switch for %s", slug)
            return False

        from integrations import capabilities

        try:
            capabilities.require_integration_action(
                "cofounder", "notify", surface="internal", caller="cofounder.notify"
            )
        except capabilities.IntegrationPolicyError as exc:
            _audit(
                slug,
                normalized_level,
                "denied",
                text,
                error=str(exc),
                audit_path=audit_path,
            )
            logger.warning(
                "cofounder.notify: capability denied for %s: %s", slug, exc
            )
            return False

        creds = _telegram_credentials()
        if creds is None:
            _audit(
                slug,
                normalized_level,
                "failed",
                text,
                error="telegram credentials not configured",
                audit_path=audit_path,
            )
            logger.warning(
                "cofounder.notify: Telegram creds not configured; %s not notified",
                slug,
            )
            return False
        token, chat_id = creds

        message_id: int | None = None
        try:
            params = {
                "chat_id": chat_id,
                "text": _build_text(slug, text, normalized_level),
                "disable_web_page_preview": "true",
            }
            reply_markup = _build_reply_markup(slug) if with_buttons else None
            if reply_markup is not None:
                params["reply_markup"] = json.dumps(reply_markup)
            data = urllib.parse.urlencode(params).encode()
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            result = payload.get("result") if isinstance(payload, dict) else None
            if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                message_id = result["message_id"]
        except Exception as exc:
            safe = _redact(f"{type(exc).__name__}: {exc}", token)
            _audit(
                slug,
                normalized_level,
                "failed",
                text,
                error=safe,
                audit_path=audit_path,
            )
            logger.warning(
                "cofounder.notify: Telegram send failed for %s: %s", slug, safe
            )
            return False

        _audit(
            slug,
            normalized_level,
            "sent",
            text,
            message_id=message_id,
            audit_path=audit_path,
        )
        _stamp_chat_thread(path, message_id, slug)
        return True
    except Exception as exc:
        logger.warning("cofounder.notify: unexpected failure for %s (%s)", slug, exc)
        return False
