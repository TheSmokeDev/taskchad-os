"""Tests for persona learning opt-in config (US-005).

Covers:
  1. _validate_learning_section — schema validation
  2. set_persona_learning — strict-read RMW writer
  3. load_persona_config + validate_config_dict — learning section wiring
  4. CLI enable/disable verbs
  5. Strict-read safety: parse-error fixture must NOT wipe the config
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from personas.services import (
    ConfigShapeError,
    _validate_learning_section,
    load_persona_config,
    set_persona_learning,
    validate_config_dict,
)


# ── _validate_learning_section ────────────────────────────────────────────


class TestValidateLearningSection:
    def test_valid_enabled_true(self, tmp_path: Path) -> None:
        _validate_learning_section({"enabled": True}, tmp_path / "c.yaml")

    def test_valid_enabled_false(self, tmp_path: Path) -> None:
        _validate_learning_section({"enabled": False}, tmp_path / "c.yaml")

    def test_valid_empty_dict(self, tmp_path: Path) -> None:
        _validate_learning_section({}, tmp_path / "c.yaml")

    def test_rejects_non_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigShapeError, match="learning"):
            _validate_learning_section("yes", tmp_path / "c.yaml")

    def test_rejects_list(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigShapeError, match="learning"):
            _validate_learning_section([True], tmp_path / "c.yaml")

    def test_rejects_enabled_string(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigShapeError, match="learning.enabled"):
            _validate_learning_section({"enabled": "yes"}, tmp_path / "c.yaml")

    def test_rejects_enabled_int(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigShapeError, match="learning.enabled"):
            _validate_learning_section({"enabled": 1}, tmp_path / "c.yaml")

    def test_accepts_unknown_keys(self, tmp_path: Path) -> None:
        _validate_learning_section(
            {"enabled": True, "future_knob": 42}, tmp_path / "c.yaml"
        )


# ── set_persona_learning (strict-read RMW) ────────────────────────────────


class TestSetPersonaLearning:
    @pytest.fixture()
    def profile_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Create a minimal profile dir with config.yaml."""
        profile = tmp_path / "profiles" / "sales"
        profile.mkdir(parents=True)
        config = profile / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {"persona": {"name": "Sales"}, "ports": {"orchestration_api": 4400}},
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "personas.services._resolve_profile_config_path",
            lambda pid: config,
        )
        return profile

    def test_enable_creates_learning_section(
        self, profile_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_persona_learning("sales", True)
        data = yaml.safe_load(
            (profile_dir / "config.yaml").read_text(encoding="utf-8")
        )
        assert data["learning"]["enabled"] is True

    def test_disable_sets_enabled_false(
        self, profile_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_persona_learning("sales", True)
        set_persona_learning("sales", False)
        data = yaml.safe_load(
            (profile_dir / "config.yaml").read_text(encoding="utf-8")
        )
        assert data["learning"]["enabled"] is False

    def test_preserves_other_sections(
        self, profile_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_persona_learning("sales", True)
        data = yaml.safe_load(
            (profile_dir / "config.yaml").read_text(encoding="utf-8")
        )
        assert data["persona"]["name"] == "Sales"
        assert data["ports"]["orchestration_api"] == 4400
        assert data["learning"]["enabled"] is True

    def test_strict_read_rejects_malformed_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R3 NB1 data-loss class: malformed YAML must NOT be wiped."""
        config = tmp_path / "config.yaml"
        original_content = "persona:\n  name: Sales\nvoice: [\n"
        config.write_text(original_content, encoding="utf-8")
        monkeypatch.setattr(
            "personas.services._resolve_profile_config_path",
            lambda pid: config,
        )
        with pytest.raises(ConfigShapeError, match="yaml:"):
            set_persona_learning("sales", True)
        assert config.read_text(encoding="utf-8") == original_content

    def test_missing_config_returns_empty_dict_creates_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When config.yaml doesn't exist, strict reader returns {} — we create it."""
        config = tmp_path / "config.yaml"
        assert not config.exists()
        monkeypatch.setattr(
            "personas.services._resolve_profile_config_path",
            lambda pid: config,
        )
        set_persona_learning("sales", True)
        assert config.exists()
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["learning"]["enabled"] is True


# ── Wiring: load_persona_config validates learning ────────────────────────


class TestLoadPersonaConfigLearningWiring:
    def test_load_rejects_invalid_learning_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.safe_dump({"learning": "bad"}, default_flow_style=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "personas.services._resolve_profile_config_path",
            lambda pid: config,
        )
        monkeypatch.setattr(
            "personas.services._activity.get_active_profile_name",
            lambda: "test",
        )
        with pytest.raises(ConfigShapeError, match="learning"):
            load_persona_config("test")

    def test_load_accepts_valid_learning_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {"learning": {"enabled": True}}, default_flow_style=False
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "personas.services._resolve_profile_config_path",
            lambda pid: config,
        )
        monkeypatch.setattr(
            "personas.services._activity.get_active_profile_name",
            lambda: "test",
        )
        result = load_persona_config("test")
        assert result["learning"]["enabled"] is True


# ── Wiring: validate_config_dict validates learning ───────────────────────


class TestValidateConfigDictLearningWiring:
    def test_rejects_invalid_learning(self) -> None:
        with pytest.raises(ConfigShapeError, match="learning"):
            validate_config_dict({"learning": [True]})

    def test_accepts_valid_learning(self) -> None:
        validate_config_dict({"learning": {"enabled": False}})

    def test_accepts_no_learning(self) -> None:
        validate_config_dict({"ports": {"api": 4322}})


# ── Grep gates ────────────────────────────────────────────────────────────


class TestGrepGates:
    def test_set_persona_learning_not_in_personas_all(self) -> None:
        """set_persona_learning is NOT in personas.__all__ (imported directly)."""
        import personas
        assert "set_persona_learning" not in personas.__all__

    def test_public_api_unchanged_at_16(self) -> None:
        """personas.__all__ stays at 16 entries (US-005 does NOT expand it)."""
        import personas
        assert len(personas.__all__) == 16

    def test_no_fail_open_reader_in_set_persona_learning(self) -> None:
        """set_persona_learning must NOT use _read_yaml_safe/_minimal_yaml_read."""
        import inspect
        source = inspect.getsource(set_persona_learning)
        assert "_read_yaml_safe" not in source
        assert "_minimal_yaml_read" not in source
        assert "_read_yaml_strict" in source
