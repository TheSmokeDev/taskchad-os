"""Provider/model health tracking for runtime routing.

Health bookkeeping is fail-open by contract: these functions must never
raise into a runtime lane. A lost health write is harmless (worst case a
cooldown clears one run later); a health exception escaping into
lane_router converts a successful run into a fake provider failure
(shipped 2026-07-16: a WinError 32 tmp->json os.replace collision between
two concurrent scheduled jobs put openai-codex on cooldown and discarded
a good result). Cross-process access to RUNTIME_HEALTH_FILE serializes on
shared.file_lock — os.replace() cannot swap a file another process holds
open on Windows, and unlocked load-modify-save loses updates.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from config import STATE_DIR, now_local
from shared import file_lock, load_state, save_state

from .profiles import RuntimeProfile

RUNTIME_HEALTH_FILE = STATE_DIR / "runtime-health.json"

_LOCK_TIMEOUT_SECONDS = 5.0

_logger = logging.getLogger(__name__)


def is_profile_available(profile: RuntimeProfile) -> bool:
    """Return True when neither the provider nor provider:model is cooling down.

    Fail-open: an unreadable health file assumes available — returning False
    would silently disable providers.
    """

    try:
        with file_lock(RUNTIME_HEALTH_FILE, timeout=_LOCK_TIMEOUT_SECONDS):
            state = load_state(RUNTIME_HEALTH_FILE)
        entities = state.get("entities", {})
        now = now_local()
        for key in (_provider_key(profile), _model_key(profile)):
            cooldown_until = _parse_timestamp(entities.get(key, {}).get("cooldown_until"))
            if cooldown_until and cooldown_until > now:
                return False
        return True
    except Exception as exc:
        _logger.warning(
            "runtime health read failed for %s (assuming available): %s",
            profile.key,
            exc,
        )
        return True


def mark_profile_success(profile: RuntimeProfile) -> None:
    """Record a successful run and clear any cooldown for this provider/model."""

    try:
        with file_lock(RUNTIME_HEALTH_FILE, timeout=_LOCK_TIMEOUT_SECONDS):
            state = load_state(RUNTIME_HEALTH_FILE)
            entities = state.setdefault("entities", {})
            now = now_local().isoformat()

            for key in (_provider_key(profile), _model_key(profile)):
                entry = entities.setdefault(key, {})
                entry["last_success_at"] = now
                entry.pop("cooldown_until", None)
                entry.pop("last_retryable_error", None)

            save_state(state, RUNTIME_HEALTH_FILE)
    except Exception as exc:
        _logger.warning(
            "runtime health write failed for %s (success not recorded): %s",
            profile.key,
            exc,
        )


def mark_profile_retryable_failure(profile: RuntimeProfile, error: str) -> None:
    """Record a retryable failure and apply cooldowns to provider and model."""

    _mark_profile_failure(profile, error)


def mark_profile_unavailable(profile: RuntimeProfile, error: str) -> None:
    """Record a provider/model availability failure and apply cooldowns."""

    _mark_profile_failure(profile, error)


def _mark_profile_failure(profile: RuntimeProfile, error: str) -> None:
    """Apply provider/model cooldown state for a failed runtime lane.

    Must never raise: callers sit inside lane_router except handlers, so an
    escaping exception here would crash the whole router mid-fallback.
    """

    try:
        with file_lock(RUNTIME_HEALTH_FILE, timeout=_LOCK_TIMEOUT_SECONDS):
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
    except Exception as exc:
        _logger.warning(
            "runtime health write failed for %s (failure not recorded): %s",
            profile.key,
            exc,
        )


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
