"""TaskChad plugin HTTP contract and boundary tests; no paid providers or real credentials."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "plugins" / "taskchad-build-grow"
spec = importlib.util.spec_from_file_location(
    "taskchad_plugin_client", PLUGIN / "scripts" / "taskchad_client.py"
)
client_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_module)
Client = client_module.TaskChadClient
ClientError = client_module.ClientError
BASE = "/api/build-grow"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _handle(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)
        payload = json.loads(raw) if raw else None
        self.server.calls.append(
            (self.command, self.path, self.headers.get("Authorization"), payload)
        )
        status, body, headers = self.server.responses.get(
            (self.command, self.path), (200, {"ok": True}, {})
        )
        encoded = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        self.send_response(status)
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _handle  # noqa: N815 -- BaseHTTPRequestHandler dispatch names
    do_POST = _handle  # noqa: N815 -- BaseHTTPRequestHandler dispatch names
    do_PUT = _handle  # noqa: N815 -- BaseHTTPRequestHandler dispatch names


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        cls.server.responses = {}
        cls.server.calls = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.server.responses.clear()
        self.server.calls.clear()
        self.client = Client(self.url, "fixture-token-not-a-real-credential")
        self.tempdir = tempfile.TemporaryDirectory()
        self.output = Path(self.tempdir.name) / "draft.zip"
        self.addCleanup(self.tempdir.cleanup)

    def response(self, method, suffix, body, status=200, headers=None):
        self.server.responses[(method, BASE + suffix)] = (status, body, headers or {})

    def test_full_workflow_uses_actual_routes_and_revision_digest_guards(self):
        brief = {"business_name": "Example", "description": "Verified fixture business facts."}
        artifact = b"fixture zip bytes"
        digest = hashlib.sha256(artifact).hexdigest()
        project = {"id": 1, "name": "Example", "brief_revision": 1, "brief": brief}
        job = {
            "id": 2,
            "project_id": 1,
            "status": "ready_for_review",
            "deliverables": [{"id": 3, "sha256": digest}],
        }
        self.response("GET", "/projects", {"projects": [project]})
        self.response("POST", "/projects", project)
        self.response("GET", "/projects/1", project)
        self.response("PUT", "/projects/1/brief", dict(project, brief_revision=2))
        self.response("GET", "/projects/1/jobs", {"jobs": [job]})
        self.response("POST", "/projects/1/jobs", dict(job, status="queued"))
        self.response("POST", "/jobs/2/run", job)
        self.response("GET", "/jobs/2", job)
        self.response("GET", "/deliverables/3/download", artifact)
        self.response("POST", "/deliverables/3/review", {"review_status": "approved"})
        self.assertEqual(self.client.projects()["projects"][0]["id"], 1)
        self.assertEqual(self.client.create_project("Example", brief)["id"], 1)
        self.assertEqual(self.client.project("1")["brief_revision"], 1)
        self.assertEqual(self.client.update_brief("1", brief, 1)["brief_revision"], 2)
        self.assertEqual(self.client.jobs("1")["jobs"][0]["id"], 2)
        self.assertEqual(self.client.create_job("1", "website")["status"], "queued")
        self.assertEqual(self.client.run_job("2")["status"], "ready_for_review")
        self.assertEqual(self.client.job("2")["deliverables"][0]["sha256"], digest)
        receipt = self.client.download("3", str(self.output), digest)
        self.assertEqual(receipt["sha256"], digest)
        self.assertEqual(self.output.read_bytes(), artifact)
        self.assertEqual(self.client.review("3", "approved", digest)["review_status"], "approved")
        self.assertTrue(
            all(
                call[2] == "Bearer fixture-token-not-a-real-credential"
                for call in self.server.calls
            )
        )
        update = next(call for call in self.server.calls if call[0] == "PUT")
        self.assertEqual(update[3], {"brief": brief, "expected_revision": 1})
        review = self.server.calls[-1]
        self.assertEqual(review[3]["expected_sha256"], digest)
        self.assertNotIn("workspace_id", update[3])

    def test_geo_job_transmits_only_selected_relative_source_text(self):
        sources = {"index.html": "<title>Example</title>", "assets/style.css": "body{}"}
        self.client.create_job("1", "geo", source_files=sources)
        self.assertEqual(self.server.calls[0][3], {"kind": "geo", "source_files": sources})

    def test_numeric_ids_from_real_api_can_be_used_directly(self):
        self.client.project(1)
        self.client.create_job(1, "geo", source_job_id=2)
        self.assertEqual(self.server.calls[0][1], BASE + "/projects/1")
        self.assertEqual(self.server.calls[1][3]["source_job_id"], "2")

    def test_geo_source_job_and_explicit_sources_are_exclusive(self):
        with self.assertRaises(ClientError):
            self.client.create_job("1", "geo", "2", {"index.html": "test"})
        self.assertEqual(self.server.calls, [])

    def test_website_job_rejects_source_upload_before_network(self):
        with self.assertRaises(ClientError):
            self.client.create_job("1", "website", source_files={"index.html": "test"})
        self.assertEqual(self.server.calls, [])

    def test_source_path_boundary_rejects_private_and_traversal_inputs(self):
        for name in [
            "../index.html",
            "/index.html",
            ".git/private.html",
            ".env",
            "nested/.env.html",
            "C:\\index.html",
            "index.html/../index.html",
            "credentials.json",
            "x//index.html",
        ]:
            with self.subTest(name=name), self.assertRaises(ClientError):
                self.client.create_job("1", "geo", source_files={"safe.html": "test", name: "test"})
        self.assertEqual(self.server.calls, [])

    def test_source_limits_require_html_and_bound_count_text_and_bytes(self):
        invalid = [
            {"style.css": "body{}"},
            {f"{n}.html": "test" for n in range(31)},
            {"index.html": 3},
            {"index.html": "x" * (client_module.MAX_JSON_BYTES + 1)},
        ]
        for sources in invalid:
            with self.subTest(kind=type(sources)), self.assertRaises(ClientError):
                self.client.create_job("1", "geo", source_files=sources)
        self.assertEqual(self.server.calls, [])

    def test_identifier_paths_cannot_change_route_or_workspace(self):
        for value in ["../other", "project/other", "project?workspace=other", "", "x" * 97]:
            with self.subTest(value=value), self.assertRaises(ClientError):
                self.client.project(value)
        self.assertEqual(self.server.calls, [])

    def test_remote_http_and_embedded_credentials_are_rejected(self):
        for url in [
            "http://example.com",
            "https://user:secret@example.com",
            "https://example.com?secret=x",
            "https://example.com#token",
            "file:///tmp/socket",
            "https://example.com/../private",
            "https://example.com:bad",
        ]:
            with self.subTest(url=url), self.assertRaises(ClientError):
                Client(url, "fixture-token")

    def test_redirect_is_rejected_without_forwarding_bearer(self):
        self.response("GET", "/projects", b"", 302, {"Location": self.url + "/credential-sink"})
        with self.assertRaisesRegex(ClientError, "HTTP 302"):
            self.client.projects()
        self.assertEqual([call[1] for call in self.server.calls], [BASE + "/projects"])

    def test_error_redacts_token_and_mutation_is_not_retried(self):
        self.response(
            "POST", "/jobs/2/run", {"detail": "Denied fixture-token-not-a-real-credential"}, 403
        )
        with self.assertRaises(ClientError) as failure:
            self.client.run_job("2")
        self.assertNotIn("fixture-token-not-a-real-credential", str(failure.exception))
        self.assertIn("[redacted]", str(failure.exception))
        self.assertEqual(len(self.server.calls), 1)

    def test_stale_revision_and_digest_conflicts_are_exposed(self):
        self.response("PUT", "/projects/1/brief", {"detail": "Brief revision conflict"}, 409)
        self.response("POST", "/deliverables/3/review", {"detail": "Artifact digest conflict"}, 409)
        with self.assertRaisesRegex(ClientError, "HTTP 409: Brief revision conflict"):
            self.client.update_brief("1", {}, 1)
        with self.assertRaisesRegex(ClientError, "HTTP 409: Artifact digest conflict"):
            self.client.review("3", "approved", "a" * 64)
        self.assertEqual(len(self.server.calls), 2)

    def test_invalid_and_excessive_json_responses_are_rejected(self):
        for raw in [b"<html>error</html>", b"true", b"x" * (client_module.MAX_JSON_BYTES + 1)]:
            self.response("GET", "/projects", raw)
            with self.assertRaises(ClientError):
                self.client.projects()

    def test_download_digest_mismatch_writes_nothing(self):
        self.response("GET", "/deliverables/3/download", b"unexpected bytes")
        with self.assertRaisesRegex(ClientError, "does not match"):
            self.client.download("3", str(self.output), "a" * 64)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_download_respects_limit_and_never_leaves_partial_artifact(self):
        self.response("GET", "/deliverables/3/download", b"x" * 129)
        with mock.patch.object(client_module, "MAX_DOWNLOAD_BYTES", 128):
            with self.assertRaisesRegex(ClientError, "size limit"):
                self.client.download("3", str(self.output))
        self.assertFalse(self.output.exists())

    def test_download_does_not_overwrite_without_explicit_force(self):
        self.output.write_bytes(b"original")
        with self.assertRaisesRegex(ClientError, "already exists"):
            self.client.download("3", str(self.output))
        self.assertEqual(self.output.read_bytes(), b"original")
        self.assertEqual(self.server.calls, [])
        self.response("GET", "/deliverables/3/download", b"new")
        self.client.download("3", str(self.output), overwrite=True)
        self.assertEqual(self.output.read_bytes(), b"new")

    def test_download_rejects_symlink_output(self):
        existing = self.output.parent / "original.zip"
        existing.write_bytes(b"original")
        self.output.symlink_to(existing)
        with self.assertRaisesRegex(ClientError, "regular file"):
            self.client.download("3", str(self.output), overwrite=True)
        self.assertEqual(existing.read_bytes(), b"original")
        self.assertEqual(self.server.calls, [])

    def test_invalid_review_or_revision_never_mutates(self):
        for action in [
            lambda: self.client.review("3", "deploy", "a" * 64),
            lambda: self.client.review("3", "approved", "short"),
            lambda: self.client.update_brief("1", {}, True),
            lambda: self.client.update_brief("1", {}, 0),
        ]:
            with self.assertRaises(ClientError):
                action()
        self.assertEqual(self.server.calls, [])

    def test_cli_uses_stdin_json_and_keeps_failure_nonzero_without_token(self):
        with mock.patch.dict(
            "os.environ",
            {
                "TASKCHAD_API_URL": self.url,
                "TASKCHAD_API_TOKEN": "fixture-token-not-a-real-credential",
            },
        ):
            with mock.patch.object(
                sys, "stdin", io.StringIO('{"business_name":"Example $(never-run)"}')
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = client_module.main(
                        ["create-project", "--name", "Example", "--brief", "-"]
                    )
                self.assertEqual(code, 0)
                self.assertTrue(json.loads(output.getvalue())["ok"])
                self.assertEqual(
                    self.server.calls[-1][3]["brief"]["business_name"], "Example $(never-run)"
                )
            self.response("GET", "/projects", {"detail": "denied"}, 401)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = client_module.main(["projects"])
            self.assertEqual(code, 1)
            self.assertIn("HTTP 401", errors.getvalue())
            self.assertNotIn("fixture-token-not-a-real-credential", errors.getvalue())


class PackageTests(unittest.TestCase):
    def test_portable_manifest_has_no_invented_host_or_app_binding(self):
        manifest = json.loads((PLUGIN / "plugin.json").read_text())
        self.assertEqual(
            manifest["$schema"], "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        )
        self.assertEqual(manifest["name"], PLUGIN.name)
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertNotIn("apps", manifest)
        self.assertNotIn("skills", manifest)
        interface = manifest["extensions"]["com.openai"]["interface"]
        self.assertLessEqual(len(interface["shortDescription"]), 30)
        self.assertEqual(interface["displayName"], "TaskChad Build + Grow")
        self.assertLessEqual(len(interface["defaultPrompt"]), 3)
        self.assertFalse((PLUGIN / "mcp.json").exists())
        self.assertFalse((PLUGIN / ".app.json").exists())

    def test_repo_marketplace_resolves_package_from_repository_root(self):
        marketplace = json.loads((REPO / ".agents/plugins/marketplace.json").read_text())
        entry = next(entry for entry in marketplace["plugins"] if entry["name"] == PLUGIN.name)
        self.assertEqual((REPO / entry["source"]["path"]).resolve(), PLUGIN.resolve())
        self.assertEqual(entry["source"]["source"], "local")
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertIn("authentication", entry["policy"])

    def test_skill_names_triggers_and_relative_dependencies_are_present(self):
        expected = {"taskchad-build", "taskchad-geo", "taskchad-project"}
        self.assertEqual(
            {path.parent.name for path in (PLUGIN / "skills").glob("*/SKILL.md")}, expected
        )
        for name in expected:
            skill = (PLUGIN / "skills" / name / "SKILL.md").read_text()
            self.assertTrue(skill.startswith(f"---\nname: {name}\ndescription: "))
            self.assertIn("Use when", skill)
            self.assertIn("explicit instructions", skill)
            self.assertIn("../../scripts/taskchad_client.py", skill)
            self.assertIn("../../references/workflow.md", skill)
        cases = json.loads((PLUGIN / "fixtures/skill-cases.json").read_text())
        self.assertEqual({item["skill"] for item in cases if item["skill"]}, expected)
        self.assertTrue(any(item["skill"] is None for item in cases))


class ApiIntegrationTests(unittest.TestCase):
    def test_real_api_client_website_geo_and_review_with_caller_scope(self):
        """Exercise real router/store/builders over HTTP, not a response-shape mock."""
        try:
            import uvicorn
            from fastapi import FastAPI
            from fastapi.responses import JSONResponse
        except ImportError:
            self.skipTest("API integration needs the OS's FastAPI/uvicorn dependencies")
        scripts = str(REPO / ".claude/scripts")
        with mock.patch.object(sys, "path", [scripts, *sys.path]):
            from build_grow.api import create_router
            from build_grow.service import BuildGrowService
            from build_grow.store import BuildGrowStore
        with tempfile.TemporaryDirectory() as folder:
            service = BuildGrowService(BuildGrowStore(Path(folder) / "test.db"))
            app = FastAPI()

            @app.middleware("http")
            async def fixture_auth(request, call_next):
                # Test authentication fixtures; production binding belongs to OS middleware.
                scope = {"Bearer fixture-a": 1, "Bearer fixture-b": 2}.get(
                    request.headers.get("Authorization")
                )
                if scope is None:
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
                request.state.workspace_id = scope
                return await call_next(request)

            app.include_router(create_router(lambda: service))
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.started, "Disposable HTTP API did not start")
                client = Client(f"http://127.0.0.1:{port}", "fixture-a")
                other = Client(f"http://127.0.0.1:{port}", "fixture-b")
                brief = json.loads((PLUGIN / "fixtures/example-brief.json").read_text())
                project = client.create_project("Example", brief)
                job = client.create_job(project["id"], "website")
                site = client.run_job(job["id"])
                self.assertEqual(site["status"], "ready_for_review", site.get("error"))
                website_zip = next(
                    item for item in site["deliverables"] if item["filename"].endswith(".zip")
                )
                download = client.download(
                    website_zip["id"], str(Path(folder) / "website.zip"), website_zip["sha256"]
                )
                self.assertEqual(download["size_bytes"], website_zip["size_bytes"])
                geo = client.create_job(project["id"], "geo", source_job_id=site["id"])
                audit = client.run_job(geo["id"])
                self.assertEqual(audit["status"], "ready_for_review", audit.get("error"))
                artifact = audit["deliverables"][0]
                client.download(artifact["id"], str(Path(folder) / "audit.txt"), artifact["sha256"])
                review = client.review(
                    artifact["id"],
                    "changes_requested",
                    artifact["sha256"],
                    "Review actual business facts.",
                )
                self.assertEqual(review["review_status"], "changes_requested")
                self.assertEqual(other.projects()["projects"], [])
                with self.assertRaisesRegex(ClientError, "HTTP 404"):
                    other.project(project["id"])
                with self.assertRaisesRegex(ClientError, "HTTP 404"):
                    other.download(artifact["id"], str(Path(folder) / "other.txt"))
                with self.assertRaisesRegex(ClientError, "HTTP 404"):
                    other.review(artifact["id"], "approved", artifact["sha256"])
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                self.assertFalse(thread.is_alive(), "Disposable API did not stop")


if __name__ == "__main__":
    unittest.main()
