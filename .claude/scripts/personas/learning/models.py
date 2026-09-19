"""Small immutable boundary types for the learning service."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class LearningError(ValueError):
    """Invalid, conflicting, unavailable, or unowned learning state."""


class LearningNotFoundError(LearningError):
    """A requested physical profile or record is absent."""


LearningNotFound = LearningNotFoundError


class LearningValidationError(LearningError):
    """An input failed validation before a lifecycle transition."""


def is_credential_key(key: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized.casefold()).strip("_")
    if normalized in {
        "token_count",
        "token_usage",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "tokens_used",
        "auth_profile",
        "auth_mode",
        "credential_source",
    }:
        return False
    if normalized == "key":
        return True
    return bool(
        re.search(
            (
                "(?:^|_)(?:auth|authorization|bearer|secret|password|passwd|passph"
                "rase|credential|credentials|cookie|cookies|token|jwt|private_key|"
                "signing_key|key_material|api_key|apikey)(?:_|$)"
            ),
            normalized,
        )
    )


def learning_model_budget() -> float | None:
    """Inherit explicit operator budgets; do not invent an API meter for OAuth."""
    raw = os.getenv("PERSONA_LEARNING_MODEL_BUDGET_USD")
    if raw is None:
        raw = os.getenv("CHAT_MAX_BUDGET_USD")
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise LearningValidationError("learning model budget must be positive and finite") from exc
    if not math.isfinite(value) or value <= 0:
        raise LearningValidationError("learning model budget must be positive and finite")
    return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (ValueError, TypeError) as exc:
        raise LearningError("learning payload must be finite JSON data") from exc


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LearningTarget:
    persona_id: str
    memory_dir: Path
    data_dir: Path
    state_dir: Path
    skills_dir: Path
    config_path: Path | None = None

    def __post_init__(self) -> None:
        from personas.core import validate_persona_name

        # Canonical default/custom are resolver identities, not new persona names.
        if self.persona_id not in {"default", "custom"}:
            validate_persona_name(self.persona_id)
        if not self.persona_id:
            raise LearningError("persona_id is required")
        for field in ("memory_dir", "data_dir", "state_dir", "skills_dir"):
            object.__setattr__(self, field, Path(getattr(self, field)))
        if self.config_path is not None:
            object.__setattr__(self, "config_path", Path(self.config_path))


@dataclass(frozen=True)
class LearningContext:
    text: str = ""
    versions: tuple[dict[str, Any], ...] = ()
    context_hash: str = ""


COGNITIVE_PHASES = frozenset({"reorient", "interpret", "reflect", "revisit"})
UNDERSTANDING_TYPES = frozenset(
    {"concept", "interpretation", "belief", "self_assessment", "source_assessment"}
)


def validate_investigation_trigger(value: Any) -> dict[str, Any]:
    """Validate the bounded observation request without granting execution authority."""
    if not isinstance(value, dict):
        raise LearningValidationError("investigation trigger must be an object")
    trigger = dict(value)
    kind = trigger.get("type")
    fields = {
        "deadline": {"type", "at"},
        "closed_candles": {"type", "asset", "venue", "timeframe", "after", "count"},
        "crossing": {
            "type",
            "asset",
            "venue",
            "timeframe",
            "metric",
            "threshold",
            "direction",
            "baseline",
        },
        "source_update": {"type", "source_id", "revision", "thread_id"},
    }
    if kind not in fields or set(trigger) - fields[kind]:
        raise LearningValidationError("unsupported investigation trigger")
    required = fields[kind] - {"thread_id"}
    if required - set(trigger):
        raise LearningValidationError("investigation trigger is missing fields")
    for key, item in trigger.items():
        if key not in {"count", "threshold", "baseline"}:
            if not isinstance(item, str) or not item.strip() or len(item) > 512:
                raise LearningValidationError("invalid investigation trigger text")
    for key in {"at", "after"} & set(trigger):
        try:
            stamp = datetime.fromisoformat(trigger[key].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
        except ValueError as exc:
            raise LearningValidationError("trigger timestamps require a timezone") from exc
        trigger[key] = stamp.astimezone(UTC).isoformat()
    if kind == "closed_candles" and (
        type(trigger["count"]) is not int or not 1 <= trigger["count"] <= 1000
    ):
        raise LearningValidationError("closed candle count must be between 1 and 1000")
    if kind == "crossing":
        if trigger["direction"] not in {"above", "below"}:
            raise LearningValidationError("crossing direction must be above or below")
        for key in ("threshold", "baseline"):
            if type(trigger[key]) not in {float, int} or not math.isfinite(trigger[key]):
                raise LearningValidationError("crossing values must be finite numbers")
    return trigger


def resolve_learning_target(persona_id: str) -> LearningTarget:
    """Resolve the requested physical profile, never the process's ambient data."""
    from personas import get_persona_paths
    from personas.services import get_profile_config_path

    if persona_id == "main":
        raise LearningError("use canonical profile id 'default'")
    paths = get_persona_paths(persona_id)
    config_path = get_profile_config_path(persona_id)
    if not Path(paths["memory"]).is_dir() and not Path(config_path).is_file():
        raise LearningNotFound("persona profile does not exist")
    return LearningTarget(
        persona_id=persona_id,
        memory_dir=Path(paths["memory"]),
        data_dir=Path(paths["data"]),
        state_dir=Path(paths["state"]),
        skills_dir=Path(paths["skills"]),
        config_path=Path(config_path),
    )
