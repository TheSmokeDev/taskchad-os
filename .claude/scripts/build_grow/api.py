"""Thin FastAPI adapter; existing orchestration middleware owns authentication."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from build_grow.models import ConflictError, NotFoundError, ValidationError
from build_grow.service import BuildGrowService
from build_grow.store import BuildGrowStore


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BusinessService(StrictBody):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2000)


class BusinessBrief(StrictBody):
    business_name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    industry: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=200)
    services: list[BusinessService] = Field(min_length=1, max_length=20)
    contact_email: str = Field(min_length=1, max_length=254)
    phone: str = Field(default="", max_length=60)
    site_url: str = Field(default="", max_length=1000)
    repository_url: str = Field(default="", max_length=1000)
    brand_color: str = Field(default="#eab308", pattern=r"^#[0-9a-fA-F]{6}$")


class CreateProjectBody(StrictBody):
    name: str = Field(min_length=1, max_length=160)
    brief: BusinessBrief


class UpdateBriefBody(StrictBody):
    brief: BusinessBrief
    expected_revision: int = Field(ge=1)


class CreateJobBody(StrictBody):
    kind: Literal["website", "geo"]
    source_job_id: int | None = Field(default=None, ge=1)
    source_files: dict[str, str] | None = None


class ReviewBody(StrictBody):
    decision: Literal["approved", "changes_requested"]
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str = Field(default="", max_length=2000)


def _default_service() -> BuildGrowService:
    import config

    # Call-time profile resolution: never use the shared chat or global vault stores.
    db_path = Path(config.get_orchestration_db_path()).with_name("build-grow.db")
    return BuildGrowService(BuildGrowStore(db_path))


def _workspace(request: Request) -> int:
    # No fallback to workspace 1: a router without auth middleware fails closed.
    workspace_id = getattr(request.state, "workspace_id", None)
    if not isinstance(workspace_id, int) or workspace_id < 1:
        raise HTTPException(status_code=403, detail="workspace binding required")
    return workspace_id


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_router(service_factory: Callable[[], BuildGrowService] | None = None) -> APIRouter:
    """Allow isolated route tests without importing the complete OS runtime."""
    router = APIRouter(prefix="/api/build-grow", tags=["Build + Grow"])

    def service() -> BuildGrowService:
        return (service_factory or _default_service)()

    @router.get("/projects")
    def list_projects(request: Request):
        ws = _workspace(request)
        return {"projects": _call(service().store.list_projects, workspace_id=ws)}

    @router.post("/projects")
    def create_project(body: CreateProjectBody, request: Request):
        ws = _workspace(request)
        return _call(service().create_project, body.name, body.brief.model_dump(), workspace_id=ws)

    @router.get("/projects/{project_id}")
    def get_project(project_id: int, request: Request):
        ws = _workspace(request)
        return _call(service().store.get_project, project_id, workspace_id=ws)

    @router.get("/projects/{project_id}/brief")
    def get_brief(project_id: int, request: Request):
        ws = _workspace(request)
        return _call(service().store.get_project, project_id, workspace_id=ws)

    @router.put("/projects/{project_id}/brief")
    def update_brief(project_id: int, body: UpdateBriefBody, request: Request):
        ws = _workspace(request)
        return _call(
            service().update_brief,
            project_id,
            body.brief.model_dump(),
            body.expected_revision,
            workspace_id=ws,
        )

    @router.get("/projects/{project_id}/jobs")
    def list_jobs(project_id: int, request: Request):
        ws = _workspace(request)
        return {"jobs": _call(service().store.list_jobs, project_id, workspace_id=ws)}

    @router.post("/projects/{project_id}/jobs")
    def create_job(project_id: int, body: CreateJobBody, request: Request):
        ws = _workspace(request)
        return _call(
            service().create_job,
            project_id,
            body.kind,
            body.source_job_id,
            body.source_files,
            workspace_id=ws,
        )

    @router.get("/jobs/{job_id}")
    def get_job(job_id: int, request: Request):
        ws = _workspace(request)
        return _call(service().store.get_job, job_id, workspace_id=ws)

    @router.post("/jobs/{job_id}/run")
    def run_job(job_id: int, request: Request):
        ws = _workspace(request)
        return _call(service().run_job, job_id, workspace_id=ws)

    @router.get("/deliverables/{deliverable_id}/download")
    def download(deliverable_id: int, request: Request):
        ws = _workspace(request)
        metadata, content = _call(
            service().store.download_deliverable, deliverable_id, workspace_id=ws
        )
        return Response(
            content,
            media_type=metadata["media_type"],
            headers={
                "Content-Disposition": f'attachment; filename="{metadata["filename"]}"',
                "ETag": f'"{metadata["sha256"]}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )

    @router.post("/deliverables/{deliverable_id}/review")
    def review(deliverable_id: int, body: ReviewBody, request: Request):
        ws = _workspace(request)
        return _call(
            service().review_deliverable,
            deliverable_id,
            body.decision,
            body.expected_sha256,
            body.note,
            workspace_id=ws,
        )

    return router


router = create_router()
