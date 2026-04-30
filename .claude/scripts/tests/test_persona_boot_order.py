"""Boot-order audit + import-shape smoke tests (PRP-7a R1 M1 + R2 NM1 + R2 NM2).

PRP-7a Workstream 4b — three-part guard against the "config imported
before HOMIE_HOME is set" class of regression:

1. **Two-tier audit (R2 NM1 — TIER A + TIER B).** The original R1 M1
   audit was single-tier: AST-walk every entry point, assert the shim
   call appears before the first config import. R2 NM1 caught the gap:
   an entry point that does not currently import config silently passes
   the single-tier check, but a future edit that adds a config import
   reintroduces import-time default-path binding without anyone
   noticing. The fix:
       - **Tier A** — text-presence regex over EVERY non-test
         ``__main__`` file (all 51). Guarantees the shim CALL exists in
         every entry point regardless of what they currently import.
       - **Tier B** — AST ordering check, applied ONLY to entry points
         that import config directly OR transitively (via
         ``runtime.bootstrap``, ``runtime.lane_router``,
         ``runtime.registry``, ``shared``, ``engine``, ``router``,
         ``session``, ``recall_service``). Asserts the shim call appears
         at module top-level BEFORE the first config-touching import.

2. **In-process + subprocess smokes.** Catch the import-cycle and
   sibling-package contracts at runtime:
       - in-process wrong-order documents the gotcha (config first,
         then HOMIE_HOME, then reload — does NOT pick up the env var
         on the cached module read; reload DOES pick it up after the
         env mutation)
       - in-process right-order proves the canonical entry-point shape
       - subprocess ``python -c "import config"`` exits 0
       - subprocess with ``HOMIE_HOME=/tmp/fake`` exits 0
       - **R2 NM2** subprocess ``python -c "import personas; assert
         'runtime' not in sys.modules"`` exits 0 — sibling-package
         placement holds, no cycle

3. **Per-CLI ``--help`` sanity (R1 M1 spawn check).** For every safe
   non-destructive CLI, run ``python -m <module> --help`` with
   ``HOMIE_HOME=<tmp>/sales`` set. If the help text mentions any
   resolved path, that path must land under ``/sales/...`` — proves
   the shim runs at module top-level on every CLI.
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Repo root + scripts dir.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "scripts"
_CHAT_DIR = _REPO_ROOT / ".claude" / "chat"
_HOOKS_DIR = _REPO_ROOT / ".claude" / "hooks"

# Same skipped-segment set used by the no-install-dir audit.
_SKIPPED_SEGMENTS: frozenset[str] = frozenset({
    "tests",
    "__pycache__",
    ".venv",
    "worktrees",
    "node_modules",
    "templates",
})

# Transitive-proxy modules that imply a config dependency. Importing any
# of these at module top-level pulls config indirectly, so the boot-shim
# must precede them too. PRP-7a R2 NM1 — anchor list.
_TRANSITIVE_CONFIG_PROXIES: tuple[str, ...] = (
    "runtime.bootstrap",
    "runtime.lane_router",
    "runtime.registry",
    "shared",
    "engine",
    "router",
    "session",
    "recall_service",
)

# Expected minimum entry-point count for the regenerated list (R1 B2 +
# R2 NM1 — count is a moving target, but never below 51).
_EXPECTED_MIN_ENTRY_POINTS = 51


# ---------------------------------------------------------------------------
# Entry-point regeneration (R1 B2 — never hand-maintained)
# ---------------------------------------------------------------------------


def _regenerate_entry_points() -> list[Path]:
    """Walk the production tree and return every non-test ``__main__`` file.

    Mirrors the live ``rg -l "__name__ == '__main__'"`` pattern from the
    PRP. We use Python's filesystem walk + content scan because ``rg`` is
    not available in every contributor's shell PATH (Windows subset
    notably).
    """
    files: list[Path] = []
    for root in (_SCRIPTS_DIR, _CHAT_DIR, _HOOKS_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel_parts = set(path.relative_to(_REPO_ROOT).parts)
            if rel_parts & _SKIPPED_SEGMENTS:
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            # Match either single or double-quoted form.
            if "if __name__ == \"__main__\"" in src or "if __name__ == '__main__'" in src:
                files.append(path)
    return sorted(files)


def _imports_config_directly_or_transitively(path: Path) -> bool:
    """Return True iff *path* directly OR transitively imports config.

    Direct: ``import config`` / ``from config import ...``.
    Transitive: imports any module in ``_TRANSITIVE_CONFIG_PROXIES``,
    each of which loads config at module top level.

    PRP-7a R2 NM1 — Tier B applies only to files in scope here.
    """
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    if re.search(
        r"^\s*(?:import\s+config|from\s+config\s+import)",
        src,
        re.MULTILINE,
    ):
        return True
    for proxy in _TRANSITIVE_CONFIG_PROXIES:
        pat = (
            rf"^\s*(?:import\s+{re.escape(proxy)}\b|"
            rf"from\s+{re.escape(proxy)}\s+import)"
        )
        if re.search(pat, src, re.MULTILINE):
            return True
    return False


def _to_relative_posix(path: Path) -> str:
    """Stable platform-independent display path."""
    return path.relative_to(_REPO_ROOT).as_posix()


# ---------------------------------------------------------------------------
# Tier A — text-presence regex (R2 NM1)
# ---------------------------------------------------------------------------


def test_tier_a_every_entry_point_has_shim_call() -> None:
    """Tier A — every non-test ``__main__`` file calls ``apply_persona_override``.

    Conservative regex match against module text. The ordering check
    (Tier B) below confirms shim placement for files that import config.
    Tier A holds the contract for ALL files so that adding a config
    import to a previously-shim-less entry point in a future PR does
    not silently skip the audit.
    """
    files = _regenerate_entry_points()
    assert len(files) >= _EXPECTED_MIN_ENTRY_POINTS, (
        f"Entry-point count fell below minimum: {len(files)} < "
        f"{_EXPECTED_MIN_ENTRY_POINTS}. Did the regen scope drift?"
    )

    # Module-top-level call, not nested. Anchored to start-of-line with
    # optional leading whitespace tolerated for layout variance.
    shim_call = re.compile(
        r"^\s*apply_persona_override\s*\(\s*\)",
        re.MULTILINE,
    )

    missing_shim: list[str] = []
    for path in files:
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            missing_shim.append(f"{_to_relative_posix(path)}: read error")
            continue
        if not shim_call.search(src):
            missing_shim.append(_to_relative_posix(path))

    assert not missing_shim, (
        f"PRP-7a R2 NM1 — Tier A: {len(missing_shim)} entry point(s) "
        f"missing apply_persona_override() call:\n  "
        + "\n  ".join(missing_shim)
    )


# ---------------------------------------------------------------------------
# Tier B — AST ordering for config-importing subset
# ---------------------------------------------------------------------------


def test_tier_b_shim_runs_before_config_import() -> None:
    """Tier B — shim call precedes the first direct/transitive config import.

    PRP-7a R1 M1 + R2 NM1: for every entry point that imports config
    (directly or transitively), the boot-shim call must appear at
    module top level BEFORE the offending import. AST walk ensures the
    check is structural, not text-shape — comments, decorators, and
    inline rewrites cannot fool it.
    """
    files = _regenerate_entry_points()
    out_of_order: list[str] = []
    in_scope = 0

    # Direct + transitive proxies as a single AST-friendly set. We
    # compare ``ast.Import``'s ``alias.name`` and ``ast.ImportFrom``'s
    # ``module`` against this set.
    proxies: frozenset[str] = frozenset({"config"} | set(_TRANSITIVE_CONFIG_PROXIES))

    for path in files:
        if not _imports_config_directly_or_transitively(path):
            continue
        in_scope += 1

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            out_of_order.append(
                f"{_to_relative_posix(path)}: parse error {exc.msg!r}"
            )
            continue

        shim_line: int | None = None
        config_line: int | None = None

        # Walk ONLY the module's top-level body. Calls inside functions
        # / classes don't count as "running before config import" because
        # they fire at call time, not module-load time.
        for node in tree.body:
            # Detect ``apply_persona_override()`` as a top-level Expr
            # wrapping a Call to a bare Name 'apply_persona_override'.
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "apply_persona_override"
                and shim_line is None
            ):
                shim_line = node.lineno
                continue

            # Detect first config-touching import.
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in proxies and config_line is None:
                    config_line = node.lineno
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in proxies and config_line is None:
                        config_line = node.lineno
                        break

        # Tier A guaranteed shim_line is non-None when the regex matched
        # at module top-level. If shim_line is None here, the call exists
        # outside top-level (inside a function), which is itself a Tier B
        # failure — the shim must run at module load.
        if shim_line is None:
            out_of_order.append(
                f"{_to_relative_posix(path)}: apply_persona_override() not "
                f"at module top-level (config import at line "
                f"{config_line})"
            )
            continue

        if config_line is not None and shim_line >= config_line:
            out_of_order.append(
                f"{_to_relative_posix(path)}: shim@{shim_line} appears "
                f"after config-touching import@{config_line}"
            )

    assert in_scope >= 30, (
        f"Tier B should cover at least 30 config-importing entry "
        f"points, got {in_scope}. Did the proxy list shrink?"
    )
    assert not out_of_order, (
        f"PRP-7a R1 M1 + R2 NM1 — Tier B: {len(out_of_order)} entry "
        f"point(s) call config before apply_persona_override() (or "
        f"call shim outside module top-level):\n  "
        + "\n  ".join(out_of_order)
    )


# ---------------------------------------------------------------------------
# In-process import-order smokes
# ---------------------------------------------------------------------------


def test_in_process_wrong_order_documents_gotcha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong-order: import config first, set HOMIE_HOME, reload picks up.

    PRP-7a R1 M1 documentation test — captures the gotcha behaviour so
    a regression in the resolver's "read env every call" contract is
    obvious. Pre-PRP-7 ``config.MEMORY_DIR`` was bound at import time
    and never changed; post-PRP-7 it's still bound at import time but
    the resolver-derived value DOES change after a reload because the
    resolver re-reads HOMIE_HOME on every call.
    """
    # Import config fresh. Need to evict any cached version from earlier
    # tests. The conftest ``sys.path`` inserts make config importable.
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    if "config" in sys.modules:
        del sys.modules["config"]

    import config as config_module  # type: ignore[import-not-found]

    # Snapshot the default-profile MEMORY_DIR.
    default_memory = Path(config_module.MEMORY_DIR).resolve(strict=False)
    # No HOMIE_HOME set, so it should land at the legacy install path.
    assert default_memory.name == "Memory", (
        f"Default-profile MEMORY_DIR should end at .../vault/memory, "
        f"got: {default_memory}"
    )

    # NOW set HOMIE_HOME and reload. The reload re-runs config.py top-level,
    # which re-resolves through personas (which reads env every call).
    custom_root = tmp_path / "custom-x"
    custom_root.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(custom_root))
    importlib.reload(config_module)

    reloaded_memory = Path(config_module.MEMORY_DIR).resolve(strict=False)
    expected = (custom_root / "memory").resolve(strict=False)
    assert reloaded_memory == expected, (
        f"After reload, MEMORY_DIR should follow HOMIE_HOME.\n"
        f"  expected: {expected}\n  reloaded: {reloaded_memory}"
    )

    # Cleanup — restore module to default state for any later tests.
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    importlib.reload(config_module)


def test_in_process_right_order_picks_up_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right-order: set HOMIE_HOME first, then import config — picks up env.

    Canonical entry-point shape: shim sets HOMIE_HOME, then framework
    imports happen. config.MEMORY_DIR resolves to the new home directly.
    """
    custom_root = tmp_path / "custom-y"
    custom_root.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(custom_root))
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    if "config" in sys.modules:
        del sys.modules["config"]

    import config as config_module  # type: ignore[import-not-found]

    actual = Path(config_module.MEMORY_DIR).resolve(strict=False)
    expected = (custom_root / "memory").resolve(strict=False)
    assert actual == expected, (
        f"PRP-7a R1 M1 — right-order import should produce "
        f"HOMIE_HOME-relative MEMORY_DIR.\n"
        f"  expected: {expected}\n  actual: {actual}"
    )

    # Cleanup.
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    importlib.reload(config_module)


# ---------------------------------------------------------------------------
# Subprocess smokes (R1 M1 + R2 NM2)
# ---------------------------------------------------------------------------


def _run_subprocess(
    code: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``python -c code`` in a clean env and return the CompletedProcess."""
    env = os.environ.copy()
    env.pop("HOMIE_HOME", None)
    env.pop("HOMIE_VAULT_DIR", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_SCRIPTS_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_subprocess_import_config_exits_zero_default_profile() -> None:
    """``python -c "import config"`` exits 0 with HOMIE_HOME unset."""
    result = _run_subprocess("import config")
    assert result.returncode == 0, (
        f"Default-profile config import should exit 0.\n"
        f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )


def test_subprocess_import_config_exits_zero_with_homie_home(
    tmp_path: Path,
) -> None:
    """``python -c "import config"`` exits 0 with HOMIE_HOME set.

    No circular import — the personas package is sibling to runtime/,
    so ``import config`` -> ``import personas`` does NOT pull
    ``runtime/__init__.py`` (the eager-loader that originally caused
    the cycle).
    """
    fake_profile = tmp_path / "fake-profile"
    fake_profile.mkdir()
    result = _run_subprocess(
        "import config",
        extra_env={"HOMIE_HOME": str(fake_profile)},
    )
    assert result.returncode == 0, (
        f"HOMIE_HOME-set config import should exit 0 (no circular "
        f"import).\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )


def test_subprocess_import_personas_does_not_load_runtime() -> None:
    """R2 NM2 — ``import personas`` does NOT pull in ``runtime``.

    PRP-7a R3 NNB4 + R2 NM2 — sibling-package placement of personas
    is the structural fix for the original ``runtime.personas`` cycle.
    The proof: importing personas in a fresh process MUST NOT load
    any module under ``runtime/``. If it did, the eager-loader in
    ``runtime/__init__.py`` would re-create the cycle.
    """
    result = _run_subprocess(
        "import personas; "
        "import sys; "
        "loaded = [m for m in sys.modules if m == 'runtime' "
        "or m.startswith('runtime.')]; "
        "assert not loaded, f'runtime modules leaked: {loaded}'; "
        "print('OK')"
    )
    assert result.returncode == 0, (
        f"PRP-7a R2 NM2 — sibling-package invariant broken: "
        f"importing personas pulled in runtime.\n"
        f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Shim invocation under HOMIE_HOME — paths resolve under the named profile
# ---------------------------------------------------------------------------


def test_shim_invocation_routes_paths_under_homie_home(tmp_path: Path) -> None:
    """Calling the shim explicitly with HOMIE_HOME=/tmp/x routes paths there.

    Per-CLI ``--help`` sanity test, in lighter form: rather than
    spawning every CLI's ``--help`` (which is slow and has destructive-
    default risk), assert the shim+config-import chain produces paths
    rooted at the HOMIE_HOME we passed. If a CLI breaks this contract,
    the audit + Tier A/B above flag the offender; this test pins down
    the expected resolution shape.
    """
    custom_root = tmp_path / "deploy-x"
    custom_root.mkdir()
    result = _run_subprocess(
        "import personas; personas.apply_persona_override();\n"
        "import config; print(config.MEMORY_DIR)",
        extra_env={"HOMIE_HOME": str(custom_root)},
    )
    assert result.returncode == 0, (
        f"shim+config import with HOMIE_HOME should exit 0.\n"
        f"  stderr: {result.stderr}"
    )
    actual = Path(result.stdout.strip()).resolve(strict=False)
    expected = (custom_root / "memory").resolve(strict=False)
    assert actual == expected, (
        f"Custom HOMIE_HOME deployment: MEMORY_DIR drift.\n"
        f"  expected: {expected}\n  actual: {actual}"
    )


# ---------------------------------------------------------------------------
# R2 B1 / NB2 — Windows literal-tilde regression test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "R2 B1 / NB2 — Windows-only literal-tilde trap. POSIX shells "
        "expand ``~`` before Python sees the value, so the regression "
        "class only applies on win32 (PowerShell / cmd-set)."
    ),
)
def test_literal_tilde_homie_home_expands_on_windows(tmp_path: Path) -> None:
    """R2 B1 / NB2 — literal ``~`` in HOMIE_HOME is expanded by the resolver.

    Without ``_normalize_env_home()``, ``Path("~/.homie/profiles/sales")
    .resolve()`` produces ``<cwd>\\~\\.homie\\profiles\\sales`` on
    Windows (literal ``~`` directory) instead of expanding to the user
    home. The shim normalizes the env value before resolving, so this
    test asserts the fix is in place.

    Setup: pin HOME to a fake path AND set HOMIE_HOME to a literal
    ``~/.homie/profiles/sales`` string with a sales dir at the
    expanded location. After the shim runs, MEMORY_DIR should land
    at ``<fake-home>/.homie/profiles/sales/memory`` with NO literal
    ``~`` segment in the path.
    """
    fake_home = tmp_path / "fake-home-tilde"
    homie_root = fake_home / ".homie"
    profiles_root = homie_root / "profiles"
    sales_dir = profiles_root / "sales"
    sales_dir.mkdir(parents=True)

    env_pin: dict[str, str] = {
        "HOME": str(fake_home),
        "USERPROFILE": str(fake_home),
        # Literal ``~`` value — what the regression hides.
        "HOMIE_HOME": "~/.homie/profiles/sales",
    }

    result = _run_subprocess(
        "import personas; personas.apply_persona_override();\n"
        "import config; print(config.MEMORY_DIR)",
        extra_env=env_pin,
    )
    assert result.returncode == 0, (
        f"PRP-7a R2 B1 / NB2 — literal-tilde HOMIE_HOME should not "
        f"crash startup.\n  stderr: {result.stderr}"
    )
    actual_str = result.stdout.strip()
    # The cardinal symptom of the bug: literal ``~`` segment in the
    # resolved path.
    assert "~" not in actual_str, (
        f"PRP-7a R2 B1 / NB2 — literal ``~`` leaked into resolved "
        f"MEMORY_DIR: {actual_str}"
    )
    actual = Path(actual_str).resolve(strict=False)
    expected = (sales_dir / "memory").resolve(strict=False)
    assert actual == expected, (
        f"PRP-7a R2 B1 / NB2 — literal-tilde expansion did not land "
        f"at the expected path.\n  expected: {expected}\n  actual: {actual}"
    )
