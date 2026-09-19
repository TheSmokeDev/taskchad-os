"""First-import deployment paths cannot diverge across executing checkouts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from personas import boot, core, deployment


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in (*deployment.DEPLOYMENT_PIN_KEYS, "HOMIE_HOME", "HOMIE_NAME", "PYTHONPATH"):
        monkeypatch.delenv(key, raising=False)


def write_checkout_env(scripts, runtime, vault, orch):
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / ".env").write_text(
        f'HOMIE_DEFAULT_PROFILE_ROOT="{runtime}"\n'
        f'HOMIE_VAULT_DIR="{vault}"\n'
        f'ORCHESTRATION_DB_PATH="{orch}"\n'
        f'SECOND_BRAIN_RUNTIME_ACTIVITY_DB="{runtime}/.claude/data/runtime-activity.sqlite3"\n'
        "SECOND_BRAIN_GENERIC_PROVIDER=must-not-be-loaded-by-bootstrap\n"
        "OPENAI_API_KEY=must-not-be-loaded-by-bootstrap\n",
        encoding="utf-8",
    )


def test_core_and_boot_resolve_trusted_source_pins_before_config(tmp_path, monkeypatch):
    scripts = tmp_path / "checkout/.claude/scripts"
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    write_checkout_env(scripts, runtime, vault, tmp_path / "root-ledger.db")
    monkeypatch.setattr(core, "__file__", str(scripts / "personas/core.py"))
    monkeypatch.setattr(boot, "__file__", str(scripts / "personas/boot.py"))
    assert core.get_default_paths()["data"] == runtime / ".claude/data"
    assert core.get_default_paths()["memory"] == vault
    assert "HOMIE_DEFAULT_PROFILE_ROOT" not in os.environ  # pure path lookup
    monkeypatch.setattr(sys, "argv", ["thehomie", "--profile", "default"])
    boot.apply_persona_override()
    assert os.environ["HOMIE_DEFAULT_PROFILE_ROOT"] == str(runtime)
    assert "ORCHESTRATION_DB_PATH" not in os.environ  # config resolves selected profile next
    assert os.environ.get("OPENAI_API_KEY") != "must-not-be-loaded-by-bootstrap"
    assert os.environ.get("SECOND_BRAIN_GENERIC_PROVIDER") != "must-not-be-loaded-by-bootstrap"


def test_explicit_environment_wins_even_over_an_invalid_overridden_file_pin(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("HOMIE_DEFAULT_PROFILE_ROOT=${UNAVAILABLE}\n")
    monkeypatch.setenv("HOMIE_DEFAULT_PROFILE_ROOT", str(tmp_path / "explicit"))
    deployment.bootstrap_deployment_pins(tmp_path)
    assert os.environ["HOMIE_DEFAULT_PROFILE_ROOT"] == str(tmp_path / "explicit")
    monkeypatch.delenv("HOMIE_DEFAULT_PROFILE_ROOT")
    with pytest.raises(ValueError, match="literal deployment path"):
        deployment.bootstrap_deployment_pins(tmp_path)


def cold_config(scripts, env):
    source = Path(__file__).parents[1]
    (scripts / "config.py").write_bytes((source / "config.py").read_bytes())
    process_env = dict(os.environ, **env)
    process_env["PYTHONPATH"] = str(source)
    program = (
        "import config,json,os; from personas import core; "
        "before={'data':str(config.DATA_DIR),'env_file':str(config.ENV_FILE),"
        "'vault':str(config.MEMORY_DIR),'orch':str(config.get_orchestration_db_path()),"
        "'root':os.environ.get('HOMIE_DEFAULT_PROFILE_ROOT'),"
        "'provider':os.environ.get('SECOND_BRAIN_GENERIC_PROVIDER')}; "
        "config.reload_config(); "
        "after={'data':str(core.get_default_paths()['data']), "
        "'vault':str(core.get_default_paths()['memory']),"
        "'orch':str(config.get_orchestration_db_path()),"
        "'root':os.environ.get('HOMIE_DEFAULT_PROFILE_ROOT')}; "
        "print(json.dumps({'before':before,'after':after}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=scripts,
        env=process_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_real_processes_first_import_and_reload_share_default_but_keep_local_ledgers(tmp_path):
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    runtime_scripts = runtime / ".claude/scripts"
    runtime_scripts.mkdir(parents=True)
    (runtime_scripts / ".env").write_text(
        f"ORCHESTRATION_DB_PATH={tmp_path}/runtime-ledger.db\n"
        "SECOND_BRAIN_GENERIC_PROVIDER=canonical-provider\n"
    )
    outputs = []
    for name in ("root-checkout", "scheduled-checkout"):
        scripts = tmp_path / name / ".claude/scripts"
        ledger = tmp_path / f"{name}.db"
        write_checkout_env(scripts, runtime, vault, ledger)
        output = cold_config(scripts, {})
        outputs.append(output)
        assert output["before"]["data"] == str(runtime / ".claude/data")
        assert output["before"]["env_file"] == str(runtime_scripts / ".env")
        assert output["before"]["vault"] == str(vault)
        assert output["before"]["orch"] == str(ledger)
        assert output["before"]["provider"] == "canonical-provider"
        assert output["after"]["orch"] == str(ledger)
        assert output["after"]["data"] == str(runtime / ".claude/data")
    assert outputs[0]["before"]["data"] == outputs[1]["before"]["data"]
    assert outputs[0]["before"]["orch"] != outputs[1]["before"]["orch"]


def test_cold_named_profile_keeps_own_data_and_explicit_domain_pin(tmp_path):
    scripts = tmp_path / "checkout/.claude/scripts"
    runtime, vault, profile = tmp_path / "runtime", tmp_path / "vault", tmp_path / "profile"
    write_checkout_env(scripts, runtime, vault, tmp_path / "root-ledger.db")
    profile.mkdir()
    (profile / ".env").write_text(f"ORCHESTRATION_DB_PATH={tmp_path}/profile-ledger.db\n")
    result = cold_config(scripts, {"HOMIE_HOME": str(profile), "HOMIE_NAME": "custom"})
    assert result["before"]["data"] == str(profile / "data")
    assert result["before"]["vault"] == str(profile / "memory")
    assert result["before"]["env_file"] == str(profile / ".env")
    assert result["before"]["orch"] == str(tmp_path / "profile-ledger.db")
    assert result["after"]["orch"] == str(tmp_path / "profile-ledger.db")


def test_named_dispatcher_child_keeps_profile_domain_pin_and_shared_activity(tmp_path, monkeypatch):
    from personas import capabilities
    from personas.learning import dispatcher
    from personas.learning.models import LearningTarget
    from personas.learning.service import LearningService
    from runtime import activity

    profile = tmp_path / "crypto"
    profile.mkdir()
    child_ledger = tmp_path / "crypto-domain.db"
    (profile / ".env").write_text(f"ORCHESTRATION_DB_PATH={child_ledger}\n")
    monkeypatch.setenv("ORCHESTRATION_DB_PATH", str(tmp_path / "main-domain.db"))
    monkeypatch.setenv("HOMIE_DEFAULT_PROFILE_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_ACTIVITY_DB", str(tmp_path / "shared-activity.db"))
    monkeypatch.setattr(
        capabilities, "build_env_sync_plan", lambda *a, **kw: SimpleNamespace(values={})
    )
    svc = LearningService(
        LearningTarget(
            "crypto", profile / "memory", profile / "data", profile / "state", profile / "skills"
        )
    )
    env = dispatcher._child_env(svc)
    assert env["ORCHESTRATION_DB_PATH"] == str(child_ledger)
    assert env["HOMIE_DEFAULT_PROFILE_ROOT"] == str(tmp_path / "runtime")
    assert env["SECOND_BRAIN_RUNTIME_ACTIVITY_DB"] == str(activity.activity_db_path())
    assert env["HOMIE_HOME"] == str(profile)
    (profile / ".env").write_text("")
    assert dispatcher._child_env(svc)["ORCHESTRATION_DB_PATH"] == str(tmp_path / "main-domain.db")


def test_literal_pin_reader_handles_quoted_hashes_and_empty_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        'HOMIE_DEFAULT_PROFILE_ROOT="C:/deploy # one" # explanation\n'
        "ORCHESTRATION_DB_PATH= # retain default\n"
    )
    assert deployment.read_deployment_pins(path) == {
        "HOMIE_DEFAULT_PROFILE_ROOT": "C:/deploy # one",
        "ORCHESTRATION_DB_PATH": "",
    }


def test_explicit_empty_pin_stays_legacy_through_full_dotenv_and_reload(tmp_path):
    scripts = tmp_path / "checkout/.claude/scripts"
    write_checkout_env(scripts, tmp_path / "other-runtime", tmp_path / "vault", tmp_path / "root.db")
    result = cold_config(scripts, {"HOMIE_DEFAULT_PROFILE_ROOT": ""})
    assert result["before"]["root"] == result["after"]["root"] == ""
    assert result["before"]["data"] == result["after"]["data"] == str(Path(core.__file__).resolve().parents[2] / "data")
