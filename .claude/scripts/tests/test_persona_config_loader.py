"""PRD-8 Phase 2 / WS1 — public reader tests for ``personas.load_persona_config``.

Covers the criteria locked in
``PRPs/contracts/prd-8-phase-2.json``:

* ``config_yaml_persona_section_validates`` — full schema, ports-only
  back-compat, missing-file FileNotFoundError, default vs named path
  resolution, malformed-YAML ConfigShapeError, R3 NM1 named-profile-with-
  missing-config raises (NOT empty dict).
* ``config_yaml_uses_pyyaml`` — pyyaml-backed read path, port-only
  fixtures round-trip identically.
* ``port_persistence_refuses_to_clobber_malformed_yaml`` — R3 NB1 +
  R4 NM3: ``allocate_port()`` refuses to overwrite a malformed
  ``config.yaml`` (operator typo ``voice: [`` OR a non-mapping
  ``ports: "4322"`` value) with a ports-only dict.
* ``config_yaml_schema_validation_rejects_invalid`` — five concrete bad
  shapes raise ``ConfigShapeError`` with field path; valid shapes do not.

R2 NM2: tests use ``tmp_path`` fixtures only — never read the real
``vault/memory/`` (sanitizer-denied; non-reproducible).

Anti-pattern enforcement (Rules 1-3):
* Rule 1 — ``load_persona_config(persona_id=None)`` resolves the active
  profile inside the body via ``_activity.get_active_profile_name()``.
* Rule 2 — file content is read on every call (no module-level cache);
  ``_write_persisted_port`` reads physical YAML state via
  ``_read_yaml_strict()`` before mutating + writing back.
* Rule 3 — tests monkey-patch ``personas.activity.get_active_profile_name``
  at the module attribute (``services._activity``), proving the
  module-attribute lookup pattern propagates.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

import personas
from personas import services as _services

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed_named_profile_config(profile_dir: Path, body: str) -> Path:
    """Write *body* to ``<profile_dir>/config.yaml`` and return the path.

    Named-profile config.yaml lives at ``<profile_root>/config.yaml`` per
    ``_resolve_profile_config_path`` (NOT under ``state/``).
    """
    config_path = profile_dir / "config.yaml"
    config_path.write_text(body, encoding="utf-8")
    return config_path


def _activate_named(
    monkeypatch: pytest.MonkeyPatch, profile_name: str
) -> None:
    """Monkey-patch the active profile via the module-attribute pattern.

    Rule 3 — tests patch ``personas.activity.get_active_profile_name``;
    ``services._activity`` re-exports the same attribute, so the patch
    propagates as long as we go through the module attribute (not a
    cached function object).
    """
    monkeypatch.setattr(
        personas.activity,
        "get_active_profile_name",
        lambda: profile_name,
    )


# ---------------------------------------------------------------------------
# load_persona_config — happy path / back-compat / missing-file
# ---------------------------------------------------------------------------


def test_load_persona_config_full_schema(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full schema config returns a dict with all six recognised sections."""
    sales_dir = multi_profile_fixture["sales"]
    _seed_named_profile_config(
        sales_dir,
        "ports:\n"
        "  orchestration_api: 4322\n"
        "persona:\n"
        "  id: sales\n"
        "  name: Sales Homie\n"
        "model:\n"
        "  preferred: claude-sonnet-4-7\n"
        "  fallback:\n"
        "    - codex\n"
        "    - gemini\n"
        "mcp:\n"
        "  servers:\n"
        "    - brave-search\n"
        "    - exa\n"
        "cabinet:\n"
        "  voice_id: VOICE_ID_EXAMPLE\n"
        "  tools:\n"
        "    - slack-cli\n"
        "voice:\n"
        "  cascade: [edge, gradium]\n",
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _activate_named(monkeypatch, "sales")

    data = personas.load_persona_config()

    assert "ports" in data
    assert data["ports"]["orchestration_api"] == 4322
    assert data["persona"]["id"] == "sales"
    assert data["persona"]["name"] == "Sales Homie"
    assert data["model"]["preferred"] == "claude-sonnet-4-7"
    assert data["model"]["fallback"] == ["codex", "gemini"]
    assert data["mcp"]["servers"] == ["brave-search", "exa"]
    assert data["cabinet"]["voice_id"] == "VOICE_ID_EXAMPLE"
    assert data["cabinet"]["tools"] == ["slack-cli"]
    # Q5 canonical default-cascade shape: list of provider names as strings.
    assert data["voice"]["cascade"] == ["edge", "gradium"]


def test_load_persona_config_voice_cascade_mapping_shape(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q5 also permits mapping items for opt-in per-persona tuning.

    Mapping items must have a ``provider`` key; mapping items are reserved
    for cases like ElevenLabs voice-cloning where a ``voice_id`` rides
    alongside the provider name.
    """
    sales_dir = multi_profile_fixture["sales"]
    config_path = sales_dir / "config.yaml"
    config_path.write_text(
        "voice:\n"
        "  cascade:\n"
        "    - edge\n"
        "    - provider: elevenlabs\n"
        "      voice_id: VOICE_ID_EXAMPLE\n",
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _activate_named(monkeypatch, "sales")

    data = personas.load_persona_config()

    assert data["voice"]["cascade"][0] == "edge"
    assert data["voice"]["cascade"][1]["provider"] == "elevenlabs"
    assert data["voice"]["cascade"][1]["voice_id"] == "VOICE_ID_EXAMPLE"


def test_validation_rejects_voice_cascade_unknown_string_provider(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown provider names must be rejected even in the bare-string shape."""
    sales_dir = multi_profile_fixture["sales"]
    config_path = sales_dir / "config.yaml"
    config_path.write_text("voice:\n  cascade: [unknown_xyz]\n")
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _activate_named(monkeypatch, "sales")

    with pytest.raises(personas.ConfigShapeError) as excinfo:
        personas.load_persona_config()

    msg = str(excinfo.value)
    assert "voice.cascade" in msg
    assert "unknown_xyz" in msg


def test_validation_rejects_voice_cascade_non_string_non_mapping_item(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cascade items must be either string or mapping — int/None/list are invalid."""
    sales_dir = multi_profile_fixture["sales"]
    config_path = sales_dir / "config.yaml"
    config_path.write_text("voice:\n  cascade: [42]\n")
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _activate_named(monkeypatch, "sales")

    with pytest.raises(personas.ConfigShapeError) as excinfo:
        personas.load_persona_config()

    msg = str(excinfo.value)
    assert "voice.cascade[0]" in msg
    assert "str or mapping" in msg


def test_load_persona_config_ports_only_backwards_compat(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ports-only legacy config loads fine; new sections are ABSENT (not None)."""
    sales_dir = multi_profile_fixture["sales"]
    _seed_named_profile_config(
        sales_dir,
        "ports:\n"
        "  orchestration_api: 4322\n"
        "  health_check: 8787\n",
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _activate_named(monkeypatch, "sales")

    data = personas.load_persona_config()

    assert data["ports"]["orchestration_api"] == 4322
    assert data["ports"]["health_check"] == 8787
    # Missing sections are absent — NOT None, NOT empty dict.
    for section in ("persona", "model", "mcp", "cabinet", "voice"):
        assert section not in data, (
            f"Missing section {section!r} should be absent from the dict, "
            f"got {data.get(section)!r}"
        )


def test_load_persona_config_missing_file_raises_filenotfound(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit named-profile lookup with no config.yaml → FileNotFoundError.

    Path-string in message must include the resolved absolute path so the
    operator can fix the setup.
    """
    sales_dir = multi_profile_fixture["sales"]
    # No config.yaml seeded.
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))

    with pytest.raises(FileNotFoundError) as excinfo:
        personas.load_persona_config("sales")

    msg = str(excinfo.value)
    assert "sales" in msg
    assert str(sales_dir / "config.yaml") in msg


def test_load_persona_config_default_path_resolution(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Default profile reads ``paths['state'] / 'config.yaml'``;
    named profiles read ``paths['state'].parent / 'config.yaml'``.

    Verifies both branches of ``_resolve_profile_config_path()`` round-trip
    a value through ``load_persona_config()``. Default-profile branch is
    exercised by routing the install dir into ``tmp_path`` so the test
    never touches the real repo.
    """
    # --- Named profile branch ---
    sales_dir = multi_profile_fixture["sales"]
    named_config = _seed_named_profile_config(
        sales_dir, "ports:\n  orchestration_api: 5000\n"
    )
    # Confirm the file lives at <profile_root>/config.yaml (NOT under state/).
    assert named_config == sales_dir / "config.yaml"
    assert named_config.parent == sales_dir
    data = personas.load_persona_config("sales")
    assert data["ports"]["orchestration_api"] == 5000

    # --- Default profile branch ---
    # Default profile config lives at get_persona_paths("default")["state"]
    # / config.yaml. Route the install dir into tmp_path via a patch on
    # ``personas.core.get_persona_paths`` so ``_resolve_profile_config_path``
    # (which calls ``get_persona_paths`` from services-imported core) sees
    # the fake state dir. The test stays hermetic — never touches the real
    # ``<install>/.claude/data/state``.
    fake_state = tmp_path / "fake_default_state"
    fake_state.mkdir()
    fake_default_paths = {"state": fake_state}

    real_get_persona_paths = personas.core.get_persona_paths

    def _fake_get_persona_paths(name: str) -> dict[str, Path]:
        if name == "default":
            return fake_default_paths
        return real_get_persona_paths(name)

    # services.py imports ``get_persona_paths`` directly from core, so the
    # binding lives on the services module. Patch BOTH the services-side
    # binding (used by ``_resolve_profile_config_path``) and the core-side
    # source (defense-in-depth in case anyone re-imports).
    monkeypatch.setattr(
        _services, "get_persona_paths", _fake_get_persona_paths
    )
    monkeypatch.setattr(
        personas.core, "get_persona_paths", _fake_get_persona_paths
    )
    (fake_state / "config.yaml").write_text(
        "ports:\n  orchestration_api: 4322\n",
        encoding="utf-8",
    )
    data2 = personas.load_persona_config("default")
    assert data2["ports"]["orchestration_api"] == 4322


def test_load_persona_config_malformed_yaml_raises_config_shape_error(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 NB1 — operator typo ``voice: [`` raises ConfigShapeError, NOT {}.

    Strict reader: ``load_persona_config()`` does NOT delegate to
    ``_read_yaml_safe()``. A malformed file MUST surface, otherwise Phase 3
    treats the file as an intentionally empty config.
    """
    sales_dir = multi_profile_fixture["sales"]
    config_path = _seed_named_profile_config(
        sales_dir,
        "ports:\n"
        "  orchestration_api: 4322\n"
        "voice: [\n",  # unclosed list — operator typo
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))

    with pytest.raises(personas.ConfigShapeError) as excinfo:
        personas.load_persona_config("sales")
    msg = str(excinfo.value)
    assert "yaml:" in msg
    assert str(config_path) in msg


def test_load_persona_config_named_profile_missing_raises(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 NM1 — active named profile + missing config.yaml MUST raise.

    Empty-dict back-compat applies ONLY when ``persona_id is None AND
    actual_id == 'default'``. Active named profile (``HOMIE_HOME=...sales``)
    with no config.yaml is a setup error, not a bootstrap state.
    """
    sales_dir = multi_profile_fixture["sales"]
    # No config.yaml seeded.
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _activate_named(monkeypatch, "sales")

    # persona_id=None resolves to "sales" → must raise (NOT return {}).
    with pytest.raises(FileNotFoundError) as excinfo:
        personas.load_persona_config()
    msg = str(excinfo.value)
    assert "sales" in msg
    assert str(sales_dir / "config.yaml") in msg


def test_load_persona_config_default_profile_missing_returns_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R3 NM1 escape hatch: default profile + persona_id=None + no
    config.yaml → empty dict (default-profile bootstrap back-compat).

    This is the ONE scenario where a missing file is allowed to return ``{}``.
    """
    fake_state = tmp_path / "fake_default_state"
    fake_state.mkdir()
    fake_default_paths = {"state": fake_state}

    real_get_persona_paths = personas.core.get_persona_paths

    def _fake_get_persona_paths(name: str) -> dict[str, Path]:
        if name == "default":
            return fake_default_paths
        return real_get_persona_paths(name)

    # Patch BOTH bindings — services.py imports ``get_persona_paths`` from
    # core, so the in-services binding is what ``_resolve_profile_config_path``
    # actually calls. Patching personas.core too is defense-in-depth.
    monkeypatch.setattr(
        _services, "get_persona_paths", _fake_get_persona_paths
    )
    monkeypatch.setattr(
        personas.core, "get_persona_paths", _fake_get_persona_paths
    )
    _activate_named(monkeypatch, "default")

    # No config.yaml seeded — default profile + None sentinel allows empty dict.
    data = personas.load_persona_config()
    assert data == {}


# ---------------------------------------------------------------------------
# pyyaml adoption — round-trip parity + functional usage
# ---------------------------------------------------------------------------


def test_minimal_yaml_read_uses_pyyaml(tmp_path: Path) -> None:
    """``_read_yaml_safe`` (alias ``_minimal_yaml_read``) parses pyyaml shapes.

    Functional test (not just import grep): asserts the helper round-trips
    list-valued fields and nested mappings — capabilities the legacy
    mini-parser did NOT support. If the helper still used the mini-parser,
    ``mcp.servers`` would come back as the literal string ``"- brave-search"``
    (or be silently dropped) instead of a Python list.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "mcp:\n"
        "  servers:\n"
        "    - brave-search\n"
        "    - exa\n"
        "voice:\n"
        "  cascade:\n"
        "    - provider: edge\n"
        "    - provider: elevenlabs\n",
        encoding="utf-8",
    )
    data = _services._read_yaml_safe(config_path)
    # pyyaml shape: lists are Python lists, nested mappings are dicts.
    assert isinstance(data["mcp"]["servers"], list)
    assert data["mcp"]["servers"] == ["brave-search", "exa"]
    assert isinstance(data["voice"]["cascade"], list)
    assert data["voice"]["cascade"][0]["provider"] == "edge"
    # Confirm the alias points at the same callable as the canonical name.
    assert _services._minimal_yaml_read is _services._read_yaml_safe


def test_existing_ports_round_trip_parity(tmp_path: Path) -> None:
    """Port-only configs round-trip identically through safe_load/safe_dump.

    Operators with pre-PRD-8 port-only configs MUST not see byte drift on
    the next ``allocate_port()`` write-back. ``default_flow_style=False``
    keeps block style; ``sort_keys=False`` preserves insertion order.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "ports:\n"
        "  orchestration_api: 4322\n"
        "  health_check: 8787\n",
        encoding="utf-8",
    )
    # Read via the new helper, write it back, read again — dicts equal.
    data1 = _services._minimal_yaml_read(config_path)
    _services._minimal_yaml_write(config_path, data1)
    data2 = _services._minimal_yaml_read(config_path)
    assert data1 == data2
    # safe_load on the produced text matches the dict.
    text_after = config_path.read_text(encoding="utf-8")
    assert yaml.safe_load(text_after) == data1


# ---------------------------------------------------------------------------
# Schema validation — five invalid shapes
# ---------------------------------------------------------------------------


def test_validation_rejects_cabinet_voice_id_int(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cabinet.voice_id`` must be str — int raises ConfigShapeError."""
    sales_dir = multi_profile_fixture["sales"]
    _seed_named_profile_config(
        sales_dir,
        "cabinet:\n  voice_id: 42\n",  # bad: int instead of str
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))

    with pytest.raises(personas.ConfigShapeError) as excinfo:
        personas.load_persona_config("sales")
    assert "cabinet.voice_id" in str(excinfo.value)


def test_validation_rejects_mcp_servers_string(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mcp.servers`` must be list — string raises ConfigShapeError."""
    sales_dir = multi_profile_fixture["sales"]
    _seed_named_profile_config(
        sales_dir,
        "mcp:\n  servers: brave-search\n",  # bad: str instead of list
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))

    with pytest.raises(personas.ConfigShapeError) as excinfo:
        personas.load_persona_config("sales")
    assert "mcp.servers" in str(excinfo.value)


def test_validation_rejects_voice_cascade_unknown_provider(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown voice provider raises ConfigShapeError naming the provider."""
    sales_dir = multi_profile_fixture["sales"]
    _seed_named_profile_config(
        sales_dir,
        "voice:\n  cascade:\n    - provider: unknown_xyz\n",
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))

    with pytest.raises(personas.ConfigShapeError) as excinfo:
        personas.load_persona_config("sales")
    msg = str(excinfo.value)
    assert "unknown_xyz" in msg
    assert "voice.cascade" in msg


def test_validation_rejects_model_fallback_non_string_item(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``model.fallback`` items must be str — int item raises with field path."""
    sales_dir = multi_profile_fixture["sales"]
    _seed_named_profile_config(
        sales_dir,
        "model:\n  fallback:\n    - codex\n    - 42\n",  # bad: int item
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))

    with pytest.raises(personas.ConfigShapeError) as excinfo:
        personas.load_persona_config("sales")
    msg = str(excinfo.value)
    assert "model.fallback[1]" in msg


def test_validation_rejects_persona_section_non_mapping(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``persona`` section must be a mapping — string raises ConfigShapeError."""
    sales_dir = multi_profile_fixture["sales"]
    _seed_named_profile_config(
        sales_dir,
        "persona: just-a-string\n",  # bad: scalar instead of mapping
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))

    with pytest.raises(personas.ConfigShapeError) as excinfo:
        personas.load_persona_config("sales")
    assert "persona" in str(excinfo.value)


def test_validation_accepts_valid_full_schema(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully valid full-schema config does NOT raise (no false positives).

    Defense-in-depth for ``config_yaml_schema_validation_rejects_invalid``'s
    "no false positives in fixtures" requirement.
    """
    sales_dir = multi_profile_fixture["sales"]
    _seed_named_profile_config(
        sales_dir,
        "ports:\n"
        "  orchestration_api: 4322\n"
        "persona:\n"
        "  id: sales\n"
        "  display_name: Sales Homie\n"
        "model:\n"
        "  preferred: claude-sonnet-4-7\n"
        "  fallback: [codex, gemini]\n"
        "mcp:\n"
        "  servers: [brave-search, exa]\n"
        "cabinet:\n"
        "  voice_id: VOICE_ID_EXAMPLE\n"
        "  tools: [slack-cli]\n"
        "voice:\n"
        "  cascade:\n"
        "    - provider: edge\n"
        "    - provider: elevenlabs\n",
    )
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))

    # Must not raise.
    data = personas.load_persona_config("sales")
    assert data["persona"]["id"] == "sales"


# ---------------------------------------------------------------------------
# R3 NB1 — port-write refuses to clobber malformed YAML
# ---------------------------------------------------------------------------


def test_allocate_port_refuses_to_clobber_malformed_config(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 NB1 regression gate — operator typo ``voice: [`` is preserved.

    Pre-fix behavior: ``_minimal_yaml_read`` failed open with ``{}`` on the
    malformed YAML; the next ``allocate_port`` then wrote a ports-only dict
    BACK to the file, destroying the persona/model/cabinet/voice sections.
    R3 NB1 fix splits the read paths — write-back goes through
    ``_read_yaml_strict`` which raises ``ConfigShapeError``.

    Test asserts:
      1. ``ConfigShapeError`` is raised by ``allocate_port``.
      2. The file bytes on disk are byte-identical to the seeded malformed
         bytes (sha256 before/after compare).
    """
    sales_dir = multi_profile_fixture["sales"]
    malformed_body = (
        "ports:\n"
        "  orchestration_api: 4322\n"
        "voice: [\n"  # unclosed list — operator typo class
    )
    config_path = _seed_named_profile_config(sales_dir, malformed_body)
    sha_before = hashlib.sha256(config_path.read_bytes()).hexdigest()

    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _activate_named(monkeypatch, "sales")
    monkeypatch.delenv("ORCHESTRATION_API_PORT", raising=False)

    with pytest.raises(personas.ConfigShapeError):
        _services.allocate_port("orchestration_api", profile_name="sales")

    # CRITICAL: file bytes UNCHANGED — no silent overwrite.
    sha_after = hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert sha_before == sha_after, (
        "allocate_port silently overwrote a malformed config.yaml — "
        "R3 NB1 fix regressed."
    )


def test_allocate_port_refuses_to_clobber_non_mapping_ports(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 NM3 — ``ports: "4322"`` (parsable, but ports is a str) refuses write.

    The YAML parses successfully (``yaml.safe_load`` returns
    ``{"ports": "4322"}``), so ``_read_yaml_strict`` accepts it. The new
    guard inside ``_write_persisted_port`` MUST then catch the
    non-mapping ``ports`` value and raise ``ConfigShapeError`` rather than
    silently replacing the string with a fresh ports dict.

    File bytes MUST NOT change.
    """
    sales_dir = multi_profile_fixture["sales"]
    body = "ports: '4322'\n"  # parses fine, but ports is a string
    config_path = _seed_named_profile_config(sales_dir, body)
    sha_before = hashlib.sha256(config_path.read_bytes()).hexdigest()

    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    _activate_named(monkeypatch, "sales")
    monkeypatch.delenv("ORCHESTRATION_API_PORT", raising=False)

    with pytest.raises(personas.ConfigShapeError) as excinfo:
        _services.allocate_port("orchestration_api", profile_name="sales")
    assert "ports" in str(excinfo.value)

    # File bytes unchanged.
    sha_after = hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert sha_before == sha_after
