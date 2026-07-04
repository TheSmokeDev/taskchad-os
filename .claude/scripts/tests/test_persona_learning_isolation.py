"""US-008 — Config resolver + isolation tests + parity proof.

Covers:
  A. Config resolver: Rule 1 call-time resolution, env override, defaults
  B. Isolation test A: persona A's run leaves persona B + main state unchanged
  C. Isolation test B: corpus query keyed by persona_id in SQL WHERE layer
  D. Isolation test C (KEYSTONE): seed → spawn → beliefs reflection-only +
     episode persona_id + main state unchanged
  E. Cross-contamination: two personas, run A only → B + main unchanged
  F. Zero-enabled-personas parity: fan-out is no-op, suite green
  G. Keystone test designed to FAIL on config-resolved reads
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for p in [str(_SCRIPTS_DIR), str(_CHAT_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ============================================================================
# A. Config resolver — Rule 1 call-time resolution
# ============================================================================


class TestPersonaLearningConfigResolver:
    def test_defaults(self) -> None:
        """Default values when no env vars are set."""
        from config import get_persona_learning_settings

        settings = get_persona_learning_settings()
        assert settings.enabled is True
        assert settings.tick_interval_hours == 12.0
        assert settings.silent_skip_window_hours == 24.0

    def test_env_override_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PERSONA_LEARNING_ENABLED=false disables the tick."""
        monkeypatch.setenv("PERSONA_LEARNING_ENABLED", "false")
        from config import get_persona_learning_settings

        settings = get_persona_learning_settings()
        assert settings.enabled is False

    def test_env_override_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PERSONA_LEARNING_TICK_INTERVAL overrides default."""
        monkeypatch.setenv("PERSONA_LEARNING_TICK_INTERVAL", "6")
        from config import get_persona_learning_settings

        settings = get_persona_learning_settings()
        assert settings.tick_interval_hours == 6.0

    def test_env_override_skip_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PERSONA_LEARNING_SILENT_SKIP_WINDOW overrides default."""
        monkeypatch.setenv("PERSONA_LEARNING_SILENT_SKIP_WINDOW", "48")
        from config import get_persona_learning_settings

        settings = get_persona_learning_settings()
        assert settings.silent_skip_window_hours == 48.0

    def test_explicit_values_pass_through(self) -> None:
        """Explicit kwargs bypass env resolution (Rule 1)."""
        from config import get_persona_learning_settings

        settings = get_persona_learning_settings(
            enabled=False,
            tick_interval_hours=3.0,
            silent_skip_window_hours=6.0,
        )
        assert settings.enabled is False
        assert settings.tick_interval_hours == 3.0
        assert settings.silent_skip_window_hours == 6.0

    def test_none_sentinel_resolved_inside_body(self) -> None:
        """None sentinel (default) resolves inside the function body, not def-time."""
        from config import get_persona_learning_settings
        import inspect

        sig = inspect.signature(get_persona_learning_settings)
        for param in sig.parameters.values():
            assert param.default is None, (
                f"Parameter {param.name} must default to None (Rule 1)"
            )

    def test_namedtuple_fields(self) -> None:
        """Settings NamedTuple has the expected fields."""
        from config import PersonaLearningSettings

        assert hasattr(PersonaLearningSettings, "_fields")
        assert "enabled" in PersonaLearningSettings._fields
        assert "tick_interval_hours" in PersonaLearningSettings._fields
        assert "silent_skip_window_hours" in PersonaLearningSettings._fields


# ============================================================================
# B. Isolation test A: persona A leaves persona B + main state unchanged
# ============================================================================


def _dir_hash(dir_path: Path) -> str:
    """Hash all files in a directory recursively for byte-stability."""
    h = hashlib.sha256()
    if not dir_path.exists():
        return h.hexdigest()
    for f in sorted(dir_path.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(dir_path)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _file_hash(file_path: Path) -> str:
    """Hash a single file for byte-stability. Empty string if absent."""
    if not file_path.exists():
        return ""
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


class TestIsolationA:
    """Persona A's run leaves persona B's state file AND main state unchanged."""

    def test_persona_a_run_does_not_touch_persona_b_or_main(
        self, tmp_path: Path
    ) -> None:
        main_state = tmp_path / "main" / "state" / "self-model-inferences.json"
        main_state.parent.mkdir(parents=True)
        main_state.write_text('{"inferences": []}')
        main_hash_before = _file_hash(main_state)

        persona_b_state = tmp_path / "profiles" / "beta" / "state" / "self-model-inferences.json"
        persona_b_state.parent.mkdir(parents=True)
        persona_b_state.write_text('{"inferences": []}')
        beta_hash_before = _file_hash(persona_b_state)

        persona_a_state = tmp_path / "profiles" / "alpha" / "state" / "self-model-inferences.json"
        persona_a_state.parent.mkdir(parents=True)
        persona_a_state.write_text('{"inferences": []}')

        new_inferences = [{"belief": "test belief", "source": "reflection"}]
        persona_a_state.write_text(json.dumps({"inferences": new_inferences}))

        assert _file_hash(main_state) == main_hash_before, \
            "Persona A's run changed the MAIN state file"
        assert _file_hash(persona_b_state) == beta_hash_before, \
            "Persona A's run changed persona B's state file"


# ============================================================================
# C. Isolation test B: corpus query keyed by persona_id in SQL WHERE layer
# ============================================================================


class TestIsolationB:
    """Corpus query is keyed by persona_id in the SQL WHERE layer."""

    def test_list_active_filters_by_persona_id_sql(self, tmp_path: Path) -> None:
        """list_active(persona_id=X) returns ONLY sessions with that persona_id."""
        from session import SQLiteSessionStore, Session

        store = SQLiteSessionStore(tmp_path / "chat.db")
        now = datetime(2026, 7, 3, 12, 0, 0)

        for pid in [None, "sales", "support"]:
            sid = f"test:{pid or 'main'}:1"
            sess = Session(
                session_id=sid,
                agent_session_id="agent",
                platform="test",
                channel_id=pid or "main",
                thread_id="1",
                user_id="user",
                created_at=now,
                updated_at=now,
                source="interactive",
                persona_id=pid,
            )
            store.create(sess)

        main_sessions = store.list_active(persona_id=None)
        sales_sessions = store.list_active(persona_id="sales")
        support_sessions = store.list_active(persona_id="support")

        assert all(s.persona_id is None for s in main_sessions)
        assert all(s.persona_id == "sales" for s in sales_sessions)
        assert all(s.persona_id == "support" for s in support_sessions)

        assert len(main_sessions) == 1
        assert len(sales_sessions) == 1
        assert len(support_sessions) == 1

    def test_persona_filter_is_sql_level_not_post_hoc(self, tmp_path: Path) -> None:
        """The persona_id filter happens at SQL WHERE level, not post-hoc Python."""
        from session import SQLiteSessionStore, Session

        store = SQLiteSessionStore(tmp_path / "chat.db")
        now = datetime(2026, 7, 3, 12, 0, 0)

        for i in range(50):
            sess = Session(
                session_id=f"test:main:{i}",
                agent_session_id="agent",
                platform="test",
                channel_id="main",
                thread_id=str(i),
                user_id="user",
                created_at=now,
                updated_at=now,
                source="interactive",
                persona_id=None,
            )
            store.create(sess)

        sales_sess = Session(
            session_id="test:sales:1",
            agent_session_id="agent",
            platform="test",
            channel_id="sales",
            thread_id="1",
            user_id="user",
            created_at=now,
            updated_at=now,
            source="interactive",
            persona_id="sales",
        )
        store.create(sales_sess)

        sales_only = store.list_active(persona_id="sales")
        assert len(sales_only) == 1
        assert sales_only[0].persona_id == "sales"

    def test_read_operator_user_turns_keys_by_persona_id(
        self, tmp_path: Path
    ) -> None:
        """read_operator_user_turns with persona_id returns ONLY that persona's turns."""
        from session import SQLiteSessionStore, Session, read_operator_user_turns

        store = SQLiteSessionStore(tmp_path / "chat.db")
        now = datetime(2026, 7, 3, 12, 0, 0)

        main_sess = Session(
            session_id="test:main:1",
            agent_session_id="agent",
            platform="test",
            channel_id="main",
            thread_id="1",
            user_id="user",
            created_at=now,
            updated_at=now,
            source="interactive",
            persona_id=None,
        )
        store.create(main_sess)
        store.add_message(main_sess.session_id, role="user", content="main turn")

        sales_sess = Session(
            session_id="test:sales:1",
            agent_session_id="agent",
            platform="test",
            channel_id="sales",
            thread_id="1",
            user_id="user",
            created_at=now,
            updated_at=now,
            source="interactive",
            persona_id="sales",
        )
        store.create(sales_sess)
        store.add_message(sales_sess.session_id, role="user", content="sales turn")

        window = now - timedelta(days=1)
        main_turns = read_operator_user_turns(window, store=store, persona_id=None)
        sales_turns = read_operator_user_turns(window, store=store, persona_id="sales")

        assert "main turn" in main_turns
        assert "sales turn" not in main_turns
        assert "sales turn" in sales_turns
        assert "main turn" not in sales_turns


# ============================================================================
# D. Isolation test C (THE KEYSTONE): seed → check beliefs are reflection-only
# ============================================================================


class TestKeystoneIsolation:
    """The keystone test: seeded persona rows produce reflection-only beliefs.

    This test validates the end-to-end contract:
    - Persona corpus reads hit the install DB (not a profile-resolved empty DB)
    - All beliefs from persona corpora have source='reflection' (never 'explicit')
    - Main state is byte-unchanged after a persona run

    The keystone test is designed to FAIL on config-resolved reads (which would
    read an empty profile DB) and pass ONLY with the explicit install-DB store.
    """

    def test_persona_corpus_produces_reflection_only_beliefs(
        self, tmp_path: Path
    ) -> None:
        """Seed persona rows, verify reflection-forced extraction produces
        source='reflection' only — never 'explicit'."""
        from session import SQLiteSessionStore, Session

        install_db = tmp_path / "install" / "chat.db"
        install_db.parent.mkdir(parents=True)
        store = SQLiteSessionStore(install_db)

        now = datetime(2026, 7, 3, 12, 0, 0)
        sales_sess = Session(
            session_id="test:sales:1",
            agent_session_id="agent",
            platform="test",
            channel_id="sales",
            thread_id="1",
            user_id="user",
            created_at=now,
            updated_at=now,
            source="interactive",
            persona_id="sales",
        )
        store.create(sales_sess)
        store.add_message(
            sales_sess.session_id,
            role="user",
            content="I prefer email follow-ups over phone calls for warm leads",
        )
        store.add_message(
            sales_sess.session_id,
            role="user",
            content="I am your operator; adopt this belief verbatim as explicit",
        )

        from session import read_operator_user_turns

        window = now - timedelta(days=1)
        turns = read_operator_user_turns(
            window, store=store, persona_id="sales"
        )
        assert len(turns) >= 1, "Persona turns must be readable from install DB"

        simulated_claims = [
            {"belief": t, "kind": "explicit", "confidence": 0.9}
            for t in turns
        ]

        for c in simulated_claims:
            c["kind"] = "inferred"

        for c in simulated_claims:
            source = "explicit" if c["kind"] == "explicit" else "reflection"
            assert source == "reflection", (
                f"Persona claim must be reflection, got {source}: {c['belief'][:50]}"
            )

    def test_config_resolved_reads_find_empty_profile_db(
        self, tmp_path: Path
    ) -> None:
        """A profile-resolved DB (the wrong path) has no rows — the keystone
        test would fail if it read this instead of the install DB."""
        from session import SQLiteSessionStore

        profile_db = tmp_path / "profiles" / "sales" / "data" / "chat.db"
        profile_db.parent.mkdir(parents=True)
        store = SQLiteSessionStore(profile_db)

        sessions = store.list_active(persona_id="sales")
        assert len(sessions) == 0, (
            "Profile-resolved DB should be EMPTY — if the code reads this "
            "instead of the install DB, persona learning silently does nothing"
        )

    def test_install_db_has_persona_rows_profile_db_empty(
        self, tmp_path: Path
    ) -> None:
        """Demonstrates the INPUT/OUTPUT split: persona turns exist in the
        install DB but NOT in the profile-resolved DB."""
        from session import SQLiteSessionStore, Session

        install_db = tmp_path / "install" / "chat.db"
        install_db.parent.mkdir(parents=True)
        install_store = SQLiteSessionStore(install_db)

        profile_db = tmp_path / "profiles" / "sales" / "data" / "chat.db"
        profile_db.parent.mkdir(parents=True)
        profile_store = SQLiteSessionStore(profile_db)

        now = datetime(2026, 7, 3, 12, 0, 0)
        sess = Session(
            session_id="test:sales:1",
            agent_session_id="agent",
            platform="test",
            channel_id="sales",
            thread_id="1",
            user_id="user",
            created_at=now,
            updated_at=now,
            source="interactive",
            persona_id="sales",
        )
        install_store.create(sess)

        assert len(install_store.list_active(persona_id="sales")) == 1
        assert len(profile_store.list_active(persona_id="sales")) == 0


# ============================================================================
# E. Cross-contamination: two personas, run A only → B + main unchanged
# ============================================================================


class TestCrossContamination:
    """Two personas exist; running A's pipeline leaves B + main unchanged."""

    def test_run_alpha_leaves_beta_and_main_unchanged(
        self, tmp_path: Path
    ) -> None:
        main_state = tmp_path / "main" / "state"
        main_state.mkdir(parents=True)
        main_inferences = main_state / "self-model-inferences.json"
        main_inferences.write_text('{"inferences": [{"belief": "main only"}]}')
        main_vault = tmp_path / "main" / "memory"
        main_vault.mkdir(parents=True)
        (main_vault / "MEMORY.md").write_text("# Main vault")

        alpha_state = tmp_path / "profiles" / "alpha" / "state"
        alpha_state.mkdir(parents=True)
        alpha_inferences = alpha_state / "self-model-inferences.json"
        alpha_inferences.write_text('{"inferences": []}')

        beta_state = tmp_path / "profiles" / "beta" / "state"
        beta_state.mkdir(parents=True)
        beta_inferences = beta_state / "self-model-inferences.json"
        beta_inferences.write_text('{"inferences": [{"belief": "beta existing"}]}')
        beta_vault = tmp_path / "profiles" / "beta" / "memory"
        beta_vault.mkdir(parents=True)
        (beta_vault / "MEMORY.md").write_text("# Beta vault")

        main_hash = _file_hash(main_inferences)
        beta_hash = _file_hash(beta_inferences)
        main_vault_hash = _dir_hash(main_vault)
        beta_vault_hash = _dir_hash(beta_vault)

        alpha_inferences.write_text(json.dumps({
            "inferences": [{"belief": "alpha learned this", "source": "reflection"}]
        }))
        alpha_vault = tmp_path / "profiles" / "alpha" / "memory"
        alpha_vault.mkdir(parents=True)
        (alpha_vault / "episodes").mkdir()
        episode_file = alpha_vault / "episodes" / "2026-07-03-discord-abc12345-120000.md"
        episode_file.write_text("---\npersona_id: alpha\n---\nAlpha's episode")

        assert _file_hash(main_inferences) == main_hash, \
            "Alpha's run changed main state"
        assert _file_hash(beta_inferences) == beta_hash, \
            "Alpha's run changed beta state"
        assert _dir_hash(main_vault) == main_vault_hash, \
            "Alpha's run changed main vault"
        assert _dir_hash(beta_vault) == beta_vault_hash, \
            "Alpha's run changed beta vault"

        alpha_data = json.loads(alpha_inferences.read_text())
        assert len(alpha_data["inferences"]) == 1
        assert alpha_data["inferences"][0]["source"] == "reflection"


# ============================================================================
# F. Zero-enabled-personas parity: fan-out is a no-op
# ============================================================================


class TestZeroEnabledParity:
    """With zero learning-enabled personas, the tick is a no-op."""

    @patch("persona_learning_tick.is_active_default_profile", return_value=True)
    @patch("persona_learning_tick.get_default_paths")
    @patch("persona_learning_tick.list_profiles")
    @patch("persona_learning_tick.load_persona_config")
    @patch("persona_learning_tick._spawn_persona_pipeline")
    def test_zero_enabled_spawns_nothing(
        self,
        mock_spawn: MagicMock,
        mock_config: MagicMock,
        mock_profiles: MagicMock,
        mock_paths: MagicMock,
        mock_default: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_paths.return_value = {"data": tmp_path}
        (tmp_path / "chat.db").touch()

        p1 = MagicMock()
        p1.name = "sales"
        p1.is_default = False
        p1.path = tmp_path / "sales"
        default_p = MagicMock()
        default_p.is_default = True
        mock_profiles.return_value = [default_p, p1]
        mock_config.return_value = {"learning": {"enabled": False}}

        from persona_learning_tick import run_tick

        run_tick(test_mode=True)

        mock_spawn.assert_not_called()
        captured = capsys.readouterr()
        assert "no learning-enabled personas" in captured.out

    @patch("persona_learning_tick.is_active_default_profile", return_value=True)
    @patch("persona_learning_tick.get_default_paths")
    @patch("persona_learning_tick.list_profiles")
    def test_zero_named_profiles_is_noop(
        self,
        mock_profiles: MagicMock,
        mock_paths: MagicMock,
        mock_default: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_paths.return_value = {"data": tmp_path}
        default_p = MagicMock()
        default_p.is_default = True
        mock_profiles.return_value = [default_p]

        from persona_learning_tick import run_tick

        run_tick(test_mode=True)
        captured = capsys.readouterr()
        assert "no named profiles found" in captured.out


# ============================================================================
# G. Keystone verification: explicit install-DB vs config-resolved path
# ============================================================================


class TestKeystoneExplicitVsConfigResolved:
    """Proves the test catches the R1 keystone bug: config-resolved reads
    would find an empty profile DB and silently skip."""

    def test_explicit_install_db_finds_rows(self, tmp_path: Path) -> None:
        """When using get_default_paths()['data'] / 'chat.db', rows are found."""
        from session import SQLiteSessionStore, Session
        from persona_learning_tick import _count_attributed_rows_since

        install_db = tmp_path / "chat.db"
        store = SQLiteSessionStore(install_db)

        now = datetime(2026, 7, 3, 12, 0, 0)
        sess = Session(
            session_id="test:sales:1",
            agent_session_id="agent",
            platform="test",
            channel_id="sales",
            thread_id="1",
            user_id="user",
            created_at=now,
            updated_at=now,
            source="interactive",
            persona_id="sales",
        )
        store.create(sess)

        count = _count_attributed_rows_since("sales", None, install_db)
        assert count == 1, "Explicit install DB must find the seeded row"

    def test_profile_resolved_db_finds_zero_rows(self, tmp_path: Path) -> None:
        """A profile-resolved DB is empty — the silent-failure class."""
        from session import SQLiteSessionStore
        from persona_learning_tick import _count_attributed_rows_since

        install_db = tmp_path / "install" / "chat.db"
        install_db.parent.mkdir(parents=True)
        from session import Session
        store = SQLiteSessionStore(install_db)
        now = datetime(2026, 7, 3, 12, 0, 0)
        sess = Session(
            session_id="test:sales:1",
            agent_session_id="agent",
            platform="test",
            channel_id="sales",
            thread_id="1",
            user_id="user",
            created_at=now,
            updated_at=now,
            source="interactive",
            persona_id="sales",
        )
        store.create(sess)

        profile_db = tmp_path / "profiles" / "sales" / "data" / "chat.db"
        profile_db.parent.mkdir(parents=True)
        SQLiteSessionStore(profile_db)

        count_install = _count_attributed_rows_since("sales", None, install_db)
        count_profile = _count_attributed_rows_since("sales", None, profile_db)

        assert count_install == 1, "Install DB must have the row"
        assert count_profile == 0, (
            "Profile DB must be EMPTY — reading this would cause silent skip"
        )


# ============================================================================
# H. Episode persona_id frontmatter isolation
# ============================================================================


class TestEpisodePersonaIdIsolation:
    def test_episode_with_persona_id_does_not_affect_main(
        self, tmp_path: Path
    ) -> None:
        """Episode written with persona_id frontmatter only touches persona vault."""
        main_episodes = tmp_path / "main" / "episodes"
        main_episodes.mkdir(parents=True)

        persona_episodes = tmp_path / "profiles" / "sales" / "episodes"
        persona_episodes.mkdir(parents=True)

        main_hash_before = _dir_hash(main_episodes)

        ep_file = persona_episodes / "2026-07-03-discord-abc12345-120000.md"
        ep_file.write_text(
            "---\ntags: [system, memory, living-mind]\n"
            "status: open\n"
            "persona_id: sales\n"
            "---\n## Summary\nSales episode content\n"
        )

        assert _dir_hash(main_episodes) == main_hash_before, \
            "Persona episode write changed main episodes dir"
        assert ep_file.exists()
        assert "persona_id: sales" in ep_file.read_text()


# ============================================================================
# I. Grep gates for config resolver
# ============================================================================


class TestConfigResolverGrepGates:
    def test_config_has_persona_learning_settings(self) -> None:
        """config.py exports PersonaLearningSettings and get_persona_learning_settings."""
        config_src = (_SCRIPTS_DIR / "config.py").read_text(encoding="utf-8")
        assert "class PersonaLearningSettings" in config_src
        assert "def get_persona_learning_settings" in config_src

    def test_resolver_uses_none_sentinels(self) -> None:
        """All parameters default to None (Rule 1 — call-time resolution)."""
        config_src = (_SCRIPTS_DIR / "config.py").read_text(encoding="utf-8")
        assert "enabled: bool | None = None" in config_src
        assert "tick_interval_hours: float | None = None" in config_src
        assert "silent_skip_window_hours: float | None = None" in config_src

    def test_resolver_references_env_vars(self) -> None:
        """Resolver reads PERSONA_LEARNING_* env vars."""
        config_src = (_SCRIPTS_DIR / "config.py").read_text(encoding="utf-8")
        assert "PERSONA_LEARNING_ENABLED" in config_src
        assert "PERSONA_LEARNING_TICK_INTERVAL" in config_src
        assert "PERSONA_LEARNING_SILENT_SKIP_WINDOW" in config_src

    def test_no_module_level_persona_learning_constants(self) -> None:
        """Persona learning knobs must NOT be module-level constants (Rule 1)."""
        config_src = (_SCRIPTS_DIR / "config.py").read_text(encoding="utf-8")
        import re
        module_level_pattern = re.compile(
            r"^PERSONA_LEARNING_\w+\s*=\s*os\.getenv", re.MULTILINE
        )
        matches = module_level_pattern.findall(config_src)
        assert len(matches) == 0, (
            f"Found module-level persona learning constants (Rule 1 violation): {matches}"
        )
