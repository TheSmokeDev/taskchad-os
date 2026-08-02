from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import cli
from click.testing import CliRunner
from diagnostics import DiagnosticsReport

from buzz_status import read_buzz_status, write_buzz_status


def test_runtime_status_never_serializes_private_key(tmp_path) -> None:
    path = tmp_path / "status.json"
    secret = "11" * 32
    write_buzz_status(
        {
            "enabled": True,
            "state": "connected",
            "active_transport": "websocket",
            "relay_host": "localhost",
            "identity": "npub1abc…123",
            "watched_channel_count": 2,
            "last_error": None,
            "private_key": secret,
        },
        path,
    )

    raw = path.read_text(encoding="utf-8")
    status = read_buzz_status(path)
    assert secret not in raw
    assert "private_key" not in raw
    assert status["state"] == "connected"


def test_chat_buzz_delegates_to_adapter_only_process(monkeypatch) -> None:
    captured = {}

    def fake_run(args, check):
        captured["args"] = args
        captured["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    result = CliRunner().invoke(cli.main, ["chat", "--buzz"])

    assert result.exit_code == 0
    assert captured["args"][-1] == "--buzz"
    assert captured["args"][-2].endswith("main.py")
    assert captured["check"] is False


def test_chat_buzz_refuses_query_mode_combination() -> None:
    result = CliRunner().invoke(cli.main, ["chat", "--buzz", "-q", "hello"])

    assert result.exit_code == 2
    assert "adapter-only" in result.output


def test_buzz_deliver_is_a_production_cron_seam(monkeypatch) -> None:
    captured = {}

    class StubAdapter:
        settings = SimpleNamespace(private_key="")

        async def deliver_scheduled(self, text, *, attachments):
            captured["text"] = text
            captured["attachments"] = attachments
            return "scheduled-event"

    monkeypatch.setattr("adapters.buzz.BuzzAdapter", StubAdapter)
    result = CliRunner().invoke(cli.main, ["buzz", "deliver", "daily result", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"success": True, "event_id": "scheduled-event"}
    assert captured == {"text": "daily result", "attachments": []}


def test_status_json_shape_has_buzz_field() -> None:
    report = DiagnosticsReport(
        timestamp="2026-07-31T00:00:00+00:00",
        uptime_seconds=1,
        buzz={
            "enabled": True,
            "state": "degraded",
            "active_transport": "polling",
            "relay_host": "localhost",
        },
    )
    payload = json.loads(json.dumps(dataclasses.asdict(report)))
    assert payload["buzz"]["active_transport"] == "polling"


def test_doctor_renders_secret_free_buzz_snapshot(monkeypatch) -> None:
    secret = "22" * 32
    report = DiagnosticsReport(
        timestamp="2026-07-31T00:00:00+00:00",
        uptime_seconds=1,
        runtime_providers={"openai-codex": "ON"},
        buzz={
            "enabled": True,
            "state": "connected",
            "active_transport": "websocket",
            "relay_host": "localhost",
            "identity": "npub1abc…123",
            "watched_channel_count": 3,
            "cli_version": "0.5.2",
            "cli_compatible": True,
            "private_key": secret,
        },
    )
    monkeypatch.setattr("diagnostics.collect_diagnostics", lambda: report)
    monkeypatch.setattr("diagnostics.check_environment", lambda: [])
    monkeypatch.setattr(cli, "_print_native_commands", lambda: None)
    monkeypatch.setattr(cli, "_print_video_learning_readiness", lambda: None)

    result = CliRunner().invoke(cli.main, ["doctor"])

    assert result.exit_code == 0
    assert "Buzz:" in result.output
    assert "State: connected" in result.output
    assert "Transport: websocket" in result.output
    assert secret not in result.output
