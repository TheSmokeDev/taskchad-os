"""The scheduled desks get a mind, not just better plumbing.

Both alpha desks shipped their one LLM call as `max_turns=1, allowed_tools=[]`.
Epic #199 built judgment AROUND that call and never touched it. These tests pin
the seam that changes it, and -- more importantly -- pin that the DISARMED path
is byte-identical to what shipped, because a regression there breaks a desk that
posts to the operator every two hours.

Path map (one test per distinct path, no test passes without exercising it):

  agentic_enabled      kill switch off | switch module absent (fail-open)
  agentic_max_turns    default | valid override | garbage | zero-or-negative
  agentic_model_tier   default quality | override
  resolve_desk_tools   disabled | config raises | payload None | success
  tool_preamble        carries the final-answer-is-JSON contract
  run_digest (x2)      ARMED: turns>1, quality tier, preamble prepended
                       DISARMED: turns==1, fast tier, prompt untouched
                       explicit tier= beats the agentic tier
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import agentic_turn  # noqa: E402


# ---------------------------------------------------------------------------
# agentic_enabled -- the switch only ever REVOKES
# ---------------------------------------------------------------------------


def test_kill_switch_disables(monkeypatch):
    import security.kill_switches as ks

    monkeypatch.setattr(ks, "is_disabled", lambda name: name == agentic_turn.KILL_SWITCH_NAME)
    assert agentic_turn.agentic_enabled() is False


def test_missing_kill_switch_module_does_not_disable(monkeypatch):
    """Absence of the switch is not a denial.

    A kill switch grants nothing -- it only takes away. If the switch store is
    unreadable the feature must stay ON, or an unrelated outage silently
    downgrades every desk run to the dumb path with no receipt.
    """
    import security.kill_switches as ks

    def _boom(name):
        raise RuntimeError("switch store unreadable")

    monkeypatch.setattr(ks, "is_disabled", _boom)
    assert agentic_turn.agentic_enabled() is True


# ---------------------------------------------------------------------------
# turn budget + tier -- all Rule 1 (resolved at call time, never a default arg)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 6),       # default
        ("20", 20),      # honored
        ("banana", 6),   # unparseable -> default, not a crash
        ("0", 6),        # zero would attach tools to a loop that cannot iterate
        ("-3", 6),       # same trap, negative
    ],
)
def test_max_turns_resolution(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("AGENTIC_SCAN_MAX_TURNS", raising=False)
    else:
        monkeypatch.setenv("AGENTIC_SCAN_MAX_TURNS", raw)
    assert agentic_turn.agentic_max_turns() == expected


def test_max_turns_is_read_per_call_not_frozen(monkeypatch):
    """Rule 1 proof: mutating the env after first use must take effect."""
    monkeypatch.setenv("AGENTIC_SCAN_MAX_TURNS", "5")
    assert agentic_turn.agentic_max_turns() == 5
    monkeypatch.setenv("AGENTIC_SCAN_MAX_TURNS", "9")
    assert agentic_turn.agentic_max_turns() == 9


def test_model_tier_default_and_override(monkeypatch):
    monkeypatch.delenv("AGENTIC_SCAN_TIER", raising=False)
    assert agentic_turn.agentic_model_tier() == "quality"
    monkeypatch.setenv("AGENTIC_SCAN_TIER", "fast")
    assert agentic_turn.agentic_model_tier() == "fast"


# ---------------------------------------------------------------------------
# resolve_desk_tools -- every failure degrades to the shipped one-shot path
# ---------------------------------------------------------------------------


def test_resolve_returns_nothing_when_disabled(monkeypatch):
    monkeypatch.setattr(agentic_turn, "agentic_enabled", lambda: False)
    assert agentic_turn.resolve_desk_tools() == (None, None)


def test_resolve_survives_config_load_failure(monkeypatch):
    """A broken persona profile must not kill the 2-hourly run."""
    monkeypatch.setattr(agentic_turn, "agentic_enabled", lambda: True)
    import personas

    def _boom(persona_id, **_kwargs):
        raise OSError("profile unreadable")

    monkeypatch.setattr(personas, "load_persona_config", _boom)
    assert agentic_turn.resolve_desk_tools() == (None, None)


def test_resolve_handles_default_deny_no_scope(monkeypatch):
    """`None` from the assembler is default-deny answering 'no scope', not an error."""
    monkeypatch.setattr(agentic_turn, "agentic_enabled", lambda: True)
    import personas
    from runtime import persona_tools

    monkeypatch.setattr(personas, "load_persona_config", lambda p, **k: {})
    monkeypatch.setattr(persona_tools, "build_persona_tool_payload", lambda p, c, **k: None)
    assert agentic_turn.resolve_desk_tools() == (None, None)


def test_resolve_success_passes_through_assembler(monkeypatch):
    """Tools must come from build_persona_tool_payload, never be built here.

    That assembler owns the kill switch, the per-call scope re-check, and the
    audit row. A desk that assembled its own tool_defs would be an unaudited
    execution path.
    """
    monkeypatch.setattr(agentic_turn, "agentic_enabled", lambda: True)
    import personas
    from runtime import persona_tools

    sentinel_defs = [{"function": {"name": "crypto_candles"}}]
    sentinel_dispatch = object()
    seen = {}

    def _fake(persona_id, cfg, **kwargs):
        seen["persona_id"] = persona_id
        seen["allowed_tool_names"] = kwargs.get("allowed_tool_names")
        return sentinel_defs, sentinel_dispatch

    monkeypatch.setattr(
        personas,
        "load_persona_config",
        lambda p, **k: {"toolsets": ["crypto"]},
    )
    monkeypatch.setattr(persona_tools, "build_persona_tool_payload", _fake)

    defs, dispatch = agentic_turn.resolve_desk_tools()
    assert defs is sentinel_defs
    assert dispatch is sentinel_dispatch
    assert seen["persona_id"] == "crypto"
    assert seen["allowed_tool_names"] == agentic_turn.SCHEDULED_TOOL_ALLOWLIST


def test_scheduled_allowlist_cannot_expose_interactive_authority():
    assert not (
        agentic_turn.SCHEDULED_TOOL_ALLOWLIST
        & agentic_turn.SCHEDULED_TOOL_DENYLIST
    )
    assert "crypto_submit_bracket" not in agentic_turn.SCHEDULED_TOOL_ALLOWLIST
    assert "x_search" not in agentic_turn.SCHEDULED_TOOL_ALLOWLIST


def test_scheduled_allowlist_includes_read_only_mint_discovery():
    assert "crypto_mintscan" in agentic_turn.SCHEDULED_TOOL_ALLOWLIST
    assert "crypto_mintscan" not in agentic_turn.SCHEDULED_TOOL_DENYLIST


def test_preamble_carries_the_json_contract():
    """The desks parse strict JSON; a prose final turn yields zero plays."""
    text = agentic_turn.tool_preamble()
    assert "FINAL message must be the JSON object" in text
    assert "crypto_safety_check" in text  # check before you judge


# ---------------------------------------------------------------------------
# run_digest -- the actual behavior change, on BOTH desks
# ---------------------------------------------------------------------------


class _Result:
    text = '{"plays": []}'


def _capture_request(monkeypatch):
    """Intercept the RuntimeRequest both desks build."""
    captured = {}

    async def _fake_lanes(request):
        captured["request"] = request
        return _Result()

    from runtime import lane_router

    monkeypatch.setattr(lane_router, "run_with_runtime_lanes", _fake_lanes)

    import security.kill_switches as ks

    monkeypatch.setattr(ks, "requireEnabled", lambda *a, **k: None)
    return captured


@pytest.mark.parametrize(
    "module_path", ["discord_alpha.digest", "x_networking.digest"]
)
def test_disarmed_is_identical_to_what_shipped(monkeypatch, module_path):
    """The regression guard. No tools => exactly the one-shot call that shipped."""
    import importlib

    mod = importlib.import_module(module_path)
    captured = _capture_request(monkeypatch)
    monkeypatch.setattr(agentic_turn, "resolve_desk_tools", lambda *a, **k: (None, None))

    asyncio.run(mod.run_digest("ORIGINAL PROMPT"))
    req = captured["request"]

    assert req.max_turns == 1
    assert req.allowed_tools == []
    assert req.tool_defs is None
    assert req.tool_dispatch is None
    assert req.prompt == "ORIGINAL PROMPT"  # preamble NOT prepended


@pytest.mark.parametrize(
    "module_path", ["discord_alpha.digest", "x_networking.digest"]
)
def test_armed_attaches_tools_turns_and_preamble(monkeypatch, module_path):
    import importlib

    mod = importlib.import_module(module_path)
    captured = _capture_request(monkeypatch)
    defs = [{"function": {"name": "crypto_candles"}}]
    dispatch = object()
    monkeypatch.setattr(agentic_turn, "resolve_desk_tools", lambda *a, **k: (defs, dispatch))
    # 7, NOT the default 12 -- asserting the default would pass even if the
    # resolver were never consulted. The value has to be distinctive or the
    # test proves nothing about the wiring.
    monkeypatch.setenv("AGENTIC_SCAN_MAX_TURNS", "7")

    asyncio.run(mod.run_digest("ORIGINAL PROMPT"))
    req = captured["request"]

    assert req.tool_defs is defs
    assert req.tool_dispatch is dispatch
    assert req.max_turns == 7, "tools with max_turns=1 call a tool and never see the result"
    # SDK-native list stays empty -- scoped set, not the built-in surface.
    assert req.allowed_tools == []
    assert req.prompt.endswith("ORIGINAL PROMPT")
    assert "FINAL message must be the JSON object" in req.prompt


@pytest.mark.parametrize(
    "module_path", ["discord_alpha.digest", "x_networking.digest"]
)
def test_explicit_tier_beats_the_agentic_tier(monkeypatch, module_path):
    """A caller that names a tier means it, armed or not."""
    import importlib

    mod = importlib.import_module(module_path)
    captured = _capture_request(monkeypatch)
    monkeypatch.setattr(
        agentic_turn, "resolve_desk_tools", lambda *a, **k: ([{"function": {"name": "x"}}], object())
    )

    captured_models = {}

    def _fake_models():
        captured_models["called"] = True
        return {"fast": "haiku-model", "quality": "sonnet-model"}

    import config

    monkeypatch.setattr(config, "get_background_models", _fake_models)

    asyncio.run(mod.run_digest("P", tier="fast"))
    assert captured["request"].model == "haiku-model"
