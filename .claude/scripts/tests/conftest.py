"""Shared fixtures for The Homie tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Ensure scripts dir is on path for imports
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

# PRP-7b WS4: exclude `_holders/` from pytest collection (subprocess helpers,
# NOT test modules). Each helper file declares an ``if __name__ == "__main__":``
# block that pytest would not invoke anyway, but keeping them out of
# collection keeps `pytest tests/` output clean.
collect_ignore_glob = ["_holders/*"]


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    """Provide a temporary state directory for PID files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def tmp_pid_file(tmp_state_dir: Path) -> Path:
    """Provide a temporary PID file path."""
    return tmp_state_dir / "bot.pid"


@pytest.fixture
def tmp_env_file(tmp_path: Path) -> Path:
    """Provide a temporary .env file for config reload tests."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CHAT_MAX_TURNS=25\n"
        "CHAT_MAX_BUDGET_USD=2.0\n"
        "OPENAI_API_KEY=sk-test-key\n"
        "VOICE_TTS_ENGINE=edge\n",
        encoding="utf-8",
    )
    return env_file


# === PRP-7a Phase 1 — Persona-resolver fixtures ===
# Added by Workstream 4a (tests-helpers). Three fixtures support the persona
# helper test suite (`test_persona_helpers.py`) and the back-compat snapshot
# in Workstream 4b (`test_default_persona_backcompat.py`).


@pytest.fixture
def tmp_homie_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a fake ``~/.homie`` root for persona-resolver tests.

    Layout:
        <tmp>/.homie/
            profiles/
                sales/
                    memory/
                    data/
                    state/
                    .env
        (active_profile not seeded by default — tests write it explicitly)

    Sets ``HOMIE_HOME`` via ``monkeypatch`` to point at the ``sales`` profile
    so callers exercising the named-profile resolution path read the right
    value. Tests that need a different ``HOMIE_HOME`` shape (custom, unset,
    root-equal-default) override via their own ``monkeypatch`` calls.
    """
    homie_root = tmp_path / ".homie"
    profile_dir = homie_root / "profiles" / "sales"
    for sub in ("memory", "data", "state"):
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)
    (profile_dir / ".env").write_text("# fake profile env\n", encoding="utf-8")
    monkeypatch.setenv("HOMIE_HOME", str(profile_dir))
    return profile_dir


@pytest.fixture
def default_profile_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Build a fake install-dir layout that triggers ``is_default_profile``.

    PRP-7a R1 B1 + Rule 2 — ``is_default_profile()`` reads physical state
    (``<install>/vault/memory/SOUL.md`` existence). To exercise the
    physical-detection path without touching the real repo, point the
    persona resolver at ``tmp_path`` via ``HOMIE_VAULT_DIR`` so
    ``get_default_paths()["memory"]`` resolves to ``<tmp>/vault/memory``,
    then drop a SOUL.md there.

    HOMIE_VAULT_DIR overrides only the ``memory`` key (R1 B5), which is
    where ``is_default_profile()`` looks for ``SOUL.md``. The other keys
    (``data``, ``state``, ...) still point at the real install — that's
    fine for the physical-detection test because those keys are not read
    by ``is_default_profile`` itself.
    """
    install = tmp_path / "install"
    memory = install / "TheHomie" / "Memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "SOUL.md").write_text(
        "# Test SOUL.md\nFixture-installed marker for is_default_profile.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMIE_VAULT_DIR", str(memory))
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    return install


@pytest.fixture
def legacy_install_paths() -> dict[str, str]:
    """Return hard-coded pre-PRP-7 expected paths as strings.

    PRP-7a R1 M5 — these values MUST be hard-coded, NOT recomputed by
    calling ``personas.get_default_paths()``. The whole point of the
    back-compat snapshot is to prove the new resolver matches the contract
    that pre-PRP-7 ``config.py`` produced; if we recomputed via the same
    resolver we'd be asserting the resolver matches itself (path-math
    theater — the bug R1 M5 explicitly calls out).

    The repo root is derived from this conftest.py's location to keep the
    fixture portable across machines / clone paths, but the SHAPE of every
    legacy path (``<repo>/.claude/data/...``, ``<repo>/vault/memory``)
    is hard-coded as a literal string template so a future refactor of
    the resolver cannot silently change the snapshot.

    Workstream 4b consumes this fixture in
    ``test_default_persona_backcompat.py``. Workstream 4a uses it inline
    for the ``get_persona_paths("default")`` -> legacy snapshot assertion
    in ``test_persona_helpers.py``.
    """
    # Repo root — `.claude/scripts/tests/conftest.py` -> tests -> scripts ->
    # .claude -> repo root. Resolve to absolute string so platform path
    # separators match what `Path.resolve()` produces inside config.py.
    repo_root = str(SCRIPTS_DIR.parent.parent)
    # Build the dictionary using literal `str(Path(...).resolve())` shape so
    # platform separator behavior matches both POSIX and Windows. The values
    # are stringified Paths to keep the contract shape obvious — tests
    # convert to Path themselves when comparing.
    return {
        "PROJECT_ROOT": str(Path(repo_root).resolve(strict=False)),
        "MEMORY_DIR": str(
            Path(repo_root, "TheHomie", "Memory").resolve(strict=False)
        ),
        "DATA_DIR": str(
            Path(repo_root, ".claude", "data").resolve(strict=False)
        ),
        "STATE_DIR": str(
            Path(repo_root, ".claude", "data", "state").resolve(strict=False)
        ),
        "ENV_FILE": str(
            Path(repo_root, ".claude", "scripts", ".env").resolve(strict=False)
        ),
        "DATABASE_PATH": str(
            Path(repo_root, ".claude", "data", "memory.db").resolve(strict=False)
        ),
        "CHAT_DB_PATH": str(
            Path(repo_root, ".claude", "data", "chat.db").resolve(strict=False)
        ),
        "ORCHESTRATION_DB_PATH": str(
            Path(
                repo_root, ".claude", "data", "orchestration.db"
            ).resolve(strict=False)
        ),
        "HEARTBEAT_STATE_FILE": str(
            Path(
                repo_root,
                ".claude",
                "data",
                "state",
                "heartbeat-state.json",
            ).resolve(strict=False)
        ),
        "REFLECTION_STATE_FILE": str(
            Path(
                repo_root,
                ".claude",
                "data",
                "state",
                "reflection-state.json",
            ).resolve(strict=False)
        ),
        "WEEKLY_STATE_FILE": str(
            Path(
                repo_root,
                ".claude",
                "data",
                "state",
                "weekly-state.json",
            ).resolve(strict=False)
        ),
        "DREAM_STATE_FILE": str(
            Path(
                repo_root,
                ".claude",
                "data",
                "state",
                "dream-state.json",
            ).resolve(strict=False)
        ),
        # Auxiliary keys consumed by `get_persona_paths("default")` outside
        # the strict back-compat snapshot but useful for default-routing tests.
        "INTEGRATIONS_DIR": str(
            Path(
                repo_root, ".claude", "scripts", "integrations"
            ).resolve(strict=False)
        ),
    }


# === PRP-7b Phase 2 — Lifecycle / clone / live-pid fixtures ===
# Added by Workstream 4 (tests). Three fixtures support Phase 2 lifecycle
# tests. Existing `tmp_homie_home`, `default_profile_install`,
# `legacy_install_paths` STAY UNCHANGED so Phase 1 tests keep passing.
#
# Fixture-split contract (R1 B5 — load-bearing):
#   - `tmp_homie_home` — pre-seeds a `sales` profile. Use ONLY for tests
#     that need the profile to already exist (delete, use, list, show).
#   - `empty_homie_root` — NO profiles seeded. Use for ALL `create_profile`
#     lifecycle tests AND clone tests (via `source_profile_with_secrets`).
#     `create_profile("sales")` on `empty_homie_root` MUST succeed.
#   - `source_profile_with_secrets` — builds on `empty_homie_root` with a
#     `source` profile containing fake `.env` secrets + memory files.
#     Used by clone tests so the source-seed is the only profile in the root.


@pytest.fixture
def empty_homie_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """R1 B5 — truly empty ``<tmp>/.homie/`` root.

    NO profiles seeded, NO ``.env`` file, NO ``active_profile`` written,
    only the bare ``<tmp>/.homie/`` directory exists. Sets ``HOMIE_HOME``
    to the root itself (not to a profile path) so the persona resolver
    treats the root as the homie home.

    Used by ALL tests that exercise ``create_profile`` so the test starts
    from a truly empty root and the create succeeds.
    """
    homie = tmp_path / ".homie"
    homie.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    # Ensure HOMIE_VAULT_DIR doesn't bleed across tests.
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    # R-post-build F4: route wrapper creation into the test tmp tree so
    # CLI clone tests (which now go through create_profile and trigger a
    # real wrapper write) don't pollute ~/.local/bin or the user's
    # AppData\Local\Programs\thehomie\bin\ on Windows. Tests that need
    # to override (test_persona_wrapper_generation.py) still call
    # ``monkeypatch.setenv("HOMIE_BIN_DIR", ...)`` themselves.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("HOMIE_BIN_DIR", str(bin_dir))
    return homie


@pytest.fixture
def source_profile_with_secrets(
    empty_homie_root: Path,
) -> Path:
    """Pre-seed ``<empty_homie_root>/profiles/source/`` with fake secrets.

    Layout:
        <empty_homie_root>/
            profiles/
                source/
                    .env                (fake secrets)
                    memory/
                        SOUL.md
                        MEMORY.md
                        USER.md

    Returns the source profile dir path. Used by clone tests so the source
    profile is the only profile in the root (clone-tests assume create
    will succeed for the destination).
    """
    profiles = empty_homie_root / "profiles"
    profiles.mkdir(exist_ok=True)
    src = profiles / "source"
    src.mkdir()
    (src / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=BOT123\n"
        "OPENAI_API_KEY=sk-test\n"
        "# comment line\n"
        "\n"
        "EMPTY_KEY=\n",
        encoding="utf-8",
    )
    mem = src / "memory"
    mem.mkdir()
    (mem / "SOUL.md").write_text("source soul\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("source memory\n", encoding="utf-8")
    (mem / "USER.md").write_text("source user\n", encoding="utf-8")
    return src


# === PRP-7c Phase 3 — Multi-profile fixture ===
# Used by test_persona_bot_lifecycle, test_persona_bot_lock_isolation,
# test_persona_lock_isolation, test_persona_port_allocation, and
# test_persona_token_collision so each test exercises a real two-profile
# layout under ``<tmp>/.homie/profiles/sales`` + ``.../engineering``.


@pytest.fixture
def multi_profile_fixture(
    empty_homie_root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Build ``<empty_homie_root>/profiles/{sales,engineering}/`` profile pair.

    Each profile gets the directory inventory matching Phase 1's
    ``_REQUIRED_PROFILE_DIRS`` (mirroring what ``personas.lifecycle.create_profile``
    seeds) plus a minimal `.env` file so tests can override token values
    without re-creating the file.

    Yields::

        {"sales": <empty_homie_root>/profiles/sales,
         "engineering": <empty_homie_root>/profiles/engineering}

    HOMIE_HOME is left set to the empty root (from ``empty_homie_root``);
    individual tests flip it to a profile root via ``monkeypatch.setenv``.
    """
    required_dirs = (
        "memory",
        "data",
        "state",
        "credentials",
        "logs",
        "run",
        # PRP-7e R3 cascade fix: dotted ``.archon`` (Archon's discovery
        # convention). ``personas.get_persona_paths(name)["archon"]`` keeps
        # the bare-string KEY but resolves to ``<profile>/.archon``.
        ".archon",
        "home",
        "cron",
        "sessions",
        "skills",
        "workspace",
    )
    profiles_root = empty_homie_root / "profiles"
    profiles_root.mkdir(exist_ok=True)
    out: dict[str, Path] = {}
    for name in ("sales", "engineering"):
        profile_dir = profiles_root / name
        profile_dir.mkdir()
        for sub in required_dirs:
            (profile_dir / sub).mkdir(parents=True, exist_ok=True)
        # Minimal .env — empty TELEGRAM_BOT_TOKEN by default; tests overwrite.
        (profile_dir / ".env").write_text(
            "# fake profile env (multi_profile_fixture)\n"
            "TELEGRAM_BOT_TOKEN=\n",
            encoding="utf-8",
        )
        out[name] = profile_dir
    return out


@pytest.fixture
def live_pid_fixture():
    """Spawn a real ``subprocess.Popen`` running ``time.sleep(60)``.

    Yields ``(pid, popen)``. Tests use the pid to write a live `bot.pid`
    file and exercise the alive-PID path of `quiesce_profile`. Teardown
    terminates the subprocess so the test never leaves a stray child.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc.pid, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass


# === Issue #27 — module-isolation fixture for import-time config binding ===
# db.py and memory_index.py copy config values into their namespaces at
# import time (`from config import ...`), so tests that need a patched
# EMBEDDING_DIMENSIONS / DATABASE_PATH to actually flow into their behavior
# must re-execute the module bodies. The old pattern in
# test_dim_drift_guard.py (importlib.reload + post-reload monkeypatch.setattr)
# leaked patched module state for the rest of the session — monkeypatch
# recorded the post-reload PATCHED values as "originals", so teardown
# "restored" the leak. The fixture below wraps
# tests/module_isolation.isolated_db_modules_ctx, which:
#   stash sys.modules entries -> apply config overrides -> pop + fresh-import
#   db then memory_index -> yield -> restore the pristine originals.
# Scoped FUNCTION (factory style), not session: consumers need per-test
# tmp_path-derived paths and different override sets per test, and
# session-scoped restore-at-exit would preserve the very inter-test leak
# this fixture exists to fix. Full pattern docs: tests/module_isolation.py.


@pytest.fixture
def isolated_db_modules():
    """Factory: ``iso = isolated_db_modules(EMBEDDING_DIMENSIONS=1024, ...)``.

    Each call applies the given config overrides via a private MonkeyPatch,
    fresh-imports ``db`` and ``memory_index`` under them, and returns a
    namespace with ``config`` / ``db`` / ``memory_index`` attributes. All
    contexts unwind LIFO at test teardown (originals restored, patches
    undone) via ExitStack.
    """
    from contextlib import ExitStack

    from tests.module_isolation import isolated_db_modules_ctx

    with ExitStack() as stack:

        def _factory(**config_overrides):
            return stack.enter_context(
                isolated_db_modules_ctx(**config_overrides)
            )

        yield _factory
