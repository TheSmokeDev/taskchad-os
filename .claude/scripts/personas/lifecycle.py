"""Profile lifecycle commands — create / list / show / delete / use / init-archon.

Phase 2 / PRP-7b Workstream 1 (lifecycle-core). Owns the operator-facing
profile commands that sit on top of Phase 1's frozen 12-helper public API.

Public-API contract (R1 M6 — FROZEN):
    ``personas.__all__`` is unchanged from Phase 1. New helpers in this
    module are NOT re-exported through ``personas/__init__.py``. Callers
    import them directly:

        from personas.lifecycle import create_profile, ProfileInfo

    NOT::

        personas.create_profile  # would require widening __all__

Module exports (consumed by ``chat/cli.py`` Click handlers + tests):
    LifecycleError              — narrow operator-facing exception
    ProfileInfo                 — dataclass: per-profile summary
    create_profile(name, ...)   — bootstrap a new profile
    list_profiles()             — walk filesystem -> ProfileInfo list
    show_profile(name)          — single-profile lookup
    delete_profile(name, ...)   — quiesce + rmtree (delete-lock wrapped)
    use_profile(name)           — set sticky active_profile
    init_archon(name)           — Phase 2 stub for archon spine

Anti-pattern compliance:
    - Rule 1: ``registered_subcommands=None`` resolved in ``create_profile``
      body; OS install flags default False; no ``config.X`` def-time binding.
    - Rule 2: every existence / containment / lock check reads physical
      state. ``_profile_root`` resolves the named-profile root via Phase 1's
      ``get_default_homie_root()`` on every call.
    - Rule 3: N/A — no Langfuse calls here.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .activity import (
    get_active_profile_name,
    is_default_profile,
    read_active_profile,
    set_active_profile,
)
from .atomic import (
    _acquire_delete_lock,
    is_pid_alive,
    quiesce_profile,
)
from .core import (
    _PERSONA_ID_RE,
    get_default_homie_root,
    get_default_paths,
    get_persona_paths,
    validate_persona_name,
)

# NOTE: ``_PERSONA_ID_RE`` is a private name in ``personas.core``. Phase 2's
# internal lifecycle / clone / wrappers / atomic modules are part of the
# same ``personas/`` package, so importing private members from siblings is
# the documented exception (PRP-7b §1948 — same-package private import
# allowance). The public-API guard at
# ``tests/test_personas_public_api.py:205-238`` enforces "no production
# code OUTSIDE ``personas/`` imports private personas helpers" — same-package
# imports are not in scope for that ban. Same-package private use is the
# standard stdlib pattern.


# =============================================================================
# EXCEPTION CLASS
# =============================================================================


class LifecycleError(RuntimeError):
    """User-facing operator error from profile lifecycle commands.

    Raised for KNOWN, expected error states the user can act on:
      - OS mismatch flags (``--launchd`` on non-darwin, ``--systemd`` on
        non-linux)
      - Wrapper rollback after default-mode wrapper-write failure
      - Path-traversal / symlink rejection in ``delete_profile``
      - Profile name collision with a CLI subcommand
      - State-file lock holders alive during delete
      - Archive extraction refused on traversal / symlink / multi-root
      - Concurrent delete (``.delete.lock`` already held)

    NOT raised for: implementation bugs, programming errors, unexpected I/O
    failures. Those propagate as bare ``RuntimeError`` (or whatever the cause
    raises) so tests catch them and the developer sees a real traceback.

    This subclass IS a ``RuntimeError`` so existing ``except RuntimeError``
    catches still match — but framework code uses ``LifecycleError`` to mark
    operator-facing failures specifically.

    Click handler boundary (``chat/cli.py``)::

        except (LifecycleError, ValueError, FileExistsError, FileNotFoundError):
            raise click.ClickException(...)

    Bare ``RuntimeError`` is intentionally NOT caught — it crashes loudly so
    bugs surface with a traceback.
    """


# =============================================================================
# CANONICAL PRD INVENTORY (R1 B2 — sourced verbatim from PRD §3.1+§8.1)
# =============================================================================
# These three constants are the source of truth for what a freshly-created
# profile looks like on disk. The lifecycle test
# ``tests/test_persona_lifecycle.py::test_create_profile_seeds_full_prd_inventory``
# asserts every entry below resolves to an existing path after
# ``create_profile("sales")`` returns.

# Identity markdown files seeded under ``<profile>/memory/`` (PRD §3.1).
# Each file gets a sensible empty body + frontmatter so providers loading
# them via ``bootstrap.py`` see a coherent (if empty) identity substrate.
_REQUIRED_IDENTITY_FILES: tuple[str, ...] = (
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "GOALS.md",
    "SELF.md",
    "WORKING.md",
    "HABITS.md",
    "HEARTBEAT.md",
    "INDEX.md",
    "LOG.md",
    "BACKLOG.md",
    "SAFETY.md",
    "SCHEMA.md",
    "MOC-Concepts.md",
    "MOC-Connections.md",
)

# Lock files (created on-demand by ``living_memory.py`` +
# ``entity_extractor.py``; Phase 2 does not pre-create — they're
# consumer-managed). Documented here for inventory completeness only.
_IDENTITY_LOCK_FILES: tuple[str, ...] = ("LOG.md.lock", "WORKING.md.lock")

# Memory subdirectories seeded under ``<profile>/memory/`` (PRD §3.1, §8.1).
_REQUIRED_MEMORY_DIRS: tuple[str, ...] = (
    "concepts",
    "connections",
    "daily",
    "weekly",
    "drafts/active",
    "drafts/sent",
    "drafts/expired",
    "finances",
    "raw",
    "teams",
    "_state",
    "_archive",
    "_canvas",
    "_dashboards",
    "_templates",
    "archive",
    "docs",
    "research",
)

# Top-level profile directories seeded under ``<profile>/`` (PRD §8.1).
_REQUIRED_PROFILE_DIRS: tuple[str, ...] = (
    "memory",
    "data",
    "state",
    "credentials",
    "logs",
    "run",
    "archon",
    "home",
    "cron",
    "sessions",
    "skills",
    "workspace",
    "codex-worktrees",
    "claude-worktrees",
)


# =============================================================================
# PRIVATE ROOT HELPERS (R1 M6 — Phase 1 __all__ stays frozen)
# =============================================================================
# These two helpers are the SINGLE source of truth for resolving the
# named-profile root. They are intentionally NOT in
# ``personas/__init__.py:__all__``; new public root-resolution helpers were
# explicitly disallowed by R1 M6 to keep the Phase 1 surface frozen.


def _profile_root(name: str) -> Path:
    """Return the on-disk root for a NAMED profile.

    R1 B1 + M6 — single source of truth for the named-profile root.
    NOT a public helper. Resolves to
    ``<default-homie-root>/profiles/<name>`` via Phase 1's
    ``get_default_homie_root()``.

    NOT valid for ``name == "default"`` or ``name == "custom"`` — those are
    handled by ``get_persona_paths(name)`` directly. The lifecycle layer
    rejects ``"default"`` before reaching this helper (see ``delete_profile``
    and ``create_profile`` guards).
    """
    return get_default_homie_root() / "profiles" / name


def _profiles_root() -> Path:
    """Return the parent directory holding all named profiles."""
    return get_default_homie_root() / "profiles"


def _default_install_root_for_clone() -> Path:
    """Return the legacy install-dir root used as a clone source.

    R1 M1 — when ``clone_from == "default"``, the source is NOT a profile
    under ``~/.homie/profiles/``; it's the legacy install-dir layout
    (``<install>/vault/memory/`` and friends). This helper returns the
    parent of ``get_default_paths()["memory"]`` — i.e. the install root —
    so the clone copy can walk the canonical default layout.
    """
    # ``get_default_paths()["memory"]`` is ``<repo>/vault/memory``;
    # parent.parent is ``<repo>/`` (the install root). The clone helper
    # consumes this as if it were a profile root.
    return get_default_paths()["memory"].parent.parent


def _count_skills(skills_dir: Path) -> int:
    """Return the count of skills in *skills_dir*.

    A "skill" is a subdirectory containing a ``SKILL.md`` file (the
    Anthropic Claude Code skill convention). Returns 0 for missing /
    empty / non-directory paths — fail-soft so ``list_profiles`` never
    raises.
    """
    if not skills_dir.is_dir():
        return 0
    try:
        return sum(
            1
            for entry in skills_dir.iterdir()
            if entry.is_dir() and (entry / "SKILL.md").exists()
        )
    except OSError:
        return 0


def _seed_identity_body(filename: str, profile_name: str) -> str:
    """Return a sensible empty body for a freshly-seeded identity file.

    Keeps the file syntactically valid (frontmatter + a heading) so any
    bootstrap consumer that loads it sees a coherent shape rather than an
    empty string. The exact content is intentionally minimal — Phase 2 is
    not in the business of authoring per-profile identity prose; that's
    the operator's job.
    """
    title = filename[: -len(".md")] if filename.endswith(".md") else filename
    return (
        f"---\n"
        f"profile: {profile_name}\n"
        f"identity_file: {filename}\n"
        f"---\n"
        f"\n"
        f"# {title}\n"
        f"\n"
        f"<!-- Seeded by `thehomie profile create {profile_name}`. "
        f"Author this file with profile-specific content as appropriate. -->\n"
    )


# =============================================================================
# DATACLASSES
# =============================================================================


@dataclass
class ProfileInfo:
    """Summary information about a single profile (mirrors Hermes' shape).

    Used by ``list_profiles()`` and ``show_profile()`` to expose
    per-profile state to Click handlers and the MC dashboard.

    Field semantics:
        - ``name``: profile identifier (matches ``_PERSONA_ID_RE``).
        - ``path``: on-disk root (``_profile_root(name)`` for named
          profiles; install root for the default profile).
        - ``is_default``: True iff this is the default install-dir profile.
        - ``bot_running``: True iff ``<path>/run/bot.pid`` (or
          install-dir analog) names a live process. Reads physical state
          via ``is_pid_alive`` (Rule 2).
        - ``has_env``: True iff ``<path>/.env`` exists.
        - ``skill_count``: count of skill directories under ``<path>/skills``.
        - ``alias_path``: optional pointer to the wrapper script
          (``~/.local/bin/<name>-homie`` etc.) — Phase 2 leaves this
          ``None``; populated by the wrapper layer when callers want to
          surface alias state.
    """

    name: str
    path: Path
    is_default: bool
    bot_running: bool
    has_env: bool
    skill_count: int
    alias_path: Optional[Path] = None


# =============================================================================
# CREATE
# =============================================================================


def create_profile(
    name: str,
    *,
    clone: bool = False,
    clone_from: Optional[str] = None,
    clone_all: bool = False,
    clone_secrets: bool = False,
    no_alias: bool = False,
    best_effort_alias: bool = False,
    install_launchd: bool = False,
    install_systemd: bool = False,
    registered_subcommands: Optional[frozenset[str]] = None,
) -> ProfileInfo:
    """Create a new persona profile.

    Hermes anchor: ``hermes_cli/profiles.py:372-463`` (create_profile).
    Deviation: ``--clone`` strips ``.env`` tokens by default (Hermes copies
    verbatim); ``--clone-secrets`` opts back into Hermes-faithful behavior.

    R1 B4 contract (OS-flag pre-validation + rollback):
      - ``--launchd`` on non-darwin / ``--systemd`` on non-linux raises
        ``LifecycleError`` BEFORE creating any files (no partial profile).
      - Wrapper-generation failure with default flags ROLLS BACK the
        partial profile dir AND raises ``LifecycleError``.
      - ``no_alias=True`` skips wrapper entirely.
      - ``best_effort_alias=True`` downgrades wrapper failure to a stderr
        warning, keeping the profile dir intact (legacy behavior).

    R1 B3 contract (wrapper points at NEW profile, not active process):
      - The wrapper alias is built against the NEW profile's root
        (``_profile_root(name)``), NOT the active process profile's
        ``HOMIE_HOME``. The wrapper module accepts an explicit
        ``profile_root`` parameter so this works regardless of which
        profile is currently active.

    R1 B2 contract (full PRD identity inventory):
      - Every entry in ``_REQUIRED_PROFILE_DIRS``, ``_REQUIRED_MEMORY_DIRS``,
        and ``_REQUIRED_IDENTITY_FILES`` is created. Identity files get
        sensible empty bodies; the lifecycle test asserts the complete
        inventory.

    R1 M1 contract (clone_from="default"):
      - ``clone_from == "default"`` is special-cased BEFORE
        ``validate_persona_name(clone_from)`` runs (validate would reject
        ``"default"`` because it's in ``_RESERVED``).

    Returns a ``ProfileInfo`` for the freshly-created profile.
    """
    # --- Step 0: OS-flag pre-validation (R1 B4 + R3 NNM3) ---------------
    # Fail FAST on OS-mismatched flags BEFORE any filesystem work so
    # there's no partial profile dir to roll back.
    if install_launchd and sys.platform != "darwin":
        raise LifecycleError(
            "--launchd is only valid on macOS (darwin); "
            f"current platform is {sys.platform!r}. No profile created."
        )
    if install_systemd and sys.platform != "linux":
        raise LifecycleError(
            "--systemd is only valid on Linux; "
            f"current platform is {sys.platform!r}. No profile created."
        )

    # --- Step 1: validate name (R1 M2 — pass live Click registry) -------
    # ``registered_subcommands`` is None-sentinel-defaulted (Rule 1) so
    # the resolution happens in ``validate_persona_name`` body.
    validate_persona_name(name, registered_subcommands=registered_subcommands)
    # ``name == "default"`` is already caught by ``validate_persona_name``
    # because "default" lives in ``core._RESERVED``. Defense in depth:
    if name == "default":
        raise ValueError(
            "Cannot create profile 'default' — it is the built-in profile."
        )

    # --- Step 2: compute target dir via the private root helper ---------
    profile_dir = _profile_root(name)
    if profile_dir.exists():
        raise FileExistsError(
            f"Profile '{name}' already exists at {profile_dir}"
        )

    # --- Step 3: resolve clone source (R1 M1 — special-case "default") --
    # R-post-build F5: track ``source_memory_dir`` separately so the
    # default-source case can rewrite ``memory/*`` lookups to land inside
    # ``get_default_paths()["memory"]`` (the real vault/memory dir).
    # Without this, light-clone helpers look for ``<install>/memory/SOUL.md``
    # — which doesn't exist — and silently seed empty identity files
    # instead of cloning the default profile's content.
    source_dir: Optional[Path] = None
    source_memory_dir: Optional[Path] = None
    if clone or clone_all or clone_from:
        if clone_from is not None:
            if clone_from == "default":
                # R1 M1 fix: do NOT call ``validate_persona_name("default")``
                # — ``core._RESERVED`` rejects it. "default" is a
                # legitimate clone SOURCE (the legacy install-dir layout).
                source_dir = _default_install_root_for_clone()
                source_memory_dir = get_default_paths()["memory"]
            else:
                validate_persona_name(clone_from)
                source_dir = _profile_root(clone_from)
        else:
            # No explicit source — clone from the active profile.
            active = get_active_profile_name()
            if active == "default":
                source_dir = _default_install_root_for_clone()
                source_memory_dir = get_default_paths()["memory"]
            elif active == "custom":
                # Custom profiles are rooted at HOMIE_HOME directly.
                from .core import get_homie_home

                source_dir = get_homie_home()
            else:
                source_dir = _profile_root(active)
        if not source_dir.is_dir():
            raise FileNotFoundError(
                f"Source profile '{clone_from or 'active'}' does not "
                f"exist at {source_dir}"
            )

    # --- Step 4-6: destructive-create block (rollback on wrapper fail) --
    # Everything below is wrapped in a try/except so we can roll back the
    # partial profile dir on wrapper failure (R1 B4). On clean success we
    # return ``profile_dir``.
    try:
        # Step 4: bootstrap directory tree (R1 B2 — full PRD inventory).
        if clone_all and source_dir is not None:
            # WS3 owns clone helpers; lazy import keeps WS1 buildable even
            # if WS3 hasn't shipped yet (the integration is at-call-site).
            #
            # R-post-build NF1 — when the clone source is the DEFAULT
            # profile (``source_memory_dir is not None`` <=> the caller
            # resolved source via ``_default_install_root_for_clone()``),
            # the source root IS the install repo. A naive
            # ``_copytree_with_strip(install_root, dest)`` would recursive-
            # copy the entire codebase: ``.git/``, nested
            # ``.claude/scripts/.env``, ``integrations/`` credentials,
            # workspace code, etc. ``_copytree_with_strip``'s ignore set
            # only filters runtime transients (locks, pids, logs) — it
            # does NOT know about secret-shaped paths or the install-dir
            # layout.
            #
            # Fix: route default-source clone-all through the same staged
            # profile-shaped tree the export path uses
            # (``_stage_default_export_tree``). It maps the safe keys from
            # ``get_default_paths()`` (memory, state, logs, run, archon,
            # home, cron, sessions, skills) into a clean named-profile
            # layout, applies the export ignore filter (denies ``.env``
            # at every depth, ``credentials/`` / ``integrations/`` dirs,
            # token-shaped files, ``.git``, caches, venvs, worktrees), and
            # the ``_assert_no_secrets_in_staged_tree`` scan fails CLOSED
            # if anything secret-shaped survived. Same semantics, same
            # invariants — clone-all and export of "default" both land at
            # the named-profile shape with zero secret leak.
            #
            # NAMED-profile clone-all keeps using ``_copytree_with_strip``
            # — the install-repo problem only applies to default because
            # that's the only source whose root is the install repo.
            from .clone import (
                _assert_no_secrets_in_staged_tree,
                _copytree_with_strip,
                _stage_default_export_tree,
            )

            if source_memory_dir is not None:
                # Default-source clone-all — staged profile-shaped tree.
                _stage_default_export_tree(profile_dir)
                # Fail-closed validator (same scan the export path runs).
                _assert_no_secrets_in_staged_tree(profile_dir)
            else:
                # Named-profile clone-all — full copytree with strip.
                _copytree_with_strip(
                    source_dir, profile_dir, carry_secrets=clone_secrets
                )
            # R2 NM5 — clone-all inventory backfill. Cloning from a partial
            # / older profile can leave the dest missing Phase 2 required
            # dirs. Run the SAME inventory-bootstrap loop the non-clone
            # path runs so the destination always satisfies the full
            # Phase 2 contract, regardless of what the source had.
            for subdir in _REQUIRED_PROFILE_DIRS:
                (profile_dir / subdir).mkdir(parents=True, exist_ok=True)
            for memdir in _REQUIRED_MEMORY_DIRS:
                (profile_dir / "memory" / memdir).mkdir(
                    parents=True, exist_ok=True
                )
        else:
            profile_dir.mkdir(parents=True, exist_ok=True)
            for subdir in _REQUIRED_PROFILE_DIRS:
                (profile_dir / subdir).mkdir(parents=True, exist_ok=True)
            for memdir in _REQUIRED_MEMORY_DIRS:
                (profile_dir / "memory" / memdir).mkdir(
                    parents=True, exist_ok=True
                )
            if source_dir is not None:
                # Light-clone path: copy specific config files + memory
                # files, NOT the full tree.
                # R-post-build F5: pass ``source_memory_dir`` so a clone
                # from ``"default"`` reads identity files from the real
                # ``vault/memory/`` dir, not from the bogus
                # ``<install>/memory/`` (which doesn't exist).
                from .clone import _clone_config_files, _clone_subdir_files

                _clone_config_files(
                    source_dir,
                    profile_dir,
                    carry_secrets=clone_secrets,
                    source_memory_dir=source_memory_dir,
                )
                _clone_subdir_files(
                    source_dir,
                    profile_dir,
                    source_memory_dir=source_memory_dir,
                )

        # Step 5: seed identity files (R1 B2 + R2 NM5 — full PRD inventory).
        # For each file not already present (clone_all may have copied
        # some), write a sensible empty body with frontmatter so
        # bootstrap.py loaders see a coherent identity substrate. This
        # loop runs on BOTH clone_all and non-clone paths — clone_all
        # backfill ensures a partial source's missing identity files get
        # seeded.
        for fname in _REQUIRED_IDENTITY_FILES:
            path = profile_dir / "memory" / fname
            if not path.exists():
                path.write_text(
                    _seed_identity_body(fname, name), encoding="utf-8"
                )

        # Step 6: create wrapper alias unless suppressed (R1 B3).
        if not no_alias:
            from .wrappers import create_wrapper_alias

            try:
                create_wrapper_alias(
                    name,
                    profile_root=profile_dir,
                    install_launchd=install_launchd,
                    install_systemd=install_systemd,
                )
            except LifecycleError:
                # R3 NNM3: wrapper layer raised an operator-facing error
                # (e.g. OS-mismatch). Re-raise unchanged — the outer
                # rollback block catches it, cleans up the partial
                # profile dir, and re-raises so the Click handler
                # surfaces it as ``click.ClickException``.
                raise
            except Exception as exc:
                if best_effort_alias:
                    print(
                        f"Warning: wrapper alias creation failed "
                        f"(best-effort): {exc}",
                        file=sys.stderr,
                    )
                else:
                    # R1 B4 + R3 NNM3 — fatal wrapper failure is
                    # OPERATOR-FACING: the user can fix the underlying
                    # cause (disk full, perms, PATH writability). Wrap
                    # as ``LifecycleError`` so the Click handler's narrow
                    # catch surfaces it as ``click.ClickException``
                    # instead of crashing. The outer rollback block sees
                    # the LifecycleError and triggers
                    # ``shutil.rmtree(profile_dir)`` BEFORE re-raise.
                    raise LifecycleError(
                        f"Wrapper alias creation failed for profile "
                        f"'{name}': {exc} (rolled back partial profile "
                        f"dir at {profile_dir})"
                    ) from exc
    except Exception:
        # R1 B4 rollback path — clean up partial profile dir, then
        # re-raise the (possibly-wrapped) exception. The Click handler's
        # narrow catch list surfaces ``LifecycleError`` /
        # ``ValueError`` / ``FileExistsError`` / ``FileNotFoundError``
        # cleanly; bare ``RuntimeError`` or other unexpected types
        # propagate as crashes (R3 NNM3 invariant).
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
        raise

    # --- Step 7: build and return ProfileInfo ---------------------------
    return ProfileInfo(
        name=name,
        path=profile_dir,
        is_default=False,
        bot_running=False,  # freshly-created profile has no bot yet
        has_env=(profile_dir / ".env").exists(),
        skill_count=_count_skills(profile_dir / "skills"),
    )


# =============================================================================
# LIST / SHOW
# =============================================================================


def _build_default_profile_info() -> Optional[ProfileInfo]:
    """Return the default profile's ``ProfileInfo`` if installed, else None.

    The default profile lives at the install root (NOT under
    ``~/.homie/profiles/``); ``is_default_profile()`` does the physical
    check (Rule 2).
    """
    if not is_default_profile():
        return None
    paths = get_default_paths()
    install_root = paths["memory"].parent.parent  # <repo>/vault/memory -> <repo>
    bot_pid_path = paths["run"] / "bot.pid"
    bot_running = False
    if bot_pid_path.exists():
        try:
            pid_text = bot_pid_path.read_text(encoding="utf-8").strip()
            pid = int(pid_text.split()[0]) if pid_text else 0
            bot_running = pid > 0 and is_pid_alive(pid)
        except (ValueError, OSError):
            pass
    env_file = paths["env_file"]
    return ProfileInfo(
        name="default",
        path=install_root,
        is_default=True,
        bot_running=bot_running,
        has_env=env_file.exists(),
        skill_count=_count_skills(paths["skills"]),
    )


def list_profiles() -> list[ProfileInfo]:
    """Walk the filesystem (Rule 2 — no sidecar registry) and return
    one ``ProfileInfo`` per discovered profile.

    Includes the default profile via ``is_default_profile()`` check at the
    install root (NOT under ``~/.homie/profiles/``).

    Phase 2 scope cut: launchd / systemd state inspection is OUT OF SCOPE
    for ``list_profiles()`` (and ``show_profile()``). Phase 5 / Phase 7 may
    extend ``ProfileInfo`` with a ``service_state`` field. Today,
    ``bot_running`` reflects only the framework's bot-pid path.
    """
    profiles: list[ProfileInfo] = []

    default_info = _build_default_profile_info()
    if default_info is not None:
        profiles.append(default_info)

    profiles_root = _profiles_root()
    if profiles_root.is_dir():
        try:
            entries = sorted(profiles_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir() or not _PERSONA_ID_RE.match(entry.name):
                continue
            bot_pid_path = entry / "run" / "bot.pid"
            bot_running = False
            if bot_pid_path.exists():
                try:
                    pid_text = bot_pid_path.read_text(
                        encoding="utf-8"
                    ).strip()
                    pid = int(pid_text.split()[0]) if pid_text else 0
                    bot_running = pid > 0 and is_pid_alive(pid)
                except (ValueError, OSError):
                    pass
            profiles.append(
                ProfileInfo(
                    name=entry.name,
                    path=entry,
                    is_default=False,
                    bot_running=bot_running,
                    has_env=(entry / ".env").exists(),
                    skill_count=_count_skills(entry / "skills"),
                )
            )
    return profiles


def show_profile(name: str) -> ProfileInfo:
    """Return ``ProfileInfo`` for a single profile by name.

    Special-case ``name == "default"`` to return the install-dir
    ``ProfileInfo`` (matches ``list_profiles`` semantics).

    Raises ``FileNotFoundError`` if the named profile does not exist.
    """
    if name == "default":
        info = _build_default_profile_info()
        if info is None:
            raise FileNotFoundError(
                "Default profile is not installed (no SOUL.md in "
                "install dir)."
            )
        return info

    validate_persona_name(name)
    profile_dir = _profile_root(name)
    if not profile_dir.is_dir():
        raise FileNotFoundError(
            f"Profile '{name}' does not exist at {profile_dir}"
        )

    bot_pid_path = profile_dir / "run" / "bot.pid"
    bot_running = False
    if bot_pid_path.exists():
        try:
            pid_text = bot_pid_path.read_text(encoding="utf-8").strip()
            pid = int(pid_text.split()[0]) if pid_text else 0
            bot_running = pid > 0 and is_pid_alive(pid)
        except (ValueError, OSError):
            pass

    return ProfileInfo(
        name=name,
        path=profile_dir,
        is_default=False,
        bot_running=bot_running,
        has_env=(profile_dir / ".env").exists(),
        skill_count=_count_skills(profile_dir / "skills"),
    )


# =============================================================================
# DELETE
# =============================================================================


def delete_profile(name: str, *, yes: bool = False) -> Path:
    """Delete a profile — wrap quiesce + rmtree in delete-lock (R1 B1).

    Single-source-of-truth root resolution: ``_profile_root(name)``.

    Destructive-sequence invariant (Rule 2 — physical state guard)::

        # Containment + symlink already validated above.
        with _acquire_delete_lock(profile_root):
            quiesce_profile(name, profile_root)   # SIGTERM, units, state-locks
            remove_wrapper_alias(name)
            shutil.rmtree(profile_root)           # only after all checks pass
        # delete-lock auto-released on exit (success or exception)

    Lifecycle-level "Cannot delete the default profile." message is
    unreachable for the literal string ``"default"`` —
    ``validate_persona_name`` rejects it via ``core._RESERVED``. Kept as
    defense in depth in case ``_RESERVED`` is reduced later.

    Returns the profile path that was deleted (for caller logging).
    """
    validate_persona_name(name)
    if name == "default":
        raise ValueError("Cannot delete the default profile.")

    profile_dir = _profile_root(name)
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"Profile '{name}' does not exist.")

    # === R2 NM4 — containment + symlink check FIRST, before any side effect ===
    # If ``~/.homie/profiles/sales`` is a symlink (escaped via ``ln -s``
    # or NTFS junction), ``_acquire_delete_lock(profile_dir)`` would write
    # ``.delete.lock`` into the link TARGET (a writable dir outside the
    # profiles root). ``remove_wrapper_alias()`` would also run before we
    # check containment. The fix: refuse symlinks AND non-contained paths
    # BEFORE acquiring the lock or touching wrappers.
    target = profile_dir.resolve(strict=False)
    profiles_root = _profiles_root().resolve(strict=False)
    try:
        target.relative_to(profiles_root)
    except ValueError:
        raise LifecycleError(
            f"Refusing to delete profile '{name}' — path escapes "
            f"profiles root: {target} not under {profiles_root}"
        )
    if profile_dir.is_symlink():
        raise LifecycleError(
            f"Refusing to delete profile '{name}' — path is a symlink: "
            f"{profile_dir} (would follow into target outside profiles "
            f"root). Remove the symlink manually if intended."
        )
    if name in {"default", "", "."}:
        raise ValueError(f"Refusing to delete reserved name: {name}")

    # --- Confirmation prompt unless --yes -------------------------------
    if not yes:
        print(f"Profile: {name}\nPath:    {profile_dir}")
        try:
            confirm = input(f"Type '{name}' to confirm: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return profile_dir
        if confirm != name:
            print("Cancelled.")
            return profile_dir

    # === R1 B1 — wrap entire destructive sequence in delete-lock context ===
    # Containment + symlink already validated above (R2 NM4). The lock
    # file therefore lands inside ``profile_dir``, which IS contained.
    with _acquire_delete_lock(profile_dir):
        # Step 1+2+4 of quiesce ladder. Raises ``LifecycleError`` on
        # active state-locks (R3 NNM3 — narrow operator-facing class).
        # The ``_acquire_delete_lock(...)`` context exits via the
        # exception, releasing the delete-lock automatically; the
        # wrapper alias and rmtree below never run.
        quiesce_profile(name, profile_dir)

        # Remove wrapper alias (Hermes-pattern content-verify before
        # unlink). Lazy import for parallel WS1/WS3 build.
        from .wrappers import remove_wrapper_alias

        remove_wrapper_alias(name)

        # Step 5: rmtree (INSIDE the delete-lock — Rule 2 invariant).
        # Containment already proven above.
        shutil.rmtree(target)
    # delete-lock auto-released here.

    # Reset active_profile if it pointed at the just-deleted profile.
    active = read_active_profile()
    if active == name:
        active_path = get_default_homie_root() / "active_profile"
        try:
            active_path.unlink()
        except FileNotFoundError:
            pass

    return profile_dir


# =============================================================================
# USE
# =============================================================================


def use_profile(name: str) -> None:
    """Set the sticky active profile to *name*.

    Validates the name + checks the profile dir exists, then routes to
    Phase 1's ``set_active_profile()`` (which already does the atomic
    tmp + ``os.replace`` write).

    Special-case ``name == "default"``: writes the literal string
    ``"default"`` even though ``read_active_profile()`` treats that value
    as "no override" (Phase 1 contract). The write is still useful as a
    visible operator signal that the default was selected explicitly.

    Raises ``ValueError`` if the name fails validation.
    Raises ``FileNotFoundError`` if the profile dir does not exist.
    """
    if name == "default":
        # Default is a legitimate target but bypasses validate_persona_name
        # (which rejects "default" because it lives in _RESERVED).
        if not is_default_profile():
            raise FileNotFoundError(
                "Default profile is not installed (no SOUL.md in "
                "install dir)."
            )
        set_active_profile(name)
        return

    validate_persona_name(name)
    profile_dir = _profile_root(name)
    if not profile_dir.is_dir():
        raise FileNotFoundError(
            f"Profile '{name}' does not exist at {profile_dir}"
        )
    set_active_profile(name)


# =============================================================================
# INIT-ARCHON (Phase 2 stub — Phase 5 ships the real spine)
# =============================================================================


def init_archon(name: str) -> None:
    """Initialize the Archon spine for a profile (Phase 2 stub).

    Creates ``<profile_root>/archon/`` with the standard subdirectories
    and a stub ``config.yaml``. Phase 5 will replace this with the real
    Archon orchestrator config; Phase 2 ships the layout so downstream
    work can target the canonical paths.

    Subdirectories created:
        - workflows/   — workflow definitions (.archon/workflows/)
        - commands/    — command shortcuts (.archon/commands/)
        - artifacts/   — workflow artifacts (.archon/artifacts/)
        - ralph/       — Ralph DAG state per feature
        - worktrees/   — Archon-spawned worktrees

    Raises ``FileNotFoundError`` if the profile root does not exist.
    """
    if name == "default":
        # Default profile uses the install-dir .archon/.
        archon_dir = get_default_paths()["archon"]
    else:
        validate_persona_name(name)
        profile_dir = _profile_root(name)
        if not profile_dir.is_dir():
            raise FileNotFoundError(
                f"Profile '{name}' does not exist at {profile_dir}"
            )
        archon_dir = profile_dir / "archon"

    archon_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("workflows", "commands", "artifacts", "ralph", "worktrees"):
        (archon_dir / sub).mkdir(parents=True, exist_ok=True)

    config_path = archon_dir / "config.yaml"
    if not config_path.exists():
        # Phase 2 stub. Phase 5 owns the real spine.
        config_path.write_text(
            'archon:\n'
            '  enabled: true\n'
            '  version: "stub"\n',
            encoding="utf-8",
        )
