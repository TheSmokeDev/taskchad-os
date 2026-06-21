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

import logging
import re
import shutil
from pathlib import Path

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
    ``promote_target_invalid``, ``killswitch_disabled``, ``not_eligible``,
    ``not_found``, ``scan_dangerous``, ``scan_caution``, ``not_approved``,
    ``move_failed``.
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
        # Idempotent: a prior promote already moved a valid skill. Reconcile + report.
        if usage.state != "promoted":
            skill_usage.mark_state(name, "promoted")
        _audit("promote", name, "promoted", verdict=result.verdict, reason="already_promoted")
        return {
            "status": "already_promoted",
            "path": str(target_dir / "SKILL.md"),
            "verdict": result.verdict,
        }

    try:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_dir), str(target_dir))
    except (OSError, shutil.Error) as exc:
        _audit("promote", name, "refused", verdict=result.verdict, reason=f"move_failed: {exc}")
        return {"status": "move_failed", "verdict": result.verdict}

    moved_md = target_dir / "SKILL.md"
    _flip_generated_to_promoted(moved_md)

    # 7) Mark promoted + audit success.
    skill_usage.mark_state(name, "promoted")
    _audit("promote", name, "promoted", verdict=result.verdict)
    return {"status": "promoted", "path": str(moved_md), "verdict": result.verdict}


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
)
