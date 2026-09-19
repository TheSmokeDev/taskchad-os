"""Persona-owned recall policy; task-local experiment overrides, never global patches."""

from __future__ import annotations

import functools
import logging
import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from personas.learning.models import LearningError

KEYS = (
    "SEARCH_VECTOR_WEIGHT",
    "SEARCH_KEYWORD_WEIGHT",
    "RECALL_MIN_SCORE",
    "RECALL_KEYWORD_MIN_SCORE",
)
_scope: ContextVar = ContextVar("recall_policy_scope", default=None)
_LOG = logging.getLogger(__name__)


def validate_values(values: dict) -> dict[str, float]:
    if set(values) != set(KEYS):
        raise LearningError("recall policy permits only its four scoring parameters")
    if any(
        type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
        for v in values.values()
    ):
        raise LearningError("recall policy values must be finite and between zero and one")
    if not math.isclose(values[KEYS[0]] + values[KEYS[1]], 1.0, abs_tol=1e-9):
        raise LearningError("recall weights must sum to one")
    return {key: float(values[key]) for key in KEYS}


def configured_values() -> dict:
    import config

    # A single explicit weight owns its complement; validate after that merge,
    # otherwise a .9 vector pin plus the .3 default is rejected prematurely.
    return apply_pins({key: getattr(config, key) for key in KEYS}, operator_pins())


def operator_pins(target=None) -> dict:
    """Read only allowlisted fields; another persona never inherits ambient pins."""
    from personas import get_active_profile_name, get_persona_paths

    values = {}
    active = target is None or target.persona_id == get_active_profile_name()
    if target is not None and target.config_path is not None:
        from dotenv import dotenv_values

        env_file = get_persona_paths(target.persona_id)["env_file"]
        if env_file.exists():
            values.update(
                {key: value for key, value in dotenv_values(env_file).items() if key in KEYS}
            )
    if active:
        values.update({key: os.environ[key] for key in KEYS if key in os.environ})
    try:
        pins = {key: float(value) for key, value in values.items()}
    except (ValueError, TypeError) as exc:
        raise LearningError("operator recall pin must be numeric") from exc
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in pins.values()):
        raise LearningError("operator recall pin outside valid range")
    return pins


def apply_pins(values: dict, pins: dict) -> dict:
    result = dict(values) | pins
    vector, keyword = KEYS[:2]
    if vector in pins and keyword not in pins:
        result[keyword] = round(1 - pins[vector], 9)
    elif keyword in pins and vector not in pins:
        result[vector] = round(1 - pins[keyword], 9)
    return validate_values(result)


def active_policy(service) -> dict | None:
    policy_id = service.store.setting("recall_policy_id")
    if not policy_id:
        return None
    record = service._owned(policy_id, "tuning_policy")
    if record.get("status") != "active":
        raise LearningError("recall policy pointer is not active")
    validate_values(record["values"])
    return record


def effective_values(service) -> dict:
    from personas import get_active_profile_name

    policy = active_policy(service)
    if policy:
        base = policy["values"]
    elif service.target.persona_id != get_active_profile_name():
        # A parent dispatcher may inspect many profiles in one process. Its
        # dotenv-derived config is not another persona's initial policy.
        base = dict(zip(KEYS, (0.7, 0.3, 0.3, 0.02), strict=True))
    else:
        base = configured_values()
    return apply_pins(base, operator_pins(service.target))


def _target_for_memory(memory_dir):
    from personas import get_active_profile_name, get_persona_paths
    from personas.core import list_persona_profile_ids
    from personas.learning.models import resolve_learning_target

    requested = Path(memory_dir).resolve()
    names = dict.fromkeys([get_active_profile_name(), "default", *list_persona_profile_ids()])
    for name in names:
        if Path(get_persona_paths(name)["memory"]).resolve() == requested:
            return resolve_learning_target(name)
    return None  # arbitrary operator-selected vaults have no inherited policy


def resolve_values(memory_dir=None) -> dict:
    import config
    from evolve.config_override import diagnostic_override_active

    if diagnostic_override_active():
        return {key: getattr(config, key) for key in KEYS}

    requested = Path(memory_dir if memory_dir is not None else config.MEMORY_DIR).resolve()
    scoped = _scope.get()
    if scoped is not None and scoped["memory_dir"] == requested:
        return dict(scoped["values"])
    target = _target_for_memory(requested)
    if target is None:
        return configured_values()
    from personas.learning.service import LearningService

    return effective_values(LearningService(target))


@contextmanager
def policy_scope(memory_dir, values, *, replay=False):
    from evolve.config_override import diagnostic_override_active

    parent = _scope.get()
    value = {
        "memory_dir": Path(memory_dir).resolve(),
        "values": dict(values) if diagnostic_override_active() else validate_values(values),
        "replay": replay,
        "failures": parent["failures"] if parent and parent["replay"] and replay else [],
        "model_calls": parent["model_calls"] if parent and parent["replay"] and replay else [],
    }
    handle = _scope.set(value)
    try:
        yield
    finally:
        _scope.reset(handle)


def is_replaying() -> bool:
    value = _scope.get()
    return value is not None and value["replay"]


def record_failure(exc):
    value = _scope.get()
    if value is not None and value["replay"]:
        from runtime.errors import RuntimeLayerError

        value["failures"].append(
            {
                "type": type(exc).__name__,
                "unavailable": isinstance(
                    exc, (RuntimeLayerError, ConnectionError, TimeoutError, OSError)
                ),
            }
        )


def replay_failures():
    value = _scope.get()
    return list(value["failures"]) if value else []


def record_runtime(result):
    value = _scope.get()
    if value is not None and value["replay"]:
        value["model_calls"].append(
            {
                key: getattr(result, key, None)
                for key in ("lane", "provider", "model", "session_id", "cost_usd")
            }
        )


def replay_model_calls():
    value = _scope.get()
    return list(value["model_calls"]) if value else []


def with_recall_policy(fn):
    """Freeze one call's version across awaits and propagated worker threads."""

    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        memory_dir = kwargs.get("memory_dir", args[1] if len(args) > 1 else None)
        try:
            values = resolve_values(memory_dir)
        except (LearningError, OSError) as exc:
            _LOG.warning("Recall policy unavailable: %s", type(exc).__name__)
            values = configured_values()
        with policy_scope(memory_dir, values, replay=is_replaying()):
            return await fn(*args, **kwargs)

    return wrapped


def neighbors(values: dict, *, pins=None) -> list[dict]:
    """At most six neighbors; clamped duplicates and pinned changes disappear."""
    values = validate_values(values)
    pins = pins or {}
    result = []
    for key, delta in (
        (KEYS[0], 0.1),
        (KEYS[0], -0.1),
        (KEYS[2], 0.05),
        (KEYS[2], -0.05),
        (KEYS[3], 0.005),
        (KEYS[3], -0.005),
    ):
        candidate = dict(values)
        candidate[key] = round(min(1.0, max(0.0, values[key] + delta)), 9)
        if key == KEYS[0]:
            candidate[KEYS[1]] = round(1 - candidate[key], 9)
        candidate = apply_pins(candidate, pins)
        if candidate != values and candidate not in result:
            result.append(candidate)
    return result
