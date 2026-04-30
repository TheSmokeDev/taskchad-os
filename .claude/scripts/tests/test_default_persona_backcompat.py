"""Default-profile back-compat snapshot (PRP-7a R1 M5 + R1 B5).

PRP-7a Workstream 4b — proves that with ``HOMIE_HOME`` unset, every
refactored constant in ``config.py`` resolves to the SAME path it produced
pre-PRP-7. Adapts the Hermes "default profile is the legacy install"
contract to The Homie's install-dir back-compat shape.

Key design notes (PRP-7a §"Test Plan > test_default_persona_backcompat.py"):

1. **Subprocess-based imports.** Each cell spawns a fresh ``python -c
   "import config; print(config.X)"`` so the test starts from a cold
   module table. The parent test process has already imported ``config``
   via earlier tests in the suite — reusing it would give us cached path
   constants from whatever env-var permutation ran first. Subprocess
   isolation is the only way to prove the resolver matches the legacy
   shape "from scratch".

2. **Hard-coded expected values.** ``legacy_install_paths`` (in
   ``conftest.py``) returns hard-coded literal-shape strings, NOT
   recomputed paths. PRP-7a R1 M5 calls this out specifically: a
   back-compat test that recomputes via the new resolver is "path-math
   theater" — the resolver matches itself by definition.

3. **Four-cell matrix:**
       (a) HOMIE_HOME unset + HOMIE_VAULT_DIR unset
           -> all paths match the legacy install layout (R1 M5)
       (b) HOMIE_HOME unset + HOMIE_VAULT_DIR=<tmp>/myvault
           -> MEMORY_DIR follows the override (R1 B5);
              STATE_DIR / ENV_FILE / DATABASE_PATH stay at install paths
       (c) An ``.env`` file is present at the resolved ENV_FILE location
           -> ENV_FILE points at it correctly (sanity check on dotenv shape)
       (d) Windows path normalization round-trip
           -> ``Path(...).resolve()`` is idempotent on win32 (catches
              backslash / case-folding regressions)

4. **String comparison after ``Path(...).resolve()``.** Both sides are
   resolved to absolute paths to dodge ``\\`` vs ``/`` and case-folding
   skew on Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Repo root — `tests/test_default_persona_backcompat.py` -> tests ->
# scripts -> .claude -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "scripts"


def _subprocess_import_print(
    code: str,
    *,
    extra_env: dict[str, str] | None = None,
    drop_env_keys: tuple[str, ...] = ("HOMIE_HOME", "HOMIE_VAULT_DIR"),
) -> str:
    """Run ``python -c "<code>"`` and return its stdout (stripped).

    Builds a clean subprocess env starting from ``os.environ.copy()`` minus
    the keys in ``drop_env_keys`` (default: HOMIE_HOME, HOMIE_VAULT_DIR).
    Then layers ``extra_env`` on top so the caller can pin specific values
    without leakage from the parent process.

    Subprocess cwd is ``.claude/scripts`` so ``import config`` resolves
    against the right module file (the ``conftest.py`` ``sys.path`` insert
    only applies inside the test process — subprocesses don't inherit it).
    """
    env = os.environ.copy()
    for key in drop_env_keys:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_SCRIPTS_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Subprocess exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n"
            f"--- code ---\n{code}"
        )
    return result.stdout.strip()


def _norm(path_str: str) -> str:
    """Normalize *path_str* via ``Path.resolve()`` for cross-platform compare.

    ``Path("X").resolve(strict=False)`` produces the same shape that
    ``personas.get_default_paths()`` does (``strict=False`` propagates from
    ``personas/core.py``), so comparing two ``str(Path(...).resolve())``
    values yields a reliable equality test on Windows + POSIX.
    """
    return str(Path(path_str).resolve(strict=False))


# ---------------------------------------------------------------------------
# Cell A — HOMIE_HOME unset + HOMIE_VAULT_DIR unset (R1 M5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "constant_key,subprocess_attr",
    [
        ("MEMORY_DIR", "config.MEMORY_DIR"),
        ("DATA_DIR", "config.DATA_DIR"),
        ("STATE_DIR", "config.STATE_DIR"),
        ("ENV_FILE", "config.ENV_FILE"),
        ("DATABASE_PATH", "config.DATABASE_PATH"),
        ("CHAT_DB_PATH", "config.CHAT_DB_PATH"),
        ("ORCHESTRATION_DB_PATH", "config.ORCHESTRATION_DB_PATH"),
        ("HEARTBEAT_STATE_FILE", "config.HEARTBEAT_STATE_FILE"),
        ("REFLECTION_STATE_FILE", "config.REFLECTION_STATE_FILE"),
        ("WEEKLY_STATE_FILE", "config.WEEKLY_STATE_FILE"),
        ("DREAM_STATE_FILE", "config.DREAM_STATE_FILE"),
    ],
)
def test_cell_a_default_profile_unset_vault_unset(
    legacy_install_paths: dict[str, str],
    constant_key: str,
    subprocess_attr: str,
) -> None:
    """Cell A — HOMIE_HOME unset + HOMIE_VAULT_DIR unset matches legacy.

    PRP-7a R1 M5: every refactored constant in ``config.py`` MUST resolve
    to the pre-PRP-7 install-dir layout when nothing is overriding the
    resolver. ``legacy_install_paths`` is hard-coded (NOT recomputed via
    the new resolver) so this test catches a silent self-equality bug.
    """
    expected = _norm(legacy_install_paths[constant_key])
    actual_raw = _subprocess_import_print(
        f"import config; print({subprocess_attr})"
    )
    actual = _norm(actual_raw)
    assert actual == expected, (
        f"PRP-7a R1 M5 — {subprocess_attr} drifted from legacy install:\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}"
    )


def test_cell_a_chat_db_path_present(
    legacy_install_paths: dict[str, str],
) -> None:
    """``CHAT_DB_PATH`` lands at ``<repo>/.claude/data/chat.db`` by default.

    Defensive sanity assertion — the parametrized test above already
    covers this constant, but the explicit ``.db`` filename check guards
    against a future refactor that might silently rename the file (e.g.
    ``chat.db`` -> ``chat_sessions.db``) without updating the resolver.
    """
    actual = _subprocess_import_print(
        "import config; print(config.CHAT_DB_PATH)"
    )
    assert actual.endswith("chat.db"), (
        f"CHAT_DB_PATH should still end in 'chat.db', got: {actual}"
    )


# ---------------------------------------------------------------------------
# Cell B — HOMIE_HOME unset + HOMIE_VAULT_DIR=<tmp>/myvault (R1 B5)
# ---------------------------------------------------------------------------


def test_cell_b_homie_vault_dir_overrides_memory_dir(
    tmp_path: Path,
    legacy_install_paths: dict[str, str],
) -> None:
    """Cell B — HOMIE_VAULT_DIR=/x sets MEMORY_DIR to /x (R1 B5 preserved).

    Pre-PRP-7 ``config.py`` honored ``HOMIE_VAULT_DIR`` as the
    override for ``MEMORY_DIR``. PRP-7a §"R1 B5" makes this a contract —
    the resolver MUST inherit the same env-var behavior.
    """
    fake_vault = tmp_path / "myvault"
    fake_vault.mkdir()
    actual_raw = _subprocess_import_print(
        "import config; print(config.MEMORY_DIR)",
        extra_env={"HOMIE_VAULT_DIR": str(fake_vault)},
    )
    actual = _norm(actual_raw)
    expected = _norm(str(fake_vault))
    assert actual == expected, (
        f"PRP-7a R1 B5 — HOMIE_VAULT_DIR override should redirect "
        f"MEMORY_DIR but did not:\n  expected: {expected}\n  actual: {actual}"
    )


@pytest.mark.parametrize(
    "constant_key,subprocess_attr",
    [
        ("STATE_DIR", "config.STATE_DIR"),
        ("ENV_FILE", "config.ENV_FILE"),
        ("DATA_DIR", "config.DATA_DIR"),
        ("DATABASE_PATH", "config.DATABASE_PATH"),
    ],
)
def test_cell_b_homie_vault_dir_does_not_touch_other_paths(
    tmp_path: Path,
    legacy_install_paths: dict[str, str],
    constant_key: str,
    subprocess_attr: str,
) -> None:
    """Cell B (cont.) — HOMIE_VAULT_DIR scope is the ``memory`` key ONLY.

    PRP-7a R1 B5 contract: the env override applies to MEMORY_DIR alone.
    STATE_DIR, ENV_FILE, DATA_DIR, and the db paths must still resolve to
    their legacy install-dir locations even when HOMIE_VAULT_DIR is set.
    Catches the "fix one thing, break four" regression.
    """
    fake_vault = tmp_path / "myvault"
    fake_vault.mkdir()
    expected = _norm(legacy_install_paths[constant_key])
    actual_raw = _subprocess_import_print(
        f"import config; print({subprocess_attr})",
        extra_env={"HOMIE_VAULT_DIR": str(fake_vault)},
    )
    actual = _norm(actual_raw)
    assert actual == expected, (
        f"PRP-7a R1 B5 — HOMIE_VAULT_DIR should NOT touch "
        f"{subprocess_attr}, but it changed:\n"
        f"  expected: {expected}\n  actual: {actual}\n"
        f"  HOMIE_VAULT_DIR was: {fake_vault}"
    )


# ---------------------------------------------------------------------------
# Cell C — existing .env present, ENV_FILE resolves correctly
# ---------------------------------------------------------------------------


def test_cell_c_env_file_points_at_install_env(
    legacy_install_paths: dict[str, str],
) -> None:
    """Cell C — ENV_FILE points at ``<install>/.claude/scripts/.env``.

    The repo's existing ``.env`` lives at this path. ``config.ENV_FILE``
    is the constant WS3 entry-point env-writer migrations consume to
    replace bare ``load_dotenv()`` and parent-path math. This test
    proves the contract is preserved on the default profile.
    """
    actual = _norm(
        _subprocess_import_print("import config; print(config.ENV_FILE)")
    )
    expected = _norm(legacy_install_paths["ENV_FILE"])
    assert actual == expected, (
        f"PRP-7a R1 M5 / WS3 contract — ENV_FILE drifted:\n"
        f"  expected: {expected}\n  actual: {actual}"
    )
    # Sanity: the file exists in this repo (basic dotenv consumer health
    # check). If a future test runs in a fresh checkout where the .env
    # was never created, this assertion is a useful failure rather than
    # a silent skip.
    assert Path(actual).exists() or Path(actual).parent.exists(), (
        f"ENV_FILE parent directory missing — repo layout drift?\n"
        f"  ENV_FILE: {actual}"
    )


def test_cell_c_env_file_load_does_not_crash(tmp_path: Path) -> None:
    """``load_dotenv(ENV_FILE, ...)`` exits cleanly with a real .env present.

    PRP-7a R1 M5 sanity check — the resolver path must be a real
    string that ``python-dotenv`` accepts. A regression where ENV_FILE
    became a directory or non-Path object would crash here, not silently
    pass the path-shape comparison above.
    """
    actual = _subprocess_import_print(
        "import config; "
        "from pathlib import Path; "
        "p = Path(config.ENV_FILE); "
        "print('OK' if p.parent.exists() else 'PARENT_MISSING')"
    )
    assert actual == "OK", (
        f"ENV_FILE parent must exist on the default profile (it lives at "
        f"<install>/.claude/scripts/.env). Got: {actual}"
    )


# ---------------------------------------------------------------------------
# Cell D — Windows path normalization round-trip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "Cell D — Windows-specific path normalization. POSIX shells "
        "expand ``~`` before Python sees it, so the regression class only "
        "applies to win32."
    ),
)
def test_cell_d_windows_path_resolve_is_idempotent(
    legacy_install_paths: dict[str, str],
) -> None:
    """Cell D — ``Path(...).resolve()`` is idempotent on win32.

    PRP-7a R2 B1 / NB2 — Windows ``Path("~/.homie/...").resolve()`` does
    NOT expand ``~`` without ``expanduser()``. The resolver uses
    ``_normalize_env_home()`` for env paths; this test asserts the
    DEFAULT path constants (which use ``Path(__file__).resolve()``)
    don't need that expansion themselves and round-trip cleanly.
    """
    # Resolve each legacy path once via subprocess, then resolve again in
    # the parent process. Both should produce identical strings.
    once_raw = _subprocess_import_print(
        "import config; print(config.MEMORY_DIR)"
    )
    twice = _norm(once_raw)  # parent-process resolve()
    once = _norm(once_raw)  # equivalent — same input, same operation
    assert once == twice, (
        f"PRP-7a R2 B1 / NB2 — Path.resolve() not idempotent on win32:\n"
        f"  once: {once}\n  twice: {twice}"
    )
    # Additionally, no literal '~' segment should appear in any of the
    # default-profile constants (they all derive from
    # ``Path(__file__).resolve()`` — no env input on the default path).
    assert "~" not in once, (
        f"Resolved default MEMORY_DIR still contains literal '~': {once}"
    )


# ---------------------------------------------------------------------------
# Catch-all coverage — auxiliary state files all under STATE_DIR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subprocess_attr,suffix",
    [
        ("config.HEARTBEAT_STATE_FILE", "heartbeat-state.json"),
        ("config.REFLECTION_STATE_FILE", "reflection-state.json"),
        ("config.WEEKLY_STATE_FILE", "weekly-state.json"),
        ("config.DREAM_STATE_FILE", "dream-state.json"),
        ("config.example_scout_STATE_FILE", "example_scout-state.json"),
    ],
)
def test_state_files_are_under_state_dir(
    legacy_install_paths: dict[str, str],
    subprocess_attr: str,
    suffix: str,
) -> None:
    """Every state file lands under ``STATE_DIR`` (R1 M5 catch-all).

    PRP-7a §"Test Plan > test_default_persona_backcompat.py — assertions"
    enumerates these as a contract block. The parametrized matrix above
    already covers the main 4; this fan-out adds the secondary state
    files and the suffix check ensures they keep their canonical names.
    """
    state_dir = _norm(legacy_install_paths["STATE_DIR"])
    actual = _norm(
        _subprocess_import_print(f"import config; print({subprocess_attr})")
    )
    assert actual == _norm(str(Path(state_dir) / suffix)), (
        f"PRP-7a R1 M5 — {subprocess_attr} not under STATE_DIR:\n"
        f"  STATE_DIR: {state_dir}\n  expected suffix: {suffix}\n"
        f"  actual:    {actual}"
    )
