"""OKF v0.2 source dossiers and coarse canonical topic synthesis."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from shared import atomic_write_text

from .paths import CurriculumPaths

_REQUIRED_FRONTMATTER = {
    "type",
    "title",
    "description",
    "status",
    "sources",
    "generated",
    "verified",
}
_TIMESTAMP = r"\d{2}:\d{2}:\d{2}"
_ENGINE_ACTOR = "thehomie/curriculum-engine"
_STRUCTURE_MIGRATED = "structure-migrated"


class CurriculumBundle:
    def __init__(self, paths: CurriculumPaths, domain: str) -> None:
        self.paths = paths
        self.domain = domain

    def ensure(self) -> None:
        for path in (
            self.paths.bundle_root,
            self.paths.bundle_root / "concepts",
            self.paths.bundle_root / "entities",
            self.paths.bundle_root / "sources",
            self.paths.raw_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not (self.paths.bundle_root / "index.md").exists():
            atomic_write_text(
                self.paths.bundle_root / "index.md",
                _document(
                    {
                        "type": "overview",
                        "title": f"{self.domain.replace('-', ' ').title()} Curriculum",
                        "description": "Persona-private, source-grounded curriculum index.",
                        "status": "active",
                        "sources": [],
                        "generated": {
                            "actor": "thehomie/curriculum-engine",
                            "at": _now(),
                        },
                        "verified": {"level": "machine-confirmed", "at": _now()},
                    },
                    "# Curriculum Index\n\n"
                    "Navigate concepts first, then open only the source dossiers needed.\n\n"
                    "## Concepts\n\n_No concepts studied yet._\n\n"
                    "## Sources\n\n_No sources studied yet._\n",
                ),
            )
        if not (self.paths.bundle_root / "log.md").exists():
            atomic_write_text(
                self.paths.bundle_root / "log.md",
                _document(
                    {
                        "type": "log",
                        "title": "Curriculum Change Log",
                        "description": "Chronological, append-only curriculum updates.",
                        "status": "active",
                        "sources": [],
                        "generated": {
                            "actor": "thehomie/curriculum-engine",
                            "at": _now(),
                        },
                        "verified": {"level": "machine-confirmed", "at": _now()},
                    },
                    "# Curriculum Change Log\n",
                ),
            )

    def write_raw(
        self,
        *,
        source_id: str,
        video_id: str,
        title: str,
        url: str,
        transcript_source: str,
        transcript: str,
    ) -> Path:
        self.ensure()
        normalized_transcript = transcript.strip()
        target = self.paths.confine_data(
            self.paths.raw_root / _slug(source_id) / _video_filename(video_id)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = _sha256(normalized_transcript)
        body = _document(
            {
                "type": "curriculum-raw-transcript",
                "title": title,
                "source_id": source_id,
                "video_id": video_id,
                "source_url": url,
                "transcript_source": transcript_source,
                "sha256": digest,
                "immutable": True,
                "captured_at": _now(),
            },
            f"# {title}\n\n{normalized_transcript}\n",
        )
        if target.exists():
            metadata, current_body = _read_document(target)
            current_transcript = _raw_transcript(current_body)
            current_digest = _sha256(current_transcript)
            expected_identity = {
                "source_id": source_id,
                "video_id": video_id,
                "source_url": url,
                "transcript_source": transcript_source,
            }
            for key, expected in expected_identity.items():
                if str(metadata.get(key) or "") != str(expected):
                    raise ValueError(f"Immutable transcript {key} mismatch at {target}")
            if metadata.get("immutable") is not True:
                raise ValueError(f"Raw transcript is not marked immutable: {target}")
            if str(metadata.get("sha256") or "") != current_digest:
                raise ValueError(
                    f"Immutable transcript digest does not match stored body: {target}"
                )
            if digest != current_digest:
                raise ValueError(
                    f"Immutable transcript already exists with different content: {target}"
                )
            return target
        atomic_write_text(target, body)
        return target

    def write_source_dossier(
        self,
        *,
        video: dict[str, Any],
        transcript_source: str,
        analysis_markdown: str,
        provider: str,
        model: str,
        runtime_lane: str,
        raw_path: Path | str | None = None,
        raw_digest: str | None = None,
    ) -> Path:
        video_id = str(video["video_id"])
        source_id = str(video["source_id"])
        url = str(video["url"])
        resolved_raw = raw_path or (
            self.paths.raw_root / _slug(source_id) / _video_filename(video_id)
        )
        raw_evidence = self._preflight_raw(
            video=video,
            transcript_source=transcript_source,
            raw_path=resolved_raw,
            raw_digest=raw_digest,
        )
        citation_errors = _validate_evidence_citations(
            analysis_markdown,
            video_id=video_id,
            raw_timestamps=raw_evidence["timestamps"],
        )
        if citation_errors:
            raise ValueError("Evidence ledger validation failed: " + "; ".join(citation_errors))

        target = self.paths.confine_memory(
            self.paths.bundle_root / "sources" / _video_filename(video_id)
        )
        today = datetime.now(UTC)
        metadata = {
            "type": "source",
            "title": str(video["title"]),
            "description": f"Studied source from {video.get('channel') or source_id}.",
            "tags": ["curriculum", source_id, str(video.get("topic") or "other")],
            "status": "active",
            "stale_after": (today + timedelta(days=365)).date().isoformat(),
            "sources": [
                {
                    "id": f"youtube:{video_id}",
                    "url": url,
                    "title": str(video["title"]),
                    "author": str(video.get("channel") or ""),
                    "published": str(video.get("upload_date") or ""),
                }
            ],
            "generated": {
                "actor": "thehomie/curriculum-engine",
                "at": _now(),
                "provider": provider,
                "model": model,
                "runtime_lane": runtime_lane,
            },
            "verified": {
                "level": "machine-confirmed",
                "at": _now(),
                "method": "source-id-and-dossier-structure",
            },
            "video_id": video_id,
            "source_id": source_id,
            "transcript_source": transcript_source,
            "raw_evidence": {
                "path": raw_evidence["relative_path"],
                "sha256": raw_evidence["sha256"],
                "immutable": True,
            },
        }
        body = (
            f"# {video['title']}\n\n"
            f"{analysis_markdown.strip()}\n\n"
            "## Sources\n\n"
            f"- [youtube:{video_id}]({url}) — {video.get('channel') or source_id}; "
            "timestamps in the evidence ledger refer to this source.\n"
        )
        topic = self.paths.confine_memory(
            self.paths.bundle_root / "concepts" / f"{_slug(str(video.get('topic') or 'other'))}.md"
        )
        mutable_paths = (
            target,
            topic,
            self.paths.bundle_root / "index.md",
            self.paths.bundle_root / "log.md",
        )
        snapshots = _snapshot_files(mutable_paths)
        try:
            self.ensure()
            atomic_write_text(target, _document(metadata, body))
            self._update_topic(video, target, analysis_markdown)
            self._regenerate_index()
            self._append_log(
                f"Studied [{video['title']}](sources/{target.name}) from `{source_id}`."
            )
            validation_errors = self.validate()
            if validation_errors:
                raise ValueError("OKF validation failed: " + "; ".join(validation_errors[:20]))
        except Exception:
            _restore_files(snapshots)
            raise
        return target

    def load_raw(
        self,
        *,
        video: dict[str, Any],
        raw_path: Path | str,
        transcript_source: str,
    ) -> tuple[Path, str]:
        """Load previously captured immutable evidence after revalidating identity."""
        confined = self.paths.confine_data(raw_path)
        self._preflight_raw(
            video=video,
            transcript_source=transcript_source,
            raw_path=confined,
            raw_digest=None,
        )
        _, body = _read_document(confined)
        return confined, _raw_transcript(body)

    def recall_paths_for_video(self, video: dict[str, Any], dossier: Path) -> tuple[Path, ...]:
        """Return every recall-indexed page changed by a source study."""
        topic = self.paths.confine_memory(
            self.paths.bundle_root / "concepts" / f"{_slug(str(video.get('topic') or 'other'))}.md"
        )
        index = self.paths.confine_memory(self.paths.bundle_root / "index.md")
        return dossier, topic, index

    def _preflight_raw(
        self,
        *,
        video: dict[str, Any],
        transcript_source: str,
        raw_path: Path | str,
        raw_digest: str | None,
    ) -> dict[str, Any]:
        video_id = str(video["video_id"])
        source_id = str(video["source_id"])
        url = str(video["url"])
        confined = self.paths.confine_data(raw_path)
        try:
            confined.relative_to(self.paths.raw_root.resolve(strict=False))
        except ValueError as exc:
            raise ValueError(f"Raw evidence is outside the raw corpus: {confined}") from exc
        expected = self.paths.confine_data(
            self.paths.raw_root / _slug(source_id) / _video_filename(video_id)
        )
        if confined != expected:
            raise ValueError(f"Raw evidence path does not match exact video identity: {confined}")
        if not confined.is_file() or confined.is_symlink():
            raise ValueError(f"Raw evidence is missing or not a regular file: {confined}")
        metadata, body = _read_document(confined)
        expected_identity = {
            "type": "curriculum-raw-transcript",
            "source_id": source_id,
            "video_id": video_id,
            "source_url": url,
            "transcript_source": transcript_source,
        }
        for key, expected_value in expected_identity.items():
            if str(metadata.get(key) or "") != str(expected_value):
                raise ValueError(f"Raw evidence {key} does not match dossier source")
        if metadata.get("immutable") is not True:
            raise ValueError("Raw evidence is not marked immutable")
        captured_at = metadata.get("captured_at")
        if not _parse_datetime(captured_at):
            raise ValueError("Raw evidence captured_at is missing or unparseable")
        transcript = _raw_transcript(body)
        recomputed = _sha256(transcript)
        if str(metadata.get("sha256") or "") != recomputed:
            raise ValueError("Raw evidence frontmatter digest does not match transcript")
        if raw_digest is not None and raw_digest != recomputed:
            raise ValueError("Caller raw digest does not match transcript")
        source_video_id = _video_id_from_url(url)
        if source_video_id and source_video_id != video_id:
            raise ValueError("Raw evidence source URL does not match video_id")
        timestamps = _raw_timestamp_tokens(transcript)
        return {
            "relative_path": confined.relative_to(self.paths.curriculum_data).as_posix(),
            "sha256": recomputed,
            "timestamps": timestamps,
        }

    def validate(self) -> list[str]:
        self.ensure()
        errors: list[str] = []
        seen_video_ids: set[str] = set()
        for path in sorted(self.paths.bundle_root.rglob("*.md")):
            try:
                metadata, body = _read_document(path)
            except ValueError as exc:
                errors.append(f"{path.relative_to(self.paths.bundle_root)}: {exc}")
                continue
            missing = sorted(_REQUIRED_FRONTMATTER - set(metadata))
            if missing:
                errors.append(
                    f"{path.relative_to(self.paths.bundle_root)}: "
                    f"missing frontmatter {', '.join(missing)}"
                )
            if path.parent.name == "sources":
                video_id = str(metadata.get("video_id") or "")
                if not video_id:
                    errors.append(f"{path.name}: missing video_id")
                elif video_id in seen_video_ids:
                    errors.append(f"{path.name}: duplicate video_id {video_id}")
                seen_video_ids.add(video_id)
                sources = metadata.get("sources")
                if not isinstance(sources, list) or not sources:
                    errors.append(f"{path.name}: source dossier has no sources")
                if "## Sources" not in body:
                    errors.append(f"{path.name}: source dossier has no Sources section")
                errors.extend(self._validate_source_dossier(path, metadata, body))
            for link in re.findall(r"\[[^\]]+\]\(([^)]+\.md)\)", body):
                target = (path.parent / link).resolve(strict=False)
                try:
                    target.relative_to(self.paths.bundle_root)
                except ValueError:
                    errors.append(f"{path.name}: link escapes bundle: {link}")
                    continue
                if not target.exists():
                    errors.append(f"{path.name}: broken link {link}")
        return errors

    def _validate_source_dossier(
        self, path: Path, metadata: dict[str, Any], body: str
    ) -> list[str]:
        errors: list[str] = []
        generated = metadata.get("generated")
        verified = metadata.get("verified")
        actor = generated.get("actor") if isinstance(generated, dict) else ""
        level = verified.get("level") if isinstance(verified, dict) else ""
        for label, value in (
            ("generated.at", generated.get("at") if isinstance(generated, dict) else None),
            ("verified.at", verified.get("at") if isinstance(verified, dict) else None),
        ):
            if not _parse_datetime(value):
                errors.append(f"{path.name}: {label} is missing or unparseable")
        stale_after = metadata.get("stale_after")
        parsed_stale = _parse_date(stale_after)
        if parsed_stale is None:
            errors.append(f"{path.name}: stale_after is missing or unparseable")
        elif parsed_stale < datetime.now(UTC).date():
            errors.append(f"{path.name}: source dossier freshness has expired")
        sources = metadata.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                published = source.get("published")
                if published and _parse_date(published) is None:
                    errors.append(f"{path.name}: source published date is unparseable")

        # Private vendor seed pages have no engine-owned raw transcript. They
        # retain upstream provenance and receive structural validation only.
        if level == _STRUCTURE_MIGRATED or actor == "thehomie/curriculum-seed-migrator":
            return errors
        if actor != _ENGINE_ACTOR:
            errors.append(f"{path.name}: unsupported source dossier generator")
            return errors

        video_id = str(metadata.get("video_id") or "")
        if path.name != _video_filename(video_id):
            errors.append(f"{path.name}: filename does not preserve exact video identity")
        raw_ref = metadata.get("raw_evidence")
        if not isinstance(raw_ref, dict):
            errors.append(f"{path.name}: missing raw_evidence")
            return errors
        relative = str(raw_ref.get("path") or "")
        try:
            raw_path = self.paths.confine_data(self.paths.curriculum_data / relative)
            raw_path.relative_to(self.paths.raw_root.resolve(strict=False))
        except ValueError:
            errors.append(f"{path.name}: raw evidence path is not confined")
            return errors
        expected_raw_path = self.paths.confine_data(
            self.paths.raw_root
            / _slug(str(metadata.get("source_id") or ""))
            / _video_filename(video_id)
        )
        if raw_path != expected_raw_path:
            errors.append(f"{path.name}: raw path/video identity mismatch")
        if not raw_path.is_file() or raw_path.is_symlink():
            errors.append(f"{path.name}: raw evidence is missing")
            return errors
        try:
            raw_metadata, raw_body = _read_document(raw_path)
        except ValueError as exc:
            errors.append(f"{path.name}: raw evidence {exc}")
            return errors
        try:
            transcript = _raw_transcript(raw_body)
        except ValueError as exc:
            errors.append(f"{path.name}: {exc}")
            return errors
        recomputed = _sha256(transcript)
        dossier_digest = str(raw_ref.get("sha256") or "")
        raw_digest = str(raw_metadata.get("sha256") or "")
        if raw_metadata.get("immutable") is not True:
            errors.append(f"{path.name}: raw evidence is not immutable")
        if not _parse_datetime(raw_metadata.get("captured_at")):
            errors.append(f"{path.name}: raw captured_at is unparseable")
        if recomputed != raw_digest:
            errors.append(f"{path.name}: raw digest does not match transcript")
        if recomputed != dossier_digest:
            errors.append(f"{path.name}: dossier digest does not match transcript")
        source = _youtube_source(metadata.get("sources"), video_id)
        expected_pairs = (
            ("video_id", raw_metadata.get("video_id"), video_id),
            ("source_id", raw_metadata.get("source_id"), metadata.get("source_id")),
            (
                "transcript_source",
                raw_metadata.get("transcript_source"),
                metadata.get("transcript_source"),
            ),
            (
                "source_url",
                raw_metadata.get("source_url"),
                source.get("url") if source else None,
            ),
        )
        for label, raw_value, dossier_value in expected_pairs:
            if str(raw_value or "") != str(dossier_value or ""):
                errors.append(f"{path.name}: raw/dossier {label} mismatch")
        if source is None:
            errors.append(f"{path.name}: missing youtube source identity")
        else:
            source_url_id = _video_id_from_url(str(source.get("url") or ""))
            if source_url_id and source_url_id != video_id:
                errors.append(f"{path.name}: source URL/video identity mismatch")
        errors.extend(
            f"{path.name}: {error}"
            for error in _validate_evidence_citations(
                body,
                video_id=video_id,
                raw_timestamps=_raw_timestamp_tokens(transcript),
            )
        )
        return errors

    def _update_topic(self, video: dict[str, Any], dossier: Path, analysis_markdown: str) -> None:
        topic = _slug(str(video.get("topic") or "other"))
        target = self.paths.confine_memory(self.paths.bundle_root / "concepts" / f"{topic}.md")
        if target.exists():
            metadata, body = _read_document(target)
        else:
            metadata = {
                "type": "concept",
                "title": topic.replace("-", " ").title(),
                "description": f"Canonical curriculum synthesis for {topic}.",
                "tags": ["curriculum", topic],
                "status": "active",
                "stale_after": (datetime.now(UTC) + timedelta(days=365)).date().isoformat(),
                "sources": [],
                "generated": {
                    "actor": "thehomie/curriculum-engine",
                    "at": _now(),
                },
                "verified": {"level": "machine-confirmed", "at": _now()},
            }
            body = f"# {metadata['title']}\n\n"
        source_ref = f"youtube:{video['video_id']}"
        sources = metadata.setdefault("sources", [])
        if isinstance(sources, list) and not any(
            isinstance(source, dict) and source.get("id") == source_ref for source in sources
        ):
            sources.append(
                {
                    "id": source_ref,
                    "url": str(video["url"]),
                    "title": str(video["title"]),
                }
            )
        link = f"../sources/{dossier.name}"
        if link not in body:
            takeaway = _extract_takeaway(analysis_markdown)
            body = (
                body.rstrip()
                + f"\n\n## {video['title']}\n\n"
                + (takeaway + "\n\n" if takeaway else "")
                + f"[Source dossier]({link}) [{source_ref}]\n"
            )
        metadata["generated"]["at"] = _now()
        atomic_write_text(target, _document(metadata, body))

    def _regenerate_index(self) -> None:
        concepts = sorted((self.paths.bundle_root / "concepts").glob("*.md"))
        sources = sorted((self.paths.bundle_root / "sources").glob("*.md"))
        metadata, _ = _read_document(self.paths.bundle_root / "index.md")
        body = (
            "# Curriculum Index\n\n"
            "Navigate concepts first, then open only the source dossiers needed.\n\n"
            "## Concepts\n\n"
            + (
                "\n".join(
                    f"- [{path.stem.replace('-', ' ').title()}](concepts/{path.name})"
                    for path in concepts
                )
                or "_No concepts studied yet._"
            )
            + "\n\n## Sources\n\n"
            + (
                "\n".join(
                    f"- [{_title_from_document(path)}](sources/{path.name})" for path in sources
                )
                or "_No sources studied yet._"
            )
            + "\n"
        )
        metadata["generated"]["at"] = _now()
        atomic_write_text(self.paths.bundle_root / "index.md", _document(metadata, body))

    def _append_log(self, line: str) -> None:
        path = self.paths.bundle_root / "log.md"
        metadata, body = _read_document(path)
        body = body.rstrip() + f"\n\n- {_now()} — {line}\n"
        metadata["generated"]["at"] = _now()
        atomic_write_text(path, _document(metadata, body))


def _document(metadata: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(
        metadata,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{body.strip()}\n"


def _read_document(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.S)
    if not match:
        raise ValueError("missing YAML frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise ValueError("frontmatter is not a mapping")
    return metadata, match.group(2).strip()


def _title_from_document(path: Path) -> str:
    try:
        metadata, _ = _read_document(path)
        return str(metadata.get("title") or path.stem)
    except ValueError:
        return path.stem


def _extract_takeaway(markdown: str) -> str:
    match = re.search(r"# Executive takeaway\s*\n(.*?)(?=\n## |\Z)", markdown, flags=re.S | re.I)
    if not match:
        return ""
    return match.group(1).strip()[:1200]


def _video_filename(video_id: str) -> str:
    """Return a readable, exact-identity filename safe on case-folding filesystems."""
    exact = str(video_id)
    readable = re.sub(r"[^A-Za-z0-9_-]+", "-", exact)[:64] or "video"
    identity = hashlib.sha256(exact.encode("utf-8")).hexdigest()[:16]
    return f"{readable}--{identity}.md"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _raw_transcript(body: str) -> str:
    match = re.match(r"^#[^\n]*\n+(.*)$", body.strip(), flags=re.S)
    if not match:
        raise ValueError("raw transcript body is missing its title")
    return match.group(1).strip()


def _raw_timestamp_tokens(transcript: str) -> set[str]:
    tokens = set(re.findall(rf"\[({_TIMESTAMP})\]", transcript))
    tokens.update(
        timestamp
        for _video_id, timestamp in re.findall(
            rf"\[youtube:([A-Za-z0-9_-]+)\s*@\s*({_TIMESTAMP})\]",
            transcript,
        )
    )
    return tokens


def _validate_evidence_citations(
    markdown: str, *, video_id: str, raw_timestamps: set[str]
) -> list[str]:
    match = re.search(
        r"^##\s+Evidence ledger\s*\n(.*?)(?=^##\s+|\Z)",
        markdown,
        flags=re.I | re.M | re.S,
    )
    if not match:
        return ["missing Evidence ledger section"]
    lines = [
        line.strip()
        for line in match.group(1).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    bullets = [line for line in lines if re.match(r"^[-*+]\s+", line)]
    entries = bullets or lines
    if not entries:
        return ["Evidence ledger has no entries"]
    errors: list[str] = []
    for number, entry in enumerate(entries, start=1):
        explicit = re.findall(
            rf"\[youtube:([A-Za-z0-9_-]+)\s*@\s*({_TIMESTAMP})\]",
            entry,
        )
        wrong_ids = sorted({source_id for source_id, _ in explicit if source_id != video_id})
        if wrong_ids:
            errors.append(f"evidence entry {number} cites wrong video_id {', '.join(wrong_ids)}")
        citations = [timestamp for source_id, timestamp in explicit if source_id == video_id]
        citations.extend(re.findall(rf"\[({_TIMESTAMP})\]", entry))
        # Compatibility for the first engine version, which emitted a bare
        # leading timestamp. New prompts require the canonical bracket form.
        if not citations:
            bare = re.search(rf"(?<![\w:])({_TIMESTAMP})(?![\w:])", entry)
            if bare:
                citations.append(bare.group(1))
        if not citations:
            errors.append(f"evidence entry {number} has no timestamp citation")
            continue
        missing = sorted({token for token in citations if token not in raw_timestamps})
        if missing:
            errors.append(
                f"evidence entry {number} cites timestamps absent from raw evidence: "
                + ", ".join(missing)
            )
    return errors


def _youtube_source(sources: Any, video_id: str) -> dict[str, Any] | None:
    if not isinstance(sources, list):
        return None
    expected = f"youtube:{video_id}"
    for source in sources:
        if isinstance(source, dict) and str(source.get("id") or "") == expected:
            return source
    return None


def _video_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.casefold().removeprefix("www.")
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        return str(parse_qs(parsed.query).get("v", [""])[0])
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    return ""


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, (str, datetime)):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"\d{8}", text):
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
    parsed_datetime = _parse_datetime(text)
    if parsed_datetime is not None:
        return parsed_datetime.date()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _snapshot_files(paths: tuple[Path, ...]) -> dict[Path, str | None]:
    return {
        path: path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        for path in paths
    }


def _restore_files(snapshots: dict[Path, str | None]) -> None:
    for path, content in snapshots.items():
        if content is None:
            if path.is_file():
                path.unlink()
        else:
            atomic_write_text(path, content)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return cleaned[:80] or "item"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
