"""Ledger, refusal, and registry lookup for persona toolset self-provisioning.

Issue #426 (epic #419). ``personas.services.add_persona_toolset`` /
``remove_persona_toolset`` are the only path that mutates an EXISTING
persona's ``toolsets:`` list — creation-time provisioning writes the initial
list through its own door (``personas.provisioning``/``blueprints``), which is
why the sentinel guard lives on both. This module owns the three things the
grant path needs and ``services.py`` should not grow inline:

* the append-only JSONL ledger (who / what / when / trigger-text / channel),
* the refusal type every honest "no" is raised with,
* the live-registry lookup + nearest-match hint behind an unknown name.

**Why the ledger carries the triggering turn.** The epic's metric 5 is "zero
grants without a matching live operator turn". A row that records only the
outcome cannot prove that; a row that carries the operator's identity AND the
verbatim text that ordered it can be grepped for the negative case directly.
So ``actor`` and ``trigger_text`` are REQUIRED by the executor's contract
rather than optional context — a grant nobody ordered is not expressible.

**Registry reads are live.** ``known_toolset_names()`` re-reads
``runtime.toolsets.TOOLSETS`` through the module attribute on every call
(Rule 2 physical-state + Rule 3 module-attribute lookup). A snapshot taken at
import time would refuse a plugin-registered toolset as "unknown" forever, and
would make the refusal a lie about the registry rather than a report of it.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# One append-only ledger for the whole provisioning surface. Grep target for
# the epic's metric 5 — every row, granted or refused, lands here.
LEDGER_FILENAME = "persona_toolset_grants.jsonl"
INTEGRATION = "personas"

# The only role that may reach the executor. Mirrors the router's
# ``role_level = {"viewer": 0, "operator": 1, "admin": 2}`` ladder
# (chat/router.py:1165) — a toolset grant is the top rung, never below it.
ADMIN_ROLE = "admin"

OPERATION_GRANT = "grant"
OPERATION_REVOKE = "revoke"
# Issue #428's counter-offer lifecycle (``personas.grant_proposals``). A
# proposal is a persona ASKING; it moves no config and grants no reach, so it
# is a third operation rather than a grant row with a softer outcome. Its rows
# share this ledger on purpose — metric 5 stays one grep target, and an
# approved proposal's actual mutation still arrives as a normal
# ``grant``/``intent`` pair written by the executor.
OPERATION_PROPOSE = "propose"

OUTCOME_GRANTED = "granted"
OUTCOME_REVOKED = "revoked"
OUTCOME_ALREADY_GRANTED = "already_granted"
OUTCOME_NOT_GRANTED = "not_granted"
OUTCOME_REFUSED = "refused"
OUTCOME_ERROR = "error"
# Proposal-lifecycle outcomes (#428). NONE of them appear in the replay's
# admitted set below, so a proposal row — including an ``approved`` one —
# contributes ZERO reach to :func:`ledger_scope`. That is the point: the
# operator's approval is recorded here, but only the executor's own
# ``intent`` + ``granted`` pair can say a persona's scope moved.
OUTCOME_PROPOSED = "proposed"
OUTCOME_APPROVED = "approved"
OUTCOME_DENIED = "denied"
OUTCOME_EXPIRED = "expired"
# The authorized-and-about-to-write row. Written BEFORE the config mutation
# and paired to its outcome row by ``correlation_id``. It records that an
# authorized attempt began — never that it succeeded. Only ``granted`` /
# ``revoked`` say that, and only after the atomic replace returned.
OUTCOME_INTENT = "intent"

# Outcome rows that mean the persona's live scope actually changed. Anything
# else (intent, refused, error, already_granted, not_granted) did not move
# physical state and must never be replayed as one.
_EFFECTIVE_OUTCOMES = frozenset({OUTCOME_GRANTED, OUTCOME_REVOKED})

# Rows that settle an intent row's correlation id — completed (granted /
# revoked) or failed-before-the-write (error). An intent with none of these
# is a torn attempt: the config moved, the outcome never got recorded.
_RESOLVING_OUTCOMES = frozenset({OUTCOME_GRANTED, OUTCOME_REVOKED, OUTCOME_ERROR})

REASON_INVALID_PERSONA = "invalid_persona"
REASON_UNKNOWN_PERSONA = "unknown_persona"
REASON_INVALID_TOOLSET = "invalid_toolset"
REASON_UNKNOWN_TOOLSET = "unknown_toolset"
REASON_MISSING_OPERATOR_TURN = "missing_operator_turn"
REASON_NOT_AUTHORIZED = "not_authorized"
REASON_KILL_SWITCH = "kill_switch"
# The registry itself could not be read, so NO name could be verified — as
# distinct from a name that was checked against a live registry and missed
# (:data:`REASON_UNKNOWN_TOOLSET`). ``known_toolset_names()`` fails closed to
# an empty tuple, which makes every name look unknown; without this reason an
# outage would be audited as, and reported as, the operator's typo. A refusal
# has to name the thing that actually failed.
REASON_REGISTRY_UNAVAILABLE = "registry_unavailable"
REASON_DEFAULT_PROFILE_UNSUPPORTED = "default_profile_unsupported"
REASON_CONFIG_SHAPE = "config_shape"
REASON_WRITE_FAILED = "write_failed"
# Another writer held the per-persona read-modify-write lock past the
# executor's bound. Nothing was read, decided, or written under this reason —
# it exists so a contended-out attempt still leaves a row, keeping the
# executor's "every exit writes exactly one ledger row" contract true.
REASON_LOCK_TIMEOUT = "lock_timeout"
# A retry found the toolset already gone from config while the ledger's
# effective rows still had it granted — i.e. a previous revoke moved physical
# state but its outcome row never landed. The retry appends the missing
# `revoked` row under this reason, correlated to that torn attempt's intent,
# so the ledger stops disagreeing with the file it describes.
REASON_REPAIR_CONFIG_ABSENT = "repair: observed config-absent"
# The mirror image: a retry found the toolset ALREADY IN config while the
# ledger had only a dangling grant intent — a grant whose config write landed
# and whose outcome row did not. Without this the grant is invisible to the
# replay, so a blueprint reconcile would not preserve a toolset the operator
# really was given. Both tears heal the same way, from physical state.
REASON_REPAIR_CONFIG_PRESENT = "repair: observed config-present"

# Long enough that the ledger row still reads as the operator's order, short
# enough that a pasted document cannot turn one grant into a log dump.
_TRIGGER_TEXT_MAX_CHARS = 400

# Ceiling for the identifier fields the replay reads (persona id, toolset,
# outcome, operation, correlation id). Every real value is well under this;
# the cap exists so a corrupt or hostile row cannot hand the replay an
# unbounded string to compare against.
_MAX_LEDGER_FIELD_CHARS = 512


class ToolsetGrantAuditError(RuntimeError):
    """A REQUIRED ledger row could not be written.

    Distinct from :class:`ToolsetGrantRefusedError` on purpose. A refusal is
    an answer the executor stands behind and has recorded; this says the
    executor could not record anything, so it will not hand back a polished
    refusal that a caller would reasonably read as audited.

    ``applied`` is the one bit a caller MUST branch on before choosing any
    word like "refused": most raises of this error mean nothing was
    mutated (a refusal row itself could not be written), but the outcome-row
    failure after a successful config write raises this SAME type with
    ``applied=True`` — the grant/revoke already landed; only its ledger
    confirmation failed. Collapsing the two into one reply would tell an
    operator a live change was rejected.

    Not a ``ValueError``: existing ``except ValueError`` callers around
    persona config writes treat that as "the request was bad", and this is
    not a bad request. It must escape those handlers.
    """

    def __init__(self, message: str, *, reason: str = "", applied: bool = False) -> None:
        self.reason = reason
        self.applied = applied
        super().__init__(message)


class ToolsetGrantRefusedError(ValueError):
    """An honest refusal from the grant executor. Never a partial write.

    ``ValueError`` subclass for the same reason ``ConfigShapeError`` is one:
    existing ``except ValueError`` callers around persona config writes keep
    working. ``reason`` is the machine-readable code a command surface
    branches on; ``suggestions`` carries nearest registry matches when the
    refusal was a name miss.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        suggestions: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason
        self.suggestions = tuple(suggestions)
        super().__init__(message)


@dataclass(frozen=True)
class LedgerScope:
    """What the operator's turns added and removed, replayed from the ledger.

    ``active`` — grants to PRESERVE through a template rewrite.
    ``tombstoned`` — names to keep OFF even when the blueprint recommends
    them, because an effective revoke took them away.

    The two are disjoint by construction: every row that adds to one removes
    from the other, so a name is never simultaneously preserved and removed.
    """

    active: tuple[str, ...] = ()
    tombstoned: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolsetGrantResult:
    """What the executor did, in terms a command surface can speak back.

    ``changed`` is the honest bit: an already-granted grant and a
    not-granted revoke are both real answers, not errors, and neither
    rewrites the file. ``suggestions`` is populated on a not-granted revoke
    with what the persona actually holds, so a typo gets a useful reply
    without abusing an exception for a non-error state.
    """

    persona_id: str
    toolset: str
    operation: str
    outcome: str
    changed: bool
    toolsets: tuple[str, ...]
    config_path: Path
    audit_id: str = ""
    suggestions: tuple[str, ...] = ()


def resolve_ledger_path(
    audit_path: Path | str | None = None,
    persona_id: str = "",
) -> Path:
    """Resolve the ledger path at call time (Rule 1 — None sentinel).

    **Invariant: the ledger, the lock, and ``config.yaml`` are all keyed to
    the TARGET persona; the ambient profile never selects the file.**

    *persona_id* names the persona the rows are ABOUT, and its own
    ``<profile>/data/`` is where they live — resolved through
    ``get_persona_paths(persona_id)``, never through the ambient
    ``config.DATA_DIR``. That constant is computed once at import from
    whichever profile the PROCESS is running as, and persona bots run as
    separate processes with ``HOMIE_HOME`` forced to their own profile root
    (``runtime/subprocess_env.py``). Keying off it meant a grant made inside
    the sales bot landed in the sales profile's ledger while provisioning —
    running as the default profile — read the install directory's, saw no
    grants, and erased them on the next reconcile. Same authorization grain,
    two different files (Rule 4: authorization grain == storage grain).

    Precedence: an explicit *audit_path* always wins (tests inject one); then
    the target persona's data dir; and only with NEITHER does this fall back
    to the ambient ``config.DATA_DIR``, which is the legacy shape and is
    correct only when there is no target persona to key on. Every caller in
    this module passes the persona it is acting for.

    ``config`` and ``personas.core`` are imported lazily inside the body:
    ``config`` imports ``personas`` at module load, so a top-level import
    here would be a cycle, and both resolvers are monkeypatched by tests,
    which only propagates when read through the module at call time.
    """
    if audit_path is not None:
        return Path(audit_path)
    persona = str(persona_id or "").strip()
    if persona:
        from personas.core import (  # noqa: PLC0415 — lazy: cycle-safe
            get_persona_paths,
        )

        return Path(get_persona_paths(persona)["data"]) / LEDGER_FILENAME
    import config  # noqa: PLC0415 — lazy: cycle-safe + test-monkeypatched

    return Path(config.DATA_DIR) / LEDGER_FILENAME


def normalize_trigger_text(text: Any) -> str:
    """Collapse the operator's turn to one greppable, secret-scrubbed line.

    Treated as hostile input at the seam: the text arrives from a chat turn,
    so it may carry newlines (which would split one event across ledger
    rows), unbounded length, or a pasted credential. Redaction runs through
    ``security.redact`` by module attribute and fails open to the normalized
    text — a redaction import failure must not cost the receipt.
    """
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    try:
        from security import redact as _redact  # noqa: PLC0415 — Rule 3 module attr

        scrubbed = _redact.redact_sensitive_text(collapsed)
        if isinstance(scrubbed, str) and scrubbed:
            collapsed = scrubbed
    except Exception as exc:  # noqa: BLE001 — redaction is defense, not the record
        _logger.warning(
            "personas.toolset_grants: trigger-text redaction unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
    return collapsed[:_TRIGGER_TEXT_MAX_CHARS]


def known_toolset_names() -> tuple[str, ...]:
    """Return the LIVE toolset registry keys, sorted. Empty means refuse.

    Fails closed on purpose. If ``runtime.toolsets`` cannot be imported we
    cannot verify any name, and writing an unverifiable grant is worse than
    refusing one — the caller's ``name not in known`` check turns an empty
    tuple into an honest "not in the live registry" refusal. The swallow
    leaves a warning receipt so the import failure is never invisible.
    """
    try:
        from runtime import toolsets as runtime_toolsets  # noqa: PLC0415 — Rule 3
    except Exception as exc:  # noqa: BLE001 — fail closed, with a receipt
        _logger.warning(
            "personas.toolset_grants: toolset registry unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return ()
    registry = getattr(runtime_toolsets, "TOOLSETS", None)
    if not isinstance(registry, dict):
        _logger.warning(
            "personas.toolset_grants: TOOLSETS is %s, not a mapping",
            type(registry).__name__,
        )
        return ()
    return tuple(sorted(str(name) for name in registry))


def describe_toolset(name: str) -> str:
    """One-line description of a registered toolset, or ``""``.

    Read live through the module attribute for the same reason
    :func:`known_toolset_names` is (Rule 2 + Rule 3): the approval card an
    operator taps must describe the bundle the registry holds RIGHT NOW, not
    whatever it held when this module was imported. A missing name, a missing
    registry, or a malformed entry is a blank description, never a raise — the
    card is legible without it and a proposal must not die on cosmetics.
    """
    wanted = str(name or "").strip()
    if not wanted:
        return ""
    try:
        from runtime import toolsets as runtime_toolsets  # noqa: PLC0415 — Rule 3

        registry = getattr(runtime_toolsets, "TOOLSETS", None)
        entry = registry.get(wanted) if isinstance(registry, dict) else None
        description = entry.get("description") if isinstance(entry, dict) else ""
    except Exception as exc:  # noqa: BLE001 — a blank description is survivable
        _logger.warning(
            "personas.toolset_grants: toolset description unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return ""
    return str(description or "").strip()


def nearest_names(
    wanted: str,
    *,
    names: tuple[str, ...] | None = None,
    limit: int = 5,
) -> tuple[str, ...]:
    """Nearest registry matches for a missed name — string distance only.

    Substring hits first (an operator typing ``research`` for
    ``research_read`` wants that one at the top), then ``difflib`` close
    matches for real typos. Deliberately NOT an NL fuzzy match: an ambiguous
    ask must come back as a question, never as a guessed grant, so this only
    ever produces a hint the operator confirms.

    Cutoff 0.6 rather than the 0.5 used for skill lookup
    (``talk_tools.py:1289``): measured against the live 18-name registry,
    0.5 offers ``browser`` for ``research`` and ``repo_read`` for
    ``seo_geo`` while 0.6 keeps every real typo hit (``reserch_raed`` ->
    ``research_read``, ``crypo`` -> ``crypto``). A grant hint that names an
    unrelated capability class is worse than no hint.
    """
    import difflib  # noqa: PLC0415 — stdlib, only needed on the refusal path

    pool = tuple(names) if names is not None else known_toolset_names()
    needle = str(wanted or "").strip().lower()
    if not needle or not pool:
        return ()

    lowered: dict[str, str] = {}
    for name in pool:
        lowered.setdefault(name.lower(), name)

    ranked: list[str] = []
    for low, original in lowered.items():
        if needle in low or low in needle:
            ranked.append(original)
    for match in difflib.get_close_matches(needle, list(lowered), n=limit, cutoff=0.6):
        original = lowered[match]
        if original not in ranked:
            ranked.append(original)
    return tuple(ranked[:limit])


def append_audit_record(
    *,
    operation: str,
    persona_id: str,
    toolset: str,
    outcome: str,
    actor: str = "",
    actor_role: str = "",
    surface: str = "",
    channel_id: str = "",
    trigger_text: str = "",
    reason: str = "",
    toolsets_after: tuple[str, ...] | list[str] = (),
    suggestions: tuple[str, ...] | list[str] = (),
    config_path: Path | str = "",
    error: str = "",
    correlation_id: str = "",
    audit_path: Path | str | None = None,
) -> str:
    """Append one ledger row and return its id. Raises on an IO failure.

    :func:`audit_attempt` is the best-effort wrapper callers use; this
    function stays strict so the wrapper has something to report.

    ``correlation_id`` pairs an :data:`OUTCOME_INTENT` row with the outcome
    row for the same attempt. An intent row with no matching outcome row is
    the readable signature of "authorized, started, never confirmed".

    The row lands in the TARGET persona's ledger — ``persona_id`` selects the
    file, not the profile this process happens to be running as. See
    :func:`resolve_ledger_path`.

    **The returned id is unique per row, not per attempt (#435).** The
    timestamp has second-level precision, so two rows for the same persona /
    operation / outcome within the same second — two different toolsets
    granted back to back, or a rapid double-tap retry — used to collapse onto
    the identical id string even though they are two distinct ledger lines. A
    future caller keying off this id (a dashboard receipt, a sibling #428/
    #429 surface) would silently read the wrong row. The toolset name and a
    short random suffix are appended so the id is unique by construction,
    while still starting with the timestamp for readability.
    """
    path = resolve_ledger_path(audit_path, persona_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "integration": INTEGRATION,
        "action": f"toolset_{operation}",
        "operation": operation,
        "persona_id": persona_id,
        "toolset": toolset,
        "outcome": outcome,
        "correlation_id": correlation_id,
        "reason": reason,
        "actor": actor,
        "actor_role": actor_role,
        "surface": surface,
        "channel_id": channel_id,
        "trigger_text": trigger_text,
        "toolsets_after": list(toolsets_after),
        "suggestions": list(suggestions),
        "config_path": str(config_path),
        "error": error[:200],
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return (
        f"{record['timestamp']}:{persona_id}:{operation}:{outcome}"
        f":{toolset}:{uuid.uuid4().hex[:8]}"
    )


def audit_attempt(**fields: Any) -> str:
    """Best-effort ledger append — a failed row never changes the outcome.

    Same precedent as ``talk_archon.audit_attempt`` and ``kill_switches``:
    the decision matters more than its record, and every swallow leaves a
    receipt. Returns the audit id, or ``""`` when the write failed.

    Use this ONLY for rows whose branch already surfaces its own failure to
    the caller (config-shape, write-failed, lock-timeout — each re-raises).
    A REFUSAL is not one of those: it returns a polished "no" the caller
    reads as recorded, so refusals go through :func:`append_audit_record`
    directly and turn an append failure into
    :class:`ToolsetGrantAuditError`.
    """
    try:
        return append_audit_record(**fields)
    except Exception as exc:  # noqa: BLE001 — audit is a record, not the action
        _logger.warning(
            "personas.toolset_grants: audit write failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return ""


def new_correlation_id() -> str:
    """A fresh id pairing one attempt's intent row with its outcome row."""
    return uuid.uuid4().hex


def _row_text(row: dict[str, Any], key: str) -> str:
    """A replay field as a bounded string, or ``""`` when it is not one.

    Replay fields (persona id, toolset, outcome, operation, correlation id)
    are short identifiers this module writes itself. A row is JSON, though,
    and JSON permits any value in any field — so a hostile or corrupt line
    can put a 10,000-level nested list where a toolset name belongs. The old
    code called ``str(...)`` on whatever it found, which recurses per nesting
    level and raised ``RecursionError`` out of a replay that promises to
    raise nothing (round 7).

    So the type is CHECKED, never coerced: anything that is not already a
    ``str`` — and any string longer than a real identifier could be — yields
    ``""``, and the caller skips the row. A field that cannot name a real
    persona or toolset can only ever fail to match one, so dropping it costs
    nothing and bounds the work per line.
    """
    value = row.get(key)
    if not isinstance(value, str) or len(value) > _MAX_LEDGER_FIELD_CHARS:
        return ""
    return value.strip()


# Every field the executor is REQUIRED to record for a real operator turn.
# A row missing any of them cannot be something this module wrote, because
# the executor refuses the whole operation when any is blank.
_PROVENANCE_FIELDS = (
    "actor",
    "actor_role",
    "trigger_text",
    "surface",
    "channel_id",
    "operation",
)


def _has_provenance(row: dict[str, Any]) -> bool:
    """True when every required operator-turn field is a nonblank string."""
    return all(_row_text(row, field) for field in _PROVENANCE_FIELDS)


def _attempt_key(row: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """``(correlation, persona, toolset, operation)`` identifying one attempt.

    ``None`` when any component is missing, which disqualifies the row — an
    attempt that cannot be identified cannot be correlated to its intent.
    """
    correlation = _row_text(row, "correlation_id")
    persona = _row_text(row, "persona_id")
    toolset = _row_text(row, "toolset")
    operation = _row_text(row, "operation")
    if not (correlation and persona and toolset and operation):
        return None
    return (correlation, persona, toolset, operation)


def _read_ledger_rows(path: Path) -> list[dict[str, Any]]:
    """Every well-formed row in *path*. Malformed input is skipped, not raised.

    **Malformed-line policy.** The ledger is an append-only log written by a
    process that can be killed mid-line, on a disk that can corrupt bytes. Two
    kinds of damage are tolerated, per LINE, so one bad line costs one row and
    nothing else:

    * bytes that are not valid UTF-8 — decoded strictly per line, so a corrupt
      line is dropped instead of taking the whole file down;
    * a line that is not a JSON object — truncated or interleaved writes.

    Both are counted and reported in ONE log line, so the damage is visible
    without a per-line log storm.

    Fail-open means BOTH halves: no phantom grants (a damaged line never
    invents reach) AND no blocked provisioning (a damaged line never turns a
    persona create or reconcile into a crash). This function therefore raises
    nothing — a missing or unreadable file is an empty ledger. Reading the
    whole file as one ``str`` is what made a single bad byte a
    ``UnicodeDecodeError`` that escaped the ``OSError`` handlers and blocked
    create/reconcile outright (round 6).
    """
    try:
        if not path.is_file():
            return []
        raw = path.read_bytes()
    except OSError as exc:
        _logger.warning(
            "personas.toolset_grants: ledger unreadable at %s: %s: %s",
            path,
            type(exc).__name__,
            exc,
        )
        return []

    rows: list[dict[str, Any]] = []
    undecodable = 0
    unparseable = 0
    for blob in raw.split(b"\n"):
        if not blob.strip():
            continue
        try:
            line = blob.decode("utf-8")
        except UnicodeDecodeError:
            undecodable += 1
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            # RecursionError belongs here: `json.loads` recurses per nesting
            # level, so a line with a few thousand nested arrays blows the
            # stack INSIDE the decoder. It is neither a ValueError nor an
            # OSError, so it escaped every handler and turned one hostile log
            # line into a blocked persona create/reconcile (round 7).
            unparseable += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            unparseable += 1
    if undecodable or unparseable:
        _logger.warning(
            "personas.toolset_grants: skipped %d undecodable and %d unparseable "
            "line(s) in %s; %d row(s) read",
            undecodable,
            unparseable,
            path,
            len(rows),
        )
    return rows


def active_grants(
    persona_id: str,
    audit_path: Path | str | None = None,
) -> tuple[str, ...]:
    """Replay the ledger into the toolsets *persona_id* currently holds BY GRANT.

    Grants minus revokes, in first-grant order. This is the executor-owned
    slice of a persona's scope: names an operator turn put there through
    :func:`personas.services.add_persona_toolset` and has not taken back.

    It is deliberately NOT the persona's full scope. Blueprint-authored and
    hand-authored toolsets never appear here, so a caller unioning this into
    a computed config preserves exactly what the ledger can prove was
    granted, and nothing else.

    A GRANT counts only from its :data:`OUTCOME_GRANTED` row — an intent
    whose write never confirmed must not resurrect a grant that was never
    live. A REVOKE drops the name from its INTENT row onward, without
    waiting for the outcome row.

    That asymmetry is deliberate: this replay is DERIVED state and the
    config file is the truth (Rule 2), so every ambiguity resolves toward
    LESS reach. The executor writes intent, mutates the config, then records
    the outcome, so an orphan revoke intent almost always means the reach is
    already gone on disk. Counting it as still-held is exactly what let a
    blueprint reconcile RE-ADD a revoked toolset (round 5).

    The cost is bounded and safe: a revoke that failed at the config write
    (intent + ``error``, toolset still on disk) reads as not-active here, so
    a later reconcile will not PRESERVE it — and it is NOT tombstoned either
    (see :func:`ledger_scope`), so a blueprint-owned bundle is never taken
    away on the strength of an unconfirmed intent.

    Fail-open by contract: a missing ledger, an unreadable one, or a
    malformed line yields no phantom grants rather than an exception. A
    provisioning run must never die because a log line was truncated.
    """
    return ledger_scope(persona_id, audit_path).active


def ledger_scope(
    persona_id: str,
    audit_path: Path | str | None = None,
) -> LedgerScope:
    """Replay the ledger into what the operator turned ON *and* OFF.

    Two sets, because "preserve the grants" is only half of what an operator
    told the system. ``active`` is what a reconcile must keep; ``tombstoned``
    is what it must keep OFF even when the blueprint recommends it.

    Without the negative set, a SUCCESSFUL revoke of a blueprint-recommended
    bundle came back on the next reconcile: the positive replay could say
    "not granted", but nothing could say "deliberately removed", and the
    template's recommendation won by default. The ledger said revoked while
    live reach returned (round 7).

    Event order wins in both directions, exactly like the positive replay
    always did — a re-grant clears the tombstone, a later revoke sets it
    again. The last thing the operator said is the thing that holds.

    Only an EFFECTIVE ``revoked`` row tombstones (including a healed repair
    row, which is an effective row by construction). A bare revoke INTENT
    drops the name from ``active`` — round 5's fail-toward-less-reach rule —
    but must NOT tombstone: if that revoke actually failed at the config
    write, tombstoning would strip a blueprint-owned bundle the operator
    never successfully removed. Declining to preserve an uncertain grant is
    cheap; deleting reach on an uncertain revoke is not.

    **Admission — what a row must be to count at all.** A row moves this
    replay only when all three hold:

    1. its schema is complete and correctly typed (see :func:`_row_text`);
    2. its operator-turn provenance is nonblank — actor, role, trigger text,
       surface, channel id, operation — the same fields the executor refuses
       to act without;
    3. an effective ``granted`` / ``revoked`` row CORRELATES to a preceding
       ``intent`` row for the same correlation id, persona, toolset, and
       operation.

    Rule 3 is the one that closes the cheap forgery. The executor cannot
    produce an uncorrelated effective row — it appends intent before it
    touches the config — so a lone ``{"persona_id":…,"toolset":…,
    "outcome":"granted"}`` is not a record of anything that happened, and it
    used to contribute reach. A healed repair row satisfies the same rule
    honestly: it carries the TORN attempt's correlation id, which is exactly
    a preceding intent.

    **What this is and is not.** The ledger is a RECORD, not an authority.
    These checks defend the replay against CORRUPT and PARTIAL rows — a torn
    write, a truncated line, a half-formed row from a crash — so that
    provisioning derives scope only from turns that demonstrably happened.
    They are NOT a security boundary against someone who can write files:
    anyone who can append to the ledger can also edit ``config.yaml``
    directly, which needs no forgery at all. Raising the bar to "looks like
    a real operator turn, with its intent" is the useful, honest limit.
    """
    persona = str(persona_id or "").strip()
    if not persona:
        return LedgerScope((), ())
    held: list[str] = []
    tombstoned: list[str] = []
    seen_intents: set[tuple[str, str, str, str]] = set()
    rejected = 0
    # Keyed to the persona being replayed, never to the ambient profile —
    # otherwise a reconcile running as the default profile reads a different
    # file than the persona bot that wrote the grants.
    for row in _read_ledger_rows(resolve_ledger_path(audit_path, persona)):
        if _row_text(row, "persona_id") != persona:
            continue
        outcome = _row_text(row, "outcome")
        name = _row_text(row, "toolset")
        if not name or not outcome:
            continue
        if outcome not in (OUTCOME_GRANTED, OUTCOME_REVOKED, OUTCOME_INTENT):
            continue
        # ── Admission. A row only moves the replay if it looks like something
        # this executor actually wrote: complete schema, real operator-turn
        # provenance, and — for an effective row — a PRECEDING intent for the
        # same attempt. A short, correctly-typed row with blank provenance is
        # the cheapest forgery there is, and it used to count.
        key = _attempt_key(row)
        if key is None or not _has_provenance(row):
            rejected += 1
            continue
        if outcome == OUTCOME_INTENT:
            seen_intents.add(key)
            if key[3] == OPERATION_REVOKE and name in held:
                # Drops the grant without waiting for the outcome row (round
                # 5), but deliberately does NOT tombstone — see the docstring.
                held.remove(name)
            continue
        if key not in seen_intents:
            # No intent for this attempt. The executor cannot produce that
            # ordering — it appends intent BEFORE it touches the config —
            # so an uncorrelated effective row is not a record of anything
            # that happened. Repair rows correlate too, by carrying the torn
            # attempt's id, so they are admitted here on the same rule.
            rejected += 1
            continue
        if outcome == OUTCOME_GRANTED:
            if name not in held:
                held.append(name)
            if name in tombstoned:
                tombstoned.remove(name)
        else:
            if name in held:
                held.remove(name)
            if name not in tombstoned:
                tombstoned.append(name)
    if rejected:
        _logger.warning(
            "personas.toolset_grants: ignored %d ledger row(s) for %s with "
            "incomplete provenance or no correlated intent",
            rejected,
            persona,
        )
    return LedgerScope(tuple(held), tuple(tombstoned))


def orphan_intent_correlation(
    persona_id: str,
    toolset: str,
    operation: str,
    audit_path: Path | str | None = None,
) -> str:
    """Correlation id of an *operation* that mutated the config but never recorded it.

    Returns the most recent :data:`OUTCOME_INTENT` row for
    *persona_id*/*toolset*/*operation* that has NO row of its own resolving
    it, or ``""`` when every intent found its outcome.

    ``granted`` / ``revoked`` resolve an intent as completed; ``error``
    resolves it as failed (the config write raised, so nothing moved). Only a
    row with none of those is the torn case: the atomic replace succeeded and
    the outcome append did not.

    Symmetric across both operations by construction — a grant and a revoke
    tear the same way, so both retry paths heal through this one reader.

    Fail-open like :func:`active_grants` — an unreadable or malformed ledger
    reports no orphan rather than raising into a caller's retry.
    """
    persona = str(persona_id or "").strip()
    name = str(toolset or "").strip()
    wanted = str(operation or "").strip()
    if not persona or not name or not wanted:
        return ""

    pending: list[str] = []
    resolved: set[str] = set()
    for row in _read_ledger_rows(resolve_ledger_path(audit_path, persona)):
        # Type-checked, never coerced — same reason as the replay above: a
        # hostile row must not be able to hand this loop an unbounded value.
        if _row_text(row, "persona_id") != persona:
            continue
        if _row_text(row, "toolset") != name:
            continue
        if _row_text(row, "operation") != wanted:
            continue
        # Same admission rule as the replay. Without it a forged intent row
        # could make a retry write a repair row for an attempt that never
        # happened — and the replay would reject that repair anyway, leaving
        # a confusing row behind. Refuse to start.
        if not _has_provenance(row):
            continue
        correlation = _row_text(row, "correlation_id")
        if not correlation:
            continue
        outcome = _row_text(row, "outcome")
        if outcome == OUTCOME_INTENT:
            pending.append(correlation)
        elif outcome in _RESOLVING_OUTCOMES:
            resolved.add(correlation)

    for correlation in reversed(pending):
        if correlation not in resolved:
            return correlation
    return ""


__all__ = [
    "ADMIN_ROLE",
    "INTEGRATION",
    "LEDGER_FILENAME",
    "OPERATION_GRANT",
    "OPERATION_PROPOSE",
    "OPERATION_REVOKE",
    "OUTCOME_ALREADY_GRANTED",
    "OUTCOME_APPROVED",
    "OUTCOME_DENIED",
    "OUTCOME_ERROR",
    "OUTCOME_EXPIRED",
    "OUTCOME_GRANTED",
    "OUTCOME_INTENT",
    "OUTCOME_NOT_GRANTED",
    "OUTCOME_PROPOSED",
    "OUTCOME_REFUSED",
    "OUTCOME_REVOKED",
    "REASON_CONFIG_SHAPE",
    "REASON_DEFAULT_PROFILE_UNSUPPORTED",
    "REASON_INVALID_PERSONA",
    "REASON_INVALID_TOOLSET",
    "REASON_KILL_SWITCH",
    "REASON_MISSING_OPERATOR_TURN",
    "REASON_NOT_AUTHORIZED",
    "REASON_UNKNOWN_PERSONA",
    "REASON_LOCK_TIMEOUT",
    "REASON_REPAIR_CONFIG_ABSENT",
    "REASON_REPAIR_CONFIG_PRESENT",
    "REASON_UNKNOWN_TOOLSET",
    "REASON_WRITE_FAILED",
    "LedgerScope",
    "ToolsetGrantAuditError",
    "ToolsetGrantRefusedError",
    "ToolsetGrantResult",
    "active_grants",
    "append_audit_record",
    "audit_attempt",
    "describe_toolset",
    "known_toolset_names",
    "ledger_scope",
    "nearest_names",
    "new_correlation_id",
    "normalize_trigger_text",
    "orphan_intent_correlation",
    "resolve_ledger_path",
]
