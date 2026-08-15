"""Pending-grant proposals — the counter-offer half of self-provisioning.

Issue #428 (epic #419). A persona that hits a missing capability should
counter-offer instead of dead-ending: it names the toolset it lacks, the
operator taps approve, the grant lands. This module owns the PROPOSAL — the
short-lived row between those two moments — and nothing else.

**The invariant this module exists to hold.** A persona can only ever
PROPOSE. There is no code path from model output to a config mutation:
:func:`propose_grant` writes a row in a sqlite store and returns; only
:func:`decide_proposal`, reached from an authenticated operator action and
gated on the admin role, calls ``personas.services.add_persona_toolset`` —
which is itself the single mutation path (#426) and re-checks the role a
second time. The LLM's contribution to the whole flow is one NAME, checked
against the live registry before it is stored.

**Everything the model touches is hostile input.** The marker it emits is
matched by a bounded regex, its payload is length- and charset-checked before
the registry is consulted, and the persona the proposal is FOR never comes
from the reply — it comes from the channel binding the caller already
resolved. A persona cannot propose a grant for a different persona, cannot
invent a toolset, and cannot smuggle a newline into the ledger.

**Storage grain matches authorization grain (Rule 4).** The store, like the
#426 ledger, lives in the TARGET persona's own ``<profile>/data/`` — resolved
through ``get_persona_paths(persona_id)``, never through the ambient
``config.DATA_DIR``, which is computed once at import from whichever profile
the PROCESS runs as. Persona bots are separate processes with their own
``HOMIE_HOME``, so keying off the ambient constant would file a proposal made
inside the sales bot where the approving process cannot find it.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import toolset_grants as _grants

_logger = logging.getLogger(__name__)

# Operator OFF control for the counter-offer surface specifically. Separate
# from ``persona_mutation`` (which the executor honors) so an operator can
# silence proposals without also freezing dashboard persona writes. Ships ON:
# an unset env var is enabled, and the switch can only ever turn it off.
KILL_SWITCH_NAME = "persona_grant_proposals"

STORE_FILENAME = "persona_grant_proposals.db"

# Button ids: ``pgrant:<action>:<persona>:<code>``. The persona rides the id
# because the store is keyed to it — the approving process must know WHICH
# persona's store to open before it can look the code up. Persona ids are
# ``[a-z0-9][a-z0-9_-]{0,63}`` and codes are ``[A-Z0-9]{6}``, so neither can
# contain the separator and a plain split is unambiguous.
CUSTOM_ID_PREFIX = "pgrant"
ACTION_APPROVE = "approve"
ACTION_DENY = "deny"

# The one surface that cannot run the command its own card prints. Cabinet's
# in-room parser recognizes help/all/add/remove/pin/unpin/voice/end and nothing
# else, so a `/grant approve …` typed in the room used to fall through as
# ordinary meeting text — the persona could then answer as if it had worked
# while the proposal quietly expired. The card is worded for this surface
# (below) and the room itself now answers `/grant` server-side instead of
# handing it to the model (`cabinet/room_commands.py`).
SURFACE_CABINET = "cabinet"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"

# Decision outcomes a chat surface branches on.
DECISION_UNKNOWN = "unknown"
DECISION_REFUSED = "refused"
DECISION_EXPIRED = "expired"
DECISION_ALREADY_DECIDED = "already_decided"
DECISION_DENIED = "denied"
DECISION_GRANTED = "granted"
DECISION_FAILED = "failed"
# The chat seam itself failed before ``decide_proposal`` could answer (nothing
# was decided and nothing mutated). Named because it lands verbatim in a
# transcript receipt, and an outcome an operator can read must be greppable.
DECISION_ERROR = "error"
# A refusal happened (nothing mutated) but the REQUIRED audit row for it
# could not be written. Distinct from DECISION_REFUSED on purpose — that
# outcome is read by callers as "recorded"; this one says the opposite, so a
# caller can never mistake an unaudited refusal for an audited one (#428
# round-2 fix, mirrors toolset_grants.ToolsetGrantAuditError).
DECISION_AUDIT_FAILED = "audit_failed"

REASON_UNKNOWN_PROPOSAL = "unknown_proposal"
REASON_PROPOSAL_EXPIRED = "proposal_expired"
REASON_ALREADY_DECIDED = "already_decided"
REASON_INVALID_MARKER = "invalid_marker"
REASON_GRANT_FAILED = "grant_failed"

# The persona's counter-offer marker. Deliberately NOT ``[[...]]`` — that is
# Obsidian wikilink syntax and vault text flows through these replies. Bounded
# payload (a name, not a paragraph) and no newline class, so one marker can
# never span lines or consume a whole reply.
_MARKER_RE = re.compile(r"<<\s*GRANT_REQUEST\s*:\s*([^>\n]{0,80}?)\s*>>", re.IGNORECASE)

# A toolset name is a registry key, not free text. Checked BEFORE the registry
# lookup so a hostile payload never reaches string-distance matching.
_TOOLSET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CODE_RE = re.compile(r"^[A-Z0-9]{6}$")

# Long enough for an operator to come back to a card after a meeting, short
# enough that a stale counter-offer cannot be approved days later against a
# request nobody remembers. Un-actioned proposals expire quietly and audited.
_PROPOSAL_TTL_S = 1800
_TTL_ENV = "HOMIE_GRANT_PROPOSAL_TTL_SECONDS"
_TTL_MIN_S = 60
_TTL_MAX_S = 86_400

# Bound on how much of the persona's own reply is kept beside the proposal.
# It is context for the card, never the authorization — the operator's turn is.
_MAX_RATIONALE_CHARS = 300

_MAX_PENDING_LISTED = 20


@dataclass(frozen=True)
class GrantProposal:
    """One persona's un-actioned ask. Nothing here has changed any config."""

    proposal_id: str
    short_code: str
    persona_id: str
    toolset: str
    status: str
    created_at: float
    expires_at: float
    surface: str
    channel_id: str
    thread_id: str
    requested_by: str
    trigger_text: str
    rationale: str
    decided_by: str = ""
    decided_at: float | None = None
    status_detail: str = ""


@dataclass(frozen=True)
class ProposalDecision:
    """What an approve/deny action did, in terms a chat surface can speak."""

    outcome: str
    proposal: GrantProposal | None
    message: str
    result: Any = None


@dataclass(frozen=True)
class CounterOffer:
    """A persona reply with its marker removed and its card attached.

    ``approve_custom_id`` / ``deny_custom_id`` are empty when there is nothing
    to approve (an unknown toolset still earns an honest reply, but never a
    button that would grant a name the registry does not have).
    """

    reply_text: str
    card_text: str
    approve_custom_id: str = ""
    deny_custom_id: str = ""
    proposal: GrantProposal | None = None


# ── Resolution helpers (Rule 1 — None sentinels, resolved at call time) ──


def resolve_store_path(
    persona_id: str,
    db_path: Path | str | None = None,
) -> Path:
    """Resolve the proposal store for *persona_id* at call time.

    Same invariant, and the same hard-won reason, as
    ``toolset_grants.resolve_ledger_path``: the file is keyed to the persona
    the rows are ABOUT, not to the profile this process happens to run as.
    An explicit *db_path* always wins (tests inject one); a named persona
    resolves through ``get_persona_paths``; only with neither does this fall
    back to the ambient ``config.DATA_DIR``.

    Both imports are lazy: ``config`` imports ``personas`` at module load, so
    a top-level import here would close a cycle, and both resolvers are
    monkeypatched by tests — which only propagates when read at call time.
    """
    if db_path is not None:
        return Path(db_path)
    persona = str(persona_id or "").strip()
    if persona:
        from personas.core import get_persona_paths  # noqa: PLC0415 — cycle-safe

        return Path(get_persona_paths(persona)["data"]) / STORE_FILENAME
    import config  # noqa: PLC0415 — cycle-safe + test-monkeypatched

    return Path(config.DATA_DIR) / STORE_FILENAME


def proposal_ttl_seconds() -> int:
    """TTL for an un-actioned proposal, read from the environment per call.

    Rule 1: no module-load snapshot and no default-arg bind, so an operator
    (or a test) changing the knob takes effect on the next proposal rather
    than on the next restart. Out-of-range and unparseable values clamp
    instead of raising — a bad env var must not disable counter-offers.
    """
    raw = os.getenv(_TTL_ENV, "").strip()
    try:
        value = int(raw) if raw else _PROPOSAL_TTL_S
    except ValueError:
        value = _PROPOSAL_TTL_S
    return max(_TTL_MIN_S, min(_TTL_MAX_S, value))


def _connect(persona_id: str, db_path: Path | str | None) -> sqlite3.Connection:
    path = resolve_store_path(persona_id, db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS persona_grant_proposals (
            proposal_id TEXT PRIMARY KEY,
            short_code TEXT NOT NULL UNIQUE,
            persona_id TEXT NOT NULL,
            toolset TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            surface TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            trigger_text TEXT NOT NULL,
            rationale TEXT NOT NULL,
            decided_by TEXT NOT NULL DEFAULT '',
            decided_at REAL,
            status_detail TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_grant_proposals_status "
        "ON persona_grant_proposals(persona_id, status, expires_at)"
    )
    conn.commit()
    return conn


def _row_to_proposal(row: sqlite3.Row | None) -> GrantProposal | None:
    if row is None:
        return None
    return GrantProposal(
        proposal_id=str(row["proposal_id"]),
        short_code=str(row["short_code"]),
        persona_id=str(row["persona_id"]),
        toolset=str(row["toolset"]),
        status=str(row["status"]),
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        surface=str(row["surface"]),
        channel_id=str(row["channel_id"]),
        thread_id=str(row["thread_id"]),
        requested_by=str(row["requested_by"]),
        trigger_text=str(row["trigger_text"]),
        rationale=str(row["rationale"]),
        decided_by=str(row["decided_by"] or ""),
        decided_at=(float(row["decided_at"]) if row["decided_at"] is not None else None),
        status_detail=str(row["status_detail"] or ""),
    )


def _audit(
    outcome: str,
    *,
    persona_id: str,
    toolset: str,
    reason: str = "",
    actor: str = "",
    actor_role: str = "",
    surface: str = "",
    channel_id: str = "",
    trigger_text: str = "",
    suggestions: tuple[str, ...] = (),
    error: str = "",
    correlation_id: str = "",
    audit_path: Path | str | None = None,
) -> str:
    """Append one ``propose``-operation row to the shared grant ledger.

    Best-effort by the same rule the executor uses for rows whose branch
    already surfaces its own outcome: a proposal is a record of an ASK, and
    losing that record must not cost the operator the counter-offer. The
    mutation the approval eventually causes is audited strictly by the
    executor, where the stake is real.
    """
    return _grants.audit_attempt(
        operation=_grants.OPERATION_PROPOSE,
        persona_id=persona_id,
        toolset=toolset,
        outcome=outcome,
        reason=reason,
        actor=actor,
        actor_role=actor_role,
        surface=surface,
        channel_id=channel_id,
        trigger_text=trigger_text,
        suggestions=suggestions,
        error=error,
        correlation_id=correlation_id,
        audit_path=audit_path,
    )


def _audit_strict(
    outcome: str,
    *,
    persona_id: str,
    toolset: str,
    reason: str = "",
    actor: str = "",
    actor_role: str = "",
    surface: str = "",
    channel_id: str = "",
    trigger_text: str = "",
    suggestions: tuple[str, ...] = (),
    error: str = "",
    correlation_id: str = "",
    audit_path: Path | str | None = None,
) -> str:
    """Append one REQUIRED propose-lifecycle row. Raises on an IO failure.

    Mirrors ``personas.services``'s own ``_audit_strict``/``_refuse`` split
    (#426): a refusal or an expiry is an outcome the caller reads back as
    "recorded", so it goes through :func:`toolset_grants.append_audit_record`
    directly rather than the best-effort :func:`_audit` wrapper. Every other
    outcome here (proposed, denied, approved) still rides ``_audit`` — those
    are records of an ASK or of proposal-side bookkeeping, and an approval's
    real mutation is already audited strictly downstream by
    ``personas.services.add_persona_toolset``.
    """
    return _grants.append_audit_record(
        operation=_grants.OPERATION_PROPOSE,
        persona_id=persona_id,
        toolset=toolset,
        outcome=outcome,
        reason=reason,
        actor=actor,
        actor_role=actor_role,
        surface=surface,
        channel_id=channel_id,
        trigger_text=trigger_text,
        suggestions=suggestions,
        error=error,
        correlation_id=correlation_id,
        audit_path=audit_path,
    )


def _mark_audit_unrecorded(
    persona_id: str,
    proposal_id: str,
    *,
    db_path: Path | str | None,
) -> None:
    """Best-effort receipt when a REQUIRED audit row could not be written.

    The proposals table already committed the state change (expiry must stay
    atomic — an un-audited row must never remain approvable). The external
    ledger may be unwritable, but this table is guaranteed present, so a
    marker in ``status_detail`` is durable, physical DB state (Rule 2), not a
    sidecar: an operator can find every unaudited expiry with one query
    against the store itself even while the ledger stays broken.
    """
    try:
        conn = _connect(persona_id, db_path)
        try:
            conn.execute(
                "UPDATE persona_grant_proposals SET status_detail = "
                "status_detail || ' (audit unrecorded)' WHERE proposal_id = ?",
                (proposal_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — caller already logged at ERROR
        _logger.error(
            "grant proposals: could not mark %s as audit-unrecorded: %s: %s",
            proposal_id,
            type(exc).__name__,
            exc,
        )


# ── Marker parsing (hostile input) ───────────────────────────────────────


def parse_grant_marker(reply_text: Any) -> tuple[str, str]:
    """Split a persona reply into ``(text without markers, first name asked)``.

    EVERY marker is stripped, not just the one acted on: a reply carrying
    three of them proposes once and leaks none of the syntax to the operator.
    The returned name is raw model output — it is charset-checked and
    registry-checked by :func:`propose_grant` before it means anything.

    Never raises. A non-string, an empty reply, or a marker with an empty
    payload all come back as ``(text, "")``.
    """
    text = str(reply_text or "")
    if not text:
        return "", ""
    match = _MARKER_RE.search(text)
    if match is None:
        return text, ""
    cleaned = _MARKER_RE.sub("", text)
    # Removing an own-line marker leaves a blank-line run behind; collapse it
    # so the operator sees a normal reply rather than a hole where it was.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, match.group(1).strip()


def normalize_toolset_name(name: Any) -> str:
    """Canonicalize a model-named toolset against the LIVE registry.

    Charset-checked first so a hostile payload never reaches string
    comparison, then matched case-insensitively so ``Research_Read`` resolves
    to the real key rather than being refused on capitalization. Returns
    ``""`` when nothing in the registry matches — the caller turns that into
    an honest miss, never a guess.
    """
    wanted = str(name or "").strip()
    if not wanted or not _TOOLSET_NAME_RE.match(wanted):
        return ""
    known = _grants.known_toolset_names()
    if wanted in known:
        return wanted
    folded = wanted.casefold()
    for candidate in known:
        if candidate.casefold() == folded:
            return candidate
    return ""


# ── Custom ids ───────────────────────────────────────────────────────────


def approve_custom_id(proposal: GrantProposal) -> str:
    return f"{CUSTOM_ID_PREFIX}:{ACTION_APPROVE}:{proposal.persona_id}:{proposal.short_code}"


def deny_custom_id(proposal: GrantProposal) -> str:
    return f"{CUSTOM_ID_PREFIX}:{ACTION_DENY}:{proposal.persona_id}:{proposal.short_code}"


def parse_custom_id(custom_id: Any) -> tuple[str, str, str] | None:
    """``(action, persona_id, short_code)`` from a button id, or ``None``.

    Every component is validated — the action against the two verbs, the
    persona through the real ``validate_persona_name`` (so this cannot drift
    from the resolver that builds the profile path), and the code against its
    fixed shape. A malformed id is ``None``, never a partially-trusted tuple.
    """
    parts = str(custom_id or "").split(":")
    if len(parts) != 4 or parts[0] != CUSTOM_ID_PREFIX:
        return None
    _, action, persona, code = parts
    if action not in {ACTION_APPROVE, ACTION_DENY}:
        return None
    try:
        from personas.core import validate_persona_name  # noqa: PLC0415 — cycle-safe

        validate_persona_name(persona)
    except Exception:  # noqa: BLE001 — an invalid name is simply not routable
        return None
    if not _CODE_RE.match(code):
        return None
    return action, persona, code


# ── Prompt guidance ──────────────────────────────────────────────────────


def counter_offer_briefing() -> str:
    """The persona-runtime line that teaches the counter-offer.

    Prompt guidance, not machinery — per the architecture, the persona learns
    this from its runtime context. The registered names are read LIVE so a
    newly registered toolset is nameable the same day, and so the model is
    choosing from an enumerable list rather than inventing a capability.

    Two distinct triggers, both issue #428 scope, both the SAME marker: a
    task blocked mid-work by a missing capability, and the operator directly
    asking for one ("add X to your kit") with no blocked task in sight. A
    round-2 gate finding confirmed the first trigger's guidance did not
    generalize to the second — the model could truthfully answer a direct
    ask ("sure" / "I can't do that") without ever emitting the marker, so
    `tee_up_from_reply()` saw nothing and no proposal existed. Both are named
    explicitly below so neither reads as the only case.
    """
    names = _grants.known_toolset_names()
    catalog = ", ".join(names) if names else "(registry unavailable — do not guess)"
    return (
        "# Missing Capability — Counter-Offer\n"
        "Two situations end your reply with the same one-line marker:\n"
        "1. A task is blocked because a whole capability is missing from your "
        "kit. Do not dead-end and do not pretend — say plainly what you cannot "
        "do.\n"
        "2. The operator directly asks you to add a capability — e.g. \"add "
        "research_read to your kit\", \"can you get X toolset\" — even with no "
        "blocked task. Acknowledging or declining without the marker is "
        "wrong; that request IS the trigger.\n"
        "In either case end the reply with exactly one line:\n"
        "`<<GRANT_REQUEST: toolset_name>>`\n"
        f"Registered toolsets: {catalog}.\n"
        "Name one of those exactly. That line is stripped before the operator sees "
        "it and only tees up a proposal they approve with one tap — you can never "
        "grant yourself anything, proposing is not permission to act, and every "
        "per-tool gate (sends, spends, browser and social writes) still applies "
        "afterwards. If a single tool call is what you need instead, use "
        "`request_tool`."
    )


def card_text(proposal: GrantProposal) -> str:
    """The operator-facing counter-offer card.

    Surface-aware in exactly one place: the decide line. Every surface gets
    the same exact commands (an adapter without inline buttons is a
    first-class approve surface), but a Cabinet room CANNOT run them, so
    printing them there as if it could was an instruction to a dead end —
    the operator pastes it back into the room, the room hands it to the LLM,
    and the proposal expires while a persona answers as though it landed.
    The Cabinet wording names where the decision actually happens.

    Nothing here is attacker-controlled: the registry description, the
    validated toolset, the validated persona, the code, the TTL, and a
    server-owned surface literal. The stored ``rationale`` (the tail of the
    model's own reply) is never rendered.
    """
    description = _grants.describe_toolset(proposal.toolset)
    minutes = max(1, int(round((proposal.expires_at - proposal.created_at) / 60)))
    lines = [
        f"**Counter-offer `{proposal.short_code}`** — `{proposal.persona_id}` is "
        f"missing the `{proposal.toolset}` toolset.",
    ]
    if description:
        lines.append(f"What it adds: {description}")
    lines.append(
        "Approving expands what it can REACH — live on its next turn. Every "
        "per-tool gate (sends, spends, browser and social writes) still applies."
    )
    approve_cmd = f"`/grant approve {proposal.persona_id} {proposal.short_code}`"
    deny_cmd = f"`/grant deny {proposal.persona_id} {proposal.short_code}`"
    if str(proposal.surface or "").strip().casefold() == SURFACE_CABINET:
        lines.append(
            "This room cannot decide it — run it where the bot listens "
            f"(Telegram/Discord/CLI): {approve_cmd} · {deny_cmd}"
        )
    else:
        lines.append(f"Approve: {approve_cmd} · Deny: {deny_cmd}")
    lines.append(f"Expires in ~{minutes}m if untouched.")
    return "\n".join(lines)


def unknown_toolset_card(persona_id: str, wanted: str) -> str:
    """An honest registry miss — the persona asked for something unregistered."""
    suggestions = _grants.nearest_names(wanted)
    hint = f" Nearest: {', '.join(suggestions)}." if suggestions else ""
    return (
        f"`{persona_id}` asked for a `{str(wanted)[:64]}` toolset, which is not in "
        f"the live registry, so there is nothing to approve.{hint}"
    )


def proposal_unavailable_card(persona_id: str, toolset: str) -> str:
    """An honest miss when the name IS registered but no proposal was made.

    Distinct from :func:`unknown_toolset_card` on purpose: the toolset name
    is real, so telling the operator it "is not in the live registry" would
    be false. The actual reason (counter-offers disabled, an unsupported
    profile, a missing operator turn) is already audited by
    :func:`propose_grant` — this card just avoids repeating the wrong one.
    """
    return (
        f"`{persona_id}` asked for the `{toolset}` toolset, but no counter-offer "
        "could be created right now. Nothing changed; check the grant ledger "
        "for the reason."
    )


# ── Store operations ─────────────────────────────────────────────────────


def expire_pending(
    persona_id: str,
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> list[GrantProposal]:
    """Quietly expire *persona_id*'s stale proposals; audit each one.

    Called at the top of every read and of the decision path, so an expired
    proposal can never be approved — the honest 410-style reply is produced
    from the row this flipped, not from a timestamp compared somewhere else.
    """
    persona = str(persona_id or "").strip()
    if not persona:
        return []
    current = time.time() if now is None else float(now)
    expired: list[GrantProposal] = []
    conn = _connect(persona, db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM persona_grant_proposals "
            "WHERE persona_id = ? AND status = ? AND expires_at <= ?",
            (persona, STATUS_PENDING, current),
        ).fetchall()
        if rows:
            conn.executemany(
                "UPDATE persona_grant_proposals SET status = ?, decided_at = ?, "
                "status_detail = 'proposal TTL elapsed' "
                "WHERE proposal_id = ? AND status = ?",
                [
                    (STATUS_EXPIRED, current, str(row["proposal_id"]), STATUS_PENDING)
                    for row in rows
                ],
            )
        conn.commit()
        expired = [p for row in rows if (p := _row_to_proposal(row)) is not None]
    finally:
        conn.close()
    for proposal in expired:
        try:
            _audit_strict(
                _grants.OUTCOME_EXPIRED,
                persona_id=proposal.persona_id,
                toolset=proposal.toolset,
                reason=REASON_PROPOSAL_EXPIRED,
                actor=proposal.requested_by,
                surface=proposal.surface,
                channel_id=proposal.channel_id,
                trigger_text=proposal.trigger_text,
                correlation_id=proposal.proposal_id,
                audit_path=audit_path,
            )
        except Exception as exc:  # noqa: BLE001 — row already expired; never lose the gap silently
            _logger.error(
                "grant proposals: expiry audit failed for %s/%s (%s): %s: %s",
                proposal.persona_id,
                proposal.toolset,
                proposal.proposal_id,
                type(exc).__name__,
                exc,
            )
            _mark_audit_unrecorded(persona, proposal.proposal_id, db_path=db_path)
    return expired


def _discover_store_paths() -> list[Path]:
    """Every proposal store that PHYSICALLY exists, newest-profile-agnostic.

    Rule 2: the sweep enumerates files on disk rather than a profile registry
    or a config list — a persona bot that wrote a store is exactly the set we
    must expire, whether or not anything else still lists that profile. Only
    existing files are returned, so a sweep never CREATES a store (``_connect``
    would happily create one) for a profile that never proposed anything.
    """
    candidates: list[Path] = []
    try:
        from personas.core import (  # noqa: PLC0415 — cycle-safe
            get_default_homie_root,
        )

        profiles_root = Path(get_default_homie_root()) / "profiles"
        if profiles_root.is_dir():
            candidates.extend(
                entry / "data" / STORE_FILENAME
                for entry in sorted(profiles_root.iterdir())
                if entry.is_dir()
            )
    except Exception as exc:  # noqa: BLE001 — a sweep must never raise
        _logger.warning(
            "grant proposals: profile store discovery failed: %s: %s",
            type(exc).__name__,
            exc,
        )
    try:
        import config  # noqa: PLC0415 — cycle-safe + test-monkeypatched

        candidates.append(Path(config.DATA_DIR) / STORE_FILENAME)
    except Exception as exc:  # noqa: BLE001 — same
        _logger.warning(
            "grant proposals: ambient store discovery failed: %s: %s",
            type(exc).__name__,
            exc,
        )
    return [path for path in dict.fromkeys(candidates) if path.is_file()]


def _personas_with_stale_rows(store: Path, current: float) -> list[str]:
    """Persona ids holding at-or-past-TTL pending rows in *store*."""
    conn = _connect("", store)
    try:
        rows = conn.execute(
            "SELECT DISTINCT persona_id FROM persona_grant_proposals "
            "WHERE status = ? AND expires_at <= ?",
            (STATUS_PENDING, current),
        ).fetchall()
    finally:
        conn.close()
    return [str(row["persona_id"]) for row in rows if str(row["persona_id"] or "")]


def sweep_expired(
    *,
    now: float | None = None,
    db_paths: list[Path | str] | None = None,
    audit_path: Path | str | None = None,
) -> list[GrantProposal]:
    """Expire every persona's un-actioned proposals at TTL, without a reader.

    The rest of this module expires LAZILY — at the top of every read and on
    the decision path — which is enough to make an expired proposal
    un-approvable but NOT enough to satisfy "un-actioned proposals expire
    quietly at TTL; expiry audited": a proposal nobody ever lists or taps
    stays physically ``pending`` forever and never emits its expiry row. This
    is the scheduled caller that closes that gap; it rides the existing
    heartbeat cadence (``heartbeat.expire_stale_grant_proposals``) the same
    way draft expiry does, so expiry lands within one heartbeat interval of
    the TTL rather than never.

    The persona id comes from the ROW, not from the directory name, so the
    ambient store (which can hold rows for more than one persona) sweeps
    correctly too. Each store and each persona is isolated: one unreadable
    file or one failing audit cannot stop the rest of the sweep.
    """
    current = time.time() if now is None else float(now)
    stores = (
        [Path(p) for p in db_paths] if db_paths is not None else _discover_store_paths()
    )
    expired: list[GrantProposal] = []
    for store in stores:
        try:
            personas = _personas_with_stale_rows(store, current)
        except Exception as exc:  # noqa: BLE001 — one bad store, not the sweep
            _logger.warning(
                "grant proposals: sweep could not read %s: %s: %s",
                store,
                type(exc).__name__,
                exc,
            )
            continue
        for persona in personas:
            try:
                expired.extend(
                    expire_pending(
                        persona,
                        now=current,
                        db_path=store,
                        audit_path=audit_path,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one bad persona, not the sweep
                _logger.warning(
                    "grant proposals: sweep failed for %s in %s: %s: %s",
                    persona,
                    store,
                    type(exc).__name__,
                    exc,
                )
    return expired


def get_proposal(
    persona_id: str,
    code_or_id: str,
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> GrantProposal | None:
    """One proposal by short code or id, with stale rows expired first."""
    persona = str(persona_id or "").strip()
    needle = str(code_or_id or "").strip()
    if not persona or not needle:
        return None
    expire_pending(persona, now=now, db_path=db_path, audit_path=audit_path)
    conn = _connect(persona, db_path)
    try:
        return _row_to_proposal(
            conn.execute(
                "SELECT * FROM persona_grant_proposals WHERE persona_id = ? "
                "AND (proposal_id = ? OR upper(short_code) = upper(?))",
                (persona, needle, needle),
            ).fetchone()
        )
    finally:
        conn.close()


def list_pending(
    persona_id: str,
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> list[GrantProposal]:
    """Un-actioned proposals for *persona_id*, newest first."""
    persona = str(persona_id or "").strip()
    if not persona:
        return []
    expire_pending(persona, now=now, db_path=db_path, audit_path=audit_path)
    conn = _connect(persona, db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM persona_grant_proposals WHERE persona_id = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (persona, STATUS_PENDING, _MAX_PENDING_LISTED),
        ).fetchall()
    finally:
        conn.close()
    return [p for row in rows if (p := _row_to_proposal(row)) is not None]


def propose_grant(
    persona_id: str,
    toolset: str,
    *,
    requested_by: str,
    trigger_text: str,
    surface: str,
    channel_id: str,
    thread_id: str = "",
    rationale: str = "",
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> GrantProposal | None:
    """Record one persona's ask. Returns ``None`` when there is nothing to ask.

    This function CANNOT change a persona's scope — it writes one row to a
    sqlite store and nothing else. Every refusal branch is audited and returns
    ``None`` rather than raising: a counter-offer that cannot be made is a
    normal reply without a card, never a broken turn.

    Refusals, in order: the operator's kill switch, a blank persona, an
    invalid persona name, the default profile (the executor cannot serve it —
    #426's Q6 verdict — so proposing there would promise a grant that must be
    refused on approve), a name that is not in the live registry, and a
    missing operator turn. The last one matters: ``requested_by`` and
    ``trigger_text`` are the turn that PROMPTED the counter-offer, and the
    executor requires them at approve time, so a proposal that cannot carry
    them is refused where it is cheap rather than at the tap.
    """
    persona = str(persona_id or "").strip()
    wanted_raw = str(toolset or "").strip()
    who = str(requested_by or "").strip()
    trigger = _grants.normalize_trigger_text(trigger_text)
    surface = str(surface or "").strip()
    channel = str(channel_id or "").strip()

    def _refuse(
        reason: str,
        error: str,
        *,
        toolset_label: str = "",
        suggestions: tuple[str, ...] = (),
    ) -> None:
        """One audited refusal row. Same shape as the executor's ``_refuse``."""
        _audit(
            _grants.OUTCOME_REFUSED,
            persona_id=persona,
            toolset=toolset_label or wanted_raw[:64],
            reason=reason,
            actor=who,
            surface=surface,
            channel_id=channel,
            trigger_text=trigger,
            suggestions=suggestions,
            error=error,
            audit_path=audit_path,
        )

    try:
        from security import kill_switches  # noqa: PLC0415 — Rule 3 module attr
    except Exception as exc:  # noqa: BLE001 — see comment
        # The switch is an OFF control, never the thing that grants the
        # feature — its absence must not silently disable a working surface.
        # Receipt only, matching runtime/persona_tools.py:152-156.
        _logger.warning(
            "grant proposals: kill-switch module unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
    else:
        try:
            kill_switches.requireEnabled(
                KILL_SWITCH_NAME, caller="personas.propose_grant"
            )
        except kill_switches.KillSwitchDisabled as exc:
            _refuse(_grants.REASON_KILL_SWITCH, str(exc))
            return None

    if not persona:
        return None

    # Checked BEFORE the name validator, matching the executor's own ordering
    # (``_mutate_persona_toolset``): ``default`` is a RESERVED name, so the
    # validator would refuse it as merely invalid and hide the real reason.
    if persona == "default":
        _refuse(
            _grants.REASON_DEFAULT_PROFILE_UNSUPPORTED,
            "the default profile does not read config `toolsets:` (#426 Q6)",
        )
        return None

    try:
        from personas.core import validate_persona_name  # noqa: PLC0415 — cycle-safe

        validate_persona_name(persona)
    except Exception as exc:  # noqa: BLE001 — an unnameable persona has no store
        _refuse(_grants.REASON_INVALID_PERSONA, str(exc))
        return None

    name = normalize_toolset_name(wanted_raw)
    if not name:
        _refuse(
            _grants.REASON_UNKNOWN_TOOLSET,
            "not in the live toolset registry",
            suggestions=_grants.nearest_names(wanted_raw),
        )
        return None

    if not who or not trigger or not surface or not channel:
        _refuse(
            _grants.REASON_MISSING_OPERATOR_TURN,
            "a proposal needs the operator turn that prompted it",
            toolset_label=name,
        )
        return None

    current = time.time() if now is None else float(now)
    expires_at = current + proposal_ttl_seconds()
    rationale_text = _grants.normalize_trigger_text(rationale)[:_MAX_RATIONALE_CHARS]

    conn = _connect(persona, db_path)
    created: GrantProposal | None = None
    try:
        # A short code has to be unique in this store; collisions are rare but
        # cheap to retry, and a UNIQUE index is the only thing that can prove
        # it. Exhausting the retries is honest failure, not a duplicate code.
        for _ in range(6):
            proposal_id = uuid.uuid4().hex
            short_code = uuid.uuid4().hex[:6].upper()
            try:
                conn.execute(
                    "INSERT INTO persona_grant_proposals ("
                    "proposal_id, short_code, persona_id, toolset, status, "
                    "created_at, expires_at, surface, channel_id, thread_id, "
                    "requested_by, trigger_text, rationale) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        proposal_id,
                        short_code,
                        persona,
                        name,
                        STATUS_PENDING,
                        current,
                        expires_at,
                        surface,
                        channel,
                        str(thread_id or "").strip(),
                        who,
                        trigger,
                        rationale_text,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                continue
            created = _row_to_proposal(
                conn.execute(
                    "SELECT * FROM persona_grant_proposals WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
            )
            break
    finally:
        conn.close()

    if created is None:
        _logger.warning(
            "grant proposals: could not allocate a short code for %s/%s", persona, name
        )
        return None

    _audit(
        _grants.OUTCOME_PROPOSED,
        persona_id=created.persona_id,
        toolset=created.toolset,
        actor=created.requested_by,
        surface=created.surface,
        channel_id=created.channel_id,
        trigger_text=created.trigger_text,
        correlation_id=created.proposal_id,
        audit_path=audit_path,
    )
    return created


def decide_proposal(
    persona_id: str,
    code_or_id: str,
    *,
    approve: bool,
    actor: str,
    actor_role: str,
    surface: str,
    channel_id: str,
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> ProposalDecision:
    """The ONLY way a proposal becomes a grant. Admin-gated, CAS'd, audited.

    ``actor_role`` is CHECKED here and CHECKED AGAIN by the executor; neither
    ESTABLISHES it. The caller must resolve the role server-side from the
    authenticated surface — a role forwarded from a payload (or produced by a
    model) reduces both gates to a formality (#426, #427).

    Order is deliberate. The proposal is READ first (a read mutates nothing)
    so every refusal row below can name the real persona and toolset, which is
    what makes the epic's metric greppable. Then the kill switch (a blanket
    off, so it is checked before any finer-grained gate — matching
    ``propose_grant``; a proposal made before the switch flipped off must not
    still be approvable). Then the role gate. Then the status checks —
    expired and already-decided are honest answers, not errors. Only then
    does the CAS flip ``pending`` to a decision, and only a winning CAS
    reaches the executor, so a double tap grants once.

    Both the kill-switch and role refusals are AUDITED, not best-effort: a
    refusal is an answer the caller reads back as recorded, so an audit-write
    failure returns :data:`DECISION_AUDIT_FAILED` instead of a polished
    "refused" that would be a lie about what the ledger actually holds.

    A failing executor leaves the proposal decided with the failure recorded:
    the mutation path already audited its own refusal, and re-running an
    ambiguous approval is worse than making the operator say it again.
    """
    persona = str(persona_id or "").strip()
    needle = str(code_or_id or "").strip()
    who = str(actor or "").strip()
    role = str(actor_role or "").strip().lower()
    surface = str(surface or "").strip()
    channel = str(channel_id or "").strip()
    current = time.time() if now is None else float(now)

    if not persona or not needle:
        return ProposalDecision(
            DECISION_UNKNOWN, None, "That counter-offer could not be identified."
        )

    proposal = get_proposal(
        persona, needle, now=current, db_path=db_path, audit_path=audit_path
    )
    if proposal is None:
        return ProposalDecision(
            DECISION_UNKNOWN,
            None,
            f"No counter-offer `{needle[:32]}` for `{persona}`.",
        )

    def _refuse_audited(
        reason: str, refused_message: str, *, error: str
    ) -> ProposalDecision:
        """One REQUIRED refusal row, or an honest audit-failed decision.

        A role-gate or kill-switch refusal mutates nothing, so unlike
        ``expire_pending`` there is no row to mark — the DECISION ITSELF is
        the only receipt, and it must not claim "recorded" when it is not.
        """
        try:
            _audit_strict(
                _grants.OUTCOME_REFUSED,
                persona_id=proposal.persona_id,
                toolset=proposal.toolset,
                reason=reason,
                actor=who,
                actor_role=role,
                surface=surface,
                channel_id=channel,
                trigger_text=proposal.trigger_text,
                correlation_id=proposal.proposal_id,
                error=error,
                audit_path=audit_path,
            )
        except Exception as exc:  # noqa: BLE001 — refuse honestly, never a phantom receipt
            _logger.error(
                "grant proposals: refusal (%s) for %s/%s could not be audited: %s: %s",
                reason,
                proposal.persona_id,
                proposal.toolset,
                type(exc).__name__,
                exc,
            )
            return ProposalDecision(
                DECISION_AUDIT_FAILED,
                proposal,
                refused_message
                + " The refusal itself could not be recorded — try again.",
            )
        return ProposalDecision(DECISION_REFUSED, proposal, refused_message)

    try:
        from security import kill_switches  # noqa: PLC0415 — Rule 3 module attr
    except Exception as exc:  # noqa: BLE001 — same precedent as propose_grant
        _logger.warning(
            "grant proposals: kill-switch module unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
    else:
        try:
            kill_switches.requireEnabled(
                KILL_SWITCH_NAME, caller="personas.decide_proposal"
            )
        except kill_switches.KillSwitchDisabled as exc:
            return _refuse_audited(
                _grants.REASON_KILL_SWITCH,
                "Toolset counter-offers are currently disabled. Nothing changed.",
                error=str(exc),
            )

    if role != _grants.ADMIN_ROLE:
        return _refuse_audited(
            _grants.REASON_NOT_AUTHORIZED,
            f"Refused: deciding a toolset counter-offer requires the "
            f"{_grants.ADMIN_ROLE} role, got {role or 'none'!r}. Nothing changed.",
            error=f"decision requires the {_grants.ADMIN_ROLE} role",
        )

    if proposal.status == STATUS_EXPIRED:
        return ProposalDecision(
            DECISION_EXPIRED,
            proposal,
            f"Counter-offer `{proposal.short_code}` expired and can no longer be "
            f"approved. Ask `{proposal.persona_id}` again if it still needs "
            f"`{proposal.toolset}`.",
        )
    if proposal.status != STATUS_PENDING:
        return ProposalDecision(
            DECISION_ALREADY_DECIDED,
            proposal,
            f"Counter-offer `{proposal.short_code}` is already "
            f"{proposal.status}{': ' + proposal.status_detail if proposal.status_detail else ''}.",
        )

    next_status = STATUS_APPROVED if approve else STATUS_DENIED
    conn = _connect(persona, db_path)
    try:
        cursor = conn.execute(
            "UPDATE persona_grant_proposals SET status = ?, decided_by = ?, "
            "decided_at = ?, status_detail = ? "
            "WHERE proposal_id = ? AND status = ?",
            (
                next_status,
                who,
                current,
                "operator approved the counter-offer" if approve else "operator denied",
                proposal.proposal_id,
                STATUS_PENDING,
            ),
        )
        conn.commit()
        won = cursor.rowcount == 1
    finally:
        conn.close()

    if not won:
        return ProposalDecision(
            DECISION_ALREADY_DECIDED,
            proposal,
            f"Counter-offer `{proposal.short_code}` was already decided.",
        )

    if not approve:
        _audit(
            _grants.OUTCOME_DENIED,
            persona_id=proposal.persona_id,
            toolset=proposal.toolset,
            actor=who,
            actor_role=role,
            surface=surface,
            channel_id=channel,
            trigger_text=proposal.trigger_text,
            correlation_id=proposal.proposal_id,
            audit_path=audit_path,
        )
        return ProposalDecision(
            DECISION_DENIED,
            proposal,
            f"Denied `{proposal.toolset}` for `{proposal.persona_id}`. Nothing changed.",
        )

    _audit(
        _grants.OUTCOME_APPROVED,
        persona_id=proposal.persona_id,
        toolset=proposal.toolset,
        actor=who,
        actor_role=role,
        surface=surface,
        channel_id=channel,
        trigger_text=proposal.trigger_text,
        correlation_id=proposal.proposal_id,
        audit_path=audit_path,
    )

    # The approving action IS the operator turn the executor demands, and the
    # turn that prompted the counter-offer is carried with it so one ledger
    # row shows both halves of "who ordered this".
    approval_trigger = (
        f"approved counter-offer {proposal.short_code}"
        f"{': ' + proposal.trigger_text if proposal.trigger_text else ''}"
    )
    try:
        # Lazy + module-attribute: ``services`` is heavy, and a test patching
        # ``personas.services.add_persona_toolset`` must propagate here.
        from personas import services as _services  # noqa: PLC0415 — cycle-safe

        result = _services.add_persona_toolset(
            proposal.persona_id,
            proposal.toolset,
            actor=who,
            actor_role=role,
            trigger_text=approval_trigger,
            surface=surface,
            channel_id=channel,
            audit_path=audit_path,
        )
    except Exception as exc:  # noqa: BLE001 — every refusal class reports the same way
        detail = f"{type(exc).__name__}: {exc}"
        _record_status_detail(
            persona, proposal.proposal_id, detail, db_path=db_path
        )
        return ProposalDecision(
            DECISION_FAILED,
            proposal,
            f"Approved, but the grant did not land: {detail}",
        )

    _record_status_detail(
        persona,
        proposal.proposal_id,
        f"grant {result.outcome}",
        db_path=db_path,
    )
    live = ", ".join(result.toolsets) or "(none)"
    return ProposalDecision(
        DECISION_GRANTED,
        proposal,
        f"Granted `{proposal.toolset}` to `{proposal.persona_id}` — live on its "
        f"next turn. Toolsets now: {live}.",
        result=result,
    )


def _record_status_detail(
    persona_id: str,
    proposal_id: str,
    detail: str,
    *,
    db_path: Path | str | None,
) -> None:
    """Attach the executor's verdict to the decided row. Never raises.

    The decision already happened and was audited; this is the operator-facing
    breadcrumb on a later ``/grant list`` or repeat tap. Losing it costs
    legibility, never correctness, so a store failure is a logged receipt.
    """
    try:
        conn = _connect(persona_id, db_path)
        try:
            conn.execute(
                "UPDATE persona_grant_proposals SET status_detail = ? "
                "WHERE proposal_id = ?",
                (str(detail)[:300], proposal_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — breadcrumb only
        _logger.warning(
            "grant proposals: status detail not recorded for %s: %s: %s",
            proposal_id,
            type(exc).__name__,
            exc,
        )


# ── The chat-surface seam ────────────────────────────────────────────────


# Receipt fields are identifiers, codes, and outcomes — never prose. Anything
# outside this charset is dropped rather than escaped, so no reply text, error
# string, or model output can ride a receipt into a later prompt.
_RECEIPT_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.:@-]+")


def _receipt_field(value: Any, *, limit: int = 64) -> str:
    """One transcript-safe receipt field: fixed charset, bounded, never blank."""
    return _RECEIPT_UNSAFE_RE.sub("", str(value or "").strip())[:limit] or "unknown"


def decision_receipt(
    decision: ProposalDecision,
    *,
    approve: bool,
    persona_id: str,
    code: str,
    actor: str,
    actor_role: str,
) -> tuple[str, str]:
    """The sanitized ``(user_row, assistant_row)`` a decided tap persists.

    A button tap has no operator TEXT to persist and its operator-facing
    reply is not transcript-safe: that reply can carry an executor exception,
    a stored ``status_detail``, or the persona's live toolset list, and the
    router's transcript is replayed into later prompts by
    ``recent_conversation``. So the durable rows are neither — they are these
    two fully server-generated, fixed-field receipts (the #424 learn-drop
    sanitized-receipt pattern, applied to BOTH rows because a tap authors
    neither of them).

    What a receipt must carry is the whole point of the acceptance criterion:
    who approved, which persona, which toolset, the code, and what actually
    happened — so a real session transcript shows counter-offer → authenticated
    approval → grant result instead of jumping from the card to the next task.

    Prefers the decided proposal's own stored values (server-side, already
    validated) over the caller's arguments, and scrubs every field either way.
    """
    proposal = decision.proposal
    action = ACTION_APPROVE if approve else ACTION_DENY
    persona = _receipt_field(getattr(proposal, "persona_id", "") or persona_id)
    short_code = _receipt_field(getattr(proposal, "short_code", "") or code, limit=32)
    toolset = _receipt_field(getattr(proposal, "toolset", ""))
    user_row = (
        f"[server command] grant {action} -> persona={persona} code={short_code}"
    )
    assistant_row = (
        f"[grant receipt] decision={action} outcome={_receipt_field(decision.outcome)} "
        f"persona={persona} toolset={toolset} code={short_code} "
        f"by={_receipt_field(actor, limit=80)} role={_receipt_field(actor_role, limit=32)}"
    )
    return user_row, assistant_row


def tee_up_from_reply(
    persona_id: str,
    reply_text: str,
    *,
    requested_by: str,
    trigger_text: str,
    surface: str,
    channel_id: str,
    thread_id: str = "",
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> CounterOffer | None:
    """Turn a persona reply's marker into a pending proposal + a card.

    The one function a chat surface calls. Returns ``None`` when the reply
    carries no marker — the overwhelmingly common case, and the cheapest one:
    a regex miss and nothing else. When a marker IS present the marker text is
    stripped either way, so an unknown-toolset ask still produces a clean
    reply plus an honest miss instead of leaking ``<<GRANT_REQUEST: …>>``.

    *persona_id* comes from the caller's already-resolved channel binding or
    active profile — never from the reply. A persona cannot propose for
    another persona because it has no way to say which persona this is.

    Synchronous sqlite + file IO. Async callers MUST route this through
    ``asyncio.to_thread``; it is called once per persona turn, off the reply
    path's critical section.

    Whole-body fail-open: a counter-offer is an affordance, and no failure in
    it may cost the operator the persona's actual answer.
    """
    try:
        cleaned, wanted = parse_grant_marker(reply_text)
        if not wanted:
            return None
        # Pre-checked so a None from propose_grant can be told apart from a
        # genuinely unregistered name: propose_grant returns None for EVERY
        # refusal branch (kill switch, invalid persona, default profile,
        # unregistered toolset, missing operator turn), and reporting all of
        # them as "not in the live registry" is a lie when the name is real.
        known_name = normalize_toolset_name(wanted)
        proposal = propose_grant(
            persona_id,
            wanted,
            requested_by=requested_by,
            trigger_text=trigger_text,
            surface=surface,
            channel_id=channel_id,
            thread_id=thread_id,
            rationale=cleaned[-_MAX_RATIONALE_CHARS:],
            now=now,
            db_path=db_path,
            audit_path=audit_path,
        )
        if proposal is None:
            if not known_name:
                return CounterOffer(
                    reply_text=cleaned,
                    card_text=unknown_toolset_card(persona_id, wanted),
                )
            return CounterOffer(
                reply_text=cleaned,
                card_text=proposal_unavailable_card(persona_id, known_name),
            )
        return CounterOffer(
            reply_text=cleaned,
            card_text=card_text(proposal),
            approve_custom_id=approve_custom_id(proposal),
            deny_custom_id=deny_custom_id(proposal),
            proposal=proposal,
        )
    except Exception as exc:  # noqa: BLE001 — never cost the turn its answer
        _logger.warning(
            "grant proposals: counter-offer tee-up failed for %s: %s: %s",
            persona_id,
            type(exc).__name__,
            exc,
        )
        return None


__all__ = [
    "ACTION_APPROVE",
    "ACTION_DENY",
    "CUSTOM_ID_PREFIX",
    "DECISION_ALREADY_DECIDED",
    "DECISION_AUDIT_FAILED",
    "DECISION_DENIED",
    "DECISION_ERROR",
    "DECISION_EXPIRED",
    "DECISION_FAILED",
    "DECISION_GRANTED",
    "DECISION_REFUSED",
    "DECISION_UNKNOWN",
    "KILL_SWITCH_NAME",
    "REASON_ALREADY_DECIDED",
    "REASON_GRANT_FAILED",
    "REASON_INVALID_MARKER",
    "REASON_PROPOSAL_EXPIRED",
    "REASON_UNKNOWN_PROPOSAL",
    "STATUS_APPROVED",
    "STATUS_DENIED",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "STORE_FILENAME",
    "SURFACE_CABINET",
    "CounterOffer",
    "GrantProposal",
    "ProposalDecision",
    "approve_custom_id",
    "card_text",
    "counter_offer_briefing",
    "decide_proposal",
    "decision_receipt",
    "deny_custom_id",
    "expire_pending",
    "get_proposal",
    "list_pending",
    "normalize_toolset_name",
    "parse_custom_id",
    "parse_grant_marker",
    "propose_grant",
    "proposal_ttl_seconds",
    "proposal_unavailable_card",
    "resolve_store_path",
    "sweep_expired",
    "tee_up_from_reply",
    "unknown_toolset_card",
]
