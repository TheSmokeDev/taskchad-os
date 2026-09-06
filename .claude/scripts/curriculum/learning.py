"""Curriculum adapter: sourced study evidence and conditional application candidates."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class StudyLearningPrompt(str):
    def __new__(cls, text: str, service, experience_id: str, context, attempt_key: str):
        instance = super().__new__(cls, text)
        instance.service = service
        instance.experience_id = experience_id
        instance.context = context
        instance.attempt_key = attempt_key
        return instance


def prepare_study(
    persona_id: str,
    video: dict,
    transcript_digest: str,
    *,
    service=None,
    transcript: str = "",
    transcript_source: str = "",
    source_timestamp: str = "",
) -> dict:
    try:
        if service is None:
            from personas.learning.service import get_learning_service

            service = get_learning_service(persona_id)
        if not service.enabled():
            return {"disabled": True}
        transcript = transcript.strip()
        if (
            transcript
            and hashlib.sha256(transcript.encode("utf-8")).hexdigest() != transcript_digest
        ):
            raise ValueError("Curriculum source transcript digest mismatch")
        origin = f"curriculum:{video['video_id']}:{transcript_digest}"
        experience = service.capture_experience(
            origin,
            "curriculum",
            str(video.get("title") or video["video_id"]),
            mode="study",
            metadata={
                "video_id": video["video_id"],
                "source_url": video["url"],
                "transcript_digest": transcript_digest,
            },
        )
        return {
            "service": service,
            "experience": experience,
            "transcript": transcript,
            "transcript_source": transcript_source,
            "source_timestamp": source_timestamp,
            "source_collected_at": experience["created_at"],
        }
    except Exception as exc:
        logger.warning("curriculum learning capture failed: %s", type(exc).__name__)
        return {"error_type": type(exc).__name__}


def render_study_context(persona_id: str, prompt: str, *, model: str | None, service=None) -> str:
    # prepare_study at the service seam owns the study experience. This receipt
    # separately records the exact synthesis prompt, not all transcript chunks.
    try:
        if service is None:
            from personas.learning.service import get_learning_service

            service = get_learning_service(persona_id)
        if not service.enabled():
            return prompt
        from personas.learning.observers import evidence_hash

        context = service.render_context(prompt, max_chars=2000, model=model)
        rendered = prompt + (
            "\n\nRelevant learned methods:\n" + context.text if context.text else ""
        )
        exp = service.capture_experience(
            f"curriculum:synthesis:{evidence_hash(prompt)}",
            "curriculum_synthesis",
            "Synthesize studied source",
            mode="study",
            metadata={"learning_role": "context_attribution"},
        )
        service.record_context_receipt(
            exp["id"],
            context,
            rendered,
            attempt_key=f"{evidence_hash(rendered)}:prepared",
            phase="prepared",
        )
        return StudyLearningPrompt(rendered, service, exp["id"], context, evidence_hash(rendered))
    except Exception as exc:
        logger.warning("curriculum learning context failed: %s", type(exc).__name__)
        return prompt


def record_study_context(prompt: str, *, result=None) -> dict:
    if not isinstance(prompt, StudyLearningPrompt):
        return {}
    if result is not None and getattr(result, "success", None) is False:
        return {}
    try:
        phase = "submitted" if result is None else "executed"
        context = prompt.service.record_context_receipt(
            prompt.experience_id,
            prompt.context,
            prompt,
            attempt_key=f"{prompt.attempt_key}:{phase}",
            phase=phase,
            provider=getattr(result, "provider", None),
            model=getattr(result, "model", None),
        )
        if result is None:
            return {}
        execution = prompt.service.record_execution(
            prompt.experience_id,
            {
                "stage": "study_synthesis",
                "success": True,
                "model_call_count": 1,
                "result_hash": hashlib.sha256(str(result.text).strip().encode("utf-8")).hexdigest(),
                "context_receipt_id": context["id"],
                "included_activation_ids": [item["activation_id"] for item in context["included"]],
                **{
                    key: getattr(result, key, None)
                    for key in ("provider", "model", "runtime_lane", "session_id", "cost_usd")
                },
            },
            attempt_key=f"{prompt.attempt_key}:synthesis",
        )
        return {
            "parent_experience_id": prompt.experience_id,
            "parent_context_receipt_id": context["id"],
            "parent_execution_id": execution["id"],
        }
    except Exception as exc:
        logger.warning("curriculum learning context receipt failed: %s", type(exc).__name__)
        return {}


def _inherit_study_context(service, experience_id: str, study) -> dict:
    """Copy an executed stored receipt, without reconstructing or rerendering the prompt."""
    linkage = getattr(study, "learning_receipt", None) or {}
    if not linkage:
        return {}
    context = service.get_record(linkage.get("parent_context_receipt_id", "")) or {}
    execution = service.get_record(linkage.get("parent_execution_id", "")) or {}
    expected_hash = hashlib.sha256(study.markdown.strip().encode("utf-8")).hexdigest()
    if (
        context.get("kind") != "context"
        or context.get("phase") != "executed"
        or execution.get("kind") != "execution"
        or execution.get("stage") != "study_synthesis"
        or execution.get("context_receipt_id") != context.get("id")
        or execution.get("experience_id") != context.get("experience_id")
        or execution.get("experience_id") != linkage.get("parent_experience_id")
        or execution.get("result_hash") != expected_hash
        or any(execution.get(key) != getattr(study, key, None) for key in ("model", "provider"))
    ):
        raise ValueError("Study attribution does not match an executed source synthesis")
    inherited = service.store.put(
        "context",
        {
            **{
                key: context[key]
                for key in (
                    "context_hash",
                    "rendered_prompt_hash",
                    "model",
                    "provider",
                    "phase",
                    "included",
                    "dropped",
                    "status",
                )
            },
            "experience_id": experience_id,
            "attempt_key": f"source-synthesis:{context['id']}",
            "attribution": "parent_inference",
            "parent_context_receipt_id": context["id"],
        },
        key=f"{experience_id}:source-synthesis:{context['id']}",
    )
    return {
        **linkage,
        "attribution": "parent_inference",
        "context_receipt_id": inherited["id"],
        "included_activation_ids": [item["activation_id"] for item in inherited["included"]],
        **{key: execution.get(key) for key in ("provider", "model", "runtime_lane", "session_id")},
    }


def complete_study(
    prepared: dict,
    *,
    video: dict,
    transcript_digest: str,
    dossier_path: str,
    study: Any,
    proposals: list[dict],
    dossier_text: str | None = None,
) -> dict:
    if "experience" not in prepared:
        return {k: v for k, v in prepared.items() if k != "service"}
    service, exp = prepared["service"], prepared["experience"]
    try:
        from personas.learning.observers import evidence_hash, study_observation

        artifact_key = evidence_hash(study.markdown)
        runtime = {
            name: getattr(study, name, None)
            for name in ("provider", "model", "runtime_lane", "session_id")
        }
        inherited = _inherit_study_context(service, exp["id"], study)
        execution = service.record_execution(
            exp["id"],
            {
                "stage": "dossier_written",
                "dossier_path": dossier_path,
                "artifact_hash": artifact_key,
                "runtime": runtime,
                **inherited,
                "model_call_count": 0,
                # This summary includes transcript extraction calls. Individual
                # synthesis inference/cost belongs only to its parent receipt.
                "reported_study_cost_usd": getattr(study, "cost_usd", None),
            },
            attempt_key=f"study:{artifact_key}",
        )
        transcript = prepared.get("transcript", "")
        if (
            transcript
            and hashlib.sha256(transcript.encode("utf-8")).hexdigest() != transcript_digest
        ):
            raise ValueError("Curriculum completion transcript digest mismatch")
        source_query = (
            study.markdown
            + "\n"
            + "\n".join(str(proposal.get("body") or "") for proposal in proposals)
        )
        source_excerpts = _source_excerpts(
            transcript,
            query=source_query,
            budget=16000,
            source_kind="transcript",
            source_url=video["url"],
            source_timestamp=prepared.get("source_timestamp", ""),
            collected_at=prepared.get("source_collected_at", ""),
            provenance=prepared.get("transcript_source", ""),
        )
        dossier_source = dossier_text if dossier_text is not None else study.markdown
        dossier_excerpts = _source_excerpts(
            dossier_source,
            query=source_query,
            budget=6000,
            source_kind="generated_dossier",
            source_url=video["url"],
            source_timestamp="",
            collected_at=execution["created_at"],
            provenance="model_synthesis_not_independent_source_evidence",
        )
        evidence = study_observation(
            source_id=video["video_id"],
            source_url=video["url"],
            transcript_digest=transcript_digest,
            dossier_path=dossier_path,
            validation_errors=[],
        )
        evidence.update(
            source_excerpts=source_excerpts,
            dossier_excerpts=dossier_excerpts,
            source_capture={
                "status": "captured" if transcript else "unavailable",
                "source_char_count": len(transcript),
                "captured_char_count": sum(len(item["text"]) for item in source_excerpts),
                "complete": bool(transcript)
                and sum(len(item["text"]) for item in source_excerpts) == len(transcript),
                "selection": "literal_timestamp_and_keyword_windows",
            },
        )
        observation = service.record_observation(
            exp["id"],
            {
                "status": "resolved",
                "quality": "direct",
                "evidence": evidence,
            },
            source_key=f"study:{artifact_key}:validation",
        )
        candidates = []
        for proposal in proposals:
            content = str(proposal.get("body") or "").strip()
            if not content:
                continue
            candidate = service.propose_candidate(
                {
                    "candidate_type": "procedure",
                    "changes_behavior": True,
                    "title": str(proposal.get("title") or "Study application"),
                    "content": content,
                    "applicability": (
                        "Only the domain circumstances supported by the source; qualification "
                        "must establish applicability and counterexamples."
                    ),
                    "evidence_ids": [observation["id"]],
                    "counterevidence_ids": [],
                    "uncertainty": (
                        "Source-derived hypothesis; application and source-claim truth "
                        "are not verified by dossier validation."
                    ),
                    "domain": "curriculum",
                    "target_file": "MEMORY.md",
                },
                source_key=f"curriculum:{video['video_id']}:{evidence_hash(content)}",
            )
            candidates.append(candidate["id"])
        return {
            "status": "recorded",
            "experience_id": exp["id"],
            "observation_id": observation["id"],
            "candidate_ids": candidates,
        }
    except Exception as exc:
        logger.warning("curriculum learning completion failed: %s", type(exc).__name__)
        return {"status": "error", "experience_id": exp["id"], "error_type": type(exc).__name__}


def _source_excerpts(
    text: str,
    *,
    query: str,
    budget: int,
    source_kind: str,
    source_url: str,
    source_timestamp: str,
    collected_at: str,
    provenance: str,
) -> list[dict]:
    """Choose bounded literal source windows; never ask a model to supply quotes."""
    if not text:
        return []
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    terms = set(re.findall(r"\b[a-z][a-z0-9]{4,}\b", query.casefold()))
    timestamps = set(re.findall(r"\b\d{2}:\d{2}:\d{2}\b", query))
    windows = []
    for start in range(0, len(text), 2000):
        body = text[start : start + 2000]
        words = set(re.findall(r"\b[a-z][a-z0-9]{4,}\b", body.casefold()))
        # Exact cited source timestamps outrank lexical overlap. Query text may
        # suggest where to look but only these untouched source bytes are saved.
        score = 100 * sum(stamp in body for stamp in timestamps) + len(terms & words)
        windows.append((score, start, min(start + 2000, len(text))))
    chosen = []
    remaining = budget
    for _, start, end in sorted(windows, key=lambda row: (-row[0], row[1])):
        if remaining <= 0:
            break
        end = min(end, start + remaining)
        chosen.append((start, end))
        remaining -= end - start
    return [
        {
            "source_kind": source_kind,
            "source_url": source_url,
            "source_timestamp": source_timestamp or None,
            "collected_at": collected_at,
            "provenance": provenance,
            "source_sha256": source_hash,
            "excerpt_sha256": hashlib.sha256(text[start:end].encode("utf-8")).hexdigest(),
            "start_char": start,
            "end_char": end,
            "source_timestamps": re.findall(r"\[\d{2}:\d{2}:\d{2}\]", text[start:end]),
            "text": text[start:end],
        }
        for start, end in sorted(chosen)
    ]
