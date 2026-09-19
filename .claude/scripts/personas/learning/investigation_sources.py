"""Observe exact source revisions already captured by this persona's harness.

Domain labels are descriptive model text, not plugin names or access grants.
This fallback never imports a model-selected module or fetches an arbitrary URL.
Installed domain observers retain their richer acquisition capabilities.
"""

from __future__ import annotations

from .models import LearningError, content_hash
from .queue import is_learning_source


def source_bindings(record: dict, parent: dict) -> dict[str, str]:
    """Project host-captured physical references; generated conclusions are excluded."""
    bindings = {}
    evidence = record.get("evidence", {})
    evidence = evidence if isinstance(evidence, dict) else {}
    revision = evidence.get("revision") or record.get("source_revision") or content_hash(evidence)
    for owner in (record, evidence, parent.get("metadata", {})):
        for key in ("source_id", "source_ref", "subject_id"):
            ref = owner.get(key)
            if isinstance(ref, str) and ref:
                bindings[ref] = str(
                    owner.get("revision") or owner.get("source_revision") or revision
                )
        for item in owner.get("source_evidence", []):
            if isinstance(item, dict):
                ref = item.get("source_ref") or item.get("ref")
                rev = item.get("source_revision") or item.get("revision")
                if isinstance(ref, str) and isinstance(rev, str) and ref and rev:
                    bindings[ref] = rev
    # A parent is also a host-owned source envelope. This lets an investigation
    # explicitly ask for the next observed result of the same actual activity.
    for ref in (parent.get("id"), parent.get("origin_key")):
        if isinstance(ref, str) and ref:
            bindings[ref] = str(revision)
    return bindings


def _original(service, row: dict) -> dict | None:
    if row.get("kind") != "observation" or row.get("investigation_id"):
        return None
    if row.get("status") not in {"partial", "resolved"} or row.get("quality") not in {
        "direct",
        "proxy",
    }:
        return None
    parent = service.store.get(row.get("experience_id", "")) or {}
    if not is_learning_source(parent) or row.get("cognitive_generated"):
        return None
    return parent


def _requested_bindings(service, inquiry: dict) -> set[str]:
    trigger = inquiry["trigger"]
    requested = trigger.get("source_id", "")
    result = {requested} if requested else set()
    # Resolve journal reference aliases only when the identifier is physically
    # owned and belongs to the inquiry's original evidence/activity.
    roots = set(inquiry.get("evidence_ids", [])) | set(inquiry.get("anchor_evidence_ids", []))
    record_id = requested.removeprefix("learning:")
    if record_id in roots or record_id == inquiry.get("experience_id"):
        row = service.store.get(record_id) or {}
        if row.get("kind") == "observation":
            parent = _original(service, row)
            if parent:
                result.update(source_bindings(row, parent))
        elif row.get("kind") == "experience" and is_learning_source(row):
            result.add(row["id"])
            if row.get("origin_key"):
                result.add(row["origin_key"])
    if trigger["type"] == "deadline":
        for row in service.store.many(list(roots)).values():
            parent = _original(service, row)
            if parent:
                result.update(source_bindings(row, parent))
        if inquiry.get("experience_id"):
            result.add(inquiry["experience_id"])
    return result


def matching_evidence(service, inquiry: dict, observation: dict) -> dict | None:
    from .cognition import _epoch, trigger_satisfied

    parent = _original(service, observation)
    if not parent or _epoch(observation.get("occurred_at", observation["created_at"])) <= _epoch(
        inquiry["created_at"]
    ):
        return None
    trigger = inquiry["trigger"]
    bindings = source_bindings(observation, parent)
    requested = _requested_bindings(service, inquiry)
    matched = sorted(requested.intersection(bindings))
    if trigger.get("source_id") in matched:
        matched.remove(trigger["source_id"])
        matched.insert(0, trigger["source_id"])
    if trigger.get("source_id") == "local_work":
        matched = ["local_work"]
        bindings["local_work"] = content_hash(observation.get("evidence", {}))
    if not matched:
        return None
    evidence = dict(observation.get("evidence", {}))
    evidence.update(
        source_id=trigger.get("source_id", matched[0]),
        revision=bindings[matched[0]],
        original_observation_id=observation["id"],
        original_source_bindings=bindings,
    )
    if not trigger_satisfied(trigger, evidence):
        return None
    # Latest evidence is replaced after each revisit. Read the immutable history
    # so pending reassessments cannot oscillate between old captured revisions.
    consumed_ids = set(inquiry.get("latest_evidence_ids", []))
    for event in service.store.events(inquiry["id"]):
        if event["event_type"] == "investigation_transition":
            payload = event["payload"]
            consumed_ids.update(payload.get("latest_evidence_ids", []))
            consumed_ids.update(payload.get("evidence_ids", []))
    stamp = _epoch(observation.get("occurred_at", observation["created_at"]))
    for consumed in service.store.many(list(consumed_ids)).values():
        if consumed.get("kind") != "observation":
            continue
        if stamp <= _epoch(consumed.get("occurred_at", consumed["created_at"])):
            return None
        old_parent = service.store.get(consumed.get("experience_id", "")) or {}
        old_bindings = source_bindings(consumed, old_parent)
        if any(old_bindings.get(ref) == bindings[ref] for ref in matched):
            return None
    return evidence


def collect_journal_source(service, inquiry: dict) -> dict:
    """No new evidence returns pending; an unrelated or generated row cannot wake it."""
    from .cognition import _epoch

    observations = sorted(
        service.store.all("observation"),
        key=lambda row: (
            _epoch(row.get("occurred_at", row["created_at"])),
            row["created_at"],
            row["id"],
        ),
        reverse=True,
    )
    for observation in observations:
        evidence = matching_evidence(service, inquiry, observation)
        if evidence is not None:
            return {
                "available": True,
                "observation_id": observation["id"],
                "source_key": evidence["revision"],
                "occurred_at": observation.get("occurred_at", observation["created_at"]),
                "quality": observation["quality"],
                "evidence": evidence,
            }
    return {"available": False, "reason": "No newer captured revision of the linked source"}


def validate_journal_result(service, inquiry: dict, result: dict) -> dict:
    observation = service._owned(result.get("observation_id", ""), "observation")
    evidence = matching_evidence(service, inquiry, observation)
    if evidence is None or evidence != result.get("evidence"):
        raise LearningError("Journal observer result is not a newer linked source")
    return observation
