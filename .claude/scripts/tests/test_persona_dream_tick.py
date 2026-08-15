"""Tests for the persona dream tick (issue #423, epic #418).

Path map — one non-vacuous test per distinct path:
  A. Config resolver (Rule 1) — defaults, per-knob env override, explicit
     pass-through, None-sentinel introspection, no module-level constants
  B. Boot order + grep gates — shim before config import, no provider imports,
     and the DOCTRINE gate: no per-persona learning filter
  C. run_tick guards — kill switch, default-profile guard, zero named profiles
  D. run_tick fan-out — spawn shape, per-persona fail-open, recency guard,
     wall-clock budget (names what it drops), oldest-first ordering, --once
  E. Shared-state collision guard — refuses to spawn onto the MAIN dream state
  F. read_child_dream_receipt — missing / present / unreadable (Rule 2 read-back)
  G. _spawn_persona_dream — command shape, --child-test, env failure, timeout,
     non-zero exit
  H. Isolation (acceptance) — per-profile state paths are distinct, a persona's
     dream write leaves its sibling and the MAIN vault byte-unchanged, and a
     REAL subprocess proves config re-roots under a profile root
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_TICK_SRC = (_SCRIPTS_DIR / "persona_dream_tick.py").read_text(encoding="utf-8")
_BAT_SRC = (_SCRIPTS_DIR / "run_persona_dream.bat").read_text(encoding="utf-8")
_SH_SRC = (_SCRIPTS_DIR / "run_persona_dream.sh").read_text(encoding="utf-8")


def _profile(name: str, root: Path):
    """A REAL ProfileInfo (not a stand-in) for a named persona."""
    from personas.lifecycle import ProfileInfo

    return ProfileInfo(
        name=name,
        path=root / name,
        is_default=False,
        bot_running=False,
        has_env=False,
        skill_count=0,
    )


def _default_profile(root: Path):
    from personas.lifecycle import ProfileInfo

    return ProfileInfo(
        name="default",
        path=root,
        is_default=True,
        bot_running=False,
        has_env=False,
        skill_count=0,
    )


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _dir_hash(path: Path) -> str:
    h = hashlib.sha256()
    if not path.exists():
        return h.hexdigest()
    for f in sorted(path.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(path)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


class _FanOutHarness:
    """Wires run_tick onto tmp dirs: fan-out stamps in a fake MAIN STATE_DIR,
    child dream-state files in per-profile trees. Nothing here touches the real
    ~/.homie tree or the install vault."""

    # The per-spawn nonce is generated inside run_tick, so tests pin it through
    # the _new_spawn_id seam and write it into the child receipts they stage —
    # exactly what a real child does by echoing SPAWN_ID_ENV back into its own
    # dream-state.json.
    SPAWN_ID = "spawn-under-test"

    def __init__(self, tmp_path: Path, names: list[str]) -> None:
        self.tmp_path = tmp_path
        self.main_state = tmp_path / "main" / "state"
        self.main_state.mkdir(parents=True)
        self.main_dream_state = self.main_state / "dream-state.json"
        self.profiles_root = tmp_path / "profiles"
        self.profiles_root.mkdir()
        self.names = names
        self.profiles = [_profile(n, self.profiles_root) for n in names]
        for p in self.profiles:
            (p.path / "state").mkdir(parents=True)

    def child_state(self, name: str) -> Path:
        return self.profiles_root / name / "state" / "dream-state.json"

    def stamp(self, name: str) -> Path:
        return self.main_state / f"persona-dream-{name}-state.json"

    def write_stamp(self, name: str, payload: dict) -> None:
        self.stamp(name).write_text(json.dumps(payload), encoding="utf-8")

    def write_child_state(self, name: str, payload: dict) -> None:
        self.child_state(name).write_text(json.dumps(payload), encoding="utf-8")

    def patches(self):
        return [
            patch("persona_dream_tick.is_active_default_profile", return_value=True),
            patch("persona_dream_tick.list_profiles", return_value=list(self.profiles)),
            patch("persona_dream_tick.STATE_DIR", self.main_state),
            patch("persona_dream_tick.DREAM_STATE_FILE", self.main_dream_state),
            patch(
                "persona_dream_tick.get_persona_paths",
                side_effect=lambda n: {"state": self.profiles_root / n / "state"},
            ),
            patch("persona_dream_tick._new_spawn_id", return_value=self.SPAWN_ID),
        ]

    def fresh_child_state(self, name: str, **extra) -> None:
        """A child receipt that PROVES it was written by the run under test:
        a recognised result, a sane last_run, and this spawn's nonce."""
        payload = {
            "result": "consolidated",
            "last_run": datetime.now().isoformat(),
            "spawn_id": self.SPAWN_ID,
            "phases_completed": [
                "orient", "gather", "consolidate", "prune", "belief_evolve",
            ],
        }
        payload.update(extra)
        self.write_child_state(name, payload)

    def run(self, **kwargs):
        from persona_dream_tick import run_tick

        stack = self.patches()
        for p in stack:
            p.start()
        try:
            return run_tick(**kwargs)
        finally:
            for p in reversed(stack):
                p.stop()


# ============================================================================
# A. Config resolver — Rule 1
# ============================================================================


class TestPersonaDreamConfigResolver:
    def test_defaults(self) -> None:
        from config import get_persona_dream_settings

        s = get_persona_dream_settings()
        assert s.enabled is True
        assert s.tick_interval_hours == 20.0
        assert s.timeout_seconds == 900.0
        # Unlimited by default — the nightly contract is EVERY named persona,
        # and any finite cap silently drops the tail of a large roster.
        assert s.max_wall_clock_seconds == 0.0
        assert s.days == 7

    @pytest.mark.parametrize(
        "env_key,env_value,field,expected",
        [
            ("PERSONA_DREAM_ENABLED", "false", "enabled", False),
            ("PERSONA_DREAM_TICK_INTERVAL", "6", "tick_interval_hours", 6.0),
            ("PERSONA_DREAM_TIMEOUT", "120", "timeout_seconds", 120.0),
            # A non-default value: 0 IS the default now, so asserting 0 here
            # would pass without the env var ever being read.
            ("PERSONA_DREAM_MAX_WALL_CLOCK", "1800", "max_wall_clock_seconds", 1800.0),
            ("PERSONA_DREAM_DAYS", "14", "days", 14),
        ],
    )
    def test_env_overrides(
        self,
        env_key: str,
        env_value: str,
        field: str,
        expected: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each knob resolves from its env var at CALL time — no reload."""
        from config import get_persona_dream_settings

        monkeypatch.setenv(env_key, env_value)
        assert getattr(get_persona_dream_settings(), field) == expected

    def test_explicit_values_bypass_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from config import get_persona_dream_settings

        monkeypatch.setenv("PERSONA_DREAM_ENABLED", "false")
        monkeypatch.setenv("PERSONA_DREAM_DAYS", "99")
        s = get_persona_dream_settings(enabled=True, days=3)
        assert s.enabled is True
        assert s.days == 3

    def test_all_params_are_none_sentinels(self) -> None:
        """Rule 1: no config value may be bound in a default arg."""
        import inspect

        from config import get_persona_dream_settings

        sig = inspect.signature(get_persona_dream_settings)
        for param in sig.parameters.values():
            assert param.default is None, f"{param.name} must default to None"

    def test_no_module_level_persona_dream_constants(self) -> None:
        src = (_SCRIPTS_DIR / "config.py").read_text(encoding="utf-8")
        matches = re.findall(r"^PERSONA_DREAM_\w+\s*=\s*os\.getenv", src, re.MULTILINE)
        assert matches == [], f"Rule 1 violation — module-level knobs: {matches}"

    def test_settings_namedtuple_fields(self) -> None:
        from config import PersonaDreamSettings

        assert PersonaDreamSettings._fields == (
            "enabled",
            "tick_interval_hours",
            "timeout_seconds",
            "max_wall_clock_seconds",
            "days",
        )


# ============================================================================
# B. Boot order + grep gates
# ============================================================================


class TestBootOrderAndGates:
    def test_shim_called_at_module_top_level(self) -> None:
        assert re.search(r"^\s*apply_persona_override\s*\(\s*\)", _TICK_SRC, re.MULTILINE)

    def test_shim_precedes_config_import(self) -> None:
        shim_pos = _TICK_SRC.find("apply_persona_override()")
        config_import = re.search(r"^\s*from\s+config\s+import", _TICK_SRC, re.MULTILINE)
        assert shim_pos >= 0 and config_import is not None
        assert shim_pos < config_import.start()

    def test_has_main_guard(self) -> None:
        assert '__name__ == "__main__"' in _TICK_SRC

    def test_no_direct_provider_imports(self) -> None:
        assert "from anthropic" not in _TICK_SRC
        assert "import anthropic" not in _TICK_SRC
        assert "claude_agent_sdk" not in _TICK_SRC

    def test_uses_capability_scoped_env_and_default_profile_guard(self) -> None:
        assert "build_capability_scoped_env" in _TICK_SRC
        assert "is_active_default_profile" in _TICK_SRC

    def test_spawns_memory_dream_with_profile_flag(self) -> None:
        assert "memory_dream.py" in _TICK_SRC
        assert '"-p", persona_name' in _TICK_SRC

    def test_no_per_persona_learning_filter(self) -> None:
        """Equality doctrine: the dream fans out to EVERY named profile.

        The learning tick gates each persona on config.yaml ``learning.enabled``;
        this tick deliberately does not, so it must not reach for that config at
        all. A future edit re-introducing the filter trips this gate.
        """
        assert "load_persona_config" not in _TICK_SRC
        assert 'get("learning"' not in _TICK_SRC
        assert "PERSONA_LEARNING_" not in _TICK_SRC


# ============================================================================
# B2. Scheduler wrapper scripts force default context (codex R2 MAJOR)
# ============================================================================


class TestSchedulerScriptsForceDefaultProfile:
    """Neither wrapper may invoke ``persona_dream_tick.py`` bare. Rank-2
    (inherited HOMIE_HOME) or rank-3 (sticky ~/.homie/active_profile) can
    silently put the scheduled process into a NAMED profile's context, and
    run_tick's own ``is_active_default_profile()`` guard then refuses —
    the entire nightly fan-out no-ops with no error, every night, until an
    operator notices. ``-p default`` is the boot-shim's rank-1 force-default
    sentinel (personas/boot.py) and always wins."""

    def test_bat_forces_default_profile(self) -> None:
        assert re.search(
            r"persona_dream_tick\.py\s+-p\s+default", _BAT_SRC
        ), "run_persona_dream.bat must invoke with -p default"

    def test_sh_forces_default_profile(self) -> None:
        assert re.search(
            r"persona_dream_tick\.py\s+-p\s+default", _SH_SRC
        ), "run_persona_dream.sh must invoke with -p default"


# ============================================================================
# C. run_tick guards
# ============================================================================


class TestGuards:
    def test_kill_switch_disables_fan_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setenv("PERSONA_DREAM_ENABLED", "false")
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch("persona_dream_tick._spawn_persona_dream") as spawn:
            h.run()
        spawn.assert_not_called()
        assert "disabled via PERSONA_DREAM_ENABLED" in capsys.readouterr().out

    @patch("persona_dream_tick.is_active_default_profile", return_value=False)
    def test_refuses_under_named_profile(self, _default, capsys) -> None:
        from persona_dream_tick import run_tick

        with patch("persona_dream_tick._spawn_persona_dream") as spawn:
            run_tick()
        spawn.assert_not_called()
        assert "must run under default profile" in capsys.readouterr().out

    def test_zero_named_profiles_is_noop(self, tmp_path: Path, capsys) -> None:
        from persona_dream_tick import run_tick

        with patch("persona_dream_tick.is_active_default_profile", return_value=True), \
             patch(
                 "persona_dream_tick.list_profiles",
                 return_value=[_default_profile(tmp_path)],
             ), \
             patch("persona_dream_tick._spawn_persona_dream") as spawn:
            run_tick()
        spawn.assert_not_called()
        assert "no named profiles found" in capsys.readouterr().out


# ============================================================================
# D. run_tick fan-out
# ============================================================================


class TestFanOut:
    def test_test_mode_stamps_without_spawning(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha", "beta"])
        with patch("persona_dream_tick._spawn_persona_dream") as spawn:
            h.run(test_mode=True)
        spawn.assert_not_called()
        for name in ("alpha", "beta"):
            stamp = json.loads(h.stamp(name).read_text())
            assert stamp["last_test_result"] == "test_skip"
            assert "last_test_run" in stamp
            # --test bookkeeping must NEVER land in the fields the recency
            # guard reads (codex R2 MAJOR) — see
            # test_test_mode_does_not_poison_recency_guard_for_real_run.
            assert "last_run" not in stamp
            assert "result" not in stamp

    def test_test_mode_does_not_poison_recency_guard_for_real_run(
        self, tmp_path: Path
    ) -> None:
        """--test is a preview, not a completed run. Running it during the
        day must never suppress that night's REAL dream — the recency guard
        only gates on a genuine last_run, which --test must leave untouched."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch("persona_dream_tick._spawn_persona_dream") as spawn:
            h.run(test_mode=True)
        spawn.assert_not_called()

        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ) as real_spawn:
            h.run()
        real_spawn.assert_called_once()

    def test_success_records_physical_child_receipt(self, tmp_path: Path) -> None:
        """The stamp carries what the PROFILE TREE says, not what the exit code
        implies (Rule 2)."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        h.fresh_child_state(
            "alpha",
            belief_evolve={"result": "ran", "adopted": 1, "rejected": 2},
        )
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ):
            h.run()
        stamp = json.loads(h.stamp("alpha").read_text())
        assert stamp["result"] == "success"
        receipt = stamp["dream_state"]
        assert receipt["present"] is True
        assert receipt["status"] == "consolidated"
        assert receipt["wrote_this_spawn"] is True
        assert receipt["result"] == "consolidated"
        assert receipt["belief_evolve_result"] == "ran"
        assert receipt["belief_adopted"] == 1
        assert receipt["path"] == str(h.child_state("alpha"))

    def test_missing_child_state_is_visible_not_assumed(self, tmp_path: Path) -> None:
        """A clean exit with NO dream-state.json in the profile tree must not
        read as a completed dream — the epic's whole acceptance rests on that
        file existing where the re-root puts it. The top-level stamp result
        must say so too (codex R2 MAJOR): exit code 0 alone is not proof a
        dream actually ran, so it must never be recorded as plain "success"."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ):
            h.run()
        stamp = json.loads(h.stamp("alpha").read_text())
        assert stamp["result"] == "no_receipt"
        receipt = stamp["dream_state"]
        assert receipt["present"] is False
        assert receipt["status"] == "missing"
        assert "result" not in receipt

    def test_one_failure_does_not_block_the_next(self, tmp_path: Path, capsys) -> None:
        h = _FanOutHarness(tmp_path, ["alpha", "beta"])
        h.fresh_child_state("beta")
        with patch(
            "persona_dream_tick._spawn_persona_dream",
            side_effect=[(False, "crash"), (True, "success")],
        ) as spawn:
            h.run()
        assert spawn.call_count == 2
        out = capsys.readouterr().out
        assert "FAILED" in out and "OK [consolidated]" in out
        assert json.loads(h.stamp("alpha").read_text())["result"] == "failed"
        assert json.loads(h.stamp("beta").read_text())["result"] == "success"

    def test_failed_spawn_still_reads_back_child_state(self, tmp_path: Path) -> None:
        """A child that died inside an LLM phase advanced its own state to
        result="failed"; the operator needs that, so the read-back runs on the
        failure path too."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        h.write_child_state("alpha", {"result": "failed", "error": "provider 429"})
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(False, "exit 1")
        ):
            h.run()
        stamp = json.loads(h.stamp("alpha").read_text())
        assert stamp["result"] == "failed"
        assert stamp["dream_state"]["result"] == "failed"

    def test_recency_guard_skips_recent_persona(self, tmp_path: Path, capsys) -> None:
        h = _FanOutHarness(tmp_path, ["alpha"])
        h.write_stamp(
            "alpha",
            {"last_run": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()},
        )
        with patch("persona_dream_tick._spawn_persona_dream") as spawn:
            h.run()
        spawn.assert_not_called()
        assert "recency guard" in capsys.readouterr().out

    def test_recency_guard_releases_after_interval(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha"])
        h.write_stamp(
            "alpha",
            {"last_run": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()},
        )
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ) as spawn:
            h.run()
        assert spawn.call_count == 1

    def test_corrupt_stamp_fails_open_and_runs(self, tmp_path: Path) -> None:
        """An unparseable last_run must never wedge a persona out of its dream."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        h.write_stamp("alpha", {"last_run": "not-a-timestamp"})
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ) as spawn:
            h.run()
        assert spawn.call_count == 1

    def test_wall_clock_budget_names_what_it_drops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """A truncated fan-out must NAME the personas it never reached — a
        silent cap reads as 'covered everyone' when it did not."""
        monkeypatch.setenv("PERSONA_DREAM_MAX_WALL_CLOCK", "10")
        h = _FanOutHarness(tmp_path, ["alpha", "beta", "gamma"])
        clock = iter([0.0, 0.0, 99.0])  # start, alpha check, beta check (over)

        with patch("persona_dream_tick.time.monotonic", side_effect=lambda: next(clock)), \
             patch(
                 "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
             ) as spawn:
            h.run()

        assert spawn.call_count == 1
        out = capsys.readouterr().out
        assert "wall-clock cap" in out and "exhausted" in out
        assert "beta" in out and "gamma" in out
        assert not h.stamp("beta").exists()

    def test_truncated_personas_are_reported_in_the_outcome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cap that truncates must be machine-visible too, not just a log
        line — the summary counts it and the outcome names it."""
        monkeypatch.setenv("PERSONA_DREAM_MAX_WALL_CLOCK", "10")
        h = _FanOutHarness(tmp_path, ["alpha", "beta", "gamma"])
        clock = iter([0.0, 0.0, 99.0])
        with patch("persona_dream_tick.time.monotonic", side_effect=lambda: next(clock)), \
             patch(
                 "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
             ):
            outcome = h.run()
        assert outcome.truncated == ("beta", "gamma")
        assert outcome.attempted == ("alpha",)

    def test_default_settings_run_the_whole_roster(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The nightly contract is EVERY named persona, so the DEFAULT config
        must not truncate a real roster.

        28 personas at three minutes each is an ordinary night once children
        have signal. Under a 3600s default the fan-out breaks after ~20 and the
        remaining eight get no dream — and "they rotate to the front tomorrow"
        is a rotation, not a nightly cadence."""
        monkeypatch.delenv("PERSONA_DREAM_MAX_WALL_CLOCK", raising=False)
        names = [f"p{i:02d}" for i in range(28)]
        h = _FanOutHarness(tmp_path, names)

        elapsed = {"t": 0.0}

        def _clock() -> float:
            elapsed["t"] += 180.0  # three minutes per persona
            return elapsed["t"]

        with patch("persona_dream_tick.time.monotonic", side_effect=_clock), \
             patch(
                 "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
             ) as spawn:
            outcome = h.run()

        assert spawn.call_count == 28
        assert outcome.truncated == ()
        assert len(outcome.attempted) == 28

    def test_zero_budget_means_unlimited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PERSONA_DREAM_MAX_WALL_CLOCK", "0")
        h = _FanOutHarness(tmp_path, ["alpha", "beta"])
        clock = iter([0.0, 10_000.0, 20_000.0])
        with patch("persona_dream_tick.time.monotonic", side_effect=lambda: next(clock)), \
             patch(
                 "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
             ) as spawn:
            h.run()
        assert spawn.call_count == 2

    def test_oldest_attempted_persona_runs_first(self, tmp_path: Path) -> None:
        """Ordering rotates the tail that a budget truncation would starve."""
        h = _FanOutHarness(tmp_path, ["alpha", "beta", "gamma"])
        now = datetime.now(timezone.utc)
        h.write_stamp("alpha", {"last_run": (now - timedelta(days=1)).isoformat()})
        h.write_stamp("beta", {"last_run": (now - timedelta(days=5)).isoformat()})
        # gamma has never run -> sorts before both

        seen: list[str] = []
        with patch(
            "persona_dream_tick._spawn_persona_dream",
            side_effect=lambda name, root, **kw: (seen.append(name), (True, "ok"))[1],
        ):
            h.run()
        assert seen == ["gamma", "beta", "alpha"]

    def test_once_stops_after_first_persona(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha", "beta"])
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ) as spawn:
            h.run(once=True)
        assert spawn.call_count == 1

    def test_child_test_flag_threads_through(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ) as spawn:
            h.run(child_test=True)
        assert spawn.call_args.kwargs["child_test"] is True
        stamp = json.loads(h.stamp("alpha").read_text())
        assert stamp["last_test_result"] == "child_test_success"
        assert stamp["last_test_child_test"] is True

    def test_child_test_bookkeeping_never_lands_in_the_guarded_fields(
        self, tmp_path: Path
    ) -> None:
        """--child-test is a probe, so its stamp must stay out of last_run /
        result — the only two fields the recency guard reads."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ):
            h.run(child_test=True)
        stamp = json.loads(h.stamp("alpha").read_text())
        assert "last_run" not in stamp
        assert "result" not in stamp

    def test_child_test_does_not_suppress_the_next_real_dream(
        self, tmp_path: Path
    ) -> None:
        """The whole point of the probe: an operator running --child-test at
        noon must not make the 20-hour guard swallow that night's REAL dream.

        With the parent recording a child test as a genuine last_run, the
        second (real) run below is skipped by the recency guard and never
        spawns — the persona silently loses its dream for the night."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ) as probe:
            h.run(child_test=True)
        probe.assert_called_once()

        h.fresh_child_state("alpha")
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ) as real_spawn:
            h.run()
        real_spawn.assert_called_once()
        assert json.loads(h.stamp("alpha").read_text())["result"] == "success"

    def test_child_test_reads_back_no_receipt(self, tmp_path: Path) -> None:
        """A child --test writes nothing, so there is nothing to read back.
        Recording a leftover state file as this probe's receipt would be the
        same false-receipt bug in a different coat."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        h.write_child_state("alpha", {"result": "consolidated", "last_run": "old"})
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ), patch("persona_dream_tick.read_child_dream_receipt") as read_back:
            h.run(child_test=True)
        read_back.assert_not_called()
        assert "dream_state" not in json.loads(h.stamp("alpha").read_text())

    def test_child_test_failure_is_still_a_failure(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(False, "exit 1")
        ):
            outcome = h.run(child_test=True)
        assert outcome.failed == ("alpha",)
        assert outcome.exit_code == 1
        assert (
            json.loads(h.stamp("alpha").read_text())["last_test_result"]
            == "child_test_failed"
        )

    def test_configured_days_and_timeout_reach_the_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PERSONA_DREAM_DAYS", "3")
        monkeypatch.setenv("PERSONA_DREAM_TIMEOUT", "42")
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ) as spawn:
            h.run()
        assert spawn.call_args.kwargs["days"] == 3
        assert spawn.call_args.kwargs["timeout_seconds"] == 42.0


# ============================================================================
# E. Shared-state collision guard
# ============================================================================


class TestStateCollisionGuard:
    def test_refuses_when_child_state_is_the_main_state(
        self, tmp_path: Path, capsys
    ) -> None:
        """If a persona's dream-state.json ever resolved onto the MAIN one, the
        child would clobber the default profile's recency guard and belief
        receipt. Caught by physical path comparison BEFORE the spawn."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        collide = h.main_dream_state.parent

        with patch("persona_dream_tick.is_active_default_profile", return_value=True), \
             patch("persona_dream_tick.list_profiles", return_value=list(h.profiles)), \
             patch("persona_dream_tick.STATE_DIR", h.main_state), \
             patch("persona_dream_tick.DREAM_STATE_FILE", h.main_dream_state), \
             patch(
                 "persona_dream_tick.get_persona_paths",
                 side_effect=lambda n: {"state": collide},
             ), \
             patch("persona_dream_tick._spawn_persona_dream") as spawn:
            from persona_dream_tick import run_tick

            run_tick()

        spawn.assert_not_called()
        assert "REFUSING to spawn" in capsys.readouterr().out
        assert (
            json.loads(h.stamp("alpha").read_text())["result"]
            == "refused_state_collision"
        )

    def test_distinct_profile_states_do_not_collide(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha", "beta"])
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ) as spawn:
            h.run()
        assert spawn.call_count == 2


# ============================================================================
# F. read_child_dream_receipt (Rule 2 read-back)
# ============================================================================


def _read_receipt(state_dir: Path, payload: object | None, *, spawn_id: str = "S1"):
    """Call the real read-back against a hand-written child state file."""
    from persona_dream_tick import read_child_dream_receipt

    state_dir.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (state_dir / "dream-state.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )
    with patch(
        "persona_dream_tick.get_persona_paths", return_value={"state": state_dir}
    ):
        return read_child_dream_receipt("alpha", spawn_id=spawn_id)


class TestReadChildDreamReceipt:
    def test_missing_file_reports_absent_with_path(self, tmp_path: Path) -> None:
        receipt = _read_receipt(tmp_path / "alpha" / "state", None)
        assert receipt["present"] is False
        assert receipt["status"] == "missing"
        assert receipt["path"].endswith("dream-state.json")

    def test_silent_result_is_reported_honestly(self, tmp_path: Path) -> None:
        """DREAM_SILENT is a real, acceptable outcome — not an error."""
        receipt = _read_receipt(
            tmp_path / "alpha" / "state",
            {
                "result": "DREAM_SILENT",
                "last_run": datetime.now().isoformat(),
                "spawn_id": "S1",
                "phases_completed": ["orient", "gather"],
            },
        )
        assert receipt["present"] is True
        assert receipt["status"] == "silent"
        assert receipt["result"] == "DREAM_SILENT"
        assert receipt["phases_completed"] == ["orient", "gather"]
        assert "belief_evolve_result" not in receipt

    def test_unreadable_state_fails_open_with_receipt(self, tmp_path: Path) -> None:
        from persona_dream_tick import read_child_dream_receipt

        with patch(
            "persona_dream_tick.get_persona_paths", side_effect=OSError("disk gone")
        ):
            receipt = read_child_dream_receipt("alpha", spawn_id="S1")
        assert receipt["present"] is False
        assert receipt["status"] == "unreadable"
        assert "disk gone" in receipt["error"]


# ============================================================================
# F2. THE RECEIPT CONTRACT — one test per truth-table cell (codex R4)
# ============================================================================
#
# Rounds 1-4 all found the same class of bug: a path answering "did this
# persona get its dream?" differently from the doctrine. The fix was to make
# RECEIPT_CONTRACT the single place that answers, so the test that matters is
# not "does case X behave" but "does EVERY row of the table hold end to end".
#
# Each case below drives the real run_tick with a fake child that produces
# exactly that physical condition, then asserts all three columns:
#   result           — what the parent stamped
#   budget consumed  — did last_run advance (does the guard now skip ~20h?)
#   failure          — did the tick tell the scheduler
#
# The budget column is the one that used to lie. A persona whose child never
# proved it ran must NOT have its night spent, or it silently loses that dream.


def _child_writer(payload: dict | str | None, *, success: bool = True):
    """A fake spawn that behaves like a real child: it receives the parent's
    nonce and leaves (or does not leave) a state file behind."""

    def _spawn(persona_name, profile_root, *, days, timeout_seconds,
               child_test=False, spawn_id=""):
        if payload is not None:
            state_dir = profile_root.parent / persona_name / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            body = payload
            if isinstance(body, dict):
                body = dict(body)
                # "MINE" means: this child echoed the nonce it was handed.
                if body.get("spawn_id") == "MINE":
                    body["spawn_id"] = spawn_id
                body = json.dumps(body)
            (state_dir / "dream-state.json").write_text(body, encoding="utf-8")
        return (success, "success" if success else "exit 1: boom")

    return _spawn


_NOW = datetime.now().isoformat()
_YESTERDAY = (datetime.now() - timedelta(days=1)).isoformat()
_FUTURE = (datetime.now() + timedelta(days=365 * 74)).isoformat()  # 2099

# (cell id, child payload, spawn ok, expected result, consumes budget, is failure)
_TRUTH_TABLE = [
    # --- ran, and proved it: budget spent, nobody paged ----------------------
    ("consolidated",
     {"result": "consolidated", "last_run": _NOW, "spawn_id": "MINE"},
     True, "success", True, False),
    ("silent",
     {"result": "DREAM_SILENT", "last_run": _NOW, "spawn_id": "MINE"},
     True, "success_silent", True, False),
    ("no_logs",
     {"result": "DREAM_SKIPPED", "last_run": _NOW, "spawn_id": "MINE"},
     True, "success_no_logs", True, False),
    ("killswitch",
     {"result": "skipped_killswitch", "last_run": _NOW, "spawn_id": "MINE"},
     True, "skipped_killswitch", True, False),
    # --- broke: no budget spent, scheduler told ------------------------------
    ("spawn_failed", None, False, "failed", False, True),
    ("child_failed",
     {"result": "failed", "last_run": _NOW, "spawn_id": "MINE"},
     True, "child_failed", False, True),
    ("missing", None, True, "no_receipt", False, True),
    ("unreadable", "{not json at all", True, "invalid_receipt", False, True),
    ("invalid_no_result", {}, True, "invalid_receipt", False, True),
    ("invalid_unknown_result",
     {"result": "bogus", "last_run": _NOW, "spawn_id": "MINE"},
     True, "invalid_receipt", False, True),
    ("future_dated",
     {"result": "consolidated", "last_run": _FUTURE, "spawn_id": "MINE"},
     True, "corrupt_receipt", False, True),
    # --- declined to run: no budget, not a fault ------------------------------
    ("stale_last_night",
     {"result": "consolidated", "last_run": _YESTERDAY, "spawn_id": "OTHER"},
     True, "stale_receipt", False, False),
    ("stale_untouched_this_spawn",
     {"result": "consolidated", "last_run": _NOW, "spawn_id": "OTHER"},
     True, "stale_receipt", False, False),
]


class TestReceiptContractTruthTable:
    @pytest.mark.parametrize(
        "cell,payload,spawn_ok,result,consumes_budget,is_failure",
        _TRUTH_TABLE,
        ids=[row[0] for row in _TRUTH_TABLE],
    )
    def test_cell(
        self,
        tmp_path: Path,
        cell: str,
        payload,
        spawn_ok: bool,
        result: str,
        consumes_budget: bool,
        is_failure: bool,
    ) -> None:
        from persona_dream_tick import RECEIPT_CONTRACT

        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch(
            "persona_dream_tick._spawn_persona_dream",
            side_effect=_child_writer(payload, success=spawn_ok),
        ):
            outcome = h.run()

        stamp = json.loads(h.stamp("alpha").read_text())
        assert stamp["result"] == result, f"{cell}: wrong stamp"
        assert ("last_run" in stamp) is consumes_budget, (
            f"{cell}: budget column violated — last_run "
            f"{'appeared' if 'last_run' in stamp else 'is missing'}"
        )
        assert stamp["last_attempt"], f"{cell}: attempt not recorded"
        assert (outcome.failed == ("alpha",)) is is_failure, f"{cell}: wrong failure"
        assert outcome.exit_code == (1 if is_failure else 0), f"{cell}: wrong exit"

        # …and the behavior above is the table, not a coincidence beside it.
        contract = RECEIPT_CONTRACT[stamp["status"]]
        assert (contract.result, contract.consumes_budget, contract.is_failure) == (
            result, consumes_budget, is_failure,
        ), f"{cell}: behavior and contract row disagree"

    def test_every_contract_row_has_a_cell(self) -> None:
        """The table cannot grow a row that nothing exercises. A new status
        without a cell here is a new path whose budget answer nobody checked —
        which is exactly how the last four rounds' bugs shipped."""
        from persona_dream_tick import RECEIPT_CONTRACT, STATUS_STAMP_ERROR

        covered = {
            "consolidated": "consolidated", "silent": "silent", "no_logs": "no_logs",
            "killswitch": "killswitch", "spawn_failed": "spawn_failed",
            "child_failed": "child_failed", "missing": "missing",
            "unreadable": "unreadable", "invalid_no_result": "invalid",
            "invalid_unknown_result": "invalid", "future_dated": "future_dated",
            "stale_last_night": "stale", "stale_untouched_this_spawn": "stale",
        }
        exercised = set(covered.values())
        # Driven by their own dedicated tests below (they need I/O sabotage or
        # a path collision rather than a child payload).
        exercised |= {"refused_collision", "state_path_error", STATUS_STAMP_ERROR}
        assert exercised == set(RECEIPT_CONTRACT), (
            f"uncovered contract rows: {set(RECEIPT_CONTRACT) - exercised}"
        )

    def test_no_status_answers_outside_the_table(self) -> None:
        """Every row states all three answers, and only 'ran and proved it'
        spends the budget."""
        from persona_dream_tick import RECEIPT_CONTRACT

        spends = {s for s, o in RECEIPT_CONTRACT.items() if o.consumes_budget}
        assert spends == {"consolidated", "silent", "no_logs", "killswitch"}
        # Nothing may both spend a persona's night AND be a failure.
        assert not [
            s for s, o in RECEIPT_CONTRACT.items() if o.consumes_budget and o.is_failure
        ]
        assert all(o.result and o.summary for o in RECEIPT_CONTRACT.values())


class TestContractCellsNeedingIoSabotage:
    """The three rows a child payload cannot produce."""

    def test_state_path_error(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha"])
        boom = MagicMock()
        boom.resolve.side_effect = OSError("path gone")
        with patch("persona_dream_tick._child_dream_state_file", return_value=boom), \
             patch("persona_dream_tick._spawn_persona_dream") as spawn:
            outcome = h.run()
        spawn.assert_not_called()
        stamp = json.loads(h.stamp("alpha").read_text())
        assert stamp["result"] == "state_path_error"
        assert "last_run" not in stamp
        assert outcome.exit_code == 1

    def test_refused_collision(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch(
            "persona_dream_tick._child_dream_state_file",
            return_value=h.main_dream_state,
        ), patch("persona_dream_tick._spawn_persona_dream") as spawn:
            outcome = h.run()
        spawn.assert_not_called()
        stamp = json.loads(h.stamp("alpha").read_text())
        assert stamp["result"] == "refused_state_collision"
        assert "last_run" not in stamp
        assert outcome.exit_code == 1

    def test_stamp_write_failure_is_a_failure_not_a_silent_success(
        self, tmp_path: Path
    ) -> None:
        """The outcome happened but could not be recorded. Reporting the write
        failure is what stops a full disk from reading as a quiet night."""
        h = _FanOutHarness(tmp_path, ["alpha"])
        with patch(
            "persona_dream_tick._spawn_persona_dream",
            side_effect=_child_writer(
                {"result": "consolidated", "last_run": _NOW, "spawn_id": "MINE"}
            ),
        ), patch(
            "persona_dream_tick.save_state", side_effect=OSError("disk full")
        ):
            outcome = h.run()
        assert outcome.failed == ("alpha",)
        assert outcome.exit_code == 1


# ============================================================================
# F2b. Roster containment — one bad stamp is one bad persona (codex R4 MAJOR 3)
# ============================================================================


class TestStampIoIsContainedPerPersona:
    def test_unreadable_stamp_does_not_abort_the_roster(self, tmp_path: Path) -> None:
        """load_state only catches JSONDecodeError, so an ACL-denied stamp (or
        a directory where the file belongs) raised OSError inside the sort key
        — before the loop existed — and every later persona silently lost its
        night. The failure belongs to the one persona that owns the stamp."""
        h = _FanOutHarness(tmp_path, ["alpha", "beta", "gamma"])
        real_load = persona_dream_tick_module().load_state

        def _load(path):
            if path.name.endswith("persona-dream-alpha-state.json"):
                raise OSError("stamp unreadable")
            return real_load(path)

        with patch("persona_dream_tick.load_state", side_effect=_load), patch(
            "persona_dream_tick._spawn_persona_dream",
            side_effect=_child_writer(
                {"result": "consolidated", "last_run": _NOW, "spawn_id": "MINE"}
            ),
        ) as spawn:
            outcome = h.run()

        spawned = {c.args[0] for c in spawn.call_args_list}
        assert spawned == {"beta", "gamma"}, "the roster stopped at the bad stamp"
        assert outcome.failed == ("alpha",)
        assert outcome.exit_code == 1
        assert json.loads(h.stamp("beta").read_text())["result"] == "success"
        assert json.loads(h.stamp("gamma").read_text())["result"] == "success"

    def test_unreadable_stamp_persona_is_not_spawned(self, tmp_path: Path) -> None:
        """Without its stamp the parent cannot tell whether this persona already
        ran tonight, and could not record the result afterwards — so it is
        skipped rather than double-dreamed."""
        h = _FanOutHarness(tmp_path, ["alpha"])

        def _load(path):
            raise OSError("stamp unreadable")

        with patch("persona_dream_tick.load_state", side_effect=_load), patch(
            "persona_dream_tick._spawn_persona_dream"
        ) as spawn:
            outcome = h.run()
        spawn.assert_not_called()
        assert outcome.failed == ("alpha",)


def persona_dream_tick_module():
    import persona_dream_tick

    return persona_dream_tick


# ============================================================================
# F3. Aggregate status / exit code (codex R3 MAJOR 4)
# ============================================================================
#
# run_tick catches per-persona failures on purpose so one bad child cannot
# starve the rest of the roster. That is only safe if the failures are then
# REPORTED: the scheduled task's FAILED branch is unreachable while a night
# where every child crashed exits 0, exactly like a night where every persona
# was quiet.


class TestAggregateExitStatus:
    def test_failed_spawn_makes_the_tick_exit_nonzero(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha", "beta"])
        h.fresh_child_state("beta")
        with patch(
            "persona_dream_tick._spawn_persona_dream",
            side_effect=[(False, "crash"), (True, "success")],
        ):
            outcome = h.run()
        assert outcome.failed == ("alpha",)
        assert outcome.attempted == ("alpha", "beta")
        assert outcome.exit_code == 1

    def test_untrustworthy_receipt_counts_as_a_failure(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha"])
        h.write_child_state("alpha", {})
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ):
            outcome = h.run()
        assert outcome.failed == ("alpha",)
        assert outcome.exit_code == 1

    def test_refusal_counts_as_a_failure(self, tmp_path: Path) -> None:
        h = _FanOutHarness(tmp_path, ["alpha"])
        collide = h.main_dream_state.parent
        with patch("persona_dream_tick.is_active_default_profile", return_value=True), \
             patch("persona_dream_tick.list_profiles", return_value=list(h.profiles)), \
             patch("persona_dream_tick.STATE_DIR", h.main_state), \
             patch("persona_dream_tick.DREAM_STATE_FILE", h.main_dream_state), \
             patch(
                 "persona_dream_tick.get_persona_paths",
                 side_effect=lambda n: {"state": collide},
             ), \
             patch("persona_dream_tick._spawn_persona_dream"):
            from persona_dream_tick import run_tick

            outcome = run_tick()
        assert outcome.failed == ("alpha",)
        assert outcome.exit_code == 1

    def test_silent_skips_are_not_failures(self, tmp_path: Path) -> None:
        """A quiet night must stay exit 0: DREAM_SILENT, a persona with no logs
        to scan, a child that skipped on its own guard, and a kill switch are
        all normal.

        Note what left this list (codex R4 MAJOR 1): "a fresh persona with no
        state file at all" used to be counted a benign skip here, which is
        exactly how a missing receipt passed for a completed dream and still
        spent that persona's night. A persona with nothing to scan now SAYS so
        (the child writes DREAM_SKIPPED); a genuinely absent receipt is a
        failure."""
        h = _FanOutHarness(tmp_path, ["alpha", "beta", "gamma", "delta"])
        h.fresh_child_state("alpha", result="DREAM_SILENT")
        h.fresh_child_state("beta", result="DREAM_SKIPPED")
        h.write_child_state(
            "gamma",
            {
                "result": "consolidated",
                "last_run": (datetime.now() - timedelta(days=1)).isoformat(),
            },
        )
        h.fresh_child_state("delta", result="skipped_killswitch")
        with patch(
            "persona_dream_tick._spawn_persona_dream", return_value=(True, "success")
        ):
            outcome = h.run()
        assert outcome.failed == ()
        assert outcome.exit_code == 0
        results = {
            n: json.loads(h.stamp(n).read_text())["result"]
            for n in ("alpha", "beta", "gamma", "delta")
        }
        assert results == {
            "alpha": "success_silent",
            "beta": "success_no_logs",
            "gamma": "stale_receipt",
            "delta": "skipped_killswitch",
        }

    def test_guard_early_returns_exit_zero(self, tmp_path: Path) -> None:
        from persona_dream_tick import run_tick

        with patch("persona_dream_tick.is_active_default_profile", return_value=False):
            assert run_tick().exit_code == 0

    def test_entrypoint_exits_with_the_outcome_code(self) -> None:
        """The wrapper scripts' FAILED branch is only reachable if __main__
        actually propagates the aggregate into the process exit code."""
        src = _TICK_SRC[_TICK_SRC.index('__name__ == "__main__"'):]
        assert "sys.exit(outcome.exit_code)" in src


# ============================================================================
# G. _spawn_persona_dream
# ============================================================================


class TestSpawn:
    def test_command_shape_carries_profile_and_days(self, tmp_path: Path) -> None:
        from persona_dream_tick import _spawn_persona_dream

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch(
            "persona_dream_tick.build_capability_scoped_env", return_value={"HOMIE_HOME": "x"}
        ), patch("persona_dream_tick.subprocess.run", return_value=completed) as run:
            ok, msg = _spawn_persona_dream(
                "alpha", tmp_path, days=5, timeout_seconds=30.0
            )
        assert (ok, msg) == (True, "success")
        cmd = run.call_args.args[0]
        assert cmd[1].endswith("memory_dream.py")
        assert cmd[2:] == ["-p", "alpha", "--days", "5"]
        assert "--test" not in cmd
        assert run.call_args.kwargs["timeout"] == 30.0
        assert run.call_args.kwargs["env"] == {"HOMIE_HOME": "x"}

    def test_child_test_appends_test_and_no_llm_flags(self, tmp_path: Path) -> None:
        """codex R3 MAJOR 2 (cost half): a probe must be free as well as
        harmless.

        ``--test`` alone is still a REAL dry run — it calls the LLM twice per
        signal-bearing persona so an operator can see what the dream would
        write. Fanning that across a 28-persona roster to prove the plumbing
        works bills a full night's tokens. ``--no-llm`` stops after the free
        phases, which is all a fan-out probe was ever asking about."""
        from persona_dream_tick import _spawn_persona_dream

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch(
            "persona_dream_tick.build_capability_scoped_env", return_value={}
        ), patch("persona_dream_tick.subprocess.run", return_value=completed) as run:
            _spawn_persona_dream(
                "alpha", tmp_path, days=7, timeout_seconds=30.0, child_test=True
            )
        cmd = run.call_args.args[0]
        assert cmd[-2:] == ["--test", "--no-llm"], cmd

    def test_real_run_carries_neither_probe_flag(self, tmp_path: Path) -> None:
        """The inverse guard: a nightly spawn must never inherit the probe
        flags, or the whole fan-out would silently stop dreaming."""
        from persona_dream_tick import _spawn_persona_dream

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch(
            "persona_dream_tick.build_capability_scoped_env", return_value={}
        ), patch("persona_dream_tick.subprocess.run", return_value=completed) as run:
            _spawn_persona_dream(
                "alpha", tmp_path, days=7, timeout_seconds=30.0, child_test=False
            )
        cmd = run.call_args.args[0]
        assert "--test" not in cmd and "--no-llm" not in cmd, cmd

    def test_env_build_failure_never_spawns(self, tmp_path: Path) -> None:
        """A capability-env failure must not fall back to the PARENT env — that
        would run a persona's dream with the default profile's secrets."""
        from persona_dream_tick import _spawn_persona_dream

        with patch(
            "persona_dream_tick.build_capability_scoped_env",
            side_effect=RuntimeError("matrix broken"),
        ), patch("persona_dream_tick.subprocess.run") as run:
            ok, msg = _spawn_persona_dream(
                "alpha", tmp_path, days=7, timeout_seconds=30.0
            )
        run.assert_not_called()
        assert ok is False
        assert "matrix broken" in msg

    def test_timeout_is_reported_not_raised(self, tmp_path: Path) -> None:
        from persona_dream_tick import _spawn_persona_dream

        with patch("persona_dream_tick.build_capability_scoped_env", return_value={}), \
             patch(
                 "persona_dream_tick.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd="x", timeout=900),
             ):
            ok, msg = _spawn_persona_dream(
                "alpha", tmp_path, days=7, timeout_seconds=900.0
            )
        assert ok is False
        assert "timeout (900s)" == msg

    def test_nonzero_exit_carries_stderr_tail(self, tmp_path: Path) -> None:
        from persona_dream_tick import _spawn_persona_dream

        completed = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="x" * 900 + "BOOM"
        )
        with patch("persona_dream_tick.build_capability_scoped_env", return_value={}), \
             patch("persona_dream_tick.subprocess.run", return_value=completed):
            ok, msg = _spawn_persona_dream(
                "alpha", tmp_path, days=7, timeout_seconds=30.0
            )
        assert ok is False
        assert msg.startswith("exit 2:")
        assert msg.endswith("BOOM")
        assert len(msg) < 600


# ============================================================================
# H. Isolation (acceptance criteria)
# ============================================================================


class TestIsolation:
    def test_each_profile_gets_its_own_dream_state_path(self) -> None:
        """The no-shared-state assertion the ticket asks for, at the resolver
        level: two personas and the main profile address three distinct files."""
        from personas import get_default_paths, get_persona_paths

        alpha = get_persona_paths("alpha")["state"] / "dream-state.json"
        beta = get_persona_paths("beta")["state"] / "dream-state.json"
        main = get_default_paths()["state"] / "dream-state.json"
        assert len({alpha, beta, main}) == 3
        assert alpha.parent != main.parent

    def test_real_dream_under_p_writes_only_inside_that_persona(
        self, tmp_path: Path
    ) -> None:
        """The ticket's acceptance test, run for real: seed main + A + B under
        one isolated Homie root, drive the ACTUAL ``memory_dream.py -p A``
        entrypoint in a subprocess (boot-shim, argparse, run_dream, all five
        phases, real evidence gate + floor), then prove A got an accepted
        receipt while B and main are byte-identical.

        Only the LLM lane and the two non-blocking post-steps are stubbed —
        the same seams test_memory_dream.py stubs — so no live call, no cost,
        no flake. Everything that decides WHERE a byte lands (the shim, config
        re-rooting, the amendment ledger, the decision-artifact dir) is the
        real thing. The previous version of this test wrote persona A's files
        by hand and asserted B was unchanged, so it passed without executing a
        single line of production code.
        """
        homie_root = tmp_path / "homie"

        # The DEFAULT (main) tree for this root — what a leak would land in.
        main_mem = homie_root / "memory"
        main_state = homie_root / "state"
        (main_mem / "daily").mkdir(parents=True)
        main_state.mkdir(parents=True)
        (main_mem / "MEMORY.md").write_text("# Main MEMORY\n", encoding="utf-8")
        (main_mem / "SELF.md").write_text("# Main SELF\n", encoding="utf-8")
        (main_state / "dream-state.json").write_text(
            json.dumps({"result": "consolidated", "last_run": "2026-01-01T00:00:00"}),
            encoding="utf-8",
        )

        # Persona B — the sibling that must not move.
        beta_mem = homie_root / "profiles" / "beta" / "memory"
        beta_state = homie_root / "profiles" / "beta" / "state"
        (beta_mem / "daily").mkdir(parents=True)
        beta_state.mkdir(parents=True)
        (beta_mem / "SELF.md").write_text("# Beta SELF\n", encoding="utf-8")
        (beta_state / "dream-state.json").write_text(
            json.dumps({"result": "DREAM_SILENT", "last_run": "2026-01-01T00:00:00"}),
            encoding="utf-8",
        )

        # Persona A — the one that actually dreams.
        alpha_root = homie_root / "profiles" / "alpha"
        alpha_mem = alpha_root / "memory"
        alpha_mem.mkdir(parents=True)
        (alpha_mem / "concepts").mkdir()
        (alpha_mem / "MEMORY.md").write_text("# Alpha MEMORY\n", encoding="utf-8")
        (alpha_mem / "SELF.md").write_text("# Alpha SELF\n", encoding="utf-8")
        (alpha_mem / "GOALS.md").write_text("# Alpha GOALS\n", encoding="utf-8")
        log_path = _seed_persona_dream_daily_log(alpha_mem)
        candidate_block = _persona_belief_candidate_block(f"daily/{log_path.name}")

        main_hash = _dir_hash(homie_root / "memory"), _dir_hash(main_state)
        beta_hash = _dir_hash(beta_mem), _dir_hash(beta_state)

        driver = tmp_path / "drive_persona_dream.py"
        driver.write_text(
            "import json, sys\n"
            f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})\n"
            # Exactly the command line the tick spawns.
            'sys.argv = ["memory_dream.py", "-p", "alpha", "--force", "--days", "7"]\n'
            "from unittest.mock import AsyncMock, MagicMock, patch\n"
            "import memory_dream\n"
            "def _res(text):\n"
            "    r = MagicMock(); r.text = text; r.provider = 'mock'\n"
            "    r.model = 'mock-model'; r.cost_usd = 0.0\n"
            "    return r\n"
            f"consolidation = 'Merged signal.\\n' + {candidate_block!r}\n"
            "rwf = AsyncMock(side_effect=[_res(consolidation), _res('PRUNE_OK')])\n"
            "judge = AsyncMock(return_value={'supported': True, 'correctness': 1.0,\n"
            "    'evidence_fidelity': 1.0, 'reason': 'matches the cited daily log'})\n"
            "with patch('runtime.lane_router.run_with_runtime_lanes', rwf), \\\n"
            "     patch('evolve.judge.judge_belief_candidate', judge), \\\n"
            "     patch('memory_dream._run_entity_compilation'), \\\n"
            "     patch('memory_dream._run_reindex'):\n"
            "    memory_dream.main()\n"
            "import config\n"
            "print('RESOLVED=' + json.dumps({\n"
            "    'dream_state': str(config.DREAM_STATE_FILE),\n"
            "    'memory_dir': str(config.MEMORY_DIR)}))\n",
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["HOMIE_HOME"] = str(homie_root)
        env.pop("HOMIE_VAULT_DIR", None)
        env["DREAM_SIGNAL_THRESHOLD"] = "1"
        env["EVOLVE_ENABLED"] = "true"
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.run(
            [sys.executable, str(driver)],
            cwd=str(_SCRIPTS_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]

        resolved = json.loads(
            [ln for ln in proc.stdout.splitlines() if ln.startswith("RESOLVED=")][-1]
            .split("=", 1)[1]
        )
        assert resolved["dream_state"].startswith(str(alpha_root.resolve()))
        assert resolved["memory_dir"].startswith(str(alpha_root.resolve()))

        # A's receipt — written by the dream itself, not by this test.
        alpha_dream_state = alpha_root / "state" / "dream-state.json"
        assert alpha_dream_state.exists(), proc.stdout[-3000:]
        state = json.loads(alpha_dream_state.read_text(encoding="utf-8"))
        assert state["result"] == "consolidated", state
        assert state["belief_evolve"]["adopted"] == 1, state["belief_evolve"]
        assert b"profile-scoped" in (alpha_mem / "SELF.md").read_bytes()
        assert list((alpha_root / "data" / "evolve" / "belief").glob("decision-*.json"))

        # …and nothing moved anywhere else.
        assert (_dir_hash(homie_root / "memory"), _dir_hash(main_state)) == main_hash, \
            "the main tree changed during a persona's dream"
        assert (_dir_hash(beta_mem), _dir_hash(beta_state)) == beta_hash, \
            "the sibling persona's tree changed during another persona's dream"

    def test_config_paths_physically_reroot_in_a_real_subprocess(
        self, tmp_path: Path
    ) -> None:
        """The load-bearing claim of the whole ticket, proven end-to-end.

        No dream internals were ported because ``memory_dream.py`` imports its
        path constants AFTER the boot-shim. This spawns a REAL interpreter with
        a profile root set exactly as ``build_capability_scoped_env`` sets it,
        and asserts the dream's state/vault/ledger constants land inside that
        root. It fails if config ever binds a path above the shim.
        """
        profile_root = tmp_path / "spikeprofile"
        (profile_root / "state").mkdir(parents=True)
        (profile_root / "memory").mkdir(parents=True)

        env = dict(os.environ)
        env["HOMIE_HOME"] = str(profile_root)
        env.pop("HOMIE_VAULT_DIR", None)
        code = (
            "import personas, json;"
            "personas.apply_persona_override();"
            "import config;"
            "print(json.dumps({"
            "'dream_state': str(config.DREAM_STATE_FILE),"
            "'memory_dir': str(config.MEMORY_DIR),"
            "'self_file': str(config.SELF_FILE),"
            "'ledger': str(config.AMENDMENT_LEDGER_FILE),"
            "'belief_decisions': str(config.BELIEF_EVOLVE_DECISION_DIR),"
            "'project_root': str(config.PROJECT_ROOT)}))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_SCRIPTS_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        paths = json.loads(proc.stdout.strip().splitlines()[-1])

        root = str(profile_root.resolve())
        for key in ("dream_state", "memory_dir", "self_file", "ledger", "belief_decisions"):
            assert paths[key].startswith(root), f"{key} did not re-root: {paths[key]}"
        assert paths["dream_state"].endswith("dream-state.json")
        # PROJECT_ROOT is the code checkout and must NOT follow the vault.
        assert not paths["project_root"].startswith(root)


# ============================================================================
# I. Phase 5 belief chain on a persona vault (spike-gate proof, codex R2)
# ============================================================================
#
# `test_config_paths_physically_reroot_in_a_real_subprocess` (above) only
# proves path re-rooting; it never calls run_dream/_run_belief_evolution_phase/
# propose_belief. The PRD's spike decision rule (Dream Phase 5 under -p) is:
# "chain exercises cleanly -> Phase 5 ON for personas." These two tests ARE
# that chain, made permanent and deterministic (fake judge reasoning — same
# injection seam test_living_self_act4.py uses — so no live LLM call, no
# flake, no cost), proving BOTH branches acceptance metric 6b asks for
# ("adopted OR rejected") against a persona-scoped memory_dir.


def _phase5_fake_reasoning(parsed: dict):
    from types import SimpleNamespace

    async def _r(context, instruction, output_schema=None, cwd=None):
        return SimpleNamespace(parsed=parsed, model="fake", cost_usd=0.0)

    return _r


class TestPhase5BeliefChainOnPersonaVault:
    def test_propose_belief_adopts_on_a_persona_vault(self, tmp_path: Path) -> None:
        """The full rail — evidence gate + deterministic floor + judge + decision
        artifact — exercised end-to-end against a PERSONA vault (not the main
        one), reaching a real ADOPT. Proves the chain the spike claimed to
        verify actually holds, permanently and without a live LLM call."""
        import asyncio

        import config as _config_mod
        from evolve import evolve_loop as el

        mem = tmp_path / "profiles" / "crypto" / "memory"
        (mem / "daily").mkdir(parents=True)
        (mem / "daily" / "2026-08-11.md").write_text(
            "the persona dream tick keeps dream state profile-scoped, "
            "observed across sessions",
            encoding="utf-8",
        )
        (mem / "SELF.md").write_text("# SELF\n", encoding="utf-8")

        monkeypatch_targets = {
            "AMENDMENT_LEDGER_FILE": tmp_path / "ledger.jsonl",
            "BELIEF_EVOLVE_DECISION_DIR": tmp_path / "profiles" / "crypto" / "data" / "evolve" / "belief",
        }
        originals = {k: getattr(_config_mod, k) for k in monkeypatch_targets}
        for k, v in monkeypatch_targets.items():
            setattr(_config_mod, k, v)
        try:
            candidate = {
                "source": "memory_dream",
                "target_file": "SELF.md",
                "summary": "PersonaDream state stays profile-scoped",
                "rationale": "the persona's own daily log records this",
                "evidence_paths": ["daily/2026-08-11.md"],
                "proposed_content": (
                    "- PersonaDream ticks keep dream state profile-scoped, "
                    "observed across sessions."
                ),
                "confidence_score": 0.9,
            }
            judge_yes = _phase5_fake_reasoning(
                {"supported": True, "correctness": 0.9, "evidence_fidelity": 0.85}
            )
            self_before = (mem / "SELF.md").read_bytes()
            result = asyncio.run(
                el.propose_belief(
                    candidate, dry_run=True, memory_dir=mem, reasoning=judge_yes
                )
            )
            self_after = (mem / "SELF.md").read_bytes()

            assert result["evidence_ok"] is True
            assert result["outcome"] == "adopt"
            # dry_run=True must never touch the target file.
            assert self_after == self_before
            decision_dir = _config_mod.BELIEF_EVOLVE_DECISION_DIR
            assert decision_dir.exists()
            assert list(decision_dir.glob("decision-*.json")), (
                "propose_belief must write a decision artifact even under dry_run"
            )
        finally:
            for k, v in originals.items():
                setattr(_config_mod, k, v)

    def test_propose_belief_rejects_cross_vault_evidence_on_a_persona_vault(
        self, tmp_path: Path
    ) -> None:
        """A candidate citing evidence OUTSIDE the persona vault (traversal to a
        sibling profile / the main tree) must be REJECTED by the confined
        evidence gate — even at max confidence and even with a judge that
        would otherwise say yes. Proves the OTHER branch of metric 6b
        ("adopted OR rejected") and that persona-vault confinement holds
        inside the full propose_belief rail, not just the resolver-level
        assertions in TestIsolation."""
        import asyncio

        import config as _config_mod
        from evolve import evolve_loop as el

        mem = tmp_path / "profiles" / "crypto" / "memory"
        mem.mkdir(parents=True)
        (mem / "SELF.md").write_text("# SELF\n", encoding="utf-8")
        # a secret OUTSIDE the persona vault the traversal would target
        outside = tmp_path / "outside_secret.txt"
        outside.write_text("SECRET profile-scoped dream state", encoding="utf-8")

        monkeypatch_targets = {
            "AMENDMENT_LEDGER_FILE": tmp_path / "ledger.jsonl",
            "BELIEF_EVOLVE_DECISION_DIR": tmp_path / "profiles" / "crypto" / "data" / "evolve" / "belief",
        }
        originals = {k: getattr(_config_mod, k) for k in monkeypatch_targets}
        for k, v in monkeypatch_targets.items():
            setattr(_config_mod, k, v)
        try:
            candidate = {
                "source": "memory_dream",
                "target_file": "SELF.md",
                "summary": "cross-vault escape",
                "rationale": "cites a path outside the persona vault",
                "evidence_paths": ["../outside_secret.txt", "../../outside_secret.txt"],
                "proposed_content": "- I read the outside secret and confirmed it.",
                "confidence_score": 0.99,
            }
            judge_yes = _phase5_fake_reasoning(
                {"supported": True, "correctness": 0.99, "evidence_fidelity": 0.99}
            )
            result = asyncio.run(
                el.propose_belief(
                    candidate, dry_run=True, memory_dir=mem, reasoning=judge_yes
                )
            )

            assert result["evidence_ok"] is False
            assert result["outcome"] == "reject"
            assert result["adopt"] is False
        finally:
            for k, v in originals.items():
                setattr(_config_mod, k, v)


# ============================================================================
# J. run_dream / _run_belief_evolution_phase — the WRAPPER, not just
#    propose_belief (reconcile round 2, finding "Phase-5 spike skipped")
# ============================================================================
#
# TestPhase5BeliefChainOnPersonaVault (above) proves the propose_belief rail
# itself (evidence gate -> floor -> judge -> artifact) holds on a persona
# vault. It never calls run_dream/_run_belief_evolution_phase — the
# orchestration layer that (a) harvests belief_candidate blocks out of the
# Phase-3 LLM response via extract_belief_candidates, and (b) checks
# HOMIE_KILLSWITCH_BELIEF_AUTONOMY BEFORE any candidate is ever looked at.
# Neither of those is exercised anywhere in the repo for ANY vault (main or
# persona) — grep confirms _run_belief_evolution_phase has exactly one
# non-comment call site (memory_dream.py itself) before these tests existed.
# These two tests call the real entrypoint end-to-end on a persona-SHAPED
# vault (profiles/<name>/... instead of vault/memory), reusing the same
# mocked-LLM-lane-router harness test_memory_dream.py's own Phase-5 tests use
# for the main homie (patch("runtime.lane_router.run_with_runtime_lanes", ...)
# + patch("evolve.judge.judge_belief_candidate", ...)) — no live LLM call, no
# flake, no cost, and it is the actual production code path, not a stand-in.


def _fake_llm_result(text: str = "CONSOLIDATION_OK"):
    """Same shape as test_memory_dream.py's _make_llm_result — a MagicMock
    standing in for the run_with_runtime_lanes RuntimeResponse."""
    result = MagicMock()
    result.text = text
    result.provider = "mock"
    result.model = "mock-model"
    result.cost_usd = 0.001
    return result


def _seed_persona_dream_daily_log(mem: Path) -> Path:
    """Yesterday-dated daily log with enough signal weight (a correction AND
    a save — the exact trigger phrases proven in test_memory_dream.py's own
    mock_daily_logs fixture) to clear DREAM_SIGNAL_THRESHOLD, and phrased so
    it also passes as in-vault evidence for the belief candidate below."""
    tz_now = datetime.now(timezone.utc)
    yesterday = (tz_now - timedelta(days=1)).strftime("%Y-%m-%d")
    daily_dir = mem / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    log_path = daily_dir / f"{yesterday}.md"
    log_path.write_text(
        "# Daily Log\n\n## Sessions\n\n### Session (14:00)\n\n"
        "Actually, the approach was wrong — the persona dream tick keeps "
        "dream state profile-scoped, observed across sessions.\n\n"
        "Key decision: persona dream state never touches the main vault.\n",
        encoding="utf-8",
    )
    return log_path


def _persona_belief_candidate_block(evidence_rel_path: str) -> str:
    return json.dumps({
        "kind": "belief_candidate",
        "target_file": "SELF.md",
        "summary": "persona dream state is profile-scoped",
        "evidence_paths": [evidence_rel_path],
        "proposed_content": (
            "- PersonaDream ticks keep dream state profile-scoped, "
            "observed across sessions."
        ),
        "confidence_score": 0.9,
    })


def _patch_dream_on_persona_shaped_vault(mem: Path, tmp_path: Path):
    """Same shape as test_memory_dream.py's _patch_dream, but rooted under a
    profiles/<name>/... tree instead of vault/memory — proves the wrapper
    holds on PERSONA-shaped paths, not just a re-used main-vault fixture.

    Two independent name bindings must be patched to the SAME persona-shaped
    paths: memory_dream.py's own top-level ``from config import ...`` (what
    Phases 1-4 read) AND evolve_loop.propose_belief's fresh per-call
    ``from config import MEMORY_DIR, AMENDMENT_LEDGER_FILE, ...`` (what Phase
    5's evidence gate + ledger apply actually resolve against). Patching only
    memory_dream's copy silently leaves propose_belief reading the REAL
    default-profile config.MEMORY_DIR — this is the exact class of bug the
    live ``-p`` boot-shim avoids by re-rooting config.py at import time,
    before either module binds its own copy."""
    import contextlib

    ledger_file = tmp_path / "state" / "amendment-proposals.jsonl"
    decision_dir = tmp_path / "data" / "evolve" / "belief"

    @contextlib.contextmanager
    def _ctx():
        with patch("memory_dream.DREAM_STATE_FILE", tmp_path / "state" / "dream-state.json"), \
             patch("memory_dream.MEMORY_FILE", mem / "MEMORY.md"), \
             patch("memory_dream.MEMORY_DIR", mem), \
             patch("memory_dream.DAILY_DIR", mem / "daily"), \
             patch("memory_dream.SELF_FILE", mem / "SELF.md"), \
             patch("memory_dream.GOALS_FILE", mem / "GOALS.md"), \
             patch("memory_dream.STATE_DIR", tmp_path / "state"), \
             patch("memory_dream.AMENDMENT_LEDGER_FILE", ledger_file), \
             patch("memory_dream.DREAM_SIGNAL_THRESHOLD", 1), \
             patch("config.MEMORY_DIR", mem), \
             patch("config.AMENDMENT_LEDGER_FILE", ledger_file), \
             patch("config.BELIEF_EVOLVE_DECISION_DIR", decision_dir):
            yield

    return _ctx()


def _seed_persona_shaped_memory_dir(tmp_path: Path) -> Path:
    mem = tmp_path / "profiles" / "crypto" / "memory"
    mem.mkdir(parents=True)
    (mem / "concepts").mkdir()
    (mem / "MEMORY.md").write_text("# MEMORY\n", encoding="utf-8")
    (mem / "SELF.md").write_text("# SELF\n", encoding="utf-8")
    (mem / "GOALS.md").write_text("# GOALS\n", encoding="utf-8")
    return mem


class TestRunDreamBeliefEvolutionOnPersonaVault:
    @pytest.mark.asyncio
    async def test_run_dream_adopts_a_belief_on_a_persona_shaped_vault(
        self, tmp_path: Path
    ) -> None:
        """The real orchestration path — run_dream -> consolidate (mocked LLM)
        -> _run_belief_evolution_phase -> extract_belief_candidates ->
        propose_belief -> real evidence gate + floor + (mocked) judge -> a
        genuine adopt that writes SELF.md and a decision artifact, all under
        a persona-shaped vault. Proves acceptance metric 6b's 'adopted'
        branch through the WRAPPER, not a direct propose_belief call."""
        mem = _seed_persona_shaped_memory_dir(tmp_path)
        log_path = _seed_persona_dream_daily_log(mem)
        candidate_block = _persona_belief_candidate_block(f"daily/{log_path.name}")

        mock_rwf = AsyncMock(side_effect=[
            _fake_llm_result("Merged signal.\n" + candidate_block),
            _fake_llm_result("PRUNE_OK"),
        ])
        adopt_judge = AsyncMock(return_value={
            "supported": True,
            "correctness": 1.0,
            "evidence_fidelity": 1.0,
            "reason": "matches the cited daily log",
        })

        self_before = (mem / "SELF.md").read_bytes()
        with _patch_dream_on_persona_shaped_vault(mem, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("evolve.judge.judge_belief_candidate", adopt_judge), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(test_mode=False, force=True, days=7)

        assert result is not None
        state = json.loads(
            (tmp_path / "state" / "dream-state.json").read_text(encoding="utf-8")
        )
        assert state["result"] == "consolidated"
        belief_evolve = state["belief_evolve"]
        assert belief_evolve["result"] == "ran"
        assert belief_evolve["adopted"] == 1, belief_evolve

        self_after = (mem / "SELF.md").read_bytes()
        assert self_after != self_before
        assert b"profile-scoped" in self_after

        decision_dir = tmp_path / "data" / "evolve" / "belief"
        assert decision_dir.exists()
        assert list(decision_dir.glob("decision-*.json"))

    @pytest.mark.asyncio
    async def test_run_dream_belief_evolution_honors_kill_switch_on_persona_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the reconcile: Finding 1 fixed propagation of
        HOMIE_KILLSWITCH_BELIEF_AUTONOMY into a persona dream child's env;
        this proves that once the switch IS set, _run_belief_evolution_phase
        actually HONORS it on a persona vault — short-circuiting before
        extract_belief_candidates or the judge ever run, dream itself still
        reports success (Phase 5 disablement is operator intent, not a
        failure), and SELF.md/the decision dir are untouched."""
        monkeypatch.setenv("HOMIE_KILLSWITCH_BELIEF_AUTONOMY", "disabled")

        mem = _seed_persona_shaped_memory_dir(tmp_path)
        log_path = _seed_persona_dream_daily_log(mem)
        candidate_block = _persona_belief_candidate_block(f"daily/{log_path.name}")

        mock_rwf = AsyncMock(side_effect=[
            _fake_llm_result("Merged signal.\n" + candidate_block),
            _fake_llm_result("PRUNE_OK"),
        ])
        judge_must_not_be_called = AsyncMock()

        self_before = (mem / "SELF.md").read_bytes()
        with _patch_dream_on_persona_shaped_vault(mem, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("evolve.judge.judge_belief_candidate", judge_must_not_be_called), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(test_mode=False, force=True, days=7)

        assert result is not None
        state = json.loads(
            (tmp_path / "state" / "dream-state.json").read_text(encoding="utf-8")
        )
        # The dream cycle itself still succeeds — Phase 5 disablement is
        # operator intent, not a failure (matches the existing kill-switch
        # contract for the main homie).
        assert state["result"] == "consolidated"
        assert state["belief_evolve"]["result"] == "skipped_killswitch"
        judge_must_not_be_called.assert_not_called()

        assert (mem / "SELF.md").read_bytes() == self_before
        decision_dir = tmp_path / "data" / "evolve" / "belief"
        assert not decision_dir.exists() or not list(decision_dir.glob("decision-*.json"))
