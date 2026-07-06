"""Tests for the cofounder v2 WS4 persona work loop (cofounder/worktick.py).

Path map (one test per distinct path, adversarial first):
  Gates
  - cofounder_delegation kill switch = refused + counted, zero services
  - COFOUNDER_WORKLOOP_ENABLED default false = disabled
  - no delegable personas = idle
  - per-tick budget caps executions across personas
  Claim semantics
  - only cofounder_assignment messages are claimed (a foreign task_assignment
    to the same persona stays pending for its real consumer)
  - dry run NEVER claims (delivery still pending afterwards) and reports
    would-execute
  Rule 4 at claim
  - grant revoked after send = refused result + acked + audited, zero
    execution
  Draft mode (end-to-end on real services)
  - executes as the persona -> vault deliverable written (frontmatter,
    draft-for-review banner), cofounder_result 'done' sent to the cofounder,
    delivery acked (in-flight slot released), subtask completed (convoy
    done), audit row + daily-log line written
  - empty draft output = failed result, still acked, subtask NOT completed
  Code mode
  - dispatch receipt = 'dispatched' result with run id + branch, subtask
    fields updated, subtask NOT completed (WS5's job)
  - no receipt = failed result, still acked
  Containment
  - a raising execution seam fails THAT assignment (result 'failed', acked),
    never the tick
  Prompt
  - persona SOUL + repo notes + never-claim-executed rule ride the prompt
  Config
  - Rule-1 env round-trip
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

import config
from cofounder import delegate as delegate_mod
from cofounder import worktick as worktick_mod
from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import CofounderAssignmentPayload
from security import kill_switches

TODAY = "2026-07-05"
NOW = datetime(2026, 7, 5, 11, 0)

ENV_KEYS = (
    "HOMIE_KILLSWITCH_COFOUNDER_DELEGATION",
    "COFOUNDER_WORKLOOP_ENABLED",
    "COFOUNDER_WORKLOOP_MAX_PER_TICK",
    "COFOUNDER_WORKLOOP_CODE_WORKFLOW",
    "COFOUNDER_PROJECTS_DIR",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def reset_counters():
    kill_switches._REFUSAL_COUNTERS.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()


@pytest.fixture
def homie_root(tmp_path, monkeypatch):
    root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(root))
    return root


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Isolated MEMORY_DIR so deliverables + daily logs never hit the real vault."""
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    monkeypatch.setattr(config, "MEMORY_DIR", vault)
    return vault


@pytest.fixture
def services():
    db = OrchestrationDB(":memory:")
    return ConvoyService(db), MailboxService(db)


@pytest.fixture(autouse=True)
def isolated_audit(tmp_path, monkeypatch):
    path = tmp_path / "delegation-audit.jsonl"
    monkeypatch.setattr(
        delegate_mod,
        "_resolve_audit_path",
        lambda audit_path=None: Path(audit_path) if audit_path else path,
    )
    return path


@pytest.fixture(autouse=True)
def quiet_daily_log(monkeypatch):
    """Daily-log lines are captured, never written to the real vault."""
    lines: list[str] = []
    import shared

    monkeypatch.setattr(
        shared,
        "append_to_daily_log",
        lambda content, section_name="Entry": lines.append(content),
    )
    return lines


def _grant(homie_root: Path, persona: str, repos=None, soul: str | None = None):
    profile_root = homie_root / "profiles" / persona
    (profile_root / "state").mkdir(parents=True, exist_ok=True)
    (profile_root / "memory").mkdir(parents=True, exist_ok=True)
    cfg = {
        "persona": {"id": persona, "display_name": persona.title()},
        "delegation": {"repos": repos if repos is not None else []},
    }
    (profile_root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    if soul:
        (profile_root / "memory" / "SOUL.md").write_text(soul, encoding="utf-8")


def _send_assignment(services, persona, *, repo=None, mode="draft", task="draft the brief", n=1):
    """One real WS3-shaped assignment sitting in the persona's mailbox."""
    convoy_service, mailbox_service = services
    from orchestration.models import CreateConvoyInput, CreateSubtaskInput

    created = convoy_service.create_convoy(
        CreateConvoyInput(
            title=f"[cofounder] {task}",
            created_by="cofounder",
            subtasks=[CreateSubtaskInput(title=task, assigned_agent_id=persona)],
        )
    )
    subtask_id = created.subtasks[0].id
    payload = CofounderAssignmentPayload(
        subtask_id=subtask_id,
        task=task,
        repo=repo,
        agenda_ref=f"AGENDA-{TODAY}.md#{n}",
        mode=mode,
    )
    message = mailbox_service.send_cofounder_assignment(
        "cofounder", persona, payload, convoy_id=created.convoy.id
    )
    return created.convoy.id, subtask_id, message.id


def _tick(services, **kwargs):
    kwargs.setdefault("worktick_settings", config.get_cofounder_worktick_settings(enabled=True))
    kwargs.setdefault("settings", config.get_cofounder_settings())
    kwargs.setdefault("services", services)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("run_draft", lambda prompt: "# Brief\n- item one")
    kwargs.setdefault("dispatch_code", lambda *a: "run-123")
    return worktick_mod.run_worktick(**kwargs)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Rotation state never touches the real cofounder-state.json."""
    from cofounder import state as state_mod

    path = tmp_path / "worktick-state.json"
    monkeypatch.setattr(
        state_mod,
        "_resolve_state_file",
        lambda sf: Path(sf) if sf is not None else path,
    )
    return path


def _inbox_statuses(mailbox_service, persona, msg_type):
    out = []
    for mwd in mailbox_service.get_inbox(persona, msg_type=msg_type):
        for d in mwd.deliveries:
            if d.recipient_agent == persona:
                out.append(d.status)
    return out


# =============================================================================
# Gates
# =============================================================================


def test_kill_switch_refuses_and_counts(monkeypatch, homie_root, vault):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER_DELEGATION", "disabled")
    monkeypatch.setattr(
        worktick_mod, "_build_services", lambda: pytest.fail("services built")
    )
    result = worktick_mod.run_worktick(
        worktick_settings=config.get_cofounder_worktick_settings(enabled=True)
    )
    assert result.outcome == worktick_mod.OUTCOME_REFUSED
    assert kill_switches.get_refusal_counters()["cofounder_delegation"] == 1


def test_disabled_by_default(homie_root, vault):
    result = worktick_mod.run_worktick()
    assert result.outcome == worktick_mod.OUTCOME_DISABLED


def test_no_delegable_personas_is_idle(homie_root, vault, services):
    result = _tick(services)
    assert result.outcome == worktick_mod.OUTCOME_IDLE


def test_budget_caps_executions_across_personas(homie_root, vault, services):
    _grant(homie_root, "sales")
    _grant(homie_root, "marketing")
    _send_assignment(services, "sales", n=1)
    _send_assignment(services, "marketing", n=2)
    result = _tick(
        services,
        worktick_settings=config.get_cofounder_worktick_settings(
            enabled=True, max_per_tick=1
        ),
    )
    assert result.outcome == worktick_mod.OUTCOME_COMPLETED
    assert len(result.executed) == 1


# =============================================================================
# Claim semantics
# =============================================================================


def test_only_cofounder_assignments_are_claimed(homie_root, vault, services):
    """A foreign typed message to the same persona must stay pending for its
    real consumer — the msg_type claim filter is load-bearing."""
    from orchestration.models import TaskAssignmentPayload

    _grant(homie_root, "sales")
    convoy_service, mailbox_service = services
    mailbox_service.send_task_assignment(
        "coordinator", "sales", TaskAssignmentPayload(subtask_id=1, title="team work")
    )
    _send_assignment(services, "sales")
    result = _tick(services)
    assert len(result.executed) == 1
    assert _inbox_statuses(mailbox_service, "sales", "task_assignment") == ["pending"]


def test_dry_run_never_claims(homie_root, vault, services):
    _grant(homie_root, "sales")
    _send_assignment(services, "sales")
    _, mailbox_service = services
    result = _tick(
        services,
        dry_run=True,
        run_draft=lambda p: pytest.fail("draft ran on a dry run"),
    )
    assert result.executed[0]["status"] == "dry-run"
    assert _inbox_statuses(mailbox_service, "sales", worktick_mod.MSG_TYPE_ASSIGNMENT) == [
        "pending"
    ]


# =============================================================================
# Rule 4 at claim
# =============================================================================


def test_revoked_grant_refuses_at_claim(homie_root, vault, services, isolated_audit):
    _grant(homie_root, "sales", repos=["YourProduct"])
    _send_assignment(services, "sales", repo="YourProduct")
    # Revoke AFTER send: rewrite the config without the delegation block.
    cfg_path = homie_root / "profiles" / "sales" / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"persona": {"id": "sales"}}), encoding="utf-8"
    )
    # Still delegable-set member? No block -> not discovered; simulate the
    # sharper case: grant exists but repo scope was narrowed.
    cfg_path.write_text(
        yaml.safe_dump(
            {"persona": {"id": "sales"}, "delegation": {"repos": ["YourBusiness"]}}
        ),
        encoding="utf-8",
    )
    result = _tick(
        services, run_draft=lambda p: pytest.fail("executed despite revoked scope")
    )
    assert result.executed[0]["status"] == worktick_mod.EXEC_REFUSED
    _, mailbox_service = services
    # Delivery acked (no poison loop) and a refused result went up.
    assert _inbox_statuses(mailbox_service, "sales", worktick_mod.MSG_TYPE_ASSIGNMENT) == []
    results = mailbox_service.get_inbox("cofounder", msg_type="cofounder_result")
    assert json.loads(results[0].message.body)["status"] == "refused"
    rows = [json.loads(l) for l in isolated_audit.read_text().splitlines()]
    assert rows[-1]["outcome"] == "worktick-refused"


# =============================================================================
# Draft mode (end-to-end)
# =============================================================================


def test_draft_happy_path_full_round_trip(
    homie_root, vault, services, isolated_audit, quiet_daily_log
):
    _grant(homie_root, "sales", repos=["YourProduct"], soul="# Sales Soul\nSPEED_MARKER")
    convoy_id, subtask_id, _ = _send_assignment(
        services, "sales", repo="YourProduct", task="draft the follow-up checklist"
    )
    convoy_service, mailbox_service = services
    prompts: list[str] = []

    def draft(prompt):
        prompts.append(prompt)
        return "# Follow-up checklist\n- call the leads"

    result = _tick(services, run_draft=draft)
    assert result.outcome == worktick_mod.OUTCOME_COMPLETED
    record = result.executed[0]
    assert record["status"] == worktick_mod.EXEC_DONE

    # Deliverable in the vault, banner intact.
    files = list((vault / "cofounder" / "deliverables").glob("DELIVERABLE-*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "status: draft-for-review" in content
    assert "call the leads" in content
    assert "nothing" in content and "executed, deployed, or verified" in content

    # Result up to the cofounder with the deliverable path.
    results = mailbox_service.get_inbox("cofounder", msg_type="cofounder_result")
    body = json.loads(results[0].message.body)
    assert body["status"] == "done"
    assert body["deliverable_path"].endswith(files[0].name)
    assert body["subtask_id"] == subtask_id

    # Delivery acked -> in-flight slot released.
    assert _inbox_statuses(mailbox_service, "sales", worktick_mod.MSG_TYPE_ASSIGNMENT) == []

    # Convoy: subtask completed -> single-subtask convoy completed.
    subtask = convoy_service.get_subtask(convoy_id, subtask_id)
    assert subtask.status == "completed"

    # Ledger + daily log carried the dispatch (reflection routes it onward).
    rows = [json.loads(l) for l in isolated_audit.read_text().splitlines()]
    assert rows[-1]["outcome"] == "worktick-done"
    assert any("cofounder-worktick" in line for line in quiet_daily_log)

    # Prompt carried the persona voice + honesty rule.
    assert "SPEED_MARKER" in prompts[0]
    assert "operator review" in prompts[0]


def test_empty_draft_is_failed_but_acked(homie_root, vault, services):
    _grant(homie_root, "sales")
    convoy_id, subtask_id, _ = _send_assignment(services, "sales")
    convoy_service, mailbox_service = services
    result = _tick(services, run_draft=lambda p: "   ")
    assert result.executed[0]["status"] == worktick_mod.EXEC_FAILED
    assert _inbox_statuses(mailbox_service, "sales", worktick_mod.MSG_TYPE_ASSIGNMENT) == []
    subtask = convoy_service.get_subtask(convoy_id, subtask_id)
    assert subtask.status != "completed"


# =============================================================================
# Code mode
# =============================================================================


def _tracked_repo_index(vault: Path, tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    (vault / "REPOSITORIES.md").write_text(
        "# Index\n\n## Active Repositories\n\n"
        "| Slug | GitHub | Visibility | Default branch | Local path | Archon | Page |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| YourProduct | x | private | master | {repo_dir} | yes | p |\n",
        encoding="utf-8",
    )
    return repo_dir


def test_code_mode_dispatches_and_reports(homie_root, vault, tmp_path, services, monkeypatch):
    _grant(homie_root, "sales", repos=["YourProduct"])
    _tracked_repo_index(vault, tmp_path)
    monkeypatch.setattr(
        "cofounder.repos.resolve_repo",
        lambda slug, **kw: __import__("cofounder.repos", fromlist=["RepoResolution"]).RepoResolution(
            slug="YourProduct", local_path=tmp_path / "repo", default_branch="master"
        ),
    )
    convoy_id, subtask_id, _ = _send_assignment(
        services, "sales", repo="YourProduct", mode="code", task="add the audit page"
    )
    convoy_service, mailbox_service = services
    dispatched: list[tuple] = []

    def fake_dispatch(workflow, branch, message, repo_path, ref):
        dispatched.append((workflow, branch, message))
        return "run-777"

    result = _tick(services, dispatch_code=fake_dispatch)
    assert result.executed[0]["status"] == worktick_mod.EXEC_DISPATCHED
    workflow, branch, message = dispatched[0]
    assert workflow == "archon-ralph-dag"
    assert branch.startswith("cofounder/assign-")
    assert "pull request" in message  # v1 merge policy rides every dispatch

    body = json.loads(
        mailbox_service.get_inbox("cofounder", msg_type="cofounder_result")[0].message.body
    )
    assert body["status"] == "dispatched"
    assert body["run_id"] == "run-777"

    subtask = convoy_service.get_subtask(convoy_id, subtask_id)
    assert subtask.status != "completed"  # WS5 owns completion
    assert subtask.worktree_branch == branch
    assert _inbox_statuses(mailbox_service, "sales", worktick_mod.MSG_TYPE_ASSIGNMENT) == []


def test_code_mode_without_receipt_is_failed(homie_root, vault, tmp_path, services, monkeypatch):
    _grant(homie_root, "sales", repos=["YourProduct"])
    _tracked_repo_index(vault, tmp_path)
    monkeypatch.setattr(
        "cofounder.repos.resolve_repo",
        lambda slug, **kw: __import__("cofounder.repos", fromlist=["RepoResolution"]).RepoResolution(
            slug="YourProduct", local_path=tmp_path / "repo", default_branch="master"
        ),
    )
    _send_assignment(services, "sales", repo="YourProduct", mode="code")
    _, mailbox_service = services
    result = _tick(services, dispatch_code=lambda *a: None)
    assert result.executed[0]["status"] == worktick_mod.EXEC_FAILED
    body = json.loads(
        mailbox_service.get_inbox("cofounder", msg_type="cofounder_result")[0].message.body
    )
    assert body["status"] == "failed"


# =============================================================================
# Containment + config
# =============================================================================


def test_raising_seam_fails_one_assignment_not_the_tick(homie_root, vault, services):
    _grant(homie_root, "sales")
    _send_assignment(services, "sales")

    def exploding(prompt):
        raise RuntimeError("provider down")

    result = _tick(services, run_draft=exploding)
    assert result.outcome == worktick_mod.OUTCOME_COMPLETED
    assert result.executed[0]["status"] == worktick_mod.EXEC_FAILED
    assert result.exit_code == 0
    _, mailbox_service = services
    assert _inbox_statuses(mailbox_service, "sales", worktick_mod.MSG_TYPE_ASSIGNMENT) == []


def test_dry_run_previews_real_fairness_one_per_persona(homie_root, vault, services):
    """The dry run must mirror the real claim shape (limit=1 per persona) —
    never spend the whole budget on one persona's queue (review finding 1)."""
    _grant(homie_root, "marketing")
    _grant(homie_root, "sales")
    _send_assignment(services, "marketing", n=1)
    _send_assignment(services, "marketing", n=2)
    _send_assignment(services, "sales", n=3)
    result = _tick(services, dry_run=True)
    by_persona = [r["persona"] for r in result.executed]
    assert by_persona.count("marketing") == 1
    assert by_persona.count("sales") == 1


def test_rotation_prevents_starvation_across_ticks(homie_root, vault, services):
    """With budget < persona count, the starting persona rotates each tick
    so later-alphabet personas are served (review finding 2)."""
    _grant(homie_root, "marketing")
    _grant(homie_root, "sales")
    settings = config.get_cofounder_worktick_settings(enabled=True, max_per_tick=1)
    _send_assignment(services, "marketing", n=1)
    _send_assignment(services, "sales", n=2)

    first = _tick(services, worktick_settings=settings)
    assert [r["persona"] for r in first.executed] == ["marketing"]
    # marketing gets NEW work before the next tick — pre-rotation this
    # starves sales forever.
    _send_assignment(services, "marketing", n=3)
    second = _tick(services, worktick_settings=settings)
    assert [r["persona"] for r in second.executed] == ["sales"]


def test_stale_claim_recovers_and_executes(homie_root, vault, services, monkeypatch):
    """A claimed-never-acked assignment (process died mid-execution) ages
    back to pending and a later tick completes it (review finding 4)."""
    import time as time_mod

    _grant(homie_root, "sales")
    _send_assignment(services, "sales")
    _, mailbox_service = services
    # Simulate the crash: claim, then die (no ack).
    claimed = mailbox_service.claim_deliveries(
        "sales", limit=1, msg_type=worktick_mod.MSG_TYPE_ASSIGNMENT
    )
    assert claimed
    # Age the claim past the TTL by rewinding claimed_at in the DB.
    mailbox_service.db.conn.execute(
        "UPDATE agent_deliveries SET claimed_at = ? WHERE status = 'claimed'",
        (int(time_mod.time()) - worktick_mod.STALE_CLAIM_SECONDS - 60,),
    )
    result = _tick(services)
    assert result.executed and result.executed[0]["status"] == worktick_mod.EXEC_DONE
    assert _inbox_statuses(mailbox_service, "sales", worktick_mod.MSG_TYPE_ASSIGNMENT) == []


def test_fresh_claim_is_not_recovered(homie_root, vault, services):
    """A recently-claimed delivery (another consumer mid-flight) stays
    claimed — the sweep only heals PAST-TTL zombies."""
    _grant(homie_root, "sales")
    _send_assignment(services, "sales")
    _, mailbox_service = services
    mailbox_service.claim_deliveries(
        "sales", limit=1, msg_type=worktick_mod.MSG_TYPE_ASSIGNMENT
    )
    result = _tick(services, run_draft=lambda p: pytest.fail("stole a live claim"))
    assert result.outcome == worktick_mod.OUTCOME_IDLE
    assert _inbox_statuses(mailbox_service, "sales", worktick_mod.MSG_TYPE_ASSIGNMENT) == [
        "claimed"
    ]


def test_tampered_agenda_ref_cannot_traverse_or_inject(homie_root, vault, services):
    """A tampered mailbox body's agenda_ref must not escape the deliverables
    dir (path traversal) or shape a dangerous branch/argv element."""
    assert worktick_mod._ref_slug("../../etc/passwd") == "etcpasswd"
    assert worktick_mod._ref_slug("--force; rm -rf") == "forcerm-rf"
    assert worktick_mod._ref_slug("") == "assignment"
    assert worktick_mod._ref_slug("AGENDA-2026-07-05.md#3") == "2026-07-05-line3"

    _grant(homie_root, "sales")
    convoy_service, mailbox_service = services
    from orchestration.models import CreateConvoyInput, CreateSubtaskInput

    created = convoy_service.create_convoy(
        CreateConvoyInput(title="[cofounder] t", created_by="cofounder",
                          subtasks=[CreateSubtaskInput(title="t")])
    )
    payload = CofounderAssignmentPayload(
        subtask_id=created.subtasks[0].id,
        task="draft it",
        agenda_ref="../../escape",
    )
    mailbox_service.send_cofounder_assignment(
        "cofounder", "sales", payload, convoy_id=created.convoy.id
    )
    result = _tick(services)
    assert result.executed[0]["status"] == worktick_mod.EXEC_DONE
    files = list((vault / "cofounder" / "deliverables").glob("DELIVERABLE-*.md"))
    assert len(files) == 1  # inside the deliverables dir, nowhere else
    assert ".." not in files[0].name


def test_worktick_settings_resolve_env_at_call_time(monkeypatch):
    defaults = config.get_cofounder_worktick_settings()
    assert defaults == config.CofounderWorktickSettings(
        enabled=False, max_per_tick=2, code_workflow="archon-ralph-dag"
    )
    monkeypatch.setenv("COFOUNDER_WORKLOOP_ENABLED", "true")
    monkeypatch.setenv("COFOUNDER_WORKLOOP_MAX_PER_TICK", "5")
    monkeypatch.setenv("COFOUNDER_WORKLOOP_CODE_WORKFLOW", "archon-piv-loop")
    live = config.get_cofounder_worktick_settings()
    assert live == config.CofounderWorktickSettings(
        enabled=True, max_per_tick=5, code_workflow="archon-piv-loop"
    )
