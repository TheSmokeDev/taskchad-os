"""Persona capability matrix tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent

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
    assert "openai_dummy_value" not in repr(plan)


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


def test_capability_scoped_env_preserves_kill_switches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator's HOMIE_KILLSWITCH_* setting MUST reach every
    capability-scoped child (persona dream/learning/curriculum ticks) even
    though no matrix env_group ever declares it — a dropped kill switch reads
    as absent in the child, and absent means enabled (fail-open the wrong
    way for a safety control)."""
    homie_root = tmp_path / ".homie"
    profile_root = homie_root / "profiles" / "crypto"
    profile_root.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    master_env = tmp_path / ".env"
    master_env.write_text("OPENAI_API_KEY=allowed_dummy_value\n", encoding="utf-8")
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups:
  runtime_core: [OPENAI_API_KEY]
skill_groups: {}
profiles:
  crypto:
    env_groups: [runtime_core]
    skills: []
""",
    )

    env = build_capability_scoped_env(
        "crypto",
        profile_root=profile_root,
        parent_env={
            "PATH": "/bin",
            "HOMIE_KILLSWITCH_BELIEF_AUTONOMY": "disabled",
            "HOMIE_KILLSWITCH_COFOUNDER": "disabled",
            "HOMIE_KILLSWITCH_ZZTEST": "disabled",
        },
        matrix_path=matrix,
        master_env_path=master_env,
    )

    assert env["HOMIE_KILLSWITCH_BELIEF_AUTONOMY"] == "disabled"
    assert env["HOMIE_KILLSWITCH_COFOUNDER"] == "disabled"
    # A synthetic name nothing else knows about (design-gate note 2): the fix
    # must thread the CLASS by prefix, not a two-name allowlist that happens to
    # cover today's switches.
    assert env["HOMIE_KILLSWITCH_ZZTEST"] == "disabled"


def test_delegated_matrix_value_cannot_re_enable_a_disabled_kill_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex R4 MAJOR 4 — capability delegation must not outrank an emergency stop.

    A capability group may legitimately delegate a HOMIE_KILLSWITCH_* key from
    the master .env. That delegated value used to merge LAST, so it landed on
    top of the operator's live "disabled" and handed the child back the very
    capability the operator had just pulled the plug on — Phase 5 could go on
    mutating persona identity after belief autonomy was switched off.

    The directions are not symmetric: absent/enabled is permissive, "disabled"
    is the refusal, so the parent's disabled value has to win.
    """
    homie_root = tmp_path / ".homie"
    profile_root = homie_root / "profiles" / "crypto"
    profile_root.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    master_env = tmp_path / ".env"
    master_env.write_text(
        "HOMIE_KILLSWITCH_BELIEF_AUTONOMY=enabled\n"
        "OPENAI_API_KEY=allowed_dummy_value\n",
        encoding="utf-8",
    )
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups:
  runtime_core: [OPENAI_API_KEY, HOMIE_KILLSWITCH_BELIEF_AUTONOMY]
skill_groups: {}
profiles:
  crypto:
    env_groups: [runtime_core]
    skills: []
""",
    )

    env = build_capability_scoped_env(
        "crypto",
        profile_root=profile_root,
        parent_env={
            "PATH": "/bin",
            "HOMIE_KILLSWITCH_BELIEF_AUTONOMY": "disabled",
        },
        matrix_path=matrix,
        master_env_path=master_env,
    )

    assert env["HOMIE_KILLSWITCH_BELIEF_AUTONOMY"] == "disabled", (
        "a delegated matrix value overrode the operator's kill switch"
    )


def test_delegation_still_applies_when_no_switch_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inverse guard: the fail-closed pass may only ever WITHHOLD. With no
    disabled switch in the parent, a delegated value passes through untouched."""
    homie_root = tmp_path / ".homie"
    profile_root = homie_root / "profiles" / "crypto"
    profile_root.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    master_env = tmp_path / ".env"
    master_env.write_text(
        "HOMIE_KILLSWITCH_BELIEF_AUTONOMY=enabled\n"
        "OPENAI_API_KEY=allowed_dummy_value\n",
        encoding="utf-8",
    )
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups:
  runtime_core: [OPENAI_API_KEY, HOMIE_KILLSWITCH_BELIEF_AUTONOMY]
skill_groups: {}
profiles:
  crypto:
    env_groups: [runtime_core]
    skills: []
""",
    )

    env = build_capability_scoped_env(
        "crypto",
        profile_root=profile_root,
        parent_env={"PATH": "/bin"},
        matrix_path=matrix,
        master_env_path=master_env,
    )

    assert env["HOMIE_KILLSWITCH_BELIEF_AUTONOMY"] == "enabled"


def test_profile_dotenv_cannot_re_enable_a_disabled_kill_switch() -> None:
    """codex R4 MAJOR 4, second half — config loads the profile .env with
    override=True, so a profile could ship HOMIE_KILLSWITCH_X=enabled and undo
    the operator's stop in every process that imports config. Proven in a REAL
    subprocess, because the behavior under test is import-time."""
    import subprocess
    import sys as _sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "homie"
        (root / "profiles" / "zz").mkdir(parents=True)
        (root / "profiles" / "zz" / ".env").write_text(
            "HOMIE_KILLSWITCH_BELIEF_AUTONOMY=enabled\n", encoding="utf-8"
        )
        env = dict(os.environ)
        env["HOMIE_HOME"] = str(root / "profiles" / "zz")
        env["HOMIE_KILLSWITCH_BELIEF_AUTONOMY"] = "disabled"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [
                _sys.executable,
                "-c",
                "import config;"
                "from security import kill_switches;"
                "print('DISABLED=' + str(kill_switches.is_disabled('BELIEF_AUTONOMY')))",
            ],
            cwd=str(_SCRIPTS_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "DISABLED=True" in proc.stdout, proc.stdout[-2000:]


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


def test_compiled_profile_overlay_wins_without_mutating_shared_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homie_root = tmp_path / ".homie"
    profile = homie_root / "profiles" / "founder-operator"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    (profile / "config.yaml").write_text(
        """
capability_blueprint:
  schema_version: 1
  template: founder-operator
  domain: founder-operations
  domain_packs: [founder_operations]
  operator_exec: false
  env_groups: [business_profile]
  skill_groups: [founder]
  skills: [direct-skill]
  scheduled_authorities: []
""",
        encoding="utf-8",
    )
    matrix = _write_matrix(
        tmp_path / "matrix.yaml",
        """
env_groups:
  runtime_core: [OPENAI_API_KEY]
  business_profile: [BUSINESS_EMAIL]
skill_groups:
  founder: [market-research]
profile_defaults:
  env_groups: [runtime_core]
  skill_groups: []
  skills: []
profiles: {}
""",
    )

    assert resolve_env_keys(
        "founder-operator",
        matrix_path=matrix,
    ) == ["BUSINESS_EMAIL"]
    assert resolve_skill_allowlist(
        "founder-operator",
        matrix_path=matrix,
    ) == frozenset({"direct-skill", "market-research"})
