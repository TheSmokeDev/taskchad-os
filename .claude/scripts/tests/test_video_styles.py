"""Unit tests for the video style registry + design resolution (video_styles).

Pure tests: no network, no render, no LLM. Covers:
  1. list_styles() shape (name kebab slug + tagline)
  2. resolve_design() precedence: file > name > env file > env style > neutral
  3. unknown explicit style raises ValueError naming the valid styles
  4. every registry design carries the required keys with valid hex colors
  5. lenient design-file parsing (markdown token scrape + JSON merge)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import video_styles  # noqa: E402
from video_styles import list_styles, resolve_design  # noqa: E402

_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

EXPECTED_STYLES = {
    "neutral",
    "blockframe",
    "coral",
    "capsule",
    "cobalt-grid",
    "editorial-forest",
    "bold-poster",
    "broadside",
    "blue-professional",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests control the env vars explicitly; clear any ambient values."""

    monkeypatch.delenv("VIDEO_STYLE", raising=False)
    monkeypatch.delenv("VIDEO_DESIGN_FILE", raising=False)


# =============================================================================
# 1. LIST SHAPE
# =============================================================================


def test_list_styles_shape() -> None:
    styles = list_styles()
    assert isinstance(styles, list)
    assert {s["name"] for s in styles} == EXPECTED_STYLES
    for entry in styles:
        assert set(entry.keys()) == {"name", "tagline"}
        assert _KEBAB.match(entry["name"]), f"not a kebab slug: {entry['name']}"
        assert isinstance(entry["tagline"], str) and entry["tagline"].strip()
        assert "\u2014" not in entry["tagline"]  # no em-dashes in user-facing text


# =============================================================================
# 2. PRECEDENCE CHAIN
# =============================================================================


def _write_json_design(tmp_path: Path) -> Path:
    design_file = tmp_path / "custom.json"
    design_file.write_text(
        json.dumps(
            {
                "name": "File Custom",
                "tagline": "from the file",
                "palette": {"bg": "#101010", "fg": "#FAFAFA", "accent": "#FF5500"},
            }
        ),
        encoding="utf-8",
    )
    return design_file


def test_design_file_param_beats_style_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    design_file = _write_json_design(tmp_path)
    monkeypatch.setenv("VIDEO_STYLE", "coral")
    design = resolve_design(style="blockframe", design_file=str(design_file))
    assert design["name"] == "file-custom"
    assert design["tagline"] == "from the file"


def test_style_param_beats_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    design_file = _write_json_design(tmp_path)
    monkeypatch.setenv("VIDEO_DESIGN_FILE", str(design_file))
    monkeypatch.setenv("VIDEO_STYLE", "coral")
    design = resolve_design(style="blockframe")
    assert design["name"] == "blockframe"


def test_env_design_file_beats_env_style(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    design_file = _write_json_design(tmp_path)
    monkeypatch.setenv("VIDEO_DESIGN_FILE", str(design_file))
    monkeypatch.setenv("VIDEO_STYLE", "coral")
    design = resolve_design()
    assert design["name"] == "file-custom"


def test_env_style_used_when_no_params(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_STYLE", "coral")
    assert resolve_design()["name"] == "coral"


def test_neutral_default_when_nothing_set() -> None:
    assert resolve_design()["name"] == "neutral"


def test_invalid_env_values_fail_open_to_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ambient config must never break a render: bad env falls through.
    monkeypatch.setenv("VIDEO_DESIGN_FILE", "/nonexistent/nowhere.md")
    monkeypatch.setenv("VIDEO_STYLE", "not-a-style")
    assert resolve_design()["name"] == "neutral"


def test_resolve_returns_a_copy_not_the_registry() -> None:
    a = resolve_design(style="coral")
    a["palette"]["bg"] = "#000001"
    b = resolve_design(style="coral")
    assert b["palette"]["bg"] != "#000001"


# =============================================================================
# 3. UNKNOWN STYLE RAISES
# =============================================================================


def test_unknown_style_raises_valueerror_naming_valid_styles() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_design(style="vaporwave-disco")
    message = str(excinfo.value)
    assert "vaporwave-disco" in message
    for name in ("blockframe", "coral", "neutral"):
        assert name in message


def test_style_name_normalization() -> None:
    # Spaces/underscores/case normalize onto the kebab slug.
    assert resolve_design(style="Cobalt Grid")["name"] == "cobalt-grid"
    assert resolve_design(style="BOLD_POSTER")["name"] == "bold-poster"


# =============================================================================
# 4. REGISTRY VALIDITY
# =============================================================================


def test_every_registry_design_has_required_keys_and_valid_hex() -> None:
    for entry in list_styles():
        design = resolve_design(style=entry["name"])
        assert design["name"] == entry["name"]
        assert isinstance(design["tagline"], str) and design["tagline"]

        palette = design["palette"]
        for role in ("bg", "fg", "accent", "accent_dim"):
            assert _HEX.match(palette[role]), (
                f"{entry['name']}.palette.{role} not hex: {palette[role]!r}"
            )
        for key, value in design.get("extras", {}).items():
            assert _HEX.match(value), f"{entry['name']}.extras.{key} not hex: {value!r}"

        fonts = design["fonts"]
        for key in ("display", "body", "mono"):
            assert isinstance(fonts[key], str) and fonts[key]
        assert fonts["google_fonts_url"].startswith("https://fonts.googleapis.com/css2?")

        motion = design["motion"]
        assert motion["entrance_ease"]
        assert motion["transition"] in {"crossfade", "cut", "slide"}

        assert isinstance(design["flags"], dict)


# =============================================================================
# 5. LENIENT DESIGN-FILE PARSING
# =============================================================================

MARKDOWN_DESIGN = """---
version: alpha
name: Demo Style (video / frame layer)
description: >
  A demo style for parser tests.
unit: the frame
---

colors:
  bg: "#101014"
  text: "#FAFAF5"
  accent: "#FF5500"
  extra-tone: "#22AA88"

typography:
  body:    { fontFamily: "Inter", cqw: 1.0, weight: 400 }
  label:   { fontFamily: "Space Mono", px: 13, weight: 600 }
  headline:{ fontFamily: "Playfair Display", cqw: 4.4, weight: 500 }
"""


def test_markdown_design_file_lenient_parse(tmp_path: Path) -> None:
    md = tmp_path / "demo-style.md"
    md.write_text(MARKDOWN_DESIGN, encoding="utf-8")

    design = resolve_design(design_file=str(md))
    assert design["name"] == "demo-style"
    assert design["tagline"] == "A demo style for parser tests."
    assert design["palette"]["bg"] == "#101014"
    assert design["palette"]["fg"] == "#FAFAF5"
    assert design["palette"]["accent"] == "#FF5500"
    # accent_dim was not declared: derived blend, still valid hex.
    assert _HEX.match(design["palette"]["accent_dim"])
    # Role-aware font mapping: display from the headline ramp, mono from label.
    assert design["fonts"]["display"] == "Playfair Display"
    assert design["fonts"]["body"] == "Inter"
    assert design["fonts"]["mono"] == "Space Mono"
    assert "Playfair+Display" in design["fonts"]["google_fonts_url"]


def test_json_design_file_merges_onto_neutral(tmp_path: Path) -> None:
    design_file = _write_json_design(tmp_path)
    design = resolve_design(design_file=str(design_file))
    # Missing sections fall back to the neutral defaults.
    assert design["fonts"]["display"]
    assert design["motion"]["transition"] in {"crossfade", "cut", "slide"}
    assert design["palette"]["bg"] == "#101010"
    assert _HEX.match(design["palette"]["accent_dim"])


def test_missing_design_file_raises() -> None:
    with pytest.raises(ValueError, match="not found"):
        resolve_design(design_file="/nonexistent/nowhere.md")


def test_thin_markdown_file_still_resolves(tmp_path: Path) -> None:
    thin = tmp_path / "thin.md"
    thin.write_text("just some prose, no tokens at all\n", encoding="utf-8")
    design = resolve_design(design_file=str(thin))
    assert design["name"] == "thin"
    # Falls back to neutral tokens rather than failing the render.
    assert _HEX.match(design["palette"]["bg"])
    assert design["fonts"]["display"]


def test_blend_hex_endpoints() -> None:
    assert video_styles.blend_hex("#000000", "#FFFFFF", 0.0) == "#000000"
    assert video_styles.blend_hex("#000000", "#FFFFFF", 1.0) == "#FFFFFF"
    mid = video_styles.blend_hex("#000000", "#FFFFFF", 0.5)
    assert _HEX.match(mid)
