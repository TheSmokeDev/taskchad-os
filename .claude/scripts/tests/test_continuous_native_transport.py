"""Actual typed adapter seams; model providers are fake, targets temporary."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from personas.learning.models import LearningTarget
from personas.learning.service import LearningService
from runtime import claude_function_hooks as function_hooks
from runtime import image_input
from runtime.base import RuntimeRequest
from runtime.claude_sdk import ClaudeSdkRuntime
from runtime.profiles import RuntimeProfile


def service(tmp_path):
    return LearningService(
        LearningTarget(
            "example",
            tmp_path / "memory",
            tmp_path / "data",
            tmp_path / "state",
            tmp_path / "skills",
        )
    )


def test_bound_native_events_reject_spoof_and_dedupe(tmp_path, monkeypatch):
    from personas.learning import service as module

    s = service(tmp_path)
    ex = s.capture_experience("origin", "test", "Inspect a local document")
    monkeypatch.setattr(module, "get_learning_service", lambda name: s)
    monkeypatch.setattr(function_hooks, "_root", lambda: tmp_path / "contexts")
    monkeypatch.setattr(function_hooks, "probe", lambda _: {"supported": True})
    request = RuntimeRequest(
        prompt="Inspect",
        cwd=tmp_path,
        task_name="test",
        metadata={
            "persona_id": "example",
            "learning": {"experience_id": ex["id"], "origin_key": "origin"},
        },
    )
    options, receipt = function_hooks.prepare(request, cli_path="fake-cli")
    env = options["env"]
    token, context = env["HOMIE_COGNITION_HOOK_TOKEN"], env["HOMIE_COGNITION_HOOK_CONTEXT"]
    first = function_hooks.ingest(context, token, {"event": "session.start"}, service=s)
    again = function_hooks.ingest(context, token, {"event": "session.start"}, service=s)
    assert first == again
    assert len(s.store.all("cognitive_cycle")) == 1
    with pytest.raises(ValueError, match="credential"):
        function_hooks.ingest(context, "wrong", {"event": "session.start"}, service=s)
    with pytest.raises(ValueError, match="persona"):
        function_hooks.ingest(
            context, token, {"event": "session.start", "persona_id": "other"}, service=s
        )
    function_hooks.ingest(
        context,
        token,
        {
            "event": "tool.call",
            "event_id": "t1",
            "details": {"result": "observed", "authorization": "opaque-secret"},
        },
        service=s,
    )
    assert "opaque-secret" not in json.dumps(s.store.all("observation"))
    assert function_hooks.finish(receipt, s)["events"] == ["session.start", "tool.call"]
    with pytest.raises(ValueError, match="expired"):
        function_hooks.ingest(context, token, {"event": "session.start"}, service=s)


def test_model_only_never_probes_or_loads_mods(tmp_path, monkeypatch):
    monkeypatch.setattr(
        function_hooks, "probe", lambda _: pytest.fail("probe grants plugin activity")
    )
    options, receipt = function_hooks.prepare(
        RuntimeRequest(
            prompt="Read supplied data", cwd=tmp_path, task_name="study", model_only=True
        )
    )
    assert options == {}
    assert receipt["reason"] == "model_only_host_callbacks"


def test_image_hash_and_format_are_checked_before_delivery(tmp_path):
    p = tmp_path / "chart.png"
    Image.new("RGB", (12, 12), "red").save(p)
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    request = RuntimeRequest(
        prompt="Read chart",
        cwd=tmp_path,
        task_name="test",
        image_paths=[p],
        metadata={"image_input_hashes": [digest]},
    )
    blocks, receipts = image_input.image_blocks(request)
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert receipts[0] == {"sha256": digest, "mime_type": "image/png", "delivered": False}
    p.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        image_input.image_blocks(request)


@pytest.mark.asyncio
async def test_sdk_supplies_images_without_tools_and_records_actual_delivery(tmp_path, monkeypatch):
    import claude_agent_sdk

    from runtime import claude_sdk

    capture = {}

    class Options:
        def __init__(self, **kwargs):
            capture.update(kwargs)

    class Result:
        session_id = "session"
        total_cost_usd = None
        subtype = "success"
        is_error = False
        result = "Chart read"

    async def query(prompt, options):
        capture["messages"] = [message async for message in prompt]
        yield SimpleNamespace(subtype="init", data={"model": "actual-model"})
        yield Result()

    monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentOptions", Options)
    monkeypatch.setattr(claude_agent_sdk, "ResultMessage", Result)
    monkeypatch.setattr(claude_agent_sdk, "query", query)
    monkeypatch.setattr(claude_sdk, "_ensure_system_cli_patch", lambda: None)
    p = tmp_path / "chart.png"
    Image.new("RGB", (12, 12), "blue").save(p)
    request = RuntimeRequest(
        prompt="Read frozen chart",
        cwd=tmp_path,
        task_name="test",
        image_paths=[p],
        model_only=True,
        allowed_tools=[],
        disallowed_tools=["*"],
        setting_sources=[],
        mcp_servers=[],
    )
    result = await ClaudeSdkRuntime(
        RuntimeProfile(key="test", provider="claude", model="alias")
    ).run(request)
    assert capture["tools"] == [] and capture["plugins"] == [] and capture["setting_sources"] == []
    assert capture["messages"][0]["message"]["content"][1]["type"] == "image"
    assert result.model == "actual-model"
    assert result.metadata["image_inputs"][0]["delivered"] is True


def test_framework_hooks_are_model_independent_and_wrap_once(monkeypatch):
    import sys

    from runtime import function_hooks as framework

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    monkeypatch.setattr(framework, "_HANDLERS", {})
    calls = []

    class Service:
        target = SimpleNamespace(persona_id="demo")

        def enqueue_cognitive_cycle(self, phase, origin, evidence, **kwargs):
            calls.append((phase, origin, evidence, kwargs))
            return {"id": "one-cycle"}

    def hook(event, next_handler):
        assert event.metadata["provider"] in {"codex", "kimi", "gemini"}
        return next_handler()

    framework.register_hook("work.start", hook, key="fixture")
    for provider in ["codex", "kimi", "gemini"]:
        event = framework.FunctionHookEvent(
            "work.start", "demo", provider, ("e1",), metadata={"provider": provider}
        )
        assert framework.emit(event, service=Service()) == {"id": "one-cycle"}
    assert len(calls) == 3


def test_native_failed_callback_cannot_appear_processed(tmp_path, monkeypatch):
    from personas.learning import service as module

    s = service(tmp_path)
    ex = s.capture_experience("native-fail", "test", "Inspect evidence")
    monkeypatch.setattr(module, "get_learning_service", lambda name: s)
    monkeypatch.setattr(function_hooks, "_root", lambda: tmp_path / "contexts")
    monkeypatch.setattr(function_hooks, "probe", lambda _: {"supported": True})
    request = RuntimeRequest(
        prompt="Inspect",
        cwd=tmp_path,
        task_name="test",
        metadata={
            "persona_id": "example",
            "learning": {"experience_id": ex["id"], "origin_key": "native-fail"},
        },
    )
    options, receipt = function_hooks.prepare(request, cli_path="fake-cli")
    with monkeypatch.context() as failed:
        failed.setattr(
            s, "enqueue_cognitive_cycle", lambda *a, **k: (_ for _ in ()).throw(OSError("busy"))
        )
        with pytest.raises(OSError):
            function_hooks.ingest(
                receipt["context_id"],
                options["env"]["HOMIE_COGNITION_HOOK_TOKEN"],
                {"event": "session.start"},
                service=s,
            )
    result = function_hooks.finish(receipt, s)
    assert result["events"] == [] and result["coverage"] == "partial"
    assert "session.start" in result["missing_events"]


@pytest.mark.asyncio
async def test_sdk_missing_terminal_result_is_retryable(tmp_path, monkeypatch):
    import claude_agent_sdk

    from runtime import claude_sdk
    from runtime.errors import RuntimeRetryableError

    async def query(prompt, options):
        if False:
            yield None

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    monkeypatch.setattr(claude_sdk, "_ensure_system_cli_patch", lambda: None)
    request = RuntimeRequest(prompt="Read data", cwd=tmp_path, task_name="test")
    with pytest.raises(RuntimeRetryableError, match="terminal"):
        await ClaudeSdkRuntime(RuntimeProfile(key="test", provider="claude", model="alias")).run(
            request
        )


@pytest.mark.asyncio
async def test_ordinary_native_settings_defaults_and_explicit_sources_remain_compatible(
    tmp_path, monkeypatch
):
    import claude_agent_sdk

    from runtime import claude_sdk

    captured = []

    class Options:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class Result:
        session_id = "fixture"
        total_cost_usd = None
        subtype = "success"
        is_error = False
        result = "Completed"

    async def query(prompt, options):
        yield Result()

    monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentOptions", Options)
    monkeypatch.setattr(claude_agent_sdk, "ResultMessage", Result)
    monkeypatch.setattr(claude_agent_sdk, "query", query)
    monkeypatch.setattr(claude_sdk, "_ensure_system_cli_patch", lambda: None)
    adapter = ClaudeSdkRuntime(
        RuntimeProfile(key="fixture", provider="claude", model="test")
    )
    await adapter.run(
        RuntimeRequest(prompt="Ordinary turn", cwd=tmp_path, task_name="chat")
    )
    await adapter.run(
        RuntimeRequest(
            prompt="Explicit sources",
            cwd=tmp_path,
            task_name="chat",
            setting_sources=["user"],
        )
    )
    await adapter.run(
        RuntimeRequest(
            prompt="Strict cognition",
            cwd=tmp_path,
            task_name="study",
            model_only=True,
            disallowed_tools=["*"],
        )
    )
    assert "setting_sources" not in captured[0]
    assert captured[1]["setting_sources"] == ["user"]
    assert captured[2]["setting_sources"] == [] and captured[2]["tools"] == []
