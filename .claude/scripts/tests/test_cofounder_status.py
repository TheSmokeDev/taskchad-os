"""US-007 — co-founder status machine contract tests.

Asserts:
  - every legal transition in STATUS_TRANSITIONS is accepted
  - every enum -> enum pair OUTSIDE the map (self-loops included) is rejected
  - non-enum current status is tolerated as active (polling never stalls) and
    may recover to any enum value; a non-enum TARGET is always rejected
  - terminal/parked classification: done is the only terminal; blocked and
    awaiting-human are parked (resumable), and every enum status falls into
    exactly one of active/parked/terminal
  - the executable-completion invariant: building -> done and new -> done are
    illegal (done only through testing or an awaiting-human approve)
  - purity: the module does no I/O and imports nothing beyond the stdlib
    typing machinery (US-011 is the only caller that touches disk)
"""

from __future__ import annotations

import inspect

import pytest

from cofounder import status
from cofounder.status import (
    ACTIVE_STATUSES,
    PARKED_STATUSES,
    STATUS_TRANSITIONS,
    STATUSES,
    TERMINAL_STATUSES,
    IllegalTransitionError,
    can_transition,
    is_active,
    is_enum,
    is_parked,
    is_terminal,
    transition,
)

LEGAL_PAIRS = [
    (current, target)
    for current, targets in STATUS_TRANSITIONS.items()
    for target in sorted(targets)
]

ILLEGAL_ENUM_PAIRS = [
    (current, target)
    for current in STATUSES
    for target in STATUSES
    if target not in STATUS_TRANSITIONS[current]
]

# Rogue strings an LLM (or an outside edit) could leave in frontmatter.
# Matching is exact, so casing/whitespace variants are non-enum too.
ROGUE_STATUSES = ["in_progress", "Done", "DONE", " done", "building ", "queued", ""]


# ── Map integrity ─────────────────────────────────────────────────────────


def test_transition_map_covers_exactly_the_enum():
    assert set(STATUS_TRANSITIONS) == set(STATUSES)
    assert len(STATUSES) == 6


def test_transition_map_targets_are_all_enum_members():
    for current, targets in STATUS_TRANSITIONS.items():
        assert targets <= set(STATUSES), f"{current} maps outside the enum"


def test_classification_sets_partition_the_enum():
    assert ACTIVE_STATUSES | PARKED_STATUSES | TERMINAL_STATUSES == set(STATUSES)
    assert not ACTIVE_STATUSES & PARKED_STATUSES
    assert not ACTIVE_STATUSES & TERMINAL_STATUSES
    assert not PARKED_STATUSES & TERMINAL_STATUSES


# ── Legal transitions ─────────────────────────────────────────────────────


@pytest.mark.parametrize(("current", "target"), LEGAL_PAIRS)
def test_every_legal_transition_accepted(current, target):
    assert can_transition(current, target) is True
    assert transition(current, target) == target


# ── Illegal transitions ───────────────────────────────────────────────────


@pytest.mark.parametrize(("current", "target"), ILLEGAL_ENUM_PAIRS)
def test_every_illegal_enum_pair_rejected(current, target):
    assert can_transition(current, target) is False
    with pytest.raises(IllegalTransitionError):
        transition(current, target)


@pytest.mark.parametrize("current", STATUSES)
def test_self_transition_is_illegal(current):
    assert can_transition(current, current) is False


def test_done_has_no_outbound_transitions():
    assert STATUS_TRANSITIONS["done"] == frozenset()
    for target in [*STATUSES, "in_progress"]:
        assert can_transition("done", target) is False


@pytest.mark.parametrize("current", ["new", "building"])
def test_done_only_reachable_through_testing_or_approve(current):
    """Executable-completion invariant: no build flips straight to done."""
    assert can_transition(current, "done") is False


def test_illegal_transition_error_is_valueerror_and_names_both_statuses():
    with pytest.raises(IllegalTransitionError) as excinfo:
        transition("building", "done")
    assert isinstance(excinfo.value, ValueError)
    assert "building" in str(excinfo.value)
    assert "done" in str(excinfo.value)


# ── Non-enum tolerance ────────────────────────────────────────────────────


@pytest.mark.parametrize("rogue", ROGUE_STATUSES)
def test_non_enum_status_is_tolerated_as_active(rogue):
    assert is_enum(rogue) is False
    assert is_active(rogue) is True
    assert is_parked(rogue) is False
    assert is_terminal(rogue) is False


@pytest.mark.parametrize("target", STATUSES)
def test_non_enum_current_recovers_to_any_enum_target(target):
    assert can_transition("in_progress", target) is True
    assert transition("in_progress", target) == target


@pytest.mark.parametrize("current", [*STATUSES, "in_progress"])
def test_non_enum_target_always_rejected(current):
    assert can_transition(current, "in_progress") is False
    with pytest.raises(IllegalTransitionError):
        transition(current, "in_progress")


# ── Classification ────────────────────────────────────────────────────────


def test_is_active_classification():
    for active in ("new", "building", "testing"):
        assert is_active(active) is True
    for inactive in ("blocked", "awaiting-human", "done"):
        assert is_active(inactive) is False


def test_is_parked_classification():
    assert is_parked("blocked") is True
    assert is_parked("awaiting-human") is True
    for other in ("new", "building", "testing", "done"):
        assert is_parked(other) is False


def test_is_terminal_only_done():
    assert is_terminal("done") is True
    for other in ("new", "building", "testing", "blocked", "awaiting-human"):
        assert is_terminal(other) is False


# ── Purity ────────────────────────────────────────────────────────────────


def test_module_is_pure_no_io_no_llm():
    """US-007 AC: pure functions, no I/O, no LLM (US-011 owns all I/O)."""
    src = inspect.getsource(status)
    forbidden = (
        "import os",
        "import io",
        "import subprocess",
        "import sqlite3",
        "import requests",
        "import httpx",
        "import yaml",
        "from pathlib",
        "import pathlib",
        "open(",
        "import config",
        "from config",
        "run_with_fallback",
        "import shared",
        "from shared",
    )
    for token in forbidden:
        assert token not in src, f"status.py must stay pure; found {token!r}"
