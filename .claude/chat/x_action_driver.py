"""X action driver — follows and notification bells via the visible browser.

Epic #465 ticket 1a. This is the EXECUTION half of the X write tools: it runs
only after ``personas.action_proposals.decide_action`` has seen an
authenticated admin approval, and it runs the stored payload — there is no
other caller and no other road in.

Hard invariants, inherited from ``social_write_driver.py``:

  - Attach-only. Every command goes through ``browser_control.run_agent_browser``
    against the already-running CDP Chrome on port 18222 — NEVER 9222, which
    sits inside this machine's WSL2/Hyper-V reserved range and fails bind().
    Nothing here launches a browser, a profile, or a headless fallback, and
    no environment variable can retarget this write path (Codex R1): the port
    is pinned, an explicit ``port=`` is a test seam, and 9222 is refused even
    then.
  - Approval artifact required (Codex R1 BLOCKER). The first act of every
    public entry point is consuming the one-use execution token the gate
    minted at approval time, via
    ``personas.action_proposals.consume_execution_token``. No token, a spent
    token, or a token bound to a different payload all fail closed — a direct
    in-process caller cannot drive a write around the gate.
  - Named session ``persona-x-hands`` keeps these writes out of the operator's
    own tab state, the same way the social-write driver uses ``primo-x``.
  - The kill switch is re-checked before EVERY handle, not once per batch: a
    mid-batch flip stops the remaining handles, which are recorded as
    ``skipped`` in the receipt.
  - Per-handle failures are DATA, never exceptions. One bad handle (deleted
    account, rate wall, selector drift) must not stop the rest of the batch or
    crash the deciding turn — the receipt says exactly which handles landed,
    and every failure (including raised ones) leaves a browser-audit row.
  - Receipts are files, not bytes: screenshots persist under
    ``DATA_DIR/browser_writes/`` (git-ignored) and only the PATH travels.

**What this does NOT defend, stated plainly** (the ``tool_impl_exec``
honesty precedent): a persona granted ``terminal`` holds a shell, and a shell
is a superset of every tool-layer gate — it can edit these files, mint its
own rows, or drive the browser directly. The token boundary defends
in-process and tool-path misuse (a direct driver call, a replayed approval,
a swapped payload), not a granted shell. That residual is a follow-up issue,
not something this module claims to close.

Everything is sync; the chat seam hops a thread before any of this runs.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# Named CDP session for persona-driven X writes — the driver's own tab state,
# never the operator's. Mirrors the social-write driver's `primo-x` precedent.
DEFAULT_SESSION = "persona-x-hands"

# The keeper-owned visible session this write path attaches to (the BrowserOps
# manual, docs/browserops-agent-browser-manual.md §8). PINNED: no env chain.
# A stale HOMIE_X_CDP_PORT used to retarget an approved write at the wrong
# browser (Codex R1) — environment cannot move a write path. Tests reseat
# ``_cdp_port`` at the module attribute (Rule 3) or pass ``port=``.
_DEFAULT_CDP_PORT = 18222
# Never attachable, even explicitly: 9222 sits inside the Windows WSL2/Hyper-V
# reserved range on this machine, and bind() there returns WSAEACCES.
_FORBIDDEN_CDP_PORT = 9222

# Last code-path check before a browser move. The tool handler validates
# earlier; this exists so no future caller can drive an arbitrary URL through
# the follow flow.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

# Accessibility-snapshot shapes, verified against the live X UI pattern the
# social-write driver already parses (`button "Name" [ref=e12]`). X renders
# the follow control as a button whose accessible name carries the TARGET
# handle: `Follow @handle` flipping to `Following @handle`. One pattern
# captures (state, handle, ref) so the driver can ANCHOR on the approved
# handle (Codex R2 BLOCKER): a profile page snapshot can carry Follow
# controls for other accounts (who-to-follow rails, hovercards), and the
# first match in page order is not the approved target. The two states are
# disjoint: "Following" fails the bare "Follow" parse because group 1 must
# be exactly one of the two words.
_FOLLOW_BTN_RE = re.compile(
    r'button "(Follow|Following)(?: @([^"]+))?" \[ref=(e\d+)\]', re.IGNORECASE
)
# The notification bell, matched as (name, ref). Direction is parsed
# SEMANTICALLY from the accessible name (Codex R2): "Turn on notifications"
# is the off state, "Turn off notifications" is the on state, and any other
# name is unrecognized — never "the string changed, call it on".
_NOTIFY_BTN_RE = re.compile(r'button "([^"]*[Nn]otif[^"]*)" \[ref=(e\d+)\]')
_NOTIFY_OFF_RE = re.compile(r"turn on", re.IGNORECASE)
_NOTIFY_ON_RE = re.compile(r"turn off", re.IGNORECASE)


def _follow_controls(snapshot: str) -> list[dict[str, str]]:
    """Every Follow/Following control in a snapshot: state, handle, ref."""
    controls: list[dict[str, str]] = []
    for match in _FOLLOW_BTN_RE.finditer(snapshot):
        controls.append(
            {
                "state": "following"
                if match.group(1).lower() == "following"
                else "follow",
                "handle": (match.group(2) or "").casefold(),
                "ref": match.group(3),
            }
        )
    return controls


def _anchor_follow_control(
    controls: list[dict[str, str]], handle: str
) -> dict[str, str] | None:
    """The ONE control belonging to *handle*, or None when ambiguous.

    Fail-closed by contract (Codex R2): a bare `Follow` with no handle, or
    two rows naming the same handle, are both unanchorable — clicking either
    would be guessing which account the approved payload meant.
    """
    wanted = handle.casefold()
    matches = [c for c in controls if c["handle"] == wanted]
    if len(matches) != 1:
        return None
    return matches[0]


def _data_dir() -> Path:
    """Resolved at call time (Rule 1 — no config value bound at def time)."""
    try:
        from config import DATA_DIR

        return Path(DATA_DIR)
    except Exception:  # pragma: no cover - import path fallback for direct scripts
        from personas import get_default_paths

        return get_default_paths()["data"]


def _cdp_port() -> int:
    """The pinned write port. A function so tests reseat it at the module attr."""
    return _DEFAULT_CDP_PORT


def _resolve_port(port: int | None) -> int:
    """Resolve the attach port, failing CLOSED on the forbidden legacy port.

    ``port=None`` resolves through ``_cdp_port()`` (pinned 18222). An explicit
    value is the test seam — but 9222 is refused even then: there is no
    legitimate caller for it, only stale state.
    """
    resolved = port if port is not None else _cdp_port()
    resolved = int(resolved)
    if resolved == _FORBIDDEN_CDP_PORT:
        raise ValueError(
            "CDP port 9222 is forbidden (WSL2-reserved range); the X write path "
            "attaches to the keeper-owned session on 18222"
        )
    return resolved


def _run(args: list[str], *, port: int, session: str, timeout: int = 30) -> Any:
    """One attach-only agent-browser command. Late module attr (Rule 3)."""
    import browser_control

    return browser_control.run_agent_browser(
        args,
        port=port,
        session=session,
        timeout=timeout,
    )


def _step_detail(result: Any) -> str:
    import browser_control

    return browser_control.redact_text_urls((result.output or "(no output)")[:300])


def _kill_switch_off() -> bool:
    """The gate's kill switch, read fresh per call (Rule 3 module attr).

    An unavailable switch module reads as ON — the switch is an operator OFF
    control, not the thing that grants the capability, and the gate's own
    decide path already enforced it before this driver was handed a token.
    """
    try:
        from security import kill_switches
    except Exception:  # noqa: BLE001 — absence must not silently disable
        return False
    try:
        return kill_switches.is_disabled("persona_action_proposals")
    except Exception:  # noqa: BLE001 — same
        return False


def _consume_approval(
    approval: Any,
    handles: list[str],
    *,
    enable_notifications: bool = False,
) -> str:
    """Verify-and-consume the gate's execution token. "" = cleared to run.

    Beyond the token itself this checks the approval payload AGREES with the
    work requested: a token is bound to the stored payload by hash, but the
    driver executes its ``handles`` argument, so an executor passing the
    approved payload beside different handles would otherwise launder one
    approval into a different write. The comparison makes that mismatch a
    refusal instead.
    """
    if not isinstance(approval, dict):
        return "missing the gate's execution token — writes require an approved action"
    payload = approval.get("payload")
    if not isinstance(payload, dict):
        return "approval carries no payload"
    if list(payload.get("handles") or []) != list(handles):
        return "approval payload handles do not match the requested handles"
    if bool(payload.get("enable_notifications", False)) != bool(enable_notifications):
        return "approval payload does not match the requested notification flag"
    from personas import action_proposals  # noqa: PLC0415 — Rule 3 module attr

    if not action_proposals.consume_execution_token(
        str(approval.get("persona_id") or ""),
        str(approval.get("action_id") or ""),
        str(approval.get("token") or ""),
        payload,
    ):
        return "execution token invalid, already consumed, or bound to a different payload"
    return ""


def _receipt_screenshot(
    *, port: int, session: str, handle: str, workflow: str
) -> str | None:
    """Persist a PNG receipt under browser_writes/ and return its PATH.

    Best-effort by design: a screenshot failure must not convert a landed
    follow into a failed one — the click verification is the truth, the PNG
    is evidence.
    """
    try:
        import browser_control

        data = browser_control.capture_browser_screenshot_png(port=port, session=session)
        out_dir = _data_dir() / "browser_writes"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{ts}-{workflow}-{handle}.png"
        out_path.write_bytes(data)
        return str(out_path)
    except Exception as exc:  # noqa: BLE001 — evidence only, never the action
        _logger.warning(
            "x-action screenshot for %s failed: %s: %s", handle, type(exc).__name__, exc
        )
        return None


def _audit(
    *,
    workflow_id: str,
    command: str,
    outcome: str,
    reason: str = "",
    handle: str,
    port: int,
    session: str,
) -> None:
    """One browser-audit row per handle attempt. Never raises into the loop."""
    try:
        import browser_audit

        browser_audit.append_browser_audit_record(
            command=command,
            workflow_id=workflow_id,
            outcome=outcome,
            reason=reason,
            cdp_port=port,
            surface="cli",
            session_id=session,
            target_url=f"https://x.com/{handle}",
            executor_name="x_action_driver",
        )
    except Exception as exc:  # noqa: BLE001 — logged, never the action's verdict
        _logger.error("x-action audit row failed for %s: %s: %s", handle, type(exc).__name__, exc)


def _snapshot_buttons(port: int, session: str) -> str:
    """The interactive snapshot text, or "" on failure."""
    result = _run(["snapshot", "-i"], port=port, session=session, timeout=30)
    if not result.ok:
        return ""
    return str(result.stdout or "")


def _open_profile(handle: str, *, port: int, session: str) -> str:
    """Open the profile page. Returns "" on success, else an error detail."""
    opened = _run(["open", f"https://x.com/{handle}"], port=port, session=session, timeout=30)
    if not opened.ok:
        return f"open failed: {_step_detail(opened)}"
    for step in (["wait", "--load", "domcontentloaded"], ["wait", "2000"]):
        result = _run(step, port=port, session=session, timeout=30)
        if not result.ok:
            return f"{step[0]} failed: {_step_detail(result)}"
    return ""


def _follow_one(handle: str, *, port: int, session: str) -> dict[str, Any]:
    """Follow one account. Outcome is data — this function never raises.

    The control is ANCHORED to the requested handle at every step (Codex R2
    BLOCKER): pre-state is read from `@handle`'s own row, the click targets
    that row's ref, and the post-check re-anchors on the same handle. A
    snapshot where the handle cannot be anchored unambiguously fails closed —
    a token bound to @handle must never follow whatever came first in the
    page order.
    """
    row: dict[str, Any] = {"handle": handle, "status": "error", "detail": "", "screenshot": None}
    if not _HANDLE_RE.match(handle):
        row["detail"] = "not an X handle shape"
        _audit(
            workflow_id="x.follow", command="x.follow", outcome="refused",
            reason=row["detail"], handle=handle, port=port, session=session,
        )
        return row

    error = _open_profile(handle, port=port, session=session)
    if error:
        row["detail"] = error
    else:
        snap = _snapshot_buttons(port, session)
        if not snap:
            row["detail"] = "profile snapshot failed"
        else:
            anchor = _anchor_follow_control(_follow_controls(snap), handle)
            if anchor is None:
                row["detail"] = (
                    f"cannot anchor a Follow control to @{handle} unambiguously "
                    "(suspended, blocked, UI drift, or a bare control)"
                )
            elif anchor["state"] == "following":
                row["status"] = "already_following"
            else:
                click = _run(["click", anchor["ref"]], port=port, session=session, timeout=20)
                if not click.ok:
                    row["detail"] = f"follow click failed: {_step_detail(click)}"
                else:
                    _run(["wait", "1500"], port=port, session=session, timeout=10)
                    # The click result is a claim; the SAME handle's flipped
                    # control is the fact. Any other control changing proves
                    # nothing about @handle.
                    post = _anchor_follow_control(
                        _follow_controls(_snapshot_buttons(port, session)), handle
                    )
                    if post is not None and post["state"] == "following":
                        row["status"] = "followed"
                    else:
                        row["detail"] = (
                            f"@{handle}'s Follow control did not flip to Following"
                        )

    if row["status"] == "error":
        _audit(
            workflow_id="x.follow", command="x.follow", outcome="error",
            reason=row["detail"], handle=handle, port=port, session=session,
        )
        return row

    row["screenshot"] = _receipt_screenshot(
        port=port, session=session, handle=handle, workflow="x-follow"
    )
    _audit(
        workflow_id="x.follow", command="x.follow", outcome=row["status"],
        handle=handle, port=port, session=session,
    )
    return row


def _notify_one(handle: str, *, port: int, session: str) -> dict[str, Any]:
    """Enable the notification bell on one profile. Never raises.

    DIRECTION matters (Codex R2): the bell is a toggle, so clicking an
    already-on bell turns notifications OFF. The pre-click accessible name is
    parsed semantically — "Turn off notifications" means ALREADY ON, which is
    the requested end state: report success WITHOUT clicking (idempotent
    enable). "Turn on notifications" means off: click, then require the known
    off->on transition (post-name matches the ON pattern) — any other post
    state, including an unchanged one, is an error. An unrecognized name is
    an error, never a guess.
    """
    row: dict[str, Any] = {"handle": handle, "status": "error", "detail": "", "screenshot": None}
    if not _HANDLE_RE.match(handle):
        row["detail"] = "not an X handle shape"
        return row

    error = _open_profile(handle, port=port, session=session)
    if error:
        row["detail"] = error
    else:
        snap = _snapshot_buttons(port, session)
        if not snap:
            row["detail"] = "profile snapshot failed"
        else:
            match = _NOTIFY_BTN_RE.search(snap)
            if not match:
                row["detail"] = "notification bell not found (UI drift or not logged in)"
            else:
                pre_name, ref = match.group(1), match.group(2)
                if _NOTIFY_ON_RE.search(pre_name):
                    # Already on — the toggle stays UNCLICKED. Clicking here
                    # would disable the very thing the approval asked for.
                    row["status"] = "notifications_on"
                elif not _NOTIFY_OFF_RE.search(pre_name):
                    row["detail"] = (
                        f"unrecognized notification control state {pre_name[:60]!r}"
                    )
                else:
                    click = _run(["click", ref], port=port, session=session, timeout=20)
                    if not click.ok:
                        row["detail"] = f"notification click failed: {_step_detail(click)}"
                    else:
                        _run(["wait", "1500"], port=port, session=session, timeout=10)
                        post = _NOTIFY_BTN_RE.search(_snapshot_buttons(port, session))
                        if post and _NOTIFY_ON_RE.search(post.group(1)):
                            row["status"] = "notifications_on"
                        else:
                            row["detail"] = (
                                "notification bell did not reach the on state "
                                "after click"
                            )

    if row["status"] == "error":
        _audit(
            workflow_id="x.notify", command="x.notify", outcome="error",
            reason=row["detail"], handle=handle, port=port, session=session,
        )
        return row
    row["screenshot"] = _receipt_screenshot(
        port=port, session=session, handle=handle, workflow="x-notify"
    )
    _audit(
        workflow_id="x.notify", command="x.notify", outcome=row["status"],
        handle=handle, port=port, session=session,
    )
    return row


def _refused_receipt(action: str, handles: list[str], detail: str) -> dict[str, Any]:
    """A whole-batch refusal: nothing ran, every handle says why."""
    return {
        "ok": False,
        "action": action,
        "results": [
            {"handle": h, "status": "refused", "detail": detail, "screenshot": None}
            for h in list(handles or [])
        ],
    }


def _row_fully_ok(row: dict[str, Any]) -> bool:
    """True only when EVERY requested sub-action for the handle landed.

    A follow that landed with a failed notification toggle is not a full
    success (Codex R2): the operator approved both, so the row verdict — and
    the batch ``ok`` — must carry the notify outcome too.
    """
    if row.get("status") not in {"followed", "already_following", "notifications_on"}:
        return False
    if "notifications" in row and row.get("notifications") != "notifications_on":
        return False
    return True


def follow_accounts(
    handles: list[str],
    *,
    enable_notifications: bool = False,
    port: int | None = None,
    session: str | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Follow each handle on X. Returns a receipt dict; never raises.

    ``approval`` is the gate's execution-token bundle
    (``persona_id``/``action_id``/``token``/``payload``) — WITHOUT it this
    function refuses before any browser command runs. ``port``/``session``
    are None sentinels resolved at call time (Rule 1): the port pins to
    18222 (9222 is refused even explicitly), the session to the persona's
    own named CDP session.
    """
    try:
        resolved_port = _resolve_port(port)
    except (TypeError, ValueError) as exc:
        return _refused_receipt("x_follow_accounts", list(handles or []), str(exc))
    resolved_session = str(session or "").strip() or DEFAULT_SESSION

    refusal = _consume_approval(
        approval, list(handles or []), enable_notifications=enable_notifications
    )
    if refusal:
        _audit(
            workflow_id="x.follow", command="x.follow", outcome="refused",
            reason=refusal, handle="(batch)", port=resolved_port,
            session=resolved_session,
        )
        return _refused_receipt("x_follow_accounts", list(handles or []), refusal)

    results: list[dict[str, Any]] = []
    remaining = list(handles or [])
    for handle in remaining:
        # Re-checked PER HANDLE (Codex R1): an operator flipping the switch
        # mid-batch stops the rest of the batch, and the receipt says so.
        if _kill_switch_off():
            results.append(
                {
                    "handle": handle,
                    "status": "skipped",
                    "detail": "kill switch disabled mid-batch",
                    "screenshot": None,
                }
            )
            continue
        try:
            row = _follow_one(handle, port=resolved_port, session=resolved_session)
            if enable_notifications and row["status"] in {"followed", "already_following"}:
                notify = _notify_one(handle, port=resolved_port, session=resolved_session)
                row["notifications"] = notify["status"]
                if notify["status"] == "error":
                    row["notification_detail"] = notify["detail"]
        except Exception as exc:  # noqa: BLE001 — per-handle failures are data
            _logger.warning("x follow %s raised: %s", handle, exc, exc_info=True)
            row = {
                "handle": handle,
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
                "screenshot": None,
            }
            # A raised handle still owes the audit trail its row (Codex R1).
            _audit(
                workflow_id="x.follow", command="x.follow", outcome="error",
                reason=row["detail"], handle=handle, port=resolved_port,
                session=resolved_session,
            )
        results.append(row)
    ok = bool(results) and all(_row_fully_ok(row) for row in results)
    return {
        "ok": ok,
        "action": "x_follow_accounts",
        "port": resolved_port,
        "session": resolved_session,
        "results": results,
    }


def enable_notifications(
    handles: list[str],
    *,
    port: int | None = None,
    session: str | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enable the notification bell for each handle. Never raises.

    Same approval-token contract as :func:`follow_accounts`.
    """
    try:
        resolved_port = _resolve_port(port)
    except (TypeError, ValueError) as exc:
        return _refused_receipt("x_enable_notifications", list(handles or []), str(exc))
    resolved_session = str(session or "").strip() or DEFAULT_SESSION

    refusal = _consume_approval(approval, list(handles or []))
    if refusal:
        _audit(
            workflow_id="x.notify", command="x.notify", outcome="refused",
            reason=refusal, handle="(batch)", port=resolved_port,
            session=resolved_session,
        )
        return _refused_receipt("x_enable_notifications", list(handles or []), refusal)

    results: list[dict[str, Any]] = []
    for handle in list(handles or []):
        if _kill_switch_off():
            results.append(
                {
                    "handle": handle,
                    "status": "skipped",
                    "detail": "kill switch disabled mid-batch",
                    "screenshot": None,
                }
            )
            continue
        try:
            row = _notify_one(handle, port=resolved_port, session=resolved_session)
        except Exception as exc:  # noqa: BLE001 — per-handle failures are data
            _logger.warning("x notify %s raised: %s", handle, exc, exc_info=True)
            row = {
                "handle": handle,
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
                "screenshot": None,
            }
            _audit(
                workflow_id="x.notify", command="x.notify", outcome="error",
                reason=row["detail"], handle=handle, port=resolved_port,
                session=resolved_session,
            )
        results.append(row)
    ok = bool(results) and all(_row_fully_ok(row) for row in results)
    return {
        "ok": ok,
        "action": "x_enable_notifications",
        "port": resolved_port,
        "session": resolved_session,
        "results": results,
    }


__all__ = [
    "DEFAULT_SESSION",
    "enable_notifications",
    "follow_accounts",
]
