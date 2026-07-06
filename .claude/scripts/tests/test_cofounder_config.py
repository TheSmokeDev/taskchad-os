"""US-001 — co-founder config resolver + kill switch contract tests.

Asserts:
  - get_cofounder_settings() knob defaults (all COFOUNDER_* env cleared)
  - env overrides picked up at CALL time after monkeypatch (Rule 1 proof —
    no module reload between calls)
  - explicit args pass through the None-sentinel (env ignored)
  - Rule 1 structural proof: every def-time default is None; no module-level
    COFOUNDER_* constant capture in config
  - kill switch 'cofounder': HOMIE_KILLSWITCH_COFOUNDER=disabled refuses via
    KillSwitchDisabled and increments the refusal counter; absent env allows
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from security import kill_switches

COFOUNDER_ENV_KEYS = (
    "COFOUNDER_ENABLED",
    "COFOUNDER_PROJECTS_DIR",
    "COFOUNDER_MAX_ITERATIONS",
    "COFOUNDER_MAX_WALL_CLOCK_HOURS",
    "COFOUNDER_MAX_CONCURRENT",
    "COFOUNDER_NOTIFY_LEVELS",
    "COFOUNDER_ZOMBIE_STALE_MINUTES",
    "COFOUNDER_ARCHON_DB",
    "COFOUNDER_WORKFLOW_PROVIDER",
    "COFOUNDER_WORKFLOW_MODEL",
)


@pytest.fixture(autouse=True)
def clear_cofounder_env(monkeypatch):
    """Each test starts with no COFOUNDER_* env (a live .env may set them)."""
    for key in COFOUNDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def reset_counters():
    """Each test starts with empty refusal counters."""
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


# === knob defaults ===


def test_defaults():
    settings = config.get_cofounder_settings()
    assert settings.enabled is False
    assert settings.projects_dir == config.MEMORY_DIR / "cofounder"
    assert settings.max_iterations == 50
    assert settings.max_wall_clock_hours == 72.0
    assert settings.max_concurrent == 2
    assert settings.notify_levels == ("done", "blocked", "awaiting-human")
    assert settings.zombie_stale_minutes == 60
    assert settings.archon_db == Path.home() / ".archon" / "archon.db"
    assert settings.workflow_provider == "claude"
    assert settings.workflow_model == "sonnet"


# === call-time env resolution (Rule 1 behavioral proof) ===


def test_env_override_picked_up_after_monkeypatch(monkeypatch, tmp_path):
    """monkeypatch.setenv takes effect on the NEXT call — no module reload."""
    before = config.get_cofounder_settings()
    assert before.enabled is False
    assert before.max_iterations == 50

    monkeypatch.setenv("COFOUNDER_ENABLED", "true")
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("COFOUNDER_MAX_ITERATIONS", "7")
    monkeypatch.setenv("COFOUNDER_MAX_WALL_CLOCK_HOURS", "1.5")
    monkeypatch.setenv("COFOUNDER_MAX_CONCURRENT", "5")
    monkeypatch.setenv("COFOUNDER_NOTIFY_LEVELS", "done")
    monkeypatch.setenv("COFOUNDER_ZOMBIE_STALE_MINUTES", "15")
    monkeypatch.setenv("COFOUNDER_ARCHON_DB", str(tmp_path / "archon.db"))
    monkeypatch.setenv("COFOUNDER_WORKFLOW_PROVIDER", "codex")
    monkeypatch.setenv("COFOUNDER_WORKFLOW_MODEL", "gpt-5.5")

    after = config.get_cofounder_settings()
    assert after.enabled is True
    assert after.projects_dir == tmp_path / "projects"
    assert after.max_iterations == 7
    assert after.max_wall_clock_hours == 1.5
    assert after.max_concurrent == 5
    assert after.notify_levels == ("done",)
    assert after.zombie_stale_minutes == 15
    assert after.archon_db == tmp_path / "archon.db"
    assert after.workflow_provider == "codex"
    assert after.workflow_model == "gpt-5.5"


def test_explicit_args_pass_through_sentinel(monkeypatch):
    """Explicit values win over env — None is the only resolve trigger."""
    monkeypatch.setenv("COFOUNDER_MAX_ITERATIONS", "99")
    monkeypatch.setenv("COFOUNDER_ENABLED", "true")
    settings = config.get_cofounder_settings(enabled=False, max_iterations=3)
    assert settings.enabled is False
    assert settings.max_iterations == 3


def test_notify_levels_parsing(monkeypatch):
    """Comma parse: spaces stripped, lowercased, empties dropped."""
    monkeypatch.setenv("COFOUNDER_NOTIFY_LEVELS", " Done , BLOCKED ,, awaiting-human ")
    settings = config.get_cofounder_settings()
    assert settings.notify_levels == ("done", "blocked", "awaiting-human")


def test_notify_levels_empty_string_disables_all(monkeypatch):
    monkeypatch.setenv("COFOUNDER_NOTIFY_LEVELS", "")
    settings = config.get_cofounder_settings()
    assert settings.notify_levels == ()


def test_notify_levels_accepts_iterable():
    settings = config.get_cofounder_settings(notify_levels=["Done", " blocked "])
    assert settings.notify_levels == ("done", "blocked")


def test_projects_dir_is_path(monkeypatch, tmp_path):
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(tmp_path))
    settings = config.get_cofounder_settings()
    assert isinstance(settings.projects_dir, Path)
    assert settings.projects_dir == tmp_path


# === Rule 1 structural proof ===


def test_rule1_all_def_time_defaults_are_none():
    """No config value is bound in func.__defaults__ — None sentinels only."""
    defaults = config.get_cofounder_settings.__defaults__
    assert defaults is not None
    assert all(d is None for d in defaults), (
        f"def-time default capture detected: {defaults}"
    )


def test_rule1_no_module_level_cofounder_constants():
    """config exposes no COFOUNDER_* module-level constant capture."""
    offenders = [
        name
        for name in dir(config)
        if name.startswith("COFOUNDER") and name != "CofounderSettings"
    ]
    assert offenders == [], (
        f"module-level COFOUNDER constants defeat call-time resolution: {offenders}"
    )


# === kill switch 'cofounder' ===


def test_kill_switch_absent_env_allows(monkeypatch):
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    assert kill_switches.requireEnabled("cofounder") is None
    assert "cofounder" not in kill_switches.get_refusal_counters()


def test_kill_switch_disabled_refuses_and_counts(monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    for expected_count in (1, 2, 3):
        with pytest.raises(kill_switches.KillSwitchDisabled) as exc_info:
            kill_switches.requireEnabled("cofounder", caller="test_cofounder_config")
        assert exc_info.value.switch_name == "cofounder"
        assert kill_switches.get_refusal_counters()["cofounder"] == expected_count


def test_kill_switch_reenable_works_without_restart(monkeypatch):
    """Toggling the env back re-allows on the next call (Rule 2 — no caching)."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        kill_switches.requireEnabled("cofounder")
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "enabled")
    assert kill_switches.requireEnabled("cofounder") is None
    # Counter keeps the historical refusal — it counts refusals, not state.
    assert kill_switches.get_refusal_counters()["cofounder"] == 1
