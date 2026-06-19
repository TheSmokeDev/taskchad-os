"""Social post audit trail — append-only JSONL.

Follows the pattern from .claude/chat/browser_audit.py.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _resolve_audit_path() -> Path:
    import config
    return config.DATA_DIR / "social_posts.jsonl"


def append_social_audit_record(
    *,
    channel: str,
    action: str,
    post_id: int | None = None,
    outcome: str = "",
    operator: str = "",
    body_preview: str = "",
    error: str = "",
    post_url: str = "",
    audit_path: Path | None = None,
) -> str:
    path = audit_path or _resolve_audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    preview = body_preview[:80].replace("\n", " ") if body_preview else ""

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel": channel,
        "action": action,
        "post_id": post_id,
        "outcome": outcome,
        "operator": operator,
        "body_preview": preview,
        "error": error,
        "post_url": post_url,
    }

    audit_id = f"{record['timestamp']}:{channel}:{action}:{post_id or 'none'}"

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return audit_id
