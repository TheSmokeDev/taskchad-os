"""Physical source identity shared by cognition and qualification.

Journal rows are representations, not independent proof. A transcript envelope,
per-turn experience and corrected revision can all describe the same message.
Missing historical identities remain unknown instead of becoming synthetic IDs.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import LearningError


def independent_sources(records: Iterable[dict]) -> dict:
    groups, unknown, generated, context_only = {}, [], [], []
    rows = list(records)
    for row in rows:
        identifier = row.get("id", "")
        metadata = row.get("metadata", {})
        if metadata.get("evidence_role") == "source_container" or metadata.get("context_only"):
            context_only.append(identifier)
            continue
        if (
            metadata.get("learning_role")
            or metadata.get("cognitive_generated")
            or row.get("cognitive_generated")
        ):
            generated.append(identifier)
            continue
        candidates = []
        for owner in (row, metadata, row.get("evidence", {})):
            if isinstance(owner, dict) and owner.get("source_evidence") is not None:
                values = owner["source_evidence"]
                if not isinstance(values, list):
                    raise LearningError("physical source evidence must be an array")
                candidates.extend(values)
        found = False
        for source in candidates:
            if not isinstance(source, dict):
                raise LearningError(
                    "physical source evidence must have a stable reference and revision"
                )
            ref = source.get("source_ref", source.get("ref"))
            revision = source.get("source_revision", source.get("revision"))
            if (
                not isinstance(ref, str)
                or not ref.strip()
                or not isinstance(revision, str)
                or not revision.strip()
            ):
                raise LearningError(
                    "physical source evidence must have a stable reference and revision"
                )
            found = True
            group = groups.setdefault(
                ref, {"source_ref": ref, "revisions": set(), "record_ids": set()}
            )
            group["revisions"].add(revision)
            group["record_ids"].add(identifier)
        if not found:
            unknown.append(identifier)
    return {
        "sources": [
            {
                "source_ref": ref,
                "revisions": sorted(row["revisions"]),
                "record_ids": sorted(row["record_ids"]),
            }
            for ref, row in sorted(groups.items())
        ],
        "known_source_count": len(groups),
        "independent_source_count": None if unknown else len(groups),
        "unknown_record_ids": sorted(set(unknown)),
        "generated_record_ids": sorted(set(generated)),
        "context_only_record_ids": sorted(set(context_only)),
        "record_count": len(rows),
    }
