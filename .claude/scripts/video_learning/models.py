"""Data contracts for the video-learning lane."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TranscriptSegment:
    start_s: float | None
    end_s: float | None
    text: str

    @property
    def timestamp(self) -> str:
        if self.start_s is None:
            return ""
        seconds = max(0, int(self.start_s))
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


@dataclass(slots=True)
class VideoMetadata:
    source: str
    source_type: str
    video_id: str = ""
    title: str = "Untitled video"
    channel: str = ""
    duration_s: float | None = None
    webpage_url: str = ""
    upload_date: str = ""


@dataclass(slots=True)
class ExtractionResult:
    metadata: VideoMetadata
    segments: list[TranscriptSegment]
    transcript_source: str
    artifact_dir: Path
    frame_paths: list[Path] = field(default_factory=list)
    visual_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def transcript(self) -> str:
        lines: list[str] = []
        for segment in self.segments:
            prefix = f"[{segment.timestamp}] " if segment.timestamp else ""
            lines.append(prefix + segment.text)
        return "\n".join(lines).strip()


@dataclass(slots=True)
class VideoLearningRequest:
    source: str
    question: str = ""
    detail: str = "smart"
    save_note: bool = True
    conversation_context: str = ""
    workspace: Path | None = None
    origin: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VideoLearningResult:
    success: bool
    job_id: str
    status: str
    summary: str = ""
    note_path: str = ""
    source: str = ""
    title: str = ""
    provider: str = ""
    model: str = ""
    runtime_lane: str = ""
    cost_usd: float | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
