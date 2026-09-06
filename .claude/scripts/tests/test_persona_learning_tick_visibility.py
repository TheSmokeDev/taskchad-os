"""Failure visibility + retry cadence for the persona learning tick.

Reproduced 2026-09-02: crypto's child exited 1 with the cause on STDOUT, the
parent kept only a stderr tail, the receipt read ``exit 1: ``, Task Scheduler
showed ``Last Result: 0``, and the 12h guard on a 12h cadence pushed the retry
to the next day. Each test here fails without its fix.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

import persona_learning_tick as tick  # noqa: E402

_CAUSE_LINE = (
    "[2026-09-02 09:31:53] Persona notes distillation call failed (non-blocking): "
    "RuntimeExecutionError: No runtime could satisfy task "
    "'persona_notes_distillation' (text_reasoning) on lane 'generic_runtime'"
)
_FAIL_HONEST_LINE = (
    "Work-note distillation did not complete — exiting non-zero so the "
    "learning tick keeps its boundary and retries these notes."
)


# ── The receipt carries the child's own reason ───────────────────────────────


class TestChildFailureTail:
    def _spawn(self, tmp_path: Path, *, stdout: str, stderr: str) -> tuple[bool, str]:
        completed = SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)
        with patch.object(tick, "build_capability_scoped_env", return_value={}):
            with patch.object(tick.subprocess, "run", return_value=completed):
                return tick._spawn_persona_pipeline("alpha", tmp_path)

    def test_stdout_cause_survives_into_the_receipt(self, tmp_path: Path) -> None:
        ok, message = self._spawn(
            tmp_path,
            stdout=f"Running daily reflection...\n{_CAUSE_LINE}\n\n{_FAIL_HONEST_LINE}\n",
            stderr="",
        )
        assert ok is False
        assert message.startswith("exit 1: ")
        assert message != "exit 1: "  # the receipt that shipped for months
        assert "persona_notes_distillation" in message
        assert "Work-note distillation did not complete" in message

    def test_stderr_traceback_is_still_kept(self, tmp_path: Path) -> None:
        _, message = self._spawn(
            tmp_path, stdout="", stderr="Traceback (most recent call last):\nValueError: boom\n"
        )
        assert "ValueError: boom" in message

    def test_both_streams_are_reported_when_both_exist(self, tmp_path: Path) -> None:
        _, message = self._spawn(tmp_path, stdout=_FAIL_HONEST_LINE, stderr="warning: x")
        assert "stdout: " in message
        assert "stderr: " in message

    def test_no_output_says_so(self, tmp_path: Path) -> None:
        _, message = self._spawn(tmp_path, stdout="", stderr="")
        assert "(child produced no output)" in message

    def test_tail_is_bounded_and_keeps_the_end(self) -> None:
        stdout = "\n".join(f"line {i} " + "x" * 200 for i in range(50))
        tail = tick._child_failure_tail(stdout, "")
        assert len(tail) <= tick._FAILURE_TAIL_MAX_CHARS
        assert "line 49" in tail
        assert "line 0 " not in tail

    def test_success_message_is_unchanged(self, tmp_path: Path) -> None:
        completed = SimpleNamespace(returncode=0, stdout="fine", stderr="")
        with patch.object(tick, "build_capability_scoped_env", return_value={}):
            with patch.object(tick.subprocess, "run", return_value=completed):
                assert tick._spawn_persona_pipeline("alpha", tmp_path) == (True, "success")


# ── The tick reports what happened and the entrypoint exits honestly ────────


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "enabled": True,
        "tick_interval_hours": 12.0,
        "silent_skip_window_hours": 24.0,
        "timeout_seconds": 900.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _profile(name: str, path: Path) -> MagicMock:
    p = MagicMock()
    p.name = name
    p.path = path
    p.is_default = False
    return p


def _run_roster(
    tmp_path: Path,
    spawn_results: list[tuple[bool, str]],
    *,
    seed_state: dict[str, dict[str, str]] | None = None,
    settings: SimpleNamespace | None = None,
    once: bool = False,
) -> tuple[tick.TickOutcome, dict[str, dict], MagicMock]:
    """Drive run_tick over personas alpha+beta with every seam stubbed.

    Returns (outcome, {persona: state-file-dict}, spawn mock).
    """
    (tmp_path / "chat.db").touch()
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    for name, state in (seed_state or {}).items():
        (state_dir / f"persona-learning-{name}-state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
    default_p = MagicMock()
    default_p.is_default = True
    roster = [default_p, _profile("alpha", tmp_path / "alpha"), _profile("beta", tmp_path / "beta")]

    spawn = MagicMock(side_effect=spawn_results)
    with (
        patch.object(tick, "is_active_default_profile", return_value=True),
        patch.object(tick, "get_default_paths", return_value={"data": tmp_path}),
        patch.object(tick, "list_profiles", return_value=roster),
        patch.object(tick, "load_persona_config", return_value={"learning": {"enabled": True}}),
        patch.object(tick, "get_persona_learning_settings", return_value=settings or _settings()),
        patch.object(
            tick, "get_background_models", return_value={"quality": "sonnet", "fast": "haiku"}
        ),
        patch.object(tick, "_count_attributed_rows_since", return_value=5),
        patch.object(tick, "_count_fresh_notes_since", return_value=0),
        patch.object(tick, "_spawn_persona_pipeline", spawn),
        patch.object(tick, "STATE_DIR", state_dir),
        patch.object(
            tick,
            "_persona_state_file",
            side_effect=lambda n: state_dir / f"persona-learning-{n}-state.json",
        ),
    ):
        outcome = tick.run_tick(once=once)

    states = {}
    for f in state_dir.glob("persona-learning-*-state.json"):
        states[f.name.removeprefix("persona-learning-").removesuffix("-state.json")] = json.loads(
            f.read_text(encoding="utf-8")
        )
    return outcome, states, spawn


class TestTickOutcomeAndExitCode:
    def test_outcome_names_failed_and_spawned(self, tmp_path: Path) -> None:
        outcome, _, _ = _run_roster(
            tmp_path,
            [(False, "exit 1: stdout: Work-note distillation did not complete"), (True, "success")],
        )
        assert outcome.spawned == ("alpha", "beta")
        assert outcome.failed == ("alpha",)

    def test_all_good_has_no_failures(self, tmp_path: Path) -> None:
        outcome, _, _ = _run_roster(tmp_path, [(True, "success"), (True, "success")])
        assert outcome.failed == ()
        assert outcome.spawned == ("alpha", "beta")

    def test_failed_names_are_summarised_in_the_log(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run_roster(tmp_path, [(False, "exit 1: boom"), (False, "timeout (300s)")])
        out = capsys.readouterr().out
        assert "2 persona(s) FAILED this tick: alpha, beta" in out

    def test_early_exits_still_return_an_outcome(self, tmp_path: Path) -> None:
        with patch.object(
            tick, "get_persona_learning_settings", return_value=_settings(enabled=False)
        ):
            outcome = tick.run_tick()
        assert outcome == tick.TickOutcome()

    def test_main_exits_nonzero_when_any_persona_failed(self) -> None:
        with patch.object(
            tick, "run_tick", return_value=tick.TickOutcome(spawned=("alpha",), failed=("alpha",))
        ):
            assert tick.main([]) == 1

    def test_main_exits_zero_when_nothing_failed(self) -> None:
        with patch.object(
            tick, "run_tick", return_value=tick.TickOutcome(spawned=("alpha",))
        ) as rt:
            assert tick.main(["--test", "--once"]) == 0
        rt.assert_called_once_with(test_mode=True, once=True)

    def test_entrypoint_forwards_the_exit_code(self) -> None:
        src = (_SCRIPTS_DIR / "persona_learning_tick.py").read_text(encoding="utf-8")
        assert "sys.exit(main())" in src

    def test_wrappers_forward_the_exit_code(self) -> None:
        sh = (_SCRIPTS_DIR / "run_persona_learning.sh").read_text(encoding="utf-8")
        bat = (_SCRIPTS_DIR / "run_persona_learning.bat").read_text(encoding="utf-8")
        assert "exit $EXITCODE" in sh
        assert "FAILED" in sh
        assert "exit /b %EXITCODE%" in bat


# ── Recency guard: a 12h guard must fire on a 12h cadence ───────────────────


class TestRecencyGuardCadence:
    def test_slot_within_jitter_of_the_interval_is_due(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        outcome, _, spawn = _run_roster(
            tmp_path,
            [(True, "success")],
            seed_state={
                # 11h50m ago: the 21:30 slot after a 09:31 stamp. Used to skip.
                "alpha": {"last_attempt": (now - timedelta(hours=11, minutes=50)).isoformat()},
                # 6h ago: genuinely recent, must still skip.
                "beta": {"last_attempt": (now - timedelta(hours=6)).isoformat()},
            },
        )
        assert spawn.call_count == 1
        assert spawn.call_args.args[0] == "alpha"
        assert outcome.spawned == ("alpha",)

    def test_well_inside_the_interval_still_skips(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        now = datetime.now(UTC)
        _, _, spawn = _run_roster(
            tmp_path,
            [],
            seed_state={
                "alpha": {"last_attempt": (now - timedelta(hours=11)).isoformat()},
                "beta": {"last_attempt": (now - timedelta(hours=1)).isoformat()},
            },
        )
        assert spawn.call_count == 0
        assert capsys.readouterr().out.count("recency guard") == 2

    def test_jitter_allowance_is_minutes_not_hours(self) -> None:
        assert 0 < tick.RECENCY_GUARD_JITTER_HOURS <= 0.5


class TestLastAttemptStamp:
    def test_stamped_at_tick_start_and_shared_across_the_roster(self, tmp_path: Path) -> None:
        _, states, _ = _run_roster(tmp_path, [(True, "success"), (True, "success")])
        assert states["alpha"]["last_attempt"] == states["beta"]["last_attempt"]
        for name in ("alpha", "beta"):
            assert states[name]["result"] == "success"
            # tick start precedes this persona's pre-spawn upper bound
            assert states[name]["last_attempt"] <= states[name]["last_run"]

    def test_failure_moves_last_attempt_but_holds_last_run(self, tmp_path: Path) -> None:
        old_run = "2026-01-01T00:00:00+00:00"
        _, states, _ = _run_roster(
            tmp_path,
            [(False, "exit 1: stdout: Work-note distillation did not complete"), (True, "success")],
            seed_state={
                "alpha": {"last_run": old_run, "last_attempt": old_run},
                # beta is newer, so overdue-first runs alpha (and the scripted
                # failure) first
                "beta": {"last_attempt": (datetime.now(UTC) - timedelta(hours=20)).isoformat()},
            },
        )
        assert states["alpha"]["last_run"] == old_run
        assert states["alpha"]["last_attempt"] != old_run
        assert states["alpha"]["result"] == "failed"
        assert "Work-note distillation" in states["alpha"]["message"]


# ── Codex review of #684: order, claim, lock ────────────────────────────────


class TestRosterOrder:
    def test_oldest_attempt_runs_first(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        _, _, spawn = _run_roster(
            tmp_path,
            [(True, "success"), (True, "success")],
            seed_state={
                "alpha": {"last_attempt": (now - timedelta(hours=20)).isoformat()},
                "beta": {"last_attempt": (now - timedelta(hours=30)).isoformat()},
            },
        )
        assert [c.args[0] for c in spawn.call_args_list] == ["beta", "alpha"]

    def test_never_attempted_runs_before_everyone(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        _, _, spawn = _run_roster(
            tmp_path,
            [(True, "success"), (True, "success")],
            seed_state={"alpha": {"last_attempt": (now - timedelta(hours=20)).isoformat()}},
        )
        assert [c.args[0] for c in spawn.call_args_list] == ["beta", "alpha"]

    def test_unreadable_stamp_sorts_first_and_is_still_attempted(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "persona-learning-alpha-state.json").write_text("{not json", encoding="utf-8")
        now = datetime.now(UTC)
        _, _, spawn = _run_roster(
            tmp_path,
            [(True, "success"), (True, "success")],
            seed_state={"beta": {"last_attempt": (now - timedelta(hours=20)).isoformat()}},
        )
        assert [c.args[0] for c in spawn.call_args_list][0] == "alpha"


class TestAttemptClaim:
    def test_last_attempt_is_written_before_the_child_starts(self, tmp_path: Path) -> None:
        seen: dict[str, object] = {}

        def probe(name: str, root: Path, **_kw: object) -> tuple[bool, str]:
            receipt = json.loads(
                (tmp_path / "state" / f"persona-learning-{name}-state.json").read_text(
                    encoding="utf-8"
                )
            )
            seen[name] = receipt.get("last_attempt")
            return (False, "exit 1: stdout: simulated")

        old = "2026-01-01T00:00:00+00:00"
        _, states, _ = _run_roster(
            tmp_path, probe, seed_state={"alpha": {"last_attempt": old, "last_run": old}}
        )
        assert seen["alpha"] is not None and seen["alpha"] != old
        assert seen["alpha"] == states["alpha"]["last_attempt"]
        assert states["alpha"]["last_run"] == old


class TestTickLock:
    def test_second_tick_exits_without_spawning_while_first_holds_the_lock(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with tick.file_lock(state_dir / "persona-learning-tick.json", timeout=1.0):
            with patch.object(tick, "TICK_LOCK_TIMEOUT_SECONDS", 0.2):
                outcome, _, spawn = _run_roster(tmp_path, [(True, "success"), (True, "success")])
        assert outcome == tick.TickOutcome()
        assert spawn.call_count == 0
        assert "another tick holds the lock" in capsys.readouterr().out

    def test_lock_is_released_after_a_normal_run(self, tmp_path: Path) -> None:
        outcome, _, _ = _run_roster(tmp_path, [(True, "success"), (True, "success")])
        assert outcome.spawned == ("alpha", "beta")
        with tick.file_lock(tmp_path / "state" / "persona-learning-tick.json", timeout=0.5):
            pass  # acquirable immediately: the tick let go


# ── Child timeout is a knob, and the receipt keeps the partial output ───────


class TestChildTimeout:
    def _spawn_with_timeout(self, tmp_path: Path, exc: subprocess.TimeoutExpired, **kw):
        with patch.object(tick, "build_capability_scoped_env", return_value={}):
            with patch.object(tick.subprocess, "run", side_effect=exc):
                return tick._spawn_persona_pipeline("alpha", tmp_path, **kw)

    def test_timeout_receipt_keeps_the_partial_stdout(self, tmp_path: Path) -> None:
        exc = subprocess.TimeoutExpired(
            cmd="memory_reflect.py",
            timeout=5,
            output="Persona note distillation: 2 candidate(s), 1 applied\nContradiction pass...\n",
        )
        ok, message = self._spawn_with_timeout(tmp_path, exc, timeout_seconds=5)
        assert ok is False
        assert message.startswith("timeout (5s): ")
        assert "1 applied" in message
        assert message != "timeout (300s)"  # the receipt that hid crypto's first lesson

    def test_timeout_receipt_accepts_bytes_output(self, tmp_path: Path) -> None:
        exc = subprocess.TimeoutExpired(cmd="x", timeout=7, output=b"bytes tail here")
        _, message = self._spawn_with_timeout(tmp_path, exc, timeout_seconds=7)
        assert "bytes tail here" in message

    def test_timeout_receipt_without_output_says_so(self, tmp_path: Path) -> None:
        exc = subprocess.TimeoutExpired(cmd="x", timeout=3)
        _, message = self._spawn_with_timeout(tmp_path, exc, timeout_seconds=3)
        assert "(child produced no output)" in message

    def test_tick_hands_the_configured_timeout_to_the_child(self, tmp_path: Path) -> None:
        _, _, spawn = _run_roster(
            tmp_path,
            [(True, "success"), (True, "success")],
            settings=_settings(timeout_seconds=1234.0),
        )
        assert spawn.call_args.kwargs["timeout_seconds"] == 1234.0

    def test_unpassed_timeout_resolves_from_config_at_call_time(self, tmp_path: Path) -> None:
        captured: dict[str, object] = {}

        def fake_run(cmd, **kw):
            captured["timeout"] = kw.get("timeout")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(tick, "build_capability_scoped_env", return_value={}):
            with patch.object(tick.subprocess, "run", side_effect=fake_run):
                with patch.object(
                    tick,
                    "get_persona_learning_settings",
                    return_value=_settings(timeout_seconds=42.0),
                ):
                    tick._spawn_persona_pipeline("alpha", tmp_path)
        assert captured["timeout"] == 42.0

    def test_config_default_is_900_and_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import config

        monkeypatch.delenv("PERSONA_LEARNING_TIMEOUT", raising=False)
        assert config.get_persona_learning_settings().timeout_seconds == 900.0
        monkeypatch.setenv("PERSONA_LEARNING_TIMEOUT", "1500")
        assert config.get_persona_learning_settings().timeout_seconds == 1500.0
        monkeypatch.setenv("PERSONA_LEARNING_TIMEOUT", "garbage")
        assert config.get_persona_learning_settings().timeout_seconds == 900.0
