"""Tests for the Called-Shots ledger spine (epic #186, T1 #187).

Every test is tmp_path-scoped via CALLED_SHOTS_* env (Rule 1 — the service
resolves settings at call time, so monkeypatch.setenv is live with no module
reload). NO live state (.claude/data, the vault) is ever touched.

Path map (one test per distinct path):
  1. Rule-1 resolver — defaults, env flip on next call, explicit-arg passthrough.
  2. record -> list_open -> reconcile -> track_record true counts (the 2-of-3
     acceptance), persona + domain scoping, derived-by-query proof.
  3. Kill-switch — disabled raises on EVERY entrypoint + refusal counted;
     absent env = default-ON.
  4. Contract errors — empty persona_id / bad decided_by / bad outcome raise
     ValueError; persona_id NOT NULL enforced PHYSICALLY (schema-level).
  5. Fail-open — DB runtime failure returns None/[]/zeros, never raises.
  6. Mirror — written + refreshed on reconcile; mirror failure never fails the
     ledger write; mirror off skips.
  7. Diagnostics — called_shots payload populated; kill-switch flips enabled.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402
from cognition import called_shots as cs  # noqa: E402
from security import kill_switches  # noqa: E402


@pytest.fixture
def ledger_env(tmp_path, monkeypatch):
    """Point the ledger + mirror at tmp_path; ensure the switch is ON."""
    db = tmp_path / "called_shots.db"
    mirror = tmp_path / "called-shots"
    monkeypatch.setenv("CALLED_SHOTS_DB_PATH", str(db))
    monkeypatch.setenv("CALLED_SHOTS_MIRROR_DIR", str(mirror))
    monkeypatch.delenv("HOMIE_KILLSWITCH_CALLED_SHOTS", raising=False)
    monkeypatch.delenv("CALLED_SHOTS_ENABLED", raising=False)
    monkeypatch.delenv("CALLED_SHOTS_MIRROR_ENABLED", raising=False)
    return SimpleNamespace(db=db, mirror=mirror)


def _stub_audit(monkeypatch):
    """Late-bind seam: keep kill-switch refusals from importing dashboard_api."""
    monkeypatch.setitem(
        sys.modules, "dashboard_api", SimpleNamespace(_audit_write=lambda **k: None)
    )


# ===========================================================================
# 1. Rule-1 resolver
# ===========================================================================


def test_resolver_defaults(monkeypatch):
    for var in (
        "CALLED_SHOTS_ENABLED",
        "CALLED_SHOTS_DB_PATH",
        "CALLED_SHOTS_STALE_AGE_DAYS",
        "CALLED_SHOTS_MIRROR_ENABLED",
        "CALLED_SHOTS_MIRROR_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    s = config.get_called_shots_settings()
    assert s.enabled is True
    assert s.db_path.endswith("called_shots.db")
    assert s.stale_age_days == 14
    assert s.mirror_enabled is True
    assert s.mirror_dir.endswith("called-shots")


def test_resolver_env_flips_on_next_call(monkeypatch):
    monkeypatch.setenv("CALLED_SHOTS_ENABLED", "false")
    monkeypatch.setenv("CALLED_SHOTS_STALE_AGE_DAYS", "3")
    s = config.get_called_shots_settings()
    assert s.enabled is False
    assert s.stale_age_days == 3
    monkeypatch.setenv("CALLED_SHOTS_ENABLED", "true")
    assert config.get_called_shots_settings().enabled is True  # no reload needed


def test_resolver_explicit_args_pass_through(monkeypatch):
    monkeypatch.setenv("CALLED_SHOTS_STALE_AGE_DAYS", "99")
    s = config.get_called_shots_settings(stale_age_days=5, db_path="X.db")
    assert s.stale_age_days == 5
    assert s.db_path == "X.db"


# ===========================================================================
# 2. Ledger lifecycle — the acceptance path
# ===========================================================================


def test_record_reconcile_track_record_true_counts(ledger_env):
    """The 2-of-3 acceptance: counts derive from rows, per (persona, domain)."""
    for outcome in ("homie_right", "homie_right", "operator_right"):
        shot = cs.record_shot(
            "sales", "pricing", "op says X", "homie says Y", "because research"
        )
        assert shot is not None and shot.status == "open"
        assert cs.reconcile(shot.id, outcome) is not None
    # A fourth shot stays open; a different domain must not pollute the count.
    open_shot = cs.record_shot("sales", "pricing", "op", "homie")
    other = cs.record_shot("sales", "hiring", "op", "homie")
    cs.reconcile(other.id, "push")

    tr = cs.track_record("sales", "pricing")
    assert (tr.resolved, tr.homie_right, tr.operator_right, tr.push, tr.open) == (
        3, 2, 1, 0, 1,
    )
    assert open_shot.id in {s.id for s in cs.list_open("sales")}


def test_persona_scoping_is_physical(ledger_env):
    """Two personas never cross — rows are keyed at the authorizing grain."""
    cs.reconcile(cs.record_shot("sales", "pricing", "a", "b").id, "homie_right")
    cs.reconcile(cs.record_shot("default", "pricing", "a", "b").id, "operator_right")
    sales = cs.track_record("sales", "pricing")
    dflt = cs.track_record("default", "pricing")
    assert (sales.homie_right, sales.operator_right) == (1, 0)
    assert (dflt.homie_right, dflt.operator_right) == (0, 1)


def test_track_record_no_domain_spans_domains(ledger_env):
    cs.reconcile(cs.record_shot("sales", "pricing", "a", "b").id, "push")
    cs.reconcile(cs.record_shot("sales", "hiring", "a", "b").id, "push")
    assert cs.track_record("sales").push == 2


def test_track_record_empty_is_zeros(ledger_env):
    tr = cs.track_record("nobody", "nothing")
    assert (tr.resolved, tr.open, tr.homie_right, tr.operator_right, tr.push) == (
        0, 0, 0, 0, 0,
    )


def test_receipts_round_trip(ledger_env):
    shot = cs.record_shot(
        "sales", "pricing", "op", "homie",
        receipts=["research/note.md:12", "MEMORY.md:88"],
    )
    row = cs.list_open("sales")[0]
    assert row.id == shot.id
    assert row.receipts == ["research/note.md:12", "MEMORY.md:88"]


def test_reconcile_unknown_id_returns_none(ledger_env):
    assert cs.reconcile(9999, "push") is None


def test_reconcile_already_resolved_is_immutable(ledger_env):
    shot = cs.record_shot("sales", "pricing", "a", "b")
    cs.reconcile(shot.id, "homie_right")
    assert cs.reconcile(shot.id, "operator_right") is None
    # The row must be UNCHANGED (state-level proof, not just the None).
    conn = sqlite3.connect(str(ledger_env.db))
    try:
        outcome = conn.execute(
            "SELECT outcome FROM called_shots WHERE id = ?", (shot.id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert outcome == "homie_right"


def test_record_shot_decided_by_operator_override(ledger_env):
    shot = cs.record_shot("sales", "pricing", "a", "b", decided_by="operator")
    assert shot.decided_by == "operator"


def test_accuracy_is_derived_not_stored(ledger_env):
    """Rule 2 physical proof: the schema has NO counter/accuracy column."""
    cs.record_shot("sales", "pricing", "a", "b")
    conn = sqlite3.connect(str(ledger_env.db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(called_shots)")}
    finally:
        conn.close()
    assert not cols & {"accuracy", "track_record", "wins", "count", "score"}


# ===========================================================================
# 3. Kill-switch — default-ON, refusals raise + count
# ===========================================================================


def test_kill_switch_disabled_refuses_every_entrypoint(ledger_env, monkeypatch):
    _stub_audit(monkeypatch)
    monkeypatch.setenv("HOMIE_KILLSWITCH_CALLED_SHOTS", "disabled")
    before = kill_switches.get_refusal_counters().get("called_shots", 0)
    for call in (
        lambda: cs.record_shot("sales", "d", "a", "b"),
        lambda: cs.list_open("sales"),
        lambda: cs.reconcile(1, "push"),
        lambda: cs.track_record("sales"),
    ):
        with pytest.raises(kill_switches.KillSwitchDisabled):
            call()
    after = kill_switches.get_refusal_counters().get("called_shots", 0)
    assert after == before + 4  # every refusal is counted


def test_kill_switch_absent_env_is_on(ledger_env):
    """Default-ON: no env var at all -> the feature just works."""
    assert cs.record_shot("sales", "d", "a", "b") is not None


# ===========================================================================
# 4. Contract errors + physical NOT NULL
# ===========================================================================


def test_record_shot_empty_persona_raises(ledger_env):
    with pytest.raises(ValueError, match="persona_id"):
        cs.record_shot("", "d", "a", "b")
    with pytest.raises(ValueError, match="persona_id"):
        cs.record_shot("   ", "d", "a", "b")


def test_record_shot_bad_decided_by_raises(ledger_env):
    with pytest.raises(ValueError, match="decided_by"):
        cs.record_shot("sales", "d", "a", "b", decided_by="jury")


def test_reconcile_bad_outcome_raises(ledger_env):
    with pytest.raises(ValueError, match="outcome"):
        cs.reconcile(1, "everyone_wins")


def test_persona_not_null_enforced_physically(ledger_env):
    """Schema-level proof: a raw NULL/empty insert dies in SQLite itself."""
    cs.record_shot("seed", "d", "a", "b")  # ensure table exists
    conn = sqlite3.connect(str(ledger_env.db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO called_shots (persona_id, created_at) VALUES (NULL, 't')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO called_shots (persona_id, created_at) VALUES ('', 't')"
            )
    finally:
        conn.close()


def test_status_outcome_pair_coupled_physically(ledger_env):
    """Schema-level proof (Kimi re-gate LOW): resolved-with-NULL-outcome and
    open-with-outcome rows both die in SQLite itself — no future writer can
    mint a row that track_record's fold would silently drop."""
    shot = cs.record_shot("seed", "d", "a", "b")
    conn = sqlite3.connect(str(ledger_env.db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE called_shots SET status = 'resolved' WHERE id = ?",
                (shot.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE called_shots SET outcome = 'push' WHERE id = ?",
                (shot.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO called_shots (persona_id, created_at, status) "
                "VALUES ('x', 't', 'resolved')"
            )
    finally:
        conn.close()


# ===========================================================================
# 5. Fail-open — runtime failures never escape
# ===========================================================================


@pytest.fixture
def broken_db_env(tmp_path, monkeypatch):
    """A DIRECTORY at the db path — every sqlite connect/write fails."""
    bad = tmp_path / "not_a_db"
    bad.mkdir()
    monkeypatch.setenv("CALLED_SHOTS_DB_PATH", str(bad))
    monkeypatch.setenv("CALLED_SHOTS_MIRROR_DIR", str(tmp_path / "m"))
    monkeypatch.delenv("HOMIE_KILLSWITCH_CALLED_SHOTS", raising=False)
    return bad


def test_fail_open_record(broken_db_env):
    assert cs.record_shot("sales", "d", "a", "b") is None  # no raise


def test_fail_open_list_open(broken_db_env):
    assert cs.list_open("sales") == []


def test_fail_open_reconcile(broken_db_env):
    assert cs.reconcile(1, "push") is None


def test_fail_open_track_record(broken_db_env):
    tr = cs.track_record("sales")
    assert (tr.resolved, tr.open) == (0, 0)
    assert tr.ok is False  # zeros mean "ledger unreadable", never "no history"


# ===========================================================================
# 6. Mirror — derived, best-effort
# ===========================================================================


def test_mirror_written_and_refreshed_on_reconcile(ledger_env):
    shot = cs.record_shot(
        "sales", "pricing", "op position", "homie position", "reasoning",
        receipts=["MEMORY.md:1"],
    )
    files = list(ledger_env.mirror.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "status: open" in text and "op position" in text and "MEMORY.md:1" in text

    cs.reconcile(shot.id, "homie_right")
    text = files[0].read_text(encoding="utf-8")  # same file, regenerated
    assert "status: resolved" in text and "homie_right" in text


def test_mirror_failure_never_fails_the_write(ledger_env, tmp_path, monkeypatch):
    """A FILE where the mirror dir should be -> mkdir fails -> row still lands."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("CALLED_SHOTS_MIRROR_DIR", str(blocker))
    shot = cs.record_shot("sales", "d", "a", "b")
    assert shot is not None
    assert len(cs.list_open("sales")) == 1  # the ledger write survived


def test_mirror_disabled_skips_write(ledger_env, monkeypatch):
    monkeypatch.setenv("CALLED_SHOTS_MIRROR_ENABLED", "false")
    cs.record_shot("sales", "d", "a", "b")
    assert not ledger_env.mirror.exists()


# ===========================================================================
# 7. Diagnostics visibility
# ===========================================================================


def test_diagnostics_payload_populated(ledger_env):
    import diagnostics

    cs.record_shot("sales", "d", "a", "b")
    report = diagnostics.DiagnosticsReport(timestamp="t", uptime_seconds=0.0)
    diagnostics._check_called_shots(report)
    assert report.called_shots["enabled"] is True
    assert report.called_shots["kill_switch_disabled"] is False
    assert report.called_shots["db_present"] is True
    assert report.called_shots["open_count"] == 1


def test_diagnostics_kill_switch_flips_enabled(ledger_env, monkeypatch):
    import diagnostics

    monkeypatch.setenv("HOMIE_KILLSWITCH_CALLED_SHOTS", "disabled")
    report = diagnostics.DiagnosticsReport(timestamp="t", uptime_seconds=0.0)
    diagnostics._check_called_shots(report)
    assert report.called_shots["enabled"] is False
    assert report.called_shots["kill_switch_disabled"] is True


# ===========================================================================
# 8. Gate R1 regressions — fail-open must survive EVERY runtime failure class
# ===========================================================================


def test_resolver_malformed_stale_age_degrades_to_default(monkeypatch):
    """R1 BLOCKER layer (i): garbage env -> default 14, never a ValueError."""
    monkeypatch.setenv("CALLED_SHOTS_STALE_AGE_DAYS", "not-an-int")
    assert config.get_called_shots_settings().stale_age_days == 14


def test_fail_open_malformed_env_all_entrypoints(ledger_env, monkeypatch):
    """R1 BLOCKER repro (a): env garbage must not escape any entrypoint."""
    monkeypatch.setenv("CALLED_SHOTS_STALE_AGE_DAYS", "garbage")
    shot = cs.record_shot("sales", "d", "a", "b")
    assert shot is not None
    assert len(cs.list_open("sales")) == 1
    assert cs.track_record("sales").open == 1
    assert cs.reconcile(shot.id, "push") is not None


def test_fail_open_garbage_db_path_type(ledger_env):
    """R1 BLOCKER repro (b): Path(123) TypeError fails open, never escapes."""
    assert cs.record_shot("sales", "d", "a", "b", db_path=123) is None
    assert cs.list_open("sales", db_path=123) == []
    assert cs.reconcile(1, "push", db_path=123) is None
    tr = cs.track_record("sales", db_path=123)
    assert tr.resolved == 0
    assert tr.ok is False


def test_fail_open_evil_receipt_object(ledger_env):
    """R1 BLOCKER repro (c): a receipt whose __str__ raises fails open."""

    class Evil:
        def __str__(self):
            raise RuntimeError("boom")

    assert cs.record_shot("sales", "d", "a", "b", receipts=[Evil()]) is None


# ===========================================================================
# 9. Gate R1 — persona seam symmetry (Rule 4)
# ===========================================================================


def test_persona_whitespace_normalized_across_entrypoints(ledger_env):
    """R1 MAJOR repro: ' sales ' records as 'sales' and READS as 'sales'."""
    shot = cs.record_shot(" sales ", "pricing", "a", "b")
    assert shot.persona_id == "sales"
    assert len(cs.list_open(" sales ")) == 1
    cs.reconcile(shot.id, "homie_right")
    assert cs.track_record(" sales ", "pricing").homie_right == 1


def test_list_open_explicit_empty_persona_raises(ledger_env):
    """R1 MAJOR repro: '' must NOT silently widen to a cross-persona read."""
    with pytest.raises(ValueError, match="persona_id"):
        cs.list_open("")
    with pytest.raises(ValueError, match="persona_id"):
        cs.list_open("   ")


def test_track_record_empty_persona_raises(ledger_env):
    with pytest.raises(ValueError, match="persona_id"):
        cs.track_record("")


def test_list_open_none_still_means_all(ledger_env):
    cs.record_shot("sales", "d", "a", "b")
    cs.record_shot("default", "d", "a", "b")
    assert len(cs.list_open(None)) == 2  # documented wildcard, unchanged


# ===========================================================================
# 10. Gate R1 — mirror frontmatter injection + query-derivation proof
# ===========================================================================


def test_mirror_frontmatter_persona_injection_blocked(ledger_env):
    """R1 MINOR: a newline-bearing persona_id cannot spoof frontmatter keys."""
    cs.record_shot("evil\nmalicious: true", "d", "a", "b")
    files = list(ledger_env.mirror.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    frontmatter = text.split("---")[1]
    # The spoofed key must never become a LINE (a YAML key); the payload text
    # surviving INSIDE the folded persona value is exactly the point.
    assert not any(
        line.strip().startswith("malicious:")
        for line in frontmatter.splitlines()
    )
    assert "persona: evil malicious: true" in frontmatter  # folded onto ONE line


def test_track_record_reflects_physically_mutated_rows(ledger_env):
    """R1 MINOR (Rule 2 PROOF): counts derive from the rows, not any cache —
    mutate a row's outcome via raw SQL and the track record follows it."""
    a = cs.record_shot("sales", "pricing", "a", "b")
    b = cs.record_shot("sales", "pricing", "a", "b")
    cs.reconcile(a.id, "homie_right")
    cs.reconcile(b.id, "homie_right")
    assert cs.track_record("sales", "pricing").homie_right == 2

    conn = sqlite3.connect(str(ledger_env.db))
    try:
        conn.execute(
            "UPDATE called_shots SET outcome = 'operator_right' WHERE id = ?",
            (b.id,),
        )
        conn.commit()
    finally:
        conn.close()

    tr = cs.track_record("sales", "pricing")
    assert (tr.homie_right, tr.operator_right) == (1, 1)  # follows physical rows


def test_diagnostics_probe_is_read_only(ledger_env, monkeypatch):
    """R1 MINOR: the probe must never CREATE/initialize a DB (mode=ro)."""
    import diagnostics

    empty = ledger_env.db  # exists() True but zero bytes — never written by us
    empty.write_bytes(b"")
    report = diagnostics.DiagnosticsReport(timestamp="t", uptime_seconds=0.0)
    diagnostics._check_called_shots(report)
    assert report.called_shots["db_present"] is True
    assert report.called_shots["open_count"] == 0
    assert empty.stat().st_size == 0  # ro probe left the file byte-untouched


# ===========================================================================
# 11. Gate R1 — the ACTUAL human /diagnostics render path
# ===========================================================================


def _render_diagnostics(monkeypatch, called_shots: dict) -> str:
    """Drive the real handle_diagnostics render with a stubbed collector."""
    import asyncio

    import core_handlers
    import diagnostics

    report = diagnostics.DiagnosticsReport(timestamp="t", uptime_seconds=0.0)
    report.called_shots = called_shots
    monkeypatch.setattr(diagnostics, "collect_diagnostics", lambda: report)
    return asyncio.run(core_handlers.handle_diagnostics(None, None, ""))


def test_diagnostics_render_shows_called_shots_on(ledger_env, monkeypatch):
    out = _render_diagnostics(
        monkeypatch,
        {"enabled": True, "kill_switch_disabled": False,
         "db_present": True, "open_count": 3},
    )
    assert "*Called Shots*" in out
    assert "status: ON" in out
    assert "open shots: 3" in out


def test_diagnostics_render_shows_kill_switch_off(ledger_env, monkeypatch):
    out = _render_diagnostics(
        monkeypatch,
        {"enabled": False, "kill_switch_disabled": True,
         "db_present": True, "open_count": 0},
    )
    assert "status: OFF (kill-switch)" in out


# ===========================================================================
# 12. Kimi gate — contract/shape regressions
# ===========================================================================


def test_reconcile_void_excluded_from_accuracy_math(ledger_env):
    """K1 MAJOR: void closes the lifecycle but never enters the denominators."""
    ids = []
    for _ in range(4):
        ids.append(cs.record_shot("sales", "pricing", "a", "b").id)
    cs.reconcile(ids[0], "homie_right")
    cs.reconcile(ids[1], "homie_right")
    cs.reconcile(ids[2], "operator_right")
    assert cs.reconcile(ids[3], "void") is not None  # CHECK accepts void

    tr = cs.track_record("sales", "pricing")
    assert (tr.resolved, tr.homie_right, tr.operator_right, tr.void) == (3, 2, 1, 1)
    assert tr.open == 0  # the voided shot's lifecycle is CLOSED
    assert cs.list_open("sales") == []


def test_domain_variants_fold_into_one_bucket(ledger_env):
    """K2 MAJOR: LLM-produced case/whitespace domain variants = ONE scorecard."""
    for domain in ("pricing", "Pricing ", " PRICING"):
        shot = cs.record_shot("sales", domain, "a", "b")
        assert shot.domain == "pricing"  # write-side normalized
        cs.reconcile(shot.id, "homie_right")
    tr = cs.track_record("sales", "Pricing")  # read-side normalized too
    assert (tr.resolved, tr.homie_right) == (3, 3)


def test_reconcile_cross_persona_refused(ledger_env, capsys):
    """K3 MAJOR (Rule 4): a caller keyed to one persona cannot settle
    another persona's bet — refused with the DISTINCT receipt."""
    shot = cs.record_shot("sales", "pricing", "a", "b")
    assert cs.reconcile(shot.id, "push", persona_id="default") is None
    assert "persona mismatch" in capsys.readouterr().out
    assert len(cs.list_open("sales")) == 1  # row untouched

    # The right persona settles it; the receipt reasons stay distinguishable.
    assert cs.reconcile(shot.id, "push", persona_id=" sales ") is not None
    assert cs.reconcile(shot.id, "push", persona_id="sales") is None
    assert "already resolved" in capsys.readouterr().out
    assert cs.reconcile(9999, "push", persona_id="sales") is None
    assert "unknown id" in capsys.readouterr().out


def test_reconcile_explicit_empty_persona_raises(ledger_env):
    with pytest.raises(ValueError, match="persona_id"):
        cs.reconcile(1, "push", persona_id="  ")


def test_track_record_ok_true_on_healthy_empty(ledger_env):
    """K4 MINOR: healthy-but-empty is ok=True zeros — distinguishable from
    the unreadable-ledger ok=False zeros."""
    tr = cs.track_record("nobody")
    assert tr.ok is True
    assert (tr.resolved, tr.open, tr.void) == (0, 0, 0)


def test_mirror_rerender_uses_fresh_row(ledger_env):
    """K5 MINOR: the mirror renders the CURRENT row, not the caller's stale
    snapshot — the interleave collapses to the re-read->replace gap."""
    shot = cs.record_shot("sales", "pricing", "a", "b")
    conn = sqlite3.connect(str(ledger_env.db))
    try:
        conn.execute(
            "UPDATE called_shots SET status='resolved', outcome='homie_right', "
            "resolved_at='now' WHERE id = ?",
            (shot.id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Re-render with the STALE open snapshot — the fresh re-read must win.
    cs._maybe_write_mirror(shot, db_path=ledger_env.db)
    text = next(iter(ledger_env.mirror.glob("*.md"))).read_text(encoding="utf-8")
    assert "status: resolved" in text and "homie_right" in text


def test_mirror_rerender_falls_back_when_row_gone(ledger_env):
    """K5 MINOR: re-read failing/missing falls back to the passed shot."""
    shot = cs.record_shot("sales", "pricing", "a", "b")
    conn = sqlite3.connect(str(ledger_env.db))
    try:
        conn.execute("DELETE FROM called_shots WHERE id = ?", (shot.id,))
        conn.commit()
    finally:
        conn.close()

    cs._maybe_write_mirror(shot, db_path=ledger_env.db)
    text = next(iter(ledger_env.mirror.glob("*.md"))).read_text(encoding="utf-8")
    assert "status: open" in text  # rendered from the passed snapshot
