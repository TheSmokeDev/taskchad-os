"""Linked-skill intake — "if I link a skill, add it to your shape" (#429).

Epic #419's fourth piece. The operator drops a skill at a persona (a repo /
docs URL, or a local path) and it becomes something THAT persona can use,
in one turn — without the operator hand-editing YAML, and without anything
skipping the security scan.

This module ORCHESTRATES; it owns no new lifecycle:

    role gate -> cognition.skill_learn.learn_skill   (existing ingest, draft
                                                      lands inert in generated/)
              -> cognition.skill_promotion.promote   (existing scan gate +
                                                      default-deny promote)
              -> personas.skill_assignment           (new: install for the
                 .assign_skill_to_persona             requesting persona only)

Three properties the ticket is graded on, and where each lives:

* **The scan stays.** ``promote`` re-runs ``scan_skill`` and refuses the
  blocking verdict. This module passes ``override_caution=False`` and offers
  NO bypass flag: a scan failure comes back as a refusal that NAMES the
  verdict and the findings. The draft is still on disk under ``generated/``,
  which every index excludes by path segment, so a refused skill has landed
  in nobody's surface — the operator can inspect it and, if they judge it
  safe, use the existing explicit ``/skills promote <name> --override-caution``
  two-step. That path is the operator's own decision on the existing
  surface, not a bypass wired into intake.
* **Operator-only.** The admin role gate runs BEFORE ingest, so a stranger's
  link is never fetched, never distilled, never written. Refusal is audited.
  ``actor_role`` is CHECKED here and must be resolved server-side by the
  caller from the authenticated surface — never from anything a payload or a
  model asserted.
* **Requesting persona only (Q5).** The install target is the persona whose
  channel carried the operator's turn. Org-wide assignment is an explicit
  operator choice and a named follow-up; there is no flag for it here. The
  restriction is COMMITTED BEFORE the skill is published: the scope row is
  written first, ``promote`` then moves the artifact into the shared central
  tree, and any failure after that point rolls the move back. Nothing is ever
  reachable while its scope is unrecorded, and a refusal never leaves an
  artifact behind (or, if the rollback itself fails, says so and names the
  path).

Every exit returns a :class:`SkillIntakeResult` rather than raising — the
caller is a chat turn, and a refusal is an answer, not an error — with ONE
deliberate exception: ``security.kill_switches.KillSwitchDisabled`` from the
assignment executor PROPAGATES (house rule), after the same rollback and
scope-undo a refusal would run. Refusals and outcomes are audited to the
requesting persona's assignment ledger, so one grep answers "what was linked
at this homie, by whom, and what was refused".
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Only a LINK is an intake. "this conversation" and pasted notes are
# ``/learn``'s job — they are authored content, not something the operator
# pointed at, and routing them here would quietly turn a chat message into an
# auto-promoted skill.
_LINK_KINDS = ("url", "path")

OUTCOME_ASSIGNED = "assigned"
OUTCOME_ALREADY_ASSIGNED = "already_assigned"
OUTCOME_ALREADY_REACHABLE = "already_reachable"
OUTCOME_REFUSED = "refused"

REASON_INVALID_SOURCE = "invalid_source"
REASON_NOT_A_LINK = "not_a_link"
REASON_MISSING_OPERATOR_TURN = "missing_operator_turn"
REASON_NOT_AUTHORIZED = "not_authorized"
REASON_INGEST_FAILED = "ingest_failed"
REASON_ASSIGN_FAILED = "assign_failed"
REASON_SCOPE_UNRECORDED = "scope_unrecorded"

# Promote statuses that mean the skill is vetted and physically in the
# central promoted/ tree. Everything else is a refusal whose reason is the
# status verbatim, so the operator sees which gate said no.
_PROMOTE_OK = ("promoted", "already_promoted")

_PROMOTE_REFUSAL_TEXT = {
    "scan_dangerous": "the security scan returned DANGEROUS",
    "scan_caution": "the security scan returned CAUTION",
    "killswitch_disabled": "the skill_promotion kill-switch is disabled",
    "not_eligible": "the draft is not promotion-eligible",
    "not_found": "the staged draft could not be located on disk",
    "not_approved": "operator approval was not recorded",
    "scope_write_failed": (
        "the persona scope of the promotion could not be recorded, so nothing "
        "was published"
    ),
    "move_failed": "the promotion move failed",
    "draft_changed": (
        "the staged draft CHANGED on disk between its security scan and the "
        "promotion move, so the scanned bytes were never published — re-link "
        "to rescan the new content"
    ),
    "promote_target_invalid": "an existing promoted/ target is invalid",
    "promoted_name_collision": (
        "a DIFFERENT skill is already promoted under this name with "
        "different content — refused rather than silently keeping the old "
        "artifact"
    ),
}


@dataclass(frozen=True)
class SkillIntakeResult:
    """Outcome of one linked-skill intake, in terms a chat surface can speak."""

    ok: bool
    outcome: str
    message: str
    persona_id: str = ""
    skill_name: str = ""
    verdict: str = ""
    reason: str = ""
    draft_path: str = ""
    install_path: str = ""
    findings: tuple[str, ...] = field(default_factory=tuple)


def _audit(
    *,
    persona_id: str,
    skill_name: str,
    outcome: str,
    reason: str,
    actor: str,
    actor_role: str,
    surface: str,
    channel_id: str,
    trigger_text: str,
    source: str,
    verdict: str = "",
    error: str = "",
    audit_path: Path | str | None = None,
) -> str:
    """Best-effort intake row on the requesting persona's assignment ledger.

    Best-effort here and strict inside the executor is deliberate: the
    executor's refusal is the one a caller could mistake for "audited", so it
    turns an append failure into an error. An intake row is a breadcrumb for
    a decision the executor either never saw (role gate) or already recorded
    itself — a lost breadcrumb must not cost the operator the answer. Every
    swallow leaves a receipt.
    """
    try:
        from personas import skill_assignment
    except Exception as exc:  # noqa: BLE001 — receipt, never break the turn
        logger.warning(
            "skill_intake: assignment ledger unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return ""
    # #429 codex R3 MAJOR: ``persona_id`` arrives here UNVALIDATED — several
    # refusals (not-a-link, not-authorized, missing operator turn) fire before
    # ``validate_persona_name`` ever runs, and the persona id can come from a
    # corrupted channel binding (``../../escaped-target``). ``resolve_ledger_path``
    # joins an unchecked id straight onto ``<root>/profiles/<id>/data`` and the
    # append then mkdirs OUTSIDE the profiles tree. Route every intake row
    # through the same containment discipline the assignment executor uses: a
    # persona that fails validation never reaches ``get_persona_paths``, even
    # to record its own refusal.
    safe_path = skill_assignment.safe_audit_path(persona_id, audit_path)
    return skill_assignment.audit_attempt(
        operation=skill_assignment.OPERATION_INTAKE,
        persona_id=persona_id,
        skill_name=skill_name,
        outcome=outcome,
        reason=reason,
        actor=actor,
        actor_role=actor_role,
        surface=surface,
        channel_id=channel_id,
        trigger_text=trigger_text,
        source=source,
        verdict=verdict,
        error=error,
        audit_path=safe_path,
    )


def _record_persona_scope(skill_name: str, persona: str) -> tuple[bool, bool]:
    """Record "this promotion is for *persona*" — returns ``(recorded, added)``.

    Blocking sidecar RMW; call it through ``asyncio.to_thread``.

    ``recorded`` False means the scope could NOT be written — no row to write
    it on, or the write itself failed. The caller must refuse there and
    publish nothing: an unrecorded scope is not "no restriction needed", it is
    a restriction we would be unable to enforce (#429 design gate B2, seam 1).
    ``added`` distinguishes a scope this turn appended from one an earlier link
    already recorded, so a rollback removes only what it put there.

    The check-and-append is ONE atomic claim (#429 codex R3 BLOCKER): the old
    split ``get_usage`` -> ``record_persona_assignment`` let two concurrent
    same-persona intakes both read "absent" and both return ``added=True`` —
    the loser's rollback then removed the scope the winner was still relying
    on. Under the atomic claim exactly one caller gets ``added=True``, and a
    caller with ``added=False`` never undoes anything.
    """
    try:
        from cognition import skill_usage

        usage, added = skill_usage.claim_persona_assignment(skill_name, persona)
    except Exception as exc:  # noqa: BLE001 — refusal, never a traceback
        logger.warning(
            "skill_intake: persona scope write failed for %r -> %r: %s",
            skill_name,
            persona,
            exc,
        )
        return False, False
    if usage is None or persona not in (usage.assigned_personas or []):
        return False, False
    return True, added


def _undo_persona_scope(skill_name: str, persona: str) -> None:
    """Drop a scope this turn recorded for a link that then did not publish."""
    try:
        from cognition import skill_usage

        skill_usage.remove_persona_assignment(skill_name, persona)
    except Exception as exc:  # noqa: BLE001 — receipt, never break the refusal
        logger.warning(
            "skill_intake: could not undo persona scope for %r -> %r: %s",
            skill_name,
            persona,
            exc,
        )


def _admin_role() -> str:
    """The role name the epic's identity gate requires, read at call time.

    Read through ``personas.toolset_grants`` so intake and the #426 executor
    can never disagree about what "operator" means. Falls back to the literal
    only when that module cannot be imported at all.
    """
    try:
        from personas import toolset_grants
    except Exception:  # noqa: BLE001 — the gate must still exist
        return "admin"
    return str(getattr(toolset_grants, "ADMIN_ROLE", "admin"))


def _is_kill_switch_disabled(exc: BaseException) -> bool:
    """True iff *exc* is the security kill-switch's disabled signal.

    Module-attribute lookup (Rule 3) so a test patching
    ``security.kill_switches.KillSwitchDisabled`` — or the module's absence
    outside the scripts env — never breaks the check.
    """
    try:
        from security import kill_switches
    except Exception:  # noqa: BLE001 — no kill-switch module, no kill-switch raise
        return False
    return isinstance(exc, kill_switches.KillSwitchDisabled)


_INTAKE_LOCKS: dict[str, threading.Lock] = {}
_INTAKE_LOCKS_GUARD = threading.Lock()


def _intake_lifecycle_lock(skill_name: str) -> threading.Lock:
    """One lock per canonical skill SLUG (#429 codex R4 + R5 BLOCKERS).

    The scope claim, the promote, the install, and any rollback are ONE
    serialized transaction per skill. The atomic claim (R3) stopped two
    intakes from both claiming the scope, but the original claimant could
    still fail AFTER a same-name intake had installed, and its rollback then
    stripped the scope the winner relied on — and an empty row reads as
    legacy/unrestricted, so the artifact went global.

    The key is the SANITIZED slug — the grain storage uses (#429 codex R5):
    ``Daily Spend`` and ``daily-spend`` fold onto one promoted directory,
    so two raw names that collide on disk must share ONE lock. The raw name
    is the fallback when the sanitizer is unimportable — a degraded key,
    never NO lock.
    """
    try:
        from cognition import skill_guard

        key = skill_guard.sanitize_skill_path_component(skill_name)
    except Exception:  # noqa: BLE001 — degraded key, never lockless
        key = skill_name
    with _INTAKE_LOCKS_GUARD:
        return _INTAKE_LOCKS.setdefault(key, threading.Lock())


async def intake_linked_skill(
    source: str,
    *,
    persona_id: str,
    actor: str,
    actor_role: str,
    trigger_text: str,
    surface: str,
    channel_id: str,
    transcript: str = "",
    cwd: Path | None = None,
    skills_dir: Path | None = None,
    audit_path: Path | str | None = None,
) -> SkillIntakeResult:
    """Ingest a linked skill, scan-gate it, and assign it to *persona_id*.

    *source* must be a link the operator pointed at — an ``http(s)://`` URL
    or a local path. *actor_role* must be resolved server-side from the
    authenticated surface.

    Never raises — every gate, every lifecycle refusal, and every unexpected
    runtime failure comes back as a :class:`SkillIntakeResult` with
    ``ok=False`` and a ``reason`` naming what stopped it — EXCEPT
    ``KillSwitchDisabled`` from the assignment executor, which propagates by
    house rule (the operator's OFF switch is never reported as a generic
    failure); the publish rollback and scope undo still run before it rises.
    """
    persona = str(persona_id or "").strip()
    raw_source = str(source or "").strip()
    who = str(actor or "").strip()
    role = str(actor_role or "").strip().lower()
    trigger = str(trigger_text or "").strip()
    surface_name = str(surface or "").strip()
    channel = str(channel_id or "").strip()

    async def _refuse(
        reason: str, message: str, *, verdict: str = "", skill: str = ""
    ) -> SkillIntakeResult:
        # M4 (#429 round-2 MAJOR): `_audit` performs synchronous mkdir/open/
        # write JSONL I/O. Called inline (not awaited) from an async chat-turn
        # function, that I/O ran ON the router's event loop for every single
        # refusal — including the identity gate, which fires for EVERY
        # stranger's message. Off-loop it like the ingest/promote/assign
        # calls already are.
        await asyncio.to_thread(
            _audit,
            persona_id=persona,
            skill_name=skill,
            outcome=OUTCOME_REFUSED,
            reason=reason,
            actor=who,
            actor_role=role,
            surface=surface_name,
            channel_id=channel,
            trigger_text=trigger or raw_source,
            source=raw_source,
            verdict=verdict,
            error=message,
            audit_path=audit_path,
        )
        return SkillIntakeResult(
            ok=False,
            outcome=OUTCOME_REFUSED,
            message=message,
            persona_id=persona,
            skill_name=skill,
            verdict=verdict,
            reason=reason,
        )

    # ── Contract + identity gates. All BEFORE any fetch, distill, or write:
    # a stranger's link must never be requested, let alone reasoned over.
    if not raw_source:
        return await _refuse(
            REASON_INVALID_SOURCE,
            "refused: nothing linked — give me a skill URL or a local path.",
        )
    if not persona:
        return await _refuse(
            REASON_INVALID_SOURCE,
            "refused: no requesting persona resolved — run this from a "
            "persona's channel so I know whose kit it goes in.",
        )
    if not who or not trigger or not surface_name or not channel:
        return await _refuse(
            REASON_MISSING_OPERATOR_TURN,
            "refused: a skill install needs the live operator turn that "
            "ordered it (actor + trigger_text + surface + channel_id).",
        )
    if role != _admin_role():
        return await _refuse(
            REASON_NOT_AUTHORIZED,
            f"refused: linking a skill requires the {_admin_role()} role, "
            f"got {role or 'none'!r}. Nothing was fetched or installed.",
        )

    from cognition import skill_learn

    # Off-loop (#429 codex R4 MAJOR): parse_source's path probe can block on
    # an SMB share; a Windows timeout there must never stall this loop.
    parsed = await asyncio.to_thread(skill_learn.parse_source, raw_source)
    if parsed.kind not in _LINK_KINDS:
        return await _refuse(
            REASON_NOT_A_LINK,
            f"refused: {raw_source!r} is not a link — linked-skill intake "
            "takes an http(s) URL or a local path. Use `/learn` to author a "
            "skill from notes or from this conversation.",
        )

    # ── Ingest through the EXISTING /learn rails: the draft lands inert under
    # generated/, which every skill index excludes by path segment.
    try:
        learned = await skill_learn.learn_skill(
            raw_source,
            transcript=transcript,
            cwd=cwd,
            skills_dir=skills_dir,
            source_session=f"linked-skill:{persona}",
        )
    except Exception as exc:  # noqa: BLE001 — a chat turn gets an answer, not a traceback
        logger.warning("skill_intake: ingest raised for %r: %s", raw_source, exc)
        return await _refuse(
            REASON_INGEST_FAILED,
            f"refused: could not ingest {raw_source!r} ({type(exc).__name__}: {exc}).",
        )

    if not learned.ok or not learned.skill_name:
        return await _refuse(
            REASON_INGEST_FAILED,
            learned.message or f"refused: nothing readable at {raw_source!r}.",
        )

    skill_name = learned.skill_name
    findings = tuple(learned.findings or ())


    # ── Lifecycle lock (#429 codex R4 BLOCKER): the scope claim, the promote,
    # the install, and any rollback are ONE serialized transaction per
    # canonical skill SLUG — a winner's commit and a loser's rollback can
    # never interleave.
    _lifecycle = _intake_lifecycle_lock(skill_name)
    # Non-blocking acquire with a loop-side yield (#429 codex R5 MAJOR):
    # ``to_thread(lock.acquire)`` parks a WORKER thread on the lock, and a
    # cancelled chat turn can never signal that thread — it acquires the
    # lock LATER and nothing ever releases it, jamming this skill name
    # until process restart. A cancelled poller simply never holds it.
    while not _lifecycle.acquire(blocking=False):
        await asyncio.sleep(0.05)
    try:
        from cognition import skill_promotion

        # ── Scope BEFORE publication (#429 design gate B1).
        #
        # ``promote`` physically moves the draft into the SHARED central
        # ``promoted/`` tree, and the ``default`` profile scans that tree with an
        # unrestricted allowlist — so the move is the instant this skill becomes
        # readable by a homie nobody named. The restriction therefore has to be on
        # record BEFORE the move, not after it: the old order (promote -> install
        # -> best-effort scope) meant every ordinary post-promote failure (the
        # persona_mutation kill-switch, a typo'd persona, a lock timeout, an
        # OSError) returned "refused: nothing installed" while the skill was live
        # and unscoped in the main homie's index.
        #
        # This write is REQUIRED. A scope that cannot be recorded is a restriction
        # that cannot be enforced, so it refuses here — with nothing published.
        scope_recorded, scope_added = await asyncio.to_thread(
            _record_persona_scope, skill_name, persona
        )
        if not scope_recorded:
            return await _refuse(
                REASON_SCOPE_UNRECORDED,
                f"refused: could not record that {skill_name!r} is for {persona!r} "
                "only, so nothing was promoted or installed. The draft is staged "
                "under `skills/generated/`, which no skill index reads.",
                skill=skill_name,
            )

        async def _refuse_scoped(
            reason: str,
            message: str,
            *,
            verdict: str = "",
            published: bool = False,
        ) -> SkillIntakeResult:
            """Refuse AFTER the scope write — taking the visible state back first.

            ``published`` means THIS call moved the artifact into ``promoted/``
            (never an ``already_promoted`` one: that belongs to an earlier
            operator decision and to whichever personas already use it). The
            rollback runs before the scope is dropped so the artifact is never
            reachable-and-unscoped, and when it fails the operator is told the
            skill IS live centrally and where — an honest receipt beats a tidy
            one.
            """
            detail = message
            if published:
                outcome = await asyncio.to_thread(
                    skill_promotion.rollback_promotion,
                    skill_name,
                    learned.draft_path,
                    reason=f"intake_{reason}",
                )
                rollback_status = str(outcome.get("status", ""))
                if rollback_status in ("rolled_back", "absent"):
                    detail = (
                        f"{message}\nNothing was published — the draft is back "
                        "under `skills/generated/`, which no skill index reads."
                    )
                else:
                    detail = (
                        f"{message}\nWARNING: {skill_name!r} IS promoted at "
                        f"`{outcome.get('path', '')}` and could NOT be rolled back "
                        f"({rollback_status}). Remove that directory if you did not "
                        "want the skill there."
                    )
            if scope_added:
                await asyncio.to_thread(_undo_persona_scope, skill_name, persona)
            return await _refuse(reason, detail, verdict=verdict, skill=skill_name)

        # ── The scan gate. promote() re-scans the physical file and refuses the
        # blocking verdict; no override is offered from this surface.
        try:
            promoted = await asyncio.to_thread(
                skill_promotion.promote,
                skill_name,
                operator_approved=True,
                override_caution=False,
            )
        except Exception as exc:  # noqa: BLE001 — same reason as above
            logger.warning("skill_intake: promote raised for %r: %s", skill_name, exc)
            # A raise can land on either side of the physical move, so the
            # rollback is asked to look: it is a no-op (``absent``) when nothing
            # was published, and takes the artifact back when something was.
            return await _refuse_scoped(
                "promote_error",
                f"refused: the promotion gate errored for {skill_name!r} "
                f"({type(exc).__name__}: {exc}).",
                published=True,
            )

        status = str(promoted.get("status", "unknown"))
        verdict = str(promoted.get("verdict", "") or learned.verdict or "")
        promoted_path = str(promoted.get("path", ""))
        # Did THIS call publish the artifact? Only ``promoted`` moves it; the
        # reconciliation below can only yield ``already_promoted`` (an earlier
        # operator's promotion, never ours to roll back) or a refusal.
        published_here = status == "promoted"

        # ── Sidecar-vs-disk reconciliation (Rule 2), for ONE status only.
        #
        # ``promote`` gates on the usage sidecar, and a skill that was already
        # promoted reads ``not_eligible`` forever after — its eligibility gate
        # (step 2) runs BEFORE its own scan gate (step 4), so a same-name relink
        # with NEW content is never actually re-scanned by ``promote`` itself.
        # That is true about the counter and false about the world — the skill is
        # live in ``promoted/``. It is also the normal case for this feature: the
        # operator links the SAME skill at a SECOND persona, and without this
        # that persona never gets it. So a ``not_eligible`` is re-decided against
        # the FILESYSTEM — but ONLY after gating the CURRENT draft's own scan
        # first (round-2 BLOCKER: a scan-failing relink must refuse here, never
        # silently fall back to whatever already sits in ``promoted/`` while
        # reporting a verdict that does not match what actually shipped), and
        # only when the freshly-linked content is content-IDENTICAL to what is
        # already promoted (a DIFFERENT file sharing the name is a collision, not
        # a relink, and must never silently keep the old artifact).
        #
        # Scoped deliberately to state mismatches. A scan verdict is never
        # re-decided by ``promote`` itself here: this block only ever RAISES the
        # severity of ``not_eligible`` to a scan refusal, never lowers one.
        if status == "not_eligible":
            block_verdict = skill_promotion.resolve_block_verdict()
            current_verdict = str(learned.verdict or "").strip().lower()
            if current_verdict == block_verdict:
                status = "scan_dangerous"
            elif current_verdict == "caution":
                status = "scan_caution"
            else:
                try:
                    existing = await asyncio.to_thread(
                        skill_promotion.resolve_reusable_promoted_skill,
                        skill_name,
                        Path(learned.draft_path),
                    )
                except Exception as exc:  # noqa: BLE001 — fall through to the refusal
                    logger.warning(
                        "skill_intake: promoted-state check failed for %r: %s", skill_name, exc
                    )
                    existing = None
                if existing is not None:
                    status = "already_promoted"
                    promoted_path = str(existing)
                else:
                    status = "promoted_name_collision"

        if status not in _PROMOTE_OK:
            detail = _PROMOTE_REFUSAL_TEXT.get(status, f"the promotion gate returned {status}")
            finding_text = ("\n  • " + "\n  • ".join(findings[:5])) if findings else ""
            return await _refuse_scoped(
                status,
                f"refused: {skill_name!r} was NOT installed — {detail}"
                f"{f' (verdict: {verdict})' if verdict else ''}."
                f"{finding_text}\n"
                "The draft is staged under `skills/generated/`, which no skill "
                "index reads, so nothing reached the homie's surface. Inspect it "
                "and promote it explicitly with `/skills` if you judge it safe.",
                verdict=verdict,
            )

        if not promoted_path:
            return await _refuse_scoped(
                "promote_path_missing",
                f"refused: {skill_name!r} promoted but reported no path to install from.",
                verdict=verdict,
                published=published_here,
            )

        # ── Assign to the REQUESTING persona only (Q5).
        from personas import skill_assignment

        try:
            assignment = await asyncio.to_thread(
                skill_assignment.assign_skill_to_persona,
                persona,
                promoted_path,
                skill_name=skill_name,
                actor=who,
                actor_role=role,
                trigger_text=trigger,
                surface=surface_name,
                channel_id=channel,
                audit_path=audit_path,
            )
        except skill_assignment.SkillAssignmentRefusedError as exc:
            return await _refuse_scoped(
                exc.reason,
                f"refused: {exc}",
                verdict=verdict,
                published=published_here,
            )
        except Exception as exc:  # noqa: BLE001 — includes kill-switch + IO failures
            # House rule (#429 codex R3 MAJOR): KillSwitchDisabled PROPAGATES. The
            # persona_mutation switch is the operator's OFF control; folding it
            # into an ordinary ``assign_failed`` would report a generic failure
            # for what is really "you turned this surface off". The a716bfb3
            # contracts still hold before the raise: a promotion THIS call
            # published is rolled back, and a scope THIS call added is dropped,
            # so propagation never leaves the skill live-and-unscoped.
            if _is_kill_switch_disabled(exc):
                if published_here:
                    await asyncio.to_thread(
                        skill_promotion.rollback_promotion,
                        skill_name,
                        learned.draft_path,
                        reason="intake_killswitch_disabled",
                    )
                if scope_added:
                    await asyncio.to_thread(_undo_persona_scope, skill_name, persona)
                raise
            logger.warning(
                "skill_intake: assignment failed for %r -> %r: %s",
                skill_name,
                persona,
                exc,
            )
            return await _refuse_scoped(
                REASON_ASSIGN_FAILED,
                f"refused: {skill_name!r} passed the scan but could not be "
                f"installed for {persona!r} ({type(exc).__name__}: {exc}).",
                verdict=verdict,
                published=published_here,
            )

        # The persona scope is already on record — it was written BEFORE promote
        # published anything (see the scope gate above). Nothing to do here but
        # report; there is no window in which this skill was reachable without its
        # restriction recorded.
        install_path = str(assignment.install_path or "")
        await asyncio.to_thread(
            _audit,
            persona_id=persona,
            skill_name=skill_name,
            outcome=assignment.outcome,
            reason=assignment.reason,
            actor=who,
            actor_role=role,
            surface=surface_name,
            channel_id=channel,
            trigger_text=trigger,
            source=raw_source,
            verdict=verdict,
            audit_path=audit_path,
        )
        return SkillIntakeResult(
            ok=True,
            outcome=assignment.outcome,
            message=_success_message(
                skill_name=skill_name,
                persona=persona,
                outcome=assignment.outcome,
                verdict=verdict,
                source_kind=parsed.kind,
                install_path=install_path,
            ),
            persona_id=persona,
            skill_name=skill_name,
            verdict=verdict,
            reason=assignment.reason,
            draft_path=learned.draft_path,
            install_path=install_path,
            findings=findings,
        )
    finally:
        _lifecycle.release()


def _success_message(
    *,
    skill_name: str,
    persona: str,
    outcome: str,
    verdict: str,
    source_kind: str,
    install_path: str,
) -> str:
    icon = {"safe": "✅", "caution": "⚠️", "dangerous": "⛔"}.get(verdict, "•")
    if outcome == OUTCOME_ALREADY_ASSIGNED:
        headline = f"*{skill_name}* was already in *{persona}*'s kit — nothing changed."
    elif outcome == OUTCOME_ALREADY_REACHABLE:
        headline = (
            f"*{skill_name}* is promoted and already in *{persona}*'s index "
            "(the default profile reads the central skills dir)."
        )
    else:
        headline = f"Added *{skill_name}* to *{persona}*'s kit."
    return (
        f"{headline}\n"
        f"  • source: {source_kind}\n"
        f"  • security scan: {icon} {verdict or 'unknown'}\n"
        + (f"  • installed: `{install_path}`\n" if install_path else "")
        + "  • live on that homie's next turn."
    )


__all__ = [
    "OUTCOME_ALREADY_ASSIGNED",
    "OUTCOME_ALREADY_REACHABLE",
    "OUTCOME_ASSIGNED",
    "OUTCOME_REFUSED",
    "REASON_ASSIGN_FAILED",
    "REASON_INGEST_FAILED",
    "REASON_INVALID_SOURCE",
    "REASON_MISSING_OPERATOR_TURN",
    "REASON_NOT_A_LINK",
    "REASON_NOT_AUTHORIZED",
    "REASON_SCOPE_UNRECORDED",
    "SkillIntakeResult",
    "intake_linked_skill",
]
