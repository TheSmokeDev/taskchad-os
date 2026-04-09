"""Core runtime request / result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .capabilities import TEXT_REASONING


@dataclass(slots=True)
class RuntimeRequest:
    """Normalized runtime request for background jobs and chat flows."""

    prompt: str
    cwd: Path | str
    task_name: str
    capability: str = TEXT_REASONING
    model: str | None = None
    fallback_model: str | None = None
    max_turns: int = 1
    max_budget_usd: float | None = None
    allowed_tools: list[str] = field(default_factory=list)
    permission_mode: str | None = None
    setting_sources: list[str] = field(default_factory=list)
    system_prompt: dict[str, Any] | str | None = None
    hooks: dict[str, Any] | None = None
    thinking: dict[str, Any] | None = None
    env: dict[str, str] | None = None
    resume: str | None = None
    stderr: Any | None = None
    allow_fallback: bool = True


@dataclass(slots=True)
class RuntimeToolCall:
    """Normalized tool-call record across providers."""

    id: str = ""
    name: str = ""
    arguments: dict[str, Any] | str | None = None
    provider_type: str | None = None
    status: str | None = None


@dataclass(slots=True)
class RuntimeResult:
    """Normalized runtime result."""

    text: str
    provider: str
    model: str
    profile_key: str | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    subtype: str | None = None
    tool_call_count: int = 0
    tool_names_used: list[str] = field(default_factory=list)
    tool_calls: list[RuntimeToolCall] = field(default_factory=list)
