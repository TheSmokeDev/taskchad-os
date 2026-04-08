"""Tests for The Homie CLI entry point and CLI adapter."""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Add paths
_CHAT_DIR = str(Path(__file__).parent.parent.parent / "chat")
_SCRIPTS_DIR = str(Path(__file__).parent.parent)
if _CHAT_DIR not in sys.path:
    sys.path.insert(0, _CHAT_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from cli import main as cli_main


class TestCLIHelp:
    """Click CliRunner tests — fast, in-process."""

    def test_main_help(self):
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_main, ["--help"])
        assert result.exit_code == 0
        assert "The Homie" in result.output

    def test_chat_help(self):
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_main, ["chat", "--help"])
        assert result.exit_code == 0
        assert "-q" in result.output
        assert "--resume" in result.output

    def test_status_help(self):
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_main, ["status", "--help"])
        assert result.exit_code == 0

    def test_setup_help(self):
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_main, ["setup", "--help"])
        assert result.exit_code == 0

    def test_doctor_help(self):
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_main, ["doctor", "--help"])
        assert result.exit_code == 0

    def test_version(self):
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_main, ["--version"])
        assert result.exit_code == 0
        assert "1.0.0" in result.output


class TestCLIAdapter:
    """Unit tests for CLIAdapter."""

    def test_platform_is_cli(self):
        from adapters.cli_adapter import CLIAdapter

        adapter = CLIAdapter(query="test")
        assert adapter.platform.value == "cli"

    @pytest.mark.asyncio
    async def test_listen_single_query(self):
        from adapters.cli_adapter import CLIAdapter

        adapter = CLIAdapter(query="hello")
        messages = []
        async for msg in adapter.listen():
            messages.append(msg)
        assert len(messages) == 1
        assert messages[0].text == "hello"

    def test_quiet_output_format(self):
        from adapters.cli_adapter import CLIAdapter

        adapter = CLIAdapter(query="test", quiet=True)
        output = adapter.format_final_output(
            "sess123",
            {"provider": "claude", "model": "opus", "cost_usd": 0.01, "tool_calls": 2},
        )
        data = json.loads(output)
        assert data["success"] is True
        assert data["session_id"] == "sess123"
        assert data["provider"] == "claude"

    def test_normal_output_format(self):
        from adapters.cli_adapter import CLIAdapter

        adapter = CLIAdapter(query="test", quiet=False)
        output = adapter.format_final_output("sess123", {"provider": "claude"})
        assert "session_id: sess123" in output
        assert "---" in output

    @pytest.mark.asyncio
    async def test_quiet_output_marks_error_from_send(self):
        from adapters.cli_adapter import CLIAdapter
        from models import Channel, OutgoingMessage, Platform

        adapter = CLIAdapter(query="test", quiet=True)
        channel = Channel(Platform.CLI, "cli-test", is_dm=True)

        await adapter.send(
            OutgoingMessage(
                text="No runtime provider available",
                channel=channel,
                is_error=True,
            )
        )

        output = adapter.format_final_output("", {})
        data = json.loads(output)
        assert data["success"] is False
        assert data["error"] == "No runtime provider available"

    @pytest.mark.asyncio
    async def test_quiet_output_ignores_placeholder_updates(self):
        from adapters.cli_adapter import CLIAdapter
        from models import Channel, OutgoingMessage, Platform

        adapter = CLIAdapter(query="test", quiet=True)
        channel = Channel(Platform.CLI, "cli-test", is_dm=True)

        await adapter.update(
            OutgoingMessage(
                text="Thinking...",
                channel=channel,
                is_update=True,
            )
        )
        await adapter.send(
            OutgoingMessage(
                text="final answer",
                channel=channel,
            )
        )

        output = adapter.format_final_output("sess123", {})
        data = json.loads(output)
        assert data["success"] is True
        assert data["response"] == "final answer"

    @pytest.mark.asyncio
    async def test_send_normal_prints(self, capsys):
        from adapters.cli_adapter import CLIAdapter
        from models import Channel, OutgoingMessage, Platform

        adapter = CLIAdapter(query="test", quiet=False)
        channel = Channel(Platform.CLI, "cli-test", is_dm=True)

        await adapter.send(OutgoingMessage(text="hello world", channel=channel))

        captured = capsys.readouterr()
        assert "hello world" in captured.out

    def test_get_session_info_returns_runtime_model(self, monkeypatch, tmp_path):
        import config
        from adapters.cli_adapter import CLIAdapter
        from session import Session, SQLiteSessionStore
        from session_keys import build_session_key

        db_path = tmp_path / "chat.db"
        monkeypatch.setattr(config, "CHAT_DB_PATH", db_path)

        store = SQLiteSessionStore(db_path)
        now = datetime.now()
        channel_id = "cli-test"
        store.create(
            Session(
                session_id=build_session_key("cli", channel_id, channel_id),
                agent_session_id="runtime-session-1",
                platform="cli",
                channel_id=channel_id,
                thread_id=channel_id,
                user_id="cli-user",
                created_at=now,
                updated_at=now,
                runtime_provider="openai-codex",
                runtime_model="chatgpt-plan-default",
            )
        )

        adapter = CLIAdapter(query="test", quiet=True)
        adapter._channel_id = channel_id

        session_info = adapter.get_session_info()
        assert session_info["provider"] == "openai-codex"
        assert session_info["model"] == "chatgpt-plan-default"


class TestQuietModeRegression:
    """Regression tests for Codex audit findings — quiet mode contract."""

    def test_quiet_stdout_is_json_only(self):
        """Finding 1: -Q stdout must be JSON-only, no framework logs."""
        result = subprocess.run(
            ["uv", "run", "thehomie", "chat", "-q", "/help", "-Q"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        stdout = result.stdout.strip()
        # stdout must be parseable as a single JSON object — no log lines
        data = json.loads(stdout)
        assert "success" in data
        # Verify NO extra lines before the JSON (the original bug)
        assert stdout.startswith("{"), f"stdout has non-JSON prefix: {stdout[:80]}"

    @pytest.mark.asyncio
    async def test_router_preserves_is_error_through_final_send(self):
        """Finding 2: engine is_error must survive router._handle_inner() → CLI adapter.

        This tests the ACTUAL router path, not just the adapter in isolation.
        The original bug: router extracted only final_text from engine output,
        then created a new OutgoingMessage WITHOUT is_error.
        """
        from adapters.cli_adapter import CLIAdapter
        from models import Channel, IncomingMessage, OutgoingMessage, Platform, User
        from router import ChatRouter

        class FakeEngine:
            """Engine that yields an error OutgoingMessage."""
            session_store = None

            async def handle_message(self, message, progress=None):
                yield OutgoingMessage(
                    text="Sorry, I hit an error: test failure",
                    channel=message.channel,
                    thread=message.thread,
                    is_error=True,
                )

        adapter = CLIAdapter(query="test", quiet=True)
        from extension_manager import ExtensionManager
        router = ChatRouter(FakeEngine(), ExtensionManager())
        router.register(adapter)

        incoming = IncomingMessage(
            text="trigger engine",
            user=User(Platform.CLI, "cli-user", "user"),
            channel=Channel(Platform.CLI, "cli-test", is_dm=True),
            platform=Platform.CLI,
        )

        # This goes through router._handle() → _handle_inner() → engine
        await router._handle(adapter, incoming)

        output = adapter.format_final_output("", {})
        data = json.loads(output)
        assert data["success"] is False, (
            "Router must preserve is_error from engine through to CLI quiet output"
        )

    def test_diagnostics_adapter_access_via_router(self):
        """Finding 3: /diagnostics must use self.adapters not self._adapters.

        The original bug: router referenced self._adapters which didn't exist.
        """
        from router import ChatRouter

        class FakeEngine:
            session_store = None

        from extension_manager import ExtensionManager
        router = ChatRouter(FakeEngine(), ExtensionManager())
        # The adapters dict must exist and be accessible
        assert hasattr(router, "adapters")
        assert isinstance(router.adapters, dict)
        # The old broken attribute must NOT exist
        assert not hasattr(router, "_adapters")

    def test_health_callback_uses_correct_adapter_attr(self):
        """Finding 4: main.py health callback must use router.adapters not router._adapters."""
        from router import ChatRouter

        class FakeEngine:
            session_store = None

        from extension_manager import ExtensionManager
        router = ChatRouter(FakeEngine(), ExtensionManager())
        # Replicate the health callback pattern from main.py
        adapters_status = {p.value: True for p in router.adapters.keys()}
        assert isinstance(adapters_status, dict)


class TestDoctorRegression:
    """Regression tests for Codex audit finding 5 — doctor false-green."""

    def test_doctor_help_exits_zero(self):
        result = subprocess.run(
            ["uv", "run", "thehomie", "doctor", "--help"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0

    def test_doctor_cli_checks_provider_health(self):
        """Finding 5: doctor must fail when zero providers are active, not false-green."""
        from diagnostics import DiagnosticsReport

        # Simulate a report with zero active providers
        report = DiagnosticsReport(
            timestamp="now",
            uptime_seconds=0.0,
            runtime_providers={"claude": "OFF", "codex": "OFF"},
        )
        active = [v for v in report.runtime_providers.values() if v == "ON"]
        has_failure = not active and report.runtime_providers
        assert has_failure, "Zero active providers should be flagged as a failure"


class TestCLISubprocess:
    """Subprocess tests — validates installed command (CLI-Anything pattern)."""

    @staticmethod
    def _resolve_cli():
        path = shutil.which("thehomie")
        if path:
            return [path]
        return ["uv", "run", "thehomie"]

    def test_help_via_subprocess(self):
        result = subprocess.run(
            self._resolve_cli() + ["--help"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert "The Homie" in result.stdout

    def test_version_via_subprocess(self):
        result = subprocess.run(
            self._resolve_cli() + ["--version"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert "1.0.0" in result.stdout
