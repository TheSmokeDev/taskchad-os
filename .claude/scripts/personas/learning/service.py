"""Canonical persona learning lifecycle over immutable profile-local records."""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from personas.learning.models import (
    LearningContext,
    LearningError,
    LearningTarget,
    canonical_json,
    content_hash,
    is_credential_key,
    resolve_learning_target,
)
from personas.learning.store import RECORD_KINDS, LearningStore

_LOG = logging.getLogger(__name__)
_FALSE = frozenset({"0", "false", "off", "no", "disabled"})
_MODES = frozenset({"real", "study", "practice", "evaluation", "backfill"})
_TAG = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")


def _atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.store.atomic():
            return method(self, *args, **kwargs)

    return wrapped


def _safe(value: Any) -> Any:
    """Reject credential-bearing structures rather than persist an unsafe receipt."""
    if isinstance(value, dict):
        if any(is_credential_key(str(key)) for key in value):
            raise LearningError("credentials cannot be stored as learning evidence")
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    canonical_json(value)
    return value


def _text(value: Any, field: str, *, maximum: int = 65536) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LearningError(f"{field} must be nonempty bounded text")
    return value.strip()


def _instant(value: Any, field: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise LearningError(f"{field} must be an ISO timestamp") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise LearningError(f"{field} must include a timezone")
    return stamp.astimezone(UTC)


class LearningService:
    def __init__(self, target: LearningTarget):
        self.target = target
        self.store = LearningStore(target)

    @classmethod
    def for_persona(cls, persona_id: str) -> LearningService:
        return cls(resolve_learning_target(persona_id))

    def _configured_enabled(self) -> bool:
        if str(os.getenv("PERSONA_LEARNING_ENABLED", "true")).casefold() in _FALSE:
            return False
        if str(os.getenv("HOMIE_KILLSWITCH_HARNESS_LEARNING", "enabled")).casefold() in _FALSE:
            return False
        # Explicit targets without config are embedded/test callers. Real callers
        # always resolve a target with its canonical config path.
        if self.target.config_path is None:
            return True
        if not self.target.config_path.exists():
            return True  # Physical profile already checked by the target resolver.
        from personas import load_persona_config

        config = load_persona_config(self.target.persona_id)
        learning = config.get("learning")
        if learning is None:
            return True
        if not isinstance(learning, dict) or not isinstance(learning.get("enabled", True), bool):
            raise LearningError("invalid persona learning configuration")
        return learning.get("enabled", True)

    def enabled(self) -> bool:
        return self._configured_enabled() and self.store.setting("paused", False) is not True

    def _require_enabled(self) -> None:
        if not self.enabled():
            raise LearningError("persona harness learning is paused or disabled")

    def set_paused(self, paused: bool) -> dict[str, Any]:
        if not isinstance(paused, bool):
            raise LearningError("paused must be boolean")
        self.store.set_setting("paused", paused)
        return self.summary()

    def _owned(self, record_id: str, kind: str | None = None) -> dict[str, Any]:
        record = self.store.get(record_id)
        if record is None or (kind is not None and record["kind"] != kind):
            raise LearningError("learning reference is missing or belongs to another profile")
        return record

    def _notify(self, record: dict[str, Any]) -> None:
        # Scheduling is a separate resumable owner. A queue failure must not turn
        # a durable experience write into a reported failure and duplicate retry.
        try:
            from personas.learning.queue import notify_record
        except ImportError:
            return
        try:
            notify_record(self, record)
        except Exception as exc:
            _LOG.warning("learning queue notification failed: %s", type(exc).__name__)

    def capture_experience(
        self,
        origin_key: str,
        surface: str,
        task: str,
        *,
        mode: str = "real",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        if mode not in _MODES:
            raise LearningError("invalid learning experience mode")
        record = self.store.put(
            "experience",
            {
                "origin_key": _text(origin_key, "origin_key", maximum=2048),
                "surface": _text(surface, "surface", maximum=100),
                "task": _text(task, "task"),
                "mode": mode,
                "metadata": _safe(metadata or {}),
                "status": "captured",
            },
            key=f"{surface}:{origin_key}",
        )
        self._notify(record)
        return record

    @_atomic
    def commit_expectation(
        self, experience_id: str, expectation: dict[str, Any], *, action_key: str = "primary"
    ) -> dict[str, Any]:
        self._require_enabled()
        experience = self._owned(experience_id, "experience")
        data = _safe(dict(expectation))
        data["claim"] = _text(data.get("claim"), "claim", maximum=4000)
        data["resolution_rule"] = _text(
            data.get("resolution_rule"), "resolution_rule", maximum=8000
        )
        deadline = _instant(data.get("check_by"), "check_by")
        if not isinstance(data.get("situation"), dict) or not data["situation"]:
            raise LearningError("expectation requires a point-in-time situation snapshot")
        phase = data.setdefault("phase", "pre_action")
        if phase not in {"pre_action", "pre_publication", "retrospective"}:
            raise LearningError("invalid expectation phase")
        if experience["mode"] == "backfill" and phase != "retrospective":
            raise LearningError("backfill cannot manufacture preregistration")
        # Retry an already committed identical expectation even after its deadline.
        key = f"{experience_id}:{action_key}"
        existing_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"homie-learning:{self.target.persona_id}:expectation:{key}"
            )
        )
        if phase == "pre_action" and self.store.get(existing_id) is None:
            if any(
                record.get("experience_id") == experience_id
                and record.get("action_key", "primary") == action_key
                for record in self.store.all("execution")
            ):
                raise LearningError("cannot preregister an action after its execution")
        if (
            phase != "retrospective"
            and deadline <= datetime.now(UTC)
            and self.store.get(existing_id) is None
        ):
            raise LearningError("new preregistered expectation must resolve in the future")
        if data.setdefault("action", "act") not in {"act", "pass"}:
            raise LearningError("expectation action must be act or pass")
        tags = data.setdefault("thesis_tags", [])
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) or not _TAG.fullmatch(tag) for tag in tags
        ):
            raise LearningError("thesis_tags must be normalized identifiers")
        confidence = data.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise LearningError("confidence must be a probability")
        data.update(experience_id=experience_id, action_key=action_key, status="open")
        record = self.store.put("expectation", data, key=key)
        self._notify(record)
        return record

    def record_execution(
        self, experience_id: str, receipt: dict[str, Any], *, attempt_key: str
    ) -> dict[str, Any]:
        self._owned(experience_id, "experience")
        data = _safe(dict(receipt))
        data.update(
            experience_id=experience_id, attempt_key=_text(attempt_key, "attempt_key", maximum=2048)
        )
        data.setdefault(
            "status",
            "executed"
            if data.get("success") is True
            else "failed"
            if data.get("success") is False
            else "recorded",
        )
        record = self.store.put("execution", data, key=f"{experience_id}:{attempt_key}")
        self._notify(record)
        return record

    @_atomic
    def record_observation(
        self, experience_id: str, observation: dict[str, Any], *, source_key: str
    ) -> dict[str, Any]:
        experience = self._owned(experience_id, "experience")
        data = _safe(dict(observation))
        if data.get("quality") not in {"direct", "proxy", "inferred"}:
            raise LearningError("observation quality is required")
        if data.get("status") not in {"open", "resolved", "partial", "unresolvable"}:
            raise LearningError("invalid observation status")
        if not isinstance(data.get("evidence"), (str, dict)) or not data["evidence"]:
            raise LearningError(
                "observation requires captured evidence or an explicit unavailable receipt"
            )
        if data.get("held") is not None and not isinstance(data["held"], bool):
            raise LearningError("held must be boolean or null")
        if data["status"] in {"open", "unresolvable"} and data.get("held") is not None:
            raise LearningError("unobserved outcomes cannot be graded true or false")
        if data.get("occurred_at") is not None:
            _instant(data["occurred_at"], "occurred_at")
        if data.get("expectation_id"):
            expected = self._owned(data["expectation_id"], "expectation")
            if expected["experience_id"] != experience_id:
                raise LearningError("observation expectation belongs to a different experience")
        supersedes = data.get("supersedes")
        if supersedes:
            old = self._owned(supersedes, "observation")
            if old["experience_id"] != experience_id:
                raise LearningError("correction belongs to a different experience")
        data.update(experience_id=experience_id, mode=experience["mode"])
        record = self.store.put("observation", data, key=f"{experience_id}:{source_key}")
        if supersedes:
            self.set_status(
                supersedes, "superseded", reason="new observation", key=f"superseded:{record['id']}"
            )
            for candidate in self.store.all("candidate"):
                if supersedes in candidate.get("evidence_ids", []) + candidate.get(
                    "counterevidence_ids", []
                ):
                    self.set_status(
                        candidate["id"],
                        "needs_reassessment",
                        reason="supporting observation corrected",
                        key=f"corrected:{record['id']}",
                    )
        if data.get("expectation_id"):
            self.set_status(
                data["expectation_id"],
                data["status"],
                reason="observation recorded",
                key=f"observation:{record['id']}",
            )
        self._notify(record)
        return record

    def evidence_records(self, ids: list[str]) -> list[dict[str, Any]]:
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise LearningError("evidence references must be record IDs")
        records = [self._owned(record_id) for record_id in ids]
        if any(record.get("status") == "superseded" for record in records):
            raise LearningError("superseded evidence requires reassessment")
        return records

    def propose_candidate(self, candidate: dict[str, Any], *, source_key: str) -> dict[str, Any]:
        self._require_enabled()
        data = _safe(dict(candidate))
        kind = data.get("candidate_type")
        if kind not in {"knowledge", "self_model", "procedure"}:
            raise LearningError("invalid learning candidate type")
        for field in ("title", "content", "applicability"):
            data[field] = _text(data.get(field), field)
        for field in ("evidence_ids", "counterevidence_ids"):
            data.setdefault(field, [])
            self.evidence_records(data[field])
        data.setdefault("changes_behavior", kind == "procedure")
        if not isinstance(data["changes_behavior"], bool):
            raise LearningError("changes_behavior must be boolean")
        if kind == "procedure":
            data["changes_behavior"] = True
        data.setdefault("target_file", "MEMORY.md")
        if data["target_file"] not in {"MEMORY.md", "SELF.md", "SOUL.md", "skill"}:
            raise LearningError("learning candidate target is outside the allowed content surfaces")
        data.setdefault("uncertainty", "unverified")
        data.setdefault("baseline_version", None)
        if data.get("prior_candidate_id"):
            self._owned(data["prior_candidate_id"], "candidate")
        # Hash before host-owned status is added; mutable projection never changes it.
        data.pop("content_hash", None)
        data.pop("status", None)
        data["content_hash"] = content_hash(data)
        data["status"] = "proposed"
        record = self.store.put("candidate", data, key=source_key)
        self._notify(record)
        return record

    def record_evaluation(
        self, candidate_id: str, evaluation: dict[str, Any], *, run_key: str
    ) -> dict[str, Any]:
        candidate = self._owned(candidate_id, "candidate")
        data = _safe(dict(evaluation))
        if data.get("candidate_hash") not in {None, candidate["content_hash"]}:
            raise LearningError("evaluation candidate hash mismatch")
        data.update(candidate_id=candidate_id, candidate_hash=candidate["content_hash"])
        data.setdefault("status", "passed" if data.get("passed") is True else "failed")
        return self.store.put("evaluation", data, key=f"{candidate_id}:{run_key}")

    @_atomic
    def record_activation(
        self, candidate_id: str, receipt: dict[str, Any], *, activation_key: str
    ) -> dict[str, Any]:
        self._require_enabled()
        candidate = self._owned(candidate_id, "candidate")
        self.evidence_records(
            candidate.get("evidence_ids", []) + candidate.get("counterevidence_ids", [])
        )
        if candidate.get("status") in {"needs_reassessment", "retired", "rejected"}:
            raise LearningError("candidate requires a fresh supported evaluation before activation")
        data = _safe(dict(receipt))
        evaluation = self._owned(data.get("evaluation_id", ""), "evaluation")
        if (
            evaluation.get("candidate_id") != candidate_id
            or evaluation.get("candidate_hash") != candidate["content_hash"]
        ):
            raise LearningError("activation evaluation does not bind this candidate")
        mode = evaluation.get("mode", "qualification")
        knowledge_support = (
            mode == "knowledge_support"
            and candidate["candidate_type"] in {"knowledge", "self_model"}
            and candidate.get("changes_behavior") is False
        )
        if evaluation.get("passed") is not True or (
            mode != "qualification" and not knowledge_support
        ):
            raise LearningError("activation requires a passed qualification receipt")
        if data.get("candidate_hash") != candidate["content_hash"]:
            raise LearningError("activation candidate hash mismatch")
        if (
            not data.get("amendment_id")
            and not data.get("skill_name")
            and not data.get("application_receipt")
        ):
            raise LearningError("activation requires a physical application receipt")
        method_status = data.setdefault("method_status", "active_provisional")
        if method_status not in {"active_provisional", "active_supported"}:
            raise LearningError("invalid activated method status")
        data.update(candidate_id=candidate_id, status=method_status)
        record = self.store.put("activation", data, key=f"{candidate_id}:{activation_key}")
        self.set_status(
            candidate_id,
            method_status,
            reason="evaluated application",
            key=f"activation:{record['id']}",
        )
        return record

    @_atomic
    def record_rollback(
        self, activation_id: str, receipt: dict[str, Any], *, rollback_key: str
    ) -> dict[str, Any]:
        activation = self._owned(activation_id, "activation")
        data = _safe(dict(receipt))
        self.store.event(activation_id, "rollback", data, key=rollback_key)
        self.set_status(
            activation["candidate_id"],
            "retired",
            reason=data.get("reason", "rolled back"),
            key=f"rollback:{activation_id}:{rollback_key}",
        )
        return self._owned(activation_id)

    def set_status(
        self,
        record_id: str,
        status: str,
        *,
        reason: str = "",
        key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._owned(record_id)
        self.store.event(
            record_id,
            "status",
            {
                "status": _text(status, "status", maximum=100),
                "reason": str(reason),
                "metadata": _safe(metadata or {}),
            },
            key=key or str(uuid.uuid4()),
        )
        return self._owned(record_id)

    def _active_methods(self, task: str | None = None) -> list[dict[str, Any]]:
        active = []
        seen = set()
        pairs = self.store.active_pairs(task)
        evidence_ids = list(
            {
                key
                for pair in pairs
                for key in pair["candidate"].get("evidence_ids", [])
                + pair["candidate"].get("counterevidence_ids", [])
            }
        )
        evidence = self.store.many(evidence_ids)
        for activation in pairs:
            candidate = activation["candidate"]
            if any(
                key not in evidence or evidence[key].get("status") == "superseded"
                for key in candidate.get("evidence_ids", [])
                + candidate.get("counterevidence_ids", [])
            ):
                continue
            identity = candidate["id"]
            if identity in seen:
                continue
            seen.add(identity)
            active.append(activation | {"candidate": candidate})
        return active

    def _context_methods(
        self, task: str, *, max_chars: int, candidate: dict | None = None
    ) -> tuple[dict, ...]:
        """Snapshot only physically applied methods needed by either paired view.

        The indexed selector remains bounded. Check just renderable methods, then
        refill if a stale physical application was excluded; no per-method DB scan.
        """
        from .context import compile_context, method_snapshot, prospective_methods
        from .promotion import activation_is_applied

        methods = self._active_methods(task)
        checked = set()
        while True:
            before = [
                m for m in methods if not candidate or m["candidate"]["id"] != candidate["id"]
            ]
            variants = [compile_context(task, before, max_chars=max_chars)]
            if candidate:
                variants.append(
                    compile_context(
                        task, prospective_methods(methods, candidate), max_chars=max_chars
                    )
                )
            needed = {v["activation_id"] for view in variants for v in view.versions}
            invalid = set()
            for method in methods:
                if method["id"] in needed and method["id"] not in checked:
                    if not activation_is_applied(self, method):
                        invalid.add(method["id"])
                    checked.add(method["id"])
            if not invalid:
                return tuple(method_snapshot(m) for m in methods if m["id"] in needed)
            methods = [m for m in methods if m["id"] not in invalid]

    def preview_context(
        self, task: str, candidate: dict, *, max_chars: int = 2000
    ) -> dict[str, Any]:
        """Freeze actual before/after deployment inputs without activating a method."""
        from dataclasses import asdict

        from .context import CONTEXT_COMPILER_VERSION, compile_context, prospective_methods

        methods = self._context_methods(task, max_chars=max_chars, candidate=candidate)
        # Requalification compares removal versus continued use of the same
        # method. A new/replacement method compares the exact current incumbents.
        baseline = tuple(m for m in methods if m["candidate"]["id"] != candidate["id"])
        after = prospective_methods(methods, candidate)
        return {
            "compiler_version": CONTEXT_COMPILER_VERSION,
            "task": task,
            "max_chars": max_chars,
            "baseline_methods": baseline,
            "candidate_methods": after,
            "baseline": asdict(compile_context(task, baseline, max_chars=max_chars)),
            "candidate": asdict(compile_context(task, after, max_chars=max_chars)),
        }

    def render_context(
        self, task: str, *, max_chars: int = 2000, model: str | None = None
    ) -> LearningContext:
        from .context import compile_context

        if not self.enabled() or max_chars <= 0:
            return LearningContext()
        methods = self._context_methods(task, max_chars=max_chars)
        return compile_context(task, methods, max_chars=max_chars)

    def record_context_receipt(
        self,
        experience_id: str,
        context: LearningContext,
        rendered_prompt: str,
        *,
        attempt_key: str,
        model: str | None = None,
        provider: str | None = None,
        phase: str = "executed",
    ) -> dict[str, Any]:
        self._owned(experience_id, "experience")
        if phase not in {"prepared", "submitted", "executed"}:
            raise LearningError("invalid context receipt phase")
        included, dropped = [], []
        for version in context.versions:
            reference = {
                key: version[key] for key in ("candidate_id", "activation_id", "content_hash")
            }
            block = version.get("rendered_block", version["content"])
            (included if block in rendered_prompt else dropped).append(reference)
        return self.store.put(
            "context",
            {
                "experience_id": experience_id,
                "attempt_key": attempt_key,
                "context_hash": context.context_hash,
                "rendered_prompt_hash": content_hash(rendered_prompt),
                "model": model,
                "provider": provider,
                "phase": phase,
                "included": included,
                "dropped": dropped,
                "status": ("delivered" if phase == "executed" else phase)
                if included
                else "not_delivered"
                if dropped
                else "empty",
            },
            key=f"{experience_id}:{attempt_key}",
        )

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        result = self.store.get(record_id)
        return result | {"events": self.store.events(record_id)} if result else None

    def list_records(
        self,
        kind: str | None = None,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        if status is None:
            return self.store.list(kind, limit=limit, cursor=cursor)
        # Filter while paging source rows; next_cursor always advances in storage.
        items, next_cursor = [], cursor
        while len(items) < limit:
            page = self.store.list(kind, limit=limit - len(items), cursor=next_cursor)
            items.extend(item for item in page["items"] if item.get("status") == status)
            next_cursor = page["next_cursor"]
            if next_cursor is None:
                break
        return {"items": items, "next_cursor": next_cursor}

    def summary(self) -> dict[str, Any]:
        from personas.learning.queue import LearningQueue

        records = self.store.all()
        counts = Counter(record["kind"] for record in records)
        statuses = Counter(record.get("status", "unknown") for record in records)
        with self.store.connection() as connection:
            coverage_ids = (
                {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT record_id FROM events WHERE event_type='coverage_failure'"
                    )
                }
                if connection is not None
                else set()
            )
        failures = [
            record
            for record in records
            if record["id"] in coverage_ids
            or record.get("status") in {"failed", "deferred", "needs_reassessment", "unresolvable"}
        ]
        jobs = LearningQueue(self).list(include_finished=True)
        pending_jobs = [job for job in jobs if job["status"] != "completed"]
        failed_jobs = [job for job in pending_jobs if job["status"] in {"retry", "failed"}]
        queue_rows = []
        for job in sorted(pending_jobs, key=lambda row: row["updated_at"], reverse=True)[:20]:
            row = {
                key: job.get(key)
                for key in ("id", "kind", "stage", "status", "last_error", "failures")
            }
            payload = job.get("payload", {})
            row["record_id"] = next(
                (
                    payload[key]
                    for key in ("candidate_id", "expectation_id", "experience_id")
                    if payload.get(key) and self.store.get(payload[key]) is not None
                ),
                None,
            )
            queue_rows.append(row)
        return {
            "persona_id": self.target.persona_id,
            "enabled": self.enabled(),
            "configured_enabled": self._configured_enabled(),
            "paused": self.store.setting("paused", False),
            "initialized": self.store.path.exists(),
            "counts": {kind: counts[kind] for kind in sorted(RECORD_KINDS)},
            "statuses": dict(statuses),
            "last_activity_at": records[0]["created_at"] if records else None,
            "pending_outcomes": sum(
                record["kind"] == "expectation" and record.get("status") in {"open", "partial"}
                for record in records
            ),
            "active_methods": self._active_methods(),
            "recent": records[:20],
            "failures": failures[:20],
            "failure_count": len(failures) + len(failed_jobs),
            "coverage_failure_count": len(coverage_ids),
            "queue": {
                "pending": sum(job["status"] != "failed" for job in pending_jobs),
                "statuses": dict(Counter(job["status"] for job in jobs)),
                "jobs": queue_rows,
            },
        }


def get_learning_service(persona_id: str) -> LearningService:
    return LearningService.for_persona(persona_id)
