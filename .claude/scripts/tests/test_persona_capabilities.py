"""Persona capability matrix tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personas.capabilities import (
    CapabilityMatrixError,
    build_capability_scoped_env,
    build_env_sync_plan,
    resolve_env_keys,
    resolve_skill_allowlist,
    safe_env_sync_summary,
    write_profile_env,
)


def _write_matrix(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_resolve_env_keys_from_groups(tmp_path: Path) -> None:
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups:
  runtime_core: [OPENAI_API_KEY, OWNER_NAME]
skill_groups: {}
profiles:
  sales:
    env_groups: [runtime_core]
    skills: []
""",
    )

    keys = resolve_env_keys(
        "sales",
        matrix_path=matrix,
        master_keys=["OPENAI_API_KEY", "OWNER_NAME", "DISCORD_BOT_TOKEN"],
    )

    assert keys == ["OPENAI_API_KEY", "OWNER_NAME"]


def test_unknown_env_group_is_rejected(tmp_path: Path) -> None:
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups:
  runtime_core: [OPENAI_API_KEY]
skill_groups: {}
profiles:
  sales:
    env_groups: [missing_group]
    skills: []
""",
    )

    with pytest.raises(CapabilityMatrixError, match="unknown group"):
        resolve_env_keys("sales", matrix_path=matrix)


def test_env_sync_summary_never_contains_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homie_root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    (homie_root / "profiles" / "sales").mkdir(parents=True)
    master_env = tmp_path / ".env"
    master_env.write_text(
        "OPENAI_API_KEY=openai_dummy_value\nOWNER_NAME=Operator\n",
        encoding="utf-8",
    )
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups:
  runtime_core: [OPENAI_API_KEY, OWNER_NAME]
skill_groups: {}
profiles:
  sales:
    env_groups: [runtime_core]
    skills: []
""",
    )

    plan = build_env_sync_plan(
        "sales",
        matrix_path=matrix,
        master_env_path=master_env,
    )
    summary = safe_env_sync_summary(plan)
    rendered = json.dumps(summary)

    assert "openai_dummy_value" not in rendered
    assert "OPENAI_API_KEY" in summary["present_keys"]
    assert plan.values["OPENAI_API_KEY"] == "openai_dummy_value"


def test_write_profile_env_uses_derived_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homie_root = tmp_path / ".homie"
    profile_root = homie_root / "profiles" / "socials"
    profile_root.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    master_env = tmp_path / ".env"
    master_env.write_text(
        "X_API_KEY=x_dummy_value\nDISCORD_BOT_TOKEN=discord_dummy_value\n",
        encoding="utf-8",
    )
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups:
  socials_write: [X_API_KEY]
skill_groups: {}
profiles:
  socials:
    env_groups: [socials_write]
    skills: []
""",
    )

    plan = build_env_sync_plan("socials", matrix_path=matrix, master_env_path=master_env)
    output = write_profile_env(plan)
    text = output.read_text(encoding="utf-8")

    assert "X_API_KEY=x_dummy_value" in text
    assert "DISCORD_BOT_TOKEN" not in text


def test_capability_scoped_env_drops_unassigned_bot_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homie_root = tmp_path / ".homie"
    profile_root = homie_root / "profiles" / "browser_ops"
    profile_root.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    master_env = tmp_path / ".env"
    master_env.write_text(
        "OPENAI_API_KEY=allowed_dummy_value\nDISCORD_BOT_TOKEN=discord_dummy_value\n",
        encoding="utf-8",
    )
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups:
  runtime_core: [OPENAI_API_KEY]
skill_groups: {}
profiles:
  browser_ops:
    env_groups: [runtime_core]
    skills: []
""",
    )

    env = build_capability_scoped_env(
        "browser_ops",
        profile_root=profile_root,
        parent_env={
            "PATH": "/bin",
            "DISCORD_BOT_TOKEN": "parent_discord_dummy_value",
            "RANDOM_API_KEY": "parent_random_dummy_value",
        },
        matrix_path=matrix,
        master_env_path=master_env,
    )

    assert env["PATH"] == "/bin"
    assert env["OPENAI_API_KEY"] == "allowed_dummy_value"
    assert "DISCORD_BOT_TOKEN" not in env
    assert "RANDOM_API_KEY" not in env
    assert env["HOMIE_HOME"] == str(profile_root)


def test_skill_allowlist_resolves_groups_and_default_all(tmp_path: Path) -> None:
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups: {}
skill_groups:
  socials: [linkedin-post, x-post]
profiles:
  default:
    skill_groups: ["*"]
  socials:
    skill_groups: [socials]
    skills: [imagegen]
""",
    )

    assert resolve_skill_allowlist("default", matrix_path=matrix) is None
    assert resolve_skill_allowlist("socials", matrix_path=matrix) == frozenset(
        {"imagegen", "linkedin-post", "x-post"}
    )
