"""Crypto Homie acceptance invariants for the Codex caller-tool bridge (#283)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_DIR.parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from runtime import persona_tools, tool_registry  # noqa: E402


@pytest.fixture
def crypto_payload():
    saved = dict(tool_registry._REGISTRY)
    tool_registry._REGISTRY.clear()
    try:
        payload = persona_tools.build_persona_tool_payload(
            "crypto", {"toolsets": ["crypto"]}
        )
        assert payload is not None
        yield payload
    finally:
        tool_registry._REGISTRY.clear()
        tool_registry._REGISTRY.update(saved)


def test_real_crypto_scope_matches_optional_private_extension(crypto_payload):
    definitions, _dispatch = crypto_payload
    names = {definition["function"]["name"] for definition in definitions}
    private_tools = {
        "crypto_last30days_read",
        "crypto_prediction_markets",
        "crypto_prediction_book",
        "crypto_source_tape",
        "crypto_chart",
    }
    has_private_extension = importlib.util.find_spec("runtime.tool_impl_crypto_round") is not None
    assert len(names) == (40 if has_private_extension else 35)
    if has_private_extension:
        assert private_tools <= names
    else:
        assert private_tools.isdisjoint(names)
    assert {
        "crypto_desk_snapshot",
        "crypto_candles",
        "crypto_indicators",
        "crypto_plays_read",
        "crypto_preflight",
        "crypto_submit_bracket",
        "request_tool",
        "skill_view",
        "skills_list",
    } <= names


def test_foreign_persona_tool_is_absent_and_injected_call_is_refused(crypto_payload):
    definitions, dispatch = crypto_payload
    names = {definition["function"]["name"] for definition in definitions}
    tool_registry.register_tool(
        "sales_private_queue",
        "Foreign persona tool.",
        toolset="sales",
        handler=lambda **kwargs: pytest.fail("foreign handler executed"),
    )

    assert "sales_private_queue" not in names
    refusal = dispatch("sales_private_queue", {})
    assert "not in this persona's granted scope" in refusal


def test_scope_version_is_stable_ordered_and_persona_bound(crypto_payload):
    definitions, _dispatch = crypto_payload
    version = persona_tools.persona_tool_scope_version("crypto", definitions)
    same = persona_tools.persona_tool_scope_version("crypto", definitions)
    reordered = persona_tools.persona_tool_scope_version(
        "crypto", list(reversed(definitions))
    )
    foreign_persona = persona_tools.persona_tool_scope_version("sales", definitions)

    assert version and version.startswith("sha256:")
    assert version == same
    assert version != reordered
    assert version != foreign_persona


@pytest.mark.parametrize(
    "relative_path",
    [
        ".claude/chat/engine.py",
        ".claude/scripts/cabinet/text_orchestrator.py",
        ".claude/chat/discord_persona_runtime.py",
    ],
)
def test_all_three_persona_surfaces_carry_the_same_scope_version_contract(relative_path):
    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert "persona_tool_scope_version(" in source
    assert "tool_scope_version=persona_scope_version" in source
    assert '"tool_scope_version": persona_scope_version' in source
