"""Tests for personas.services public validation helpers (PRD-8 Phase 3 / WS2).

R1 B4 disposition — dashboard PATCH /api/agents/{id}/files/config.yaml
endpoint MUST validate operator-authored YAML through
``personas.validate_config_yaml_text`` (NOT a duplicated YAML parser in
the dashboard slice — Q5 single-yaml-surface lock).

These tests cover the two new helpers that grow ``personas.__all__`` from
14 → 16:

  * ``validate_config_dict(data)`` — validates an in-memory dict; reuses
    the internal ``_validate_*_section`` helpers; raises
    ``ConfigShapeError`` on schema violation.
  * ``validate_config_yaml_text(text)`` — parses YAML text + validates
    + returns the parsed dict on success; raises ``ConfigShapeError``
    on parse OR schema failure.
"""
from __future__ import annotations

import pytest

import personas
from personas.services import (
    ConfigShapeError,
    validate_config_dict,
    validate_config_yaml_text,
)


# ── validate_config_dict ─────────────────────────────────────────────────


def test_validate_config_dict_accepts_valid_shape() -> None:
    """Empty dict + dicts with all-known sections pass without raising."""
    # Empty dict is legal — operator may scaffold a config before
    # populating it.
    validate_config_dict({})

    # Full config with all 6 known sections (operator's reference shape).
    full = {
        "ports": {"orchestration_api": 4322, "health_check": 4323},
        "persona": {
            "id": "sales",
            "name": "Sales Homie",
            "display_name": "Sales",
            "role": "operator",
        },
        "model": {
            "preferred": "claude-opus-4-7",
            "fallback": ["openai_codex", "gemini"],
        },
        "mcp": {"servers": ["context7", {"name": "exa"}]},
        "cabinet": {"voice_id": "voice-A", "tools": ["search", "ingest"]},
        "voice": {"cascade": ["edge", "elevenlabs"]},
    }
    validate_config_dict(full)


def test_validate_config_dict_raises_config_shape_error_on_bad_section() -> None:
    """Schema violations surface as ``ConfigShapeError`` with field path."""
    # ``ports.X`` must be int, not str.
    bad_ports = {"ports": {"orchestration_api": "4322"}}
    with pytest.raises(ConfigShapeError) as exc_info:
        validate_config_dict(bad_ports)
    msg = str(exc_info.value)
    assert "ports.orchestration_api" in msg
    assert "int" in msg

    # ``persona.id`` must be str.
    bad_persona = {"persona": {"id": 42}}
    with pytest.raises(ConfigShapeError) as exc_info:
        validate_config_dict(bad_persona)
    assert "persona.id" in str(exc_info.value)

    # Top-level non-dict (list).
    with pytest.raises(ConfigShapeError) as exc_info:
        validate_config_dict([1, 2, 3])  # type: ignore[arg-type]
    assert "top-level must be mapping" in str(exc_info.value)


# ── validate_config_yaml_text ────────────────────────────────────────────


def test_validate_config_yaml_text_parses_and_validates() -> None:
    """Valid YAML text is parsed and validated, returning the parsed dict."""
    text = """
persona:
  id: sales
  name: Sales Homie
model:
  preferred: claude-opus-4-7
"""
    result = validate_config_yaml_text(text)
    assert isinstance(result, dict)
    assert result["persona"]["id"] == "sales"
    assert result["model"]["preferred"] == "claude-opus-4-7"

    # Empty text → empty dict (legal — operator may save scaffold).
    assert validate_config_yaml_text("") == {}


def test_validate_config_yaml_text_raises_on_yaml_error() -> None:
    """Malformed YAML raises ``ConfigShapeError`` with ``yaml:`` prefix."""
    # Unbalanced bracket — hard YAML parse error.
    text = "voice:\n  cascade: [edge, elevenlabs"
    with pytest.raises(ConfigShapeError) as exc_info:
        validate_config_yaml_text(text)
    msg = str(exc_info.value)
    assert msg.startswith("yaml:") or "yaml" in msg.lower()
    # Should reference the inline-text sentinel, NOT a real path.
    assert "<config-text>" in msg


def test_validate_config_yaml_text_raises_on_schema_error() -> None:
    """Schema violation in well-formed YAML still raises ``ConfigShapeError``."""
    # Well-formed YAML but ``model.fallback`` is a string (must be list).
    text = """
model:
  preferred: claude-opus-4-7
  fallback: codex
"""
    with pytest.raises(ConfigShapeError) as exc_info:
        validate_config_yaml_text(text)
    assert "model.fallback" in str(exc_info.value)


# ── public API exposure ──────────────────────────────────────────────────


def test_validate_helpers_exported_from_personas_package() -> None:
    """The two new helpers are reachable via ``personas.validate_*`` (R1 B4).

    PRD-8 Phase 3 / WS2 grows ``personas.__all__`` from 14 → 16. The
    ``test_personas_public_api.py`` suite owns the size assertion; this
    test only asserts the dashboard slice can ``from personas import
    validate_config_yaml_text`` without reaching into ``personas.services``.
    """
    assert hasattr(personas, "validate_config_dict")
    assert hasattr(personas, "validate_config_yaml_text")
    # Reachable via the package surface (not just the submodule).
    assert callable(personas.validate_config_dict)
    assert callable(personas.validate_config_yaml_text)
