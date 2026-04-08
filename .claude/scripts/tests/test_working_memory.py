"""Tests for WorkingMemory — immutability, operations, region management.

Move 5b core tests. Proves the frozen dataclass + tuple pattern works.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.working_memory import Memory, WorkingMemory

# === Immutability ===


class TestImmutability:
    def test_wm_is_frozen(self):
        wm = WorkingMemory(soul_name="test")
        try:
            wm.soul_name = "mutated"
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass

    def test_memory_is_frozen(self):
        m = Memory(role="user", content="hello")
        try:
            m.content = "mutated"
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass

    def test_with_memory_returns_new_instance(self):
        wm = WorkingMemory(soul_name="test")
        wm2 = wm.with_memory(Memory(role="user", content="hi"))
        assert wm.length == 0
        assert wm2.length == 1
        assert wm is not wm2

    def test_empty_wm_operations_return_valid_wm(self):
        wm = WorkingMemory(soul_name="test")
        assert wm.slice(0).length == 0
        assert wm.filter(lambda m: True).length == 0
        assert wm.map(lambda m: m).length == 0
        assert wm.order_regions().length == 0
        assert wm.to_messages() == []


# === List operations ===


class TestListOperations:
    def _make_wm(self, n: int = 3) -> WorkingMemory:
        wm = WorkingMemory(soul_name="test")
        for i in range(n):
            wm = wm.with_memory(Memory(role="user", content=f"msg-{i}"))
        return wm

    def test_with_memory_appends(self):
        wm = self._make_wm(2)
        assert wm.length == 2
        assert wm.memories[0].content == "msg-0"
        assert wm.memories[1].content == "msg-1"

    def test_with_memory_autofills_id_and_timestamp(self):
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(role="user", content="hi"))
        assert wm.memories[0].id != ""
        assert wm.memories[0].timestamp != ""

    def test_slice(self):
        wm = self._make_wm(5)
        sliced = wm.slice(1, 3)
        assert sliced.length == 2
        assert sliced.memories[0].content == "msg-1"
        assert sliced.memories[1].content == "msg-2"

    def test_slice_preserves_soul_name(self):
        wm = self._make_wm(3)
        sliced = wm.slice(0, 1)
        assert sliced.soul_name == "test"

    def test_filter(self):
        wm = self._make_wm(4)
        filtered = wm.filter(lambda m: "1" in m.content or "3" in m.content)
        assert filtered.length == 2

    def test_concat(self):
        wm1 = self._make_wm(2)
        wm2 = WorkingMemory(soul_name="other")
        wm2 = wm2.with_memory(Memory(role="assistant", content="response"))
        combined = wm1.concat(wm2)
        assert combined.length == 3
        assert combined.soul_name == "test"  # Keeps first WM's metadata

    def test_map(self):
        from dataclasses import replace

        wm = self._make_wm(3)
        mapped = wm.map(lambda m: replace(m, content=m.content.upper()))
        assert mapped.memories[0].content == "MSG-0"
        assert wm.memories[0].content == "msg-0"  # Original unchanged

    def test_prepend(self):
        wm = self._make_wm(2)
        wm = wm.prepend(Memory(role="system", content="system prompt"))
        assert wm.length == 3
        assert wm.memories[0].role == "system"
        assert wm.memories[0].content == "system prompt"

    def test_with_monologue(self):
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_monologue("I think this is interesting")
        assert wm.length == 1
        assert wm.memories[0].role == "assistant"
        assert wm.memories[0].region == "internal"


# === Region operations ===


class TestRegionOperations:
    def _make_wm_with_regions(self) -> WorkingMemory:
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(role="system", content="soul", region="identity"))
        wm = wm.with_memory(Memory(role="system", content="user", region="user_model"))
        wm = wm.with_memory(Memory(role="user", content="hello", region="recent_conversation"))
        wm = wm.with_memory(Memory(role="system", content="recall", region="recalled_memory"))
        return wm

    def test_with_region_tags_all(self):
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(role="user", content="hi"))
        wm = wm.with_memory(Memory(role="assistant", content="hello"))
        tagged = wm.with_region("summary")
        assert all(m.region == "summary" for m in tagged.memories)

    def test_without_regions(self):
        wm = self._make_wm_with_regions()
        filtered = wm.without_regions("identity", "user_model")
        assert filtered.length == 2
        assert all(m.region not in ("identity", "user_model") for m in filtered.memories)

    def test_with_only_regions(self):
        wm = self._make_wm_with_regions()
        only = wm.with_only_regions("identity", "recalled_memory")
        assert only.length == 2
        assert all(m.region in ("identity", "recalled_memory") for m in only.memories)

    def test_order_regions(self):
        wm = self._make_wm_with_regions()
        ordered = wm.order_regions()
        regions = [m.region for m in ordered.memories]
        # identity should come before user_model, which should come before recalled_memory
        assert regions.index("identity") < regions.index("user_model")
        assert regions.index("user_model") < regions.index("recalled_memory")
        assert regions.index("recalled_memory") < regions.index("recent_conversation")


# === Conversion ===


class TestConversion:
    def test_to_messages(self):
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(role="system", content="You are helpful"))
        wm = wm.with_memory(Memory(role="user", content="Hello"))
        msgs = wm.to_messages()
        assert len(msgs) == 2
        assert msgs[0] == {"role": "system", "content": "You are helpful"}
        assert msgs[1] == {"role": "user", "content": "Hello"}

    def test_to_messages_with_name(self):
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(role="user", content="Hi", name="owner"))
        msgs = wm.to_messages()
        assert msgs[0]["name"] == "owner"

    def test_to_system_prompt(self):
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(
            role="system", content="I am the soul", region="identity",
        ))
        wm = wm.with_memory(Memory(
            role="system", content="User info", region="user_model",
        ))
        wm = wm.with_memory(Memory(
            role="user", content="Hello",  # Non-system should be skipped
        ))
        prompt = wm.to_system_prompt()
        assert "Identity" in prompt
        assert "I am the soul" in prompt
        assert "User Model" in prompt
        assert "User info" in prompt
        assert "Hello" not in prompt

    def test_repr(self):
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(role="system", content="hi", region="identity"))
        r = repr(wm)
        assert "test" in r
        assert "identity=1" in r


# === Memory operations ===


class TestMemory:
    def test_with_region(self):
        m = Memory(role="user", content="hello")
        m2 = m.with_region("summary")
        assert m.region is None
        assert m2.region == "summary"

    def test_with_metadata(self):
        m = Memory(role="user", content="hello")
        m2 = m.with_metadata("score", 0.95)
        assert m.metadata == ()
        assert dict(m2.metadata)["score"] == 0.95


# === WM Factory (from regions.py) ===


class TestWMFactory:
    def test_build_initial_working_memory(self):
        from cognition.regions import build_initial_working_memory

        vault_files = {
            "SOUL.md": "# Soul\nI am a test bot",
            "USER.md": "# User\nName: Test User",
        }
        wm = build_initial_working_memory(
            soul_name="test-bot",
            vault_files=vault_files,
            skill_index="- **skill-a**: Does thing A",
        )
        assert wm.soul_name == "test-bot"
        assert wm.length == 3  # soul + user + skill_index

        # Check regions
        regions = {m.region for m in wm.memories}
        assert "identity" in regions
        assert "user_model" in regions
        assert "procedural_memory" in regions

    def test_build_initial_wm_with_prefetched(self):
        from cognition.regions import build_initial_working_memory

        wm = build_initial_working_memory(
            soul_name="test",
            vault_files={"SOUL.md": "soul content"},
            prefetched_context="Lead data: 5 new leads today",
        )
        prefetched = [m for m in wm.memories if m.region == "prefetched_context"]
        assert len(prefetched) == 1
        assert "Lead data" in prefetched[0].content

    def test_build_initial_wm_empty(self):
        from cognition.regions import build_initial_working_memory

        wm = build_initial_working_memory(soul_name="empty", vault_files={})
        assert wm.length == 0
        assert wm.soul_name == "empty"


# === Integrator ===


class TestIntegrator:
    def test_integrate_perception_adds_user_message(self):
        import asyncio

        from cognition.integrator import integrate_perception

        wm = WorkingMemory(soul_name="test")

        result = asyncio.get_event_loop().run_until_complete(
            integrate_perception(wm, "Hello bot!")
        )
        assert result.length == 1
        assert result.memories[0].role == "user"
        assert result.memories[0].content == "Hello bot!"
        assert result.memories[0].region == "recent_conversation"

    def test_integrate_perception_adds_continuity(self):
        import asyncio

        from cognition.integrator import integrate_perception

        wm = WorkingMemory(soul_name="test")

        result = asyncio.get_event_loop().run_until_complete(
            integrate_perception(wm, "Hi", continuity_text="Focus: SEO audit")
        )
        continuity = [m for m in result.memories if m.region == "continuity"]
        assert len(continuity) == 1
        assert "SEO audit" in continuity[0].content

    def test_integrate_perception_prefetched_idempotent(self):
        import asyncio

        from cognition.integrator import integrate_perception

        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(
            role="system", content="existing", region="prefetched_context", source="router",
        ))

        result = asyncio.get_event_loop().run_until_complete(
            integrate_perception(wm, "Hi", prefetched_context="new data")
        )
        prefetched = [m for m in result.memories if m.region == "prefetched_context"]
        assert len(prefetched) == 1  # Did NOT add duplicate
        assert prefetched[0].content == "existing"


# === Runtime Bridge ===


class TestRuntimeBridge:
    def test_render_runtime_request_basic(self):
        from cognition.runtime_bridge import render_runtime_request

        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(
            role="system", content="I am helpful", region="identity",
        ))

        request = render_runtime_request(
            wm, "What is 2+2?", "claude", cwd="/tmp",
        )
        assert request.prompt == "What is 2+2?"
        assert request.task_name == "wm_transform"
        assert "I am helpful" in request.system_prompt["append"]

    def test_render_runtime_request_model_hint(self):
        """Processor arg maps to model hint on RuntimeRequest."""
        from cognition.runtime_bridge import render_runtime_request

        wm = WorkingMemory(soul_name="test")

        # "fast" processor should map to haiku model
        request = render_runtime_request(wm, "Hi", "fast", cwd="/tmp")
        assert request.model == "claude-haiku-4-5"

        # "quality" processor should map to sonnet model
        request = render_runtime_request(wm, "Hi", "quality", cwd="/tmp")
        assert request.model == "claude-sonnet-4-6"

        # "claude" processor should map to None (default)
        request = render_runtime_request(wm, "Hi", "claude", cwd="/tmp")
        assert request.model is None

    def test_render_runtime_request_includes_conversation(self):
        """Non-system memories should appear in system prompt as Recent Conversation."""
        from cognition.runtime_bridge import render_runtime_request

        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(
            role="system", content="I am helpful", region="identity",
        ))
        wm = wm.with_memory(Memory(
            role="user", content="What is 2+2?", region="recent_conversation",
        ))
        wm = wm.with_memory(Memory(
            role="assistant", content="It is 4.", region="recent_conversation",
        ))

        request = render_runtime_request(wm, "Follow up", "claude", cwd="/tmp")
        prompt_text = request.system_prompt["append"]
        assert "# Recent Conversation" in prompt_text
        assert "What is 2+2?" in prompt_text
        assert "It is 4." in prompt_text

    def test_render_runtime_request_includes_tool_outputs(self):
        """Tool memories should appear in Recent Conversation with tool label."""
        from cognition.runtime_bridge import render_runtime_request

        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(
            role="tool", content='{"results": [1]}',
            tool_name="search", region="recent_conversation",
        ))

        request = render_runtime_request(wm, "Next", "claude", cwd="/tmp")
        prompt_text = request.system_prompt["append"]
        assert "Tool (search)" in prompt_text
        assert '{"results": [1]}' in prompt_text

    def test_render_runtime_request_with_schema(self):
        from cognition.runtime_bridge import render_runtime_request

        wm = WorkingMemory(soul_name="test")
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

        request = render_runtime_request(
            wm, "Think", "claude", cwd="/tmp", schema=schema,
        )
        assert "JSON" in request.prompt

    def test_apply_runtime_result(self):
        from dataclasses import dataclass

        from cognition.runtime_bridge import apply_runtime_result

        @dataclass
        class FakeResult:
            text: str = '{"answer": "four"}'

        wm = WorkingMemory(soul_name="test")
        new_wm, value = apply_runtime_result(
            wm, FakeResult(), instruction="What is 2+2?",
        )
        # Should have added user + assistant messages
        assert new_wm.length == 2
        assert new_wm.memories[0].role == "user"
        assert new_wm.memories[1].role == "assistant"
        # Value should be parsed JSON
        assert isinstance(value, dict)
        assert value["answer"] == "four"

    def test_apply_runtime_result_plain_text(self):
        from dataclasses import dataclass

        from cognition.runtime_bridge import apply_runtime_result

        @dataclass
        class FakeResult:
            text: str = "Just a plain response"

        wm = WorkingMemory(soul_name="test")
        new_wm, value = apply_runtime_result(
            wm, FakeResult(), instruction="Hello",
        )
        assert isinstance(value, str)
        assert value == "Just a plain response"
