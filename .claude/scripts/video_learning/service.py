"""Orchestration service for Homie's `/watch` command."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .analyze import analyze_video, apply_approved_proposal, propose_application
from .extract import VideoSourceError, check_dependencies, extract_video
from .models import VideoLearningRequest, VideoLearningResult
from .store import VideoJobStore


class VideoLearningService:
    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        memory_dir: Path | None = None,
        workspace: Path | None = None,
    ) -> None:
        import config

        self.data_dir = Path(data_dir or config.DATA_DIR) / "video_learning"
        self.memory_dir = Path(memory_dir or config.MEMORY_DIR)
        self.workspace = Path(workspace or config.PROJECT_ROOT)
        self.artifacts_dir = self.data_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.store = VideoJobStore(self.data_dir)
        self._semaphore = asyncio.Semaphore(1)
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def create_job(self, request: VideoLearningRequest) -> dict[str, Any]:
        row = self.store.create({
            "source": request.source,
            "question": request.question,
            "detail": request.detail,
            "save_note": request.save_note,
            "conversation_context": request.conversation_context,
            "workspace": str(request.workspace or self.workspace),
            "origin": request.origin,
        })
        return row

    def start(self, job_id: str) -> asyncio.Task[VideoLearningResult]:
        task = asyncio.create_task(self.run(job_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(job_id, None))
        return task

    async def run(self, job_id: str) -> VideoLearningResult:
        async with self._semaphore:
            row = self.store.get(job_id)
            if row is None:
                return VideoLearningResult(False, job_id, "failed", error="Unknown video-learning job.")
            request = dict(row.get("request") or {})
            artifact_dir = self.artifacts_dir / job_id
            try:
                self._cancel_if_requested(job_id)
                self.store.update(job_id, status="extracting", stage_detail="Reading metadata and transcript")
                source_type = "local" if not request["source"].lower().startswith(("http://", "https://")) else "url"
                extraction = await extract_video(
                    request["source"], artifact_dir,
                    detail=request.get("detail") or "smart",
                    allow_local=source_type == "local",
                )

                self._cancel_if_requested(job_id)
                self.store.update(job_id, status="analyzing", stage_detail="Comparing lessons with current context")
                recalled = await self._recall_context(extraction.metadata.title, request.get("question", ""))
                workspace = Path(request.get("workspace") or self.workspace).resolve(strict=False)
                analysis = await analyze_video(
                    extraction,
                    question=request.get("question", ""),
                    conversation_context=request.get("conversation_context", ""),
                    recalled_context=recalled,
                    workspace=workspace,
                )

                self._cancel_if_requested(job_id)
                note_path = ""
                if request.get("save_note", True):
                    self.store.update(job_id, status="saving", stage_detail="Saving and indexing sourced learning note")
                    note = await asyncio.to_thread(self._save_note, job_id, extraction, analysis)
                    note_path = str(note)
                    await self._index_note(note)

                result_payload = {
                    "summary": analysis.markdown,
                    "note_path": note_path,
                    "source": extraction.metadata.webpage_url or extraction.metadata.source,
                    "title": extraction.metadata.title,
                    "provider": analysis.runtime.provider,
                    "model": analysis.runtime.model,
                    "runtime_lane": analysis.runtime.runtime_lane,
                    "cost_usd": analysis.runtime.cost_usd,
                    "warnings": extraction.warnings,
                    "transcript_source": extraction.transcript_source,
                    "frame_count": len(extraction.frame_paths),
                }
                self.store.update(job_id, status="ready", stage_detail="Ready", result=result_payload)
                await asyncio.to_thread(self.cleanup_old_artifacts)
                return VideoLearningResult(True, job_id, "ready", **{
                    key: value for key, value in result_payload.items()
                    if key in VideoLearningResult.__dataclass_fields__
                })
            except asyncio.CancelledError:
                self.store.update(job_id, status="cancelled", stage_detail="Cancelled")
                raise
            except Exception as exc:
                status = "cancelled" if str(exc) == "cancel_requested" else "failed"
                message = "Cancelled by operator." if status == "cancelled" else str(exc)
                self.store.update(job_id, status=status, stage_detail=message, error=message)
                return VideoLearningResult(False, job_id, status, error=message, source=request.get("source", ""))

    async def propose(self, job_id: str) -> dict[str, Any]:
        row = self.store.get(job_id)
        if row is None or row.get("status") not in {"ready", "applied"}:
            raise ValueError("That video job is not ready for application.")
        request = dict(row.get("request") or {})
        result = dict(row.get("result") or {})
        self.store.update(job_id, status="proposing", stage_detail="Drafting a bounded local application proposal")
        try:
            proposal, token, runtime = await propose_application(
                summary=result.get("summary", ""),
                conversation_context=request.get("conversation_context", ""),
                workspace=Path(request.get("workspace") or self.workspace),
            )
            application = {
                "proposal": proposal,
                "approval_token": token,
                "status": "awaiting_approval",
                "provider": runtime.provider,
                "model": runtime.model,
                "created_at": datetime.now(UTC).isoformat(),
            }
            self.store.update(job_id, status="ready", stage_detail="Application proposal awaiting approval", application=application)
            return application
        except Exception:
            self.store.update(job_id, status="ready", stage_detail="Video dossier ready; application proposal failed")
            raise

    async def apply(self, job_id: str, approval_token: str) -> dict[str, Any]:
        row = self.store.get(job_id)
        application = dict((row or {}).get("application") or {})
        if not application or application.get("status") != "awaiting_approval":
            raise ValueError("No exact application proposal is awaiting approval for that job.")
        if approval_token != application.get("approval_token"):
            raise ValueError("Approval token mismatch; regenerate or approve the exact current proposal.")
        request = dict(row.get("request") or {})
        self.store.update(job_id, status="applying", stage_detail="Applying the exact approved local proposal")
        try:
            runtime = await apply_approved_proposal(
                proposal=application["proposal"],
                approval_token=approval_token,
                workspace=Path(request.get("workspace") or self.workspace),
            )
            application.update({
                "status": "applied",
                "applied_at": datetime.now(UTC).isoformat(),
                "report": runtime.text.strip(),
                "provider": runtime.provider,
                "model": runtime.model,
            })
            self.store.update(job_id, status="applied", stage_detail="Approved proposal applied locally", application=application)
            return application
        except Exception as exc:
            application["status"] = "failed"
            application["error"] = str(exc)
            self.store.update(job_id, status="ready", stage_detail="Application failed; dossier remains ready", application=application)
            raise

    def status(self, job_id: str = "") -> dict[str, Any] | None:
        return self.store.get(job_id) if job_id else self.store.latest()

    def cancel(self, job_id: str) -> bool:
        ok = self.store.cancel(job_id)
        task = self._tasks.get(job_id)
        if ok and task and not task.done():
            task.cancel()
        return ok

    def retry(self, job_id: str) -> dict[str, Any] | None:
        return self.store.retry(job_id)

    def dependency_report(self) -> list[str]:
        return check_dependencies()

    async def _recall_context(self, title: str, question: str) -> str:
        try:
            import sys
            chat_dir = Path(__file__).resolve().parents[2] / "chat"
            if str(chat_dir) not in sys.path:
                sys.path.insert(0, str(chat_dir))
            from recall_service import SearchMode, recall

            response = await recall(
                f"{title} {question}".strip(),
                self.memory_dir,
                search_mode=SearchMode.HYBRID,
                caller="video_learning",
                max_results=6,
            )
            return response.formatted_text
        except Exception:
            return ""

    def _save_note(self, job_id: str, extraction: Any, analysis: Any) -> Path:
        now = datetime.now(UTC)
        slug = _slug(extraction.metadata.title)
        note_dir = self.memory_dir / "research" / "videos"
        note_dir.mkdir(parents=True, exist_ok=True)
        note_path = note_dir / f"{now:%Y-%m-%d}-{slug}.md"
        if note_path.exists():
            note_path = note_dir / f"{now:%Y-%m-%d}-{slug}-{job_id[:6]}.md"
        source = extraction.metadata.webpage_url or extraction.metadata.source
        frontmatter = {
            "type": "video-learning",
            "source": source,
            "source_type": extraction.metadata.source_type,
            "video_id": extraction.metadata.video_id,
            "title": extraction.metadata.title,
            "channel": extraction.metadata.channel,
            "transcript_source": extraction.transcript_source,
            "ingested_at": now.isoformat(),
            "job_id": job_id,
            "provider": analysis.runtime.provider,
            "model": analysis.runtime.model,
            "runtime_lane": analysis.runtime.runtime_lane,
            "frame_count": len(extraction.frame_paths),
        }
        yaml_lines = ["---"] + [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items()] + ["---", ""]
        body = "\n".join(yaml_lines) + analysis.markdown.strip() + "\n"
        note_path.write_text(body, encoding="utf-8")
        return note_path

    async def _index_note(self, note_path: Path) -> None:
        try:
            import sys
            chat_dir = Path(__file__).resolve().parents[2] / "chat"
            if str(chat_dir) not in sys.path:
                sys.path.insert(0, str(chat_dir))
            from recall_service import reindex_file

            await asyncio.to_thread(reindex_file, note_path, self.memory_dir, True)
        except Exception:
            # The Markdown note remains source-of-truth and can be indexed by
            # the next normal sync if the embedding/index service is offline.
            pass

    def _cancel_if_requested(self, job_id: str) -> None:
        row = self.store.get(job_id) or {}
        if row.get("cancel_requested"):
            raise RuntimeError("cancel_requested")

    def cleanup_old_artifacts(self, *, days: int = 7) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        removed = 0
        for path in self.artifacts_dir.iterdir() if self.artifacts_dir.exists() else []:
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                if modified < cutoff:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


_SERVICE: VideoLearningService | None = None


def get_video_learning_service() -> VideoLearningService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = VideoLearningService()
    return _SERVICE


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "video")[:80]
