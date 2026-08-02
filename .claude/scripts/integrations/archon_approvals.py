"""Archon gate phrases — what a paused run's CURRENT gate demands as its answer.

Epic #252 / ticket #259. Architecture: ``PRD-archon-execution-spine.architecture.md``
F4 (steering rides Archon's own primitives; we invent none).

This module exists because an approve is not a boolean. Archon records the
approve comment as the gate node's captured output, and a bare approve defaults
it to the literal ``"Approved"`` — which fails every deterministic
``<gate>-check`` node that greps for ``APPROVE SPEND`` / ``APPROVE DEPLOY``
(``docs/manual/features/archon-steering-gates.md``). An approval surface that
does not carry the phrase fails on exactly the gates it exists to answer.

Two boundaries hold here:

* **Rule 2 — the phrase is read from PHYSICAL state at act time.** A gate can
  re-ask with different copy between the moment the operator was shown a pause
  and the moment they answer it, so the phrase is resolved from the ledger when
  the answer is sent, never from a rendered snapshot.
* **The gate message is hostile input.** It is workflow-authored and carries
  substituted node output, so :func:`extract_required_phrase` only asks WHICH of
  the framework's own :data:`REQUIRED_APPROVAL_PHRASES` the copy names and
  returns that constant. Nothing from the message reaches Archon, so an agent
  cannot smuggle a comment through the gate copy.

Ticket #268 (approval routing to Telegram/Discord) is parked and owns the PUSH
half — the cards, the taps, the spoken override, the watcher. These names and
semantics are its verbatim shape, so unparking it is a superset merge rather
than a rename.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from integrations import archon_events

logger = logging.getLogger(__name__)

#: The event type Archon writes when a workflow reaches an approval gate.
APPROVAL_REQUESTED: Final[str] = "approval_requested"

#: The two verbatim phrases the repo's deterministic ``<gate>-check`` nodes
#: grep for. Order is longest-first so a message naming both cannot be
#: shortened by a prefix match.
PHRASE_APPROVE_DEPLOY: Final[str] = "APPROVE DEPLOY"
PHRASE_APPROVE_SPEND: Final[str] = "APPROVE SPEND"
REQUIRED_APPROVAL_PHRASES: Final[tuple[str, ...]] = (
    PHRASE_APPROVE_DEPLOY,
    PHRASE_APPROVE_SPEND,
)


def extract_required_phrase(message: str) -> str:
    """The verbatim approval phrase this gate's copy demands, or ``""``.

    PURE, and deliberately not a parser: the message may only SELECT among the
    framework's own constants, never supply text.

    ``""`` is the honest answer for a gate with no phrase check, and it matters:
    an ``interactive_loop`` gate reads a non-empty comment as FEEDBACK and runs
    another iteration instead of finalizing
    (``feedbackProvided = comment.trim().length > 0``). Sending a phrase nobody
    asked for would silently change what approving means.
    """
    if not isinstance(message, str) or not message:
        return ""
    upper = message.upper()
    for phrase in REQUIRED_APPROVAL_PHRASES:
        if phrase in upper:
            return phrase
    return ""


def read_raw_gate_message(
    run_id: str,
    *,
    db_path: Path | str | None = None,
) -> str:
    """The newest gate message for a run, UNTRUNCATED. BLOCKING; never raises.

    The one seam that turns a capped telemetry frame back into the full gate
    copy, so the phrase can be found wherever the workflow author put it.
    Returns ``""`` on any failure — every caller degrades to the capped event
    copy or to a bare approve, both of which are pre-existing behaviour.

    ``db_path`` is a ``None`` sentinel resolved inside
    :func:`archon_events.read_gate_data_raw` at call time (Rule 1).
    """
    if not run_id:
        return ""
    try:
        data, status = archon_events.read_gate_data_raw(run_id, db_path=db_path)
    except Exception as exc:  # noqa: BLE001 — the capped copy is the fallback
        logger.warning("archon_approvals: raw gate message lookup failed (%s)", exc)
        return ""
    if status != archon_events.STATUS_OK or not data:
        return ""
    message = data.get("message")
    return message if isinstance(message, str) else ""


def read_gate_phrase(
    run_id: str,
    *,
    db_path: Path | str | None = None,
) -> str:
    """The phrase the run's CURRENT gate demands, read from the ledger.

    BLOCKING (read-only SQLite); never raises. An unreadable ledger returns
    ``""`` — a bare approve, which fails the check node LOUDLY rather than
    approving something under a phrase nobody verified was still being asked
    for.

    Reads through :func:`archon_events.read_gate_data_raw`, NOT the display
    reader. The display reader caps every value at 800 characters, and a gate
    message puts substituted run config and preflight output ahead of the
    phrase — a 904-character render already left the phrase at index 878, so
    the cap returned an empty phrase and sent a bare approve into a check node
    that demanded ``APPROVE DEPLOY``.
    """
    return extract_required_phrase(read_raw_gate_message(run_id, db_path=db_path))


__all__ = [
    "APPROVAL_REQUESTED",
    "PHRASE_APPROVE_DEPLOY",
    "PHRASE_APPROVE_SPEND",
    "REQUIRED_APPROVAL_PHRASES",
    "extract_required_phrase",
    "read_gate_phrase",
    "read_raw_gate_message",
]
