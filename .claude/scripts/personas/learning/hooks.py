"""Host-owned learning adapters for every interactive persona surface.

The final envelope precedes publication, never drafting. Provider-owned internal
steps have no trustworthy host callback and are explicitly outside coverage.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)
_CURRENT_TURN = contextvars.ContextVar("persona_learning_turn", default=None)
_CURRENT_ACTION = contextvars.ContextVar("persona_learning_action", default=None)
_ENVELOPE = re.compile(r"\s*<<LEARNING_EXPECTATION:\s*(\{.*?\})\s*>>\s*$", re.S)
_GUIDANCE = (
    "\n\n# Learning from this task\n"
    "Use relevant learned methods below when their conditions apply. For a meaningful "
    "action, use record_expectation if available BEFORE acting: state your own "
    "testable claim, observation deadline, resolution rule and current situation. "
    "Do not invent a prediction just to fill a record. For a recommendation with "
    'a downstream outcome, you may append <<LEARNING_EXPECTATION:{"claim":"...",'
    '"check_by":"aware ISO instant","resolution_rule":"...",'
    '"situation":{"context":"..."}}>> to your reply. The host captures '
    "that before publication, not before drafting. Ordinary conversation needs no prediction.\n"
)


def _service_for(persona_id: str):
    from personas.learning.service import get_learning_service

    return get_learning_service(persona_id)


def canonical_turn_id(incoming: Any) -> str:
    """One ingress ID shared by learning and permission-resume bookkeeping."""
    raw = getattr(incoming, "raw_event", None)
    raw = raw if isinstance(raw, dict) else {}
    turn = (
        raw.get("elevation_original_turn_id")
        or getattr(incoming, "platform_message_id", None)
        or raw.get("learning_generated_turn_id")
        or uuid.uuid4().hex
    )
    raw.setdefault("elevation_original_turn_id", str(turn))
    try:
        incoming.raw_event = raw
    except (AttributeError, TypeError):
        pass
    return str(turn)


def incoming_origin(incoming: Any, session_key: str) -> str:
    """Preserve the original turn across approval resumes; text is not an ID."""
    raw = getattr(incoming, "raw_event", None)
    raw = raw if isinstance(raw, dict) else {}
    inherited = os.environ.get("HOMIE_LEARNING_ORIGIN_KEY") or raw.get("learning_origin_key")
    if inherited:
        # A delegated child may receive a fresh CLI session on retry.
        return str(inherited)
    return f"{session_key}:{canonical_turn_id(incoming)}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _runtime_meta(result: Any) -> dict:
    return {
        name: getattr(result, name, None)
        for name in ("runtime_lane", "provider", "model", "session_id", "tool_call_count")
    }


def record_actor_expectation(expectation: dict, *, persona_id: str) -> dict:
    turn = _CURRENT_TURN.get()
    if turn is None or turn.persona_id != persona_id:
        raise ValueError("no matching host-owned learning turn")
    return turn.commit_actor_expectation(expectation)


def current_action_learning_context(*, persona_id: str) -> dict[str, str] | None:
    """Host-only linkage to the consumed expectation of the current action.

    A proposal owner may persist this in its exact-payload approval record.
    Never accept these IDs from model arguments or infer an ambient persona.
    """
    turn, action = _CURRENT_TURN.get(), _CURRENT_ACTION.get()
    if (
        turn is None
        or turn.persona_id != persona_id
        or not turn.experience
        or not action
        or not action.get("expectation_id")
    ):
        return None
    return {
        "persona_id": persona_id,
        "experience_id": turn.experience["id"],
        "expectation_id": action["expectation_id"],
    }


@dataclass
class SurfaceTurn:
    request: Any
    persona_id: str
    surface: str
    origin_id: str
    attempt_id: str
    require_capture: bool = False
    service: Any = None
    experience: dict | None = None
    context: Any = None
    expectation: dict | None = None
    action_count: int = 0
    failures: list[str] = field(default_factory=list)
    runtime_attempt: dict = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.RLock, repr=False)
    _expectations: dict[str, dict] = field(default_factory=dict, repr=False)

    def failure(self, operation: str, exc: BaseException | str) -> None:
        # Do not log exception payloads: storage failures may embed private data.
        reason = (
            f"{operation}:{type(exc).__name__}"
            if isinstance(exc, BaseException)
            else f"{operation}:{exc}"
        )
        self.failures.append(reason)
        self.request.metadata["learning"]["coverage_failures"] = list(self.failures)
        logger.warning(
            "learning coverage failed persona=%s surface=%s operation=%s",
            self.persona_id,
            self.surface,
            reason,
        )
        if self.service is not None and self.experience is not None:
            try:
                self.service.store.event(
                    self.experience["id"],
                    "coverage_failure",
                    {"operation": operation, "reason": reason},
                    key=f"{self.attempt_id}:{len(self.failures)}",
                )
            except Exception:
                pass
        if self.require_capture:
            raise RuntimeError(f"learning capture required: {reason}") from (
                exc if isinstance(exc, BaseException) else None
            )

    def commit_actor_expectation(self, expectation: dict) -> dict:
        if self.service is None or self.experience is None:
            raise ValueError("learning capture unavailable")
        from runtime.base import current_runtime_attempt

        attempt_id = current_runtime_attempt().get("attempt_id", self.attempt_id)
        payload = dict(expectation)
        payload["phase"] = "pre_action"
        payload["author"] = "persona"
        committed = self.service.commit_expectation(
            self.experience["id"],
            payload,
            action_key=f"{attempt_id}:claim:{uuid.uuid4().hex}",
        )
        with self._lock:
            self._expectations[attempt_id] = copy.deepcopy(committed)
            if attempt_id == self.attempt_id:
                self.expectation = self._expectations[attempt_id]
        return committed

    def _begin_action(self, name, arguments):
        if name == "record_expectation" or self.experience is None:
            return None
        from runtime import tool_registry
        from runtime.base import current_runtime_attempt

        runtime = current_runtime_attempt() or dict(self.runtime_attempt)
        attempt_id = runtime.get("attempt_id", self.attempt_id)
        entry = tool_registry.get_entry(name)
        effect = entry.effect if entry is not None else "unknown"
        requires_expectation = effect in {"write", "execute"}
        # Reserve an action and consume its prediction together. Never hold this
        # lock across SQLite or tool I/O; parallel calls retain immutable copies.
        with self._lock:
            self.action_count += 1
            expected = self._expectations.get(attempt_id)
            action = {
                "action_key": f"{attempt_id}:tool:{self.action_count}",
                "attempt_id": attempt_id,
                "expectation_id": (expected or {}).get("id") if effect != "read" else None,
                "effect": effect,
                "expectation_requirement": "required"
                if requires_expectation
                else "unclassified"
                if effect == "unknown"
                else "not_required",
                "runtime": runtime,
            }
            if effect != "read":
                self._expectations.pop(attempt_id, None)
                if attempt_id == self.attempt_id:
                    self.expectation = None
        if action["expectation_id"] is None and (
            requires_expectation or self.require_capture and effect == "unknown"
        ):
            self.failure("pre_action", "missing_actor_expectation")
        try:
            self.service.record_execution(
                self.experience["id"],
                {
                    "stage": "started",
                    **action,
                    "tool": name,
                    "argument_hash": _hash(arguments),
                    "coverage": "host_dispatch_only",
                },
                attempt_key=f"{action['action_key']}:started",
            )
        except Exception as exc:
            self.failure("action_started", exc)
        return action

    def wrap_dispatch(self, original):
        async def await_result(awaitable, action, name):
            token = _CURRENT_TURN.set(self)
            action_token = _CURRENT_ACTION.set(action)
            try:
                result = await awaitable
                if action:
                    await asyncio.to_thread(self._tool_result, action, name, result)
                return result
            except BaseException as exc:
                if action:
                    await asyncio.to_thread(
                        self._tool_result, action, name, None, type(exc).__name__
                    )
                raise
            finally:
                _CURRENT_ACTION.reset(action_token)
                _CURRENT_TURN.reset(token)

        async def dispatch_async(name, arguments=None):
            token = _CURRENT_TURN.set(self)
            action = None
            try:
                action = await asyncio.to_thread(self._begin_action, name, arguments)
                # Keep the ContextVar active in the actual await, not merely
                # while constructing the coroutine object.
                return await await_result(original(name, arguments), action, name)
            finally:
                _CURRENT_TURN.reset(token)

        def dispatch_sync(name, arguments=None):
            token = _CURRENT_TURN.set(self)
            action_token = _CURRENT_ACTION.set(None)
            action = None
            try:
                action = self._begin_action(name, arguments)
                _CURRENT_ACTION.set(action)
                result = original(name, arguments)
                if inspect.isawaitable(result):
                    return await_result(result, action, name)
                if action:
                    self._tool_result(action, name, result)
                return result
            except BaseException as exc:
                if action:
                    self._tool_result(action, name, None, type(exc).__name__)
                raise
            finally:
                _CURRENT_ACTION.reset(action_token)
                _CURRENT_TURN.reset(token)

        return dispatch_async if inspect.iscoroutinefunction(original) else dispatch_sync

    def _tool_result(self, action, name, result, error=None):
        try:
            evidence = result
            if isinstance(result, str):
                try:
                    evidence = json.loads(result)
                except (ValueError, TypeError):
                    evidence = result[:8000]
            elif not isinstance(result, (dict, list, int, float, bool, type(None))):
                evidence = str(result)[:8000]
            self.service.record_execution(
                self.experience["id"],
                {
                    "stage": "failed" if error else "returned",
                    **action,
                    "tool": name,
                    "result": evidence,
                    "error": error,
                },
                attempt_key=f"{action['action_key']}:returned",
            )
        except Exception as exc:
            self.failure("action_result", exc)

    async def attempt_observer(self, event: dict) -> None:
        await asyncio.to_thread(self._record_runtime_attempt, event)

    def _record_runtime_attempt(self, event: dict) -> None:
        if event["phase"] == "started":
            with self._lock:
                self.attempt_id = event["attempt_id"]
                self.runtime_attempt = dict(event)
                self.expectation = None
            self.request.metadata["learning"]["attempt_key"] = self.attempt_id
        if self.service is None or self.experience is None:
            return
        try:
            receipt = None
            if event["phase"] == "started" and self.context is not None:
                receipt = self.service.record_context_receipt(
                    self.experience["id"],
                    self.context,
                    self.request.prompt,
                    attempt_key=event["attempt_id"],
                    model=event.get("model"),
                    provider=event.get("provider"),
                    phase="submitted",
                )
            self.service.record_execution(
                self.experience["id"],
                {
                    "stage": "runtime_" + event["phase"],
                    "runtime": event,
                    "context_receipt_id": (receipt or {}).get("id"),
                },
                attempt_key=f"{event['attempt_id']}:runtime:{event['phase']}",
            )
        except Exception as exc:
            self.failure("runtime_attempt", exc)

    async def acomplete(self, result: Any) -> str:
        return await asyncio.to_thread(self.complete, result)

    async def afailed(self, exc: BaseException) -> None:
        await asyncio.to_thread(self.failed, exc)

    async def aretry_request(self, request: Any, *, reason: str) -> Any:
        return await asyncio.to_thread(self.retry_request, request, reason=reason)

    def retry_request(self, request: Any, *, reason: str) -> Any:
        """Prepare a retry; the lane callback records its actual submission."""
        self.attempt_id = uuid.uuid4().hex
        metadata = dict(request.metadata or {})
        metadata["learning"] = dict(metadata.get("learning") or {})
        metadata["learning"].update(attempt_key=self.attempt_id, retry_reason=reason)
        self.request = replace(request, metadata=metadata)
        return self.request

    def complete(self, result: Any) -> str:
        text = str(getattr(result, "text", "") or "").strip()
        marker = _ENVELOPE.search(text)
        if marker:
            # Remove the private marker even when malformed or learning disabled.
            text = text[: marker.start()].rstrip()
            if self.service is not None and self.experience is not None:
                try:
                    payload = json.loads(marker.group(1))
                    payload.update(phase="pre_publication", author="persona")
                    self.service.commit_expectation(
                        self.experience["id"], payload, action_key=f"publication:{self.attempt_id}"
                    )
                except Exception as exc:
                    self.failure("publication_expectation", exc)
        if self.service is not None and self.experience is not None:
            try:
                actual_context = None
                if self.context is not None:
                    actual_context = self.service.record_context_receipt(
                        self.experience["id"],
                        self.context,
                        self.request.prompt,
                        attempt_key=f"{self.attempt_id}:actual_runtime",
                        model=getattr(result, "model", None),
                        provider=getattr(result, "provider", None),
                    )
                self.service.record_execution(
                    self.experience["id"],
                    {
                        "stage": "generated",
                        "context_receipt_id": (actual_context or {}).get("id"),
                        "included_activation_ids": [
                            v["activation_id"] for v in (actual_context or {}).get("included", [])
                        ],
                        "model": getattr(result, "model", None),
                        "provider": getattr(result, "provider", None),
                        "artifact": text,
                        "artifact_hash": _hash(text),
                        "publication_confirmed": False,
                        "runtime": _runtime_meta(result),
                        "coverage": "host_handoff; native_internal_steps_uncaptured",
                    },
                    attempt_key=f"{self.attempt_id}:generated",
                )
                self.service.record_observation(
                    self.experience["id"],
                    {
                        "quality": "direct",
                        "status": "partial",
                        "evidence": {
                            "kind": "runtime_artifact",
                            "artifact_hash": _hash(text),
                            "runtime": _runtime_meta(result),
                        },
                        "domain_outcome_observed": False,
                    },
                    source_key=f"{self.attempt_id}:artifact",
                )
            except Exception as exc:
                self.failure("completion", exc)
        return text

    def failed(self, exc: BaseException) -> None:
        if self.service is not None and self.experience is not None:
            try:
                self.service.record_execution(
                    self.experience["id"],
                    {
                        "stage": "failed",
                        "error_type": type(exc).__name__,
                    },
                    attempt_key=f"{self.attempt_id}:failed",
                )
            except Exception as capture_exc:
                self.failure("runtime_failure", capture_exc)


def prepare_turn(
    request: Any,
    *,
    persona_id: str,
    surface: str,
    origin_id: str,
    attempt_id: str | None = None,
    require_capture: bool = False,
    service: Any = None,
    task: str | None = None,
    capture_only: bool = False,
    capture_metadata: dict | None = None,
) -> SurfaceTurn:
    """Attach relevant content and receipts without modifying identity or tools.

    Called after a surface's final prompt clamp. Context uses the turn prompt
    (stdin) and has its own 2K character budget; it cannot push identity over the
    Windows argv ceiling. Receipt inspects the exact outgoing prompt string.
    """
    request = replace(request, metadata=dict(request.metadata or {}))
    attempt = attempt_id or uuid.uuid4().hex
    request.metadata["persona_id"] = persona_id
    request.metadata["learning"] = {
        "surface": surface,
        "origin_key": origin_id,
        "attempt_key": attempt,
        "coverage": "host_dispatch_and_pre_publication",
        "native_internal_steps": "uncaptured",
        "coverage_failures": [],
    }
    turn = SurfaceTurn(request, persona_id, surface, origin_id, attempt, require_capture)
    request.attempt_observer = turn.attempt_observer
    try:
        turn.service = service if service is not None else _service_for(persona_id)
        if not turn.service.enabled():
            request.metadata["learning"]["coverage"] = "disabled"
            turn.service = None
            return turn
        turn.experience = turn.service.capture_experience(
            origin_id,
            surface,
            task if task is not None else request.prompt,
            metadata={
                "capture_scope": "host",
                "native_internal_steps": "uncaptured",
                **(capture_metadata or {}),
            },
        )
        request.metadata["learning"]["experience_id"] = turn.experience["id"]
        if capture_only:
            request.metadata["learning"]["coverage"] = "host_capture_only; no_model_request"
            return turn
        turn.context = turn.service.render_context(
            request.prompt, max_chars=2000, model=request.model
        )
        request.prompt = _GUIDANCE.lstrip() + turn.context.text + "\n\n" + request.prompt
        request.metadata["learning"]["selected_versions"] = [
            {
                key: value
                for key, value in version.items()
                if key not in {"content", "rendered_block"}
            }
            for version in turn.context.versions
        ]
        request.metadata["learning"]["experience_id"] = turn.experience["id"]
        if request.tool_dispatch is not None and not request.model_only:
            request.tool_dispatch = turn.wrap_dispatch(request.tool_dispatch)
    except Exception as exc:
        turn.failure("prepare", exc)
    return turn


async def prepare_turn_async(request: Any, **kwargs) -> SurfaceTurn:
    """Run blocking profile/database preparation outside interactive loops."""
    return await asyncio.to_thread(prepare_turn, request, **kwargs)
