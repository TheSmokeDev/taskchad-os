"""X write tools (epic #465 1a) — validation, registration shape, toolset wiring.

The security-critical flow (dispatch -> proposal -> decide -> executor) lives
in ``test_persona_action_proposals.py``. This file owns the tool module's own
contract:

* argument validation — invalid input is an error string and NO proposal row
* registration metadata — write effect, dedicated_gate, persona_scoped, and
  the executor binding that makes an approval able to run
* the executor's argument passthrough — stored payload in, driver call out
* the real ``x_social_write`` toolset entry (deliverable D)

Dispatch is driven through the real ``build_persona_tool_payload`` path with a
minimal toolset registry; the deliverable-D test reads the REAL registry.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
CHAT_DIR = SCRIPTS_DIR.parent / "chat"
if str(CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_DIR))

import config  # noqa: E402
from personas import action_proposals  # noqa: E402
from runtime import persona_tools, tool_impl_x_write, tool_registry  # noqa: E402

PERSONA = "sales"

_MINIMAL_TOOLSETS = {
    "safe_core": {"description": "d", "tools": [], "includes": []},
    "browser_read": {"description": "d", "tools": [], "includes": ["safe_core"]},
    "x_social_write": {
        "description": "d",
        "tools": ["x_follow_accounts", "x_enable_notifications"],
        "includes": ["browser_read"],
    },
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    saved_registry = dict(tool_registry._REGISTRY)
    tool_registry._REGISTRY.clear()
    saved_executors = dict(action_proposals._EXECUTORS)
    action_proposals._EXECUTORS.clear()

    homie = tmp_path / ".homie"
    profile_dir = homie / "profiles" / PERSONA
    (profile_dir / "data").mkdir(parents=True)
    (profile_dir / "memory").mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "ambient-data", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_TOOLS", raising=False)
    monkeypatch.setattr("runtime.toolsets.TOOLSETS", _MINIMAL_TOOLSETS, raising=False)
    yield tmp_path
    action_proposals._EXECUTORS.clear()
    action_proposals._EXECUTORS.update(saved_executors)
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(saved_registry)


@pytest.fixture
def profile_dir(tmp_path: Path) -> Path:
    return tmp_path / ".homie" / "profiles" / PERSONA


def _dispatch(arguments: dict, *, tool: str = "x_follow_accounts") -> str:
    assert tool_impl_x_write.register_tools() == 2
    payload = persona_tools.build_persona_tool_payload(
        PERSONA, {"toolsets": ["x_social_write"]}
    )
    assert payload is not None
    return payload[1](tool, arguments)


def _proposal_count(profile_dir: Path) -> int:
    return len(action_proposals.list_pending(PERSONA))


# ── Validation — bad input is an error string and leaves no row ────────────


def test_empty_handles_are_refused_before_any_proposal(profile_dir: Path):
    result = _dispatch({"handles": []})
    assert result.startswith("error: handles must name at least one account")
    assert _proposal_count(profile_dir) == 0


def test_non_list_handles_are_refused(profile_dir: Path):
    result = _dispatch({"handles": "alice"})
    assert result.startswith("error: handles must be a JSON list")
    assert _proposal_count(profile_dir) == 0


@pytest.mark.parametrize(
    "bad",
    ["bad handle!", "a" * 16, "../etc", "ali;ce", "", "@", "white space"],
)
def test_handle_shape_is_enforced(profile_dir: Path, bad: str):
    result = _dispatch({"handles": ["alice", bad]})
    assert "not an X handle" in result
    assert _proposal_count(profile_dir) == 0


def test_handle_count_is_bounded(profile_dir: Path):
    result = _dispatch({"handles": [f"h{i}" for i in range(26)]})
    assert "at most 25" in result
    assert _proposal_count(profile_dir) == 0


def test_at_sign_is_stripped_and_duplicates_collapse(profile_dir: Path):
    card = _dispatch({"handles": ["@Alice", "alice", "bob"]})
    assert "/act approve" in card
    proposal = action_proposals.list_pending(PERSONA)[0]
    # Dedup is order-preserved and case-insensitive on X only at the site —
    # here "Alice" and "alice" are distinct strings and both kept.
    assert proposal.arguments["handles"] == ["Alice", "alice", "bob"]


def test_notify_tool_validates_the_same_way(profile_dir: Path):
    result = _dispatch({"handles": ["nope!"]}, tool="x_enable_notifications")
    assert "not an X handle" in result
    assert _proposal_count(profile_dir) == 0


def test_missing_persona_identity_is_an_error_not_a_proposal():
    # Handler-direct UNIT check of the persona_scoped guard only — the
    # dispatch path injects _persona_id and is covered in the sibling file.
    result = tool_impl_x_write._x_follow_accounts(handles=["alice"])
    assert result.startswith("error: persona identity missing")


# ── Card content — the operator reads exactly what will run ────────────────


def test_follow_card_summarizes_handles_and_the_notification_flag():
    card = _dispatch({"handles": ["alice", "bob"], "enable_notifications": True})
    assert "@alice" in card and "@bob" in card
    assert "notifications" in card
    assert f"/act approve {PERSONA}" in card and f"/act deny {PERSONA}" in card


def test_notify_card_names_the_tool_and_handles():
    card = _dispatch({"handles": ["carol"]}, tool="x_enable_notifications")
    assert "x_enable_notifications" in card
    assert "@carol" in card


# ── Registration shape — the metadata IS the security boundary ──────────────


def test_tools_register_as_dedicated_gate_persona_scoped_writes():
    assert tool_impl_x_write.register_tools() == 2
    for name in ("x_follow_accounts", "x_enable_notifications"):
        entry = tool_registry.get_entry(name)
        assert entry is not None
        assert entry.toolset == "x_social_write"
        assert entry.effect == "write"
        assert entry.dedicated_gate is True
        assert entry.elevatable is False
        assert entry.persona_scoped is True
        assert entry.handler is not None
        # The tool and its executor are one unit.
        assert action_proposals.get_action_executor(name) is not None


def test_executor_passes_the_stored_payload_and_token_to_the_driver(
    monkeypatch, profile_dir
):
    """End to end through the gate: the driver sees the stored payload plus
    the gate-minted approval bundle — never caller-supplied arguments.

    (Codex R1: the pre-R1 test called the executor directly, which proved a
    passthrough the security boundary no longer allows — executors require
    the minted token, so the only honest test drives the approval.)
    """
    import x_action_driver

    seen: list[dict] = []

    def fake_follow(handles, *, enable_notifications=False, port=None, session=None,
                    approval=None):
        seen.append(
            {
                "handles": list(handles),
                "enable_notifications": enable_notifications,
                "approval": approval,
            }
        )
        return {"ok": True, "results": [
            {"handle": h, "status": "followed", "detail": "", "screenshot": None}
            for h in handles
        ]}

    monkeypatch.setattr(x_action_driver, "follow_accounts", fake_follow)

    card = _dispatch({"handles": ["alice"], "enable_notifications": True})
    match = re.search(r"\*\*Action `([A-Z0-9]{6})`\*\*", card)
    assert match
    decision = action_proposals.decide_action(
        PERSONA, match.group(1), True,
        user_role="admin", source="interactive", actor="owner",
    )

    assert decision.outcome == action_proposals.DECISION_EXECUTED
    assert len(seen) == 1
    assert seen[0]["handles"] == ["alice"]
    assert seen[0]["enable_notifications"] is True
    approval = seen[0]["approval"]
    assert approval["persona_id"] == PERSONA
    assert approval["token"], "the executor must hand the driver the minted token"
    assert approval["payload"] == {"enable_notifications": True, "handles": ["alice"]}


# ── Deliverable E — the REAL wiring, end to end ─────────────────────────────
#
# Everything above isolates with a synthetic toolset registry, which is the
# right unit discipline — and exactly how a broken real wiring hides (#429 R6:
# declared, refused, invisible; the tests passed because they never loaded the
# shipped registry). This test is the anti-vacuity anchor: REAL
# ``runtime.toolsets.TOOLSETS``, REAL ``tool_impl.register_tools()`` bootstrap
# (via ``ensure_tools_registered`` inside the payload builder), REAL dispatch.
# Revert EITHER the toolsets.py entry OR the tool_impl.py import block and
# this fails — the payload never carries the write tools and dispatch answers
# out-of-scope instead of proposing.


def test_real_wiring_full_bootstrap_proposes(profile_dir: Path):
    import importlib

    import runtime.toolsets as real_toolsets

    # The autouse fixture patched the module attr to the minimal registry;
    # reload rebinds the SHIPPED one.
    importlib.reload(real_toolsets)

    payload = persona_tools.build_persona_tool_payload(
        PERSONA, {"toolsets": ["x_social_write"]}
    )
    assert payload is not None, "real x_social_write scope assembled to nothing"
    defs, dispatch = payload
    names = {(d.get("function") or {}).get("name") for d in defs}
    assert {"x_follow_accounts", "x_enable_notifications"} <= names

    card = dispatch("x_follow_accounts", {"handles": ["alice"]})
    assert "**Action `" in card, f"expected a proposal card, got: {card[:200]!r}"
    assert "not in this persona's granted scope" not in card
    assert _proposal_count(profile_dir) == 1


# ── Deliverable D — the real toolset entry ──────────────────────────────────


def test_real_registry_carries_x_social_write():
    # Reads the REAL runtime.toolsets entry: the autouse fixture monkeypatches
    # the module attribute, so reload the module to see the shipped registry.
    import importlib

    import runtime.toolsets as real_toolsets

    original = importlib.reload(real_toolsets).TOOLSETS.get("x_social_write")
    assert original is not None, "x_social_write missing from the live registry"
    assert set(original["tools"]) == {"x_follow_accounts", "x_enable_notifications"}
    assert original["includes"] == ["browser_read"]
    assert "gate" in original["description"]


# ── Codex R3 BLOCKER — the approval surface shows EVERY stored target ────────
#
# The card the operator approves is the authorization: a handle hidden behind
# "(+N more)" would still execute on approval. Summaries are bounded by the
# 25-handle validation cap, so full rendering is cheap and mandatory.


_TWENTY_FIVE = [f"acct{i}" for i in range(1, 26)]


def test_card_renders_all_25_handles(profile_dir: Path):
    card = _dispatch({"handles": _TWENTY_FIVE})
    for handle in _TWENTY_FIVE:
        assert f"@{handle}" in card, f"card hides approved target @{handle}"
    assert "+15 more" not in card and "(+" not in card
    assert _proposal_count(profile_dir) == 1


def test_act_list_renders_full_summary(profile_dir: Path):
    import asyncio

    import core_handlers

    _dispatch({"handles": _TWENTY_FIVE})
    reply = asyncio.run(
        core_handlers.handle_act(None, None, f"list {PERSONA}")
    )
    for handle in _TWENTY_FIVE:
        assert f"@{handle}" in reply, f"/act list hides approved target @{handle}"
