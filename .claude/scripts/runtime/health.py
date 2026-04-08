"""Provider/model health tracking for runtime routing."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from config import STATE_DIR, now_local
from shared import load_state, save_state

from .profiles import RuntimeProfile

RUNTIME_HEALTH_FILE = STATE_DIR / "runtime-health.json"


def is_profile_available(profile: RuntimeProfile) -> bool:
    """Return True when neither the provider nor provider:model is cooling down."""

    state = load_state(RUNTIME_HEALTH_FILE)
    entities = state.get("entities", {})
    now = now_local()
    for key in (_provider_key(profile), _model_key(profile)):
        cooldown_until = _parse_timestamp(entities.get(key, {}).get("cooldown_until"))
        if cooldown_until and cooldown_until > now:
            return False
    return True


def mark_profile_success(profile: RuntimeProfile) -> None:
    """Record a successful run and clear any cooldown for this provider/model."""

    state = load_state(RUNTIME_HEALTH_FILE)
    entities = state.setdefault("entities", {})
    now = now_local().isoformat()

    for key in (_provider_key(profile), _model_key(profile)):
        entry = entities.setdefault(key, {})
        entry["last_success_at"] = now
        entry.pop("cooldown_until", None)
        entry.pop("last_retryable_error", None)

    save_state(state, RUNTIME_HEALTH_FILE)


def mark_profile_retryable_failure(profile: RuntimeProfile, error: str) -> None:
    """Record a retryable failure and apply cooldowns to provider and model."""

    _mark_profile_failure(profile, error)


def mark_profile_unavailable(profile: RuntimeProfile, error: str) -> None:
    """Record a provider/model availability failure and apply cooldowns."""

    _mark_profile_failure(profile, error)


def _mark_profile_failure(profile: RuntimeProfile, error: str) -> None:
    """Apply provider/model cooldown state for a failed runtime lane."""

    state = load_state(RUNTIME_HEALTH_FILE)
    entities = state.setdefault("entities", {})
    now = now_local()

    provider_entry = entities.setdefault(_provider_key(profile), {})
    provider_entry["last_retryable_error"] = error
    provider_entry["last_failure_at"] = now.isoformat()
    provider_entry["cooldown_until"] = (
        now + timedelta(seconds=_provider_cooldown_seconds())
    ).isoformat()

    model_entry = entities.setdefault(_model_key(profile), {})
    model_entry["last_retryable_error"] = error
    model_entry["last_failure_at"] = now.isoformat()
    model_entry["cooldown_until"] = (
        now + timedelta(seconds=_model_cooldown_seconds())
    ).isoformat()

    save_state(state, RUNTIME_HEALTH_FILE)


def _provider_key(profile: RuntimeProfile) -> str:
    return f"provider:{profile.provider}"


def _model_key(profile: RuntimeProfile) -> str:
    return f"model:{profile.provider}:{profile.model}"


def _provider_cooldown_seconds() -> int:
    return int(os.getenv("SECOND_BRAIN_PROVIDER_COOLDOWN_SECONDS", "300"))


def _model_cooldown_seconds() -> int:
    return int(os.getenv("SECOND_BRAIN_MODEL_COOLDOWN_SECONDS", "900"))


def _parse_timestamp(value: object):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
