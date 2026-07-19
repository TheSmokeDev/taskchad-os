from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import runtime.gemini_cli as gemini_cli
import runtime.profiles as profiles
from runtime.auth_profiles import AuthProfileStatus
from runtime.base import RuntimeRequest
from runtime.capabilities import TOOL_REASONING
from runtime.errors import RuntimeConfigError, RuntimeRetryableError
from runtime.profiles import RuntimeProfile


def _gemini_profile(
    key_prefix: str = "fallback",
    model: str = "gemini-3-flash-preview",
) -> RuntimeProfile:
    return RuntimeProfile(
        key=f"{key_prefix}-gemini-cli",
        provider="gemini-cli",
        model=model,
        command="gemini",
        auth_profile="oauth-personal",
        candidate_models=(model, "gemini-3-pro-preview", "gemini-2.5-flash"),
    )


def test_resolve_primary_gemini_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # `.env` has canonical selection vars (SECOND_BRAIN_RUNTIME_LANE, SECOND_BRAIN_GENERIC_PROVIDER)
    # which short-circuit selection BEFORE legacy SECOND_BRAIN_RUNTIME_PROVIDER is read.
    # config.py's load_dotenv(override=True) means we have to clear them in-test, not in shell.
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "gemini")
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_MODEL", raising=False)
    monkeypatch.setattr(
        profiles,
        "_gemini_profile",
        lambda **kwargs: _gemini_profile(
            kwargs["key_prefix"],
            kwargs.get("model") or "gemini-3-flash-preview",
        ),
    )

    request = RuntimeRequest(prompt="hi", cwd=".", task_name="safe_text")
    resolved = profiles.resolve_runtime_profiles(request)

    assert resolved[0].provider == "gemini-cli"
    assert resolved[0].model == "gemini-3-flash-preview"


def test_resolve_runtime_profiles_prefers_gemini_auto_fallback_when_codex_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # See note on test_resolve_primary_gemini_profile — `.env` selection vars
    # must be cleared before the legacy provider pin can take effect.
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "claude")
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(profiles, "_openai_codex_profile", lambda **_kwargs: None)
    monkeypatch.setattr(
        profiles,
        "_gemini_profile",
        lambda **kwargs: _gemini_profile(
            kwargs["key_prefix"],
            kwargs.get("model") or "gemini-3-flash-preview",
        ),
    )

    request = RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    resolved = profiles.resolve_runtime_profiles(request)

    assert [profile.provider for profile in resolved] == ["claude", "gemini-cli"]


@pytest.mark.asyncio
async def test_gemini_cli_runtime_executes_via_gemini_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = gemini_cli.GeminiCliRuntime(_gemini_profile(key_prefix="primary"))
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        gemini_cli,
        "gemini_auth_status",
        lambda _profile=None: AuthProfileStatus(True, 'Authenticated via "oauth-personal"'),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")

        class FakeProcess:
            returncode = 0

            async def communicate(self, input=None):
                return (b"Loaded cached credentials.\nGEMINI_OK\n", b"")

        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    request = RuntimeRequest(
        prompt="Reply with exactly GEMINI_OK",
        cwd=tmp_path,
        task_name="summary",
    )

    result = await runtime.run(request)

    assert result.text == "GEMINI_OK"
    assert result.provider == "gemini-cli"
    assert result.profile_key == "primary-gemini-cli"
    assert "--model" in captured["args"]
    # Prompt delivered via stdin (dash arg), NOT as a CLI argument
    assert "-" in captured["args"]
    assert captured["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_gemini_cli_runtime_requires_login(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = gemini_cli.GeminiCliRuntime(_gemini_profile(key_prefix="primary"))
    monkeypatch.setattr(
        gemini_cli,
        "gemini_auth_status",
        lambda _profile=None: AuthProfileStatus(False, "not configured"),
    )

    with pytest.raises(RuntimeConfigError):
        await runtime.run(RuntimeRequest(prompt="hi", cwd=".", task_name="summary"))


def test_gemini_cli_runtime_maps_capacity_errors() -> None:
    with pytest.raises(RuntimeRetryableError):
        raise gemini_cli._map_gemini_error("429 No capacity available for model gemini-2.5-pro")


def test_gemini_cli_runtime_maps_permission_errors_to_config() -> None:
    with pytest.raises(RuntimeConfigError):
        raise gemini_cli._map_gemini_error("403 Permission denied on resource project")


@pytest.mark.asyncio
async def test_gemini_cli_runtime_advances_to_next_model_on_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = gemini_cli.GeminiCliRuntime(
        RuntimeProfile(
            key="primary-gemini-cli",
            provider="gemini-cli",
            model="gemini-3-flash-preview",
            command="gemini",
            auth_profile="oauth-personal",
            candidate_models=(
                "gemini-3-flash-preview",
                "gemini-3-pro-preview",
            ),
        )
    )
    attempts: list[str] = []

    monkeypatch.setattr(
        gemini_cli,
        "gemini_auth_status",
        lambda _profile=None: AuthProfileStatus(True, 'Authenticated via "oauth-personal"'),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        model = args[args.index("--model") + 1]
        attempts.append(model)

        class FakeProcess:
            def __init__(self, current_model: str) -> None:
                self.current_model = current_model
                self.returncode = 1 if current_model == "gemini-3-flash-preview" else 0

            async def communicate(self, input=None):
                if self.current_model == "gemini-3-flash-preview":
                    payload = (
                        "Loaded cached credentials.\n"
                        "429 No capacity available for model gemini-3-flash-preview"
                    )
                    return (
                        payload.encode("utf-8"),
                        b"",
                    )
                return (b"GEMINI_LADDER_OK\n", b"")

        return FakeProcess(model)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await runtime.run(RuntimeRequest(prompt="hi", cwd=".", task_name="ladder"))

    assert attempts == ["gemini-3-flash-preview", "gemini-3-pro-preview"]
    assert result.model == "gemini-3-pro-preview"
    assert result.text == "GEMINI_LADDER_OK"


@pytest.mark.asyncio
async def test_gemini_cli_runtime_injects_gemini_guidance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The Gemini lane must ride brevity discipline into the prompt (the only
    output-control vector — the gemini CLI has no max-tokens flag)."""
    runtime = gemini_cli.GeminiCliRuntime(_gemini_profile(key_prefix="primary"))
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        gemini_cli,
        "gemini_auth_status",
        lambda _profile=None: AuthProfileStatus(True, 'Authenticated via "oauth-personal"'),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        class FakeProcess:
            returncode = 0

            async def communicate(self, input=None):
                captured["input"] = input
                return (b"OK\n", b"")

        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await runtime.run(RuntimeRequest(prompt="yo", cwd=tmp_path, task_name="summary"))

    sent = captured["input"].decode("utf-8")
    assert "Gemini operational directives" in sent
    assert "Conciseness" in sent


@pytest.mark.asyncio
async def test_gemini_cli_runtime_scrubs_secrets_from_tool_sandbox_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Issue #128 — a scheduled TOOL_REASONING job must not hand the Gemini CLI
    child the bot's Telegram/Langfuse/etc. secrets.

    The gemini CLI has no --sandbox concept at all, so env scrubbing is the ONLY
    mitigation for this call site — there is no sandbox-level backstop the way
    there partially is for Codex. The tool path here runs --yolo.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:leak-me-not")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "<REDACTED-langfuse>")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-leak-me-not")
    monkeypatch.setenv("OPENAI_API_KEY", "<REDACTED-openai>")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-keep-me")

    runtime = gemini_cli.GeminiCliRuntime(_gemini_profile(key_prefix="primary"))
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        gemini_cli,
        "gemini_auth_status",
        lambda _profile=None: AuthProfileStatus(True, 'Authenticated via "oauth-personal"'),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")

        class FakeProcess:
            returncode = 0

            async def communicate(self, input=None):
                return (b"GEMINI_OK\n", b"")

        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await runtime.run(
        RuntimeRequest(
            prompt="scheduled tool task",
            cwd=tmp_path,
            task_name="heartbeat",
            capability=TOOL_REASONING,
        )
    )

    # Guard: prove the test exercised the auto-approve tool path, not a
    # read-only or text-only child.
    assert "--yolo" in captured["args"]

    env = captured["env"]
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "LANGFUSE_SECRET_KEY" not in env
    assert "SLACK_BOT_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    # Acceptance criterion — provider auth intact.
    assert env["GEMINI_API_KEY"] == "gm-keep-me"


@pytest.mark.asyncio
async def test_gemini_cli_runtime_request_env_secret_cannot_reintroduce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """gate/#140 (inverts the old ordering guard) — mirrors the Codex-side test.
    request.env is now merged into the parent env BEFORE the single scrub, so a
    secret-shaped key smuggled via request.env is DROPPED, while non-secret
    overrides (HOMIE_HOME, GOOGLE_CLOUD_PROJECT) still survive. The GCP-project
    auto-injection runs last and must still win / be injected."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "auto-injected-project")

    runtime = gemini_cli.GeminiCliRuntime(_gemini_profile(key_prefix="primary"))
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        gemini_cli,
        "gemini_auth_status",
        lambda _profile=None: AuthProfileStatus(True, 'Authenticated via "oauth-personal"'),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")

        class FakeProcess:
            returncode = 0

            async def communicate(self, input=None):
                return (b"OK\n", b"")

        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await runtime.run(
        RuntimeRequest(
            prompt="scheduled tool task",
            cwd=tmp_path,
            task_name="heartbeat",
            capability=TOOL_REASONING,
            env={
                "HOMIE_HOME": "/explicit/profile",
                "TELEGRAM_BOT_TOKEN": "smuggled-secret",
                "GOOGLE_CLOUD_PROJECT": "explicit-project",
            },
        )
    )

    # Guard: prove the scrub ran against the auto-approve --yolo tool child
    # (the Gemini CLI has no --sandbox concept — env scrubbing is the ONLY
    # mitigation at this call site).
    assert "--yolo" in captured["args"]

    env = captured["env"]
    # Secret smuggled via request.env is dropped by the merge-then-scrub.
    assert "TELEGRAM_BOT_TOKEN" not in env
    # Non-secret overrides still win.
    assert env["HOMIE_HOME"] == "/explicit/profile"
    assert env["GOOGLE_CLOUD_PROJECT"] == "explicit-project"


def test_gemini_default_ladder_excludes_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECOND_BRAIN_GEMINI_MODEL", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GEMINI_MODEL_LADDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_MODEL", raising=False)
    ladder = profiles._gemini_model_ladder()
    assert ladder[0] == "gemini-2.5-flash"
    assert all("preview" not in model for model in ladder)


def test_gemini_primary_model_is_ga(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_MODEL", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GEMINI_MODEL", raising=False)
    assert profiles._primary_model_for_provider("gemini-cli") == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_cancellation_kills_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Issue #133 — when the lane-level wait_for (or an operator) cancels the
    adapter mid-run, the Gemini child must be reaped, not orphaned. Cancelling
    communicate() alone does NOT kill the child; the CancelledError handler
    must call kill()/wait(). ``call_count`` asserts the model ladder is not
    advanced on cancel (directly, not just as a side effect of `raise`)."""
    # Pin the POSIX plain-kill branch — win32 tree-kill covered separately (#133).
    monkeypatch.setattr(gemini_cli.sys, "platform", "linux")
    runtime = gemini_cli.GeminiCliRuntime(_gemini_profile(key_prefix="primary"))

    monkeypatch.setattr(
        gemini_cli,
        "gemini_auth_status",
        lambda _profile=None: AuthProfileStatus(True, 'Authenticated via "oauth-personal"'),
    )

    started = asyncio.Event()
    holder: dict[str, object] = {}
    call_count = {"n": 0}

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
        call_count["n"] += 1
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
        await asyncio.wait_for(task, timeout=5)

    assert holder["proc"].killed is True
    assert call_count["n"] == 1  # must not have advanced to the next candidate model


@pytest.mark.asyncio
async def test_cancellation_reap_is_bounded_even_if_child_wont_die(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Issue #133 — kill() alone doesn't guarantee the child reaps promptly
    (a Windows zombie can hold wait() open). The reap's own timeout=5 bound
    must fire so cancellation cleanup itself can never hang."""
    # Pin the POSIX plain-kill branch — win32 tree-kill covered separately (#133).
    monkeypatch.setattr(gemini_cli.sys, "platform", "linux")
    runtime = gemini_cli.GeminiCliRuntime(_gemini_profile(key_prefix="primary"))

    monkeypatch.setattr(
        gemini_cli,
        "gemini_auth_status",
        lambda _profile=None: AuthProfileStatus(True, 'Authenticated via "oauth-personal"'),
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
    gemini_cli._reap_process(proc)
    assert proc.kill_called is False


def test_reap_process_suppresses_kill_race() -> None:
    class RacingProcess:
        def __init__(self) -> None:
            self.returncode = None

        def kill(self) -> None:
            raise ProcessLookupError("already gone")

    # Must not raise.
    gemini_cli._reap_process(RacingProcess())


def test_reap_process_tree_kills_on_windows(monkeypatch) -> None:
    """#133 gate: same npm .CMD wrapper class as the Codex adapter — the reap
    must taskkill /T the tree, never plain kill(), on Windows."""
    import runtime.gemini_cli as gemini_cli

    calls: dict[str, list[str]] = {}
    monkeypatch.setattr(gemini_cli.sys, "platform", "win32")

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd

    monkeypatch.setattr(gemini_cli.subprocess, "run", fake_run)

    class WrapperProcess:
        returncode = None
        pid = 5151

        def kill(self):
            raise AssertionError("plain kill() must not be used on win32 (#133)")

    gemini_cli._reap_process(WrapperProcess())
    assert calls["cmd"][:4] == ["taskkill", "/T", "/F", "/PID"]
    assert calls["cmd"][4] == "5151"
