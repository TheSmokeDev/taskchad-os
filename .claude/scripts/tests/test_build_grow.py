"""Boundaries for persistent briefs, workspace-scoped jobs, and review receipts."""

from __future__ import annotations

import io
import sqlite3
import stat
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from build_grow.api import create_router  # noqa: E402 -- standalone scripts path above
from build_grow.models import Artifact, ConflictError, NotFoundError, ValidationError  # noqa: E402
from build_grow.service import BuildGrowService, read_site_archive  # noqa: E402
from build_grow.store import BuildGrowStore  # noqa: E402


@pytest.fixture
def brief():
    return {
        "business_name": "Example Plumbing",
        "description": "Owner-provided plumbing services.",
        "services": [{"name": "Repairs", "description": "Leak and fixture repairs."}],
        "contact_email": "hello@example.com",
        "site_url": "https://example.com",
    }


def archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as out:
        for filename, content in files.items():
            out.writestr(filename, content)
    return data.getvalue()


def builder(kind, brief, source_files):
    if kind == "website":
        return [
            Artifact(
                "site.zip",
                "application/zip",
                archive(
                    {
                        "index.html": f"<html><title>{brief['business_name']}</title></html>",
                        "styles.css": "body { color: black; }",
                    }
                ),
            )
        ]
    return [Artifact("geo.json", "application/json", b'{"status":"review"}')]


@pytest.fixture
def svc(tmp_path):
    return BuildGrowService(BuildGrowStore(tmp_path / "build-grow.db"), builder)


def site_job(svc, brief, workspace_id=2):
    project = svc.create_project("Website", brief, workspace_id=workspace_id)
    job = svc.create_job(project["id"], "website", workspace_id=workspace_id)
    return project, svc.run_job(job["id"], workspace_id=workspace_id)


def test_persistent_revisions_freeze_job_inputs(svc, brief):
    project = svc.create_project("Website", brief, workspace_id=2)
    job = svc.create_job(project["id"], "website", workspace_id=2)
    updated = svc.update_brief(
        project["id"], {**brief, "business_name": "Changed"}, 1, workspace_id=2
    )
    assert updated["brief_revision"] == 2
    reopened = BuildGrowStore(svc.store.db_path)
    assert (
        reopened.get_project(project["id"], workspace_id=2)["brief"]["business_name"] == "Changed"
    )
    result = svc.run_job(job["id"], workspace_id=2)
    assert result["brief_revision"] == 1
    _, content = svc.store.download_deliverable(result["deliverables"][0]["id"], workspace_id=2)
    assert "Example Plumbing" in read_site_archive(content)["index.html"]
    with pytest.raises(ConflictError):
        svc.update_brief(project["id"], brief, 1, workspace_id=2)
    with pytest.raises(ConflictError):
        svc.review_deliverable(
            result["deliverables"][0]["id"],
            "approved",
            result["deliverables"][0]["sha256"],
            workspace_id=2,
        )


def test_cross_workspace_reads_writes_downloads_and_reviews_are_absent(svc, brief):
    project, job = site_job(svc, brief)
    deliverable = job["deliverables"][0]
    other = svc.create_project("Other", brief, workspace_id=3)
    calls = [
        lambda: svc.store.get_project(project["id"], workspace_id=3),
        lambda: svc.update_brief(project["id"], brief, 1, workspace_id=3),
        lambda: svc.store.list_jobs(project["id"], workspace_id=3),
        lambda: svc.create_job(project["id"], "website", workspace_id=3),
        lambda: svc.store.get_job(job["id"], workspace_id=3),
        lambda: svc.run_job(job["id"], workspace_id=3),
        lambda: svc.store.download_deliverable(deliverable["id"], workspace_id=3),
        lambda: svc.review_deliverable(
            deliverable["id"], "approved", deliverable["sha256"], workspace_id=3
        ),
        lambda: svc.create_job(other["id"], "geo", source_job_id=job["id"], workspace_id=3),
    ]
    for call in calls:
        with pytest.raises(NotFoundError):
            call()
    assert svc.store.get_job(job["id"], workspace_id=2) == job
    assert len(svc.store.list_projects(workspace_id=3)) == 1


def test_duplicate_run_is_claimed_once(svc, brief):
    project = svc.create_project("Website", brief, workspace_id=2)
    job = svc.create_job(project["id"], "website", workspace_id=2)
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def slow_builder(*args):
        calls.append(args)
        entered.set()
        assert finish.wait(5)
        return builder(*args)

    svc.artifact_builder = slow_builder
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(svc.run_job, job["id"], workspace_id=2)
        assert entered.wait(5)
        with pytest.raises(ConflictError):
            svc.run_job(job["id"], workspace_id=2)
        finish.set()
        assert running.result()["status"] == "ready_for_review"
    assert len(calls) == 1


def test_restart_recovers_expired_jobs_only_in_bound_workspace(svc, brief):
    project = svc.create_project("Website", brief, workspace_id=2)
    other = svc.create_project("Other", brief, workspace_id=3)
    stale = svc.create_job(project["id"], "website", workspace_id=2)
    fresh = svc.create_job(project["id"], "website", workspace_id=2)
    foreign = svc.create_job(other["id"], "website", workspace_id=3)
    for job, workspace in ((stale, 2), (fresh, 2), (foreign, 3)):
        svc.store.claim_job(job["id"], workspace_id=workspace)
    with sqlite3.connect(svc.store.db_path) as conn:
        conn.execute(
            "UPDATE jobs SET updated_at='2000-01-01T00:00:00.000Z' WHERE id IN (?,?)",
            (stale["id"], foreign["id"]),
        )
    reopened = BuildGrowStore(svc.store.db_path)
    assert reopened.get_job(stale["id"], workspace_id=2)["status"] == "failed"
    assert reopened.get_job(fresh["id"], workspace_id=2)["status"] == "running"
    with sqlite3.connect(svc.store.db_path) as conn:
        assert (
            conn.execute("SELECT status FROM jobs WHERE id=?", (foreign["id"],)).fetchone()[0]
            == "running"
        )
    with pytest.raises(ConflictError):
        reopened.finish_job(
            stale["id"], [Artifact("late.txt", "text/plain", b"late")], workspace_id=2
        )
    assert reopened.get_job(stale["id"], workspace_id=2)["deliverables"] == []


def test_failure_retains_safe_failed_state_without_partial_deliverables(svc, brief):
    project = svc.create_project("Website", brief, workspace_id=2)
    job = svc.create_job(project["id"], "website", workspace_id=2)

    def broken(*args):
        raise RuntimeError("sensitive internal path or credential")

    svc.artifact_builder = broken
    result = svc.run_job(job["id"], workspace_id=2)
    assert result["status"] == "failed" and not result["deliverables"]
    assert "sensitive" not in result["error"]
    with pytest.raises(ConflictError):
        svc.run_job(job["id"], workspace_id=2)


def test_review_is_hash_bound_and_history_is_immutable(svc, brief):
    _, job = site_job(svc, brief)
    artifact = job["deliverables"][0]
    with pytest.raises(ConflictError):
        svc.review_deliverable(artifact["id"], "approved", "0" * 64, workspace_id=2)
    assert (
        svc.review_deliverable(artifact["id"], "approved", artifact["sha256"], workspace_id=2)[
            "review_status"
        ]
        == "approved"
    )
    svc.review_deliverable(
        artifact["id"],
        "changes_requested",
        artifact["sha256"],
        "Correct the details",
        workspace_id=2,
    )
    with sqlite3.connect(svc.store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 2
        for table in ("deliverables", "reviews", "brief_revisions"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(f"DELETE FROM {table}")


def test_geo_uses_current_project_site_and_rejects_other_project_or_old_revision(svc, brief):
    project, website = site_job(svc, brief)
    job = svc.create_job(project["id"], "geo", workspace_id=2)
    assert job["source_job_id"] == website["id"]
    assert svc.run_job(job["id"], workspace_id=2)["status"] == "ready_for_review"
    other = svc.create_project("Other", brief, workspace_id=2)
    with pytest.raises(NotFoundError):
        svc.create_job(other["id"], "geo", source_job_id=website["id"], workspace_id=2)
    svc.update_brief(project["id"], brief, 1, workspace_id=2)
    with pytest.raises(ConflictError):
        svc.create_job(project["id"], "geo", source_job_id=website["id"], workspace_id=2)
    with pytest.raises(ValidationError):
        svc.create_job(project["id"], "geo", workspace_id=2)


@pytest.mark.parametrize(
    "name",
    [
        "../index.html",
        "/index.html",
        "a/../index.html",
        "a\\index.html",
        "https:index.html",
        "index.js",
    ],
)
def test_unsafe_source_paths_are_refused(svc, brief, name):
    project = svc.create_project("Website", brief, workspace_id=2)
    with pytest.raises(ValidationError):
        svc.create_job(project["id"], "geo", source_files={name: "<html/>"}, workspace_id=2)
    with pytest.raises(ValidationError):
        read_site_archive(archive({name: "<html/>"}))


def test_zip_refuses_duplicates_symlinks_and_uncompressed_size():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as out:
        out.writestr("index.html", "<html/>")
        with pytest.warns(UserWarning):
            out.writestr("index.html", "duplicate")
    with pytest.raises(ValidationError, match="duplicate"):
        read_site_archive(data.getvalue())
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as out:
        entry = zipfile.ZipInfo("index.html")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        out.writestr(entry, "outside.html")
    with pytest.raises(ValidationError, match="ordinary"):
        read_site_archive(data.getvalue())
    bomb = archive({"index.html": "x" * (2 * 1024 * 1024 + 1)})
    with pytest.raises(ValidationError, match="uncompressed"):
        read_site_archive(bomb)


@pytest.mark.parametrize(
    "field,value",
    [
        ("site_url", "http://example.com"),
        ("site_url", "https://user:password@example.com"),
        ("site_url", "https://example.com?a=b"),
        ("site_url", "https://example.com/#fragment"),
        ("repository_url", "https://other.example/a/b"),
        ("brand_color", "red"),
    ],
)
def test_brief_url_and_color_contract(svc, brief, field, value):
    with pytest.raises(ValidationError):
        svc.create_project("Website", {**brief, field: value}, workspace_id=2)


def test_api_workspace_binding_and_attachment_contract(svc, brief):
    app = FastAPI()

    @app.middleware("http")
    async def scoped(request, call_next):
        token = request.headers.get("Authorization")
        if token in {"Bearer a", "Bearer b"}:
            request.state.workspace_id = 2 if token == "Bearer a" else 3
        return await call_next(request)

    app.include_router(create_router(lambda: svc))
    client = TestClient(app)
    assert client.get("/api/build-grow/projects").status_code == 403
    headers = {"Authorization": "Bearer a"}
    project = client.post(
        "/api/build-grow/projects", headers=headers, json={"name": "Example", "brief": brief}
    ).json()
    assert set(project) == {"id", "name", "brief_revision", "brief", "created_at", "updated_at"}
    path = f"/api/build-grow/projects/{project['id']}"
    assert client.get(path, headers={"Authorization": "Bearer b"}).status_code == 404
    job = client.post(f"{path}/jobs", headers=headers, json={"kind": "website"}).json()
    result = client.post(f"/api/build-grow/jobs/{job['id']}/run", headers=headers).json()
    artifact = result["deliverables"][0]
    download = client.get(
        f"/api/build-grow/deliverables/{artifact['id']}/download", headers=headers
    )
    assert download.headers["content-disposition"] == 'attachment; filename="site.zip"'
    assert download.headers["etag"] == f'"{artifact["sha256"]}"'
    assert read_site_archive(download.content)["index.html"]
    stale = client.put(
        f"{path}/brief", headers=headers, json={"brief": brief, "expected_revision": 2}
    )
    assert stale.status_code == 409


def test_route_policies_cover_all_build_grow_routes():
    from orchestration.route_policy import ROUTE_POLICY, all_registered_routes

    app = FastAPI()
    app.include_router(create_router(lambda: None))
    routes = {key for key in all_registered_routes(app) if key[1].startswith("/api/build-grow/")}
    assert len(routes) == 11
    assert all(ROUTE_POLICY[key] == "tenant_workspace" for key in routes)
