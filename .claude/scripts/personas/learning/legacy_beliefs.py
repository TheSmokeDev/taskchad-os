"""Additive links from the physical inference corpus to the learning journal.

Legacy counters cannot prove how many original observations existed. Imports
keep those counters for inspection, and leave independent historical support
unknown. Neither import nor a context read confers behavioral authority.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import LearningContext, content_hash


def partition_legacy_context(records: list, context: LearningContext | None):
    """Represent each selected, unchanged linked version in exactly one region.

    Callers supply the legacy records they will actually display after their
    confidence/scope/cap selection. Explicit directives stay in the authoritative
    legacy region. Inferred beliefs use the already selected canonical block.
    Unselected, paused, edited, or incompletely rendered versions stay untouched.
    """
    if context is None or not context.text:
        return records, context
    represented = {
        version.get("record_id"): version
        for version in context.versions
        if version.get("record_kind") == "understanding"
        and isinstance(version.get("rendered_block"), str)
        and version["rendered_block"]
        and version["rendered_block"] in context.text
    }
    kept, explicit_ids = [], set()
    for record in records:
        selected = (
            record.journal_record_id in represented
            and record.journal_revision is not None
            and belief_revision(record) == record.journal_revision
        )
        if not selected or record.source == "explicit":
            kept.append(record)
        if selected and record.source == "explicit":
            explicit_ids.add(record.journal_record_id)
    if not explicit_ids:
        return kept, context
    text = context.text
    for record_id in explicit_ids:
        text = text.replace(represented[record_id]["rendered_block"], "", 1)
    versions = tuple(
        version for version in context.versions if version.get("record_id") not in explicit_ids
    )
    return kept, LearningContext(text, versions, content_hash(text))


def belief_revision(record) -> str:
    payload = asdict(record)
    for key in list(payload):
        if key.startswith("journal_"):
            payload.pop(key)
    return content_hash(payload)


def _content_revision(record) -> str:
    return content_hash([record.inference, record.observation, record.source])


def _state_file(service) -> Path:
    return service.target.state_dir / "self-model-inferences.json"


def legacy_understanding_current(row: dict, service) -> bool:
    """Read-time physical validity for journal copies; manual edits win."""
    if row.get("origin") != "legacy_import":
        return True
    from cognition.self_model import InferenceTracker

    records = InferenceTracker(_state_file(service)).load()
    record = next((r for r in records if r.id == row.get("legacy_id")), None)
    return bool(
        record is not None
        and record.status in {"active", "confirmed"}
        and (record.source == "explicit" or not record.contradicted_by)
        and belief_revision(record) == row.get("legacy_revision")
    )


def reconcile_active_beliefs(records: list, state_file: Path, *, service=None) -> list:
    """Suppress stale inferred copies when their canonical identity is retired.

    No write occurs here. A physically edited row is authoritative until the next
    additive import; a stored journal hash cannot erase an operator's manual edit.
    """
    if service is None:
        from .service import get_learning_service

        persona_ids = {r.journal_persona_id for r in records if r.journal_persona_id}
        if len(persona_ids) != 1:
            return [r for r in records if not r.journal_record_id or r.source == "explicit"]
        service = get_learning_service(next(iter(persona_ids)))
    if Path(state_file).absolute() != _state_file(service).absolute():
        raise ValueError("legacy belief journal belongs to a different state file")
    linked = service.store.many([r.journal_record_id for r in records if r.journal_record_id])
    return [
        r
        for r in records
        if not r.journal_record_id
        or r.source == "explicit"
        or _content_revision(r) != r.journal_content_revision
        or linked.get(r.journal_record_id, {}).get("status") in {"tentative", "supported"}
    ]


def sync_legacy_beliefs(service, state_file: Path | None = None) -> dict:
    """Idempotently import physical versions, preserving originals in a backup.

    Only this persona's canonical state file is eligible. Original rows stay in
    place and acquire links; repeat imports never create observations or count
    historical claims as new real-world experience.
    """
    from cognition.self_model import InferenceTracker

    state_file = Path(state_file) if state_file is not None else _state_file(service)
    if state_file.absolute() != _state_file(service).absolute():
        raise ValueError("legacy belief import belongs to a different persona")
    if not state_file.exists():
        return {"imported": 0, "unchanged": 0, "backup_path": None}
    raw = state_file.read_bytes()
    # Fail closed on corrupt input; never turn a failed read into an empty write.
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, list):
        raise ValueError("legacy belief corpus must be a list")
    tracker = InferenceTracker(state_file)
    records = tracker.load()
    if len(records) != len(parsed):
        raise ValueError("legacy belief corpus contains invalid records")
    prior = {
        (r.get("legacy_id"), r.get("legacy_revision")): r
        for r in service.store.all("understanding")
        if r.get("origin") == "legacy_import"
    }
    imported = unchanged = 0
    changed = False
    backup = None
    for record in records:
        revision = belief_revision(record)
        current = prior.get((record.id, revision))
        if current is None:
            snapshot = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
            predecessor = record.journal_record_id
            prior_record = service.get_record(predecessor) if predecessor else None
            keep_retired = bool(
                prior_record
                and prior_record.get("status") not in {"tentative", "supported"}
                and _content_revision(record) == record.journal_content_revision
                and record.source != "explicit"
            )
            current = service.record_understanding(
                {
                    "understanding_type": "belief",
                    "title": record.inference[:200],
                    "content": record.inference,
                    "scope": "operator" if record.source == "explicit" else "persona",
                    "uncertainty": (
                        "Legacy history is unknown; reported counts are not independent evidence."
                        if record.historical_evidence_count is None
                        else "Sources are tracked; descriptive support remains unevaluated."
                    ),
                    "origin": "legacy_import",
                    "legacy_id": record.id,
                    "legacy_revision": revision,
                    "legacy_source": record.source,
                    "explicit_operator_authority": record.source == "explicit",
                    "historical_evidence_count": record.historical_evidence_count,
                    "known_evidence_count": record.known_evidence_count,
                    "reported_legacy_evidence_count": record.evidence_count,
                    "source_evidence": record.source_evidence,
                    "legacy_status": record.status,
                    "legacy_contradicted_by": record.contradicted_by,
                    "source_manifest": [
                        {
                            "ref": f"legacy-belief:{record.id}",
                            "revision": revision,
                            "kind": "legacy_belief",
                            "text": snapshot,
                            "start": 0,
                            "end": len(snapshot),
                            "complete": True,
                        }
                    ],
                    "evidence_ids": [],
                    "counterevidence_ids": [],
                    **({"predecessor_id": predecessor} if predecessor else {}),
                },
                source_key=f"legacy-belief:{record.id}:{revision}",
            )
            if (
                keep_retired
                or record.status not in {"active", "confirmed"}
                or (record.contradicted_by and record.source != "explicit")
            ):
                current = service.set_status(
                    current["id"],
                    "invalidated",
                    reason=f"physical legacy belief is {record.status} or contradicted",
                    key=f"legacy-state:{revision}",
                )
            imported += 1
        else:
            unchanged += 1
        if (
            record.journal_record_id,
            record.journal_revision,
            record.journal_persona_id,
            record.journal_content_revision,
        ) != (
            current["id"],
            revision,
            service.target.persona_id,
            _content_revision(record),
        ):
            record.journal_record_id = current["id"]
            record.journal_revision = revision
            record.journal_persona_id = service.target.persona_id
            record.journal_content_revision = _content_revision(record)
            changed = True
    if changed:
        backup = state_file.with_name(
            f"{state_file.name}.pre-journal-{content_hash(raw.hex())[:16]}.bak"
        )
        try:
            with backup.open("xb") as handle:
                handle.write(raw)
        except FileExistsError:
            if backup.read_bytes() != raw:
                raise ValueError("legacy belief backup collision")
        # Refuse to overwrite a concurrent physical edit after the journal work.
        if state_file.read_bytes() != raw:
            raise ValueError("legacy belief corpus changed during import; retry")
        tracker.save(records)
    return {
        "imported": imported,
        "unchanged": unchanged,
        "backup_path": str(backup) if backup else None,
    }
