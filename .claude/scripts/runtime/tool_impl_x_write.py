"""X write tools — the persona's hands on X, behind the dedicated action gate.

Epic #465 ticket 1a. The doctrine these tools implement: granting the
``x_social_write`` toolset lets a persona PROPOSE a write; it never executes
one. Each handler below validates its arguments, records a pending action
proposal (``personas/action_proposals.py``), and returns the approval CARD as
its tool result. The browser is never touched on this path — execution happens
later, in :func:`personas.action_proposals.decide_action`, which runs the
STORED payload through the executors registered here.

Both tools are ``dedicated_gate=True``: the registry refuses to make them
elevatable, ``request_tool`` refuses them, and the base persona bootstrap
refuses to carry them. There is exactly one road to a follow, and it has an
operator on it.

**What this does NOT defend, stated plainly** (the ``tool_impl_exec``
honesty precedent): a persona granted ``terminal`` holds a shell, and a shell
is a superset of every tool-layer gate — the token boundary between
``decide_action``, these executors, and ``x_action_driver`` defends
in-process and tool-path misuse (direct driver calls, replayed approvals,
swapped payloads), not a granted shell. That residual is a follow-up issue,
not fixed here.

Handlers are sync, return plain strings, and never raise into the dispatch
loop — same contract as ``tool_impl_eyes``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_logger = logging.getLogger(__name__)

TOOLSET = "x_social_write"

TOOL_FOLLOW = "x_follow_accounts"
TOOL_NOTIFY = "x_enable_notifications"

# X handles are 1-15 chars of [A-Za-z0-9_], optionally @-prefixed on input.
# Anything else never reaches a stored payload, let alone a browser.
_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9_]{1,15}$")
_MAX_HANDLES = 25


def _normalize_handles(handles: Any) -> tuple[list[str], str]:
    """Validate the handle list. Returns ``(clean_handles, error)``.

    Both empty: error. STRICT types (Codex R1): the dispatcher passes the
    model's JSON straight through, so validation is the only place a
    type-confused payload can be refused — ``[123]`` must not become the
    handle ``"123"``. Handles must be an actual JSON list whose elements are
    all actual strings; the only massaging afterwards is stripping a leading
    ``@``. An invalid call leaves no proposal row behind.
    """
    if not isinstance(handles, list):
        return [], "error: handles must be a JSON list of X handle strings"
    clean: list[str] = []
    for raw in handles:
        if not isinstance(raw, str):
            return [], f"error: every handle must be a string, got {type(raw).__name__}"
        handle = raw.strip()
        if not _HANDLE_RE.match(handle):
            return [], (
                f"error: {handle[:40]!r} is not an X handle "
                "(1-15 letters, digits, or underscores)"
            )
        clean.append(handle.lstrip("@"))
    if not clean:
        return [], "error: handles must name at least one account"
    if len(clean) > _MAX_HANDLES:
        return [], f"error: at most {_MAX_HANDLES} accounts per action (got {len(clean)})"
    # Dedup, order-preserved — one follow per account per action.
    return list(dict.fromkeys(clean)), ""


def _normalize_notify_flag(value: Any) -> tuple[bool, str]:
    """The notification flag must be an actual JSON boolean (Codex R1).

    ``bool("false")`` is True — coercion here would authorize the opposite
    of what the caller typed. Absent (the signature default) is False;
    anything that is not literally ``True``/``False`` is an error.
    """
    if type(value) is bool:  # noqa: E721 — exact type check is the point
        return value, ""
    return False, (
        "error: enable_notifications must be a JSON boolean "
        f"(true/false), got {type(value).__name__}"
    )


def _follow_summary(handles: list[str], enable_notifications: bool) -> str:
    # EVERY approved target is rendered. The summary is the operator's
    # authorization surface — a handle hidden behind "(+N more)" would still
    # execute on approval (#465 1a codex R3 BLOCKER). Bounded by _MAX_HANDLES
    # at validation, so full rendering costs < ~450 chars.
    shown = ", ".join(f"@{h}" for h in handles)
    suffix = " and turn on notifications" if enable_notifications else ""
    return f"Follow {len(handles)} account(s): {shown}{suffix}."


def _notify_summary(handles: list[str]) -> str:
    shown = ", ".join(f"@{h}" for h in handles)
    return f"Enable notifications for {len(handles)} account(s): {shown}."


def _propose(persona_id: str, tool_name: str, arguments: dict[str, Any], summary: str) -> str:
    """Record the proposal and return the card, or an honest error string.

    KillSwitchDisabled propagates INTO the dispatch loop, which converts it
    to an error result for the model — the gate being off is an answer, not
    a crash. Every other failure lands here as a plain string.
    """
    from personas import action_proposals  # noqa: PLC0415 — Rule 3 module attr

    proposal = action_proposals.propose_action(persona_id, tool_name, arguments, summary)
    if proposal is None:
        return (
            f"error: could not create an approval proposal for {tool_name}; "
            "nothing was recorded or executed"
        )
    return action_proposals.card_text(proposal)


def _x_follow_accounts(
    handles: Any = None,
    enable_notifications: Any = False,
    _persona_id: str = "",
    **_: Any,
) -> str:
    """Propose following accounts on X. NEVER follows anything itself."""
    persona = str(_persona_id or "").strip()
    if not persona:
        return "error: persona identity missing — refusing to propose"
    clean, error = _normalize_handles(handles)
    if error:
        return error
    notify, error = _normalize_notify_flag(enable_notifications)
    if error:
        return error
    return _propose(
        persona,
        TOOL_FOLLOW,
        {"handles": clean, "enable_notifications": notify},
        _follow_summary(clean, notify),
    )


def _x_enable_notifications(handles: Any = None, _persona_id: str = "", **_: Any) -> str:
    """Propose enabling notifications for already-followed accounts."""
    persona = str(_persona_id or "").strip()
    if not persona:
        return "error: persona identity missing — refusing to propose"
    clean, error = _normalize_handles(handles)
    if error:
        return error
    return _propose(
        persona,
        TOOL_NOTIFY,
        {"handles": clean},
        _notify_summary(clean),
    )


# ── Executors — the only code here that reaches the browser ───────────────
#
# Called by action_proposals.decide_action with the STORED payload and the
# one-use execution token minted by the winning CAS. The token, action id,
# persona id, and the untouched payload are handed to the driver as the
# ``approval`` bundle; the driver consumes the token (atomically, once)
# before any browser command runs. The driver import is lazy and module-level
# (Rule 3): tests monkeypatch x_action_driver.follow_accounts, and the
# persona runtime keeps the chat-slice import off the registration path.


def _approval(persona_id: str, action_id: str, token: str, payload: dict) -> dict[str, Any]:
    return {
        "persona_id": persona_id,
        "action_id": action_id,
        "token": token,
        "payload": payload,
    }


def _execute_follow_accounts(
    *, persona_id: str, action_id: str, execution_token: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    import x_action_driver  # noqa: PLC0415 — chat slice; Rule 3 module attr

    args = dict(arguments or {})
    return x_action_driver.follow_accounts(
        list(args.get("handles") or []),
        enable_notifications=args.get("enable_notifications", False) is True,
        approval=_approval(persona_id, action_id, execution_token, args),
    )


def _execute_enable_notifications(
    *, persona_id: str, action_id: str, execution_token: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    import x_action_driver  # noqa: PLC0415 — chat slice; Rule 3 module attr

    args = dict(arguments or {})
    return x_action_driver.enable_notifications(
        list(args.get("handles") or []),
        approval=_approval(persona_id, action_id, execution_token, args),
    )


_SPECS: tuple[tuple[str, str, dict[str, Any], Any, Any], ...] = (
    (
        TOOL_FOLLOW,
        "Follow one or more X accounts (optionally enabling their notifications). "
        "This is a WRITE: calling it creates an operator-approval proposal and "
        "returns an approval card — nothing happens until the operator approves "
        "the exact proposal with /act approve.",
        {
            "type": "object",
            "properties": {
                "handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "X handles to follow, with or without the @ (max 25).",
                },
                "enable_notifications": {
                    "type": "boolean",
                    "description": "Also turn on the notification bell for each account.",
                    "default": False,
                },
            },
            "required": ["handles"],
        },
        _x_follow_accounts,
        _execute_follow_accounts,
    ),
    (
        TOOL_NOTIFY,
        "Enable notifications for one or more already-followed X accounts. "
        "This is a WRITE: calling it creates an operator-approval proposal and "
        "returns an approval card — nothing happens until the operator approves "
        "the exact proposal with /act approve.",
        {
            "type": "object",
            "properties": {
                "handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "X handles to enable notifications for (max 25).",
                },
            },
            "required": ["handles"],
        },
        _x_enable_notifications,
        _execute_enable_notifications,
    ),
)


def register_tools() -> int:
    """Register the X write tools and their executors. Never raises."""
    from personas import action_proposals  # noqa: PLC0415 — cycle-safe
    from runtime import tool_registry

    registered = 0
    for name, description, parameters, handler, executor in _SPECS:
        try:
            tool_registry.register_tool(
                name,
                description,
                toolset=TOOLSET,
                parameters=parameters,
                handler=handler,
                effect="write",
                # The handler needs the CALLING persona (the proposal is filed
                # in that persona's own store) — never ambient profile state.
                persona_scoped=True,
                # The whole point: never one-time elevatable, never on the
                # base bootstrap. The action gate is the only road.
                dedicated_gate=True,
            )
            # The tool and its executor are one unit: a tool without an
            # executor is a loud, audited refusal at decide time.
            action_proposals.register_action_executor(name, executor)
            registered += 1
        except Exception:  # noqa: BLE001 — one dead tool must not deny the other
            _logger.warning("failed to register X write tool %r", name, exc_info=True)
    return registered


__all__ = [
    "TOOLSET",
    "TOOL_FOLLOW",
    "TOOL_NOTIFY",
    "register_tools",
]
