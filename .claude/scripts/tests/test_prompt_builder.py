"""Tests for runtime.prompt_builder — shared CLI prompt rendering."""

from __future__ import annotations

from runtime.base import RuntimeRequest
from runtime.capabilities import TEXT_REASONING, TOOL_REASONING
from runtime.prompt_builder import GEMINI_GUIDANCE, INTEGRATION_HINTS, render_cli_prompt


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


def test_text_preamble_has_output_discipline() -> None:
    req = RuntimeRequest(prompt="yo", cwd=".", task_name="t", capability=TEXT_REASONING)
    result = render_cli_prompt(req)
    assert "Match the length of your response" in result


def test_tool_preamble_has_output_discipline() -> None:
    req = RuntimeRequest(prompt="go", cwd=".", task_name="t", capability=TOOL_REASONING)
    result = render_cli_prompt(req, framework_tool_map="")
    assert "Match the length of your response" in result


def test_model_guidance_appended_on_text_path() -> None:
    req = RuntimeRequest(prompt="yo", cwd=".", task_name="t", capability=TEXT_REASONING)
    result = render_cli_prompt(req, model_guidance="MODELGUIDE_MARKER")
    assert "MODELGUIDE_MARKER" in result


def test_model_guidance_appended_on_tool_path() -> None:
    req = RuntimeRequest(prompt="go", cwd=".", task_name="t", capability=TOOL_REASONING)
    result = render_cli_prompt(
        req, framework_tool_map="", model_guidance="MODELGUIDE_MARKER"
    )
    assert "MODELGUIDE_MARKER" in result


def test_model_guidance_absent_by_default() -> None:
    req = RuntimeRequest(prompt="yo", cwd=".", task_name="t", capability=TEXT_REASONING)
    result = render_cli_prompt(req)
    assert "Gemini operational directives" not in result


def test_gemini_guidance_constant_leads_with_conciseness() -> None:
    assert "Gemini operational directives" in GEMINI_GUIDANCE
    assert "Conciseness" in GEMINI_GUIDANCE


def test_text_preamble_grounds_against_fabricated_action_claims() -> None:
    req = RuntimeRequest(prompt="yo", cwd=".", task_name="t", capability=TEXT_REASONING)
    result = render_cli_prompt(req)
    assert "never claim to have read files" in result


# ---------------------------------------------------------------------------
# conversational flag — in-character preamble for user-facing turns (cabinet
# personas + chat replies). Provider-agnostic: both CLI lanes share this builder.
# ---------------------------------------------------------------------------


def test_conversational_text_turn_swaps_backstage_preamble() -> None:
    req = RuntimeRequest(
        prompt="what's our priority?", cwd=".", task_name="t",
        capability=TEXT_REASONING, conversational=True,
    )
    result = render_cli_prompt(req)
    # The exact backstage script the homie embarrassingly recited is GONE.
    assert "safe text-only" not in result
    assert "no tools in this mode" not in result
    assert "reasoning task for The Homie runtime layer" not in result
    # ...replaced by an in-character frame.
    assert "speaking as yourself in a live conversation" in result
    assert "Stay fully in character" in result


def test_conversational_keeps_anti_fabrication_and_forbids_plumbing() -> None:
    req = RuntimeRequest(
        prompt="yo", cwd=".", task_name="t",
        capability=TEXT_REASONING, conversational=True,
    )
    result = render_cli_prompt(req)
    assert "taken an action you haven't" in result
    assert "backstage plumbing the user must never hear about" in result


def test_conversational_flag_ignored_on_tool_path() -> None:
    req = RuntimeRequest(
        prompt="go", cwd=".", task_name="t",
        capability=TOOL_REASONING, conversational=True,
    )
    result = render_cli_prompt(req, framework_tool_map="")
    # Tool turns keep the tool preamble; the conversational frame is not used.
    assert "full access to read and write files" in result
    assert "Stay fully in character" not in result


def test_conversational_preserves_persona_system_prompt() -> None:
    req = RuntimeRequest(
        prompt="status?", cwd=".", task_name="t",
        capability=TEXT_REASONING, conversational=True,
        system_prompt="You are the Sales homie.",
    )
    result = render_cli_prompt(req)
    assert "System context:\nYou are the Sales homie." in result


def test_text_turn_without_conversational_keeps_backstage_preamble() -> None:
    # Backstage reasoning jobs (reflection, dream, formatter) are unchanged.
    req = RuntimeRequest(
        prompt="reflect", cwd=".", task_name="t",
        capability=TEXT_REASONING, conversational=False,
    )
    result = render_cli_prompt(req)
    assert "safe text-only" in result
    assert "Stay fully in character" not in result


def test_conversational_skips_gemini_guidance() -> None:
    """Gemini's tool/runtime-discipline guidance must NOT leak into a conversational
    turn — it would re-introduce the "verify first: read files / execute" framing the
    in-character preamble is meant to suppress."""
    req = RuntimeRequest(
        prompt="yo", cwd=".", task_name="t",
        capability=TEXT_REASONING, conversational=True,
    )
    result = render_cli_prompt(req, model_guidance=GEMINI_GUIDANCE)
    assert "Gemini operational directives" not in result
    assert "verify first" not in result.lower()
    assert "Stay fully in character" in result


def test_non_conversational_text_still_gets_model_guidance() -> None:
    """Backstage text turns are unchanged — guidance still appended."""
    req = RuntimeRequest(
        prompt="yo", cwd=".", task_name="t",
        capability=TEXT_REASONING, conversational=False,
    )
    result = render_cli_prompt(req, model_guidance=GEMINI_GUIDANCE)
    assert "Gemini operational directives" in result


def test_user_facing_requests_set_conversational_flag() -> None:
    """The two user-facing RuntimeRequest sites carry conversational=True so a homie
    never narrates its sandbox. Source guard (non-comment lines) against accidental
    removal — the cabinet wiring also has a behavioral test in
    test_cabinet_text_orchestrator.py."""
    from pathlib import Path

    scripts = Path(__file__).resolve().parent.parent
    cabinet_src = (scripts / "cabinet" / "text_orchestrator.py").read_text(encoding="utf-8")
    engine_src = (scripts.parent / "chat" / "engine.py").read_text(encoding="utf-8")

    def _has_real_flag(src: str) -> bool:
        return any(
            "conversational=True" in line and not line.lstrip().startswith("#")
            for line in src.splitlines()
        )

    assert _has_real_flag(cabinet_src), "cabinet persona turn must set conversational=True"
    assert _has_real_flag(engine_src), "engine chat turn must set conversational=True"
