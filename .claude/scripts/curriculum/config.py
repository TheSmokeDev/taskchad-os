"""Strict, call-time curriculum settings for one persona profile."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CurriculumSource:
    id: str
    url: str
    kind: str = "youtube_channel"
    policy: str = "curated"
    seed_url: str = ""


@dataclass(frozen=True, slots=True)
class CurriculumSettings:
    persona_id: str
    enabled: bool = False
    domain: str = "general"
    sources: tuple[CurriculumSource, ...] = field(default_factory=tuple)
    schedule_hours: int = 6
    backfill_limit: int = 120
    metadata_batch_size: int = 50
    daily_skims: int = 10
    daily_deep_studies: int = 3
    steady_daily_deep_studies: int = 1
    admission_model_tier: str = "fast"
    study_model_tier: str = "quality"


def get_curriculum_settings(persona_id: str) -> CurriculumSettings:
    """Read and parse settings at call time from the canonical profile config."""
    import personas
    from personas.lifecycle import validate_persona_name

    validate_persona_name(persona_id)
    raw = personas.load_persona_config(persona_id)
    section = raw.get("curriculum") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        section = {}
    sources: list[CurriculumSource] = []
    for value in section.get("sources", []):
        if not isinstance(value, dict):
            continue
        sources.append(
            CurriculumSource(
                id=str(value.get("id") or "").strip(),
                url=str(value.get("url") or "").strip(),
                kind=str(value.get("kind") or "youtube_channel").strip(),
                policy=str(value.get("policy") or "curated").strip(),
                seed_url=str(value.get("seed_url") or "").strip(),
            )
        )
    return CurriculumSettings(
        persona_id=persona_id,
        enabled=bool(section.get("enabled", False)),
        domain=str(section.get("domain") or "general").strip(),
        sources=tuple(source for source in sources if source.id and source.url),
        schedule_hours=int(section.get("schedule_hours", 6)),
        backfill_limit=int(section.get("backfill_limit", 120)),
        metadata_batch_size=int(section.get("metadata_batch_size", 50)),
        daily_skims=int(section.get("daily_skims", 10)),
        daily_deep_studies=int(section.get("daily_deep_studies", 3)),
        steady_daily_deep_studies=int(section.get("steady_daily_deep_studies", 1)),
        admission_model_tier=str(section.get("admission_model_tier") or "fast").strip(),
        study_model_tier=str(section.get("study_model_tier") or "quality").strip(),
    )


def settings_to_section(settings: CurriculumSettings) -> dict[str, Any]:
    return {
        "enabled": settings.enabled,
        "domain": settings.domain,
        "sources": [
            {
                "id": source.id,
                "url": source.url,
                "kind": source.kind,
                "policy": source.policy,
                **({"seed_url": source.seed_url} if source.seed_url else {}),
            }
            for source in settings.sources
        ],
        "schedule_hours": settings.schedule_hours,
        "backfill_limit": settings.backfill_limit,
        "metadata_batch_size": settings.metadata_batch_size,
        "daily_skims": settings.daily_skims,
        "daily_deep_studies": settings.daily_deep_studies,
        "steady_daily_deep_studies": settings.steady_daily_deep_studies,
        "admission_model_tier": settings.admission_model_tier,
        "study_model_tier": settings.study_model_tier,
    }
