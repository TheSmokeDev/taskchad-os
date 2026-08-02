"""Migrate a local/private OKF v0.1 knowledge seed into a persona bundle."""

from __future__ import annotations

import hashlib
import io
import re
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from shared import atomic_write_text

from .bundle import CurriculumBundle
from .config import get_curriculum_settings
from .ledger import CurriculumLedger
from .paths import resolve_curriculum_paths


def import_okf_seed(
    persona_id: str,
    seed_root: Path | str,
    *,
    source_id: str = "private-okf-seed",
    generate_embeddings: bool = True,
) -> dict[str, Any]:
    """Import synthesized Markdown only; vendor/raw seed remains data-private."""
    root = Path(seed_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Seed root is not a directory: {root}")
    settings = get_curriculum_settings(persona_id)
    paths = resolve_curriculum_paths(persona_id, settings.domain)
    bundle = CurriculumBundle(paths, settings.domain)
    bundle.ensure()
    ledger = CurriculumLedger(paths.ledger_path, persona_id)
    configured_source = next(
        (source for source in settings.sources if source.id == source_id), None
    )
    ledger.upsert_source(
        source_id,
        kind=configured_source.kind if configured_source else "okf_seed",
        url=configured_source.url if configured_source else root.as_uri(),
        policy=configured_source.policy if configured_source else "full",
        metadata={"seed_root": str(root)},
    )
    imported = 0
    manifest_rows = 0
    manifest_video_ids: set[str] = set()
    skipped = 0
    errors: list[str] = []
    candidates = [
        path
        for path in sorted(root.rglob("*.md"))
        if ".git" not in path.parts
        and path.name.casefold() not in {"readme.md", "making-of.md", "index.md"}
    ]
    for candidate in candidates:
        try:
            source_path = _resolve_seed_candidate(candidate, root)
            metadata, body = _read_document(source_path)
            kind = _kind(metadata, source_path, root)
            if kind not in {"concept", "entity", "source"}:
                skipped += 1
                continue
            target = _target(paths.bundle_root, kind, source_path, root)
            source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
            existing = (
                target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            )
            existing_metadata, _ = _read_text_document(existing) if existing else ({}, "")
            if existing_metadata.get("upstream_sha256") == source_sha256:
                migrated = existing
                skipped += 1
            else:
                migrated = _migrate(
                    metadata,
                    body,
                    source_path=source_path,
                    seed_root=root,
                    source_id=source_id,
                    source_sha256=source_sha256,
                    kind=kind,
                )
                atomic_write_text(target, migrated)
                imported += 1
            if kind == "source":
                migrated_metadata, _ = _read_text_document(migrated)
                manifest_video_ids.add(str(migrated_metadata["video_id"]))
                source_url = _first_source_url(migrated_metadata.get("sources"))
                if ledger.import_studied_video(
                    video_id=str(migrated_metadata["video_id"]),
                    source_id=source_id,
                    url=source_url,
                    title=str(migrated_metadata["title"]),
                    channel=str(
                        migrated_metadata.get("channel")
                        or migrated_metadata.get("author")
                        or source_id
                    ),
                    upload_date=str(
                        migrated_metadata.get("published")
                        or migrated_metadata.get("upload_date")
                        or ""
                    ),
                    dossier_path=str(target),
                ):
                    manifest_rows += 1
        except Exception as exc:
            errors.append(f"{_candidate_label(candidate, root)}: {type(exc).__name__}: {exc}")
    pruned_rows = ledger.prune_seed_imports(source_id, manifest_video_ids)
    for row in pruned_rows:
        dossier = Path(str(row.get("dossier_path") or "")).resolve(strict=False)
        try:
            dossier.relative_to(paths.bundle_root)
        except ValueError:
            continue
        if dossier.is_file():
            dossier.unlink()
    if imported or pruned_rows:
        bundle._regenerate_index()
        bundle._append_log(
            f"Imported private synthesized seed `{source_id}` "
            f"({imported} changed, {skipped} unchanged/skipped, "
            f"{len(pruned_rows)} pruned)."
        )
    index_stats: dict[str, int] | None = None
    try:
        from memory_index import sync_index

        # sync_index is intentionally batch-scoped to the whole persona memory
        # root. Besides indexing newly imported pages, this reconciles deleted
        # source dossiers and repairs an empty/stale profile DB on an unchanged
        # seed rerun. Its per-file progress output would corrupt
        # ``import-seed --json``'s quiet stdout contract, so retain only the
        # structured counts returned by the API.
        with redirect_stdout(io.StringIO()):
            index_stats = sync_index(
                memory_dir=paths.memory_root,
                generate_embeddings=generate_embeddings,
            )
    except Exception as exc:
        errors.append(f"recall index sync: {type(exc).__name__}: {exc}")
    validation_errors = bundle.validate()
    return {
        "success": not errors and not validation_errors,
        "persona_id": persona_id,
        "source_id": source_id,
        "seed_root": str(root),
        "imported": imported,
        "manifest_rows": manifest_rows,
        "pruned": len(pruned_rows),
        "skipped": skipped,
        "errors": errors[:50],
        "validation_errors": validation_errors,
        "index_stats": index_stats,
    }


def _resolve_seed_candidate(path: Path, seed_root: Path) -> Path:
    """Return a regular in-root seed file without following seed symlinks."""
    cursor = path
    while cursor != seed_root:
        if cursor.is_symlink():
            raise ValueError("Seed candidate uses a symbolic link")
        parent = cursor.parent
        if parent == cursor:
            raise ValueError("Seed candidate has no path to seed root")
        cursor = parent
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(seed_root)
    except ValueError as exc:
        raise ValueError(f"Seed candidate escapes seed root: {resolved}") from exc
    if not resolved.is_file():
        raise ValueError("Seed candidate is not a regular file")
    return resolved


def _candidate_label(path: Path, seed_root: Path) -> str:
    try:
        return path.relative_to(seed_root).as_posix()
    except ValueError:
        return str(path)


def _read_document(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.S)
    if not match:
        return {}, text.strip()
    metadata = yaml.safe_load(match.group(1))
    return (metadata if isinstance(metadata, dict) else {}), match.group(2).strip()


def _read_text_document(text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.S)
    if not match:
        raise ValueError("migrated seed page has no frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise ValueError("migrated seed frontmatter is not a mapping")
    return metadata, match.group(2).strip()


def _kind(metadata: dict[str, Any], path: Path, seed_root: Path) -> str:
    raw = str(metadata.get("type") or "").casefold()
    if raw in {"concept", "entity", "source"}:
        return raw
    relative_parts = {part.casefold() for part in path.relative_to(seed_root).parts[:-1]}
    for folder, kind in (
        ("concepts", "concept"),
        ("entities", "entity"),
        ("sources", "source"),
    ):
        if folder in relative_parts:
            return kind
    return ""


def _target(
    bundle_root: Path,
    kind: str,
    source_path: Path,
    seed_root: Path,
) -> Path:
    relative = source_path.relative_to(seed_root)
    expected = {"concept": "concepts", "entity": "entities", "source": "sources"}[kind]
    parts = list(relative.parts)
    try:
        start = next(index for index, part in enumerate(parts) if part.casefold() == expected)
    except StopIteration as exc:
        raise ValueError(f"Seed page has no {expected}/ path: {relative}") from exc
    return bundle_root.joinpath(*parts[start:])


def _migrate(
    metadata: dict[str, Any],
    body: str,
    *,
    source_path: Path,
    seed_root: Path,
    source_id: str,
    source_sha256: str,
    kind: str,
) -> str:
    now = datetime.now(UTC)
    title = str(metadata.get("title") or source_path.stem.replace("-", " ").title())
    sources = metadata.get("sources")
    if not isinstance(sources, list):
        sources = []
    seed_ref = {
        "id": f"seed:{source_id}:{source_path.relative_to(seed_root).as_posix()}",
        "title": title,
    }
    if not any(isinstance(value, dict) and value.get("id") == seed_ref["id"] for value in sources):
        sources.append(seed_ref)
    if kind == "source":
        youtube_id = str(metadata.get("video_id") or metadata.get("youtube_id") or "")
        source_url = str(metadata.get("url") or "")
        if youtube_id or source_url:
            youtube_ref = {
                "id": f"youtube:{youtube_id}" if youtube_id else source_url,
                "url": source_url,
                "title": title,
                "published": str(metadata.get("published") or ""),
            }
            if not any(
                isinstance(value, dict) and value.get("id") == youtube_ref["id"]
                for value in sources
            ):
                sources.insert(0, youtube_ref)
    migrated: dict[str, Any] = {
        **metadata,
        "type": kind,
        "title": title,
        "description": str(
            metadata.get("description")
            or f"Private synthesized seed page imported from {source_id}."
        ),
        "status": str(metadata.get("status") or "active"),
        "stale_after": str(
            metadata.get("stale_after") or (now + timedelta(days=180)).date().isoformat()
        ),
        "sources": sources,
        "generated": {
            "actor": "thehomie/curriculum-seed-migrator",
            "at": now.isoformat(timespec="seconds"),
            "upstream": source_id,
            "upstream_generation": metadata.get("generation") or metadata.get("generated"),
        },
        "verified": {
            "level": "structure-migrated",
            "at": now.isoformat(timespec="seconds"),
            "method": "local-private-seed-import",
        },
        "upstream_sha256": source_sha256,
    }
    if kind == "source":
        migrated["video_id"] = str(
            metadata.get("video_id")
            or metadata.get("youtube_id")
            or _video_id_from_sources(sources)
            or hashlib.sha256(seed_ref["id"].encode()).hexdigest()[:16]
        )
        body = re.sub(
            r"\[Raw transcript\]\([^)]+\.md\)",
            "Raw transcript retained in the private vendor seed.",
            body,
            flags=re.I,
        )
        if "## Sources" not in body:
            body = body.rstrip() + "\n\n## Sources\n\n- Private seed provenance above.\n"
    frontmatter = yaml.safe_dump(
        migrated,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{body.strip()}\n"


def _video_id_from_sources(sources: list[Any]) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        value = str(source.get("id") or source.get("url") or "")
        match = re.search(r"(?:youtube:|[?&]v=|youtu\.be/)([A-Za-z0-9_-]{6,})", value)
        if match:
            return match.group(1)
    return ""


def _first_source_url(sources: Any) -> str:
    if not isinstance(sources, list):
        return ""
    for source in sources:
        if not isinstance(source, dict):
            continue
        value = str(source.get("url") or "")
        if value.startswith("https://"):
            return value
    return ""
