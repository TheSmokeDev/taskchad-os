"""Tests for the autonomous delegation pass (cofounder/autopilot.py).

Path map (one test per distinct path, adversarial first):
  Gate order
  - cofounder_delegation kill switch = refused + counted, ZERO agenda read
  - COFOUNDER_DELEGATION_ENABLED false = structural no-op: no agenda read,
    no ledger read, no state write
  - flag on + no agenda for TODAY = no-agenda
  - yesterday's agenda alone = no-agenda (the transport's 2-day human
    fallback must never fire for autonomy: unexecuted proposals expire)
  - the transport is called with date=today and approved_by=cofounder-autopilot
  Selection
  - priority ascending, then line order
  - non-proposed lines (delegated/done/failed) are never re-attempted
  - COFOUNDER_AUTO_MAX_PRIORITY ceiling drops lower-priority lines
  - nothing eligible = idle (transport untouched)
  Budgets (Rule-2 physical reads)
  - autopilot rows in today's ledger spend the daily budget = budget-reached
  - a MANUAL (operator) ledger row does not spend the autopilot budget
  - the in-pass budget stops the loop mid-way
  - only `sent` spends budget — a refusal does not
  - COFOUNDER_AUTO_CODE_MAX_PER_DAY defers code lines, drafts still go
  - code rows already sent today count toward the code budget
  Retry memo (state file, agenda attempts-map pattern)
  - a line-specific refusal burns an attempt; at MAX_ATTEMPTS_PER_LINE the
    next tick skips the line
  - transient outcomes (capped/busy) burn nothing
  - yesterday's memo expires
  - a raising transport is contained per line (attempt burned, pass continues)
  - a corrupt state file fails open (pass still delegates)
  Dry run
  - selects and logs, delegates nothing, writes no state
  Mid-pass stop
  - a kill-switch refusal from the transport stops the remaining lines
  End to end (REAL transport, REAL services on an in-memory orchestration DB)
  - ledger row stamped cofounder-autopilot, agenda item flipped delegated,
    a second pass is idle
  Config (Rule 1)
  - defaults wide open, env round-trip, no def-time capture
  Heartbeat seam
  - fires between the agenda pass and the work loop, --test flows dry_run
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

import config
from cofounder import autopilot as autopilot_mod
from cofounder import delegate as delegate_mod
from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from security import kill_switches

TODAY = "2026-08-12"
NOW = datetime(2026, 8, 12, 6, 30)

ENV_KEYS = (
    "HOMIE_KILLSWITCH_COFOUNDER_DELEGATION",
    "HOMIE_KILLSWITCH_COFOUNDER",
    "COFOUNDER_DELEGATION_ENABLED",
    "COFOUNDER_MAX_ASSIGNMENTS_PER_DAY",
    "COFOUNDER_MAX_INFLIGHT_PER_PERSONA",
    "COFOUNDER_AUTO_MAX_PRIORITY",
    "COFOUNDER_AUTO_MAX_PER_DAY",
    "COFOUNDER_AUTO_CODE_MAX_PER_DAY",
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


@pytest.fixture(autouse=True)
def isolated_audit(tmp_path, monkeypatch):
    """Route the delegation ledger into tmp (never the real DATA_DIR)."""
    path = tmp_path / "delegation-audit.jsonl"
    monkeypatch.setattr(
        delegate_mod,
        "_resolve_audit_path",
        lambda audit_path=None: Path(audit_path) if audit_path else path,
    )
    return path


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "cofounder-state.json"


@pytest.fixture
def homie_root(tmp_path, monkeypatch):
    root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(root))
    return root


@pytest.fixture
def services():
    db = OrchestrationDB(":memory:")
    return ConvoyService(db), MailboxService(db)


class _Recorder:
    """A transport stub: records calls, replays queued outcomes."""

    def __init__(self, *outcomes: str, default: str = delegate_mod.OUTCOME_SENT):
        self.outcomes = list(outcomes)
        self.default = default
        self.calls: list[dict] = []

    def __call__(self, line_number, *, date=None, approved_by=None):
        self.calls.append(
            {"line": line_number, "date": date, "approved_by": approved_by}
        )
        outcome = self.outcomes.pop(0) if self.outcomes else self.default
        return delegate_mod.DelegationResult(
            outcome=outcome,
            message=f"line {line_number}: {outcome}",
            convoy_id=100 + line_number if outcome == delegate_mod.OUTCOME_SENT else None,
            persona="sales",
        )

    @property
    def lines(self) -> list[int]:
        return [call["line"] for call in self.calls]


def _item(n=1, persona="sales", repo="YourProduct", priority=1, mode="draft", **kw):
    base = {
        "n": n,
        "persona": persona,
        "repo": repo,
        "task": f"task {n}",
        "why": "w",
        "priority": priority,
        "mode": mode,
        "status": "proposed",
    }
    base.update(kw)
    return base


def _agenda(tmp_path: Path, items: list[dict], day: str = TODAY) -> Path:
    agendas = tmp_path / "cofounder" / "agendas"
    agendas.mkdir(parents=True, exist_ok=True)
    path = agendas / f"AGENDA-{day}.json"
    path.write_text(
        json.dumps({"date": day, "summary": "s", "items": items}), encoding="utf-8"
    )
    return path


def _ledger(path: Path, rows: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _sent_row(line: int, day: str = TODAY, approved_by=autopilot_mod.APPROVED_BY):
    return {
        "timestamp": f"{day}T12:00:00+00:00",
        "local_date": day,
        "outcome": delegate_mod.OUTCOME_SENT,
        "line": line,
        "approved_by": approved_by,
        "persona": "sales",
    }


def _run(tmp_path, state_file, **kwargs):
    kwargs.setdefault(
        "settings",
        config.get_cofounder_settings(projects_dir=tmp_path / "cofounder"),
    )
    kwargs.setdefault(
        "delegation_settings", config.get_cofounder_delegation_settings(enabled=True)
    )
    kwargs.setdefault("autopilot_settings", config.get_cofounder_autopilot_settings())
    kwargs.setdefault("state_file", state_file)
    kwargs.setdefault("now", NOW)
    return autopilot_mod.run_autopilot_pass(**kwargs)


# =============================================================================
# Gate order
# =============================================================================


def test_kill_switch_refuses_before_any_agenda_read(monkeypatch, tmp_path, state_file):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER_DELEGATION", "disabled")
    _agenda(tmp_path, [_item()])
    monkeypatch.setattr(
        autopilot_mod,
        "_read_agenda",
        lambda *a, **k: pytest.fail("agenda read past the kill switch"),
    )

    result = _run(tmp_path, state_file)

    assert result.outcome == autopilot_mod.OUTCOME_REFUSED
    assert kill_switches.get_refusal_counters()["cofounder_delegation"] == 1
    assert result.attempted == []


def test_flag_off_is_a_structural_noop(monkeypatch, tmp_path, state_file):
    _agenda(tmp_path, [_item()])
    for name in ("_read_agenda", "_sent_lines_today", "_attempts_today"):
        monkeypatch.setattr(
            autopilot_mod,
            name,
            lambda *a, **k: pytest.fail(f"{name} ran with the flag off"),
        )

    result = _run(
        tmp_path,
        state_file,
        delegation_settings=config.get_cofounder_delegation_settings(enabled=False),
    )

    assert result.outcome == autopilot_mod.OUTCOME_DISABLED
    assert not state_file.exists()


def test_flag_absent_defaults_armed(tmp_path, state_file):
    """The default env (flag absent) is ARMED — autonomy ships on (v1.4.0)."""
    assert config.get_cofounder_delegation_settings().enabled is True


def test_flag_false_resolves_from_env_when_not_injected(
    monkeypatch, tmp_path, state_file
):
    """An explicit env "false" is the propose-only retreat lever."""
    monkeypatch.setenv("COFOUNDER_DELEGATION_ENABLED", "false")
    _agenda(tmp_path, [_item()])

    result = autopilot_mod.run_autopilot_pass(
        settings=config.get_cofounder_settings(projects_dir=tmp_path / "cofounder"),
        state_file=state_file,
        now=NOW,
        delegate_line=lambda *a, **k: pytest.fail("delegated while dormant"),
    )

    assert result.outcome == autopilot_mod.OUTCOME_DISABLED


def test_missing_agenda_is_quiet(tmp_path, state_file):
    transport = _Recorder()

    result = _run(tmp_path, state_file, delegate_line=transport)

    assert result.outcome == autopilot_mod.OUTCOME_NO_AGENDA
    assert transport.calls == []


def test_yesterdays_agenda_is_never_resurrected(tmp_path, state_file):
    """delegate's no-date fallback walks back 2 days — a HUMAN convenience.
    Autonomy passes date= explicitly, so an unexecuted yesterday expires."""
    _agenda(tmp_path, [_item()], day="2026-08-11")
    transport = _Recorder()

    result = _run(tmp_path, state_file, delegate_line=transport)

    assert result.outcome == autopilot_mod.OUTCOME_NO_AGENDA
    assert transport.calls == []


def test_transport_gets_explicit_date_and_autopilot_stamp(tmp_path, state_file):
    _agenda(tmp_path, [_item()])
    transport = _Recorder()

    result = _run(tmp_path, state_file, delegate_line=transport)

    assert result.outcome == autopilot_mod.OUTCOME_COMPLETED
    assert transport.calls == [
        {"line": 1, "date": TODAY, "approved_by": "cofounder-autopilot"}
    ]
    assert result.sent == 1


# =============================================================================
# Selection
# =============================================================================


def test_priority_ascending_then_line_order(tmp_path, state_file):
    _agenda(
        tmp_path,
        [
            _item(n=1, priority=3),
            _item(n=2, priority=1),
            _item(n=3, priority=2),
            _item(n=4, priority=1),
        ],
    )
    transport = _Recorder()

    _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=9),
    )

    assert transport.lines == [2, 4, 3, 1]


def test_non_proposed_lines_are_never_reattempted(tmp_path, state_file):
    _agenda(
        tmp_path,
        [
            _item(n=1, status="delegated"),
            _item(n=2, status="done"),
            _item(n=3, status="failed"),
            _item(n=4),
        ],
    )
    transport = _Recorder()

    _run(tmp_path, state_file, delegate_line=transport)

    assert transport.lines == [4]


def test_priority_ceiling_drops_lower_priority_lines(tmp_path, state_file):
    _agenda(
        tmp_path,
        [_item(n=1, priority=1), _item(n=2, priority=2), _item(n=3, priority=3)],
    )
    transport = _Recorder()

    _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(
            max_priority=1, max_per_day=9
        ),
    )

    assert transport.lines == [1]


def test_no_eligible_lines_is_idle(tmp_path, state_file):
    _agenda(tmp_path, [_item(n=1, status="delegated")])
    transport = _Recorder()

    result = _run(tmp_path, state_file, delegate_line=transport)

    assert result.outcome == autopilot_mod.OUTCOME_IDLE
    assert transport.calls == []


# =============================================================================
# Budgets
# =============================================================================


def test_ledger_rows_spend_the_daily_budget(tmp_path, state_file, isolated_audit):
    _agenda(tmp_path, [_item(n=1), _item(n=2)])
    _ledger(isolated_audit, [_sent_row(7), _sent_row(8)])
    transport = _Recorder()

    result = _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=2),
    )

    assert result.outcome == autopilot_mod.OUTCOME_BUDGET
    assert transport.calls == []


def test_manual_approvals_do_not_spend_the_autopilot_budget(
    tmp_path, state_file, isolated_audit
):
    """The GLOBAL cap already counts operator sends (delegate._count_sent_today);
    the autopilot budget is a separate retreat lever over its own rows."""
    _agenda(tmp_path, [_item(n=1)])
    _ledger(isolated_audit, [_sent_row(5, approved_by="operator")])
    transport = _Recorder()

    result = _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=1),
    )

    assert result.outcome == autopilot_mod.OUTCOME_COMPLETED
    assert transport.lines == [1]


def test_yesterdays_autopilot_rows_do_not_spend_todays_budget(
    tmp_path, state_file, isolated_audit
):
    _agenda(tmp_path, [_item(n=1)])
    _ledger(isolated_audit, [_sent_row(1, day="2026-08-11")])
    transport = _Recorder()

    result = _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=1),
    )

    assert transport.lines == [1]
    assert result.sent == 1


def test_in_pass_budget_stops_the_loop(tmp_path, state_file):
    _agenda(tmp_path, [_item(n=1), _item(n=2), _item(n=3)])
    transport = _Recorder()

    _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=2),
    )

    assert transport.lines == [1, 2]


def test_only_sent_spends_budget(tmp_path, state_file):
    """A scope refusal costs nothing — the transport's cap counts sent rows
    only, and so does the autopilot budget."""
    _agenda(tmp_path, [_item(n=1), _item(n=2), _item(n=3)])
    transport = _Recorder(delegate_mod.OUTCOME_SCOPE_DENIED)

    result = _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=2),
    )

    assert transport.lines == [1, 2, 3]
    assert result.sent == 2


def test_code_budget_defers_code_lines_but_not_drafts(tmp_path, state_file):
    _agenda(
        tmp_path,
        [_item(n=1, mode="code"), _item(n=2, mode="draft"), _item(n=3, mode="code")],
    )
    transport = _Recorder()

    _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(
            max_per_day=9, code_max_per_day=0
        ),
    )

    assert transport.lines == [2]


def test_code_budget_of_one_sends_the_first_code_line_only(tmp_path, state_file):
    _agenda(tmp_path, [_item(n=1, mode="code"), _item(n=2, mode="code")])
    transport = _Recorder()

    _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(
            max_per_day=9, code_max_per_day=1
        ),
    )

    assert transport.lines == [1]


def test_code_rows_already_sent_today_count_against_the_code_budget(
    tmp_path, state_file, isolated_audit
):
    _agenda(tmp_path, [_item(n=1, mode="code"), _item(n=2, mode="code", status="done")])
    _ledger(isolated_audit, [_sent_row(2)])  # line 2 is a code line
    transport = _Recorder()

    _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(
            max_per_day=9, code_max_per_day=1
        ),
    )

    assert transport.calls == []


# =============================================================================
# Retry memo
# =============================================================================


def test_line_refused_twice_is_skipped_on_the_next_tick(tmp_path, state_file):
    _agenda(tmp_path, [_item(n=1)])
    denied = _Recorder(default=delegate_mod.OUTCOME_SCOPE_DENIED)

    for _ in range(autopilot_mod.MAX_ATTEMPTS_PER_LINE):
        _run(tmp_path, state_file, delegate_line=denied)
    assert denied.lines == [1, 1]

    third = _Recorder()
    result = _run(tmp_path, state_file, delegate_line=third)

    assert third.calls == []
    assert result.outcome == autopilot_mod.OUTCOME_IDLE
    memo = json.loads(state_file.read_text(encoding="utf-8"))["autopilot"]
    assert memo == {"date": TODAY, "lines": {"1": 2}}


def test_transient_outcomes_burn_no_attempt(tmp_path, state_file):
    _agenda(tmp_path, [_item(n=1)])
    transport = _Recorder(
        delegate_mod.OUTCOME_CAPPED,
        delegate_mod.OUTCOME_BUSY,
        delegate_mod.OUTCOME_CAPPED,
    )

    for _ in range(3):
        _run(tmp_path, state_file, delegate_line=transport)

    assert transport.lines == [1, 1, 1]
    assert not state_file.exists()


def test_yesterdays_memo_expires(tmp_path, state_file):
    state_file.write_text(
        json.dumps({"autopilot": {"date": "2026-08-11", "lines": {"1": 9}}}),
        encoding="utf-8",
    )
    _agenda(tmp_path, [_item(n=1)])
    transport = _Recorder()

    _run(tmp_path, state_file, delegate_line=transport)

    assert transport.lines == [1]


def test_raising_transport_is_contained_per_line(tmp_path, state_file):
    _agenda(tmp_path, [_item(n=1), _item(n=2)])
    calls: list[int] = []

    def boom(line_number, *, date=None, approved_by=None):
        calls.append(line_number)
        if line_number == 1:
            raise RuntimeError("transport exploded")
        return delegate_mod.DelegationResult(
            outcome=delegate_mod.OUTCOME_SENT, message="ok", convoy_id=1
        )

    result = _run(tmp_path, state_file, delegate_line=boom)

    assert calls == [1, 2]
    assert result.outcome == autopilot_mod.OUTCOME_COMPLETED
    assert result.sent == 1
    memo = json.loads(state_file.read_text(encoding="utf-8"))["autopilot"]
    assert memo["lines"] == {"1": 1}


def test_corrupt_state_file_fails_open(tmp_path, state_file):
    state_file.write_text("{not json", encoding="utf-8")
    _agenda(tmp_path, [_item(n=1)])
    transport = _Recorder()

    result = _run(tmp_path, state_file, delegate_line=transport)

    assert transport.lines == [1]
    assert result.sent == 1


# =============================================================================
# Dry run
# =============================================================================


def test_dry_run_selects_but_never_delegates(tmp_path, state_file, caplog):
    _agenda(tmp_path, [_item(n=1, priority=3), _item(n=2, priority=1)])
    transport = _Recorder()

    with caplog.at_level("INFO"):
        result = _run(tmp_path, state_file, dry_run=True, delegate_line=transport)

    assert transport.calls == []
    assert result.dry_run is True
    assert [row["line"] for row in result.attempted] == [2, 1]
    assert all(row["outcome"] == "dry-run" for row in result.attempted)
    assert not state_file.exists()
    assert "[dry-run] would delegate line 2" in caplog.text


def test_dry_run_respects_the_budget(tmp_path, state_file):
    _agenda(tmp_path, [_item(n=1), _item(n=2), _item(n=3)])

    result = _run(
        tmp_path,
        state_file,
        dry_run=True,
        delegate_line=_Recorder(),
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=1),
    )

    assert [row["line"] for row in result.attempted] == [1]


# =============================================================================
# Mid-pass stop
# =============================================================================


def test_kill_switch_refusal_from_the_transport_stops_the_pass(tmp_path, state_file):
    _agenda(tmp_path, [_item(n=1), _item(n=2), _item(n=3)])
    transport = _Recorder(delegate_mod.OUTCOME_REFUSED)

    result = _run(
        tmp_path,
        state_file,
        delegate_line=transport,
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=9),
    )

    assert transport.lines == [1]
    assert result.sent == 0


# =============================================================================
# End to end — REAL transport, REAL services
# =============================================================================


def _grant_persona(homie_root: Path, persona_id: str, repos: list[str] | None):
    profile_root = homie_root / "profiles" / persona_id
    (profile_root / "state").mkdir(parents=True, exist_ok=True)
    cfg: dict = {"persona": {"id": persona_id, "display_name": persona_id.title()}}
    if repos is not None:
        cfg["delegation"] = {"repos": repos}
    (profile_root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def test_end_to_end_self_delegation_through_the_real_transport(
    tmp_path, state_file, homie_root, services, isolated_audit, monkeypatch
):
    # The pass hands the transport a line NUMBER and a DATE, never a path:
    # both sides resolve the agenda from the same config (Rule 1, call-time),
    # which is what keeps this one delegation path instead of two.
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(tmp_path / "cofounder"))
    _grant_persona(homie_root, "sales", ["YourProduct"])
    agenda_path = _agenda(tmp_path, [_item(n=1), _item(n=2, persona="sales")])
    monkeypatch.setattr(delegate_mod, "_build_services", lambda: services)

    result = _run(
        tmp_path,
        state_file,
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=1),
    )

    assert result.outcome == autopilot_mod.OUTCOME_COMPLETED
    assert result.sent == 1
    rows = [json.loads(line) for line in isolated_audit.read_text().splitlines()]
    sent = [r for r in rows if r["outcome"] == delegate_mod.OUTCOME_SENT]
    assert len(sent) == 1
    assert sent[0]["approved_by"] == "cofounder-autopilot"
    assert sent[0]["local_date"] == TODAY
    item = json.loads(agenda_path.read_text(encoding="utf-8"))["items"][0]
    assert item["status"] == "delegated"
    assert item["convoy_id"] == sent[0]["convoy_id"]

    # The status flip is what makes the next tick idle for that line, and the
    # ledger row is what spends the day's autopilot budget.
    again = _run(
        tmp_path,
        state_file,
        autopilot_settings=config.get_cofounder_autopilot_settings(max_per_day=1),
    )
    assert again.outcome == autopilot_mod.OUTCOME_BUDGET


def test_end_to_end_ungranted_persona_is_denied_and_memoized(
    tmp_path, state_file, homie_root, services, isolated_audit, monkeypatch
):
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(tmp_path / "cofounder"))
    _grant_persona(homie_root, "sales", None)  # no delegation: block
    _agenda(tmp_path, [_item(n=1)])
    monkeypatch.setattr(delegate_mod, "_build_services", lambda: services)

    result = _run(tmp_path, state_file)

    assert result.sent == 0
    assert result.attempted[0]["outcome"] == delegate_mod.OUTCOME_SCOPE_DENIED
    memo = json.loads(state_file.read_text(encoding="utf-8"))["autopilot"]
    assert memo["lines"] == {"1": 1}


# =============================================================================
# Config (Rule 1)
# =============================================================================


def test_autopilot_defaults_are_wide_open(monkeypatch):
    monkeypatch.setenv("COFOUNDER_MAX_ASSIGNMENTS_PER_DAY", "7")
    settings = config.get_cofounder_autopilot_settings()
    assert settings.max_priority is None  # no ceiling: P3 auto-delegates
    assert settings.max_per_day == 7  # follows the global cap
    assert settings.code_max_per_day is None  # unlimited within the global cap


def test_autopilot_env_round_trip(monkeypatch):
    monkeypatch.setenv("COFOUNDER_AUTO_MAX_PRIORITY", "2")
    monkeypatch.setenv("COFOUNDER_AUTO_MAX_PER_DAY", "3")
    monkeypatch.setenv("COFOUNDER_AUTO_CODE_MAX_PER_DAY", "1")
    settings = config.get_cofounder_autopilot_settings()
    assert settings == (2, 3, 1)


def test_stamp_matches_what_the_brief_renders_as_self_assigned():
    """The ledger stamp is a cross-surface contract: /cofounder brief reads it
    to label a line "self-assigned". A rename here silently breaks that."""
    from cofounder import brief as brief_mod

    assert autopilot_mod.APPROVED_BY == brief_mod.AUTOPILOT_APPROVER


def test_autopilot_rule1_no_def_time_capture():
    defaults = config.get_cofounder_autopilot_settings.__defaults__
    assert defaults is not None
    assert all(default is None for default in defaults), (
        f"def-time default capture detected: {defaults}"
    )


# =============================================================================
# Heartbeat seam
# =============================================================================


def test_heartbeat_seam_runs_autopilot_between_agenda_and_worktick(
    monkeypatch, tmp_path
):
    import heartbeat

    calls: list[str] = []

    async def fake_run_heartbeat(test_mode: bool = False):
        calls.append("run_heartbeat")
        return None

    monkeypatch.setattr(heartbeat, "run_heartbeat", fake_run_heartbeat)
    monkeypatch.setattr(heartbeat, "ensure_directories", lambda: None)
    monkeypatch.setattr(heartbeat, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".claude" / "scripts").mkdir(parents=True)

    captured: list[dict] = []
    monkeypatch.setattr(
        "cofounder.run_pass.run_pass", lambda **kw: calls.append("run_pass")
    )
    monkeypatch.setattr(
        "cofounder.agenda.run_agenda_pass", lambda **kw: calls.append("agenda")
    )

    def fake_autopilot(**kwargs):
        calls.append("autopilot")
        captured.append(kwargs)

    monkeypatch.setattr("cofounder.autopilot.run_autopilot_pass", fake_autopilot)
    monkeypatch.setattr(
        "cofounder.worktick.run_worktick", lambda **kw: calls.append("worktick")
    )
    monkeypatch.setattr(
        "cofounder.report.run_report_pass", lambda **kw: calls.append("report")
    )
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--test"])

    heartbeat.main()

    assert calls == [
        "run_heartbeat",
        "run_pass",
        "agenda",
        "autopilot",
        "worktick",
        "report",
    ]
    assert captured == [{"dry_run": True}]
    assert not (tmp_path / ".claude" / "scripts" / "heartbeat_errors.log").exists()


def test_heartbeat_seam_failure_cannot_break_the_heartbeat(monkeypatch, tmp_path):
    import heartbeat

    async def fake_run_heartbeat(test_mode: bool = False):
        return None

    monkeypatch.setattr(heartbeat, "run_heartbeat", fake_run_heartbeat)
    monkeypatch.setattr(heartbeat, "ensure_directories", lambda: None)
    monkeypatch.setattr(heartbeat, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".claude" / "scripts").mkdir(parents=True)

    def boom(**kwargs):
        raise RuntimeError("autopilot exploded")

    monkeypatch.setattr("cofounder.run_pass.run_pass", lambda **kw: None)
    monkeypatch.setattr("cofounder.agenda.run_agenda_pass", lambda **kw: None)
    monkeypatch.setattr("cofounder.autopilot.run_autopilot_pass", boom)
    worktick_ran: list[bool] = []
    monkeypatch.setattr(
        "cofounder.worktick.run_worktick", lambda **kw: worktick_ran.append(True)
    )
    monkeypatch.setattr("cofounder.report.run_report_pass", lambda **kw: None)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--test"])

    heartbeat.main()  # must not raise

    log_text = (tmp_path / ".claude" / "scripts" / "heartbeat_errors.log").read_text(
        encoding="utf-8"
    )
    assert "RuntimeError: autopilot exploded" in log_text
    assert worktick_ran == [True]  # the later seams still run
