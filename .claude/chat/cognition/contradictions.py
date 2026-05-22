"""Bounded contradiction and roadmap-drift detection.

The first detector is intentionally deterministic and conservative. It looks
for documentation that claims a cognitive-loop subsystem is live while the
code-backed status collector reports a non-live state, then emits source-path
findings for human resolution.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FINDING_STATUSES = frozenset({"open", "resolved", "dismissed"})
FINDING_TYPES = frozenset({"contradiction", "roadmap_drift"})
DEFAULT_SCAN_TARGETS = (
    "WORKBOARD.md",
    "PRDs/active",
    "PRPs/active",
)

_SAFE_LINE_RE = re.compile(
    r"\b(still|remains|remain|not|future|planned|deferred|shadow_only|"
    r"shadow-only|partial|drift|missing|unproven|not proven)\b",
    re.IGNORECASE,
)

_LIVE_CLAIM_RE = re.compile(
    r"\b(live|shipped|done|complete|completed|closed|green|proven|ready)\b",
    re.IGNORECASE,
)


@dataclass
class DriftFinding:
    """A bounded finding requiring human review."""

    id: str = ""
    created_at: str = ""
    finding_type: str = "roadmap_drift"
    severity: str = "medium"
    subsystem: str = ""
    current_state: str = ""
    summary: str = ""
    source_paths: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    status: str = "open"
    detector: str = "cognitive_loop_status"
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()
        if self.finding_type not in FINDING_TYPES:
            self.finding_type = "roadmap_drift"
        if self.status not in FINDING_STATUSES:
            self.status = "open"
        self.source_paths = [str(path) for path in self.source_paths]
        self.evidence = [str(item) for item in self.evidence]
        if not self.dedupe_key:
            self.dedupe_key = _dedupe_key(
                self.finding_type,
                self.subsystem,
                self.current_state,
                *self.source_paths,
                *self.evidence,
            )


class DriftLedger:
    """Append-only JSONL store for contradiction/drift findings."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, finding: DriftFinding) -> bool:
        """Append an open finding if it is not already active."""

        if finding.dedupe_key in self._active_dedupe_keys():
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(finding), ensure_ascii=False) + "\n")
            handle.flush()
        return True

    def read_all(self) -> list[DriftFinding]:
        findings: list[DriftFinding] = []
        for record in self._iter_records():
            finding = _coerce_dataclass(DriftFinding, record)
            if finding is not None:
                findings.append(finding)
        return findings

    def read_open(self) -> list[DriftFinding]:
        return [finding for finding in self.read_all() if finding.status == "open"]

    def count_open(self) -> int:
        return len(self.read_open())

    def mark_status(self, finding_id: str, *, status: str) -> bool:
        if status not in {"resolved", "dismissed"}:
            return False
        return self._update_record(finding_id, {"status": status})

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

    def _update_record(self, finding_id: str, updates: dict[str, Any]) -> bool:
        records = self._iter_records()
        found = False
        for record in records:
            if record.get("id") == finding_id:
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
            finding.dedupe_key
            for finding in self.read_all()
            if finding.status == "open"
        }


def detect_cognitive_loop_drift(
    project_root: Path | str,
    cognitive_loop_status: dict[str, Any],
    *,
    max_findings: int = 5,
) -> list[DriftFinding]:
    """Detect docs that over-claim non-live cognitive-loop subsystems."""

    root = Path(project_root)
    subsystem_states = _extract_subsystem_states(cognitive_loop_status)
    rules = _status_rules(subsystem_states)
    findings: list[DriftFinding] = []

    if not rules:
        return findings

    for path in _iter_scan_files(root):
        rel_path = _relative_path(path, root)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            if not _LIVE_CLAIM_RE.search(line) or _SAFE_LINE_RE.search(line):
                continue
            for subsystem, label, state, pattern in rules:
                if not pattern.search(line):
                    continue
                source = f"{rel_path}:{line_no}"
                findings.append(
                    DriftFinding(
                        finding_type="roadmap_drift",
                        severity="medium",
                        subsystem=subsystem,
                        current_state=state,
                        summary=(
                            f"{label} is currently {state}, but {source} "
                            "claims a live/completed state."
                        ),
                        source_paths=[source],
                        evidence=[line.strip()],
                    )
                )
                if len(findings) >= max_findings:
                    return findings
    return findings


def build_drift_detection_section(
    project_root: Path | str,
    cognitive_loop_status: dict[str, Any],
    *,
    max_findings: int = 5,
) -> str:
    """Render deterministic drift findings for scheduled prompts."""

    findings = detect_cognitive_loop_drift(
        project_root,
        cognitive_loop_status,
        max_findings=max_findings,
    )
    if not findings:
        return (
            "## Cognitive Loop Drift Findings\n\n"
            "No deterministic cognitive-loop roadmap drift findings were detected."
        )

    lines = ["## Cognitive Loop Drift Findings"]
    for finding in findings:
        source = ", ".join(finding.source_paths)
        evidence = finding.evidence[0] if finding.evidence else ""
        lines.append(
            f"- [{finding.severity}] {finding.summary} "
            f"Source: {source}. Evidence: {evidence}"
        )
    return "\n".join(lines)


def _extract_subsystem_states(
    cognitive_loop_status: dict[str, Any],
) -> dict[str, str]:
    subsystems = cognitive_loop_status.get("subsystems", {})
    if not isinstance(subsystems, dict):
        return {}
    states: dict[str, str] = {}
    for name, value in subsystems.items():
        if isinstance(value, dict):
            states[str(name)] = str(value.get("state", "unknown"))
    return states


def _status_rules(
    states: dict[str, str],
) -> list[tuple[str, str, str, re.Pattern[str]]]:
    rules: list[tuple[str, str, str, re.Pattern[str]]] = []
    rule_specs = {
        "working_memory": (
            "WorkingMemory production ownership",
            r"\b(WorkingMemory|working memory|full living loop|full mental loop)\b",
        ),
        "self_amendment": (
            "Self-amendment proposal ledger",
            r"\b(self[- ]amendment|self[- ]evolution|proposal ledger)\b",
        ),
        "contradiction_detection": (
            "Contradiction/drift detector",
            r"\b(contradiction|drift detector|roadmap[- ]drift)\b",
        ),
    }
    for subsystem, (label, pattern) in rule_specs.items():
        state = states.get(subsystem)
        if not state or state == "live":
            continue
        rules.append((subsystem, label, state, re.compile(pattern, re.IGNORECASE)))
    return rules


def _iter_scan_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for target in DEFAULT_SCAN_TARGETS:
        path = root / target
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.md")))
    return files


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


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
    "DEFAULT_SCAN_TARGETS",
    "FINDING_STATUSES",
    "FINDING_TYPES",
    "DriftFinding",
    "DriftLedger",
    "build_drift_detection_section",
    "detect_cognitive_loop_drift",
)
