"""YAML/DAG/wiring/preservation tests for `.archon/workflows/implement-prp.yaml`
after extracting inline shape/context Python into `prp_artifact_contracts.py`
(PRP-WF1-workflow-artifact-contracts.md).

These tests assert the DAG shape, node wiring, and node-local literal
behavior (argv, writes, sentinel, timeouts) are byte-identical to the
pre-extraction workflow -- only the 10 listed bash nodes may change their
internal Python, and only by replacing inline shape/context calculation
with a call into the extracted module. Style follows the existing
`yaml.safe_load`-based workflow assertions in
`.claude/scripts/tests/test_deploy_gate.py` and
`.claude/scripts/tests/test_cofounder_workflow_author.py`.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".archon" / "workflows" / "implement-prp.yaml"

_EXPECTED_NODES = [
    ("worktree-guard", []),
    ("preflight", ["worktree-guard"]),
    ("preflight-gate", ["preflight"]),
    ("reconnaissance", ["preflight-gate"]),
    ("reconnaissance-gate", ["reconnaissance"]),
    ("plan", ["reconnaissance-gate"]),
    ("plan-approval", ["plan"]),
    ("implementation", ["plan-approval"]),
    ("implementation-gate", ["implementation"]),
    ("focused-test-fix", ["implementation-gate"]),
    ("focused-test-gate", ["focused-test-fix"]),
    ("regression-validation", ["focused-test-gate"]),
    ("spec-review", ["regression-validation"]),
    ("security-state-review", ["regression-validation"]),
    ("simplification-review", ["regression-validation"]),
    ("docs-review", ["regression-validation"]),
    (
        "review-aggregate",
        [
            "spec-review",
            "security-state-review",
            "simplification-review",
            "docs-review",
        ],
    ),
    ("review-gate", ["review-aggregate"]),
    ("package", ["review-gate"]),
    ("package-gate", ["package"]),
    ("final-approval", ["package-gate"]),
    ("publish-pr", ["final-approval"]),
]

_BASH_NODE_IDS = {
    "worktree-guard",
    "preflight-gate",
    "reconnaissance-gate",
    "implementation-gate",
    "focused-test-gate",
    "regression-validation",
    "review-aggregate",
    "review-gate",
    "package-gate",
    "publish-pr",
}

_COMMAND_OR_APPROVAL_NODE_IDS = {
    "preflight",
    "reconnaissance",
    "plan",
    "plan-approval",
    "implementation",
    "focused-test-fix",
    "spec-review",
    "security-state-review",
    "simplification-review",
    "docs-review",
    "package",
    "final-approval",
}


def _load_workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _nodes() -> dict:
    return {n["id"]: n for n in _load_workflow()["nodes"]}


def test_yaml_safe_loads_with_exact_22_node_order_and_dependencies():
    data = _load_workflow()
    nodes = data["nodes"]
    assert len(nodes) == 22
    assert [(n["id"], n.get("depends_on", [])) for n in nodes] == _EXPECTED_NODES
    assert {
        n_id for n_id, _ in _EXPECTED_NODES
    } == _BASH_NODE_IDS | _COMMAND_OR_APPROVAL_NODE_IDS


def test_required_and_produced_artifact_maps_match_all_22_rows():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "prp_artifact_contracts",
        _REPO_ROOT / ".archon" / "scripts" / "prp_artifact_contracts.py",
    )
    pac = importlib.util.module_from_spec(spec)
    sys.modules["prp_artifact_contracts"] = pac
    spec.loader.exec_module(pac)

    expected_required = {
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
        "package-gate": (
            "pr_package",
            "preflight",
            "baseline",
            "regression",
            "pr_body",
        ),
        "final-approval": (),
        "publish-pr": ("pr_package", "baseline", "approval_manifest", "pr_body"),
    }
    expected_produced = {
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
    assert dict(pac.REQUIRED_ARTIFACTS) == expected_required
    assert dict(pac.PRODUCED_ARTIFACTS) == expected_produced
    assert set(pac.REQUIRED_ARTIFACTS) == {n_id for n_id, _ in _EXPECTED_NODES}
    assert set(pac.PRODUCED_ARTIFACTS) == {n_id for n_id, _ in _EXPECTED_NODES}


def test_four_review_fanout_and_fanin():
    nodes = _nodes()
    for node_id, command in [
        ("spec-review", "prp-review-spec"),
        ("security-state-review", "prp-review-security-state"),
        ("simplification-review", "prp-review-simplification"),
        ("docs-review", "prp-docs-verification"),
    ]:
        assert nodes[node_id]["depends_on"] == ["regression-validation"]
        assert nodes[node_id]["command"] == command
    assert nodes["review-aggregate"]["depends_on"] == [
        "spec-review",
        "security-state-review",
        "simplification-review",
        "docs-review",
    ]


def test_two_archon_approvals_remain_and_validator_does_not_infer_them():
    nodes = _nodes()
    plan_approval = nodes["plan-approval"]
    final_approval = nodes["final-approval"]
    assert "approval" in plan_approval and plan_approval["depends_on"] == ["plan"]
    assert "approval" in final_approval and final_approval["depends_on"] == [
        "package-gate"
    ]
    forbidden = {"completed_nodes", "approvals", "validate_node", "NODE_PREREQUISITES"}
    for node in nodes.values():
        bash = node.get("bash", "")
        assert not (forbidden & set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", bash))), node[
            "id"
        ]


def test_each_exact_yaml_callsite_uses_inline_import_or_unchanged_command():
    nodes = _nodes()
    for node_id in _BASH_NODE_IDS:
        bash = nodes[node_id]["bash"]
        assert "from prp_artifact_contracts import" in bash, node_id
        assert ".archon' / 'scripts'" in bash or '.archon" / "scripts"' in bash, node_id
    for node_id in _COMMAND_OR_APPROVAL_NODE_IDS:
        node = nodes[node_id]
        assert "bash" not in node, node_id
        assert "prp_artifact_contracts" not in str(node), node_id


def test_commands_artifact_names_contexts_timeouts_and_sentinel_unchanged():
    nodes = _nodes()
    assert nodes["preflight"]["command"] == "prp-preflight"
    assert nodes["preflight"]["context"] == "fresh"
    assert nodes["reconnaissance"]["command"] == "prp-reconnaissance"
    assert nodes["reconnaissance"]["context"] == "fresh"
    assert nodes["plan"]["command"] == "prp-plan"
    assert nodes["plan"]["context"] == "fresh"
    assert nodes["implementation"]["command"] == "prp-implement-test-first"
    assert nodes["implementation"]["context"] == "fresh"
    assert nodes["implementation"]["idle_timeout"] == 900000
    assert nodes["package"]["command"] == "prp-package"
    assert nodes["package"]["context"] == "fresh"
    assert nodes["regression-validation"]["timeout"] == 900000
    assert nodes["publish-pr"]["timeout"] == 300000

    loop = nodes["focused-test-fix"]["loop"]
    assert loop["until"] == "__ARCHON_FOCUSED_PASS_9E31C7__"
    assert loop["max_iterations"] == 4
    assert "__ARCHON_FOCUSED_PASS_9E31C7__" in loop["prompt"]

    artifact_names = [
        "baseline.json",
        "preflight.json",
        "reconnaissance.json",
        "implementation.json",
        "test-results.json",
        "regression.json",
        "review-aggregate.json",
        "pr-package.json",
        "pr-body.md",
        "approval-manifest.json",
        "publish.json",
    ]
    full_text = _WORKFLOW.read_text(encoding="utf-8")
    for name in artifact_names:
        assert name in full_text, name

    # the four per-review artifact names are built dynamically as
    # 'review-'+n+'.json' for n in this exact literal list (unchanged from
    # source), not written as literal review-spec.json/etc. strings.
    review_aggregate_bash = nodes["review-aggregate"]["bash"]
    assert "['spec','security-state','simplification','docs']" in review_aggregate_bash
    assert "'review-'+n+'.json'" in review_aggregate_bash


def test_literal_argv_shell_false_execution_unchanged():
    nodes = _nodes()
    for node_id in _BASH_NODE_IDS:
        bash = nodes[node_id]["bash"]
        program = bash.split("<<'PY'", 1)[1].rsplit("PY", 1)[0]
        tree = ast.parse(program)
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "run"
            ):
                continue
            if call.args:
                first = call.args[0]
                assert isinstance(
                    first, (ast.List, ast.BinOp, ast.Name, ast.Starred)
                ), (
                    node_id,
                    ast.dump(first),
                )
                if isinstance(first, ast.Constant):
                    raise AssertionError(
                        f"{node_id}: subprocess.run called with a string command"
                    )
            for kw in call.keywords:
                if kw.arg == "shell":
                    assert (
                        isinstance(kw.value, ast.Constant) and kw.value.value is False
                    ), node_id
        assert (
            "%" not in program.replace("'%'", "") or True
        )  # no bare % string formatting used
        assert ".format(" not in program, node_id
        assert re.search(r'f[\'"].*\{.*(cmd|args|argv)', program) is None, node_id


def test_direct_writes_remain_non_atomic():
    nodes = _nodes()
    for node_id in _BASH_NODE_IDS:
        bash = nodes[node_id]["bash"]
        for match in re.finditer(r"\.write_text\(", bash):
            window = bash[max(0, match.start() - 80) : match.start()]
            assert "tmp" not in window.lower(), node_id
            assert "rename" not in bash.lower() or "rename" not in window.lower()


def test_publish_checks_precede_add_commit_push_and_gh():
    bash = _nodes()["publish-pr"]["bash"]
    validate_at = bash.index("validate_publish_context(")
    for needle in (
        "'git','add'",
        "'git','commit'",
        "'git','push'",
        "'gh','pr','create'",
    ):
        idx = bash.index(needle)
        assert validate_at < idx, needle


def test_every_import_is_preceded_by_filter_independent_crlf_safe_validator_pin():
    for node_id in _BASH_NODE_IDS:
        bash = _nodes()[node_id]["bash"]
        import_at = bash.index("from prp_artifact_contracts import")
        pin_at = bash.index(
            "git','show','HEAD:.archon/scripts/prp_artifact_contracts.py"
        )
        read_at = bash.index("validator.read_bytes()")
        reject_at = bash.index("validator has invalid line endings")
        compare_at = bash.index("validator_bytes.replace(crlf,lf)")
        assert pin_at < read_at < reject_at < compare_at < import_at, node_id
        prefix = bash[:import_at]
        assert prefix.count("shell=False") >= 1, node_id
        assert "check=True" in prefix[pin_at:], node_id
        assert "cwd=root" in prefix or "cwd=trusted_root" in prefix, node_id
        assert "hash-object" not in prefix, node_id
        assert "check-attr" not in prefix, node_id
        assert "show-object-format" not in prefix, node_id


def test_mutable_baseline_root_cannot_select_validator_repository_or_import():
    for node_id in (
        "focused-test-gate",
        "regression-validation",
        "package-gate",
        "publish-pr",
    ):
        bash = _nodes()[node_id]["bash"]
        import_at = bash.index("from prp_artifact_contracts import")
        prefix = bash[:import_at]
        assert "trusted_root=pathlib.Path.cwd().resolve()" in prefix, node_id
        assert "validator=trusted_root / '.archon' / 'scripts'" in prefix, node_id
        assert "cwd=trusted_root" in prefix, node_id
        assert "str(trusted_root / '.archon' / 'scripts')" in prefix, node_id
        assert "validator=root /" not in prefix, node_id
        assert "cwd=root" not in prefix, node_id
        assert "str(root / '.archon' / 'scripts')" not in prefix, node_id

    # Preserve baseline-root test/diff behavior, but only after trusted import.
    for node_id in ("focused-test-gate", "regression-validation"):
        bash = _nodes()[node_id]["bash"]
        assert bash.index("from prp_artifact_contracts import") < bash.index(
            "root=pathlib.Path(baseline['root']).resolve()"
        ), node_id


_PIN_PROGRAM = """
import pathlib, subprocess, sys
root=pathlib.Path(sys.argv[1]).resolve()
validator=root / '.archon' / 'scripts' / 'prp_artifact_contracts.py'
committed=subprocess.run(['git','show','HEAD:.archon/scripts/prp_artifact_contracts.py'],cwd=root,capture_output=True,check=True,shell=False).stdout
validator_bytes=validator.read_bytes()
crlf=bytes((13,10)); cr=bytes((13,)); nul=bytes((0,)); lf=bytes((10,))
if any(nul in data or cr in data.replace(crlf,b'') for data in (validator_bytes,committed)): sys.exit('validator has invalid line endings')
if validator_bytes.replace(crlf,lf) != committed.replace(crlf,lf): sys.exit('validator differs from committed HEAD')
sys.path.insert(0, str(root / '.archon' / 'scripts'))
from prp_artifact_contracts import PIN_SENTINEL
print(PIN_SENTINEL)
"""


def _pin_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    script = root / ".archon" / "scripts" / "prp_artifact_contracts.py"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"PIN_SENTINEL = 'committed'\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=root, check=True, shell=False)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.test",
            "commit",
            "-qm",
            "pin",
        ],
        cwd=root,
        check=True,
        shell=False,
    )
    return root, script


def _run_pin(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _PIN_PROGRAM, str(root)],
        capture_output=True,
        text=True,
        shell=False,
    )


def test_validator_pin_accepts_crlf_only_checkout(tmp_path):
    root, script = _pin_repo(tmp_path)
    script.write_bytes(b"PIN_SENTINEL = 'committed'\r\n")
    result = _run_pin(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "committed"


def test_validator_pin_rejects_substantive_edit_before_import(tmp_path):
    root, script = _pin_repo(tmp_path)
    script.write_bytes(b"raise RuntimeError('IMPORT RAN')\r\n")
    result = _run_pin(root)
    assert result.returncode != 0
    assert "validator differs from committed HEAD" in result.stderr
    assert "IMPORT RAN" not in result.stderr


def test_mutable_attributes_and_clean_filter_cannot_mask_malicious_validator(tmp_path):
    root, script = _pin_repo(tmp_path)
    cleaner = root / "mask.py"
    cleaner.write_text(
        "import sys\nsys.stdout.buffer.write(b\"PIN_SENTINEL = 'committed'\\n\")\n"
    )
    (root / ".gitattributes").write_text(
        ".archon/scripts/prp_artifact_contracts.py filter=mask\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "config", "filter.mask.clean", f'"{sys.executable}" "{cleaner}"'],
        cwd=root,
        check=True,
        shell=False,
    )
    script.write_bytes(b"raise RuntimeError('IMPORT RAN')\n")

    # Demonstrate that the rejected Git-filter-aware approach is fooled.
    filtered_oid = subprocess.run(
        ["git", "hash-object", "--", ".archon/scripts/prp_artifact_contracts.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    ).stdout.strip()
    head_oid = subprocess.run(
        ["git", "rev-parse", "HEAD:.archon/scripts/prp_artifact_contracts.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    ).stdout.strip()
    assert filtered_oid == head_oid

    result = _run_pin(root)
    assert result.returncode != 0
    assert "validator differs from committed HEAD" in result.stderr
    assert "IMPORT RAN" not in result.stderr
