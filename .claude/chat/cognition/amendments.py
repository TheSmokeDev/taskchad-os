"""Human-gated amendment proposals for durable memory files.

This module deliberately stops at proposal capture. It does not apply edits to
SELF.md, SOUL.md, USER.md, or MEMORY.md. Applying a proposal is an explicit
operator workflow and is outside this slice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

AMENDMENT_TARGETS = frozenset({"SELF.md", "SOUL.md", "USER.md", "MEMORY.md"})
PROPOSAL_STATUSES = frozenset({"pending", "approved", "rejected", "applied"})


@dataclass
class AmendmentProposal:
    """A durable-memory amendment waiting for human review."""

    id: str = ""
    created_at: str = ""
    source: str = ""
    target_file: str = ""
    summary: str = ""
    rationale: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    proposed_content: str = ""
    status: str = "pending"
    reviewer: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()
        self.target_file = normalize_target_file(self.target_file)
        self.status = self.status if self.status in PROPOSAL_STATUSES else "pending"
        self.evidence_paths = [str(path) for path in self.evidence_paths]
        if not self.dedupe_key:
            self.dedupe_key = _dedupe_key(
                self.source,
                self.target_file,
                self.summary,
                self.proposed_content,
            )


class ProposalLedger:
    """Append-only JSONL store for human-gated amendment proposals."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, proposal: AmendmentProposal) -> bool:
        """Append a proposal if its target is valid and not already pending."""

        if proposal.target_file not in AMENDMENT_TARGETS:
            return False
        if proposal.dedupe_key in self._active_dedupe_keys():
            return False

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(proposal), ensure_ascii=False) + "\n")
            handle.flush()
        return True

    def read_all(self) -> list[AmendmentProposal]:
        """Return all well-formed proposals from the ledger."""

        proposals: list[AmendmentProposal] = []
        for record in self._iter_records():
            proposal = _coerce_dataclass(AmendmentProposal, record)
            if proposal is not None:
                proposals.append(proposal)
        return proposals

    def read_pending(self) -> list[AmendmentProposal]:
        """Return proposals still waiting on human review."""

        return [proposal for proposal in self.read_all() if proposal.status == "pending"]

    def count_pending(self) -> int:
        """Return the pending proposal count."""

        return len(self.read_pending())

    def mark_reviewed(
        self,
        proposal_id: str,
        *,
        status: str,
        reviewer: str,
        note: str | None = None,
    ) -> bool:
        """Mark a proposal approved or rejected without applying it."""

        if status not in {"approved", "rejected"}:
            return False
        return self._update_record(
            proposal_id,
            {
                "status": status,
                "reviewer": reviewer,
                "reviewed_at": datetime.now(UTC).isoformat(),
                "review_note": note,
            },
        )

    def _iter_records(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            with open(self._path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            return []
        return records

    def _update_record(self, proposal_id: str, updates: dict[str, Any]) -> bool:
        records = self._iter_records()
        found = False
        for record in records:
            if record.get("id") == proposal_id:
                record.update(updates)
                found = True
        if found:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return found

    def _active_dedupe_keys(self) -> set[str]:
        return {
            proposal.dedupe_key
            for proposal in self.read_all()
            if proposal.status in {"pending", "approved"}
        }


def build_amendment_gate_section(
    ledger_file: Path | str,
    *,
    source: str,
    targets: Iterable[str] = AMENDMENT_TARGETS,
) -> str:
    """Return prompt instructions for proposal-only durable memory changes."""

    target_list = ", ".join(sorted(normalize_target_file(target) for target in targets))
    return f"""## Human-Gated Durable Memory Amendments

Durable identity and memory file changes are proposal-only in this lane.
Do not directly edit `SELF.md`, `SOUL.md`, `USER.md`, or `MEMORY.md`.

If a change is warranted for one of those files, append one JSON object per
line to this proposal ledger instead:

`{Path(ledger_file)}`

Required JSON keys:
- `source`: `{source}`
- `target_file`: one of `{target_list}`
- `summary`: short human review title
- `rationale`: why the change is justified
- `evidence_paths`: source files or logs supporting the proposal
- `proposed_content`: the exact concise text or patch-style note to review
- `status`: `pending`

No proposal means no ledger write. Never mark a proposal approved, rejected, or
applied yourself."""


def normalize_target_file(value: str) -> str:
    """Normalize and validate an amendment target filename."""

    name = Path(str(value)).name
    return name if name in AMENDMENT_TARGETS else str(value).strip()


def _dedupe_key(*parts: str) -> str:
    normalized = "\n".join(" ".join(str(part).split()).lower() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _coerce_dataclass(cls, record: dict[str, Any]):
    names = {field.name for field in fields(cls)}
    try:
        return cls(**{name: record.get(name) for name in names})
    except (TypeError, ValueError):
        return None


__all__ = (
    "AMENDMENT_TARGETS",
    "PROPOSAL_STATUSES",
    "AmendmentProposal",
    "ProposalLedger",
    "build_amendment_gate_section",
    "normalize_target_file",
)
