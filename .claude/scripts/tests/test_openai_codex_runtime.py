from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import runtime.openai_codex as openai_codex
import runtime.profiles as profiles
from runtime.auth_profiles import AuthProfileStatus
from runtime.base import RUNTIME_LANE_GENERIC, RuntimeRequest
from runtime.errors import RuntimeConfigError
from runtime.profiles import RuntimeProfile


def _codex_profile(key_prefix: str = "fallback", model: str = "gpt-5") -> RuntimeProfile:
    return RuntimeProfile(
        key=f"{key_prefix}-openai-codex",
        provider="openai-codex",
        model=model,
        command="codex",
        auth_profile="default",
    )


def test_resolve_primary_openai_codex_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "openai_codex")
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_MODEL", raising=False)
    monkeypatch.setattr(
        profiles,
        "_openai_codex_profile",
        lambda **kwargs: _codex_profile(**kwargs),
    )

    request = RuntimeRequest(prompt="hi", cwd=".", task_name="safe_text")
    resolved = profiles.resolve_runtime_profiles(request)

    # Codex should be primary when pinned
    assert resolved[0].provider == "openai-codex"
    assert resolved[0].command == "codex"


def test_resolve_runtime_profiles_includes_openai_codex_in_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "claude")
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(profiles, "OPENAI_API_KEY", "")
    monkeypatch.setattr(
        profiles,
        "_openai_codex_profile",
        lambda **kwargs: _codex_profile(kwargs["key_prefix"], kwargs.get("model", "gpt-5")),
    )

    request = RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    resolved = profiles.resolve_runtime_profiles(request)
    providers = [p.provider for p in resolved]

    # Codex should be in the fallback chain
    assert "openai-codex" in providers


@pytest.mark.asyncio
async def test_openai_codex_runtime_executes_via_codex_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = _codex_profile(key_prefix="primary")
    runtime = openai_codex.OpenAICodexRuntime(profile)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        openai_codex,
        "codex_auth_status",
        lambda _profile=None: AuthProfileStatus(True, "Logged in using ChatGPT"),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        output_path = Path(args[args.index("--output-last-message") + 1])

        class FakeProcess:
            returncode = 0

            async def communicate(self, data: bytes):
                captured["prompt"] = data.decode("utf-8")
                output_path.write_text("Codex says hello", encoding="utf-8")
                return (b'{"type":"thread.started","thread_id":"t1"}\n', b"")

        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    request = RuntimeRequest(
        prompt="Summarize this",
        cwd=tmp_path,
        task_name="summary",
        system_prompt={"append": "Stay concise."},
    )

    result = await runtime.run(request)

    assert result.text == "Codex says hello"
    assert result.runtime_lane == RUNTIME_LANE_GENERIC
    assert result.provider == "openai-codex"
    assert result.profile_key == "primary-openai-codex"
    assert "--json" in captured["args"]
    assert "--sandbox" in captured["args"]
    assert "model_reasoning_effort=\"medium\"" in captured["args"]
    assert "Stay concise." in captured["prompt"]


@pytest.mark.asyncio
async def test_openai_codex_runtime_skips_explicit_model_for_plan_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = openai_codex.OpenAICodexRuntime(
        RuntimeProfile(
            key="primary-openai-codex",
            provider="openai-codex",
            model="chatgpt-plan-default",
            command="codex",
            auth_profile="default",
        )
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        openai_codex,
        "codex_auth_status",
        lambda _profile=None: AuthProfileStatus(True, "Logged in using ChatGPT"),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        output_path = Path(args[args.index("--output-last-message") + 1])

        class FakeProcess:
            returncode = 0

            async def communicate(self, _data: bytes):
                output_path.write_text("ok", encoding="utf-8")
                return (b'{"type":"thread.started","thread_id":"t1"}\n', b"")

        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await runtime.run(RuntimeRequest(prompt="hi", cwd=".", task_name="summary"))

    assert result.text == "ok"
    assert result.runtime_lane == RUNTIME_LANE_GENERIC
    assert "--model" not in captured["args"]


@pytest.mark.asyncio
async def test_openai_codex_runtime_extracts_command_execution_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = openai_codex.OpenAICodexRuntime(_codex_profile(key_prefix="primary"))

    monkeypatch.setattr(
        openai_codex,
        "codex_auth_status",
        lambda _profile=None: AuthProfileStatus(True, "Logged in using ChatGPT"),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        output_path = Path(args[args.index("--output-last-message") + 1])

        class FakeProcess:
            returncode = 0

            async def communicate(self, _data: bytes):
                output_path.write_text("TOKEN", encoding="utf-8")
                stdout = (
                    b'{"type":"item.started","item":{"id":"item_1","type":"command_execution","status":"in_progress"}}\n'
                    b'{"type":"item.completed","item":{"id":"item_1","type":"command_execution","status":"completed","command":"Get-Content file.txt"}}\n'
                    b'{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"done"}}\n'
                )
                return (stdout, b"")

        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await runtime.run(
        RuntimeRequest(prompt="Read file", cwd=tmp_path, task_name="summary")
    )

    assert result.text == "TOKEN"
    assert result.runtime_lane == RUNTIME_LANE_GENERIC
    assert result.tool_call_count == 1
    assert result.tool_names_used == ["command_execution"]
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "command_execution"
    assert result.tool_calls[0].arguments == {"command": "Get-Content file.txt"}
    assert result.tool_calls[0].status == "completed"


def test_parse_codex_json_events_collects_errors_and_non_json() -> None:
    summary = openai_codex._parse_codex_json_events(
        "\n".join(
            [
                '{"type":"error","message":"Reconnecting..."}',
                '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","status":"declined"}}',
                "plain text line",
            ]
        )
    )

    assert summary["tool_call_count"] == 1
    assert summary["tool_names_used"] == ["command_execution"]
    assert len(summary["tool_calls"]) == 1
    assert summary["tool_calls"][0].provider_type == "command_execution"
    assert summary["error_text"] == "Reconnecting..."
    assert summary["non_json_text"] == "plain text line"


def test_parse_codex_json_events_ignores_internal_hook_commands() -> None:
    summary = openai_codex._parse_codex_json_events(
        "\n".join(
            [
                '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","status":"completed","command":"python ~/.claude/hooks/check_live_chat.py --agent codex"}}',
                '{"type":"item.completed","item":{"id":"item_2","type":"command_execution","status":"completed","command":"Get-Content file.txt"}}',
            ]
        )
    )

    assert summary["tool_call_count"] == 1
    assert len(summary["tool_calls"]) == 1
    assert summary["tool_calls"][0].arguments == {"command": "Get-Content file.txt"}


def test_codex_reasoning_effort_uses_low_for_tiny_chat_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECOND_BRAIN_CODEX_REASONING_EFFORT", raising=False)

    request = RuntimeRequest(
        prompt="Reply with exactly: OK",
        cwd=".",
        task_name="chat_turn",
    )

    assert openai_codex._codex_reasoning_effort(request) == "low"


def test_codex_reasoning_effort_keeps_medium_for_normal_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECOND_BRAIN_CODEX_REASONING_EFFORT", raising=False)

    request = RuntimeRequest(
        prompt="Summarize the architecture and list tradeoffs.",
        cwd=".",
        task_name="summary",
    )

    assert openai_codex._codex_reasoning_effort(request) == "medium"


@pytest.mark.asyncio
async def test_openai_codex_runtime_requires_login(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = openai_codex.OpenAICodexRuntime(_codex_profile(key_prefix="primary"))
    monkeypatch.setattr(
        openai_codex,
        "codex_auth_status",
        lambda _profile=None: AuthProfileStatus(False, "Not logged in"),
    )

    with pytest.raises(RuntimeConfigError):
        await runtime.run(RuntimeRequest(prompt="hi", cwd=".", task_name="summary"))
