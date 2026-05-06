"""Persona-profile resolver core helpers.

Hermes-faithful persona resolver. Helpers in this module are pure functions
over Path/str — stdlib-only, no runtime dependency, import-safe at any time.
This is what lets ``config.py`` import ``personas`` without re-creating the
``runtime/__init__.py`` eager-load cycle (see PRP-7a "Import-Cycle Fix Plan").

Hermes anchors:
    - hermes_constants.py:11-18  -> get_hermes_home (env-on-every-call)
    - hermes_constants.py:21-58  -> get_default_hermes_root (Docker-aware root)
    - hermes_cli/profiles.py:33  -> persona id regex
    - hermes_cli/profiles.py:102-113 -> reserved + subcommand sets

Deliberate deviations (PRP-7a Hermes-Faithful Checklist):
    - ``_RESERVED`` adds "homie" / "thehomie" — owner's binary names differ
      from Hermes'.
    - ``get_default_paths`` has no Hermes parallel: The Homie ships install-dir
      back-compat; the helper preserves the legacy ``HOMIE_VAULT_DIR`` env
      override on the ``memory`` key (PRP-7a R1 B5).
    - ``get_persona_paths`` extends Hermes' ``_PROFILE_DIRS`` to match PRD §8.1.

Anti-patterns enforced (MEMORY.md "Code Review Patterns"):
    - Rule 1: no ``config.X`` value bound as a default arg. ``None`` sentinel
      pattern is used everywhere a tunable could plausibly be overridden.
    - Rule 2: physical-state, not meta — ``is_default_profile()`` lives in
      ``personas.activity`` and reads ``<install>/vault/memory/SOUL.md``,
      it does NOT consult ``~/.homie/active_profile`` (which is meta cache).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Hermes anchor: hermes_cli/profiles.py:33 — regex copied verbatim with the
# variable renamed for The Homie.
_PERSONA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Hermes anchor: hermes_cli/profiles.py:102-105 — set extended with The Homie's
# binary names ("homie", "thehomie") so a user can't accidentally pick a name
# that collides with the launcher.
_RESERVED = frozenset({
    "homie",
    "thehomie",
    "default",
    "test",
    "tmp",
    "root",
    "sudo",
})

# Hermes anchor: hermes_cli/profiles.py:108-113 — The Homie's Click subcommand
# seed (Phase 1 uses a static set; Phase 2 / PRP-7b will derive this from the
# live Click registry). Names here mirror ``cli_entry.py`` / ``chat/cli.py`` /
# scheduled-job CLIs.
_HOMIE_SUBCOMMANDS_SEED = frozenset({
    "chat", "model", "status", "diagnostics", "doctor", "setup",
    "heartbeat", "reflect", "weekly", "dream", "search", "ingest",
    "evolve", "convoy", "mailbox", "team",
    "profile", "archon", "session", "config",
})


def validate_persona_name(
    name: str,
    *,
    registered_subcommands: frozenset[str] | None = None,
) -> None:
    """Raise ``ValueError`` if *name* is not a valid persona identifier.

    Hermes anchor: hermes_cli/profiles.py:159-167 (validate_profile_name).
    Deviation: also blocks Click-subcommand collisions (Hermes does this in
    ``check_alias_collision`` at :188-196 — PRP-7a folds both checks into
    one helper because Phase 1 has no separate alias-collision concern yet).

    Anti-pattern Rule 1: ``registered_subcommands`` defaults to ``None`` and
    is resolved against ``_HOMIE_SUBCOMMANDS_SEED`` inside the body, so a
    runtime caller (Phase 2 / PRP-7b) can pass the live Click registry without
    needing a default-arg rebind.
    """
    subcommands = (
        registered_subcommands
        if registered_subcommands is not None
        else _HOMIE_SUBCOMMANDS_SEED
    )
    if not _PERSONA_ID_RE.match(name):
        raise ValueError(
            f"Invalid persona name '{name}': must match {_PERSONA_ID_RE.pattern}"
        )
    if name in _RESERVED:
        raise ValueError(f"Persona name '{name}' is reserved")
    if name in subcommands:
        raise ValueError(f"Persona name '{name}' collides with a CLI subcommand")


def _normalize_env_home(raw: str) -> Path:
    """Expand ``~`` and resolve a path string from an env var.

    PRP-7a R2 B1 / NB2 — without ``expanduser()``, on Windows
    ``Path("~/.homie/profiles/sales").resolve()`` becomes
    ``<cwd>\\~\\.homie\\profiles\\sales`` (literal ``~`` directory in cwd),
    which:
        - misclassifies the named ``sales`` profile as ``custom`` in
          ``get_active_profile_name()``
        - writes config/state under a relative ``~`` directory in the cwd

    ``strict=False`` keeps non-existent paths from raising — we use this
    helper for both comparison and join contexts where the target may not
    exist yet (fresh install, named profile not bootstrapped).
    """
    return Path(raw).expanduser().resolve(strict=False)


def get_default_homie_root() -> Path:
    """Return the root Homie directory for profile-level operations.

    Hermes anchor: hermes_constants.py:21-58 (get_default_hermes_root).

    Standard deployments: ``~/.homie``. Docker / custom deployments where
    ``HOMIE_HOME`` points outside ``~/.homie``: ``HOMIE_HOME`` itself (or its
    grandparent if it's a profile path of the form ``<root>/profiles/<name>``).

    Anti-pattern Rule 1: env reads happen on every call — never cached at
    module load. PRP-7a R2 B1 / NB2: every env-supplied path goes through
    ``_normalize_env_home()`` so literal ``~`` works on Windows.
    """
    native = (Path.home() / ".homie").resolve(strict=False)
    env_home = os.environ.get("HOMIE_HOME", "").strip()
    if not env_home:
        return native
    env_path = _normalize_env_home(env_home)
    try:
        env_path.relative_to(native)
        # HOMIE_HOME is under ~/.homie (default or named profile).
        return native
    except ValueError:
        pass
    # Docker / custom — check for `<root>/profiles/<name>` shape.
    if env_path.parent.name == "profiles":
        return env_path.parent.parent
    # Otherwise HOMIE_HOME itself is the root.
    return env_path


def get_homie_home() -> Path:
    """Return the active Homie home directory.

    Hermes anchor: hermes_constants.py:11-18 (get_hermes_home). Verbatim
    shape — env var renamed (``HERMES_HOME`` -> ``HOMIE_HOME``). Reads env
    every call (Anti-pattern Rule 1: no caching at module load).

    PRP-7a R2 B1 / NB2: the value is normalized via ``_normalize_env_home()``
    so a literal ``~`` is expanded before any caller does path math on it.
    """
    val = os.environ.get("HOMIE_HOME", "").strip()
    return _normalize_env_home(val) if val else get_default_homie_root()


def get_default_paths() -> dict[str, Path]:
    """Return the legacy install-dir paths used by the default profile.

    Used when ``get_active_profile_name()`` returns ``"default"``.

    Deliberate deviation (no Hermes parallel): Hermes always roots paths under
    ``~/.hermes``. The Homie has install-dir back-compat — for default-profile
    users (owner's repo, Engineering Homie), every path resolves to
    ``<install>/.../`` exactly like pre-PRP-7. PRP-7a §10.2 "default-profile
    invariant" is the entire reason this helper exists.

    PRP-7a R1 B5: ``HOMIE_VAULT_DIR`` env override is preserved on the
    ``memory`` key (existing ``config.py:23-31`` contract).

    Anti-pattern Rule 1: ``HOMIE_VAULT_DIR`` is read every call, NOT bound
    at def time. Path math derives from this file's location, not from any
    ``config.X`` constant.
    """
    # personas/core.py -> personas/ -> scripts/
    scripts_dir = Path(__file__).resolve().parent.parent
    # scripts/ -> .claude/ -> repo/
    project_root = scripts_dir.parent.parent

    # PRP-7a R1 B5 — preserve HOMIE_VAULT_DIR override on MEMORY_DIR.
    # ``config.py:23-31`` reads this env var today; the resolver inherits
    # that contract verbatim so default-profile users see no change.
    vault_override = os.environ.get("HOMIE_VAULT_DIR", "").strip()
    memory_dir = (
        Path(vault_override).expanduser().resolve(strict=False)
        if vault_override
        else (project_root / "TheHomie" / "Memory").resolve(strict=False)
    )

    data_dir = (project_root / ".claude" / "data").resolve(strict=False)
    state_dir = (data_dir / "state").resolve(strict=False)
    scripts_path = (project_root / ".claude" / "scripts").resolve(strict=False)

    return {
        "memory": memory_dir,
        "data": data_dir,
        "state": state_dir,
        "env_file": (scripts_path / ".env").resolve(strict=False),
        "credentials": (scripts_path / "integrations").resolve(strict=False),
        "logs": data_dir,
        "run": state_dir,
        "archon": (project_root / ".archon").resolve(strict=False),
        "home": (project_root / "home").resolve(strict=False),
        "cron": (scripts_path / "cron").resolve(strict=False),
        "sessions": (data_dir / "sessions").resolve(strict=False),
        "skills": (project_root / ".claude" / "skills").resolve(strict=False),
        "workspace": project_root.resolve(strict=False),
    }


def get_persona_paths(name: str) -> dict[str, Path]:
    """Return the path map for a given profile name.

    Hermes anchor: inferred from ``hermes_cli/profiles.py:_PROFILE_DIRS``
    (:36-50) — Hermes builds these per-profile dirs at create time; we
    return them lazily via Path joins so the resolver stays import-safe.

    PRP-7a R1 B1 — explicit selection DOMINATES physical-state detection:
        - ``"default"``  -> legacy install-dir paths (back-compat)
        - ``"custom"``   -> rooted at ``HOMIE_HOME`` directly (NOT under
                            ``<root>/profiles/custom/``)
        - ``"<name>"``   -> ALWAYS under ``~/.homie/profiles/<name>/``,
                            even if ``<install>/vault/memory/SOUL.md``
                            exists on the same machine.

    Anti-pattern Rule 1: profile root is computed on every call (no def-time
    binding to ``HOMIE_HOME`` or any ``config.X`` constant).
    """
    if name == "default":
        return get_default_paths()
    if name == "custom":
        # Custom profiles use HOMIE_HOME as the profile root itself
        # (NOT ``<root>/profiles/custom/`` — that would silently change
        # the layout for an explicit operator-set HOMIE_HOME=/some/path).
        profile_root = get_homie_home()
    else:
        # Named profile: ~/.homie/profiles/<name>/
        profile_root = get_default_homie_root() / "profiles" / name
    return {
        "memory": profile_root / "memory",
        "data": profile_root / "data",
        "state": profile_root / "state",
        "env_file": profile_root / ".env",
        "credentials": profile_root / "credentials",
        "logs": profile_root / "logs",
        "run": profile_root / "run",
        # PRP-7e R3 cascade fix: dotted ``.archon`` per Archon's discovery
        # convention. Dict KEY ``archon`` is preserved for back-compat with
        # consumers (``personas/lifecycle.py:904``, ``personas/archon.py``).
        "archon": profile_root / ".archon",
        "home": profile_root / "home",
        "cron": profile_root / "cron",
        "sessions": profile_root / "sessions",
        "skills": profile_root / "skills",
        "workspace": profile_root / "workspace",
    }
