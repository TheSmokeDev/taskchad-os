"""Composite Codex model-only transport: protocol I/O fake, worker/evaluator real."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from personas.learning import evaluation, worker
from personas.learning.evaluation import runtime_reasoning as production_reasoning
from personas.learning.models import LearningTarget
from personas.learning.queue import LearningQueue
from personas.learning.service import LearningService
from runtime import lane_router
from runtime import openai_codex_app_server as app
from runtime.base import RuntimeRequest
from runtime.errors import RuntimeUnsupportedCapabilityError
from runtime.openai_codex import OpenAICodexRuntime
from runtime.profiles import RuntimeProfile


def request(tmp_path, **values):
    return RuntimeRequest(
        **(
            dict(
                prompt="Reason only about supplied evidence",
                cwd=tmp_path,
                task_name="cognition",
                model_only=True,
                disallowed_tools=["*"],
                allowed_tools=[],
                mcp_servers=[],
                hooks=None,
                setting_sources=[],
            )
            | values
        )
    )


def profile():
    return RuntimeProfile("fixture-codex", "openai-codex", "configured-model", command="codex")


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_CODEX_APP_SERVER_COMMAND", raising=False)
    monkeypatch.setattr(app, "resolve_codex_executable", lambda _: "fixture-codex")
    homes = []
    streams = []

    def home():
        root = tmp_path / f"isolated-{len(homes)}"
        (root / ".codex").mkdir(parents=True)
        (root / "empty").mkdir()
        result = SimpleNamespace(name=str(root), cleanup=lambda: None)
        homes.append(result)
        return result

    monkeypatch.setattr(app, "_isolated_codex_home", home)

    class Stream:
        def __init__(self, values=()):
            self.values = list(values)
            self.writes = []

        async def readline(self):
            return (json.dumps(self.values.pop(0)).encode() + b"\n") if self.values else b""

        def write(self, line):
            self.writes.append(json.loads(line))

        async def drain(self):
            pass

    class Process:
        def __init__(self, messages):
            self.stdout = Stream(messages)
            self.stderr = Stream()
            self.stdin = Stream()
            self.returncode = None
            self.pid = 1000

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        root = homes[-1].name
        messages = [
            {
                "id": 1,
                "result": {"userAgent": "the-homie/0.146.0 fixture", "codexHome": root + "/.codex"},
            },
            {
                "id": 2,
                "result": {
                    "thread": {"id": "thread"},
                    "model": "observed-model",
                    "modelProvider": "openai",
                    "sandbox": {"type": "readOnly", "networkAccess": False},
                    "approvalPolicy": "never",
                    "instructionSources": [],
                    "runtimeWorkspaceRoots": [],
                    "cwd": root + "/empty",
                },
            },
            {"id": 3, "result": {"turn": {"id": "turn"}}},
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": (
                            '{"supported":true,"contradictions_addressed":true,'
                            '"changes_behavior":false}'
                        ),
                    }
                },
            },
            {"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "completed"}}},
        ]
        process = Process(messages)
        streams.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return streams, Process


@pytest.mark.asyncio
async def test_real_composite_zero_tool_request_uses_pinned_isolated_appserver(tmp_path, transport):
    streams, _ = transport
    adapter = app.OpenAICodexAppServerRuntime(profile())
    assert adapter.supports_model_only()
    assert not OpenAICodexRuntime(profile()).supports_model_only()
    result = await adapter.run(request(tmp_path))
    assert result.model == "observed-model" and result.tool_call_count == 0
    assert result.metadata["model_only"]["tools"] == "none"
    assert result.metadata["model_only"]["output_limit_mode"] == "host_generated_content_bytes"
    assert not result.metadata["model_only"]["provider_token_budget_enforced"]
    start = next(x for x in streams[0].stdin.writes if x.get("method") == "thread/start")["params"]
    assert start["dynamicTools"] == start["environments"] == start["selectedCapabilityRoots"] == []
    assert start["sandbox"] == "read-only" and start["approvalPolicy"] == "never"
    assert streams[0].returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"max_budget_usd": 0.1},
        {"metadata": {"max_output_tokens": 1000}},
        {"image_paths": ["chart.png"]},
    ],
)
async def test_unsupported_budget_and_images_refused_before_spawn(tmp_path, transport, change):
    streams, _ = transport
    with pytest.raises(RuntimeUnsupportedCapabilityError):
        await app.OpenAICodexAppServerRuntime(profile()).run(request(tmp_path, **change))
    assert not streams


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"id": 9, "method": "item/tool/call", "params": {}},
        {"id": 9, "method": "item/commandExecution/requestApproval", "params": {}},
        {"method": "item/completed", "params": {"item": {"type": "dynamicToolCall"}}},
        {"method": "item/completed", "params": {"item": {"type": "fileChange"}}},
        {"id": 9, "method": "unknown/serverRequest", "params": {}},
    ],
)
async def test_zero_tool_protocol_rejects_every_tool_or_server_request(tmp_path, transport, event):
    _, process_type = transport
    client = app.CodexAppServerClient(request(tmp_path), profile(), executable="fixture")
    client._process = process_type([event])
    with pytest.raises(app.CodexAmbientAuthorityError):
        await client._read_message()


@pytest.mark.asyncio
async def test_model_only_aggregate_output_cap_fails_closed(tmp_path, transport):
    _, process_type = transport
    client = app.CodexAppServerClient(
        request(tmp_path, metadata={"max_output_bytes": 1024}), profile(), executable="fixture"
    )
    client._process = process_type(
        [{"method": "item/agentMessage/delta", "params": {"delta": "x" * 600}}] * 2
    )
    await client._read_message()
    with pytest.raises(app.CodexAppServerProtocolError, match="generated content byte limit"):
        await client._read_message()


@pytest.mark.asyncio
async def test_real_worker_support_judge_reaches_composite_codex_without_claude(
    tmp_path, monkeypatch, transport
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
    event = service.capture_experience("observed", "chat_engine", "Retain a chart observation")
    candidate = service.propose_candidate(
        {
            "candidate_type": "knowledge",
            "title": "Observation",
            "content": "RSI moved lower alongside price.",
            "applicability": "chart",
            "changes_behavior": False,
            "evidence_ids": [event["id"]],
        },
        source_key="source",
    )
    q = LearningQueue(service)
    while claimed := q.claim():
        q.finish_stage(claimed, status="completed", stage="done")
    q.enqueue("candidate", "support", payload={"candidate_id": candidate["id"]})
    claimed = q.claim()
    q.finish_stage(claimed, stage="evaluate")
    monkeypatch.setattr(lane_router, "_resolve_lane_profiles", lambda _: [profile()])
    monkeypatch.setattr(evaluation, "runtime_reasoning", production_reasoning)
    monkeypatch.setattr(evaluation, "learning_model_budget", lambda: None, raising=False)
    monkeypatch.delenv("PERSONA_LEARNING_MODEL_BUDGET_USD", raising=False)
    monkeypatch.delenv("CHAT_MAX_BUDGET_USD", raising=False)
    result = await worker.run_worker(service, max_stages=1, activity_path=tmp_path / "activity.db")
    assert result["status"] == "checkpointed"
    final = next(r for r in service.store.all("evaluation") if r.get("mode") == "knowledge_support")
    assert final["passed"] and final["support"]["grader"]["provider"] == "openai-codex"
    assert final["support"]["grader"]["model"] == "observed-model"
    assert len(transport[0]) == 1


@pytest.mark.asyncio
async def test_large_user_echo_and_metadata_do_not_consume_generated_output_budget(
    tmp_path, transport
):
    _, process_type = transport
    client = app.CodexAppServerClient(
        request(tmp_path, metadata={"max_output_bytes": 1024}), profile(), executable="fixture"
    )
    client._process = process_type(
        [
            {
                "method": "item/completed",
                "params": {"item": {"type": "userMessage", "text": "evidence" * 40000}},
            },
            {"method": "thread/tokenUsage/updated", "params": {"details": "context" * 40000}},
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "bounded conclusion"}},
            },
        ]
    )
    for _ in range(3):
        await client._read_message()
    assert client._output_bytes == len("bounded conclusion")
    assert client._wire_bytes > 262144
    assert client._wire_bytes < client._wire_byte_limit()


@pytest.mark.asyncio
async def test_wire_guard_still_rejects_metadata_stream_without_generated_text(tmp_path, transport):
    _, process_type = transport
    client = app.CodexAppServerClient(
        request(tmp_path, metadata={"max_output_bytes": 1024}), profile(), executable="fixture"
    )
    client._process = process_type(
        [{"method": "thread/tokenUsage/updated", "params": {"details": "x" * 400000}}] * 3
    )
    await client._read_message()
    await client._read_message()
    with pytest.raises(app.CodexAppServerProtocolError, match="wire byte limit"):
        await client._read_message()
    assert client._output_bytes == 0
