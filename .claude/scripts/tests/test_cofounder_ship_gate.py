"""US-020 — ship gate: default decider wiring, kill switch both directions,
and the full-loop --test smoke.

Everything the operator's Phase 9 flip depends on, locked at pipeline level.

Path map (one test per distinct path):
  - COFOUNDER_ENABLED defaults false; a bare run_pass() is a quiet disabled
    no-op (nothing in this story — or suite — enables it)
  - default decider wiring: run_pass with NO decide kwarg reaches
    cofounder.orchestrate.decide (the call-time module-attribute seam) and
    the decision executes through the normal pipeline
  - a broken orchestrate module fails open to decision-pending — the
    deterministic pass still completes
  - kill switch disabled refuses the PASS (refusal counted, quiet exit 0)
  - kill switch disabled refuses the NOTIFY (refusal counted, zero HTTP)
  - restartless both-directions toggle: disabled -> refused, re-enabled ->
    pass completes AND notify sends, disabled again -> refused — one
    process, no reload, no ordering dependence
  - the full-loop --test smoke: fixture project goes discover ->
    deterministic gates -> the REAL default-wired decider (stub runtime) ->
    decide-without-dispatch, with zero state writes, zero dispatch, zero
    HTTP, and the index doc untouched
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import cofounder
import config
from cofounder import engine_archon, orchestrate, project_model
from cofounder import notify as notify_mod
from cofounder.run_pass import (
    OUTCOME_COMPLETED,
    OUTCOME_DISABLED,
    OUTCOME_REFUSED,
    PROJECT_DECIDED,
    PROJECT_DECIDED_DRY,
    PROJECT_DECISION_PENDING,
    run_pass,
)
from orchestration import observability
from runtime import registry
from runtime.base import RuntimeResult
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
    "SECOND_BRAIN_BACKGROUND_QUALITY_MODEL",
)


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch, tmp_path):
    """No knob/kill-switch/Telegram env leaks (config's load_dotenv override);
    Langfuse pinned OFF + observation jsonl and notify audit redirected to tmp
    (US-013 gotcha); the workflow cache cleared so the real decider never
    reuses another test's archon CLI result. Telegram creds are DELETED, not
    stubbed — the two send-path tests install their own fake transport, and
    every other path must prove itself credential-free."""
    for key in COFOUNDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setattr(observability, "_OBS_LOG", tmp_path / "obs" / "obs.jsonl")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    orchestrate._workflow_cache.clear()
    yield
    orchestrate._workflow_cache.clear()


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
def no_http(monkeypatch):
    """Any HTTP attempt fails the test (proves the zero-HTTP invariants)."""

    def forbidden(*args, **kwargs):
        pytest.fail("HTTP call attempted; this path must never reach Telegram")

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", forbidden)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_fake_telegram(monkeypatch, *, message_id: int = 7020) -> list[dict]:
    """Record every outgoing sendMessage; answer a canned success payload."""
    sends: list[dict] = []

    def fake_urlopen(req, timeout=10):
        sends.append(
            {
                "url": req.full_url,
                "params": dict(urllib.parse.parse_qsl(req.data.decode())),
            }
        )
        return _FakeResponse({"ok": True, "result": {"message_id": message_id}})

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    return sends


def make_project(
    projects_dir: Path,
    slug: str,
    *,
    status: str = "new",
    activity: tuple[str, ...] = ("- 2026-07-04T08:00:00 created",),
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


class RuntimeStub:
    """Async run_with_fallback stand-in: records requests, returns canned text."""

    def __init__(self, text: str):
        self.text = text
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return RuntimeResult(
            text=self.text,
            runtime_lane="claude_native",
            provider="claude",
            model=request.model or "",
        )


# === the master enable ships (and stays) OFF ===


def test_cofounder_enabled_defaults_false_and_pass_noops():
    """Env cleared = the shipped default: enabled False, bare pass no-ops."""
    assert config.get_cofounder_settings().enabled is False

    result = run_pass()

    assert result.outcome == OUTCOME_DISABLED
    assert result.exit_code == 0


# === default decider wiring (the US-012 gap, closed here) ===


def test_default_decider_wiring_reaches_orchestrate(
    monkeypatch, projects_dir, tmp_path
):
    """run_pass with NO decide kwarg resolves cofounder.orchestrate.decide at
    call time and executes its decision through the normal pipeline."""
    path = make_project(projects_dir, "wired")
    calls = []

    def recording_decide(project, context):
        calls.append((project.slug, context["reason"]))
        return {"action": "park", "log_line": "parking for operator review"}

    monkeypatch.setattr(orchestrate, "decide", recording_decide)

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
    )

    assert calls == [("wired", "new-project")]
    assert result.project_outcomes["wired"] == PROJECT_DECIDED
    parsed = project_model.parse_project_file(path)
    assert parsed.frontmatter.status == "awaiting-human"
    assert "parking for operator review" in parsed.activity_log


def test_broken_orchestrate_fails_open_to_decision_pending(
    monkeypatch, projects_dir, tmp_path
):
    """A broken orchestrate module = pending decision, never a crashed pass."""
    path = make_project(projects_dir, "sturdy")
    before = path.read_bytes()
    monkeypatch.setattr(cofounder, "orchestrate", None)

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert result.project_outcomes["sturdy"] == PROJECT_DECISION_PENDING
    assert path.read_bytes() == before


# === kill switch, both directions, both surfaces ===


def test_kill_switch_disabled_refuses_pass_and_counts(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")

    result = run_pass(settings=enabled_settings(tmp_path), state_file=tmp_path / "s.json")

    assert result.outcome == OUTCOME_REFUSED
    assert result.exit_code == 0
    assert kill_switches.get_refusal_counters()["cofounder"] == 1


def test_kill_switch_disabled_refuses_notify_and_counts(
    monkeypatch, projects_dir, no_http
):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "555")
    project = SimpleNamespace(slug="gated", path=projects_dir / "gated.md")

    sent = notify_mod.notify(project, "gated: done", "done")

    assert sent is False  # no_http proves zero HTTP behind this
    assert kill_switches.get_refusal_counters()["cofounder"] == 1


def test_kill_switch_reenables_without_restart_both_surfaces(
    monkeypatch, projects_dir, tmp_path
):
    """disabled -> refused; re-enabled -> pass runs AND notify sends; disabled
    again -> refused. One process, no reload, no restart-order dependence."""
    path = make_project(projects_dir, "toggle")
    project = SimpleNamespace(slug="toggle", path=path)
    settings = enabled_settings(projects_dir)
    state_file = tmp_path / "cofounder-state.json"
    monkeypatch.setattr(orchestrate, "decide", None)  # decision-pending pass
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "555")
    sends = install_fake_telegram(monkeypatch)

    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    assert run_pass(settings=settings, state_file=state_file).outcome == OUTCOME_REFUSED
    assert notify_mod.notify(project, "toggle: done", "done") is False
    assert sends == []

    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "enabled")
    assert run_pass(settings=settings, state_file=state_file).outcome == OUTCOME_COMPLETED
    assert notify_mod.notify(project, "toggle: done", "done") is True
    assert len(sends) == 1

    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    assert run_pass(settings=settings, state_file=state_file).outcome == OUTCOME_REFUSED
    assert notify_mod.notify(project, "toggle: done", "done") is False
    assert len(sends) == 1  # no further HTTP after the re-disable
    assert kill_switches.get_refusal_counters()["cofounder"] == 4


# === the full-loop --test smoke (the AC's end-to-end proof) ===


VALID_REUSE = json.dumps(
    {
        "action": "reuse",
        "workflow": "wf-one",
        "message": "implement the spec; leave the work as a pull request",
        "status": "building",
        "plan": "- [ ] first slice",
        "log_line": "kicking off iteration 1",
    }
)


def test_full_loop_test_smoke(monkeypatch, projects_dir, tmp_path, no_http):
    """discover -> deterministic gates -> the REAL default-wired decider with
    a stubbed runtime -> decide-without-dispatch. Zero state writes, zero
    dispatch, zero HTTP, index doc untouched."""
    path = make_project(projects_dir, "smoke")
    before = path.read_bytes()
    index_doc = projects_dir.parent / "COFOUNDER-PROJECTS.md"
    index_doc.write_text(
        "---\ntags: [system, cofounder]\ndate: 2026-07-04\n---\n"
        "# Co-Founder Projects\n\n## Active Projects\n\n_No active projects._\n",
        encoding="utf-8",
    )
    index_before = index_doc.read_bytes()
    state_file = tmp_path / "cofounder-state.json"

    stub = RuntimeStub(text=VALID_REUSE)  # the model WANTS a dispatch
    monkeypatch.setattr(registry, "run_with_fallback", stub)
    monkeypatch.setattr(orchestrate, "available_workflows", lambda *a, **k: ["wf-one"])
    monkeypatch.setattr(orchestrate, "repo_workflow_preferences", lambda *a, **k: "")
    monkeypatch.setattr(orchestrate, "_repo_local_path", lambda slug: None)

    def forbidden_dispatch(*args, **kwargs):
        pytest.fail("dispatch must never run in --test")

    monkeypatch.setattr(engine_archon, "dispatch", forbidden_dispatch)

    result = run_pass(
        dry_run=True,
        settings=enabled_settings(projects_dir),
        state_file=state_file,
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert result.dry_run is True
    assert result.project_outcomes["smoke"] == PROJECT_DECIDED_DRY
    # The decision really happened, through the default-wired decider.
    assert len(stub.requests) == 1
    assert "Build smoke." in stub.requests[0].prompt
    # ... and nothing was executed or written.
    assert not state_file.exists()
    assert path.read_bytes() == before
    assert index_doc.read_bytes() == index_before
