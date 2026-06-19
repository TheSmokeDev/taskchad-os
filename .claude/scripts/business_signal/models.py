"""Core data types for the signal engine pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SignalItem:
    """A single fetched signal item flowing through the pipeline."""

    source: str
    title: str
    url: str
    summary: str
    relevance_score: float = 0.0
    tags: list[str] = field(default_factory=list)
    fetched_at: str = ""
    content_angle: str | None = None


@dataclass(slots=True)
class SignalDigest:
    """Aggregated output of a single signal engine run."""

    date: str
    items: list[SignalItem] = field(default_factory=list)
    drafts_created: list[str] = field(default_factory=list)
    sources_checked: int = 0
    sources_failed: int = 0
    total_fetched: int = 0
    total_triaged: int = 0
    markdown_body: str = ""
