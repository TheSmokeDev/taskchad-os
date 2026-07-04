"""Suggested scheduled jobs — proposed automations the operator accepts by hand.

A *suggestion* is a ready-to-run scheduled-job spec that The Homie surfaces to
the operator, who accepts it (creates the real scheduled task) or dismisses it
(latched so it is never re-offered). This is the single surface every automation
proposal flows through, regardless of where it came from:

  * ``catalog``  — a curated starter automation (daily briefing, important-mail
                   monitor, weekly review, vault sweep, ...).
  * ``blueprint`` — the operator filled an automation blueprint via
                   ``/blueprints <key> slot=val``; that registers a suggestion
                   instead of auto-scheduling.
  * ``usage``    — a background review noticed a recurring ask a scheduled job
                   would serve.
  * ``integration`` — the operator connected an account and the obvious
                   automations for that surface are offered.

Accepting a suggestion calls the injected ``create_fn`` with the stored
``job_spec`` — there is NO second job engine, and this module never imports the
creator directly (the chat handler injects an HTTP client that POSTs to the
guarded ``/api/scheduled`` path in a SEPARATE process). Suggestions never
auto-create jobs; acceptance is always explicit (consent-first, default-deny).
Dismissed suggestions latch by a stable ``dedup_key`` so the same proposal is
not re-offered after the operator says no.

Ported from Hermes v0.18 ``cron/suggestions.py`` (algorithm verbatim). Re-anchors
for The Homie:
  * storage: ``config.STATE_DIR / "suggestions.json"`` resolved at CALL time
    (Rule 1 — never bound at module scope; the persona resolver may re-root
    STATE_DIR mid-process, and a module-scope path also trips the install-dir
    AST audit).
  * concurrency: ``shared.file_lock`` (cross-process, Windows-safe) instead of a
    threading lock — the chat process and background reviews may both write.
  * timestamps: ``datetime.now().isoformat()`` instead of the Hermes clock.
  * accept: the creator is INJECTED (``create_fn``), not imported.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from shared import file_lock

logger = logging.getLogger(__name__)

# Cap pending suggestions so the list never becomes a nag wall. When full,
# new suggestions are dropped (the operator should clear the backlog first).
MAX_PENDING = 5

VALID_SOURCES = frozenset({"catalog", "blueprint", "usage", "integration"})
_STATUS_PENDING = "pending"
_STATUS_ACCEPTING = "accepting"  # in-flight claim: a create_fn is running for it
_STATUS_ACCEPTED = "accepted"
_STATUS_DISMISSED = "dismissed"

_LOCK_TIMEOUT_S = 5.0

# Stale-claim recovery TTL. An ``accepting`` row this old (or older) is treated
# as STRANDED — a process crashed between the claim and its commit/rollback — and
# is converted back to ``pending`` so the suggestion never becomes permanently
# invisible/unretryable. This MUST exceed the create path's own upper bound: the
# accept POST goes through scheduled_api's httpx client, whose DEFAULT_TIMEOUT_S
# is 10s, so a live in-flight accept cannot exceed ~10s before it raises and
# rolls back. 30s = 3x that ceiling: comfortably longer than any legitimate
# in-flight window (never recover a live claim), yet a bounded stranding window.
_ACCEPT_CLAIM_TTL_S = 30.0


def _suggestions_file() -> Path:
    """Resolve the suggestions store path at CALL time (Rule 1).

    ``config.STATE_DIR`` is read inside the body — never bound at module scope —
    so a persona profile swap that re-roots STATE_DIR mid-process is honored, and
    the install-dir AST audit stays clean.
    """
    import config

    return config.STATE_DIR / "suggestions.json"


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Harmless partial no-op on win32; keep the upstream shape.
        pass


def _load_raw() -> dict[str, Any]:
    path = _suggestions_file()
    if not path.exists():
        return {"suggestions": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("suggestions.json unreadable (%s); starting empty", e)
        return {"suggestions": []}
    if isinstance(data, dict) and isinstance(data.get("suggestions"), list):
        return data
    if isinstance(data, list):
        return {"suggestions": data}
    logger.warning("suggestions.json malformed; starting empty")
    return {"suggestions": []}


def _save_raw(suggestions: list[dict[str, Any]]) -> None:
    """Atomically persist the suggestion list (tmp + os.replace).

    Callers hold the ``file_lock`` for the load->modify->save cycle; this writer
    is never called outside a locked section for a mutation.
    """
    path = _suggestions_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".sugg_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"suggestions": suggestions, "updated_at": datetime.now().isoformat()},
                f,
                indent=2,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)  # atomic, cross-platform (Windows-safe)
        _secure_file(path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _claim_is_stale(accepting_at: str | None, now: datetime) -> bool:
    """True if an ``accepting`` claim timestamp is older than the recovery TTL.

    A missing or unparseable timestamp is treated as stale (recover it): a live
    claim always writes ``accepting_at``, so the only rows lacking a usable one
    are legacy/corrupt — better to re-offer than to strand.
    """
    if not accepting_at:
        return True
    try:
        claimed = datetime.fromisoformat(accepting_at)
    except (ValueError, TypeError):
        return True
    return (now - claimed).total_seconds() >= _ACCEPT_CLAIM_TTL_S


def _recover_stale_claims() -> int:
    """Convert stranded ``accepting`` rows back to ``pending`` under the store lock.

    A crash between the pending->accepting claim and its commit/rollback would
    otherwise leave the row ``accepting`` forever — invisible to ``list_pending``,
    unresolvable by ``get_suggestion``, and un-re-addable (dedup). This runs
    BEFORE any decision read (see ``load_suggestions`` / ``add_suggestion``) so a
    stale claim is healed before it can hide a suggestion. FRESH claims (younger
    than the TTL) are left untouched, preserving duplicate-create protection for
    live in-flight accepts. Returns the number of rows recovered.
    """
    now = datetime.now()
    with file_lock(_suggestions_file(), timeout=_LOCK_TIMEOUT_S):
        suggestions = _load_raw().get("suggestions", [])
        recovered = 0
        for s in suggestions:
            if s.get("status") != _STATUS_ACCEPTING:
                continue
            if _claim_is_stale(s.get("accepting_at"), now):
                s["status"] = _STATUS_PENDING
                s.pop("accepting_at", None)
                s.pop("claim_token", None)
                recovered += 1
        if recovered:
            logger.info("Recovered %d stale accept claim(s) back to pending", recovered)
            _save_raw(suggestions)
        return recovered


def load_suggestions() -> list[dict[str, Any]]:
    """Return all suggestion records (any status).

    Heals stranded ``accepting`` claims first (``_recover_stale_claims``) so
    every read-driven decision — list, resolve, accept, dismiss — sees a
    recovered stale claim as ``pending`` rather than an invisible zombie.
    """
    _recover_stale_claims()
    return _load_raw().get("suggestions", [])


def list_pending() -> list[dict[str, Any]]:
    """Return pending suggestions in creation order (oldest first)."""
    return [s for s in load_suggestions() if s.get("status") == _STATUS_PENDING]


def add_suggestion(
    *,
    title: str,
    description: str,
    source: str,
    job_spec: dict[str, Any],
    dedup_key: str,
) -> dict[str, Any] | None:
    """Register a pending suggestion. Returns the record, or None if skipped.

    Skipped when: the source is unknown, the same ``dedup_key`` was already
    dismissed or accepted (never re-offer), an identical pending suggestion
    exists, or the pending list is full (``MAX_PENDING``).

    ``job_spec`` is the ``/api/scheduled`` create body (``persona_id, prompt,
    schedule, next_run``); accepting passes it straight through the injected
    ``create_fn`` so there is no second schema to keep in sync.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"unknown suggestion source: {source!r}")
    if not title.strip() or not dedup_key.strip():
        raise ValueError("title and dedup_key are required")

    # Heal stranded claims first so the dedup decision below sees a recovered
    # stale row as pending (separate lock — never nested inside our own).
    _recover_stale_claims()

    with file_lock(_suggestions_file(), timeout=_LOCK_TIMEOUT_S):
        suggestions = _load_raw().get("suggestions", [])

        # Any prior record with this dedup_key latches: pending, mid-accept
        # (accepting), already accepted, or dismissed — never re-offer.
        for existing in suggestions:
            if existing.get("dedup_key") == dedup_key:
                return None

        pending_count = sum(1 for s in suggestions if s.get("status") == _STATUS_PENDING)
        if pending_count >= MAX_PENDING:
            logger.info("Suggestion backlog full (%d); dropping %r", MAX_PENDING, title)
            return None

        record = {
            "id": uuid.uuid4().hex[:12],
            "title": title.strip(),
            "description": description.strip(),
            "source": source,
            "job_spec": job_spec,
            "dedup_key": dedup_key.strip(),
            "status": _STATUS_PENDING,
            "created_at": datetime.now().isoformat(),
        }
        suggestions.append(record)
        _save_raw(suggestions)
        return record


def get_suggestion(ref: str) -> dict[str, Any] | None:
    """Resolve a suggestion by id, 1-based pending index, or title (exact)."""
    suggestions = load_suggestions()
    # By id.
    for s in suggestions:
        if s.get("id") == ref:
            return s
    # By 1-based pending index.
    if ref.isdigit():
        pending = [s for s in suggestions if s.get("status") == _STATUS_PENDING]
        idx = int(ref) - 1
        if 0 <= idx < len(pending):
            return pending[idx]
    # By exact title (case-insensitive).
    for s in suggestions:
        if s.get("title", "").lower() == ref.lower():
            return s
    return None


def _set_status(suggestion_id: str, status: str) -> bool:
    with file_lock(_suggestions_file(), timeout=_LOCK_TIMEOUT_S):
        suggestions = _load_raw().get("suggestions", [])
        changed = False
        for s in suggestions:
            if s.get("id") == suggestion_id:
                s["status"] = status
                s["resolved_at"] = datetime.now().isoformat()
                # A dismiss can land on an in-flight ``accepting`` row and must
                # win; clear the claim so the resolved record stays tidy.
                s.pop("accepting_at", None)
                s.pop("claim_token", None)
                changed = True
                break
        if changed:
            _save_raw(suggestions)
        return changed


def _claim_for_accept(suggestion_id: str) -> str | None:
    """Atomically claim a PENDING row for accept — pending -> accepting.

    Returns a unique claim token on success, or None if the row is not pending
    (lost race / not found / already resolved). The token + ``accepting_at``
    timestamp are written in the SAME locked transaction: exactly one racer wins
    the claim, and the timestamp lets ``_recover_stale_claims`` heal a crash.
    """
    token = uuid.uuid4().hex
    with file_lock(_suggestions_file(), timeout=_LOCK_TIMEOUT_S):
        suggestions = _load_raw().get("suggestions", [])
        for s in suggestions:
            if s.get("id") == suggestion_id:
                if s.get("status") != _STATUS_PENDING:
                    return None
                s["status"] = _STATUS_ACCEPTING
                s["accepting_at"] = datetime.now().isoformat()
                s["claim_token"] = token
                _save_raw(suggestions)
                return token
        return None


def _resolve_claim(suggestion_id: str, token: str, *, to: str) -> bool:
    """CAS an ``accepting`` row we OWN (matching ``token``) to ``to``.

    Compare-and-set on BOTH status==accepting AND claim_token==token, so the
    commit/rollback is a no-op when the row was:
      * dismissed mid-flight (status changed) — the dismiss is preserved; or
      * recovered as stale and re-claimed by another caller (token changed) —
        our late write cannot clobber the new owner (ABA-safe).
    Clears the claim metadata on success. Returns True iff it transitioned.
    """
    with file_lock(_suggestions_file(), timeout=_LOCK_TIMEOUT_S):
        suggestions = _load_raw().get("suggestions", [])
        for s in suggestions:
            if s.get("id") == suggestion_id:
                if s.get("status") != _STATUS_ACCEPTING or s.get("claim_token") != token:
                    return False
                s["status"] = to
                s.pop("accepting_at", None)
                s.pop("claim_token", None)
                if to == _STATUS_ACCEPTED:
                    s["resolved_at"] = datetime.now().isoformat()
                _save_raw(suggestions)
                return True
        return False


def dismiss_suggestion(ref: str) -> bool:
    """Dismiss a suggestion (latched — never re-offered for its dedup_key)."""
    s = get_suggestion(ref)
    if not s:
        return False
    return _set_status(s["id"], _STATUS_DISMISSED)


def accept_suggestion(
    ref: str,
    *,
    create_fn: Callable[[dict[str, Any]], Any],
    origin: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Accept a suggestion: create the real scheduled job from its ``job_spec``.

    ``create_fn`` (REQUIRED) is a synchronous callable ``(spec: dict) -> job``.
    An ``origin`` (platform/chat) is merged into the spec so "origin" delivery
    routes back to where the operator accepted.

    Returns the created job, or None if the suggestion isn't found / not pending
    / already being accepted by a concurrent caller (the loser of the race — the
    winner ran ``create_fn`` exactly once). If ``create_fn`` raises, the claim is
    rolled back to pending and the exception propagates (the caller translates it
    to a friendly refusal) — the guard runs server-side inside the creator, so a
    blocked spec never latches as accepted.
    """
    # get_suggestion heals stale claims first, so a crash-stranded row is seen
    # as pending here rather than an invisible zombie.
    s = get_suggestion(ref)
    if not s or s.get("status") != _STATUS_PENDING:
        return None
    sid = s["id"]

    spec = dict(s.get("job_spec") or {})
    if origin is not None and "origin" not in spec:
        spec["origin"] = origin

    # Atomic claim — exactly one racer flips pending -> accepting and gets the
    # token; the loser gets None and does NOT call create_fn (create runs once).
    token = _claim_for_accept(sid)
    if token is None:
        return None

    try:
        job = create_fn(spec)
    except BaseException:
        # Guard refusal / any failure: roll our claim back so the proposal is
        # offered again. Token-CAS so a racing dismiss (or a stale-recovery
        # re-claim) is never clobbered.
        _resolve_claim(sid, token, to=_STATUS_PENDING)
        raise

    # Commit only if still OUR claim — a racing dismiss or re-claim is preserved.
    _resolve_claim(sid, token, to=_STATUS_ACCEPTED)
    return job


async def accept_suggestion_async(
    ref: str,
    *,
    create_fn: Callable[[dict[str, Any]], Awaitable[Any]],
    origin: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Async twin of ``accept_suggestion`` — ``create_fn`` is awaited.

    Identical claim/latch semantics: an atomic pending -> accepting claim gates
    entry (loser returns None, create runs once); on success the claim commits
    to accepted; if the awaited ``create_fn`` raises, the claim rolls back to
    pending and the exception propagates. The chat handler uses this form with an
    ``httpx.AsyncClient`` POST to ``/api/scheduled``.
    """
    s = get_suggestion(ref)
    if not s or s.get("status") != _STATUS_PENDING:
        return None
    sid = s["id"]

    spec = dict(s.get("job_spec") or {})
    if origin is not None and "origin" not in spec:
        spec["origin"] = origin

    # Atomic claim — see accept_suggestion. The claim is synchronous (no await
    # between the pending read and the CAS), so it is indivisible on the loop.
    token = _claim_for_accept(sid)
    if token is None:
        return None

    try:
        job = await create_fn(spec)
    except BaseException:
        _resolve_claim(sid, token, to=_STATUS_PENDING)
        raise

    _resolve_claim(sid, token, to=_STATUS_ACCEPTED)
    return job


def clear_resolved() -> int:
    """Drop accepted records from disk. Returns the count removed.

    Dismissed records must be RETAINED for their dedup_key (so they are not
    re-offered). Only ACCEPTED records are pruned — they have served their
    purpose once the scheduled task exists.
    """
    with file_lock(_suggestions_file(), timeout=_LOCK_TIMEOUT_S):
        suggestions = _load_raw().get("suggestions", [])
        kept = [s for s in suggestions if s.get("status") != _STATUS_ACCEPTED]
        removed = len(suggestions) - len(kept)
        if removed:
            _save_raw(kept)
        return removed
