"""Safe video metadata, transcript, audio, and frame extraction."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import math
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from .models import ExtractionResult, TranscriptSegment, VideoMetadata

_TIMING_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s+-->\s+"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")
_VISUAL_CUES = (
    "slide", "chart", "graph", "diagram", "screen", "demo", "dashboard",
    "look at", "as you can see", "shown here", "whiteboard", "visual",
)


class VideoSourceError(ValueError):
    pass


def validate_source(source: str, *, allow_local: bool) -> tuple[str, str]:
    value = (source or "").strip()
    if not value:
        raise VideoSourceError("A video URL is required.")
    # urlparse treats a Windows drive letter as a URI scheme ("c:"). Resolve
    # that shape as a local path before applying the URL scheme policy.
    is_windows_path = bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"))
    if is_windows_path:
        if not allow_local:
            raise VideoSourceError("Remote chat channels accept public http(s) video URLs only.")
        path = Path(value).expanduser().resolve(strict=False)
        if not path.is_file():
            raise VideoSourceError(f"Local video not found: {path}")
        if path.stat().st_size > 1_073_741_824:
            raise VideoSourceError("Local videos larger than 1 GiB are not accepted.")
        return "local", str(path)
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        if parsed.username or parsed.password:
            raise VideoSourceError("URLs containing credentials are not allowed.")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise VideoSourceError("The video URL has no hostname.")
        if host in {"localhost", "localhost.localdomain"}:
            raise VideoSourceError("Local and private network URLs are not allowed.")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443)}
        except socket.gaierror as exc:
            raise VideoSourceError(f"Could not resolve the video host: {host}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast)):
                raise VideoSourceError("Local and private network URLs are not allowed.")
        return "url", value
    if parsed.scheme:
        raise VideoSourceError("Only http(s) video URLs are supported.")
    if not allow_local:
        raise VideoSourceError("Remote chat channels accept public http(s) video URLs only.")
    path = Path(value).expanduser().resolve(strict=False)
    if not path.is_file():
        raise VideoSourceError(f"Local video not found: {path}")
    if path.stat().st_size > 1_073_741_824:
        raise VideoSourceError("Local videos larger than 1 GiB are not accepted.")
    return "local", str(path)


def check_dependencies() -> list[str]:
    return [name for name in ("yt-dlp", "ffmpeg", "ffprobe") if shutil.which(name) is None]


async def extract_video(
    source: str,
    artifact_dir: Path,
    *,
    detail: str = "smart",
    allow_local: bool = False,
) -> ExtractionResult:
    source_type, normalized = await asyncio.to_thread(validate_source, source, allow_local=allow_local)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if source_type == "url":
        metadata, raw = await asyncio.to_thread(_remote_metadata, normalized)
        if raw.get("is_live"):
            raise VideoSourceError("Active livestreams are not supported; use the archived video after it ends.")
        segments, transcript_source = await asyncio.to_thread(
            _remote_captions, normalized, artifact_dir, raw
        )
    else:
        metadata = await asyncio.to_thread(_local_metadata, Path(normalized))
        raw = {}
        segments, transcript_source = [], ""

    if extraction_duration := metadata.duration_s:
        if extraction_duration > 14_400:
            raise VideoSourceError("Videos longer than four hours are outside the bounded v1 lane.")
    warnings: list[str] = []
    media_path = Path(normalized) if source_type == "local" else None
    if not segments:
        audio_path = await asyncio.to_thread(_extract_audio, normalized, source_type, artifact_dir)
        transcript = await _transcribe(audio_path)
        segments = [TranscriptSegment(None, None, transcript.strip())] if transcript.strip() else []
        transcript_source = "speech-to-text"
        warnings.append("No usable captions were available; speech-to-text fallback has no source timestamps.")
    if not segments:
        raise VideoSourceError("No transcript could be extracted from this video.")

    transcript_text = "\n".join(segment.text for segment in segments)
    needs_visuals, visual_reason = _needs_visuals(detail, metadata.title, transcript_text)
    frames: list[Path] = []
    if needs_visuals:
        if media_path is None:
            media_path = await asyncio.to_thread(_download_video, normalized, artifact_dir)
        frame_limit = 24 if detail == "deep" else 8
        frames = await asyncio.to_thread(
            _extract_frames,
            media_path,
            artifact_dir / "frames",
            metadata.duration_s,
            frame_limit,
        )
        if not frames:
            warnings.append("Visual analysis was requested, but no frames could be extracted.")

    if raw and not metadata.webpage_url:
        metadata.webpage_url = normalized
    return ExtractionResult(
        metadata=metadata,
        segments=segments,
        transcript_source=transcript_source,
        artifact_dir=artifact_dir,
        frame_paths=frames,
        visual_reason=visual_reason,
        warnings=warnings,
    )


def parse_vtt(text: str) -> list[TranscriptSegment]:
    """Parse WebVTT and collapse rolling auto-caption duplicates."""
    blocks = re.split(r"\r?\n\s*\r?\n", text.replace("\ufeff", ""))
    rows: list[TranscriptSegment] = []
    last_text = ""
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_idx = next((i for i, line in enumerate(lines) if _TIMING_RE.search(line)), None)
        if timing_idx is None:
            continue
        match = _TIMING_RE.search(lines[timing_idx])
        if not match:
            continue
        caption = " ".join(lines[timing_idx + 1 :])
        caption = html.unescape(_TAG_RE.sub("", caption))
        caption = re.sub(r"\s+", " ", caption).strip()
        if not caption or caption == last_text:
            continue
        # YouTube auto captions often repeat the prior line, then append words.
        if last_text and caption.startswith(last_text):
            caption = caption[len(last_text) :].strip()
        elif last_text and last_text.startswith(caption):
            continue
        if not caption:
            continue
        rows.append(
            TranscriptSegment(
                _parse_timestamp(match.group("start")),
                _parse_timestamp(match.group("end")),
                caption,
            )
        )
        last_text = " ".join([last_text, caption]).strip()[-500:]
    return rows


def _run(args: list[str], *, cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _remote_metadata(url: str) -> tuple[VideoMetadata, dict]:
    result = _run(["yt-dlp", "--dump-single-json", "--skip-download", "--no-playlist", "--", url])
    if result.returncode != 0:
        raise VideoSourceError(_tool_error("yt-dlp metadata", result.stderr))
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VideoSourceError("yt-dlp returned invalid video metadata.") from exc
    return VideoMetadata(
        source=url,
        source_type="url",
        video_id=str(raw.get("id") or ""),
        title=str(raw.get("title") or "Untitled video"),
        channel=str(raw.get("channel") or raw.get("uploader") or ""),
        duration_s=_as_float(raw.get("duration")),
        webpage_url=str(raw.get("webpage_url") or url),
        upload_date=str(raw.get("upload_date") or ""),
    ), raw


def _local_metadata(path: Path) -> VideoMetadata:
    result = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ])
    duration = None
    if result.returncode == 0:
        try:
            duration = _as_float(json.loads(result.stdout).get("format", {}).get("duration"))
        except json.JSONDecodeError:
            pass
    return VideoMetadata(
        source=str(path), source_type="local", video_id=path.stem,
        title=path.stem.replace("_", " ").replace("-", " ").strip() or path.name,
        duration_s=duration,
    )


def _remote_captions(
    url: str,
    artifact_dir: Path,
    metadata: dict | None = None,
) -> tuple[list[TranscriptSegment], str]:
    template = str(artifact_dir / "captions.%(ext)s")
    result = _run([
        "yt-dlp", "--skip-download", "--no-playlist", "--write-subs", "--write-auto-subs",
        "--sub-langs", "en.*,en", "--sub-format", "vtt", "-o", template, "--", url,
    ], timeout=300)
    candidates = sorted(artifact_dir.glob("captions*.vtt"), key=lambda p: ("orig" not in p.name, p.name))
    for candidate in candidates:
        segments = parse_vtt(candidate.read_text(encoding="utf-8", errors="replace"))
        if segments:
            subtitles = (metadata or {}).get("subtitles") or {}
            has_creator_english = any(str(language).lower().startswith("en") for language in subtitles)
            kind = "creator captions" if has_creator_english else "automatic captions"
            return segments, kind
    return [], ""


def _extract_audio(source: str, source_type: str, artifact_dir: Path) -> Path:
    output = artifact_dir / "audio.mp3"
    if source_type == "url":
        result = _run([
            "yt-dlp", "--no-playlist", "--max-filesize", "1G", "-x", "--audio-format", "mp3",
            "-o", str(output), "--", source,
        ])
        candidates = sorted(artifact_dir.glob("audio.*"))
        if result.returncode != 0 or not candidates:
            raise VideoSourceError(_tool_error("yt-dlp audio", result.stderr))
        return candidates[0]
    result = _run(["ffmpeg", "-y", "-i", source, "-vn", "-ac", "1", "-ar", "16000", str(output)])
    if result.returncode != 0 or not output.is_file():
        raise VideoSourceError(_tool_error("ffmpeg audio", result.stderr))
    return output


async def _transcribe(audio_path: Path) -> str:
    chat_dir = Path(__file__).resolve().parents[2] / "chat"
    if str(chat_dir) not in sys.path:
        sys.path.insert(0, str(chat_dir))
    from voice import transcribe_audio_file

    return await transcribe_audio_file(audio_path)


def _download_video(url: str, artifact_dir: Path) -> Path:
    output = artifact_dir / "video.%(ext)s"
    result = _run([
        "yt-dlp", "--no-playlist", "--max-filesize", "1G",
        "-f", "best[height<=720]/best", "-o", str(output), "--", url,
    ])
    candidates = [p for p in artifact_dir.glob("video.*") if p.is_file()]
    if result.returncode != 0 or not candidates:
        raise VideoSourceError(_tool_error("yt-dlp video", result.stderr))
    return candidates[0]


def _extract_frames(video: Path, frame_dir: Path, duration_s: float | None, limit: int) -> list[Path]:
    frame_dir.mkdir(parents=True, exist_ok=True)
    duration = duration_s or 600.0
    interval = max(5.0, duration / max(1, limit))
    result = _run([
        "ffmpeg", "-y", "-i", str(video), "-vf",
        f"fps=1/{interval:.3f},scale=768:-2", "-frames:v", str(limit),
        str(frame_dir / "frame-%03d.jpg"),
    ])
    if result.returncode != 0:
        return []
    return sorted(frame_dir.glob("frame-*.jpg"))[:limit]


def _needs_visuals(detail: str, title: str, transcript: str) -> tuple[bool, str]:
    if detail == "transcript":
        return False, "transcript-only mode"
    if detail == "deep":
        return True, "deep mode"
    haystack = f"{title}\n{transcript[:60000]}".lower()
    cues = [cue for cue in _VISUAL_CUES if cue in haystack]
    return (bool(cues), "visual cues: " + ", ".join(cues[:5]) if cues else "no strong visual cues")


def _parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _tool_error(label: str, stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    detail = lines[-1] if lines else "unknown error"
    return f"{label} failed: {detail[:500]}"
