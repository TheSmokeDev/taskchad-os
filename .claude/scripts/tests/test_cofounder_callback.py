"""Tests for the co-founder outcome callback — autonomy T6 (#414).

Test design split by code path:
  1. Settings resolver — Rule 1 call-time resolution, default ON.
  2. Gates — knob off, kill switch, cold start (no watermark yet).
  3. Trigger set — autopilot sends, terminal agenda flips, grant blockers,
     and the operator-approved send that is deliberately NOT a trigger.
  4. Watermark — fires once per new outcome set, never twice; a build with
     nothing new leaves the stored boundary untouched.
  5. Partial failure — an unreadable ledger or agenda dir degrades to the
     source that worked; a broken watermark read fails open to a bare turn.
  6. Engine seam — the sibling gate shape, off-loop execution, fail-open,
     suffix ORDER (after crypto, before the session brief), and the proof
     that firing never touches session-brief state.

No test touches live vault/state files — every path is tmp_path-scoped and
clocks are injected via ``now=``.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402
from cognition import cofounder_callback as callback_mod  # noqa: E402

WATERMARK = datetime(2026, 6, 11, 22, 0)
NOW = datetime(2026, 6, 12, 6, 30)
OVERNIGHT = datetime(2026, 6, 12, 3, 15)


@pytest.fixture(autouse=True)
def _sweep_callback_env(monkeypatch):
    """Neither knob nor kill switch leaks in from the operator's own .env."""
    monkeypatch.delenv("COFOUNDER_CALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)


def _state_dir(tmp_path: Path, watermark: datetime | None = WATERMARK) -> Path:
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    if watermark is not None:
        callback_mod.write_callback_watermark(watermark, state_dir=root, now=NOW)
    return root


def _stored_watermark(state_dir: Path) -> datetime | None:
    return callback_mod.read_callback_watermark(state_dir=state_dir)


def _ledger(tmp_path: Path, rows: list[dict] | None = None) -> Path:
    path = tmp_path / "cofounder_delegation.jsonl"
    lines = [json.dumps(row) for row in (rows or [])]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def _row(
    *,
    outcome: str = "sent",
    at: datetime = OVERNIGHT,
    aware_utc: bool = False,
    line: int = 2,
    persona: str = "marketing",
    approved_by: str | None = "cofounder-autopilot",
    detail: str = "Draft the lead-delivery acceptance checklist",
) -> dict:
    return {
        "timestamp": _utc_iso_for_local(at) if aware_utc else at.isoformat(),
        "local_date": at.date().isoformat(),
        "integration": "cofounder",
        "action": "delegate",
        "persona": persona,
        "line": line,
        "outcome": outcome,
        "detail": detail,
        "convoy_id": 21,
        "message_id": 701,
        "approved_by": approved_by,
    }


def _utc_iso_for_local(local_dt: datetime) -> str:
    """Aware-UTC ISO string whose LOCAL equivalent is ``local_dt`` (the shape
    ``delegate._audit`` actually writes)."""
    local_tz = datetime.now().astimezone().tzinfo
    return local_dt.replace(tzinfo=local_tz).astimezone(UTC).isoformat()


def _agendas(tmp_path: Path, agendas: dict[str, dict] | None = None) -> Path:
    agenda_dir = tmp_path / "cofounder" / "agendas"
    agenda_dir.mkdir(parents=True, exist_ok=True)
    for day, payload in (agendas or {}).items():
        (agenda_dir / f"AGENDA-{day}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    return agenda_dir


def _item(
    *,
    n: int = 3,
    status: str = "done",
    persona: str = "seo_geo",
    reported_at: datetime | None = OVERNIGHT,
    result_summary: str = "deliverable written: GSC indexing plan",
) -> dict:
    item = {
        "n": n,
        "persona": persona,
        "repo": "YourProduct",
        "task": "Prepare a GSC/indexing submission plan",
        "priority": 1,
        "mode": "draft",
        "status": status,
        "result_summary": result_summary,
    }
    if reported_at is not None:
        item["reported_at"] = reported_at.isoformat()
    return item


def _build(
    tmp_path: Path,
    *,
    state_dir: Path | None = None,
    rows: list[dict] | None = None,
    agendas: dict[str, dict] | None = None,
    ledger_file: Path | None = None,
    agenda_dir: Path | None = None,
    now: datetime = NOW,
    settings=None,
) -> tuple[str, dict]:
    return callback_mod.build_cofounder_callback(
        state_dir=state_dir if state_dir is not None else _state_dir(tmp_path),
        delegation_ledger_file=(
            ledger_file if ledger_file is not None else _ledger(tmp_path, rows)
        ),
        agenda_dir=agenda_dir if agenda_dir is not None else _agendas(tmp_path, agendas),
        now=now,
        settings=settings,
    )


# =============================================================================
# 1 — settings resolver (Rule 1)
# =============================================================================


def test_callback_knob_defaults_on_and_resolves_at_call_time(monkeypatch):
    assert config.get_cofounder_callback_settings().enabled is True
    monkeypatch.setenv("COFOUNDER_CALLBACK_ENABLED", "false")
    assert config.get_cofounder_callback_settings().enabled is False
    monkeypatch.setenv("COFOUNDER_CALLBACK_ENABLED", "true")
    assert config.get_cofounder_callback_settings().enabled is True


def test_explicit_argument_beats_the_env(monkeypatch):
    monkeypatch.setenv("COFOUNDER_CALLBACK_ENABLED", "false")
    assert config.get_cofounder_callback_settings(enabled=True).enabled is True


# =============================================================================
# 2 — gates
# =============================================================================


def test_knob_false_is_a_structural_no_op(tmp_path, monkeypatch):
    """Disabled means NO reads at all, not a read whose output is dropped."""
    monkeypatch.setattr(
        callback_mod,
        "_read_ledger_events",
        lambda *_a, **_k: pytest.fail("ledger read while the knob is off"),
    )
    state_dir = _state_dir(tmp_path)
    line, decision = _build(
        tmp_path,
        state_dir=state_dir,
        rows=[_row()],
        settings=config.get_cofounder_callback_settings(enabled=False),
    )
    assert line == ""
    assert decision["reason"] == "disabled"
    assert decision["fired"] is False
    assert _stored_watermark(state_dir) == WATERMARK


def test_kill_switch_mutes_the_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    line, decision = _build(tmp_path, rows=[_row()])
    assert line == ""
    assert decision["reason"] == "kill_switch"


def test_cold_start_seeds_the_watermark_and_stays_silent(tmp_path):
    """No stored boundary means no way to know what was already said."""
    state_dir = _state_dir(tmp_path, watermark=None)
    line, decision = _build(tmp_path, state_dir=state_dir, rows=[_row()])
    assert line == ""
    assert decision["reason"] == "initialized"
    assert _stored_watermark(state_dir) == NOW


def test_corrupt_state_file_is_treated_as_a_cold_start(tmp_path):
    state_dir = _state_dir(tmp_path, watermark=None)
    (state_dir / callback_mod.STATE_FILE_NAME).write_text("{not json", encoding="utf-8")
    line, decision = _build(tmp_path, state_dir=state_dir, rows=[_row()])
    assert line == ""
    assert decision["reason"] == "initialized"
    assert _stored_watermark(state_dir) == NOW


# =============================================================================
# 3 — trigger set
# =============================================================================


def test_autopilot_delegation_fires_as_self_assigned_work(tmp_path):
    line, decision = _build(tmp_path, rows=[_row()])
    assert decision["fired"] is True
    assert decision["reason"] == "fired"
    assert decision["delegations"] == 1
    assert (
        "- self-assigned line 2 to marketing: Draft the lead-delivery "
        "acceptance checklist" in line
    )


def test_aware_utc_ledger_timestamps_are_normalized_to_local(tmp_path):
    """The shape ``delegate._audit`` actually writes (UTC, aware)."""
    _line, decision = _build(tmp_path, rows=[_row(aware_utc=True)])
    assert decision["fired"] is True
    assert decision["delegations"] == 1


def test_operator_approved_send_is_never_a_callback(tmp_path):
    """A line the operator ran himself was surfaced by that conversation."""
    line, decision = _build(
        tmp_path, rows=[_row(approved_by="operator-chat-confirm")]
    )
    assert line == ""
    assert decision["reason"] == "no_new_outcomes"
    assert decision["delegations"] == 0


def test_terminal_agenda_flips_fire(tmp_path):
    line, decision = _build(
        tmp_path,
        agendas={
            "2026-06-11": {
                "items": [
                    _item(),
                    _item(
                        n=4,
                        status="failed",
                        persona="marketing",
                        result_summary="archon run 88 failed",
                    ),
                ]
            }
        },
    )
    assert decision["completions"] == 2
    assert "- line 3 (seo_geo) done: deliverable written: GSC indexing plan" in line
    assert "- line 4 (marketing) failed: archon run 88 failed" in line


def test_non_terminal_agenda_status_is_not_an_outcome(tmp_path):
    line, decision = _build(
        tmp_path,
        agendas={"2026-06-11": {"items": [_item(status="in_progress")]}},
    )
    assert line == ""
    assert decision["reason"] == "no_new_outcomes"


def test_scope_denied_row_surfaces_as_a_grant_blocker(tmp_path):
    line, decision = _build(
        tmp_path,
        rows=[
            _row(
                outcome="scope-denied",
                line=4,
                persona="finance",
                approved_by="cofounder-autopilot",
                detail="Persona `finance` has no `delegation:` grant",
            )
        ],
    )
    assert decision["blockers"] == 1
    assert "- finance needs a delegation grant to take line 4" in line


def test_command_echo_names_the_exact_typed_commands(tmp_path):
    line, _decision = _build(tmp_path, rows=[_row()])
    for command in (
        "/cofounder show <slug>",
        "/cofounder steer <slug> <text>",
        "/cofounder pause <slug>",
        "/cofounder run <n>",
    ):
        assert command in line


def test_group_caps_report_their_overflow(tmp_path):
    rows = [
        _row(line=n, at=datetime(2026, 6, 12, 3, n), detail=f"task {n}")
        for n in range(1, 8)
    ]
    line, decision = _build(tmp_path, rows=rows)
    assert decision["delegations"] == 7
    rendered = [item for item in line.splitlines() if item.startswith("- self-assigned")]
    assert len(rendered) == callback_mod.MAX_PER_GROUP
    assert "- (+2 more outcome(s) not listed)" in line


# =============================================================================
# 4 — watermark
# =============================================================================


def test_fires_once_per_outcome_set_and_never_repeats(tmp_path):
    state_dir = _state_dir(tmp_path)
    ledger = _ledger(tmp_path, [_row()])
    agenda_dir = _agendas(tmp_path, {"2026-06-11": {"items": [_item()]}})

    first, first_decision = callback_mod.build_cofounder_callback(
        state_dir=state_dir,
        delegation_ledger_file=ledger,
        agenda_dir=agenda_dir,
        now=NOW,
    )
    assert first_decision["fired"] is True
    assert first != ""
    assert _stored_watermark(state_dir) == OVERNIGHT

    second, second_decision = callback_mod.build_cofounder_callback(
        state_dir=state_dir,
        delegation_ledger_file=ledger,
        agenda_dir=agenda_dir,
        now=NOW,
    )
    assert second == ""
    assert second_decision["fired"] is False
    assert second_decision["reason"] == "no_new_outcomes"


def test_the_next_outcome_after_a_fire_still_gets_through(tmp_path):
    state_dir = _state_dir(tmp_path)
    agenda_dir = _agendas(tmp_path)
    first, _ = callback_mod.build_cofounder_callback(
        state_dir=state_dir,
        delegation_ledger_file=_ledger(tmp_path, [_row()]),
        agenda_dir=agenda_dir,
        now=NOW,
    )
    assert first != ""
    later = _ledger(
        tmp_path,
        [_row(), _row(line=5, persona="sales", at=datetime(2026, 6, 12, 5, 0))],
    )
    second, decision = callback_mod.build_cofounder_callback(
        state_dir=state_dir,
        delegation_ledger_file=later,
        agenda_dir=agenda_dir,
        now=NOW,
    )
    assert decision["delegations"] == 1
    assert "- self-assigned line 5 to sales" in second
    assert "line 2" not in second


def test_nothing_new_leaves_the_stored_boundary_untouched(tmp_path):
    state_dir = _state_dir(tmp_path)
    line, decision = _build(
        tmp_path,
        state_dir=state_dir,
        rows=[_row(at=datetime(2026, 6, 11, 12, 0))],
        agendas={
            "2026-06-10": {"items": [_item(reported_at=datetime(2026, 6, 10, 9, 0))]}
        },
    )
    assert line == ""
    assert decision["reason"] == "no_new_outcomes"
    assert _stored_watermark(state_dir) == WATERMARK


def test_capped_overflow_still_advances_past_every_event(tmp_path):
    """A watermark that lagged the render caps would replay the overflow."""
    state_dir = _state_dir(tmp_path)
    rows = [
        _row(line=n, at=datetime(2026, 6, 12, 3, n), detail=f"task {n}")
        for n in range(1, 8)
    ]
    _line, decision = _build(tmp_path, state_dir=state_dir, rows=rows)
    assert decision["fired"] is True
    assert _stored_watermark(state_dir) == datetime(2026, 6, 12, 3, 7)


# =============================================================================
# 5 — partial failure
# =============================================================================


def test_unreadable_ledger_degrades_to_the_agenda_source(tmp_path):
    unreadable = tmp_path / "ledger-is-a-directory"
    unreadable.mkdir()
    line, decision = _build(
        tmp_path,
        ledger_file=unreadable,
        agendas={"2026-06-11": {"items": [_item()]}},
    )
    assert decision["fired"] is True
    assert decision["delegations"] == 0
    assert "- line 3 (seo_geo) done" in line


def test_missing_ledger_and_agenda_dir_stay_silent(tmp_path):
    line, decision = _build(
        tmp_path,
        ledger_file=tmp_path / "nope.jsonl",
        agenda_dir=tmp_path / "no-agendas",
    )
    assert line == ""
    assert decision["reason"] == "no_new_outcomes"


def test_malformed_ledger_lines_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "cofounder_delegation.jsonl"
    path.write_text(
        "{not json\n\n" + json.dumps(_row()) + "\n[]\n",
        encoding="utf-8",
    )
    line, decision = _build(tmp_path, ledger_file=path)
    assert decision["delegations"] == 1
    assert "- self-assigned line 2 to marketing" in line


def test_watermark_read_failure_fails_open(tmp_path, monkeypatch, capsys):
    def _explode(**_kwargs):
        raise OSError("state dir gone")

    monkeypatch.setattr(callback_mod, "read_callback_watermark", _explode)
    line, decision = _build(tmp_path, rows=[_row()])
    assert line == ""
    assert decision["reason"] == "error"
    assert "cofounder_callback" in capsys.readouterr().out


# =============================================================================
# 6 — engine seam
# =============================================================================


def _make_engine(tmp_path: Path):
    from engine import ConversationEngine
    from session import SQLiteSessionStore

    project_root = tmp_path / "project"
    (project_root / "TheHomie" / "Memory" / "daily").mkdir(parents=True)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    return ConversationEngine(store, project_root), store


def _message(text: str = "morning, where are we?", *, source: str = "interactive"):
    from models import Channel, IncomingMessage, Platform, Thread, User

    return IncomingMessage(
        text=text,
        user=User(
            platform=Platform.TELEGRAM,
            platform_id="1111111111",
            display_name="Smoke",
        ),
        channel=Channel(
            platform=Platform.TELEGRAM,
            platform_id="2222222222",
            is_dm=True,
        ),
        platform=Platform.TELEGRAM,
        thread=Thread(thread_id="thread-1"),
        source=source,
    )


@pytest.mark.asyncio
async def test_engine_gate_skips_piv_and_non_interactive(tmp_path, monkeypatch):
    engine, _store = _make_engine(tmp_path)
    monkeypatch.setattr(
        callback_mod,
        "build_cofounder_callback",
        lambda *_a, **_k: pytest.fail("builder touched behind a closed gate"),
    )
    piv = _message()
    piv.is_piv = True
    trace: dict = {}
    assert await engine._maybe_cofounder_callback(piv, trace_decisions=trace) == ""
    assert trace["cofounder_callback"]["reason"] == "is_piv"

    trace = {}
    assert (
        await engine._maybe_cofounder_callback(
            _message(source="cron"), trace_decisions=trace
        )
        == ""
    )
    assert trace["cofounder_callback"]["reason"] == "non_interactive"


@pytest.mark.asyncio
async def test_engine_runs_the_builder_off_the_event_loop(tmp_path, monkeypatch):
    engine, _store = _make_engine(tmp_path)
    loop_thread = threading.get_ident()
    observed: list[int] = []

    def _build_stub(**_kwargs):
        observed.append(threading.get_ident())
        return "", {"fired": False, "reason": "no_new_outcomes"}

    monkeypatch.setattr(callback_mod, "build_cofounder_callback", _build_stub)
    trace: dict = {}
    assert await engine._maybe_cofounder_callback(_message(), trace_decisions=trace) == ""
    assert observed and all(thread_id != loop_thread for thread_id in observed)
    assert trace["cofounder_callback"]["reason"] == "no_new_outcomes"


@pytest.mark.asyncio
async def test_engine_fails_open_when_the_builder_raises(tmp_path, monkeypatch, capsys):
    engine, _store = _make_engine(tmp_path)

    def _explode(**_kwargs):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(callback_mod, "build_cofounder_callback", _explode)
    trace: dict = {}
    assert await engine._maybe_cofounder_callback(_message(), trace_decisions=trace) == ""
    assert trace["cofounder_callback"]["reason"] == "error"
    assert "[CoFounder] callback non-blocking failure" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_full_turn_orders_callback_after_crypto_before_the_brief(
    tmp_path, monkeypatch
):
    import engine as engine_module

    from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeResult

    engine, store = _make_engine(tmp_path)
    prompts: list[str] = []

    async def _run(request):
        prompts.append(request.prompt)
        return RuntimeResult(
            text="got it",
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="test",
            model="model",
            profile_key="profile",
            session_id="session",
            cost_usd=0.0,
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", _run)

    async def _crypto(_message, **_kwargs):
        return "CRYPTO_MARKER"

    monkeypatch.setattr(engine, "_maybe_crypto_plays_callback", _crypto)
    monkeypatch.setattr(
        callback_mod,
        "build_cofounder_callback",
        lambda **_kwargs: (
            "COFOUNDER_MARKER",
            {"fired": True, "reason": "fired"},
        ),
    )
    monkeypatch.setattr(
        engine,
        "_maybe_session_brief",
        lambda _message, trace_decisions=None: ("BRIEF_MARKER", None),
    )

    bare = "morning, where are we?"
    outputs = [output async for output in engine.handle_message(_message(bare))]
    assert outputs[-1].text == "got it"
    prompt = next(p for p in prompts if "COFOUNDER_MARKER" in p)
    assert prompt.index("CRYPTO_MARKER") < prompt.index("COFOUNDER_MARKER")
    assert prompt.index("COFOUNDER_MARKER") < prompt.index("BRIEF_MARKER")

    history = store.list_messages("telegram:2222222222:thread-1")
    assert history[0].content == bare
    assert all("COFOUNDER_MARKER" not in item.content for item in history)


@pytest.mark.asyncio
async def test_firing_never_touches_session_brief_state(tmp_path, monkeypatch):
    """The callback owns its own watermark — #138 token machinery stays clear."""
    from cognition import proactive_brief

    engine, _store = _make_engine(tmp_path)
    monkeypatch.setattr(
        proactive_brief,
        "clear_brief_owed",
        lambda **_kwargs: pytest.fail("callback consumed the brief marker"),
    )
    monkeypatch.setattr(
        callback_mod,
        "build_cofounder_callback",
        lambda **_kwargs: ("COFOUNDER_MARKER", {"fired": True, "reason": "fired"}),
    )
    assert await engine._maybe_cofounder_callback(_message()) == "COFOUNDER_MARKER"
    assert engine._session_brief_pending is None
    assert engine._session_brief_fired_at is None
