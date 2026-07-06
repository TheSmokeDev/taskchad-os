"""Co-founder status machine — deterministic transitions with non-enum tolerance.

Pure module (US-007): no I/O, no LLM, no clock. The pass (US-011) is the only
caller that touches disk; this module only classifies status strings and
validates transitions (the ``orchestration/contract.py`` frozen-map precedent).

Status enum (prd.md Phase 1)::

    new | building | testing | blocked | awaiting-human | done

Classification:

* active   — ``new``/``building``/``testing`` PLUS any non-enum string. An LLM
  that invents ``in_progress`` must never stall polling (reference-build
  lesson, prd.md Phase 3): unknown strings count as in-flight builds until
  code re-stamps a real enum value.
* parked   — ``blocked``/``awaiting-human``. Resumable, never terminal-archived.
* terminal — ``done`` only (the archive-to-done/ trigger).

Matching is EXACT (case-sensitive, no trimming): ``Done`` is a non-enum string
and therefore active. Code that writes status (US-011/US-012) validates
enum-only values, so a rogue casing can only arrive from an outside edit —
and staying active (keep polling; the next decision folds it back) is the
fail-safe reading.

Transition rules:

* enum -> enum is legal iff ``target in STATUS_TRANSITIONS[current]``.
* non-enum current -> enum target is always legal: the map cannot enumerate
  rogue strings, and recovery code must be able to re-stamp them back into
  the enum.
* any current -> non-enum target is always illegal: code never writes a
  non-enum status.
* ``done`` has no outbound transitions.
* ``building -> done`` (and ``new -> done``) are deliberately absent:
  completion is the executable check, so every ``done`` passes through
  ``testing`` or an ``awaiting-human`` approve.
"""

from __future__ import annotations

from typing import Literal

CofounderStatus = Literal["new", "building", "testing", "blocked", "awaiting-human", "done"]

STATUSES: tuple[str, ...] = ("new", "building", "testing", "blocked", "awaiting-human", "done")

ACTIVE_STATUSES: frozenset[str] = frozenset(["new", "building", "testing"])
PARKED_STATUSES: frozenset[str] = frozenset(["blocked", "awaiting-human"])
TERMINAL_STATUSES: frozenset[str] = frozenset(["done"])

# Legal enum -> enum transitions. Keys cover the full enum so membership in
# this map doubles as the enum check; terminal states map to an empty set.
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset(["building", "testing", "awaiting-human"]),
    "building": frozenset(["testing", "blocked", "awaiting-human"]),
    "testing": frozenset(["building", "done", "blocked", "awaiting-human"]),
    "blocked": frozenset(["building", "testing", "awaiting-human"]),
    "awaiting-human": frozenset(["new", "building", "testing", "done"]),
    "done": frozenset(),
}


class IllegalTransitionError(ValueError):
    """A status transition outside the legal map (or to a non-enum target)."""


def is_enum(status: str) -> bool:
    """True when ``status`` is one of the six canonical enum values."""
    return status in STATUS_TRANSITIONS


def is_active(status: str) -> bool:
    """True for an in-flight build: new/building/testing OR any non-enum string."""
    if status in ACTIVE_STATUSES:
        return True
    return not is_enum(status)


def is_parked(status: str) -> bool:
    """True for blocked / awaiting-human — resumable, never archived."""
    return status in PARKED_STATUSES


def is_terminal(status: str) -> bool:
    """True only for done; blocked and awaiting-human are parked, not terminal."""
    return status in TERMINAL_STATUSES


def can_transition(current: str, target: str) -> bool:
    """Whether ``current -> target`` is legal. Pure predicate; never raises."""
    if not is_enum(target):
        return False
    if not is_enum(current):
        # Recovery re-stamp: fold a rogue (active-by-tolerance) status back
        # into the enum.
        return True
    return target in STATUS_TRANSITIONS[current]


def transition(current: str, target: str) -> str:
    """Validate ``current -> target``; return ``target`` or raise.

    Raises:
        IllegalTransitionError: when the transition is not legal.
    """
    if not can_transition(current, target):
        raise IllegalTransitionError(f"illegal status transition: {current!r} -> {target!r}")
    return target
