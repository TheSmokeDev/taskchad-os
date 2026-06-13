"""Tests for heartbeat blocker escalation (Living Mind Act 1).

PRP: PRPs/active/PRP-living-mind-act1-heartbeat-blocker-escalation.md

Test design split by code path (R1 M2 — no gas-station e2e):
  1. Pure counter/promotion helpers — fixed tz-aware datetimes (R1 M3).
  2. Gather-path — monkeypatched integration modules raise so the REAL
     except branches in gather_heartbeat_context() execute.
  3. run_heartbeat() ordering — promotion + pre-runtime save precede the
     runtime call, including when the runtime raises (R1 B2).
  4. State migration/normalization (R2 NM1).
  5. Atomic-save regression for shared.save_state (R2 NM2 — hosted here
     because no tests/test_shared*.py exists).
  6. Promotion allowlist gating (R2 NM3).

No test touches the live heartbeat-state.json or live WORKING.md — all
state files and memory dirs are tmp_path-scoped.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import config  # noqa: E402
import heartbeat  # noqa: E402
import living_memory  # noqa: E402
import shared  # noqa: E402
from runtime import langfuse_setup  # noqa: E402
from runtime.base import RUNTIME_LANE_GENERIC, RuntimeResult  # noqa: E402

# Fixed tz-aware clock for helper tests (R1 M3) — never the wall clock.
TZ = timezone(timedelta(hours=-5))

GOOGLE_SIG = "google:oauth_invalid_grant"
GOOGLE_SUMMARY = "Google OAuth refresh broken (invalid_grant) — Gmail/Calendar blind"
GOOGLE_FIX = "uv run python setup_auth.py"


def _dt(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def _google_entry(days: list[str], last_promoted: str | None = None) -> dict:
    return {
        "first_seen": f"{days[0]}T08:00:00-05:00",
        "last_seen": f"{days[-1]}T08:00:00-05:00",
        "distinct_days": list(days),
        "summary": GOOGLE_SUMMARY,
        "fix_hint": GOOGLE_FIX,
        "last_promoted": last_promoted,
    }


# =============================================================================
# 1. classify_blocker — pure classification
# =============================================================================


class TestClassifyBlocker:
    def test_invalid_grant_maps_to_google_oauth_signature(self):
        obs = heartbeat.classify_blocker(
            "gmail",
            "Google token refresh failed: ('invalid_grant: Token has been "
            "expired or revoked.', {'error': 'invalid_grant'})",
        )
        assert obs.signature == GOOGLE_SIG
        assert obs.summary == GOOGLE_SUMMARY
        assert obs.fix_hint == GOOGLE_FIX

    def test_refresh_error_text_maps_to_same_signature(self):
        obs = heartbeat.classify_blocker(
            "calendar", "RefreshError raised while refreshing credentials"
        )
        assert obs.signature == GOOGLE_SIG
        assert obs.fix_hint == GOOGLE_FIX

    def test_unknown_error_maps_to_generic_class_with_no_fix(self):
        obs = heartbeat.classify_blocker("asana", "connection reset by peer")
        assert obs.signature == "asana:error"
        assert obs.summary == "connection reset by peer"
        assert obs.fix_hint is None

    def test_generic_summary_strips_newlines_collapses_whitespace_caps_120(self):
        noisy = "line one\n\n   line\ttwo   " + ("x" * 300)
        obs = heartbeat.classify_blocker("slack", noisy)
        assert "\n" not in obs.summary
        assert "  " not in obs.summary
        assert obs.summary.startswith("line one line two")
        assert len(obs.summary) == 120

    def test_signatures_are_stable_across_volatile_error_text(self):
        a = heartbeat.classify_blocker(
            "gmail", "invalid_grant at 2026-06-09T13:06:13 request-id=abc123"
        )
        b = heartbeat.classify_blocker(
            "calendar", "invalid_grant at 2026-06-11T07:00:00 request-id=zzz999"
        )
        # Same failure class from any Google-backed integration → one signature,
        # no timestamps / request IDs leaking into it.
        assert a.signature == b.signature == GOOGLE_SIG
        # Generic class signature carries only the integration name.
        g1 = heartbeat.classify_blocker("finance", "timeout after 30s (req 111)")
        g2 = heartbeat.classify_blocker("finance", "timeout after 99s (req 222)")
        assert g1.signature == g2.signature == "finance:error"


# =============================================================================
# 1b. get_heartbeat_blocker_settings — Rule 1 call-time resolution
# =============================================================================


class TestBlockerSettings:
    def test_defaults(self, monkeypatch):
        for var in (
            "HEARTBEAT_BLOCKER_PROMOTE_DAYS",
            "HEARTBEAT_BLOCKER_WINDOW_DAYS",
            "HEARTBEAT_BLOCKER_REPROMOTE_DAYS",
            "HEARTBEAT_BLOCKER_MAX_ACTIVE",
            "HEARTBEAT_BLOCKER_PROMOTE_ALLOWLIST",
        ):
            monkeypatch.delenv(var, raising=False)
        settings = config.get_heartbeat_blocker_settings()
        assert settings.promote_days == 3
        assert settings.window_days == 7
        assert settings.repromote_days == 3
        assert settings.max_active == 3
        # Act 2 widening: auth_failed classes join the default allowlist
        # (token_missing deliberately does NOT — config state, not regression).
        assert settings.promote_allowlist == frozenset(
            {GOOGLE_SIG, "asana:auth_failed", "slack:auth_failed"}
        )

    def test_all_five_env_overrides_resolve_at_call_time_without_reload(
        self, monkeypatch
    ):
        """monkeypatch.setenv passes WITHOUT module reload (Rule 1 — R1 B3)."""
        monkeypatch.setenv("HEARTBEAT_BLOCKER_PROMOTE_DAYS", "5")
        monkeypatch.setenv("HEARTBEAT_BLOCKER_WINDOW_DAYS", "14")
        monkeypatch.setenv("HEARTBEAT_BLOCKER_REPROMOTE_DAYS", "2")
        monkeypatch.setenv("HEARTBEAT_BLOCKER_MAX_ACTIVE", "9")
        monkeypatch.setenv(
            "HEARTBEAT_BLOCKER_PROMOTE_ALLOWLIST",
            "google:oauth_invalid_grant, bank_sync:error",
        )
        settings = config.get_heartbeat_blocker_settings()
        assert settings.promote_days == 5
        assert settings.window_days == 14
        assert settings.repromote_days == 2
        assert settings.max_active == 9
        assert settings.promote_allowlist == frozenset(
            {GOOGLE_SIG, "bank_sync:error"}
        )

    def test_explicit_args_win_over_env(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_BLOCKER_PROMOTE_DAYS", "5")
        settings = config.get_heartbeat_blocker_settings(
            promote_days=1, promote_allowlist={"custom:sig"}
        )
        assert settings.promote_days == 1
        assert settings.promote_allowlist == frozenset({"custom:sig"})


# =============================================================================
# 4. normalize_blocker_state — migration / normalization (R2 NM1)
# =============================================================================


class TestNormalizeBlockerState:
    def test_old_live_shape_initializes_empty_observations(self, tmp_path):
        """Pre-Act-1 live shape (alert_history + last_run only) loads fail-open."""
        state_file = tmp_path / "heartbeat-state.json"
        state_file.write_text(
            json.dumps(
                {
                    "alert_history": [
                        {"text": "alert", "alerted_at": "2026-06-11T13:06:13-05:00"}
                    ],
                    "last_run": "2026-06-11T21:04:23-05:00",
                }
            ),
            encoding="utf-8",
        )
        state = shared.load_state(state_file)
        before_other = json.dumps(
            {k: v for k, v in state.items()}, sort_keys=True, default=str
        )
        heartbeat.normalize_blocker_state(state)
        assert state["blocker_observations"] == {}
        # Pre-existing keys preserved exactly
        after_other = json.dumps(
            {k: v for k, v in state.items() if k != "blocker_observations"},
            sort_keys=True,
            default=str,
        )
        assert after_other == before_other

    def test_empty_state_initializes_empty_observations(self):
        state: dict = {}
        heartbeat.normalize_blocker_state(state)
        assert state == {"blocker_observations": {}}

    def test_non_dict_observations_reset_fail_open(self, capsys):
        state = {"blocker_observations": ["not", "a", "dict"]}
        heartbeat.normalize_blocker_state(state)
        assert state["blocker_observations"] == {}
        assert "malformed" in capsys.readouterr().out

    def test_malformed_entries_dropped_or_coerced(self, capsys):
        state = {
            "blocker_observations": {
                "sig:not_a_dict": "garbage",
                "sig:bad_fields": {
                    "first_seen": 12345,
                    "last_seen": None,
                    "distinct_days": "not-a-list",
                    "summary": None,
                    "fix_hint": 42,
                    "last_promoted": 7,
                },
                "sig:bad_days": {
                    "first_seen": "2026-06-01T08:00:00-05:00",
                    "last_seen": "2026-06-01T08:00:00-05:00",
                    "distinct_days": ["2026-06-01", "2026-06-01", "bad-date", 42],
                    "summary": "ok",
                    "fix_hint": None,
                    "last_promoted": None,
                },
            }
        }
        heartbeat.normalize_blocker_state(state)
        obs = state["blocker_observations"]
        assert "sig:not_a_dict" not in obs
        assert obs["sig:bad_fields"]["first_seen"] is None
        assert obs["sig:bad_fields"]["last_seen"] is None
        assert obs["sig:bad_fields"]["distinct_days"] == []
        assert obs["sig:bad_fields"]["summary"] == ""
        assert obs["sig:bad_fields"]["fix_hint"] is None
        assert obs["sig:bad_fields"]["last_promoted"] is None
        # duplicate + invalid days normalized (set semantics, sorted)
        assert obs["sig:bad_days"]["distinct_days"] == ["2026-06-01"]
        assert "dropped" in capsys.readouterr().out

    def test_normalize_is_idempotent(self):
        state = {
            "alert_history": [],
            "blocker_observations": {
                GOOGLE_SIG: _google_entry(["2026-06-10", "2026-06-09", "2026-06-10"])
            },
        }
        heartbeat.normalize_blocker_state(state)
        snap1 = json.dumps(state, sort_keys=True, default=str)
        heartbeat.normalize_blocker_state(state)
        snap2 = json.dumps(state, sort_keys=True, default=str)
        assert snap1 == snap2
        # duplicate day collapsed + sorted
        assert state["blocker_observations"][GOOGLE_SIG]["distinct_days"] == [
            "2026-06-09",
            "2026-06-10",
        ]

    def test_unknown_top_level_keys_round_trip_unchanged(self):
        custom = {"nested": [1, 2, {"deep": "value"}], "flag": True}
        state = {"my_future_key": custom, "last_run": "2026-06-11T21:00:00-05:00"}
        before = json.dumps(state["my_future_key"], sort_keys=True)
        heartbeat.normalize_blocker_state(state)
        after = json.dumps(state["my_future_key"], sort_keys=True)
        assert before == after
        assert state["last_run"] == "2026-06-11T21:00:00-05:00"


# =============================================================================
# 1c. Counter / window helpers — fixed tz-aware clocks (R1 M3)
# =============================================================================


class TestRecordAndWindow:
    def test_same_day_observations_count_once(self):
        state: dict = {"blocker_observations": {}}
        for hour in (8, 12, 23):
            heartbeat.record_blocker_observations(
                state,
                [("gmail", "invalid_grant: expired")],
                now=_dt(2026, 6, 10, hour),
            )
        entry = state["blocker_observations"][GOOGLE_SIG]
        assert entry["distinct_days"] == ["2026-06-10"]
        # 3 same-day observations do NOT promote (distinct-day proof)
        report = heartbeat.promote_eligible_blockers(
            state,
            Path("unused-not-reached"),
            promote_days=3,
            window_days=7,
            repromote_days=3,
            max_active=3,
            promote_allowlist={GOOGLE_SIG},
            now=_dt(2026, 6, 10, 23, 30),
        )
        assert report["promoted"] == []

    def test_cross_midnight_observations_count_two_days(self):
        state: dict = {"blocker_observations": {}}
        heartbeat.record_blocker_observations(
            state, [("gmail", "invalid_grant")], now=_dt(2026, 6, 10, 23, 59)
        )
        heartbeat.record_blocker_observations(
            state, [("gmail", "invalid_grant")], now=_dt(2026, 6, 11, 0, 1)
        )
        entry = state["blocker_observations"][GOOGLE_SIG]
        assert entry["distinct_days"] == ["2026-06-10", "2026-06-11"]

    def test_days_outside_window_do_not_count(self):
        """Acceptance-normative: days 1, 2, 9 with a 7-day window = 2 effective
        at day 9 → no promotion."""
        entry = _google_entry(["2026-06-01", "2026-06-02", "2026-06-09"])
        effective = heartbeat.effective_blocker_days(
            entry, window_days=7, now=_dt(2026, 6, 9)
        )
        assert effective == ["2026-06-02", "2026-06-09"]
        state = {"blocker_observations": {GOOGLE_SIG: entry}}
        report = heartbeat.promote_eligible_blockers(
            state,
            Path("unused-not-reached"),
            promote_days=3,
            window_days=7,
            repromote_days=3,
            max_active=3,
            promote_allowlist={GOOGLE_SIG},
            now=_dt(2026, 6, 9),
        )
        assert report["promoted"] == []

    def test_window_exact_boundaries(self):
        entry = _google_entry(["2026-06-02"])
        # delta == window_days → counts (inclusive boundary)
        assert heartbeat.effective_blocker_days(
            entry, window_days=7, now=_dt(2026, 6, 9)
        ) == ["2026-06-02"]
        # delta == window_days + 1 → out
        assert (
            heartbeat.effective_blocker_days(
                entry, window_days=7, now=_dt(2026, 6, 10)
            )
            == []
        )
        # future-dated day never counts
        future = _google_entry(["2026-06-20"])
        assert (
            heartbeat.effective_blocker_days(
                future, window_days=7, now=_dt(2026, 6, 9)
            )
            == []
        )

    def test_every_candidate_is_recorded_full_visibility(self):
        """R2 NM3: counters record EVERY candidate, not just allowlisted ones."""
        state: dict = {"blocker_observations": {}}
        heartbeat.record_blocker_observations(
            state,
            [
                ("gmail", "invalid_grant"),
                ("bank_sync", "timeout after 30s"),
                ("finance", "supabase 500"),
            ],
            now=_dt(2026, 6, 10),
        )
        obs = state["blocker_observations"]
        assert set(obs.keys()) == {GOOGLE_SIG, "bank_sync:error", "finance:error"}
        assert obs["bank_sync:error"]["summary"] == "timeout after 30s"
        assert obs["bank_sync:error"]["fix_hint"] is None


# =============================================================================
# Prune behavior
# =============================================================================


class TestPruneBlockerObservations:
    def test_signature_aged_out_of_window_is_dropped(self, capsys):
        state = {
            "blocker_observations": {
                GOOGLE_SIG: _google_entry(["2026-06-01"]),
                "bank_sync:error": {
                    "first_seen": "2026-06-09T08:00:00-05:00",
                    "last_seen": "2026-06-09T08:00:00-05:00",
                    "distinct_days": ["2026-06-09"],
                    "summary": "timeout",
                    "fix_hint": None,
                    "last_promoted": None,
                },
            }
        }
        pruned = heartbeat.prune_blocker_observations(
            state, window_days=7, now=_dt(2026, 6, 9)
        )
        # google last_seen 2026-06-01 → delta 8 > 7 → dropped
        assert pruned == [GOOGLE_SIG]
        assert GOOGLE_SIG not in state["blocker_observations"]
        assert "bank_sync:error" in state["blocker_observations"]
        assert "pruned" in capsys.readouterr().out

    def test_out_of_window_days_trimmed_from_survivors(self):
        entry = _google_entry(["2026-06-01", "2026-06-08", "2026-06-09"])
        entry["last_seen"] = "2026-06-09T08:00:00-05:00"
        state = {"blocker_observations": {GOOGLE_SIG: entry}}
        heartbeat.prune_blocker_observations(state, window_days=7, now=_dt(2026, 6, 9))
        assert state["blocker_observations"][GOOGLE_SIG]["distinct_days"] == [
            "2026-06-08",
            "2026-06-09",
        ]

    def test_fixed_blocker_prunes_and_never_repromotes(self, tmp_path):
        """A fixed blocker stops being observed → ages out → no re-promotion."""
        memory_dir = tmp_path / "memory"
        state = {
            "blocker_observations": {
                GOOGLE_SIG: _google_entry(
                    ["2026-06-01", "2026-06-02", "2026-06-03"],
                    last_promoted="2026-06-03T12:00:00-05:00",
                )
            }
        }
        now = _dt(2026, 6, 15)
        heartbeat.prune_blocker_observations(state, window_days=7, now=now)
        assert state["blocker_observations"] == {}
        report = heartbeat.promote_eligible_blockers(
            state,
            memory_dir,
            promote_days=3,
            window_days=7,
            repromote_days=3,
            max_active=3,
            promote_allowlist={GOOGLE_SIG},
            now=now,
        )
        assert report["promoted"] == []
        assert not (memory_dir / "WORKING.md").exists()


# =============================================================================
# Promotion — threshold, bullet shape, repromote, guardrail, fail-open (R1 M4)
# =============================================================================


def _promote(state, memory_dir, now, **overrides):
    kwargs = dict(
        promote_days=3,
        window_days=7,
        repromote_days=3,
        max_active=3,
        promote_allowlist={GOOGLE_SIG},
        now=now,
    )
    kwargs.update(overrides)
    return heartbeat.promote_eligible_blockers(state, memory_dir, **kwargs)


class TestPromotion:
    def test_three_distinct_days_promote_with_tag_and_fix(self, tmp_path):
        memory_dir = tmp_path / "memory"
        state = {
            "blocker_observations": {
                GOOGLE_SIG: _google_entry(["2026-06-08", "2026-06-09", "2026-06-10"])
            }
        }
        report = _promote(state, memory_dir, _dt(2026, 6, 10))
        assert report["promoted"] == [GOOGLE_SIG]
        content = (memory_dir / "WORKING.md").read_text(encoding="utf-8")
        # Promoted bullet shape: [heartbeat] tag + summary + fix + status
        assert (
            "[heartbeat] Google OAuth refresh broken (invalid_grant) "
            "— Gmail/Calendar blind — fix: uv run python setup_auth.py — open"
        ) in content
        assert state["blocker_observations"][GOOGLE_SIG]["last_promoted"] is not None

    def test_no_fix_hint_means_no_dangling_fix_text(self, tmp_path):
        memory_dir = tmp_path / "memory"
        entry = {
            "first_seen": "2026-06-08T08:00:00-05:00",
            "last_seen": "2026-06-10T08:00:00-05:00",
            "distinct_days": ["2026-06-08", "2026-06-09", "2026-06-10"],
            "summary": "bank sync timing out every morning run",
            "fix_hint": None,
            "last_promoted": None,
        }
        state = {"blocker_observations": {"bank_sync:error": entry}}
        report = _promote(
            state,
            memory_dir,
            _dt(2026, 6, 10),
            promote_allowlist={"bank_sync:error"},
        )
        assert report["promoted"] == ["bank_sync:error"]
        content = (memory_dir / "WORKING.md").read_text(encoding="utf-8")
        assert "[heartbeat] bank sync timing out every morning run — open" in content
        assert "— fix:" not in content

    def test_no_duplicate_promotion_within_repromote_days(self, tmp_path):
        memory_dir = tmp_path / "memory"
        state = {
            "blocker_observations": {
                GOOGLE_SIG: _google_entry(
                    ["2026-06-08", "2026-06-09", "2026-06-10"],
                    last_promoted="2026-06-09T12:00:00-05:00",
                )
            }
        }
        # days_since(last_promoted) = 1 < 3 → blocked, nothing written
        report = _promote(state, memory_dir, _dt(2026, 6, 10))
        assert report["promoted"] == []
        assert report["deduped"] == []
        assert not (memory_dir / "WORKING.md").exists()

    def test_repromote_boundary_eligible_at_exact_days_with_dedup_skip(
        self, tmp_path
    ):
        """At days_since == repromote_days the gate opens; if an equivalent
        thread still sits in Open Threads, the dedup-skip path returns 0 and
        last_promoted is still refreshed (deterministic)."""
        memory_dir = tmp_path / "memory"
        subject = f"[heartbeat] {GOOGLE_SUMMARY} — fix: {GOOGLE_FIX}"
        # Pre-existing equivalent thread (real today's date → inside dedup window)
        assert living_memory.append_open_thread(memory_dir, subject, "open") == 1

        now = _dt(2026, 6, 10)
        state = {
            "blocker_observations": {
                GOOGLE_SIG: _google_entry(
                    ["2026-06-08", "2026-06-09", "2026-06-10"],
                    last_promoted="2026-06-07T12:00:00-05:00",  # exactly 3 days
                )
            }
        }
        report = _promote(state, memory_dir, now)
        assert report["promoted"] == []
        assert report["deduped"] == [GOOGLE_SIG]
        # last_promoted refreshed on dedup skip
        assert (
            state["blocker_observations"][GOOGLE_SIG]["last_promoted"]
            == now.isoformat()
        )
        content = (memory_dir / "WORKING.md").read_text(encoding="utf-8")
        assert content.count("[heartbeat]") == 1  # no duplicate bullet

    def test_guardrail_skips_at_cap_and_promotes_when_slot_frees(
        self, tmp_path, capsys
    ):
        memory_dir = tmp_path / "memory"
        for i in range(3):
            living_memory.append_open_thread(
                memory_dir, f"[heartbeat] existing blocker thread {i}", "open"
            )
        state = {
            "blocker_observations": {
                GOOGLE_SIG: _google_entry(["2026-06-08", "2026-06-09", "2026-06-10"])
            }
        }
        report = _promote(state, memory_dir, _dt(2026, 6, 10))
        assert report["skipped_guardrail"] == [GOOGLE_SIG]
        assert report["promoted"] == []
        # Counter state persists for later promotion — last_promoted untouched
        assert state["blocker_observations"][GOOGLE_SIG]["last_promoted"] is None
        assert "guardrail" in capsys.readouterr().out

        # Free a slot → promotes
        ok, _detail = living_memory.resolve_open_thread(memory_dir, 1)
        assert ok
        report2 = _promote(state, memory_dir, _dt(2026, 6, 10))
        assert report2["promoted"] == [GOOGLE_SIG]
        content = (memory_dir / "WORKING.md").read_text(encoding="utf-8")
        assert "Google OAuth refresh broken" in content

    def test_append_failure_is_fail_open_and_retries_next_run(
        self, tmp_path, monkeypatch, capsys
    ):
        memory_dir = tmp_path / "memory"

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(living_memory, "append_open_thread", _boom)
        state = {
            "blocker_observations": {
                GOOGLE_SIG: _google_entry(["2026-06-08", "2026-06-09", "2026-06-10"])
            }
        }
        report = _promote(state, memory_dir, _dt(2026, 6, 10))
        assert report["promoted"] == []
        # last_promoted NOT set → next run retries
        assert state["blocker_observations"][GOOGLE_SIG]["last_promoted"] is None
        assert "non-fatal" in capsys.readouterr().out

    def test_resolve_heartbeat_item_mixed_with_manual_threads(self, tmp_path):
        """R1 M4: resolve_open_thread() resolves a [heartbeat] item without
        disturbing manual threads; re-promotion gates on last_promoted, not
        active-thread presence."""
        memory_dir = tmp_path / "memory"
        living_memory.append_open_thread(
            memory_dir, "manual thread about the dashboard work", "open"
        )
        now = _dt(2026, 6, 10)
        state = {
            "blocker_observations": {
                GOOGLE_SIG: _google_entry(["2026-06-08", "2026-06-09", "2026-06-10"])
            }
        }
        report = _promote(state, memory_dir, now)
        assert report["promoted"] == [GOOGLE_SIG]

        data = living_memory.read_working_memory(memory_dir)
        assert len(data.open_threads) == 2
        hb_index = next(
            i + 1
            for i, b in enumerate(data.open_threads)
            if "[heartbeat]" in b
        )
        ok, detail = living_memory.resolve_open_thread(memory_dir, hb_index)
        assert ok
        assert "[heartbeat]" in detail

        data = living_memory.read_working_memory(memory_dir)
        # Manual thread untouched; [heartbeat] item archived as resolved
        assert len(data.open_threads) == 1
        assert "manual thread about the dashboard work" in data.open_threads[0]
        assert any(
            "[resolved" in b and "[heartbeat]" in b for b in data.archived
        )

        # Still-firing blocker, zero active [heartbeat] threads, but
        # last_promoted is fresh → NO re-promotion inside repromote_days.
        report2 = _promote(state, memory_dir, _dt(2026, 6, 11))
        assert report2["promoted"] == []
        assert report2["deduped"] == []

        # After repromote_days (>=) it re-promotes (days still in window).
        report3 = _promote(state, memory_dir, _dt(2026, 6, 13))
        assert report3["promoted"] == [GOOGLE_SIG]
        data = living_memory.read_working_memory(memory_dir)
        assert any("[heartbeat]" in b for b in data.open_threads)


# =============================================================================
# 6. Allowlist gating (R2 NM3)
# =============================================================================


class TestAllowlistGate:
    def test_recurring_transient_counted_but_not_promoted(self, tmp_path):
        """A generic {integration}:error at 3 distinct days is fully recorded
        in state but never creates a WORKING.md thread (default allowlist)."""
        memory_dir = tmp_path / "memory"
        state: dict = {"blocker_observations": {}}
        for day in (8, 9, 10):
            heartbeat.record_blocker_observations(
                state, [("bank_sync", "timeout after 30s")], now=_dt(2026, 6, day)
            )
        entry = state["blocker_observations"]["bank_sync:error"]
        assert len(entry["distinct_days"]) == 3  # observed + counted

        report = heartbeat.promote_eligible_blockers(
            state,
            memory_dir,
            promote_days=3,
            window_days=7,
            repromote_days=3,
            max_active=3,
            promote_allowlist=None,  # default allowlist via env/default
            now=_dt(2026, 6, 10),
        )
        assert report["promoted"] == []
        assert not (memory_dir / "WORKING.md").exists()
        # Detection breadth unchanged — entry stays fully recorded
        assert entry["summary"] == "timeout after 30s"

    def test_allowlisted_signature_with_identical_days_promotes(self, tmp_path):
        memory_dir = tmp_path / "memory"
        state: dict = {"blocker_observations": {}}
        for day in (8, 9, 10):
            heartbeat.record_blocker_observations(
                state,
                [("bank_sync", "timeout"), ("gmail", "invalid_grant")],
                now=_dt(2026, 6, day),
            )
        report = _promote(state, memory_dir, _dt(2026, 6, 10))
        assert report["promoted"] == [GOOGLE_SIG]
        content = (memory_dir / "WORKING.md").read_text(encoding="utf-8")
        assert "bank sync" not in content.lower()

    def test_allowlist_env_override_resolves_call_time(self, tmp_path, monkeypatch):
        memory_dir = tmp_path / "memory"
        state = {
            "blocker_observations": {
                "bank_sync:error": {
                    "first_seen": "2026-06-08T08:00:00-05:00",
                    "last_seen": "2026-06-10T08:00:00-05:00",
                    "distinct_days": ["2026-06-08", "2026-06-09", "2026-06-10"],
                    "summary": "timeout after 30s",
                    "fix_hint": None,
                    "last_promoted": None,
                }
            }
        }
        monkeypatch.setenv("HEARTBEAT_BLOCKER_PROMOTE_ALLOWLIST", "bank_sync:error")
        report = heartbeat.promote_eligible_blockers(
            state,
            memory_dir,
            promote_days=3,
            window_days=7,
            repromote_days=3,
            max_active=3,
            promote_allowlist=None,  # env-resolved in the body, no reload
            now=_dt(2026, 6, 10),
        )
        assert report["promoted"] == ["bank_sync:error"]


# =============================================================================
# 2. Gather-path — REAL except branches via monkeypatched integration modules
# =============================================================================


def _benign(value):
    def _fn(*_args, **_kwargs):
        return value

    return _fn


def _raising(exc):
    def _fn(*_args, **_kwargs):
        raise exc

    return _fn


def _install_fake_integrations(monkeypatch, **raises):
    """Install fake integration modules for gather_heartbeat_context().

    Pass e.g. ``gmail=RuntimeError(...)`` to make that integration's first
    call raise — exercising the REAL except branch in heartbeat.py. All other
    integrations return benign empties (no candidates).
    """

    def mod(**attrs):
        return types.SimpleNamespace(**attrs)

    gmail_exc = raises.get("gmail")
    monkeypatch.setitem(
        sys.modules,
        "integrations.gmail",
        mod(
            get_unread_count=_raising(gmail_exc) if gmail_exc else _benign(0),
            check_for_urgent_emails=_benign([]),
            list_emails=_benign([]),
            format_emails_for_context=_benign("(none)"),
        ),
    )
    cal_exc = raises.get("calendar")
    monkeypatch.setitem(
        sys.modules,
        "integrations.calendar_api",
        mod(
            get_today_events=_raising(cal_exc) if cal_exc else _benign([]),
            check_for_upcoming_meetings=_benign([]),
            format_events_for_context=_benign("(none)"),
        ),
    )
    asana_exc = raises.get("asana")
    monkeypatch.setitem(
        sys.modules,
        "integrations.asana_api",
        mod(
            get_overdue_tasks=_raising(asana_exc) if asana_exc else _benign([]),
            get_due_soon_tasks=_benign([]),
            format_tasks_for_context=_benign("(none)"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "integrations.slack_api",
        mod(
            check_for_important_messages=_benign([]),
            format_messages_for_context=_benign("(none)"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "integrations.bank_sync",
        mod(
            sync_bank_data=_benign(
                {"transactions_synced": 0, "balances_updated": 0, "errors": []}
            ),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "integrations.finance_api",
        mod(
            get_upcoming_bills=_benign([]),
            get_expiring_loans=_benign([]),
            check_low_balances=_benign([]),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "integrations.finance_analytics",
        mod(get_category_budget_status=_benign([])),
    )
    monkeypatch.setitem(
        sys.modules,
        "integrations.outlook",
        mod(list_emails=_benign([]), get_email_body=_benign("")),
    )


class TestGatherPath:
    @pytest.mark.asyncio
    async def test_gmail_invalid_grant_surfaces_through_real_except_branch(
        self, monkeypatch
    ):
        """R1 M2/B5: a raising integration travels the REAL except branch and
        lands in the third return element."""
        _install_fake_integrations(
            monkeypatch,
            gmail=RuntimeError(
                "Google token refresh failed: ('invalid_grant: Token has been "
                "expired or revoked.', {'error': 'invalid_grant'})\n"
                "Run 'uv run python setup_auth.py' to re-authenticate."
            ),
        )
        context, _source_ids, candidates, facts = (
            await heartbeat.gather_heartbeat_context()
        )
        gmail_candidates = [c for c in candidates if c[0] == "gmail"]
        # Error → facts key ABSENT (never both candidate and facts)
        assert "email" not in facts
        assert len(gmail_candidates) == 1
        assert "invalid_grant" in gmail_candidates[0][1]
        # Existing error-section behavior unchanged
        assert "**Error fetching email:**" in context
        # And the candidate classifies into the Google OAuth class
        obs = heartbeat.classify_blocker(*gmail_candidates[0])
        assert obs.signature == GOOGLE_SIG

    @pytest.mark.asyncio
    async def test_calendar_branch_also_surfaces_google_class(self, monkeypatch):
        _install_fake_integrations(
            monkeypatch,
            gmail=RuntimeError("invalid_grant: expired"),
            calendar=RuntimeError("invalid_grant: expired"),
        )
        _context, _sids, candidates, _facts = await heartbeat.gather_heartbeat_context()
        names = [c[0] for c in candidates]
        assert "gmail" in names
        assert "calendar" in names
        signatures = {heartbeat.classify_blocker(*c).signature for c in candidates}
        assert signatures == {GOOGLE_SIG}

    @pytest.mark.asyncio
    async def test_generic_error_candidate_from_real_branch(self, monkeypatch):
        _install_fake_integrations(monkeypatch, asana=ValueError("boom badly"))
        _context, _sids, candidates, facts = await heartbeat.gather_heartbeat_context()
        assert ("asana", "boom badly") in candidates
        assert "tasks" not in facts  # raising block leaves its facts key absent
        obs = heartbeat.classify_blocker("asana", "boom badly")
        assert obs.signature == "asana:error"

    @pytest.mark.asyncio
    async def test_quiet_gather_returns_empty_candidates(self, monkeypatch):
        """Quiet (all-success, zero-data) gather: no candidates, and the
        4th element carries a facts key for every SUCCEEDING block (facts
        presence == sense healthy — the Act 2 contract), all zero-counted."""
        _install_fake_integrations(monkeypatch)
        result = await heartbeat.gather_heartbeat_context()
        assert len(result) == 4
        assert result[2] == []
        assert result[3] == {
            "email": {"unread_count": 0, "urgent_count": 0},
            "calendar": {"today_count": 0, "upcoming_count": 0},
            "tasks": {"overdue_count": 0, "due_soon_count": 0},
            "community": {"slack_important_count": 0},
            "finance": {
                "low_balance_accounts": [],
                "bills_due_count": 0,
                "expiring_loans": [],
                "overspend": [],
            },
        }


# =============================================================================
# 3. run_heartbeat ordering — promotion + pre-runtime save precede runtime
# =============================================================================


def _install_run_heartbeat_harness(
    monkeypatch,
    tmp_path,
    *,
    candidates,
    seeded_state=None,
    runtime_error=None,
):
    """Isolate run_heartbeat(): tmp state file + tmp memory dir + fake runtime.

    The fake runtime snapshots the on-disk state file and WORKING.md at call
    time — the ordering proof (R1 B2). load_state/save_state stay REAL and
    operate on the tmp state file.
    """
    state_file = tmp_path / "state" / "heartbeat-state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if seeded_state is not None:
        state_file.write_text(
            json.dumps(seeded_state, indent=2, default=str), encoding="utf-8"
        )
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(heartbeat, "HEARTBEAT_STATE_FILE", state_file)
    monkeypatch.setattr(heartbeat, "MEMORY_DIR", memory_dir)

    async def fake_gather():
        # Four-value gather contract (Living Mind Act 2) — no dual-shape shim.
        return ("## Email\n\nquiet context for ordering test", [], list(candidates), {})

    runtime_calls: list[dict] = []

    async def fake_runtime(request):
        working = memory_dir / "WORKING.md"
        runtime_calls.append(
            {
                "state_json": (
                    json.loads(state_file.read_text(encoding="utf-8"))
                    if state_file.exists()
                    else None
                ),
                "working_md": (
                    working.read_text(encoding="utf-8") if working.exists() else ""
                ),
            }
        )
        if runtime_error is not None:
            raise runtime_error
        return RuntimeResult(
            text="HEARTBEAT_OK",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.4-mini",
        )

    async def fake_recall(**_kwargs):
        return types.SimpleNamespace(formatted_text="")

    monkeypatch.setitem(
        sys.modules,
        "recall_service",
        types.SimpleNamespace(
            recall=fake_recall,
            reindex_changed=lambda _memory_dir: {"files_indexed": 0},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "memory_index",
        types.SimpleNamespace(
            sync_index=lambda: {"files_indexed": 0, "files_skipped": 0}
        ),
    )
    monkeypatch.setattr(heartbeat, "gather_heartbeat_context", fake_gather)
    monkeypatch.setattr(heartbeat, "gather_habits_context", lambda: "- [x] ok")
    monkeypatch.setattr(
        heartbeat, "gather_circle_drafts_context", lambda: ("none", [], [])
    )
    monkeypatch.setattr(heartbeat, "gather_email_drafts_context", lambda: "none")
    monkeypatch.setattr(
        heartbeat,
        "reconcile_active_drafts",
        lambda *_args: "No active drafts to reconcile.",
    )
    monkeypatch.setattr(heartbeat, "expire_old_drafts", lambda: 0)
    monkeypatch.setattr(heartbeat, "gather_active_drafts_context", lambda: "none")
    monkeypatch.setattr(
        heartbeat,
        "_assemble_heartbeat_cognition_section",
        lambda _memory_dir: "## Shared Proactive Brief\n\nnone",
    )
    monkeypatch.setattr(heartbeat, "append_to_daily_log", lambda *_a, **_k: None)
    monkeypatch.setattr(heartbeat, "log_hook_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(heartbeat, "run_with_runtime_lanes", fake_runtime)
    # Keep Langfuse out of the loop; the accessor-gate consultation is proven
    # in test_heartbeat_gate_consults_accessor below.
    monkeypatch.setattr(langfuse_setup, "get_observation_client", lambda: None)

    return state_file, memory_dir, runtime_calls


def _seeded_two_day_state() -> dict:
    today = heartbeat.now_local().date()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    return {
        "alert_history": [],
        "blocker_observations": {
            GOOGLE_SIG: _google_entry([d2, d1]),
        },
    }


class TestRunHeartbeatOrdering:
    @pytest.mark.asyncio
    async def test_run_heartbeat_consumes_third_gather_element(
        self, monkeypatch, tmp_path
    ):
        """R1 M1: the three-value gather contract is consumed — a candidate in
        the third element ends up counted in persisted state."""
        state_file, _memory_dir, _calls = _install_run_heartbeat_harness(
            monkeypatch,
            tmp_path,
            candidates=[("gmail", "invalid_grant: token expired")],
        )
        result = await heartbeat.run_heartbeat(test_mode=True)
        assert result is None
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        entry = saved["blocker_observations"][GOOGLE_SIG]
        today = heartbeat.now_local().date().isoformat()
        assert entry["distinct_days"] == [today]
        assert entry["summary"] == GOOGLE_SUMMARY

    @pytest.mark.asyncio
    async def test_promotion_and_state_save_precede_runtime_call(
        self, monkeypatch, tmp_path
    ):
        state_file, _memory_dir, runtime_calls = _install_run_heartbeat_harness(
            monkeypatch,
            tmp_path,
            candidates=[("gmail", "invalid_grant: token expired")],
            seeded_state=_seeded_two_day_state(),
        )
        await heartbeat.run_heartbeat(test_mode=True)
        assert len(runtime_calls) >= 1
        snapshot = runtime_calls[0]
        # At runtime-call time the state file ALREADY carried the promotion…
        entry = snapshot["state_json"]["blocker_observations"][GOOGLE_SIG]
        assert len(entry["distinct_days"]) == 3
        assert entry["last_promoted"] is not None
        # …and WORKING.md already carried the thread.
        assert "[heartbeat] Google OAuth refresh broken" in snapshot["working_md"]

    @pytest.mark.asyncio
    async def test_runtime_failure_keeps_promotion_and_state(
        self, monkeypatch, tmp_path
    ):
        """R1 B2 discriminator: runtime raises AFTER promotion — the
        WORKING.md thread exists AND persisted state carries last_promoted
        (without the pre-runtime save this state would be lost)."""
        state_file, memory_dir, _calls = _install_run_heartbeat_harness(
            monkeypatch,
            tmp_path,
            candidates=[("gmail", "invalid_grant: token expired")],
            seeded_state=_seeded_two_day_state(),
            runtime_error=RuntimeError("runtime lane down"),
        )
        result = await heartbeat.run_heartbeat(test_mode=True)
        assert result is None  # handled, not raised

        working = (memory_dir / "WORKING.md").read_text(encoding="utf-8")
        assert (
            "[heartbeat] Google OAuth refresh broken (invalid_grant) "
            "— Gmail/Calendar blind — fix: uv run python setup_auth.py — open"
        ) in working

        saved = json.loads(state_file.read_text(encoding="utf-8"))
        entry = saved["blocker_observations"][GOOGLE_SIG]
        assert entry["last_promoted"] is not None
        assert len(entry["distinct_days"]) == 3
        # post-run save never ran (early return) — last_run not updated
        assert "last_run" not in saved

    @pytest.mark.asyncio
    async def test_append_open_thread_failure_never_breaks_heartbeat(
        self, monkeypatch, tmp_path
    ):
        """Acceptance: append_open_thread raising → heartbeat completes."""
        state_file, memory_dir, runtime_calls = _install_run_heartbeat_harness(
            monkeypatch,
            tmp_path,
            candidates=[("gmail", "invalid_grant: token expired")],
            seeded_state=_seeded_two_day_state(),
        )

        def _boom(*_a, **_k):
            raise OSError("vault unwritable")

        monkeypatch.setattr(living_memory, "append_open_thread", _boom)

        result = await heartbeat.run_heartbeat(test_mode=True)
        assert result is None
        assert len(runtime_calls) == 1  # runtime still ran
        # Promotion failed → last_promoted stays unset so next run retries
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert saved["blocker_observations"][GOOGLE_SIG]["last_promoted"] is None

    @pytest.mark.asyncio
    async def test_heartbeat_gate_consults_accessor(self, monkeypatch, tmp_path):
        """R2 NB1: heartbeat's Langfuse propagation gate reaches the accessor
        through module-attribute lookup — patching langfuse_setup affects it."""
        calls: list[int] = []

        _install_run_heartbeat_harness(monkeypatch, tmp_path, candidates=[])

        def _counting_accessor():
            calls.append(1)
            return None

        monkeypatch.setattr(
            langfuse_setup, "get_observation_client", _counting_accessor
        )
        await heartbeat.run_heartbeat(test_mode=True)
        assert len(calls) >= 1


# =============================================================================
# 5. Atomic save_state regression (R2 NM2 — shared.py root-cause fix)
# =============================================================================


class TestAtomicSaveState:
    def test_round_trip_and_no_tmp_leftover(self, tmp_path):
        state_file = tmp_path / "state.json"
        shared.save_state({"alert_history": [], "k": "v"}, state_file)
        assert json.loads(state_file.read_text(encoding="utf-8")) == {
            "alert_history": [],
            "k": "v",
        }
        assert not state_file.with_suffix(".json.tmp").exists()
        # JSON shape unchanged (indent=2)
        assert state_file.read_text(encoding="utf-8").startswith('{\n  "')

    def test_replace_failure_leaves_original_intact(self, tmp_path, monkeypatch):
        import os as _os

        state_file = tmp_path / "state.json"
        original = {"alert_history": [{"text": "keep me"}], "last_run": "x"}
        shared.save_state(original, state_file)
        before = state_file.read_text(encoding="utf-8")

        def _boom(*_a, **_k):
            raise OSError("interrupted mid-replace")

        monkeypatch.setattr(_os, "replace", _boom)
        with pytest.raises(OSError):
            shared.save_state({"alert_history": [], "corrupting": "write"}, state_file)

        # Original file content survives intact and loadable
        assert state_file.read_text(encoding="utf-8") == before
        assert shared.load_state(state_file) == original

    def test_serialization_failure_never_touches_file(self, tmp_path):
        state_file = tmp_path / "state.json"
        original = {"alert_history": []}
        shared.save_state(original, state_file)
        before = state_file.read_text(encoding="utf-8")

        circular: dict = {"a": []}
        circular["a"].append(circular)  # json.dumps raises ValueError
        with pytest.raises(ValueError):
            shared.save_state(circular, state_file)

        assert state_file.read_text(encoding="utf-8") == before
        assert not state_file.with_suffix(".json.tmp").exists()


# =============================================================================
# Accessor contract (R2 NB1)
# =============================================================================


class TestObservationClientAccessor:
    def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "false")
        assert langfuse_setup.get_observation_client() is None

    def test_never_raises_fail_open(self, monkeypatch):
        def _boom():
            raise RuntimeError("config exploded")

        monkeypatch.setattr(langfuse_setup, "is_langfuse_enabled", _boom)
        assert langfuse_setup.get_observation_client() is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
