"""Profile-scoped Archon spine integration (PRP-7 Phase 5 — PRP-7e).

Owns the real ``init_archon()`` body that replaces the Phase 2 stub at
``personas/lifecycle.py:init_archon``. Phase 2 only created the directory
skeleton + a 3-line stub config; Phase 5 ships:

    - profile-scoped capability config (PRD §11.1 verbatim shape)
    - shape-aware idempotency guard (Rule 2 — physical state, not meta)
    - shape-aware migration (preserves operator's custom keys)
    - atomic YAML write (Windows-safe — mirrors ``activity.set_active_profile``)
    - binary version detection + version-lock (R1 M4)
    - smoke workflow seeding (asset shipped by WS2a)

Public surface (consumed via direct submodule import — NOT re-exported
through ``personas/__init__.py``; the 12-helper ``__all__`` is FROZEN):

    Exceptions:
        ArchonError                   — base
        ArchonNotInstalledError       — exit 4 (PRD §12.3)
        ArchonVersionMismatchError    — exit 7 (Q3 NEW PRD §12.3 row)
        ArchonConfigShapeError        — exit 1

    Path resolvers:
        get_archon_config_path(name)        -> Path
        get_archon_worktree_dir(name)       -> Path
        get_archon_artifacts_dir(name)      -> Path
        get_archon_workflows_dir(name)      -> Path

    State helpers (Rule 2 — physical inspection):
        is_archon_initialized(name)         -> bool
        get_actual_config_shape(name)       -> dict | None

    Detection:
        detect_archon_binary(*, expected_version=None) -> tuple[Path, str]

    Initializer:
        init_archon(name, *, install_smoke=True, archon_version=None,
                    force=False, strict_version=False) -> Path

Anti-pattern compliance:
    - Rule 1: ``archon_version`` and ``expected_version`` use the ``None``
      sentinel pattern. Default arg never binds a tunable config value
      (no ``_DEFAULT_ARCHON_VERSION`` constant — version is detected on
      first init, R1 M4).
    - Rule 2: ``is_archon_initialized()`` reads + parses the actual
      ``config.yaml`` and runs a value-aware shape check (R3 NB2). Does
      NOT trust meta caches or sidecar markers.
    - Rule 3: N/A — no Langfuse calls in WS1 hot path.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from .core import (
    get_default_paths,
    get_persona_paths,
    validate_persona_name,
)
from .lifecycle import LifecycleError, _profile_root


# =============================================================================
# EXCEPTION HIERARCHY
# =============================================================================


class ArchonError(LifecycleError):
    """Base class for Phase 5 archon errors. Subclass of LifecycleError so
    the ``except (LifecycleError, ...)`` blocks at the existing CLI handler
    boundary continue to catch these without churn (WS2a narrows the catch
    list to surface specific exit codes for the install-state and
    version-mismatch subclasses)."""


class ArchonNotInstalledError(ArchonError):
    """Archon binary not on PATH or unparseable. Maps to PRD §12.3 exit 4."""


class ArchonVersionMismatchError(ArchonError):
    """Installed archon version does not match the expected/locked version.
    Maps to PRD §12.3 exit 7 (Q3 NEW row "Tool version mismatch")."""


class ArchonConfigShapeError(ArchonError):
    """Existing config.yaml is malformed beyond auto-migration. Maps to
    PRD §12.3 exit 1 (generic runtime error)."""


# =============================================================================
# MODULE CONSTANTS
# =============================================================================


# PRP-7e R3: directory name on disk is ``.archon`` (dotted) per Archon's
# discovery convention. The dict KEY in
# ``get_persona_paths(name)["archon"]`` is preserved for back-compat.
_ARCHON_SUBDIRS: tuple[str, ...] = (
    "workflows",
    "commands",
    "artifacts",
    "ralph",
    "worktrees",
)


# Locked field-order template for the PRD §11.1 capability config.
# Python 3.7+ preserves dict insertion order; ``yaml.safe_dump(sort_keys=False)``
# emits keys in this exact sequence. Tests assert the locked order verbatim.
#
# ``archon_version`` is a placeholder filled in by ``_build_capability_config``
# from detection or operator override — NOT a tunable default arg (Rule 1).
_CAPABILITY_CONFIG_TEMPLATE: dict = {
    "capabilities": {
        "archon": {
            "enabled": True,
            "binary": "archon",
            "archon_version": None,  # filled at build time
            "root": ".archon",
            "workflows_dir": ".archon/workflows",
            "commands_dir": ".archon/commands",
            "artifacts_dir": ".archon/artifacts",
            "ralph_dir": ".archon/ralph",
            "worktrees_dir": ".archon/worktrees",
            "default_workflow": "archon-assist",
        }
    },
    "worktree": {
        "baseBranch": "master",
        "base_path": ".archon/worktrees",
    },
}


# Shape allowlist — every path must be present for ``_validate_config_shape``
# to return True. R3 NB2 fix (R4): ``default_workflow`` added (was missing
# in earlier R3 spec).
_REQUIRED_CONFIG_FIELDS: tuple[tuple[str, ...], ...] = (
    ("capabilities", "archon", "enabled"),
    ("capabilities", "archon", "binary"),
    ("capabilities", "archon", "archon_version"),
    ("capabilities", "archon", "root"),
    ("capabilities", "archon", "workflows_dir"),
    ("capabilities", "archon", "commands_dir"),
    ("capabilities", "archon", "artifacts_dir"),
    ("capabilities", "archon", "ralph_dir"),
    ("capabilities", "archon", "worktrees_dir"),
    ("capabilities", "archon", "default_workflow"),
    ("worktree", "base_path"),
)


# Canonical derived layout values. ``_validate_config_shape`` asserts these
# EXACT strings (NOT just key presence). A config with a stale R2-era
# ``root: archon`` (no leading dot) FAILS shape and triggers migration.
# ``default_workflow`` is presence-only (any non-empty string passes —
# operator can customize).
_CANONICAL_DERIVED_VALUES: dict[tuple[str, ...], str] = {
    ("capabilities", "archon", "root"): ".archon",
    ("capabilities", "archon", "workflows_dir"): ".archon/workflows",
    ("capabilities", "archon", "commands_dir"): ".archon/commands",
    ("capabilities", "archon", "artifacts_dir"): ".archon/artifacts",
    ("capabilities", "archon", "ralph_dir"): ".archon/ralph",
    ("capabilities", "archon", "worktrees_dir"): ".archon/worktrees",
    ("worktree", "base_path"): ".archon/worktrees",
}


# Regex for parsing ``archon version`` stdout. The binary prints lines like
# ``Archon CLI v0.3.10`` after any ``[archon] loaded ...`` preamble.
_VERSION_LINE_RE = re.compile(
    r"^Archon CLI v(?P<version>\d+\.\d+\.\d+)", re.MULTILINE
)


# Smoke workflow asset path. Resolved at module load time —
# ``Path(__file__).resolve().parent`` is ``.claude/scripts/personas/``.
# Up three levels gets to the repo root, then under ``.claude/templates/``.
# WS2a (cli-asset-and-sanitizer) creates the asset on disk.
_SMOKE_TEMPLATE: Path = (
    Path(__file__).resolve().parent.parent.parent
    / "templates"
    / "profile-isolation-smoke.yaml"
)


# =============================================================================
# PATH RESOLVERS
# =============================================================================


def _archon_root_for(name: str) -> Path:
    """Return ``<profile>/.archon`` (named) or ``<install>/.archon`` (default).

    Resolves via the public ``get_persona_paths``/``get_default_paths``
    helpers so the dotted-path convention (PRP-7e R3 cascade) lives in
    one place. NEVER does a literal ``profile_root / "archon"`` join —
    that's exactly the Phase 2 latent bug PRP-7e Task 0 closed.
    """
    if name == "default":
        return get_default_paths()["archon"]
    return get_persona_paths(name)["archon"]


def get_archon_config_path(name: str) -> Path:
    """Return ``<archon_root>/config.yaml`` for *name*."""
    return _archon_root_for(name) / "config.yaml"


def get_archon_worktree_dir(name: str) -> Path:
    """Return ``<archon_root>/worktrees`` for *name*."""
    return _archon_root_for(name) / "worktrees"


def get_archon_artifacts_dir(name: str) -> Path:
    """Return ``<archon_root>/artifacts`` for *name*."""
    return _archon_root_for(name) / "artifacts"


def get_archon_workflows_dir(name: str) -> Path:
    """Return ``<archon_root>/workflows`` for *name*."""
    return _archon_root_for(name) / "workflows"


# =============================================================================
# CONFIG SHAPE VALIDATION & MIGRATION
# =============================================================================


def _validate_config_shape(config: dict) -> bool:
    """True iff *config* has every required field AND every canonical derived
    layout value matches the locked string.

    Rule 2 fix (R1 B4): rejects Phase 2 stub
    (``archon: enabled: true, version: "stub"``) and rejects the install-
    default 4-line shape. Both lack ``capabilities.archon.archon_version``.

    R3 NB2 fix (R4): also rejects configs with stale R2 derived values
    like ``root: archon`` (no leading dot). Triggers migration to the
    canonical dotted layout.

    Phase 5 post-build F1 fix: also rejects configs whose
    ``capabilities.archon.archon_version`` is null / blank / non-string /
    empty. Without this gate a hand-edited / partially migrated config
    could carry a ``archon_version: null`` or ``archon_version: ""`` and
    still be treated as initialized — which would silently bypass the
    strict-version drift check.
    """
    if not isinstance(config, dict):
        return False
    # Step 1: presence check.
    for path in _REQUIRED_CONFIG_FIELDS:
        node: object = config
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return False
            node = node[key]
    # Step 2: canonical-value check (R3 NB2).
    for path, expected_value in _CANONICAL_DERIVED_VALUES.items():
        cursor: object = config
        for key in path:
            # Safe: presence already verified in step 1; ``cursor`` is a
            # dict at every level under the verified paths.
            assert isinstance(cursor, dict)
            cursor = cursor[key]
        if cursor != expected_value:
            return False
    # Step 3: ``default_workflow`` is non-empty string (presence-only check
    # already in step 1; this layers a basic sanity gate).
    default_wf = config["capabilities"]["archon"]["default_workflow"]
    if not isinstance(default_wf, str) or not default_wf.strip():
        return False
    # Step 4 (F1 post-build fix): ``archon_version`` must be a non-empty
    # string. ``None`` / ``""`` / ``"   "`` / non-string all fail shape so
    # ``is_archon_initialized`` triggers MERGE-not-overwrite on next init.
    archon_version = config["capabilities"]["archon"]["archon_version"]
    if not isinstance(archon_version, str) or not archon_version.strip():
        return False
    return True


def _merge_config_shape(existing: dict, template: dict) -> dict:
    """Merge *template* into *existing* with R4-layered semantics:

    - missing required keys -> ADDED from template
    - operator's custom non-derived keys -> PRESERVED
    - canonical derived layout values (entries in
      ``_CANONICAL_DERIVED_VALUES``) -> OVERWRITTEN with template values
      regardless of existing value (R3 NB2 migration path for stale
      ``root: archon`` -> ``root: .archon``)
    - existing ``archon_version`` -> PRESERVED if non-empty string
      (operator pin survives), otherwise REPLACED with template value
      (post-build iter-2 F1 fix: prevents ``archon_version: null`` from
      surviving the merge and making the post-write config still fail
      shape — which would leave ``init_archon`` in a broken loop where
      ``is_archon_initialized`` returns False on the just-written file)
    - existing ``default_workflow`` -> PRESERVED if non-empty string,
      otherwise template wins
    """
    merged = copy.deepcopy(existing)

    def _merge(into: dict, frm: dict, path_prefix: tuple[str, ...] = ()) -> None:
        for key, value in frm.items():
            current_path = path_prefix + (key,)
            if key not in into:
                into[key] = copy.deepcopy(value)
            elif current_path in _CANONICAL_DERIVED_VALUES:
                # R3 NB2: overwrite stale derived layout values verbatim.
                into[key] = copy.deepcopy(value)
            elif isinstance(into[key], dict) and isinstance(value, dict):
                _merge(into[key], value, current_path)
            # else: existing non-derived scalar/list preserved.

    _merge(merged, template)
    # Special case: scalar repair safety nets. ``archon_version`` and
    # ``default_workflow`` must each be a non-empty string for the post-
    # merge config to pass ``_validate_config_shape``. Without these
    # repairs, ``init_archon`` could write back a config carrying
    # ``archon_version: null`` (because the deep-merge only fills MISSING
    # keys) and the next ``is_archon_initialized`` call would return
    # False on the just-written file. Iter-2 F1 fix.
    existing_av = (
        existing.get("capabilities", {}).get("archon", {}).get("archon_version")
    )
    if not isinstance(existing_av, str) or not existing_av.strip():
        merged["capabilities"]["archon"]["archon_version"] = template[
            "capabilities"
        ]["archon"]["archon_version"]
    existing_dw = (
        existing.get("capabilities", {}).get("archon", {}).get("default_workflow")
    )
    if not isinstance(existing_dw, str) or not existing_dw.strip():
        merged["capabilities"]["archon"]["default_workflow"] = template[
            "capabilities"
        ]["archon"]["default_workflow"]
    return merged


def get_actual_config_shape(name: str) -> Optional[dict]:
    """Return the parsed ``config.yaml`` dict for *name*, or None if absent
    / unreadable.

    Rule 2 helper — used by ``thehomie archon status`` (WS2b) to report
    what's actually on disk rather than what the meta cache claims.
    """
    cfg_path = get_archon_config_path(name)
    if not cfg_path.is_file():
        return None
    try:
        parsed = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def is_archon_initialized(name: str) -> bool:
    """Rule 2 + shape: True iff config.yaml exists AND passes shape check.

    R1 B4 fix: previous (Phase 2-era) implementations checked only
    ``Path.is_file()``, which silently passed Phase 2 stubs and stale
    R2 configs through as "initialized". Phase 5's check parses the file
    and runs ``_validate_config_shape`` so a stale config triggers
    migration on next ``init_archon`` call.
    """
    parsed = get_actual_config_shape(name)
    if parsed is None:
        return False
    return _validate_config_shape(parsed)


# =============================================================================
# ATOMIC YAML WRITE
# =============================================================================


def _atomic_write_yaml(target: Path, payload: dict) -> None:
    """Atomically write *payload* as YAML to *target*.

    Mirrors ``personas/activity.py:set_active_profile`` verbatim:
    ``NamedTemporaryFile(delete=False)`` in the SAME directory as the
    target, closed via the ``with`` block exit BEFORE ``os.replace``.
    Same-directory + close-before-rename is what makes this Windows-safe.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        delete=False,
        prefix=".config.",
        suffix=".tmp",
    ) as tmp:
        tmp.write(body)
        tmp_path = tmp.name
    os.replace(tmp_path, target)


# =============================================================================
# CAPABILITY-CONFIG BUILDER
# =============================================================================


def _build_capability_config(*, archon_version: str) -> dict:
    """Return a deep copy of the locked-order template with ``archon_version``
    populated.

    *archon_version* is REQUIRED — the caller (``init_archon``) resolves it
    via detection or operator override before reaching this builder. Rule 1:
    no default arg here — the placeholder ``None`` in the template is
    overwritten on every call with a concrete string.
    """
    if not isinstance(archon_version, str) or not archon_version.strip():
        raise ArchonConfigShapeError(
            f"_build_capability_config: archon_version must be a non-empty "
            f"string, got {archon_version!r}"
        )
    cfg = copy.deepcopy(_CAPABILITY_CONFIG_TEMPLATE)
    cfg["capabilities"]["archon"]["archon_version"] = archon_version
    return cfg


# =============================================================================
# BINARY DETECTION
# =============================================================================


def _strip_archon_preamble(stdout: str) -> str:
    """Drop ``[archon] loaded ...`` preamble lines from *stdout*.

    The archon binary prefixes its actual output with one or more
    ``[archon] loaded <plugin>...`` lines. Strip them so the version
    regex can match the underlying ``Archon CLI vX.Y.Z`` line cleanly.
    """
    lines = stdout.splitlines()
    cleaned = [line for line in lines if not line.startswith("[archon]")]
    return "\n".join(cleaned)


def detect_archon_binary(
    *, expected_version: Optional[str] = None
) -> tuple[Path, str]:
    """Locate the archon binary on PATH and return ``(path, version)``.

    Documented behavior: ``shutil.which("archon")`` returns the FIRST archon
    on PATH (Windows: ``.cmd`` / ``.exe`` / no-extension all valid). On a
    box with multiple installs, whichever is earlier in PATH wins. That's
    the operator's responsibility to manage.

    Args:
        expected_version: Rule 1 None sentinel. When set, raises
            ``ArchonVersionMismatchError`` if the installed version does
            not match. When None, no version check is performed.

    Raises:
        ArchonNotInstalledError: binary not on PATH, or stdout
            unparseable, or subprocess timed out / OSError.
        ArchonVersionMismatchError: installed version != expected_version.
    """
    binary = shutil.which("archon")
    if binary is None:
        raise ArchonNotInstalledError(
            "archon binary not found on PATH. Install via "
            "'curl -fsSL https://archon.thehomie.ai/install.sh | bash' "
            "or see https://archon.thehomie.ai/install"
        )
    binary_path = Path(binary)
    # Suppress the CLAUDECODE warning Archon prints when invoked from inside
    # a Claude Code session — it pollutes stderr which would otherwise be
    # the caller's signal channel.
    env = os.environ.copy()
    env["ARCHON_SUPPRESS_NESTED_CLAUDE_WARNING"] = "1"
    try:
        result = subprocess.run(
            [str(binary_path), "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArchonNotInstalledError(
            f"archon binary at {binary_path} is unrunnable: {exc}"
        ) from exc
    cleaned = _strip_archon_preamble(
        result.stdout.decode("utf-8", errors="replace")
    )
    match = _VERSION_LINE_RE.search(cleaned)
    if not match:
        raise ArchonNotInstalledError(
            f"archon binary at {binary_path} returned unparseable version "
            f"output. Stdout (first 200 chars): {cleaned[:200]!r}"
        )
    installed_version = match.group("version")
    # Rule 1 None sentinel: optional version-lock check.
    if expected_version is not None and installed_version != expected_version:
        raise ArchonVersionMismatchError(
            f"archon binary at {binary_path} reports version "
            f"{installed_version!r}, expected {expected_version!r}. "
            f"Either upgrade/downgrade the binary OR re-init the profile "
            f"with --archon-version {installed_version}."
        )
    return binary_path, installed_version


# =============================================================================
# SMOKE WORKFLOW SEEDING
# =============================================================================


def _seed_smoke_workflow(workflows_dir: Path) -> bool:
    """Copy the smoke template into *workflows_dir* if absent.

    Returns True if the file was written this call, False if it already
    existed (idempotent) or if the asset hasn't shipped yet (WS2a).

    The asset itself ships at ``.claude/templates/profile-isolation-smoke.yaml``
    via WS2a (cli-asset-and-sanitizer). When WS2a hasn't landed yet, this
    helper still no-ops so WS1's tests can run on their own — the asset
    presence is enforced by WS3's isolation acceptance test, not WS1's
    init test.

    Phase 5 post-build R2: surface a stderr warning when the asset is
    missing. WS2a has shipped, so a missing asset post-Phase 5 indicates
    a sanitizer regression or a corrupted clone. Silent no-op was
    misleading — the operator deserves a hint.
    """
    target = workflows_dir / "profile-isolation-smoke.yaml"
    if target.is_file():
        return False
    if not _SMOKE_TEMPLATE.is_file():
        print(
            f"WARNING: smoke workflow template missing at {_SMOKE_TEMPLATE} — "
            f"profile isolation smoke check will be unavailable. This usually "
            f"means WS2a's asset was deleted by sanitization or a clone is "
            f"incomplete. Re-run sanitize.py or refetch the repo.",
            file=sys.stderr,
        )
        return False
    workflows_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_SMOKE_TEMPLATE.read_bytes())
    return True


# =============================================================================
# INIT_ARCHON — the real Phase 5 implementation
# =============================================================================


def init_archon(
    name: str,
    *,
    install_smoke: bool = True,
    archon_version: Optional[str] = None,  # Rule 1 None sentinel
    force: bool = False,
    strict_version: bool = False,
) -> Path:
    """Initialize the Archon spine for a profile (Phase 5 real implementation).

    Replaces the Phase 2 stub. Steps:

      1. Resolve the archon root via ``get_persona_paths(name)["archon"]``
         (named) or ``get_default_paths()["archon"]`` (default). NEVER does
         a literal ``profile_root / "archon"`` join — that was the Phase 2
         latent bug Task 0 closed.
      2. PRE-FLIGHT binary detection (Rule 2 — physical, not meta). If
         ``archon_version`` pinned, version-lock check happens here. If
         strict-version drift, raises BEFORE any disk write.
      3. Idempotent dir create (subdirs: workflows / commands / artifacts /
         ralph / worktrees).
      4. Shape-aware config write:
           - missing OR ``--force`` -> fresh write
           - present + valid shape -> no-op
           - present + invalid shape -> migrate-merge (preserves operator's
             non-derived custom keys)
      5. Seed smoke workflow (idempotent; ``--force`` re-seeds).

    Args:
        name: Profile name. ``"default"`` routes to the install-dir; any
            other name routes to ``~/.homie/profiles/<name>/``.
        install_smoke: Seed the smoke workflow asset when True. Fixed boolean
            default — NOT a tunable config (R1 minor caveat documented).
        archon_version: Pin a specific version. None means detect from binary
            and use that as the lock (Rule 1 — sentinel resolved in body).
        force: Overwrite existing config + re-seed smoke workflow.
        strict_version: Fail on version drift instead of warning.

    Returns:
        The ``<archon_root>`` Path that was written (the caller — typically
        the Click handler — echoes this to the operator).

    Raises:
        FileNotFoundError: profile dir does not exist on disk.
        ArchonNotInstalledError: archon binary not detectable (see
            ``detect_archon_binary``).
        ArchonVersionMismatchError: under ``strict_version=True`` and
            ``archon_version`` pinned to something other than the installed
            binary's version.
        ValueError: profile name fails ``validate_persona_name``.
    """
    # ---- Step 1: resolve archon root (R3 cascade — uses the resolver) ----
    if name == "default":
        archon_root = get_default_paths()["archon"]
    else:
        validate_persona_name(name)
        profile_dir = _profile_root(name)
        if not profile_dir.is_dir():
            raise FileNotFoundError(
                f"Profile '{name}' does not exist at {profile_dir}"
            )
        archon_root = get_persona_paths(name)["archon"]

    # ---- Step 2: PRE-FLIGHT binary detection (Rule 2 — physical, fail fast) ----
    if archon_version is not None:
        # Operator pinned a version — validate against installed binary
        # BEFORE any disk write.
        lock_version = archon_version
        if strict_version:
            # Raises ArchonVersionMismatchError if drift; no disk written yet.
            detect_archon_binary(expected_version=lock_version)
        else:
            try:
                _, installed = detect_archon_binary()
                if installed != lock_version:
                    print(
                        f"WARNING: installed archon version {installed!r} "
                        f"differs from pinned {lock_version!r}; pass "
                        f"--strict-version to fail",
                        file=sys.stderr,
                    )
            except ArchonNotInstalledError:
                # Re-raise so nothing is written — same fail-fast contract.
                raise
    else:
        # No pin — detect and use installed version as the lock.
        _, installed = detect_archon_binary()
        lock_version = installed

    # ---- Step 3: idempotent dir create ----
    archon_root.mkdir(parents=True, exist_ok=True)
    for sub in _ARCHON_SUBDIRS:
        (archon_root / sub).mkdir(parents=True, exist_ok=True)

    # ---- Step 4: shape-aware config write ----
    config_path = archon_root / "config.yaml"
    if force or not config_path.is_file():
        # Fresh write — discards any operator customizations under --force
        # (that's the documented contract).
        config = _build_capability_config(archon_version=lock_version)
        _atomic_write_yaml(config_path, config)
    else:
        # Existing file — parse and check shape (presence + canonical values).
        try:
            existing = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            existing = None
        if not isinstance(existing, dict):
            existing = {}
        if _validate_config_shape(existing):
            # F1 post-build fix: BEFORE the no-op short-circuit, compare the
            # existing config's locked archon_version to the installed
            # binary's version. The earlier no-op path skipped this check,
            # which made exit code 7 unreachable for the common "binary
            # upgraded after init" case (Rule 2 violation: trusted the file
            # cache instead of comparing physical state). Shape validation
            # already proved ``archon_version`` is a non-empty string at
            # this point.
            existing_locked = existing["capabilities"]["archon"]["archon_version"]
            # ``lock_version`` was resolved in Step 2 from either the operator
            # pin (--archon-version) or the binary's detected version. The
            # comparison surfaces drift between an existing config and the
            # binary currently on PATH, regardless of which source set it.
            if existing_locked != lock_version:
                if strict_version:
                    raise ArchonVersionMismatchError(
                        f"existing config locked to archon_version "
                        f"{existing_locked!r}, but installed binary / "
                        f"requested lock is {lock_version!r}. "
                        f"Either upgrade/downgrade the binary OR re-init "
                        f"with --force to overwrite the lock."
                    )
                # Non-strict: warn but PRESERVE the config (no rewrite).
                # The operator can re-init with --force or pin the desired
                # version explicitly.
                print(
                    f"WARNING: existing config locks archon_version "
                    f"{existing_locked!r} but installed binary / requested "
                    f"lock is {lock_version!r}. Pass --strict-version to "
                    f"fail, or --force to overwrite.",
                    file=sys.stderr,
                )
            # Already PRD-compliant + canonical — preserve verbatim.
        else:
            # MIGRATE: required fields added, canonical derived layout values
            # OVERWRITTEN, operator's custom non-derived keys preserved,
            # operator's archon_version pin preserved if present.
            template = _build_capability_config(archon_version=lock_version)
            merged = _merge_config_shape(existing, template)
            _atomic_write_yaml(config_path, merged)

    # ---- Step 5: idempotent smoke workflow seed ----
    if install_smoke:
        if force:
            target = archon_root / "workflows" / "profile-isolation-smoke.yaml"
            if target.is_file():
                target.unlink()
        _seed_smoke_workflow(archon_root / "workflows")

    return archon_root
