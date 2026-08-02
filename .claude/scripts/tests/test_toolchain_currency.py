from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.openai_codex_app_server import SUPPORTED_CODEX_VERSION
from toolchain_currency import SPECS, SUPPORTED_CLAUDE_CODE_VERSION, ToolchainCurrency


def _write_lock(root: Path) -> None:
    scripts = root / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "uv.lock").write_text(
        """
version = 1

[[package]]
name = "claude-agent-sdk"
version = "0.1.81"

[[package]]
name = "openai"
version = "2.41.0"

[[package]]
name = "mcp"
version = "1.26.0"
""".strip(),
        encoding="utf-8",
    )


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def test_check_reports_gate_and_sdk_migration_without_treating_unused_sdk_as_installed(
    tmp_path: Path,
) -> None:
    _write_lock(tmp_path)

    def runner(argv, _cwd, _timeout):
        if argv[0] == "codex":
            return _completed(argv, stdout=f"codex-cli {SUPPORTED_CODEX_VERSION}\n")
        if argv[0] == "claude":
            return _completed(argv, stdout="2.1.219 (Claude Code)\n")
        raise AssertionError(argv)

    latest = {
        "codex-cli": "0.147.0",
        "claude-code-cli": "2.1.220",
        "codex-sdk-js": "0.147.0",
        "claude-agent-sdk-python": "0.2.128",
        "openai-sdk-python": "2.42.0",
        "mcp-python": "2.0.0",
    }
    manager = ToolchainCurrency(
        tmp_path,
        runner=runner,
        latest_lookup=lambda spec: latest[spec.id],
    )

    report = manager.check()
    items = {item.id: item for item in report.items}

    assert items["codex-cli"].state == "compatible_current_newer_ungated"
    assert items["codex-cli"].auto_apply is False
    assert items["claude-code-cli"].state == "compatibility_update_required"
    assert items["claude-code-cli"].desired_version == SUPPORTED_CLAUDE_CODE_VERSION
    assert items["claude-code-cli"].auto_apply is True
    assert items["codex-sdk-js"].state == "not_in_use"
    assert items["codex-sdk-js"].current_version is None
    assert items["claude-agent-sdk-python"].state == "migration_required"
    assert items["mcp-python"].state == "migration_required"
    assert items["openai-sdk-python"].state == "dependency_update_proposal"


def test_safe_apply_updates_only_managed_clis_and_keeps_codex_on_gate(tmp_path: Path) -> None:
    _write_lock(tmp_path)
    versions = {"codex": "0.145.0", "claude": "2.1.219"}
    installs: list[list[str]] = []
    latest = {
        "codex-cli": "0.147.0",
        "claude-code-cli": "2.1.221",
        "codex-sdk-js": "0.147.0",
        "claude-agent-sdk-python": "0.2.128",
        "openai-sdk-python": "2.42.0",
        "mcp-python": "2.0.0",
    }

    def runner(argv, _cwd, _timeout):
        if argv[0] in versions:
            return _completed(argv, stdout=f"{versions[argv[0]]}\n")
        if argv[:3] == ["npm", "install", "-g"]:
            installs.append(list(argv))
            package, version = argv[3].rsplit("@", 1)
            if package == "@openai/codex":
                versions["codex"] = version
            if package == "@anthropic-ai/claude-code":
                versions["claude"] = version
            return _completed(argv, stdout="updated\n")
        raise AssertionError(argv)

    manager = ToolchainCurrency(
        tmp_path,
        state_dir=tmp_path / "state",
        runner=runner,
        latest_lookup=lambda spec: latest[spec.id],
    )
    receipt = manager.apply_safe_cli_updates(scheduled=True)

    assert receipt.success is True
    assert [row["id"] for row in receipt.attempted] == ["codex-cli", "claude-code-cli"]
    assert [argv[3] for argv in installs] == [
        f"@openai/codex@{SUPPORTED_CODEX_VERSION}",
        f"@anthropic-ai/claude-code@{SUPPORTED_CLAUDE_CODE_VERSION}",
    ]
    assert not any("sdk" in argv[3] for argv in installs)
    assert manager.history(limit=1)[0]["receipt_id"] == receipt.receipt_id


def test_every_manifest_target_has_a_unique_id() -> None:
    assert len({spec.id for spec in SPECS}) == len(SPECS)
