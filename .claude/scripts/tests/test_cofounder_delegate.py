"""Tests for the cofounder v2 WS3 delegation transport (cofounder/delegate.py).

Path map (one test per distinct path, adversarial first):
  Gates
  - cofounder_delegation kill switch = refused + counted + audit row,
    ZERO service construction
  - missing agenda json = friendly no-agenda; bad line = bad-line;
    already-delegated = no re-send
  Scope (Rule 4, fail-closed at send)
  - persona without a delegation: block = scope-denied + audit
  - repo not in delegation.repos = scope-denied
  - repo=None with the block present = allowed
  - unknown persona (no profile config) = scope-denied
  Caps (Rule-2 physical state)
  - daily cap counted from the sent ledger = capped
  - per-persona in-flight counted from un-acked mailbox deliveries = capped
  - inbox read failure = conservative refusal
  Happy path (REAL services on an in-memory orchestration DB)
  - approval works with COFOUNDER_DELEGATION_ENABLED absent/false
    (operator resolution #4: the flag gates autonomy, never approvals)
  - convoy created ([cofounder] title, subtask assigned to the persona),
    typed message sent (msg_type + payload round-trip), audit row written,
    JSON item stamped delegated, second run = already-delegated
  - services raising = error outcome + audit row, never a raise
  Surfaces
  - render_agenda_status: no-agenda text; items with delegation markers
  - agenda.py writes the machine-readable JSON sibling (n + proposed)
  - config resolver Rule-1 env round-trip
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import config
from cofounder import agenda as agenda_mod
from cofounder import delegate as delegate_mod
from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from security import kill_switches

TODAY = "2026-07-05"
NOW = datetime(2026, 7, 5, 10, 0)

ENV_KEYS = (
    "HOMIE_KILLSWITCH_COFOUNDER_DELEGATION",
    "HOMIE_KILLSWITCH_COFOUNDER",
    "COFOUNDER_DELEGATION_ENABLED",
    "COFOUNDER_MAX_ASSIGNMENTS_PER_DAY",
    "COFOUNDER_MAX_INFLIGHT_PER_PERSONA",
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
def services():
    db = OrchestrationDB(":memory:")
    return ConvoyService(db), MailboxService(db)


def _grant_persona(homie_root: Path, persona_id: str, repos: list[str] | None):
    """A persona profile with (or without) a delegation grant."""
    profile_root = homie_root / "profiles" / persona_id
    (profile_root / "state").mkdir(parents=True, exist_ok=True)
    cfg: dict = {"persona": {"id": persona_id, "display_name": persona_id.title()}}
    if repos is not None:
        cfg["delegation"] = {"repos": repos}
    (profile_root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _agenda(tmp_path: Path, items: list[dict]) -> Path:
    agendas = tmp_path / "cofounder" / "agendas"
    agendas.mkdir(parents=True, exist_ok=True)
    path = agendas / f"AGENDA-{TODAY}.json"
    path.write_text(
        json.dumps({"date": TODAY, "summary": "s", "items": items}), encoding="utf-8"
    )
    return path


def _item(n=1, persona="sales", repo="YourProduct", task="close the leads", **kw):
    base = {
        "n": n,
        "persona": persona,
        "repo": repo,
        "task": task,
        "why": "w",
        "priority": 1,
        "status": "proposed",
    }
    base.update(kw)
    return base


def _run(tmp_path, services, n=1, **kwargs):
    kwargs.setdefault("settings", config.get_cofounder_settings(projects_dir=tmp_path / "cofounder"))
    kwargs.setdefault("delegation_settings", config.get_cofounder_delegation_settings())
    kwargs.setdefault("services", services)
    kwargs.setdefault("now", NOW)
    return delegate_mod.run_agenda_line(n, **kwargs)


@pytest.fixture(autouse=True)
def isolated_audit(tmp_path, monkeypatch):
    """Route the delegation ledger into tmp (never the real DATA_DIR)."""
    path = tmp_path / "delegation-audit.jsonl"
    monkeypatch.setattr(
        delegate_mod, "_resolve_audit_path", lambda audit_path=None: Path(audit_path) if audit_path else path
    )
    return path


# =============================================================================
# Gates
# =============================================================================


def test_kill_switch_refuses_counts_and_audits(monkeypatch, tmp_path, isolated_audit):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER_DELEGATION", "disabled")

    def forbidden():
        pytest.fail("services built past the kill switch")

    monkeypatch.setattr(delegate_mod, "_build_services", forbidden)
    result = delegate_mod.run_agenda_line(1)
    assert result.outcome == delegate_mod.OUTCOME_REFUSED
    assert kill_switches.get_refusal_counters()["cofounder_delegation"] == 1
    rows = [json.loads(l) for l in isolated_audit.read_text().splitlines()]
    assert rows[-1]["outcome"] == "refused_killswitch"


def test_missing_agenda_is_friendly(tmp_path, services):
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_NO_AGENDA
    assert "No machine-readable agenda" in result.message


def test_bad_line_number(tmp_path, services):
    _agenda(tmp_path, [_item(n=1)])
    result = _run(tmp_path, services, n=7)
    assert result.outcome == delegate_mod.OUTCOME_BAD_LINE


def test_already_delegated_does_not_resend(tmp_path, services, homie_root):
    _grant_persona(homie_root, "sales", ["YourProduct"])
    _agenda(tmp_path, [_item(status="delegated", convoy_id=9)])
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_ALREADY
    assert "convoy 9" in result.message


# =============================================================================
# Scope (Rule 4, fail-closed)
# =============================================================================


def test_persona_without_delegation_block_denied(tmp_path, services, homie_root, isolated_audit):
    _grant_persona(homie_root, "sales", None)  # profile exists, NO grant
    _agenda(tmp_path, [_item()])
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_SCOPE_DENIED
    assert "no `delegation:` grant" in result.message
    rows = [json.loads(l) for l in isolated_audit.read_text().splitlines()]
    assert rows[-1]["outcome"] == "scope-denied"


def test_repo_not_granted_denied(tmp_path, services, homie_root):
    _grant_persona(homie_root, "sales", ["YourBusiness"])
    _agenda(tmp_path, [_item(repo="YourProduct")])
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_SCOPE_DENIED
    assert "not granted repo `YourProduct`" in result.message


def test_non_repo_task_allowed_with_block(tmp_path, services, homie_root):
    _grant_persona(homie_root, "sales", [])  # block present, empty repos
    _agenda(tmp_path, [_item(repo=None)])
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_SENT


def test_unknown_persona_denied(tmp_path, services, homie_root):
    _agenda(tmp_path, [_item(persona="ghost")])
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_SCOPE_DENIED
    assert "not a delegable target" in result.message


# =============================================================================
# Caps
# =============================================================================


def test_daily_cap_from_sent_ledger(tmp_path, services, homie_root, isolated_audit):
    _grant_persona(homie_root, "sales", ["YourProduct"])
    _agenda(tmp_path, [_item()])
    rows = [
        {"timestamp": f"{TODAY}T0{i}:00:00+00:00", "local_date": TODAY, "outcome": "sent"}
        for i in range(5)
    ]
    isolated_audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_CAPPED
    assert "Daily delegation cap" in result.message


def test_daily_cap_keys_on_local_date_not_utc_timestamp(
    tmp_path, services, homie_root, isolated_audit
):
    """The UTC-midnight-crossover bug (review Critical #1): a sent row whose
    UTC timestamp already rolled to TOMORROW still counts against TODAY's
    cap via local_date — and a row with today's UTC prefix but yesterday's
    local_date does NOT count."""
    _grant_persona(homie_root, "sales", ["YourProduct"])
    _agenda(tmp_path, [_item()])
    rows = [
        # Evening send west of UTC: local TODAY, UTC tomorrow.
        {"timestamp": "2026-07-06T01:30:00+00:00", "local_date": TODAY, "outcome": "sent"}
        for _ in range(5)
    ] + [
        # Yesterday-evening send: UTC prefix says TODAY, local_date says not.
        {"timestamp": f"{TODAY}T02:00:00+00:00", "local_date": "2026-07-04", "outcome": "sent"}
    ]
    isolated_audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert delegate_mod._count_sent_today(TODAY, isolated_audit) == 5
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_CAPPED


def test_sent_audit_row_carries_local_date(tmp_path, services, homie_root, isolated_audit):
    _grant_persona(homie_root, "sales", ["YourProduct"])
    _agenda(tmp_path, [_item()])
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_SENT
    rows = [json.loads(l) for l in isolated_audit.read_text().splitlines()]
    assert rows[-1]["local_date"] == TODAY


def test_concurrent_approval_is_serialized_to_busy(
    tmp_path, services, homie_root, monkeypatch
):
    """A held agenda lock (the double-tap's first call mid-flight) makes the
    second call return the friendly busy outcome instead of double-sending
    (review Critical #2)."""
    from shared import file_lock

    _grant_persona(homie_root, "sales", ["YourProduct"])
    agenda_path = _agenda(tmp_path, [_item()])
    monkeypatch.setattr(delegate_mod, "_LOCK_TIMEOUT_S", 0.1)
    with file_lock(agenda_path, timeout=1.0):
        result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_BUSY
    assert "mid-flight" in result.message
    # And with the lock released, the same call proceeds.
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_SENT


def test_traversal_shaped_persona_is_scope_denied(tmp_path, services, homie_root):
    """Tampered artifact defense: a traversal-shaped persona id must never
    reach the config loader (review defense-in-depth)."""
    _agenda(tmp_path, [_item(persona="../../evil")])
    result = _run(tmp_path, services)
    assert result.outcome == delegate_mod.OUTCOME_SCOPE_DENIED


def test_inflight_cap_from_mailbox(tmp_path, services, homie_root):
    _grant_persona(homie_root, "sales", ["YourProduct"])
    _agenda(tmp_path, [_item(n=1), _item(n=2, task="second task")])
    first = _run(tmp_path, services, n=1)
    assert first.outcome == delegate_mod.OUTCOME_SENT
    # The first assignment sits un-acked in the mailbox → cap (default 1).
    second = _run(tmp_path, services, n=2)
    assert second.outcome == delegate_mod.OUTCOME_CAPPED
    assert "un-acked" in second.message


@pytest.mark.parametrize("exc", [RuntimeError("db locked"), TypeError("signature drift")])
def test_inbox_failure_refuses_conservatively(
    tmp_path, services, homie_root, monkeypatch, exc
):
    """ANY inbox failure — including a future get_inbox signature drift
    (TypeError) — fails CLOSED; a silently vanishing cap is the opposite
    of conservative (review)."""
    _grant_persona(homie_root, "sales", ["YourProduct"])
    _agenda(tmp_path, [_item()])
    convoy_service, mailbox_service = services

    def broken(*a, **k):
        raise exc

    monkeypatch.setattr(mailbox_service, "get_inbox", broken)
    result = _run(tmp_path, (convoy_service, mailbox_service))
    assert result.outcome == delegate_mod.OUTCOME_CAPPED
    assert "refusing conservatively" in result.message


# =============================================================================
# Happy path (real services, in-memory DB)
# =============================================================================


def test_happy_path_full_round_trip(tmp_path, services, homie_root, isolated_audit):
    """Approval executes with COFOUNDER_DELEGATION_ENABLED absent (false):
    the flag gates autonomy, never operator approvals (resolution #4)."""
    assert config.get_cofounder_delegation_settings().enabled is False

    _grant_persona(homie_root, "sales", ["YourProduct"])
    agenda_path = _agenda(tmp_path, [_item()])
    convoy_service, mailbox_service = services

    result = _run(tmp_path, services, approved_by="smoke")
    assert result.outcome == delegate_mod.OUTCOME_SENT
    assert result.persona == "sales"
    assert result.convoy_id is not None and result.message_id is not None

    # Convoy: cofounder-authored, subtask assigned to the persona.
    convoy = convoy_service.get_convoy(result.convoy_id)
    assert convoy.convoy.title.startswith("[cofounder]")
    assert convoy.convoy.created_by == "cofounder"
    assert convoy.subtasks[0].assigned_agent_id == "sales"

    # Mailbox: typed message, payload round-trips.
    inbox = mailbox_service.get_inbox("sales", msg_type=delegate_mod.MSG_TYPE)
    assert len(inbox) == 1
    body = json.loads(inbox[0].message.body)
    assert body["task"] == "close the leads"
    assert body["repo"] == "YourProduct"
    assert body["agenda_ref"] == f"AGENDA-{TODAY}.md#1"
    assert body["subtask_id"] == convoy.subtasks[0].id

    # Ledger: one sent row with the approver.
    rows = [json.loads(l) for l in isolated_audit.read_text().splitlines()]
    assert rows[-1]["outcome"] == "sent"
    assert rows[-1]["approved_by"] == "smoke"

    # Artifact: the JSON item is stamped delegated.
    data = json.loads(agenda_path.read_text(encoding="utf-8"))
    assert data["items"][0]["status"] == "delegated"
    assert data["items"][0]["convoy_id"] == result.convoy_id

    # Idempotence: a second approval is a no-op.
    again = _run(tmp_path, services)
    assert again.outcome == delegate_mod.OUTCOME_ALREADY


def test_service_failure_is_error_with_audit(tmp_path, services, homie_root, monkeypatch, isolated_audit):
    _grant_persona(homie_root, "sales", ["YourProduct"])
    _agenda(tmp_path, [_item()])
    convoy_service, mailbox_service = services

    def broken(*a, **k):
        raise RuntimeError("convoy table gone")

    monkeypatch.setattr(convoy_service, "create_convoy", broken)
    result = _run(tmp_path, (convoy_service, mailbox_service))
    assert result.outcome == delegate_mod.OUTCOME_ERROR
    rows = [json.loads(l) for l in isolated_audit.read_text().splitlines()]
    assert rows[-1]["outcome"] == "error"


# =============================================================================
# Surfaces
# =============================================================================


def test_render_agenda_status_no_agenda(tmp_path):
    text = delegate_mod.render_agenda_status(
        date=TODAY,
        settings=config.get_cofounder_settings(projects_dir=tmp_path / "cofounder"),
    )
    assert "No agenda" in text


def test_render_agenda_status_lists_lines_with_markers(tmp_path):
    _agenda(
        tmp_path,
        [_item(n=1), _item(n=2, task="audit pages", status="delegated", convoy_id=3)],
    )
    text = delegate_mod.render_agenda_status(
        date=TODAY,
        settings=config.get_cofounder_settings(projects_dir=tmp_path / "cofounder"),
    )
    assert "1. [P1|draft] sales -> YourProduct: close the leads" in text
    # WS5 markers: delegated (awaiting execution) = ⏳; done = ✅.
    assert "⏳ 2." in text
    assert "/cofounder run <n>" in text


def test_agenda_pass_writes_json_sibling(tmp_path, monkeypatch):
    """WS2's writer produces the machine-readable half WS3 consumes."""
    scan = {
        "repos": ["YourProduct"],
        "repo_pages": {},
        "goals": "",
        "projects": [],
        "personas": [{"id": "sales", "name": "Sales", "role": "r"}],
    }
    monkeypatch.setattr(agenda_mod, "build_portfolio_scan", lambda s: scan)
    raw = json.dumps(
        {
            "summary": "s",
            "items": [
                {"persona": "sales", "repo": "YourProduct", "task": "t", "why": "", "priority": 2}
            ],
        }
    )
    result = agenda_mod.run_agenda_pass(
        settings=config.get_cofounder_settings(projects_dir=tmp_path / "cofounder"),
        agenda_settings=config.get_cofounder_agenda_settings(
            enabled=True, agenda_hour=0
        ),
        state_file=tmp_path / "state.json",
        now=NOW,
        propose=lambda prompt: raw,
        notify=lambda *a, **k: True,
    )
    assert result.outcome == agenda_mod.OUTCOME_COMPLETED
    json_path = tmp_path / "cofounder" / "agendas" / f"AGENDA-{TODAY}.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["items"][0]["n"] == 1
    assert data["items"][0]["status"] == "proposed"


def test_agenda_regeneration_refused_over_delegated_lines(tmp_path, monkeypatch):
    """--force regeneration must not orphan delegation stamps (review
    Important #3): once a line is delegated, the pass refuses to rewrite
    the day's pair; both artifacts stay byte-identical."""
    agenda_path = _agenda(
        tmp_path, [_item(status="delegated", convoy_id=4, message_id=7)]
    )
    before = agenda_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        agenda_mod,
        "build_portfolio_scan",
        lambda s: pytest.fail("scan ran despite delegated lines"),
    )
    result = agenda_mod.run_agenda_pass(
        force=True,
        settings=config.get_cofounder_settings(projects_dir=tmp_path / "cofounder"),
        agenda_settings=config.get_cofounder_agenda_settings(
            enabled=True, agenda_hour=0
        ),
        state_file=tmp_path / "state.json",
        now=NOW,
        propose=lambda prompt: pytest.fail("LLM ran despite delegated lines"),
    )
    assert result.outcome == agenda_mod.OUTCOME_HAS_DELEGATED
    assert agenda_path.read_text(encoding="utf-8") == before


def test_delegation_settings_resolve_env_at_call_time(monkeypatch):
    defaults = config.get_cofounder_delegation_settings()
    assert defaults == config.CofounderDelegationSettings(
        enabled=False, max_assignments_per_day=5, max_inflight_per_persona=1
    )
    monkeypatch.setenv("COFOUNDER_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("COFOUNDER_MAX_ASSIGNMENTS_PER_DAY", "9")
    monkeypatch.setenv("COFOUNDER_MAX_INFLIGHT_PER_PERSONA", "2")
    live = config.get_cofounder_delegation_settings()
    assert live == config.CofounderDelegationSettings(
        enabled=True, max_assignments_per_day=9, max_inflight_per_persona=2
    )


def test_validator_rejects_bad_delegation_shapes():
    from personas import services as personas_services

    with pytest.raises(personas_services.ConfigShapeError):
        personas_services.validate_config_dict({"delegation": "yes"})
    with pytest.raises(personas_services.ConfigShapeError):
        personas_services.validate_config_dict({"delegation": {"repos": "YourProduct"}})
    with pytest.raises(personas_services.ConfigShapeError):
        personas_services.validate_config_dict({"delegation": {"repos": [1]}})
    personas_services.validate_config_dict({"delegation": {"repos": ["YourProduct"]}})
