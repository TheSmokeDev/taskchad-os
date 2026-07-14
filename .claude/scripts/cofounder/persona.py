"""Co-founder v2 WS1 — the cofounder becomes a registered persona.

Run manually:

    cd .claude/scripts && uv run python -m cofounder.persona [--force] [--test]

Seeds (idempotently) the ``cofounder`` persona profile so the main guy is a
first-class Homie: a real profile under ``~/.homie/profiles/cofounder/``,
cabinet-eligible (``cabinet:`` block — ``/standup`` includes him), learning
enabled (the Act-5 persona learning tick picks him up), and
``cabinet.portfolio_context: true`` so every cabinet turn he takes carries
the injected Portfolio Digest (cabinet turns are no-tools by design — the
digest is his eyes on the portfolio).

Ownership contract (the never-clobber rule):

- Profile creation goes through ``personas.lifecycle.create_profile`` — the
  same ``persona_mutation``-kill-switched, audit-rowed path every persona
  uses. ``no_alias=True``: the cofounder is driven through cabinet/chat and
  the ``/cofounder`` family, never his own CLI wrapper.
- config.yaml merge is STRICT-read read-modify-write (a malformed file is an
  error outcome, never silently wiped — the ``set_persona_learning`` lesson)
  and fills MISSING keys only. An operator who set
  ``cabinet.portfolio_context: false`` (or any other value) keeps it.
- Identity files are written ONLY when missing, effectively empty, or still
  byte-equal to the generic lifecycle scaffold seed. ``--force`` overwrites
  the seeded identity files deliberately.

Everything here is INERT until used: registering the persona grants no
capability — cabinet turns stay default-deny no-tools, delegation (WS3+)
stays behind its own flag, and all existing mutation gates are untouched.

No exception escapes :func:`seed_cofounder_persona`; every outcome is a
:class:`SeedResult` (exit code 1 only for ``error``).
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

# Boot-shim (PRP-7a): persona env overrides must apply BEFORE any
# config-touching import resolves paths (this module writes profile state,
# so the target root must be the persona-resolved one).
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

logger = logging.getLogger(__name__)

COFOUNDER_PERSONA_ID = "cofounder"
COFOUNDER_DISPLAY_NAME = "Co-Founder"
COFOUNDER_ROLE = (
    "Runs the company portfolio — reads the repos, goals, and dispatch "
    "history, sets the day's agenda, and delegates to the department-head "
    "personas (propose-first)"
)

OUTCOME_CREATED = "created"
OUTCOME_UPDATED = "updated"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_REFUSED = "refused"
OUTCOME_ERROR = "error"

# Identity files this seeder owns content for. Everything else the lifecycle
# scaffold created stays scaffold (the operator fills it, or learning does).
_SOUL_FILE = "SOUL.md"
_MEMORY_FILE = "MEMORY.md"

COFOUNDER_SOUL = """\
# SOUL.md — Co-Founder

## Who You Are

You are the Homie — the operator's co-founder, the main guy. This profile
is your CABINET SEAT and work-loop voice; the default chat on Telegram,
Discord, and mobile is the same character (its soul lives in the operator
vault). One guy, several rooms — never talk about "the cofounder" in the
third person.

You hold the whole portfolio in your head: every tracked repo, the goals,
what shipped yesterday, what's stuck, and which department-head homie
should be on what today. You are not a chatbot with a title; you run the
company's day so the operator can run the vision.

## Voice

Direct, warm, hype when something ships, zero corporate filler. You talk
like a partner, not a report: "yo — SEO homie found the YourBusiness drop,
already drafted the fix plan, say the word." Lead with the state of the
business, then the single next move. Short lines. No hedging.

## How You Operate

- **Propose first.** Your agenda lines are PROPOSALS until the operator
  approves (or the autonomy flag is flipped for you). Never claim work was
  executed unless the portfolio state proves it.
- **The Portfolio Digest is your eyes.** Cabinet turns inject it — the
  latest agenda, active co-founder projects, tracked repos. Answer from it.
  If the digest is absent, say what you'd need instead of guessing.
- **Delegate, don't do.** Department heads (sales, marketing, SEO,
  outbound, ...) execute; Archon is their hands. You assign, track, and
  report. Building a project inline yourself is a rule violation — a chat
  reply or project-file edit is an INSTRUCTION to the orchestrator.
- **Standups:** when the room asks, give portfolio state — what moved,
  what's blocked, what you propose next. One tight update, not a memo.
- **The machinery:** morning agenda card (propose-only) -> the operator
  approves lines with `/cofounder run <n>` -> homies execute (drafts land
  in the vault's cofounder/deliverables/; code goes through Archon,
  PR-for-review) -> results flow back as pulse cards + the evening
  checkout. `/cofounder agenda` shows today's live line statuses.
- **Escalate like a partner.** Blocked, awaiting-human, or a decision above
  your pay grade — flag it plainly and say what you recommend.

## Boundaries (non-negotiable)

- You never merge code, never post/send/dial externally, never mint "done"
  — executable checks and operator approval do.
- Delegation grants WORK, never new capabilities: every persona keeps its
  own default-deny gates no matter what you assign.
- If a capability is off (kill switch, dormant flag), you say so and stop —
  you never route around a gate.
"""

COFOUNDER_MEMORY = """\
# MEMORY.md — Co-Founder

_Long-lived facts the co-founder persona has earned. The learning loop
appends here; the operator curates._

## Standing Orientation

- Portfolio truth lives in the operator vault: REPOSITORIES.md (the repo
  index), repositories/<slug>.md (per-repo pages), GOALS.md, and
  COFOUNDER-PROJECTS.md (active project index).
- My daily agenda artifacts land in the vault at cofounder/agendas/ — the
  newest one is injected into my cabinet turns as the Portfolio Digest.
- My agenda is propose-only until the operator approves a line ("run it")
  or flips the delegation flag.
"""


@dataclass
class SeedResult:
    """What one seeding run did. ``error`` is the only non-zero exit code."""

    outcome: str
    profile_created: bool = False
    config_changes: list[str] = field(default_factory=list)
    identity_written: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.outcome == OUTCOME_ERROR else 0


def seed_cofounder_persona(
    *,
    force: bool = False,
    dry_run: bool = False,
    persona_id: str = COFOUNDER_PERSONA_ID,
) -> SeedResult:
    """Create/refresh the cofounder persona profile. Never raises.

    ``force`` overwrites the seeder-owned identity files even when the
    operator edited them (config.yaml merge stays missing-keys-only either
    way). ``dry_run`` reports what WOULD change and writes nothing.
    """
    try:
        from security import kill_switches  # Rule 3: module-attribute lookup

        try:
            kill_switches.requireEnabled(
                "persona_mutation", caller="cofounder.persona_seed"
            )
        except kill_switches.KillSwitchDisabled:
            logger.info("cofounder.persona: refused by persona_mutation kill switch")
            return SeedResult(outcome=OUTCOME_REFUSED)

        from personas import core as personas_core
        from personas import lifecycle as personas_lifecycle

        profile_root = (
            personas_core.get_default_homie_root() / "profiles" / persona_id
        )
        profile_created = False
        if not profile_root.is_dir():  # Rule 2: physical state decides
            if dry_run:
                logger.info(
                    "cofounder.persona: [dry-run] would create profile %r",
                    persona_id,
                )
            else:
                # The gated lifecycle path (persona_mutation re-checked +
                # audit row inside). no_alias: the cofounder needs no CLI
                # wrapper — cabinet/chat + /cofounder are his surfaces.
                personas_lifecycle.create_profile(persona_id, no_alias=True)
            profile_created = True

        config_changes = _merge_config(persona_id, dry_run=dry_run)
        identity_written = _seed_identity_files(
            persona_id, profile_root, force=force, dry_run=dry_run
        )

        if profile_created:
            outcome = OUTCOME_CREATED
        elif config_changes or identity_written:
            outcome = OUTCOME_UPDATED
        else:
            outcome = OUTCOME_UNCHANGED
        logger.info(
            "cofounder.persona: %s%s (config: %s; identity: %s)",
            "[dry-run] " if dry_run else "",
            outcome,
            ", ".join(config_changes) or "none",
            ", ".join(identity_written) or "none",
        )
        return SeedResult(
            outcome=outcome,
            profile_created=profile_created,
            config_changes=config_changes,
            identity_written=identity_written,
        )
    except Exception as exc:  # the whole-run wrap: nothing escapes the caller
        logger.exception("cofounder.persona: seeding failed")
        return SeedResult(
            outcome=OUTCOME_ERROR, error=f"{type(exc).__name__}: {exc}"
        )


def _merge_config(persona_id: str, *, dry_run: bool) -> list[str]:
    """Fill MISSING config.yaml keys (strict-read RMW; operator keys win).

    Raises to the caller's wrap on a malformed file — a config that cannot
    be strict-read must surface as an error, never be silently rewritten
    (the ``set_persona_learning`` lesson).
    """
    import yaml

    from personas import services as personas_services

    cfg = personas_services.read_profile_config(persona_id, strict=True)
    changes: list[str] = []

    persona = cfg.get("persona")
    if not isinstance(persona, dict):
        persona = {}
        cfg["persona"] = persona
        changes.append("persona")
    for key, value in (
        ("id", persona_id),
        ("name", COFOUNDER_DISPLAY_NAME),
        ("display_name", COFOUNDER_DISPLAY_NAME),
        ("role", COFOUNDER_ROLE),
    ):
        if key not in persona:
            persona[key] = value
            changes.append(f"persona.{key}")

    cabinet = cfg.get("cabinet")
    if not isinstance(cabinet, dict):
        # The block's PRESENCE is cabinet eligibility (roster contract).
        cabinet = {}
        cfg["cabinet"] = cabinet
        changes.append("cabinet")
    if "tools" not in cabinet:
        cabinet["tools"] = []  # default-deny: no cabinet tools
        changes.append("cabinet.tools")
    if "portfolio_context" not in cabinet:
        cabinet["portfolio_context"] = True
        changes.append("cabinet.portfolio_context")

    learning = cfg.get("learning")
    if not isinstance(learning, dict):
        learning = {}
        cfg["learning"] = learning
        changes.append("learning")
    if "enabled" not in learning:
        learning["enabled"] = True
        changes.append("learning.enabled")

    if not changes or dry_run:
        return changes

    personas_services.validate_config_dict(cfg)
    config_path = personas_services.get_profile_config_path(persona_id)
    _atomic_write(
        config_path,
        yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False),
    )
    return changes


def _seed_identity_files(
    persona_id: str,
    profile_root: Path,
    *,
    force: bool,
    dry_run: bool,
) -> list[str]:
    """Write the seeder-owned identity files under the never-clobber rule."""
    from personas import lifecycle as personas_lifecycle

    memory_dir = profile_root / "memory"
    written: list[str] = []
    for fname, body in ((_SOUL_FILE, COFOUNDER_SOUL), (_MEMORY_FILE, COFOUNDER_MEMORY)):
        path = memory_dir / fname
        if not _may_write_identity(
            path, fname, persona_id, personas_lifecycle, force=force
        ):
            continue
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, body)
        written.append(fname)
    return written


def _may_write_identity(
    path: Path, fname: str, persona_id: str, lifecycle_mod, *, force: bool
) -> bool:
    """True when the file is missing, effectively empty, or still the
    generic lifecycle scaffold seed. Operator content only yields to
    ``force``. An unreadable scaffold comparison degrades to NOT writing
    (never clobber on uncertainty)."""
    if force:
        return True
    try:
        if not path.exists():
            return True
        current = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not current.strip():
        return True
    try:
        scaffold = lifecycle_mod._seed_identity_body(fname, persona_id)
    except Exception:
        return False
    return current == scaffold


def _atomic_write(path: Path, content: str) -> None:
    """Write atomically via shared.atomic_write_text (consolidated 2026-07-07)."""
    from shared import atomic_write_text

    atomic_write_text(path, content)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cofounder.persona",
        description="Seed the cofounder persona profile (idempotent).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the seeder-owned identity files even if operator-edited",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="dry run: report what would change, write nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = seed_cofounder_persona(force=args.force, dry_run=args.test)
    logger.info(
        "cofounder.persona: outcome=%s created=%s config=%d identity=%d",
        result.outcome,
        result.profile_created,
        len(result.config_changes),
        len(result.identity_written),
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
