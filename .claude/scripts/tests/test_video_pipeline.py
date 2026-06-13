"""Unit tests for the brief-to-MP4 video pipeline pure logic (video_pipeline).

No network, no render, no LLM, no TTS. Covers:
  1. allocate_scene_frames() math: floor, pad, scale-to-exact-total
  2. the claim-safety gate: supplied passes, invented metric and
     superlatives rejected
  3. the copy-leakage gate: the exact FIFA director's-notes strings from the
     2026-06-11 operator run are fixtures; retry flow; fallback drop
  4. the deterministic fallback composer: viewer-facing subject lines, never
     a verbatim echo of the raw brief
  5. brief intent: heuristic extraction, lane-response parsing, precedence
  6. voice spec parsing + precedence (param > env > Andrew default)
  7. compose_html(): pre-hide set calls, relative served-asset refs,
     window.__timelines, opening-beat art layer, design-token-driven CSS,
     plus the archetype dispatch surface: stat scenes, per-beat art_map,
     karaoke captions on/off, transition selection, texture flags
  8. check_dependencies(): list contract under mocked tool resolution
  Plus duration fill: beat-count targeting in _beats_prompt, the refill
  retry, fill_scene_frames bounded stretch, parser kind/energy/stat/items
  with the 16-beat cap, and the captions switch resolution

Plus a born-clean regression: the shipped files must not contain any
private/house token. The image-adapter provider name is allowed ONLY in
video_imagegen.py (it is a documented optional public adapter); it stays
forbidden everywhere else.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

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
    find_copy_leakage,
    parse_beats,
)

BRIEF = "The pipeline turns one brief into a finished video. It ships with nine visual styles."
CLAIMS = "Search latency dropped from 90ms to 50ms. 12 tests cover the gate."

_EM = chr(0x2014)  # em-dash, built via chr() so this file stays born-clean


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
# 1b. FILL_SCENE_FRAMES (bounded stretch toward a STATED duration)
# =============================================================================


def test_fill_frames_bounded_stretch_caps_at_118_percent() -> None:
    # Natural pace ~34s against a stated 60s target: stretch stops at the
    # FILL_STRETCH_CAP and the shortfall is recorded.
    durations = [11.0, 11.0, 11.0]
    natural_total = sum(allocate_scene_frames(durations))
    notes: list[str] = []
    frames = video_pipeline.fill_scene_frames(
        durations, 60, duration_stated=True, notes=notes
    )
    assert sum(frames) == int(natural_total * video_pipeline.FILL_STRETCH_CAP)
    assert sum(frames) < 60 * video_pipeline.FPS
    assert any("shortfall" in n for n in notes)


def test_fill_frames_stretch_reaches_target_when_close() -> None:
    # Natural pace ~56.5s against 60s: the cap allows reaching the target
    # exactly, with no shortfall note.
    durations = [28.0, 28.0]
    notes: list[str] = []
    frames = video_pipeline.fill_scene_frames(
        durations, 60, duration_stated=True, notes=notes
    )
    assert sum(frames) == 60 * video_pipeline.FPS
    assert notes == []


def test_fill_frames_no_stretch_when_duration_not_stated() -> None:
    durations = [11.0, 11.0, 11.0]
    frames = video_pipeline.fill_scene_frames(durations, 60, duration_stated=False)
    assert frames == allocate_scene_frames(durations)


def test_fill_frames_scale_down_branch_preserved() -> None:
    # Long voiceovers still scale DOWN to the target exactly.
    durations = [30.0, 30.0, 30.0]
    frames = video_pipeline.fill_scene_frames(durations, 60, duration_stated=True)
    assert sum(frames) == 60 * video_pipeline.FPS


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
# 3. COPY-LEAKAGE GATE (FIFA evidence fixtures: operator run 2026-06-11)
# =============================================================================

FIFA_BRIEF = "make a video of FIFA. Mexico just won. let's do two minutes."

# Exact strings the lane shipped in the cobalt-grid FIFA render. The voices
# were director's notes; the subheads were design commentary. All must flag.
FIFA_LEAKY_FIXTURES = [
    (
        "A cobalt-grid opener frames the win with editorial tension and match-day energy.",
        "Mexico just won, and the FIFA spotlight shifts straight into celebration.",
    ),
    (
        "Parchment texture meets sharp grid lines as the result becomes the central visual beat.",
        "The screen tracks the rush from final whistle to national reaction.",
    ),
    (
        "Newsreader type carries the emotion without overexplaining what the brief already makes clear.",
        "Keep the visuals focused on Mexico, the win, and the FIFA frame.",
    ),
    (
        "The close turns the celebration into a clean, graphic finish with confident movement.",
        "End on the energy of the win, then let the moment breathe.",
    ),
]


def _fifa_leaky_beats() -> list[Beat]:
    return [
        Beat(eyebrow="FIFA", headline=f"Beat {i}", subhead=sub, voice_text=voice)
        for i, (sub, voice) in enumerate(FIFA_LEAKY_FIXTURES)
    ]


def test_leakage_flags_fifa_directors_notes() -> None:
    design = video_styles.resolve_design(style="cobalt-grid")
    leaks = find_copy_leakage(_fifa_leaky_beats(), design, FIFA_BRIEF)
    joined = " | ".join(leaks).lower()
    # Director's notes in the narration.
    assert "the screen" in joined
    assert "keep the" in joined
    assert "the visuals" in joined
    assert "end on" in joined
    # Design commentary in the subheads.
    assert "parchment" in joined
    assert "grid lines" in joined
    assert "the close" in joined
    assert "the brief" in joined
    # Style-vocabulary words (cobalt-grid name/tagline) flag too.
    assert "cobalt" in joined or "newsreader" in joined


def test_leakage_clean_viewer_copy_passes() -> None:
    design = video_styles.resolve_design(style="cobalt-grid")
    beats = [
        Beat(
            eyebrow="CHAMPIONS",
            headline="Mexico take the title",
            subhead="A first world title, sealed in the capital",
            voice_text="Mexico are world champions, and the country erupted.",
        ),
        Beat(
            eyebrow="THE RUN",
            headline="Unbeaten to the end",
            subhead="From the group stage to the final whistle",
            voice_text="They carried an unbeaten run all the way through the final.",
        ),
    ]
    assert find_copy_leakage(beats, design, FIFA_BRIEF) == []


def test_leakage_style_word_excused_when_topical() -> None:
    design = video_styles.resolve_design(style="cobalt-grid")
    beats = [
        Beat(
            eyebrow="LIVE",
            headline="The call heard nationwide",
            subhead="The newsreader broke the result on air",
            voice_text="A newsreader announced the result to the nation.",
        )
    ]
    # Brief does NOT mention the word: it is style vocabulary -> leak.
    assert find_copy_leakage(beats, design, "Mexico won the cup")
    # Brief DOES mention it: topical content -> clean.
    assert (
        find_copy_leakage(
            beats, design, "Cover how the newsreader broke the news that Mexico won"
        )
        == []
    )


def test_generate_beats_retry_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    design = video_styles.resolve_design(style="cobalt-grid")
    leaky = json.dumps(
        {
            "duration_s": 120,
            "aspect": None,
            "beats": [
                {
                    "eyebrow": "FIFA",
                    "headline": "Mexico takes the moment",
                    "subhead": "Parchment texture meets sharp grid lines",
                    "voice": "The screen tracks the rush from final whistle to reaction.",
                    "cta": "",
                }
            ],
        }
    )
    clean = json.dumps(
        {
            "duration_s": 120,
            "aspect": None,
            "beats": [
                {
                    "eyebrow": "CHAMPIONS",
                    "headline": "Mexico take the title",
                    "subhead": "A world title sealed at home",
                    "voice": "Mexico are world champions.",
                    "cta": "",
                },
                {
                    "eyebrow": "THE RUN",
                    "headline": "Unbeaten to the end",
                    "subhead": "Group stage to final, no defeats",
                    "voice": "They went from the group stage to the final without a defeat.",
                    "cta": "",
                },
            ],
        }
    )
    calls: list[str] = []

    def fake_lane(prompt: str, task_name: str = "") -> tuple[str, str]:
        calls.append(task_name)
        return (leaky if len(calls) == 1 else clean), "lane:test-model"

    monkeypatch.setattr(video_pipeline, "_run_lane", fake_lane)
    beats, provider, notes, intent = video_pipeline.generate_beats(
        FIFA_BRIEF, "", design, 30
    )

    assert calls == ["video_brief_beats", "video_brief_beats_retry"]
    assert provider == "lane:test-model"
    assert find_copy_leakage(beats, design, FIFA_BRIEF) == []
    assert intent["duration_s"] == 120  # lane-extracted intent survives
    assert any("leakage rejected" in n for n in notes)


def test_generate_beats_still_dirty_drops_to_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design = video_styles.resolve_design(style="cobalt-grid")
    leaky = json.dumps(
        {
            "beats": [
                {
                    "eyebrow": "FIFA",
                    "headline": "Mexico takes it",
                    "subhead": "The close turns the celebration into a graphic finish",
                    "voice": "Keep the visuals focused on Mexico and the win.",
                }
            ]
        }
    )

    def fake_lane(prompt: str, task_name: str = "") -> tuple[str, str]:
        return leaky, "lane:test-model"

    monkeypatch.setattr(video_pipeline, "_run_lane", fake_lane)
    beats, provider, notes, _intent = video_pipeline.generate_beats(
        FIFA_BRIEF, "", design, 30
    )

    assert provider == "fallback"
    assert any("retry still leaking" in n for n in notes)
    # The fallback itself is leak-free and viewer-facing.
    assert find_copy_leakage(beats, design, FIFA_BRIEF) == []


def test_generate_beats_lane_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    design = video_styles.resolve_design(style="neutral")

    def boom(prompt: str, task_name: str = "") -> tuple[str, str]:
        raise RuntimeError("no lanes configured")

    monkeypatch.setattr(video_pipeline, "_run_lane", boom)
    beats, provider, notes, intent = video_pipeline.generate_beats(BRIEF, "", design, 30)

    assert provider == "fallback"
    assert len(beats) == 2
    assert any("lane unavailable" in n for n in notes)
    assert intent == {"duration_s": None, "aspect": None}


# =============================================================================
# 3b. DURATION FILL: BEATS PROMPT TARGETS + REFILL RETRY
# =============================================================================


def _clean_beat_obj(i: int) -> dict:
    return {
        "kind": "caption",
        "energy": "medium",
        "eyebrow": "PART",
        "headline": f"Chapter {i} of the story",
        "subhead": "",
        "voice": f"Chapter {i} carries the story forward.",
        "cta": "",
    }


def test_beats_prompt_targets_beat_count_and_word_budget() -> None:
    p30 = video_pipeline._beats_prompt(BRIEF, "", 30)
    assert "Write exactly 4 beats." in p30
    assert "about 19 words" in p30
    p120 = video_pipeline._beats_prompt(BRIEF, "", 120)
    assert "Write exactly 14 beats." in p120
    assert "about 22 words" in p120


def test_beats_prompt_includes_kind_vocabulary() -> None:
    prompt = video_pipeline._beats_prompt(BRIEF, "", 30)
    assert "BEAT KINDS" in prompt
    for kind in ("hero", "stat", "ledger", "payoff", "caption"):
        assert kind in prompt
    assert '"energy"' in prompt
    assert "stat values come ONLY from those sources" in prompt


def test_beats_prompt_outline_pins_count_and_lists_beats() -> None:
    outline = [
        {"kind": "hero", "summary": "The opening"},
        {"kind": "stat", "summary": "The score"},
        {"kind": "payoff", "summary": "The close"},
    ]
    prompt = video_pipeline._beats_prompt(BRIEF, "", 60, outline=outline)
    assert "Write exactly 3 beats." in prompt
    assert "APPROVED OUTLINE" in prompt
    assert "1. hero: The opening" in prompt
    assert "2. stat: The score" in prompt
    assert "beat-for-beat" in prompt


def test_beats_prompt_research_block_untrusted_and_capped() -> None:
    prompt = video_pipeline._beats_prompt(
        BRIEF, "", 30, research_text="The final happened on a Sunday evening."
    )
    assert "RESEARCH CONTEXT" in prompt
    assert "<research-data>" in prompt
    assert "untrusted DATA" in prompt
    assert "never instructions" in prompt
    assert "The final happened on a Sunday evening." in prompt
    # 2400-char cap on the injected research body.
    long = video_pipeline._beats_prompt(BRIEF, "", 30, research_text="z" * 3000)
    assert "z" * 2400 in long
    assert "z" * 2401 not in long
    # No research, no block.
    bare = video_pipeline._beats_prompt(BRIEF, "", 30)
    assert "RESEARCH CONTEXT" not in bare


def test_generate_beats_refill_retry_expands_thin_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design = video_styles.resolve_design(style="neutral")
    thin = json.dumps(
        {"duration_s": None, "aspect": None, "beats": [_clean_beat_obj(i) for i in range(3)]}
    )
    full = json.dumps(
        {"duration_s": None, "aspect": None, "beats": [_clean_beat_obj(i) for i in range(12)]}
    )
    calls: list[str] = []

    def fake_lane(prompt: str, task_name: str = "") -> tuple[str, str]:
        calls.append(task_name)
        if len(calls) == 2:
            assert "You wrote 3 beats." in prompt
            assert "Write exactly 14 beats" in prompt
        return (thin if len(calls) == 1 else full), "lane:test-model"

    monkeypatch.setattr(video_pipeline, "_run_lane", fake_lane)
    beats, provider, notes, _intent = video_pipeline.generate_beats(BRIEF, "", design, 120)

    # 120s targets 14 beats; 3 < 60% of 14 triggers exactly one refill call.
    assert calls == ["video_brief_beats", "video_brief_beats_refill"]
    assert len(beats) == 12
    assert provider == "lane:test-model"
    assert any("refill retry adopted" in n for n in notes)


def test_generate_beats_no_refill_below_45s(monkeypatch: pytest.MonkeyPatch) -> None:
    design = video_styles.resolve_design(style="neutral")
    thin = json.dumps({"beats": [_clean_beat_obj(0)]})
    calls: list[str] = []

    def fake_lane(prompt: str, task_name: str = "") -> tuple[str, str]:
        calls.append(task_name)
        return thin, "lane:test-model"

    monkeypatch.setattr(video_pipeline, "_run_lane", fake_lane)
    beats, provider, _notes, _intent = video_pipeline.generate_beats(BRIEF, "", design, 30)

    # 1 beat is under the 60% threshold, but short videos never refill.
    assert calls == ["video_brief_beats"]
    assert len(beats) == 1
    assert provider == "lane:test-model"


def test_generate_beats_refill_failure_keeps_first_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design = video_styles.resolve_design(style="neutral")
    thin = json.dumps({"beats": [_clean_beat_obj(i) for i in range(3)]})
    calls: list[str] = []

    def fake_lane(prompt: str, task_name: str = "") -> tuple[str, str]:
        calls.append(task_name)
        return (thin if len(calls) == 1 else "no json this time"), "lane:test-model"

    monkeypatch.setattr(video_pipeline, "_run_lane", fake_lane)
    beats, provider, notes, _intent = video_pipeline.generate_beats(BRIEF, "", design, 120)

    assert calls == ["video_brief_beats", "video_brief_beats_refill"]
    assert len(beats) == 3  # the thin-but-clean first parse still ships
    assert provider == "lane:test-model"
    assert any("did not improve" in n for n in notes)


def test_check_claims_research_text_allowlists_metrics() -> None:
    research = "The team finished the season with 58% possession on average."
    assert not check_claims("They averaged 58% possession", BRIEF, "").ok
    assert check_claims("They averaged 58% possession", BRIEF, "", research).ok


# =============================================================================
# 4. DETERMINISTIC FALLBACK COMPOSER (viewer-facing, no brief echo)
# =============================================================================


def test_fallback_never_echoes_directive_brief() -> None:
    brief = "Make a two minute video about Mexico winning the FIFA World Cup, vertical"
    beats = fallback_beats(brief)
    assert len(beats) == 2

    joined_voice = " ".join(b.voice_text for b in beats).lower()
    # The directive shell never reaches the narration.
    assert "make a" not in joined_voice
    assert "video about" not in joined_voice
    assert brief.lower() not in joined_voice
    # Duration/orientation directives are stripped from the subject.
    assert "two minute" not in joined_voice
    assert "vertical" not in joined_voice
    # The narration is about the subject.
    assert "mexico" in joined_voice

    design = video_styles.resolve_design(style="neutral")
    assert find_copy_leakage(beats, design, brief) == []
    joined_all = " ".join(b.render_text() + " " + b.voice_text for b in beats)
    assert check_claims(joined_all, brief, "").ok


def test_fallback_content_brief_speaks_content_not_raw_echo() -> None:
    beats = fallback_beats(BRIEF)
    assert len(beats) == 2
    normalized = re.sub(r"\s+", " ", BRIEF.strip())
    for beat in beats:
        assert beat.voice_text != normalized  # never the raw brief verbatim
    assert "finished video" in beats[0].headline or "finished video" in beats[0].voice_text


def test_fallback_single_sentence_brief_gets_prefixed_voice() -> None:
    brief = "Mexico won the world cup at home."
    beats = fallback_beats(brief)
    normalized = re.sub(r"\s+", " ", brief.strip())
    assert beats[0].voice_text != normalized
    assert "mexico" in beats[0].voice_text.lower()


def test_coerce_beats_falls_back_to_two_deterministic_beats() -> None:
    beats, used_fallback = coerce_beats("total garbage output", BRIEF)
    assert used_fallback
    assert len(beats) == 2
    joined = " ".join(b.render_text() + " " + b.voice_text for b in beats)
    assert check_claims(joined, BRIEF, "").ok


def test_coerce_beats_uses_parsed_when_valid() -> None:
    beats, used_fallback = coerce_beats(GOOD_JSON_OUTPUT, BRIEF)
    assert not used_fallback
    assert len(beats) == 2


def test_fallback_beats_strip_em_dashes() -> None:
    beats = fallback_beats(f"Fast feedback {_EM} without the wait. Ships today.")
    for beat in beats:
        assert _EM not in beat.render_text()
        assert _EM not in beat.voice_text


def test_fallback_beats_kinds_hero_then_caption() -> None:
    # Both fallback code paths (phrase mode and content mode) declare kinds.
    for brief in ("Make a video about Mexico winning the cup", BRIEF):
        beats = fallback_beats(brief)
        assert beats[0].kind == "hero"
        assert beats[1].kind == "caption"


# =============================================================================
# 5. PARSING + BRIEF INTENT
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

OBJECT_OUTPUT = """Sure.

```json
{"duration_s": 120, "aspect": "9:16", "beats": [
  {"eyebrow": "GO", "headline": "A headline", "subhead": "Context line", "voice": "A spoken line.", "cta": ""},
  {"eyebrow": "TWO", "headline": "Another headline", "subhead": "More context", "voice": "Another spoken line.", "cta": ""}
]}
```
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


def test_parse_response_object_with_intent() -> None:
    beats, intent = video_pipeline.parse_response(OBJECT_OUTPUT)
    assert beats is not None and len(beats) == 2
    assert intent == {"duration_s": 120, "aspect": "9:16"}


def test_parse_response_invalid_intent_values() -> None:
    text = (
        '{"duration_s": 999, "aspect": "4:3", "beats": '
        '[{"eyebrow": "A", "headline": "H", "subhead": "S", "voice": "V."}]}'
    )
    beats, intent = video_pipeline.parse_response(text)
    assert beats is not None
    assert intent["duration_s"] == 120  # capped at MAX_BRIEF_DURATION_S
    assert intent["aspect"] is None  # unknown canvas ignored


def test_parse_beats_kind_energy_stat_items() -> None:
    text = json.dumps(
        {
            "beats": [
                {
                    "kind": "stat",
                    "energy": "high",
                    "eyebrow": "SCORE",
                    "headline": "The final score",
                    "subhead": "",
                    "voice": "The final score told the story.",
                    "stat": {"value": "2-0", "label": "final score"},
                    "items": [
                        {"title": "First half", "detail": "An early opener"},
                        {"title": "Second half", "detail": "Sealed late"},
                    ],
                }
            ]
        }
    )
    beats = parse_beats(text)
    assert beats is not None
    assert beats[0].kind == "stat"
    assert beats[0].energy == "high"
    assert beats[0].stat == {"value": "2-0", "label": "final score"}
    assert beats[0].items == [
        {"title": "First half", "detail": "An early opener"},
        {"title": "Second half", "detail": "Sealed late"},
    ]


def test_parse_beats_invalid_kind_and_energy_default() -> None:
    text = json.dumps(
        {"beats": [{"kind": "explosion", "energy": "extreme", "headline": "H", "voice": "V."}]}
    )
    beats = parse_beats(text)
    assert beats is not None
    assert beats[0].kind == "caption"
    assert beats[0].energy == "medium"


def test_parse_beats_missing_new_fields_default_clean() -> None:
    beats = parse_beats(GOOD_JSON_OUTPUT)
    assert beats is not None
    assert beats[0].kind == "caption"
    assert beats[0].energy == "medium"
    assert beats[0].stat == {}
    assert beats[0].items == []


def test_parse_beats_stat_and_items_clamped() -> None:
    text = json.dumps(
        {
            "beats": [
                {
                    "headline": "H",
                    "voice": "V.",
                    "stat": {"value": "x" * 40, "label": "y" * 60},
                    "items": [
                        {"title": "t" * 50, "detail": "d" * 100},
                        {"title": "", "detail": ""},
                        "plain entry",
                        {"title": "a"},
                        {"title": "b"},
                        {"title": "c"},
                    ],
                }
            ]
        }
    )
    beats = parse_beats(text)
    assert beats is not None
    assert len(beats[0].stat["value"]) <= 16
    assert len(beats[0].stat["label"]) <= 28
    assert len(beats[0].items) == 4  # empties dropped, capped at 4
    assert len(beats[0].items[0]["title"]) <= 24
    assert len(beats[0].items[0]["detail"]) <= 64
    assert beats[0].items[1]["title"] == "plain entry"


def test_parse_beats_stat_without_value_dropped() -> None:
    text = json.dumps(
        {"beats": [{"headline": "H", "voice": "V.", "stat": {"label": "score only"}}]}
    )
    beats = parse_beats(text)
    assert beats is not None
    assert beats[0].stat == {}


def test_parse_beats_caps_at_sixteen() -> None:
    objs = [{"headline": f"Beat number {i}", "voice": "A spoken line."} for i in range(20)]
    beats = parse_beats(json.dumps({"beats": objs}))
    assert beats is not None
    assert len(beats) == 16


def test_parse_numbered_lines_cap_at_sixteen() -> None:
    lines = "\n".join(
        f"{i + 1}. EYE | Headline {i} | Sub {i} | Voice line {i}." for i in range(20)
    )
    beats = parse_beats(lines)
    assert beats is not None
    assert len(beats) == 16


def test_extract_intent_heuristic() -> None:
    extract = video_pipeline.extract_intent_heuristic
    assert extract("let's do two minutes")["duration_s"] == 120
    assert extract("make it three minutes")["duration_s"] == 120  # capped
    assert extract("a 45 second spot")["duration_s"] == 45
    assert extract("go vertical for shorts")["aspect"] == "9:16"
    assert extract("a square loop")["aspect"] == "1:1"
    assert extract("widescreen please")["aspect"] == "16:9"
    assert extract("just a plain brief") == {"duration_s": None, "aspect": None}


def test_resolve_render_intent_precedence() -> None:
    resolve = video_pipeline.resolve_render_intent
    extracted = {"duration_s": 120, "aspect": "9:16"}
    # Explicit kwargs win.
    assert resolve("1:1", 20, extracted) == ("1:1", 20)
    # Brief-extracted wins over defaults.
    assert resolve(None, None, extracted) == ("9:16", 120)
    # Defaults when nothing else.
    assert resolve(None, None, {}) == ("16:9", 30)
    # Invalid values are ignored at every level.
    assert resolve("4:3", None, {"aspect": "bogus", "duration_s": None}) == ("16:9", 30)


# =============================================================================
# 6. VOICE SPEC + PRECEDENCE
# =============================================================================


def test_default_voice_is_andrew_at_14() -> None:
    assert video_pipeline.DEFAULT_VOICE == "en-US-AndrewMultilingualNeural"
    assert video_pipeline.DEFAULT_VOICE_RATE == "+14%"


def test_parse_voice_spec() -> None:
    parse = video_pipeline._parse_voice_spec
    assert parse("en-US-BrianMultilingualNeural|-4%") == (
        "en-US-BrianMultilingualNeural",
        "-4%",
    )
    assert parse("en-GB-SoniaNeural") == ("en-GB-SoniaNeural", "")
    assert parse("auto") == ("", "")
    assert parse("") == ("", "")
    assert parse("SomeVoice|fast") == ("SomeVoice", "")  # malformed rate dropped


def test_resolve_voice_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_VOICE", raising=False)
    monkeypatch.delenv("VIDEO_VOICE_RATE", raising=False)
    resolve = video_pipeline._resolve_voice

    # Default chain: Andrew at +14%.
    assert resolve() == ("en-US-AndrewMultilingualNeural", "+14%")
    assert resolve("auto") == ("en-US-AndrewMultilingualNeural", "+14%")

    # Env wins over the default.
    monkeypatch.setenv("VIDEO_VOICE", "en-GB-RyanNeural|+2%")
    assert resolve() == ("en-GB-RyanNeural", "+2%")

    # Param wins over env.
    assert resolve("en-US-BrianMultilingualNeural|-4%") == (
        "en-US-BrianMultilingualNeural",
        "-4%",
    )
    # Param without a rate falls to the default rate (not the env spec rate).
    assert resolve("en-US-BrianMultilingualNeural") == (
        "en-US-BrianMultilingualNeural",
        "+14%",
    )


def test_resolve_captions_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_CAPTIONS", raising=False)
    resolve = video_pipeline._resolve_captions

    assert resolve() is True  # default on
    assert resolve("off") is False
    assert resolve("on") is True

    monkeypatch.setenv("VIDEO_CAPTIONS", "off")
    assert resolve() is False  # env wins over the default
    assert resolve("on") is True  # param wins over env

    monkeypatch.setenv("VIDEO_CAPTIONS", "definitely")
    assert resolve() is True  # unrecognized env falls through to the default


# =============================================================================
# 7. COMPOSITION HTML (pre-hide, served assets, opening-beat art)
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


def test_compose_html_opening_beat_art_only() -> None:
    design = video_styles.resolve_design(style="neutral")
    html = compose_html(
        _two_beats(),
        design,
        width=1920,
        height=1080,
        fps=30,
        total_frames=180,
        hero_rel="assets/hero.png",
    )
    # The art layer exists exactly once, inside the OPENING beat.
    assert html.count('class="opening-art"') == 1
    assert 'id="s0-art"' in html
    assert 'id="s1-art"' not in html
    assert "url('assets/hero.png')" in html
    # Without art, no opening-art layer renders at all.
    bare = compose_html(
        _two_beats(), design, width=1920, height=1080, fps=30, total_frames=180
    )
    assert "opening-art" not in bare


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
# 7b. ARCHETYPE DISPATCH, ART MAP, KARAOKE, TRANSITIONS, TEXTURE
# =============================================================================


def _three_beats() -> list[Beat]:
    beats = _two_beats() + _two_beats()[:1]
    for beat in beats:
        beat.scene_frames = 90
    return beats


def _voiced_beats() -> list[Beat]:
    beats = _two_beats()
    beats[0].voice_duration = 2.0
    beats[1].voice_duration = 2.5
    return beats


def test_compose_html_stat_archetype_renders_value() -> None:
    design = video_styles.resolve_design(style="neutral")
    beats = _two_beats()
    beats[1].kind = "stat"
    beats[1].stat = {"value": "2-0", "label": "final score"}
    scene_kinds: list[str] = []
    html = compose_html(
        beats,
        design,
        width=1920,
        height=1080,
        fps=30,
        total_frames=180,
        scene_kinds=scene_kinds,
    )
    assert 'id="s1-stat"' in html
    assert ">2-0</div>" in html
    assert "final score" in html
    assert 'id="s0-stat"' not in html
    assert scene_kinds == ["caption", "stat"]


def test_compose_html_art_map_renders_single_layer_in_target_scene() -> None:
    design = video_styles.resolve_design(style="neutral")
    html = compose_html(
        _three_beats(),
        design,
        width=1920,
        height=1080,
        fps=30,
        total_frames=270,
        art_map={2: "assets/art2.png"},
    )
    # Exactly one archetype art layer, owned by scene 2.
    assert html.count('class="va-art"') == 1
    assert 'id="s2-art"' in html
    assert 'id="s0-art"' not in html
    assert 'id="s1-art"' not in html
    assert "url('assets/art2.png')" in html
    # The art_map path never uses the legacy opening-art layer.
    assert "opening-art" not in html


def test_compose_html_captions_on_emits_karaoke_strip() -> None:
    design = video_styles.resolve_design(style="neutral")
    html = compose_html(
        _voiced_beats(),
        design,
        width=1920,
        height=1080,
        fps=30,
        total_frames=180,
        captions_on=True,
    )
    assert 'id="cap"' in html
    assert 'id="cap-b0w0"' in html
    assert 'id="cap-b1w0"' in html
    assert html.count('class="cap-page"') >= 2

    # Every word-highlight tween lands inside its owning scene's window
    # (scene 0 spans 0..3s, scene 1 spans 3..6s at 90 frames each).
    matches = list(
        re.finditer(
            r'tl\.to\("#cap-b(\d+)w\d+", \{ color: "[^"]*", '
            r"duration: [\d.]+, ease: \"none\" \}, ([\d.]+)\);",
            html,
        )
    )
    assert matches
    for match in matches:
        beat_idx, t = int(match.group(1)), float(match.group(2))
        lo, hi = (0.0, 3.0) if beat_idx == 0 else (3.0, 6.0)
        assert lo <= t <= hi, f"highlight at {t}s escaped scene {beat_idx}"


def test_compose_html_captions_off_no_cap_strip() -> None:
    design = video_styles.resolve_design(style="neutral")
    html = compose_html(
        _voiced_beats(),
        design,
        width=1920,
        height=1080,
        fps=30,
        total_frames=180,
        captions_on=False,
    )
    assert 'id="cap"' not in html
    assert "cap-b0w0" not in html


def test_compose_html_captions_skip_unvoiced_beats() -> None:
    design = video_styles.resolve_design(style="neutral")
    beats = _two_beats()
    beats[0].voice_duration = 2.0  # beat 1 stays unmeasured
    html = compose_html(
        beats, design, width=1920, height=1080, fps=30, total_frames=180,
        captions_on=True,
    )
    assert 'id="cap-b0w0"' in html
    assert "cap-b1w" not in html


def test_compose_html_transition_respects_design_motion() -> None:
    # coral's motion default is slide; caption scenes carry pref "auto".
    coral = video_styles.resolve_design(style="coral")
    html = compose_html(
        _two_beats(), coral, width=1920, height=1080, fps=30, total_frames=180
    )
    assert 'tl.set("#s1", { autoAlpha: 0, x: 60 }, 0);' in html
    assert "x: -60" in html

    # A stat scene's whip preference overrides the design default.
    neutral = video_styles.resolve_design(style="neutral")
    beats = _two_beats()
    beats[1].kind = "stat"
    beats[1].stat = {"value": "2-0", "label": "score"}
    html2 = compose_html(
        beats, neutral, width=1920, height=1080, fps=30, total_frames=180
    )
    assert "xPercent: 100" in html2


def test_compose_html_texture_flags_and_blackout_plate() -> None:
    broadside = video_styles.resolve_design(style="broadside")
    html = compose_html(
        _two_beats(), broadside, width=1920, height=1080, fps=30, total_frames=180
    )
    assert 'id="tex-grain"' in html
    assert 'id="tex-vignette"' in html

    blockframe = video_styles.resolve_design(style="blockframe")
    html2 = compose_html(
        _two_beats(), blockframe, width=1920, height=1080, fps=30, total_frames=180
    )
    assert "tex-grain" not in html2
    assert "tex-vignette" not in html2

    # The blackout plate and its closing tween ship on EVERY style.
    for doc in (html, html2):
        assert 'id="blackout"' in doc
        assert 'tl.to("#blackout"' in doc


# =============================================================================
# 8. CHECK_DEPENDENCIES
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
# 9. RENDER_BRIEF OPERATIONAL-FAILURE CONTRACT (no render invoked)
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


def test_render_brief_style_auto_resolves_via_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deps short-circuit AFTER style resolution, so no render is attempted.
    monkeypatch.setattr(video_pipeline, "check_dependencies", lambda: ["ffmpeg"])
    result = video_pipeline.render_brief(
        "Championship match highlights from the world cup final", style="auto"
    )
    assert result["style"] == "bold-poster"
    assert result["ok"] is False


# =============================================================================
# 9b. RENDER_BRIEF RESEARCH THREADING (render/voice/lane seams all stubbed)
# =============================================================================


def _dossier(**overrides) -> dict:
    base = {
        "ok": True,
        "mode": "url",
        "query": "https://example.test/page",
        "url": "https://example.test/page",
        "title": "Example Page",
        "summary_text": "Background facts about the topic.",
        "facts": ["A fact about the topic."],
        "claims_text": "",
        "derived_design": None,
        "images": [],
        "search": [],
        "audit": [],
        "notes": [],
        "html_text": "<html><body>cached page</body></html>",
    }
    base.update(overrides)
    return base


def test_render_brief_research_derived_style_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deps short-circuit AFTER style resolution, so no render is attempted.
    monkeypatch.setattr(video_pipeline, "check_dependencies", lambda: ["ffmpeg"])
    derived = video_styles.design_from_tokens(
        "brandsite", {"bg": "#101014", "text": "#FAFAF5", "accent": "#FF5500"}, ["Inter"]
    )
    calls: list[str] = []

    def fake_build_dossier(query: str, **kwargs) -> dict:
        calls.append(query)
        return _dossier(derived_design=derived)

    stub_mod = SimpleNamespace(
        build_dossier=fake_build_dossier,
        collect_reference_images=lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(video_pipeline, "_video_research_module", lambda: stub_mod)
    result = video_pipeline.render_brief(
        "a story brief", research="https://example.test/page"
    )
    assert calls == ["https://example.test/page"]
    assert result["style"] == "brandsite"  # the dossier's derived design won
    assert result["ok"] is False


def test_render_brief_explicit_style_beats_derived_design(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_pipeline, "check_dependencies", lambda: ["ffmpeg"])
    derived = video_styles.design_from_tokens(
        "brandsite", {"bg": "#101014", "text": "#FAFAF5"}, []
    )
    result = video_pipeline.render_brief(
        "a story brief",
        style="coral",
        research_dossier=_dossier(derived_design=derived),
    )
    assert result["style"] == "coral"


def test_render_brief_auto_style_consults_dossier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_pipeline, "check_dependencies", lambda: ["ffmpeg"])
    seen: dict = {}

    def fake_suggest(brief: str, dossier=None) -> str:
        seen["dossier"] = dossier
        return "neutral"

    monkeypatch.setattr(video_pipeline.video_styles, "suggest_style", fake_suggest)
    dossier = _dossier()
    result = video_pipeline.render_brief(
        "a story brief", style="auto", research_dossier=dossier
    )
    assert seen["dossier"] is dossier
    assert result["style"] == "neutral"


def test_render_brief_research_module_absent_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_pipeline, "_video_research_module", lambda: None)
    monkeypatch.setattr(video_pipeline, "check_dependencies", lambda: ["ffmpeg"])
    result = video_pipeline.render_brief(
        "a story brief", research="https://example.test/x"
    )
    assert result["style"] == "neutral"  # falls through to env/neutral, no crash
    assert "missing dependencies" in result["error"]


def _research_render(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dossier: dict,
    lane_json: str,
    *,
    render_ok: bool = False,
    art_max: int | None = None,
    render_kwargs: dict | None = None,
) -> tuple[dict, list[str]]:
    """Drive render_brief to the render stage with every heavy seam stubbed."""

    monkeypatch.setattr(video_pipeline, "check_dependencies", lambda: [])
    monkeypatch.setattr(
        video_pipeline, "build_voiceover", lambda beats, assets_dir, voice=None: ""
    )
    monkeypatch.setenv("VIDEO_ART", "off")
    monkeypatch.setenv("VIDEO_JUDGE", "off")
    prompts: list[str] = []

    def fake_lane(prompt: str, task_name: str = "") -> tuple[str, str]:
        prompts.append(prompt)
        return lane_json, "lane:test-model"

    monkeypatch.setattr(video_pipeline, "_run_lane", fake_lane)
    if render_ok:
        monkeypatch.setattr(
            video_pipeline,
            "run_hyperframes_render",
            lambda out_dir, mp4_path, fps=30: {"ok": True, "error": "", "command": ""},
        )
        monkeypatch.setattr(
            video_pipeline,
            "verify_rendered_mp4",
            lambda path, dur: {"ok": False, "reason": "stub verify", "duration": 1.0},
        )
    else:
        monkeypatch.setattr(
            video_pipeline,
            "run_hyperframes_render",
            lambda out_dir, mp4_path, fps=30: {
                "ok": False,
                "error": "render stubbed",
                "command": "",
            },
        )
    result = video_pipeline.render_brief(
        "a story brief",
        style="neutral",
        research_dossier=dossier,
        art_max=art_max,
        output_root=str(tmp_path),
        **(render_kwargs or {}),
    )
    return result, prompts


def test_render_brief_research_text_reaches_lane_prompt_and_run_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dossier = _dossier(summary_text="The final ended 2-0 on a Sunday evening.")
    result, prompts = _research_render(monkeypatch, tmp_path, dossier, GOOD_JSON_OUTPUT)

    # The dossier summary rides into the copy prompt as untrusted data.
    assert any("<research-data>" in p and "2-0" in p for p in prompts)

    out_dir = Path(result["output_dir"])
    research = json.loads((out_dir / "research.json").read_text(encoding="utf-8"))
    assert "html_text" not in research  # cached page html never persists
    assert research["mode"] == "url"
    assert research["title"] == "Example Page"

    beats_doc = json.loads((out_dir / "beats.json").read_text(encoding="utf-8"))
    assert beats_doc["research"] == {
        "mode": "url",
        "url": "https://example.test/page",
        "title": "Example Page",
        "images": 0,
        "search": 0,
    }
    assert any(str(n).startswith("research: mode=url") for n in beats_doc["notes"])


def test_render_brief_dossier_claims_thread_into_final_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stat_json = json.dumps(
        {
            "duration_s": None,
            "aspect": None,
            "beats": [
                {
                    "kind": "hero",
                    "eyebrow": "RECAP",
                    "headline": "The season in one number",
                    "subhead": "",
                    "voice": "The champions averaged 58% possession.",
                    "cta": "",
                },
                {
                    "kind": "payoff",
                    "eyebrow": "DONE",
                    "headline": "A season to remember",
                    "subhead": "",
                    "voice": "A season to remember.",
                    "cta": "",
                },
            ],
        }
    )
    summary = "The champions averaged 58% possession."

    # Internal gate passes via summary_text, but with an EMPTY claims_text
    # the FINAL gate rejects the claim-shaped stat.
    rejected = _dossier(summary_text=summary, claims_text="")
    result, _prompts = _research_render(
        monkeypatch, tmp_path, rejected, stat_json, render_ok=True
    )
    assert result["provider"] == "lane:test-model"
    assert result["score"]["categories"]["claim_safety"] == 0

    # The same dossier carrying the metric in claims_text lets it pass.
    allowed = _dossier(summary_text=summary, claims_text=summary)
    result2, _prompts2 = _research_render(
        monkeypatch, tmp_path, allowed, stat_json, render_ok=True
    )
    assert result2["score"]["categories"]["claim_safety"] == 16


def test_render_brief_dossier_images_become_art_refs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ref = tmp_path / "ref0.png"
    ref.write_bytes(b"\x89PNG fake")
    dossier = _dossier(
        images=[{"url": "https://example.test/og.png", "path": str(ref), "kind": "og"}]
    )
    seen: dict = {}

    def fake_plan(beats, design, aspect, assets_dir, *, refs=None, max_images=None):
        seen["refs"] = refs
        seen["max_images"] = max_images
        return {0: "assets/hero.png"}

    monkeypatch.setattr(video_pipeline.video_imagegen, "generate_art_plan", fake_plan)
    monkeypatch.setattr(video_pipeline, "check_dependencies", lambda: [])
    monkeypatch.setattr(
        video_pipeline, "build_voiceover", lambda beats, assets_dir, voice=None: ""
    )
    monkeypatch.delenv("VIDEO_ART", raising=False)
    monkeypatch.setenv("VIDEO_JUDGE", "off")
    monkeypatch.setattr(
        video_pipeline,
        "_run_lane",
        lambda prompt, task_name="": (GOOD_JSON_OUTPUT, "lane:test-model"),
    )
    monkeypatch.setattr(
        video_pipeline,
        "run_hyperframes_render",
        lambda out_dir, mp4_path, fps=30: {"ok": False, "error": "stub", "command": ""},
    )
    result = video_pipeline.render_brief(
        "a story brief",
        style="neutral",
        research_dossier=dossier,
        art_max=2,
        output_root=str(tmp_path),
    )
    assert seen["refs"] == [str(ref)]  # dossier images ride as identity refs
    assert seen["max_images"] == 2  # art_max threads through

    beats_doc = json.loads(
        (Path(result["output_dir"]) / "beats.json").read_text(encoding="utf-8")
    )
    assert beats_doc["art"] == "generated"
    assert beats_doc["art_map"] == {"0": "assets/hero.png"}


def test_render_brief_art_off_skips_art_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom_plan(*args, **kwargs):
        raise AssertionError("the art plan must not run when art is off")

    monkeypatch.setattr(video_pipeline.video_imagegen, "generate_art_plan", boom_plan)
    result, _prompts = _research_render(
        monkeypatch, tmp_path, _dossier(), GOOD_JSON_OUTPUT
    )
    beats_doc = json.loads(
        (Path(result["output_dir"]) / "beats.json").read_text(encoding="utf-8")
    )
    assert beats_doc["art"] == "css"
    assert beats_doc["art_map"] == {}


# =============================================================================
# 10. BORN-CLEAN REGRESSION (public modules: no private/house tokens)
# =============================================================================

# Forbidden everywhere. (The previous default-voice name moved OUT of this
# list when it became the shipped public default.)
_BASE_FORBIDDEN = (
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
    "TELEGRAM_BOT" + "_TOKEN",
)

# The image-adapter provider CLI name: allowed ONLY in video_imagegen.py
# (documented optional public adapter); forbidden in every other file.
_PROVIDER_TOKEN = "co" + "dex"

_STRICT_FILES = (
    _SCRIPTS / "video_styles.py",
    _SCRIPTS / "video_pipeline.py",
    _SCRIPTS / "video_archetypes.py",
    _SCRIPTS / "video_research.py",
    Path(__file__),
    Path(__file__).parent / "test_video_styles.py",
    Path(__file__).parent / "test_video_imagegen.py",
    Path(__file__).parent / "test_video_archetypes.py",
    Path(__file__).parent / "test_video_research.py",
)
_PROVIDER_OK_FILES = (_SCRIPTS / "video_imagegen.py",)


def test_born_clean_no_forbidden_tokens_or_em_dashes() -> None:
    for path in _STRICT_FILES + _PROVIDER_OK_FILES:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for token in _BASE_FORBIDDEN:
            assert token.lower() not in lowered, f"{path.name} contains {token!r}"
        assert _EM not in text, f"{path.name} contains an em-dash"
    for path in _STRICT_FILES:
        lowered = path.read_text(encoding="utf-8").lower()
        assert _PROVIDER_TOKEN not in lowered, (
            f"{path.name} contains the provider token (allowed only in the adapter)"
        )


# =============================================================================
# 11. GENERATE_VISION (the operator-approved production plan + render binding)
# =============================================================================

VISION_BRIEF = "Mexico beat Brazil 2-0 to win the cup. The run stayed unbeaten."

VISION_JSON = """```json
{"angle": "Mexico's first world title, sealed 2-0.",
 "beats": [
   {"kind": "hero", "summary": "Open on the trophy lift."},
   {"kind": "stat", "summary": "The 2-0 scoreline as a wallpaper number."},
   {"kind": "payoff", "summary": "Close on the celebration."}],
 "imagery": {"treatment": "css", "note": "type does the work"}}
```"""


def test_generate_vision_happy_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_lane(prompt: str, task_name: str = "") -> tuple[str, str]:
        calls.append(task_name)
        assert "BEAT KINDS" in prompt
        for kind in ("hero", "stat", "ledger", "payoff", "caption"):
            assert kind in prompt
        assert "FIRST beat is always" in prompt
        assert "LAST beat is always" in prompt
        return VISION_JSON, "lane:test-model"

    monkeypatch.setattr(video_pipeline, "_run_lane", fake_lane)
    vision = video_pipeline.generate_vision(
        VISION_BRIEF, kind="event", style="bold-poster", voice_label="ryan"
    )
    assert calls == ["video_vision"]
    assert vision["ok"] is True
    assert vision["angle"] == "Mexico's first world title, sealed 2-0."
    assert [b["kind"] for b in vision["beats"]] == ["hero", "stat", "payoff"]
    assert vision["imagery"] == {"treatment": "css", "note": "type does the work"}
    assert vision["duration_s"] == 30  # kind default: event
    assert vision["aspect"] == "16:9"
    assert vision["style"] == "bold-poster"
    assert vision["voice"] == "ryan"
    assert vision["provider"] == "lane:test-model"


def test_generate_vision_claims_reject_retry_then_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invented = json.dumps(
        {
            "angle": "Ticket sales jumped 400% after the final.",
            "beats": [
                {"kind": "hero", "summary": "Open on the trophy lift."},
                {"kind": "payoff", "summary": "Close on the celebration."},
            ],
            "imagery": {"treatment": "css", "note": "type"},
        }
    )
    calls: list[str] = []

    def fake_lane(prompt: str, task_name: str = "") -> tuple[str, str]:
        calls.append(task_name)
        if len(calls) == 2:
            assert "YOUR PREVIOUS ATTEMPT WAS REJECTED" in prompt
        return invented, "lane:test-model"

    monkeypatch.setattr(video_pipeline, "_run_lane", fake_lane)
    vision = video_pipeline.generate_vision(VISION_BRIEF, kind="event")
    assert calls == ["video_vision", "video_vision_retry"]
    assert vision["provider"] == "fallback"
    assert any("claim gate" in n for n in vision["notes"])
    assert any("fallback" in n for n in vision["notes"])
    # The deterministic fallback shape: hero first, payoff last, never photos.
    assert vision["beats"][0]["kind"] == "hero"
    assert vision["beats"][-1]["kind"] == "payoff"
    assert 2 <= len(vision["beats"]) <= 8
    assert vision["imagery"]["treatment"] in ("stylized", "css")


def test_generate_vision_lane_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(prompt: str, task_name: str = "") -> tuple[str, str]:
        raise RuntimeError("no lanes configured")

    monkeypatch.setattr(video_pipeline, "_run_lane", boom)
    vision = video_pipeline.generate_vision(VISION_BRIEF, kind="promo")
    assert vision["ok"] is True  # the wizard always gets a usable card
    assert vision["provider"] == "fallback"
    assert any("lane unavailable" in n for n in vision["notes"])


def test_generate_vision_schema_clamps(monkeypatch: pytest.MonkeyPatch) -> None:
    sprawling = json.dumps(
        {
            "angle": "the story goes on and on " * 12,  # ~300 chars
            "beats": [
                {"kind": "caption", "summary": "another scene tells more of the story " * 6}
                for _ in range(10)
            ],
            "imagery": {"treatment": "photos", "note": "grab the site shots"},
        }
    )
    monkeypatch.setattr(
        video_pipeline, "_run_lane", lambda prompt, task_name="": (sprawling, "lane:test-model")
    )
    vision = video_pipeline.generate_vision(VISION_BRIEF, kind="event")
    assert len(vision["angle"]) <= video_pipeline.VISION_ANGLE_MAX_CHARS
    assert len(vision["beats"]) == video_pipeline.VISION_MAX_BEATS  # 10 -> 8
    assert all(
        len(b["summary"]) <= video_pipeline.VISION_SUMMARY_MAX_CHARS
        for b in vision["beats"]
    )
    # photos without any dossier visuals is coerced to stylized, with a note.
    assert vision["imagery"]["treatment"] == "stylized"
    assert any("coerced" in n for n in vision["notes"])


def test_generate_vision_photos_kept_with_dossier_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photos = json.dumps(
        {
            "angle": "The title run, in real shots.",
            "beats": [
                {"kind": "hero", "summary": "Open on the trophy lift."},
                {"kind": "payoff", "summary": "Close on the celebration."},
            ],
            "imagery": {"treatment": "photos", "note": "the site has the shots"},
        }
    )
    monkeypatch.setattr(
        video_pipeline, "_run_lane", lambda prompt, task_name="": (photos, "lane:test-model")
    )
    dossier = _dossier(images=[{"url": "https://example.test/og.png", "path": "x", "kind": "og"}])
    vision = video_pipeline.generate_vision(VISION_BRIEF, kind="event", dossier=dossier)
    assert vision["imagery"]["treatment"] == "photos"


def test_generate_vision_duration_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(prompt: str, task_name: str = "") -> tuple[str, str]:
        raise RuntimeError("offline")

    monkeypatch.setattr(video_pipeline, "_run_lane", boom)
    # Kind defaults when nothing else is stated.
    assert video_pipeline.generate_vision("a plain brief", kind="hype")["duration_s"] == 20
    assert video_pipeline.generate_vision("a plain brief", kind="promo")["duration_s"] == 30
    assert video_pipeline.generate_vision("a plain brief", kind="explainer")["duration_s"] == 45
    assert video_pipeline.generate_vision("a plain brief", kind=None)["duration_s"] == 30
    # A brief-stated length beats the kind default.
    stated = video_pipeline.generate_vision("a 90 second recap of the match", kind="hype")
    assert stated["duration_s"] == 90
    # The explicit argument wins over everything, clamped to 8..120.
    assert video_pipeline.generate_vision("a brief", kind="hype", duration_s=300)["duration_s"] == 120
    assert video_pipeline.generate_vision("a brief", kind="hype", duration_s=4)["duration_s"] == 8


def test_vision_prompt_redo_block(monkeypatch: pytest.MonkeyPatch) -> None:
    prior = {"beats": [{"kind": "hero", "summary": "Open on the trophy lift."}]}
    prompt = video_pipeline._vision_prompt(
        VISION_BRIEF, feedback="lead with the score", prior_vision=prior
    )
    assert "OPERATOR REDO:" in prompt
    assert "Operator notes: lead with the score" in prompt
    assert "Produce a DIFFERENT take; do not repeat the prior outline." in prompt
    assert "PRIOR OUTLINE (do not repeat): 1.[hero] Open on the trophy lift." in prompt
    bare = video_pipeline._vision_prompt(VISION_BRIEF)
    assert "OPERATOR REDO:" not in bare
    # photos is only offered as a choosable treatment when the dossier has visuals.
    with_photos = video_pipeline._vision_prompt(VISION_BRIEF, photos_allowed=True)
    assert '"photos"' in with_photos
    assert '"photos"' not in bare


def test_render_brief_vision_outline_reaches_prompt_and_beats_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vision = {
        "ok": True,
        "angle": "The whole story in one pass.",
        "beats": [
            {"kind": "hero", "summary": "The opening"},
            {"kind": "stat", "summary": "The score"},
            {"kind": "payoff", "summary": "The close"},
        ],
        "imagery": {"treatment": "stylized", "note": ""},
        "duration_s": 30,
        "aspect": "16:9",
    }
    result, prompts = _research_render(
        monkeypatch, tmp_path, _dossier(), GOOD_JSON_OUTPUT, render_kwargs={"vision": vision}
    )
    assert "APPROVED OUTLINE" in prompts[0]
    assert "1. hero: The opening" in prompts[0]
    assert "2. stat: The score" in prompts[0]
    assert "Write exactly 3 beats." in prompts[0]
    beats_doc = json.loads(
        (Path(result["output_dir"]) / "beats.json").read_text(encoding="utf-8")
    )
    assert beats_doc["vision"]["angle"] == "The whole story in one pass."
    assert len(beats_doc["vision"]["beats"]) == 3


def test_render_brief_imagery_css_disables_art(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom_plan(*args, **kwargs):
        raise AssertionError("imagery=css must skip the art plan entirely")

    monkeypatch.setattr(video_pipeline.video_imagegen, "generate_art_plan", boom_plan)
    monkeypatch.delenv("VIDEO_ART", raising=False)  # css must win WITHOUT the env switch
    monkeypatch.setenv("VIDEO_JUDGE", "off")
    monkeypatch.setattr(video_pipeline, "check_dependencies", lambda: [])
    monkeypatch.setattr(
        video_pipeline, "build_voiceover", lambda beats, assets_dir, voice=None: ""
    )
    monkeypatch.setattr(
        video_pipeline,
        "_run_lane",
        lambda prompt, task_name="": (GOOD_JSON_OUTPUT, "lane:test-model"),
    )
    monkeypatch.setattr(
        video_pipeline,
        "run_hyperframes_render",
        lambda out_dir, mp4_path, fps=30: {"ok": False, "error": "stub", "command": ""},
    )
    result = video_pipeline.render_brief(
        "a story brief", style="neutral", imagery="css", output_root=str(tmp_path)
    )
    beats_doc = json.loads(
        (Path(result["output_dir"]) / "beats.json").read_text(encoding="utf-8")
    )
    assert beats_doc["art"] == "css"
    assert beats_doc["art_map"] == {}


def test_render_brief_imagery_photos_maps_refs_to_art_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ref0 = tmp_path / "ref0.png"
    ref1 = tmp_path / "ref1.png"
    ref0.write_bytes(b"\x89PNG fake")
    ref1.write_bytes(b"\x89PNG fake")
    dossier = _dossier(
        images=[
            {"url": "https://example.test/a.png", "path": str(ref0), "kind": "og"},
            {"url": "https://example.test/b.png", "path": str(ref1), "kind": "img"},
        ]
    )
    photo_beats = json.dumps(
        {
            "duration_s": None,
            "aspect": None,
            "beats": [
                {
                    "kind": "hero",
                    "energy": "medium",
                    "eyebrow": "GO",
                    "headline": "A headline",
                    "subhead": "",
                    "voice": "A spoken line.",
                    "cta": "",
                },
                {
                    "kind": "payoff",
                    "energy": "medium",
                    "eyebrow": "END",
                    "headline": "Another headline",
                    "subhead": "",
                    "voice": "Another spoken line.",
                    "cta": "",
                },
            ],
        }
    )

    def boom_plan(*args, **kwargs):
        raise AssertionError("imagery=photos must use refs directly, never generate")

    monkeypatch.setattr(video_pipeline.video_imagegen, "generate_art_plan", boom_plan)
    # photos is an explicit operator approval: it outranks even VIDEO_ART=off.
    result, _prompts = _research_render(
        monkeypatch,
        tmp_path,
        dossier,
        photo_beats,
        render_kwargs={"imagery": "photos"},
    )
    out_dir = Path(result["output_dir"])
    beats_doc = json.loads((out_dir / "beats.json").read_text(encoding="utf-8"))
    assert beats_doc["art"] == "photos"
    assert beats_doc["art_map"] == {"0": "assets/hero.png", "1": "assets/art1.png"}
    assert (out_dir / "assets" / "hero.png").is_file()
    assert (out_dir / "assets" / "art1.png").is_file()
