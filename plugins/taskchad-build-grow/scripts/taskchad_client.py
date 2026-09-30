#!/usr/bin/env python3
"""Bounded TaskChad Build + Grow HTTP client; Python standard library only."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath
from urllib import error, parse, request

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_BRIEF_BYTES = 256 * 1024
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
SHA_PATTERN = re.compile(r"[a-fA-F0-9]{64}\Z")
SOURCE_SUFFIXES = {".html", ".css", ".json", ".xml", ".txt"}


class ClientError(Exception):
    """A safe, user-facing connection or validation failure."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a caller's bearer token to a redirected destination.
        return None


def identifier(value: str | int) -> str:
    if type(value) is int and value > 0:
        value = str(value)
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ClientError("Invalid project, job, or deliverable identifier.")
    return value


def validate_source_files(files: dict) -> dict:
    if not isinstance(files, dict) or not 1 <= len(files) <= 30:
        raise ClientError("source_files must contain between 1 and 30 text files.")
    if not any(
        isinstance(name, str) and PurePosixPath(name).suffix.lower() == ".html"
        for name in files
    ):
        raise ClientError("Artifact GEO audits require at least one HTML file.")
    total = 0
    for filename, content in files.items():
        if not isinstance(filename, str) or not filename or len(filename) > 240:
            raise ClientError("Source filenames must be bounded relative paths.")
        path = PurePosixPath(filename)
        if (
            path.is_absolute()
            or "\\" in filename
            or ":" in filename
            or any(ord(char) < 32 for char in filename)
            or any(
                part in {"", ".", ".."} or part.startswith(".")
                for part in filename.split("/")
            )
            or path.suffix.lower() not in SOURCE_SUFFIXES
        ):
            raise ClientError(
                "Source paths must be relative website text files without hidden or parent paths."
            )
        if path.name.lower().startswith(("credentials", "id_rsa", "id_ed25519")):
            raise ClientError("Credential files cannot be used as audit sources.")
        if not isinstance(content, str):
            raise ClientError("Source file contents must be text.")
        total += len(filename.encode("utf-8")) + len(content.encode("utf-8"))
        if total > MAX_JSON_BYTES:
            raise ClientError("Source files exceed the 2 MiB limit.")
    return files


def read_json(path: str, max_bytes: int = MAX_JSON_BYTES):
    """Read explicit JSON input from a file or stdin, without shell interpolation."""
    try:
        if path == "-":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            raw = stream.read(max_bytes + 1)
        else:
            with open(path, "rb") as stream:
                raw = stream.read(max_bytes + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if len(raw) > max_bytes:
            raise ClientError("JSON input exceeds the size limit.")
        return json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ClientError("Could not read valid JSON input.") from exc


class TaskChadClient:
    """Use one operator-configured origin and one caller-bound bearer token."""

    def __init__(
        self, api_url: str | None = None, token: str | None = None, timeout: float = 30
    ):
        api_url = (
            api_url
            if api_url is not None
            else os.environ.get("TASKCHAD_API_URL", "http://127.0.0.1:4322")
        )
        token = token if token is not None else os.environ.get("TASKCHAD_API_TOKEN", "")
        try:
            parsed = parse.urlsplit(api_url)
            host = parsed.hostname
            _ = parsed.port
        except (TypeError, ValueError) as exc:
            raise ClientError("TASKCHAD_API_URL must be a valid API origin.") from exc
        if (
            not host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ClientError(
                "TASKCHAD_API_URL must not contain credentials, a query, or a fragment."
            )
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ClientError(
                "Use HTTPS for remote TaskChad APIs; HTTP is allowed only on loopback."
            )
        if any(ord(char) < 32 for char in api_url) or ".." in parsed.path.split("/"):
            raise ClientError("Invalid API URL path.")
        if (
            not isinstance(token, str)
            or not token
            or any(ord(char) < 32 for char in token)
        ):
            raise ClientError(
                "Set TASKCHAD_API_TOKEN to the caller's TaskChad API token."
            )
        if not 0 < timeout <= 120:
            raise ClientError("Timeout must be between 0 and 120 seconds.")
        self.base_url = api_url.rstrip("/") + "/api/build-grow"
        self._token = token
        self.timeout = timeout
        handlers = [_NoRedirect()]
        if loopback:
            handlers.append(request.ProxyHandler({}))
        self._opener = request.build_opener(*handlers)

    def _safe_error(self, raw: bytes, status: int) -> str:
        message = ""
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                detail = body.get("detail", body.get("error", ""))
                if isinstance(detail, str):
                    message = detail
                elif isinstance(detail, dict):
                    message = str(detail.get("message", ""))
        except (ValueError, UnicodeError):
            pass
        message = message.replace(self._token, "[redacted]")
        message = " ".join(message.split())[:400]
        return f"TaskChad API returned HTTP {status}" + (
            f": {message}" if message else "."
        )

    def _response(
        self, method: str, path: str, payload=None, max_bytes: int = MAX_JSON_BYTES
    ) -> bytes:
        data = None
        if payload is not None:
            try:
                data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ClientError("Request must contain valid JSON values.") from exc
            if len(data) > MAX_JSON_BYTES:
                raise ClientError("Request exceeds the 2 MiB limit.")
        req = request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/json, application/zip",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ClientError("API response exceeds the size limit.")
            return raw
        except error.HTTPError as exc:
            with exc:
                raw = exc.read(4096)
            raise ClientError(self._safe_error(raw, exc.code)) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            # Exceptions may contain configured URLs. Do not print them or the request.
            raise ClientError(
                "Could not reach TaskChad API. Check the configured address and server status."
            ) from exc

    def _json(self, method: str, path: str, payload=None):
        raw = self._response(method, path, payload)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ClientError(
                "TaskChad API returned an invalid JSON response."
            ) from exc
        if not isinstance(result, (dict, list)):
            raise ClientError("TaskChad API returned an unexpected JSON response.")
        return result

    def projects(self):
        return self._json("GET", "/projects")

    def create_project(self, name: str, brief: dict):
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise ClientError("Project name must contain between 1 and 160 characters.")
        self._brief(brief)
        return self._json("POST", "/projects", {"name": name, "brief": brief})

    @staticmethod
    def _brief(brief: dict):
        if not isinstance(brief, dict):
            raise ClientError("Business brief must be a JSON object.")

    def project(self, project_id: str):
        return self._json("GET", f"/projects/{identifier(project_id)}")

    def update_brief(self, project_id: str, brief: dict, expected_revision: int):
        self._brief(brief)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ClientError("expected_revision must be a positive integer.")
        return self._json(
            "PUT",
            f"/projects/{identifier(project_id)}/brief",
            {
                "brief": brief,
                "expected_revision": expected_revision,
            },
        )

    def jobs(self, project_id: str):
        return self._json("GET", f"/projects/{identifier(project_id)}/jobs")

    def create_job(
        self,
        project_id: str,
        kind: str,
        source_job_id: str | None = None,
        source_files: dict | None = None,
    ):
        if kind not in {"website", "geo"}:
            raise ClientError("Job kind must be website or geo.")
        if kind != "geo" and (source_job_id is not None or source_files is not None):
            raise ClientError("Only GEO jobs accept source files or a source job.")
        if source_job_id is not None and source_files is not None:
            raise ClientError("Choose source_job_id or source_files, not both.")
        payload = {"kind": kind}
        if source_job_id is not None:
            payload["source_job_id"] = identifier(source_job_id)
        if source_files is not None:
            payload["source_files"] = validate_source_files(source_files)
        return self._json("POST", f"/projects/{identifier(project_id)}/jobs", payload)

    def job(self, job_id: str):
        return self._json("GET", f"/jobs/{identifier(job_id)}")

    def run_job(self, job_id: str):
        # No retries: callers inspect current job state after uncertain network outcomes.
        return self._json("POST", f"/jobs/{identifier(job_id)}/run", {})

    def review(
        self, deliverable_id: str, decision: str, expected_sha256: str, note: str = ""
    ):
        if decision not in {"approved", "changes_requested"}:
            raise ClientError("Review decision must be approved or changes_requested.")
        if not isinstance(expected_sha256, str) or not SHA_PATTERN.fullmatch(
            expected_sha256
        ):
            raise ClientError("Review requires the deliverable's exact SHA-256 digest.")
        if not isinstance(note, str) or len(note) > 2000:
            raise ClientError("Review note must be at most 2000 characters.")
        return self._json(
            "POST",
            f"/deliverables/{identifier(deliverable_id)}/review",
            {
                "decision": decision,
                "expected_sha256": expected_sha256.lower(),
                "note": note,
            },
        )

    def download(
        self,
        deliverable_id: str,
        output: str,
        expected_sha256: str | None = None,
        overwrite: bool = False,
    ):
        """Write one bounded artifact to an explicit path, atomically after digest verification."""
        if expected_sha256 is not None and not SHA_PATTERN.fullmatch(expected_sha256):
            raise ClientError("Expected download digest must be a SHA-256 value.")
        destination = Path(output).expanduser().absolute()
        if destination.is_symlink() or destination.is_dir():
            raise ClientError("Download output must be a regular file path.")
        if destination.exists() and not overwrite:
            raise ClientError(
                "Download output already exists; choose another path or explicitly use --force."
            )
        if not destination.parent.is_dir():
            raise ClientError("Download output directory does not exist.")
        raw = self._response(
            "GET",
            f"/deliverables/{identifier(deliverable_id)}/download",
            max_bytes=MAX_DOWNLOAD_BYTES,
        )
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256.lower():
            raise ClientError(
                "Downloaded artifact does not match the expected SHA-256 digest."
            )
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".taskchad-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
            if overwrite:
                # Replace the directory entry; do not follow a symlink created
                # after the initial check.
                os.replace(temporary, destination)
            else:
                # Atomic no-clobber publication, even if a file appeared during the request.
                os.link(temporary, destination)
                temporary.unlink()
            temporary = None
        except OSError as exc:
            raise ClientError(
                "Could not write artifact to the requested output path."
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"output": str(destination), "sha256": digest, "size_bytes": len(raw)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TaskChad Build + Grow client. Credentials come from environment variables."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "projects", help="List projects in the authenticated workspace."
    )
    create = commands.add_parser(
        "create-project",
        help="Create a project from a business brief JSON file or stdin (-).",
    )
    create.add_argument("--name", required=True)
    create.add_argument("--brief", required=True)
    project = commands.add_parser(
        "project", help="Read a project and its current brief revision."
    )
    project.add_argument("project_id")
    update = commands.add_parser(
        "update-brief", help="Replace a brief using its current revision."
    )
    update.add_argument("project_id")
    update.add_argument("--brief", required=True)
    update.add_argument("--expected-revision", required=True, type=int)
    jobs = commands.add_parser("jobs", help="List a project's jobs.")
    jobs.add_argument("project_id")
    create_job = commands.add_parser(
        "create-job", help="Create a website draft or an artifact GEO audit."
    )
    create_job.add_argument("project_id")
    create_job.add_argument("--kind", required=True, choices=["website", "geo"])
    sources = create_job.add_mutually_exclusive_group()
    sources.add_argument("--source-job-id")
    sources.add_argument(
        "--source-files",
        help="JSON object mapping selected relative website paths to text.",
    )
    for name, help_text in [
        ("job", "Read job state and deliverable metadata."),
        ("run-job", "Run a queued local job once."),
    ]:
        subparser = commands.add_parser(name, help=help_text)
        subparser.add_argument("job_id")
    download = commands.add_parser(
        "download", help="Download an artifact to an explicit file path."
    )
    download.add_argument("deliverable_id")
    download.add_argument("--output", required=True)
    download.add_argument("--expected-sha256")
    download.add_argument(
        "--force", action="store_true", help="Explicitly replace the output file."
    )
    review = commands.add_parser(
        "review",
        help="Record a decision for a specific artifact digest; this does not deploy it.",
    )
    review.add_argument("deliverable_id")
    review.add_argument(
        "--decision", required=True, choices=["approved", "changes_requested"]
    )
    review.add_argument("--expected-sha256", required=True)
    review.add_argument("--note", default="")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = TaskChadClient()
        if args.command == "projects":
            result = client.projects()
        elif args.command == "create-project":
            result = client.create_project(
                args.name, read_json(args.brief, MAX_BRIEF_BYTES)
            )
        elif args.command == "project":
            result = client.project(args.project_id)
        elif args.command == "update-brief":
            result = client.update_brief(
                args.project_id,
                read_json(args.brief, MAX_BRIEF_BYTES),
                args.expected_revision,
            )
        elif args.command == "jobs":
            result = client.jobs(args.project_id)
        elif args.command == "create-job":
            files = (
                read_json(args.source_files) if args.source_files is not None else None
            )
            result = client.create_job(
                args.project_id, args.kind, args.source_job_id, files
            )
        elif args.command == "job":
            result = client.job(args.job_id)
        elif args.command == "run-job":
            result = client.run_job(args.job_id)
        elif args.command == "download":
            result = client.download(
                args.deliverable_id, args.output, args.expected_sha256, args.force
            )
        else:
            result = client.review(
                args.deliverable_id, args.decision, args.expected_sha256, args.note
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
