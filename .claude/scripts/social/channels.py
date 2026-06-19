"""Config-driven social channel registry.

Loads channels.yaml at call-time (Rule 1 — no module-scope cache).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SocialChannel:
    channel_id: str = ""
    display_name: str = ""
    execution_method: str = "manual"
    cadence_enabled: bool = False
    cadence_interval_hours: int = 24
    voice_profile: str = ""
    topic_pool: list[str] = field(default_factory=list)
    browser_workflow_id: str | None = None


_DEFAULT_YAML_PATH: Path | None = None


def _resolve_yaml_path(yaml_path: Path | None = None) -> Path:
    if yaml_path is not None:
        return yaml_path
    return Path(__file__).parent / "channels.yaml"


def _load_channels(yaml_path: Path | None = None) -> dict[str, SocialChannel]:
    path = _resolve_yaml_path(yaml_path)
    if not path.is_file():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    raw: dict[str, Any] = data.get("channels", {})
    result: dict[str, SocialChannel] = {}
    for cid, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        result[cid] = SocialChannel(
            channel_id=cid,
            display_name=cfg.get("display_name", cid),
            execution_method=cfg.get("execution_method", "manual"),
            cadence_enabled=bool(cfg.get("cadence_enabled", False)),
            cadence_interval_hours=int(cfg.get("cadence_interval_hours", 24)),
            voice_profile=cfg.get("voice_profile", ""),
            topic_pool=cfg.get("topic_pool", []) or [],
            browser_workflow_id=cfg.get("browser_workflow_id"),
        )
    return result


def get_channel(
    channel_id: str, *, yaml_path: Path | None = None
) -> SocialChannel | None:
    channels = _load_channels(yaml_path)
    return channels.get(channel_id)


def list_channels(*, yaml_path: Path | None = None) -> list[SocialChannel]:
    return list(_load_channels(yaml_path).values())


def list_active_channels(*, yaml_path: Path | None = None) -> list[SocialChannel]:
    return [c for c in _load_channels(yaml_path).values() if c.cadence_enabled]
