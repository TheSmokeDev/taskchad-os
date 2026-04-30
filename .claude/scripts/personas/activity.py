"""Active-profile metadata helpers.

Reads / writes the ``~/.homie/active_profile`` sticky-meta file and resolves
the currently-active profile name. ``is_default_profile()`` is the only
helper that reads filesystem state instead of meta — it answers "is the
canonical install-dir vault present?" by checking
``<install>/vault/memory/SOUL.md`` directly (Rule 2 — physical state,
not meta).

Hermes anchors:
    - hermes_cli/profiles.py:145-148 -> get_active_profile_path
    - hermes_cli/main.py:117-128     -> read_active_profile (extracted from
                                        the inline shim block for testability;
                                        Hermes inlines this in
                                        ``_apply_profile_override``)
    - hermes_cli/profiles.py: write helper for ``profile use`` -> set_active_profile
                                        (Hermes uses the same atomic
                                        ``tempfile + os.replace`` pattern)

Deliberate deviations (PRP-7a Hermes-Faithful Checklist):
    - ``is_default_profile`` has NO Hermes parallel. Hermes treats
      ``~/.hermes`` as the default home; The Homie has install-dir
      back-compat where ``<install>/vault/memory/`` is the source of
      truth for default users (PRD §7.4 + Rule 2 in MEMORY.md).
    - ``get_active_profile_name`` returns an explicit
      ``"default" | "<name>" | "custom"`` enum so the config refactor
      (PRP-7a Workstream 2) can branch cleanly.

Anti-pattern enforcement:
    - Rule 2 (MEMORY.md): ``is_default_profile()`` reads filesystem state,
      NOT ``~/.homie/active_profile``. Meta lies under partial rebuild,
      backup restore, or manual tampering — physical state is the only
      trustable source.
    - Rule 1 (MEMORY.md): no ``config.X`` constants bound to default args.
      Every helper computes paths from the live env on every call.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .core import (
    _normalize_env_home,
    get_default_homie_root,
    get_default_paths,
)


def get_active_profile_path() -> Path:
    """Return the path to the sticky ``active_profile`` meta file.

    Hermes anchor: hermes_cli/profiles.py:145-148 (_get_active_profile_path).
    Verbatim shape — env-var name renamed.
    """
    return get_default_homie_root() / "active_profile"


def read_active_profile() -> str | None:
    """Return the sticky active-profile name, or ``None``.

    Hermes anchor: hermes_cli/main.py:117-128 — the inline read inside
    ``_apply_profile_override``. Extracted into its own helper here for
    testability and so the boot shim and other callers share one tolerant
    read path.

    Tolerates corrupt / binary / empty / whitespace / ``"default"`` content
    — none of these raise. PRP-7a R1 B4 + R3 NNB1: passive-corrupt
    cases (empty / whitespace / binary garbage) silently return ``None``;
    invalid-non-empty strings (e.g. ``"Sales"``, ``"a" * 80``) round-trip
    through this helper untouched and the caller (``apply_persona_override``)
    handles the validation + warning flow.
    """
    path = get_active_profile_path()
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (UnicodeDecodeError, OSError):
        return None
    if not text or text == "default":
        return None
    return text


def set_active_profile(name: str) -> None:
    """Atomically write *name* to the sticky ``active_profile`` meta file.

    Hermes anchor: hermes_cli/profiles.py write-side of the ``profile use``
    command (uses the same tmp-file + ``os.replace`` pattern).

    Windows-safe: ``NamedTemporaryFile(delete=False)`` is closed via the
    ``with`` block exit BEFORE ``os.replace`` runs. Keeping the tmp file
    in the same directory as the target keeps the rename atomic on every
    filesystem we ship to.
    """
    target = get_active_profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        delete=False,
        prefix=".active_profile.",
        suffix=".tmp",
    ) as tmp:
        tmp.write(name)
        tmp_path = tmp.name
    os.replace(tmp_path, target)


def is_default_profile() -> bool:
    """Return ``True`` iff the canonical install-dir vault is present on disk.

    Deliberate deviation (no Hermes parallel) — see module docstring.

    PHYSICAL-STATE check (Rule 2, MEMORY.md): looks at
    ``<install>/vault/memory/SOUL.md`` existence on disk. Does NOT consult
    ``~/.homie/active_profile`` or any other meta cache. Per PRP-7a R1 B1,
    this helper is consulted only as the rank-4 fallback — explicit
    ``HOMIE_HOME`` selection (rank 1-3) ALWAYS dominates.
    """
    paths = get_default_paths()
    soul = paths["memory"] / "SOUL.md"
    return soul.exists()


def get_active_profile_name() -> str:
    """Return the active profile name as ``"default" | "<name>" | "custom"``.

    Hermes anchor: inferred — Hermes resolves the same logic but inline
    in ``_apply_profile_override`` (hermes_cli/main.py:117-128) plus
    ``hermes_constants.get_default_hermes_root`` (:21-58). PRP-7a §7.4
    extracts the result into an explicit string enum so ``config.py`` can
    branch cleanly between default install paths and per-profile paths.

    Precedence:
        1. ``HOMIE_HOME`` unset                -> ``"default"``
        2. ``HOMIE_HOME == ~/.homie``          -> ``"default"`` (root, not a
                                                  named profile)
        3. ``HOMIE_HOME == ~/.homie/profiles/<name>[/...]``
                                               -> ``"<name>"``
        4. ``HOMIE_HOME`` outside ``~/.homie/profiles/``
                                               -> ``"custom"``

    PRP-7a R1 B1 — explicit selection ALWAYS dominates physical-state
    detection. On owner's real install (where SOUL.md exists),
    ``HOMIE_HOME=~/.homie/profiles/sales`` MUST resolve to ``"sales"``,
    NOT ``"default"``. Only when ``HOMIE_HOME`` is unset do we fall back
    to the physical-state default (which itself only matters in Phase 1
    for the rank-4 path inside ``apply_persona_override`` — this helper
    just reports the env shape).

    PRP-7a R2 B1 / NB2 — every env-derived path is normalized via
    ``_normalize_env_home()`` so the literal ``~`` Windows trap doesn't
    misclassify a named profile as ``"custom"``.
    """
    env_home = os.environ.get("HOMIE_HOME", "").strip()
    if not env_home:
        # No explicit selection — physical default (SOUL.md present) or
        # fresh-install bootstrap. Either way: rank-4 default.
        return "default"
    env_path = _normalize_env_home(env_home)
    default_root = (Path.home() / ".homie").resolve(strict=False)
    if env_path == default_root:
        # HOMIE_HOME=~/.homie itself -> root, NOT a named profile.
        return "default"
    try:
        rel = env_path.relative_to(default_root / "profiles")
    except ValueError:
        # HOMIE_HOME outside ~/.homie/profiles/ -> custom profile.
        return "custom"
    # HOMIE_HOME=~/.homie/profiles/<name>[/...] -> named profile.
    return rel.parts[0]
