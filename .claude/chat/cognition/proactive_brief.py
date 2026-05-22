"""Unified proactive brief builder for living-loop entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cognition.scheduled_payload import (
    build_scheduled_cognition_payload,
    render_identity_context,
)


@dataclass(frozen=True)
class ProactiveBrief:
    """Rendered cognition brief plus source metadata for proof surfaces."""

    section: str
    source_paths: dict[str, str]
    include_identity: bool = False


def build_proactive_brief(
    memory_dir: Path,
    *,
    daily_dir: Path | None = None,
    inference_state_file: Path | None = None,
    include_identity: bool = False,
    header: str = "## Proactive Brief",
    max_daily_chars: int = 1200,
    max_heartbeat_chars: int = 1500,
) -> ProactiveBrief:
    """Build the shared proactive cognition brief.

    This is intentionally read-only. It gives chat bootstrap, heartbeat, and
    scheduled cognition one canonical proactive context path without granting
    any automatic memory mutation behavior.
    """

    memory_dir = Path(memory_dir)
    daily_root = Path(daily_dir) if daily_dir is not None else memory_dir / "daily"
    payload = build_scheduled_cognition_payload(
        memory_dir,
        inference_state_file=inference_state_file,
    )

    sections: list[str] = []
    if include_identity:
        identity = render_identity_context(payload)
        if identity:
            sections.append(identity)
    if payload.active_inference_section:
        sections.append(payload.active_inference_section)
    if payload.working_memory_section:
        sections.append(payload.working_memory_section)

    daily_signal = _read_recent_daily_signal(daily_root, max_daily_chars)
    if daily_signal:
        sections.append("## Recent Daily Signal\n\n" + daily_signal)

    heartbeat_policy = _read_limited(memory_dir / "HEARTBEAT.md", max_heartbeat_chars)
    if heartbeat_policy:
        sections.append("## Heartbeat Checklist\n\n" + heartbeat_policy)

    body = "\n\n".join(sections)
    section = f"{header}\n\n{body}" if body else ""
    return ProactiveBrief(
        section=section,
        source_paths={
            "memory_dir": str(memory_dir),
            "daily_dir": str(daily_root),
            "inference_state_file": str(inference_state_file or ""),
            "heartbeat_file": str(memory_dir / "HEARTBEAT.md"),
            "working_file": str(memory_dir / "WORKING.md"),
        },
        include_identity=include_identity,
    )


def build_proactive_brief_section(
    memory_dir: Path,
    *,
    daily_dir: Path | None = None,
    inference_state_file: Path | None = None,
    include_identity: bool = False,
    header: str = "## Proactive Brief",
) -> str:
    """Return only the rendered proactive brief section."""

    return build_proactive_brief(
        memory_dir,
        daily_dir=daily_dir,
        inference_state_file=inference_state_file,
        include_identity=include_identity,
        header=header,
    ).section


def _read_recent_daily_signal(daily_dir: Path, max_chars: int) -> str:
    try:
        files = sorted(Path(daily_dir).glob("*.md"), reverse=True)
    except OSError:
        return ""
    for path in files[:2]:
        text = _read_limited(path, max_chars)
        if text:
            return f"### {path.stem}\n\n{text}"
    return ""


def _read_limited(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_newline = cut.rfind("\n")
    if last_newline > max_chars // 2:
        cut = cut[:last_newline]
    return cut + "\n[TRUNCATED]"


__all__ = (
    "ProactiveBrief",
    "build_proactive_brief",
    "build_proactive_brief_section",
)
