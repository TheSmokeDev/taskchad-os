"""Persistent, restart-safe job state for video learning."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"ready", "failed", "cancelled", "applied"}
ACTIVE_STATES = {"queued", "extracting", "analyzing", "saving", "proposing", "applying"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class VideoJobStore:
    """One JSON manifest per job; writes are atomic on the local filesystem."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.jobs_dir = self.root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._mark_interrupted()

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now = utc_now()
        row = {
            "job_id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "request": request,
            "stage_detail": "Waiting for the video-learning worker",
            "cancel_requested": False,
            "attempt": 1,
            "owner_pid": os.getpid(),
            "result": {},
            "application": {},
        }
        self._write(row)
        return row

    def get(self, job_id: str) -> dict[str, Any] | None:
        if not job_id or not job_id.replace("-", "").isalnum():
            return None
        path = self.jobs_dir / f"{job_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def latest(self) -> dict[str, Any] | None:
        rows = [row for path in self.jobs_dir.glob("*.json") if (row := self.get(path.stem))]
        return max(rows, key=lambda row: row.get("created_at", ""), default=None)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        row = self.get(job_id)
        if row is None:
            raise KeyError(job_id)
        row.update(changes)
        row["updated_at"] = utc_now()
        self._write(row)
        return row

    def cancel(self, job_id: str) -> bool:
        row = self.get(job_id)
        if row is None or row.get("status") in TERMINAL_STATES:
            return False
        self.update(job_id, cancel_requested=True, stage_detail="Cancellation requested")
        return True

    def retry(self, job_id: str) -> dict[str, Any] | None:
        old = self.get(job_id)
        if old is None or old.get("status") not in {"failed", "cancelled", "interrupted"}:
            return None
        request = dict(old.get("request") or {})
        row = self.create(request)
        row["attempt"] = int(old.get("attempt") or 1) + 1
        row["retried_from"] = job_id
        self._write(row)
        return row

    def _write(self, row: dict[str, Any]) -> None:
        path = self.jobs_dir / f"{row['job_id']}.json"
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=self.jobs_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(row, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp_name, path)
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def _mark_interrupted(self) -> None:
        for path in self.jobs_dir.glob("*.json"):
            row = self.get(path.stem)
            owner_pid = int((row or {}).get("owner_pid") or 0)
            if row and row.get("status") in ACTIVE_STATES and not _pid_alive(owner_pid):
                row["status"] = "interrupted"
                row["stage_detail"] = "The Homie restarted while this job was running; retry is safe."
                row["updated_at"] = utc_now()
                self._write(row)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False
    return True
