"""T3 #189 — /shots command family, stale sweep, track-record callback.

One test per distinct code path (house testing principle), plus the R1-gate
integration proofs: registry-dispatch reachability, engine-turn seam, and the
reflection post-step wire. Exercises the REAL handler/sweep/callback against
the REAL T1 service on an isolated tmp ledger — service calls are only
monkeypatched where a failure state is physically unstageable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling test helpers

import core_handlers  # type: ignore[import-not-found]  # noqa: E402
from cognition import called_shots as cs  # noqa: E402
from cognition import shots_callback  # noqa: E402

import called_shots_sweep  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger_env(tmp_path, monkeypatch):
    """Isolated ledger + mirror; switch ON; audit seam stubbed."""
    db = tmp_path / "called_shots.db"
    mirror = tmp_path / "called-shots"
    monkeypatch.setenv("CALLED_SHOTS_DB_PATH", str(db))
    monkeypatch.setenv("CALLED_SHOTS_MIRROR_DIR", str(mirror))
    monkeypatch.delenv("HOMIE_KILLSWITCH_CALLED_SHOTS", raising=False)
    monkeypatch.delenv("CALLED_SHOTS_ENABLED", raising=False)
    monkeypatch.delenv("CALLED_SHOTS_STALE_AGE_DAYS", raising=False)
    monkeypatch.setitem(
        sys.modules, "dashboard_api", SimpleNamespace(_audit_write=lambda **k: None)
    )
    return SimpleNamespace(db=db, mirror=mirror)


@pytest.fixture
def as_sales(monkeypatch):
    """Pin the active-persona resolver to 'sales' (module-attr, Rule 3)."""
    monkeypatch.setattr(shots_callback, "resolve_active_persona", lambda: "sales")


def _incoming() -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        channel=SimpleNamespace(platform_id="chan1"),
        thread=None,
        user_role="admin",
        chat_id="chan1",
        text="",
    )


async def _shots(args: str) -> str:
    return await core_handlers.handle_shots(None, _incoming(), args)


# ===========================================================================
# 1. /shots command family
# ===========================================================================


@pytest.mark.asyncio
async def test_list_empty(ledger_env, as_sales):
    out = await _shots("list")
    assert "No open bets" in out


@pytest.mark.asyncio
async def test_list_scoped_to_active_persona(ledger_env, as_sales):
    cs.record_shot("sales", "pricing", "op pos", "homie pos")
    cs.record_shot("default", "hiring", "op pos", "homie pos")
    out = await _shots("")
    assert "pricing" in out and "hiring" not in out


@pytest.mark.asyncio
async def test_list_all_crosses_personas(ledger_env, as_sales):
    cs.record_shot("sales", "pricing", "a", "b")
    cs.record_shot("default", "hiring", "a", "b")
    out = await _shots("list all")
    assert "pricing" in out and "hiring" in out


@pytest.mark.asyncio
async def test_list_unreadable_never_renders_no_open_bets(
    ledger_env, as_sales, monkeypatch
):
    """R1: a broken ledger renders 'unreadable', NEVER an empty healthy list."""
    monkeypatch.setattr(cs, "list_open_checked", lambda *a, **k: ([], False))
    out = await _shots("list")
    assert "unreadable" in out and "No open bets" not in out


@pytest.mark.asyncio
async def test_resolve_operator_right_voice(ledger_env, as_sales):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    out = await _shots(f"resolve {shot.id} operator_right")
    assert "You called it" in out and f"#{shot.id}" in out


@pytest.mark.asyncio
async def test_resolve_homie_right_voice(ledger_env, as_sales):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    out = await _shots(f"resolve {shot.id} homie_right")
    assert "Told you so" in out


@pytest.mark.asyncio
async def test_resolve_void_strikes(ledger_env, as_sales):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    out = await _shots(f"resolve {shot.id} void")
    assert "Struck" in out
    record = cs.track_record("sales", "pricing")
    assert record.void == 1 and record.resolved == 0  # struck, not counted


@pytest.mark.asyncio
async def test_resolve_unknown_id_distinct(ledger_env, as_sales):
    out = await _shots("resolve 999 push")
    assert "no such shot" in out


@pytest.mark.asyncio
async def test_resolve_already_settled_distinct(ledger_env, as_sales):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    cs.reconcile(shot.id, "push", persona_id="sales")
    out = await _shots(f"resolve {shot.id} operator_right")
    assert "already settled" in out and "push" in out


@pytest.mark.asyncio
async def test_resolve_cross_persona_distinct_and_row_untouched(ledger_env, as_sales):
    shot = cs.record_shot("default", "hiring", "a", "b")
    out = await _shots(f"resolve {shot.id} push")
    assert "belongs to persona 'default'" in out
    # Rule 4: the ROW is untouched, not just the message (DB-level assert).
    still_open = [s.id for s in cs.list_open("default")]
    assert shot.id in still_open


@pytest.mark.asyncio
async def test_resolve_classification_unreadable_distinct(
    ledger_env, as_sales, monkeypatch
):
    """4th distinct outcome: probe failure renders unreadable, not unknown-id."""
    monkeypatch.setattr(cs, "get_shot_checked", lambda *a, **k: (None, False))
    out = await _shots("resolve 999 push")
    assert "unreadable" in out and "no such shot" not in out


@pytest.mark.asyncio
async def test_resolve_bad_outcome_is_bad_input(ledger_env, as_sales):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    out = await _shots(f"resolve {shot.id} everyone_wins")
    assert out.startswith("Bad input")


@pytest.mark.asyncio
async def test_resolve_non_numeric_id(ledger_env, as_sales):
    out = await _shots("resolve seven push")
    assert "must be a number" in out


@pytest.mark.asyncio
async def test_kill_switch_renders_clean_refusal(ledger_env, as_sales, monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_CALLED_SHOTS", "disabled")
    out = await _shots("list")
    assert "disabled by operator" in out  # rendered, never raised


@pytest.mark.asyncio
async def test_soft_off_leaves_commands_alive(ledger_env, as_sales, monkeypatch):
    """Kimi L1: soft-OFF must not strand open shots — settle paths stay up."""
    shot = cs.record_shot("sales", "pricing", "a", "b")
    monkeypatch.setenv("CALLED_SHOTS_ENABLED", "false")
    assert "pricing" in await _shots("list")
    assert "You called it" in await _shots(f"resolve {shot.id} operator_right")


@pytest.mark.asyncio
async def test_unresolvable_persona_refuses_without_ledger_op(ledger_env, monkeypatch):
    """R1 BLOCKER: resolver ERROR fails CLOSED — refusal, zero ledger ops."""
    shot = cs.record_shot("default", "hiring", "a", "b")
    monkeypatch.setattr(shots_callback, "resolve_active_persona", lambda: None)
    out = await _shots(f"resolve {shot.id} push")
    assert "Can't resolve the active persona" in out
    # No reconcile ran on a guessed persona — row physically untouched.
    still_open = [s.id for s in cs.list_open("default")]
    assert shot.id in still_open
    # Scoped list also refuses; the explicit all-view still works.
    assert "Can't resolve the active persona" in await _shots("list")
    assert "hiring" in await _shots("list all")


@pytest.mark.asyncio
async def test_list_flattens_newline_positions(ledger_env, as_sales):
    """LOW-5: newline-embedded positions must not break the numbered list."""
    shot = cs.record_shot("sales", "pricing", "line one\nline two", "mine\r\nalso")
    out = await _shots("list")
    shot_lines = [ln for ln in out.split("\n") if f"#{shot.id}" in ln]
    assert len(shot_lines) == 1  # ONE list line — positions folded into it
    assert "line one line two" in shot_lines[0]
    assert "mine also" in shot_lines[0]


@pytest.mark.asyncio
async def test_usage_on_garbage(ledger_env, as_sales):
    out = await _shots("frobnicate")
    assert "receipts ledger" in out


# ===========================================================================
# 2. Stale-shot sweep (reflection post-step)
# ===========================================================================


def _backdate(db: Path, shot_id: int, days: int) -> None:
    import sqlite3
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE called_shots SET created_at = ? WHERE id = ?", (old, shot_id)
        )
        conn.commit()
    finally:
        conn.close()


def test_sweep_flags_stale_writes_receipt(ledger_env, monkeypatch):
    stale = cs.record_shot("sales", "pricing", "a", "b")
    cs.record_shot("sales", "fresh-topic", "a", "b")  # stays fresh
    _backdate(ledger_env.db, stale.id, days=30)

    received: list[tuple[str, str]] = []
    import shared

    monkeypatch.setattr(
        shared,
        "append_to_daily_log",
        lambda content, section="Entry": received.append((content, section)),
    )
    result = called_shots_sweep.run_called_shots_sweep(test_mode=False)
    assert result == "SHOTS_SWEEP: 1 stale open shot(s)"
    assert len(received) == 1
    content, section = received[0]
    assert section == "Called Shots"
    assert f"#{stale.id}" in content and "pricing" in content
    assert "fresh-topic" not in content


def test_sweep_silent_when_all_fresh(ledger_env):
    cs.record_shot("sales", "pricing", "a", "b")
    assert called_shots_sweep.run_called_shots_sweep() == "SHOTS_SWEEP_SILENT"


def test_sweep_dark_under_soft_off(ledger_env, monkeypatch):
    stale = cs.record_shot("sales", "pricing", "a", "b")
    _backdate(ledger_env.db, stale.id, days=30)
    monkeypatch.setenv("CALLED_SHOTS_ENABLED", "false")
    assert called_shots_sweep.run_called_shots_sweep() == "SHOTS_SWEEP_SILENT"


def test_sweep_silent_under_kill_switch_no_raise(ledger_env, monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_CALLED_SHOTS", "disabled")
    assert called_shots_sweep.run_called_shots_sweep() == "SHOTS_SWEEP_SILENT"


def test_sweep_unused_install_creates_no_db(ledger_env):
    """MINOR-2a: an enabled-but-unused install must NOT manufacture a ledger
    on the daily reflection cadence (the ro probe can never create the DB)."""
    assert not ledger_env.db.exists()
    assert called_shots_sweep.run_called_shots_sweep() == "SHOTS_SWEEP_SILENT"
    assert not ledger_env.db.exists()  # no side-effect DB materialized


def test_sweep_unreadable_ledger_honest_receipt(ledger_env, monkeypatch):
    """MINOR-2b: unreadable ledger → UNREADABLE receipt, never 'nothing stale'."""
    ledger_env.db.mkdir()  # a directory at the db path = unreadable ledger
    result = called_shots_sweep.run_called_shots_sweep()
    assert result == "ledger UNREADABLE (see bot log)"
    assert result != "SHOTS_SWEEP_SILENT"


def test_sweep_skips_garbage_timestamp_row(ledger_env):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    import sqlite3

    conn = sqlite3.connect(str(ledger_env.db))
    try:
        conn.execute(
            "UPDATE called_shots SET created_at = 'not-a-date' WHERE id = ?",
            (shot.id,),
        )
        conn.commit()
    finally:
        conn.close()
    # Garbage row is skipped (fail-open), not crashed on and not flagged.
    assert called_shots_sweep.run_called_shots_sweep() == "SHOTS_SWEEP_SILENT"


def test_age_parser_hoisted_and_shared_by_both_surfaces(ledger_env, monkeypatch):
    """LOW-4 (#193): ONE age parser — ``cognition.called_shots.shot_age_days`` —
    backs BOTH the T3 stale sweep and the ``/shots`` list renderer; the divergent
    private copies (``called_shots_sweep._age_days`` /
    ``core_handlers._shots_age_days``'s inline parse) are gone.

    Proven three ways: (1) a naive and a tz-aware stamp of the SAME instant
    parse to the identical age (the UTC-coercion is shared, not re-implemented);
    (2) the SAME garbage stamp fails open on BOTH surfaces (None / '?'); (3) a
    spy on the cognition parser sees BOTH the renderer and the sweep route
    through it (module-attribute lookup — Rule 3 — so the monkeypatch reaches
    both slices).
    """
    import sqlite3
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    # 3d6h ago: a half-day buffer so microsecond clock drift between the
    # explicit-``now`` parse and the renderer's internal clock can never cross
    # the whole-day boundary this test asserts on.
    naive = (now - timedelta(days=3, hours=6)).replace(tzinfo=None).isoformat()
    aware = (now - timedelta(days=3, hours=6)).isoformat()

    # (1) naive == aware for the same instant, and it is 3 whole days old.
    assert cs.shot_age_days(naive, now) == cs.shot_age_days(aware, now)
    assert int(cs.shot_age_days(naive, now)) == 3

    # (2) same garbage → fail-open on both surfaces, via the one parser.
    assert cs.shot_age_days("not-a-date", now) is None
    assert core_handlers._shots_age_days("not-a-date") == "?"

    # (3) spy proves both call sites resolve the ONE cognition parser.
    seen: list[str] = []
    real = cs.shot_age_days

    def _spy(created_at, when=None):
        seen.append(str(created_at))
        return real(created_at, when)

    monkeypatch.setattr(cs, "shot_age_days", _spy)

    # renderer path (chat slice): 3d6h old → "3d"
    assert core_handlers._shots_age_days(naive) == "3d"

    # sweep path (scripts slice): stage ONE stale open shot, run the sweep.
    monkeypatch.setenv("CALLED_SHOTS_STALE_AGE_DAYS", "1")
    shot = cs.record_shot("sales", "pricing", "a", "b")
    conn = sqlite3.connect(str(ledger_env.db))
    try:
        conn.execute(
            "UPDATE called_shots SET created_at = ? WHERE id = ?",
            (naive, shot.id),
        )
        conn.commit()
    finally:
        conn.close()
    called_shots_sweep.run_called_shots_sweep(test_mode=True)

    # Both surfaces (renderer + the one open row in the sweep) hit the spy.
    assert seen.count(naive) >= 2


# ===========================================================================
# 3. Track-record callback (builder unit — caller owns dedup marking)
# ===========================================================================


def _settle(persona: str, domain: str, outcomes: list[str]) -> None:
    for outcome in outcomes:
        shot = cs.record_shot(persona, domain, "a", "b")
        cs.reconcile(shot.id, outcome, persona_id=persona)


def _cb(text: str, persona="sales", fired=None, key="c1"):
    return shots_callback.build_shots_callback(
        text,
        persona,
        fired_keys=fired if fired is not None else set(),
        conversation_key=key,
    )


def test_callback_fires_with_real_counts_stats_first(ledger_env):
    _settle("sales", "pricing", ["operator_right", "operator_right", "homie_right"])
    fired: set = set()
    line, decision = _cb("I think our pricing is too low again", fired=fired)
    assert decision["fired"] is True and decision["domain"] == "pricing"
    assert "Settled 3 bet(s)" in line
    assert "operator right 2" in line and "me right 1" in line
    # Stats come BEFORE the domain (survive any tail truncation).
    assert line.index("Settled 3") < line.index("'pricing'")
    # Builder does NOT mark — the caller owns it via dedup_key.
    assert fired == set() and decision["dedup_key"] == ("c1", "pricing")


def test_callback_dedups_when_caller_marked(ledger_env):
    _settle("sales", "pricing", ["push"])
    fired: set = set()
    line1, d1 = _cb("pricing question here", fired=fired)
    assert d1["fired"] is True and line1
    fired.add(d1["dedup_key"])  # the engine's marking step
    line2, d2 = _cb("another pricing question", fired=fired)
    assert d2["fired"] is False and d2["reason"] == "deduped" and line2 == ""


def test_callback_never_claims_off_unreadable_track_record(ledger_env, monkeypatch):
    """Kimi m1: ok=False renders NOTHING — no affirmative 'no history' claim."""
    _settle("sales", "pricing", ["push"])
    monkeypatch.setattr(
        cs,
        "track_record",
        lambda *a, **k: cs.TrackRecord(persona_id="sales", domain="pricing", ok=False),
    )
    line, decision = _cb("pricing question")
    assert line == "" and decision["reason"] == "ledger_unreadable"


def test_callback_skips_on_unreadable_domain_listing(ledger_env, monkeypatch):
    """R1: list_resolved_domains None (unreadable) → silent skip, no claim."""
    _settle("sales", "pricing", ["push"])
    monkeypatch.setattr(cs, "list_resolved_domains", lambda *a, **k: None)
    line, decision = _cb("pricing question")
    assert line == "" and decision["reason"] == "ledger_unreadable"


def test_callback_void_only_domain_stays_silent(ledger_env):
    """A domain whose only 'resolved' rows are VOID has no record to cite."""
    _settle("sales", "pricing", ["void"])
    line, decision = _cb("pricing question")
    assert line == "" and decision["reason"] == "no_resolved_rows"


def test_callback_dark_under_soft_off(ledger_env, monkeypatch):
    _settle("sales", "pricing", ["push"])
    monkeypatch.setenv("CALLED_SHOTS_ENABLED", "false")
    line, decision = _cb("pricing question")
    assert line == "" and decision["reason"] == "soft_off"


def test_callback_dark_under_kill_switch(ledger_env, monkeypatch):
    _settle("sales", "pricing", ["push"])
    monkeypatch.setenv("HOMIE_KILLSWITCH_CALLED_SHOTS", "disabled")
    line, decision = _cb("pricing question")
    assert line == "" and decision["reason"] == "kill_switch"


def test_callback_no_domain_match(ledger_env):
    _settle("sales", "pricing", ["push"])
    line, decision = _cb("what's for lunch today homie")
    assert line == "" and decision["reason"] == "no_domain_match"


def test_callback_short_text_gate(ledger_env):
    _settle("sales", "pricing", ["push"])
    line, decision = _cb("pricing")
    assert line == "" and decision["reason"] == "too_short"


def test_callback_persona_unresolvable_skips(ledger_env):
    """R1 BLOCKER: resolver ERROR (None) → no ledger read, distinct reason."""
    _settle("sales", "pricing", ["push"])
    line, decision = _cb("pricing question", persona=None)
    assert line == "" and decision["reason"] == "persona_unresolvable"


def test_callback_sanitizes_hostile_domain_stats_survive(ledger_env):
    """Markdown/newline-hostile domain is folded; counts render regardless."""
    evil = "pricing\n# fake header [link](x)"
    _settle("sales", evil, ["push"])
    stored = cs.list_resolved_domains("sales")[0]
    line, decision = _cb(f"about {stored} again", fired=set())
    assert decision["fired"] is True
    assert "Settled 1 bet(s)" in line
    body = line.split("\n", 1)[1]  # rendered block below the header line
    assert "\n" not in body and "# fake" not in body  # folded, not injectable


# ===========================================================================
# 3b. set_decided_by — the one-way override ratchet (Kimi adjudication)
# ===========================================================================


def test_decided_by_ratchet_happy_operator(ledger_env):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    updated = cs.set_decided_by(shot.id, "operator", persona_id="sales")
    assert updated is not None and updated.decided_by == "operator"
    assert updated.status == "open"  # the bet STAYS open to settle later


def test_decided_by_ratchet_happy_homie(ledger_env):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    updated = cs.set_decided_by(shot.id, "homie", persona_id="sales")
    assert updated is not None and updated.decided_by == "homie"


def test_decided_by_second_call_skips_already_decided(ledger_env, capsys):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    assert cs.set_decided_by(shot.id, "operator", persona_id="sales") is not None
    assert cs.set_decided_by(shot.id, "homie", persona_id="sales") is None
    assert "already decided" in capsys.readouterr().out
    # Ratchet held: the first write survives.
    row, ok = cs.get_shot_checked(shot.id)
    assert ok and row.decided_by == "operator"


def test_decided_by_resolved_shot_skips_not_open(ledger_env, capsys):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    cs.reconcile(shot.id, "push", persona_id="sales")
    assert cs.set_decided_by(shot.id, "operator", persona_id="sales") is None
    assert "not open" in capsys.readouterr().out


def test_decided_by_invalid_targets_raise(ledger_env):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    with pytest.raises(ValueError, match="one-way ratchet"):
        cs.set_decided_by(shot.id, "open")  # ratchet never re-opens
    with pytest.raises(ValueError, match="one-way ratchet"):
        cs.set_decided_by(shot.id, "jury")


def test_decided_by_cross_persona_row_untouched(ledger_env, capsys):
    shot = cs.record_shot("default", "hiring", "a", "b")
    assert cs.set_decided_by(shot.id, "operator", persona_id="sales") is None
    assert "persona mismatch" in capsys.readouterr().out
    row, ok = cs.get_shot_checked(shot.id)
    assert ok and row.decided_by == "open"  # DB-level: untouched (Rule 4)


def test_decided_by_kill_switch_raises(ledger_env, monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_CALLED_SHOTS", "disabled")
    from security import kill_switches

    with pytest.raises(kill_switches.KillSwitchDisabled):
        cs.set_decided_by(1, "operator")


def test_decided_by_runtime_failure_fails_open(ledger_env):
    assert cs.set_decided_by(1, "operator", db_path=123) is None  # no raise


def test_decided_by_refreshes_mirror(ledger_env):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    cs.set_decided_by(shot.id, "operator", persona_id="sales")
    mirror_files = list(ledger_env.mirror.glob("*.md"))
    assert len(mirror_files) == 1
    assert "**Decided by:** operator" in mirror_files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_shots_decided_command_happy_and_skips(ledger_env, as_sales):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    out = await _shots(f"decided {shot.id} operator")
    assert "decided by you" in out and "Still open" in out
    # Ratchet skip through the command surface — distinct message.
    out2 = await _shots(f"decided {shot.id} homie")
    assert "one-way ratchet" in out2
    # Unknown / settled / mismatch / bad-target paths.
    assert "no such shot" in await _shots("decided 999 operator")
    cs.reconcile(shot.id, "operator_right", persona_id="sales")
    assert "already settled" in await _shots(f"decided {shot.id} operator")
    other = cs.record_shot("default", "hiring", "a", "b")
    assert "belongs to persona 'default'" in await _shots(f"decided {other.id} homie")
    bad = cs.record_shot("sales", "ads", "a", "b")
    assert (await _shots(f"decided {bad.id} jury")).startswith("Bad input")
    assert "must be a number" in await _shots("decided seven operator")
    assert "Usage:" in await _shots("decided 5")


# ===========================================================================
# 4. Persona resolver (fail-closed on ERROR, semantic mapping preserved)
# ===========================================================================


def test_resolver_error_fails_closed_to_none(monkeypatch):
    import personas.activity as activity

    monkeypatch.setattr(
        activity,
        "get_active_profile_name",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert shots_callback.resolve_active_persona() is None


def test_resolver_semantic_mapping_unchanged(monkeypatch):
    import personas.activity as activity

    monkeypatch.setattr(activity, "get_active_profile_name", lambda: "custom")
    assert shots_callback.resolve_active_persona() == "default"
    monkeypatch.setattr(activity, "get_active_profile_name", lambda: "closer")
    assert shots_callback.resolve_active_persona() == "closer"
    monkeypatch.setattr(activity, "get_active_profile_name", lambda: "")
    assert shots_callback.resolve_active_persona() == "default"


# ===========================================================================
# 5. Engine seam — gate, canonical dedup, cap, /clear reset, full turn
# ===========================================================================


def _make_engine(tmp_path):
    from engine import ConversationEngine
    from session import SQLiteSessionStore

    project_root = tmp_path / "project"
    (project_root / "TheHomie" / "Memory" / "daily").mkdir(parents=True)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    return ConversationEngine(store, project_root), store


def _engine_message(text: str, thread: str = "t1", source: str = "interactive"):
    from models import Channel, IncomingMessage, Platform, Thread, User

    return IncomingMessage(
        text=text,
        user=User(platform=Platform.TELEGRAM, platform_id="u1", display_name="Smoke"),
        channel=Channel(platform=Platform.TELEGRAM, platform_id="chan-1", is_dm=True),
        platform=Platform.TELEGRAM,
        thread=Thread(thread_id=thread),
        source=source,
    )


@pytest.mark.asyncio
async def test_engine_gate_skips_piv_and_cron_without_ledger_read(
    ledger_env, as_sales, tmp_path, monkeypatch
):
    convo, _ = _make_engine(tmp_path)
    _settle("sales", "pricing", ["push"])

    def _boom(*a, **k):  # any ledger read on a gated turn = test failure
        raise AssertionError("ledger read on a gated turn")

    monkeypatch.setattr(cs, "list_resolved_domains", _boom)

    piv = _engine_message("pricing question here")
    piv.is_piv = True
    trace: dict = {}
    assert await convo._maybe_called_shots_callback(piv, trace_decisions=trace) == ""
    assert trace["called_shots_callback"]["reason"] == "is_piv"

    cron = _engine_message("pricing question here", source="cron")
    trace = {}
    assert await convo._maybe_called_shots_callback(cron, trace_decisions=trace) == ""
    assert trace["called_shots_callback"]["reason"] == "non_interactive"
    assert len(convo._shots_callback_fired) == 0  # no dedup consumed


@pytest.mark.asyncio
async def test_engine_dedup_keys_by_thread_not_channel(ledger_env, as_sales, tmp_path):
    """Two threads on ONE channel must not suppress each other (R1)."""
    convo, _ = _make_engine(tmp_path)
    _settle("sales", "pricing", ["push"])
    t1 = {}
    line1 = await convo._maybe_called_shots_callback(
        _engine_message("pricing question here", thread="t1"), trace_decisions=t1
    )
    t2 = {}
    line2 = await convo._maybe_called_shots_callback(
        _engine_message("pricing question here", thread="t2"), trace_decisions=t2
    )
    assert line1 and line2  # both fired — canonical key includes the thread
    assert t1["called_shots_callback"]["fired"] and t2["called_shots_callback"]["fired"]
    # Same thread again → deduped.
    t3 = {}
    line3 = await convo._maybe_called_shots_callback(
        _engine_message("pricing again ok", thread="t1"), trace_decisions=t3
    )
    assert line3 == "" and t3["called_shots_callback"]["reason"] == "deduped"


@pytest.mark.asyncio
async def test_engine_dedup_cap_evicts_oldest(ledger_env, as_sales, tmp_path):
    convo, _ = _make_engine(tmp_path)
    convo._SHOTS_CALLBACK_FIRED_CAP = 2  # instance shadow of the class attr
    for domain in ("alpha", "beta", "gamma"):
        _settle("sales", domain, ["push"])
        await convo._maybe_called_shots_callback(
            _engine_message(f"question about {domain} today", thread="t1")
        )
    assert len(convo._shots_callback_fired) == 2
    remaining_domains = {k[1] for k in convo._shots_callback_fired}
    assert remaining_domains == {"beta", "gamma"}  # alpha (oldest) evicted


@pytest.mark.asyncio
async def test_engine_clear_resets_session_dedup(ledger_env, as_sales, tmp_path):
    convo, _ = _make_engine(tmp_path)
    _settle("sales", "pricing", ["push"])
    msg = _engine_message("pricing question here", thread="t1")
    assert await convo._maybe_called_shots_callback(msg) != ""
    # /clear seam: reset THIS session's entries → the domain may fire again.
    convo.reset_shots_callback_for_session("telegram", "chan-1", "t1")
    assert (
        await convo._maybe_called_shots_callback(
            _engine_message("pricing once more", thread="t1")
        )
        != ""
    )


@pytest.mark.asyncio
async def test_engine_full_turn_injects_block_history_stays_bare(
    ledger_env, as_sales, tmp_path, monkeypatch
):
    """Integration (R1): a REAL interactive turn drives the callback — the
    block rides the RuntimeRequest prompt; persisted history shows the bare
    operator text only."""
    import engine as engine_module
    from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeResult

    convo, store = _make_engine(tmp_path)
    _settle("sales", "pricing", ["operator_right", "homie_right"])

    captured: dict = {}

    async def fake_run(request):
        captured["prompt"] = request.prompt
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="claude",
            model="m",
            profile_key="p",
            session_id="s",
            cost_usd=0.0,
            tool_calls=[],
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)
    bare_text = "I think our pricing is way off"
    outputs = [
        out
        async for out in convo.handle_message(_engine_message(bare_text, thread="t9"))
    ]
    assert outputs[-1].text == "ok"
    assert "Called-Shots Track Record" in captured["prompt"]
    assert "operator right 1 / me right 1" in captured["prompt"]
    # History purity: the persisted operator message is the BARE text.
    messages = store.list_messages("telegram:chan-1:t9")
    assert messages[0].content == bare_text
    assert all("Called-Shots Track Record" not in m.content for m in messages)


# ===========================================================================
# 6. Integration wires — registry dispatch + reflection post-step
# ===========================================================================


@pytest.mark.asyncio
async def test_shots_reachable_via_registry_dispatch(ledger_env, as_sales):
    """R1: /shots must be reachable through the ACTUAL command registry —
    the same COMMANDS + CATEGORIES + CORE_HANDLERS wiring main.py:638 boots
    with. Removing the commands.py row OR the CORE_HANDLERS entry kills this
    test (not a direct handle_shots call)."""
    from commands import CATEGORIES, COMMANDS
    from extension_manager import ExtensionManager

    manager = ExtensionManager()
    manager.register_core_commands(COMMANDS, CATEGORIES, core_handlers.CORE_HANDLERS)
    out = await manager.dispatch(
        "shots", None, _incoming(), "list", collect_only=True
    )
    assert out is not None and "No open bets" in out
    # The decided intake rides the same registry wire (addendum).
    shot = cs.record_shot("sales", "pricing", "a", "b")
    out2 = await manager.dispatch(
        "shots", None, _incoming(), f"decided {shot.id} operator", collect_only=True
    )
    assert out2 is not None and "decided by you" in out2


def test_reflection_post_step_invokes_sweep(ledger_env, monkeypatch, tmp_path):
    """R1: the reflect post-step actually calls the sweep (wire-removal dies)."""
    import test_memory_reflect as tmr

    calls: list[bool] = []
    monkeypatch.setattr(
        called_shots_sweep,
        "run_called_shots_sweep",
        lambda test_mode=False: calls.append(True) or "SHOTS_SWEEP_SILENT",
    )
    out = tmr._drive_reflection_for_print(monkeypatch, tmp_path, apply_return=(0, 0))
    assert calls == [True]
    assert "Called-shots sweep" in out


def test_reflection_survives_sweep_raise(ledger_env, monkeypatch, tmp_path):
    """R1: a sweep crash is non-blocking — reflection completes."""
    import test_memory_reflect as tmr

    def _boom(test_mode=False):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(called_shots_sweep, "run_called_shots_sweep", _boom)
    out = tmr._drive_reflection_for_print(monkeypatch, tmp_path, apply_return=(0, 0))
    assert "Called-shots sweep failed (non-blocking)" in out
