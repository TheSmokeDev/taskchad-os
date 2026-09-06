"""Evaluated automatic adoption through the existing amendment/skill ledgers.

This is the only learning mutation authority. A candidate's confidence or status
does not authorize a write; the exact persisted receipt and its evidence do.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from personas.learning.evaluation import (
    EVALUATOR_VERSION,
    EvaluationReceipt,
    _evidence_snapshot,
    canonical_hash,
    validate_evaluation_context_binding,
)
from personas.learning.models import LearningError


def _bound_receipt(
    service: Any, candidate_id: str, evaluation_id: str, *, allow_retired: bool = False
) -> tuple[dict, dict]:
    candidate = service.get_record(candidate_id)
    evaluation = service.get_record(evaluation_id)
    if not candidate or candidate.get("kind") != "candidate":
        raise LearningError("candidate_not_found")
    if not allow_retired and candidate.get("status") in {"retired", "superseded"}:
        raise LearningError("candidate_retired")
    if not evaluation or evaluation.get("kind") != "evaluation":
        raise LearningError("evaluation_not_found")
    if (
        evaluation.get("candidate_id") != candidate_id
        or evaluation.get("candidate_hash") != candidate["content_hash"]
    ):
        raise LearningError("evaluation_candidate_mismatch")
    body = evaluation.get("receipt")
    if not isinstance(body, dict) or canonical_hash(body) != evaluation.get("receipt_hash"):
        raise LearningError("evaluation_receipt_integrity_failed")
    receipt = EvaluationReceipt(**body)
    if (
        receipt.profile_id != service.target.persona_id
        or receipt.candidate_hash != candidate["content_hash"]
        or receipt.evaluator_version != EVALUATOR_VERSION
        or receipt.passed is not True
        or evaluation.get("passed") is not True
    ):
        raise LearningError("evaluation_authority_invalid")
    behavioral = (
        candidate["candidate_type"] == "procedure"
        or candidate["changes_behavior"]
        or receipt.support.get("changes_behavior") is not False
    )
    if behavioral:
        if (
            receipt.mode != "qualification"
            or receipt.reason != "qualified_provisional"
            or not receipt.manifest_hash
            or not receipt.comparisons
            or not receipt.model
            or receipt.candidate_score is None
            or receipt.baseline_score is None
            or receipt.candidate_score <= receipt.baseline_score
        ):
            raise LearningError("behavior_requires_qualification")
        validate_evaluation_context_binding(service, candidate, receipt)
    elif receipt.mode not in {"knowledge_support", "qualification"}:
        raise LearningError("unsupported_evaluation_mode")
    if (
        receipt.support.get("supported") is not True
        or receipt.support.get("contradictions_addressed") is not True
    ):
        raise LearningError("candidate_evidence_unsupported")
    records = service.evidence_records(list(receipt.evidence_hashes))
    current = {record["id"]: canonical_hash(_evidence_snapshot(record)) for record in records}
    if current != receipt.evidence_hashes:
        raise LearningError("evaluation_evidence_changed")
    return candidate, evaluation


def _ledger(service):
    from cognition.amendments import ProposalLedger

    return ProposalLedger(service.target.state_dir / "amendment-proposals.jsonl")


def _assert_path(root: Path, target: Path) -> Path:
    # Check physical ancestors before resolution: a junction must never move a
    # learned procedure into another persona's tree.
    current = target
    while current != current.parent:
        if current.is_symlink() or (
            current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise LearningError("learning_application_path_is_link")
        if current == root:
            break
        current = current.parent
    if not target.resolve().is_relative_to(root.resolve()):
        raise LearningError("learning_application_path_escaped_profile")
    return target


def _apply_amendment(
    service,
    candidate,
    evaluation,
    *,
    restoring: bool = False,
    proposal_id_override: str | None = None,
):
    from cognition.amendments import AmendmentPolicy, AmendmentProposal, apply_amendment_if_allowed

    ledger = _ledger(service)
    proposal_id = proposal_id_override or str(
        uuid.uuid5(uuid.NAMESPACE_URL, "learning:" + candidate["id"])
    )
    proposal = next((item for item in ledger.read_all() if item.id == proposal_id), None)
    if proposal and proposal.status == "rolled_back" and restoring:
        if ledger._update_record_unique(proposal.id, {"status": "pending"}) != "updated":
            raise LearningError("restore_amendment_prepare_failed")
        proposal.status = "pending"
    elif proposal and proposal.status == "rolled_back":
        # Compensation after an activation-storage failure leaves the canonical
        # rollback row intact; a retry gets its own deterministic ledger identity.
        attempts = sum(
            item.rationale == "Evaluated automatic adoption " + evaluation["id"]
            and item.status == "rolled_back"
            for item in ledger.read_all()
        )
        proposal_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, "learning:" + candidate["id"] + f":retry:{attempts}")
        )
        proposal = next((item for item in ledger.read_all() if item.id == proposal_id), None)
    if proposal is None:
        proposal = AmendmentProposal(
            id=proposal_id,
            source="harness_learning",
            target_file=candidate["target_file"],
            summary=candidate["title"],
            rationale="Evaluated automatic adoption " + evaluation["id"],
            proposed_content=candidate["content"],
            evidence_paths=["learning-record:" + eid for eid in candidate["evidence_ids"]],
            confidence_score=0.0,
        )
        if not ledger.append(proposal):
            raise LearningError("amendment_content_already_owned")

    def verify(proposal, memory_dir):
        current, _ = _bound_receipt(
            service, candidate["id"], evaluation["id"], allow_retired=restoring
        )
        okay = (
            Path(memory_dir).resolve() == service.target.memory_dir.resolve()
            and proposal.source == "harness_learning"
            and proposal.proposed_content == current["content"]
            and proposal.target_file == current["target_file"]
        )
        return okay, "evaluated_learning_receipt" if okay else "evaluation_binding_failed"

    # This policy is supplied for ONE source-owned proposal only, never used to
    # drain other producers' pending rows. Confidence does not grant permission.
    policy = AmendmentPolicy(
        min_confidence=0,
        min_evidence_paths=1,
        max_content_chars=12000,
        allow_destructive=True,
        evidence_check=verify,
        source_target_allowlist={
            "harness_learning": frozenset({"MEMORY.md", "SELF.md", "SOUL.md"})
        },
    )
    result = apply_amendment_if_allowed(
        proposal, ledger, service.target.memory_dir, policy=policy, section_cap=1000000
    )
    if result.status != "applied":
        raise LearningError("amendment_application_" + result.status + ":" + result.policy_reason)
    # Canonical ledger contains the exact final hashes even on retry reconciliation.
    applied = next(item for item in ledger.read_all() if item.id == proposal_id)
    return {
        "amendment_id": proposal_id,
        "application_receipt": asdict(result),
        "target_file": candidate["target_file"],
        "applied_hash": applied.after_hash,
    }


def _apply_skill(service, candidate, evaluation, *, restoring: bool = False):
    from cognition import skill_promotion, skill_usage
    from cognition.skill_promotion import _skill_content_signature
    from cognition.skills import SkillSpec, write_skill

    root = service.target.skills_dir
    name = (
        "learning-" + candidate["id"].replace("-", "")[:12] + "-" + candidate["content_hash"][:12]
    )
    sidecar = service.target.data_dir / "skill_usage.json"
    audit_path = service.target.data_dir / "skill_audit.jsonl"
    expected_body = candidate["content"].strip()
    draft = _assert_path(root, root / "generated" / "learning" / name / "SKILL.md")
    promoted = _assert_path(root, root / "promoted" / name / "SKILL.md")

    def verify_existing(path):
        if not path.is_file():
            return False
        _, body = _skill_content_signature(path.read_text(encoding="utf-8"))
        return body.strip() == expected_body

    if promoted.exists():
        if not verify_existing(promoted):
            raise LearningError("promoted_skill_content_conflict")
        result = {"status": "already_promoted", "path": str(promoted)}
    else:
        if draft.exists() and not verify_existing(draft):
            raise LearningError("draft_skill_content_conflict")
        if not draft.exists():
            draft = write_skill(
                SkillSpec(
                    name=name,
                    description=candidate["title"].replace("\n", " "),
                    category="learning",
                    trigger_patterns=[candidate["applicability"]],
                    source_session=candidate["id"],
                    created_at=candidate["created_at"],
                    body=expected_body,
                ),
                root,
            )
        draft_hash = hashlib.sha256(draft.read_bytes()).hexdigest()
        if not skill_usage.get_usage(name, sidecar_path=sidecar):
            # One actual draft registration, never fake recurrence to pass an old gate.
            skill_usage.record_recurrence(
                name, source_session=candidate["id"], path=str(draft), sidecar_path=sidecar
            )
        skill_usage.record_persona_assignment(name, service.target.persona_id, sidecar_path=sidecar)

        def verify(path, pinned_digest):
            _bound_receipt(service, candidate["id"], evaluation["id"], allow_retired=restoring)
            return pinned_digest == draft_hash and verify_existing(path)

        result = skill_promotion.promote(
            name,
            operator_approved=False,
            evaluation_check=verify,
            persona_id=service.target.persona_id,
            skills_dir=root,
            sidecar_path=sidecar,
            audit_path=audit_path,
        )
        if result["status"] not in {"promoted", "already_promoted"}:
            raise LearningError("skill_application_" + result["status"])
    return {
        "skill_name": name,
        "draft_path": str(draft.parent),
        "application_receipt": result,
        "applied_hash": hashlib.sha256(promoted.read_bytes()).hexdigest(),
    }


def _compensate_application(service, application, *, reason):
    if application.get("amendment_id"):
        from cognition.amendment_rollback import rollback_amendment

        result = asdict(
            rollback_amendment(
                application["amendment_id"],
                "harness_learning",
                reason,
                ledger=_ledger(service),
                memory_dir=service.target.memory_dir,
                preserve_later=True,
            )
        )
    else:
        from cognition.skill_promotion import rollback_promotion

        result = rollback_promotion(
            application["skill_name"],
            application["draft_path"],
            reason=reason,
            skills_dir=service.target.skills_dir,
            sidecar_path=service.target.data_dir / "skill_usage.json",
            audit_path=service.target.data_dir / "skill_audit.jsonl",
            expected_hash=application["applied_hash"],
        )
    if result.get("status") != "rolled_back":
        raise LearningError(
            "activation_failed_and_compensation_incomplete:" + str(result.get("status"))
        )
    return result


def promote_candidate(service: Any, candidate_id: str, evaluation_id: str) -> dict:
    if not service.enabled():
        raise LearningError("learning_disabled")
    candidate, evaluation = _bound_receipt(service, candidate_id, evaluation_id)
    token = service.store.claim(candidate_id, "adoption")
    if token is None:
        raise LearningError("adoption_in_progress")
    application, previous, retired_prior = None, None, None
    try:
        # Intent commits before physical publication. The following transaction
        # excludes evidence correction until application and activation agree.
        service.store.event(
            candidate_id,
            "adoption_intent",
            {"evaluation_id": evaluation_id, "candidate_hash": candidate["content_hash"]},
            key="adopt:" + evaluation_id,
        )
        with service.store.atomic():
            candidate, evaluation = _bound_receipt(service, candidate_id, evaluation_id)
            receipt = EvaluationReceipt(**evaluation["receipt"])
            if receipt.mode == "qualification":
                validate_evaluation_context_binding(service, candidate, receipt, check_current=True)
            for activation in service.store.all("activation"):
                if activation.get("candidate_id") == candidate_id and activation.get("status") in {
                    "active_provisional",
                    "active_supported",
                }:
                    if not activation_is_applied(service, activation):
                        raise LearningError("active_application_changed")
                    if activation.get("evaluation_id") == evaluation_id:
                        return activation
                    previous = activation
                    break
            if previous:
                application = {
                    key: previous[key]
                    for key in (
                        "amendment_id",
                        "application_receipt",
                        "target_file",
                        "applied_hash",
                        "skill_name",
                        "draft_path",
                    )
                    if key in previous
                }
            elif candidate["candidate_type"] == "procedure" or candidate["target_file"] == "skill":
                application = _apply_skill(service, candidate, evaluation)
            else:
                application = _apply_amendment(service, candidate, evaluation)
            prior = next(
                (
                    item
                    for item in service.store.all("activation")
                    if item.get("candidate_id") == candidate.get("prior_candidate_id")
                    and item.get("status") in {"active_provisional", "active_supported"}
                ),
                None,
            )
            qualified_models = list(previous.get("qualified_models", [])) if previous else []
            if evaluation.get("model") and evaluation["model"] not in qualified_models:
                qualified_models.append(evaluation["model"])
            result = service.record_activation(
                candidate_id,
                {
                    "evaluation_id": evaluation_id,
                    "candidate_hash": candidate["content_hash"],
                    "method_status": "active_provisional",
                    "procedure_version": candidate["content_hash"],
                    "qualified_models": qualified_models,
                    "claim_scope": evaluation.get("claim_scope", "controlled_task_evaluation"),
                    "prior_activation_id": prior["id"]
                    if prior
                    else previous.get("prior_activation_id")
                    if previous
                    else None,
                    **application,
                },
                activation_key=evaluation_id,
            )
            if previous:
                service.set_status(
                    previous["id"],
                    "superseded",
                    reason="new model qualification",
                    key="requalified:" + evaluation_id,
                )
            if prior:
                retired = rollback_activation(
                    service,
                    prior["id"],
                    reason="replaced_by:" + candidate_id,
                    restore_previous=False,
                )
                if retired.get("status") != "rolled_back":
                    raise LearningError("prior_method_retirement_conflict")
                retired_prior = prior
            return result
    except Exception:
        if application and previous is None:
            _compensate_application(service, application, reason="activation persistence failed")
        if retired_prior:
            _restore_previous(service, retired_prior, cause_id="compensate:" + candidate_id)
        raise
    finally:
        service.store.release_claim(candidate_id, "adoption", token)


def rollback_activation(
    service: Any, activation_id: str, *, reason: str, restore_previous: bool = True
) -> dict:
    if not str(reason).strip():
        raise LearningError("rollback_reason_required")
    activation = service.get_record(activation_id)
    if not activation or activation.get("kind") != "activation":
        raise LearningError("activation_not_found")
    if activation.get("status") == "rolled_back":
        return activation
    if activation.get("status") not in {"active_provisional", "active_supported"}:
        raise LearningError("activation_not_current")
    token = service.store.claim(activation_id, "rollback")
    if token is None:
        raise LearningError("rollback_in_progress")
    try:
        service.store.event(
            activation_id,
            "rollback_intent",
            {"reason": reason},
            key="rollback:" + canonical_hash(reason),
        )
        if activation.get("amendment_id"):
            from cognition.amendment_rollback import rollback_amendment

            result = asdict(
                rollback_amendment(
                    activation["amendment_id"],
                    "harness_learning",
                    reason,
                    ledger=_ledger(service),
                    memory_dir=service.target.memory_dir,
                    preserve_later=True,
                )
            )
        elif activation.get("skill_name"):
            from cognition.skill_promotion import rollback_promotion

            result = rollback_promotion(
                activation["skill_name"],
                activation["draft_path"],
                reason=reason,
                skills_dir=service.target.skills_dir,
                sidecar_path=service.target.data_dir / "skill_usage.json",
                audit_path=service.target.data_dir / "skill_audit.jsonl",
                expected_hash=activation.get("applied_hash"),
            )
        else:
            raise LearningError("activation_has_no_application")
        if result.get("status") != "rolled_back":
            service.store.event(
                activation_id,
                "rollback_failure",
                result,
                key="rollback_failure:" + canonical_hash(result),
            )
            return {
                "activation_id": activation_id,
                "status": "conflict" if "conflict" in str(result) else "failed",
                "application_receipt": result,
            }
        service.record_rollback(
            activation_id,
            {"reason": reason, "application_receipt": result},
            rollback_key="completed",
        )
        rolled_back = service.set_status(
            activation_id, "rolled_back", reason=reason, key="rollback_status"
        )
        if restore_previous and activation.get("prior_activation_id"):
            prior = service.get_record(activation["prior_activation_id"])
            try:
                restored = _restore_previous(service, prior, cause_id=activation_id)
                service.store.event(
                    activation_id,
                    "prior_restored",
                    {"activation_id": restored["id"]},
                    key="prior_restored",
                )
                rolled_back["restored_previous_activation_id"] = restored["id"]
            except (LearningError, OSError) as exc:
                service.store.event(
                    activation_id,
                    "prior_restore_failed",
                    {"reason": str(exc)},
                    key="prior_restore_failed:" + canonical_hash(str(exc)),
                )
                rolled_back["prior_restoration_error"] = str(exc)
        return rolled_back
    finally:
        service.store.release_claim(activation_id, "rollback", token)


def _restore_previous(service, prior, *, cause_id):
    """Restore a still-supported predecessor, preserving its original content version."""
    if not prior or prior.get("kind") != "activation":
        raise LearningError("prior_activation_missing")
    with service.store.atomic():
        candidate, evaluation = _bound_receipt(
            service, prior["candidate_id"], prior["evaluation_id"], allow_retired=True
        )
        if candidate.get("status") in {"needs_reassessment", "rejected"}:
            raise LearningError("prior_evidence_requires_reassessment")
        application = (
            _apply_skill(service, candidate, evaluation, restoring=True)
            if prior.get("skill_name")
            else _apply_amendment(
                service,
                candidate,
                evaluation,
                restoring=True,
                proposal_id_override=prior.get("amendment_id"),
            )
        )
        service.set_status(
            candidate["id"],
            "qualified",
            reason="restore supported predecessor",
            key="restore_qualified:" + cause_id,
        )
        return service.record_activation(
            candidate["id"],
            {
                "evaluation_id": evaluation["id"],
                "candidate_hash": candidate["content_hash"],
                "method_status": "active_provisional",
                "procedure_version": candidate["content_hash"],
                "qualified_models": prior.get("qualified_models", []),
                "prior_activation_id": prior.get("prior_activation_id"),
                "restored_from_activation_id": prior["id"],
                **application,
            },
            activation_key="restored:" + cause_id,
        )


def reassess_activation(
    service: Any,
    activation_id: str,
    evaluation_id: str,
    *,
    observation_ids: list[str] | None = None,
) -> dict:
    """Act on a fresh comparison; observational support remains noncausal."""
    activation = service.get_record(activation_id)
    if not activation or activation.get("kind") != "activation":
        raise LearningError("activation_not_found")
    evaluation = service.get_record(evaluation_id)
    if not evaluation or evaluation.get("candidate_id") != activation["candidate_id"]:
        raise LearningError("reassessment_candidate_mismatch")
    body = evaluation.get("receipt")
    if (
        not isinstance(body, dict)
        or canonical_hash(body) != evaluation.get("receipt_hash")
        or body.get("profile_id") != service.target.persona_id
        or body.get("candidate_hash") != activation["candidate_hash"]
        or body.get("evaluator_version") != EVALUATOR_VERSION
    ):
        raise LearningError("reassessment_receipt_invalid")
    if evaluation.get("reason") in {"new_hard_failure", "no_primary_improvement"}:
        return rollback_activation(
            service, activation_id, reason="fresh_evaluation:" + evaluation["reason"]
        )
    _bound_receipt(service, activation["candidate_id"], evaluation_id)
    supported_ids = []
    for observation in service.evidence_records(observation_ids or []):
        if (
            observation.get("kind") != "observation"
            or observation.get("quality") != "direct"
            or observation.get("status") != "resolved"
        ):
            continue
        experience = service.get_record(observation.get("experience_id", ""))
        delivered = any(
            context.get("experience_id") == observation.get("experience_id")
            and any(
                item.get("activation_id") == activation_id for item in context.get("included", [])
            )
            for context in service.store.all("context")
        )
        if experience and experience.get("mode") == "real" and delivered:
            supported_ids.append(observation["id"])
    return service.store.event(
        activation_id,
        "reassessment",
        {
            "evaluation_id": evaluation_id,
            "real_observation_ids": sorted(set(supported_ids)),
            "claim_scope": "observational_support_only"
            if supported_ids
            else "controlled_task_evaluation",
        },
        key="reassessment:" + evaluation_id,
    )


def activation_is_applied(service: Any, activation: dict) -> bool:
    """Read physical content authority without healing files or mutating status."""
    try:
        candidate = service.get_record(activation["candidate_id"])
        if not candidate or candidate["content_hash"] != activation["candidate_hash"]:
            return False
        if activation.get("skill_name"):
            target = _assert_path(
                service.target.skills_dir,
                service.target.skills_dir / "promoted" / activation["skill_name"] / "SKILL.md",
            )
            return hashlib.sha256(target.read_bytes()).hexdigest() == activation.get("applied_hash")
        if activation.get("amendment_id"):
            from cognition.amendment_rollback import _find_unique_raw_record

            row, refusal = _find_unique_raw_record(_ledger(service), activation["amendment_id"])
            if (
                refusal
                or row.get("status") != "applied"
                or row.get("proposed_content") != candidate["content"]
            ):
                return False
            target = _assert_path(
                service.target.memory_dir, service.target.memory_dir / row["target_file"]
            )
            block = (
                f"<!-- HOMIE_AUTO_AMENDMENT:{row['id']} -->\n- {row['proposed_content'].strip()}\n"
                f"  - source: {row['source']}\n"
                f"  - evidence: {', '.join(row['evidence_paths'])}\n"
            ).encode()
            data = target.read_bytes()
            return sum(data.count(form) for form in (block, block.replace(b"\n", b"\r\n"))) == 1
    except (OSError, ValueError, KeyError):
        return False
    return False
