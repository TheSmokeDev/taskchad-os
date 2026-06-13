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


# =============================================================================
# 6. SUGGEST_STYLE (brief -> best style; heuristic, never raises)
# =============================================================================


@pytest.mark.parametrize(
    ("brief", "expected"),
    [
        ("Championship match highlights, the team lifted the cup", "bold-poster"),
        ("Mexico just won the world cup final", "bold-poster"),
        ("Quarterly revenue review for enterprise clients", "blue-professional"),
        ("Close more deals and win new customers this quarter", "blue-professional"),
        ("Breaking news headlines from the newsroom tonight", "broadside"),
        ("A longform editorial analysis of the housing market", "cobalt-grid"),
        ("A calm forest documentary on sustainable farming", "editorial-forest"),
        ("A playful birthday surprise reel for the kids", "capsule"),
        ("High energy festival launch party announcement", "coral"),
        ("Developer tools for terminal coding workflows", "blockframe"),
        ("A quiet morning routine, nothing else", "neutral"),
        ("", "neutral"),
    ],
)
def test_suggest_style_keyword_heuristics(brief: str, expected: str) -> None:
    assert video_styles.suggest_style(brief) == expected


def test_suggest_style_always_returns_valid_name() -> None:
    names = {entry["name"] for entry in list_styles()}
    for brief in ("", "zzz qqq", "a" * 5000, "1234567890"):
        assert video_styles.suggest_style(brief) in names


def test_suggest_style_never_raises_on_weird_input() -> None:
    # Non-string input is coerced, never raised on.
    names = {entry["name"] for entry in list_styles()}
    assert video_styles.suggest_style(None) == "neutral"  # type: ignore[arg-type]
    assert video_styles.suggest_style(12345) in names  # type: ignore[arg-type]


def test_suggest_style_llm_refine_is_opt_in_and_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Off by default: the lane is never consulted.
    monkeypatch.delenv("VIDEO_SUGGEST_LLM", raising=False)

    def _boom(brief: str) -> str:
        raise AssertionError("lane must not be consulted when refinement is off")

    monkeypatch.setattr(video_styles, "_suggest_via_lane", _boom)
    assert video_styles.suggest_style("world cup final") == "bold-poster"

    # Opted in: a valid lane answer wins; an empty/invalid one falls back.
    monkeypatch.setenv("VIDEO_SUGGEST_LLM", "on")
    monkeypatch.setattr(video_styles, "_suggest_via_lane", lambda brief: "capsule")
    assert video_styles.suggest_style("world cup final") == "capsule"
    monkeypatch.setattr(video_styles, "_suggest_via_lane", lambda brief: "")
    assert video_styles.suggest_style("world cup final") == "bold-poster"


# =============================================================================
# 7. DESIGN_FROM_TOKENS (scraped tokens -> complete validated design)
# =============================================================================


def test_design_from_tokens_role_mapping_and_extras() -> None:
    design = video_styles.design_from_tokens(
        "Acme Site",
        {
            "background": "#101014",
            "ink": "#FAFAF5",
            "accent": "#FF5500",
            "spark": "#22AA88",
        },
        ["Space Grotesk", "Inter"],
        tagline="from the site",
    )
    assert design["name"] == "acme-site"
    assert design["tagline"] == "from the site"
    assert design["palette"]["bg"] == "#101014"
    assert design["palette"]["fg"] == "#FAFAF5"
    assert design["palette"]["accent"] == "#FF5500"
    # The unmapped color lands in extras, untouched.
    assert design["extras"].get("spark") == "#22AA88"


def test_design_from_tokens_derives_dim_and_drops_invalid_hex() -> None:
    design = video_styles.design_from_tokens(
        "x", {"bg": "#000000", "fg": "#FFFFFF", "accent": "#FF0000"}, []
    )
    # accent_dim was not supplied: derived blend, still valid hex.
    assert _HEX.match(design["palette"]["accent_dim"])
    junk = video_styles.design_from_tokens(
        "x", {"bg": "not-a-color", "fg": "#FFFFFF", "accent": "rgb(1,2,3)"}, []
    )
    # Invalid tokens are dropped; every role still resolves to valid hex.
    for role in ("bg", "fg", "accent", "accent_dim"):
        assert _HEX.match(junk["palette"][role]), role


def test_design_from_tokens_fonts_mapping_and_neutral_fallbacks() -> None:
    design = video_styles.design_from_tokens(
        "x", {"bg": "#101014", "fg": "#FAFAF5"}, ["Playfair Display", "Inter"]
    )
    assert design["fonts"]["display"] == "Playfair Display"
    assert design["fonts"]["body"] == "Inter"
    assert design["fonts"]["mono"]  # neutral mono fallback (no mono family given)
    assert "Playfair+Display" in design["fonts"]["google_fonts_url"]

    mono = video_styles.design_from_tokens(
        "x", {"bg": "#101014", "fg": "#FAFAF5"}, ["IBM Plex Mono"]
    )
    assert mono["fonts"]["mono"] == "IBM Plex Mono"

    bare = video_styles.design_from_tokens("x", {"bg": "#101014", "fg": "#FAFAF5"}, [])
    for key in ("display", "body", "mono"):
        assert isinstance(bare["fonts"][key], str) and bare["fonts"][key]
    assert bare["fonts"]["google_fonts_url"].startswith(
        "https://fonts.googleapis.com/css2?"
    )


def test_design_from_tokens_returns_complete_dict() -> None:
    design = video_styles.design_from_tokens(
        "brand", {"bg": "#0B0B10", "text": "#F2F2F2", "primary": "#3355FF"}, ["Inter"]
    )
    for section in ("palette", "extras", "fonts", "motion", "flags"):
        assert isinstance(design.get(section), dict), section
    for role in ("bg", "fg", "accent", "accent_dim"):
        assert _HEX.match(design["palette"][role]), role
    assert design["palette"]["accent"] == "#3355FF"  # "primary" maps onto accent
    assert design["motion"]["transition"] in {"crossfade", "cut", "slide"}
    assert design["name"] == "brand"
    empty = video_styles.design_from_tokens("", {}, [])
    assert empty["name"] == "derived"
    for role in ("bg", "fg", "accent", "accent_dim"):
        assert _HEX.match(empty["palette"][role])


# =============================================================================
# 8. SUGGEST_STYLES_RANKED (+ suggest_style back-compat)
# =============================================================================


def _dark_dossier() -> dict:
    return {
        "derived_design": {
            "palette": {
                "bg": "#111111",
                "fg": "#F0ECE5",
                "accent": "#555555",
                "accent_dim": "#222222",
            },
            "fonts": {"display": "Inter", "body": "Inter", "mono": "Inter"},
        }
    }


def test_suggest_styles_ranked_full_permutation_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_SUGGEST_LLM", raising=False)
    names = {entry["name"] for entry in list_styles()}
    ranked = video_styles.suggest_styles_ranked("world cup final highlights")
    assert set(ranked) == names and len(ranked) == len(names)
    assert ranked[0] == "bold-poster"
    assert ranked == video_styles.suggest_styles_ranked("world cup final highlights")


def test_suggest_styles_ranked_no_signal_is_registry_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_SUGGEST_LLM", raising=False)
    ranked = video_styles.suggest_styles_ranked("a quiet morning routine, nothing else")
    assert ranked == [entry["name"] for entry in list_styles()]


def test_suggest_styles_ranked_dossier_dark_palette_boost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_SUGGEST_LLM", raising=False)
    brief = "a quiet morning routine, nothing else"
    base = video_styles.suggest_styles_ranked(brief)
    boosted = video_styles.suggest_styles_ranked(brief, _dark_dossier())
    assert boosted[:2] == ["blockframe", "cobalt-grid"]
    assert boosted != base
    assert set(boosted) == set(base)  # still a full permutation


def test_suggest_styles_ranked_keyword_outranks_dossier_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_SUGGEST_LLM", raising=False)
    ranked = video_styles.suggest_styles_ranked(
        "world cup final highlights", _dark_dossier()
    )
    assert ranked[0] == "bold-poster"  # +3 keyword beats the +2 dossier boost


def test_suggest_styles_ranked_kind_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_SUGGEST_LLM", raising=False)
    brief = "a quiet morning routine, nothing else"
    ranked = video_styles.suggest_styles_ranked(brief, kind="hype")
    assert ranked[:2] == ["bold-poster", "broadside"]  # registry-order tie-break
    explainer = video_styles.suggest_styles_ranked(brief, kind="explainer")
    assert explainer[:2] == ["neutral", "blue-professional"]
    assert video_styles.suggest_styles_ranked(brief, kind="unknown") == [
        entry["name"] for entry in list_styles()
    ]


def test_suggest_styles_ranked_lane_refine_promotes_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_SUGGEST_LLM", "on")
    monkeypatch.setattr(video_styles, "_suggest_via_lane", lambda brief: "capsule")
    ranked = video_styles.suggest_styles_ranked("world cup final")
    assert ranked[0] == "capsule"
    assert set(ranked) == {entry["name"] for entry in list_styles()}


def test_suggest_styles_ranked_never_raises_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_SUGGEST_LLM", raising=False)
    names = {entry["name"] for entry in list_styles()}
    assert set(video_styles.suggest_styles_ranked(None)) == names  # type: ignore[arg-type]
    assert set(video_styles.suggest_styles_ranked("x", {"derived_design": "junk"})) == names
    assert set(
        video_styles.suggest_styles_ranked("x", {"derived_design": {"palette": "no"}})
    ) == names


def test_suggest_style_one_arg_back_compat_equals_ranked_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_SUGGEST_LLM", raising=False)
    for brief in (
        "world cup final",
        "quarterly revenue review for enterprise clients",
        "a quiet morning routine, nothing else",
    ):
        assert video_styles.suggest_style(brief) == video_styles.suggest_styles_ranked(brief)[0]
    # Two-arg form with a dossier follows the dossier-aware ranking.
    assert (
        video_styles.suggest_style("a quiet morning routine, nothing else", _dark_dossier())
        == "blockframe"
    )
