"""Env hygiene on the claude_native SDK path (issue #137).

The SDK subprocess transport merges the FULL parent os.environ into the child
CLI process; options.env can only OVERRIDE a key, never remove one. An
inherited ANTHROPIC_API_KEY would silently bypass the Max-plan OAuth path and
move billing off the subscription. ClaudeSdkRuntime therefore:

  - strips any caller-provided ANTHROPIC_API_KEY from request.env, and
  - neutralizes an inherited one with "" (falsy to the Node CLI) when it is
    actually present in the parent env,

while keeping the default deployment (no key, no request.env) byte-identical.

Capture pattern mirrors tests/test_runtime_request_cabinet_fields.py — patch the
SDK symbols so the options-dict construction is visible to the test (no real
subprocess / CLI is spawned).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from runtime.base import RuntimeRequest
from runtime.claude_sdk import ClaudeSdkRuntime
from runtime.profiles import RuntimeProfile


def _capture_options() -> tuple[dict[str, object], type, object]:
    """Return (captured-kwargs dict, dummy options class, empty async query)."""
    captured: dict[str, object] = {}

    class _DummyOptions:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def _empty_query(prompt, options):  # noqa: ARG001
        # Async iterator returning nothing — exits the async-for cleanly.
        if False:
            yield None

    return captured, _DummyOptions, _empty_query


def _runtime() -> ClaudeSdkRuntime:
    profile = RuntimeProfile(
        key="primary-claude",
        provider="claude",
        model="claude-haiku-4-5-20251001",
    )
    return ClaudeSdkRuntime(profile)


async def _run_and_capture(request: RuntimeRequest) -> dict[str, object]:
    captured, dummy_options, empty_query = _capture_options()
    with (
        patch("claude_agent_sdk.ClaudeAgentOptions", dummy_options),
        patch("claude_agent_sdk.query", empty_query),
    ):
        await _runtime().run(request)
    return captured


@pytest.mark.asyncio
async def test_inherited_anthropic_key_neutralized(monkeypatch) -> None:
    """Parent env has ANTHROPIC_API_KEY → options.env overrides it to ""."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "<REDACTED-anthropic>")
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="env_hygiene",
        capability="text_reasoning",
        max_turns=1,
        env={"CLAUDECODE": ""},
    )

    captured = await _run_and_capture(request)

    assert captured["env"]["ANTHROPIC_API_KEY"] == ""
    assert captured["env"]["CLAUDECODE"] == ""  # existing override survives


@pytest.mark.asyncio
async def test_request_env_anthropic_key_stripped(monkeypatch) -> None:
    """A caller-provided ANTHROPIC_API_KEY never reaches the options dict verbatim."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="env_hygiene",
        capability="text_reasoning",
        max_turns=1,
        env={"ANTHROPIC_API_KEY": "sk-evil"},
    )

    captured = await _run_and_capture(request)

    assert captured["env"].get("ANTHROPIC_API_KEY", "") == ""


@pytest.mark.asyncio
async def test_request_env_key_stripped_case_insensitively(monkeypatch) -> None:
    """Windows env keys are case-insensitive: a lowercase/mixed-case caller key
    must be stripped too, or it becomes THE key for the child (#137 gate)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="env_hygiene",
        capability="text_reasoning",
        max_turns=1,
        env={"anthropic_api_key": "sk-evil", "Anthropic_Api_Key": "sk-evil-2"},
    )

    captured = await _run_and_capture(request)

    assert all(k.upper() != "ANTHROPIC_API_KEY" for k in captured["env"]), (
        f"case-variant key survived the strip: {list(captured['env'])}"
    )


@pytest.mark.asyncio
async def test_no_key_no_env_change(monkeypatch) -> None:
    """No parent key + no request.env → options omit env entirely (byte-parity)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="env_hygiene",
        capability="text_reasoning",
        max_turns=1,
    )

    captured = await _run_and_capture(request)

    assert "env" not in captured
