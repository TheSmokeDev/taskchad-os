"""Ghost Phone P4.1 seam — the ghost-only capability gate + the HARD INVARIANT.

These prove the seam is structurally safe: the OS-level capabilities are
unreachable for any target != 'ghost' even when every gate is ON (default-ON for
the ghost since 2026-07-07), and audited. The single most important test is
``test_hard_invariant_beats_an_enabled_gate`` — the structural invariant, not
the per-capability default, is the safety line.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import ghost_capabilities as gc  # type: ignore[import-not-found]  # noqa: E402

_ALL_CAPS = tuple(gc.GHOST_CAPABILITIES)
_NON_GHOST = ("phone", "desktop", "", "tablet", "GHOST")  # 'GHOST' != 'ghost' — exact match


def _all_on_env() -> dict[str, str]:
    """Every capability gate flipped ON — to prove the invariant beats gates."""
    return {cap.env_flag: "true" for cap in gc.GHOST_CAPABILITIES.values()}


# ── The HARD INVARIANT — ghost-only, structurally ────────────────────────────


@pytest.mark.parametrize("capability", _ALL_CAPS)
@pytest.mark.parametrize("target", _NON_GHOST)
def test_capability_unreachable_for_non_ghost_target(capability: str, target: str) -> None:
    audits: list[dict] = []
    with pytest.raises(gc.GhostCapabilityDenied, match="only available for target='ghost'"):
        gc.require_ghost_capability(
            capability, target=target, environ=_all_on_env(), audit=lambda **r: audits.append(r)
        )
    # Even with EVERY gate on, a non-ghost request is blocked on the target line.
    assert audits and audits[-1]["outcome"] == "blocked"


def test_hard_invariant_beats_an_enabled_gate() -> None:
    # THE safety line (landmine 4): a personal-phone request can never read SMS
    # even when the SMS gate is explicitly enabled. The invariant precedes the
    # gate check, so enabling a power does not open it for target=phone.
    env = {"HOMIE_GHOST_CAP_SMS_READ": "true"}
    with pytest.raises(gc.GhostCapabilityDenied, match="only available for target='ghost'"):
        gc.require_ghost_capability("ghost.sms.read", target="phone", environ=env)


def test_unknown_capability_on_non_ghost_still_blocked_on_target_line() -> None:
    # Defense in depth: the target check runs before the registry lookup, so an
    # unknown capability on a non-ghost target is refused as a target violation.
    with pytest.raises(gc.GhostCapabilityDenied, match="only available for target='ghost'"):
        gc.require_ghost_capability("ghost.camera.stream", target="desktop", environ={})


# ── DEFAULT-ON on the ghost itself (2026-07-07 operator decision) ────────────


@pytest.mark.parametrize("capability", _ALL_CAPS)
def test_ghost_capability_default_on(capability: str) -> None:
    # With no env set, every ghost power is ALLOWED for target=ghost — the
    # structural invariant (target-line) is the safety line, not the default.
    audits: list[dict] = []
    cap = gc.require_ghost_capability(
        capability, target="ghost", environ={}, audit=lambda **r: audits.append(r)
    )
    assert cap.id == capability
    assert audits[-1]["outcome"] == "allowed"


@pytest.mark.parametrize("capability", _ALL_CAPS)
def test_ghost_capability_env_false_disables(capability: str) -> None:
    # The env flag still overrides: an explicit =false kills that one power.
    cap = gc.GHOST_CAPABILITIES[capability]
    audits: list[dict] = []
    with pytest.raises(gc.GhostCapabilityDenied, match="disabled"):
        gc.require_ghost_capability(
            capability,
            target="ghost",
            environ={cap.env_flag: "false"},
            audit=lambda **r: audits.append(r),
        )
    assert audits[-1]["outcome"] == "blocked"


def test_ghost_unknown_capability_denied() -> None:
    with pytest.raises(gc.GhostCapabilityDenied, match="unknown ghost capability"):
        gc.require_ghost_capability("ghost.nope", target="ghost", environ={})


# ── The new takeover powers are registered (B1) ──────────────────────────────


def test_takeover_capabilities_registered() -> None:
    for cap_id in ("ghost.screen.view", "ghost.input.tap", "ghost.app.install"):
        assert cap_id in gc.GHOST_CAPABILITIES
    # ghost.input.tap covers tap/text/swipe/key — one write gate for all input.
    assert gc.GHOST_CAPABILITIES["ghost.input.tap"].effect == "write"
    assert gc.GHOST_CAPABILITIES["ghost.screen.view"].effect == "read"


# ── Disabling one gate does not disable the others ───────────────────────────


def test_disabling_one_gate_does_not_disable_the_others() -> None:
    env = {"HOMIE_GHOST_CAP_SMS_READ": "false"}  # only SMS killed
    assert gc.is_ghost_capability_enabled("ghost.sms.read", environ=env) is False
    assert gc.is_ghost_capability_enabled("ghost.screen.view", environ=env) is True
    # screen.view stays allowed on the ghost despite SMS being killed.
    cap = gc.require_ghost_capability("ghost.screen.view", target="ghost", environ=env)
    assert cap.id == "ghost.screen.view"
    with pytest.raises(gc.GhostCapabilityDenied, match="disabled"):
        gc.require_ghost_capability("ghost.sms.read", target="ghost", environ=env)


def test_is_ghost_capability_enabled_is_call_time() -> None:
    # Default-ON: empty env enables; explicit =false disables; unknown = False.
    assert gc.is_ghost_capability_enabled("ghost.storage.read", environ={}) is True
    assert (
        gc.is_ghost_capability_enabled(
            "ghost.storage.read", environ={"HOMIE_GHOST_CAP_STORAGE_READ": "false"}
        )
        is False
    )
    # Any non-"true" value disables (same trimming/casing rule as before).
    assert (
        gc.is_ghost_capability_enabled(
            "ghost.storage.read", environ={"HOMIE_GHOST_CAP_STORAGE_READ": "nope"}
        )
        is False
    )
    assert gc.is_ghost_capability_enabled("nonexistent", environ={}) is False


def test_every_capability_is_default_on_and_uniquely_gated() -> None:
    # Every capability ships enabled; each has its own distinct env flag.
    flags = [cap.env_flag for cap in gc.GHOST_CAPABILITIES.values()]
    assert len(flags) == len(set(flags))  # one gate per power, no shared switch
    for cap in gc.GHOST_CAPABILITIES.values():
        assert cap.default_on is True
        assert gc.is_ghost_capability_enabled(cap.id, environ={}) is True
