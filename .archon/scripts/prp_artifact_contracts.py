"""Deterministic artifact shape/context validation for the implement-prp DAG.

Extracted, behavior-preserving, from the inline Python previously duplicated
across 10 `bash:` nodes in `.archon/workflows/implement-prp.yaml`
(PRP-WF1-workflow-artifact-contracts.md). This module validates artifacts and
Git context only. Workflow completion and human approval are exclusively
Archon `approval:` node state -- nothing here infers, gates, or represents
them, and there is no `validate_node`/prerequisite authority.

Pure stdlib. No network, no Archon import, no LLM call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath


class Reason(StrEnum):
    ARTIFACT_MISSING = "artifact_missing"
    JSON_INVALID = "json_invalid"
    SCHEMA_INVALID = "schema_invalid"
    FIELD_INVALID = "field_invalid"
    PATH_INVALID = "path_invalid"
    COMMAND_NOT_ALLOWLISTED = "command_not_allowlisted"
    HEAD_REVISION_MISMATCH = "head_revision_mismatch"
    BRANCH_MISMATCH = "branch_mismatch"
    ANCESTRY_MISMATCH = "ancestry_mismatch"
    CHANGED_FILES_MISMATCH = "changed_files_mismatch"
    DIFF_DIGEST_MISMATCH = "diff_digest_mismatch"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: Reason | None
    detail: str
    normalized: object | None = None


@dataclass(frozen=True)
class DiffState:
    changed_files: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class ArtifactSet:
    artifacts_dir: Path
    values: Mapping[str, object]


# Transcription/test metadata only (PRP §7) -- not a second DAG scheduler.
# Archon's `depends_on` edges and list order in implement-prp.yaml remain
# the only authoritative execution graph.
REQUIRED_ARTIFACTS: Mapping[str, tuple[str, ...]] = {
    "worktree-guard": (),
    "preflight": ("baseline",),
    "preflight-gate": ("preflight",),
    "reconnaissance": ("preflight",),
    "reconnaissance-gate": ("reconnaissance",),
    "plan": ("baseline", "preflight", "reconnaissance"),
    "plan-approval": ("plan",),
    "implementation": ("baseline", "preflight", "reconnaissance", "plan"),
    "implementation-gate": ("implementation",),
    "focused-test-fix": ("preflight", "implementation"),
    "focused-test-gate": ("preflight", "baseline"),
    "regression-validation": ("preflight", "baseline"),
    "spec-review": (),
    "security-state-review": (),
    "simplification-review": (),
    "docs-review": (),
    "review-aggregate": (
        "review_spec",
        "review_security_state",
        "review_simplification",
        "review_docs",
    ),
    "review-gate": ("review_aggregate", "focused_results", "regression"),
    "package": ("baseline", "preflight", "focused_results", "regression", "review_aggregate"),
    "package-gate": ("pr_package", "preflight", "baseline", "regression", "pr_body"),
    "final-approval": (),
    "publish-pr": ("pr_package", "baseline", "approval_manifest", "pr_body"),
}

PRODUCED_ARTIFACTS: Mapping[str, tuple[str, ...]] = {
    "worktree-guard": ("baseline",),
    "preflight": ("preflight",),
    "preflight-gate": (),
    "reconnaissance": ("reconnaissance",),
    "reconnaissance-gate": (),
    "plan": ("plan",),
    "plan-approval": (),
    "implementation": ("implementation", "implementation_md"),
    "implementation-gate": (),
    "focused-test-fix": ("focused_results",),
    "focused-test-gate": ("focused_results",),
    "regression-validation": ("regression",),
    "spec-review": ("review_spec",),
    "security-state-review": ("review_security_state",),
    "simplification-review": ("review_simplification",),
    "docs-review": ("review_docs",),
    "review-aggregate": ("review_aggregate",),
    "review-gate": (),
    "package": ("pr_package", "pr_body"),
    "package-gate": ("approval_manifest",),
    "final-approval": (),
    "publish-pr": ("publication",),
}


# ---------------------------------------------------------------------------
# shared shape predicates
# ---------------------------------------------------------------------------


def _is_int_not_bool(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _nonempty_str(x: object) -> bool:
    return isinstance(x, str) and bool(x)


def _str_list(x: object) -> bool:
    return isinstance(x, list) and all(isinstance(i, str) for i in x)


def _nonempty_str_list(x: object) -> bool:
    return isinstance(x, list) and bool(x) and all(isinstance(i, str) for i in x)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _lower_hex64(x: object) -> bool:
    return isinstance(x, str) and bool(_HEX64.match(x))


def _confined_strict(x: object) -> bool:
    """No backslash, no colon, not absolute, no `..` part (preflight-gate rule)."""
    if not isinstance(x, str) or not x or chr(92) in x or ":" in x:
        return False
    p = PurePosixPath(x)
    return not p.is_absolute() and ".." not in p.parts


_ARGV_FAMILIES_UV = ("pytest", "ruff")


def _valid_argv(a: object) -> bool:
    if not (isinstance(a, list) and bool(a) and all(isinstance(x, str) and x for x in a)):
        return False
    return (
        (a[:4] == ["uv", "run", "--extra", "dev"] and len(a) >= 5 and a[4] in _ARGV_FAMILIES_UV)
        or a[:2] == ["npm", "test"]
        or a[:3] == ["npm", "run", "typecheck"]
    )


def _valid_test_spec(s: object) -> bool:
    return (
        isinstance(s, dict)
        and set(s) == {"cwd", "argv"}
        and _confined_strict(s.get("cwd"))
        and _valid_argv(s.get("argv"))
    )


def _valid_run(r: object) -> bool:
    return (
        isinstance(r, dict)
        and set(r) == {"spec", "exit_code", "evidence"}
        and _valid_test_spec(r["spec"])
        and _is_int_not_bool(r["exit_code"])
        and isinstance(r["evidence"], str)
    )


# ---------------------------------------------------------------------------
# canonical (producer-strictness) validators -- §4.1, exact key sets
# ---------------------------------------------------------------------------


def _canon_baseline(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "root",
        "git_dir",
        "common_dir",
        "branch",
        "baseline_head",
        "baseline_status",
    }:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "baseline: unexpected key set")
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "baseline: schema must be integer 1")
    for key in ("root", "git_dir", "common_dir"):
        v = payload[key]
        if not isinstance(v, str) or not v or not Path(v).is_absolute():
            return ValidationResult(
                False, Reason.FIELD_INVALID, f"baseline: {key} must be an absolute path string"
            )
    if not _nonempty_str(payload["branch"]):
        return ValidationResult(False, Reason.FIELD_INVALID, "baseline: branch must be nonempty")
    if not _nonempty_str(payload["baseline_head"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "baseline: baseline_head must be nonempty"
        )
    if payload["baseline_status"] != "":
        return ValidationResult(
            False, Reason.FIELD_INVALID, "baseline: baseline_status must be empty"
        )
    return ValidationResult(True, None, "", normalized=payload)


_PREFLIGHT_DECISIONS = {"proceed", "revise", "escalate", "abort"}


def _canon_preflight(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "decision",
        "prp_path",
        "scope",
        "allowed_paths",
        "focused_tests",
        "regression_tests",
        "blockers",
    }:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "preflight: unexpected key set")
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "preflight: schema must be integer 1")
    if payload["decision"] not in _PREFLIGHT_DECISIONS:
        return ValidationResult(False, Reason.FIELD_INVALID, "preflight: invalid decision")
    if not _confined_strict(payload["prp_path"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "preflight: prp_path must be a confined path"
        )
    if not _nonempty_str(payload["scope"]):
        return ValidationResult(False, Reason.FIELD_INVALID, "preflight: scope must be nonempty")
    allowed = payload["allowed_paths"]
    if (
        not isinstance(allowed, list)
        or not allowed
        or not all(_confined_strict(x) for x in allowed)
    ):
        return ValidationResult(False, Reason.FIELD_INVALID, "preflight: invalid allowed_paths")
    for key in ("focused_tests", "regression_tests"):
        specs = payload[key]
        if not isinstance(specs, list) or not specs or not all(_valid_test_spec(s) for s in specs):
            return ValidationResult(False, Reason.FIELD_INVALID, f"preflight: invalid {key}")
    if not _str_list(payload["blockers"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "preflight: blockers must be a string array"
        )
    if payload["decision"] == "proceed" and payload["blockers"]:
        return ValidationResult(
            False, Reason.FIELD_INVALID, "preflight: proceed requires empty blockers"
        )
    return ValidationResult(True, None, "", normalized=payload)


_RECON_STATUSES = {"ready", "revise", "escalate", "abort"}


def _canon_reconnaissance(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "status",
        "files",
        "invariants",
        "risks",
        "evidence",
    }:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "reconnaissance: unexpected key set")
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "reconnaissance: schema must be integer 1"
        )
    if payload["status"] not in _RECON_STATUSES:
        return ValidationResult(False, Reason.FIELD_INVALID, "reconnaissance: invalid status")
    for key in ("files", "invariants", "risks", "evidence"):
        if not _nonempty_str_list(payload[key]):
            return ValidationResult(
                False,
                Reason.FIELD_INVALID,
                f"reconnaissance: {key} must be a nonempty string array",
            )
    return ValidationResult(True, None, "", normalized=payload)


_IMPL_STATUSES = {"ready", "incomplete", "escalate"}


def _canon_implementation(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "status",
        "red_green_evidence",
        "changed_files",
        "blockers",
    }:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "implementation: unexpected key set")
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "implementation: schema must be integer 1"
        )
    if payload["status"] not in _IMPL_STATUSES:
        return ValidationResult(False, Reason.FIELD_INVALID, "implementation: invalid status")
    if not isinstance(payload["red_green_evidence"], list):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "implementation: red_green_evidence must be an array"
        )
    if not _str_list(payload["changed_files"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "implementation: changed_files must be a string array"
        )
    if not _str_list(payload["blockers"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "implementation: blockers must be a string array"
        )
    ready_iff = bool(payload["red_green_evidence"]) and not payload["blockers"]
    if (payload["status"] == "ready") != ready_iff:
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "implementation: status ready iff RED/GREEN evidence is nonempty and blockers is empty",
        )
    return ValidationResult(True, None, "", normalized=payload)


_FOCUSED_STATUSES = {"pass", "fail", "escalate"}


def _canon_focused_results(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {"schema", "status", "runs", "blockers"}:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "focused_results: unexpected key set")
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "focused_results: schema must be integer 1"
        )
    if payload["status"] not in _FOCUSED_STATUSES:
        return ValidationResult(False, Reason.FIELD_INVALID, "focused_results: invalid status")
    runs = payload["runs"]
    if not isinstance(runs, list) or not all(_valid_run(r) for r in runs):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "focused_results: runs must be an array of valid entries"
        )
    if not _str_list(payload["blockers"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "focused_results: blockers must be a string array"
        )
    pass_iff = bool(runs) and all(r["exit_code"] == 0 for r in runs) and not payload["blockers"]
    if (payload["status"] == "pass") != pass_iff:
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "focused_results: status pass iff runs nonempty, every exit_code 0, blockers empty",
        )
    return ValidationResult(True, None, "", normalized=payload)


_REGRESSION_STATUSES = {"pass", "fail"}


def _canon_regression(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "status",
        "runs",
        "skipped",
        "blockers",
        "changed_files",
        "validated_diff_digest",
    }:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "regression: unexpected key set")
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "regression: schema must be integer 1"
        )
    if payload["status"] not in _REGRESSION_STATUSES:
        return ValidationResult(False, Reason.FIELD_INVALID, "regression: invalid status")
    runs = payload["runs"]
    if not isinstance(runs, list) or not all(_valid_run(r) for r in runs):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "regression: runs must be an array of valid entries"
        )
    if not _str_list(payload["skipped"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "regression: skipped must be a string array"
        )
    if not _str_list(payload["blockers"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "regression: blockers must be a string array"
        )
    if not _str_list(payload["changed_files"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "regression: changed_files must be a string array"
        )
    if not _lower_hex64(payload["validated_diff_digest"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "regression: validated_diff_digest must be lowercase 64-hex",
        )
    pass_iff = bool(runs) and all(r["exit_code"] == 0 for r in runs)
    if (payload["status"] == "pass") != pass_iff:
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "regression: status pass iff runs is nonempty and every exit_code is 0",
        )
    return ValidationResult(True, None, "", normalized=payload)


_REVIEW_VERDICTS = {"pass", "block"}
_FINDING_SEVERITIES = {"blocking", "advisory"}


def _valid_finding(f: object) -> bool:
    return (
        isinstance(f, dict)
        and set(f) == {"severity", "path", "evidence", "remedy"}
        and f["severity"] in _FINDING_SEVERITIES
        and all(isinstance(f[k], str) for k in ("path", "evidence", "remedy"))
    )


def _canon_review(payload: object, *, enforce_block_iff: bool) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "verdict",
        "findings",
        "evidence",
    }:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "review: unexpected key set")
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "review: schema must be integer 1")
    if payload["verdict"] not in _REVIEW_VERDICTS:
        return ValidationResult(False, Reason.FIELD_INVALID, "review: invalid verdict")
    findings = payload["findings"]
    if not isinstance(findings, list) or not all(_valid_finding(f) for f in findings):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "review: findings must be an array of valid entries"
        )
    if not isinstance(payload["evidence"], list):
        return ValidationResult(False, Reason.FIELD_INVALID, "review: evidence must be an array")
    if enforce_block_iff:
        has_blocking = any(f["severity"] == "blocking" for f in findings)
        if (payload["verdict"] == "block") != has_blocking:
            return ValidationResult(
                False,
                Reason.FIELD_INVALID,
                "review: verdict block iff a blocking finding exists",
            )
    return ValidationResult(True, None, "", normalized=payload)


def _canon_review_spec(payload: object) -> ValidationResult:
    return _canon_review(payload, enforce_block_iff=True)


def _canon_review_security_state(payload: object) -> ValidationResult:
    return _canon_review(payload, enforce_block_iff=True)


def _canon_review_simplification(payload: object) -> ValidationResult:
    return _canon_review(payload, enforce_block_iff=True)


def _canon_review_docs(payload: object) -> ValidationResult:
    return _canon_review(payload, enforce_block_iff=False)


_REVIEW_NAMES = ("spec", "security-state", "simplification", "docs")


def _canon_review_aggregate(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {"schema", "verdict", "reviews"}:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "review_aggregate: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "review_aggregate: schema must be integer 1"
        )
    reviews = payload["reviews"]
    if (
        not isinstance(reviews, dict)
        or set(reviews) != set(_REVIEW_NAMES)
        or not all(v in _REVIEW_VERDICTS for v in reviews.values())
    ):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "review_aggregate: invalid reviews map"
        )
    if payload["verdict"] not in _REVIEW_VERDICTS:
        return ValidationResult(False, Reason.FIELD_INVALID, "review_aggregate: invalid verdict")
    expect_pass = all(v == "pass" for v in reviews.values())
    if (payload["verdict"] == "pass") != expect_pass:
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "review_aggregate: verdict pass iff every review passes",
        )
    return ValidationResult(True, None, "", normalized=payload)


def _canon_pr_package(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "status",
        "title",
        "commit_message",
        "branch",
        "body_file",
        "changed_files",
        "test_evidence",
    }:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "pr_package: unexpected key set")
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "pr_package: schema must be integer 1"
        )
    if payload["status"] != "packaged":
        return ValidationResult(False, Reason.FIELD_INVALID, "pr_package: status must be packaged")
    for key in ("title", "commit_message"):
        v = payload[key]
        if not isinstance(v, str) or not v.strip():
            return ValidationResult(
                False, Reason.FIELD_INVALID, f"pr_package: {key} must be nonblank"
            )
    if not _nonempty_str(payload["branch"]):
        return ValidationResult(False, Reason.FIELD_INVALID, "pr_package: branch must be nonempty")
    if payload["body_file"] != "pr-body.md":
        return ValidationResult(
            False, Reason.FIELD_INVALID, "pr_package: body_file must be pr-body.md"
        )
    if not _nonempty_str_list(payload["changed_files"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "pr_package: changed_files must be a nonempty string array"
        )
    if not isinstance(payload["test_evidence"], list):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "pr_package: test_evidence must be an array"
        )
    return ValidationResult(True, None, "", normalized=payload)


def _canon_approval_manifest(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "baseline_head",
        "branch",
        "changed_files",
        "approved_diff_digest",
    }:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "approval_manifest: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "approval_manifest: schema must be integer 1"
        )
    if not _nonempty_str(payload["baseline_head"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "approval_manifest: baseline_head must be nonempty"
        )
    if not _nonempty_str(payload["branch"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "approval_manifest: branch must be nonempty"
        )
    changed = payload["changed_files"]
    if not _str_list(changed) or not all(x for x in changed) or changed != sorted(changed):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "approval_manifest: changed_files must be sorted nonempty strings",
        )
    if not _lower_hex64(payload["approved_diff_digest"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "approval_manifest: approved_diff_digest must be lowercase 64-hex",
        )
    return ValidationResult(True, None, "", normalized=payload)


_PR_URL = re.compile(r"https://github\.com/[^/]+/[^/]+/pull/[1-9][0-9]*")


def _canon_publication(payload: object) -> ValidationResult:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "status",
        "branch",
        "commit",
        "url",
    }:
        return ValidationResult(False, Reason.SCHEMA_INVALID, "publication: unexpected key set")
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "publication: schema must be integer 1"
        )
    if payload["status"] != "published":
        return ValidationResult(
            False, Reason.FIELD_INVALID, "publication: status must be published"
        )
    if not _nonempty_str(payload["branch"]):
        return ValidationResult(False, Reason.FIELD_INVALID, "publication: branch must be nonempty")
    if not _nonempty_str(payload["commit"]):
        return ValidationResult(False, Reason.FIELD_INVALID, "publication: commit must be nonempty")
    if not isinstance(payload["url"], str) or not _PR_URL.fullmatch(payload["url"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "publication: url must be a GitHub PR URL"
        )
    return ValidationResult(True, None, "", normalized=payload)


_CANONICAL_VALIDATORS = {
    "baseline": _canon_baseline,
    "preflight": _canon_preflight,
    "reconnaissance": _canon_reconnaissance,
    "implementation": _canon_implementation,
    "focused_results": _canon_focused_results,
    "regression": _canon_regression,
    "review_spec": _canon_review_spec,
    "review_security_state": _canon_review_security_state,
    "review_simplification": _canon_review_simplification,
    "review_docs": _canon_review_docs,
    "review_aggregate": _canon_review_aggregate,
    "pr_package": _canon_pr_package,
    "approval_manifest": _canon_approval_manifest,
    "publication": _canon_publication,
}


# ---------------------------------------------------------------------------
# consumer policies (§5) -- literal transcriptions of the current inline
# gate logic; each preserves the exact permissiveness of its source node.
# ---------------------------------------------------------------------------


def _consumer_preflight_gate(d: object) -> ValidationResult:
    specs = (d.get("focused_tests"), d.get("regression_tests"))
    allowed = d.get("allowed_paths")

    valid_paths = (
        isinstance(allowed, list)
        and bool(allowed)
        and all(
            isinstance(x, str)
            and x
            and chr(92) not in x
            and ":" not in x
            and not PurePosixPath(x).is_absolute()
            and ".." not in PurePosixPath(x).parts
            for x in allowed
        )
    )
    ok = (
        d.get("schema") == 1
        and d.get("decision") == "proceed"
        and not d.get("blockers")
        and valid_paths
        and all(isinstance(x, list) and x and all(_valid_test_spec(s) for s in x) for x in specs)
    )
    return ValidationResult(ok, None if ok else Reason.FIELD_INVALID, "")


def _consumer_reconnaissance_gate(d: object) -> ValidationResult:
    ok = not (
        d.get("schema") != 1
        or d.get("status") != "ready"
        or not d.get("files")
        or not d.get("evidence")
    )
    return ValidationResult(
        ok, None if ok else Reason.FIELD_INVALID, "" if ok else "reconnaissance not ready"
    )


def _consumer_implementation_gate(d: object) -> ValidationResult:
    ok = not (d.get("schema") != 1 or d.get("status") != "ready" or not d.get("red_green_evidence"))
    return ValidationResult(
        ok, None if ok else Reason.FIELD_INVALID, "" if ok else "implementation is not ready"
    )


def _consumer_review_aggregate_item(d: object) -> ValidationResult:
    ok = not (
        d.get("schema") != 1
        or d.get("verdict") not in ["pass", "block"]
        or not isinstance(d.get("findings"), list)
    )
    return ValidationResult(ok, None if ok else Reason.FIELD_INVALID, "")


def _consumer_review_gate_aggregate(a: object) -> ValidationResult:
    ok = not (
        a.get("schema") != 1
        or a.get("verdict") != "pass"
        or set(a.get("reviews", {}).values()) != {"pass"}
    )
    return ValidationResult(
        ok,
        None if ok else Reason.FIELD_INVALID,
        "" if ok else "review aggregate blocked; abandon run and restart after remediation",
    )


def _consumer_review_gate_focused(t: object) -> ValidationResult:
    ok = not (t.get("status") != "pass" or any(x.get("exit_code") != 0 for x in t.get("runs", [])))
    return ValidationResult(
        ok, None if ok else Reason.FIELD_INVALID, "" if ok else "focused tests failed"
    )


def _consumer_review_gate_regression(r: object) -> ValidationResult:
    ok = not (
        r.get("status") != "pass"
        or r.get("skipped")
        or any(x.get("exit_code") != 0 for x in r.get("runs", []))
    )
    return ValidationResult(
        ok, None if ok else Reason.FIELD_INVALID, "" if ok else "regression tests failed"
    )


_CONSUMER_VALIDATORS = {
    ("preflight", "preflight_gate"): _consumer_preflight_gate,
    ("reconnaissance", "reconnaissance_gate"): _consumer_reconnaissance_gate,
    ("implementation", "implementation_gate"): _consumer_implementation_gate,
    ("review_spec", "review_aggregate"): _consumer_review_aggregate_item,
    ("review_security_state", "review_aggregate"): _consumer_review_aggregate_item,
    ("review_simplification", "review_aggregate"): _consumer_review_aggregate_item,
    ("review_docs", "review_aggregate"): _consumer_review_aggregate_item,
    ("review_aggregate", "review_gate"): _consumer_review_gate_aggregate,
    ("focused_results", "review_gate"): _consumer_review_gate_focused,
    ("regression", "review_gate"): _consumer_review_gate_regression,
}


def validate_payload(kind: str, payload: object, *, policy: str = "canonical") -> ValidationResult:
    if policy == "canonical":
        fn = _CANONICAL_VALIDATORS.get(kind)
        if fn is None:
            return ValidationResult(False, Reason.SCHEMA_INVALID, f"unknown artifact kind: {kind}")
        return fn(payload)
    fn = _CONSUMER_VALIDATORS.get((kind, policy))
    if fn is None:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, f"unknown policy {policy!r} for kind {kind!r}"
        )
    return fn(payload)


# ---------------------------------------------------------------------------
# §9 execution-time argv/path validation (focused-test-gate, regression-validation)
# ---------------------------------------------------------------------------


def validate_argv(spec: object, repo_root: Path) -> ValidationResult:
    if (
        not isinstance(spec, dict)
        or set(spec) != {"cwd", "argv"}
        or not isinstance(spec["cwd"], str)
        or not spec["cwd"]
    ):
        return ValidationResult(False, Reason.FIELD_INVALID, "invalid test spec")
    cwd = (repo_root / spec["cwd"]).resolve()
    try:
        cwd.relative_to(repo_root)
    except ValueError:
        return ValidationResult(False, Reason.PATH_INVALID, "test cwd escapes repository")
    argv = spec["argv"]
    if not _valid_argv(argv):
        return ValidationResult(
            False, Reason.COMMAND_NOT_ALLOWLISTED, "test argv is not allowlisted"
        )
    return ValidationResult(True, None, "", normalized=(cwd, argv))


# ---------------------------------------------------------------------------
# §8.1 / §8.2 diff computations -- two distinct algorithms, never merged
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: object, *, check: bool = True, text: bool = True):
    return subprocess.run(args, cwd=cwd, text=text, capture_output=True, check=check, shell=False)


def compute_regression_diff_state(repo_root: Path, head: str) -> DiffState:
    tracked = _run_git(["git", "diff", "--name-only", "-z", head], repo_root).stdout
    extra_for_changed = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    ).stdout
    changed = sorted(x for x in (tracked + extra_for_changed).split("\0") if x)
    digest = hashlib.sha256()
    digest.update(_run_git(["git", "diff", "--binary", head, "--"], repo_root, text=False).stdout)
    extra_for_hash = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    ).stdout
    for path in extra_for_hash.split("\0"):
        if path:
            digest.update(path.encode() + b"\0" + (repo_root / path).read_bytes() + b"\0")
    return DiffState(changed_files=tuple(changed), digest=digest.hexdigest())


def compute_publication_diff_state(repo_root: Path, base: str) -> DiffState:
    return DiffState(
        changed_files=_publication_changed_files(repo_root, base),
        digest=_publication_digest(repo_root, base),
    )


def _publication_changed_files(repo_root: Path, base: str) -> tuple[str, ...]:
    tracked = _run_git(["git", "diff", "--name-only", "-z", base], repo_root).stdout.split("\0")
    extra = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    ).stdout.split("\0")
    return tuple(sorted({x.replace("\\", "/") for x in tracked + extra if x}))


def _publication_digest(repo_root: Path, base: str) -> str:
    extra = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    ).stdout.split("\0")
    digest = hashlib.sha256()
    digest.update(_run_git(["git", "diff", "--binary", base, "--"], repo_root, text=False).stdout)
    for p in sorted(set(extra) - {""}):
        digest.update(p.encode() + b"\0" + (repo_root / p).read_bytes() + b"\0")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# §10.1 package-gate / §10.2 publish-pr -- ordered context checks
# ---------------------------------------------------------------------------


def validate_package_context(state: ArtifactSet) -> ValidationResult:
    values = state.values
    pkg = values["pkg"]
    pre = values["preflight"]
    base = values["baseline"]
    regression = values["regression"]
    root = base["root"]

    branch = _run_git(["git", "symbolic-ref", "--short", "HEAD"], root).stdout.strip()
    head = _run_git(["git", "rev-parse", "HEAD"], root).stdout.strip()
    if head != base["baseline_head"] or branch != base["branch"]:
        return ValidationResult(
            False,
            Reason.HEAD_REVISION_MISMATCH,
            "baseline HEAD or branch changed before approval",
        )
    if _run_git(
        ["git", "merge-base", "--is-ancestor", base["baseline_head"], head], root, check=False
    ).returncode:
        return ValidationResult(False, Reason.ANCESTRY_MISMATCH, "baseline is not an ancestor")

    required = {
        "schema",
        "status",
        "title",
        "commit_message",
        "branch",
        "body_file",
        "changed_files",
        "test_evidence",
    }
    if (
        set(pkg) != required
        or pkg["schema"] != 1
        or pkg["status"] != "packaged"
        or pkg["branch"] != branch
        or pkg["body_file"] != "pr-body.md"
        or not all(isinstance(pkg[x], str) and pkg[x].strip() for x in ("title", "commit_message"))
    ):
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "invalid package schema/status/branch"
        )

    body = Path(state.artifacts_dir) / "pr-body.md"
    if not body.is_file() or not body.read_text().strip():
        return ValidationResult(False, Reason.FIELD_INVALID, "missing/empty PR body")

    repo_root = Path(root)
    changed = list(_publication_changed_files(repo_root, base["baseline_head"]))

    allowed = pre.get("allowed_paths")
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(
            not isinstance(x, str)
            or not x
            or PurePosixPath(x).is_absolute()
            or ".." in PurePosixPath(x).parts
            for x in allowed
        )
    ):
        return ValidationResult(False, Reason.PATH_INVALID, "invalid preflight allowed_paths")

    def in_scope(p: str) -> bool:
        return any(p == a.rstrip("/") or p.startswith(a.rstrip("/") + "/") for a in allowed)

    if (
        not changed
        or any(not in_scope(p) for p in changed)
        or sorted(pkg["changed_files"]) != changed
    ):
        return ValidationResult(
            False,
            Reason.CHANGED_FILES_MISMATCH,
            "changed paths violate preflight scope or package",
        )

    digest = _publication_digest(repo_root, base["baseline_head"])
    if (
        regression.get("validated_diff_digest") != digest
        or regression.get("changed_files") != changed
    ):
        return ValidationResult(
            False,
            Reason.DIFF_DIGEST_MISMATCH,
            "code changed after deterministic regression validation",
        )

    manifest = {
        "schema": 1,
        "baseline_head": head,
        "branch": branch,
        "changed_files": changed,
        "approved_diff_digest": digest,
    }
    return ValidationResult(True, None, "", normalized=manifest)


def validate_publish_context(state: ArtifactSet) -> ValidationResult:
    values = state.values
    pkg = values["pkg"]
    base = values["baseline"]
    approved = values["approved"]
    root = base["root"]

    branch = _run_git(["git", "symbolic-ref", "--short", "HEAD"], root).stdout.strip()
    head = _run_git(["git", "rev-parse", "HEAD"], root).stdout.strip()
    if head != base["baseline_head"] or approved.get("baseline_head") != head:
        return ValidationResult(
            False,
            Reason.HEAD_REVISION_MISMATCH,
            "HEAD changed since baseline/approval (agent commits forbidden)",
        )
    if branch != base["branch"] or branch != pkg["branch"] or branch != approved.get("branch"):
        return ValidationResult(False, Reason.BRANCH_MISMATCH, "branch/package mismatch")
    if _run_git(
        ["git", "merge-base", "--is-ancestor", base["baseline_head"], head], root, check=False
    ).returncode:
        return ValidationResult(False, Reason.ANCESTRY_MISMATCH, "baseline ancestry failed")

    repo_root = Path(root)
    changed = list(_publication_changed_files(repo_root, head))

    if changed != approved.get("changed_files") or changed != sorted(pkg.get("changed_files", [])):
        return ValidationResult(
            False,
            Reason.CHANGED_FILES_MISMATCH,
            "changed files differ from approved package",
        )

    digest = _publication_digest(repo_root, head)
    if digest != approved.get("approved_diff_digest"):
        return ValidationResult(False, Reason.DIFF_DIGEST_MISMATCH, "approved diff digest changed")

    if not changed:
        return ValidationResult(False, Reason.CHANGED_FILES_MISMATCH, "nothing to publish")

    return ValidationResult(True, None, "", normalized=changed)


# ---------------------------------------------------------------------------
# optional CLI (§11) -- local dev/test convenience, not wired into any node
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prp_artifact_contracts")
    sub = parser.add_subparsers(dest="cmd", required=True)

    vp = sub.add_parser("validate-payload")
    vp.add_argument("--kind", required=True)
    vp.add_argument("--policy", default="canonical")
    vp.add_argument("--file", required=True)

    rd = sub.add_parser("regression-diff-state")
    rd.add_argument("--repo-root", required=True)
    rd.add_argument("--head", required=True)

    pd = sub.add_parser("publication-diff-state")
    pd.add_argument("--repo-root", required=True)
    pd.add_argument("--base", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 64

    try:
        if args.cmd == "validate-payload":
            file_path = Path(args.file)
            if not file_path.is_file():
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "reason": Reason.ARTIFACT_MISSING.value,
                            "detail": f"missing artifact: {file_path}",
                        },
                        sort_keys=True,
                    )
                )
                return 2
            try:
                payload = json.loads(file_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                print(
                    json.dumps(
                        {"ok": False, "reason": Reason.JSON_INVALID.value, "detail": str(exc)},
                        sort_keys=True,
                    )
                )
                return 2
            result = validate_payload(args.kind, payload, policy=args.policy)
            print(
                json.dumps(
                    {
                        "ok": result.ok,
                        "reason": result.reason.value if result.reason else None,
                        "detail": result.detail,
                    },
                    sort_keys=True,
                )
            )
            return 0 if result.ok else 2
        if args.cmd == "regression-diff-state":
            diff_state = compute_regression_diff_state(Path(args.repo_root), args.head)
            print(
                json.dumps(
                    {"changed_files": list(diff_state.changed_files), "digest": diff_state.digest},
                    sort_keys=True,
                )
            )
            return 0
        if args.cmd == "publication-diff-state":
            diff_state = compute_publication_diff_state(Path(args.repo_root), args.base)
            print(
                json.dumps(
                    {"changed_files": list(diff_state.changed_files), "digest": diff_state.digest},
                    sort_keys=True,
                )
            )
            return 0
    except Exception as exc:  # internal error, never a contract verdict
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    return 64


if __name__ == "__main__":
    sys.exit(main())
