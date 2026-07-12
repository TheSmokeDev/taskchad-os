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
    # PRP-WF1B §8/§9 -- mirror prp_publication_binding.PublicationReason
    # exactly (same names, same snake_case values) so validate_package_context
    # / validate_publish_context can translate a PublicationResult into a
    # ValidationResult via `Reason(result.reason.value)` without inventing a
    # second vocabulary for the same failure modes.
    ARTIFACT_INVALID = "artifact_invalid"
    REPOSITORY_IDENTITY_MISMATCH = "repository_identity_mismatch"
    BASELINE_REVISION_MISMATCH = "baseline_revision_mismatch"
    PACKAGE_INVALID = "package_invalid"
    CHANGED_PATHS_MISMATCH = "changed_paths_mismatch"
    REGRESSION_STATE_MISMATCH = "regression_state_mismatch"
    PUBLICATION_LOCKED = "publication_locked"
    PROSPECTIVE_TREE_INVALID = "prospective_tree_invalid"
    GIT_OBJECT_STATE_MISMATCH = "git_object_state_mismatch"
    PROSPECTIVE_COMMIT_INVALID = "prospective_commit_invalid"
    TRANSACTION_SEAL_FAILED = "transaction_seal_failed"
    PACKAGE_CHANGED_DURING_SEAL = "package_changed_during_seal"
    APPROVAL_BINDING_INVALID = "approval_binding_invalid"
    LOCAL_REF_DIVERGED = "local_ref_diverged"
    REMOTE_DIVERGED = "remote_diverged"
    APPROVED_CONTENT_CHANGED = "approved_content_changed"
    SEALED_TRANSACTION_INVALID = "sealed_transaction_invalid"
    COMMIT_TRANSITION_INVALID = "commit_transition_invalid"
    PRE_SIDE_EFFECT_REVALIDATION_FAILED = "pre_side_effect_revalidation_failed"
    OBJECT_INSTALL_FAILED = "object_install_failed"
    LOCAL_REF_UPDATE_FAILED = "local_ref_update_failed"
    PUSH_FAILED = "push_failed"
    REMOTE_VERIFICATION_FAILED = "remote_verification_failed"
    REMOTE_PR_CONFLICT = "remote_pr_conflict"
    REMOTE_PR_AMBIGUOUS = "remote_pr_ambiguous"
    PR_CREATE_FAILED = "pr_create_failed"
    REMOTE_PR_BODY_MISMATCH = "remote_pr_body_mismatch"
    PUBLICATION_RESULT_INVALID = "publication_result_invalid"
    PR_STATE_IMPOSSIBLE = "pr_state_impossible"
    REMOTE_CREATION_UNSUPPORTED = "remote_creation_unsupported"


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
    "package": (
        "baseline",
        "preflight",
        "focused_results",
        "regression",
        "review_aggregate",
    ),
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
    if not (
        isinstance(a, list) and bool(a) and all(isinstance(x, str) and x for x in a)
    ):
        return False
    return (
        (
            a[:4] == ["uv", "run", "--extra", "dev"]
            and len(a) >= 5
            and a[4] in _ARGV_FAMILIES_UV
        )
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
    """Schema-2 only (PRP-WF1B §3.2/§4/§11): `baseline.json` now records the
    `RepositoryIdentity` fields 1:1. This is a fail-closed cutover -- schema 1
    is rejected outright, never implicitly upgraded."""
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "root",
        "git_dir",
        "common_dir",
        "worktree_id",
        "object_format",
        "branch_ref",
        "baseline_commit",
    }:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "baseline: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 2:
        return ValidationResult(
            False,
            Reason.SCHEMA_INVALID,
            "baseline: schema 1 is rejected, no implicit upgrade",
        )
    for key in ("root", "git_dir", "common_dir"):
        v = payload[key]
        if not isinstance(v, str) or not v or not Path(v).is_absolute():
            return ValidationResult(
                False,
                Reason.FIELD_INVALID,
                f"baseline: {key} must be an absolute path string",
            )
    if not _lower_hex64(payload["worktree_id"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "baseline: worktree_id must be lowercase 64-hex",
        )
    if payload["object_format"] not in ("sha1", "sha256"):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "baseline: object_format must be sha1 or sha256",
        )
    branch_ref = payload["branch_ref"]
    if not isinstance(branch_ref, str) or not branch_ref.startswith("refs/heads/"):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "baseline: branch_ref must be a full refs/heads/ ref",
        )
    if not _nonempty_str(payload["baseline_commit"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "baseline: baseline_commit must be nonempty"
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
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "preflight: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "preflight: schema must be integer 1"
        )
    if payload["decision"] not in _PREFLIGHT_DECISIONS:
        return ValidationResult(
            False, Reason.FIELD_INVALID, "preflight: invalid decision"
        )
    if not _confined_strict(payload["prp_path"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "preflight: prp_path must be a confined path"
        )
    if not _nonempty_str(payload["scope"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "preflight: scope must be nonempty"
        )
    allowed = payload["allowed_paths"]
    if (
        not isinstance(allowed, list)
        or not allowed
        or not all(_confined_strict(x) for x in allowed)
    ):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "preflight: invalid allowed_paths"
        )
    for key in ("focused_tests", "regression_tests"):
        specs = payload[key]
        if (
            not isinstance(specs, list)
            or not specs
            or not all(_valid_test_spec(s) for s in specs)
        ):
            return ValidationResult(
                False, Reason.FIELD_INVALID, f"preflight: invalid {key}"
            )
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
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "reconnaissance: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "reconnaissance: schema must be integer 1"
        )
    if payload["status"] not in _RECON_STATUSES:
        return ValidationResult(
            False, Reason.FIELD_INVALID, "reconnaissance: invalid status"
        )
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
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "implementation: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "implementation: schema must be integer 1"
        )
    if payload["status"] not in _IMPL_STATUSES:
        return ValidationResult(
            False, Reason.FIELD_INVALID, "implementation: invalid status"
        )
    if not isinstance(payload["red_green_evidence"], list):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "implementation: red_green_evidence must be an array",
        )
    if not _str_list(payload["changed_files"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "implementation: changed_files must be a string array",
        )
    if not _str_list(payload["blockers"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "implementation: blockers must be a string array",
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
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "status",
        "runs",
        "blockers",
    }:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "focused_results: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "focused_results: schema must be integer 1"
        )
    if payload["status"] not in _FOCUSED_STATUSES:
        return ValidationResult(
            False, Reason.FIELD_INVALID, "focused_results: invalid status"
        )
    runs = payload["runs"]
    if not isinstance(runs, list) or not all(_valid_run(r) for r in runs):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "focused_results: runs must be an array of valid entries",
        )
    if not _str_list(payload["blockers"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "focused_results: blockers must be a string array",
        )
    pass_iff = (
        bool(runs)
        and all(r["exit_code"] == 0 for r in runs)
        and not payload["blockers"]
    )
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
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "regression: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "regression: schema must be integer 1"
        )
    if payload["status"] not in _REGRESSION_STATUSES:
        return ValidationResult(
            False, Reason.FIELD_INVALID, "regression: invalid status"
        )
    runs = payload["runs"]
    if not isinstance(runs, list) or not all(_valid_run(r) for r in runs):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "regression: runs must be an array of valid entries",
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
            False,
            Reason.FIELD_INVALID,
            "regression: changed_files must be a string array",
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
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "review: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "review: schema must be integer 1"
        )
    if payload["verdict"] not in _REVIEW_VERDICTS:
        return ValidationResult(False, Reason.FIELD_INVALID, "review: invalid verdict")
    findings = payload["findings"]
    if not isinstance(findings, list) or not all(_valid_finding(f) for f in findings):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "review: findings must be an array of valid entries",
        )
    if not isinstance(payload["evidence"], list):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "review: evidence must be an array"
        )
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
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "verdict",
        "reviews",
    }:
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
        return ValidationResult(
            False, Reason.FIELD_INVALID, "review_aggregate: invalid verdict"
        )
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
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "pr_package: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 1:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "pr_package: schema must be integer 1"
        )
    if payload["status"] != "packaged":
        return ValidationResult(
            False, Reason.FIELD_INVALID, "pr_package: status must be packaged"
        )
    for key in ("title", "commit_message"):
        v = payload[key]
        if not isinstance(v, str) or not v.strip():
            return ValidationResult(
                False, Reason.FIELD_INVALID, f"pr_package: {key} must be nonblank"
            )
    if not _nonempty_str(payload["branch"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "pr_package: branch must be nonempty"
        )
    if payload["body_file"] != "pr-body.md":
        return ValidationResult(
            False, Reason.FIELD_INVALID, "pr_package: body_file must be pr-body.md"
        )
    if not _nonempty_str_list(payload["changed_files"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "pr_package: changed_files must be a nonempty string array",
        )
    if not isinstance(payload["test_evidence"], list):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "pr_package: test_evidence must be an array"
        )
    return ValidationResult(True, None, "", normalized=payload)


_APPROVAL_PAYLOAD_KEYS_V2 = {
    "schema",
    "run_id",
    "package_bytes_digest",
    "body_bytes_digest",
    "title",
    "commit_message",
    "branch_ref",
    "repository",
    "pr_base_branch",
    "baseline_commit",
    "baseline_tree",
    "changed_entries",
    "tree_oid",
    "object_inventory_digest",
    "object_state_digest",
    "commit_oid",
    "author_ident",
    "committer_ident",
    "timestamp",
}
_REPOSITORY_KEYS_V2 = {
    "worktree_id",
    "root",
    "git_dir",
    "common_dir",
    "object_format",
    "repository_slug",
}
_SEALED_TRANSACTION_KEYS_V2 = {"path", "package", "body", "metadata", "object_count"}
_CHANGE_STATUSES_V2 = {"A", "M", "D", "T", "R"}
_MODE_RE = re.compile(r"^[0-7]{6}$")
_OID_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_ZERO_OID_RE = re.compile(r"^0+$")


def _valid_changed_entry_v2(e: object) -> bool:
    if not isinstance(e, dict) or set(e) != {
        "status",
        "score",
        "old_mode",
        "new_mode",
        "old_oid",
        "new_oid",
        "old_path",
        "new_path",
    }:
        return False
    if e["status"] not in _CHANGE_STATUSES_V2:
        return False
    if e["status"] == "R":
        if not (isinstance(e["score"], str) and re.fullmatch(r"\d{3}", e["score"])):
            return False
        if not isinstance(e["old_path"], str) or not isinstance(e["new_path"], str):
            return False
    else:
        if e["score"] is not None or e["old_path"] is not None:
            return False
        if not isinstance(e["new_path"], str):
            return False
    for mode_key in ("old_mode", "new_mode"):
        m = e[mode_key]
        if not isinstance(m, str) or not (m == "000000" or _MODE_RE.match(m)):
            return False
    for oid_key in ("old_oid", "new_oid"):
        o = e[oid_key]
        if not isinstance(o, str) or not (_OID_RE.match(o) or _ZERO_OID_RE.match(o)):
            return False
    return True


def _valid_repository_v2(r: object) -> bool:
    return (
        isinstance(r, dict)
        and set(r) == _REPOSITORY_KEYS_V2
        and _lower_hex64(r["worktree_id"])
        and all(
            isinstance(r[k], str) and Path(r[k]).is_absolute()
            for k in ("root", "git_dir", "common_dir")
        )
        and r["object_format"] in ("sha1", "sha256")
        and _nonempty_str(r["repository_slug"])
        and "/" in r["repository_slug"]
    )


def _valid_approval_payload_v2(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload) != _APPROVAL_PAYLOAD_KEYS_V2:
        return False
    if payload.get("schema") != 2:
        return False
    if not all(
        _nonempty_str(payload[k]) for k in ("run_id", "title", "commit_message")
    ):
        return False
    if not re.fullmatch(r"[0-9a-f]{32}", payload["run_id"]):
        return False
    if not _lower_hex64(payload["package_bytes_digest"]) or not _lower_hex64(
        payload["body_bytes_digest"]
    ):
        return False
    if not isinstance(payload["branch_ref"], str) or not payload[
        "branch_ref"
    ].startswith("refs/heads/"):
        return False
    if not _valid_repository_v2(payload["repository"]):
        return False
    if not _nonempty_str(payload["pr_base_branch"]):
        return False
    if not _nonempty_str(payload["baseline_commit"]) or not _nonempty_str(
        payload["baseline_tree"]
    ):
        return False
    entries = payload["changed_entries"]
    if not isinstance(entries, list) or not all(
        _valid_changed_entry_v2(e) for e in entries
    ):
        return False
    if not _nonempty_str(payload["tree_oid"]) or not _nonempty_str(
        payload["commit_oid"]
    ):
        return False
    if not _lower_hex64(payload["object_inventory_digest"]) or not _lower_hex64(
        payload["object_state_digest"]
    ):
        return False
    if not _nonempty_str(payload["author_ident"]) or not _nonempty_str(
        payload["committer_ident"]
    ):
        return False
    return isinstance(payload["timestamp"], str) and payload["timestamp"].isdigit()


def _valid_sealed_transaction_ref(s: object) -> bool:
    if not isinstance(s, dict) or set(s) != _SEALED_TRANSACTION_KEYS_V2:
        return False
    if (
        s["package"] != "sealed-pr-package.json"
        or s["body"] != "sealed-pr-body.md"
        or s["metadata"] != "transaction.json"
    ):
        return False
    if not _confined_strict(s["path"]):
        return False
    return _is_int_not_bool(s["object_count"]) and s["object_count"] >= 0


def _canon_approval_manifest(payload: object) -> ValidationResult:
    """Schema-2 only (PRP-WF1B §6/§11): `approval-manifest.json` is a
    fail-closed cutover from the historical schema-1 shape -- no implicit
    upgrade, no dual-schema acceptance."""
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "approval_revision",
        "approval_digest",
        "payload",
        "sealed_transaction",
    }:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "approval_manifest: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 2:
        return ValidationResult(
            False,
            Reason.SCHEMA_INVALID,
            "approval_manifest: schema 1 is rejected, no implicit upgrade",
        )
    if not _valid_approval_payload_v2(payload["payload"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "approval_manifest: payload does not match the schema-2 approval_payload shape",
        )
    if not _valid_sealed_transaction_ref(payload["sealed_transaction"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "approval_manifest: sealed_transaction does not match the exact schema",
        )
    inner = payload["payload"]
    if not _lower_hex64(payload["approval_digest"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "approval_manifest: approval_digest malformed"
        )
    expected_revision = f"2:{inner['run_id']}:{payload['approval_digest'][:16]}"
    if payload["approval_revision"] != expected_revision:
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "approval_manifest: approval_revision malformed",
        )
    return ValidationResult(True, None, "", normalized=payload)


_PR_URL = re.compile(r"https://github\.com/[^/]+/[^/]+/pull/[1-9][0-9]*")


def _canon_publication(payload: object) -> ValidationResult:
    """Schema-2 only (PRP-WF1B §6/§11): fail-closed cutover."""
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "status",
        "approval_revision",
        "approval_digest",
        "branch",
        "commit",
        "tree",
        "url",
        "remote_state",
    }:
        return ValidationResult(
            False, Reason.SCHEMA_INVALID, "publication: unexpected key set"
        )
    if not _is_int_not_bool(payload["schema"]) or payload["schema"] != 2:
        return ValidationResult(
            False,
            Reason.SCHEMA_INVALID,
            "publication: schema 1 is rejected, no implicit upgrade",
        )
    if payload["status"] != "published":
        return ValidationResult(
            False, Reason.FIELD_INVALID, "publication: status must be published"
        )
    if payload["remote_state"] != "published":
        return ValidationResult(
            False, Reason.FIELD_INVALID, "publication: remote_state must be published"
        )
    if not _lower_hex64(payload["approval_digest"]):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "publication: approval_digest must be lowercase 64-hex",
        )
    approval_revision = payload["approval_revision"]
    if not isinstance(approval_revision, str) or not approval_revision.startswith("2:"):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "publication: approval_revision must be schema-2 shaped",
        )
    if not isinstance(payload["branch"], str) or not payload["branch"].startswith(
        "refs/heads/"
    ):
        return ValidationResult(
            False,
            Reason.FIELD_INVALID,
            "publication: branch must be a full refs/heads/ ref",
        )
    if not _nonempty_str(payload["commit"]) or not _nonempty_str(payload["tree"]):
        return ValidationResult(
            False, Reason.FIELD_INVALID, "publication: commit/tree must be nonempty"
        )
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
        and all(
            isinstance(x, list) and x and all(_valid_test_spec(s) for s in x)
            for x in specs
        )
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
        ok,
        None if ok else Reason.FIELD_INVALID,
        "" if ok else "reconnaissance not ready",
    )


def _consumer_implementation_gate(d: object) -> ValidationResult:
    ok = not (
        d.get("schema") != 1
        or d.get("status") != "ready"
        or not d.get("red_green_evidence")
    )
    return ValidationResult(
        ok,
        None if ok else Reason.FIELD_INVALID,
        "" if ok else "implementation is not ready",
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
        ""
        if ok
        else "review aggregate blocked; abandon run and restart after remediation",
    )


def _consumer_review_gate_focused(t: object) -> ValidationResult:
    ok = not (
        t.get("status") != "pass"
        or any(x.get("exit_code") != 0 for x in t.get("runs", []))
    )
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
        ok,
        None if ok else Reason.FIELD_INVALID,
        "" if ok else "regression tests failed",
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


def validate_payload(
    kind: str, payload: object, *, policy: str = "canonical"
) -> ValidationResult:
    if policy == "canonical":
        fn = _CANONICAL_VALIDATORS.get(kind)
        if fn is None:
            return ValidationResult(
                False, Reason.SCHEMA_INVALID, f"unknown artifact kind: {kind}"
            )
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
        return ValidationResult(
            False, Reason.PATH_INVALID, "test cwd escapes repository"
        )
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
    return subprocess.run(
        args, cwd=cwd, text=text, capture_output=True, check=check, shell=False
    )


def compute_regression_diff_state(repo_root: Path, head: str) -> DiffState:
    tracked = _run_git(["git", "diff", "--name-only", "-z", head], repo_root).stdout
    extra_for_changed = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    ).stdout
    changed = sorted(x for x in (tracked + extra_for_changed).split("\0") if x)
    digest = hashlib.sha256()
    digest.update(
        _run_git(["git", "diff", "--binary", head, "--"], repo_root, text=False).stdout
    )
    extra_for_hash = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    ).stdout
    for path in extra_for_hash.split("\0"):
        if path:
            digest.update(
                path.encode() + b"\0" + (repo_root / path).read_bytes() + b"\0"
            )
    return DiffState(changed_files=tuple(changed), digest=digest.hexdigest())


def compute_publication_diff_state(repo_root: Path, base: str) -> DiffState:
    return DiffState(
        changed_files=_publication_changed_files(repo_root, base),
        digest=_publication_digest(repo_root, base),
    )


def _publication_changed_files(repo_root: Path, base: str) -> tuple[str, ...]:
    tracked = _run_git(
        ["git", "diff", "--name-only", "-z", base], repo_root
    ).stdout.split("\0")
    extra = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    ).stdout.split("\0")
    return tuple(sorted({x.replace("\\", "/") for x in tracked + extra if x}))


def _publication_digest(repo_root: Path, base: str) -> str:
    extra = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    ).stdout.split("\0")
    digest = hashlib.sha256()
    digest.update(
        _run_git(["git", "diff", "--binary", base, "--"], repo_root, text=False).stdout
    )
    for p in sorted(set(extra) - {""}):
        digest.update(p.encode() + b"\0" + (repo_root / p).read_bytes() + b"\0")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# §10.1 package-gate / §10.2 publish-pr -- ordered context checks
# ---------------------------------------------------------------------------

_ppb_module = None


def _publication_binding():
    """Lazily resolve the sibling `prp_publication_binding` module. Reuses
    whatever is already registered in `sys.modules` (the production bash
    nodes import it there themselves, after their own CRLF-safe pin check,
    per PRP-WF1B §10/Stage 10); falls back to loading it by file location
    next to this module for direct/test invocation."""
    global _ppb_module
    if _ppb_module is not None:
        return _ppb_module
    existing = sys.modules.get("prp_publication_binding")
    if existing is not None:
        _ppb_module = existing
        return _ppb_module
    import importlib.util

    script = Path(__file__).resolve().parent / "prp_publication_binding.py"
    spec = importlib.util.spec_from_file_location("prp_publication_binding", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["prp_publication_binding"] = module
    spec.loader.exec_module(module)
    _ppb_module = module
    return _ppb_module


def _derive_repository_slug(root: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode != 0:
        raise ValueError("no origin remote configured")
    url = result.stdout.strip()
    m = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$", url)
    if not m:
        raise ValueError(f"origin remote is not a recognizable GitHub URL: {url}")
    return f"{m.group(1)}/{m.group(2)}"


def _derive_pr_base_branch(root: Path, repository_slug: str) -> str:
    result = subprocess.run(
        ["gh", "repo", "view", "--repo", repository_slug, "--json", "defaultBranchRef"],
        cwd=root,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30.0,
    )
    if result.returncode != 0:
        raise ValueError(f"gh repo view failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
        return data["defaultBranchRef"]["name"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("gh repo view returned an unexpected shape") from exc


def validate_package_context(state: ArtifactSet) -> ValidationResult:
    """PRP-WF1B §8: rediscover `RepositoryIdentity` independently of
    `baseline.json`, build and validate a sealed transaction under the
    repository lock, and construct the schema-2 `approval-manifest.json`
    dict. Zero main-repository ref/object/remote publication side effect --
    `package-gate` only reads and, via `atomic_write`, persists this
    function's `normalized` result; it never mutates history/refs/remotes."""
    ppb = _publication_binding()
    values = state.values
    pkg = values["pkg"]
    pre = values["preflight"]
    base = values["baseline"]
    regression = values["regression"]
    artifacts_dir = Path(state.artifacts_dir)

    # row 1: strictly (re)read package/body as bytes; preflight/baseline/
    # regression were already parsed by the caller from their own artifacts.
    package_path = artifacts_dir / "pr-package.json"
    body_path = artifacts_dir / "pr-body.md"
    try:
        package_bytes = ppb.read_confined_regular_bytes(package_path)
        body_bytes = ppb.read_confined_regular_bytes(body_path)
    except OSError:
        return ValidationResult(
            False, Reason.ARTIFACT_INVALID, "package/body artifact unreadable"
        )
    try:
        parsed_package = ppb._strict_json_loads(package_bytes)
    except (ppb.CanonicalJsonError, UnicodeDecodeError, json.JSONDecodeError):
        return ValidationResult(
            False, Reason.ARTIFACT_INVALID, "package/body artifact unreadable"
        )
    if parsed_package != pkg:
        return ValidationResult(
            False, Reason.ARTIFACT_INVALID, "package changed while being read"
        )

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
        not isinstance(pkg, dict)
        or set(pkg) != required
        or pkg["schema"] != 1
        or pkg["status"] != "packaged"
        or pkg["body_file"] != "pr-body.md"
        or not all(
            isinstance(pkg[x], str) and pkg[x].strip()
            for x in ("title", "commit_message")
        )
    ):
        return ValidationResult(
            False, Reason.PACKAGE_INVALID, "invalid package schema/status"
        )
    if not body_bytes.strip():
        return ValidationResult(False, Reason.PACKAGE_INVALID, "missing/empty PR body")

    # row 2: trusted repository discovery equals baseline identity
    try:
        identity = ppb.discover_repository(Path.cwd())
    except ppb.RepositoryIdentityError as exc:
        return ValidationResult(False, Reason.REPOSITORY_IDENTITY_MISMATCH, str(exc))
    if (
        base.get("root") != str(identity.root)
        or base.get("git_dir") != str(identity.git_dir)
        or base.get("common_dir") != str(identity.common_dir)
        or base.get("object_format") != identity.object_format
        or base.get("branch_ref") != identity.branch_ref
    ):
        return ValidationResult(
            False,
            Reason.REPOSITORY_IDENTITY_MISMATCH,
            "repository identity differs from baseline",
        )

    # row 3: live HEAD/branch equal baseline
    if identity.baseline_commit != base.get("baseline_commit"):
        return ValidationResult(
            False,
            Reason.BASELINE_REVISION_MISMATCH,
            "baseline HEAD changed before approval",
        )

    # row 4: package branch matches the live branch
    short_branch = identity.branch_ref.removeprefix("refs/heads/")
    if pkg["branch"] not in (short_branch, identity.branch_ref):
        return ValidationResult(
            False, Reason.PACKAGE_INVALID, "package branch does not match live branch"
        )

    # row 5: changed paths nonempty, package-equal, and allowed
    allowed = pre.get("allowed_paths")
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(
            not isinstance(x, str)
            or not x
            or "\\" in x
            or x.startswith(":")
            or PurePosixPath(x).is_absolute()
            or "." in PurePosixPath(x).parts
            or ".." in PurePosixPath(x).parts
            for x in allowed
        )
        or len(allowed) != len(set(allowed))
    ):
        return ValidationResult(
            False, Reason.PATH_INVALID, "invalid preflight allowed_paths"
        )

    pkg_changed = pkg.get("changed_files")
    if (
        not isinstance(pkg_changed, list)
        or not pkg_changed
        or any(not isinstance(path, str) or not path for path in pkg_changed)
        or len(pkg_changed) != len(set(pkg_changed))
    ):
        return ValidationResult(
            False, Reason.CHANGED_PATHS_MISMATCH, "package changed_files empty/invalid"
        )

    def in_scope(p: str) -> bool:
        return any(
            p == a.rstrip("/") or p.startswith(a.rstrip("/") + "/") for a in allowed
        )

    if any(not in_scope(p) for p in pkg_changed):
        return ValidationResult(
            False,
            Reason.CHANGED_PATHS_MISMATCH,
            "changed paths violate preflight scope",
        )

    # row 6: WF1 regression changed files/digest still match current legacy state
    live_diff = compute_publication_diff_state(identity.root, identity.baseline_commit)
    if regression.get("validated_diff_digest") != live_diff.digest or regression.get(
        "changed_files"
    ) != list(live_diff.changed_files):
        return ValidationResult(
            False,
            Reason.REGRESSION_STATE_MISMATCH,
            "code changed after deterministic regression validation",
        )
    if sorted(pkg_changed) != list(live_diff.changed_files):
        return ValidationResult(
            False,
            Reason.CHANGED_PATHS_MISMATCH,
            "package changed_files differ from live diff state",
        )

    # rows 7-11: exclusive lock, build + validate the sealed transaction
    try:
        with ppb.acquire_repository_lock(
            artifacts_dir,
            identity.common_dir,
            identity.branch_ref,
            run_id="package-gate",
        ):
            try:
                tx = ppb.build_sealed_transaction(
                    identity,
                    artifacts_dir,
                    package_bytes,
                    body_bytes,
                    allowed,
                )
            except ppb.TransactionError as exc:
                return ValidationResult(False, Reason(exc.reason), str(exc))

            approved_files = ppb.changed_paths_projection(
                tx.manifest["changed_entries"]
            )
            if approved_files != sorted(pkg_changed):
                return ValidationResult(
                    False,
                    Reason.GIT_OBJECT_STATE_MISMATCH,
                    "tree-derived changed entries do not equal package paths",
                )

            payload = dict(tx.manifest)

            transaction_metadata = ppb._strict_json_loads(
                ppb.read_confined_regular_bytes(tx.directory / "transaction.json")
            )
            if (
                set(transaction_metadata) != {"schema", "payload", "object_count"}
                or transaction_metadata.get("schema") != 2
                or transaction_metadata.get("payload") != payload
            ):
                return ValidationResult(
                    False,
                    Reason.TRANSACTION_SEAL_FAILED,
                    "sealed transaction metadata does not exactly equal its payload",
                )
            digest_value = ppb.approval_digest(payload)
            manifest = {
                "schema": 2,
                "approval_revision": f"2:{payload['run_id']}:{digest_value[:16]}",
                "approval_digest": digest_value,
                "payload": payload,
                "sealed_transaction": {
                    "path": f"publication-transactions/{payload['run_id']}.sealed",
                    "package": "sealed-pr-package.json",
                    "body": "sealed-pr-body.md",
                    "metadata": "transaction.json",
                    "object_count": transaction_metadata["object_count"],
                },
            }

            # row 12: reread package/body/identity; every payload digest is
            # unchanged before the manifest replaces any prior one on disk.
            reread_package = ppb.read_confined_regular_bytes(package_path)
            reread_body = ppb.read_confined_regular_bytes(body_path)
            try:
                fresh_identity = ppb.discover_repository(Path.cwd())
            except ppb.RepositoryIdentityError as exc:
                return ValidationResult(
                    False,
                    Reason.PACKAGE_CHANGED_DURING_SEAL,
                    f"repository identity changed during seal: {exc}",
                )
            if fresh_identity != identity:
                return ValidationResult(
                    False,
                    Reason.PACKAGE_CHANGED_DURING_SEAL,
                    "repository identity changed during seal",
                )
            reseal_check = ppb.validate_sealed_transaction(
                fresh_identity,
                artifacts_dir,
                ppb.canonical_json(manifest),
                reread_package,
                reread_body,
            )
            if not reseal_check.ok:
                return ValidationResult(
                    False,
                    Reason.PACKAGE_CHANGED_DURING_SEAL,
                    f"package/body changed during seal: {reseal_check.detail}",
                )
    except ppb.PublicationLockedError as exc:
        return ValidationResult(False, Reason.PUBLICATION_LOCKED, str(exc))

    return ValidationResult(True, None, "", normalized=manifest)


def validate_publish_context(state: ArtifactSet) -> ValidationResult:
    """PRP-WF1B §9: rediscover `RepositoryIdentity` independently, then
    delegate every check and the (only-after-all-checks-pass) publication
    side effect to `publish_sealed_transaction`. No `git add`/`git commit`/
    `git push`/`gh pr create` is invoked from this function or from the
    `publish-pr` node -- they all happen inside `publish_sealed_transaction`,
    strictly after its own row-1..8 zero-side-effect checks."""
    ppb = _publication_binding()
    artifacts_dir = Path(state.artifacts_dir)

    try:
        identity = ppb.discover_repository(Path.cwd())
    except ppb.RepositoryIdentityError as exc:
        return ValidationResult(False, Reason.REPOSITORY_IDENTITY_MISMATCH, str(exc))

    result = ppb.publish_sealed_transaction(identity, artifacts_dir)
    if not result.ok:
        return ValidationResult(False, Reason(result.reason.value), result.detail)

    return ValidationResult(True, None, "", normalized=result.value)


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
                        {
                            "ok": False,
                            "reason": Reason.JSON_INVALID.value,
                            "detail": str(exc),
                        },
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
                    {
                        "changed_files": list(diff_state.changed_files),
                        "digest": diff_state.digest,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.cmd == "publication-diff-state":
            diff_state = compute_publication_diff_state(Path(args.repo_root), args.base)
            print(
                json.dumps(
                    {
                        "changed_files": list(diff_state.changed_files),
                        "digest": diff_state.digest,
                    },
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
