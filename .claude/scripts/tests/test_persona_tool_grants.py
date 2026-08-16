"""Single-capability (per-tool) persona grants (epic #465 ticket 1c).

The toolset grant rail moved BUNDLES; this maps the same rail moving ONE
registered tool, end to end:

* the executor's happy paths (grant, revoke) and honest misses
  (already_granted / not_granted), with byte-level config assertions
* grant-time validation: unknown tool names refused on BOTH doors (direct
  grant and counter-offer proposal), with nothing written
* reach-only doctrine: a ``dedicated_gate`` tool CAN be granted, and the REAL
  dispatch path then only PROPOSES — the execution gate is intact
* the full marker chain: ``<<GRANT_REQUEST: tool:…>>`` -> proposal kind=tool
  -> admin approve -> physical ``tools:`` -> scope -> payload definition
* the reconcile hazard: provisioning's ledger merge must carry tool grants or
  the next template rewrite erases them
* ledger replay round-trips kind — a tool and a toolset sharing a name never
  alias
* the chat command parser's ``tool:<name>`` syntax

Fixture shapes are cloned from test_persona_toolset_grants.py /
test_persona_grant_proposals.py: a physical tmp profile, an OPERATOR turn,
and physical-state assertions throughout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
CHAT_DIR = SCRIPTS_DIR.parent / "chat"
if str(CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_DIR))

import persona_grant_commands  # noqa: E402

from personas import grant_proposals as proposals  # noqa: E402
from personas import provisioning  # noqa: E402
from personas import toolset_grants as grants  # noqa: E402
from personas.services import (  # noqa: E402
    add_persona_tool,
    read_profile_config,
    remove_persona_tool,
    resolve_persona_tool_scope,
)

# Real registered tools (runtime registry) — grants must name one.
KNOWN_TOOL = "memory_search"
OTHER_TOOL = "search_files"
# A dedicated-gate WRITE tool (1a): grantable for reach, gated for action.
GATED_TOOL = "x_follow_accounts"

OPERATOR = {
    "actor": "owner",
    "actor_role": "admin",
    "trigger_text": "give sales memory search",
    "surface": "telegram",
    "channel_id": "42",
}

TURN = {
    "requested_by": "owner",
    "trigger_text": "can you search your own memory?",
    "surface": "telegram",
    "channel_id": "42",
}


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A physical named-profile tree at ``<tmp>/.homie/profiles/sales``."""
    homie = tmp_path / ".homie"
    profile_dir = homie / "profiles" / "sales"
    (profile_dir / "state").mkdir(parents=True)
    (profile_dir / "data").mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    for var in (
        "HOMIE_KILLSWITCH_PERSONA_MUTATION",
        "HOMIE_KILLSWITCH_PERSONA_GRANT_PROPOSALS",
        "HOMIE_KILLSWITCH_PERSONA_TOOLS",
        "HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS",
    ):
        monkeypatch.delenv(var, raising=False)
    (profile_dir / "config.yaml").write_text(
        "persona:\n  id: sales\n  display_name: Sales\ntoolsets:\n  - safe_core\n",
        encoding="utf-8",
    )
    return profile_dir


def config_bytes(profile_dir: Path) -> bytes:
    return (profile_dir / "config.yaml").read_bytes()


def ledger_rows(profile_dir: Path) -> list[dict]:
    path = profile_dir / "data" / grants.LEDGER_FILENAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── Executor: grant / revoke a single tool ─────────────────────────────────


def test_grant_tool_writes_the_tools_list_and_a_kind_stamped_ledger_pair(
    profile: Path,
):
    before = config_bytes(profile)
    assert b"\ntools:" not in before  # `toolsets:` contains the substring `tools`

    result = add_persona_tool("sales", KNOWN_TOOL, **OPERATOR)

    assert result.changed is True
    assert result.outcome == grants.OUTCOME_GRANTED
    assert read_profile_config("sales")["tools"] == [KNOWN_TOOL]
    # The pre-existing toolsets declaration is untouched.
    assert read_profile_config("sales")["toolsets"] == ["safe_core"]

    pair = [
        row
        for row in ledger_rows(profile)
        if row["outcome"] in {grants.OUTCOME_INTENT, grants.OUTCOME_GRANTED}
    ]
    assert [row["outcome"] for row in pair] == [grants.OUTCOME_INTENT, grants.OUTCOME_GRANTED]
    assert pair[0]["correlation_id"] == pair[1]["correlation_id"]
    for row in pair:
        assert row["operation"] == grants.OPERATION_GRANT_TOOL
        assert row["kind"] == grants.KIND_TOOL
        assert row["toolset"] == KNOWN_TOOL


def test_revoke_tool_removes_it_byte_for_byte(profile: Path):
    add_persona_tool("sales", KNOWN_TOOL, **OPERATOR)
    add_persona_tool("sales", OTHER_TOOL, **OPERATOR)
    assert read_profile_config("sales")["tools"] == [KNOWN_TOOL, OTHER_TOOL]

    result = remove_persona_tool("sales", KNOWN_TOOL, **OPERATOR)

    assert result.changed is True
    assert result.outcome == grants.OUTCOME_REVOKED
    assert read_profile_config("sales")["tools"] == [OTHER_TOOL]
    assert b"memory_search" not in config_bytes(profile)


def test_already_granted_and_not_granted_are_honest_misses(profile: Path):
    add_persona_tool("sales", KNOWN_TOOL, **OPERATOR)
    again = add_persona_tool("sales", KNOWN_TOOL, **OPERATOR)
    assert again.changed is False
    assert again.outcome == grants.OUTCOME_ALREADY_GRANTED

    missing = remove_persona_tool("sales", OTHER_TOOL, **OPERATOR)
    assert missing.changed is False
    assert missing.outcome == grants.OUTCOME_NOT_GRANTED
    assert missing.suggestions == (KNOWN_TOOL,)


def test_unknown_tool_is_refused_and_writes_nothing(profile: Path):
    before = config_bytes(profile)
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_tool("sales", "no_such_tool_anywhere", **OPERATOR)
    assert excinfo.value.reason == grants.REASON_UNKNOWN_TOOL
    assert config_bytes(profile) == before


def test_tool_grant_reuses_every_gate(profile: Path):
    """Spot-check the shared gates fire for the tool grain."""
    with pytest.raises(grants.ToolsetGrantRefusedError) as excinfo:
        add_persona_tool("sales", KNOWN_TOOL, **{**OPERATOR, "actor_role": "viewer"})
    assert excinfo.value.reason == grants.REASON_NOT_AUTHORIZED
    assert "tools" not in read_profile_config("sales")


# ── Reach-only: a dedicated-gate tool granted, then dispatch only PROPOSES ──


def test_dedicated_gate_tool_grants_reach_but_dispatch_only_proposes(
    profile: Path, monkeypatch: pytest.MonkeyPatch
):
    import x_action_driver

    from runtime import persona_tools

    calls: list = []
    monkeypatch.setattr(
        x_action_driver,
        "follow_accounts",
        lambda *a, **kw: calls.append((a, kw)) or {"ok": True, "results": []},
    )

    result = add_persona_tool("sales", GATED_TOOL, **OPERATOR)
    assert result.changed is True, "a dedicated_gate tool is grantable — reach only"

    config = read_profile_config("sales")
    scope = resolve_persona_tool_scope(config)
    assert GATED_TOOL in scope.tools

    # REAL assembly + dispatch over the physical config: the model is OFFERED
    # the tool, but calling it produces an approval card, not a browser move.
    payload = persona_tools.build_persona_tool_payload("sales", config)
    assert payload is not None
    defs, dispatch = payload
    assert GATED_TOOL in {d["function"]["name"] for d in defs}
    card = dispatch(GATED_TOOL, {"handles": ["alice"]})
    assert "/act approve" in card
    assert calls == [], "the write driver must not run without /act approval"


# ── The marker chain, end to end ────────────────────────────────────────────


def test_tool_marker_round_trips_to_a_live_capability(profile: Path):
    from runtime import persona_tools

    offer = proposals.tee_up_from_reply(
        "sales",
        "I cannot search my own memory from here.\n\n<<GRANT_REQUEST: tool:memory_search>>",
        **TURN,
    )
    assert offer is not None and offer.proposal is not None
    assert offer.proposal.kind == grants.KIND_TOOL
    assert offer.proposal.toolset == KNOWN_TOOL
    assert "GRANT_REQUEST" not in offer.reply_text
    assert "capability" in offer.card_text

    decision = proposals.decide_proposal(
        "sales",
        offer.proposal.short_code,
        approve=True,
        actor="owner",
        actor_role="admin",
        surface="telegram",
        channel_id="42",
    )
    assert decision.outcome == proposals.DECISION_GRANTED

    # Physical state, then the builder's view of it.
    config = read_profile_config("sales")
    assert config["tools"] == [KNOWN_TOOL]
    assert resolve_persona_tool_scope(config).tools == (KNOWN_TOOL,)
    payload = persona_tools.build_persona_tool_payload("sales", config)
    assert payload is not None
    assert KNOWN_TOOL in {d["function"]["name"] for d in payload[0]}

    # Ledger carries the tool grain end to end.
    tool_rows = [row for row in ledger_rows(profile) if row.get("kind") == "tool"]
    assert any(row["operation"] == grants.OPERATION_GRANT_TOOL for row in tool_rows)


def test_unknown_tool_marker_is_an_honest_miss_with_no_proposal(profile: Path):
    offer = proposals.tee_up_from_reply(
        "sales",
        "Blocked.\n\n<<GRANT_REQUEST: tool:no_such_tool_anywhere>>",
        **TURN,
    )
    assert offer is not None
    assert offer.proposal is None
    assert "not in the live registry" in offer.card_text
    assert b"\ntools:" not in config_bytes(profile)


# ── Reconcile preserves tool grants (the erase-on-reconcile hazard) ──────────


def test_reconcile_preserves_a_ledger_tool_grant(profile: Path):
    add_persona_tool("sales", KNOWN_TOOL, **OPERATOR)
    # The blueprint rewrite starts from a config whose tools list does NOT
    # carry the grant — the erase shape.
    merged: dict = {"toolsets": ["safe_core"], "tools": []}

    preserved, removed, preserved_tools, removed_tools = (
        provisioning._preserve_ledger_grants(merged, "sales")
    )

    assert merged["tools"] == [KNOWN_TOOL], "reconcile must carry the tool grant"
    assert preserved_tools == (KNOWN_TOOL,)
    assert removed_tools == ()


def test_reconcile_respects_a_tool_grant_tombstone(profile: Path):
    add_persona_tool("sales", KNOWN_TOOL, **OPERATOR)
    remove_persona_tool("sales", KNOWN_TOOL, **OPERATOR)
    merged: dict = {"toolsets": ["safe_core"], "tools": [KNOWN_TOOL]}

    provisioning._preserve_ledger_grants(merged, "sales")

    assert merged["tools"] == [], "a revoked tool grant must stay off"


# ── Replay: kind round-trips and same-named grains never alias ───────────────


def test_tool_and_toolset_sharing_a_name_never_alias(profile: Path, monkeypatch):
    from runtime import tool_registry

    # One name, both grains: a toolset AND a tool called "shared_capability".
    monkeypatch.setattr(
        "runtime.toolsets.TOOLSETS",
        {"shared_capability": {"description": "d", "tools": [], "includes": []}},
        raising=False,
    )
    tool_registry.register_tool(
        "shared_capability", "d", toolset="safe_core", handler=lambda **kw: "x"
    )
    try:
        from personas.services import add_persona_toolset

        add_persona_toolset("sales", "shared_capability", **OPERATOR)
        add_persona_tool("sales", "shared_capability", **OPERATOR)

        scope = grants.ledger_scope("sales")
        assert "shared_capability" in scope.active
        assert "shared_capability" in scope.active_tools

        remove_persona_tool("sales", "shared_capability", **OPERATOR)
        scope = grants.ledger_scope("sales")
        assert "shared_capability" in scope.active, "the TOOLSET grant survives the tool revoke"
        assert "shared_capability" not in scope.active_tools
        assert "shared_capability" in scope.tombstoned_tools
    finally:
        tool_registry.unregister_tool("shared_capability")


# ── The typed command surface ────────────────────────────────────────────────


def test_parser_accepts_the_tool_prefix():
    parsed = persona_grant_commands.parse_persona_command("grant sales tool:memory_search")
    assert not parsed.error
    assert parsed.kind == "tool"
    assert parsed.toolset == "memory_search"
    assert parsed.persona_id == "sales"

    short = persona_grant_commands.parse_persona_command("revoke tool:memory_search")
    assert short.kind == "tool" and short.persona_id == ""

    bad = persona_grant_commands.parse_persona_command("grant sales tool:not-a-tool")
    assert bad.error and "not a tool name" in bad.error


def test_typed_command_grants_a_tool_end_to_end(profile: Path):
    incoming = SimpleNamespace(
        platform="cli",
        source="interactive",
        user_role="admin",
        text="/persona grant sales tool:memory_search",
        user=SimpleNamespace(platform_id="owner"),
        channel=SimpleNamespace(platform_id="42"),
    )
    reply = persona_grant_commands.execute_persona_command(
        incoming, "grant sales tool:memory_search"
    )
    assert "added to sales" in reply
    assert read_profile_config("sales")["tools"] == [KNOWN_TOOL]

    receipt = persona_grant_commands.transcript_receipt("grant sales tool:memory_search")
    assert "tool=memory_search" in receipt
    assert "toolset=" not in receipt
