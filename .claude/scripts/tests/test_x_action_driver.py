"""X action driver (epic #465 1a, Codex R1) — the execution boundary itself.

The persona-level flow (dispatch -> proposal -> decide) lives in
``test_persona_action_proposals.py`` with the driver mocked. This file tests
the REAL driver with the BROWSER mocked instead (``browser_control`` module
attrs), because the R1 findings live at this layer:

* no token / spent token / token bound to a different payload -> fail closed,
  zero browser commands (the gate-bypass BLOCKER)
* the kill switch is re-checked per handle — a mid-batch flip skips the rest
* the port pins to 18222, ignores env retargeting, and refuses 9222 always
* notification verification compares pre/post button state — an unchanged
  off-state snapshot is a failure, never "notifications_on"
* a raised per-handle error still leaves a browser-audit row

Tokens are minted through the REAL gate: ``propose_action`` + ``decide_action``
with a capturing executor, so the approval bundle under test is exactly what
production hands the driver.
"""

from __future__ import annotations

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

import x_action_driver  # noqa: E402

import config  # noqa: E402
from personas import action_proposals  # noqa: E402

PERSONA = "sales"

def _default_buttons() -> dict[str, str]:
    """A profile page carrying controls for THREE handles (Codex R2 shape)."""
    return {
        "e9": "Follow @alice",
        "e10": "Follow @bob",
        "e11": "Follow @carol",
        "e7": "Turn on notifications",
    }


def _toggle(name: str) -> str:
    """Realistic control behavior: each control toggles ITS OWN state only."""
    low = name.lower()
    if low.startswith("follow @"):
        return "Following @" + name.split("@", 1)[1]
    if low.startswith("following @"):
        return "Follow @" + name.split("@", 1)[1]
    if "turn on" in low:
        return "Turn off notifications"
    if "turn off" in low:
        return "Turn on notifications"
    return name


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The browser layer, faked at the module attrs the driver reads.

    The fake mutates ONLY the clicked control's own row (Codex R2 — the R1
    whole-snapshot swap masked which ref the driver selected). Click behavior
    is overridable per test via ``state["on_click"](ref)``; setting
    ``state["respond"]`` False simulates X ignoring the click entirely.
    """
    import browser_audit
    import browser_control

    state = {
        "buttons": _default_buttons(),
        "respond": True,
        "on_click": None,
    }
    commands: list[list[str]] = []
    ports: list[int] = []
    audits: list[dict] = []

    def fake_run(args, *, port, session=None, timeout=30, **kw):
        commands.append(list(args))
        ports.append(port)
        if args[:1] == ["click"] and state["respond"]:
            ref = str(args[1]).lstrip("@")
            if state["on_click"] is not None:
                state["on_click"](ref)
            elif ref in state["buttons"]:
                state["buttons"][ref] = _toggle(state["buttons"][ref])
        if args[:1] == ["snapshot"]:
            text = "".join(
                f'- button "{name}" [ref={ref}]\n'
                for ref, name in state["buttons"].items()
            )
            return SimpleNamespace(ok=True, stdout=text, output=text, stderr="")
        return SimpleNamespace(ok=True, stdout="", output="", stderr="")

    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run)
    monkeypatch.setattr(
        browser_control, "capture_browser_screenshot_png", lambda **kw: b"png-bytes"
    )
    monkeypatch.setattr(
        browser_audit,
        "append_browser_audit_record",
        lambda **kw: audits.append(kw) or kw,
    )
    return SimpleNamespace(state=state, commands=commands, ports=ports, audits=audits)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    homie = tmp_path / ".homie"
    profile_dir = homie / "profiles" / PERSONA
    (profile_dir / "data").mkdir(parents=True)
    (profile_dir / "memory").mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "ambient-data", raising=False)
    for var in (
        "HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS",
        "HOMIE_X_CDP_PORT",
        "X_BROWSER_CDP_PORT",
        "HOMIE_SOCIAL_CDP_PORT",
        "HOMIE_BROWSER_CDP_PORT",
        "AGENT_BROWSER_CDP_PORT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "personas.experience._reindex_note", lambda *a, **k: None, raising=False
    )
    saved = dict(action_proposals._EXECUTORS)
    action_proposals._EXECUTORS.clear()
    yield tmp_path
    action_proposals._EXECUTORS.clear()
    action_proposals._EXECUTORS.update(saved)


def _mint(arguments: dict | None = None, *, tool: str = "x_follow_accounts") -> dict:
    """A real approval bundle: gate-minted token, captured at the executor."""
    args = (
        arguments
        if arguments is not None
        else {"handles": ["alice"], "enable_notifications": False}
    )
    proposal = action_proposals.propose_action(PERSONA, tool, args, "test summary")
    assert proposal is not None
    captured: dict = {}

    def capture(**kw):
        captured.update(kw)
        return {"ok": True, "results": []}

    action_proposals.register_action_executor(tool, capture)
    decision = action_proposals.decide_action(
        PERSONA, proposal.short_code, True,
        user_role="admin", source="interactive", actor="tester",
    )
    assert decision.outcome == action_proposals.DECISION_EXECUTED
    return {
        "persona_id": captured["persona_id"],
        "action_id": captured["action_id"],
        "token": captured["execution_token"],
        "payload": captured["arguments"],
    }


# ── The token boundary (BLOCKER) ────────────────────────────────────────────


def test_driver_refuses_without_an_approval_token(browser):
    receipt = x_action_driver.follow_accounts(["alice"])

    assert receipt["ok"] is False
    assert {row["status"] for row in receipt["results"]} == {"refused"}
    assert browser.commands == [], "a tokenless call must not move the browser"


def test_driver_refuses_a_consumed_token(browser):
    approval = _mint()
    first = x_action_driver.follow_accounts(["alice"], approval=approval)
    assert first["ok"] is True
    assert first["results"][0]["status"] == "followed"
    before = len(browser.commands)

    second = x_action_driver.follow_accounts(["alice"], approval=approval)

    assert second["ok"] is False
    assert "consumed" in second["results"][0]["detail"]
    assert len(browser.commands) == before, "a replay must not move the browser"


def test_driver_refuses_a_token_bound_to_a_different_payload(browser):
    approval = _mint({"handles": ["alice"], "enable_notifications": False})
    # Same token, payload the operator did NOT approve. The handles agree with
    # the call, so only the hash binding can catch this.
    tampered = {**approval, "payload": {"handles": ["mallory"], "enable_notifications": False}}
    receipt = x_action_driver.follow_accounts(["mallory"], approval=tampered)

    assert receipt["ok"] is False
    assert receipt["results"][0]["status"] == "refused"
    assert browser.commands == []


def test_driver_refuses_an_approval_whose_payload_disagrees_with_the_call(browser):
    approval = _mint({"handles": ["alice"], "enable_notifications": False})
    # The approved payload says alice; the call asks for bob.
    receipt = x_action_driver.follow_accounts(["bob"], approval=approval)

    assert receipt["ok"] is False
    assert "do not match" in receipt["results"][0]["detail"]
    assert browser.commands == []


def test_valid_token_executes_and_writes_evidence(browser, tmp_path: Path):
    approval = _mint()
    receipt = x_action_driver.follow_accounts(["alice"], approval=approval)

    assert receipt["ok"] is True
    row = receipt["results"][0]
    assert row["status"] == "followed"
    assert row["screenshot"], "a landed follow persists a screenshot receipt"
    assert Path(row["screenshot"]).is_file()
    assert any(cmd[:1] == ["click"] for cmd in browser.commands)
    assert any(a["outcome"] == "followed" for a in browser.audits)


# ── Kill switch per handle (BLOCKER part b) ─────────────────────────────────


def test_kill_switch_flip_mid_batch_skips_the_remaining_handles(
    browser, monkeypatch: pytest.MonkeyPatch
):
    # Mint FIRST: the gate itself consults the switch during decide, and the
    # flip-under-test must only exist once the driver loop is running.
    approval = _mint({"handles": ["alice", "bob", "carol"], "enable_notifications": False})

    checks = {"calls": 0}

    def flip_after_first(name: str) -> bool:
        checks["calls"] += 1
        return checks["calls"] > 1  # first handle clears, then the switch is OFF

    monkeypatch.setattr("security.kill_switches.is_disabled", flip_after_first)

    receipt = x_action_driver.follow_accounts(
        ["alice", "bob", "carol"], approval=approval
    )

    statuses = {row["handle"]: row["status"] for row in receipt["results"]}
    assert statuses == {"alice": "followed", "bob": "skipped", "carol": "skipped"}
    opens = [cmd for cmd in browser.commands if cmd[:1] == ["open"]]
    assert len(opens) == 1, "skipped handles never reach the browser"


# ── The port pin (M3) ───────────────────────────────────────────────────────


def test_port_pins_to_18222_and_ignores_the_environment(
    browser, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOMIE_X_CDP_PORT", "9222")
    monkeypatch.setenv("AGENT_BROWSER_CDP_PORT", "9999")
    approval = _mint()

    receipt = x_action_driver.follow_accounts(["alice"], approval=approval)

    assert receipt["port"] == 18222
    assert browser.ports and set(browser.ports) == {18222}, (
        "an env-retargeted port must never reach the browser layer"
    )


def test_explicit_9222_is_refused_even_as_a_test_seam(browser):
    approval = _mint()
    receipt = x_action_driver.follow_accounts(["alice"], port=9222, approval=approval)

    assert receipt["ok"] is False
    assert "9222" in receipt["results"][0]["detail"]
    assert browser.commands == []


def test_explicit_non_forbidden_port_is_the_test_seam(browser):
    approval = _mint()
    receipt = x_action_driver.follow_accounts(["alice"], port=19222, approval=approval)
    assert receipt["port"] == 19222
    assert receipt["ok"] is True


# ── Notification verification is directional (R1 M6 / R2 MAJOR) ────────────


def test_unchanged_notification_bell_is_a_failure_not_a_success(browser):
    browser.state["respond"] = False  # X ignored the click: same name after
    approval = _mint({"handles": ["alice"]}, tool="x_enable_notifications")

    receipt = x_action_driver.enable_notifications(["alice"], approval=approval)

    row = receipt["results"][0]
    assert row["status"] == "error"
    assert "did not reach the on state" in row["detail"]
    assert not any(a["outcome"] == "notifications_on" for a in browser.audits)


def test_flipped_notification_bell_is_reported_on(browser):
    approval = _mint({"handles": ["alice"]}, tool="x_enable_notifications")

    receipt = x_action_driver.enable_notifications(["alice"], approval=approval)

    assert receipt["results"][0]["status"] == "notifications_on"
    assert any(a["outcome"] == "notifications_on" for a in browser.audits)


def test_already_on_bell_is_success_without_clicking(browser):
    """Idempotent enable: an already-on bell must NOT be clicked — clicking
    the toggle would turn notifications OFF and a naive check would report on.
    """
    browser.state["buttons"] = {"e7": "Turn off notifications"}
    approval = _mint({"handles": ["alice"]}, tool="x_enable_notifications")

    receipt = x_action_driver.enable_notifications(["alice"], approval=approval)

    assert receipt["results"][0]["status"] == "notifications_on"
    clicks = [cmd for cmd in browser.commands if cmd[:1] == ["click"]]
    assert clicks == [], "an already-on bell must never be clicked"
    # The bell is still on: no toggle happened to flip it off.
    assert browser.state["buttons"]["e7"] == "Turn off notifications"


def test_unrecognized_bell_state_is_an_error_not_a_guess(browser):
    browser.state["buttons"] = {"e7": "Notifications"}
    approval = _mint({"handles": ["alice"]}, tool="x_enable_notifications")

    receipt = x_action_driver.enable_notifications(["alice"], approval=approval)

    row = receipt["results"][0]
    assert row["status"] == "error"
    assert "unrecognized" in row["detail"]
    assert not any(cmd[:1] == ["click"] for cmd in browser.commands)


# ── Follow controls are anchored to the approved handle (R2 BLOCKER) ────────


def test_click_targets_the_approved_handles_own_ref(browser):
    """Only bob is approved; the page carries alice's control FIRST. The
    driver must select bob's ref — page order must never pick the target."""
    approval = _mint({"handles": ["bob"], "enable_notifications": False})

    receipt = x_action_driver.follow_accounts(["bob"], approval=approval)

    assert receipt["results"][0]["status"] == "followed"
    clicks = [cmd for cmd in browser.commands if cmd[:1] == ["click"]]
    assert clicks == [["click", "e10"]], "the click must target @bob's own control"
    assert browser.state["buttons"]["e9"] == "Follow @alice", "alice untouched"
    assert browser.state["buttons"]["e10"] == "Following @bob"


def test_a_change_on_another_handles_control_does_not_count(browser):
    """A buggy/malicious page flips ALICE's control when bob's ref is clicked.
    The driver's verification re-anchors on bob, so this is a failure."""

    def buggy_page(clicked_ref: str) -> None:
        # Whatever was clicked, alice's control flips; bob's never does.
        browser.state["buttons"]["e9"] = "Following @alice"

    browser.state["on_click"] = buggy_page
    approval = _mint({"handles": ["bob"], "enable_notifications": False})

    receipt = x_action_driver.follow_accounts(["bob"], approval=approval)

    row = receipt["results"][0]
    assert row["status"] == "error"
    assert "did not flip" in row["detail"]
    assert not any(a["outcome"] == "followed" for a in browser.audits)


def test_unanchorable_snapshot_fails_closed_without_clicking(browser):
    """A bare `Follow` control names no handle — anchoring is impossible."""
    browser.state["buttons"] = {"e9": "Follow"}
    approval = _mint({"handles": ["alice"], "enable_notifications": False})

    receipt = x_action_driver.follow_accounts(["alice"], approval=approval)

    row = receipt["results"][0]
    assert row["status"] == "error"
    assert "cannot anchor" in row["detail"]
    assert not any(cmd[:1] == ["click"] for cmd in browser.commands)


def test_already_following_is_reported_without_clicking(browser):
    browser.state["buttons"]["e9"] = "Following @alice"
    approval = _mint({"handles": ["alice"], "enable_notifications": False})

    receipt = x_action_driver.follow_accounts(["alice"], approval=approval)

    assert receipt["results"][0]["status"] == "already_following"
    assert not any(cmd[:1] == ["click"] for cmd in browser.commands)


# ── Combined follow+notify verdict (R2 MAJOR) ───────────────────────────────


def test_failed_notify_inside_a_successful_follow_is_not_ok(browser):
    """Follow lands, the requested notification toggle fails: the receipt must
    carry both sub-action outcomes and the batch must not report ok."""

    def bell_ignores_clicks(ref: str) -> None:
        if ref != "e7":
            browser.state["buttons"][ref] = _toggle(browser.state["buttons"][ref])

    browser.state["on_click"] = bell_ignores_clicks
    approval = _mint({"handles": ["alice"], "enable_notifications": True})

    receipt = x_action_driver.follow_accounts(
        ["alice"], enable_notifications=True, approval=approval
    )

    row = receipt["results"][0]
    assert row["status"] == "followed"
    assert row["notifications"] == "error"
    assert row["notification_detail"]
    assert receipt["ok"] is False, "a failed requested sub-action fails the batch"


# ── Raised per-handle errors still leave audit rows (M5) ────────────────────


def test_a_raised_handle_is_data_and_is_audited(
    browser, monkeypatch: pytest.MonkeyPatch
):
    import browser_control

    real_run = browser_control.run_agent_browser

    def exploding_run(args, *, port, session=None, timeout=30, **kw):
        if args[:1] == ["open"] and args[1].endswith("/bob"):
            raise RuntimeError("renderer gone")
        return real_run(args, port=port, session=session, timeout=timeout, **kw)

    monkeypatch.setattr(browser_control, "run_agent_browser", exploding_run)
    approval = _mint({"handles": ["alice", "bob"], "enable_notifications": False})

    receipt = x_action_driver.follow_accounts(["alice", "bob"], approval=approval)

    statuses = {row["handle"]: row["status"] for row in receipt["results"]}
    assert statuses == {"alice": "followed", "bob": "error"}
    assert receipt["ok"] is False
    error_rows = [a for a in browser.audits if a["outcome"] == "error"]
    assert any("renderer gone" in a["reason"] for a in error_rows)
