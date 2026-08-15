"""Operator-gated promotion + audit for self-authored skills (WS3 / Rails 2 & 4).

A drafted skill flows: draft (inert, in ``generated/``) -> recurrence telemetry
(WS2) -> *this gate* -> live in the prompt. Promotion is the moment a model-written
instruction is allowed to shape the agent's behavior, so it is default-deny and
multiply gated:

    kill-switch -> reuse-eligibility (physical sidecar, Rule 2) -> draft located
        -> security scan (WS1) -> operator approval -> physical move out of
        ``generated/`` -> mark promoted -> audit.

Every decision (promote / reject / each refusal / scan-preview / stale-archive)
writes its OWN audit row via ``skill_audit`` (B6). The physical move (NOT a flag
flip) is what re-includes the skill in ``build_skill_index`` / ``discover_skills``,
which filter by the ``generated`` path segment (Rule 2 — path is source of truth).

Design invariants:
- Rule 1 — config (threshold/skills-dir) resolved at CALL TIME inside the body.
- Rule 2 — eligibility/state read from the physical usage sidecar + disk.
- Rule 3 — ``kill_switches`` used via module-attribute lookup so tests can monkeypatch.
- Fail-open audit — an audit-write failure never aborts the security decision.
- This is an INTERNAL mutation: gated by command + kill-switch + audit, NOT
  registered in ``integrations/capabilities.py`` (that registry is external-API only).
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from uuid import uuid4

from cognition import skill_usage
from cognition.skill_guard import sanitize_skill_path_component, scan_skill
from cognition.skills import _parse_skill_frontmatter

from security import (
    kill_switches,  # Rule 3 — module-attr lookup, never `from ... import requireEnabled`
)

logger = logging.getLogger(__name__)

_DEFAULT_PROMOTE_REUSE_THRESHOLD = 3
_DEFAULT_SCAN_BLOCK_VERDICT = "dangerous"

_KILLSWITCH_NAME = "skill_promotion"


# --------------------------------------------------------------------------- #
# Call-time resolvers (Rule 1) — never bind these at import.
# --------------------------------------------------------------------------- #


def _resolve_threshold(threshold: int | None = None) -> int:
    """Resolve the reuse threshold at CALL TIME (Rule 1, None-sentinel)."""
    if threshold is not None:
        return int(threshold)
    try:
        from config import SKILL_PROMOTE_REUSE_THRESHOLD

        return int(SKILL_PROMOTE_REUSE_THRESHOLD)
    except Exception:
        return _DEFAULT_PROMOTE_REUSE_THRESHOLD


def _resolve_block_verdict() -> str:
    """Resolve the scan verdict that BLOCKS promotion at CALL TIME (Rule 1).

    Defaults to ``"dangerous"``. Read via ``config`` so an env override /
    ``monkeypatch.setenv("SKILL_SCAN_BLOCK_VERDICT", ...)`` takes effect on the
    next call (the knob is resolved through ``config.__getattr__``, PEP 562).
    """
    try:
        from config import SKILL_SCAN_BLOCK_VERDICT

        return str(SKILL_SCAN_BLOCK_VERDICT).strip() or _DEFAULT_SCAN_BLOCK_VERDICT
    except Exception:
        return _DEFAULT_SCAN_BLOCK_VERDICT


def _resolve_skills_dir() -> Path:
    """Resolve the skills root (``.claude/skills``) at CALL TIME (Rule 1).

    Read ``config.CLAUDE_DIR`` by attribute access so a test's
    ``monkeypatch.setattr(config, "CLAUDE_DIR", ...)`` redirects every path
    derived from it on the next call. ``generated/`` and ``promoted/`` are its
    children.
    """
    try:
        import config

        return Path(config.CLAUDE_DIR) / "skills"
    except Exception:  # pragma: no cover - import path fallback for direct scripts
        return Path(__file__).resolve().parents[2] / "skills"


def _generated_root(skills_dir: Path) -> Path:
    return skills_dir / "generated"


def _promoted_root(skills_dir: Path) -> Path:
    return skills_dir / "promoted"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _audit(
    action: str,
    skill_name: str,
    outcome: str,
    *,
    verdict: str = "",
    reason: str = "",
) -> None:
    """Fail-open audit emission — never raise into the gate (B6)."""
    try:
        from skill_audit import append_skill_audit_record

        append_skill_audit_record(
            action,
            skill_name,
            outcome,
            verdict=verdict,
            reason=reason,
            surface="scheduler" if action == "archive" else "",
        )
    except Exception as exc:  # noqa: BLE001 - audit best-effort
        logger.warning("skill_promotion audit failed (%s/%s): %s", action, outcome, exc)


def _find_generated_draft(name: str, skills_dir: Path, hint_path: str = "") -> Path | None:
    """Locate the SKILL.md of a generated draft named ``name``.

    Prefers the usage sidecar's stored ``path`` hint (disambiguates duplicate
    draft names) when it still resolves under ``generated/``; otherwise walks
    ``generated/**/<name>/SKILL.md``. Returns the SKILL.md path or None.
    """
    generated = _generated_root(skills_dir)

    # 1) Prefer the stored hint, but ONLY if it still lives under generated/.
    if hint_path:
        hint = Path(hint_path)
        candidate = hint if hint.name.upper() == "SKILL.MD" else hint / "SKILL.md"
        try:
            under_generated = candidate.resolve().is_relative_to(generated.resolve())
        except (OSError, ValueError):
            under_generated = False
        if under_generated and candidate.exists():
            return candidate

    # 2) Walk generated/ for a dir whose name matches.
    if not generated.exists():
        return None
    for skill_md in generated.rglob("SKILL.md"):
        if skill_md.parent.name == name:
            return skill_md
    return None


def _record_scope_before_publication(name: str) -> bool:
    """Put a scope decision ON RECORD before the artifact becomes reachable.

    #429 design gate B1 ("publish-then-scope"): the move into ``promoted/`` is
    the instant a skill becomes readable by the unrestricted-allowlist profile,
    so the write that decides WHO may read it belongs BEFORE the move, not
    after. A caller that already scoped this skill to one persona (linked-skill
    intake records its row before calling ``promote``) is left alone — this
    only marks an UNSCOPED row as positively unrestricted, which is exactly
    what a global ``/skills promote`` means.

    Returns False when nothing could be recorded (the row vanished between the
    eligibility gate and here, or the sidecar write failed). The caller refuses
    on False rather than publishing something whose scope is unknown.
    """
    try:
        return skill_usage.mark_scope_unrestricted(name) is not None
    except Exception as exc:  # noqa: BLE001 — a refusal, never a crash
        logger.warning("could not record promotion scope for %s: %s", name, exc)
        return False


def _flip_generated_to_promoted(skill_md: Path) -> None:
    """Rewrite frontmatter ``generated: true`` -> ``promoted: true`` in place.

    Best-effort: a rewrite failure does not undo the physical move (the move out
    of ``generated/`` is the load-bearing gate; the flag is informational).
    """
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return
    new_content, n = re.subn(
        r"^generated:\s*true\s*$",
        "promoted: true",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        # No `generated: true` line — insert `promoted: true` into the frontmatter.
        new_content = re.sub(
            r"\n---\s*\n",
            "\npromoted: true\n---\n",
            content,
            count=1,
        )
    try:
        skill_md.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        logger.warning("could not flip frontmatter for %s: %s", skill_md, exc)


def _flip_promoted_to_generated(skill_md: Path) -> None:
    """Inverse of :func:`_flip_generated_to_promoted`, for a rollback.

    Best-effort for the same reason: the PATH is the gate (an index excludes
    by the ``generated`` path segment, Rule 2), so a file back under
    ``generated/`` is inert whatever its frontmatter says. Flipping the flag
    keeps the artifact from describing itself as promoted while it is not.
    """
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return
    new_content, n = re.subn(
        r"^promoted:\s*true\s*$",
        "generated: true",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        return
    try:
        skill_md.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        logger.warning("could not un-flip frontmatter for %s: %s", skill_md, exc)


def _promoted_target_is_valid(target_dir: Path) -> bool:
    """True iff an existing ``promoted/<name>/`` is a REAL, indexable skill (F2).

    Rule 2 — the existing directory is derived state; its mere existence is not
    proof a prior promote succeeded. A partial/aborted prior run can leave an
    empty dir (or a half-written one). Treat the target as "already promoted"
    ONLY when ALL of the following hold against the PHYSICAL file:

      1. ``target_dir/SKILL.md`` exists, AND
      2. ``scan_skill`` on it does NOT return the blocking verdict (a dangerous
         file sitting at the target must never be reported as a success), AND
      3. it would be indexable — frontmatter parses with a non-empty ``name``
         AND ``description`` (the exact gate ``build_skill_index`` applies).

    Any failure -> False -> ``promote`` refuses with ``promote_target_invalid``
    instead of marking usage promoted against a bogus target.
    """
    skill_md = target_dir / "SKILL.md"
    if not skill_md.exists():
        return False
    # Scan must not flag the blocking verdict (config-driven, Rule 1).
    try:
        if scan_skill(skill_md).verdict == _resolve_block_verdict():
            return False
    except Exception:  # noqa: BLE001 - a scan that blows up is not a valid target
        return False
    # Indexable: parseable frontmatter with non-empty name + description
    # (mirrors cognition.skills.build_skill_index's inclusion gate).
    try:
        fm = _parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
    except OSError:
        return False
    return bool(fm.get("name") and fm.get("description"))


# --------------------------------------------------------------------------- #
# Public API (consumed by WS4: `/skills review|promote|reject` + scheduled archive)
# --------------------------------------------------------------------------- #


def promote(
    name: str,
    *,
    operator_approved: bool,
    override_caution: bool = False,
) -> dict:
    """Promote an eligible, scan-passed, operator-approved skill draft.

    Gate order (default-deny — first failing gate returns + audits, no move):
      1. kill-switch enabled (Rule 3 module-attr lookup).
      2. reuse-eligibility — physical sidecar says ``state=="eligible"`` AND
         ``recurrence_count >= threshold`` (B3, Rule 2).
      3. the generated draft is locatable on disk.
      4. security scan — ``dangerous`` always refuses; ``caution`` refuses unless
         ``override_caution`` (M1).
      5. operator approval (default-deny).
      6. physical move ``generated/.../<name>`` -> ``skills/promoted/<name>``,
         flip frontmatter, ``mark_state("promoted")``, audit ``promoted``.

    Returns a dict whose ``status`` is one of: ``promoted``, ``already_promoted``,
    ``promote_target_invalid``, ``promoted_name_collision``, ``draft_changed``,
    ``killswitch_disabled``, ``not_eligible``, ``not_found``, ``scan_dangerous``,
    ``scan_caution``, ``not_approved``, ``scope_write_failed``, ``move_failed``.

    Step 6 is ordered scope-then-move (B1): the physical move is the LAST
    thing that happens, after every gate AND after the write that decides who
    may read the result. :func:`rollback_promotion` undoes it for a caller
    whose own work fails afterwards.
    """
    # 1) Kill-switch (Rule 3).
    try:
        kill_switches.requireEnabled(_KILLSWITCH_NAME, caller="skill_promotion.promote")
    except kill_switches.KillSwitchDisabled:
        _audit("promote", name, "refused", reason="killswitch_disabled")
        return {"status": "killswitch_disabled"}

    threshold = _resolve_threshold()

    # 2) Reuse-eligibility — read the PHYSICAL sidecar (B3, Rule 2).
    usage = skill_usage.get_usage(name)
    if not (usage and usage.state == "eligible" and usage.recurrence_count >= threshold):
        state = usage.state if usage else "absent"
        count = usage.recurrence_count if usage else 0
        _audit(
            "promote",
            name,
            "refused",
            reason=f"not_eligible (state={state}, count={count}, threshold={threshold})",
        )
        return {"status": "not_eligible"}

    skills_dir = _resolve_skills_dir()

    # 3) Locate the generated draft on disk.
    hint = usage.path if usage else ""
    skill_md = _find_generated_draft(name, skills_dir, hint_path=hint)
    if skill_md is None:
        _audit("promote", name, "refused", reason="not_found")
        return {"status": "not_found"}

    # 3c) Take POSSESSION of the draft (#429 codex R4 BLOCKER): move it to a
    #     staging sibling under generated/ and scan + promote the STAGED copy.
    #     The 3b/6b digest pin narrowed the scan/move window, but the final
    #     move still happened by PATHNAME — a rewrite landing between the
    #     recheck and the move slid unverified bytes under it. With possession,
    #     the scanned bytes ARE the moved bytes by construction; a concurrent
    #     rewrite of the canonical path can only ever cause an honest refusal
    #     (this call's draft is no longer there to be moved). Any exit before
    #     the final move restores the draft exactly where it was found, and
    #     staging never enters an index because it never leaves generated/.
    original_dir = skill_md.parent
    staged_dir: Path | None = original_dir.parent / (
        f".promote-staging-{uuid4().hex[:12]}"
    )
    try:
        shutil.move(str(original_dir), str(staged_dir))
    except (OSError, shutil.Error) as exc:
        _audit("promote", name, "refused", reason=f"staging_failed: {exc}")
        return {"status": "not_found"}
    skill_md = staged_dir / "SKILL.md"

    try:
        # 3b) Pin the draft's bytes BEFORE the scan (#429 codex R3 BLOCKER — the
        #     scan/move TOCTOU). Two intakes over the SAME canonical draft path
        #     interleave: A scans safe bytes, B overwrites the draft with
        #     dangerous bytes and is refused, A resumes and moves B's bytes. The
        #     digest is taken before the scan and re-verified immediately before
        #     the move, so the only bytes that can be promoted are bytes no newer
        #     than the ones the scan saw — a post-pin rewrite can only turn this
        #     into a refusal, never into a promotion of unverified content.
        try:
            pinned_digest = hashlib.sha256(skill_md.read_bytes()).hexdigest()
        except OSError:
            _audit("promote", name, "refused", reason="not_found")
            return {"status": "not_found"}

        # 4) Security scan (WS1) — the configured blocking verdict always refuses;
        #    caution refuses unless override (M1). Block verdict is resolved at call
        #    time (Rule 1, Rec 1) so SKILL_SCAN_BLOCK_VERDICT is a live knob.
        block_verdict = _resolve_block_verdict()
        result = scan_skill(skill_md)
        if result.verdict == block_verdict:
            _audit("promote", name, "refused", verdict=result.verdict, reason="scan_dangerous")
            return {"status": "scan_dangerous", "verdict": result.verdict}
        if result.verdict == "caution" and not override_caution:
            _audit("promote", name, "refused", verdict=result.verdict, reason="scan_caution")
            return {"status": "scan_caution", "verdict": result.verdict}

        # 5) Operator approval (default-deny).
        if not operator_approved:
            _audit("promote", name, "refused", verdict=result.verdict, reason="not_approved")
            return {"status": "not_approved", "verdict": result.verdict}

        # 6) Physical move out of generated/ -> skills/promoted/<name> (sanitized).
        safe_name = sanitize_skill_path_component(name)
        target_dir = _promoted_root(skills_dir) / safe_name
        src_dir = skill_md.parent

        if target_dir.exists():
            # F2 (Rule 2): an existing target dir is derived state, NOT proof a prior
            # promote succeeded. Mark usage promoted ONLY if the physical target is a
            # real, non-blocking, indexable skill. A partial/aborted prior run can
            # leave an empty or invalid dir; trusting `exists()` there would mark
            # usage promoted against a target that never enters the prompt.
            if not _promoted_target_is_valid(target_dir):
                _audit(
                    "promote",
                    name,
                    "refused",
                    verdict=result.verdict,
                    reason="promote_target_invalid",
                )
                return {"status": "promote_target_invalid", "verdict": result.verdict}
            # #429 codex R3 BLOCKER (auth grain == storage grain): a VALID target
            # is not proof it is THIS skill. ``sanitize_skill_path_component``
            # folds distinct names onto one canonical dir (sales' "Daily Spend"
            # and marketing's "daily-spend" both slug to ``promoted/daily-spend``),
            # so the target-exists branch must reconcile CONTENT identity before
            # reporting success — otherwise the caller is handed (and goes on to
            # install) an artifact produced from a DIFFERENT persona's bytes than
            # the ones just scanned. Identical content is a legitimate re-promote;
            # different content is a name collision and refuses rather than
            # silently keeping the old artifact.
            if not _skill_content_matches(skill_md, target_dir / "SKILL.md"):
                _audit(
                    "promote",
                    name,
                    "refused",
                    verdict=result.verdict,
                    reason="promoted_name_collision",
                )
                return {"status": "promoted_name_collision", "verdict": result.verdict}
            # Idempotent: a prior promote already moved THIS content. Reconcile + report.
            if usage.state != "promoted":
                skill_usage.mark_state(name, "promoted")
            _audit("promote", name, "promoted", verdict=result.verdict, reason="already_promoted")
            return {
                "status": "already_promoted",
                "path": str(target_dir / "SKILL.md"),
                "verdict": result.verdict,
            }

        # 6a) Scope BEFORE publication (B1). The move below is what makes the skill
        #     readable; a scope decision recorded after it would leave a window —
        #     and every failure in that window used to leave the skill live and
        #     unscoped while the operator was told it was refused.
        if not _record_scope_before_publication(name):
            _audit(
                "promote",
                name,
                "refused",
                verdict=result.verdict,
                reason="scope_write_failed",
            )
            return {"status": "scope_write_failed", "verdict": result.verdict}

        # 6b) Re-verify the pinned bytes IMMEDIATELY before the move (#429 codex R3
        #     BLOCKER): if the draft changed on disk since the scan (a concurrent
        #     same-name intake rewrote it — including one whose own scan just
        #     refused those bytes), nothing unverified is promoted. The refusal is
        #     honest and safe: the current draft stays staged under generated/ and
        #     a re-link rescans the new content.
        try:
            current_digest = hashlib.sha256(skill_md.read_bytes()).hexdigest()
        except OSError:
            current_digest = ""
        if current_digest != pinned_digest:
            _audit(
                "promote",
                name,
                "refused",
                verdict=result.verdict,
                reason="draft_changed",
            )
            return {"status": "draft_changed", "verdict": result.verdict}

        try:
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_dir), str(target_dir))
            staged_dir = None  # consumed — the staged copy IS the artifact
        except (OSError, shutil.Error) as exc:
            _audit("promote", name, "refused", verdict=result.verdict, reason=f"move_failed: {exc}")
            return {"status": "move_failed", "verdict": result.verdict}

        moved_md = target_dir / "SKILL.md"
        _flip_generated_to_promoted(moved_md)

        # 7) Mark promoted + audit success.
        skill_usage.mark_state(name, "promoted")
        _audit("promote", name, "promoted", verdict=result.verdict)
        return {"status": "promoted", "path": str(moved_md), "verdict": result.verdict}
    finally:
        if staged_dir is not None:
            # Unconsumed exit — put the draft back where it was found. A
            # concurrent intake may have RECREATED the canonical directory
            # while we held possession (#429 codex R5 BLOCKER): shutil.move
            # onto an existing dir NESTS our (possibly scan-refused) bytes
            # inside that newer draft, and a later promote of the combined
            # tree would carry them along. Never merge — park the refused
            # bytes under a distinct .refused- name, still inside generated/
            # (inert to every index and to _find_generated_draft, which looks
            # the canonical name up by path), and audit the divergence.
            try:
                if original_dir.exists():
                    parked = original_dir.parent / (
                        f"{original_dir.name}.refused-{uuid4().hex[:8]}"
                    )
                    shutil.move(str(staged_dir), str(parked))
                    _audit(
                        "promote",
                        name,
                        "refused",
                        reason=(
                            "restore_parked: canonical draft recreated "
                            f"concurrently; refused bytes parked at {parked.name}"
                        ),
                    )
                else:
                    shutil.move(str(staged_dir), str(original_dir))
            except (OSError, shutil.Error) as exc:
                logger.warning(
                    "promote: could not restore the staged draft for %r: %s",
                    name,
                    exc,
                )


def rollback_promotion(
    name: str,
    draft_dir: Path | str,
    *,
    reason: str = "",
) -> dict:
    """Undo a promotion this process just performed — the inverse of step 6/7.

    Exists because a caller can fail AFTER ``promote`` succeeded (linked-skill
    intake still has to install the skill for one persona, and that can refuse:
    kill-switch, unknown persona, lock timeout, ``OSError``). Leaving the
    artifact in the shared ``promoted/`` tree while telling the operator
    "refused: nothing installed" is the state divergence #429's design gate
    called out, so the visible change is taken back and the draft returns to
    ``generated/`` — inert, inspectable, and re-linkable.

    Only ever call this for a promotion THIS call performed
    (``status == "promoted"``). An ``already_promoted`` artifact belongs to an
    earlier operator decision and to any persona already using it; rolling that
    back would delete someone else's live skill.

    Returns ``{"status": ...}`` — ``rolled_back`` (the artifact is out of
    ``promoted/``), ``absent`` (nothing there to undo), ``unsafe_target`` /
    ``restore_blocked`` (the draft path is not a usable destination), or
    ``restore_failed`` (the move back itself failed — the caller must then tell
    the operator the skill IS live centrally, and where).
    """
    skills_dir = _resolve_skills_dir()
    try:
        safe_name = sanitize_skill_path_component(name)
    except ValueError as exc:
        return {"status": "unsafe_target", "error": str(exc)}
    target_dir = _promoted_root(skills_dir) / safe_name
    if not target_dir.is_dir():
        return {"status": "absent"}

    draft = Path(draft_dir)
    if draft.name.upper() == "SKILL.MD":
        draft = draft.parent
    generated_root = _generated_root(skills_dir)
    try:
        within_generated = draft.resolve().is_relative_to(generated_root.resolve())
    except (OSError, ValueError):
        within_generated = False
    if not within_generated:
        # Never move a promoted artifact somewhere the draft rails do not own.
        return {"status": "unsafe_target", "path": str(target_dir)}
    if draft.exists():
        # ``shutil.move`` onto an existing directory nests INSIDE it; refuse
        # rather than bury the artifact one level deeper than any rail looks.
        return {"status": "restore_blocked", "path": str(target_dir)}

    try:
        draft.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target_dir), str(draft))
    except (OSError, shutil.Error) as exc:
        _audit("promote", name, "refused", reason=f"rollback_failed: {exc}")
        return {"status": "restore_failed", "path": str(target_dir), "error": str(exc)}

    _flip_promoted_to_generated(draft / "SKILL.md")
    try:
        # Back to the pre-promote lifecycle state so a retry is a normal
        # promote rather than a "not_eligible" that has to be reconciled
        # against the filesystem.
        skill_usage.mark_state(name, "eligible")
    except Exception as exc:  # noqa: BLE001 — the physical move is the gate
        logger.warning("could not reset usage state for %s after rollback: %s", name, exc)
    _audit("promote", name, "rolled_back", reason=reason or "post_promote_failure")
    return {"status": "rolled_back", "path": str(draft)}


def resolve_block_verdict() -> str:
    """Public call-time accessor for the scan verdict that blocks promotion.

    Rule 1 — resolves ``config.SKILL_SCAN_BLOCK_VERDICT`` fresh on every call.
    Exists so a caller OUTSIDE this module's own gate order (linked-skill
    intake's same-name relink reconciliation, #429) can apply the identical
    blocking rule ``promote()`` step 4 uses, instead of re-deriving it or
    reaching for the private ``_resolve_block_verdict``.
    """
    return _resolve_block_verdict()


# Frontmatter fields ``write_skill`` emits that describe WHAT a skill is —
# compared for reuse identity. Deliberately excludes ``generated``/``promoted``
# (flips in place the moment a promote physically moves the file — see
# ``_flip_generated_to_promoted``) and ``source_session`` / ``created_at``
# (stamped FRESH on every ``/learn`` or ``/skills link`` ingest — including a
# genuine relink of the exact same source). A raw byte comparison would treat
# every legitimate "same skill, second persona" relink as a content mismatch,
# because those two fields never repeat even when the distilled content is
# identical.
_IDENTITY_FRONTMATTER_FIELDS = (
    "name", "description", "version", "category", "tools_used", "trigger_patterns",
)


def _skill_content_signature(text: str) -> tuple:
    """The STABLE identity of a SKILL.md's content — see
    ``_IDENTITY_FRONTMATTER_FIELDS`` for what is (and is not) compared."""
    fm = _parse_skill_frontmatter(text)
    identity_fields = tuple(fm.get(field, "") for field in _IDENTITY_FRONTMATTER_FIELDS)
    match = re.match(r"^---\s*\n.*?\n---\s*\n?", text, re.DOTALL)
    body = text[match.end():].strip() if match else text.strip()
    return (identity_fields, body)


def _skill_content_matches(candidate_skill_md: Path, promoted_skill_md: Path) -> bool:
    """True iff two SKILL.md files carry the same STABLE content identity.

    Fail-closed: an unreadable file is NOT a match — no path may hand a caller
    an artifact whose bytes it could not verify.
    """
    try:
        candidate_text = candidate_skill_md.read_text(encoding="utf-8")
        promoted_text = promoted_skill_md.read_text(encoding="utf-8")
    except OSError:
        return False
    return _skill_content_signature(candidate_text) == _skill_content_signature(promoted_text)


def resolve_reusable_promoted_skill(name: str, candidate_skill_md: Path) -> Path | None:
    """Return the existing PROMOTED ``SKILL.md`` for *name* iff its content
    matches *candidate_skill_md* — else ``None``.

    ``promote()``'s reuse-eligibility gate (step 2) runs BEFORE its own scan
    gate (step 4), so a same-name relink of a draft with NEW content is never
    actually scanned by ``promote()`` once the sidecar already reads
    ``state=="promoted"`` from a PRIOR promote of that name — it always
    returns ``not_eligible`` first. Reusing whatever already sits in
    ``promoted/`` in that case is correct ONLY for the "operator relinks the
    exact same skill at a second persona" case (#429's Q5 normal path); it is
    WRONG the instant a DIFFERENT file happens to share the name — silently
    keeping the old artifact there would report success while installing
    something other than what the caller just scanned. Content identity is
    compared (see ``_skill_content_signature``), not raw bytes: a genuine
    relink still stamps a fresh ``created_at``/``source_session`` on every
    ingest, so a byte-for-byte compare would reject the exact case this
    reuse path exists for.
    """
    promoted_md = resolve_promoted_skill(name)
    if promoted_md is None:
        return None
    if not _skill_content_matches(candidate_skill_md, promoted_md):
        return None
    return promoted_md


def resolve_promoted_skill(name: str) -> Path | None:
    """Return the PHYSICAL promoted ``SKILL.md`` for *name*, or None (Rule 2).

    ``promote`` gates on the usage sidecar, which is derived state: once a
    draft has been promoted the sidecar reads ``promoted`` and every later
    call returns ``not_eligible`` — truthfully about the sidecar, misleadingly
    about the world, because the skill IS live in ``promoted/``. A caller that
    needs to know "is this skill vetted and on disk right now" (linked-skill
    intake assigning an already-promoted skill to a SECOND persona) must ask
    the filesystem, not the counter.

    Returns the path only when the target passes the same validity bar
    ``promote`` applies to an existing target — file present, scan not
    blocking, frontmatter indexable — so a partial or hostile directory never
    reads as "promoted".
    """
    safe_name = sanitize_skill_path_component(name)
    target_dir = _promoted_root(_resolve_skills_dir()) / safe_name
    if not target_dir.is_dir() or not _promoted_target_is_valid(target_dir):
        return None
    return target_dir / "SKILL.md"


def reject_skill(name: str, reason: str) -> dict:
    """Reject a skill draft — archive it + audit (B6, distinct verb).

    This is NOT ``promote(operator_approved=True)``. It archives the usage row so
    the draft stops being surfaced as promotable, and writes a ``reject`` audit row.
    """
    skill_usage.mark_state(name, "archived")
    _audit("reject", name, "rejected", reason=reason)
    return {"status": "rejected"}


def archive_stale() -> list[str]:
    """Archive stale staged drafts and write one audit row per archived skill (NM2).

    WS2's ``prune_stale`` flips state ONLY (no audit dependency). This WS3 wrapper
    owns the audit emission so all skill-action audit rows originate in WS3.
    Intended for a scheduled seam (dream/reflection cron).
    """
    names = skill_usage.prune_stale()
    for archived_name in names:
        _audit("archive", archived_name, "stale_archived", reason="stale_no_recurrence")
    return names


def list_promotable(threshold: int | None = None) -> list[dict]:
    """List eligible drafts with a fresh scan preview; each preview audits (B6).

    Returns ``[{"name", "verdict", "recurrence_count"}, ...]`` for every eligible
    draft (the operator's ``/skills review`` surface). A draft whose file cannot
    be located previews as verdict ``unknown``.
    """
    limit = _resolve_threshold(threshold)
    skills_dir = _resolve_skills_dir()
    out: list[dict] = []
    for usage in skill_usage.list_eligible(limit):
        skill_md = _find_generated_draft(usage.name, skills_dir, hint_path=usage.path)
        if skill_md is None:
            verdict = "unknown"
        else:
            verdict = scan_skill(skill_md).verdict
        _audit("scan_preview", usage.name, verdict, verdict=verdict)
        out.append(
            {
                "name": usage.name,
                "verdict": verdict,
                "recurrence_count": usage.recurrence_count,
            }
        )
    return out


__all__ = (
    "promote",
    "reject_skill",
    "archive_stale",
    "list_promotable",
    "resolve_block_verdict",
    "resolve_promoted_skill",
    "resolve_reusable_promoted_skill",
    "rollback_promotion",
)
