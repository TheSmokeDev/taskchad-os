"""Shared, redacted operator projection for the learning CLI and dashboard.

Business transitions remain in the learning service and promotion module. This
surface only accepts opaque record ids; it never opens an evidence path supplied
by an operator or a model.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from personas.learning.models import is_credential_key
from security import redact as redact_module

if TYPE_CHECKING:
    from personas.learning.service import LearningService

_ID = re.compile(r"^[A-Za-z0-9_:-]{1,160}$")
_ABSOLUTE_PATH = re.compile(
    "(?:[A-Za-z]:[\\\\/]|\\\\\\\\)[^\\s<>\"']+|(?<![\\w:])/(?:home|Users|root|t"
    "mp|var|mnt|etc|srv|opt)/[^\\s<>\"']+"
)
_ENVELOPE = frozenset({"id", "kind", "persona_id", "created_at"})
_LINK_FIELDS = frozenset(
    {
        "experience_id",
        "expectation_id",
        "candidate_id",
        "evaluation_id",
        "activation_id",
        "evidence_ids",
        "counterevidence_ids",
        "supersedes",
    }
)
_KINDS = frozenset(
    {
        "experience",
        "expectation",
        "execution",
        "observation",
        "candidate",
        "evaluation",
        "activation",
        "context",
        "failure",
    }
)
_ATTENTION_STATUSES = frozenset({"failed", "deferred", "needs_reassessment", "unresolvable"})


def safe_text(value: str) -> str:
    """No secret-bearing text escapes if global log redaction is disabled."""
    if not getattr(redact_module, "_REDACT_ENABLED", True):
        return "[text withheld: secret redaction is disabled]"
    return _ABSOLUTE_PATH.sub("[local path]", redact_module.redact(value))


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            safe_text(str(key)): "[redacted]" if is_credential_key(str(key)) else _safe(child)
            for key, child in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_safe(child) for child in value]
    if isinstance(value, str):
        return safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return safe_text(str(value))


def _record_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError("Invalid learning record id")
    return value


class LearningOperator:
    """A request-scoped presenter with no cache or background actions."""

    def __init__(self, service: LearningService):
        self.service = service

    def _present(self, row: dict, *, links: bool = False) -> dict:
        result = {key: row.get(key, "") for key in _ENVELOPE}
        result["payload"] = _safe(
            {key: value for key, value in row.items() if key not in _ENVELOPE}
        )
        if row.get("kind") == "activation" and isinstance(row.get("candidate"), dict):
            result["payload"]["title"] = safe_text(str(row["candidate"].get("title", "Method")))
            result["payload"]["status"] = row.get("method_status", row.get("status", "unknown"))
        result["links"] = []
        if links:
            seen: set[str] = set()
            for field in _LINK_FIELDS:
                raw = row.get(field, [])
                values = raw if isinstance(raw, list) else [raw]
                for record_id in values[:60]:
                    if (
                        not isinstance(record_id, str)
                        or not _ID.fullmatch(record_id)
                        or record_id in seen
                    ):
                        continue
                    # Only references in THIS persona's ledger are navigable.
                    if self.service.get_record(record_id) is not None:
                        seen.add(record_id)
                        result["links"].append(
                            {"id": record_id, "label": f"{field.replace('_', ' ')}: {record_id}"}
                        )
        return result

    def summary(self) -> dict:
        data = self.service.summary()
        return {
            "persona_id": self.service.target.persona_id,
            "enabled": data["configured_enabled"],
            "paused": data["paused"],
            "initialized": data["initialized"],
            "counts": data["counts"],
            "statuses": _safe(data["statuses"]),
            "last_activity_at": data["last_activity_at"],
            "pending_outcomes": data["pending_outcomes"],
            "active_methods": [self._present(row) for row in data["active_methods"]],
            "failures": data.get(
                "failure_count",
                sum(data["statuses"].get(status, 0) for status in _ATTENTION_STATUSES),
            ),
            "queue": _safe(data.get("queue", {"pending": 0, "statuses": {}, "jobs": []})),
            "recent_failures": [self._present(row) for row in data["failures"]],
        }

    def list_records(
        self,
        kind: str | None = None,
        *,
        limit: int = 30,
        cursor: str | None = None,
        status: str | None = None,
    ) -> dict:
        if kind is not None and kind not in _KINDS:
            raise ValueError("Unknown learning record kind")
        if not 1 <= limit <= 100:
            raise ValueError("Learning page limit must be between 1 and 100")
        if cursor is not None and len(cursor) > 256:
            raise ValueError("Invalid learning cursor")
        if kind == "failure":
            # UI grouping only; transitions and status ownership stay in core.
            items, next_cursor = [], cursor
            while len(items) < limit:
                page = self.service.list_records(limit=limit - len(items), cursor=next_cursor)
                items.extend(
                    row for row in page["items"] if row.get("status") in _ATTENTION_STATUSES
                )
                next_cursor = page["next_cursor"]
                if next_cursor is None:
                    break
            page = {"items": items, "next_cursor": next_cursor}
        else:
            page = self.service.list_records(kind, limit=limit, cursor=cursor, status=status)
        return {
            "persona_id": self.service.target.persona_id,
            "records": [self._present(row) for row in page["items"]],
            "next_cursor": page["next_cursor"],
        }

    def get_record(self, record_id: str) -> dict:
        row = self.service.get_record(_record_id(record_id))
        if row is None:
            raise LookupError("Learning record not found")
        result = self._present(row, links=True)
        result["payload"]["history"] = _safe(self.service.store.events(record_id))
        return result

    def set_paused(self, paused: bool) -> dict:
        self.service.set_paused(paused)
        return self.summary()

    def rollback(self, activation_id: str) -> dict:
        from personas.learning import promotion

        row = self.service.get_record(_record_id(activation_id))
        if row is None or row["kind"] != "activation":
            raise LookupError("Learning activation not found")
        receipt = promotion.rollback_activation(
            self.service,
            activation_id,
            reason="Operator requested rollback",
        )
        if receipt.get("status") != "rolled_back":
            from personas.learning.models import LearningError

            if receipt.get("status") == "conflict":
                raise LearningError(
                    "Rollback conflicts with newer method changes; inspect the activation history"
                )
            raise LearningError(
                "Rollback failed; inspect the activation history for the recorded failure"
            )
        return _safe(receipt)


def get_learning_operator(persona_id: str) -> LearningOperator:
    from personas.learning import service

    return LearningOperator(service.get_learning_service(persona_id))
