"""Persona action proposals — the dedicated operator gate for WRITE tools.

Epic #465 ticket 1a. One case per distinct path through the flow:

* a granted persona's tool call PROPOSES — card out, driver untouched, row in
* an admin approve EXECUTES the stored payload — executor, experience note,
  and ledger row all physically present
* every ``decide_action`` refusal branch — non-admin, non-interactive source,
  expired, double-tap, unknown code, no executor
* the kill switch PROPAGATES and nothing executes
* an ungranted persona never reaches propose (out-of-scope at dispatch)
* ``request_tool`` one-time elevation refuses a dedicated-gate tool
* personas granted only browser/crypto/social never SEE the write tools
* tamper: the executor runs the STORED payload, not anything the deciding
  caller could supply

The critical path is driven through the REAL dispatch —
``build_persona_tool_payload`` -> ``dispatch("x_follow_accounts", ...)`` —
never handler-direct calls. The browser layer is mocked at the module
attribute (Rule 3): ``x_action_driver.follow_accounts``.
"""

from __future__ import annotations

import json
import re
import sqlite3
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
from security import kill_switches  # noqa: E402

PERSONA = "sales"

_MINIMAL_TOOLSETS = {
    "safe_core": {"description": "d", "tools": [], "includes": []},
    # Non-empty scopes: a toolset resolving to ZERO tools makes
    # build_persona_tool_payload return None before dispatch exists.
    "browser_read": {"description": "d", "tools": ["page_read"], "includes": ["safe_core"]},
    "browser": {"description": "d", "tools": [], "includes": ["browser_read"]},
    "crypto": {"description": "d", "tools": ["chart_read"], "includes": []},
    "social": {"description": "d", "tools": [], "includes": ["browser"]},
    "x_social_write": {
        "description": "d",
        "tools": ["x_follow_accounts", "x_enable_notifications"],
        "includes": ["browser_read"],
    },
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Physical tmp profile tree + isolated registry/executors/env."""
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
    for var in (
        "HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS",
        "HOMIE_KILLSWITCH_PERSONA_TOOLS",
        "HOMIE_KILLSWITCH_PERSONA_ELEVATION",
        "HOMIE_ACTION_PROPOSAL_TTL_SECONDS",
        "HOMIE_VAULT_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("runtime.toolsets.TOOLSETS", _MINIMAL_TOOLSETS, raising=False)
    # The note WRITE is the assertion target; the reindex loads an embedding
    # model from HF Hub — a network fetch has no place in a hermetic test.
    monkeypatch.setattr(
        "personas.experience._reindex_note", lambda *a, **k: None, raising=False
    )
    yield tmp_path
    action_proposals._EXECUTORS.clear()
    action_proposals._EXECUTORS.update(saved_executors)
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(saved_registry)


@pytest.fixture
def profile_dir(tmp_path: Path) -> Path:
    return tmp_path / ".homie" / "profiles" / PERSONA


@pytest.fixture
def driver_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Recording fakes for the browser driver, patched at the module attr."""
    import x_action_driver

    calls: dict[str, list] = {"follow": [], "notify": []}

    def fake_follow(
        handles, *, enable_notifications=False, port=None, session=None, approval=None
    ):
        calls["follow"].append(
            {
                "handles": list(handles),
                "enable_notifications": enable_notifications,
                "port": port,
                "session": session,
                "has_approval": isinstance(approval, dict)
                and bool(approval.get("token")),
            }
        )
        return {
            "ok": True,
            "action": "x_follow_accounts",
            "results": [
                {"handle": h, "status": "followed", "detail": "", "screenshot": None}
                for h in handles
            ],
        }

    def fake_notify(handles, *, port=None, session=None, approval=None):
        calls["notify"].append(
            {
                "handles": list(handles),
                "port": port,
                "session": session,
                "has_approval": isinstance(approval, dict)
                and bool(approval.get("token")),
            }
        )
        return {
            "ok": True,
            "action": "x_enable_notifications",
            "results": [
                {"handle": h, "status": "notifications_on", "detail": "", "screenshot": None}
                for h in handles
            ],
        }

    monkeypatch.setattr(x_action_driver, "follow_accounts", fake_follow)
    monkeypatch.setattr(x_action_driver, "enable_notifications", fake_notify)
    return calls


def _register() -> None:
    assert tool_impl_x_write.register_tools() == 2
    # Dummy read tools so the non-x scopes resolve to a non-empty payload.
    tool_registry.register_tool(
        "page_read", "read a page", toolset="browser_read", handler=lambda **kw: "page"
    )
    tool_registry.register_tool(
        "chart_read", "read a chart", toolset="crypto", handler=lambda **kw: "chart"
    )


def _payload(toolsets: list[str], **config_extra):
    result = persona_tools.build_persona_tool_payload(
        PERSONA, {"toolsets": toolsets, **config_extra}
    )
    assert result is not None
    return result


def _propose_via_dispatch(arguments: dict, *, tool: str = "x_follow_accounts") -> str:
    """Drive the real dispatch path and return the card text."""
    _register()
    _defs, dispatch = _payload(["x_social_write"])
    return dispatch(tool, arguments)


def _code_from_card(card: str) -> str:
    match = re.search(r"\*\*Action `([A-Z0-9]{6})`\*\*", card)
    assert match, f"card carries no approval code: {card!r}"
    return match.group(1)


def _approve(code: str, **overrides):
    fields = {
        "user_role": "admin",
        "source": "interactive",
        "actor": "owner",
        "surface": "cli",
        "channel_id": "1",
    }
    fields.update(overrides)
    return action_proposals.decide_action(PERSONA, code, True, **fields)


def _store_rows(profile_dir: Path) -> list[dict]:
    store = profile_dir / "data" / action_proposals.STORE_FILENAME
    if not store.exists():
        return []
    conn = sqlite3.connect(store)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM persona_action_proposals")]
    finally:
        conn.close()


def _ledger_rows(profile_dir: Path) -> list[dict]:
    ledger = profile_dir / "data" / action_proposals.LEDGER_FILENAME
    if not ledger.exists():
        return []
    return [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── 1. A granted call proposes; it never executes ─────────────────────────


def test_granted_call_returns_a_card_and_touches_nothing(
    profile_dir: Path, driver_calls: dict
):
    card = _propose_via_dispatch({"handles": ["alice", "bob"], "enable_notifications": True})

    assert f"/act approve {PERSONA}" in card
    assert "@alice" in card and "@bob" in card
    assert driver_calls == {"follow": [], "notify": []}, "the handler must never drive"

    rows = _store_rows(profile_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == action_proposals.STATUS_PENDING
    assert row["tool_name"] == "x_follow_accounts"
    assert json.loads(row["arguments_json"]) == {
        "handles": ["alice", "bob"],
        "enable_notifications": True,
    }
    # Rule 4: the store landed in the TARGET persona's data dir, not ambient.
    assert row["persona_id"] == PERSONA
    assert not (Path(config.DATA_DIR) / action_proposals.STORE_FILENAME).exists()


# ── 2. Admin approve executes the stored payload, end to end ───────────────


def test_admin_approve_executes_and_leaves_physical_receipts(
    profile_dir: Path, driver_calls: dict
):
    card = _propose_via_dispatch({"handles": ["alice", "bob"], "enable_notifications": True})
    code = _code_from_card(card)

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_EXECUTED
    assert driver_calls["follow"] == [
        {
            "handles": ["alice", "bob"],
            "enable_notifications": True,
            "port": None,
            "session": None,
            # The executor handed the driver the gate-minted token bundle.
            "has_approval": True,
        }
    ]

    row = _store_rows(profile_dir)[0]
    assert row["status"] == action_proposals.STATUS_APPROVED
    assert row["decided_by"] == "owner"
    assert "followed" in row["outcome_json"]

    # Experience receipt physically present in the persona's own memory tree.
    notes = list((profile_dir / "memory" / "experience").glob("*.md"))
    assert notes, "no experience note written"
    body = notes[0].read_text(encoding="utf-8")
    assert "x_follow_accounts" in body
    assert "operator-approved -> executed" in body

    # Ledger carries the full lifecycle for this action id.
    outcomes = [
        (r["operation"], r["outcome"])
        for r in _ledger_rows(profile_dir)
        if r["correlation_id"] == row["action_id"]
    ]
    assert ("propose", "proposed") in outcomes
    assert ("decide", "approved") in outcomes
    assert ("execute", "executed") in outcomes


# ── 3. Refusal branches execute nothing ─────────────────────────────────────


def test_non_admin_decide_is_refused_and_nothing_runs(
    profile_dir: Path, driver_calls: dict
):
    code = _code_from_card(_propose_via_dispatch({"handles": ["alice"]}))

    decision = _approve(code, user_role="viewer")

    assert decision.outcome == action_proposals.DECISION_REFUSED
    assert "admin" in decision.message
    assert driver_calls["follow"] == []
    assert _store_rows(profile_dir)[0]["status"] == action_proposals.STATUS_PENDING
    refused = [r for r in _ledger_rows(profile_dir) if r["outcome"] == "refused"]
    assert refused and refused[0]["reason"] == action_proposals.REASON_NOT_AUTHORIZED


def test_non_interactive_source_is_refused(profile_dir: Path, driver_calls: dict):
    code = _code_from_card(_propose_via_dispatch({"handles": ["alice"]}))

    decision = _approve(code, source="cron")

    assert decision.outcome == action_proposals.DECISION_REFUSED
    assert driver_calls["follow"] == []
    assert _store_rows(profile_dir)[0]["status"] == action_proposals.STATUS_PENDING


def test_unknown_code_is_an_honest_miss(driver_calls: dict):
    _propose_via_dispatch({"handles": ["alice"]})
    decision = _approve("ZZZZZZ")
    assert decision.outcome == action_proposals.DECISION_UNKNOWN
    assert driver_calls["follow"] == []


def test_deny_records_the_decision_and_executes_nothing(
    profile_dir: Path, driver_calls: dict
):
    code = _code_from_card(_propose_via_dispatch({"handles": ["alice"]}))

    decision = action_proposals.decide_action(
        PERSONA, code, False, user_role="admin", source="interactive", actor="owner"
    )

    assert decision.outcome == action_proposals.DECISION_DENIED
    assert driver_calls["follow"] == []
    assert _store_rows(profile_dir)[0]["status"] == action_proposals.STATUS_DENIED


# ── 4. TTL and the CAS: expired cannot run, a double tap runs once ──────────


def test_expired_proposal_cannot_be_approved(profile_dir: Path, driver_calls: dict):
    _register()
    proposal = action_proposals.propose_action(
        PERSONA, "x_follow_accounts", {"handles": ["alice"]}, "follow alice", now=1000.0
    )
    assert proposal is not None

    decision = _approve(proposal.short_code, now=1000.0 + 3600)

    assert decision.outcome == action_proposals.DECISION_EXPIRED
    assert driver_calls["follow"] == []
    assert _store_rows(profile_dir)[0]["status"] == action_proposals.STATUS_EXPIRED


def test_double_approve_executes_at_most_once(profile_dir: Path, driver_calls: dict):
    code = _code_from_card(_propose_via_dispatch({"handles": ["alice"]}))

    first = _approve(code)
    second = _approve(code)

    assert first.outcome == action_proposals.DECISION_EXECUTED
    assert second.outcome == action_proposals.DECISION_ALREADY_DECIDED
    assert len(driver_calls["follow"]) == 1


# ── 5. The kill switch blocks the decision and nothing executes ─────────────


def test_kill_switch_disabled_propagates_and_blocks(
    profile_dir: Path, driver_calls: dict, monkeypatch: pytest.MonkeyPatch
):
    code = _code_from_card(_propose_via_dispatch({"handles": ["alice"]}))
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS", "disabled")

    with pytest.raises(kill_switches.KillSwitchDisabled):
        _approve(code)

    assert driver_calls["follow"] == []
    assert _store_rows(profile_dir)[0]["status"] == action_proposals.STATUS_PENDING


def test_kill_switch_disabled_blocks_proposing(
    driver_calls: dict, monkeypatch: pytest.MonkeyPatch
):
    _register()
    _defs, dispatch = _payload(["x_social_write"])
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS", "disabled")

    result = json.loads(dispatch("x_follow_accounts", {"handles": ["alice"]}))

    # The dispatch loop converts the propagated exception into an error
    # result for the model — the gate being off is an answer, not a crash.
    assert "error" in result
    assert driver_calls["follow"] == []


# ── 6. No grant, no proposal ───────────────────────────────────────────────


def test_persona_without_the_grant_is_out_of_scope(profile_dir: Path, driver_calls: dict):
    _register()
    _defs, dispatch = _payload(["browser"])

    result = json.loads(dispatch("x_follow_accounts", {"handles": ["alice"]}))

    assert "not in this persona's granted scope" in result["error"]
    assert driver_calls["follow"] == []
    assert _store_rows(profile_dir) == []


# ── 7. request_tool one-time elevation refuses a dedicated-gate tool ────────


def test_request_tool_cannot_elevate_a_dedicated_gate_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver_calls: dict
):
    from runtime import persona_elevation

    _register()
    persona_elevation.register_tools()
    monkeypatch.setattr(
        persona_tools, "PERSONA_CHAT_BASE_TOOLS", ("request_tool",), raising=False
    )
    context = {
        "persona_id": PERSONA,
        "platform": "cli",
        "channel_id": "chan-1",
        "thread_id": "chan-1",
        "session_key": "cli:test:test",
        "turn_id": "turn-1",
        "original_user_id": "operator-1",
        "original_user_name": "Operator",
        "original_user_role": "admin",
        "original_text": "follow alice",
        "has_attachments": False,
        "project_root": str(tmp_path),
    }
    payload = persona_tools.build_persona_tool_payload(
        PERSONA, {"toolsets": ["safe_core"]}, request_context=context
    )
    assert payload is not None

    result = json.loads(
        payload[1](
            "request_tool",
            {
                "tool": "x_follow_accounts",
                "reason": "need to follow one account",
                "arguments": {"handles": ["alice"]},
            },
        )
    )

    assert result["status"] == "refused"
    assert driver_calls["follow"] == []


# ── 8. Other toolsets never surface the write tools ─────────────────────────


@pytest.mark.parametrize("toolset", ["browser", "crypto", "social"])
def test_non_x_toolsets_contain_no_x_write_tools(toolset: str):
    _register()
    defs, _dispatch = _payload([toolset])
    names = {row["function"]["name"] for row in defs}
    assert "x_follow_accounts" not in names
    assert "x_enable_notifications" not in names


def test_x_social_write_toolset_surfaces_exactly_the_write_tools():
    _register()
    defs, _dispatch = _payload(["x_social_write"])
    names = {row["function"]["name"] for row in defs}
    assert {"x_follow_accounts", "x_enable_notifications"} <= names


# ── 9. Tamper: the executor runs the STORED payload ─────────────────────────


def test_decide_executes_the_stored_payload_not_caller_input(
    profile_dir: Path, driver_calls: dict
):
    code = _code_from_card(_propose_via_dispatch({"handles": ["alice"]}))

    # Rewrite the stored row the way a DB-level tamper would. The point of the
    # assertion is the direction of trust: execution reads the STORE, and
    # decide_action has no parameter through which caller arguments could
    # arrive at all.
    store = profile_dir / "data" / action_proposals.STORE_FILENAME
    conn = sqlite3.connect(store)
    try:
        conn.execute(
            "UPDATE persona_action_proposals SET arguments_json = ?",
            (json.dumps({"handles": ["mallory"], "enable_notifications": False}),),
        )
        conn.commit()
    finally:
        conn.close()

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_EXECUTED
    assert driver_calls["follow"][0]["handles"] == ["mallory"]
    assert driver_calls["follow"][0]["enable_notifications"] is False


# ── 10. No executor: loud, audited, nothing executed ────────────────────────


def test_approval_without_an_executor_fails_loudly(profile_dir: Path):
    proposal = action_proposals.propose_action(
        PERSONA, "x_unwired_tool", {"handles": ["alice"]}, "follow alice"
    )
    assert proposal is not None

    decision = _approve(proposal.short_code)

    assert decision.outcome == action_proposals.DECISION_FAILED
    assert "no registered executor" in decision.message
    row = _store_rows(profile_dir)[0]
    assert row["status"] == action_proposals.STATUS_APPROVED
    assert "no executor" in row["status_detail"]
    outcomes = [
        (r["operation"], r["outcome"], r["reason"]) for r in _ledger_rows(profile_dir)
    ]
    assert ("execute", "failed", action_proposals.REASON_NO_EXECUTOR) in outcomes


# ── Codex R1: receipt truthfulness ─────────────────────────────────────────


def test_failed_driver_receipt_is_recorded_as_failed_not_executed(
    profile_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    import x_action_driver

    monkeypatch.setattr(
        x_action_driver,
        "follow_accounts",
        lambda handles, **kw: {
            "ok": False,
            "action": "x_follow_accounts",
            "results": [
                {"handle": h, "status": "error", "detail": "open failed", "screenshot": None}
                for h in handles
            ],
        },
    )
    code = _code_from_card(_propose_via_dispatch({"handles": ["alice"]}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_FAILED
    assert "failure" in decision.message
    row = _store_rows(profile_dir)[0]
    assert row["status_detail"] == "failed"
    outcomes = [(r["operation"], r["outcome"]) for r in _ledger_rows(profile_dir)]
    assert ("execute", "failed") in outcomes
    assert ("execute", "executed") not in outcomes
    notes = list((profile_dir / "memory" / "experience").glob("*.md"))
    body = notes[0].read_text(encoding="utf-8")
    assert "operator-approved -> failed" in body
    assert "@alice: error" in body


def test_partial_driver_receipt_is_recorded_honestly(
    profile_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    import x_action_driver

    monkeypatch.setattr(
        x_action_driver,
        "follow_accounts",
        lambda handles, **kw: {
            "ok": False,
            "action": "x_follow_accounts",
            "results": [
                {"handle": "alice", "status": "followed", "detail": "",
                 "screenshot": "C:/receipts/alice.png"},
                {"handle": "bob", "status": "error", "detail": "Follow button not found",
                 "screenshot": None},
            ],
        },
    )
    code = _code_from_card(_propose_via_dispatch({"handles": ["alice", "bob"]}))

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_PARTIAL
    assert "1 of 2" in decision.message
    row = _store_rows(profile_dir)[0]
    assert row["status_detail"] == "partial"
    outcomes = [(r["operation"], r["outcome"]) for r in _ledger_rows(profile_dir)]
    assert ("execute", "partial") in outcomes
    # The persona's memory lists each handle's real outcome, with evidence.
    notes = list((profile_dir / "memory" / "experience").glob("*.md"))
    body = notes[0].read_text(encoding="utf-8")
    assert "operator-approved -> partial" in body
    assert "@alice: followed (C:/receipts/alice.png)" in body
    assert "@bob: error (Follow button not found)" in body


# ── Codex R1: pathological failures still leave a complete row ──────────────


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("str is broken")

    def __repr__(self) -> str:
        raise RuntimeError("repr is broken too")


def test_pathological_executor_exception_still_records_everything(
    profile_dir: Path,
):
    def boom(**kwargs):
        raise _UnprintableError()

    _register()
    # Replace the real executor AFTER registration: the tool stays wired, the
    # executor is the pathology under test.
    action_proposals.register_action_executor("x_follow_accounts", boom)
    _defs, dispatch = _payload(["x_social_write"])
    card = dispatch("x_follow_accounts", {"handles": ["alice"]})
    code = _code_from_card(card)

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_FAILED
    assert "_UnprintableError" in decision.message
    row = _store_rows(profile_dir)[0]
    assert row["status"] == action_proposals.STATUS_APPROVED
    assert row["status_detail"].startswith("failed: _UnprintableError")
    outcomes = [(r["operation"], r["outcome"]) for r in _ledger_rows(profile_dir)]
    assert ("execute", "failed") in outcomes
    notes = list((profile_dir / "memory" / "experience").glob("*.md"))
    assert notes, "experience note must exist even for a pathological failure"


# ── Codex R1: type-confused payloads are refused, no row ────────────────────


@pytest.mark.parametrize("bad_handles", [[123], [True], [None], [{"h": "alice"}]])
def test_non_string_handles_are_refused(profile_dir: Path, bad_handles: list):
    result = _propose_via_dispatch_raw(bad_handles)
    assert result.startswith("error: every handle must be a string")
    assert _store_rows(profile_dir) == []


def _propose_via_dispatch_raw(handles) -> str:
    _register()
    _defs, dispatch = _payload(["x_social_write"])
    return dispatch("x_follow_accounts", {"handles": handles})


@pytest.mark.parametrize("bad_flag", ["false", 1, 0, None, "yes"])
def test_non_boolean_notification_flag_is_refused(profile_dir: Path, bad_flag):
    _register()
    _defs, dispatch = _payload(["x_social_write"])
    result = dispatch(
        "x_follow_accounts", {"handles": ["alice"], "enable_notifications": bad_flag}
    )
    assert "must be a JSON boolean" in result
    assert _store_rows(profile_dir) == []


# ── Codex R1 BLOCKER: the persona terminal cannot reach for gate internals ──


@pytest.mark.parametrize(
    "needle",
    ["decide_action(", "x_action_driver", "propose_action(", "persona_action_proposals"],
)
def test_persona_terminal_refuses_gate_internal_commands(needle: str):
    from runtime import tool_impl_exec

    command = (
        "python -c \"import personas.action_proposals as ap; ap."
        + needle
        + "'sales', 'ABC123', True, user_role='admin', source='interactive')\""
    )
    result = tool_impl_exec._terminal(command=command)
    assert result.startswith("error: refused")
    assert "action-gate internals" in result


def test_gate_internal_patterns_are_not_in_the_shared_denylist():
    """The operator's PreToolUse hook list is deliberately untouched."""
    import shared

    for pattern in ("decide_action(", "x_action_driver", "propose_action(",
                    "persona_action_proposals"):
        assert pattern not in shared.DANGEROUS_BASH_PATTERNS
        assert pattern not in shared.DANGEROUS_SSH_PATTERNS


def test_persona_terminal_still_runs_benign_commands():
    from runtime import tool_impl_exec

    result = tool_impl_exec._terminal(command="echo gate-benign-check")
    assert "gate-benign-check" in result
    assert "refused" not in result


# ── Codex R2: a failed requested sub-action is not a full execution ─────────


def test_follow_ok_notify_failed_classifies_partial_and_note_shows_both(
    profile_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """The driver-shaped row {follow landed, notifications failed} must not
    classify as executed — the operator approved BOTH sub-actions."""
    import x_action_driver

    monkeypatch.setattr(
        x_action_driver,
        "follow_accounts",
        lambda handles, **kw: {
            "ok": False,
            "action": "x_follow_accounts",
            "results": [
                {
                    "handle": "alice",
                    "status": "followed",
                    "detail": "",
                    "screenshot": "C:/receipts/alice.png",
                    "notifications": "error",
                    "notification_detail": (
                        "notification bell did not reach the on state after click"
                    ),
                }
            ],
        },
    )
    code = _code_from_card(
        _propose_via_dispatch({"handles": ["alice"], "enable_notifications": True})
    )

    decision = _approve(code)

    assert decision.outcome == action_proposals.DECISION_PARTIAL
    row = _store_rows(profile_dir)[0]
    assert row["status_detail"] == "partial"
    outcomes = [(r["operation"], r["outcome"]) for r in _ledger_rows(profile_dir)]
    assert ("execute", "partial") in outcomes
    assert ("execute", "executed") not in outcomes
    notes = list((profile_dir / "memory" / "experience").glob("*.md"))
    body = notes[0].read_text(encoding="utf-8")
    assert "@alice: followed; notifications: error" in body
    assert "did not reach the on state" in body
