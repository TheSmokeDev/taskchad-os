"""Persona-scoped controller for curriculum discovery, study, and review."""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from security import kill_switches
from video_learning.extract import extract_video

from .admission import (
    AdmissionBatchResult,
    AdmissionDecision,
    cognitive_admission_batch,
    deterministic_admission,
)
from .bundle import CurriculumBundle
from .config import CurriculumSettings, get_curriculum_settings
from .discovery import DiscoveryResult, discover_source
from .ledger import CurriculumLedger
from .paths import CurriculumPaths, resolve_curriculum_paths
from .study import (
    CurriculumSkimResult,
    CurriculumStudyResult,
    skim_extraction,
    study_extraction,
)

_CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
_SERVICE_CACHE: dict[str, CurriculumService] = {}
_CONFIDENCE_BY_GRADE = {
    "A": 0.95,
    "B": 0.85,
    "C": 0.70,
    "D": 0.85,
    "F": 0.95,
}


class CurriculumService:
    """One controller instance; all paths and settings resolve at call time."""

    def __init__(self, persona_id: str) -> None:
        from personas.lifecycle import validate_persona_name

        validate_persona_name(persona_id)
        self.persona_id = persona_id

    @property
    def settings(self) -> CurriculumSettings:
        return get_curriculum_settings(self.persona_id)

    @property
    def paths(self) -> CurriculumPaths:
        settings = self.settings
        return resolve_curriculum_paths(self.persona_id, settings.domain)

    def _ledger(self, *, create: bool) -> CurriculumLedger | None:
        path = self.paths.ledger_path
        if not create and not path.exists():
            return None
        return CurriculumLedger(path, self.persona_id)

    def status(self) -> dict[str, Any]:
        """Read status without creating a DB or invoking a model."""
        settings = self.settings
        paths = resolve_curriculum_paths(self.persona_id, settings.domain)
        ledger = self._ledger(create=False)
        counts = ledger.state_counts() if ledger else {}
        proposals = ledger.list_proposals(status="pending") if ledger else []
        return {
            "success": True,
            "persona_id": self.persona_id,
            "enabled": settings.enabled,
            "domain": settings.domain,
            "source_count": len(settings.sources),
            "schedule_hours": settings.schedule_hours,
            "kill_switch_disabled": kill_switches.is_disabled("persona_curriculum"),
            "ledger_exists": paths.ledger_path.exists(),
            "state_counts": counts,
            "pending_proposals": len(proposals),
            "studies_today": ledger.studies_today() if ledger else 0,
            "skims_today": ledger.skims_today() if ledger else 0,
            "bundle_exists": paths.bundle_root.exists(),
            "runtime": _empty_runtime(),
        }

    def sources(self) -> dict[str, Any]:
        settings = self.settings
        ledger = self._ledger(create=False)
        physical = {row["source_id"]: row for row in (ledger.list_sources() if ledger else [])}
        rows = []
        for source in settings.sources:
            row = asdict(source)
            state = physical.get(source.id, {})
            row.update(
                {
                    "channel_id": state.get("channel_id", ""),
                    "watermark": state.get("watermark", ""),
                    "last_polled_at": state.get("last_polled_at", ""),
                    "last_error": state.get("last_error", ""),
                }
            )
            rows.append(row)
        return {
            "success": True,
            "persona_id": self.persona_id,
            "enabled": settings.enabled,
            "sources": rows,
            "runtime": _empty_runtime(),
        }

    def enable(self) -> dict[str, Any]:
        from personas import services as persona_services

        kill_switches.requireEnabled("persona_mutation", caller="curriculum_enable")
        persona_services.set_persona_curriculum_enabled(self.persona_id, True)
        return self.status()

    def disable(self) -> dict[str, Any]:
        from personas import services as persona_services

        kill_switches.requireEnabled("persona_mutation", caller="curriculum_disable")
        persona_services.set_persona_curriculum_enabled(self.persona_id, False)
        return self.status()

    async def discover(
        self,
        *,
        full_inventory: bool = False,
        cognitive_admission: bool = True,
    ) -> dict[str, Any]:
        settings = self.settings
        if not settings.enabled:
            return self._skipped("curriculum disabled")
        kill_switches.requireEnabled("persona_curriculum", caller="curriculum_discover")
        ledger = self._ledger(create=True)
        assert ledger is not None
        discovered_count = 0
        source_results: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        policies: dict[str, str] = {}
        for source in settings.sources:
            ledger.upsert_source(
                source.id,
                kind=source.kind,
                url=source.url,
                policy=source.policy,
                metadata={"seed_url": source.seed_url} if source.seed_url else {},
            )
            policies[source.id] = source.policy
            try:
                result = await asyncio.to_thread(
                    discover_source,
                    source,
                    full_inventory=full_inventory,
                )
                new_rows = 0
                for video in result.videos:
                    if ledger.discover_video(video):
                        new_rows += 1
                        candidates.append(video)
                discovered_count += new_rows
                ledger.update_source_poll(
                    source.id,
                    channel_id=result.channel_id,
                    watermark=result.watermark,
                    error="",
                )
                source_results.append(_discovery_payload(result, new_rows=new_rows, error=""))
            except Exception as exc:
                ledger.update_source_poll(source.id, error=str(exc))
                source_results.append(
                    {
                        "source_id": source.id,
                        "new_videos": 0,
                        "method": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        curated_source_ids = tuple(
            source.id for source in settings.sources if source.policy == "curated"
        )
        active_curated = ledger.count_active_canon(curated_source_ids)
        decisions, admission_runtimes = await self._admit(
            candidates,
            policies=policies,
            settings=settings,
            cognitive=cognitive_admission,
            active_curated=active_curated,
        )
        for runtime in admission_runtimes:
            ledger.record_runtime_receipt("admission", runtime)
        for decision in decisions:
            ledger.set_admission(
                decision.video_id,
                decision=decision.decision,
                score=decision.score,
                topic=decision.topic,
                reason=decision.reason,
                method=decision.method,
            )
        return {
            "success": not any(row.get("error") for row in source_results),
            "persona_id": self.persona_id,
            "discovered": discovered_count,
            "admitted": sum(d.decision == "deep" for d in decisions),
            "skimmed": sum(d.decision == "skim" for d in decisions),
            "rejected": sum(d.decision == "reject" for d in decisions),
            "sources": source_results,
            "runtime": _aggregate_runtime_receipts(admission_runtimes),
        }

    async def run_once(
        self,
        *,
        full_inventory: bool = False,
        study_limit: int | None = None,
        cognitive_admission: bool = True,
    ) -> dict[str, Any]:
        settings = self.settings
        if not settings.enabled:
            return self._skipped("curriculum disabled")
        kill_switches.requireEnabled("persona_curriculum", caller="curriculum_run")
        discovery = await self.discover(
            full_inventory=full_inventory,
            cognitive_admission=cognitive_admission,
        )
        ledger = self._ledger(create=True)
        assert ledger is not None
        recovered_claims = ledger.recover_stale_claims()
        if study_limit == 0:
            return {
                "success": bool(discovery.get("success", False)),
                "persona_id": self.persona_id,
                "discovery_only": True,
                "discovery": discovery,
                "skims": [],
                "studies": [],
                "study_budget_remaining": 0,
                "recovered_claims": recovered_claims,
                "runtime": _empty_runtime(),
            }
        curated_sources = [source for source in settings.sources if source.policy == "curated"]
        is_backfill = any(
            ledger.count_videos(
                source_id=source.id,
                states=("studied",),
            )
            < settings.backfill_limit
            for source in curated_sources
        )
        daily_limit = (
            settings.daily_deep_studies if is_backfill else settings.steady_daily_deep_studies
        )
        remaining = max(0, daily_limit - ledger.studies_today())
        if study_limit is not None:
            remaining = min(remaining, max(0, study_limit))
        results: list[dict[str, Any]] = []
        skim_remaining = max(0, settings.daily_skims - ledger.skims_today())
        skim_results: list[dict[str, Any]] = []
        skim_candidates = [
            row
            for row in ledger.list_videos(states=("skimmed", "failed"), limit=10_000)
            if row["decision"] == "skim"
        ][:skim_remaining]
        for video in skim_candidates:
            skim_results.append(await self.skim_video(str(video["video_id"])))
        study_candidates = [
            row
            for row in ledger.list_videos(states=("admitted", "failed"), limit=10_000)
            if row["decision"] == "deep"
        ][:remaining]
        for video in study_candidates:
            results.append(await self.study_video(str(video["video_id"])))
        return {
            "success": bool(discovery.get("success", False))
            and all(result.get("success", False) for result in skim_results)
            and all(result.get("success", False) for result in results),
            "persona_id": self.persona_id,
            "discovery": discovery,
            "skims": skim_results,
            "studies": results,
            "study_budget_remaining": max(0, remaining - len(results)),
            "recovered_claims": recovered_claims,
            "runtime": _aggregate_runtime(
                [
                    {
                        "success": bool(discovery.get("success", False)),
                        "runtime": discovery.get("runtime"),
                    },
                    *skim_results,
                    *results,
                ]
            ),
        }

    async def skim_video(self, video_id: str) -> dict[str, Any]:
        settings = self.settings
        if not settings.enabled:
            return self._skipped("curriculum disabled")
        kill_switches.requireEnabled("persona_curriculum", caller="curriculum_skim")
        ledger = self._ledger(create=True)
        assert ledger is not None
        if not ledger.claim_skim(video_id):
            return {
                "success": False,
                "persona_id": self.persona_id,
                "video_id": video_id,
                "error": "video is not in a skimmable state",
                "runtime": _empty_runtime(),
            }
        video = ledger.get_video(video_id)
        assert video is not None
        paths = self.paths
        try:
            extraction = await extract_video(
                str(video["url"]),
                paths.confine_data(paths.artifacts_root / video_id),
                detail="transcript",
                allow_local=False,
                local_stt_only=True,
            )
            bundle = CurriculumBundle(paths, settings.domain)
            raw_path = bundle.write_raw(
                source_id=str(video["source_id"]),
                video_id=video_id,
                title=str(video["title"]),
                url=str(video["url"]),
                transcript_source=extraction.transcript_source,
                transcript=extraction.transcript,
            )
            skim = await skim_extraction(
                extraction,
                persona_id=self.persona_id,
                doctrine_index=self._doctrine_index(),
                workspace=paths.profile_root,
                model_tier=settings.admission_model_tier,
            )
            ledger.complete_skim(
                video_id,
                promote=skim.promote,
                score=skim.score,
                reason=skim.reason,
                method="cognitive-transcript-skim",
                transcript_source=extraction.transcript_source,
                raw_path=str(raw_path),
                provider=skim.provider,
                model=skim.model,
                runtime_lane=skim.runtime_lane,
                cost_usd=skim.cost_usd,
            )
            runtime = _skim_runtime(skim)
            ledger.record_runtime_receipt("skim", runtime, video_id=video_id)
            return {
                "success": True,
                "persona_id": self.persona_id,
                "video_id": video_id,
                "decision": "deep" if skim.promote else "reject",
                "reason": skim.reason,
                "runtime": runtime,
            }
        except Exception as exc:
            ledger.fail_video(video_id, f"{type(exc).__name__}: {exc}")
            return {
                "success": False,
                "persona_id": self.persona_id,
                "video_id": video_id,
                "error": f"{type(exc).__name__}: {exc}",
                "runtime": _empty_runtime(),
            }

    async def study_video(self, video_id: str) -> dict[str, Any]:
        settings = self.settings
        if not settings.enabled:
            return self._skipped("curriculum disabled")
        kill_switches.requireEnabled("persona_curriculum", caller="curriculum_study")
        ledger = self._ledger(create=True)
        assert ledger is not None
        if not ledger.claim_study(video_id):
            return {
                "success": False,
                "persona_id": self.persona_id,
                "video_id": video_id,
                "error": "video is not in an admissible study state",
                "runtime": _empty_runtime(),
            }
        video = ledger.get_video(video_id)
        if video is None:
            ledger.fail_video(video_id, "video disappeared after claim")
            return {
                "success": False,
                "video_id": video_id,
                "error": "video not found",
                "runtime": _empty_runtime(),
            }
        paths = self.paths
        try:
            artifact_dir = paths.confine_data(paths.artifacts_root / video_id)
            bundle = CurriculumBundle(paths, settings.domain)
            cached_raw_path: Path | None = None
            cached_transcript: str | None = None
            cached_transcript_source = str(video.get("transcript_source") or "")
            if str(video.get("raw_path") or "") and cached_transcript_source:
                cached_raw_path, cached_transcript = bundle.load_raw(
                    video=video,
                    raw_path=str(video["raw_path"]),
                    transcript_source=cached_transcript_source,
                )
            extraction = await extract_video(
                str(video["url"]),
                artifact_dir,
                detail="smart",
                allow_local=False,
                local_stt_only=True,
                transcript_override=cached_transcript,
                transcript_source_override=cached_transcript_source,
            )
            raw_path = cached_raw_path or bundle.write_raw(
                source_id=str(video["source_id"]),
                video_id=video_id,
                title=str(video["title"]),
                url=str(video["url"]),
                transcript_source=extraction.transcript_source,
                transcript=extraction.transcript,
            )
            doctrine = await self._recall_doctrine(video)
            study = await study_extraction(
                extraction,
                persona_id=self.persona_id,
                persona_context=self._persona_context(),
                recalled_doctrine=doctrine,
                workspace=paths.profile_root,
                study_model_tier=settings.study_model_tier,
            )
            dossier = bundle.write_source_dossier(
                video=video,
                transcript_source=extraction.transcript_source,
                analysis_markdown=study.markdown,
                provider=study.provider,
                model=study.model,
                runtime_lane=study.runtime_lane,
                raw_path=raw_path,
                raw_digest=hashlib.sha256(
                    extraction.transcript.strip().encode("utf-8")
                ).hexdigest(),
            )
            validation_errors = bundle.validate()
            if validation_errors:
                raise ValueError("OKF validation failed: " + "; ".join(validation_errors[:10]))
            self._reindex(bundle.recall_paths_for_video(video, dossier))
            proposal_ids = self._capture_proposals(ledger, video_id, study.markdown)
            ledger.complete_study(
                video_id,
                transcript_source=extraction.transcript_source,
                raw_path=str(raw_path),
                dossier_path=str(dossier),
                provider=study.provider,
                model=study.model,
                runtime_lane=study.runtime_lane,
                cost_usd=study.cost_usd,
            )
            runtime = _runtime(study)
            ledger.record_runtime_receipt("study", runtime, video_id=video_id)
            return {
                "success": True,
                "persona_id": self.persona_id,
                "video_id": video_id,
                "title": video["title"],
                "raw_path": str(raw_path),
                "dossier_path": str(dossier),
                "proposal_ids": proposal_ids,
                "runtime": runtime,
            }
        except Exception as exc:
            ledger.fail_video(video_id, f"{type(exc).__name__}: {exc}")
            return {
                "success": False,
                "persona_id": self.persona_id,
                "video_id": video_id,
                "error": f"{type(exc).__name__}: {exc}",
                "runtime": _empty_runtime(),
            }

    def review(self, *, status: str | None = "pending") -> dict[str, Any]:
        ledger = self._ledger(create=False)
        proposals = ledger.list_proposals(status=status, limit=100) if ledger else []
        return {
            "success": True,
            "persona_id": self.persona_id,
            "proposals": proposals,
            "runtime": _empty_runtime(),
        }

    def rebalance_curated(self, source_id: str) -> dict[str, Any]:
        """Re-apply the 120-item diversity contract to physical metadata rows."""
        settings = self.settings
        source = next(
            (
                candidate
                for candidate in settings.sources
                if candidate.id == source_id and candidate.policy == "curated"
            ),
            None,
        )
        if source is None:
            raise ValueError(f"Source is not configured as curated: {source_id}")
        ledger = self._ledger(create=False)
        if ledger is None:
            raise ValueError("Curriculum ledger does not exist.")
        rows = ledger.list_videos(source_id=source_id, limit=10_000)
        decisions = [
            AdmissionDecision(
                video_id=str(row["video_id"]),
                decision=str(row["decision"] or "reject"),
                score=float(row["score"]),
                topic=str(row["topic"] or "other"),
                reason=str(row["reason"]),
                method=str(row["decision_method"] or "rebalance"),
            )
            for row in rows
            if row["state"] in {"admitted", "skimmed", "rejected", "failed"}
        ]
        balanced = _curate_precomputed(decisions, total_limit=settings.backfill_limit)
        for decision in balanced:
            ledger.rebalance_admission(
                decision.video_id,
                decision=decision.decision,
                score=decision.score,
                topic=decision.topic,
                reason=decision.reason,
                method=decision.method,
            )
        return {
            "success": True,
            "persona_id": self.persona_id,
            "source_id": source_id,
            "canon": sum(decision.decision in {"deep", "skim"} for decision in balanced),
            "admitted": sum(decision.decision == "deep" for decision in balanced),
            "skimmed": sum(decision.decision == "skim" for decision in balanced),
            "rejected": sum(decision.decision == "reject" for decision in balanced),
            "runtime": _empty_runtime(),
        }

    def route(self, proposal_id: str, *, recipient: str = "operator") -> dict[str, Any]:
        """Operator-approved mailbox routing. This creates no executable task."""
        ledger = self._ledger(create=False)
        if ledger is None:
            raise ValueError("Curriculum ledger does not exist.")
        proposal = ledger.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError(f"Unknown proposal: {proposal_id}")
        if proposal["status"] != "pending":
            raise ValueError(f"Proposal is already {proposal['status']}.")

        import config
        from orchestration.db import OrchestrationDB
        from orchestration.mailbox_service import MailboxService
        from orchestration.models import SendMessageInput

        db = OrchestrationDB(config.ORCHESTRATION_DB_PATH)
        try:
            message = MailboxService(db).send_message(
                SendMessageInput(
                    from_agent=self.persona_id,
                    recipients=[recipient],
                    subject=f"Curriculum proposal: {proposal['title']}",
                    body=(
                        f"{proposal['body']}\n\n"
                        "Approval receipt only. No implementation or external "
                        "action has started."
                    ),
                    artifact_refs={
                        "proposal_id": proposal_id,
                        "persona_id": self.persona_id,
                        "video_id": proposal["video_id"],
                    },
                    dedupe_key=f"curriculum-proposal:{proposal_id}",
                    msg_type="curriculum_proposal",
                )
            )
        finally:
            db.close()
        if not ledger.route_proposal(proposal_id):
            raise RuntimeError("Proposal state changed before route commit.")
        return {
            "success": True,
            "persona_id": self.persona_id,
            "proposal_id": proposal_id,
            "status": "routed",
            "recipient": recipient,
            "mailbox_message_id": message.id,
            "work_started": False,
            "runtime": _empty_runtime(),
        }

    def grade(self, proposal_id: str, grade: str, *, note: str = "") -> dict[str, Any]:
        ledger = self._ledger(create=False)
        if ledger is None:
            raise ValueError("Curriculum ledger does not exist.")
        proposal = ledger.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError(f"Unknown proposal: {proposal_id}")
        normalized = grade.strip().upper()
        ledger.add_grade(proposal_id, normalized, note)
        candidate = self._stage_grade(proposal, normalized, note)
        return {
            "success": True,
            "persona_id": self.persona_id,
            "proposal_id": proposal_id,
            "grade": normalized,
            "staged": candidate is not None,
            "staging_path": str(self.paths.staging_path),
            "runtime": _empty_runtime(),
        }

    async def _admit(
        self,
        videos: list[dict[str, Any]],
        *,
        policies: dict[str, str],
        settings: CurriculumSettings,
        cognitive: bool,
        active_curated: int,
    ) -> tuple[list[AdmissionDecision], list[dict[str, Any]]]:
        if not videos:
            return [], []
        doctrine = self._doctrine_index()
        raw: list[AdmissionDecision] = []
        runtimes: list[dict[str, Any]] = []
        for start in range(0, len(videos), settings.metadata_batch_size):
            batch = videos[start : start + settings.metadata_batch_size]
            if cognitive:
                batch_result = await cognitive_admission_batch(
                    batch,
                    persona_id=self.persona_id,
                    doctrine_index=doctrine,
                    workspace=self.paths.profile_root,
                    model_tier=settings.admission_model_tier,
                )
                batch_decisions = batch_result.decisions
                runtimes.append(_admission_runtime(batch_result))
            else:
                batch_decisions = [deterministic_admission(row) for row in batch]
            raw.extend(batch_decisions)
        decision_by_id = {item.video_id: item for item in raw}
        curated_videos = [
            video for video in videos if policies.get(str(video["source_id"])) == "curated"
        ]
        if curated_videos:
            curated = _curate_precomputed(
                [decision_by_id[str(video["video_id"])] for video in curated_videos],
                total_limit=max(0, settings.backfill_limit - active_curated),
            )
            decision_by_id.update({item.video_id: item for item in curated})
        return [decision_by_id[str(video["video_id"])] for video in videos], runtimes

    async def _recall_doctrine(self, video: dict[str, Any]) -> str:
        if str(_CHAT_DIR) not in sys.path:
            sys.path.insert(0, str(_CHAT_DIR))
        try:
            from recall_service import SearchMode, recall

            response = await recall(
                (
                    f"{video.get('title', '')} {video.get('topic', '')} "
                    f"{self.settings.domain} doctrine"
                ),
                memory_dir=self.paths.memory_root,
                search_mode=SearchMode.KEYWORD,
                caller="curriculum_study",
                max_results=8,
            )
            return response.formatted_text
        except Exception:
            return self._doctrine_index()

    def _reindex(self, paths: tuple[Path, ...]) -> None:
        if str(_CHAT_DIR) not in sys.path:
            sys.path.insert(0, str(_CHAT_DIR))
        try:
            from recall_service import reindex_file

            for path in paths:
                reindex_file(path, self.paths.memory_root)
        except Exception as exc:
            raise RuntimeError(f"recall indexing failed: {exc}") from exc

    def _persona_context(self) -> str:
        sections = []
        for filename in ("SOUL.md", "SELF.md", "MEMORY.md"):
            path = self.paths.memory_root / filename
            if path.is_file():
                sections.append(
                    f"## {filename}\n" + path.read_text(encoding="utf-8", errors="replace")[:12_000]
                )
        return "\n\n".join(sections)

    def _doctrine_index(self) -> str:
        path = self.paths.bundle_root / "index.md"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:12_000]

    def _capture_proposals(
        self, ledger: CurriculumLedger, video_id: str, markdown: str
    ) -> list[str]:
        block = _section(markdown, "Application candidates")
        if not block or block.casefold().strip(" ._-") in {
            "none",
            "no application candidates",
            "zero",
        }:
            return []
        proposals = _split_proposals(block)
        return [
            ledger.add_proposal(
                video_id,
                title=title,
                body=body,
                target=_proposal_target(body),
            )
            for title, body in proposals[:5]
        ]

    def _stage_grade(self, proposal: dict[str, Any], grade: str, note: str) -> Any | None:
        if str(_CHAT_DIR) not in sys.path:
            sys.path.insert(0, str(_CHAT_DIR))
        from cognition.injection import is_injection_attempt
        from cognition.staging import StagingCandidate, StagingStore

        body = str(proposal["body"]).strip()
        if is_injection_attempt(body) or is_injection_attempt(note):
            raise ValueError("Grade evidence failed the persona injection gate.")
        digest = hashlib.sha256(
            f"{self.persona_id}\0{proposal['proposal_id']}\0{grade}".encode()
        ).hexdigest()[:24]
        observation = (
            f"Curriculum proposal '{proposal['title']}' was graded {grade} by the operator."
        )
        if note.strip():
            observation += f" Outcome note: {note.strip()[:800]}"
        candidate = StagingCandidate(
            source_turn=(f"reflection:curriculum:{self.persona_id}:{proposal['proposal_id']}"),
            source="reflection",
            candidate_type="procedural",
            observation=observation,
            inference="",
            confidence=_CONFIDENCE_BY_GRADE[grade],
            evidence_count=1,
            dedupe_key=f"curriculum-grade:{grade}:{digest}",
            promotion_target="MEMORY.md",
        )
        StagingStore(self.paths.staging_path).append(candidate)
        return candidate

    def _skipped(self, reason: str) -> dict[str, Any]:
        return {
            "success": True,
            "persona_id": self.persona_id,
            "skipped": True,
            "reason": reason,
            "runtime": _empty_runtime(),
        }


def get_curriculum_service(persona_id: str) -> CurriculumService:
    service = _SERVICE_CACHE.get(persona_id)
    if service is None:
        service = CurriculumService(persona_id)
        _SERVICE_CACHE[persona_id] = service
    return service


def _curate_precomputed(
    decisions: list[AdmissionDecision], *, total_limit: int
) -> list[AdmissionDecision]:
    per_topic_limit = max(1, total_limit // 6)
    selected_ids: set[str] = set()
    topic_counts: dict[str, int] = {}
    eligible = sorted(
        (decision for decision in decisions if decision.decision in {"deep", "skim"}),
        key=lambda item: (
            0 if item.decision == "deep" else 1,
            -item.score,
            item.video_id,
        ),
    )
    for decision in eligible:
        if len(selected_ids) >= total_limit:
            break
        count = topic_counts.get(decision.topic, 0)
        if count >= per_topic_limit:
            continue
        selected_ids.add(decision.video_id)
        topic_counts[decision.topic] = count + 1
    # If the catalog is topic-skewed, fill the remaining canon by global score
    # instead of silently returning fewer than the locked total.
    for decision in eligible:
        if len(selected_ids) >= total_limit:
            break
        selected_ids.add(decision.video_id)
    output: list[AdmissionDecision] = []
    for decision in decisions:
        if decision.video_id in selected_ids or decision.decision == "reject":
            output.append(decision)
        else:
            output.append(
                AdmissionDecision(
                    decision.video_id,
                    "reject",
                    decision.score,
                    decision.topic,
                    "outside the curated metadata canon and daily study budget",
                    decision.method,
                )
            )
    return output


def _section(markdown: str, title: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(title)}\s*\n(.*?)(?=^##\s+|\Z)",
        markdown,
        flags=re.I | re.M | re.S,
    )
    return match.group(1).strip() if match else ""


def _split_proposals(block: str) -> list[tuple[str, str]]:
    chunks = re.split(r"(?=^(?:###\s+|[-*]\s+\*\*))", block, flags=re.M)
    output: list[tuple[str, str]] = []
    for chunk in chunks:
        body = chunk.strip()
        if not body:
            continue
        first = body.splitlines()[0]
        title = re.sub(r"^(?:###\s+|[-*]\s+\*\*?)|\*+$", "", first).strip()
        output.append((title[:160] or "Curriculum application", body[:12_000]))
    return output or [("Curriculum application", block[:12_000])]


def _proposal_target(body: str) -> str:
    match = re.search(r"\btarget\s*:\s*([^\n]+)", body, flags=re.I)
    return match.group(1).strip()[:160] if match else ""


def _discovery_payload(result: DiscoveryResult, *, new_rows: int, error: str) -> dict[str, Any]:
    return {
        "source_id": result.source_id,
        "channel_id": result.channel_id,
        "inventory_count": len(result.videos),
        "new_videos": new_rows,
        "method": result.method,
        "watermark": result.watermark,
        "error": error,
    }


def _empty_runtime() -> dict[str, Any]:
    return {
        "success": True,
        "error": "",
        "session_id": "",
        "lane": "",
        "provider": "",
        "model": "",
        "cost_usd": None,
        "tool_calls": 0,
        "execution_time_ms": None,
        "calls": [],
    }


def _admission_runtime(batch: AdmissionBatchResult) -> dict[str, Any]:
    result = batch.runtime
    return {
        **_empty_runtime(),
        "success": not bool(batch.fallback_error),
        "error": batch.fallback_error,
        "session_id": str(result.session_id or "") if result else "",
        "lane": result.runtime_lane if result else "",
        "provider": result.provider if result else "",
        "model": result.model if result else "",
        "cost_usd": result.cost_usd if result else None,
        "tool_calls": result.tool_call_count if result else 0,
        "execution_time_ms": batch.execution_time_ms,
        "calls": [_runtime_result_call(result, batch.execution_time_ms)] if result else [],
    }


def _runtime_result_call(result: Any, execution_time_ms: int) -> dict[str, Any]:
    return {
        "session_id": str(getattr(result, "session_id", "") or ""),
        "lane": str(getattr(result, "runtime_lane", "") or ""),
        "provider": str(getattr(result, "provider", "") or ""),
        "model": str(getattr(result, "model", "") or ""),
        "cost_usd": getattr(result, "cost_usd", None),
        "tool_calls": int(getattr(result, "tool_call_count", 0) or 0),
        "execution_time_ms": execution_time_ms,
    }


def _runtime(study: CurriculumStudyResult) -> dict[str, Any]:
    return {
        **_empty_runtime(),
        "session_id": study.session_id,
        "lane": study.runtime_lane,
        "provider": study.provider,
        "model": study.model,
        "cost_usd": study.cost_usd,
        "tool_calls": study.tool_call_count,
        "execution_time_ms": study.execution_time_ms,
        "calls": list(study.calls),
    }


def _skim_runtime(skim: CurriculumSkimResult) -> dict[str, Any]:
    return {
        **_empty_runtime(),
        "session_id": skim.session_id,
        "lane": skim.runtime_lane,
        "provider": skim.provider,
        "model": skim.model,
        "cost_usd": skim.cost_usd,
        "tool_calls": skim.tool_call_count,
        "execution_time_ms": skim.execution_time_ms,
        "calls": list(skim.calls),
    }


def _aggregate_runtime_receipts(
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    if not receipts:
        return _empty_runtime()
    runtime = _empty_runtime()
    last = receipts[-1]
    runtime.update(
        {
            "success": all(bool(row.get("success", False)) for row in receipts),
            "error": "; ".join(str(row.get("error") or "") for row in receipts if row.get("error"))[
                :4000
            ],
            "session_id": str(last.get("session_id") or ""),
            "lane": str(last.get("lane") or ""),
            "provider": str(last.get("provider") or ""),
            "model": str(last.get("model") or ""),
            "cost_usd": sum(
                float(row["cost_usd"]) for row in receipts if row.get("cost_usd") is not None
            )
            or None,
            "tool_calls": sum(int(row.get("tool_calls") or 0) for row in receipts),
            "execution_time_ms": sum(int(row.get("execution_time_ms") or 0) for row in receipts),
            "calls": [
                call
                for row in receipts
                for call in list(row.get("calls") or [])
                if isinstance(call, dict)
            ],
        }
    )
    return runtime


def _aggregate_runtime(results: list[dict[str, Any]]) -> dict[str, Any]:
    receipts = [result["runtime"] for result in results if isinstance(result.get("runtime"), dict)]
    return _aggregate_runtime_receipts(receipts)
