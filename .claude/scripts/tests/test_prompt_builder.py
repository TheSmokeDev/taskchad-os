"""Tests for runtime.prompt_builder — shared CLI prompt rendering."""

from __future__ import annotations

from runtime.base import RuntimeRequest
from runtime.capabilities import TEXT_REASONING, TOOL_REASONING
from runtime.prompt_builder import INTEGRATION_HINTS, render_cli_prompt


def test_tool_reasoning_includes_preamble_and_hints() -> None:
    req = RuntimeRequest(
        prompt="do it", cwd=".", task_name="test", capability=TOOL_REASONING
    )
    result = render_cli_prompt(req, framework_tool_map="Framework tool map:\n- skill")
    assert "The Homie runtime layer" in result
    assert "Key integrations" in result
    assert "Framework tool map" in result
    assert "User task:" in result


def test_text_reasoning_excludes_hints() -> None:
    req = RuntimeRequest(
        prompt="think", cwd=".", task_name="test", capability=TEXT_REASONING
    )
    result = render_cli_prompt(req)
    assert "safe text-only" in result
    assert "Key integrations" not in result


def test_system_prompt_string_injected() -> None:
    req = RuntimeRequest(
        prompt="go", cwd=".", task_name="t", system_prompt="Be concise."
    )
    result = render_cli_prompt(req)
    assert "System context:\nBe concise." in result


def test_system_prompt_dict_extracts_append() -> None:
    req = RuntimeRequest(
        prompt="go", cwd=".", task_name="t", system_prompt={"append": "Stay focused."}
    )
    result = render_cli_prompt(req)
    assert "System context:\nStay focused." in result


def test_system_prompt_none_skipped() -> None:
    req = RuntimeRequest(prompt="go", cwd=".", task_name="t", system_prompt=None)
    result = render_cli_prompt(req)
    assert "System context" not in result


def test_system_prompt_empty_string_skipped() -> None:
    req = RuntimeRequest(prompt="go", cwd=".", task_name="t", system_prompt="   ")
    result = render_cli_prompt(req)
    assert "System context" not in result


def test_system_prompt_dict_empty_append_skipped() -> None:
    req = RuntimeRequest(
        prompt="go", cwd=".", task_name="t", system_prompt={"append": "  "}
    )
    result = render_cli_prompt(req)
    assert "System context" not in result


def test_system_prompt_dict_no_append_key_skipped() -> None:
    req = RuntimeRequest(
        prompt="go", cwd=".", task_name="t", system_prompt={"preset": "claude_code"}
    )
    result = render_cli_prompt(req)
    assert "System context" not in result


def test_task_name_and_prompt_always_present() -> None:
    req = RuntimeRequest(prompt="hello world", cwd=".", task_name="my_task")
    result = render_cli_prompt(req)
    assert "Task name: my_task" in result
    assert "User task:\nhello world" in result


def test_custom_preamble_overrides_default() -> None:
    req = RuntimeRequest(
        prompt="go", cwd=".", task_name="t", capability=TOOL_REASONING
    )
    result = render_cli_prompt(req, tool_preamble="Custom agent preamble.")
    assert "Custom agent preamble." in result
    assert "The Homie runtime layer" not in result


def test_custom_integration_hints_override() -> None:
    req = RuntimeRequest(
        prompt="go", cwd=".", task_name="t", capability=TOOL_REASONING
    )
    result = render_cli_prompt(
        req,
        integration_hints="Use custom tools only.",
        framework_tool_map="",
    )
    assert "Use custom tools only." in result
    assert "Key integrations" not in result


def test_integration_hints_constant_matches_expected_content() -> None:
    assert "Email:" in INTEGRATION_HINTS
    assert "Calendar" in INTEGRATION_HINTS
    assert "Search Console" in INTEGRATION_HINTS
    assert "Analytics" in INTEGRATION_HINTS
    assert "Memory search" in INTEGRATION_HINTS
