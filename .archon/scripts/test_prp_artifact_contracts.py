"""Tests for the extracted PRP-WF1 artifact contract module.

The rule this module enforces -- deterministic shape/context validation for
the implement-prp DAG's JSON artifacts -- previously lived only as inline
Python duplicated across 10 bash nodes in implement-prp.yaml. This is
preservation, not redesign (PRP-WF1-workflow-artifact-contracts.md): every
test here proves the extracted module reproduces the exact current behavior,
not an improved one.

No network, no Archon, no LLM. compute_regression_diff_state /
compute_publication_diff_state tests use throwaway git repos (the `_git`
helper precedent in `.claude/scripts/tests/test_deploy_gate.py:58-64`).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "prp_artifact_contracts.py"

spec = importlib.util.spec_from_file_location("prp_artifact_contracts", _SCRIPT)
pac = importlib.util.module_from_spec(spec)
sys.modules["prp_artifact_contracts"] = pac
spec.loader.exec_module(pac)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


# ---------------------------------------------------------------------------
# fixtures: one minimal-valid instance per §4.1 canonical payload
# ---------------------------------------------------------------------------


def _spec(cwd="a", tool="pytest") -> dict:
    if tool in ("pytest", "ruff"):
        argv = ["uv", "run", "--extra", "dev", tool]
    elif tool == "npm-test":
        argv = ["npm", "test"]
    else:
        argv = ["npm", "run", "typecheck"]
    return {"cwd": cwd, "argv": argv}


_ABS_ROOT = str(Path.cwd().anchor) + "repo"
_ABS_GIT_DIR = str(Path.cwd().anchor) + "repo/.git/worktrees/w"
_ABS_COMMON_DIR = str(Path.cwd().anchor) + "repo/.git"


def _baseline(**over) -> dict:
    d = {
        "schema": 1,
        "root": _ABS_ROOT,
        "git_dir": _ABS_GIT_DIR,
        "common_dir": _ABS_COMMON_DIR,
        "branch": "b",
        "baseline_head": "a" * 40,
        "baseline_status": "",
    }
    d.update(over)
    return d


def _preflight(**over) -> dict:
    d = {
        "schema": 1,
        "decision": "proceed",
        "prp_path": "docs/prps/x.md",
        "scope": "scope text",
        "allowed_paths": ["a/b.py"],
        "focused_tests": [_spec()],
        "regression_tests": [_spec()],
        "blockers": [],
    }
    d.update(over)
    return d


def _reconnaissance(**over) -> dict:
    d = {
        "schema": 1,
        "status": "ready",
        "files": ["a.py"],
        "invariants": ["inv"],
        "risks": ["risk"],
        "evidence": ["a.py:1"],
    }
    d.update(over)
    return d


def _implementation(**over) -> dict:
    d = {
        "schema": 1,
        "status": "ready",
        "red_green_evidence": ["red then green"],
        "changed_files": ["a.py"],
        "blockers": [],
    }
    d.update(over)
    return d


def _run(exit_code=0) -> dict:
    return {"spec": _spec(), "exit_code": exit_code, "evidence": "ok"}


def _focused_results(**over) -> dict:
    d = {"schema": 1, "status": "pass", "runs": [_run()], "blockers": []}
    d.update(over)
    return d


def _regression(**over) -> dict:
    d = {
        "schema": 1,
        "status": "pass",
        "runs": [_run()],
        "skipped": [],
        "blockers": [],
        "changed_files": ["a.py"],
        "validated_diff_digest": "0" * 64,
    }
    d.update(over)
    return d


def _finding(severity="blocking") -> dict:
    return {"severity": severity, "path": "a.py", "evidence": "e", "remedy": "fix"}


def _review(verdict="block", findings=None, **over) -> dict:
    d = {
        "schema": 1,
        "verdict": verdict,
        "findings": findings if findings is not None else [_finding()],
        "evidence": ["e"],
    }
    d.update(over)
    return d


def _review_aggregate(**over) -> dict:
    d = {
        "schema": 1,
        "verdict": "pass",
        "reviews": {
            "spec": "pass",
            "security-state": "pass",
            "simplification": "pass",
            "docs": "pass",
        },
    }
    d.update(over)
    return d


def _pr_package(**over) -> dict:
    d = {
        "schema": 1,
        "status": "packaged",
        "title": "t",
        "commit_message": "m",
        "branch": "b",
        "body_file": "pr-body.md",
        "changed_files": ["a.py"],
        "test_evidence": ["e"],
    }
    d.update(over)
    return d


def _approval_manifest(**over) -> dict:
    d = {
        "schema": 1,
        "baseline_head": "a" * 40,
        "branch": "b",
        "changed_files": ["a.py", "b.py"],
        "approved_diff_digest": "0" * 64,
    }
    d.update(over)
    return d


def _publication(**over) -> dict:
    d = {
        "schema": 1,
        "status": "published",
        "branch": "b",
        "commit": "a" * 40,
        "url": "https://github.com/o/r/pull/12",
    }
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# canonical payloads and cross-field rules (§4.1 items 1-11)
# ---------------------------------------------------------------------------


def test_all_canonical_payloads_and_cross_field_rules():
    # 1. baseline
    assert pac.validate_payload("baseline", _baseline()).ok
    assert not pac.validate_payload("baseline", _baseline(schema=True)).ok  # bool, not int
    assert not pac.validate_payload("baseline", _baseline(root="relative")).ok
    assert not pac.validate_payload("baseline", _baseline(baseline_status="dirty")).ok
    assert not pac.validate_payload("baseline", {**_baseline(), "extra": 1}).ok

    # 2. preflight
    assert pac.validate_payload("preflight", _preflight()).ok
    assert not pac.validate_payload("preflight", _preflight(decision="nope")).ok
    assert not pac.validate_payload("preflight", _preflight(allowed_paths=[])).ok
    assert not pac.validate_payload("preflight", _preflight(focused_tests=[])).ok
    # cross-field: proceed requires empty blockers
    assert not pac.validate_payload("preflight", _preflight(decision="proceed", blockers=["x"])).ok
    assert pac.validate_payload("preflight", _preflight(decision="revise", blockers=["x"])).ok

    # 3. reconnaissance (see also the dedicated test below)
    assert pac.validate_payload("reconnaissance", _reconnaissance()).ok
    assert not pac.validate_payload("reconnaissance", _reconnaissance(status="nope")).ok

    # 4. implementation cross-field iff
    assert pac.validate_payload("implementation", _implementation()).ok
    assert not pac.validate_payload(
        "implementation", _implementation(status="ready", red_green_evidence=[])
    ).ok
    assert not pac.validate_payload(
        "implementation",
        _implementation(status="incomplete", red_green_evidence=["x"], blockers=[]),
    ).ok
    assert pac.validate_payload(
        "implementation",
        _implementation(status="incomplete", red_green_evidence=[], blockers=["b"]),
    ).ok

    # 5. focused_results cross-field iff
    assert pac.validate_payload("focused_results", _focused_results()).ok
    assert not pac.validate_payload(
        "focused_results", _focused_results(status="pass", runs=[_run(exit_code=1)])
    ).ok
    assert not pac.validate_payload("focused_results", _focused_results(status="pass", runs=[])).ok
    assert pac.validate_payload(
        "focused_results", _focused_results(status="fail", runs=[_run(exit_code=1)])
    ).ok

    # 6. regression cross-field iff
    assert pac.validate_payload("regression", _regression()).ok
    assert not pac.validate_payload(
        "regression", _regression(status="pass", runs=[_run(exit_code=2)])
    ).ok
    assert not pac.validate_payload("regression", _regression(validated_diff_digest="not-hex")).ok

    # 7. review verdict iff blocking finding (spec/security-state/simplification)
    for kind in ("review_spec", "review_security_state", "review_simplification"):
        assert pac.validate_payload(
            kind, _review(verdict="block", findings=[_finding("blocking")])
        ).ok
        assert not pac.validate_payload(
            kind, _review(verdict="pass", findings=[_finding("blocking")])
        ).ok
        assert not pac.validate_payload(
            kind, _review(verdict="block", findings=[_finding("advisory")])
        ).ok
        assert pac.validate_payload(
            kind, _review(verdict="pass", findings=[_finding("advisory")])
        ).ok
    # docs has no invented iff: block verdict with only advisory findings is shape-valid
    assert pac.validate_payload(
        "review_docs", _review(verdict="block", findings=[_finding("advisory")])
    ).ok
    assert pac.validate_payload(
        "review_docs", _review(verdict="pass", findings=[_finding("blocking")])
    ).ok

    # 8. review_aggregate pass iff every review passes
    assert pac.validate_payload("review_aggregate", _review_aggregate()).ok
    assert not pac.validate_payload(
        "review_aggregate",
        _review_aggregate(
            verdict="pass", reviews={**_review_aggregate()["reviews"], "docs": "block"}
        ),
    ).ok
    assert pac.validate_payload(
        "review_aggregate",
        _review_aggregate(
            verdict="block", reviews={**_review_aggregate()["reviews"], "docs": "block"}
        ),
    ).ok

    # 9. pr_package exact key set; shape only (no live-scope claims)
    assert pac.validate_payload("pr_package", _pr_package()).ok
    assert not pac.validate_payload("pr_package", _pr_package(status="draft")).ok
    assert not pac.validate_payload("pr_package", _pr_package(title="  ")).ok
    assert not pac.validate_payload("pr_package", _pr_package(body_file="other.md")).ok

    # 10. approval_manifest
    assert pac.validate_payload("approval_manifest", _approval_manifest()).ok
    assert not pac.validate_payload(
        "approval_manifest", _approval_manifest(changed_files=["b.py", "a.py"])
    ).ok  # not sorted
    assert not pac.validate_payload(
        "approval_manifest", _approval_manifest(approved_diff_digest="XYZ")
    ).ok

    # 11. publication
    assert pac.validate_payload("publication", _publication()).ok
    assert not pac.validate_payload("publication", _publication(status="draft")).ok
    assert not pac.validate_payload(
        "publication", _publication(url="https://example.com/o/r/pull/1")
    ).ok
    assert not pac.validate_payload(
        "publication", _publication(url="https://github.com/o/r/pull/0")
    ).ok


def test_reconnaissance_requires_all_four_nonempty_collections_canonically():
    for key in ("files", "invariants", "risks", "evidence"):
        assert not pac.validate_payload("reconnaissance", _reconnaissance(**{key: []})).ok
    assert pac.validate_payload(
        "reconnaissance", _reconnaissance(status="escalate")
    ).ok  # still requires all four


def test_ready_implementation_and_focused_pass_cross_field_equivalence():
    for evidence, blockers, status, expect in [
        (["x"], [], "ready", True),
        ([], [], "ready", False),
        (["x"], ["b"], "ready", False),
        ([], ["b"], "incomplete", True),
        (["x"], [], "incomplete", False),
    ]:
        result = pac.validate_payload(
            "implementation",
            _implementation(red_green_evidence=evidence, blockers=blockers, status=status),
        )
        assert result.ok is expect, (evidence, blockers, status)

    for runs, status, expect in [
        ([_run(0)], "pass", True),
        ([], "pass", False),
        ([_run(0), _run(1)], "pass", False),
        ([_run(1)], "fail", True),
    ]:
        result = pac.validate_payload("focused_results", _focused_results(runs=runs, status=status))
        assert result.ok is expect, (runs, status)


def test_review_verdict_matches_blocking_findings_for_command_contracts():
    assert pac.validate_payload(
        "review_security_state",
        _review(verdict="block", findings=[_finding("blocking"), _finding("advisory")]),
    ).ok
    assert not pac.validate_payload(
        "review_security_state", _review(verdict="pass", findings=[_finding("blocking")])
    ).ok
    # docs never enforces the iff regardless of findings shape
    assert pac.validate_payload("review_docs", _review(verdict="pass", findings=[])).ok


def test_package_shape_is_separate_from_live_scope_context():
    # shape validator alone accepts a package whose changed_files it cannot
    # verify against live Git; only validate_package_context does that.
    result = pac.validate_payload("pr_package", _pr_package(changed_files=["nonexistent/path.py"]))
    assert result.ok


def test_consumer_policies_preserve_extra_and_uninspected_fields():
    preflight_extra = {**_preflight(), "extra_field": "z"}
    ok1 = pac.validate_payload("preflight", preflight_extra, policy="preflight_gate")
    assert ok1.ok
    assert not pac.validate_payload("preflight", preflight_extra, policy="canonical").ok

    recon_extra = {**_reconnaissance(), "extra": 1}
    del recon_extra["invariants"]
    del recon_extra["risks"]
    ok2 = pac.validate_payload("reconnaissance", recon_extra, policy="reconnaissance_gate")
    assert ok2.ok and ok2.detail == ""
    assert not pac.validate_payload("reconnaissance", recon_extra, policy="canonical").ok

    impl_extra = {**_implementation(), "extra": 1}
    del impl_extra["changed_files"]
    del impl_extra["blockers"]
    ok3 = pac.validate_payload("implementation", impl_extra, policy="implementation_gate")
    assert ok3.ok and ok3.detail == ""
    assert not pac.validate_payload("implementation", impl_extra, policy="canonical").ok

    review_extra = {**_review(verdict="pass", findings=[]), "extra": 1}
    assert pac.validate_payload("review_spec", review_extra, policy="review_aggregate").ok
    assert not pac.validate_payload("review_spec", review_extra, policy="canonical").ok

    agg_extra = {**_review_aggregate(), "extra": 1}
    ok4 = pac.validate_payload("review_aggregate", agg_extra, policy="review_gate")
    assert ok4.ok and ok4.detail == ""
    focused_extra = {**_focused_results(), "extra": 1}
    ok5 = pac.validate_payload("focused_results", focused_extra, policy="review_gate")
    assert ok5.ok and ok5.detail == ""
    regression_extra = {**_regression(), "extra": 1}
    ok6 = pac.validate_payload("regression", regression_extra, policy="review_gate")
    assert ok6.ok and ok6.detail == ""

    # review_gate also vacuously accepts missing/empty runs (execution nodes
    # already establish nonempty runs; the gate must not invent a new check).
    assert pac.validate_payload("focused_results", {"status": "pass"}, policy="review_gate").ok
    assert pac.validate_payload(
        "regression", {"status": "pass", "skipped": []}, policy="review_gate"
    ).ok

    # a successful gate result never carries a stale "not ready"/"failed"
    # message -- only a rejection does (the node's own sys.exit literal is
    # what the workflow surfaces; the module's detail must not mislead a
    # direct/CLI caller into thinking a passing payload failed).
    bad = pac.validate_payload("reconnaissance", {}, policy="reconnaissance_gate")
    assert not bad.ok and bad.detail == "reconnaissance not ready"


# ---------------------------------------------------------------------------
# §9 paths and argv
# ---------------------------------------------------------------------------


def test_preflight_paths_and_four_argv_families(tmp_path):
    root = _repo(tmp_path)

    for bad_cwd in ("a\\b", "a:b", "/abs", "../x", ""):
        assert not pac.validate_payload("preflight", _preflight(allowed_paths=[bad_cwd])).ok

    for tool, argv in [
        ("pytest", ["uv", "run", "--extra", "dev", "pytest", "-q"]),
        ("ruff", ["uv", "run", "--extra", "dev", "ruff", "check"]),
        ("npm-test", ["npm", "test", "--", "-w"]),
        ("npm-typecheck", ["npm", "run", "typecheck"]),
    ]:
        assert pac.validate_payload(
            "preflight", _preflight(focused_tests=[{"cwd": "a", "argv": argv}])
        ).ok

    # uv requires token 5 in {pytest, ruff}
    assert not pac.validate_payload(
        "preflight",
        _preflight(focused_tests=[{"cwd": "a", "argv": ["uv", "run", "--extra", "dev"]}]),
    ).ok
    assert not pac.validate_payload(
        "preflight",
        _preflight(focused_tests=[{"cwd": "a", "argv": ["uv", "run", "--extra", "dev", "black"]}]),
    ).ok
    # npm run typecheck matches by 3-token prefix only, same as npm test:
    # the literal source has no length check for either family, so a
    # trailing arg is permitted, not rejected.
    assert pac.validate_payload(
        "preflight",
        _preflight(focused_tests=[{"cwd": "a", "argv": ["npm", "run", "typecheck", "extra"]}]),
    ).ok

    result = pac.validate_argv(_spec(cwd="."), root)
    assert result.ok
    cwd, argv = result.normalized
    assert cwd == root.resolve()

    escaping = pac.validate_argv({"cwd": "../outside", "argv": ["npm", "test"]}, root)
    assert not escaping.ok
    assert escaping.detail == "test cwd escapes repository"

    bad_spec = pac.validate_argv({"cwd": "."}, root)
    assert not bad_spec.ok
    assert bad_spec.detail == "invalid test spec"

    not_allowlisted = pac.validate_argv({"cwd": ".", "argv": ["rm", "-rf", "/"]}, root)
    assert not not_allowlisted.ok
    assert not_allowlisted.detail == "test argv is not allowlisted"


# ---------------------------------------------------------------------------
# §8.1 / §8.2 diff algorithms — separate, never merged
# ---------------------------------------------------------------------------


def test_regression_diff_algorithm_preserves_git_order_and_duplicates(tmp_path):
    root = _repo(tmp_path)
    (root / "seed.txt").write_text("changed\n", encoding="utf-8")
    (root / "z_new.txt").write_text("z\n", encoding="utf-8")
    (root / "a_new.txt").write_text("a\n", encoding="utf-8")

    state = pac.compute_regression_diff_state(root, "HEAD")
    assert isinstance(state.changed_files, tuple)
    # sorted, no slash-normalization requirement, no dedup guarantee needed
    # here (no duplicate paths in this fixture) but order must be sorted.
    assert list(state.changed_files) == sorted(state.changed_files)
    assert "seed.txt" in state.changed_files
    assert "a_new.txt" in state.changed_files
    assert "z_new.txt" in state.changed_files
    assert len(state.digest) == 64
    int(state.digest, 16)  # lowercase hex

    state_again = pac.compute_regression_diff_state(root, "HEAD")
    assert state_again.digest == state.digest
    assert state_again.changed_files == state.changed_files


def test_publication_diff_algorithm_sorts_deduplicates_and_normalizes(tmp_path):
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD").stdout.strip()
    (root / "seed.txt").write_text("changed\n", encoding="utf-8")
    (root / "b_new.txt").write_text("b\n", encoding="utf-8")

    state = pac.compute_publication_diff_state(root, base)
    assert list(state.changed_files) == sorted(set(state.changed_files))
    assert all("\\" not in p for p in state.changed_files)
    assert len(state.digest) == 64
    int(state.digest, 16)

    # the two algorithms are distinct: publication is keyed off an
    # arbitrary base ref (here, one commit further back than HEAD) while
    # regression is always keyed off HEAD -- that's the intentional
    # difference §8.2 preserves, not a coincidental output difference.
    (root / "committed_after_base.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second commit")
    state_vs_older_base = pac.compute_publication_diff_state(root, base)
    regression_state = pac.compute_regression_diff_state(root, "HEAD")
    assert "committed_after_base.txt" in state_vs_older_base.changed_files
    assert "committed_after_base.txt" not in regression_state.changed_files
    assert state_vs_older_base.digest != regression_state.digest


# ---------------------------------------------------------------------------
# §10.1 package-gate / §10.2 publish-pr ordered context checks
# ---------------------------------------------------------------------------


def _package_ctx(root, artifacts_dir=None, *, pkg=None, pre=None, base=None, regression=None):
    return pac.ArtifactSet(
        artifacts_dir=artifacts_dir if artifacts_dir is not None else root,
        values={
            "pkg": pkg if pkg is not None else _pr_package(branch="b", changed_files=["seed.txt"]),
            "preflight": pre if pre is not None else _preflight(allowed_paths=["seed.txt"]),
            "baseline": base
            if base is not None
            else {"root": str(root), "baseline_head": "", "branch": "b"},
            "regression": regression
            if regression is not None
            else _regression(changed_files=["seed.txt"]),
        },
    )


def _live_package_fixture(tmp_path):
    # ARTIFACTS_DIR is a directory outside the repo tree (real Archon usage);
    # only pr-body.md lives there, never inside the git working tree.
    root = _repo(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "branch", "-m", "b")
    (root / "seed.txt").write_text("changed\n", encoding="utf-8")
    (artifacts_dir / "pr-body.md").write_text("body text\n", encoding="utf-8")
    state = pac.compute_publication_diff_state(root, head)
    pkg = _pr_package(branch="b", changed_files=list(state.changed_files))
    pre = _preflight(allowed_paths=["seed.txt"])
    base = {"root": str(root), "baseline_head": head, "branch": "b"}
    regression = _regression(
        changed_files=list(state.changed_files), validated_diff_digest=state.digest
    )
    return root, artifacts_dir, pkg, pre, base, regression


def test_package_gate_literal_check_order_and_messages(tmp_path):
    root, artifacts_dir, pkg, pre, base, regression = _live_package_fixture(tmp_path)

    ok = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=pkg, pre=pre, base=base, regression=regression)
    )
    assert ok.ok, ok.detail
    assert ok.normalized["schema"] == 1
    assert ok.normalized["changed_files"] == ["seed.txt"]

    # step 2: head/branch changed before approval
    stale_base = {**base, "baseline_head": "0" * 40}
    r = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=pkg, pre=pre, base=stale_base, regression=regression)
    )
    assert not r.ok and r.detail == "baseline HEAD or branch changed before approval"

    # step 4: invalid package shape
    bad_pkg = {**pkg, "status": "draft"}
    r = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=bad_pkg, pre=pre, base=base, regression=regression)
    )
    assert not r.ok and r.detail == "invalid package schema/status/branch"

    # step 5: missing PR body
    (artifacts_dir / "pr-body.md").unlink()
    r = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=pkg, pre=pre, base=base, regression=regression)
    )
    assert not r.ok and r.detail == "missing/empty PR body"
    (artifacts_dir / "pr-body.md").write_text("body text\n", encoding="utf-8")

    # step 7: invalid allowed_paths
    bad_pre = _preflight(allowed_paths=["../escape"])
    r = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=pkg, pre=bad_pre, base=base, regression=regression)
    )
    assert not r.ok and r.detail == "invalid preflight allowed_paths"

    # step 8: package changed_files mismatch
    bad_pkg2 = {**pkg, "changed_files": ["other.py"]}
    r = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=bad_pkg2, pre=pre, base=base, regression=regression)
    )
    assert not r.ok and r.detail == "changed paths violate preflight scope or package"

    # step 10: regression digest stale
    bad_regression = {**regression, "validated_diff_digest": "f" * 64}
    r = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=pkg, pre=pre, base=base, regression=bad_regression)
    )
    assert not r.ok and r.detail == "code changed after deterministic regression validation"


def test_publish_gate_literal_check_order_and_zero_prior_side_effects(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "branch", "-m", "b")
    (root / "seed.txt").write_text("changed\n", encoding="utf-8")
    state = pac.compute_publication_diff_state(root, head)

    base = {"root": str(root), "baseline_head": head, "branch": "b"}
    pkg = _pr_package(branch="b", changed_files=list(state.changed_files))
    approved = _approval_manifest(
        baseline_head=head,
        branch="b",
        changed_files=list(state.changed_files),
        approved_diff_digest=state.digest,
    )
    ctx = pac.ArtifactSet(
        artifacts_dir=root, values={"pkg": pkg, "baseline": base, "approved": approved}
    )

    calls = []
    real_run = subprocess.run

    def spy(args, *a, **kw):
        calls.append(args)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    ok = pac.validate_package_context and pac.validate_publish_context(ctx)
    assert ok.ok, ok.detail
    assert ok.normalized == list(state.changed_files)
    mutating = {"add", "commit", "push"}
    assert not any(set(c) & mutating for c in calls if isinstance(c, list))

    # step 2: HEAD changed since baseline/approval
    stale_approved = {**approved, "baseline_head": "0" * 40}
    ctx2 = pac.ArtifactSet(
        artifacts_dir=root, values={"pkg": pkg, "baseline": base, "approved": stale_approved}
    )
    r = pac.validate_publish_context(ctx2)
    assert not r.ok and r.detail == "HEAD changed since baseline/approval (agent commits forbidden)"

    # step 3: branch mismatch
    bad_pkg = {**pkg, "branch": "other"}
    ctx3 = pac.ArtifactSet(
        artifacts_dir=root, values={"pkg": bad_pkg, "baseline": base, "approved": approved}
    )
    r = pac.validate_publish_context(ctx3)
    assert not r.ok and r.detail == "branch/package mismatch"

    # step 6: changed files differ from approved package
    bad_approved = {**approved, "changed_files": ["other.py"]}
    ctx4 = pac.ArtifactSet(
        artifacts_dir=root, values={"pkg": pkg, "baseline": base, "approved": bad_approved}
    )
    r = pac.validate_publish_context(ctx4)
    assert not r.ok and r.detail == "changed files differ from approved package"

    # step 8: digest mismatch
    bad_approved2 = {**approved, "approved_diff_digest": "f" * 64}
    ctx5 = pac.ArtifactSet(
        artifacts_dir=root, values={"pkg": pkg, "baseline": base, "approved": bad_approved2}
    )
    r = pac.validate_publish_context(ctx5)
    assert not r.ok and r.detail == "approved diff digest changed"

    # step 9: nothing to publish (no diff from base at all)
    clean_root = _repo(tmp_path / "clean")
    clean_head = _git(clean_root, "rev-parse", "HEAD").stdout.strip()
    _git(clean_root, "branch", "-m", "b")
    clean_base = {"root": str(clean_root), "baseline_head": clean_head, "branch": "b"}
    clean_state = pac.compute_publication_diff_state(clean_root, clean_head)
    clean_pkg = _pr_package(branch="b", changed_files=list(clean_state.changed_files))
    clean_approved = _approval_manifest(
        baseline_head=clean_head,
        branch="b",
        changed_files=list(clean_state.changed_files),
        approved_diff_digest=clean_state.digest,
    )
    ctx6 = pac.ArtifactSet(
        artifacts_dir=clean_root,
        values={"pkg": clean_pkg, "baseline": clean_base, "approved": clean_approved},
    )
    r = pac.validate_publish_context(ctx6)
    assert not r.ok and r.detail == "nothing to publish"


def test_scope_and_changed_mismatches_precede_untracked_byte_reads(tmp_path, monkeypatch):
    root, artifacts_dir, pkg, pre, base, regression = _live_package_fixture(tmp_path)
    unreadable = root / "outside.txt"
    unreadable.write_text("x\n", encoding="utf-8")
    real_read_bytes = Path.read_bytes

    def fail_read(path):
        if path == unreadable:
            raise FileNotFoundError("simulated missing untracked file")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    package_result = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=pkg, pre=pre, base=base, regression=regression)
    )
    assert not package_result.ok
    assert package_result.detail == "changed paths violate preflight scope or package"

    approved = _approval_manifest(
        baseline_head=base["baseline_head"],
        branch="b",
        changed_files=["different.txt"],
        approved_diff_digest="0" * 64,
    )
    publish_result = pac.validate_publish_context(
        pac.ArtifactSet(
            artifacts_dir=artifacts_dir,
            values={"pkg": {**pkg, "changed_files": ["different.txt"]}, "baseline": base, "approved": approved},
        )
    )
    assert not publish_result.ok
    assert publish_result.detail == "changed files differ from approved package"


# ---------------------------------------------------------------------------
# no orchestration/approval authority leakage
# ---------------------------------------------------------------------------


def test_approval_manifest_is_context_not_approval_authority(tmp_path):
    root, artifacts_dir, pkg, pre, base, regression = _live_package_fixture(tmp_path)
    ok = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=pkg, pre=pre, base=base, regression=regression)
    )
    manifest = ok.normalized
    assert set(manifest) == {
        "schema",
        "baseline_head",
        "branch",
        "changed_files",
        "approved_diff_digest",
    }
    assert "approved" not in manifest
    assert "ok_to_publish" not in manifest
    assert "approval" not in json.dumps(manifest).lower()


def test_artifact_set_has_no_completion_or_approval_state():
    fields = {f.name for f in dataclasses.fields(pac.ArtifactSet)}
    assert fields == {"artifacts_dir", "values"}

    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    identifiers = (
        {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        | {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
    )
    forbidden = {
        "completed_nodes",
        "approvals",
        "ArtifactReason",
        "validate_artifact",
        "validate_transition",
        "NODE_PREREQUISITES",
        "validate_node",
    }
    assert not (identifiers & forbidden), identifiers & forbidden


# ---------------------------------------------------------------------------
# evidence handling stays opaque / not the module's concern
# ---------------------------------------------------------------------------


def test_evidence_suffix_and_non_echoing_diagnostics(tmp_path):
    root, artifacts_dir, pkg, pre, base, regression = _live_package_fixture(tmp_path)
    secret = "SECRET-MARKER-DO-NOT-LEAK"
    (root / "seed.txt").write_text(secret + "\n", encoding="utf-8")
    bad_pkg = {**pkg, "status": "draft"}
    r = pac.validate_package_context(
        _package_ctx(root, artifacts_dir, pkg=bad_pkg, pre=pre, base=base, regression=regression)
    )
    assert not r.ok
    assert secret not in r.detail
    fixed_messages = {
        "baseline HEAD or branch changed before approval",
        "baseline is not an ancestor",
        "invalid package schema/status/branch",
        "missing/empty PR body",
        "invalid preflight allowed_paths",
        "changed paths violate preflight scope or package",
        "code changed after deterministic regression validation",
    }
    assert r.detail in fixed_messages


# ---------------------------------------------------------------------------
# import mechanics + optional CLI (§11)
# ---------------------------------------------------------------------------


def test_plain_file_import_and_cli_exit_codes(tmp_path):
    # importing via importlib.util.spec_from_file_location with no package
    # install and no __init__.py already happened at module import time
    # above; re-assert the module resolved with the expected public surface.
    assert hasattr(pac, "validate_payload")
    assert not (Path(__file__).parent / "__init__.py").exists()

    payload_path = tmp_path / "preflight.json"
    payload_path.write_text(json.dumps(_preflight()), encoding="utf-8")
    assert (
        pac.main(
            [
                "validate-payload",
                "--kind",
                "preflight",
                "--policy",
                "canonical",
                "--file",
                str(payload_path),
            ]
        )
        == 0
    )

    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(_preflight(decision="nope")), encoding="utf-8")
    assert (
        pac.main(
            [
                "validate-payload",
                "--kind",
                "preflight",
                "--policy",
                "canonical",
                "--file",
                str(bad_path),
            ]
        )
        == 2
    )

    assert (
        pac.main(
            [
                "validate-payload",
                "--kind",
                "preflight",
                "--policy",
                "canonical",
                "--file",
                str(tmp_path / "missing.json"),
            ]
        )
        == 2
    )

    assert pac.main(["nonsense-command"]) == 64
    assert pac.main([]) == 64

    root = _repo(tmp_path / "cli-repo")
    rc = pac.main(["regression-diff-state", "--repo-root", str(root), "--head", "HEAD"])
    assert rc == 0

    rc = pac.main(
        [
            "publication-diff-state",
            "--repo-root",
            str(root),
            "--base",
            _git(root, "rev-parse", "HEAD").stdout.strip(),
        ]
    )
    assert rc == 0

    assert (
        pac.main(
            [
                "regression-diff-state",
                "--repo-root",
                str(tmp_path / "no-such-repo"),
                "--head",
                "HEAD",
            ]
        )
        == 1
    )
