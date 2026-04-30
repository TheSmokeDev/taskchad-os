"""Pre-import boot shim and subprocess-env helpers.

``apply_persona_override`` is the entry-point shim the 51 ``__main__`` files
call BEFORE importing ``config``. It pre-parses ``--profile/-p``, falls back
through the precedence chain (CLI flag > existing HOMIE_HOME > sticky
active_profile > physical default), validates, sets ``os.environ["HOMIE_HOME"]``,
and strips the flag from ``sys.argv`` so Click never sees it.

Hermes anchors:
    - hermes_cli/main.py:91-160       -> apply_persona_override (shim verbatim)
    - hermes_constants.py:115-138     -> get_subprocess_env (conditional shape)
    - hermes_cli/profiles.py:resolve_profile_env -> resolve_persona_env

Deliberate deviations:
    - Precedence chain (PRP-7a R2 NB1) — Hermes' shim does CLI -> sticky;
      The Homie's adds the rank-2 short-circuit for an existing
      ``HOMIE_HOME`` env var so a parent process / orchestrator / Docker
      can pin the profile without a stale ``~/.homie/active_profile``
      overriding it.
    - Source-split error handling (PRP-7a R3 NNB1) — explicit CLI errors
      ``sys.exit(1)``; sticky-meta errors warn-and-fall-back. Hermes hard-
      fails on every error path; The Homie's contract is that startup must
      survive any sticky-meta corruption.
    - ``get_subprocess_env`` returns ``dict[str, str]`` instead of
      ``str | None`` because The Homie's subprocess sites need a full env
      dict for ``subprocess.run(env=...)`` whereas Hermes uses
      ``get_subprocess_home()`` to set a single env var. Same conditional
      logic — set ``HOME`` (and ``USERPROFILE`` on win32) only when
      ``<HOMIE_HOME>/home`` exists on disk.

Anti-pattern enforcement:
    - Rule 1: no def-time binding to ``config.X`` constants. Every read of
      ``HOMIE_HOME`` and the active-profile file happens on call.
    - Failsafe: any unexpected exception inside ``apply_persona_override``
      warns to stderr and returns. Bugs in this module MUST NEVER prevent
      a CLI from starting (Hermes' contract at hermes_cli/main.py:139-145).
"""

from __future__ import annotations

import os
import sys

from .activity import read_active_profile
from .core import (
    _normalize_env_home,
    get_default_homie_root,
    get_homie_home,
    validate_persona_name,
)


def resolve_persona_env(name: str) -> str:
    """Return the absolute ``HOMIE_HOME`` path string for *name*.

    Hermes anchor: hermes_cli/profiles.py resolve_profile_env.

    PRP-7a R1 B1 — only ``"default"`` routes through the legacy install
    paths (via ``get_default_homie_root()``). Named profiles ALWAYS land
    under ``~/.homie/profiles/<name>/``. ``"custom"`` is never a name passed
    to this function — custom profiles already have ``HOMIE_HOME`` set
    explicitly by the parent context, so the shim doesn't need to resolve
    them.

    Raises ``FileNotFoundError`` if the named profile directory does not
    exist on disk. The caller (``apply_persona_override``) decides whether
    to ``sys.exit(1)`` (explicit CLI source) or warn-and-fall-back (sticky
    meta source) per PRP-7a R1 B4 / R3 NNB1.
    """
    if name == "default":
        return str(get_default_homie_root())
    profile_dir = get_default_homie_root() / "profiles" / name
    if not profile_dir.exists():
        raise FileNotFoundError(
            f"Profile '{name}' not found at {profile_dir}"
        )
    return str(profile_dir)


def apply_persona_override() -> None:
    """Pre-parse ``--profile/-p`` and set ``HOMIE_HOME`` before module imports.

    Hermes anchor: hermes_cli/main.py:99-160. Verbatim shape, with two
    deviations explicitly justified above (rank-2 short-circuit and
    source-split error handling).

    Precedence chain (PRP-7a R2 NB1 — ENFORCED):
        1. CLI flag: ``--profile <name>`` / ``-p <name>`` / ``--profile=<name>``
        2. Existing ``HOMIE_HOME`` env var
        3. Sticky ``~/.homie/active_profile`` meta file
        4. Physical default (no profile selected)

    Each rank takes precedence over every lower rank — once a higher-rank
    source provides a value, lower-rank sources are NOT consulted.

    Error handling (PRP-7a R3 NNB1 source split):
        - Rank 1 (explicit CLI):
            * ValueError (invalid name)        -> sys.exit(1) with Error:
            * FileNotFoundError (missing dir)  -> sys.exit(1) with Error:
        - Rank 3 (sticky meta):
            * ValueError (invalid name in file) -> warn + fall back
            * FileNotFoundError (missing dir)   -> warn + fall back
        - Any other exception                   -> warn + fall back

    The contract guarantees: an explicit CLI flag with a problem hard-fails
    so the user sees the error; sticky-meta corruption (stale or hand-edited
    ``~/.homie/active_profile``) NEVER bricks startup. PRD §14.13.
    """
    argv = sys.argv[1:]

    # Rank 1: CLI flag pre-parse.
    explicit_name: str | None = None  # came from --profile / -p (rank 1)
    sticky_name: str | None = None    # came from ~/.homie/active_profile (rank 3)
    consume = 0
    for i, arg in enumerate(argv):
        if arg in ("--profile", "-p") and i + 1 < len(argv):
            explicit_name = argv[i + 1]
            consume = 2
            break
        if arg.startswith("--profile="):
            explicit_name = arg.split("=", 1)[1]
            consume = 1
            break

    # Rank 2: existing HOMIE_HOME env var. If parent process / orchestrator
    # / Docker has already pinned HOMIE_HOME and there's no CLI flag, take
    # that value (after normalization) and return WITHOUT consulting sticky
    # meta. Prevents stale ``~/.homie/active_profile`` from overriding an
    # explicit env-set selection (PRP-7a R2 NB1).
    if explicit_name is None:
        existing_env = os.environ.get("HOMIE_HOME", "").strip()
        if existing_env:
            try:
                normalized = str(_normalize_env_home(existing_env))
                # Only rewrite if normalization actually changed the value
                # (literal ``~`` expansion, relative-path resolution, etc.).
                if normalized != existing_env:
                    os.environ["HOMIE_HOME"] = normalized
            except Exception as exc:  # never let normalization brick startup
                print(
                    f"Warning: HOMIE_HOME normalization failed ({exc}); "
                    f"using raw env value as-is",
                    file=sys.stderr,
                )
            return  # rank 2 wins — do NOT read sticky meta

    # Rank 3: sticky ``~/.homie/active_profile`` meta file.
    if explicit_name is None:
        try:
            sticky_name = read_active_profile()
        except Exception:
            # Defense in depth — read_active_profile is already tolerant,
            # but a bug in disk I/O must NEVER prevent startup.
            sticky_name = None

    profile_name = explicit_name if explicit_name is not None else sticky_name
    if profile_name is None:
        # Rank 4: no profile selected — fall through to physical default.
        return

    try:
        validate_persona_name(profile_name)
        homie_home = resolve_persona_env(profile_name)
    except ValueError as exc:
        # PRP-7a R3 NNB1 — split by source. Sticky meta with invalid
        # contents (e.g. uppercase ``Sales``, length >64, reserved name)
        # would otherwise hard-fail every startup until the file is
        # manually deleted. Same class as R1 B4.
        if explicit_name is not None:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(
            f"Warning: invalid active_profile '{profile_name}' — "
            f"ignoring and falling back to default. {exc}",
            file=sys.stderr,
        )
        return
    except FileNotFoundError as exc:
        # PRP-7a R1 B4 — sticky meta pointing at a missing profile dir
        # warns + falls back; explicit CLI selection hard-fails.
        if explicit_name is not None:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(
            f"Warning: active_profile points at missing profile "
            f"'{profile_name}' — ignoring and falling back to default. {exc}",
            file=sys.stderr,
        )
        return
    except Exception as exc:
        # Final failsafe — any other bug warns + falls back.
        # Hermes contract (hermes_cli/main.py:139-145): a bug in profile
        # resolution must NEVER prevent the CLI from starting.
        print(
            f"Warning: profile override failed ({exc}), using default",
            file=sys.stderr,
        )
        return

    os.environ["HOMIE_HOME"] = homie_home

    # Strip --profile / -p from sys.argv so Click / argparse don't choke.
    if consume > 0:
        for i, arg in enumerate(argv):
            if arg in ("--profile", "-p"):
                start = i + 1  # +1 because argv is sys.argv[1:]
                sys.argv = sys.argv[:start] + sys.argv[start + consume:]
                break
            if arg.startswith("--profile="):
                start = i + 1
                sys.argv = sys.argv[:start] + sys.argv[start + 1:]
                break


def get_subprocess_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env dict for ``subprocess.run(env=...)`` calls.

    Hermes anchor: hermes_constants.py:115-138 (get_subprocess_home).

    Conditional shape — when ``<HOMIE_HOME>/home/`` exists on disk,
    subprocesses get ``HOME`` (and ``USERPROFILE`` on win32) pointed at that
    per-profile home directory so system tools (git, ssh, gh, npm) write
    their configs into the profile's persistent volume instead of the
    OS-level ``~/``. When the directory doesn't exist, behavior is
    unchanged — return a copy of ``os.environ`` unmodified.

    NEVER mutates parent ``os.environ`` (Hermes contract — see
    hermes_constants.py:127-129).

    PRP-7a deviation (Hermes-Faithful Checklist): returns ``dict[str, str]``
    instead of ``str | None`` because The Homie's subprocess sites need a
    full env dict, and we set ``USERPROFILE`` alongside ``HOME`` on win32
    so PowerShell-spawned children resolve ``~`` correctly inside the
    profile home.
    """
    env = os.environ.copy()
    home_dir = get_homie_home() / "home"
    if home_dir.is_dir():
        env["HOME"] = str(home_dir)
        if sys.platform == "win32":
            env["USERPROFILE"] = str(home_dir)
    if extra_env:
        env.update(extra_env)
    return env
