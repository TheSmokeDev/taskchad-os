"""Tests for the Session Opening Brief — Living Mind Act 4 (the 6:30am moment).

Test design split by code path (categories map to the PRP's validation plan):
  1. Settings resolver — Rule 1 call-time resolution, locked defaults.
  2. Away resolver — interactive-only sessions + interactive-trigger clear
     events (R1 B1/M3 discriminators), aware-store normalization (B3).
  3. normalize_physical_timestamp — all four R1 B3 timestamp classes.
  4. Gate boundary — inclusive away threshold, disabled reads NOTHING.
  5. Boredom discriminator — THE wrong-condition test (R1 B2, both sides).
  6. Content contract — caps, ordering, freshness, sanitization, M4
     cap-priority semantics.
 10. Brief-owed marker IO + note_router_activity seam units (R1 B4).
 11. Builder partial-failure seams (per-source degradation).
 13. Cross-lane survival — the brief lands in `User task:` on generic lanes.

No test touches live vault/state files — all paths are tmp_path-scoped;
clocks are injected via ``now=``/``last_activity=``. Born-clean fixtures:
all ids are the synthetic ``telegram:1111111111:2222222222`` family.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402
import engine as engine_module  # noqa: E402
from cognition.proactive_brief import (  # noqa: E402
    SessionOpeningBrief,
    build_session_opening_brief,
    clear_brief_owed,
    normalize_physical_timestamp,
    read_recent_operator_journal_lines,
    read_brief_owed,
    write_brief_owed,
)
from session import Session, SQLiteSessionStore  # noqa: E402

SESSION_BRIEF_ENV_VARS = (
    "SESSION_BRIEF_ENABLED",
    "SESSION_BRIEF_AWAY_HOURS",
    "SESSION_BRIEF_MIN_FRESH_ITEMS",
    "SESSION_BRIEF_MAX_PER_SECTION",
    "SESSION_BRIEF_MAX_CHARS",
)

# Fixed clocks — the operator left 2026-06-11 22:00 and opens 06:30 next day.
LAST_ACTIVITY = datetime(2026, 6, 11, 22, 0)
NOW = datetime(2026, 6, 12, 6, 30)

WM_TEMPLATE = (
    "---\n"
    "tags: [system, memory, working]\n"
    "status: current\n"
    "date: {date}\n"
    'summary: "test"\n'
    "---\n"
    "\n"
    "# WORKING.md\n"
    "\n"
    "## Open Threads\n"
    "\n"
    "{threads}\n"
    "\n"
    "## Active Hypotheses\n"
    "\n"
    "\n"
    "## Unresolved Questions\n"
    "\n"
    "\n"
    "## Heartbeat Observations\n"
    "\n"
    "{observations}\n"
    "\n"
    "## Archived (Cold)\n"
)

EPISODE_TEMPLATE = (
    "---\n"
    "tags: [system, memory, living-mind]\n"
    "status: {status}\n"
    "date: {date}\n"
    'session_id: "telegram-1111111111-2222222222"\n'
    "surface: {surface}\n"
    'lifecycle: "{lifecycle}"\n'
    'summary: "{summary}"\n'
    "---\n"
    "\n"
    "# Episode\n"
    "\n"
    "## Summary\n"
    "\n"
    "body\n"
)


def _sweep_brief_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in SESSION_BRIEF_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _vault(
    tmp_path: Path,
    *,
    threads: str = "",
    observations: str = "",
    date: str = "2026-06-12",
) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / "WORKING.md").write_text(
        WM_TEMPLATE.format(date=date, threads=threads, observations=observations),
        encoding="utf-8",
    )
    return vault


def _write_episode(
    vault: Path,
    name: str,
    *,
    status: str = "open",
    date: str = "2026-06-12",
    surface: str = "telegram",
    lifecycle: str = "20260612-053000",
    summary: str = "an episode summary",
) -> Path:
    episodes_dir = vault / "episodes"
    episodes_dir.mkdir(exist_ok=True)
    path = episodes_dir / name
    path.write_text(
        EPISODE_TEMPLATE.format(
            status=status,
            date=date,
            surface=surface,
            lifecycle=lifecycle,
            summary=summary,
        ),
        encoding="utf-8",
    )
    return path


def _ledger_row(
    *,
    status: str = "applied",
    applied_at: str | None = None,
    summary: str = "learned a durable lesson",
    target_file: str = "MEMORY.md",
    row_id: str = "11784e97-1111-2222-3333-444444444444",
) -> str:
    row = {
        "id": row_id,
        "created_at": "2026-06-12T05:00:00+00:00",
        "source": "reflection",
        "target_file": target_file,
        "summary": summary,
        "rationale": "",
        "evidence_paths": [],
        "proposed_content": "content " + row_id,
        "status": status,
        "dedupe_key": "",
        "confidence_score": 0.9,
        "applied_at": applied_at,
    }
    return json.dumps(row)


def _ledger(tmp_path: Path, rows: list[str]) -> Path:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    return ledger


def _cofounder_ledger(tmp_path: Path, rows: list[dict] | None = None) -> Path:
    path = tmp_path / "cofounder_delegation.jsonl"
    lines = [json.dumps(row) for row in (rows or [])]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def _delegation_row(
    *,
    outcome: str = "sent",
    timestamp: str | None = None,
    line: int = 2,
    persona: str = "marketing",
    approved_by: str | None = "cofounder-autopilot",
    detail: str = "Draft the lead-delivery acceptance checklist",
) -> dict:
    return {
        "timestamp": timestamp or _utc_iso_for_local(datetime(2026, 6, 12, 3, 15)),
        "local_date": "2026-06-12",
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


def _agendas(tmp_path: Path, agendas: dict[str, dict] | None = None) -> Path:
    agenda_dir = tmp_path / "cofounder" / "agendas"
    agenda_dir.mkdir(parents=True, exist_ok=True)
    for day, payload in (agendas or {}).items():
        (agenda_dir / f"AGENDA-{day}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    return agenda_dir


def _agenda_item(
    *,
    n: int = 2,
    status: str = "done",
    persona: str = "seo_geo",
    reported_at: str | None = None,
    result_summary: str = "deliverable written: GSC indexing plan",
    task: str = "Prepare a GSC/indexing submission plan",
) -> dict:
    item = {
        "n": n,
        "persona": persona,
        "repo": "YourProduct",
        "task": task,
        "priority": 1,
        "mode": "draft",
        "status": status,
        "result_summary": result_summary,
    }
    if reported_at is not None:
        item["reported_at"] = reported_at
    return item


def _build(
    tmp_path: Path,
    vault: Path,
    *,
    last_activity: datetime | None = LAST_ACTIVITY,
    now: datetime = NOW,
    ledger_file: Path | None = None,
    delegation_ledger_file: Path | None = None,
    agenda_dir: Path | None = None,
    settings=None,
) -> SessionOpeningBrief:
    if ledger_file is None:
        ledger_file = _ledger(tmp_path, [])
    if delegation_ledger_file is None:
        delegation_ledger_file = _cofounder_ledger(tmp_path)
    if agenda_dir is None:
        agenda_dir = _agendas(tmp_path)
    return build_session_opening_brief(
        vault,
        last_activity=last_activity,
        now=now,
        settings=settings,
        ledger_file=ledger_file,
        delegation_ledger_file=delegation_ledger_file,
        agenda_dir=agenda_dir,
    )


def _utc_iso_for_local(local_dt: datetime) -> str:
    """Aware-UTC ISO string whose LOCAL equivalent is ``local_dt``."""
    local_tz = datetime.now().astimezone().tzinfo
    return local_dt.replace(tzinfo=local_tz).astimezone(UTC).isoformat()


# =============================================================================
# Category 1 — settings resolver (Rule 1)
# =============================================================================


class TestSettingsResolver:
    def test_locked_defaults(self, monkeypatch):
        _sweep_brief_env(monkeypatch)
        settings = config.get_session_brief_settings()
        assert settings.enabled is True
        assert settings.away_hours == 8.0
        assert settings.min_fresh_items == 1
        assert settings.max_per_section == 5
        assert settings.max_chars == 2400

    def test_setenv_wins_on_next_call_without_reload(self, monkeypatch):
        _sweep_brief_env(monkeypatch)
        before = config.get_session_brief_settings()
        assert before.away_hours == 8.0
        monkeypatch.setenv("SESSION_BRIEF_AWAY_HOURS", "2.5")
        monkeypatch.setenv("SESSION_BRIEF_ENABLED", "false")
        monkeypatch.setenv("SESSION_BRIEF_MAX_CHARS", "999")
        after = config.get_session_brief_settings()
        assert after.away_hours == 2.5
        assert after.enabled is False
        assert after.max_chars == 999

    def test_explicit_args_win_over_env(self, monkeypatch):
        monkeypatch.setenv("SESSION_BRIEF_AWAY_HOURS", "2.5")
        monkeypatch.setenv("SESSION_BRIEF_MIN_FRESH_ITEMS", "7")
        settings = config.get_session_brief_settings(
            away_hours=12.0, min_fresh_items=3,
        )
        assert settings.away_hours == 12.0
        assert settings.min_fresh_items == 3


# =============================================================================
# Category 3 — normalize_physical_timestamp (R1 B3, all four classes)
# =============================================================================


class TestNormalizePhysicalTimestamp:
    def test_aware_postgres_datetime_to_local_naive(self):
        aware = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
        out = normalize_physical_timestamp(aware)
        assert out is not None
        assert out.tzinfo is None
        assert out == aware.astimezone().replace(tzinfo=None)

    def test_naive_clear_event_iso_string_unchanged(self):
        out = normalize_physical_timestamp("2026-06-12T06:30:00.123456")
        assert out == datetime(2026, 6, 12, 6, 30, 0, 123456)
        assert out.tzinfo is None

    def test_aware_iso_string_converted(self):
        out = normalize_physical_timestamp("2026-06-12T10:00:00+00:00")
        expected = datetime(2026, 6, 12, 10, 0, tzinfo=UTC).astimezone().replace(
            tzinfo=None
        )
        assert out == expected
        assert out.tzinfo is None

    def test_aware_utc_amendment_crossing_local_day_boundary(self):
        # An instant near UTC midnight — the local date is whatever the LOCAL
        # zone says, never the raw UTC date. The freshness discriminator.
        iso = "2026-06-12T00:30:00+00:00"
        out = normalize_physical_timestamp(iso)
        expected = datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)
        assert out == expected
        assert out.date() == expected.date()

    def test_none_empty_and_garbage_never_raise(self):
        assert normalize_physical_timestamp(None) is None
        assert normalize_physical_timestamp("") is None
        assert normalize_physical_timestamp("   ") is None
        assert normalize_physical_timestamp("not-a-date") is None
        assert normalize_physical_timestamp(12345) is None  # type: ignore[arg-type]

    def test_naive_datetime_passthrough(self):
        naive = datetime(2026, 6, 12, 6, 30)
        assert normalize_physical_timestamp(naive) is naive


# =============================================================================
# Category 2 — away resolver (engine.resolve_last_operator_activity)
# =============================================================================


def _seed_session(
    store: SQLiteSessionStore,
    *,
    key: str,
    updated_at: datetime,
    source: str = "interactive",
) -> None:
    platform, channel, thread = key.split(":")
    store.create(
        Session(
            session_id=key,
            agent_session_id="",
            platform=platform,
            channel_id=channel,
            thread_id=thread,
            user_id="1111111111",
            created_at=updated_at,
            updated_at=updated_at,
            message_count=1,
            source=source,
        )
    )


def _write_events(state_dir: Path, rows: list[dict | str]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "clear-lifecycle-events.jsonl"
    lines = [
        row if isinstance(row, str) else json.dumps(row) for row in rows
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestAwayResolver:
    def test_newest_interactive_session_returned(self, tmp_path):
        store = SQLiteSessionStore(tmp_path / "chat.db")
        _seed_session(
            store,
            key="telegram:1111111111:2222222222",
            updated_at=datetime(2026, 6, 11, 10, 0),
        )
        out = engine_module.resolve_last_operator_activity(
            store, state_dir=tmp_path / "state"
        )
        assert out == datetime(2026, 6, 11, 10, 0)

    def test_cron_tool_hook_rows_ignored_even_when_newer(self, tmp_path):
        store = SQLiteSessionStore(tmp_path / "chat.db")
        _seed_session(
            store,
            key="telegram:1111111111:2222222222",
            updated_at=datetime(2026, 6, 11, 10, 0),
        )
        _seed_session(
            store,
            key="cli:3333333333:3333333333",
            updated_at=datetime(2026, 6, 11, 11, 0),
            source="cron",
        )
        _seed_session(
            store,
            key="cli:4444444444:4444444444",
            updated_at=datetime(2026, 6, 11, 12, 0),
            source="tool",
        )
        _seed_session(
            store,
            key="cli:5555555555:5555555555",
            updated_at=datetime(2026, 6, 11, 13, 0),
            source="hook",
        )
        out = engine_module.resolve_last_operator_activity(
            store, state_dir=tmp_path / "state"
        )
        assert out == datetime(2026, 6, 11, 10, 0)

    def test_interactive_clear_event_wins_after_delete_on_clear(self, tmp_path):
        store = SQLiteSessionStore(tmp_path / "chat.db")
        _seed_session(
            store,
            key="telegram:1111111111:2222222222",
            updated_at=datetime(2026, 6, 11, 10, 0),
        )
        store.delete("telegram", "1111111111", "2222222222")
        state_dir = tmp_path / "state"
        _write_events(
            state_dir,
            [{
                "timestamp": "2026-06-11T14:00:00",
                "session_id": "telegram:1111111111:2222222222",
                "trigger_source": "interactive",
            }],
        )
        out = engine_module.resolve_last_operator_activity(
            store, state_dir=state_dir
        )
        assert out == datetime(2026, 6, 11, 14, 0)

    def test_newer_cron_triggered_clear_ignored(self, tmp_path):
        """B1/M3 discriminator: interactive session 10:00 + cron clear 11:00
        -> 10:00; adding an interactive clear 12:00 -> 12:00."""
        store = SQLiteSessionStore(tmp_path / "chat.db")
        _seed_session(
            store,
            key="telegram:1111111111:2222222222",
            updated_at=datetime(2026, 6, 11, 10, 0),
        )
        state_dir = tmp_path / "state"
        _write_events(
            state_dir,
            [{
                "timestamp": "2026-06-11T11:00:00",
                "session_id": "cli:3333333333:3333333333",
                "trigger_source": "cron",
            }],
        )
        out = engine_module.resolve_last_operator_activity(
            store, state_dir=state_dir
        )
        assert out == datetime(2026, 6, 11, 10, 0)

        _write_events(
            state_dir,
            [
                {
                    "timestamp": "2026-06-11T11:00:00",
                    "session_id": "cli:3333333333:3333333333",
                    "trigger_source": "cron",
                },
                {
                    "timestamp": "2026-06-11T12:00:00",
                    "session_id": "telegram:1111111111:2222222222",
                    "trigger_source": "interactive",
                },
            ],
        )
        out = engine_module.resolve_last_operator_activity(
            store, state_dir=state_dir
        )
        assert out == datetime(2026, 6, 11, 12, 0)

    def test_legacy_row_without_trigger_source_is_interactive(self, tmp_path):
        """Dated-note contract (2026-06-12): missing field = legacy operator
        /clear — it counts as presence."""
        store = SQLiteSessionStore(tmp_path / "chat.db")
        state_dir = tmp_path / "state"
        _write_events(
            state_dir,
            [{
                "timestamp": "2026-06-11T09:00:00",
                "session_id": "telegram:1111111111:2222222222",
            }],
        )
        out = engine_module.resolve_last_operator_activity(
            store, state_dir=state_dir
        )
        assert out == datetime(2026, 6, 11, 9, 0)

    def test_aware_store_updated_at_normalized_no_typeerror(self, tmp_path):
        """B3: a Postgres-shaped store returns AWARE datetimes — the resolver
        must normalize, not raise, and produce a correct max vs a naive
        clear event."""
        aware = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
        fake_store = SimpleNamespace(
            list_recent=lambda **kwargs: [SimpleNamespace(updated_at=aware)]
        )
        state_dir = tmp_path / "state"
        expected_session = aware.astimezone().replace(tzinfo=None)
        # A naive interactive clear event one hour after the session leg.
        event_ts = expected_session + timedelta(hours=1)
        _write_events(
            state_dir,
            [{
                "timestamp": event_ts.isoformat(),
                "trigger_source": "interactive",
            }],
        )
        out = engine_module.resolve_last_operator_activity(
            fake_store, state_dir=state_dir
        )
        assert out == event_ts

    def test_malformed_jsonl_lines_skipped(self, tmp_path):
        store = SQLiteSessionStore(tmp_path / "chat.db")
        state_dir = tmp_path / "state"
        _write_events(
            state_dir,
            [
                "not json at all {{{",
                json.dumps(["a", "list", "row"]),
                {"timestamp": "garbage-timestamp", "trigger_source": "interactive"},
                {"timestamp": "2026-06-11T08:00:00", "trigger_source": "interactive"},
            ],
        )
        out = engine_module.resolve_last_operator_activity(
            store, state_dir=state_dir
        )
        assert out == datetime(2026, 6, 11, 8, 0)

    def test_no_evidence_returns_none(self, tmp_path):
        store = SQLiteSessionStore(tmp_path / "chat.db")
        out = engine_module.resolve_last_operator_activity(
            store, state_dir=tmp_path / "state"
        )
        assert out is None

    def test_store_raising_other_leg_still_answers(self, tmp_path):
        class _BrokenStore:
            def list_recent(self, **kwargs):
                raise RuntimeError("store down")

        state_dir = tmp_path / "state"
        _write_events(
            state_dir,
            [{
                "timestamp": "2026-06-11T07:00:00",
                "trigger_source": "interactive",
            }],
        )
        out = engine_module.resolve_last_operator_activity(
            _BrokenStore(), state_dir=state_dir
        )
        assert out == datetime(2026, 6, 11, 7, 0)


# =============================================================================
# Category 4 — gate boundary (builder, fixed clocks)
# =============================================================================


class TestGateBoundary:
    def test_exactly_threshold_fires_inclusive(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path, observations="- [2026-06-12] [calendar] busy day: 5 events"
        )
        last = NOW - timedelta(hours=8)  # exactly 8h
        brief = _build(tmp_path, vault, last_activity=last)
        assert brief.fired is True
        assert brief.away_hours == pytest.approx(8.0)

    def test_one_second_under_threshold_not_away(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path, observations="- [2026-06-12] [calendar] busy day: 5 events"
        )
        last = NOW - timedelta(hours=8) + timedelta(seconds=1)
        brief = _build(tmp_path, vault, last_activity=last)
        assert brief.fired is False
        assert brief.suppressed_reason == "not_away"
        assert brief.prompt_block == ""

    def test_no_history(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        brief = _build(tmp_path, vault, last_activity=None)
        assert brief.suppressed_reason == "no_history"
        assert brief.away_hours is None

    def test_disabled_reads_no_sources(self, tmp_path, monkeypatch):
        """Ordering proof: with the kill switch off, the builder must return
        BEFORE any source read happens."""
        _sweep_brief_env(monkeypatch)
        monkeypatch.setenv("SESSION_BRIEF_ENABLED", "false")
        import living_memory

        calls: list[Path] = []
        monkeypatch.setattr(
            living_memory,
            "read_working_memory",
            lambda memory_dir: calls.append(memory_dir),
        )
        missing = tmp_path / "does-not-exist"
        brief = build_session_opening_brief(
            missing,
            last_activity=LAST_ACTIVITY,
            now=NOW,
            ledger_file=tmp_path / "no-ledger.jsonl",
            delegation_ledger_file=tmp_path / "no-delegations.jsonl",
            agenda_dir=tmp_path / "no-agendas",
        )
        assert brief.suppressed_reason == "disabled"
        assert brief.prompt_block == ""
        assert calls == []

    def test_away_hours_env_override_moves_boundary(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setenv("SESSION_BRIEF_AWAY_HOURS", "2")
        vault = _vault(
            tmp_path, observations="- [2026-06-12] [calendar] busy day: 5 events"
        )
        last = NOW - timedelta(hours=3)
        brief = _build(tmp_path, vault, last_activity=last)
        assert brief.fired is True


# =============================================================================
# Category 5 — boredom discriminator (THE wrong-condition test, R1 B2)
# =============================================================================


class TestBoredomDiscriminator:
    def test_stale_only_vault_stays_silent_at_24h(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path,
            threads="- [2026-06-01] follow up on the vendor invoice",
            observations="- [2026-06-01] [calendar] busy day: 5 events",
            date="2026-06-01",
        )
        brief = _build(
            tmp_path, vault, last_activity=NOW - timedelta(hours=24)
        )
        assert brief.fired is False
        assert brief.suppressed_reason == "no_fresh_items"
        assert brief.prompt_block == ""

    def test_one_fresh_observation_flips_it(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path,
            threads="- [2026-06-01] follow up on the vendor invoice",
            observations=(
                "- [2026-06-01] [calendar] busy day: 5 events\n"
                "- [2026-06-12] [email] 3 urgent unread emails"
            ),
        )
        brief = _build(tmp_path, vault)
        assert brief.fired is True
        assert brief.fresh_items == 1
        assert "3 urgent unread emails" in brief.prompt_block
        assert "## What changed while away" in brief.prompt_block

    def test_fresh_heartbeat_thread_alone_fires(self, tmp_path, monkeypatch):
        """B2 side one: a fresh [heartbeat]-tagged thread IS a change."""
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path,
            threads="- [2026-06-12] [heartbeat] google:oauth_invalid_grant persists",
        )
        brief = _build(tmp_path, vault)
        assert brief.fired is True
        assert brief.fresh_items == 1
        # Rendered in "What changed", not merely Mid-flight.
        what = brief.prompt_block.split("## What changed while away")[1]
        assert "google:oauth_invalid_grant persists" in what

    def test_same_bullet_without_heartbeat_tag_stays_silent(
        self, tmp_path, monkeypatch
    ):
        """B2 side two: a fresh MANUAL thread never counts, at any away
        duration."""
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path,
            threads="- [2026-06-12] google:oauth_invalid_grant persists",
        )
        brief = _build(
            tmp_path, vault, last_activity=NOW - timedelta(hours=72)
        )
        assert brief.fired is False
        assert brief.suppressed_reason == "no_fresh_items"
        assert brief.prompt_block == ""

    def test_min_fresh_items_env_honored(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setenv("SESSION_BRIEF_MIN_FRESH_ITEMS", "2")
        vault = _vault(
            tmp_path,
            observations="- [2026-06-12] [calendar] busy day: 5 events",
        )
        brief = _build(tmp_path, vault)
        assert brief.fired is False
        assert brief.suppressed_reason == "no_fresh_items"
        assert brief.fresh_items == 1


# =============================================================================
# Category 6 — content contract
# =============================================================================


class TestContentContract:
    def test_recent_daily_journals_and_vault_ops_receipt_ground_the_brief(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        daily = vault / "daily"
        daily.mkdir()
        (daily / "2026-06-12.md").write_text(
            "# Daily\n\n- Restored Google OAuth with live Gmail proof.\n"
            "- YourBusiness queue is a draft, not proof of calls.\n",
            encoding="utf-8",
        )
        ops = vault / "_ops"
        ops.mkdir()
        (ops / "history.md").write_text(
            "## 2026-06-12\n\n"
            "- **Actions**: Read daily logs and verified Gmail live.\n"
            "- **Impact**: Removed the stale OAuth blocker.\n",
            encoding="utf-8",
        )

        brief = _build(tmp_path, vault)

        assert brief.fired is True
        assert "journal (2026-06-12)" in brief.prompt_block
        assert "Restored Google OAuth" in brief.prompt_block
        assert "vault-ops receipt" in brief.prompt_block
        assert "Removed the stale OAuth blocker" in brief.prompt_block
        assert "Never describe a draft" in brief.prompt_block

    def test_journal_reader_ignores_notes_older_than_away_boundary(self, tmp_path):
        vault = _vault(tmp_path)
        daily = vault / "daily"
        daily.mkdir()
        (daily / "2026-06-10.md").write_text(
            "- old item that must not resurface\n", encoding="utf-8"
        )

        lines = read_recent_operator_journal_lines(vault, since=LAST_ACTIVITY)

        assert lines == []

    def test_per_source_cap_and_newest_first(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setenv("SESSION_BRIEF_MAX_PER_SECTION", "3")
        observations = "\n".join(
            f"- [2026-06-12] [calendar] observation number {i}" for i in range(1, 6)
        )
        vault = _vault(tmp_path, observations=observations)
        brief = _build(tmp_path, vault)
        assert brief.fired is True
        # Cap 3, seeded 5 (+2 over): only the NEWEST three render.
        for i in (5, 4, 3):
            assert f"observation number {i}" in brief.prompt_block
        for i in (2, 1):
            assert f"observation number {i}" not in brief.prompt_block
        # Newest first: 5 before 4 before 3.
        i5 = brief.prompt_block.index("observation number 5")
        i4 = brief.prompt_block.index("observation number 4")
        i3 = brief.prompt_block.index("observation number 3")
        assert i5 < i4 < i3

    def test_empty_section_suppression(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path, observations="- [2026-06-12] [calendar] busy day: 5 events"
        )
        brief = _build(tmp_path, vault)
        assert brief.fired is True
        assert "## Mid-flight" not in brief.prompt_block
        assert "## Self updates" not in brief.prompt_block

    def test_episode_freshness_exact_and_status_agnostic(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        # 1s BEFORE the boundary instant — excluded.
        _write_episode(
            vault,
            "2026-06-11-telegram-aaaa1111-215959.md",
            date="2026-06-11",
            lifecycle="20260611-215959",
            summary="too old to mention",
        )
        # Exactly the boundary — excluded (strict).
        _write_episode(
            vault,
            "2026-06-11-telegram-aaaa1111-220000.md",
            date="2026-06-11",
            lifecycle="20260611-220000",
            summary="exactly the boundary",
        )
        # 1s AFTER — included; CONSOLIDATED — the status-agnostic
        # discriminator (the overnight dream must not hide it).
        _write_episode(
            vault,
            "2026-06-11-telegram-aaaa1111-220001.md",
            status="consolidated",
            date="2026-06-11",
            lifecycle="20260611-220001",
            summary="overnight consolidated episode",
        )
        brief = _build(tmp_path, vault)
        assert brief.fired is True
        assert "overnight consolidated episode" in brief.prompt_block
        assert "too old to mention" not in brief.prompt_block
        assert "exactly the boundary" not in brief.prompt_block

    def test_working_day_floor_documented_granularity(
        self, tmp_path, monkeypatch
    ):
        """Locked as intended: a same-day bullet written BEFORE the boundary
        instant still counts fresh (WORKING.md carries day resolution)."""
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path,
            observations="- [2026-06-12] [calendar] same day before leaving",
        )
        last = datetime(2026, 6, 12, 8, 0)
        brief = _build(
            tmp_path, vault, last_activity=last, now=datetime(2026, 6, 12, 18, 0),
        )
        assert brief.fired is True
        assert "same day before leaving" in brief.prompt_block

    def test_amendment_freshness_strict_with_utc_conversion(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        fresh_applied = _utc_iso_for_local(LAST_ACTIVITY + timedelta(hours=2))
        stale_applied = _utc_iso_for_local(LAST_ACTIVITY - timedelta(hours=2))
        ledger = _ledger(
            tmp_path,
            [
                _ledger_row(
                    applied_at=fresh_applied,
                    summary="fresh applied amendment",
                    row_id="11784e97-1111-2222-3333-444444444401",
                ),
                _ledger_row(
                    applied_at=stale_applied,
                    summary="stale applied amendment",
                    row_id="11784e97-1111-2222-3333-444444444402",
                ),
                _ledger_row(
                    status="pending",
                    applied_at=None,
                    summary="pending amendment",
                    row_id="11784e97-1111-2222-3333-444444444403",
                ),
            ],
        )
        brief = _build(tmp_path, vault, ledger_file=ledger)
        assert brief.fired is True
        assert brief.fresh_items == 1
        assert "## Self updates (memory amendments)" in brief.prompt_block
        assert "fresh applied amendment" in brief.prompt_block
        assert "stale applied amendment" not in brief.prompt_block
        assert "pending amendment" not in brief.prompt_block
        assert "MEMORY.md:" in brief.prompt_block

    def test_per_line_sanitization(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path,
            observations='- [2026-06-12] [email] `weird` "quoted"\ttab bullet',
        )
        brief = _build(tmp_path, vault)
        assert brief.fired is True
        assert "`" not in brief.prompt_block.split("conversation wins.")[1]
        assert "weird quoted tab bullet" in brief.prompt_block

    def test_cap_priority_semantics_m4(self, tmp_path, monkeypatch):
        """M4: per-source caps before the total cap; instruction reserved
        byte-intact; >=1 item per fired fresh source survives; Mid-flight
        (context-only) drops first; marker lands at a newline boundary."""
        _sweep_brief_env(monkeypatch)
        monkeypatch.setenv("SESSION_BRIEF_MAX_CHARS", "10")  # deliberately tiny
        observations = "\n".join(
            f"- [2026-06-12] [calendar] observation number {i}" for i in range(1, 4)
        )
        threads = "\n".join(
            f"- [2026-06-10] manual context thread {i}" for i in range(1, 4)
        )
        vault = _vault(tmp_path, observations=observations, threads=threads)
        _write_episode(
            vault,
            "2026-06-12-telegram-aaaa1111-053000.md",
            summary="episode under pressure",
        )
        ledger = _ledger(
            tmp_path,
            [_ledger_row(
                applied_at=_utc_iso_for_local(LAST_ACTIVITY + timedelta(hours=1)),
                summary="amendment under pressure",
            )],
        )
        brief = _build(tmp_path, vault, ledger_file=ledger)
        assert brief.fired is True
        block = brief.prompt_block
        # Instruction survives byte-intact.
        assert block.startswith("# Session Opening Brief (deliver first)")
        assert "OPEN your reply" in block
        assert "conversation wins." in block
        # One item from EACH fired fresh source survives.
        assert "observation number 3" in block  # newest observation
        assert "episode under pressure" in block
        assert "amendment under pressure" in block
        # Context-only Mid-flight dropped first — entirely gone here.
        assert "## Mid-flight" not in block
        assert "manual context thread" not in block
        # Truncation marker at a newline boundary.
        assert block.endswith("\n[TRUNCATED]")

    def test_instruction_names_away_duration(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path, observations="- [2026-06-12] [calendar] busy day: 5 events"
        )
        brief = _build(tmp_path, vault)
        assert "~8.5h" in brief.prompt_block
        assert "last activity 2026-06-11 22:00" in brief.prompt_block

    def test_midflight_renders_context_threads_when_fired(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path,
            threads="- [2026-06-01] stale manual thread rides along",
            observations="- [2026-06-12] [calendar] busy day: 5 events",
        )
        brief = _build(tmp_path, vault)
        assert brief.fired is True
        assert brief.fresh_items == 1  # the thread never counts
        assert "## Mid-flight (open threads)" in brief.prompt_block
        assert "stale manual thread rides along" in brief.prompt_block


# =============================================================================
# Category 14 — co-founder outcomes as a COUNTED source (#411).
#
# Two physical reads, one counted group: the append-only delegation ledger
# (`outcome=sent`) and terminal agenda-JSON flips (`reported_at`). Overnight
# outcomes ALONE must be able to wake the brief; still-proposed lines are
# orientation and must not.
# =============================================================================

FRESH_SENT_AT = datetime(2026, 6, 12, 3, 15)
FRESH_FLIP_AT = datetime(2026, 6, 12, 5, 0)


class TestCofounderCountedSource:
    def test_overnight_self_delegation_alone_fires(self, tmp_path, monkeypatch):
        """THE acceptance path: nothing else fresh in the vault, yet the
        operator still wakes up to a brief."""
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        ledger = _cofounder_ledger(tmp_path, [_delegation_row()])
        brief = _build(tmp_path, vault, delegation_ledger_file=ledger)
        assert brief.fired is True
        assert brief.fresh_items == 1
        assert "## What changed while away" in brief.prompt_block
        assert (
            "- cofounder: delegated line 2 to marketing (self-assigned): "
            "Draft the lead-delivery acceptance checklist" in brief.prompt_block
        )

    def test_human_approver_renders_by_name(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        ledger = _cofounder_ledger(
            tmp_path, [_delegation_row(approved_by="operator-chat-confirm")]
        )
        brief = _build(tmp_path, vault, delegation_ledger_file=ledger)
        assert brief.fired is True
        assert "(operator-chat-confirm)" in brief.prompt_block
        assert "self-assigned" not in brief.prompt_block

    def test_blank_approver_drops_the_parenthetical(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        ledger = _cofounder_ledger(tmp_path, [_delegation_row(approved_by=None)])
        brief = _build(tmp_path, vault, delegation_ledger_file=ledger)
        assert brief.fired is True
        assert "- cofounder: delegated line 2 to marketing: " in brief.prompt_block
        assert "()" not in brief.prompt_block

    def test_only_sent_outcomes_count(self, tmp_path, monkeypatch):
        """The ledger records every attempt; only an actual SEND is a change."""
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        ledger = _cofounder_ledger(
            tmp_path,
            [
                _delegation_row(outcome=outcome, detail=f"outcome {outcome}")
                for outcome in ("worktick-done", "report-done", "refused", "error")
            ],
        )
        brief = _build(tmp_path, vault, delegation_ledger_file=ledger)
        assert brief.fired is False
        assert brief.suppressed_reason == "no_fresh_items"
        assert brief.fresh_items == 0

    def test_sent_freshness_is_strict_after_the_boundary(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        stale = _cofounder_ledger(
            tmp_path,
            [
                _delegation_row(
                    timestamp=_utc_iso_for_local(LAST_ACTIVITY - timedelta(seconds=1)),
                    detail="one second too early",
                ),
                _delegation_row(
                    timestamp=_utc_iso_for_local(LAST_ACTIVITY),
                    detail="exactly the boundary",
                ),
            ],
        )
        assert _build(tmp_path, vault, delegation_ledger_file=stale).fired is False

        fresh = _cofounder_ledger(
            tmp_path,
            [
                _delegation_row(
                    timestamp=_utc_iso_for_local(LAST_ACTIVITY + timedelta(seconds=1)),
                    detail="one second after the boundary",
                )
            ],
        )
        brief = _build(tmp_path, vault, delegation_ledger_file=fresh)
        assert brief.fired is True
        assert "one second after the boundary" in brief.prompt_block

    def test_missing_ledger_contributes_nothing(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        brief = _build(
            tmp_path, vault, delegation_ledger_file=tmp_path / "never-written.jsonl"
        )
        assert brief.fired is False
        assert brief.fresh_items == 0

    def test_malformed_ledger_lines_skipped(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        path = tmp_path / "cofounder_delegation.jsonl"
        path.write_text(
            "\n".join(
                [
                    "not json at all",
                    "{truncated",
                    "[]",
                    '"a bare string"',
                    json.dumps(_delegation_row(detail="the one good row")),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        brief = _build(tmp_path, vault, delegation_ledger_file=path)
        assert brief.fired is True
        assert brief.fresh_items == 1
        assert "the one good row" in brief.prompt_block

    def test_ledger_as_directory_fails_open_other_sources_render(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path, observations="- [2026-06-12] [calendar] busy day: 5 events"
        )
        bad = tmp_path / "delegations-as-dir.jsonl"
        bad.mkdir()
        brief = _build(tmp_path, vault, delegation_ledger_file=bad)
        assert brief.fired is True
        assert "busy day: 5 events" in brief.prompt_block
        assert "cofounder:" not in brief.prompt_block

    def test_agenda_completion_alone_fires(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        agenda_dir = _agendas(
            tmp_path,
            {
                "2026-06-12": {
                    "date": "2026-06-12",
                    "items": [
                        _agenda_item(
                            reported_at=_utc_iso_for_local(FRESH_FLIP_AT),
                            result_summary="deliverable written: GSC indexing plan",
                        )
                    ],
                }
            },
        )
        brief = _build(tmp_path, vault, agenda_dir=agenda_dir)
        assert brief.fired is True
        assert brief.fresh_items == 1
        assert (
            "- cofounder: line 2 (seo_geo) done: deliverable written: GSC "
            "indexing plan" in brief.prompt_block
        )

    def test_failed_flip_counts_too(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        agenda_dir = _agendas(
            tmp_path,
            {
                "2026-06-12": {
                    "items": [
                        _agenda_item(
                            status="failed",
                            reported_at=_utc_iso_for_local(FRESH_FLIP_AT),
                            result_summary="archon run 88 failed",
                        )
                    ]
                }
            },
        )
        brief = _build(tmp_path, vault, agenda_dir=agenda_dir)
        assert brief.fired is True
        assert "- cofounder: line 2 (seo_geo) failed: archon run 88 failed" in (
            brief.prompt_block
        )

    def test_non_terminal_statuses_never_count(self, tmp_path, monkeypatch):
        """Today's proposed/delegated/dispatched lines are ORIENTATION — they
        belong to the passive portfolio region, not the counted brief."""
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        agenda_dir = _agendas(
            tmp_path,
            {
                "2026-06-12": {
                    "items": [
                        _agenda_item(
                            n=i,
                            status=status,
                            reported_at=_utc_iso_for_local(FRESH_FLIP_AT),
                            result_summary=f"status {status}",
                        )
                        for i, status in enumerate(
                            ("proposed", "delegated", "dispatched"), start=1
                        )
                    ]
                }
            },
        )
        brief = _build(tmp_path, vault, agenda_dir=agenda_dir)
        assert brief.fired is False
        assert brief.fresh_items == 0

    def test_completion_without_reported_at_skipped(self, tmp_path, monkeypatch):
        """No stamp = no provable flip instant; a legacy row must not claim
        to be overnight news."""
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        agenda_dir = _agendas(
            tmp_path,
            {"2026-06-12": {"items": [_agenda_item(reported_at=None)]}},
        )
        brief = _build(tmp_path, vault, agenda_dir=agenda_dir)
        assert brief.fired is False
        assert brief.fresh_items == 0

    def test_completion_freshness_strict_and_timezone_normalized(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        stale_dir = _agendas(
            tmp_path,
            {
                "2026-06-11": {
                    "items": [
                        _agenda_item(
                            reported_at=_utc_iso_for_local(LAST_ACTIVITY),
                            result_summary="exactly the boundary",
                        )
                    ]
                }
            },
        )
        assert _build(tmp_path, vault, agenda_dir=stale_dir).fired is False

        # Naive-local stamp (report.py writes local time) one hour after.
        naive_dir = _agendas(
            tmp_path,
            {
                "2026-06-12": {
                    "items": [
                        _agenda_item(
                            reported_at=(
                                LAST_ACTIVITY + timedelta(hours=1)
                            ).isoformat(timespec="seconds"),
                            result_summary="naive local stamp",
                        )
                    ]
                }
            },
        )
        brief = _build(tmp_path, vault, agenda_dir=naive_dir)
        assert brief.fired is True
        assert "naive local stamp" in brief.prompt_block

    def test_malformed_agenda_json_skipped_sibling_survives(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        agenda_dir = _agendas(
            tmp_path,
            {
                "2026-06-12": {
                    "items": [
                        _agenda_item(
                            reported_at=_utc_iso_for_local(FRESH_FLIP_AT),
                            result_summary="the readable agenda",
                        )
                    ]
                }
            },
        )
        (agenda_dir / "AGENDA-2026-06-13.json").write_text(
            "{ not json", encoding="utf-8"
        )
        (agenda_dir / "AGENDA-2026-06-14.json").write_text("[]", encoding="utf-8")
        brief = _build(tmp_path, vault, agenda_dir=agenda_dir)
        assert brief.fired is True
        assert brief.fresh_items == 1
        assert "the readable agenda" in brief.prompt_block

    def test_missing_agenda_dir_contributes_nothing(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        brief = _build(tmp_path, vault, agenda_dir=tmp_path / "no-such-agendas")
        assert brief.fired is False
        assert brief.fresh_items == 0

    def test_agenda_scan_bounded_to_newest_files(self, tmp_path, monkeypatch):
        """Only report.py's poll window can still flip a line; older agendas
        are not re-read on every brief."""
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        days = [f"2026-06-{day:02d}" for day in range(5, 13)]  # 8 files
        agenda_dir = _agendas(
            tmp_path,
            {
                day: {
                    "items": [
                        _agenda_item(
                            reported_at=_utc_iso_for_local(FRESH_FLIP_AT),
                            result_summary=f"flip from {day}",
                        )
                    ]
                }
                for day in days
            },
        )
        brief = _build(tmp_path, vault, agenda_dir=agenda_dir)
        assert brief.fired is True
        assert brief.fresh_items == 7  # newest 7 of 8 scanned
        assert "flip from 2026-06-05" not in brief.prompt_block

    def test_both_sources_combine_newest_first(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        ledger = _cofounder_ledger(
            tmp_path,
            [
                _delegation_row(
                    timestamp=_utc_iso_for_local(FRESH_SENT_AT),
                    detail="the 03:15 delegation",
                )
            ],
        )
        agenda_dir = _agendas(
            tmp_path,
            {
                "2026-06-12": {
                    "items": [
                        _agenda_item(
                            reported_at=_utc_iso_for_local(FRESH_FLIP_AT),
                            result_summary="the 05:00 completion",
                        )
                    ]
                }
            },
        )
        brief = _build(
            tmp_path, vault, delegation_ledger_file=ledger, agenda_dir=agenda_dir
        )
        assert brief.fired is True
        assert brief.fresh_items == 2
        block = brief.prompt_block
        assert block.index("the 05:00 completion") < block.index("the 03:15 delegation")

    def test_cap_applies_to_the_combined_group(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setenv("SESSION_BRIEF_MAX_PER_SECTION", "3")
        vault = _vault(tmp_path)
        ledger = _cofounder_ledger(
            tmp_path,
            [
                _delegation_row(
                    line=i,
                    timestamp=_utc_iso_for_local(
                        FRESH_SENT_AT + timedelta(minutes=i)
                    ),
                    detail=f"delegation number {i}",
                )
                for i in range(1, 6)
            ],
        )
        brief = _build(tmp_path, vault, delegation_ledger_file=ledger)
        assert brief.fired is True
        assert brief.fresh_items == 5  # counted in full
        for i in (5, 4, 3):
            assert f"delegation number {i}" in brief.prompt_block
        for i in (2, 1):  # cap 3 — only the newest three render
            assert f"delegation number {i}" not in brief.prompt_block

    def test_detail_truncated_at_a_word_boundary(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        ledger = _cofounder_ledger(
            tmp_path, [_delegation_row(detail="wordy " * 40)]
        )
        brief = _build(tmp_path, vault, delegation_ledger_file=ledger)
        assert brief.fired is True
        rendered = [
            line
            for line in brief.prompt_block.splitlines()
            if line.startswith("- cofounder:")
        ]
        assert len(rendered) == 1
        detail = rendered[0].rsplit(": ", 1)[1]
        assert len(detail) <= 80
        assert detail.endswith("wordy")

    def test_stale_cofounder_state_stays_silent(self, tmp_path, monkeypatch):
        """The boredom contract holds: yesterday's finished work is not news."""
        _sweep_brief_env(monkeypatch)
        vault = _vault(tmp_path)
        ledger = _cofounder_ledger(
            tmp_path,
            [
                _delegation_row(
                    timestamp=_utc_iso_for_local(datetime(2026, 6, 10, 9, 0)),
                    detail="yesterday's delegation",
                )
            ],
        )
        agenda_dir = _agendas(
            tmp_path,
            {
                "2026-06-10": {
                    "items": [
                        _agenda_item(
                            reported_at=_utc_iso_for_local(
                                datetime(2026, 6, 10, 17, 0)
                            ),
                            result_summary="yesterday's completion",
                        )
                    ]
                }
            },
        )
        brief = _build(
            tmp_path, vault, delegation_ledger_file=ledger, agenda_dir=agenda_dir
        )
        assert brief.fired is False
        assert brief.suppressed_reason == "no_fresh_items"
        assert brief.prompt_block == ""

    def test_group_reserves_one_item_under_char_pressure(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setenv("SESSION_BRIEF_MAX_CHARS", "10")  # deliberately tiny
        vault = _vault(
            tmp_path,
            threads="- [2026-06-10] manual context thread",
            observations="- [2026-06-12] [calendar] busy day: 5 events",
        )
        ledger = _cofounder_ledger(
            tmp_path, [_delegation_row(detail="reserved cofounder line")]
        )
        brief = _build(tmp_path, vault, delegation_ledger_file=ledger)
        assert brief.fired is True
        block = brief.prompt_block
        assert "busy day: 5 events" in block
        assert "reserved cofounder line" in block
        assert "manual context thread" not in block  # context drops first


# =============================================================================
# Category 10 (unit) — brief-owed marker IO + note_router_activity seam
# =============================================================================


class TestBriefOwedMarkerIO:
    def test_round_trip(self, tmp_path):
        write_brief_owed(LAST_ACTIVITY, state_dir=tmp_path, now=NOW)
        assert read_brief_owed(state_dir=tmp_path) == LAST_ACTIVITY
        payload = json.loads(
            (tmp_path / "session-brief-owed.json").read_text(encoding="utf-8")
        )
        assert payload["last_activity"] == LAST_ACTIVITY.isoformat()
        assert payload["detected_at"] == NOW.isoformat()

    def test_missing_marker_reads_none(self, tmp_path):
        assert read_brief_owed(state_dir=tmp_path) is None

    def test_corrupt_marker_reads_none(self, tmp_path):
        (tmp_path / "session-brief-owed.json").write_text(
            "{not json", encoding="utf-8"
        )
        assert read_brief_owed(state_dir=tmp_path) is None
        (tmp_path / "session-brief-owed.json").write_text(
            json.dumps(["a", "list"]), encoding="utf-8"
        )
        assert read_brief_owed(state_dir=tmp_path) is None

    def test_clear_is_best_effort(self, tmp_path):
        clear_brief_owed(state_dir=tmp_path)  # missing — no raise
        write_brief_owed(LAST_ACTIVITY, state_dir=tmp_path)
        clear_brief_owed(state_dir=tmp_path)
        assert read_brief_owed(state_dir=tmp_path) is None


def _bare_engine(store) -> engine_module.ConversationEngine:
    """ConversationEngine without __init__ — note_router_activity only needs
    session_store + module seams, so unit tests skip the heavy identity
    bootstrap entirely (no live reads)."""
    eng = engine_module.ConversationEngine.__new__(
        engine_module.ConversationEngine
    )
    eng.session_store = store
    eng._session_brief_fired_at = None
    return eng


def _message(source: str = "interactive", **kwargs) -> SimpleNamespace:
    return SimpleNamespace(source=source, is_piv=False, **kwargs)


class TestNoteRouterActivity:
    def test_away_gap_writes_marker_with_pre_bump_boundary(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
        old = datetime.now() - timedelta(hours=10)
        monkeypatch.setattr(
            engine_module,
            "resolve_last_operator_activity",
            lambda store, **kwargs: old,
        )
        eng = _bare_engine(store=None)
        eng.note_router_activity(_message())
        assert read_brief_owed(state_dir=tmp_path / "state") == old

    def test_no_gap_no_marker(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
        recent = datetime.now() - timedelta(minutes=5)
        monkeypatch.setattr(
            engine_module,
            "resolve_last_operator_activity",
            lambda store, **kwargs: recent,
        )
        eng = _bare_engine(store=None)
        eng.note_router_activity(_message())
        assert read_brief_owed(state_dir=tmp_path / "state") is None

    def test_non_interactive_source_no_marker(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")

        def _boom(store, **kwargs):
            raise AssertionError("resolver must not run for cron turns")

        monkeypatch.setattr(
            engine_module, "resolve_last_operator_activity", _boom
        )
        eng = _bare_engine(store=None)
        for source in ("cron", "tool", "hook", "cron ", "TOOL", ""):
            eng.note_router_activity(_message(source=source))
        assert read_brief_owed(state_dir=tmp_path / "state") is None

    def test_existing_marker_not_overwritten(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        state_dir = tmp_path / "state"
        monkeypatch.setattr(config, "STATE_DIR", state_dir)
        first_boundary = datetime(2026, 6, 11, 22, 0)
        write_brief_owed(first_boundary, state_dir=state_dir)
        monkeypatch.setattr(
            engine_module,
            "resolve_last_operator_activity",
            lambda store, **kwargs: datetime.now() - timedelta(hours=20),
        )
        eng = _bare_engine(store=None)
        eng.note_router_activity(_message())
        assert read_brief_owed(state_dir=state_dir) == first_boundary

    def test_disabled_no_marker(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setenv("SESSION_BRIEF_ENABLED", "false")
        monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(
            engine_module,
            "resolve_last_operator_activity",
            lambda store, **kwargs: datetime.now() - timedelta(hours=20),
        )
        eng = _bare_engine(store=None)
        eng.note_router_activity(_message())
        assert read_brief_owed(state_dir=tmp_path / "state") is None

    def test_seam_is_fail_open(self, tmp_path, monkeypatch):
        _sweep_brief_env(monkeypatch)
        monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")

        def _boom(store, **kwargs):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(
            engine_module, "resolve_last_operator_activity", _boom
        )
        eng = _bare_engine(store=None)
        eng.note_router_activity(_message())  # must not raise


# =============================================================================
# Category 11 — builder partial-failure seams
# =============================================================================


class TestPartialFailureSeams:
    def test_ledger_path_is_directory_other_sections_render(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path, observations="- [2026-06-12] [calendar] busy day: 5 events"
        )
        _write_episode(
            vault,
            "2026-06-12-telegram-aaaa1111-053000.md",
            summary="episode survives ledger failure",
        )
        bad_ledger = tmp_path / "ledger-as-dir.jsonl"
        bad_ledger.mkdir()
        brief = build_session_opening_brief(
            vault,
            last_activity=LAST_ACTIVITY,
            now=NOW,
            ledger_file=bad_ledger,
            delegation_ledger_file=_cofounder_ledger(tmp_path),
            agenda_dir=_agendas(tmp_path),
        )
        assert brief.fired is True
        assert "busy day: 5 events" in brief.prompt_block
        assert "episode survives ledger failure" in brief.prompt_block
        assert "## Self updates" not in brief.prompt_block

    def test_episodes_dir_occupied_by_file_other_sections_render(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = _vault(
            tmp_path, observations="- [2026-06-12] [calendar] busy day: 5 events"
        )
        (vault / "episodes").write_text("not a directory", encoding="utf-8")
        brief = _build(tmp_path, vault)
        assert brief.fired is True
        assert "busy day: 5 events" in brief.prompt_block
        assert "- session (" not in brief.prompt_block

    def test_working_md_missing_episodes_alone_fire(
        self, tmp_path, monkeypatch
    ):
        _sweep_brief_env(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_episode(
            vault,
            "2026-06-12-telegram-aaaa1111-053000.md",
            summary="episode without working memory",
        )
        brief = _build(tmp_path, vault)
        assert brief.fired is True
        assert brief.fresh_items == 1
        assert "episode without working memory" in brief.prompt_block


# =============================================================================
# Category 13 — cross-lane survival (read-only consumer proof)
# =============================================================================


class TestCrossLaneSurvival:
    def test_brief_lands_in_user_task_not_system_context(self):
        from runtime.base import RuntimeRequest
        from runtime.prompt_builder import render_cli_prompt

        brief_block = (
            "# Session Opening Brief (deliver first)\n\nbrief body here"
        )
        request = RuntimeRequest(
            prompt="good morning, how are we looking?\n\n" + brief_block,
            cwd=Path("."),
            task_name="chat_turn",
            system_prompt={"append": "identity context block"},
        )
        rendered = render_cli_prompt(request)
        assert "User task:" in rendered
        before_task, after_task = rendered.split("User task:", 1)
        assert "# Session Opening Brief" in after_task
        assert "# Session Opening Brief" not in before_task
        assert "System context:" in before_task
