"""Tests for cognition.steps — reasoning step interface."""

from __future__ import annotations

from cognition.steps import (
    CognitiveContext,
    CognitiveStepResult,
    ReasoningStepResult,
    _extract_json,
)


def test_extract_json_raw():
    assert _extract_json('{"key": "value"}') == {"key": "value"}


def test_extract_json_code_block():
    text = '```json\n{"key": "value"}\n```'
    assert _extract_json(text) == {"key": "value"}


def test_extract_json_invalid():
    assert _extract_json("not json at all") is None


def test_extract_json_with_surrounding_text():
    text = 'Here is the result:\n```json\n{"a": 1}\n```\nDone.'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_array():
    text = '["item1", "item2", "item3"]'
    assert _extract_json(text) == ["item1", "item2", "item3"]


def test_extract_json_array_in_code_block():
    text = '```json\n["a", "b"]\n```'
    assert _extract_json(text) == ["a", "b"]


def test_extract_json_empty_object():
    assert _extract_json("{}") == {}


def test_extract_json_plain_string():
    """Plain string is not valid JSON object/array."""
    assert _extract_json('"just a string"') is None


def test_extract_json_number():
    """Number is not valid JSON object/array."""
    assert _extract_json("42") is None


def test_reasoning_step_result_defaults():
    r = ReasoningStepResult(
        output_text="hi", parsed=None, model="test", cost_usd=0.0, latency_ms=0.0
    )
    assert r.output_text == "hi"
    assert r.parsed is None
    assert r.model == "test"
    assert r.cost_usd == 0.0
    assert r.latency_ms == 0.0


def test_reasoning_step_result_with_parsed():
    r = ReasoningStepResult(
        output_text='{"a": 1}',
        parsed={"a": 1},
        model="claude-sonnet-4-6",
        cost_usd=0.01,
        latency_ms=500.0,
    )
    assert r.parsed == {"a": 1}
    assert r.cost_usd == 0.01


# === Move 3: CognitiveContext + CognitiveStepResult tests ===


def test_cognitive_context_defaults():
    ctx = CognitiveContext()
    assert ctx.active_process == "default"
    assert ctx.step_history == []
    assert ctx.internal_thoughts == []
    assert ctx.decisions == []
    assert ctx.session_id == ""
    assert ctx.turn_number == 0


def test_cognitive_context_custom():
    ctx = CognitiveContext(
        session_id="s-123",
        turn_number=5,
        active_process="planning",
    )
    assert ctx.session_id == "s-123"
    assert ctx.active_process == "planning"


def test_cognitive_context_mutability():
    ctx = CognitiveContext()
    ctx.internal_thoughts.append("thought 1")
    ctx.decisions.append("decision A")
    ctx.step_history.append({"type": "reflect"})
    assert len(ctx.internal_thoughts) == 1
    assert len(ctx.decisions) == 1
    assert len(ctx.step_history) == 1


def test_cognitive_step_result_reflect():
    r = CognitiveStepResult("reflect", "some thought", 100.0, "claude", 0.01)
    assert r.step_type == "reflect"
    assert r.output == "some thought"
    assert r.latency_ms == 100.0


def test_cognitive_step_result_query():
    r = CognitiveStepResult("query", True, 50.0)
    assert r.step_type == "query"
    assert r.output is True


def test_cognitive_step_result_brainstorm():
    r = CognitiveStepResult("brainstorm", ["idea1", "idea2"], 200.0)
    assert isinstance(r.output, list)
    assert len(r.output) == 2


def test_cognitive_step_result_defaults():
    r = CognitiveStepResult("decide", "option A")
    assert r.latency_ms == 0.0
    assert r.model == ""
    assert r.cost_usd == 0.0
