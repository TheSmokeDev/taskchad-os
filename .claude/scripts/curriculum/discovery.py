"""YouTube RSS deltas and flat-playlist inventory without media downloads."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import feedparser

from .config import CurriculumSource


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    source_id: str
    channel_id: str
    videos: tuple[dict[str, Any], ...]
    method: str
    watermark: str = ""


def discover_source(
    source: CurriculumSource,
    *,
    full_inventory: bool,
    timeout_s: int = 180,
) -> DiscoveryResult:
    if source.kind != "youtube_channel":
        return DiscoveryResult(source.id, "", (), "seed-only")
    if not full_inventory:
        channel_id = resolve_channel_id(source.url, timeout_s=timeout_s)
        delta = _discover_rss(source, channel_id)
        if delta.videos:
            return delta
    return _discover_playlist(source, timeout_s=timeout_s)


def describe_video(
    url: str,
    *,
    source_id: str,
    expected_video_id: str = "",
    timeout_s: int = 120,
) -> dict[str, Any]:
    """Resolve ONE video's catalog metadata without downloading media.

    Returns the same row shape `discover_source` produces so an operator drop
    enters the ledger through the existing discovery contract instead of a
    second, forked extractor.
    """
    result = _run_ytdlp(
        ["--no-playlist", "--skip-download", "--dump-single-json", url],
        timeout_s=timeout_s,
    )
    raw = json.loads(result.stdout)
    if not isinstance(raw, dict):
        raise RuntimeError("yt-dlp returned no video metadata.")
    entry_type = str(raw.get("_type") or "video")
    if entry_type not in {"video", ""}:
        raise RuntimeError(f"The link resolves to a {entry_type}, not a single video.")
    video_id = str(raw.get("id") or "")
    if not video_id:
        raise RuntimeError("yt-dlp returned no video id.")
    if expected_video_id and video_id != expected_video_id:
        raise RuntimeError(
            f"Resolved video id {video_id!r} does not match the dropped "
            f"link {expected_video_id!r}."
        )
    duration = raw.get("duration")
    return {
        "video_id": video_id,
        "source_id": source_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": str(raw.get("title") or "Untitled video"),
        "channel": str(raw.get("channel") or raw.get("uploader") or ""),
        "upload_date": str(raw.get("upload_date") or ""),
        "duration_s": float(duration) if isinstance(duration, (int, float)) else None,
    }


def resolve_channel_id(url: str, *, timeout_s: int = 60) -> str:
    result = _run_ytdlp(
        ["--flat-playlist", "--playlist-end", "1", "--dump-single-json", url],
        timeout_s=timeout_s,
    )
    raw = json.loads(result.stdout)
    return str(raw.get("channel_id") or raw.get("id") or "")


def _discover_rss(
    source: CurriculumSource,
    channel_id: str,
) -> DiscoveryResult:
    if not channel_id:
        return DiscoveryResult(source.id, "", (), "rss-unavailable")
    feed = feedparser.parse(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    )
    videos: list[dict[str, Any]] = []
    for entry in feed.entries:
        video_id = str(
            entry.get("yt_videoid")
            or entry.get("youtube_videoid")
            or str(entry.get("link") or "").split("v=")[-1]
        )
        if not video_id:
            continue
        videos.append(
            {
                "video_id": video_id,
                "source_id": source.id,
                "url": str(entry.get("link") or f"https://www.youtube.com/watch?v={video_id}"),
                "title": str(entry.get("title") or "Untitled video"),
                "channel": str(entry.get("author") or ""),
                "upload_date": str(entry.get("published") or ""),
                "duration_s": None,
            }
        )
    watermark = videos[0]["video_id"] if videos else ""
    return DiscoveryResult(source.id, channel_id, tuple(videos), "rss", watermark)


def _discover_playlist(
    source: CurriculumSource,
    *,
    timeout_s: int,
) -> DiscoveryResult:
    result = _run_ytdlp(
        ["--flat-playlist", "--dump-single-json", _videos_url(source.url)],
        timeout_s=timeout_s,
    )
    raw = json.loads(result.stdout)
    channel_id = str(raw.get("channel_id") or raw.get("id") or "")
    channel = str(raw.get("channel") or raw.get("uploader") or "")
    videos: list[dict[str, Any]] = []
    for entry in raw.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id") or "")
        if not video_id:
            continue
        duration = entry.get("duration")
        videos.append(
            {
                "video_id": video_id,
                "source_id": source.id,
                "url": str(
                    entry.get("url")
                    if str(entry.get("url") or "").startswith("http")
                    else f"https://www.youtube.com/watch?v={video_id}"
                ),
                "title": str(entry.get("title") or "Untitled video"),
                "channel": channel,
                "upload_date": str(entry.get("upload_date") or ""),
                "duration_s": float(duration) if isinstance(duration, (int, float)) else None,
            }
        )
    watermark = videos[0]["video_id"] if videos else ""
    return DiscoveryResult(
        source.id, channel_id, tuple(videos), "flat-playlist", watermark
    )


def _run_ytdlp(args: list[str], *, timeout_s: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["yt-dlp", *args],
        cwd=str(Path.cwd()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "yt-dlp failed").strip()
        raise RuntimeError(detail[-2000:])
    return result


def _videos_url(url: str) -> str:
    """Normalize a channel home URL to its video catalog tab."""
    value = url.rstrip("/")
    if "youtube.com/@" in value and not value.endswith(
        ("/videos", "/shorts", "/streams")
    ):
        return value + "/videos"
    return value
