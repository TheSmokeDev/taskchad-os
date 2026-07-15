"""Focused tests for the Repo Scout persona seeder and capability grant."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from personas import capabilities as capability_mod
from personas import repo_scout as persona_mod
from security import kill_switches


@pytest.fixture(autouse=True)
def clean_persona_mutation_state(monkeypatch):
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


@pytest.fixture
def homie_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(root))
    return root


def _profile_root(homie_root: Path) -> Path:
    return homie_root / "profiles" / persona_mod.REPO_SCOUT_PERSONA_ID


def _read_config(homie_root: Path) -> dict:
    path = _profile_root(homie_root) / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_kill_switch_refuses_without_writes(monkeypatch, homie_root):
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")

    result = persona_mod.seed_repo_scout_persona()

    assert result.outcome == persona_mod.OUTCOME_REFUSED
    assert result.exit_code == 0
    assert not homie_root.exists()


def test_dry_run_reports_but_writes_nothing(homie_root):
    result = persona_mod.seed_repo_scout_persona(dry_run=True)

    assert result.outcome == persona_mod.OUTCOME_CREATED
    assert not homie_root.exists()


def test_fresh_seed_is_locked_down_and_learning_off(homie_root):
    result = persona_mod.seed_repo_scout_persona()

    assert result.outcome == persona_mod.OUTCOME_CREATED
    cfg = _read_config(homie_root)
    assert cfg["persona"]["id"] == "repo-scout"
    assert cfg["persona"]["display_name"] == "Repo Scout"
    assert cfg["persona"]["role"] == persona_mod.REPO_SCOUT_ROLE
    assert cfg["cabinet"]["tools"] == []
    assert cfg["learning"]["enabled"] is False
    assert cfg["delegation"]["repos"] == []

    memory = _profile_root(homie_root) / "memory"
    assert (memory / "SOUL.md").read_text(encoding="utf-8") == persona_mod.REPO_SCOUT_SOUL
    assert "NEVER executed" in persona_mod.REPO_SCOUT_SOUL
    assert (memory / "MEMORY.md").read_text(encoding="utf-8") == persona_mod.REPO_SCOUT_MEMORY


def test_operator_authored_soul_never_overwritten(homie_root):
    persona_mod.seed_repo_scout_persona()
    soul_path = _profile_root(homie_root) / "memory" / "SOUL.md"
    operator_text = "# SOUL.md\n\nOperator rewrote this on purpose.\n"
    soul_path.write_text(operator_text, encoding="utf-8")

    result = persona_mod.seed_repo_scout_persona()

    assert soul_path.read_text(encoding="utf-8") == operator_text
    assert "SOUL.md" not in result.identity_written


def test_existing_config_keys_untouched(homie_root):
    persona_mod.seed_repo_scout_persona()
    cfg_path = _profile_root(homie_root) / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["learning"]["enabled"] = True
    cfg["persona"]["role"] = "operator-customized role"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    result = persona_mod.seed_repo_scout_persona()

    refreshed = _read_config(homie_root)
    assert refreshed["learning"]["enabled"] is True
    assert refreshed["persona"]["role"] == "operator-customized role"
    assert not any(c.startswith("learning") for c in result.config_changes)


def test_matrix_grants_github_ops_and_validates():
    matrix = capability_mod.load_capability_matrix()
    capability_mod.validate_capability_matrix(matrix)

    assert "github_ops" in matrix["env_groups"]
    assert "GITHUB_TOKEN" in matrix["env_groups"]["github_ops"]

    profile = matrix["profiles"]["repo-scout"]
    assert "github_ops" in profile["env_groups"]
    assert profile["skill_groups"] == []
