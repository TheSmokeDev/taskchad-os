"""Policy-gated durable-memory amendments for the cognitive loop.

Scheduled cognition emits small structured amendment records. This module is
the machine policy gate that decides whether those records are safe to apply to
durable cognitive files, writes rollback snapshots, and preserves an audit
ledger. It intentionally allows bounded self-evolution while rejecting secrets,
large rewrites, destructive edits, and low-evidence identity changes.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AMENDMENT_TARGETS = frozenset({"SELF.md", "SOUL.md", "USER.md", "MEMORY.md"})
PROPOSAL_STATUSES = frozenset({
    "pending",
    "approved",
    "rejected",
    "applied",
    "policy_rejected",
    "skipped",
})
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|bearer\s+[a-z0-9._-]{12,}|"
    r"sk-[a-z0-9_-]{12,}|xox[baprs]-[a-z0-9-]{12,})"
)
_DESTRUCTIVE_RE = re.compile(r"(?i)\b(delete|remove|erase|drop|wipe|truncate)\b")


@dataclass
class AmendmentProposal:
    """A durable-memory amendment and its policy/apply audit state."""

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
    confidence_score: float = 0.0
    policy_decision: str = ""
    policy_reason: str = ""
    before_hash: str = ""
    after_hash: str = ""
    rollback_snapshot_path: str = ""
    applied_at: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()
        self.target_file = normalize_target_file(self.target_file)
        self.status = self.status if self.status in PROPOSAL_STATUSES else "pending"
        self.evidence_paths = [str(path) for path in self.evidence_paths]
        try:
            self.confidence_score = float(self.confidence_score or 0.0)
        except (TypeError, ValueError):
            self.confidence_score = 0.0
        if not self.dedupe_key:
            self.dedupe_key = _dedupe_key(
                self.source,
                self.target_file,
                self.summary,
                self.proposed_content,
            )


class ProposalLedger:
    """JSONL store for amendment proposals and policy/apply audit fields."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, proposal: AmendmentProposal) -> bool:
        """Append a proposal if its target is valid and not already active."""

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
        """Return proposals still waiting on policy/apply processing."""

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
            if proposal.status in {"pending", "approved", "applied"}
        }


@dataclass(frozen=True)
class AmendmentPolicy:
    """Machine policy thresholds for autonomous amendment application."""

    min_confidence: float = 0.75
    min_evidence_paths: int = 1
    max_content_chars: int = 1200
    allow_destructive: bool = False


@dataclass(frozen=True)
class AmendmentApplyResult:
    """Result for one policy/apply attempt."""

    proposal_id: str
    target_file: str
    status: str
    policy_decision: str
    policy_reason: str
    before_hash: str = ""
    after_hash: str = ""
    rollback_snapshot_path: str = ""


def build_amendment_gate_section(
    ledger_file: Path | str,
    *,
    source: str,
    targets: Iterable[str] = AMENDMENT_TARGETS,
) -> str:
    """Return prompt instructions for policy-gated durable-memory changes."""

    target_list = ", ".join(sorted(normalize_target_file(target) for target in targets))
    return f"""## Policy-Gated Durable Memory Amendments

Durable identity and memory file changes are autonomous only through the
machine policy gate. Do not directly edit `SELF.md`, `SOUL.md`, `USER.md`, or
`MEMORY.md`; emit bounded JSON amendment records for this ledger instead:

`{Path(ledger_file)}`

The policy engine may automatically apply records that have enough evidence,
safe content, a valid target, rollback coverage, and no duplicate dedupe key.

Required JSON keys:
- `source`: `{source}`
- `target_file`: one of `{target_list}`
- `summary`: short human review title
- `rationale`: why the change is justified
- `evidence_paths`: source files or logs supporting the proposal
- `proposed_content`: the exact concise text or patch-style note to review
- `confidence_score`: 0.0-1.0 confidence in the amendment
- `status`: `pending`

No proposal means no ledger write. Never include secrets, credentials, account
tokens, or broad deletion instructions. Keep each amendment under 1200 chars."""


def parse_amendment_records(
    text: str,
    *,
    default_source: str = "scheduled_cognition",
) -> list[AmendmentProposal]:
    """Parse JSON object or JSON-array amendment records from model output."""

    proposals: list[AmendmentProposal] = []
    for record in _iter_json_records(text):
        if not isinstance(record, dict):
            continue
        data = dict(record)
        data.setdefault("source", default_source)
        data.setdefault("status", "pending")
        proposal = _coerce_dataclass(AmendmentProposal, data)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def process_amendment_output(
    text: str,
    ledger: ProposalLedger,
    memory_dir: Path | str,
    *,
    default_source: str = "scheduled_cognition",
    auto_apply: bool = True,
    policy: AmendmentPolicy | None = None,
) -> list[AmendmentApplyResult]:
    """Capture structured amendments from output and optionally apply them."""

    for proposal in parse_amendment_records(text, default_source=default_source):
        ledger.append(proposal)
    if not auto_apply:
        return []
    return apply_policy_approved_amendments(
        ledger,
        memory_dir,
        policy=policy,
    )


def apply_policy_approved_amendments(
    ledger: ProposalLedger,
    memory_dir: Path | str,
    *,
    policy: AmendmentPolicy | None = None,
    limit: int | None = None,
) -> list[AmendmentApplyResult]:
    """Apply pending/approved amendments that pass policy evaluation."""

    active_policy = policy or AmendmentPolicy()
    results: list[AmendmentApplyResult] = []
    candidates = [
        proposal for proposal in ledger.read_all()
        if proposal.status in {"pending", "approved"}
    ]
    if limit is not None:
        candidates = candidates[:limit]

    for proposal in candidates:
        result = apply_amendment_if_allowed(
            proposal,
            ledger,
            memory_dir,
            policy=active_policy,
        )
        results.append(result)
    return results


def apply_amendment_if_allowed(
    proposal: AmendmentProposal,
    ledger: ProposalLedger,
    memory_dir: Path | str,
    *,
    policy: AmendmentPolicy | None = None,
) -> AmendmentApplyResult:
    """Evaluate and apply one amendment proposal if machine policy allows."""

    active_policy = policy or AmendmentPolicy()
    allowed, reason = evaluate_amendment_policy(proposal, active_policy)
    if not allowed:
        ledger._update_record(
            proposal.id,
            {
                "status": "policy_rejected",
                "policy_decision": "reject",
                "policy_reason": reason,
                "reviewed_at": datetime.now(UTC).isoformat(),
            },
        )
        return AmendmentApplyResult(
            proposal_id=proposal.id,
            target_file=proposal.target_file,
            status="policy_rejected",
            policy_decision="reject",
            policy_reason=reason,
        )

    memory_root = Path(memory_dir)
    target = memory_root / proposal.target_file
    before = _read_text(target)
    before_hash = _sha256(before)
    rollback = _write_rollback_snapshot(ledger.path, proposal, before)
    after = _append_autonomous_amendment(before, proposal)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(after, encoding="utf-8")
    after_hash = _sha256(after)
    applied_at = datetime.now(UTC).isoformat()
    ledger._update_record(
        proposal.id,
        {
            "status": "applied",
            "policy_decision": "apply",
            "policy_reason": reason,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "rollback_snapshot_path": str(rollback),
            "applied_at": applied_at,
            "reviewed_at": applied_at,
            "reviewer": "machine_policy",
        },
    )
    return AmendmentApplyResult(
        proposal_id=proposal.id,
        target_file=proposal.target_file,
        status="applied",
        policy_decision="apply",
        policy_reason=reason,
        before_hash=before_hash,
        after_hash=after_hash,
        rollback_snapshot_path=str(rollback),
    )


def evaluate_amendment_policy(
    proposal: AmendmentProposal,
    policy: AmendmentPolicy | None = None,
) -> tuple[bool, str]:
    """Return whether a proposal is allowed and a stable reason string."""

    active_policy = policy or AmendmentPolicy()
    content = proposal.proposed_content.strip()
    if proposal.target_file not in AMENDMENT_TARGETS:
        return False, "target_not_allowed"
    if not content:
        return False, "empty_content"
    if len(content) > active_policy.max_content_chars:
        return False, "content_too_large"
    if proposal.confidence_score < active_policy.min_confidence:
        return False, "low_confidence"
    if len(proposal.evidence_paths) < active_policy.min_evidence_paths:
        return False, "insufficient_evidence"
    if _SECRET_RE.search(content):
        return False, "secret_like_content"
    if not active_policy.allow_destructive and _DESTRUCTIVE_RE.search(content):
        return False, "destructive_change_requires_manual_review"
    return True, "policy_allowed"


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


def _iter_json_records(text: str) -> list[Any]:
    records: list[Any] = []
    cleaned_lines = [
        line.strip() for line in str(text).splitlines()
        if line.strip() and not line.strip().startswith("```")
    ]
    joined = "\n".join(cleaned_lines)
    try:
        decoded = json.loads(joined)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list):
        return decoded
    if isinstance(decoded, dict):
        return [decoded]

    for line in cleaned_lines:
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_rollback_snapshot(
    ledger_path: Path,
    proposal: AmendmentProposal,
    before: str,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_id = proposal.id.replace("-", "")[:12]
    rollback_dir = ledger_path.parent / "rollback"
    rollback_dir.mkdir(parents=True, exist_ok=True)
    rollback_path = rollback_dir / f"{proposal.target_file}.{timestamp}.{safe_id}.bak"
    rollback_path.write_text(before, encoding="utf-8")
    return rollback_path


def _append_autonomous_amendment(before: str, proposal: AmendmentProposal) -> str:
    content = proposal.proposed_content.strip()
    marker = f"<!-- HOMIE_AUTO_AMENDMENT:{proposal.id} -->"
    block = (
        f"{marker}\n"
        f"- {content}\n"
        f"  - source: {proposal.source}\n"
        f"  - evidence: {', '.join(proposal.evidence_paths)}\n"
    )
    base = before.rstrip()
    if "## Autonomous Amendments" not in base:
        return f"{base}\n\n## Autonomous Amendments\n\n{block}".lstrip()
    return f"{base}\n\n{block}"


__all__ = (
    "AMENDMENT_TARGETS",
    "AmendmentApplyResult",
    "AmendmentPolicy",
    "PROPOSAL_STATUSES",
    "AmendmentProposal",
    "ProposalLedger",
    "apply_amendment_if_allowed",
    "apply_policy_approved_amendments",
    "build_amendment_gate_section",
    "evaluate_amendment_policy",
    "normalize_target_file",
    "parse_amendment_records",
    "process_amendment_output",
)
