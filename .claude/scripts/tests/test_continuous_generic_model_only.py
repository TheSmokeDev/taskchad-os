"""Zero-tool generic cognition over real adapters with only provider I/O faked."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from personas.learning import hooks
from personas.learning.models import LearningTarget
from personas.learning.service import LearningService
from personas.learning.worker import _runtime_role as production_runtime_role
from runtime import openai_codex, registry, selection
from runtime.auth_profiles import AuthProfileStatus
from runtime.base import RuntimeRequest
from runtime.errors import (
    RuntimeConfigError,
    RuntimeExecutionError,
    RuntimeUnsupportedCapabilityError,
)
from runtime.openai_compatible import OpenAICompatibleRuntime, _model_only_output_limit
from runtime.profiles import RuntimeProfile


def request(tmp_path, **kwargs):
    values = dict(
        prompt="Interpret chart evidence",
        cwd=tmp_path,
        task_name="cognitive",
        model_only=True,
        allowed_tools=[],
        disallowed_tools=["*"],
        mcp_servers=[],
        setting_sources=[],
        hooks=None,
        model="native-only-hint",
    )
    return RuntimeRequest(**(values | kwargs))


def profile(provider):
    return RuntimeProfile(
        key="test-" + provider,
        provider=provider,
        model="configured-model",
        api_key="test-placeholder",
        base_url="https://example.invalid/v1",
    )


def completion(text="Retained understanding", **kwargs):
    message = SimpleNamespace(content=text, tool_calls=[], function_call=None)
    values = dict(
        model="actual-provider-model",
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(completion_tokens=14),
    )
    return SimpleNamespace(**(values | kwargs))


def response(text="Retained understanding", **kwargs):
    values = dict(
        model="actual-provider-model",
        status="completed",
        output_text=text,
        output=[
            SimpleNamespace(
                type="message", content=[SimpleNamespace(type="output_text", text=text)]
            )
        ],
        usage=SimpleNamespace(input_tokens=20, output_tokens=14, total_tokens=34),
    )
    return SimpleNamespace(**(values | kwargs))


def install_client(monkeypatch, answer):
    import openai

    captured = []

    async def create(**kwargs):
        captured.append(kwargs)
        return answer

    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        responses=SimpleNamespace(create=create),
    )
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: fake)
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["kimi", "openai-compatible"])
async def test_no_tools_wire_budget_and_actual_model_receipt(tmp_path, monkeypatch, provider):
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_MAX_OUTPUT_TOKENS", raising=False)
    captured = install_client(monkeypatch, completion() if provider == "kimi" else response())
    adapter = OpenAICompatibleRuntime(profile(provider))
    assert adapter.supports_model_only() is True
    result = await adapter.run(request(tmp_path))
    assert len(captured) == 1
    call = captured[0]
    assert call["model"] == "configured-model"
    assert call["tools"] == [] and call["tool_choice"] == "none"
    ceiling_key = "max_completion_tokens" if provider == "kimi" else "max_output_tokens"
    assert call[ceiling_key] == 4096
    assert "functions" not in call and "previous_response_id" not in call
    assert result.model == "actual-provider-model" and result.provider == provider
    assert result.cost_usd is None and result.tool_call_count == 0
    assert result.metadata["model_only"]["max_output_tokens"] == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["kimi", "openai-compatible"])
async def test_non_null_dollar_budget_refused_before_any_provider_call(
    tmp_path, monkeypatch, provider
):
    captured = install_client(monkeypatch, completion())
    with pytest.raises(RuntimeUnsupportedCapabilityError, match="USD budget"):
        await OpenAICompatibleRuntime(profile(provider)).run(request(tmp_path, max_budget_usd=0.20))
    assert not captured


@pytest.mark.parametrize(
    "override,ceiling,expected",
    [
        (None, None, 4096),
        (16000, None, 16000),
        (512, None, 512),
        (16000, "8000", 8000),
        (512, "8000", 512),
        (None, "8000", 8000),
    ],
)
def test_output_limit_respects_explicit_larger_request_and_installation_ceiling(
    tmp_path, monkeypatch, override, ceiling, expected
):
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_MAX_OUTPUT_TOKENS", raising=False)
    if ceiling is not None:
        monkeypatch.setenv("SECOND_BRAIN_GENERIC_MAX_OUTPUT_TOKENS", ceiling)
    assert (
        _model_only_output_limit(request(tmp_path, metadata={"max_output_tokens": override}))
        == expected
    )


@pytest.mark.parametrize("override", [0, -1, True, "4096", 1.5])
def test_invalid_output_limit_refused(tmp_path, override):
    with pytest.raises(RuntimeConfigError):
        _model_only_output_limit(request(tmp_path, metadata={"max_output_tokens": override}))


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["tool_calls", "legacy_function", "truncated"])
async def test_chat_refuses_unexpected_tool_or_incomplete_output(tmp_path, monkeypatch, shape):
    answer = completion()
    if shape == "tool_calls":
        answer.choices[0].message.tool_calls = [SimpleNamespace(id="unexpected", function="shell")]
    elif shape == "legacy_function":
        answer.choices[0].message.function_call = {"name": "shell"}
    else:
        answer.choices[0].finish_reason = "length"
    captured = install_client(monkeypatch, answer)
    with pytest.raises(RuntimeExecutionError, match="unexpected tool activity or incomplete"):
        await OpenAICompatibleRuntime(profile("kimi")).run(request(tmp_path))
    assert len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["function_call", "web_search_call", "incomplete"])
async def test_responses_refuses_any_tool_output_or_incomplete_result(tmp_path, monkeypatch, shape):
    answer = response()
    if shape == "incomplete":
        answer.status = "incomplete"
    else:
        answer.output.append(SimpleNamespace(type=shape))
    captured = install_client(monkeypatch, answer)
    with pytest.raises(RuntimeExecutionError):
        await OpenAICompatibleRuntime(profile("openai-compatible")).run(request(tmp_path))
    assert len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["kimi", "openai-compatible"])
async def test_exact_frozen_image_bytes_reach_generic_wire_with_no_tools(
    tmp_path, monkeypatch, provider
):
    image = tmp_path / "snapshot.png"
    Image.new("RGB", (12, 12), "red").save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    captured = install_client(monkeypatch, completion() if provider == "kimi" else response())
    result = await OpenAICompatibleRuntime(profile(provider)).run(
        request(tmp_path, image_paths=[image], metadata={"image_input_hashes": [digest]})
    )
    call = captured[0]
    content = call["messages"][-1]["content"] if provider == "kimi" else call["input"][0]["content"]
    image_block = content[1]
    url = image_block["image_url"]["url"] if provider == "kimi" else image_block["image_url"]
    import base64

    assert hashlib.sha256(base64.b64decode(url.split(",", 1)[1])).hexdigest() == digest
    assert result.metadata["image_inputs"][0]["sha256"] == digest
    assert result.metadata["image_inputs"][0]["delivered"] is True
    assert call["tool_choice"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lane,expected", [("generic_runtime", None), ("claude_native", "native-quality")]
)
async def test_worker_role_uses_native_hint_only_for_native_selection(
    tmp_path, monkeypatch, lane, expected
):
    import config

    monkeypatch.setattr(
        selection, "resolve_runtime_selection", lambda: selection.RuntimeSelection(lane=lane)
    )
    monkeypatch.setattr(config, "get_background_models", lambda: {"quality": "native-quality"})
    service = LearningService(
        LearningTarget(
            "crypto",
            tmp_path / "memory",
            tmp_path / "data",
            tmp_path / "state",
            tmp_path / "skills",
        )
    )
    captured = []

    async def model(value):
        captured.append(value)
        return SimpleNamespace(
            text='{"candidate":null}',
            model="actual",
            provider="kimi",
            runtime_lane="generic_runtime",
            profile_key="fake",
            cost_usd=None,
            tool_calls=[],
            tool_call_count=0,
            tool_names_used=[],
        )

    monkeypatch.setattr(registry, "run_with_fallback", model)
    await production_runtime_role(
        service, {"id": "example", "payload": {}}, "propose", "Investigate"
    )
    assert captured[0].model == expected


@pytest.mark.asyncio
async def test_same_persona_retained_understanding_reaches_kimi_and_codex_adapters(
    tmp_path, monkeypatch
):
    service = LearningService(
        LearningTarget(
            "crypto",
            tmp_path / "memory",
            tmp_path / "data",
            tmp_path / "state",
            tmp_path / "skills",
        )
    )
    evidence = service.capture_experience("chart-observed", "chat_engine", "Reversal chart")
    understanding = service.record_understanding(
        {
            "understanding_type": "belief",
            "title": "Reversal conditions",
            "content": "A reversal needs another closed candle.",
            "scope": "reversal chart",
            "uncertainty": "One observation only.",
            "evidence_ids": [evidence["id"]],
        },
        source_key="chart-belief",
    )
    captured = install_client(monkeypatch, completion("Kimi chart response"))
    kimi_turn = hooks.prepare_turn(
        request(tmp_path, prompt="Explain reversal chart"),
        persona_id="crypto",
        surface="chat_engine",
        origin_id="kimi-turn",
        service=service,
    )
    kimi_result = await OpenAICompatibleRuntime(profile("kimi")).run(kimi_turn.request)
    kimi_turn.complete(kimi_result)
    assert understanding["content"] in captured[0]["messages"][-1]["content"]

    monkeypatch.setattr(
        openai_codex, "codex_auth_status", lambda _: AuthProfileStatus(True, "fixture")
    )
    monkeypatch.setattr(openai_codex, "_reserve_output_path", lambda: tmp_path / "codex-last.txt")
    seen = []

    async def start(*args, **kwargs):
        class Process:
            returncode = 0

            async def communicate(self, data):
                seen.append(data.decode())
                Path(args[args.index("--output-last-message") + 1]).write_text(
                    "Codex chart response"
                )
                return b'{"type":"thread.started","thread_id":"fixture"}\n', b""

        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    codex = openai_codex.OpenAICodexRuntime(
        RuntimeProfile("codex-fixture", "openai-codex", "codex-model", command="codex")
    )
    assert codex.supports_model_only() is False  # Native shell constraint remains unchanged.
    codex_turn = hooks.prepare_turn(
        request(tmp_path, prompt="Explain reversal chart", model_only=False),
        persona_id="crypto",
        surface="chat_engine",
        origin_id="codex-turn",
        service=service,
    )
    codex_result = await codex.run(codex_turn.request)
    codex_turn.complete(codex_result)
    assert understanding["content"] in seen[0]
    delivered = [r for r in service.store.all("context") if r.get("phase") == "executed"]
    assert {r["provider"] for r in delivered} == {"kimi", "openai-codex"}
    assert all(
        any(i.get("record_id") == understanding["id"] for i in r["included"]) for r in delivered
    )
