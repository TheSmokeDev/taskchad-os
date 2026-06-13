"""Append-only proactive action queue for autonomous cognition."""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from integrations.capabilities import require_integration_action
except Exception:  # pragma: no cover - optional when imported outside scripts env
    require_integration_action = None  # type: ignore[assignment]

try:
    # Cross-process file lock (F3): the read-then-write dedupe must be atomic
    # across concurrent CROSS-SESSION turns (the router serializes per
    # conversation key only, so two different threads/channels/adapters write
    # the SAME global proactive-actions.jsonl concurrently). Without the lock
    # both turns read _active_dedupe_keys() before either writes -> a duplicate
    # operator nudge or an interleaved JSONL line on win32.
    from shared import file_lock as _file_lock
except Exception:  # pragma: no cover - optional when imported outside scripts env
    _file_lock = None  # type: ignore[assignment]


@contextlib.contextmanager
def _append_lock(path: Path) -> Iterator[None]:
    """Guard the queue read-then-write with the shared cross-process lock (F3).

    Fail-open: if ``shared.file_lock`` is unavailable (module imported outside
    the scripts env) the append proceeds unlocked — best-effort agency must
    never hard-fail a turn, and the in-call/per-turn dedupe still bounds the
    common single-process case.
    """
    if _file_lock is None:
        yield
        return
    with _file_lock(path):
        yield

ACTION_STATUSES = frozenset({
    "queued",
    "policy_rejected",
    "dispatched",
    "failed",
    "skipped",
})


@dataclass
class ProactiveAction:
    """A cognition-selected follow-up or external effect."""

    id: str = ""
    created_at: str = ""
    source: str = ""
    reason: str = ""
    urgency: int = 1
    channel: str = "operator_notification"
    effect: str = "notify"
    message: str = ""
    integration: str = ""
    action: str = ""
    policy_decision: str = ""
    dispatch_status: str = "queued"
    result: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()
        self.dispatch_status = (
            self.dispatch_status
            if self.dispatch_status in ACTION_STATUSES
            else "queued"
        )
        try:
            self.urgency = max(1, min(5, int(self.urgency)))
        except (TypeError, ValueError):
            self.urgency = 1
        self.evidence_paths = [str(path) for path in self.evidence_paths]
        if not self.dedupe_key:
            self.dedupe_key = "|".join([
                self.source.strip().lower(),
                self.channel.strip().lower(),
                self.effect.strip().lower(),
                " ".join(self.message.split()).lower(),
            ])


class ProactiveActionQueue:
    """JSONL queue for proactive action decisions."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, action: ProactiveAction) -> bool:
        """Queue an action unless an active duplicate already exists.

        F3: the dedupe READ (``_active_dedupe_keys``) and the WRITE happen under
        ONE cross-process ``file_lock`` so the dedupe guarantee holds across
        concurrent cross-session turns (two turns in different threads/channels
        writing the same global queue). Without the lock both could read before
        either writes -> a duplicate nudge, or an interleaved JSONL line on
        win32 (no PIPE_BUF atomicity on ``open("a")``).
        """

        if not action.message.strip():
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _append_lock(self._path):
            if action.dedupe_key in self._active_dedupe_keys():
                return False
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(action), ensure_ascii=False) + "\n")
                handle.flush()
        return True

    def read_all(self) -> list[ProactiveAction]:
        """Return all well-formed queued actions."""

        actions: list[ProactiveAction] = []
        for record in self._iter_records():
            action = _coerce_dataclass(ProactiveAction, record)
            if action is not None:
                actions.append(action)
        return actions

    def read_queued(self) -> list[ProactiveAction]:
        """Return actions that have not been dispatched or rejected."""

        return [
            action for action in self.read_all()
            if action.dispatch_status == "queued"
        ]

    def mark(self, action_id: str, **updates: Any) -> bool:
        """Update one queued action record."""

        records = self._iter_records()
        found = False
        for record in records:
            if record.get("id") == action_id:
                record.update(updates)
                found = True
        if found:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return found

    def dispatch_console(self, action_id: str) -> bool:
        """Mark a local operator notification as dispatched.

        This is the deterministic, no-network dispatch path used by tests and
        local dry-run proof. External sends should go through integration
        policy before a real adapter is called.
        """

        action = next((item for item in self.read_queued() if item.id == action_id), None)
        if action is None:
            return False
        if action.channel != "operator_notification":
            return self.mark(
                action.id,
                dispatch_status="policy_rejected",
                policy_decision="reject",
                result="unsupported_dispatch_channel",
            )
        return self.mark(
            action.id,
            dispatch_status="dispatched",
            policy_decision="allow",
            result="console_operator_notification",
        )

    def _iter_records(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            with open(self._path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            return []
        return records

    def _active_dedupe_keys(self) -> set[str]:
        return {
            action.dedupe_key
            for action in self.read_all()
            if action.dispatch_status == "queued"
        }


def evaluate_action_policy(action: ProactiveAction) -> tuple[bool, str]:
    """Return whether a proactive action is allowed to be dispatched."""

    if action.channel == "operator_notification":
        return True, "local_operator_notification"
    if action.integration and action.action and require_integration_action is not None:
        try:
            require_integration_action(
                action.integration,
                action.action,
                surface="internal",
                caller="proactive_actions.evaluate_action_policy",
            )
            return True, "integration_policy_allowed"
        except Exception as exc:
            return False, f"integration_policy_rejected:{exc}"
    return False, "unsupported_channel"


def _coerce_dataclass(cls, record: dict[str, Any]):
    names = {field.name for field in fields(cls)}
    try:
        return cls(**{name: record.get(name) for name in names})
    except (TypeError, ValueError):
        return None


__all__ = (
    "ACTION_STATUSES",
    "ProactiveAction",
    "ProactiveActionQueue",
    "evaluate_action_policy",
)
