"""Append-only audit log for self-authored-skill promotion actions (WS3 / Rail 4).

Every scan-preview / promote / reject / refusal / stale-archive event writes ONE
JSONL row here (B6). The skill-from-experience loop is a self-influence surface;
the audit trail is what makes promotion default-deny-with-a-paper-trail like every
other mutating surface in the framework.

Cloned from ``browser_audit.py`` (UTC ISO timestamp, append-only JSONL, redaction)
with ONE deliberate difference (NM1 / Rule 1): the log path is resolved at CALL
TIME via ``config.DATA_DIR`` attribute access inside the function body. ``browser_audit.py``
binds ``BROWSER_AUDIT_LOG = DATA_DIR / ...`` at import — that snapshots ``DATA_DIR``
and breaks ``HOMIE_HOME`` / test path overrides. Do NOT copy that here.

Fail-open: an audit-write failure NEVER raises into the caller (the promotion gate
must complete its security decision even if the paper trail momentarily fails).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUDIT_FILE_NAME = "skill_actions.jsonl"

KNOWN_SURFACES = {
    "cli",
    "telegram",
    "discord",
    "slack",
    "mission_control",
    "whatsapp",
    "scheduler",
    "unknown",
}

# Secret-shaped tokens scrubbed out of any free-text field before it is stored.
# A `reason` string could otherwise echo a leaked credential into the log. The
# `verdict` from skill_guard is a fixed enum and the Finding `match` is already
# pre-redacted upstream, so this guards the operator-/caller-supplied `reason`.
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|credential)\s*[=:]\s*"
        r"[\"']?[A-Za-z0-9+/=_\-]{12,}"
    ),
)

_REASON_MAX_LEN = 500


def _resolve_audit_path(path: Path | str | None = None) -> Path:
    """Resolve the audit-log path at CALL TIME (Rule 1 / NM1).

    Explicit ``path`` wins (tests). Otherwise ``DATA_DIR/skill_actions.jsonl`` is
    resolved by importing ``config`` inside the body and reading ``config.DATA_DIR``
    by attribute access — so a test's ``monkeypatch.setattr(config, "DATA_DIR", ...)``
    and ``HOMIE_HOME`` overrides take effect on the NEXT call with no module reload.
    """
    if path is not None:
        return Path(path)
    try:
        import config

        base = Path(config.DATA_DIR)
    except Exception:  # pragma: no cover - import path fallback for direct scripts
        base = Path(__file__).resolve().parents[1] / "data"
    return base / AUDIT_FILE_NAME


def _redact(text: str) -> str:
    """Strip secret-shaped tokens from a free-text field and truncate it."""
    if not text:
        return ""
    scrubbed = text
    for pat in _SECRET_PATTERNS:
        scrubbed = pat.sub("[REDACTED]", scrubbed)
    if len(scrubbed) > _REASON_MAX_LEN:
        scrubbed = scrubbed[: _REASON_MAX_LEN - 3] + "..."
    return scrubbed


def normalize_surface(surface: str | None) -> str:
    value = (surface or "unknown").strip().lower()
    if value == "web":
        value = "mission_control"
    return value if value in KNOWN_SURFACES else "unknown"


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def append_skill_audit_record(
    action: str,
    skill_name: str,
    outcome: str,
    *,
    verdict: str = "",
    reason: str = "",
    surface: str = "",
    session_id: str = "",
    path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Append one sanitized skill-action audit row and return it (or None on failure).

    Args:
        action: one of ``scan_preview`` / ``promote`` / ``reject`` / ``archive``.
        skill_name: the self-authored skill the action targets.
        outcome: terminal outcome — ``promoted`` / ``rejected`` / ``refused`` /
            ``stale_archived`` / a scan verdict (``safe``/``caution``/``dangerous``).
        verdict: last scan verdict, when relevant.
        reason: free-text refusal/context reason (redacted + truncated).
        surface: originating surface (telegram/cli/scheduler/...).
        session_id: best-effort session/provenance tag.
        path: explicit log-path override (tests); resolves ``DATA_DIR`` when None.

    Fail-open: returns the written record, or ``None`` if the write failed. NEVER
    raises into the caller — the security action proceeds regardless (same posture
    as ``kill_switches`` best-effort audit).
    """
    try:
        log_path = _resolve_audit_path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": _utc_timestamp(),
            "action": action,
            "skill_name": skill_name,
            "outcome": outcome,
            "verdict": verdict,
            "reason": _redact(reason),
            "surface": normalize_surface(surface),
            "session_id": session_id or None,
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
        return record
    except Exception as exc:  # noqa: BLE001 - audit is best-effort, never fatal
        logger.warning("skill audit-write failed (%s): %s", action, exc)
        return None


__all__ = (
    "AUDIT_FILE_NAME",
    "append_skill_audit_record",
    "normalize_surface",
)
