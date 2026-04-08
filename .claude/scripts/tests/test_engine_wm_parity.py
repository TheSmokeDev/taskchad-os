"""Parity tests for engine WM migration.

Move 5b: Proves the WorkingMemory path produces equivalent state to
the legacy prompt-assembly path. Tests runtime bridge rendering,
WM factory output, and cross-profile execution readiness.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.regions import PromptRegion, assemble_regions, build_initial_working_memory
from cognition.working_memory import Memory, WorkingMemory


class TestRuntimeBridgeRendering:
    """render_runtime_request produces valid RuntimeRequests from WM state."""

    def test_empty_wm_produces_valid_request(self):
        from cognition.runtime_bridge import render_runtime_request

        wm = WorkingMemory(soul_name="test")
        request = render_runtime_request(wm, "Hello", "claude", cwd="/tmp")
        assert request.prompt == "Hello"
        assert request.task_name == "wm_transform"
        assert request.max_turns == 1

    def test_wm_system_prompt_matches_region_assembly(self):
        """WM.to_system_prompt() should produce content equivalent to assemble_regions()."""
        # Build via legacy regions
        regions = [
            PromptRegion("identity", "I am a helpful assistant", 4000, frozen=True),
            PromptRegion("user_model", "User is a developer", 3000, frozen=True),
        ]
        legacy_prompt = assemble_regions(regions)

        # Build via WM
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(
            role="system", content="I am a helpful assistant", region="identity",
        ))
        wm = wm.with_memory(Memory(
            role="system", content="User is a developer", region="user_model",
        ))
        wm_prompt = wm.to_system_prompt()

        # Both should contain the same content
        assert "I am a helpful assistant" in legacy_prompt
        assert "I am a helpful assistant" in wm_prompt
        assert "User is a developer" in legacy_prompt
        assert "User is a developer" in wm_prompt

    def test_instruction_as_memory_object(self):
        from cognition.runtime_bridge import render_runtime_request

        wm = WorkingMemory(soul_name="test")
        instruction = Memory(role="user", content="Think about X")
        request = render_runtime_request(wm, instruction, "claude", cwd="/tmp")
        assert request.prompt == "Think about X"


class TestWMFactoryParity:
    """build_initial_working_memory produces WM equivalent to _build_frozen_regions."""

    def test_factory_includes_all_vault_files(self):
        vault = {
            "SOUL.md": "Soul content",
            "SELF.md": "Self model",
            "USER.md": "User profile",
            "MEMORY.md": "Key decisions",
        }
        wm = build_initial_working_memory("test", vault)
        assert wm.length == 4
        regions = {m.region for m in wm.memories}
        assert regions == {"identity", "self_model", "user_model", "durable_memory"}

    def test_factory_skips_empty_files(self):
        vault = {"SOUL.md": "content", "USER.md": ""}
        wm = build_initial_working_memory("test", vault)
        assert wm.length == 1  # Only SOUL.md

    def test_factory_preserves_source(self):
        vault = {"SOUL.md": "content"}
        wm = build_initial_working_memory("test", vault)
        assert wm.memories[0].source == "vault"


class TestPrefetchedContextParity:
    """Router-provided prefetched context survives as explicit WM state."""

    def test_prefetched_is_first_class_memory(self):
        wm = build_initial_working_memory(
            "test",
            {"SOUL.md": "soul"},
            prefetched_context="5 new leads today",
        )
        prefetched = [m for m in wm.memories if m.region == "prefetched_context"]
        assert len(prefetched) == 1
        assert prefetched[0].source == "router"
        assert "5 new leads" in prefetched[0].content

    def test_prefetched_not_silently_dropped(self):
        """Prefetched context must survive order_regions and to_system_prompt."""
        wm = build_initial_working_memory(
            "test",
            {"SOUL.md": "soul"},
            prefetched_context="Lead data here",
        )
        wm = wm.order_regions()
        prompt = wm.to_system_prompt()
        assert "Lead data here" in prompt


class TestToolResultParity:
    """Tool outputs round-trip through WM without flattening."""

    def test_tool_memory_roundtrip(self):
        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(
            role="tool",
            content='{"results": [1, 2, 3]}',
            tool_name="search",
            tool_call_id="tc_123",
            region="recent_conversation",
            source="tool",
        ))

        msgs = wm.to_messages()
        assert msgs[0]["role"] == "tool"
        assert msgs[0]["content"] == '{"results": [1, 2, 3]}'

        # Tool memory survives filter
        filtered = wm.filter(lambda m: m.role == "tool")
        assert filtered.length == 1
        assert filtered.memories[0].tool_name == "search"


class TestObservabilityMetrics:
    """5b observability dataclasses work correctly."""

    def test_transform_log(self):
        from cognition.observability import TransformLog, log_transform_event

        log = TransformLog(
            latency_ms=150.0,
            wm_size=12,
            processor="claude",
            success=True,
        )
        log_transform_event(log)  # Should not raise

    def test_process_transition_log(self):
        from cognition.observability import ProcessTransitionLog, log_process_transition

        log = ProcessTransitionLog(
            from_process="default",
            to_process="planning",
            trigger="planning_signal",
            session_id="test:123",
        )
        log_process_transition(log)

    def test_cognition_metrics(self):
        from cognition.observability import CognitionMetrics

        metrics = CognitionMetrics(
            session_id="test",
            total_transforms=5,
            avg_transform_latency_ms=120.5,
            wm_high_water_mark=25,
        )
        assert metrics.total_transforms == 5
        assert metrics.wm_high_water_mark == 25


class TestCognitiveStepFactory:
    """create_cognitive_step produces callable steps."""

    def test_factory_returns_callable(self):
        from cognition.steps import create_cognitive_step

        step = create_cognitive_step(command="Think about this")
        assert callable(step)

    def test_builtin_steps_exist(self):
        from cognition.steps import (
            brainstorm,
            decide,
            external_dialog,
            internal_monologue,
            mental_query,
        )
        assert callable(external_dialog)
        assert callable(internal_monologue)
        assert callable(brainstorm)
        assert callable(decide)
        assert callable(mental_query)


class TestExecutableProcesses:
    """Execute_process returns callable for each MentalProcess."""

    def test_all_processes_have_functions(self):
        from cognition.processes import (
            MentalProcess,
            execute_process,
        )
        for process in MentalProcess:
            fn = execute_process(process)
            assert callable(fn), f"No function for {process}"

    def test_execute_process_returns_callable(self):
        from cognition.processes import MentalProcess, execute_process

        fn = execute_process(MentalProcess.PLANNING)
        assert callable(fn)
        assert fn.__name__ == "planning_process"
