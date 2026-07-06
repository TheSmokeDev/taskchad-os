"""US-013 — workflow authoring + the cofounder_pass observability span.

Authoring paths:
  - stamp_workflow stamps provider/model at the WORKFLOW level and at every
    LOOP-node level (loop nodes ignore per-node provider — reference lesson);
    plain nodes keep their own values
  - validate_draft: yaml.safe_load round-trip + required keys (name, nodes);
    every malformed shape raises WorkflowDraftError
  - author_workflow writes <repo>/.archon/workflows/<name>.yaml stamped from
    the backend knob resolved at CALL time (Rule 1 env proof); a hostile
    draft name can never escape the workflows folder
  - invalid draft = no-op + warning, no file, never a raise
  - restamp_workflow overwrites drift at both levels; a clean file is never
    rewritten; missing/garbage files fail open to False

Pipeline wiring (run_pass):
  - an author decision writes the stamped workflow, appends ONE [author]
    Activity Log line, and records the path in the state entry
  - an invalid draft via the pipeline is a no-op with ONE [warn] line
  - the pass RE-stamps recorded authored workflows after every non-dry cycle
    (LLM drift guard) and prunes entries whose file is gone
  - a dry run never re-stamps (zero writes)

Span matrix (test_team_observability_matrix.py style, enabled/disabled x
happy/error) + the fail-open proof that a broken span never breaks the pass.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import config
from cofounder import orchestrate as orchestrate_mod
from cofounder import project_model
from cofounder import repos as repos_mod
from cofounder import run_pass as run_pass_mod
from cofounder import state as state_mod
from cofounder.repos import RepoResolution
from cofounder.run_pass import (
    OUTCOME_COMPLETED,
    PROJECT_AUTHORED,
    PROJECT_DECISION_NOOP,
    PROJECT_DECISION_PENDING,
    PROJECT_ERROR,
    run_pass,
)
from cofounder.workflow_author import (
    WorkflowDraftError,
    author_workflow,
    restamp_workflow,
    stamp_workflow,
    validate_draft,
    workflow_file_name,
)
from orchestration import observability
from security import kill_switches

COFOUNDER_ENV_KEYS = (
    "COFOUNDER_ENABLED",
    "COFOUNDER_PROJECTS_DIR",
    "COFOUNDER_MAX_ITERATIONS",
    "COFOUNDER_MAX_WALL_CLOCK_HOURS",
    "COFOUNDER_MAX_CONCURRENT",
    "COFOUNDER_NOTIFY_LEVELS",
    "COFOUNDER_ZOMBIE_STALE_MINUTES",
    "COFOUNDER_ARCHON_DB",
    "COFOUNDER_WORKFLOW_PROVIDER",
    "COFOUNDER_WORKFLOW_MODEL",
)

VALID_DRAFT = """\
name: cofounder-demo
description: drafted by the decider
provider: gemini
nodes:
  - id: plan
    model: opus
    prompt: plan the work
  - id: implement
    provider: gemini
    model: flash
    loop:
      prompt: build one story
      until: COMPLETE
      max_iterations: 5
  - id: report
    prompt: report completion
"""


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch, tmp_path):
    """No COFOUNDER_*/kill-switch env; Langfuse OFF unless a test patches it;
    the disabled-path observation jsonl lands in tmp, never the real repo."""
    for key in COFOUNDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setattr(observability, "_OBS_LOG", tmp_path / "obs" / "obs.jsonl")
    # US-020 wired orchestrate.decide as the run_pass default decider; pin it
    # back to None so a run_pass call without decide= never reaches a live LLM.
    monkeypatch.setattr(orchestrate_mod, "decide", None)
    yield


@pytest.fixture(autouse=True)
def reset_counters():
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


@pytest.fixture()
def projects_dir(tmp_path):
    pdir = tmp_path / "cofounder"
    pdir.mkdir()
    return pdir


@pytest.fixture()
def repo_dir(tmp_path, monkeypatch):
    """A local repo the project's slug resolves to (resolve_repo stubbed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        repos_mod,
        "resolve_repo",
        lambda slug_, **kw: RepoResolution(
            slug="demo", local_path=repo, default_branch="master"
        ),
    )
    return repo


def make_project(
    projects_dir: Path,
    slug: str,
    *,
    status: str = "new",
    activity: tuple[str, ...] = ("- 2026-07-03T08:00:00 created",),
    **fm_overrides,
) -> Path:
    fm = {"tags": ["system", "cofounder"], "status": status}
    fm.update(fm_overrides)
    body = (
        f"# {slug}\n\n"
        "## Spec (STATIC - orchestrator MUST NOT rewrite; only the operator edits)\n"
        f"Build {slug}.\n\n"
        "## Plan / Working Memory (MUTABLE - orchestrator may rewrite)\n"
        f"- [ ] plan {slug}\n\n"
        "## Activity Log (APPEND-ONLY - newest at the bottom)\n"
        + "\n".join(activity)
        + "\n"
    )
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body
    path = projects_dir / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    return path


def enabled_settings(projects_dir: Path, **overrides):
    return config.get_cofounder_settings(
        enabled=True, projects_dir=projects_dir, **overrides
    )


class DecideStub:
    def __init__(self, decision=None):
        self.decision = decision
        self.calls = []

    def __call__(self, project, context):
        self.calls.append((project.slug, dict(context)))
        return self.decision


def load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# === stamping: workflow level + every loop-node level ===


def test_stamp_sets_workflow_level_provider_and_model():
    data = yaml.safe_load(VALID_DRAFT)
    stamp_workflow(data, provider="claude", model="sonnet")
    assert data["provider"] == "claude"
    assert data["model"] == "sonnet"


def test_stamp_overwrites_loop_nodes_and_leaves_plain_nodes_alone():
    data = yaml.safe_load(VALID_DRAFT)
    stamp_workflow(data, provider="claude", model="sonnet")
    plan, implement, report = data["nodes"]
    # loop node: the drafted provider/model are overwritten at the node level
    assert implement["provider"] == "claude"
    assert implement["model"] == "sonnet"
    assert implement["loop"]["until"] == "COMPLETE"  # loop body untouched
    # plain nodes keep whatever the draft gave them
    assert plan["model"] == "opus"
    assert "provider" not in plan
    assert "provider" not in report and "model" not in report


# === draft validation: safe_load round-trip + required keys ===


def test_validate_draft_rejects_unparseable_yaml():
    with pytest.raises(WorkflowDraftError, match="not valid YAML"):
        validate_draft("{unclosed")


@pytest.mark.parametrize(
    ("label", "draft"),
    [
        ("non-mapping", "just a string"),
        ("missing-name", "nodes:\n  - id: a\n"),
        ("blank-name", "name: '  '\nnodes:\n  - id: a\n"),
        ("missing-nodes", "name: wf\n"),
        ("empty-nodes", "name: wf\nnodes: []\n"),
        ("nodes-not-list", "name: wf\nnodes: nope\n"),
    ],
)
def test_validate_draft_requires_name_and_nodes(label, draft):
    with pytest.raises(WorkflowDraftError):
        validate_draft(draft)


def test_workflow_file_name_rejects_all_unsafe_name():
    with pytest.raises(WorkflowDraftError):
        workflow_file_name("../..")


# === author_workflow: write + stamp from the backend knob ===


def test_author_workflow_writes_stamped_yaml(repo_dir):
    written = author_workflow(repo_dir, VALID_DRAFT)

    assert written == repo_dir / ".archon" / "workflows" / "cofounder-demo.yaml"
    data = load_workflow(written)  # safe_load round-trip
    assert data["name"] == "cofounder-demo"
    assert data["provider"] == "claude" and data["model"] == "sonnet"  # defaults
    loop_node = data["nodes"][1]
    assert loop_node["provider"] == "claude" and loop_node["model"] == "sonnet"


def test_author_workflow_stamps_env_knob_at_call_time(repo_dir, monkeypatch):
    """Rule 1 behavioral proof: the knob is read on THIS call, not at import."""
    monkeypatch.setenv("COFOUNDER_WORKFLOW_PROVIDER", "codex")
    monkeypatch.setenv("COFOUNDER_WORKFLOW_MODEL", "gpt-5.5")

    written = author_workflow(repo_dir, VALID_DRAFT)

    data = load_workflow(written)
    assert data["provider"] == "codex" and data["model"] == "gpt-5.5"
    loop_node = data["nodes"][1]
    assert loop_node["provider"] == "codex" and loop_node["model"] == "gpt-5.5"


def test_author_workflow_invalid_draft_is_noop_with_warning(repo_dir, caplog):
    with caplog.at_level(logging.WARNING, logger="cofounder.workflow_author"):
        written = author_workflow(repo_dir, "{unclosed")

    assert written is None
    assert not (repo_dir / ".archon").exists()  # nothing written at all
    assert any("workflow draft invalid" in r.message for r in caplog.records)


def test_author_workflow_hostile_name_stays_inside_workflows_dir(repo_dir):
    draft = "name: ../../escape\nnodes:\n  - id: a\n    prompt: x\n"

    written = author_workflow(repo_dir, draft)

    assert written is not None
    assert written.parent == repo_dir / ".archon" / "workflows"
    assert written.name == "escape.yaml"
    assert not (repo_dir.parent / "escape.yaml").exists()


# === restamp_workflow: the drift guard ===


def test_restamp_overwrites_drift_at_both_levels(repo_dir):
    written = author_workflow(repo_dir, VALID_DRAFT)
    drifted = load_workflow(written)
    drifted["provider"] = "deepseek"  # an LLM edit drifted the stamp
    drifted["nodes"][1]["model"] = "r1"
    written.write_text(yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8")

    assert restamp_workflow(written) is True

    data = load_workflow(written)
    assert data["provider"] == "claude" and data["model"] == "sonnet"
    loop_node = data["nodes"][1]
    assert loop_node["provider"] == "claude" and loop_node["model"] == "sonnet"


def test_restamp_clean_file_returns_false_and_never_rewrites(repo_dir):
    written = author_workflow(repo_dir, VALID_DRAFT)
    before_bytes = written.read_bytes()
    before_mtime = written.stat().st_mtime_ns

    assert restamp_workflow(written) is False

    assert written.read_bytes() == before_bytes
    assert written.stat().st_mtime_ns == before_mtime


def test_restamp_missing_or_garbage_file_fails_open(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="cofounder.workflow_author"):
        assert restamp_workflow(tmp_path / "gone.yaml") is False

    garbage = tmp_path / "garbage.yaml"
    garbage.write_text("just a string", encoding="utf-8")
    assert restamp_workflow(garbage) is False
    assert garbage.read_text(encoding="utf-8") == "just a string"  # untouched


# === pipeline wiring: the author decision executes through CODE ===


def test_author_decision_writes_workflow_and_logs(projects_dir, repo_dir, tmp_path):
    path = make_project(projects_dir, "drafter", status="new", repo="demo")
    state_file = tmp_path / "cofounder-state.json"
    decide = DecideStub(
        {"action": "author", "message": VALID_DRAFT, "log_line": "drafting a workflow"}
    )

    result = run_pass(
        settings=enabled_settings(projects_dir), state_file=state_file, decide=decide
    )

    assert result.project_outcomes["drafter"] == PROJECT_AUTHORED
    written = repo_dir / ".archon" / "workflows" / "cofounder-demo.yaml"
    assert written.exists()
    assert load_workflow(written)["provider"] == "claude"  # stamped on write
    parsed = project_model.parse_project_file(path)
    assert parsed.activity_log.count("[author]") == 1
    assert "cofounder-demo" in parsed.activity_log
    assert parsed.frontmatter.last_run  # machine state re-stamped in code
    state = state_mod.load_state(state_file)
    assert state["projects"]["drafter"]["authored_workflows"] == [str(written)]


def test_author_decision_invalid_draft_is_noop_with_warn_line(
    projects_dir, repo_dir, tmp_path
):
    path = make_project(projects_dir, "sloppy", status="new", repo="demo")
    decide = DecideStub({"action": "author", "message": "{unclosed"})

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
        decide=decide,
    )

    assert result.project_outcomes["sloppy"] == PROJECT_DECISION_NOOP
    assert not (repo_dir / ".archon").exists()  # nothing written
    parsed = project_model.parse_project_file(path)
    assert parsed.activity_log.count("[warn]") == 1
    assert parsed.frontmatter.status == "new"  # untouched


def test_pass_restamps_authored_workflows_and_prunes_gone_files(
    projects_dir, repo_dir, tmp_path
):
    make_project(projects_dir, "alpha", status="awaiting-human")
    written = author_workflow(repo_dir, VALID_DRAFT)
    drifted = load_workflow(written)
    drifted["provider"] = "deepseek"  # drift introduced between passes
    drifted["nodes"][1]["model"] = "r1"
    written.write_text(yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8")
    state_file = tmp_path / "cofounder-state.json"
    gone = repo_dir / ".archon" / "workflows" / "gone.yaml"
    state_mod.update_project_state(
        "alpha", state_file, authored_workflows=[str(written), str(gone)]
    )

    result = run_pass(settings=enabled_settings(projects_dir), state_file=state_file)

    assert result.outcome == OUTCOME_COMPLETED
    data = load_workflow(written)
    assert data["provider"] == "claude" and data["model"] == "sonnet"  # re-stamped
    assert data["nodes"][1]["model"] == "sonnet"
    entry = state_mod.load_state(state_file)["projects"]["alpha"]
    assert entry["authored_workflows"] == [str(written)]  # gone file pruned


def test_dry_run_pass_never_restamps(projects_dir, repo_dir, tmp_path):
    make_project(projects_dir, "alpha", status="awaiting-human")
    written = author_workflow(repo_dir, VALID_DRAFT)
    drifted = load_workflow(written)
    drifted["provider"] = "deepseek"
    written.write_text(yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8")
    before_bytes = written.read_bytes()
    state_file = tmp_path / "cofounder-state.json"
    state_mod.update_project_state(
        "alpha", state_file, authored_workflows=[str(written)]
    )
    before_state = state_file.read_bytes()

    result = run_pass(
        dry_run=True, settings=enabled_settings(projects_dir), state_file=state_file
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert written.read_bytes() == before_bytes  # drift left alone on --test
    assert state_file.read_bytes() == before_state


# === the cofounder_pass span matrix (enabled/disabled x happy/error) ===


def _make_mock_client():
    ctx = MagicMock()
    client = MagicMock()
    client.start_as_current_observation.return_value = ctx
    client.get_current_trace_id.return_value = "cofounder-trace-001"
    client.get_current_observation_id.return_value = "cofounder-obs-001"
    fake_mod = MagicMock()
    fake_mod.get_client.return_value = client
    return fake_mod, client


def _span_metadata_calls(client):
    return [
        call.kwargs.get("metadata")
        for call in client.update_current_span.call_args_list
        if call.kwargs.get("metadata")
    ]


def test_span_enabled_happy_emits_cofounder_pass_with_metadata(
    projects_dir, tmp_path
):
    make_project(projects_dir, "alpha", status="new")
    fake_mod, client = _make_mock_client()
    with (
        patch("runtime.langfuse_setup.is_langfuse_enabled", return_value=True),
        patch("orchestration.observability.init_langfuse"),
        patch.dict("sys.modules", {"langfuse": fake_mod}),
    ):
        result = run_pass(
            settings=enabled_settings(projects_dir),
            state_file=tmp_path / "cofounder-state.json",
        )

    assert result.project_outcomes["alpha"] == PROJECT_DECISION_PENDING
    span_names = [
        call.kwargs.get("name")
        for call in client.start_as_current_observation.call_args_list
    ]
    assert "cofounder_pass" in span_names
    final = [md for md in _span_metadata_calls(client) if "project" in md]
    assert final, "no cofounder_pass metadata reached the span"
    md = final[-1]
    assert md["project"] == "alpha"
    assert md["action"] == PROJECT_DECISION_PENDING
    assert md["latency_ms"] is not None
    assert md["status_flip"] is None  # decision-pending flips nothing


def test_span_enabled_error_records_error_and_pass_survives(
    projects_dir, tmp_path, monkeypatch
):
    make_project(projects_dir, "alpha", status="new")
    fake_mod, client = _make_mock_client()

    def boom(project, *args, **kwargs):
        raise RuntimeError("pipeline surprise")

    monkeypatch.setattr(run_pass_mod, "_project_pass", boom)
    with (
        patch("runtime.langfuse_setup.is_langfuse_enabled", return_value=True),
        patch("orchestration.observability.init_langfuse"),
        patch.dict("sys.modules", {"langfuse": fake_mod}),
    ):
        result = run_pass(
            settings=enabled_settings(projects_dir),
            state_file=tmp_path / "cofounder-state.json",
        )

    assert result.outcome == OUTCOME_COMPLETED  # containment still holds
    assert result.project_outcomes["alpha"] == PROJECT_ERROR
    levels = [
        call.kwargs.get("level")
        for call in client.update_current_span.call_args_list
    ]
    assert "ERROR" in levels
    error_md = [md for md in _span_metadata_calls(client) if md.get("action")]
    assert error_md and error_md[-1]["action"] == PROJECT_ERROR


def test_span_disabled_happy_logs_observation_row(projects_dir, tmp_path):
    make_project(projects_dir, "alpha", status="new")

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
    )

    assert result.project_outcomes["alpha"] == PROJECT_DECISION_PENDING
    rows = [
        json.loads(line)
        for line in observability._OBS_LOG.read_text(encoding="utf-8").splitlines()
    ]
    ours = [r for r in rows if r.get("name") == "cofounder_pass"]
    assert ours and ours[-1]["status"] == "ok"
    md = ours[-1]["metadata"]
    assert md["project"] == "alpha"
    assert md["action"] == PROJECT_DECISION_PENDING
    assert md["latency_ms"] is not None


def test_span_disabled_error_logs_error_row_and_pass_survives(
    projects_dir, tmp_path, monkeypatch
):
    make_project(projects_dir, "alpha", status="new")

    def boom(project, *args, **kwargs):
        raise RuntimeError("pipeline surprise")

    monkeypatch.setattr(run_pass_mod, "_project_pass", boom)

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert result.project_outcomes["alpha"] == PROJECT_ERROR
    rows = [
        json.loads(line)
        for line in observability._OBS_LOG.read_text(encoding="utf-8").splitlines()
    ]
    ours = [r for r in rows if r.get("name") == "cofounder_pass"]
    assert ours and ours[-1]["status"] == "error"
    assert ours[-1]["error_type"] == "RuntimeError"
    assert ours[-1]["metadata"]["action"] == PROJECT_ERROR


def test_observability_failure_never_breaks_the_pass(
    projects_dir, tmp_path, monkeypatch
):
    """A broken span helper runs the pipeline bare (Invariant 6, fail-open)."""
    path = make_project(projects_dir, "alpha", status="new")

    def broken_span(*args, **kwargs):
        raise RuntimeError("observability exploded")

    monkeypatch.setattr(observability, "orchestration_span", broken_span)

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert result.project_outcomes["alpha"] == PROJECT_DECISION_PENDING
    assert path.exists()  # the pipeline itself ran untouched
