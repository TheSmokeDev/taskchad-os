"""Shared fixtures for The Homie tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure scripts dir is on path for imports
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


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
