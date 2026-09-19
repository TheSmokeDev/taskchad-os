"""Deployment identity must not depend on which checkout executes a persona."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from personas import core
from personas.learning.models import resolve_learning_target
from personas.worker_environment import bootstrap_scoped_worker


@pytest.fixture(autouse=True)
def clean_selection(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in (
        "HOMIE_HOME",
        "HOMIE_NAME",
        "HOMIE_DEFAULT_PROFILE_ROOT",
        "HOMIE_VAULT_DIR",
        "ORCHESTRATION_DB_PATH",
        "SECOND_BRAIN_RUNTIME_ACTIVITY_DB",
        "DISCORD_CHANNEL_BINDINGS_FILE",
        "CRYPTO_PLAYS_DB_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


def test_default_install_override_and_legacy_fallback(tmp_path, monkeypatch):
    module_path = Path(core.__file__)
    monkeypatch.setattr(
        core, "__file__", str(tmp_path / "checkout-a/.claude/scripts/personas/core.py")
    )
    assert core.get_default_paths()["workspace"] == (tmp_path / "checkout-a").resolve()
    deployed = tmp_path / "deployed"
    (deployed / "vault/memory").mkdir(parents=True)
    monkeypatch.setenv("HOMIE_DEFAULT_PROFILE_ROOT", str(deployed))
    first = resolve_learning_target("default")
    monkeypatch.setattr(
        core, "__file__", str(tmp_path / "checkout-b/.claude/scripts/personas/core.py")
    )
    assert resolve_learning_target("default") == first
    assert first.data_dir == deployed / ".claude/data"
    assert first.config_path == deployed / ".claude/data/state/config.yaml"
    monkeypatch.setattr(core, "__file__", str(module_path))


def test_named_custom_paths_stay_isolated_from_default_override(tmp_path, monkeypatch):
    native = tmp_path / "user"
    monkeypatch.setattr(Path, "home", lambda: native)
    monkeypatch.setenv("HOMIE_DEFAULT_PROFILE_ROOT", str(tmp_path / "deployed"))
    assert core.get_persona_paths("crypto")["data"] == native / ".homie/profiles/crypto/data"
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / "custom"))
    assert core.get_persona_paths("custom")["data"] == tmp_path / "custom/data"
    assert core.get_default_paths()["data"] == tmp_path / "deployed/.claude/data"


def test_runtime_activity_shares_default_root_but_explicit_pin_wins(tmp_path, monkeypatch):
    from runtime.activity import activity_db_path

    monkeypatch.setenv("HOMIE_DEFAULT_PROFILE_ROOT", str(tmp_path / "deploy"))
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / "named"))
    assert activity_db_path() == tmp_path / "deploy/.claude/data/runtime-activity.sqlite3"
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_ACTIVITY_DB", str(tmp_path / "pinned.sqlite3"))
    assert activity_db_path() == tmp_path / "pinned.sqlite3"


def test_orchestration_getter_observes_env_and_constant_at_call_time(tmp_path, monkeypatch):
    import config

    monkeypatch.setenv("HOMIE_DEFAULT_PROFILE_ROOT", str(tmp_path / "deploy"))
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", config._ORCHESTRATION_DB_LEGACY_DEFAULT)
    assert config.get_orchestration_db_path() == tmp_path / "deploy/.claude/data/orchestration.db"
    monkeypatch.setenv("ORCHESTRATION_DB_PATH", str(tmp_path / "first.db"))
    assert config.get_orchestration_db_path() == tmp_path / "first.db"
    monkeypatch.setenv("ORCHESTRATION_DB_PATH", str(tmp_path / "second.db"))
    assert config.get_orchestration_db_path() == tmp_path / "second.db"
    monkeypatch.delenv("ORCHESTRATION_DB_PATH")
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "compat.db")
    assert config.get_orchestration_db_path() == tmp_path / "compat.db"


def test_scoped_children_keep_installation_pins_after_capabilities(tmp_path, monkeypatch):
    import runtime.subprocess_env
    from personas import capabilities

    monkeypatch.setattr(
        runtime.subprocess_env, "get_scrubbed_sdk_env", lambda **kw: dict(kw["parent_env"])
    )
    monkeypatch.setattr(
        capabilities,
        "build_env_sync_plan",
        lambda *a, **kw: SimpleNamespace(values={"ORCHESTRATION_DB_PATH": "stale.db"}),
    )
    pins = {
        "HOMIE_DEFAULT_PROFILE_ROOT": str(tmp_path / "deploy"),
        "ORCHESTRATION_DB_PATH": str(tmp_path / "shared.db"),
        "SECOND_BRAIN_RUNTIME_ACTIVITY_DB": str(tmp_path / "activity.db"),
        "SECOND_BRAIN_GENERIC_MAX_OUTPUT_TOKENS": "2048",
        "SECOND_BRAIN_CODEX_APP_SERVER_COMMAND": str(tmp_path / "pinned-codex.exe"),
    }
    child = capabilities.build_capability_scoped_env(
        "crypto", profile_root=tmp_path / "crypto", parent_env=pins
    )
    assert all(child[key] == value for key, value in pins.items())
    assert child["HOMIE_HOME"] == str(tmp_path / "crypto")


def test_crypto_bootstrap_keeps_persona_and_physical_domain_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    profile = tmp_path / "user/.homie/profiles/crypto"
    profile.mkdir(parents=True)
    (profile / ".env").write_text(
        "CRYPTO_PLAYS_DB_PATH=pinned-plays.db\nX_NETWORKING_ENABLED=true\n"
    )
    monkeypatch.setenv("X_NETWORKING_ENABLED", "false")
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / "old/.claude"))
    selected = bootstrap_scoped_worker(
        "crypto",
        runtime_home=tmp_path / "deployed/.claude",
        data_pins={"CRYPTO_PLAYS_DB_PATH": "crypto_plays.db"},
    )
    from personas.activity import get_active_profile_name

    assert selected == profile / ".env"
    assert os.environ["HOMIE_HOME"] == str(profile.resolve())
    assert get_active_profile_name() == "crypto"
    assert os.environ["ORCHESTRATION_DB_PATH"] == str(
        tmp_path / "deployed/.claude/data/orchestration.db"
    )
    assert os.environ["CRYPTO_PLAYS_DB_PATH"] == "pinned-plays.db"
    assert os.environ["X_NETWORKING_ENABLED"] == "false"
    monkeypatch.delenv("X_NETWORKING_ENABLED")


def test_config_dotenv_cannot_redirect_bound_deployment(tmp_path):
    deployed = tmp_path / "deploy"
    scripts = deployed / ".claude/scripts"
    scripts.mkdir(parents=True)
    (scripts / ".env").write_text(
        "HOMIE_DEFAULT_PROFILE_ROOT=wrong\nORCHESTRATION_DB_PATH=wrong.db\nHOMIE_HOME=wrong-persona\n"
    )
    env = dict(os.environ)
    env.update(
        HOMIE_DEFAULT_PROFILE_ROOT=str(deployed),
        HOMIE_HOME=str(Path.home() / ".homie"),
        ORCHESTRATION_DB_PATH=str(tmp_path / "shared.db"),
    )
    script = (
        "import config,json,os; print(json.dumps({'db':str(config.get_orchestration_db_path()),"
        "'memory':str(config.MEMORY_DIR),'home':os.environ['HOMIE_HOME']}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["db"] == str(tmp_path / "shared.db")
    assert data["memory"] == str(deployed / "vault/memory")
    assert data["home"] == env["HOMIE_HOME"]


def test_relative_default_root_is_refused_instead_of_splitting_checkouts(monkeypatch):
    monkeypatch.setenv("HOMIE_DEFAULT_PROFILE_ROOT", "relative-install")
    with pytest.raises(ValueError, match="absolute installation root"):
        core.get_default_paths()


def test_real_worker_config_boot_preserves_parent_stop_and_domain_pins(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".env").write_text(
        "UPWORK_SUBMIT_ENABLED=true\nCRYPTO_PLAYS_DB_PATH=wrong.db\n"
        "ORCHESTRATION_DB_PATH=wrong.db\n"
    )
    env = dict(os.environ)
    env.update(
        HOMIE_HOME=str(profile),
        HOMIE_NAME="custom",
        UPWORK_SUBMIT_ENABLED="false",
        CRYPTO_PLAYS_DB_PATH=str(tmp_path / "plays.db"),
        ORCHESTRATION_DB_PATH=str(tmp_path / "shared.db"),
    )
    script = (
        "import config,os,json; print(json.dumps({k:os.getenv(k) for k in "
        "['UPWORK_SUBMIT_ENABLED','CRYPTO_PLAYS_DB_PATH','ORCHESTRATION_DB_PATH']}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert all(result[key] == env[key] for key in result)


def test_dotenv_cannot_change_an_unset_default_persona_selection(tmp_path):
    deployed = tmp_path / "deploy"
    scripts = deployed / ".claude/scripts"
    scripts.mkdir(parents=True)
    (scripts / ".env").write_text("HOMIE_HOME=incorrect-custom-root\n")
    env = dict(os.environ)
    env.pop("HOMIE_HOME", None)
    env.update(HOMIE_DEFAULT_PROFILE_ROOT=str(deployed))
    proc = subprocess.run(
        [sys.executable, "-c", "import config,personas; print(personas.get_active_profile_name())"],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip().splitlines()[-1] == "default"


def test_scoped_worker_retains_explicit_named_persona_home(tmp_path, monkeypatch):
    home = tmp_path / "tenant/profiles/crypto"
    monkeypatch.setenv("HOMIE_HOME", str(home))
    monkeypatch.setenv("HOMIE_NAME", "crypto")
    assert bootstrap_scoped_worker("crypto") == home / ".env"
    assert os.environ["HOMIE_HOME"] == str(home.resolve())


def test_config_reload_keeps_host_pinned_appserver_binary(tmp_path, monkeypatch):
    import config

    selected = tmp_path / "proven-codex.exe"
    envfile = tmp_path / ".env"
    envfile.write_text("SECOND_BRAIN_CODEX_APP_SERVER_COMMAND=unverified.exe\n")
    monkeypatch.setattr(config, "ENV_FILE", envfile)
    monkeypatch.setenv("SECOND_BRAIN_CODEX_APP_SERVER_COMMAND", str(selected))
    config._load_profile_environment()
    assert os.environ["SECOND_BRAIN_CODEX_APP_SERVER_COMMAND"] == str(selected)
