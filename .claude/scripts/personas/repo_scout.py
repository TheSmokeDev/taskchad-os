"""Seed the ``repo-scout`` specialist persona.

Run manually from ``.claude/scripts``::

    uv run python -m personas.repo_scout
    uv run python -m personas.repo_scout --test

The seeder follows the canonical persona lifecycle and never creates a live
profile implicitly.  Config values are filled only when missing.  SOUL.md and
MEMORY.md are authored only when missing, empty, or still byte-equal to the
generic lifecycle scaffold; operator-authored identity is never overwritten.

Registration grants no external authority.  Channel turns remain no-tools,
learning starts disabled, and evaluations run as detached read-only jobs
through the /stars command surface — never from the channel itself.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

from personas import apply_persona_override

apply_persona_override()

logger = logging.getLogger(__name__)

REPO_SCOUT_PERSONA_ID = "repo-scout"
REPO_SCOUT_DISPLAY_NAME = "Repo Scout"
REPO_SCOUT_ROLE = (
    "Owns the starred-repo backlog, the weekly GitHub signal digest, and "
    "read-only repository evaluations; discusses findings in his channel and "
    "never executes evaluated code or performs external writes"
)

OUTCOME_CREATED = "created"
OUTCOME_UPDATED = "updated"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_REFUSED = "refused"
OUTCOME_ERROR = "error"

_SOUL_FILE = "SOUL.md"
_MEMORY_FILE = "MEMORY.md"

REPO_SCOUT_SOUL = """\
# SOUL.md — Repo Scout

## Who You Are

You are the Homie's repository scout: a sharp senior engineer who tracks the
operator's starred backlog, watches what is trending, and evaluates repos on
request. You hold strong opinions grounded in evidence — file trees,
manifests, commit recency, README claims — and you say "skip" as readily as
"adopt". Hype is not evidence.

## How You Operate

- Your knowledge lives in your memory: weekly GitHub Signal digests and
  repo-eval verdict notes are synced into your research memory. Ground every
  claim in them; when your memory has no entry for a repo, say so plainly.
- Recommend with a bridge to the operator's active work, never generic
  praise. "You're wiring X — this repo gives you Y" is the bar.
- The digest and eval pipelines run as background jobs, not in this channel.
  When asked to act, point at the command surface: /stars refresh,
  /stars eval <owner/repo>, /stars used <repo>, /stars snooze <repo>.
- Be direct about verdicts: adopt / try / skip, with the concrete reason and
  an effort estimate. A wrong confident verdict is worse than a hedged one.

## Boundaries

- You cannot run tools in this channel; you reason over text and your own
  memory only. Never claim to have cloned, fetched, or executed anything.
- Evaluations are read-only analysis of files. Repo code is NEVER executed,
  installed, or tested — no exceptions, and you never suggest otherwise.
- No external writes: no starring, unstarring, issues, comments, or browser
  use. The operator owns every action; you own the recommendation.
"""

REPO_SCOUT_MEMORY = """\
# MEMORY.md — Repo Scout

_Operator-curated facts for this specialist. Learning is disabled initially._

## Standing Orientation

- Weekly GitHub Signal digests are written to the vault under
  Memory/github-signal/ (dated YYYY-WNN.md) and synced into my
  memory/research/github-signal/ for recall.
- Repo evaluation verdict notes land beside the digests under
  github-signal/evals/ and are synced the same way.
- The command surface is /stars: status · refresh · eval <owner/repo> ·
  used <repo> · snooze <repo> [weeks] · trending. Refresh and eval run as
  detached jobs and deliver cards to Telegram and my Discord channel.
- Repo lifecycle: a pick marked "used" is never resurfaced; "snoozed" sleeps
  for N weeks; evaluation never changes used/snoozed status.
"""


@dataclass
class SeedResult:
    """Result of one seed pass; only ``error`` maps to a non-zero exit."""

    outcome: str
    profile_created: bool = False
    config_changes: list[str] = field(default_factory=list)
    identity_written: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.outcome == OUTCOME_ERROR else 0


def seed_repo_scout_persona(
    *,
    dry_run: bool = False,
    persona_id: str = REPO_SCOUT_PERSONA_ID,
) -> SeedResult:
    """Create or refresh the specialist profile without clobbering operator work."""

    try:
        from security import kill_switches

        try:
            kill_switches.requireEnabled("persona_mutation", caller="personas.repo_scout_seed")
        except kill_switches.KillSwitchDisabled:
            logger.info("repo-scout persona seed refused by persona_mutation kill switch")
            return SeedResult(outcome=OUTCOME_REFUSED)

        from personas import core as personas_core
        from personas import lifecycle as personas_lifecycle

        profile_root = personas_core.get_default_homie_root() / "profiles" / persona_id
        profile_created = False
        if not profile_root.is_dir():
            if dry_run:
                logger.info("[dry-run] would create profile %r", persona_id)
            else:
                personas_lifecycle.create_profile(persona_id, no_alias=True)
            profile_created = True

        config_changes = _merge_config(persona_id, dry_run=dry_run)
        identity_written = _seed_identity_files(persona_id, profile_root, dry_run=dry_run)

        if profile_created:
            outcome = OUTCOME_CREATED
        elif config_changes or identity_written:
            outcome = OUTCOME_UPDATED
        else:
            outcome = OUTCOME_UNCHANGED
        return SeedResult(
            outcome=outcome,
            profile_created=profile_created,
            config_changes=config_changes,
            identity_written=identity_written,
        )
    except Exception as exc:
        logger.exception("repo-scout persona seeding failed")
        return SeedResult(
            outcome=OUTCOME_ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )


def _merge_config(persona_id: str, *, dry_run: bool) -> list[str]:
    """Fill missing config keys after strict parse and schema validation."""

    import yaml

    from personas import services as personas_services

    cfg = personas_services.read_profile_config(persona_id, strict=True)
    personas_services.validate_config_dict(cfg)
    changes: list[str] = []

    persona = cfg.get("persona")
    if persona is None:
        persona = {}
        cfg["persona"] = persona
        changes.append("persona")
    for key, value in (
        ("id", persona_id),
        ("name", REPO_SCOUT_DISPLAY_NAME),
        ("display_name", REPO_SCOUT_DISPLAY_NAME),
        ("role", REPO_SCOUT_ROLE),
    ):
        if key not in persona:
            persona[key] = value
            changes.append(f"persona.{key}")

    cabinet = cfg.get("cabinet")
    if cabinet is None:
        cabinet = {}
        cfg["cabinet"] = cabinet
        changes.append("cabinet")
    if "tools" not in cabinet:
        cabinet["tools"] = []
        changes.append("cabinet.tools")

    learning = cfg.get("learning")
    if learning is None:
        learning = {}
        cfg["learning"] = learning
        changes.append("learning")
    if "enabled" not in learning:
        learning["enabled"] = False
        changes.append("learning.enabled")

    delegation = cfg.get("delegation")
    if delegation is None:
        delegation = {}
        cfg["delegation"] = delegation
        changes.append("delegation")
    if "repos" not in delegation:
        delegation["repos"] = []
        changes.append("delegation.repos")

    if not changes or dry_run:
        return changes

    personas_services.validate_config_dict(cfg)
    _atomic_write(
        personas_services.get_profile_config_path(persona_id),
        yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False),
    )
    return changes


def _seed_identity_files(
    persona_id: str,
    profile_root: Path,
    *,
    dry_run: bool,
) -> list[str]:
    """Author owned identity files only when they are still unowned."""

    from personas import lifecycle as personas_lifecycle

    memory_dir = profile_root / "memory"
    written: list[str] = []
    for filename, body in (
        (_SOUL_FILE, REPO_SCOUT_SOUL),
        (_MEMORY_FILE, REPO_SCOUT_MEMORY),
    ):
        path = memory_dir / filename
        if not _may_write_identity(path, filename, persona_id, personas_lifecycle):
            continue
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, body)
        written.append(filename)
    return written


def _may_write_identity(
    path: Path,
    filename: str,
    persona_id: str,
    lifecycle_module,
) -> bool:
    """Return true only for missing, empty, or generic scaffold identity."""

    try:
        if not path.exists():
            return True
        current = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not current.strip():
        return True
    try:
        scaffold = lifecycle_module._seed_identity_body(filename, persona_id)
    except Exception:
        return False
    return current == scaffold


def _atomic_write(path: Path, content: str) -> None:
    from shared import atomic_write_text

    atomic_write_text(path, content)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m personas.repo_scout",
        description="Seed the Repo Scout persona without overwriting operator identity.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="dry run: report what would change and write nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = seed_repo_scout_persona(dry_run=args.test)
    logger.info(
        "repo-scout persona: outcome=%s created=%s config=%d identity=%d",
        result.outcome,
        result.profile_created,
        len(result.config_changes),
        len(result.identity_written),
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
