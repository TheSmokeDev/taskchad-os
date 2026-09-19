"""One automatic change admission and authorization policy.

Producers supply proposals, never permission. Source support and qualification
remain evaluation-owned; amendment/skill publication and rollback keep their
existing physical owners. Explicit operator instructions outrank all proposals.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import LearningError

# Conservative admission: ambiguous standing advice must take the behavioral
# path even when a producer (or support grader) labels it descriptive.
_BEHAVIOR = re.compile(
    r"(?im)(?:\b(?:always|never|must|should|shall|from now on|going forward|"
    r"by default|prefer|prioriti[sz]e|avoid)\b|"
    r"(?:^|[.!?]\s+)(?:please\s+)?(?:ask|use|check|send|run|skip|confirm|"
    r"offer|require|ensure|do not|don't|follow|treat|respond|consult|read)\b|"
    r"\bI (?:will|tend to|work best|usually|routinely)\b)"
)
_DIRECTIVE = re.compile(r"(?i)\b(?:never|always|must|shall|do not|don't|only|forbidden)\b")


def requires_qualification(candidate: dict, support: dict | None = None) -> bool:
    return (
        candidate.get("candidate_type") in {"procedure", "skill"}
        or candidate.get("target_file") in {"skill", "SOUL.md"}
        or candidate.get("changes_behavior") is not False
        or bool(_BEHAVIOR.search(str(candidate.get("content", ""))))
        or (support is not None and support.get("changes_behavior") is not False)
    )


@dataclass(frozen=True)
class AutomaticProposal:
    change_type: str
    title: str
    content: str
    applicability: str
    evidence_ids: tuple[str, ...] = ()
    counterevidence_ids: tuple[str, ...] = ()
    target_file: str = "MEMORY.md"
    producer: str = "synthesis"
    candidate_type: str = "knowledge"
    cycle_id: str = ""
    derived_input_ids: tuple[str, ...] = ()
    source_manifest: tuple[dict, ...] = ()
    producer_runtime: dict = field(default_factory=dict)
    uncertainty: str = "unverified"
    domain: str = "general"


def original_evidence_records(service, record_ids: list[str]) -> list[dict]:
    """Keep generated rehearsal/derivations out of independent source support."""
    from .queue import is_learning_source

    records = service.evidence_records(record_ids)
    for record in records:
        if record["kind"] not in {
            "experience",
            "expectation",
            "execution",
            "observation",
            "context",
        }:
            raise LearningError("automatic_support_requires_original_evidence")
        experience = (
            record
            if record["kind"] == "experience"
            else service.get_record(record.get("experience_id", ""))
        )
        if not experience or not is_learning_source(experience):
            raise LearningError("automatic_support_requires_original_evidence")
    return records


def submit_proposal(service: Any, payload: AutomaticProposal | dict, *, source_key: str) -> dict:
    """Persist host-bound provenance and enqueue ordinary evaluation candidates.

    Legacy source references are preserved as derived context. Empty evidence
    remains pending rather than inventing an experience to unlock evaluation.
    The whole admission is atomic, so a retry cannot orphan its candidate.
    """
    from .service import _safe

    service._require_enabled()
    data = _safe(asdict(payload) if isinstance(payload, AutomaticProposal) else dict(payload))
    for key in ("title", "content", "applicability"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise LearningError("automatic_proposal_requires_" + key)
    kind = data.get("candidate_type", "knowledge")
    if kind == "skill":
        kind = "procedure"
        data["target_file"] = "skill"
    if kind not in {"knowledge", "self_model", "procedure"}:
        raise LearningError("invalid_automatic_proposal_type")
    change_type = data.get("change_type") or (
        "behavior" if kind == "procedure" or data.get("changes_behavior") is True else "description"
    )
    if change_type not in {"tentative", "description", "behavior"}:
        raise LearningError("invalid_automatic_change_type")
    if "changes_behavior" in data and type(data["changes_behavior"]) is not bool:
        raise LearningError("changes_behavior_must_be_boolean")
    data.update(candidate_type=kind, change_type=change_type)
    data["changes_behavior"] = change_type == "behavior" or data.get("changes_behavior", False)
    data["changes_behavior"] = requires_qualification(data)
    for key in ("evidence_ids", "counterevidence_ids", "derived_input_ids"):
        data[key] = list(dict.fromkeys(data.get(key, [])))
    original_evidence_records(service, data["evidence_ids"] + data["counterevidence_ids"])
    for key in data["derived_input_ids"]:
        record = service._owned(key)
        if record["kind"] not in {
            "understanding",
            "investigation",
            "cognitive_cycle",
            "synthesis_cycle",
        }:
            raise LearningError("invalid_automatic_derived_input")
    if data.get("cycle_id"):
        cycle = service._owned(data["cycle_id"])
        if cycle["kind"] not in {"cognitive_cycle", "synthesis_cycle"}:
            raise LearningError("invalid_automatic_cycle")
    data["source_manifest"] = list(data.get("source_manifest", []))
    data.setdefault("target_file", "MEMORY.md")
    data.setdefault("uncertainty", "unverified")
    # These fields can only be derived by the host, never accepted from a model.
    for key in ("id", "status", "candidate_id", "evaluation_id", "activation_id", "authority"):
        data.pop(key, None)
    with service.store.atomic():
        if change_type == "tentative":
            understanding = service.record_understanding(
                {
                    "understanding_type": "belief" if kind == "self_model" else "interpretation",
                    "title": data["title"],
                    "content": data["content"],
                    "scope": data["applicability"],
                    "uncertainty": data["uncertainty"],
                    "evidence_ids": data["evidence_ids"],
                    "counterevidence_ids": data["counterevidence_ids"],
                    "origin": "synthesis",
                    "source_manifest": data["source_manifest"],
                    "derived_input_ids": data["derived_input_ids"],
                    **({"cycle_id": data["cycle_id"]} if data.get("cycle_id") else {}),
                },
                source_key="automatic:" + source_key,
            )
            result = {**data, "status": "tentative", "understanding_id": understanding["id"]}
        elif data["evidence_ids"]:
            candidate = service.propose_candidate(data, source_key="automatic:" + source_key)
            result = {**data, "status": "pending", "candidate_id": candidate["id"]}
        else:
            result = {**data, "status": "pending", "reason": "missing_supporting_evidence"}
        return service.store.put("change_proposal", result, key=source_key)


def propose_automatic_change(service, proposal, *, proposal_key):
    return submit_proposal(service, proposal, source_key=proposal_key)


def update_proposal_state(
    service, candidate_id: str, status: str, *, receipt_id: str, reason: str = ""
) -> None:
    for record in service.store.all("change_proposal"):
        if record.get("candidate_id") == candidate_id:
            service.set_status(
                record["id"],
                status,
                reason=reason,
                key=f"authority:{receipt_id}:{status}",
                metadata={"receipt_id": receipt_id},
            )


def protected_context(service) -> dict:
    """Snapshot manual bytes and explicit directives without mutating legacy files."""
    from cognition.amendments import split_autonomous_amendments

    files = {}
    for name in ("SOUL.md", "SELF.md", "USER.md", "MEMORY.md"):
        path = service.target.memory_dir / name
        if path.is_symlink() or not path.resolve().is_relative_to(
            service.target.memory_dir.resolve()
        ):
            raise LearningError("operator_context_path_escaped_profile")
        if path.exists():
            # Never silently truncate authoritative instructions before judging.
            text, _ = split_autonomous_amendments(path.read_text(encoding="utf-8"))
            if len(text) > 65536:
                raise LearningError("operator_context_too_large")
            files[name] = text.rstrip()
    explicit = []
    path = service.target.state_dir / "self-model-inferences.json"
    if path.is_symlink() or not path.resolve().is_relative_to(service.target.state_dir.resolve()):
        raise LearningError("operator_context_path_escaped_profile")
    if path.exists():
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise LearningError("operator_context_invalid")
        explicit = [
            {key: row.get(key) for key in ("id", "inference", "observation", "source", "status")}
            for row in records
            if row.get("source") == "explicit" and row.get("status", "active") == "active"
        ]
    return {"manual_files": files, "explicit_directives": explicit}


def has_operator_directives(context: dict) -> bool:
    return bool(context.get("explicit_directives")) or any(
        _DIRECTIVE.search(text)
        or any(line.strip() and not line.lstrip().startswith("#") for line in text.splitlines())
        for text in context.get("manual_files", {}).values()
    )


@dataclass(frozen=True)
class AutomaticAuthorization:
    """Host-only capability bound to one proposal and a persisted evaluation."""

    service: Any
    candidate_id: str
    evaluation_id: str
    proposal_id: str
    restoring: bool = False

    def verify(self, proposal, memory_dir: Path) -> tuple[bool, str]:
        from .promotion import _bound_receipt

        if proposal.id != self.proposal_id:
            return False, "automatic_authorization_wrong_proposal"
        candidate, _ = _bound_receipt(
            self.service, self.candidate_id, self.evaluation_id, allow_retired=self.restoring
        )
        if (
            Path(memory_dir).resolve() != self.service.target.memory_dir.resolve()
            or proposal.source != "harness_learning"
            or proposal.proposed_content != candidate["content"]
            or proposal.target_file != candidate["target_file"]
        ):
            return False, "automatic_authorization_binding_failed"
        return True, "evaluated_learning_receipt"


async def evaluate_automatic_change(
    service, candidate_id, *, manifest=None, judge=None, run_case=None, apply=True
):
    """Use the existing receipt owner; unavailable inference stays retryable."""
    from .evaluation import evaluate_candidate
    from .promotion import promote_candidate

    receipt = await evaluate_candidate(
        service, candidate_id, manifest=manifest, judge=judge, run_case=run_case
    )
    if receipt.get("errors"):
        from .errors import LearningOutputError

        raise LearningOutputError("automatic_evaluation_incomplete")
    if not receipt.get("passed"):
        return {
            "status": "pending"
            if receipt.get("reason") == "behavior_requires_qualification"
            else "rejected",
            "candidate_id": candidate_id,
            "evaluation_id": receipt["id"],
            "reason": receipt.get("reason"),
        }
    result = {"status": "qualified", "candidate_id": candidate_id, "evaluation_id": receipt["id"]}
    if apply:
        activation = promote_candidate(service, candidate_id, receipt["id"])
        result.update(status="applied", activation_id=activation["id"])
    return result
