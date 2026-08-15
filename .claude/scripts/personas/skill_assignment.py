"""Persona-scoped skill assignment — the install half of linked-skill intake.

Issue #429 (epic #419). When an operator links a skill at a persona, the
existing lifecycle does the vetting (``cognition.skill_learn`` ingests,
``cognition.skill_promotion`` runs the security scan and the default-deny
promote gate); this module does the one thing that lifecycle has no notion
of — putting the vetted skill in front of THAT persona and nobody else.

**Why a profile-local install and not a config key.** Ticket #429 says: ride
the #426 executor if persona skill assignment is config-shaped, use its own
surface if it has one. It has one. Every persona runtime builds its skill
index as ``build_skill_index(central, allowlist=..., extra_skill_dirs=[
get_persona_paths(persona)["skills"]])`` — ``chat/engine.py``,
``chat/discord_persona_runtime.py``, ``chat/web_persona_runtime.py``,
``cabinet/text_orchestrator.py``. The extra dirs are read WITHOUT the
central allowlist filter and only for the persona they belong to, which is
Q5 ("requesting persona only") by construction.

The config-shaped alternative was considered and rejected on evidence: the
allowlist lives in ``capability_blueprint.skills``, which
``personas/blueprint_migration.py`` COMPILES from a blueprint. Hand-writing
that key from here would (a) make a compiled overlay appear for a persona
that had none, which ``capabilities._compiled_profile_entry`` then honors
WHOLESALE — silently emptying the ``env_groups`` that persona was getting
from the shared matrix — and (b) be overwritten by the next reconcile.
``personas/provisioning.py`` never touches ``<profile>/skills/``, so the
profile-local install survives a reconcile with no ledger-replay union of
the kind #426 round 4 had to add for ``toolsets:``.

**Ledger contract, and how it differs from #426 deliberately.** Every exit
appends exactly ONE row to an append-only JSONL in the TARGET persona's data
dir. There is no intent/outcome PAIR here because nothing REPLAYS this
ledger into reach: the persona's installed skills are read from the
directory itself on every index build (Rule 2 — physical state is the
truth), so the ledger is a record, never an authority. #426 needs the pair
because ``toolset_grants.ledger_scope`` replays its rows into a config
reconcile; this one would be ceremony.

``actor`` / ``actor_role`` / ``trigger_text`` / ``surface`` / ``channel_id``
are REQUIRED keyword args for the same reason as #426: the epic's metric is
"zero grants without a matching live operator turn", so an assignment nobody
ordered must not be expressible. ``actor_role`` is CHECKED here, never
established here — resolve it server-side from the authenticated surface.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Rule 3 — module-attribute lookup so a test patching
# ``personas.toolset_grants.normalize_trigger_text`` (or the redaction behind
# it) propagates. The trigger-text contract is identical for both executors,
# so it is shared rather than re-implemented with drift.
from . import toolset_grants as _grants
from .core import get_persona_paths, validate_persona_name

_logger = logging.getLogger(__name__)

# One append-only ledger for the whole linked-skill surface: both the intake
# decisions and the install outcomes land here, so a single grep over one
# persona's file answers "what skills was this homie given, by whom, on whose
# word, and what was refused".
LEDGER_FILENAME = "persona_skill_assignments.jsonl"
INTEGRATION = "personas"

# The operator kill-switch for persona persistent-state writes (dashboard
# soft/hard delete, avatar writes, curriculum bootstrap, #426 toolset grants).
# An install writes into the persona's profile tree, so it honors the same
# switch rather than minting a new one — the switch only turns the surface
# OFF, never on.
KILL_SWITCH = "persona_mutation"

# How long an install waits for another writer on the same persona's skills
# dir. The critical section is one small tree copy and a rename, so this is
# queue headroom, not a working budget. Bounded because the caller is a chat
# turn: a wedged holder must surface as an audited error, not a hung reply.
_INSTALL_LOCK_TIMEOUT_S = 10.0

OPERATION_ASSIGN = "assign"
OPERATION_INTAKE = "intake"

OUTCOME_ASSIGNED = "assigned"
OUTCOME_ALREADY_ASSIGNED = "already_assigned"
# The default profile indexes the CENTRAL skills dir with an unrestricted
# allowlist, so a centrally-promoted skill is already in its index. Writing a
# profile-local copy would put the same skill under two paths in one index.
# Not a refusal — the operator's ask is satisfied, just not by us.
OUTCOME_ALREADY_REACHABLE = "already_reachable"
OUTCOME_REFUSED = "refused"
OUTCOME_ERROR = "error"

REASON_INVALID_PERSONA = "invalid_persona"
REASON_UNKNOWN_PERSONA = "unknown_persona"
REASON_INVALID_SKILL = "invalid_skill"
REASON_MISSING_SOURCE = "missing_source"
REASON_MISSING_OPERATOR_TURN = "missing_operator_turn"
REASON_NOT_AUTHORIZED = "not_authorized"
REASON_KILL_SWITCH = "kill_switch"
REASON_DEFAULT_PROFILE_CENTRAL = "default_profile_central"
REASON_INSTALL_FAILED = "install_failed"
REASON_LOCK_TIMEOUT = "lock_timeout"

# Ceiling on every identifier field written to the ledger. Real values are far
# under it; the cap exists so a hostile skill name or source label cannot turn
# one row into a log dump.
_MAX_FIELD_CHARS = 400

# Path components are built from an LLM-authored skill name (the distiller
# writes it), so the name is hostile input at this seam. Only these survive.
_UNSAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


class SkillAssignmentAuditError(RuntimeError):
    """A REQUIRED ledger row could not be written.

    Distinct from :class:`SkillAssignmentRefusedError` for the same reason
    #426 splits them: a refusal is an answer we stand behind AND recorded,
    while this says nothing could be recorded — so the caller must not be
    handed a polished "no" it would reasonably read as audited. Nothing was
    installed on this path either; the difference is what we can PROVE.

    Not a ``ValueError``: existing ``except ValueError`` handlers around
    persona writes mean "the request was bad", and this is not that.
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        self.reason = reason
        super().__init__(message)


class SkillAssignmentRefusedError(ValueError):
    """An honest refusal from the assignment executor. Never a partial install.

    ``ValueError`` subclass to match ``ConfigShapeError`` /
    ``ToolsetGrantRefusedError`` so existing persona-write callers keep
    working. ``reason`` is the machine-readable code a surface branches on.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class SkillAssignmentResult:
    """What the executor did, in terms a command surface can speak back.

    ``changed`` is the honest bit: an already-installed skill and a
    default-profile no-op are real answers, not errors, and neither writes
    a file.
    """

    persona_id: str
    skill_name: str
    outcome: str
    changed: bool
    install_path: Path | None = None
    audit_id: str = ""
    reason: str = ""


def resolve_ledger_path(
    audit_path: Path | str | None = None,
    persona_id: str = "",
) -> Path:
    """Resolve the ledger path at call time (Rule 1 — None sentinel).

    **Invariant: the ledger is keyed to the TARGET persona, never to the
    profile this PROCESS happens to be running as.** ``config.DATA_DIR`` is
    computed once at import from the ambient profile, and persona bots run as
    separate processes with ``HOMIE_HOME`` forced to their own root
    (``runtime/subprocess_env.py``). Keying off it would file an assignment
    made from the sales bot in the sales ledger while an assignment made from
    the default process for the same persona landed somewhere else — same
    authorization grain, two storage grains (Rule 4). #426 shipped this bug
    and fixed it; this module starts on the fixed side.

    Precedence: an explicit *audit_path* wins (tests inject one), then the
    target persona's data dir, and only with NEITHER does this fall back to
    the ambient ``config.DATA_DIR`` — correct only when there is no target
    persona to key on. Every caller here passes the persona it acts for.
    """
    if audit_path is not None:
        return Path(audit_path)
    persona = str(persona_id or "").strip()
    if persona:
        return Path(get_persona_paths(persona)["data"]) / LEDGER_FILENAME
    # Lazy: ``config`` imports ``personas`` at module load, so a top-level
    # import here is a cycle; the module read also keeps tests' monkeypatch
    # of ``config.DATA_DIR`` effective.
    import config  # noqa: PLC0415 — cycle-safe + test-monkeypatched

    return Path(config.DATA_DIR) / LEDGER_FILENAME


def _persona_is_safe_for_paths(persona: str) -> bool:
    """True iff *persona* is safe to hand to ``get_persona_paths()`` /
    ``resolve_ledger_path()``.

    Empty ("no persona resolved yet") and ``"default"`` (its own hardcoded,
    non-traversable resolver) are always safe. Any OTHER value must first
    pass ``validate_persona_name`` — M3 (#429 round-2 MAJOR): a hostile or
    malformed persona id (e.g. a corrupted channel binding) must never
    reach ``get_persona_paths()`` even to record its OWN refusal audit row.
    ``get_persona_paths()`` joins an unvalidated name straight onto
    ``<root>/profiles/<name>/`` with no containment check of its own — a
    name like ``"../../AppData/Roaming/pwn"`` resolves OUTSIDE the profiles
    tree, and ``append_audit_record`` will ``mkdir(parents=True)`` there.
    """
    if not persona or persona == "default":
        return True
    try:
        validate_persona_name(persona)
    except ValueError:
        return False
    return True


def safe_audit_path(
    persona_id: str,
    audit_path: Path | str | None = None,
) -> Path | str | None:
    """The ledger target for *persona_id* that is SAFE even when the id is not.

    Returns a value to pass as ``audit_path`` into :func:`append_audit_record`
    / :func:`audit_attempt`:

    * an explicit caller-supplied *audit_path* always wins (tests inject one);
    * a persona that passes :func:`_persona_is_safe_for_paths` returns ``None``
      so :func:`resolve_ledger_path` derives the persona's own ledger;
    * anything else forces the AMBIENT ledger — the unvalidated id never
      reaches ``get_persona_paths()``, even to write a refusal row.

    Shared by the assignment executor (M3, #429 round 2) and linked-skill
    intake (#429 codex R3 MAJOR): intake's refusals fire BEFORE
    ``validate_persona_name`` ever runs, so its audit writes were reaching the
    filesystem join with an id straight off a channel binding.
    """
    if audit_path is not None:
        return audit_path
    if _persona_is_safe_for_paths(str(persona_id or "").strip()):
        return None
    import config  # noqa: PLC0415 — mirrors resolve_ledger_path's own fallback

    return Path(config.DATA_DIR) / LEDGER_FILENAME


def _clip(value: Any) -> str:
    """One bounded, single-line string for a ledger identifier field."""
    return " ".join(str(value or "").split())[:_MAX_FIELD_CHARS]


def append_audit_record(
    *,
    operation: str,
    persona_id: str,
    skill_name: str,
    outcome: str,
    actor: str = "",
    actor_role: str = "",
    surface: str = "",
    channel_id: str = "",
    trigger_text: str = "",
    reason: str = "",
    source: str = "",
    verdict: str = "",
    install_path: Path | str = "",
    error: str = "",
    audit_path: Path | str | None = None,
) -> str:
    """Append one ledger row and return its id. Raises on an IO failure.

    :func:`audit_attempt` is the best-effort wrapper most callers use; this
    stays strict so the wrapper — and the refusal path — have something to
    report. The row lands in the TARGET persona's ledger (see
    :func:`resolve_ledger_path`).
    """
    path = resolve_ledger_path(audit_path, persona_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "integration": INTEGRATION,
        "action": f"skill_{operation}",
        "operation": operation,
        "persona_id": _clip(persona_id),
        "skill_name": _clip(skill_name),
        "outcome": outcome,
        "reason": _clip(reason),
        "actor": _clip(actor),
        "actor_role": _clip(actor_role),
        "surface": _clip(surface),
        "channel_id": _clip(channel_id),
        "trigger_text": trigger_text,
        "source": _clip(source),
        "verdict": _clip(verdict),
        "install_path": str(install_path),
        "error": _clip(error)[:200],
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return f"{record['timestamp']}:{record['persona_id']}:{operation}:{outcome}"


def audit_attempt(**fields: Any) -> str:
    """Best-effort ledger append — a failed row never changes the outcome.

    Same precedent as ``toolset_grants.audit_attempt``: the decision matters
    more than its record, and every swallow leaves a receipt. Returns the
    audit id, or ``""`` when the write failed.

    Use this ONLY where the branch already surfaces its own failure. A
    REFUSAL is not one of those — it returns a polished "no" a caller reads
    as recorded — so refusals go through :func:`append_audit_record` and turn
    an append failure into :class:`SkillAssignmentAuditError`.
    """
    try:
        return append_audit_record(**fields)
    except Exception as exc:  # noqa: BLE001 — audit is a record, not the action
        _logger.warning(
            "personas.skill_assignment: audit write failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return ""


def safe_skill_dir_name(name: str) -> str:
    """Slug an LLM-authored skill name into ONE safe path component.

    Hard-rejects rather than silently repairing the shapes that mean
    traversal, because a name that wants to escape is not a naming accident:
    ``..``, path separators, an absolute/drive-qualified path, a leading dot,
    or a control character. Everything else is slugged to
    ``[A-Za-z0-9._-]`` and the caller still asserts the resolved directory
    stays under the persona's skills root (defense in depth — the same
    two-layer shape ``cognition.skills.write_skill`` uses).

    Raises ``ValueError`` on an unusable name; the executor converts that to
    an audited refusal.
    """
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("skill name is empty")
    if any(ord(ch) < 0x20 for ch in raw):
        raise ValueError("skill name contains a control character")
    if ".." in raw or "/" in raw or "\\" in raw or ":" in raw:
        raise ValueError(f"skill name is not a single path component: {raw!r}")
    slug = _UNSAFE_COMPONENT_RE.sub("-", raw).strip("-")
    if not slug or slug.startswith("."):
        raise ValueError(f"skill name has no usable path form: {raw!r}")
    return slug


def persona_skill_dir(persona_id: str) -> Path:
    """The profile-local skills root every persona runtime indexes for *persona_id*."""
    return Path(get_persona_paths(str(persona_id).strip())["skills"])


def installed_skill_names(persona_id: str) -> tuple[str, ...]:
    """Skill directory names physically installed for *persona_id*, sorted.

    Rule 2: read the directory, never a sidecar or a config claim. The
    persona's index is built by walking this tree, so the tree is the only
    thing that can be right about what the persona can reach.

    Fail-open: an unreadable or absent dir is an empty install set, so a
    caller reporting reach cannot be turned into a crash by a permissions
    blip.
    """
    try:
        root = persona_skill_dir(persona_id)
        if not root.is_dir():
            return ()
        return tuple(
            sorted(
                skill_md.parent.name
                for skill_md in root.rglob("SKILL.md")
                if skill_md.is_file()
            )
        )
    except OSError as exc:
        _logger.warning(
            "personas.skill_assignment: could not list installed skills for %s: %s",
            persona_id,
            exc,
        )
        return ()


def _normalize_source(source: Path | str) -> tuple[Path, Path] | None:
    """Return ``(skill_dir, skill_md)`` for *source*, or None when unusable.

    Accepts either the ``SKILL.md`` itself (what ``skill_promotion.promote``
    hands back) or the directory holding it.
    """
    try:
        path = Path(source).expanduser()
    except (OSError, ValueError, TypeError):
        return None
    try:
        if path.is_file() and path.name.upper() == "SKILL.MD":
            return path.parent, path
        if path.is_dir() and (path / "SKILL.md").is_file():
            return path, path / "SKILL.md"
    except OSError:
        return None
    return None


def _install_tree(source_dir: Path, target_dir: Path) -> None:
    """Copy *source_dir* over *target_dir* through a staged rename.

    Staged OUTSIDE the persona's skills root on purpose. A half-copied tree
    sitting inside it would be visible to ``build_skill_index``'s
    ``rglob("SKILL.md")`` for the duration of the copy, which is exactly the
    window where a persona could pick up a truncated instruction file. The
    staging dir is a sibling of the skills root (same profile tree, so the
    rename stays within one filesystem and is atomic).

    An existing target is renamed aside first and removed only after the new
    tree is in place, so a failure mid-swap leaves the OLD skill installed
    rather than nothing.
    """
    skills_root = target_dir.parent
    stage_root = skills_root.parent
    stage_root.mkdir(parents=True, exist_ok=True)
    skills_root.mkdir(parents=True, exist_ok=True)

    token = uuid.uuid4().hex[:8]
    staging = stage_root / f".skill-staging-{token}"
    backup = stage_root / f".skill-replaced-{token}"

    shutil.copytree(source_dir, staging)
    displaced = False
    restore_failed = False
    try:
        if target_dir.exists():
            os.replace(target_dir, backup)
            displaced = True
        os.replace(staging, target_dir)
    except OSError:
        # Put the previous install back before surfacing the failure — a
        # failed re-assign must not cost the persona a skill it already had.
        if displaced and not target_dir.exists():
            try:
                os.replace(backup, target_dir)
            except OSError as restore_exc:  # noqa: PERF203 — one-shot restore
                # M2: the restore itself just failed, so `backup` is the ONLY
                # surviving copy of the skill the persona had BEFORE this
                # install started. The unconditional `finally` below used to
                # delete it regardless — a transient failure at BOTH replace
                # calls then left neither the new skill nor the old one,
                # violating "never a partial install". Flag it so `finally`
                # preserves the backup instead.
                restore_failed = True
                _logger.error(
                    "personas.skill_assignment: could not restore %s from %s "
                    "— preserving %s so the prior install stays recoverable: %s",
                    target_dir,
                    backup,
                    backup,
                    restore_exc,
                )
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if backup.exists() and not restore_failed:
            shutil.rmtree(backup, ignore_errors=True)


def assign_skill_to_persona(
    persona_id: str,
    source: Path | str,
    *,
    skill_name: str,
    actor: str,
    actor_role: str,
    trigger_text: str,
    surface: str,
    channel_id: str,
    audit_path: Path | str | None = None,
) -> SkillAssignmentResult:
    """Install one vetted skill into *persona_id*'s own skill surface.

    This is the ONLY path that puts a skill in front of a single persona, and
    it assumes the caller already ran the vetting: *source* must be a skill
    that came out of ``cognition.skill_promotion.promote`` (scan-gated,
    operator-approved). Nothing here re-opens that gate and nothing here
    bypasses it — an unpromoted draft simply never reaches this function,
    because the orchestrator (``cognition.skill_intake``) refuses first.

    Gate order — every gate BEFORE any write, each leaving one refusal row:
    persona named -> skill named -> source resolvable -> live operator turn
    complete -> admin role -> kill-switch -> valid persona name -> profile
    directory physically on disk -> default profile short-circuit.

    Blocking file IO. An async surface calls this through
    ``asyncio.to_thread`` so a slow disk cannot wedge the event loop.

    Raises:
        SkillAssignmentRefusedError: any gate above. Nothing was written.
        SkillAssignmentAuditError: a refusal that could not be recorded
            (nothing was written), or an install whose outcome row failed
            (the skill IS installed; the row is not).
        KillSwitchDisabled: the operator disabled ``persona_mutation``.
        OSError: the install itself failed (audited before it propagates).
    """
    persona = str(persona_id or "").strip()
    name = str(skill_name or "").strip()
    who = str(actor or "").strip()
    role = str(actor_role or "").strip().lower()
    trigger = _grants.normalize_trigger_text(trigger_text)
    surface_name = str(surface or "").strip()
    channel = str(channel_id or "").strip()
    source_label = str(source or "")

    def _safe_audit_path() -> Path | str | None:
        # M3: computed FRESH on every _audit/_refuse call, not once — several
        # gates below (kill-switch, missing name, incomplete operator turn,
        # unauthorized role) can fire BEFORE `validate_persona_name` ever
        # runs, each with its own reason but the SAME unvalidated `persona`.
        # Delegates to the module-level ``safe_audit_path`` so intake and this
        # executor share one containment discipline.
        return safe_audit_path(persona, audit_path)

    def _audit(
        outcome: str,
        *,
        reason: str = "",
        install_path: Path | str = "",
        error: str = "",
    ) -> str:
        return audit_attempt(
            operation=OPERATION_ASSIGN,
            persona_id=persona,
            skill_name=name,
            outcome=outcome,
            reason=reason,
            actor=who,
            actor_role=role,
            surface=surface_name,
            channel_id=channel,
            trigger_text=trigger,
            source=source_label,
            install_path=install_path,
            error=error,
            audit_path=_safe_audit_path(),
        )

    def _refuse(reason: str, message: str) -> None:
        # A refusal row is REQUIRED, not best-effort: the ticket's acceptance
        # criterion is "refusal audited", and a caller catching the refusal
        # cannot tell an audited "no" from a swallowed one. An unwritable
        # ledger therefore comes back as a distinct audit failure.
        try:
            append_audit_record(
                operation=OPERATION_ASSIGN,
                persona_id=persona,
                skill_name=name,
                outcome=OUTCOME_REFUSED,
                reason=reason,
                actor=who,
                actor_role=role,
                surface=surface_name,
                channel_id=channel,
                trigger_text=trigger,
                source=source_label,
                error=message,
                audit_path=_safe_audit_path(),
            )
        except Exception as exc:  # noqa: BLE001 — re-raised as a distinct type
            _logger.error(
                "personas.skill_assignment: refusal (%s) could not be audited: %s: %s",
                reason,
                type(exc).__name__,
                exc,
            )
            raise SkillAssignmentAuditError(
                f"refusal could not be audited ({reason}): "
                f"{type(exc).__name__}: {exc}. Nothing was written.",
                reason=reason,
            ) from exc
        raise SkillAssignmentRefusedError(message, reason=reason)

    # ── Contract gates. None can partially apply.
    if not persona:
        _refuse(
            REASON_INVALID_PERSONA,
            "refused: no persona named — say which homie the skill is for.",
        )
    if not name:
        _refuse(
            REASON_INVALID_SKILL,
            "refused: no skill named — the promoted skill has no name to install under.",
        )
    if not who or not trigger or not surface_name or not channel:
        _refuse(
            REASON_MISSING_OPERATOR_TURN,
            "refused: a skill assignment needs the live operator turn that "
            "ordered it (actor + trigger_text + surface + channel_id). An "
            "assignment nobody ordered is not expressible.",
        )
    if role != _grants.ADMIN_ROLE:
        _refuse(
            REASON_NOT_AUTHORIZED,
            f"refused: skill assignment requires the {_grants.ADMIN_ROLE} "
            f"role, got {role or 'none'!r}.",
        )

    resolved = _normalize_source(source)
    if resolved is None:
        _refuse(
            REASON_MISSING_SOURCE,
            f"refused: no readable SKILL.md at {source_label!r} — nothing to install.",
        )
    source_dir, source_md = resolved

    try:
        from security import kill_switches  # noqa: PLC0415 — Rule 3 module attr
    except Exception as exc:  # noqa: BLE001 — receipt, not a silent disable
        # Precedent: personas/services.py:1066-1077. The switch is an operator
        # OFF control, not the thing that grants capability, so its absence
        # must not silently disable a working feature.
        _logger.warning(
            "personas.skill_assignment: kill-switch module unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
    else:
        try:
            kill_switches.requireEnabled(
                KILL_SWITCH, caller="personas.assign_skill_to_persona"
            )
        except kill_switches.KillSwitchDisabled as exc:
            _audit(OUTCOME_REFUSED, reason=REASON_KILL_SWITCH, error=str(exc))
            raise

    # The default profile indexes the CENTRAL skills dir with an unrestricted
    # allowlist (persona-capability-matrix.yaml: profiles.default.skill_groups
    # == ["*"]), and get_persona_paths("default")["skills"] IS that central
    # dir. The promote step already put the skill there, so a profile-local
    # copy would list the same skill twice under two paths. Honest no-op, not
    # a refusal — the operator's ask is satisfied.
    #
    # Checked BEFORE ``validate_persona_name`` deliberately: ``default`` is in
    # that helper's ``_RESERVED`` set (``personas/core.py:43-56``), so
    # validating first would report the main homie as an invalid NAME rather
    # than answering the actual question. Same ordering as #426, whose
    # default-profile branch also precedes its name validation.
    if persona == "default":
        audit_id = _audit(
            OUTCOME_ALREADY_REACHABLE,
            reason=REASON_DEFAULT_PROFILE_CENTRAL,
            install_path=source_md,
        )
        return SkillAssignmentResult(
            persona_id=persona,
            skill_name=name,
            outcome=OUTCOME_ALREADY_REACHABLE,
            changed=False,
            install_path=source_md,
            audit_id=audit_id,
            reason=REASON_DEFAULT_PROFILE_CENTRAL,
        )

    try:
        validate_persona_name(persona)
    except ValueError as exc:
        _refuse(REASON_INVALID_PERSONA, f"refused: {exc}")

    try:
        slug = safe_skill_dir_name(name)
    except ValueError as exc:
        _refuse(REASON_INVALID_SKILL, f"refused: {exc}")

    # Rule 2: the persona exists iff its profile directory is on disk. Without
    # this a typo'd name would provision a ghost profile tree, because the
    # install mkdirs its own parents.
    skills_root = persona_skill_dir(persona)
    profile_root = skills_root.parent
    if not profile_root.is_dir():
        _refuse(
            REASON_UNKNOWN_PERSONA,
            f"refused: no profile directory for {persona!r} at {profile_root} "
            "— create the persona first.",
        )

    target_dir = skills_root / slug
    # Defense in depth: even with the name slugged, assert the resolved target
    # cannot escape the persona's own skills root.
    if not target_dir.resolve().is_relative_to(skills_root.resolve()):
        _refuse(
            REASON_INVALID_SKILL,
            f"refused: {name!r} resolves outside {skills_root} — not installing.",
        )

    # Function-local by necessity: ``shared`` imports ``personas.services`` at
    # module level, so a top-level import here would close a cycle. The module
    # read also keeps Rule 3 (a test patching ``shared.file_lock`` propagates).
    from shared import file_lock  # noqa: PLC0415 — see comment

    acquired = False
    try:
        # Serialized on the persona's skills root (the lock file is a SIBLING
        # of it, never inside — a stray file in the indexed tree is noise the
        # index would have to walk). Two turns installing at once would
        # otherwise race the staged rename.
        with file_lock(skills_root, timeout=_INSTALL_LOCK_TIMEOUT_S):
            acquired = True

            target_md = target_dir / "SKILL.md"
            had_prior_install = target_md.is_file()
            if had_prior_install:
                try:
                    same = target_md.read_bytes() == source_md.read_bytes()
                except OSError:
                    same = False
                if same:
                    audit_id = _audit(
                        OUTCOME_ALREADY_ASSIGNED, install_path=target_md
                    )
                    return SkillAssignmentResult(
                        persona_id=persona,
                        skill_name=name,
                        outcome=OUTCOME_ALREADY_ASSIGNED,
                        changed=False,
                        install_path=target_md,
                        audit_id=audit_id,
                    )

            try:
                _install_tree(source_dir, target_dir)
            except (OSError, shutil.Error) as exc:
                _audit(
                    OUTCOME_ERROR,
                    reason=REASON_INSTALL_FAILED,
                    install_path=target_dir,
                    error=str(exc),
                )
                raise

            # Physical state HAS moved; only now may the ledger say so. A
            # failure here is not swallowed — and it is not left live either
            # (#429 codex R7 BLOCKER): an assignment with no ledger row must
            # not sit in the persona's own index while the operator reads
            # "could not be installed". The persona-local copy is taken back
            # before the raise; the caller rolls back the central promotion
            # and the scope row. A retry is idempotent (it comes back
            # ``already_assigned``).
            try:
                audit_id = append_audit_record(
                    operation=OPERATION_ASSIGN,
                    persona_id=persona,
                    skill_name=name,
                    outcome=OUTCOME_ASSIGNED,
                    actor=who,
                    actor_role=role,
                    surface=surface_name,
                    channel_id=channel,
                    trigger_text=trigger,
                    source=source_label,
                    install_path=target_md,
                    audit_path=audit_path,
                )
            except Exception as exc:  # noqa: BLE001 — re-raised as a distinct type
                undone = False
                try:
                    shutil.rmtree(target_dir)
                    undone = True
                except (OSError, shutil.Error) as undo_exc:
                    _logger.error(
                        "personas.skill_assignment: audit failed AND the local "
                        "undo of %s at %s failed — the skill is LIVE and "
                        "unrecorded: %s: %s",
                        name,
                        target_dir,
                        type(undo_exc).__name__,
                        undo_exc,
                    )
                _logger.error(
                    "personas.skill_assignment: %s installed for %s but its "
                    "ledger row could not be written: %s: %s",
                    name,
                    persona,
                    type(exc).__name__,
                    exc,
                )
                state_note = (
                    "the persona-local copy was removed again — nothing is "
                    "installed"
                    if undone
                    else f"the skill IS live at {target_dir} — remove that "
                    "directory if you did not want it there"
                )
                if undone and had_prior_install:
                    # _install_tree's backup is already gone by this point —
                    # the prior version is unrecoverable; say so.
                    state_note += (
                        "; a previous local version was replaced in the same "
                        "operation and could not be retained"
                    )
                raise SkillAssignmentAuditError(
                    f"{name!r} was installed for {persona!r} at {target_md} "
                    f"but its ledger row could not be written "
                    f"({type(exc).__name__}: {exc}); {state_note}.",
                    reason=REASON_INSTALL_FAILED,
                ) from exc

            return SkillAssignmentResult(
                persona_id=persona,
                skill_name=name,
                outcome=OUTCOME_ASSIGNED,
                changed=True,
                install_path=target_md,
                audit_id=audit_id,
            )
    except TimeoutError as exc:
        # Only an ACQUISITION timeout is ours to label. ``TimeoutError`` is an
        # ``OSError`` subclass, so a timeout raised from inside the critical
        # section was already audited by the branch that owns it; the flag
        # keeps this from writing a second, wrong row.
        if acquired:
            raise
        _audit(OUTCOME_ERROR, reason=REASON_LOCK_TIMEOUT, error=str(exc))
        raise


__all__ = [
    "INTEGRATION",
    "KILL_SWITCH",
    "LEDGER_FILENAME",
    "OPERATION_ASSIGN",
    "OPERATION_INTAKE",
    "OUTCOME_ALREADY_ASSIGNED",
    "OUTCOME_ALREADY_REACHABLE",
    "OUTCOME_ASSIGNED",
    "OUTCOME_ERROR",
    "OUTCOME_REFUSED",
    "REASON_DEFAULT_PROFILE_CENTRAL",
    "REASON_INSTALL_FAILED",
    "REASON_INVALID_PERSONA",
    "REASON_INVALID_SKILL",
    "REASON_KILL_SWITCH",
    "REASON_LOCK_TIMEOUT",
    "REASON_MISSING_OPERATOR_TURN",
    "REASON_MISSING_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_UNKNOWN_PERSONA",
    "SkillAssignmentAuditError",
    "SkillAssignmentRefusedError",
    "SkillAssignmentResult",
    "append_audit_record",
    "assign_skill_to_persona",
    "audit_attempt",
    "installed_skill_names",
    "persona_skill_dir",
    "resolve_ledger_path",
    "safe_audit_path",
    "safe_skill_dir_name",
]
