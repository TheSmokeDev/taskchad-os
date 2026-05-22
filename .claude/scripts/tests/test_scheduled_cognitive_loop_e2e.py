"""Scheduled cognitive-loop validation probes with temp vault state."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from cognitive_loop_test_harness import (  # noqa: E402
    build_scheduled_entrypoint_report,
    seed_cognitive_loop_temp_vault,
)


@pytest.mark.parametrize(
    "entrypoint",
    ["memory_reflect", "memory_weekly", "memory_dream"],
)
def test_scheduled_identity_probes_use_temp_vault(entrypoint: str, tmp_path: Path) -> None:
    vault = seed_cognitive_loop_temp_vault(tmp_path / "TheHomie" / "Memory")

    report = build_scheduled_entrypoint_report(entrypoint, vault)

    assert report["success"] is True
    assert report["vault_root"] == str(vault.resolve())
    assert report["writes"] == []
    assert report["external_sends"] == []
    assert report["runtime_mode"] == "fake_deterministic_probe"
    assert report["identity_payload_present"] is True
    assert report["active_inferences_present"] is True
    assert report["working_memory_present"] is True
    assert report["state"] == "live"


def test_heartbeat_probe_uses_shared_scheduled_cognition_payload(tmp_path: Path) -> None:
    vault = seed_cognitive_loop_temp_vault(tmp_path / "TheHomie" / "Memory")

    report = build_scheduled_entrypoint_report("heartbeat", vault)

    assert report["success"] is True
    assert report["writes"] == []
    assert report["external_sends"] == []
    assert report["identity_payload_present"] is True
    assert report["active_inferences_present"] is True
    assert report["working_memory_present"] is True
    assert report["state"] == "live"
    assert "heartbeat_identity_unification" not in report["missing"]


@pytest.mark.parametrize(
    ("script_name", "entrypoint"),
    [
        ("memory_reflect.py", "memory_reflect"),
        ("memory_weekly.py", "memory_weekly"),
        ("memory_dream.py", "memory_dream"),
        ("heartbeat.py", "heartbeat"),
    ],
)
def test_scheduled_scripts_emit_clean_json_with_vault_override(
    script_name: str,
    entrypoint: str,
    tmp_path: Path,
) -> None:
    vault = seed_cognitive_loop_temp_vault(tmp_path / "TheHomie" / "Memory")

    result = subprocess.run(
        [
            sys.executable,
            script_name,
            "--test",
            "--json",
            "--vault",
            str(vault),
        ],
        cwd=_SCRIPTS_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["entrypoint"] == entrypoint
    assert data["vault_root"] == str(vault.resolve())
    assert data["writes"] == []
    assert data["external_sends"] == []
    assert data["runtime_mode"] == "fake_deterministic_probe"
    assert data["state"] == "live"
