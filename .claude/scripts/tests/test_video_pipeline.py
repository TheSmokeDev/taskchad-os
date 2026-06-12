"""Unit tests for the brief-to-MP4 video pipeline pure logic (video_pipeline).

No network, no render, no LLM, no TTS. Covers:
  1. allocate_scene_frames() math: floor, pad, scale-to-exact-total
  2. the claim-safety gate: supplied passes, invented metric and
     superlatives rejected
  3. beats parsing: JSON block, numbered lines, deterministic 2-beat fallback
  4. compose_html(): pre-hide set calls, relative served-asset refs,
     window.__timelines registration, design-token-driven CSS
  5. check_dependencies(): list contract under mocked tool resolution

Plus a born-clean regression: the four shipped files must not contain any
private/house token (the pipeline is a public, model-agnostic module).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import video_pipeline  # noqa: E402
import video_styles  # noqa: E402
from video_pipeline import (  # noqa: E402
    Beat,
    allocate_scene_frames,
    check_claims,
    check_dependencies,
    coerce_beats,
    compose_html,
    fallback_beats,
    parse_beats,
)

BRIEF = "The pipeline turns one brief into a finished video. It ships with nine visual styles."
CLAIMS = "Search latency dropped from 90ms to 50ms. 12 tests cover the gate."


# =============================================================================
# 1. ALLOCATE_SCENE_FRAMES MATH
# =============================================================================


def test_allocate_basic_ceil_plus_pad() -> None:
    # 2.0s at 30fps = 60 frames + 8 pad = 68.
    assert allocate_scene_frames([2.0], fps=30, min_frames=54, pad_frames=8) == [68]


def test_allocate_floor_applies_to_short_beats() -> None:
    # 0.5s -> 15 + 8 = 23, floored at 54.
    assert allocate_scene_frames([0.5], fps=30, min_frames=54, pad_frames=8) == [54]


def test_allocate_zero_duration_gets_floor() -> None:
    frames = allocate_scene_frames([0.0, 0.0], fps=30)
    assert frames == [video_pipeline.MIN_SCENE_FRAMES] * 2


def test_allocate_empty_returns_empty() -> None:
    assert allocate_scene_frames([]) == []


def test_allocate_scale_to_total_exact_sum() -> None:
    # Natural: [68, 98, 54] = 220. Cap at 180 -> the sum is EXACTLY 180.
    frames = allocate_scene_frames([2.0, 3.0, 0.5], fps=30, total_frames=180)
    assert sum(frames) == 180
    assert all(f >= 54 for f in frames)


def test_allocate_scale_up_to_total_exact_sum() -> None:
    frames = allocate_scene_frames([2.0, 2.0], fps=30, total_frames=300)
    assert sum(frames) == 300


def test_allocate_no_total_returns_natural() -> None:
    frames = allocate_scene_frames([2.0, 3.0], fps=30, min_frames=54, pad_frames=8)
    assert frames == [68, 98]


# =============================================================================
# 2. CLAIM-SAFETY GATE
# =============================================================================


def test_claims_supplied_metric_passes() -> None:
    check = check_claims("Latency dropped to 50ms, covered by 12 tests", BRIEF, CLAIMS)
    assert check.ok, check.detail


def test_claims_invented_metric_rejected() -> None:
    check = check_claims("Now 10x faster with 99% uptime", BRIEF, CLAIMS)
    assert not check.ok
    assert any("10x" in r for r in check.rejections)
    assert any("99%" in r for r in check.rejections)


def test_claims_superlatives_always_rejected() -> None:
    # Even when the word appears in the source, superlatives never ship.
    check = check_claims("The best and fastest pipeline", "the best fastest pipeline")
    assert not check.ok
    assert any("best" in r for r in check.rejections)
    assert any("fastest" in r for r in check.rejections)


def test_claims_invented_price_rejected() -> None:
    check = check_claims("Only $29 per month", BRIEF, CLAIMS)
    assert not check.ok


def test_claims_clean_copy_passes() -> None:
    check = check_claims("One brief in, one finished video out", BRIEF, CLAIMS)
    assert check.ok


# =============================================================================
# 3. BEATS PARSING + DETERMINISTIC FALLBACK
# =============================================================================

GOOD_JSON_OUTPUT = """Here you go.

```json
[
  {"eyebrow": "NEW", "headline": "One brief in", "subhead": "A finished video out", "voice": "One brief in, one finished video out.", "cta": ""},
  {"eyebrow": "STYLES", "headline": "Nine visual styles", "subhead": "Pick one per render", "voice": "Nine visual styles ship in the registry.", "cta": ""}
]
```
"""

NUMBERED_OUTPUT = """BEATS:
1. NEW | One brief in | A finished video out | One brief in, one finished video out.
2. STYLES | Nine visual styles | Pick one per render | Nine visual styles ship in the registry.
"""


def test_parse_beats_json_block() -> None:
    beats = parse_beats(GOOD_JSON_OUTPUT)
    assert beats is not None and len(beats) == 2
    assert beats[0].headline == "One brief in"
    assert beats[1].eyebrow == "STYLES"
    assert beats[1].voice_text.startswith("Nine visual styles")


def test_parse_beats_numbered_lines() -> None:
    beats = parse_beats(NUMBERED_OUTPUT)
    assert beats is not None and len(beats) == 2
    assert beats[0].eyebrow == "NEW"
    assert beats[0].subhead == "A finished video out"


def test_parse_beats_malformed_returns_none() -> None:
    assert parse_beats("I could not produce the beats, sorry about that.") is None
    assert parse_beats("") is None
    assert parse_beats("```json\n{not valid json}\n```") is None


def test_coerce_beats_falls_back_to_two_deterministic_beats() -> None:
    beats, used_fallback = coerce_beats("total garbage output", BRIEF)
    assert used_fallback
    assert len(beats) == 2
    # Fallback copy is drawn from the brief itself (claim-safe by construction).
    assert "finished video" in beats[0].headline or "finished video" in beats[0].voice_text
    joined = " ".join(b.render_text() + " " + b.voice_text for b in beats)
    assert check_claims(joined, BRIEF, "").ok


def test_coerce_beats_uses_parsed_when_valid() -> None:
    beats, used_fallback = coerce_beats(GOOD_JSON_OUTPUT, BRIEF)
    assert not used_fallback
    assert len(beats) == 2


def test_fallback_beats_strip_em_dashes() -> None:
    beats = fallback_beats("Fast feedback \u2014 without the wait. Ships today.")
    for beat in beats:
        assert "\u2014" not in beat.render_text()
        assert "\u2014" not in beat.voice_text


# =============================================================================
# 4. COMPOSITION HTML (pre-hide, served assets, timeline registration)
# =============================================================================


def _two_beats() -> list[Beat]:
    beats, _ = coerce_beats(GOOD_JSON_OUTPUT, BRIEF)
    for beat in beats:
        beat.scene_frames = 90
    return beats


def test_compose_html_prehide_and_timeline() -> None:
    design = video_styles.resolve_design(style="neutral")
    html = compose_html(
        _two_beats(), design, width=1920, height=1080, fps=30, total_frames=180
    )
    # Timeline registration + clip classes.
    assert 'window.__timelines["main"] = tl;' in html
    assert 'class="scene clip"' in html
    # PRE-HIDE rule: every later-revealing element is set invisible at t=0.
    assert 'tl.set("#s0-headline", { autoAlpha: 0' in html
    assert 'tl.set("#s1", { autoAlpha: 0' in html
    prehide_count = html.count("autoAlpha: 0")
    assert prehide_count >= 7  # 2 scenes x 3 elements + the s1 container
    # Every pre-hidden element gets revealed again.
    assert 'tl.to("#s0-headline", { autoAlpha: 1' in html


def test_compose_html_served_assets_are_relative() -> None:
    design = video_styles.resolve_design(style="neutral")
    html = compose_html(
        _two_beats(),
        design,
        width=1920,
        height=1080,
        fps=30,
        total_frames=180,
        audio_rel="assets/voice.mp3",
        hero_rel="assets/hero.png",
    )
    assert 'src="assets/voice.mp3"' in html
    assert "url('assets/hero.png')" in html
    assert "file://" not in html
    # No absolute drive/filesystem refs in served asset paths.
    assert not re.search(r"""(?:src|href)=["'][A-Za-z]:""", html)


def test_compose_html_consumes_design_tokens_only() -> None:
    design = video_styles.resolve_design(style="blockframe")
    html = compose_html(
        _two_beats(), design, width=1920, height=1080, fps=30, total_frames=180
    )
    palette = design["palette"]
    assert palette["bg"] in html
    assert palette["accent"] in html
    assert design["fonts"]["display"] in html
    assert design["fonts"]["google_fonts_url"] in html
    # blockframe flourishes: hard borders + offset shadow + uppercase display.
    assert "box-shadow" in html
    assert "text-transform: uppercase" in html


def test_compose_html_duration_matches_total_frames() -> None:
    design = video_styles.resolve_design(style="neutral")
    html = compose_html(
        _two_beats(), design, width=1280, height=720, fps=30, total_frames=180
    )
    assert 'data-duration="6.0"' in html  # 180 frames / 30 fps
    assert 'data-width="1280"' in html and 'data-height="720"' in html


# =============================================================================
# 5. CHECK_DEPENDENCIES
# =============================================================================


def test_check_dependencies_reports_all_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video_pipeline.shutil, "which", lambda name: None)
    monkeypatch.setattr(video_pipeline, "_edge_tts_importable", lambda: False)
    missing = check_dependencies()
    assert missing == ["node", "npx", "ffmpeg", "ffprobe", "edge_tts"]


def test_check_dependencies_empty_when_all_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_pipeline.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(video_pipeline, "_edge_tts_importable", lambda: True)
    assert check_dependencies() == []


def test_check_dependencies_returns_list_on_real_box() -> None:
    missing = check_dependencies()
    assert isinstance(missing, list)
    assert all(isinstance(name, str) for name in missing)


# =============================================================================
# 6. RENDER_BRIEF OPERATIONAL-FAILURE CONTRACT (no render invoked)
# =============================================================================


def test_render_brief_unknown_style_returns_error_not_raise() -> None:
    result = video_pipeline.render_brief("a brief", style="not-a-style")
    assert result["ok"] is False
    assert "unknown style" in result["error"]
    assert set(result.keys()) == {
        "ok",
        "mp4_path",
        "output_dir",
        "duration_s",
        "score",
        "provider",
        "style",
        "error",
    }


def test_render_brief_empty_brief_returns_error() -> None:
    result = video_pipeline.render_brief("   ", style="neutral")
    assert result["ok"] is False
    assert result["error"] == "empty brief"
    assert result["style"] == "neutral"


def test_render_brief_missing_dependencies_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_pipeline, "check_dependencies", lambda: ["ffmpeg"])
    result = video_pipeline.render_brief("a brief", style="neutral")
    assert result["ok"] is False
    assert "missing dependencies: ffmpeg" in result["error"]


# =============================================================================
# 7. BORN-CLEAN REGRESSION (public module: no private/house tokens)
# =============================================================================

_FORBIDDEN = (
    "ItsS" + "mokeDev",
    "Smoke" + "Alot420",
    "Smoke" + "Dev",
    "Dyna" + "mous",
    "HOMIE-FRAME" + "-MD",
    "x_vi" + "deo",
    "homie-ship" + "post",
    "homie-vi" + "deo",
    "C:\\" + "Users",
    "C:/" + "Users",
    "second-" + "brain",
    "De" + "gen",
    "AndrewMultilingual" + "Neural",
    "TELEGRAM_BOT" + "_TOKEN",
    "co" + "dex",
)

_SHIPPED_FILES = (
    _SCRIPTS / "video_styles.py",
    _SCRIPTS / "video_pipeline.py",
    Path(__file__),
    Path(__file__).parent / "test_video_styles.py",
)


def test_born_clean_no_forbidden_tokens_or_em_dashes() -> None:
    for path in _SHIPPED_FILES:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for token in _FORBIDDEN:
            assert token.lower() not in lowered, f"{path.name} contains {token!r}"
        assert "\u2014" not in text, f"{path.name} contains an em-dash"
