"""Unit tests for the scene archetype engine (video_archetypes).

Pure tests: no network, no render, no LLM. Covers:
  1. registry completeness (ARCHETYPES == KINDS)
  2. the full matrix: every archetype x 4 styles x 2 aspects validates
     clean, ids are sid-prefixed, dom is non-empty, late ids are revealed
  3. caption archetype: byte-compatible panel DOM + entrance parity with
     the composer's inline builder
  4. structural gates: no hex literals in the module source, no
     random/clock JS in ANY emitted line, born-clean module source
  5. palette-driven CSS: own style hexes present, foreign style hexes absent
  6. transition emitters: cut/crossfade/slide shapes, dip-over-blackout,
     whip transform reset, chroma_split gating + CSS fallback
  7. resolve_archetype backfill matrix
  8. texture layer: flag gating, always-emitted blackout, finite drift
  9. vertical adaptation, group-stagger budget, sub-beat motion, energy
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import video_archetypes as va  # noqa: E402
import video_styles  # noqa: E402

_EM = chr(0x2014)  # em-dash, built via chr() so this file stays born-clean

STYLES = ("neutral", "blockframe", "broadside", "bold-poster")

_DOM_ID = re.compile(r'id="([^"]+)"')
_LINE_TIME = re.compile(r", ([0-9.]+)\);$")
_FORBIDDEN_JS = re.compile(
    r"Math\.random|Date\.now|requestAnimationFrame|setTimeout|performance\.now"
)


def make_beat(kind: str = "", **overrides) -> SimpleNamespace:
    """Duck-typed beat (the engine never imports the pipeline's Beat)."""

    beat = SimpleNamespace(
        eyebrow="SIGNAL",
        headline="One clear win",
        subhead="A supporting line about the topic",
        cta="example.com/start",
        voice_text="A spoken line about the topic.",
        kind=kind,
        energy="medium",
        stat={},
        items=[],
    )
    if kind == "stat":
        beat.stat = {"value": "2-0", "label": "final score"}
    if kind in ("list", "cards", "ledger"):
        beat.items = [
            {"title": "First point", "detail": "why it matters"},
            {"title": "Second point", "detail": "what changed"},
            {"title": "Third point", "detail": ""},
        ]
    for key, value in overrides.items():
        setattr(beat, key, value)
    return beat


def make_spec(
    index: int = 1,
    count: int = 4,
    *,
    vertical: bool = False,
    dur: float = 6.0,
    art: str = "",
    energy: str = "medium",
) -> va.SceneSpec:
    width, height = (1080, 1920) if vertical else (1920, 1080)
    return va.SceneSpec(
        sid=f"s{index}",
        index=index,
        count=count,
        start_s=round(index * dur, 3),
        dur_s=dur,
        width=width,
        height=height,
        m=min(width, height),
        fps=30,
        vertical=vertical,
        energy=energy,
        art_rel=art,
        caption_band_px=int(height * 0.085),
    )


def build(kind: str, style: str, *, vertical: bool = False, **spec_kw) -> va.SceneFragment:
    design = video_styles.resolve_design(style=style)
    return va.ARCHETYPES[kind](make_beat(kind), design, make_spec(vertical=vertical, **spec_kw))


def all_js(frag: va.SceneFragment) -> list[str]:
    return list(frag.entrance_js) + list(frag.sub_beat_js)


# =============================================================================
# 1. REGISTRY COMPLETENESS
# =============================================================================


def test_registry_covers_every_kind() -> None:
    assert set(va.ARCHETYPES.keys()) == set(va.KINDS)
    assert len(va.KINDS) == 9
    for fn in va.ARCHETYPES.values():
        assert callable(fn)


def test_contract_constants() -> None:
    assert va.ENERGIES == ("low", "medium", "high")
    assert set(va.RASTER_SAFE) == {"hero", "quote", "stat", "caption"}
    assert set(va.TRANSITIONS) == {"cut", "crossfade", "slide", "whip", "dip", "chroma_split"}


# =============================================================================
# 2. FULL MATRIX: kind x style x aspect
# =============================================================================


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("vertical", (False, True), ids=("16x9", "9x16"))
@pytest.mark.parametrize("kind", va.KINDS)
def test_matrix_validates_clean(kind: str, style: str, vertical: bool) -> None:
    frag = build(kind, style, vertical=vertical)
    assert frag.dom.strip(), f"{kind}/{style} produced empty dom"
    assert va.validate_fragment(frag) == []
    # All element ids carry the sid prefix.
    for eid in _DOM_ID.findall(frag.dom):
        assert eid.startswith("s1-"), f"{kind}/{style}: id {eid!r} not sid-prefixed"
    # Every late id is revealed by an autoAlpha: 1 line.
    joined = "\n".join(all_js(frag))
    for lid in frag.late_ids:
        pattern = rf'"#{re.escape(lid)}"[^\n]*autoAlpha: 1'
        assert re.search(pattern, joined), f"{kind}/{style}: late id {lid} never revealed"
    # Explicit background handling: palette-driven surfaces exist.
    assert "background" in (frag.css + frag.dom)
    # Fragments declare a css dedupe key and a transition preference.
    assert frag.css_key
    assert frag.transition_pref in ("auto",) + va.TRANSITIONS


@pytest.mark.parametrize("kind", va.KINDS)
def test_matrix_entrance_lines_are_absolute_statements(kind: str) -> None:
    frag = build(kind, "neutral")
    for line in all_js(frag):
        assert line.rstrip().endswith(";"), f"{kind}: JS line missing semicolon: {line!r}"
        assert "tl." in line


# =============================================================================
# 3. CAPTION BYTE-COMPAT (the acceptance gate for the composer swap)
# =============================================================================


def test_caption_dom_byte_compatible_with_composer_panel() -> None:
    design = video_styles.resolve_design(style="neutral")
    spec = make_spec(index=1)
    beat = make_beat(
        "caption",
        eyebrow="A & B",
        headline="Big <One>",
        subhead='With "quotes"',
        cta="go now",
    )
    frag = va.ARCHETYPES["caption"](beat, design, spec)
    expected = (
        '      <div class="panel">\n'
        '        <div id="s1-eyebrow" class="eyebrow">A &amp; B</div>\n'
        '        <div id="s1-headline" class="headline">Big &lt;One&gt;</div>\n'
        '        <div id="s1-subhead" class="subhead">With &quot;quotes&quot;</div>\n'
        '        <div id="s1-cta" class="cta">go now</div>\n'
        "      </div>"
    )
    assert frag.dom == expected


def test_caption_optional_fields_drop_out_of_dom() -> None:
    design = video_styles.resolve_design(style="neutral")
    beat = make_beat("caption", eyebrow="", subhead="", cta="")
    frag = va.ARCHETYPES["caption"](beat, design, make_spec(index=0))
    expected = (
        '      <div class="panel">\n'
        '        <div id="s0-headline" class="headline">One clear win</div>\n'
        "      </div>"
    )
    assert frag.dom == expected
    assert frag.late_ids == ["s0-headline"]


def test_caption_entrance_parity_with_composer() -> None:
    """The reveal lines keep the composer's exact tl.to shape and offsets."""

    design = video_styles.resolve_design(style="neutral")
    spec = make_spec(index=1, dur=6.0)  # start_s == 6.0
    frag = va.ARCHETYPES["caption"](make_beat("caption"), design, spec)
    for suffix, offset in (("eyebrow", 0.10), ("headline", 0.26), ("subhead", 0.50), ("cta", 0.70)):
        line = (
            f'  tl.to("#s1-{suffix}", {{ autoAlpha: 1, y: 0, duration: 0.55, '
            f'ease: "power3.out" }}, {round(6.0 + offset, 3)});'
        )
        assert line in frag.entrance_js
    # Pre-hide stays the composer's job: no self-hide at t=0.
    for line in all_js(frag):
        assert "autoAlpha: 0" not in line


def test_caption_css_keeps_panel_classes_and_tokens() -> None:
    design = video_styles.resolve_design(style="blockframe")
    frag = va.ARCHETYPES["caption"](make_beat("caption"), design, make_spec())
    for cls in (".panel", ".eyebrow", ".headline", ".subhead", ".cta"):
        assert cls in frag.css
    # blockframe flourishes survive the port: offset shadow + uppercase.
    assert "box-shadow" in frag.css
    assert "text-transform: uppercase" in frag.css


# =============================================================================
# 4. STRUCTURAL GATES (module source + emitted JS)
# =============================================================================

_MODULE_PATH = _SCRIPTS / "video_archetypes.py"


def test_no_hex_literals_in_module_source() -> None:
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert re.findall(r"#[0-9A-Fa-f]{6}", src) == []


def test_no_forbidden_js_in_any_emitted_line() -> None:
    lines: list[str] = []
    spec_kw = dict(dur=7.0, art="assets/art1.png")
    for style in STYLES:
        design = video_styles.resolve_design(style=style)
        design["flags"]["typed_eyebrow"] = True
        design["flags"]["shader_transitions"] = True
        design["flags"]["grain"] = True
        design["flags"]["vignette"] = True
        design["flags"]["hud_scanline"] = True
        for kind in va.KINDS:
            for vertical in (False, True):
                frag = va.ARCHETYPES[kind](
                    make_beat(kind), design, make_spec(vertical=vertical, **spec_kw)
                )
                lines += all_js(frag)
        texture = va.build_texture(design, width=1920, height=1080, total_s=40.0)
        lines += all_js(texture)
        for tkind in va.TRANSITIONS:
            setup, boundary = va.build_transition(tkind, "s0", "s1", 6.0, design)
            lines += setup + boundary
    assert lines
    for line in lines:
        assert not _FORBIDDEN_JS.search(line), f"forbidden JS in: {line[:120]!r}"
    # No infinite repeats anywhere either.
    for line in lines:
        assert "repeat: -1" not in line


def test_born_clean_module_and_test_sources() -> None:
    forbidden = (
        "ItsS" + "mokeDev",
        "Smoke" + "Alot420",
        "Smoke" + "Dev",
        "Dyna" + "mous",
        "HOMIE-FRAME" + "-MD",
        "x_vi" + "deo",
        "homie-ship" + "post",
        "homie-vi" + "deo",
        "C:" + chr(92) + "Users",
        "C:/" + "Users",
        "second-" + "brain",
        "De" + "gen",
        "TELEGRAM_BOT" + "_TOKEN",
        "co" + "dex",  # provider token: allowed only in the image adapter
    )
    for path in (_MODULE_PATH, Path(__file__)):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for token in forbidden:
            assert token.lower() not in lowered, f"{path.name} contains {token!r}"
        assert _EM not in text, f"{path.name} contains an em-dash"


# =============================================================================
# 5. PALETTE-DRIVEN CSS
# =============================================================================


def emitted_text(frag: va.SceneFragment) -> str:
    return frag.dom + "\n" + frag.css + "\n" + "\n".join(all_js(frag))


@pytest.mark.parametrize("kind", ("caption", "stat", "cards", "hero"))
def test_own_style_hexes_present_foreign_absent(kind: str) -> None:
    own = video_styles.resolve_design(style="broadside")
    foreign = video_styles.resolve_design(style="bold-poster")
    frag = build(kind, "broadside")
    text = emitted_text(frag)
    assert own["palette"]["accent"] in text
    assert own["palette"]["fg"] in text
    for role in ("bg", "fg", "accent", "accent_dim"):
        assert foreign["palette"][role] not in text, f"foreign {role} leaked into {kind}"


def test_panel_chrome_css_flag_mapping() -> None:
    m = 1080
    hard = video_styles.resolve_design(style="blockframe")
    assert "border:" in va.panel_chrome_css(hard, m)
    assert "box-shadow" in va.panel_chrome_css(hard, m)  # offset_shadow
    card = video_styles.resolve_design(style="blue-professional")
    chrome = va.panel_chrome_css(card, m)
    assert "border-radius" in chrome
    assert card["palette"]["accent_dim"] in chrome
    plain = video_styles.resolve_design(style="neutral")
    assert va.panel_chrome_css(plain, m, default="transparent") == "background: transparent;"
    assert "border-radius" in va.panel_chrome_css(plain, m)  # wash default


# =============================================================================
# 6. TRANSITIONS
# =============================================================================


def _design(style: str = "neutral", **flags) -> dict:
    design = video_styles.resolve_design(style=style)
    design["flags"].update(flags)
    return design


def test_transition_cut_crossfade_slide_shapes() -> None:
    design = _design()
    setup, lines = va.build_transition("cut", "s0", "s1", 6.0, design)
    assert '  tl.set("#s1", { autoAlpha: 0 }, 0);' in setup
    assert '  tl.set("#s0", { autoAlpha: 0 }, 6.0);' in lines
    assert '  tl.set("#s1", { autoAlpha: 1 }, 6.0);' in lines

    setup, lines = va.build_transition("slide", "s0", "s1", 6.0, design)
    assert '  tl.set("#s1", { autoAlpha: 0, x: 60 }, 0);' in setup
    assert any("x: -60" in line for line in lines)

    setup, lines = va.build_transition("crossfade", "s0", "s1", 6.0, design)
    joined = "\n".join(lines)
    assert 'tl.to("#s0", { autoAlpha: 0, duration: 0.45' in joined
    assert 'tl.to("#s1", { autoAlpha: 1, duration: 0.45' in joined


def test_transition_dip_references_blackout_and_straddles() -> None:
    _setup, lines = va.build_transition("dip", "s0", "s1", 6.0, _design())
    joined = "\n".join(lines)
    assert joined.count("#blackout") == 2  # fade in + fade out
    assert "opacity: 1, duration: 0.34" in joined  # in before the boundary
    assert "opacity: 0, duration: 0.42" in joined  # out after the boundary
    assert 'tl.set("#s1", { autoAlpha: 1 }, 6.04);' in joined


def test_transition_whip_blurs_and_resets_transforms() -> None:
    _setup, lines = va.build_transition("whip", "s0", "s1", 6.0, _design())
    joined = "\n".join(lines)
    assert 'filter: "blur(14px)"' in joined
    assert 'filter: "blur(0px)"' in joined
    # Outgoing scene gets its transform reset after the whip.
    assert 'tl.set("#s0", { xPercent: 0, filter: "blur(0px)" }' in joined
    # Vertical canvases whip on the y axis.
    _setup, vlines = va.build_transition("whip", "s0", "s1", 6.0, _design(), vertical=True)
    vjoined = "\n".join(vlines)
    assert "yPercent: 100" in vjoined
    assert 'tl.set("#s0", { yPercent: 0, filter: "blur(0px)" }' in vjoined


def test_resolve_transition_precedence_and_chroma_gating() -> None:
    design = _design()  # neutral: motion transition == crossfade, no shader flag
    assert va.resolve_transition("auto", design) == "crossfade"
    assert va.resolve_transition("", design) == "crossfade"
    assert va.resolve_transition("whip", design) == "whip"
    assert va.resolve_transition("bogus", design) == "crossfade"
    cut_design = _design("blockframe")  # motion transition == cut
    assert va.resolve_transition("auto", cut_design) == "cut"
    assert va.resolve_transition("dip", cut_design) == "dip"

    # chroma_split: flag off -> crossfade even between raster-safe kinds.
    assert (
        va.resolve_transition("chroma_split", design, prev_kind="hero", cur_kind="stat")
        == "crossfade"
    )
    shader = _design(shader_transitions=True)
    assert (
        va.resolve_transition("chroma_split", shader, prev_kind="hero", cur_kind="stat")
        == "chroma_split"
    )
    # Raster-unsafe archetype on either side degrades to crossfade.
    assert (
        va.resolve_transition("chroma_split", shader, prev_kind="mockup", cur_kind="stat")
        == "crossfade"
    )
    assert (
        va.resolve_transition("chroma_split", shader, prev_kind="quote", cur_kind="ledger")
        == "crossfade"
    )


def test_transition_chroma_emits_shader_and_css_fallback() -> None:
    design = _design(shader_transitions=True)
    setup, lines = va.build_transition("chroma_split", "s0", "s1", 6.0, design)
    assert '  tl.set("#s1", { autoAlpha: 0 }, 0);' in setup
    block = "\n".join(lines)
    # WebGL path: guarded one-time bootstrap + per-cut progress proxy.
    assert "window.__vidFx" in block
    assert "getContext" in block and "webgl" in block
    assert "fx2.begin" in block and "fx2.draw(proxy.v)" in block
    # Accent seam bloom is palette-derived (no hex in the shader source).
    assert "vec3(" in block
    # CSS-crossfade fallback when gl is unavailable.
    assert 'tl.to("#s1", { autoAlpha: 1, duration: 0.5, ease: "power2.inOut" }' in block
    assert 'tl.to("#s0", { autoAlpha: 0, duration: 0.5, ease: "power2.inOut" }' in block
    # The fallback bg fill is the design's own palette value.
    assert design["palette"]["bg"] in block


# =============================================================================
# 7. RESOLVE_ARCHETYPE BACKFILL MATRIX
# =============================================================================


def test_resolve_declared_kind_wins() -> None:
    design = video_styles.resolve_design(style="neutral")
    spec = make_spec(index=1)
    beat = make_beat("stat")  # stat content present
    assert va.resolve_archetype("quote", beat, design, spec) == "quote"
    # Falls back to beat.kind when the explicit arg is empty.
    assert va.resolve_archetype("", make_beat("ledger"), design, spec) == "ledger"


def test_resolve_backfill_matrix() -> None:
    design = video_styles.resolve_design(style="neutral")
    mid = make_spec(index=1, count=4)
    first = make_spec(index=0, count=4)
    last = make_spec(index=3, count=4)

    # stat content wins the backfill.
    stat_beat = make_beat("", stat={"value": "2-0", "label": "score"})
    assert va.resolve_archetype("", stat_beat, design, mid) == "stat"
    assert va.resolve_archetype("not-a-kind", stat_beat, design, mid) == "stat"

    # >=2 items: details -> ledger, titles-only -> cards.
    detailed = make_beat("", items=[{"title": "A", "detail": "x"}, {"title": "B", "detail": ""}])
    assert va.resolve_archetype("", detailed, design, mid) == "ledger"
    titles = make_beat("", items=[{"title": "A"}, {"title": "B"}])
    assert va.resolve_archetype("", titles, design, mid) == "cards"

    # Position rules.
    plain = make_beat("")
    assert va.resolve_archetype("", plain, design, first) == "hero"
    assert va.resolve_archetype("", plain, design, last) == "payoff"  # has cta
    no_cta = make_beat("", cta="")
    assert va.resolve_archetype("", no_cta, design, last) == "payoff"  # count >= 3
    two_last = make_spec(index=1, count=2)
    assert va.resolve_archetype("", no_cta, design, two_last) == "caption"
    assert va.resolve_archetype("", plain, design, mid) == "caption"


def test_build_scene_dispatches_and_returns_resolved_kind() -> None:
    design = video_styles.resolve_design(style="neutral")
    kind, frag = va.build_scene(make_beat("stat"), design, make_spec(index=1))
    assert kind == "stat"
    assert "s1-stat" in frag.dom
    kind, frag = va.build_scene(make_beat(""), design, make_spec(index=0))
    assert kind == "hero"


def test_hero_without_art_degrades_to_focal_block() -> None:
    design = video_styles.resolve_design(style="neutral")
    bare = va.ARCHETYPES["hero"](make_beat("hero"), design, make_spec(index=0, art=""))
    assert "s0-focal" in bare.dom
    assert "-art" not in bare.dom
    assert "radial-gradient" in bare.css
    arted = va.ARCHETYPES["hero"](
        make_beat("hero"), design, make_spec(index=0, art="assets/art0.png")
    )
    assert "s0-art" in arted.dom
    assert "url('assets/art0.png')" in arted.dom
    assert "s0-focal" not in arted.dom
    # The art layer animates (parallax float) and is revealed.
    assert any("-art" in line and "autoAlpha: 1" in line for line in arted.entrance_js)


def test_hero_typed_eyebrow_flag_switches_mechanic() -> None:
    plain = video_styles.resolve_design(style="neutral")
    frag = va.ARCHETYPES["hero"](make_beat("hero"), plain, make_spec(index=0))
    assert "eyetext" not in frag.dom
    typed_design = _design(typed_eyebrow=True)
    frag = va.ARCHETYPES["hero"](make_beat("hero"), typed_design, make_spec(index=0))
    assert "s0-eyetext" in frag.dom and "s0-caret" in frag.dom
    joined = "\n".join(frag.entrance_js)
    assert "textContent" in joined
    assert 'ease: "steps(1)"' in joined  # caret blink


# =============================================================================
# 8. TEXTURE LAYER
# =============================================================================


def test_texture_blackout_always_emitted() -> None:
    design = video_styles.resolve_design(style="neutral")  # zero texture flags
    frag = va.build_texture(design, width=1920, height=1080, total_s=30.0)
    assert 'id="blackout"' in frag.dom
    assert "#blackout" in frag.css
    assert va.validate_fragment(frag) == []
    # No flags -> no grain, no canvas, no scanlines.
    assert "tex-grain" not in frag.dom
    assert "fx-canvas" not in frag.dom
    assert "tex-scan" not in frag.dom


def test_texture_flags_gate_layers() -> None:
    design = _design("broadside", grain=True, vignette=True, hud_scanline=True, shader_transitions=True)
    frag = va.build_texture(design, width=1920, height=1080, total_s=40.0)
    assert 'id="tex-grain"' in frag.dom
    assert 'id="tex-vignette"' in frag.dom
    assert 'id="tex-scan-a"' in frag.dom  # broadside is a dark canvas
    assert 'id="fx-canvas"' in frag.dom
    assert "feTurbulence" in frag.css
    # Grain drift is finite: explicit repeat: 0, full duration.
    drift = [line for line in frag.entrance_js if "tex-grain-tex" in line]
    assert drift and "repeat: 0" in drift[0] and "duration: 40.0" in drift[0]


def test_texture_scanlines_skip_light_canvases() -> None:
    light = _design("blockframe", hud_scanline=True)
    frag = va.build_texture(light, width=1920, height=1080, total_s=30.0)
    assert "tex-scan" not in frag.dom


# =============================================================================
# 9. LAYOUT ADAPTATION, STAGGER BUDGET, SUB-BEATS, ENERGY
# =============================================================================


def test_vertical_adaptations() -> None:
    cards_h = build("cards", "neutral", vertical=False)
    cards_v = build("cards", "neutral", vertical=True)
    assert "flex-direction: row" in cards_h.css
    assert "flex-direction: column" in cards_v.css

    mock_h = build("mockup", "neutral", vertical=False)
    mock_v = build("mockup", "neutral", vertical=True)
    assert f"width: {int(1920 * 0.52)}px" in mock_h.css
    assert f"width: {int(1080 * 0.86)}px" in mock_v.css

    stat_h = build("stat", "neutral", vertical=False)
    stat_v = build("stat", "neutral", vertical=True)
    assert "align-items: flex-start" in stat_h.css
    assert "align-items: center" in stat_v.css  # the recenter


def test_stat_renders_wallpaper_scale_value() -> None:
    frag = build("stat", "bold-poster")
    assert 'id="s1-stat"' in frag.dom and "2-0" in frag.dom
    assert 'id="s1-statlabel"' in frag.dom and "final score" in frag.dom
    assert f"font-size: {int(1080 * 0.30)}px" in frag.css
    # bold-poster carries stacked_text_shadow: the value honors it.
    assert "text-shadow: 0.045em 0.045em 0" in frag.css


@pytest.mark.parametrize("kind,marker", (("list", "-chip"), ("cards", "-card"), ("ledger", "-row")))
def test_group_stagger_under_half_second(kind: str, marker: str) -> None:
    beat = make_beat(
        kind,
        items=[{"title": f"Point {i}", "detail": "detail text"} for i in range(4)],
    )
    design = video_styles.resolve_design(style="neutral")
    frag = va.ARCHETYPES[kind](beat, design, make_spec(index=1))
    times = []
    for line in frag.entrance_js:
        if marker in line and "autoAlpha: 1" in line:
            match = _LINE_TIME.search(line)
            assert match, line
            times.append(float(match.group(1)))
    assert len(times) == 4
    assert max(times) - min(times) < 0.5


def test_kinetic_words_stagger_under_half_second() -> None:
    beat = make_beat("hero", headline="seven words walk into a tiny bar")
    design = video_styles.resolve_design(style="neutral")
    frag = va.ARCHETYPES["hero"](beat, design, make_spec(index=0))
    times = []
    for line in frag.entrance_js:
        if re.search(r'-w\d+"', line):
            times.append(float(_LINE_TIME.search(line).group(1)))
    assert len(times) == 7
    assert max(times) - min(times) < 0.5


@pytest.mark.parametrize("kind", va.KINDS)
def test_sub_beat_motion_for_long_scenes(kind: str) -> None:
    frag = build(kind, "neutral", dur=6.0)
    assert frag.sub_beat_js, f"{kind}: no sub-beat motion for a 6s scene"


def test_energy_scales_entrance_speed() -> None:
    design = video_styles.resolve_design(style="neutral")
    hot = va.ARCHETYPES["stat"](make_beat("stat"), design, make_spec(index=1, energy="high"))
    cold = va.ARCHETYPES["stat"](make_beat("stat"), design, make_spec(index=1, energy="low"))

    def value_duration(frag: va.SceneFragment) -> float:
        line = next(l for l in frag.entrance_js if "-stat" in l and "autoAlpha: 1" in l)
        return float(re.search(r"duration: ([0-9.]+)", line).group(1))

    assert value_duration(hot) < value_duration(cold)


def test_mockup_drive_sequence_fits_inside_scene() -> None:
    frag = build("mockup", "neutral", dur=5.0)  # spec index 1 -> start 5.0, end 10.0
    times = []
    for line in all_js(frag):
        match = _LINE_TIME.search(line)
        if match:
            times.append(float(match.group(1)))
    in_scene = [t for t in times if t > 0]
    assert in_scene and max(in_scene) <= 10.0
    joined = "\n".join(frag.sub_beat_js)
    assert "textContent" in joined  # typed URL
    assert 'width: "100%"' in joined  # progress sweep
    assert "-page2" in joined and "-page1" in joined  # page swap


# =============================================================================
# 10. VALIDATOR
# =============================================================================


def test_validator_flags_broken_fragment() -> None:
    broken = va.SceneFragment(
        dom='<div id="s9-a"></div>',
        entrance_js=['  tl.to("#s9-b", { autoAlpha: 1, duration: 0.4 }, 1.0);'],
        late_ids=["s9-c"],
    )
    notes = va.validate_fragment(broken)
    joined = " | ".join(notes)
    assert "revealed id not declared late: s9-b" in joined
    assert "late id never revealed: s9-c" in joined
    assert "js references id missing from dom: s9-b" in joined
    assert "late id missing from dom: s9-c" in joined


def test_validator_never_raises() -> None:
    notes = va.validate_fragment(object())  # not even a fragment
    assert isinstance(notes, list) and notes
    assert va.validate_fragment(va.SceneFragment(dom="")) == []
