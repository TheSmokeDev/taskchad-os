"""Model-independent framework function hooks.

Adapters emit host-attributed events. Registered Python middleware can observe
or wrap delivery; the built-in cognitive handler persists work for the shared
reasoning worker. No provider SDK or model-specific hook feature is required.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

_EVENT_PHASE = {
    "work.start": "reorient",
    "evidence.received": "interpret",
    "work.completed": "reflect",
    "work.failed": "reflect",
    "session.closed": "reflect",
    "investigation.due": "revisit",
}
_PHASE_EVENT = {
    "reorient": "work.start",
    "interpret": "evidence.received",
    "reflect": "work.completed",
    "revisit": "investigation.due",
}
_LOCK = RLock()
_HANDLERS: dict[str, tuple[str, Callable]] = {}


@dataclass(frozen=True)
class FunctionHookEvent:
    name: str
    persona_id: str
    activity_id: str
    evidence_ids: tuple[str, ...]
    experience_id: str | None = None
    investigation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def register_hook(name: str, callback: Callable, *, key: str) -> None:
    """Register trusted framework middleware as (event, next_handler) -> receipt."""
    if name not in _EVENT_PHASE and name != "*":
        raise ValueError("unknown framework hook event")
    if not key or not callable(callback):
        raise ValueError("hook requires a stable key and callable handler")
    with _LOCK:
        if key in _HANDLERS and _HANDLERS[key] != (name, callback):
            raise ValueError("hook key is already registered")
        _HANDLERS[key] = (name, callback)


def unregister_hook(key: str) -> None:
    with _LOCK:
        _HANDLERS.pop(key, None)


def emit(event: FunctionHookEvent, *, service=None) -> dict:
    """Deliver one event independent of the source model; no inference runs here."""
    if event.name not in _EVENT_PHASE:
        raise ValueError("unknown framework hook event")
    from personas.learning.service import get_learning_service

    service = service or get_learning_service(event.persona_id)
    if service.target.persona_id != event.persona_id:
        raise ValueError("hook service belongs to another persona")
    with _LOCK:
        handlers = [callback for name, callback in _HANDLERS.values() if name in {event.name, "*"}]

    def deliver(index):
        if index < len(handlers):
            result = handlers[index](event, lambda: deliver(index + 1))
            if not isinstance(result, dict):
                raise TypeError("hook must return its durable receipt")
            return result
        return service.enqueue_cognitive_cycle(
            _EVENT_PHASE[event.name],
            event.activity_id,
            list(event.evidence_ids),
            experience_id=event.experience_id,
            investigation_id=event.investigation_id,
            metadata={**event.metadata, "framework_hook": event.name},
        )

    return deliver(0)


def emit_cognitive_event(
    phase,
    origin_key,
    evidence_ids,
    *,
    service,
    experience_id=None,
    investigation_id=None,
    metadata=None,
):
    event = FunctionHookEvent(
        _PHASE_EVENT[phase],
        service.target.persona_id,
        origin_key,
        tuple(sorted(set(evidence_ids))),
        experience_id,
        investigation_id,
        dict(metadata or {}),
    )
    return emit(event, service=service)
