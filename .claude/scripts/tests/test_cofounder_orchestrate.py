"""US-012 — LLM orchestration pass: decide-only, strict JSON.

Paths, adversarial first:

parse_decision (pure):
  - unknown key `spec` rejected (the Spec write-back vector)
  - invented status rejected; unknown action rejected; multi-action rejected
  - plan carrying an H2 heading rejected (the other Spec-shadow vector)
  - multi-line log_line rejected; non-string scalars rejected
  - garbage / non-object JSON raises; fenced + prose-wrapped JSON accepted
  - valid minimal and full decisions normalize to all six keys

decide (runtime patched):
  - RuntimeRequest is QUALITY tier (call-time env proof), no tools, 1 turn
  - invalid JSON = no-op + ONE [warn] Activity Log line; Spec/plan untouched
  - invalid JSON on a dry run writes NOTHING
  - runtime failure fails open to None + [warn] line

inputs:
  - available_workflows caches per repo path; TTL expiry refetches;
    CLI failure fails open to []; child env is CLAUDECODE-scrubbed
  - _workflow_names tolerates strings / objects / wrapper-dict shapes
  - repo_workflow_preferences reads the per-repo page section; missing page
    and greenfield degrade to ""

prompt:
  - last ~10 Activity Log lines only; steering + workflows + completion
    check included; spec capped with a [truncated] marker

end-to-end (the recorded --test proof):
  - run_pass(dry_run=True, decide=orchestrate.decide) with a stubbed runtime
    logs the decision and dispatches NOTHING (zero writes, zero dispatch)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import config
from cofounder import engine_archon, orchestrate, project_model
from cofounder.orchestrate import DecisionParseError, parse_decision
from cofounder.run_pass import PROJECT_DECIDED_DRY, run_pass
from orchestration import observability
from runtime import registry
from runtime.base import RuntimeResult
from runtime.capabilities import TEXT_REASONING

ENV_KEYS = (
    "COFOUNDER_ENABLED",
    "COFOUNDER_PROJECTS_DIR",
    "COFOUNDER_MAX_ITERATIONS",
    "COFOUNDER_MAX_WALL_CLOCK_HOURS",
    "COFOUNDER_MAX_CONCURRENT",
    "COFOUNDER_NOTIFY_LEVELS",
    "COFOUNDER_ZOMBIE_STALE_MINUTES",
    "COFOUNDER_ARCHON_DB",
    "HOMIE_KILLSWITCH_COFOUNDER",
    "SECOND_BRAIN_BACKGROUND_QUALITY_MODEL",
)


@pytest.fixture(autouse=True)
def clean_env_and_cache(monkeypatch, tmp_path):
    """No leaked knobs (.env load_dotenv override) and a fresh workflow cache.

    Langfuse pinned OFF + observation jsonl redirected to tmp: the run_pass
    e2e test crosses the US-013 cofounder_pass span, and a live .env pointing
    at a dead server would burn OTEL retries / append to the real .omx log.
    """
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setattr(observability, "_OBS_LOG", tmp_path / "obs" / "obs.jsonl")
    orchestrate._workflow_cache.clear()
    yield
    orchestrate._workflow_cache.clear()


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


def ctx(**overrides):
    base = {
        "reason": "new-project",
        "job_status": None,
        "working_path": None,
        "new_steering": [],
        "iterations": 0,
        "in_flight": 0,
        "max_concurrent": 2,
        "dry_run": False,
    }
    base.update(overrides)
    return base


class RuntimeStub:
    """Async run_with_fallback stand-in: records requests, returns canned text."""

    def __init__(self, text: str = "", error: Exception | None = None):
        self.text = text
        self.error = error
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return RuntimeResult(
            text=self.text,
            runtime_lane="claude_native",
            provider="claude",
            model=request.model or "",
        )


@pytest.fixture()
def patch_runtime(monkeypatch):
    def _patch(text: str = "", error: Exception | None = None) -> RuntimeStub:
        stub = RuntimeStub(text=text, error=error)
        monkeypatch.setattr(registry, "run_with_fallback", stub)
        return stub

    return _patch


@pytest.fixture()
def patch_inputs(monkeypatch):
    """Isolate decide() from the archon CLI and the vault repo index."""
    monkeypatch.setattr(
        orchestrate, "available_workflows", lambda *a, **k: ["wf-one", "wf-two"]
    )
    monkeypatch.setattr(orchestrate, "repo_workflow_preferences", lambda *a, **k: "")
    monkeypatch.setattr(orchestrate, "_repo_local_path", lambda slug: None)


VALID_REUSE = json.dumps(
    {
        "action": "reuse",
        "workflow": "wf-one",
        "message": "implement the spec; open a PR for review",
        "status": "building",
        "plan": "- [ ] first slice",
        "log_line": "kicking off iteration 1",
    }
)


# === parse_decision — adversarial first ===


def test_parse_unknown_key_spec_rejected():
    raw = json.dumps({"action": "park", "spec": "rewritten spec text"})
    with pytest.raises(DecisionParseError, match="unknown keys"):
        parse_decision(raw)


def test_parse_invented_status_rejected():
    raw = json.dumps({"action": "park", "status": "in_progress"})
    with pytest.raises(DecisionParseError, match="invented status"):
        parse_decision(raw)


def test_parse_unknown_action_rejected():
    with pytest.raises(DecisionParseError, match="action"):
        parse_decision(json.dumps({"action": "deploy-to-prod"}))


def test_parse_action_must_be_one_string_move():
    with pytest.raises(DecisionParseError, match="action"):
        parse_decision(json.dumps({"action": ["reuse", "test"]}))


def test_parse_plan_with_h2_heading_rejected():
    raw = json.dumps({"action": "park", "plan": "## Spec\nnew spec body"})
    with pytest.raises(DecisionParseError, match="H2"):
        parse_decision(raw)


def test_parse_multiline_log_line_rejected():
    raw = json.dumps({"action": "park", "log_line": "one\ntwo"})
    with pytest.raises(DecisionParseError, match="single line"):
        parse_decision(raw)


def test_parse_non_string_workflow_rejected():
    raw = json.dumps({"action": "reuse", "workflow": 7})
    with pytest.raises(DecisionParseError, match="workflow"):
        parse_decision(raw)


def test_parse_garbage_raises():
    with pytest.raises(DecisionParseError, match="not valid JSON"):
        parse_decision("the next move is to reuse wf-one")


def test_parse_non_object_json_raises():
    with pytest.raises(DecisionParseError, match="not a JSON object"):
        parse_decision(json.dumps(["reuse"]))


def test_parse_valid_minimal_park_normalizes_all_keys():
    decision = parse_decision(json.dumps({"action": "PARK"}))
    assert decision == {
        "action": "park",
        "workflow": None,
        "message": None,
        "status": None,
        "plan": None,
        "log_line": None,
    }


def test_parse_valid_full_reuse_round_trip():
    decision = parse_decision(VALID_REUSE)
    assert decision["action"] == "reuse"
    assert decision["workflow"] == "wf-one"
    assert decision["status"] == "building"
    assert decision["plan"] == "- [ ] first slice"


def test_parse_fenced_json_accepted():
    decision = parse_decision("```json\n" + VALID_REUSE + "\n```")
    assert decision["action"] == "reuse"


def test_parse_json_embedded_in_prose_accepted():
    decision = parse_decision("Here is my decision:\n" + VALID_REUSE + "\nGood luck!")
    assert decision["workflow"] == "wf-one"


# === decide — the seam callable (runtime patched) ===


def test_decide_returns_decision_and_quality_tier_request(
    tmp_path, monkeypatch, patch_runtime, patch_inputs
):
    monkeypatch.setenv("SECOND_BRAIN_BACKGROUND_QUALITY_MODEL", "quality-tier-test")
    stub = patch_runtime(text=VALID_REUSE)
    path = make_project(tmp_path, "alpha")
    project = project_model.parse_project_file(path)

    decision = orchestrate.decide(
        project, ctx(new_steering=["- 2026-07-04 [steer] ship the MVP"])
    )

    assert decision is not None and decision["action"] == "reuse"
    assert len(stub.requests) == 1
    request = stub.requests[0]
    # QUALITY background tier, resolved at CALL time (Rule 1) — never the
    # interactive flagship; decide-only: no tools, one turn.
    assert request.model == "quality-tier-test"
    assert request.allowed_tools == []
    assert request.max_turns <= 2
    assert request.task_name == "cofounder_orchestrate"
    assert request.capability == TEXT_REASONING
    # Inputs assembled in code landed in the prompt.
    assert "Build alpha." in request.prompt
    assert "[steer] ship the MVP" in request.prompt
    assert "wf-one" in request.prompt


def test_decide_invalid_json_is_noop_with_one_warn_line(
    tmp_path, patch_runtime, patch_inputs
):
    patch_runtime(text="I think we should probably reuse wf-one?")
    path = make_project(tmp_path, "beta")
    project = project_model.parse_project_file(path)

    decision = orchestrate.decide(project, ctx())

    assert decision is None
    after = project_model.parse_project_file(path)
    assert after.spec == project.spec  # no partial write anywhere near the Spec
    assert after.plan == project.plan
    warn_lines = [
        line for line in after.activity_log.splitlines() if "[warn]" in line
    ]
    assert len(warn_lines) == 1
    assert "orchestration decision invalid" in warn_lines[0]


def test_decide_invalid_json_on_dry_run_writes_nothing(
    tmp_path, patch_runtime, patch_inputs
):
    patch_runtime(text="not json")
    path = make_project(tmp_path, "gamma")
    project = project_model.parse_project_file(path)
    before = path.read_bytes()

    decision = orchestrate.decide(project, ctx(dry_run=True))

    assert decision is None
    assert path.read_bytes() == before


def test_decide_runtime_failure_fails_open(tmp_path, patch_runtime, patch_inputs):
    patch_runtime(error=RuntimeError("lane down"))
    path = make_project(tmp_path, "delta")
    project = project_model.parse_project_file(path)

    decision = orchestrate.decide(project, ctx())

    assert decision is None
    after = project_model.parse_project_file(path)
    assert "orchestration step failed (RuntimeError)" in after.activity_log


# === prompt assembly ===


def test_build_prompt_uses_last_ten_log_lines_only(tmp_path):
    activity = tuple(f"- 2026-07-03T08:00:00 line-{n:02d}" for n in range(1, 16))
    path = make_project(tmp_path, "tail", activity=activity)
    project = project_model.parse_project_file(path)

    prompt = orchestrate.build_prompt(project, ctx(), [], "")

    assert "line-15" in prompt and "line-06" in prompt
    assert "line-05" not in prompt


def test_build_prompt_includes_steering_workflows_prefs_and_check(tmp_path):
    path = make_project(tmp_path, "rich", completion_check="pytest -x")
    project = project_model.parse_project_file(path)

    prompt = orchestrate.build_prompt(
        project,
        ctx(new_steering=["- ts [steer] focus on auth"]),
        ["wf-a", "wf-b"],
        "- prefer wf-a for features",
    )

    assert "[steer] focus on auth" in prompt
    assert "wf-a, wf-b" in prompt
    assert "- prefer wf-a for features" in prompt
    assert "pytest -x" in prompt
    assert "never invent a status" in prompt.lower()


def test_build_prompt_caps_the_spec(tmp_path):
    project = project_model.CofounderProject(
        path=tmp_path / "big.md",
        title="big",
        frontmatter=project_model.ProjectFrontmatter(),
        spec="S" * (orchestrate.SPEC_PROMPT_CAP + 500) + "SPEC-TAIL-SENTINEL",
        plan="- [ ] p",
        activity_log="",
    )

    prompt = orchestrate.build_prompt(project, ctx(), [], "")

    assert "[truncated]" in prompt
    assert "SPEC-TAIL-SENTINEL" not in prompt


# === available workflows (cached CLI list) ===


class FetchRecorder:
    def __init__(self, names=("wf-one",)):
        self.names = list(names)
        self.calls = []

    def __call__(self, repo_path, archon_bin=None):
        self.calls.append(repo_path)
        return list(self.names)


def test_available_workflows_caches_per_repo_path(monkeypatch):
    fetch = FetchRecorder()
    monkeypatch.setattr(orchestrate, "_fetch_workflow_list", fetch)

    first = orchestrate.available_workflows("C:/repos/a")
    second = orchestrate.available_workflows("C:/repos/a")
    other = orchestrate.available_workflows("C:/repos/b")

    assert first == second == other == ["wf-one"]
    assert len(fetch.calls) == 2  # a cached + b fetched


def test_available_workflows_ttl_expiry_refetches(monkeypatch):
    fetch = FetchRecorder()
    monkeypatch.setattr(orchestrate, "_fetch_workflow_list", fetch)

    orchestrate.available_workflows("C:/repos/a", ttl_seconds=0)
    orchestrate.available_workflows("C:/repos/a", ttl_seconds=0)

    assert len(fetch.calls) == 2


def test_fetch_workflow_list_cli_failure_fails_open(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("archon not installed")

    monkeypatch.setattr(orchestrate.subprocess, "run", boom)
    assert orchestrate._fetch_workflow_list("C:/repos/a") == []


def test_fetch_workflow_list_scrubs_claudecode_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDECODE_ENTRYPOINT", "cli")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='["wf-live"]', stderr="")

    monkeypatch.setattr(orchestrate.subprocess, "run", fake_run)

    names = orchestrate._fetch_workflow_list(tmp_path)

    assert names == ["wf-live"]
    assert captured["argv"][1:] == ["workflow", "list", "--json"]
    assert captured["cwd"] == str(tmp_path)
    assert not any(k.upper().startswith("CLAUDECODE") for k in captured["env"])


def test_workflow_names_tolerates_all_json_shapes():
    assert orchestrate._workflow_names('["a", "b"]') == ["a", "b"]
    assert orchestrate._workflow_names('[{"name": "c"}, {"name": ""}]') == ["c"]
    assert orchestrate._workflow_names('{"workflows": [{"name": "d"}]}') == ["d"]
    assert orchestrate._workflow_names("not json") == []


# === per-repo page workflow preferences ===


def test_repo_workflow_preferences_reads_section(tmp_path):
    pages = tmp_path / "repositories"
    pages.mkdir()
    (pages / "myrepo.md").write_text(
        "# myrepo\n\n## Workflow Preferences\n- prefer archon-ralph-dag\n\n"
        "## Dispatch History\n- none\n",
        encoding="utf-8",
    )

    prefs = orchestrate.repo_workflow_preferences("myrepo", memory_dir=tmp_path)

    assert "prefer archon-ralph-dag" in prefs
    assert "Dispatch History" not in prefs


def test_repo_workflow_preferences_missing_page_is_empty(tmp_path):
    assert orchestrate.repo_workflow_preferences("ghost", memory_dir=tmp_path) == ""


def test_repo_workflow_preferences_greenfield_and_blank_are_empty(tmp_path):
    assert orchestrate.repo_workflow_preferences("greenfield", memory_dir=tmp_path) == ""
    assert orchestrate.repo_workflow_preferences("", memory_dir=tmp_path) == ""
    assert orchestrate.repo_workflow_preferences(None, memory_dir=tmp_path) == ""


# === the recorded --test end-to-end: decide-without-dispatch ===


class DispatchRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("dispatch must never run in --test")


def test_dry_run_pass_decides_without_dispatch(
    tmp_path, monkeypatch, patch_runtime, caplog
):
    """The AC proof: --test + the REAL decider = decision logged, nothing
    dispatched, zero writes."""
    projects_dir = tmp_path / "cofounder"
    projects_dir.mkdir()
    path = make_project(projects_dir, "e2e")
    before = path.read_bytes()
    state_file = tmp_path / "cofounder-state.json"
    stub = patch_runtime(text=VALID_REUSE)  # the model WANTS a dispatch
    dispatch = DispatchRecorder()
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)
    monkeypatch.setattr(orchestrate, "available_workflows", lambda *a, **k: ["wf-one"])

    with caplog.at_level(logging.INFO, logger="cofounder.run_pass"):
        result = run_pass(
            dry_run=True,
            settings=config.get_cofounder_settings(
                enabled=True, projects_dir=projects_dir
            ),
            state_file=state_file,
            decide=orchestrate.decide,
        )

    assert result.project_outcomes["e2e"] == PROJECT_DECIDED_DRY
    # Decision logged (decide ran through the real assembly + parse path).
    assert len(stub.requests) == 1
    assert "Build e2e." in stub.requests[0].prompt
    assert any(
        "decision" in record.message and "not executed" in record.message
        for record in caplog.records
    )
    # Nothing dispatched, nothing written.
    assert dispatch.calls == []
    assert not state_file.exists()
    assert path.read_bytes() == before
