"""Policy-gated durable-memory amendments for the cognitive loop.

Scheduled cognition emits small structured amendment records. This module is
the machine policy gate that decides whether those records are safe to apply to
durable cognitive files, writes rollback snapshots, and preserves an audit
ledger. It intentionally allows bounded self-evolution while rejecting secrets,
large rewrites, destructive edits, and low-evidence identity changes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
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
    "superseded",
})
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|bearer\s+[a-z0-9._-]{12,}|"
    r"sk-[a-z0-9_-]{12,}|xox[baprs]-[a-z0-9-]{12,})"
)
_DESTRUCTIVE_RE = re.compile(r"(?i)\b(delete|remove|erase|drop|wipe|truncate)\b")
_AMENDMENT_MARKER_RE = re.compile(r"<!-- HOMIE_AUTO_AMENDMENT:([0-9a-fA-F-]+) -->")
_SECTION_HEADER = "## Autonomous Amendments"


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
                self.target_file,
                self.proposed_content,
            )


_LEDGER_LOCK_TIMEOUT_S = 5.0
# Thread-local registry of lockfile paths held by this process. msvcrt/fcntl
# locks are per-HANDLE — re-acquiring the same lockfile from the same process
# (e.g. ``append()`` nested inside a held ``ledger_file_lock``) would block
# until timeout. The registry makes same-thread nesting a no-op while
# cross-thread and cross-process acquisition still serialize via the OS lock.
_HELD_LEDGER_LOCKS = threading.local()


def _held_ledger_lock_paths() -> set[str]:
    paths = getattr(_HELD_LEDGER_LOCKS, "paths", None)
    if paths is None:
        paths = set()
        _HELD_LEDGER_LOCKS.paths = paths
    return paths


def _ledger_lock_file(path: Path | str) -> Path:
    """Lockfile path for a ledger — IDENTICAL to shared.file_lock's convention."""

    target = Path(path)
    return target.with_suffix(target.suffix + ".lock")


@contextlib.contextmanager
def _ledger_lock(
    path: Path | str,
    timeout: float = _LEDGER_LOCK_TIMEOUT_S,
) -> Iterator[None]:
    """Cross-process ledger lock; same-thread nesting is a no-op.

    Interoperates with ``scripts/shared.py``'s ``file_lock``: same
    ``<path>.lock`` lockfile name and the same msvcrt/fcntl non-blocking
    acquire + retry loop, so processes using either helper exclude each
    other. Raises ``TimeoutError`` when the lock cannot be acquired within
    ``timeout`` seconds (mirroring ``shared.file_lock``). OS advisory locks
    self-release on process death, so no manual stale-break is needed —
    also matching shared's semantics. Stdlib-only by design.
    """

    lock_file = _ledger_lock_file(path)
    key = str(lock_file)
    held = _held_ledger_lock_paths()
    if key in held:
        yield
        return
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_file, "w", encoding="utf-8")  # noqa: SIM115
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire lock on {lock_file} within {timeout}s"
                    )
                time.sleep(0.05)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
    finally:
        if acquired:
            if sys.platform == "win32":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# Public name for producers (memory_reflect / memory_weekly / memory_dream)
# that serialize whole pipeline sections around process_amendment_output.
# Producers MUST use this — not shared.file_lock — for the ledger: both take
# the same OS lock on the same lockfile, but only this one registers in the
# reentrancy guard, so the ledger mutations inside the section nest instead
# of deadlocking against the caller's own lock.
ledger_file_lock = _ledger_lock


def _parse_record_line(line: str) -> dict[str, Any] | None:
    """Parse one raw JSONL line into a record dict, or None if unparseable."""

    stripped = line.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


class ProposalLedger:
    """JSONL store for amendment proposals and policy/apply audit fields."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, proposal: AmendmentProposal) -> bool:
        """Append a proposal if its target is valid and not already active.

        The dedupe check and the write happen under the ledger lock so two
        processes cannot both pass the active-key check and append twins.
        """

        if proposal.target_file not in AMENDMENT_TARGETS:
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _ledger_lock(self._path):
            incoming_key = _dedupe_key(
                proposal.target_file, proposal.proposed_content
            )
            if incoming_key in self._active_dedupe_keys():
                return False
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(proposal), ensure_ascii=False) + "\n")
                handle.flush()
        return True

    def read_all(self) -> list[AmendmentProposal]:
        """Return all well-formed proposals from the ledger.

        Records written by an LLM without `id`/`created_at` are healed with
        durable values and rewritten to disk so later status updates match.
        The heal is line-based: unparseable lines are kept verbatim in their
        original position — never deleted.
        """

        self._heal_missing_fields()
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

    def _read_raw_lines(self) -> list[str]:
        """Return the raw ledger lines (no parsing, no filtering)."""

        if not self._path.exists():
            return []
        try:
            with open(self._path, encoding="utf-8") as handle:
                return handle.read().splitlines()
        except OSError:
            return []

    def _iter_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in self._read_raw_lines():
            record = _parse_record_line(line)
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _heal_line(line: str) -> str | None:
        """Return the healed replacement for one raw line, or None to keep it.

        Only parseable JSON-object lines missing ``id``/``created_at`` get a
        replacement; malformed, blank, and complete lines stay byte-identical.
        """

        record = _parse_record_line(line)
        if record is None:
            return None
        healed = False
        if not record.get("id"):
            record["id"] = str(uuid.uuid4())
            healed = True
        if not record.get("created_at"):
            record["created_at"] = datetime.now(UTC).isoformat()
            healed = True
        return json.dumps(record, ensure_ascii=False) if healed else None

    def _heal_missing_fields(self) -> None:
        """Heal id/created_at line-by-line on the RAW file, under the lock.

        Best-effort like the pre-lock heal: lock timeouts and OS errors are
        swallowed so a contended heal degrades to an unhealed read instead of
        crashing the caller; the next reader retries.
        """

        if not any(
            self._heal_line(line) is not None for line in self._read_raw_lines()
        ):
            return
        try:
            with _ledger_lock(self._path):
                healed_any = False
                out_lines: list[str] = []
                for line in self._read_raw_lines():  # re-read under the lock
                    replacement = self._heal_line(line)
                    if replacement is None:
                        out_lines.append(line)
                    else:
                        out_lines.append(replacement)
                        healed_any = True
                if healed_any:
                    _atomic_write_text(
                        self._path,
                        "".join(line + "\n" for line in out_lines),
                    )
        except (OSError, TimeoutError):
            pass

    def _update_record(self, proposal_id: str, updates: dict[str, Any]) -> bool:
        """Update one record by id under the ledger lock.

        Line-based rewrite: only the matched record's line is re-dumped;
        every other line — including unparseable ones — is preserved verbatim.
        """

        with _ledger_lock(self._path):
            found = False
            out_lines: list[str] = []
            for line in self._read_raw_lines():
                record = _parse_record_line(line)
                if record is not None and record.get("id") == proposal_id:
                    record.update(updates)
                    out_lines.append(json.dumps(record, ensure_ascii=False))
                    found = True
                else:
                    out_lines.append(line)
            if found:
                _atomic_write_text(
                    self._path,
                    "".join(line + "\n" for line in out_lines),
                )
            return found

    def _active_dedupe_keys(self) -> set[str]:
        return {
            _dedupe_key(proposal.target_file, proposal.proposed_content)
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


@dataclass(frozen=True)
class CollapseReport:
    """Result of collapsing an Autonomous Amendments section."""

    target_file: str
    blocks_before: int
    blocks_kept: int
    ledger_applied: int
    ledger_superseded: int


def build_amendment_gate_section(
    ledger_file: Path | str,
    *,
    source: str,
    targets: Iterable[str] = AMENDMENT_TARGETS,
    ledger: ProposalLedger | None = None,
    digest_limit: int = 10,
) -> str:
    """Return prompt instructions for policy-gated durable-memory changes."""

    target_list = ", ".join(sorted(normalize_target_file(target) for target in targets))
    section = f"""## Policy-Gated Durable Memory Amendments

Durable identity and memory file changes are autonomous only through the
machine policy gate. Do not directly edit `SELF.md`, `SOUL.md`, `USER.md`, or
`MEMORY.md`; emit bounded JSON amendment records for this ledger instead:

`{Path(ledger_file)}`

Do NOT create or edit the ledger file yourself. Output the JSON records in
your final message only; the runtime appends them to the ledger.

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
    if ledger is None or digest_limit <= 0:
        return section
    try:
        recent = [
            proposal for proposal in ledger.read_all()
            if proposal.status in {"pending", "applied"}
        ][-digest_limit:]
        if not recent:
            return section
        lines = []
        for proposal in recent:
            snippet = " ".join(
                str(proposal.summary or proposal.proposed_content).split()
            )[:100]
            lines.append(f"- [{proposal.status}] {proposal.target_file}: {snippet}")
        return (
            section
            + "\n\n### Recently Proposed (do NOT re-propose these)\n\n"
            + "\n".join(lines)
        )
    except Exception:
        return section


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
    apply_limit: int | None = None,
    section_cap: int = 20,
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
        limit=apply_limit,
        section_cap=section_cap,
    )


def apply_policy_approved_amendments(
    ledger: ProposalLedger,
    memory_dir: Path | str,
    *,
    policy: AmendmentPolicy | None = None,
    limit: int | None = None,
    section_cap: int = 20,
) -> list[AmendmentApplyResult]:
    """Apply pending/approved amendments that pass policy evaluation.

    ``limit`` bounds PHYSICAL target writes per run; reconciled and rejected
    proposals do not consume the budget.
    """

    active_policy = policy or AmendmentPolicy()
    results: list[AmendmentApplyResult] = []
    candidates = [
        proposal for proposal in ledger.read_all()
        if proposal.status in {"pending", "approved"}
    ]

    physical_writes = 0
    for proposal in candidates:
        if limit is not None and physical_writes >= limit:
            break
        result = apply_amendment_if_allowed(
            proposal,
            ledger,
            memory_dir,
            policy=active_policy,
            section_cap=section_cap,
        )
        results.append(result)
        if result.policy_decision == "apply":
            physical_writes += 1
    return results


def apply_amendment_if_allowed(
    proposal: AmendmentProposal,
    ledger: ProposalLedger,
    memory_dir: Path | str,
    *,
    policy: AmendmentPolicy | None = None,
    section_cap: int = 20,
) -> AmendmentApplyResult:
    """Evaluate and apply one amendment proposal if machine policy allows."""

    active_policy = policy or AmendmentPolicy()
    memory_root = Path(memory_dir)
    target = memory_root / proposal.target_file
    before: str | None = None
    if proposal.target_file in AMENDMENT_TARGETS:
        before = _read_text(target)
        if _amendment_already_present(before, proposal):
            now = datetime.now(UTC).isoformat()
            ledger._update_record(
                proposal.id,
                {
                    "status": "applied",
                    "policy_decision": "apply",
                    "policy_reason": "already_present_reconciled",
                    "reviewer": "machine_policy",
                    "reviewed_at": now,
                    "applied_at": now,
                },
            )
            return AmendmentApplyResult(
                proposal_id=proposal.id,
                target_file=proposal.target_file,
                status="applied",
                policy_decision="reconcile",
                policy_reason="already_present_in_target",
            )

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

    if before is None:
        before = _read_text(target)
    before_hash = _sha256(before)
    rollback = _write_rollback_snapshot(
        ledger.path, proposal.target_file, proposal.id, before
    )
    after = _append_autonomous_amendment(before, proposal, section_cap=section_cap)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(after, encoding="utf-8")
    after_hash = _sha256(after)
    applied_at = datetime.now(UTC).isoformat()
    ledger_updated = ledger._update_record(
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
        policy_reason=reason if ledger_updated else "applied_but_ledger_update_failed",
        before_hash=before_hash,
        after_hash=after_hash,
        rollback_snapshot_path=str(rollback),
    )


def collapse_autonomous_amendments(
    target_path: Path | str,
    ledger: ProposalLedger,
    *,
    section_cap: int = 20,
) -> CollapseReport:
    """Collapse duplicate autonomous-amendment blocks and reconcile the ledger.

    Physical target state is the source of truth: duplicate blocks are deduped
    by normalized content (first occurrence wins, bounded by ``section_cap``),
    surviving blocks claim matching pending/approved ledger records as applied,
    and the unclaimed backlog is marked superseded. Running collapse twice is a
    no-op.
    """

    target = Path(target_path)
    # Crash-safe write ordering: (1) plan kept blocks purely from the TARGET
    # text (no ledger access at all — not even read_all's id-heal may touch
    # disk yet), (2) write the rollback snapshot, (3) atomically write the
    # collapsed TARGET, (4) only then touch the ledger (heal + reconcile).
    # If the target write raises, the ledger file is byte-identical to
    # before; a ledger failure AFTER a successful target write is the safe
    # direction — the idempotent already-present reconcile self-heals on the
    # next apply pass. Kept blocks retain their original marker ids; ledger
    # reconciliation is content-based, so no marker rewrite is needed.
    # The whole section holds the ledger lock so producers cannot interleave.
    with _ledger_lock(ledger.path):
        original_text = _read_text(target)
        head, blocks = _split_amendment_section(original_text)

        kept_blocks: list[str] = []
        kept_keys: list[str] = []
        seen: set[str] = set()
        for block in blocks:
            key = _normalize_for_match(_block_content(block))
            if key in seen:
                continue
            seen.add(key)
            kept_blocks.append(block)
            kept_keys.append(key)
        if len(kept_blocks) > section_cap:
            kept_blocks = kept_blocks[-section_cap:]
            kept_keys = kept_keys[-section_cap:]
        planned_keys = set(kept_keys)

        collapsed_text = head + "".join(kept_blocks)
        if collapsed_text != original_text:
            _write_rollback_snapshot(
                ledger.path, str(target_path), "collapse", original_text
            )
            _atomic_write_text(target, collapsed_text)

        # Phase 4 — the ONLY ledger touch point (read_all's id-heal included)
        # runs after the target write succeeded.
        ledger_applied = 0
        ledger_superseded = 0
        claimed: set[str] = set()
        now = datetime.now(UTC).isoformat()
        for proposal in ledger.read_all():
            if proposal.status not in {"pending", "approved"}:
                continue
            key = _normalize_for_match(proposal.proposed_content)
            if key in planned_keys and key not in claimed:
                claimed.add(key)
                if ledger._update_record(
                    proposal.id,
                    {
                        "status": "applied",
                        "reviewer": "collapse_reconcile",
                        "policy_decision": "apply",
                        "policy_reason": "reconciled_by_collapse",
                        "reviewed_at": now,
                        "applied_at": now,
                    },
                ):
                    ledger_applied += 1
            else:
                if ledger._update_record(
                    proposal.id,
                    {
                        "status": "superseded",
                        "reviewer": "collapse_reconcile",
                        "policy_reason": "stale_backlog_collapse",
                        "reviewed_at": now,
                    },
                ):
                    ledger_superseded += 1

    return CollapseReport(
        target_file=str(target_path),
        blocks_before=len(blocks),
        blocks_kept=len(kept_blocks),
        ledger_applied=ledger_applied,
        ledger_superseded=ledger_superseded,
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


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text durably via a sibling tempfile + os.replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        delete=False,
        encoding="utf-8",
        suffix=".tmp",
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except OSError:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_for_match(text: str) -> str:
    return " ".join(str(text).split()).lower()


def _amendment_already_present(target_text: str, proposal: AmendmentProposal) -> bool:
    """True when the proposal's marker id or an equal-content block exists.

    The marker-id check stays whole-file; the content check is per-BLOCK
    normalized EQUALITY against the parsed Autonomous Amendments blocks.
    Substring matching against the whole file falsely reconciled short
    proposals that merely appeared in ordinary prose.
    """

    if f"HOMIE_AUTO_AMENDMENT:{proposal.id}" in target_text:
        return True
    content = _normalize_for_match(proposal.proposed_content)
    if not content:
        return False
    _, blocks = _split_amendment_section(target_text)
    return any(
        _normalize_for_match(_block_content(block)) == content for block in blocks
    )


def _split_amendment_section(text: str) -> tuple[str, list[str]]:
    """Split into (head, amendment blocks); ``head + "".join(blocks) == text``."""

    idx = text.find(_SECTION_HEADER)
    if idx == -1:
        return text, []
    newline = text.find("\n", idx)
    if newline == -1:
        return text, []
    body_start = newline + 1
    parts = re.split(r"(?=<!-- HOMIE_AUTO_AMENDMENT:)", text[body_start:])
    return text[:body_start] + parts[0], parts[1:]


def _block_content(block: str) -> str:
    """Extract the amendment content text from one marker block."""

    newline = block.find("\n")
    body = block[newline + 1:] if newline != -1 else ""
    cut = body.find("\n  - source:")
    if cut != -1:
        body = body[:cut]
    body = body.strip()
    if body.startswith("- "):
        body = body[2:]
    return body.strip()


def _write_rollback_snapshot(
    ledger_path: Path,
    target_file: str,
    record_id: str,
    before: str,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_id = str(record_id).replace("-", "")[:12]
    safe_name = Path(str(target_file)).name
    rollback_dir = ledger_path.parent / "rollback"
    rollback_dir.mkdir(parents=True, exist_ok=True)
    rollback_path = rollback_dir / f"{safe_name}.{timestamp}.{safe_id}.bak"
    rollback_path.write_text(before, encoding="utf-8")
    return rollback_path


def _append_autonomous_amendment(
    before: str,
    proposal: AmendmentProposal,
    *,
    section_cap: int = 20,
) -> str:
    content = proposal.proposed_content.strip()
    marker = f"<!-- HOMIE_AUTO_AMENDMENT:{proposal.id} -->"
    block = (
        f"{marker}\n"
        f"- {content}\n"
        f"  - source: {proposal.source}\n"
        f"  - evidence: {', '.join(proposal.evidence_paths)}\n"
    )
    base = before.rstrip()
    if _SECTION_HEADER not in base:
        appended = f"{base}\n\n{_SECTION_HEADER}\n\n{block}".lstrip()
    else:
        appended = f"{base}\n\n{block}"
    head, blocks = _split_amendment_section(appended)
    if len(blocks) > section_cap:
        blocks = blocks[-section_cap:]
    return head + "".join(blocks)


__all__ = (
    "AMENDMENT_TARGETS",
    "AmendmentApplyResult",
    "AmendmentPolicy",
    "CollapseReport",
    "PROPOSAL_STATUSES",
    "AmendmentProposal",
    "ProposalLedger",
    "apply_amendment_if_allowed",
    "apply_policy_approved_amendments",
    "build_amendment_gate_section",
    "collapse_autonomous_amendments",
    "evaluate_amendment_policy",
    "ledger_file_lock",
    "normalize_target_file",
    "parse_amendment_records",
    "process_amendment_output",
)
