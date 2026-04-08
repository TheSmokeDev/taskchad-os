from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import runtime.gemini_cli as gemini_cli
import runtime.profiles as profiles
from runtime.auth_profiles import AuthProfileStatus
from runtime.base import RuntimeRequest
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
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "claude")
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(profiles, "OPENAI_API_KEY", "")
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
