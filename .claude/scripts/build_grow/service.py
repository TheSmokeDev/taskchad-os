"""Bounded deterministic jobs; there is no provider execution or deployment here."""

from __future__ import annotations

import io
import logging
import re
import stat
import zipfile
from collections.abc import Callable

from build_grow.models import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_FILES,
    Artifact,
    ConflictError,
    NotFoundError,
    ValidationError,
    bounded_text,
    normalize_brief,
    normalize_source_files,
    validate_source_filename,
)
from build_grow.store import BuildGrowStore

logger = logging.getLogger(__name__)


def read_site_archive(content: bytes) -> dict[str, str]:
    """Read a bounded static site ZIP in memory; never extract or execute its contents."""
    if len(content) > MAX_SOURCE_BYTES:
        raise ValidationError("website archive exceeds the 2 MiB limit")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_SOURCE_FILES:
                raise ValidationError("website archive must contain 1–30 static files")
            if sum(entry.file_size for entry in entries) > MAX_SOURCE_BYTES:
                raise ValidationError("website archive exceeds the 2 MiB uncompressed limit")
            files = {}
            for entry in entries:
                filename = validate_source_filename(entry.filename)
                if filename in files:
                    raise ValidationError("website archive contains duplicate filenames")
                if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                    raise ValidationError("website archive must contain ordinary unencrypted files")
                with archive.open(entry) as source:
                    raw = source.read(MAX_SOURCE_BYTES + 1)
                if len(raw) > MAX_SOURCE_BYTES:
                    raise ValidationError("website archive member exceeds the 2 MiB limit")
                files[filename] = raw.decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError, RuntimeError, NotImplementedError) as exc:
        raise ValidationError("website archive is not a supported UTF-8 static site ZIP") from exc
    return normalize_source_files(files)


def _validate_artifacts(artifacts: object) -> list[Artifact]:
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 10:
        raise ValidationError("builder must return 1–10 artifacts")
    names, total = set(), 0
    for artifact in artifacts:
        if (
            not isinstance(artifact, Artifact)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", artifact.filename)
            or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", artifact.media_type)
            or not isinstance(artifact.content, bytes)
        ):
            raise ValidationError("builder returned an invalid artifact")
        if artifact.filename in names or len(artifact.content) > MAX_SOURCE_BYTES:
            raise ValidationError("builder artifacts must be unique and at most 2 MiB each")
        names.add(artifact.filename)
        total += len(artifact.content)
    if total > 8 * 1024 * 1024:
        raise ValidationError("builder artifacts exceed the 8 MiB total limit")
    return artifacts


class BuildGrowService:
    def __init__(
        self,
        store: BuildGrowStore,
        artifact_builder: Callable[[str, dict, dict[str, str]], list[Artifact]] | None = None,
    ):
        self.store = store
        self.artifact_builder = artifact_builder

    def create_project(self, name: str, brief: dict, *, workspace_id: int) -> dict:
        return self.store.create_project(
            bounded_text(name, "project name", 160, required=True),
            normalize_brief(brief),
            workspace_id=workspace_id,
        )

    def update_brief(
        self, project_id: int, brief: dict, expected_revision: int, *, workspace_id: int
    ) -> dict:
        if not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValidationError("expected_revision must be a positive integer")
        return self.store.update_brief(
            project_id, normalize_brief(brief), expected_revision, workspace_id=workspace_id
        )

    def create_job(
        self,
        project_id: int,
        kind: str,
        source_job_id: int | None = None,
        source_files: dict[str, str] | None = None,
        *,
        workspace_id: int,
    ) -> dict:
        if kind not in {"website", "geo"}:
            raise ValidationError("kind must be website or geo")
        project = self.store.get_project(project_id, workspace_id=workspace_id)
        if source_job_id is not None and source_files is not None:
            raise ValidationError("choose source_job_id or source_files, not both")
        if kind == "website" and (source_job_id is not None or source_files is not None):
            raise ValidationError("website jobs do not accept source files or source jobs")
        frozen_sources = {}
        if kind == "geo":
            if source_files is not None:
                frozen_sources = normalize_source_files(source_files)
            else:
                source = (
                    self.store.get_job(source_job_id, workspace_id=workspace_id)
                    if source_job_id is not None
                    else self.store.latest_website_job(
                        project_id, project["brief_revision"], workspace_id=workspace_id
                    )
                )
                if source is None:
                    raise ValidationError("create a website job or supply HTML source files first")
                if source["project_id"] != project_id:
                    raise NotFoundError("source job not found in this project")
                if (
                    source["kind"] != "website"
                    or source["status"] != "ready_for_review"
                    or source["brief_revision"] != project["brief_revision"]
                ):
                    raise ConflictError("source website must be ready and use the current brief")
                archives = [
                    item
                    for item in source["deliverables"]
                    if item["media_type"] == "application/zip"
                ]
                if len(archives) != 1:
                    raise ValidationError("source website must contain one static site ZIP")
                _, archive = self.store.download_deliverable(
                    archives[0]["id"], workspace_id=workspace_id
                )
                frozen_sources = read_site_archive(archive)
                source_job_id = source["id"]
        return self.store.create_job(
            project_id,
            kind,
            frozen_sources,
            source_job_id,
            project["brief_revision"],
            workspace_id=workspace_id,
        )

    def run_job(self, job_id: int, *, workspace_id: int) -> dict:
        snapshot = self.store.claim_job(job_id, workspace_id=workspace_id)
        try:
            builder = self.artifact_builder
            if builder is None:
                from build_grow.artifacts import build_artifacts

                builder = build_artifacts
            artifacts = _validate_artifacts(
                builder(snapshot["kind"], snapshot["brief"], snapshot["source_files"])
            )
            return self.store.finish_job(job_id, artifacts, workspace_id=workspace_id)
        except Exception:
            logger.exception(
                "Build + Grow generation failed for job %s in workspace %s", job_id, workspace_id
            )
            return self.store.fail_job(job_id, workspace_id=workspace_id)

    def review_deliverable(
        self,
        deliverable_id: int,
        decision: str,
        expected_sha256: str,
        note: str = "",
        *,
        workspace_id: int,
    ) -> dict:
        if decision not in {"approved", "changes_requested"}:
            raise ValidationError("decision must be approved or changes_requested")
        if not isinstance(expected_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ):
            raise ValidationError("expected_sha256 must be a SHA-256 hex digest")
        return self.store.review_deliverable(
            deliverable_id,
            decision,
            expected_sha256,
            bounded_text(note, "review note", 2000),
            workspace_id=workspace_id,
        )
