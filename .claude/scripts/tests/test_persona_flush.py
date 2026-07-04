"""Tests for US-004: Persona flush — episode persona_id frontmatter,
hook env threading, episodes dir in profile inventory, byte-stability.

Test categories:
  1. Episode writer: persona_id in frontmatter when set, omitted when unset.
  2. Episode reader: read_episode_frontmatter parses persona_id correctly.
  3. Hook env threading: run_hook_script passes env to subprocess.
  4. clear_session_with_lifecycle threads hook_env to _invoke_hook.
  5. Lifecycle inventory: episodes dir in _REQUIRED_MEMORY_DIRS.
  6. Byte-stability: persona flush to persona vault leaves main vault unchanged.
  7. memory_flush persona_id resolution: profile name → episode frontmatter.
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from episodes import (  # noqa: E402
    EpisodeWriteStatus,
    read_episode_frontmatter,
    write_episode_from_flush,
)
from personas.lifecycle import _REQUIRED_MEMORY_DIRS  # noqa: E402

SYNTHETIC_SAFE_ID = "telegram-1111111111-2222222222"
CTX_FILENAME = f"session-flush-{SYNTHETIC_SAFE_ID}-20260703-100000.md"
FIXED_NOW = datetime(2026, 7, 3, 10, 0, 30)
SAMPLE_RESPONSE = """\
## Summary
Sales call with prospect about renewal.

## Key Decisions
Decided to offer 10% discount.
"""


# ── 1. Episode writer: persona_id frontmatter ────────────────────────────


def test_episode_writer_emits_persona_id_when_set(tmp_path: Path):
    """persona_id: appears in frontmatter when the param is set."""
    status, path = write_episode_from_flush(
        tmp_path,
        context_filename=CTX_FILENAME,
        response_text=SAMPLE_RESPONSE,
        now=FIXED_NOW,
        persona_id="sales",
    )
    assert status == EpisodeWriteStatus.WRITTEN
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "persona_id: sales" in content


def test_episode_writer_omits_persona_id_when_none(tmp_path: Path):
    """persona_id: is NOT in frontmatter when the param is None (main Homie)."""
    status, path = write_episode_from_flush(
        tmp_path,
        context_filename=CTX_FILENAME,
        response_text=SAMPLE_RESPONSE,
        now=FIXED_NOW,
        persona_id=None,
    )
    assert status == EpisodeWriteStatus.WRITTEN
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "persona_id" not in content


def test_episode_writer_omits_persona_id_when_default(tmp_path: Path):
    """Calling without persona_id at all (backward compat) omits the field."""
    status, path = write_episode_from_flush(
        tmp_path,
        context_filename=CTX_FILENAME,
        response_text=SAMPLE_RESPONSE,
        now=FIXED_NOW,
    )
    assert status == EpisodeWriteStatus.WRITTEN
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "persona_id" not in content


# ── 2. Episode reader: parses persona_id ─────────────────────────────────


def test_reader_parses_persona_id(tmp_path: Path):
    """read_episode_frontmatter returns persona_id when present."""
    _, path = write_episode_from_flush(
        tmp_path,
        context_filename=CTX_FILENAME,
        response_text=SAMPLE_RESPONSE,
        now=FIXED_NOW,
        persona_id="seo",
    )
    fm = read_episode_frontmatter(path)
    assert fm["persona_id"] == "seo"


def test_reader_tolerates_missing_persona_id(tmp_path: Path):
    """read_episode_frontmatter works fine without persona_id (main episodes)."""
    _, path = write_episode_from_flush(
        tmp_path,
        context_filename=CTX_FILENAME,
        response_text=SAMPLE_RESPONSE,
        now=FIXED_NOW,
    )
    fm = read_episode_frontmatter(path)
    assert "persona_id" not in fm
    assert fm["status"] == "open"


# ── 3. Hook env threading ────────────────────────────────────────────────


def test_run_hook_script_passes_env_to_subprocess(tmp_path: Path):
    """run_hook_script threads env= to subprocess.run."""
    from session_lifecycle_hooks import run_hook_script

    hook_file = tmp_path / "test-hook.py"
    hook_file.write_text("import sys; sys.exit(0)", encoding="utf-8")

    custom_env = {**os.environ, "HOMIE_HOME": str(tmp_path)}

    with patch("session_lifecycle_hooks.HOOKS_DIR", tmp_path):
        with patch("session_lifecycle_hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stderr="", stdout=""
            )
            run_hook_script("test-hook.py", {"key": "val"}, env=custom_env)
            _, kwargs = mock_run.call_args
            assert kwargs["env"] is custom_env


def test_run_hook_script_env_none_by_default(tmp_path: Path):
    """Without env=, subprocess.run gets env=None (inherits parent)."""
    from session_lifecycle_hooks import run_hook_script

    hook_file = tmp_path / "test-hook.py"
    hook_file.write_text("import sys; sys.exit(0)", encoding="utf-8")

    with patch("session_lifecycle_hooks.HOOKS_DIR", tmp_path):
        with patch("session_lifecycle_hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stderr="", stdout=""
            )
            run_hook_script("test-hook.py", {"key": "val"})
            _, kwargs = mock_run.call_args
            assert kwargs["env"] is None


# ── 4. clear_session_with_lifecycle threads hook_env ─────────────────────


def test_clear_lifecycle_threads_hook_env():
    """hook_env is forwarded to _invoke_hook calls."""
    from session_lifecycle_hooks import clear_session_with_lifecycle

    custom_env = {"HOMIE_HOME": "/tmp/persona-sales"}
    mock_store = MagicMock()
    mock_store.delete.return_value = True
    mock_store.list_messages.return_value = []
    mock_session = MagicMock(
        session_id="test:1:1",
        user_id="u",
        message_count=0,
        runtime_lane="",
        runtime_provider="",
        runtime_model="",
        created_at=None,
        updated_at=None,
    )

    with patch("session_lifecycle_hooks.run_hook_script") as mock_hook:
        mock_hook.return_value = MagicMock(
            ok=True, returncode=0, stderr="", stdout="", detail=lambda: "ok"
        )
        clear_session_with_lifecycle(
            store=mock_store,
            session=mock_session,
            platform="test",
            channel_id="1",
            thread_id="1",
            hook_env=custom_env,
        )
        for call in mock_hook.call_args_list:
            _, kwargs = call
            assert kwargs.get("env") is custom_env


# ── 5. Lifecycle inventory ───────────────────────────────────────────────


def test_episodes_in_required_memory_dirs():
    """episodes is in the profile memory directory inventory."""
    assert "episodes" in _REQUIRED_MEMORY_DIRS


# ── 6. Byte-stability: persona flush leaves main vault unchanged ─────────


def _dir_hash(directory: Path) -> str:
    """Hash all files in a directory tree for byte-stability comparison."""
    h = hashlib.sha256()
    for f in sorted(directory.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(directory)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def test_persona_flush_leaves_main_vault_unchanged(tmp_path: Path):
    """Writing a persona episode to persona vault does not touch main vault."""
    main_vault = tmp_path / "main"
    main_vault.mkdir()
    (main_vault / "SOUL.md").write_text("identity", encoding="utf-8")
    (main_vault / "episodes").mkdir()

    persona_vault = tmp_path / "persona-sales"
    persona_vault.mkdir()
    (persona_vault / "episodes").mkdir()

    main_hash_before = _dir_hash(main_vault)

    write_episode_from_flush(
        persona_vault,
        context_filename=CTX_FILENAME,
        response_text=SAMPLE_RESPONSE,
        now=FIXED_NOW,
        persona_id="sales",
    )

    main_hash_after = _dir_hash(main_vault)
    assert main_hash_before == main_hash_after, "Main vault was modified by persona flush"

    persona_episodes = list((persona_vault / "episodes").glob("*.md"))
    assert len(persona_episodes) == 1
    content = persona_episodes[0].read_text(encoding="utf-8")
    assert "persona_id: sales" in content


# ── 7. memory_flush persona_id resolution ────────────────────────────────


def test_flush_resolves_persona_id_for_named_profile():
    """When running under a named profile, flush passes persona_id to episode writer."""
    with patch("personas.activity.get_active_profile_name", return_value="sales"):
        from personas.activity import get_active_profile_name

        profile = get_active_profile_name()
        persona_id = profile if profile not in ("default", "custom") else None
        assert persona_id == "sales"


def test_flush_resolves_none_for_default_profile():
    """Under default profile, persona_id is None (main Homie)."""
    with patch("personas.activity.get_active_profile_name", return_value="default"):
        from personas.activity import get_active_profile_name

        profile = get_active_profile_name()
        persona_id = profile if profile not in ("default", "custom") else None
        assert persona_id is None
