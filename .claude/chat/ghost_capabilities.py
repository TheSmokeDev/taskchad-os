"""Ghost Phone P4.1 seam — the ghost-only OS-capability registry.

P4.1 is the operator's real end-goal: because the ghost is a DEDICATED device he
owns (his accounts logged in once), the agent may see ITS screen, tap/type on
ITS surface, launch + install ITS apps, read ITS SMS / storage / notifications —
the OS-level reach P3.0 deliberately walled off on the operator's PERSONAL phone
is SAFE here. The takeover powers (screen.view / input.tap / app.*) are wired
into ``ghost_device.py`` + the ``/api/ghost-viewer/*`` dashboard surface.

This module DECLARES + GATES + AUDITS those powers. Three enforced rules:

  1. HARD INVARIANT — these capabilities are STRUCTURALLY unreachable for any
     target != "ghost". A phone/desktop request can NEVER reach a screen/tap/
     app/SMS/storage/notif call. Enforced FIRST, before any gate check, and it
     beats the gate: an ENABLED capability still does not open it for the
     personal phone. The operator's personal S24 stays Chrome-only forever.
     THIS is the safety line, not the per-capability default.
  2. GATE — every capability has a per-power env kill-switch. Operator decision
     (2026-07-07): because the structural invariant (rule 1) already protects
     the outside world and the personal device, the ghost's own powers ship
     DEFAULT-ON. The env flag still overrides BOTH ways: HOMIE_GHOST_CAP_X=false
     disables a power, and any non-"true" value disables it. Upstream of this
     seam, the master switch (HOMIE_GHOST_ENABLED) + kill-switch
     (HOMIE_KILLSWITCH_GHOST) gate the ghost surface at every entry point — boot
     (`/ghost up`, CLI), `test-app`, and each `/api/ghost-viewer/*` route (via
     `_resolve_browser_target`) — so the operator's emergency brake stops the
     takeover powers on an already-booted ghost, not just boot.
  3. AUDIT — every attempt (blocked or allowed) writes an audit row.

Chat / dashboard surface: "see the ghost's screen" -> ghost.screen.view;
"tap here on the ghost" -> ghost.input.tap; "open <app> on the ghost" ->
ghost.app.launch; "install <apk> on the ghost" -> ghost.app.install; each
routes through require_ghost_capability(..., target="ghost").
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

GHOST_TARGET = "ghost"


@dataclass(frozen=True)
class GhostCapability:
    id: str
    env_flag: str  # per-power operator kill-switch env var
    description: str
    effect: str  # "read" | "write"
    default_on: bool = True  # ships ON for the ghost (operator decision 2026-07-07)


# The P4.1 capability registry. Each power has a UNIQUE env kill-switch and
# ships DEFAULT-ON for the ghost; the structural ghost-only invariant (rule 1
# in the module docstring), not the default, is the safety line.
GHOST_CAPABILITIES: dict[str, GhostCapability] = {
    "ghost.screen.view": GhostCapability(
        "ghost.screen.view",
        "HOMIE_GHOST_CAP_SCREEN_VIEW",
        "capture the ghost's live screen (adb exec-out screencap -p)",
        "read",
    ),
    "ghost.input.tap": GhostCapability(
        "ghost.input.tap",
        "HOMIE_GHOST_CAP_INPUT_TAP",
        "tap / type / swipe / keyevent on the ghost (adb shell input)",
        "write",
    ),
    "ghost.app.launch": GhostCapability(
        "ghost.app.launch",
        "HOMIE_GHOST_CAP_APP_LAUNCH",
        "launch an app on the ghost (adb am start / monkey)",
        "write",
    ),
    "ghost.app.install": GhostCapability(
        "ghost.app.install",
        "HOMIE_GHOST_CAP_APP_INSTALL",
        "install an APK on the ghost (adb install)",
        "write",
    ),
    "ghost.sms.read": GhostCapability(
        "ghost.sms.read",
        "HOMIE_GHOST_CAP_SMS_READ",
        "read the ghost's SMS inbox (adb content query content://sms)",
        "read",
    ),
    "ghost.storage.read": GhostCapability(
        "ghost.storage.read",
        "HOMIE_GHOST_CAP_STORAGE_READ",
        "read the ghost's storage (adb ls / pull)",
        "read",
    ),
    "ghost.notif.read": GhostCapability(
        "ghost.notif.read",
        "HOMIE_GHOST_CAP_NOTIF_READ",
        "read the ghost's notifications (adb dumpsys notification)",
        "read",
    ),
}


class GhostCapabilityDenied(RuntimeError):
    """Raised when a ghost OS-capability is refused (wrong target / gate off /
    unknown). Deterministic message; safe to surface to the operator."""


def is_ghost_capability_enabled(
    capability: str, *, environ: dict[str, str] | None = None
) -> bool:
    """True iff the capability exists AND its per-power env kill-switch is not
    turned off (call-time, Rule 1).

    The default is the capability's ``default_on`` (all ship ON for the ghost).
    The env flag overrides BOTH ways: only the literal "true" (case-insensitive,
    trimmed) enables; any other value — including "false" — disables. So an
    operator can kill exactly one power with ``HOMIE_GHOST_CAP_X=false``.
    """

    cap = GHOST_CAPABILITIES.get(capability)
    if cap is None:
        return False
    env = environ if environ is not None else os.environ
    default = "true" if cap.default_on else "false"
    return (env.get(cap.env_flag, default) or default).strip().lower() == "true"


def _default_audit(**row: Any) -> None:
    """Best-effort audit row via the browser audit trail (fail-open)."""

    try:
        from browser_audit import append_browser_audit_record  # noqa: PLC0415

        append_browser_audit_record(
            command=row.get("command", "ghost.capability"),
            workflow_id=row.get("capability"),
            action=row.get("capability"),
            outcome=row.get("outcome", "blocked"),
            reason=row.get("reason", ""),
            surface="ghost",
            # #100 structured column: the resolved (or rejected) target.
            target=row.get("target"),
        )
    except Exception:
        pass


def require_ghost_capability(
    capability: str,
    *,
    target: str,
    environ: dict[str, str] | None = None,
    caller: str = "unspecified",
    audit: Callable[..., None] | None = None,
) -> GhostCapability:
    """Gate a ghost OS-capability. Returns its metadata or raises
    ``GhostCapabilityDenied``. Every call — allowed or denied — writes an audit row.

    The HARD INVARIANT is enforced FIRST: a target other than ``ghost`` can never
    reach any of these powers, so the operator's personal phone / desktop is
    structurally excluded from screen/tap/app/SMS/storage/notif reach — even if a
    capability's gate is enabled.
    """

    emit = audit if audit is not None else _default_audit

    # 1. HARD INVARIANT — structurally ghost-only. Checked BEFORE the registry
    #    lookup and BEFORE the gate, so it beats an enabled gate and an unknown
    #    capability on a non-ghost target is still refused on the target line.
    if target != GHOST_TARGET:
        reason = (
            f"ghost OS-capability {capability!r} is only available for "
            f"target={GHOST_TARGET!r}, not {target!r}"
        )
        emit(
            capability=capability,
            target=target,
            outcome="blocked",
            reason=reason,
            caller=caller,
            command=f"{capability} (target={target})",
        )
        raise GhostCapabilityDenied(reason)

    cap = GHOST_CAPABILITIES.get(capability)
    if cap is None:
        reason = f"unknown ghost capability {capability!r}"
        emit(
            capability=capability,
            target=target,
            outcome="blocked",
            reason=reason,
            caller=caller,
            command=capability,
        )
        raise GhostCapabilityDenied(reason)

    # 2. GATE — each power ships ON but can be killed by its own env switch.
    if not is_ghost_capability_enabled(capability, environ=environ):
        reason = f"ghost capability {cap.id!r} is disabled — set {cap.env_flag}=true to enable it"
        emit(
            capability=cap.id,
            target=target,
            outcome="blocked",
            reason=reason,
            caller=caller,
            command=cap.id,
        )
        raise GhostCapabilityDenied(reason)

    # 3. Allowed to PROCEED.
    emit(
        capability=cap.id,
        target=target,
        outcome="allowed",
        reason="gate open",
        caller=caller,
        command=cap.id,
    )
    return cap
