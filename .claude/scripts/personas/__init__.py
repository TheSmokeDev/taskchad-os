"""Persona-profile resolver — public API for The Homie's profile foundation.

This package is the structural fix for PRP-7a R3 NNB4 (the import cycle
caused by the original ``runtime/personas.py`` placement).

Module layout:
    - core.py     -> validation, root resolution, default + per-profile path maps
    - activity.py -> sticky active_profile read / atomic write, name resolver,
                     physical-state default detection
    - boot.py     -> ``apply_persona_override`` pre-import shim,
                     ``resolve_persona_env``, ``get_subprocess_env``
    - services.py -> profile-aware bot lifecycle paths, port allocation,
                     ``load_persona_config`` (PRD-8 Phase 2 / WS1)
    - _audit.py   -> PRIVATE — AST helper used only by the
                     ``test_no_install_dir_paths.py`` acceptance gate.
                     NOT in ``__all__`` (PRP-7a R2 NM3).

Hermes-faithful: the package is the verbatim parallel of Hermes' choice to
keep ``hermes_constants.py`` OUTSIDE the ``hermes_cli/`` package so it
stays "Import-safe with no dependencies" (Hermes module docstring at
hermes_constants.py:3-4). That structural decision is the explicit reason
``config.py`` can import this package without re-creating the eager-load
cycle in ``runtime/__init__.py``.

Public API surface (PRP-7a R1 M6 + R2 M6 frozen 12 + PRD-8 Phase 2 +2 = 14):
    apply_persona_override, resolve_persona_env, get_subprocess_env,
    validate_persona_name, get_homie_home, get_default_paths,
    get_persona_paths, get_active_profile_path, read_active_profile,
    set_active_profile, get_active_profile_name, is_default_profile,
    load_persona_config, ConfigShapeError.

``tests/test_personas_public_api.py`` asserts ``personas.__all__`` matches
the API Surface verbatim and ``len(personas.__all__) == len(EXPECTED_PUBLIC_API)``
(no magic number — PRD-8 Phase 2 / R2 NM1).
"""

from __future__ import annotations

from personas.activity import (
    get_active_profile_name,
    get_active_profile_path,
    is_default_profile,
    read_active_profile,
    set_active_profile,
)
from personas.boot import (
    apply_persona_override,
    get_subprocess_env,
    resolve_persona_env,
)
from personas.core import (
    get_default_paths,
    get_homie_home,
    get_persona_paths,
    validate_persona_name,
)
from personas.services import (
    ConfigShapeError,
    load_persona_config,
    validate_config_dict,
    validate_config_yaml_text,
)

__all__ = [
    "ConfigShapeError",
    "apply_persona_override",
    "get_active_profile_name",
    "get_active_profile_path",
    "get_default_paths",
    "get_homie_home",
    "get_persona_paths",
    "get_subprocess_env",
    "is_default_profile",
    "load_persona_config",
    "read_active_profile",
    "resolve_persona_env",
    "set_active_profile",
    "validate_config_dict",
    "validate_config_yaml_text",
    "validate_persona_name",
]
# 16 helpers exposed (PRD-8 Phase 3 / WS2 — added validate_config_dict +
# validate_config_yaml_text per R1 B4; was 14 in Phase 2 after load_persona_config
# + ConfigShapeError; was 12 in PRP-7a Phase 1). Sorted alphabetically.
# test_personas_public_api.py asserts
# ``len(personas.__all__) == len(EXPECTED_PUBLIC_API)`` so the list is the
# single source of truth — no magic number.
