"""Secret-safe Buzz runtime status shared by CLI, diagnostics, and Dashboard."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from buzz_config import buzz_runtime_status_path, get_buzz_settings

_DEFAULT = {
    "enabled": False,
    "state": "disabled",
    "active_transport": "none",
    "relay_host": "",
    "identity": "",
    "watched_channel_count": 0,
    "last_event_time": None,
    "cli_version": None,
    "cli_compatible": None,
    "lock_conflict": False,
    "last_error": None,
    "updated_at": None,
    "snapshot_stale": False,
}


def _safe_status(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value.get(key, default) for key, default in _DEFAULT.items()}


def configured_status() -> dict[str, Any]:
    settings = get_buzz_settings()
    result = dict(_DEFAULT)
    result.update(
        {
            "enabled": settings.configured,
            "state": "stopped" if settings.configured else "disabled",
            "relay_host": settings.relay_host,
            "watched_channel_count": len(settings.channels),
        }
    )
    return result


def read_buzz_status(path: Path | None = None) -> dict[str, Any]:
    target = path or buzz_runtime_status_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Buzz status is not an object")
        result = _safe_status(raw)
        updated_at = result.get("updated_at")
        if updated_at and result.get("state") in {"connected", "degraded"}:
            try:
                observed = datetime.fromisoformat(str(updated_at))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - observed.astimezone(UTC)).total_seconds()
                if age > 180:
                    result["snapshot_stale"] = True
                    result["state"] = "failed"
                    result["active_transport"] = "none"
                    result["last_error"] = "Buzz runtime status is stale"
            except ValueError:
                result["snapshot_stale"] = True
        return result
    except (OSError, ValueError, json.JSONDecodeError):
        return configured_status()


def write_buzz_status(status: dict[str, Any], path: Path | None = None) -> None:
    target = path or buzz_runtime_status_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _safe_status(status)
    payload["updated_at"] = datetime.now(UTC).isoformat()
    handle, temporary = tempfile.mkstemp(prefix=".buzz-status-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
