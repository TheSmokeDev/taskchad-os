from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import runtime.openai_codex as openai_codex
import runtime.profiles as profiles
from runtime.auth_profiles import AuthProfileStatus
from runtime.base import RUNTIME_LANE_GENERIC, RuntimeRequest
from runtime.capabilities import TOOL_REASONING
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
    # Clear lane-first env so the legacy provider key gets the routing.
    # The lane-first refactor (PR1/PR2 2026-04-10) added
    # SECOND_BRAIN_RUNTIME_LANE which is read via dotenv at config-load
    # time. If `.env` sets it to ``claude_native`` (e.g. on this repo),
    # the legacy ``SECOND_BRAIN_RUNTIME_PROVIDER=openai_codex`` setting
    # alone is overridden because the explicit lane wins in
    # ``resolve_runtime_selection()``. Clearing both new keys here
    # restores the pre-lane-first contract that the test asserts.
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "openai_codex")
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_MODEL", raising=False)
    monkeypatch.setattr(
        profiles,
        "_openai_codex_profile",
        lambda **kwargs: _codex_profile(**kwargs),
    )

    request = RuntimeRequest(prompt="hi", cwd=".", task_name="safe_text")
    resolved = profiles.resolve_runtime_profiles(request)

    # Codex should be primary when pinned via the legacy provider key.
    assert resolved[0].provider == "openai-codex"
    assert resolved[0].command == "codex"


def test_resolve_runtime_profiles_includes_openai_codex_in_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "claude")
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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
    assert result.model == "chatgpt-plan-default"
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
async def test_openai_codex_runtime_scrubs_secrets_from_tool_sandbox_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Issue #128 — a scheduled TOOL_REASONING job must not hand the
    danger-full-access Codex child the bot's Telegram/Langfuse/etc. secrets.

    Exercises the exact shape heartbeat.py dispatches: capability=TOOL_REASONING,
    no read_only_tools, no workspace_write_tools → sandbox danger-full-access.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:leak-me-not")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "<REDACTED-langfuse>")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-leak-me-not")
    monkeypatch.setenv("OPENAI_API_KEY", "<REDACTED-openai>")

    runtime = openai_codex.OpenAICodexRuntime(_codex_profile(key_prefix="primary"))
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

            async def communicate(self, _data: bytes):
                output_path.write_text("ok", encoding="utf-8")
                return (b'{"type":"thread.started","thread_id":"t1"}\n', b"")

        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    request = RuntimeRequest(
        prompt="heartbeat scheduled task",
        cwd=tmp_path,
        task_name="heartbeat",
        capability=TOOL_REASONING,
        allowed_tools=["Read", "Write", "Edit", "Bash"],
    )
    result = await runtime.run(request)

    # Guard: prove this test actually exercised the dangerous path. If the
    # sandbox resolution ever changes, this test must fail rather than pass
    # vacuously against a read-only child.
    args = captured["args"]
    assert args[args.index("--sandbox") + 1] == "danger-full-access"

    env = captured["env"]
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "LANGFUSE_SECRET_KEY" not in env
    assert "DISCORD_BOT_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    # Provider auth intact: subscription auth is $HOME-rooted, so the carve-out
    # must survive or the lane breaks.
    assert "HOME" in env or "USERPROFILE" in env
    assert result.text == "ok"


@pytest.mark.asyncio
async def test_openai_codex_runtime_request_env_secret_cannot_reintroduce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """gate/#140 (inverts the old ordering guard) — request.env is now merged
    into the parent env BEFORE the scrub, so a secret-shaped key smuggled via
    request.env (e.g. Cabinet passing persona secrets) is DROPPED, while a
    non-secret override (HOMIE_HOME) still survives. Pre-gate, request.env was
    applied AFTER the scrub and could reintroduce a scrubbed secret."""
    runtime = openai_codex.OpenAICodexRuntime(_codex_profile(key_prefix="primary"))
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

            async def communicate(self, _data: bytes):
                output_path.write_text("ok", encoding="utf-8")
                return (b'{"type":"thread.started","thread_id":"t1"}\n', b"")

        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await runtime.run(
        RuntimeRequest(
            prompt="heartbeat scheduled task",
            cwd=tmp_path,
            task_name="heartbeat",
            capability=TOOL_REASONING,
            allowed_tools=["Read", "Write", "Edit", "Bash"],
            env={"HOMIE_HOME": "/explicit/profile", "TELEGRAM_BOT_TOKEN": "smuggled-secret"},
        )
    )

    # Guard: prove the scrub ran against the dangerous full-access child.
    args = captured["args"]
    assert args[args.index("--sandbox") + 1] == "danger-full-access"

    env = captured["env"]
    # Secret smuggled via request.env is dropped by the merge-then-scrub.
    assert "TELEGRAM_BOT_TOKEN" not in env
    # Non-secret override still wins.
    assert env["HOMIE_HOME"] == "/explicit/profile"


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


@pytest.mark.asyncio
async def test_cancellation_kills_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Issue #133 — when the lane-level wait_for (or an operator) cancels the
    adapter mid-run, the Codex child must be reaped, not orphaned. Cancelling
    communicate() alone does NOT kill the child; the CancelledError handler
    must call kill()/wait(). Proves the reap path the lane test cannot see."""
    # Pin the POSIX plain-kill branch — win32 tree-kill covered separately (#133).
    monkeypatch.setattr(openai_codex.sys, "platform", "linux")
    runtime = openai_codex.OpenAICodexRuntime(_codex_profile(key_prefix="primary"))

    monkeypatch.setattr(
        openai_codex,
        "codex_auth_status",
        lambda _profile=None: AuthProfileStatus(True, "Logged in using ChatGPT"),
    )

    started = asyncio.Event()
    holder: dict[str, object] = {}

    class StubProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.killed = False

        async def communicate(self, _data: bytes = b""):
            started.set()
            await asyncio.sleep(3600)  # never returns on its own
            return (b"", b"")

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode if self.returncode is not None else 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        proc = StubProcess()
        holder["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    task = asyncio.create_task(
        runtime.run(RuntimeRequest(prompt="hi", cwd=tmp_path, task_name="summary"))
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert holder["proc"].killed is True


@pytest.mark.asyncio
async def test_cancellation_reap_is_bounded_even_if_child_wont_die(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Issue #133 — kill() alone doesn't guarantee the child reaps promptly
    (a Windows zombie can hold wait() open). The reap's own timeout=5 bound
    must fire so cancellation cleanup itself can never hang."""
    # Pin the POSIX plain-kill branch — win32 tree-kill covered separately (#133).
    monkeypatch.setattr(openai_codex.sys, "platform", "linux")
    runtime = openai_codex.OpenAICodexRuntime(_codex_profile(key_prefix="primary"))

    monkeypatch.setattr(
        openai_codex,
        "codex_auth_status",
        lambda _profile=None: AuthProfileStatus(True, "Logged in using ChatGPT"),
    )

    started = asyncio.Event()
    holder: dict[str, object] = {}

    class StubProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.killed = False

        async def communicate(self, _data: bytes = b""):
            started.set()
            await asyncio.sleep(3600)
            return (b"", b"")

        def kill(self) -> None:
            self.killed = True
            # NOTE: returncode intentionally NOT set here — simulates a
            # zombie child that doesn't report exit immediately.

        async def wait(self) -> int:
            await asyncio.sleep(3600)  # never returns on its own
            return -9

    async def fake_create_subprocess_exec(*args, **kwargs):
        proc = StubProcess()
        holder["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    task = asyncio.create_task(
        runtime.run(RuntimeRequest(prompt="hi", cwd=tmp_path, task_name="summary"))
    )
    await started.wait()
    task.cancel()

    # The whole cancellation path (including the internal timeout=5 reap
    # wait) must resolve well under the stub's 3600s hang.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    assert holder["proc"].killed is True


def test_reap_process_skips_already_exited_process() -> None:
    class ExitedProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.kill_called = False

        def kill(self) -> None:
            self.kill_called = True

    proc = ExitedProcess()
    openai_codex._reap_process(proc)
    assert proc.kill_called is False


def test_reap_process_suppresses_kill_race() -> None:
    class RacingProcess:
        def __init__(self) -> None:
            self.returncode = None

        def kill(self) -> None:
            raise ProcessLookupError("already gone")

    # Must not raise.
    openai_codex._reap_process(RacingProcess())


def test_reap_process_tree_kills_on_windows(monkeypatch) -> None:
    """#133 gate: the CLI is an npm .CMD wrapper on Windows — plain kill()
    terminates only the wrapper while the Node descendant (the actual wedged
    CLI) survives. The reap must taskkill /T the whole tree."""
    calls: dict[str, list[str]] = {}
    monkeypatch.setattr(openai_codex.sys, "platform", "win32")

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd

    monkeypatch.setattr(openai_codex.subprocess, "run", fake_run)

    class WrapperProcess:
        returncode = None
        pid = 4242

        def kill(self):
            raise AssertionError("plain kill() must not be used on win32 (#133)")

    openai_codex._reap_process(WrapperProcess())
    assert calls["cmd"][:4] == ["taskkill", "/T", "/F", "/PID"]
    assert calls["cmd"][4] == "4242"
