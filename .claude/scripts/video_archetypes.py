"""Scene archetype engine for the framework video pipeline.

Nine deterministic scene builders (KINDS) turn one beat + one design dict +
one SceneSpec into a SceneFragment: DOM, CSS, pre-hide declarations, and
absolute-time GSAP statements. The composer (video_pipeline.compose_html)
owns scene containers, the global background hero layer, asset serving, and
the master timeline; this module never imports the pipeline.

Beat duck-type contract (no pipeline import; attributes read via getattr):
    eyebrow: str        headline: str       subhead: str        cta: str
    voice_text: str     kind: str           energy: str ("low|medium|high")
    stat: dict          ({"value": "2-0", "label": "final score"})
    items: list[dict]   ([{"title": ..., "detail": ...}], up to 4 used)

Pre-hide discipline (structural):
    - Archetypes NEVER emit their own ``tl.set(..., { autoAlpha: 0 }, 0)``.
      They DECLARE ``late_ids``; the composer emits one
      ``tl.set("#<id>", { autoAlpha: 0 }, 0);`` per declared id.
    - autoAlpha, never plain opacity, for reveal targets: the shader raster
      helper checks each element's OWN computed style, and opacity does not
      inherit to children, but visibility:hidden does, so autoAlpha hides a
      container's text/children from texture capture too.
    - Decorative pulse elements (highlight rings, progress sweeps, swap page
      states, carets) ship with ``opacity: 0`` as their natural CSS state and
      animate plain opacity; they are leaf decorations, not reveal targets,
      and are intentionally NOT late_ids.
    - Per-word kinetic spans hide themselves via fromTo immediateRender
      (from-state applies at timeline build), inside an autoAlpha-gated
      container that IS a late_id.

Emitted JS contract:
    - Every entrance_js / sub_beat_js entry is an absolute-time GSAP
      statement ending in a semicolon, e.g.
      ``tl.fromTo("#s2-chip0", {...}, {...}, 3.42);``. Times are computed
      from ``spec.start_s`` offsets. The chroma_split transition is the one
      exception: it emits a single guarded IIFE block (still ending ``;``).
    - No Math.random / Date.now / requestAnimationFrame / setTimeout /
      performance.now anywhere. No infinite repeats.

Background contract: archetypes never paint an opaque full-bleed layer; the
composer's global palette hero layer stays visible behind every scene. When
``spec.art_rel`` is set the archetype emits its own ``{sid}-art`` backdrop
plus a palette scrim (hero animates it, other kinds keep it static).

Transition emitters return ``(setup_js, boundary_js)``: setup lines belong in
the composer's t=0 prep block (incoming-scene hide + initial offsets;
duplicates of composer prehides are harmless no-ops), boundary lines in the
transition block. The always-emitted ``#blackout`` plate (build_texture) is
the dip target and the final-close plate.

Color rule: ZERO hex literals in this module. Every color is read from the
design dict (palette/extras) or derived with video_styles helpers; rgba()/
rgb() strings are built from hex_to_rgb output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from video_styles import blend_hex, hex_to_rgb, relative_luminance

# =============================================================================
# CONTRACT CONSTANTS
# =============================================================================

KINDS = (
    "hero",
    "stat",
    "list",
    "quote",
    "cards",
    "ledger",
    "mockup",
    "payoff",
    "caption",
)

ENERGIES = ("low", "medium", "high")

TRANSITIONS = ("cut", "crossfade", "slide", "whip", "dip", "chroma_split")

# Archetypes whose base state survives the canvas2d raster used by the
# chroma_split shader (solid backgrounds + plain text; no critical SVG or
# image content). The shader transition is only allowed between two scenes
# whose archetypes are BOTH in this set.
RASTER_SAFE = frozenset({"hero", "quote", "stat", "caption"})


@dataclass(frozen=True)
class SceneSpec:
    """Per-scene geometry/timing the composer hands to every archetype."""

    sid: str  # "s3"; every element id MUST be f"{sid}-..."
    index: int  # 0-based scene index
    count: int  # total scene count
    start_s: float  # absolute scene start on the master timeline
    dur_s: float  # scene duration in seconds
    width: int
    height: int
    m: int  # min(width, height); the sizing unit
    fps: int
    vertical: bool
    energy: str
    art_rel: str  # "" or served-asset relative path for this beat's art
    caption_band_px: int  # reserved karaoke-caption band at the bottom


@dataclass
class SceneFragment:
    """One archetype's contribution to the composed document."""

    dom: str
    css: str = ""
    css_key: str = ""  # dedupe key; the composer emits each key once
    late_ids: list[str] = field(default_factory=list)
    entrance_js: list[str] = field(default_factory=list)
    sub_beat_js: list[str] = field(default_factory=list)
    transition_pref: str = "auto"  # cut|crossfade|slide|whip|dip|auto


# =============================================================================
# SMALL HELPERS
# =============================================================================


def _t(value: float) -> float:
    return round(float(value), 3)


def _esc(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = hex_to_rgb(hex_color)
    return f"rgba({r},{g},{b},{alpha:g})"


def _scaled_rgb(hex_color: str, factor: float) -> str:
    r, g, b = hex_to_rgb(hex_color)
    f = max(0.0, min(1.0, factor))
    return f"rgb({int(r * f)},{int(g * f)},{int(b * f)})"


def _darkest(design: dict) -> str:
    """The darker of bg/fg; base for plates and vignettes."""

    palette = design["palette"]
    bg, fg = palette["bg"], palette["fg"]
    return bg if relative_luminance(bg) <= relative_luminance(fg) else fg


def _is_dark(design: dict) -> bool:
    return relative_luminance(design["palette"]["bg"]) < 0.5


def _muted(design: dict) -> str:
    palette = design["palette"]
    return blend_hex(palette["fg"], palette["bg"], 0.35)


def _energy_mult(energy: str) -> float:
    return {"low": 1.15, "medium": 1.0, "high": 0.85}.get(energy or "medium", 1.0)


def _ease(design: dict) -> str:
    return (design.get("motion", {}) or {}).get("entrance_ease", "power3.out")


def _stat_fields(beat: Any) -> tuple[str, str]:
    raw = getattr(beat, "stat", None) or {}
    if not isinstance(raw, dict):
        return "", ""
    return (
        str(raw.get("value") or "").strip(),
        str(raw.get("label") or "").strip(),
    )


def _item_fields(beat: Any, limit: int = 4) -> list[tuple[str, str]]:
    raw = getattr(beat, "items", None) or []
    out: list[tuple[str, str]] = []
    for item in list(raw)[:limit]:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            detail = str(item.get("detail") or "").strip()
        else:
            title, detail = str(item).strip(), ""
        if title or detail:
            out.append((title or detail, detail if title else ""))
    return out


# =============================================================================
# CHROME HELPERS (flag -> chrome mapping, ported from the composer)
# =============================================================================


def panel_chrome_css(design: dict, m: int, *, default: str = "wash") -> str:
    """Map style flags to panel/card surface chrome.

    Flag branches are byte-compatible with the composer's flourish block:
    hard_borders -> bordered panel (+ offset shadow), card_chrome ->
    accent-wash card. With no chrome flag the ``default`` keyword picks the
    surface: "wash" (subtle fg-into-bg card; cards/ledger/mockup/stat) or
    "transparent" (the caption panel's historical default).
    """

    palette = design["palette"]
    flags = design.get("flags", {}) or {}
    extras = design.get("extras", {}) or {}
    bg, fg = palette["bg"], palette["fg"]
    accent, accent_dim = palette["accent"], palette["accent_dim"]

    if flags.get("hard_borders"):
        border_w = max(4, m // 240)
        shadow_w = border_w * 2 if flags.get("offset_shadow") else 0
        panel_bg = extras.get("white", bg)
        css = (
            f"background: {panel_bg}; border: {border_w}px solid {fg};"
            f" padding: {int(m * 0.045)}px {int(m * 0.05)}px;"
        )
        if shadow_w:
            css += f" box-shadow: {shadow_w}px {shadow_w}px 0 {fg};"
        return css
    if flags.get("card_chrome"):
        return (
            f"background: {blend_hex(bg, accent, 0.05)};"
            f" border: 2px solid {accent_dim}; border-radius: {int(m * 0.014)}px;"
            f" padding: {int(m * 0.04)}px {int(m * 0.045)}px;"
        )
    if default == "transparent":
        return "background: transparent;"
    return (
        f"background: {blend_hex(bg, fg, 0.06)};"
        f" border: 1px solid {blend_hex(bg, fg, 0.16)};"
        f" border-radius: {int(m * 0.014)}px;"
        f" padding: {int(m * 0.04)}px {int(m * 0.045)}px;"
    )


def _headline_extra(design: dict) -> str:
    flags = design.get("flags", {}) or {}
    accent_dim = design["palette"]["accent_dim"]
    extra = ""
    if flags.get("uppercase_display"):
        extra += " text-transform: uppercase;"
    if flags.get("lowercase_display"):
        extra += " text-transform: lowercase;"
    if flags.get("tilted_display"):
        extra += " transform: rotate(-3deg); transform-origin: left bottom;"
    if flags.get("stacked_text_shadow"):
        extra += f" text-shadow: 0.045em 0.045em 0 {accent_dim};"
    return extra


def _eyebrow_pill(design: dict) -> str:
    flags = design.get("flags", {}) or {}
    fg = design["palette"]["fg"]
    if flags.get("pill_shapes") or flags.get("pill_tags"):
        return (
            f" border: 2px solid {fg}; border-radius: 999px;"
            f" padding: 0.35em 1em; width: max-content;"
        )
    return ""


def _marker_radius(design: dict, m: int) -> str:
    """Filled marker shape: circle under pill styles, soft square otherwise."""

    flags = design.get("flags", {}) or {}
    if flags.get("pill_shapes") or flags.get("pill_tags"):
        return "50%"
    return f"{max(2, int(m * 0.004))}px"


# =============================================================================
# SHARED BUILD BLOCKS
# =============================================================================


def _common_css(design: dict, spec: SceneSpec) -> str:
    """Shared header/kinetic/art classes; identical across kinds, so the
    duplicate definitions across css_key blocks are harmless."""

    palette = design["palette"]
    fonts = design["fonts"]
    m = spec.m
    bg, fg, accent = palette["bg"], palette["fg"], palette["accent"]
    muted = _muted(design)
    glow = f" text-shadow: 0 0 {int(m * 0.033)}px {_rgba(accent, 0.5)};" if _is_dark(design) else ""
    weight = int(fonts.get("display_weight", 800))
    return f"""      .va-eyebrow {{ font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.026)}px; font-weight: 600; letter-spacing: 0.18em; text-transform: uppercase; color: {accent};{_eyebrow_pill(design)} }}
      .va-head {{ font-family: "{fonts['display']}", serif; font-size: {int(m * 0.064)}px; font-weight: {weight}; line-height: 1.06; color: {fg};{_headline_extra(design)} }}
      .va-sub {{ font-family: "{fonts['body']}", sans-serif; font-size: {int(m * 0.030)}px; font-weight: 400; line-height: 1.35; color: {muted}; }}
      .kw {{ display: inline-block; }}
      .kw-accent {{ color: {accent};{glow} }}
      .va-art {{ position: absolute; inset: 0; z-index: 0; background-size: contain; background-position: center; background-repeat: no-repeat; }}
      .va-scrim {{ position: absolute; inset: 0; z-index: 1; pointer-events: none; background: linear-gradient(180deg, {_rgba(bg, 0)} 40%, {_rgba(bg, 0.55)} 66%, {_rgba(bg, 0.92)} 100%); }}"""


def _art_backdrop(spec: SceneSpec) -> str:
    """Static scrimmed art backdrop for non-hero kinds (hero animates its own)."""

    if not spec.art_rel:
        return ""
    return (
        f'      <div id="{spec.sid}-art" class="va-art" '
        f"style=\"background-image: url('{spec.art_rel}');\"></div>\n"
        f'      <div class="va-scrim"></div>\n'
    )


def _header_dom(sid: str, wrap_class: str, eyebrow: str, headline: str, subhead: str) -> str:
    parts = [f'      <div class="{wrap_class}">']
    if eyebrow:
        parts.append(
            f'        <div id="{sid}-eyebrow" class="va-eyebrow">{_esc(eyebrow)}</div>'
        )
    parts.append(
        f'        <div id="{sid}-headline" class="va-head">{_esc(headline)}</div>'
    )
    if subhead:
        parts.append(
            f'        <div id="{sid}-subhead" class="va-sub">{_esc(subhead)}</div>'
        )
    parts.append("      </div>")
    return "\n".join(parts)


def _header_js(
    sid: str,
    design: dict,
    spec: SceneSpec,
    *,
    has_eyebrow: bool,
    has_subhead: bool,
) -> tuple[list[str], list[str]]:
    """Slide-in reveals for the shared header block. Returns (lines, late_ids)."""

    ease = _ease(design)
    em = _energy_mult(spec.energy)
    rise = int(spec.m * 0.026)
    t0 = spec.start_s
    lines: list[str] = []
    late: list[str] = []
    rows = [("eyebrow", 0.10, has_eyebrow), ("headline", 0.22, True), ("subhead", 0.42, has_subhead)]
    for suffix, offset, present in rows:
        if not present:
            continue
        eid = f"{sid}-{suffix}"
        late.append(eid)
        lines.append(f'  tl.set("#{eid}", {{ y: {rise} }}, 0);')
        lines.append(
            f'  tl.to("#{eid}", {{ autoAlpha: 1, y: 0, duration: {_t(0.5 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + offset)});'
        )
    return lines, late


def _kinetic_words(
    sid: str,
    text: str,
    t0: float,
    *,
    m: int,
    accent_last: bool = True,
) -> tuple[str, list[str], list[str]]:
    """Per-word spans + decaying-slide tweens (the kinetic-words reveal).

    Words self-hide via fromTo immediateRender; the CALLER must put the
    containing headline element in late_ids and reveal it with an
    autoAlpha set at (or before) ``t0``. Stagger is capped so the whole
    group spreads < 0.5s.
    """

    words = [w for w in str(text or "").split() if w]
    if not words:
        words = [""]
    slides = (90, 64, 44, 28, 16, 10)
    n = len(words)
    step = 0.10 if n <= 1 else min(0.10, 0.45 / (n - 1))
    spans: list[str] = []
    lines: list[str] = []
    ids: list[str] = []
    for k, word in enumerate(words):
        wid = f"{sid}-w{k}"
        cls = "kw kw-accent" if (accent_last and k == n - 1 and n > 1) else "kw"
        spans.append(f'<span id="{wid}" class="{cls}">{_esc(word)}</span>')
        slide = int(round(slides[k % len(slides)] * m / 1080.0))
        lines.append(
            f'  tl.fromTo("#{wid}", {{ x: {slide}, y: {int(m * 0.015)}, opacity: 0 }}, '
            f'{{ x: 0, y: 0, opacity: 1, duration: 0.42, ease: "power3.out" }}, '
            f"{_t(t0 + k * step)});"
        )
        ids.append(wid)
    return " ".join(spans), lines, ids


def _typed_text_js(sid: str, target_suffix: str, text: str, t0: float, step: float) -> list[str]:
    """Indexed tl.call textContent sets (terminal-type mechanic)."""

    lines: list[str] = []
    eid = f"{sid}-{target_suffix}"
    for k in range(len(text) + 1):
        snippet = json.dumps(text[:k])
        lines.append(
            f'  tl.call(function () {{ document.getElementById("{eid}").textContent = '
            f"{snippet}; }}, null, {_t(t0 + k * step)});"
        )
    return lines


# =============================================================================
# ARCHETYPE BUILDERS
# =============================================================================


def _build_caption(beat: Any, design: dict, spec: SceneSpec) -> SceneFragment:
    """Byte-compatible port of the composer's inline panel builder."""

    palette = design["palette"]
    fonts = design["fonts"]
    m, sid = spec.m, spec.sid
    bg, fg, accent = palette["bg"], palette["fg"], palette["accent"]
    ease = _ease(design)
    eyebrow = str(getattr(beat, "eyebrow", "") or "")
    headline = str(getattr(beat, "headline", "") or "")
    subhead = str(getattr(beat, "subhead", "") or "")
    cta = str(getattr(beat, "cta", "") or "")

    panel_parts: list[str] = []
    if eyebrow:
        panel_parts.append(
            f'        <div id="{sid}-eyebrow" class="eyebrow">{_esc(eyebrow)}</div>'
        )
    panel_parts.append(
        f'        <div id="{sid}-headline" class="headline">{_esc(headline)}</div>'
    )
    if subhead:
        panel_parts.append(
            f'        <div id="{sid}-subhead" class="subhead">{_esc(subhead)}</div>'
        )
    if cta:
        panel_parts.append(f'        <div id="{sid}-cta" class="cta">{_esc(cta)}</div>')
    panel = '      <div class="panel">\n' + "\n".join(panel_parts) + "\n      </div>"
    dom = _art_backdrop(spec) + panel

    weight = int(fonts.get("display_weight", 800))
    css = f"""      .panel {{ position: relative; z-index: 1; display: flex; flex-direction: column; gap: {int(m * 0.020)}px; max-width: {int(spec.width * 0.80)}px; {panel_chrome_css(design, m, default="transparent")} }}
      .eyebrow {{ font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.026)}px; font-weight: 600; letter-spacing: 0.18em; text-transform: uppercase; color: {accent};{_eyebrow_pill(design)} }}
      .headline {{ font-family: "{fonts['display']}", serif; font-size: {int(m * 0.088)}px; font-weight: {weight}; line-height: 1.04; color: {fg};{_headline_extra(design)} }}
      .subhead {{ font-family: "{fonts['body']}", sans-serif; font-size: {int(m * 0.036)}px; font-weight: 400; line-height: 1.35; color: {_muted(design)}; }}
      .cta {{ font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.030)}px; font-weight: 600; color: {accent}; margin-top: {int(m * 0.012)}px; }}
{_common_css(design, spec)}"""

    late: list[str] = []
    entrance: list[str] = []
    rise = int(m * 0.026)
    reveal_offsets = (
        ("eyebrow", 0.10, bool(eyebrow)),
        ("headline", 0.26, True),
        ("subhead", 0.50, bool(subhead)),
        ("cta", 0.70, bool(cta)),
    )
    for suffix, offset, present in reveal_offsets:
        if not present:
            continue
        eid = f"{sid}-{suffix}"
        late.append(eid)
        entrance.append(f'  tl.set("#{eid}", {{ y: {rise} }}, 0);')
        entrance.append(
            f'  tl.to("#{eid}", {{ autoAlpha: 1, y: 0, duration: 0.55, '
            f'ease: "{ease}" }}, {round(spec.start_s + offset, 3)});'
        )

    sub_beat: list[str] = []
    if spec.dur_s > 4:
        drift = max(4, int(m * 0.008))
        sub_beat.append(
            f'  tl.to("#{sid}-headline", {{ y: -{drift}, '
            f'duration: {_t(max(0.8, spec.dur_s - 1.8))}, ease: "sine.inOut" }}, '
            f"{_t(spec.start_s + 1.5)});"
        )

    return SceneFragment(
        dom=dom,
        css=css,
        css_key="k-caption",
        late_ids=late,
        entrance_js=entrance,
        sub_beat_js=sub_beat,
        transition_pref="auto",
    )


def _build_hero(beat: Any, design: dict, spec: SceneSpec) -> SceneFragment:
    palette = design["palette"]
    flags = design.get("flags", {}) or {}
    m, sid = spec.m, spec.sid
    bg, accent, accent_dim = palette["bg"], palette["accent"], palette["accent_dim"]
    ease = _ease(design)
    em = _energy_mult(spec.energy)
    t0 = spec.start_s
    dark = _is_dark(design)
    eyebrow = str(getattr(beat, "eyebrow", "") or "")
    headline = str(getattr(beat, "headline", "") or "")
    subhead = str(getattr(beat, "subhead", "") or "")
    cta = str(getattr(beat, "cta", "") or "")
    typed = bool(flags.get("typed_eyebrow")) and bool(eyebrow)

    pad_x = int(spec.width * 0.099)
    pad_b = int(spec.height * 0.12) + spec.caption_band_px
    glow_size = int(m * 1.1)
    blend = "screen" if dark else "normal"
    glow_alpha = 0.5 if dark else 0.3

    dom_parts: list[str] = []
    late: list[str] = []
    entrance: list[str] = []
    sub_beat: list[str] = []

    if spec.art_rel:
        dom_parts.append(
            f'      <div id="{sid}-art" class="va-art" '
            f"style=\"background-image: url('{spec.art_rel}');\"></div>"
        )
        dom_parts.append('      <div class="va-scrim"></div>')
        late.append(f"{sid}-art")
        entrance.append(
            f'  tl.fromTo("#{sid}-art", {{ scale: 1.07, y: {int(m * 0.015)} }}, '
            f"{{ autoAlpha: 1, scale: 1.0, y: -{int(m * 0.0075)}, duration: {_t(1.6 * em)}, "
            f'ease: "power2.out" }}, {_t(t0)});'
        )
        if spec.dur_s > 4:
            sub_beat.append(
                f'  tl.to("#{sid}-art", {{ scale: 1.03, y: {int(m * 0.002)}, '
                f'duration: {_t(max(1.0, spec.dur_s - 1.8))}, ease: "sine.inOut" }}, '
                f"{_t(t0 + 1.7)});"
            )
    else:
        # No-art degrade: CSS-gradient focal block (palette orb + ring).
        focal_size = int(m * 0.62)
        if spec.vertical:
            focal_pos = f"left: {int((spec.width - focal_size) / 2)}px; top: {int(spec.height * 0.10)}px;"
        else:
            focal_pos = f"right: {int(spec.width * 0.10)}px; top: {int(spec.height * 0.12)}px;"
        dom_parts.append(
            f'      <div id="{sid}-focal" class="vh-focal" style="{focal_pos}"></div>'
        )
        late.append(f"{sid}-focal")
        entrance.append(
            f'  tl.fromTo("#{sid}-focal", {{ scale: 0.82 }}, '
            f"{{ autoAlpha: 1, scale: 1.0, duration: {_t(1.2 * em)}, "
            f'ease: "power2.out" }}, {_t(t0 + 0.05)});'
        )
        if spec.dur_s > 4:
            sub_beat.append(
                f'  tl.to("#{sid}-focal", {{ scale: 1.08, rotation: 6, '
                f'duration: {_t(max(1.0, spec.dur_s - 1.6))}, ease: "sine.inOut" }}, '
                f"{_t(t0 + 1.4)});"
            )

    dom_parts.append(f'      <div id="{sid}-glowa" class="vh-glow"></div>')
    late.append(f"{sid}-glowa")
    entrance.append(
        f'  tl.fromTo("#{sid}-glowa", {{ scale: 0.86 }}, '
        f"{{ autoAlpha: 1, scale: 1.06, duration: {_t(1.1 * em)}, "
        f'ease: "power2.out" }}, {_t(t0 + 0.1)});'
    )
    if spec.dur_s > 4:
        sub_beat.append(
            f'  tl.to("#{sid}-glowa", {{ scale: 1.12, duration: 3.0, '
            f'ease: "sine.inOut" }}, {_t(t0 + 1.3)});'
        )

    spans, word_lines, _word_ids = _kinetic_words(sid, headline, t0 + 0.55, m=m)
    text_parts = ['      <div class="vh-text">']
    if eyebrow:
        if typed:
            text_parts.append(
                f'        <div id="{sid}-eyebrow" class="va-eyebrow vh-typed">'
                f'<span id="{sid}-eyetext"></span>'
                f'<span id="{sid}-caret" class="vh-caret"></span></div>'
            )
        else:
            text_parts.append(
                f'        <div id="{sid}-eyebrow" class="va-eyebrow">{_esc(eyebrow)}</div>'
            )
    text_parts.append(f'        <div id="{sid}-headline" class="vh-headline">{spans}</div>')
    if subhead:
        text_parts.append(f'        <div id="{sid}-subhead" class="va-sub">{_esc(subhead)}</div>')
    if cta:
        text_parts.append(f'        <div id="{sid}-cta" class="vh-cta">{_esc(cta)}</div>')
    text_parts.append("      </div>")
    dom_parts.append("\n".join(text_parts))

    if eyebrow:
        late.append(f"{sid}-eyebrow")
        if typed:
            text = eyebrow[:24]
            entrance.append(f'  tl.set("#{sid}-eyebrow", {{ autoAlpha: 1 }}, {_t(t0 + 0.2)});')
            entrance.extend(_typed_text_js(sid, "eyetext", text, t0 + 0.25, 0.045))
            entrance.append(
                f'  tl.to("#{sid}-caret", {{ opacity: 0, duration: 0.34, repeat: 9, '
                f'yoyo: true, ease: "steps(1)" }}, {_t(t0 + 0.3)});'
            )
            entrance.append(
                f'  tl.set("#{sid}-caret", {{ opacity: 0 }}, '
                f"{_t(t0 + 0.25 + len(text) * 0.045 + 0.8)});"
            )
        else:
            entrance.append(f'  tl.set("#{sid}-eyebrow", {{ y: {int(m * 0.026)} }}, 0);')
            entrance.append(
                f'  tl.to("#{sid}-eyebrow", {{ autoAlpha: 1, y: 0, duration: {_t(0.5 * em)}, '
                f'ease: "{ease}" }}, {_t(t0 + 0.15)});'
            )
    late.append(f"{sid}-headline")
    entrance.append(f'  tl.set("#{sid}-headline", {{ autoAlpha: 1 }}, {_t(t0 + 0.5)});')
    entrance.extend(word_lines)
    if subhead:
        late.append(f"{sid}-subhead")
        entrance.append(f'  tl.set("#{sid}-subhead", {{ y: {int(m * 0.022)} }}, 0);')
        entrance.append(
            f'  tl.to("#{sid}-subhead", {{ autoAlpha: 1, y: 0, duration: {_t(0.55 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + 1.0)});'
        )
    if cta:
        late.append(f"{sid}-cta")
        entrance.append(f'  tl.set("#{sid}-cta", {{ y: {int(m * 0.020)} }}, 0);')
        entrance.append(
            f'  tl.to("#{sid}-cta", {{ autoAlpha: 1, y: 0, duration: {_t(0.5 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + 1.2)});'
        )

    fonts = design["fonts"]
    weight = int(fonts.get("display_weight", 800))
    css = f"""      .vh-text {{ position: absolute; z-index: 4; left: {pad_x}px; bottom: {pad_b}px; max-width: {int(spec.width * 0.84)}px; display: flex; flex-direction: column; gap: {int(m * 0.020)}px; }}
      .vh-headline {{ font-family: "{fonts['display']}", serif; font-size: {int(m * 0.096)}px; font-weight: {weight}; line-height: 1.02; color: {palette['fg']};{_headline_extra(design)} }}
      .vh-cta {{ font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.030)}px; font-weight: 600; color: {accent}; }}
      .vh-glow {{ position: absolute; z-index: 2; width: {glow_size}px; height: {glow_size}px; right: {int(spec.width * 0.04)}px; top: -{int(m * 0.18)}px; border-radius: 50%; pointer-events: none; mix-blend-mode: {blend}; background: radial-gradient(circle, {_rgba(accent, glow_alpha)} 0%, {_rgba(accent, 0.16)} 38%, {_rgba(accent, 0)} 70%); }}
      .vh-focal {{ position: absolute; z-index: 1; width: {int(m * 0.62)}px; height: {int(m * 0.62)}px; border-radius: 50%; border: 2px solid {accent_dim}; background: radial-gradient(circle at 34% 30%, {accent_dim} 0%, {_rgba(accent, 0.18)} 48%, {_rgba(bg, 0)} 74%); }}
      .vh-typed {{ display: flex; align-items: center; min-height: {int(m * 0.035)}px; }}
      .vh-caret {{ display: inline-block; width: {max(2, int(m * 0.012))}px; height: {int(m * 0.028)}px; margin-left: 4px; background: {accent}; box-shadow: 0 0 {int(m * 0.011)}px {_rgba(accent, 0.7)}; }}
{_common_css(design, spec)}"""

    return SceneFragment(
        dom="\n".join(dom_parts),
        css=css,
        css_key="k-hero",
        late_ids=late,
        entrance_js=entrance,
        sub_beat_js=sub_beat,
        transition_pref="auto",
    )


def _build_stat(beat: Any, design: dict, spec: SceneSpec) -> SceneFragment:
    palette = design["palette"]
    fonts = design["fonts"]
    flags = design.get("flags", {}) or {}
    m, sid = spec.m, spec.sid
    fg, accent, accent_dim = palette["fg"], palette["accent"], palette["accent_dim"]
    ease = _ease(design)
    em = _energy_mult(spec.energy)
    t0 = spec.start_s
    eyebrow = str(getattr(beat, "eyebrow", "") or "")
    subhead = str(getattr(beat, "subhead", "") or "")
    value, label = _stat_fields(beat)
    value = (value or str(getattr(beat, "headline", "") or ""))[:24]
    label = (label or subhead)[:48]
    show_sub = bool(subhead) and subhead != label

    # vertical recenters; widescreen keeps the wallpaper poster left-aligned.
    align = "center" if spec.vertical else "flex-start"
    text_align = "center" if spec.vertical else "left"
    pad_x = int(spec.width * (0.08 if spec.vertical else 0.099))

    shadow = ""
    if flags.get("stacked_text_shadow"):
        shadow = f" text-shadow: 0.045em 0.045em 0 {accent_dim};"
    elif flags.get("offset_shadow"):
        off = max(3, m // 160)
        shadow = f" text-shadow: {off}px {off}px 0 {fg};"

    parts = [f'      <div class="vst-wrap">']
    if eyebrow:
        parts.append(f'        <div id="{sid}-eyebrow" class="va-eyebrow">{_esc(eyebrow)}</div>')
    parts.append(f'        <div id="{sid}-stat" class="vst-value">{_esc(value)}</div>')
    if label:
        parts.append(f'        <div id="{sid}-statlabel" class="vst-label">{_esc(label)}</div>')
    if show_sub:
        parts.append(f'        <div id="{sid}-subhead" class="va-sub">{_esc(subhead)}</div>')
    parts.append("      </div>")
    dom = _art_backdrop(spec) + "\n".join(parts)

    weight = int(fonts.get("display_weight", 800))
    css = f"""      .vst-wrap {{ position: absolute; inset: 0; z-index: 4; display: flex; flex-direction: column; justify-content: center; align-items: {align}; text-align: {text_align}; gap: {int(m * 0.018)}px; padding: {int(spec.height * 0.08)}px {pad_x}px {int(spec.height * 0.08) + spec.caption_band_px}px; }}
      .vst-value {{ font-family: "{fonts['display']}", serif; font-size: {int(m * 0.30)}px; font-weight: {weight}; line-height: 0.95; color: {accent};{shadow} }}
      .vst-label {{ font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.030)}px; font-weight: 600; letter-spacing: 0.22em; text-transform: uppercase; color: {fg}; }}
{_common_css(design, spec)}"""

    late: list[str] = []
    entrance: list[str] = []
    if eyebrow:
        late.append(f"{sid}-eyebrow")
        entrance.append(f'  tl.set("#{sid}-eyebrow", {{ y: {int(m * 0.026)} }}, 0);')
        entrance.append(
            f'  tl.to("#{sid}-eyebrow", {{ autoAlpha: 1, y: 0, duration: {_t(0.5 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + 0.10)});'
        )
    late.append(f"{sid}-stat")
    entrance.append(
        f'  tl.fromTo("#{sid}-stat", {{ scale: 0.72 }}, '
        f"{{ autoAlpha: 1, scale: 1, duration: {_t(0.6 * em)}, "
        f'ease: "{ease}" }}, {_t(t0 + 0.18)});'
    )
    if label:
        late.append(f"{sid}-statlabel")
        entrance.append(f'  tl.set("#{sid}-statlabel", {{ y: {int(m * 0.020)} }}, 0);')
        entrance.append(
            f'  tl.to("#{sid}-statlabel", {{ autoAlpha: 1, y: 0, duration: {_t(0.5 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + 0.46)});'
        )
    if show_sub:
        late.append(f"{sid}-subhead")
        entrance.append(f'  tl.set("#{sid}-subhead", {{ y: {int(m * 0.020)} }}, 0);')
        entrance.append(
            f'  tl.to("#{sid}-subhead", {{ autoAlpha: 1, y: 0, duration: {_t(0.5 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + 0.62)});'
        )

    sub_beat: list[str] = []
    if spec.dur_s > 4:
        mid = t0 + max(1.6, spec.dur_s * 0.45)
        sub_beat.append(
            f'  tl.to("#{sid}-stat", {{ scale: 1.05, duration: 0.32, yoyo: true, '
            f'repeat: 1, ease: "sine.inOut" }}, {_t(mid)});'
        )
        if not shadow:
            # Glow pulse only when no flag shadow is parked on the value
            # (a textShadow tween would stomp the flag chrome on yoyo-out).
            sub_beat.append(
                f'  tl.fromTo("#{sid}-stat", {{ textShadow: "0 0 0px {_rgba(accent, 0)}" }}, '
                f'{{ textShadow: "0 0 {int(m * 0.045)}px {_rgba(accent, 0.6)}", duration: 0.6, '
                f'yoyo: true, repeat: 1, ease: "sine.inOut" }}, {_t(mid + 0.1)});'
            )

    return SceneFragment(
        dom=dom,
        css=css,
        css_key="k-stat",
        late_ids=late,
        entrance_js=entrance,
        sub_beat_js=sub_beat,
        transition_pref="whip",
    )


def _build_list(beat: Any, design: dict, spec: SceneSpec) -> SceneFragment:
    palette = design["palette"]
    fonts = design["fonts"]
    m, sid = spec.m, spec.sid
    bg, fg, accent = palette["bg"], palette["fg"], palette["accent"]
    ease = _ease(design)
    em = _energy_mult(spec.energy)
    t0 = spec.start_s
    eyebrow = str(getattr(beat, "eyebrow", "") or "")
    headline = str(getattr(beat, "headline", "") or "")
    subhead = str(getattr(beat, "subhead", "") or "")
    items = _item_fields(beat)
    if not items:
        items = [(subhead or headline, "")]

    pad_x = int(spec.width * 0.099)
    chips: list[str] = ['      <div class="vli-stack">']
    for i, (title, detail) in enumerate(items):
        inner = f'<div class="vli-k">{_esc(title[:40])}</div>'
        if detail:
            inner += f'<div class="vli-v">{_esc(detail[:64])}</div>'
        chips.append(
            f'        <div id="{sid}-chip{i}" class="vli-chip">'
            f'<div class="vli-dot"></div><div class="vli-txt">{inner}</div></div>'
        )
    chips.append("      </div>")
    dom = (
        _art_backdrop(spec)
        + _header_dom(sid, "vli-headwrap", eyebrow, headline, subhead if spec.vertical else "")
        + "\n"
        + "\n".join(chips)
    )

    if spec.vertical:
        stack_pos = f"left: {pad_x}px; right: {pad_x}px; top: {int(spec.height * 0.40)}px;"
        head_pos = f"left: {pad_x}px; right: {pad_x}px; top: {int(spec.height * 0.10)}px;"
    else:
        stack_pos = f"right: {pad_x}px; top: {int(spec.height * 0.24)}px; width: {int(spec.width * 0.36)}px;"
        head_pos = f"left: {pad_x}px; top: {int(spec.height * 0.20)}px; max-width: {int(spec.width * 0.44)}px;"

    css = f"""      .vli-headwrap {{ position: absolute; z-index: 4; {head_pos} display: flex; flex-direction: column; gap: {int(m * 0.018)}px; }}
      .vli-stack {{ position: absolute; z-index: 4; {stack_pos} display: flex; flex-direction: column; gap: {int(m * 0.024)}px; }}
      .vli-chip {{ display: flex; align-items: center; gap: {int(m * 0.020)}px; {panel_chrome_css(design, m)} }}
      .vli-dot {{ width: {int(m * 0.022)}px; height: {int(m * 0.022)}px; flex-shrink: 0; border-radius: {_marker_radius(design, m)}; background: {accent}; }}
      .vli-txt {{ display: flex; flex-direction: column; gap: {int(m * 0.004)}px; }}
      .vli-k {{ font-family: "{fonts['body']}", sans-serif; font-size: {int(m * 0.030)}px; font-weight: 600; color: {fg}; }}
      .vli-v {{ font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.020)}px; letter-spacing: 0.06em; color: {_muted(design)}; }}
{_common_css(design, spec)}"""

    entrance, late = _header_js(
        sid, design, spec, has_eyebrow=bool(eyebrow), has_subhead=bool(subhead) and spec.vertical
    )
    n = len(items)
    step = 0.15 if n <= 1 else min(0.15, 0.45 / (n - 1))
    slide = int(m * 0.055)
    for i in range(n):
        cid = f"{sid}-chip{i}"
        late.append(cid)
        entrance.append(
            f'  tl.fromTo("#{cid}", {{ x: {slide} }}, '
            f"{{ autoAlpha: 1, x: 0, duration: {_t(0.55 * em)}, "
            f'ease: "{ease}" }}, {_t(t0 + 0.5 + i * step)});'
        )

    sub_beat: list[str] = []
    if spec.dur_s > 4:
        mid = t0 + max(1.8, spec.dur_s * 0.5)
        sub_beat.append(
            f'  tl.to("#{sid}-chip0 .vli-dot", {{ scale: 1.35, duration: 0.3, yoyo: true, '
            f'repeat: 1, ease: "sine.inOut" }}, {_t(mid)});'
        )

    return SceneFragment(
        dom=dom,
        css=css,
        css_key="k-list",
        late_ids=late,
        entrance_js=entrance,
        sub_beat_js=sub_beat,
        transition_pref="auto",
    )


def _build_quote(beat: Any, design: dict, spec: SceneSpec) -> SceneFragment:
    palette = design["palette"]
    fonts = design["fonts"]
    m, sid = spec.m, spec.sid
    accent = palette["accent"]
    ease = _ease(design)
    em = _energy_mult(spec.energy)
    t0 = spec.start_s
    headline = str(getattr(beat, "headline", "") or "")
    attrib = str(getattr(beat, "subhead", "") or "") or str(getattr(beat, "eyebrow", "") or "")

    spans, word_lines, _ids = _kinetic_words(sid, headline, t0 + 0.3, m=m)
    parts = ['      <div class="vq-wrap">']
    parts.append(f'        <div id="{sid}-quote" class="vq-line">{spans}</div>')
    if attrib:
        parts.append(f'        <div id="{sid}-attrib" class="vq-attrib">{_esc(attrib)}</div>')
    parts.append("      </div>")
    dom = _art_backdrop(spec) + "\n".join(parts)

    pad_x = int(spec.width * (0.10 if spec.vertical else 0.14))
    weight = int(fonts.get("display_weight", 800))
    css = f"""      .vq-wrap {{ position: absolute; inset: 0; z-index: 4; display: flex; flex-direction: column; justify-content: center; gap: {int(m * 0.030)}px; padding: {int(spec.height * 0.10)}px {pad_x}px {int(spec.height * 0.10) + spec.caption_band_px}px; }}
      .vq-line {{ font-family: "{fonts['display']}", serif; font-size: {int(m * 0.066)}px; font-weight: {weight}; line-height: 1.14; color: {palette['fg']};{_headline_extra(design)} }}
      .vq-line::before {{ content: "\\201C"; color: {accent}; margin-right: 0.12em; }}
      .vq-attrib {{ font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.026)}px; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase; color: {accent}; }}
{_common_css(design, spec)}"""

    late = [f"{sid}-quote"]
    entrance = [f'  tl.set("#{sid}-quote", {{ autoAlpha: 1 }}, {_t(t0 + 0.25)});']
    entrance.extend(word_lines)
    if attrib:
        late.append(f"{sid}-attrib")
        entrance.append(f'  tl.set("#{sid}-attrib", {{ y: {int(m * 0.020)} }}, 0);')
        entrance.append(
            f'  tl.to("#{sid}-attrib", {{ autoAlpha: 1, y: 0, duration: {_t(0.5 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + 0.95)});'
        )

    sub_beat: list[str] = []
    if spec.dur_s > 4:
        sub_beat.append(
            f'  tl.to("#{sid}-quote", {{ scale: 1.02, transformOrigin: "center center", '
            f'duration: {_t(max(1.0, spec.dur_s - 1.6))}, ease: "sine.inOut" }}, '
            f"{_t(t0 + 1.4)});"
        )

    return SceneFragment(
        dom=dom,
        css=css,
        css_key="k-quote",
        late_ids=late,
        entrance_js=entrance,
        sub_beat_js=sub_beat,
        transition_pref="crossfade",
    )


def _build_cards(beat: Any, design: dict, spec: SceneSpec) -> SceneFragment:
    palette = design["palette"]
    fonts = design["fonts"]
    m, sid = spec.m, spec.sid
    fg, accent = palette["fg"], palette["accent"]
    ease = _ease(design)
    em = _energy_mult(spec.energy)
    t0 = spec.start_s
    eyebrow = str(getattr(beat, "eyebrow", "") or "")
    headline = str(getattr(beat, "headline", "") or "")
    items = _item_fields(beat)
    if not items:
        items = [(headline, str(getattr(beat, "subhead", "") or ""))]

    cards: list[str] = ['      <div class="vcd-row">']
    for i, (title, detail) in enumerate(items):
        inner = f'<div class="vcd-strip"></div><div class="vcd-title">{_esc(title[:40])}</div>'
        if detail:
            inner += f'<div class="vcd-detail">{_esc(detail[:64])}</div>'
        inner += f'<div id="{sid}-ring{i}" class="vcd-ring"></div>'
        cards.append(f'        <div id="{sid}-card{i}" class="vcd-card">{inner}</div>')
    cards.append("      </div>")
    dom = (
        _art_backdrop(spec)
        + _header_dom(sid, "vcd-headwrap", eyebrow, headline, "")
        + "\n"
        + "\n".join(cards)
    )

    direction = "column" if spec.vertical else "row"
    if spec.vertical:
        row_pos = (
            f"left: {int(spec.width * 0.08)}px; right: {int(spec.width * 0.08)}px; "
            f"top: {int(spec.height * 0.34)}px;"
        )
        card_w = "width: 100%;"
    else:
        row_pos = f"left: 50%; top: {int(spec.height * 0.56)}px; transform: translateX(-50%);"
        card_w = f"width: {int(spec.width * 0.20)}px;"
    head_pos = (
        f"left: {int(spec.width * 0.08)}px; right: {int(spec.width * 0.08)}px; "
        f"top: {int(spec.height * 0.12)}px; text-align: center; align-items: center;"
    )

    css = f"""      .vcd-headwrap {{ position: absolute; z-index: 4; {head_pos} display: flex; flex-direction: column; gap: {int(m * 0.016)}px; }}
      .vcd-row {{ position: absolute; z-index: 4; {row_pos} display: flex; flex-direction: {direction}; gap: {int(m * 0.037)}px; }}
      .vcd-card {{ position: relative; overflow: hidden; {card_w} {panel_chrome_css(design, m)} }}
      .vcd-strip {{ width: {int(m * 0.042)}px; height: {max(3, int(m * 0.006))}px; border-radius: {_marker_radius(design, m)}; background: {accent}; margin-bottom: {int(m * 0.014)}px; }}
      .vcd-title {{ font-family: "{fonts['display']}", serif; font-size: {int(m * 0.032)}px; font-weight: {int(fonts.get('display_weight', 800))}; color: {fg}; }}
      .vcd-detail {{ font-family: "{fonts['body']}", sans-serif; font-size: {int(m * 0.020)}px; line-height: 1.4; color: {_muted(design)}; margin-top: {int(m * 0.008)}px; }}
      .vcd-ring {{ position: absolute; inset: 0; border-radius: inherit; border: 2px solid {accent}; box-shadow: 0 0 {int(m * 0.033)}px {_rgba(accent, 0.45)}, inset 0 0 {int(m * 0.028)}px {_rgba(accent, 0.18)}; opacity: 0; pointer-events: none; }}
{_common_css(design, spec)}"""

    entrance, late = _header_js(sid, design, spec, has_eyebrow=bool(eyebrow), has_subhead=False)
    n = len(items)
    step = 0.15 if n <= 1 else min(0.15, 0.45 / (n - 1))
    rise = int(m * 0.065)
    for i in range(n):
        cid = f"{sid}-card{i}"
        late.append(cid)
        entrance.append(
            f'  tl.fromTo("#{cid}", {{ y: {rise}, scale: 0.92 }}, '
            f"{{ autoAlpha: 1, y: 0, scale: 1, duration: {_t(0.6 * em)}, "
            f'ease: "{ease}" }}, {_t(t0 + 0.5 + i * step)});'
        )

    # Highlight-ring cycle (the model-swap socket, absorbed as a sub-beat).
    sub_beat: list[str] = []
    if spec.dur_s >= 3.4 and n >= 1:
        win_start = t0 + 1.6
        window = max(0.7 * n, spec.dur_s - 2.2)
        seg = max(0.7, window / n)
        for i in range(n):
            ti = win_start + i * seg
            sub_beat.append(
                f'  tl.fromTo("#{sid}-ring{i}", {{ opacity: 0 }}, '
                f'{{ opacity: 1, duration: 0.25, ease: "power2.out" }}, {_t(ti)});'
            )
            sub_beat.append(
                f'  tl.to("#{sid}-ring{i}", {{ opacity: 0, duration: 0.25, '
                f'ease: "power2.in" }}, {_t(ti + seg * 0.6)});'
            )

    return SceneFragment(
        dom=dom,
        css=css,
        css_key="k-cards",
        late_ids=late,
        entrance_js=entrance,
        sub_beat_js=sub_beat,
        transition_pref="dip",
    )


def _build_ledger(beat: Any, design: dict, spec: SceneSpec) -> SceneFragment:
    palette = design["palette"]
    fonts = design["fonts"]
    m, sid = spec.m, spec.sid
    bg, fg, accent = palette["bg"], palette["fg"], palette["accent"]
    ease = _ease(design)
    em = _energy_mult(spec.energy)
    t0 = spec.start_s
    eyebrow = str(getattr(beat, "eyebrow", "") or "")
    headline = str(getattr(beat, "headline", "") or "")
    subhead = str(getattr(beat, "subhead", "") or "")
    items = _item_fields(beat)
    if not items:
        items = [(subhead or headline, "")]

    rows: list[str] = []
    for i, (title, detail) in enumerate(items):
        tag = _esc((detail or f"{i + 1:02d}")[:24])
        rows.append(
            f'          <div id="{sid}-row{i}" class="vlg-row">'
            f'<div class="vlg-tick"></div>'
            f'<div class="vlg-txt">{_esc(title[:48])}</div>'
            f'<div class="vlg-tag">{tag}</div></div>'
        )
    bar_label = _esc((eyebrow or "LOG")[:24])
    panel = (
        f'      <div id="{sid}-ledger" class="vlg-panel">\n'
        f'        <div class="vlg-bar"><span class="vlg-d1"></span>'
        f'<span class="vlg-d2"></span><span class="vlg-d3"></span>'
        f'<span class="vlg-file">{bar_label}</span></div>\n'
        f'        <div class="vlg-body">\n' + "\n".join(rows) + "\n        </div>\n"
        f"      </div>"
    )
    dom = (
        _art_backdrop(spec)
        + _header_dom(sid, "vlg-headwrap", eyebrow, headline, subhead if not spec.vertical else "")
        + "\n"
        + panel
    )

    pad_x = int(spec.width * 0.099)
    if spec.vertical:
        panel_pos = f"left: {int(spec.width * 0.07)}px; right: {int(spec.width * 0.07)}px; top: {int(spec.height * 0.36)}px;"
        head_pos = f"left: {pad_x}px; right: {pad_x}px; top: {int(spec.height * 0.10)}px;"
    else:
        panel_pos = f"right: {pad_x}px; top: {int(spec.height * 0.22)}px; width: {int(spec.width * 0.46)}px;"
        head_pos = f"left: {pad_x}px; top: {int(spec.height * 0.22)}px; max-width: {int(spec.width * 0.36)}px;"
    surface = blend_hex(bg, fg, 0.05)
    line = blend_hex(bg, fg, 0.16)

    css = f"""      .vlg-headwrap {{ position: absolute; z-index: 4; {head_pos} display: flex; flex-direction: column; gap: {int(m * 0.018)}px; }}
      .vlg-panel {{ position: absolute; z-index: 4; {panel_pos} background: {surface}; border: 1px solid {line}; border-radius: {int(m * 0.015)}px; overflow: hidden; }}
      .vlg-bar {{ height: {int(m * 0.048)}px; display: flex; align-items: center; gap: {int(m * 0.008)}px; padding: 0 {int(m * 0.018)}px; background: {blend_hex(bg, fg, 0.10)}; border-bottom: 1px solid {line}; }}
      .vlg-d1, .vlg-d2, .vlg-d3 {{ width: {int(m * 0.011)}px; height: {int(m * 0.011)}px; border-radius: 50%; }}
      .vlg-d1 {{ background: {_muted(design)}; }}
      .vlg-d2 {{ background: {_rgba(accent, 0.5)}; }}
      .vlg-d3 {{ background: {accent}; }}
      .vlg-file {{ margin-left: {int(m * 0.012)}px; font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.016)}px; letter-spacing: 0.12em; text-transform: uppercase; color: {_muted(design)}; }}
      .vlg-body {{ padding: {int(m * 0.020)}px {int(m * 0.024)}px; display: flex; flex-direction: column; gap: {int(m * 0.014)}px; }}
      .vlg-row {{ display: flex; align-items: center; gap: {int(m * 0.016)}px; padding: {int(m * 0.014)}px {int(m * 0.018)}px; border-radius: {int(m * 0.011)}px; background: {_rgba(accent, 0.05)}; border: 1px solid {_rgba(accent, 0.12)}; }}
      .vlg-tick {{ width: {int(m * 0.020)}px; height: {int(m * 0.020)}px; flex-shrink: 0; border-radius: {_marker_radius(design, m)}; background: {accent}; }}
      .vlg-txt {{ font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.022)}px; color: {fg}; }}
      .vlg-tag {{ margin-left: auto; font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.016)}px; letter-spacing: 0.14em; text-transform: uppercase; color: {accent}; }}
{_common_css(design, spec)}"""

    entrance, late = _header_js(
        sid, design, spec, has_eyebrow=bool(eyebrow), has_subhead=bool(subhead) and not spec.vertical
    )
    late.append(f"{sid}-ledger")
    entrance.append(
        f'  tl.fromTo("#{sid}-ledger", {{ y: {int(m * 0.055)}, scale: 0.985 }}, '
        f"{{ autoAlpha: 1, y: 0, scale: 1, duration: {_t(0.6 * em)}, "
        f'ease: "{ease}" }}, {_t(t0 + 0.4)});'
    )
    n = len(items)
    step = 0.15 if n <= 1 else min(0.15, 0.45 / (n - 1))
    slide = int(m * 0.037)
    for i in range(n):
        rid = f"{sid}-row{i}"
        late.append(rid)
        entrance.append(
            f'  tl.fromTo("#{rid}", {{ x: -{slide} }}, '
            f"{{ autoAlpha: 1, x: 0, duration: {_t(0.5 * em)}, "
            f'ease: "{ease}" }}, {_t(t0 + 0.85 + i * step)});'
        )

    sub_beat: list[str] = []
    if spec.dur_s > 4:
        mid = t0 + max(2.0, spec.dur_s * 0.55)
        sub_beat.append(
            f'  tl.to("#{sid}-row0 .vlg-tick", {{ scale: 1.4, duration: 0.3, yoyo: true, '
            f'repeat: 1, ease: "sine.inOut" }}, {_t(mid)});'
        )

    return SceneFragment(
        dom=dom,
        css=css,
        css_key="k-ledger",
        late_ids=late,
        entrance_js=entrance,
        sub_beat_js=sub_beat,
        transition_pref="auto",
    )


def _build_mockup(beat: Any, design: dict, spec: SceneSpec) -> SceneFragment:
    palette = design["palette"]
    fonts = design["fonts"]
    m, sid = spec.m, spec.sid
    bg, fg, accent, accent_dim = (
        palette["bg"],
        palette["fg"],
        palette["accent"],
        palette["accent_dim"],
    )
    ease = _ease(design)
    em = _energy_mult(spec.energy)
    t0, dur = spec.start_s, max(2.4, spec.dur_s)
    eyebrow = str(getattr(beat, "eyebrow", "") or "")
    headline = str(getattr(beat, "headline", "") or "")
    subhead = str(getattr(beat, "subhead", "") or "")
    url_text = (str(getattr(beat, "cta", "") or "") or headline)[:36]

    browser = f"""      <div id="{sid}-browser" class="vmk-browser">
        <div class="vmk-bar"><span class="vmk-d1"></span><span class="vmk-d2"></span><span class="vmk-d3"></span>
          <span class="vmk-url"><span id="{sid}-addr" class="vmk-addr"></span><span id="{sid}-urlcaret" class="vmk-caret"></span></span>
        </div>
        <div class="vmk-body">
          <div id="{sid}-progress" class="vmk-progress"></div>
          <div class="vmk-page">
            <div id="{sid}-page1" class="vmk-state">
              <div class="vmk-b vmk-h1"></div><div class="vmk-b vmk-r1"></div><div class="vmk-b vmk-r2"></div><div class="vmk-b vmk-r3"></div><div class="vmk-b vmk-cta"></div>
            </div>
            <div id="{sid}-page2" class="vmk-state">
              <div class="vmk-b vmk-h2"></div><div class="vmk-b vmk-r1"></div><div class="vmk-b vmk-r2"></div><div class="vmk-b vmk-card"></div>
            </div>
          </div>
        </div>
      </div>"""
    dom = (
        _art_backdrop(spec)
        + _header_dom(sid, "vmk-headwrap", eyebrow, headline, subhead)
        + "\n"
        + browser
    )

    pad_x = int(spec.width * 0.099)
    if spec.vertical:
        bw = int(spec.width * 0.86)
        browser_pos = f"left: {int(spec.width * 0.07)}px; top: {int(spec.height * 0.30)}px; width: {bw}px;"
        head_pos = f"left: {pad_x}px; right: {pad_x}px; top: {int(spec.height * 0.08)}px;"
    else:
        bw = int(spec.width * 0.52)
        browser_pos = f"right: {pad_x}px; top: {int(spec.height * 0.16)}px; width: {bw}px;"
        head_pos = f"left: {pad_x}px; top: {int(spec.height * 0.20)}px; max-width: {int(spec.width * 0.30)}px;"
    body_h = int(bw * 0.46)
    block = _rgba(fg, 0.08)
    block_strong = _rgba(fg, 0.13)
    surface = blend_hex(bg, fg, 0.05)
    line = blend_hex(bg, fg, 0.16)

    css = f"""      .vmk-headwrap {{ position: absolute; z-index: 4; {head_pos} display: flex; flex-direction: column; gap: {int(m * 0.018)}px; }}
      .vmk-browser {{ position: absolute; z-index: 4; {browser_pos} background: {surface}; border: 1px solid {line}; border-radius: {int(m * 0.015)}px; overflow: hidden; }}
      .vmk-bar {{ height: {int(m * 0.050)}px; display: flex; align-items: center; gap: {int(m * 0.009)}px; padding: 0 {int(m * 0.018)}px; background: {blend_hex(bg, fg, 0.10)}; border-bottom: 1px solid {line}; }}
      .vmk-d1, .vmk-d2, .vmk-d3 {{ width: {int(m * 0.012)}px; height: {int(m * 0.012)}px; border-radius: 50%; }}
      .vmk-d1 {{ background: {_muted(design)}; }}
      .vmk-d2 {{ background: {_rgba(accent, 0.5)}; }}
      .vmk-d3 {{ background: {accent}; }}
      .vmk-url {{ margin-left: {int(m * 0.014)}px; flex: 1; height: {int(m * 0.032)}px; border-radius: {int(m * 0.0075)}px; background: {surface}; border: 1px solid {_rgba(accent, 0.14)}; display: flex; align-items: center; padding: 0 {int(m * 0.014)}px; font-family: "{fonts['mono']}", monospace; font-size: {int(m * 0.0165)}px; color: {blend_hex(accent, fg, 0.35)}; }}
      .vmk-addr {{ white-space: nowrap; }}
      .vmk-caret {{ display: inline-block; width: 2px; height: {int(m * 0.020)}px; margin-left: 1px; background: {accent}; box-shadow: 0 0 {int(m * 0.0075)}px {_rgba(accent, 0.7)}; vertical-align: middle; opacity: 0; }}
      .vmk-body {{ position: relative; height: {body_h}px; background: {blend_hex(bg, fg, 0.03)}; overflow: hidden; }}
      .vmk-progress {{ position: absolute; top: 0; left: 0; height: 3px; width: 0%; background: linear-gradient(90deg, {accent}, {accent_dim}); box-shadow: 0 0 {int(m * 0.011)}px {_rgba(accent, 0.7)}; z-index: 5; opacity: 0; }}
      .vmk-page {{ position: absolute; inset: {int(m * 0.020)}px; border-radius: {int(m * 0.009)}px; background: {surface}; border: 1px solid {_rgba(fg, 0.05)}; overflow: hidden; }}
      .vmk-state {{ position: absolute; inset: 0; }}
      #{sid}-page2 {{ opacity: 0; }}
      .vmk-b {{ position: absolute; border-radius: {max(3, int(m * 0.0055))}px; background: {block}; }}
      .vmk-h1 {{ left: 4%; top: 9%; width: 30%; height: {max(8, int(m * 0.024))}px; background: {block_strong}; }}
      .vmk-h2 {{ left: 4%; top: 9%; width: 36%; height: {max(8, int(m * 0.024))}px; background: {_rgba(accent, 0.18)}; }}
      .vmk-r1 {{ left: 4%; top: 33%; width: 56%; height: {max(6, int(m * 0.015))}px; }}
      .vmk-r2 {{ left: 4%; top: 45%; width: 50%; height: {max(6, int(m * 0.015))}px; }}
      .vmk-r3 {{ left: 4%; top: 57%; width: 54%; height: {max(6, int(m * 0.015))}px; }}
      .vmk-cta {{ left: 4%; top: 72%; width: 22%; height: {max(12, int(m * 0.050))}px; background: {_rgba(accent, 0.16)}; border: 1px solid {_rgba(accent, 0.34)}; }}
      .vmk-card {{ left: 4%; top: 62%; width: 60%; height: {max(14, int(m * 0.062))}px; background: {_rgba(fg, 0.05)}; border: 1px solid {_rgba(accent, 0.16)}; }}
{_common_css(design, spec)}"""

    entrance, late = _header_js(sid, design, spec, has_eyebrow=bool(eyebrow), has_subhead=bool(subhead))
    late.append(f"{sid}-browser")
    entrance.append(
        f'  tl.fromTo("#{sid}-browser", {{ y: {int(m * 0.046)}, scale: 0.985 }}, '
        f"{{ autoAlpha: 1, y: 0, scale: 1, duration: {_t(0.55 * em)}, "
        f'ease: "{ease}" }}, {_t(t0 + 0.4)});'
    )

    # Drive sequence as duration fractions so any scene length fits:
    # type -> progress sweep -> page swap.
    type_start = t0 + max(0.8, dur * 0.18)
    type_step = min(0.035, (dur * 0.28) / max(1, len(url_text)))
    nav = t0 + dur * 0.55
    sub_beat: list[str] = []
    sub_beat.extend(_typed_text_js(sid, "addr", url_text, type_start, type_step))
    sub_beat.append(
        f'  tl.to("#{sid}-urlcaret", {{ opacity: 1, duration: 0.05, ease: "none" }}, {_t(type_start - 0.1)});'
    )
    sub_beat.append(
        f'  tl.to("#{sid}-urlcaret", {{ opacity: 0, duration: 0.28, repeat: 7, yoyo: true, '
        f'ease: "steps(1)" }}, {_t(type_start)});'
    )
    sub_beat.append(
        f'  tl.set("#{sid}-urlcaret", {{ opacity: 0 }}, {_t(type_start + len(url_text) * type_step + 0.5)});'
    )
    sub_beat.append(f'  tl.set("#{sid}-progress", {{ opacity: 1, width: "0%" }}, {_t(nav)});')
    sub_beat.append(
        f'  tl.to("#{sid}-progress", {{ width: "100%", duration: 0.55, ease: "power2.inOut" }}, {_t(nav)});'
    )
    sub_beat.append(
        f'  tl.to("#{sid}-progress", {{ opacity: 0, duration: 0.18, ease: "power2.in" }}, {_t(nav + 0.56)});'
    )
    sub_beat.append(
        f'  tl.to("#{sid}-page1", {{ opacity: 0, duration: 0.32, ease: "power2.inOut" }}, {_t(nav + 0.28)});'
    )
    sub_beat.append(
        f'  tl.to("#{sid}-page2", {{ opacity: 1, duration: 0.36, ease: "power2.out" }}, {_t(nav + 0.32)});'
    )

    return SceneFragment(
        dom=dom,
        css=css,
        css_key="k-mockup",
        late_ids=late,
        entrance_js=entrance,
        sub_beat_js=sub_beat,
        transition_pref="whip",
    )


def _build_payoff(beat: Any, design: dict, spec: SceneSpec) -> SceneFragment:
    palette = design["palette"]
    fonts = design["fonts"]
    m, sid = spec.m, spec.sid
    accent = palette["accent"]
    ease = _ease(design)
    em = _energy_mult(spec.energy)
    t0 = spec.start_s
    dark = _is_dark(design)
    eyebrow = str(getattr(beat, "eyebrow", "") or "")
    headline = str(getattr(beat, "headline", "") or "")
    subhead = str(getattr(beat, "subhead", "") or "")
    cta = str(getattr(beat, "cta", "") or "")

    spans, word_lines, _ids = _kinetic_words(sid, headline, t0 + 0.65, m=m)
    plate_parts = [f'        <div id="{sid}-plate" class="vpf-plate">']
    if eyebrow:
        plate_parts.append(f'          <div id="{sid}-eyebrow" class="va-eyebrow">{_esc(eyebrow)}</div>')
    plate_parts.append(f'          <div id="{sid}-headline" class="vpf-headline">{spans}</div>')
    if subhead:
        plate_parts.append(f'          <div id="{sid}-subhead" class="va-sub">{_esc(subhead)}</div>')
    if cta:
        plate_parts.append(f'          <div id="{sid}-cta" class="vpf-cta">{_esc(cta)}</div>')
    plate_parts.append("        </div>")
    dom = (
        _art_backdrop(spec)
        + f'      <div id="{sid}-glow" class="vpf-glow"></div>\n'
        + '      <div class="vpf-wrap">\n'
        + "\n".join(plate_parts)
        + "\n      </div>"
    )

    plate_w = int(spec.width * (0.84 if spec.vertical else 0.62))
    glow_size = int(m * 1.2)
    blend = "screen" if dark else "normal"
    weight = int(fonts.get("display_weight", 800))
    css = f"""      .vpf-wrap {{ position: absolute; inset: 0; z-index: 4; display: flex; align-items: center; justify-content: center; padding-bottom: {spec.caption_band_px}px; }}
      .vpf-plate {{ width: {plate_w}px; display: flex; flex-direction: column; align-items: center; text-align: center; gap: {int(m * 0.020)}px; {panel_chrome_css(design, m)} }}
      .vpf-headline {{ font-family: "{fonts['display']}", serif; font-size: {int(m * 0.072)}px; font-weight: {weight}; line-height: 1.06; color: {palette['fg']};{_headline_extra(design)} }}
      .vpf-cta {{ font-family: "{fonts['mono']}", sans-serif; font-size: {int(m * 0.032)}px; font-weight: 600; letter-spacing: 0.04em; color: {accent}; margin-top: {int(m * 0.010)}px; }}
      .vpf-glow {{ position: absolute; z-index: 1; width: {glow_size}px; height: {glow_size}px; left: 50%; top: 42%; margin-left: -{glow_size // 2}px; margin-top: -{glow_size // 2}px; border-radius: 50%; pointer-events: none; mix-blend-mode: {blend}; background: radial-gradient(circle, {_rgba(accent, 0.42 if dark else 0.26)} 0%, {_rgba(accent, 0.12)} 42%, {_rgba(accent, 0)} 72%); }}
{_common_css(design, spec)}"""

    late = [f"{sid}-glow", f"{sid}-plate", f"{sid}-headline"]
    entrance = [
        f'  tl.fromTo("#{sid}-glow", {{ scale: 0.85 }}, '
        f"{{ autoAlpha: 1, scale: 1.08, duration: {_t(1.0 * em)}, "
        f'ease: "power2.out" }}, {_t(t0 + 0.05)});',
        f'  tl.fromTo("#{sid}-plate", {{ y: {int(m * 0.33)} }}, '
        f"{{ autoAlpha: 1, y: 0, duration: {_t(0.7 * em)}, "
        f'ease: "{ease}" }}, {_t(t0 + 0.1)});',
        f'  tl.set("#{sid}-headline", {{ autoAlpha: 1 }}, {_t(t0 + 0.6)});',
    ]
    entrance.extend(word_lines)
    if eyebrow:
        late.append(f"{sid}-eyebrow")
        entrance.append(f'  tl.set("#{sid}-eyebrow", {{ y: {int(m * 0.018)} }}, 0);')
        entrance.append(
            f'  tl.to("#{sid}-eyebrow", {{ autoAlpha: 1, y: 0, duration: {_t(0.45 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + 0.5)});'
        )
    if subhead:
        late.append(f"{sid}-subhead")
        entrance.append(f'  tl.set("#{sid}-subhead", {{ y: {int(m * 0.018)} }}, 0);')
        entrance.append(
            f'  tl.to("#{sid}-subhead", {{ autoAlpha: 1, y: 0, duration: {_t(0.5 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + 1.05)});'
        )
    if cta:
        late.append(f"{sid}-cta")
        entrance.append(f'  tl.set("#{sid}-cta", {{ y: {int(m * 0.022)} }}, 0);')
        entrance.append(
            f'  tl.to("#{sid}-cta", {{ autoAlpha: 1, y: 0, duration: {_t(0.55 * em)}, '
            f'ease: "{ease}" }}, {_t(t0 + 1.2)});'
        )

    sub_beat: list[str] = []
    if spec.dur_s > 4:
        mid = t0 + max(2.2, spec.dur_s * 0.55)
        target = f"{sid}-cta" if cta else f"{sid}-headline"
        sub_beat.append(
            f'  tl.fromTo("#{target}", {{ textShadow: "0 0 0px {_rgba(accent, 0)}" }}, '
            f'{{ textShadow: "0 0 {int(m * 0.028)}px {_rgba(accent, 0.85)}", duration: 0.6, '
            f'yoyo: true, repeat: 1, ease: "sine.inOut" }}, {_t(mid)});'
        )
        sub_beat.append(
            f'  tl.to("#{sid}-glow", {{ scale: 1.16, duration: {_t(max(1.0, spec.dur_s - 2.0))}, '
            f'ease: "sine.inOut" }}, {_t(t0 + 1.2)});'
        )

    return SceneFragment(
        dom=dom,
        css=css,
        css_key="k-payoff",
        late_ids=late,
        entrance_js=entrance,
        sub_beat_js=sub_beat,
        transition_pref="dip",
    )


ARCHETYPES: dict[str, Callable[[Any, dict, SceneSpec], SceneFragment]] = {
    "hero": _build_hero,
    "stat": _build_stat,
    "list": _build_list,
    "quote": _build_quote,
    "cards": _build_cards,
    "ledger": _build_ledger,
    "mockup": _build_mockup,
    "payoff": _build_payoff,
    "caption": _build_caption,
}


# =============================================================================
# RESOLUTION + DISPATCH
# =============================================================================


def resolve_archetype(kind: str, beat: Any, design: dict, spec: SceneSpec) -> str:
    """Pick the archetype for a beat: declared kind wins, then content backfill.

    Backfill: stat content -> "stat"; >=2 items -> "ledger" (details present)
    or "cards" (titles only); first scene -> "hero"; last scene with a cta
    (or in a 3+ scene video) -> "payoff"; default -> "caption". A hero
    without art degrades inside the builder (CSS-gradient focal block).
    """

    declared = str(kind or getattr(beat, "kind", "") or "").strip().lower()
    if declared in KINDS:
        return declared

    value, _label = _stat_fields(beat)
    if value:
        return "stat"
    items = _item_fields(beat)
    if len(items) >= 2:
        if any(detail for _title, detail in items):
            return "ledger"
        return "cards"
    if spec.index == 0:
        return "hero"
    cta = str(getattr(beat, "cta", "") or "")
    if spec.index == spec.count - 1 and (cta or spec.count >= 3):
        return "payoff"
    return "caption"


def build_scene(beat: Any, design: dict, spec: SceneSpec) -> tuple[str, SceneFragment]:
    """Resolve + build one scene. Returns (resolved_kind, fragment)."""

    resolved = resolve_archetype(getattr(beat, "kind", ""), beat, design, spec)
    return resolved, ARCHETYPES[resolved](beat, design, spec)


# =============================================================================
# TEXTURE LAYER (grain / vignette / scanlines / blackout plate / fx canvas)
# =============================================================================

_GRAIN_URI = (
    "data:image/svg+xml,%3Csvg viewBox='0 0 256 256' "
    "xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence "
    "type='fractalNoise' baseFrequency='0.62' numOctaves='3' "
    "stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' "
    "height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"
)


def build_texture(design: dict, *, width: int, height: int, total_s: float) -> SceneFragment:
    """Global texture overlays. The ``#blackout`` plate is ALWAYS emitted
    (dip transitions + the final close target); grain / vignette / scanlines
    hang off style flags. Scanlines only land on dark canvases. All drift is
    finite (repeat: 0, duration == total_s)."""

    flags = design.get("flags", {}) or {}
    palette = design["palette"]
    accent = palette["accent"]
    m = min(width, height)
    dark = _is_dark(design)
    plate = _scaled_rgb(_darkest(design), 0.35)

    dom: list[str] = []
    css: list[str] = []
    js: list[str] = []

    if flags.get("shader_transitions"):
        dom.append(
            f'      <canvas id="fx-canvas" width="{width}" height="{height}"></canvas>'
        )
        css.append(
            "      #fx-canvas { position: absolute; inset: 0; z-index: 40; "
            "display: none; pointer-events: none; }"
        )

    if flags.get("grain"):
        dom.append('      <div id="tex-grain"><div id="tex-grain-tex"></div></div>')
        css.append(
            "      #tex-grain { position: absolute; inset: 0; z-index: 60; "
            "pointer-events: none; opacity: 0.5; overflow: hidden; }"
        )
        css.append(
            "      #tex-grain-tex { position: absolute; top: -50%; left: -50%; "
            f'width: 200%; height: 200%; background: url("{_GRAIN_URI}"); '
            "opacity: 0.16; mix-blend-mode: overlay; }"
        )
        js.append(
            f'  tl.to("#tex-grain-tex", {{ x: {int(m * 0.028)}, y: -{int(m * 0.022)}, '
            f'duration: {_t(total_s)}, ease: "none", repeat: 0 }}, 0);'
        )

    if flags.get("vignette"):
        alpha = 0.85 if dark else 0.30
        spread = int(m * 0.30)
        soft = int(m * 0.085)
        vr, vg, vb = hex_to_rgb(_darkest(design))
        vig = f"rgba({int(vr * 0.30)},{int(vg * 0.30)},{int(vb * 0.30)},{alpha:g})"
        css.append(
            f"      #tex-vignette {{ position: absolute; inset: 0; z-index: 55; "
            f"pointer-events: none; box-shadow: inset 0 0 {spread}px {soft}px {vig}; }}"
        )
        dom.append('      <div id="tex-vignette"></div>')

    if flags.get("hud_scanline") and dark:
        css.append(
            f"      .tex-scan {{ position: absolute; left: 0; right: 0; height: 2px; "
            f"z-index: 50; pointer-events: none; background: {_rgba(accent, 0.30)}; "
            f"box-shadow: 0 0 {int(m * 0.017)}px {_rgba(accent, 0.55)}; }}"
        )
        dom.append(
            f'      <div id="tex-scan-a" class="tex-scan" style="top: {int(height * 0.28)}px;"></div>'
        )
        dom.append(
            f'      <div id="tex-scan-b" class="tex-scan" style="top: {int(height * 0.67)}px; opacity: 0.5;"></div>'
        )
        js.append(
            f'  tl.fromTo("#tex-scan-a", {{ y: -{int(height * 0.04)} }}, '
            f'{{ y: {int(height * 0.22)}, duration: {_t(total_s)}, ease: "none", repeat: 0 }}, 0);'
        )
        js.append(
            f'  tl.fromTo("#tex-scan-b", {{ y: {int(height * 0.05)} }}, '
            f'{{ y: -{int(height * 0.18)}, duration: {_t(total_s)}, ease: "none", repeat: 0 }}, 0);'
        )

    # Always-on dip/close plate (opacity 0 natural state; dip + the composer's
    # final close tween drive it).
    dom.append('      <div id="blackout"></div>')
    css.append(
        f"      #blackout {{ position: absolute; inset: 0; z-index: 70; "
        f"background: {plate}; opacity: 0; pointer-events: none; }}"
    )

    return SceneFragment(
        dom="\n".join(dom),
        css="\n".join(css),
        css_key="texture",
        late_ids=[],
        entrance_js=js,
        sub_beat_js=[],
        transition_pref="",
    )


# =============================================================================
# TRANSITIONS
# =============================================================================


def resolve_transition(
    pref: str,
    design: dict,
    *,
    prev_kind: str = "",
    cur_kind: str = "",
) -> str:
    """Transition kind for one boundary.

    Precedence: incoming scene's archetype pref > design motion transition >
    crossfade. ``chroma_split`` survives only when the design flag
    ``shader_transitions`` is on AND both boundary archetypes are in
    RASTER_SAFE; otherwise it degrades to crossfade.
    """

    motion = design.get("motion", {}) or {}
    flags = design.get("flags", {}) or {}
    choice = str(pref or "").strip().lower()
    if choice in ("", "auto") or choice not in TRANSITIONS:
        choice = str(motion.get("transition", "") or "").strip().lower()
    if choice not in TRANSITIONS:
        choice = "crossfade"
    if choice == "chroma_split":
        if (
            not flags.get("shader_transitions")
            or prev_kind not in RASTER_SAFE
            or cur_kind not in RASTER_SAFE
        ):
            choice = "crossfade"
    return choice


_CHROMA_TEMPLATE = """  (function () {
    if (!window.__vidFx) {
      var canvas = document.getElementById("fx-canvas");
      var gl = null;
      if (canvas) { try { gl = canvas.getContext("webgl", { preserveDrawingBuffer: true }); } catch (e) { gl = null; } }
      var fx = { gl: gl, canvas: canvas, active: false, tF: null, tT: null };
      if (gl) {
        gl.viewport(0, 0, canvas.width, canvas.height);
        var quad = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, quad);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
        var compile = function (src, type) { var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
        var vert = "attribute vec2 a_pos; varying vec2 v_uv; void main(){ v_uv = a_pos * 0.5 + 0.5; v_uv.y = 1.0 - v_uv.y; gl_Position = vec4(a_pos, 0, 1); }";
        var frag = "precision mediump float; varying vec2 v_uv; uniform sampler2D u_from, u_to; uniform float u_p;"
          + " void main(){ vec2 c = v_uv - 0.5; float fs = u_p * 0.085;"
          + " float fr = texture2D(u_from, clamp(v_uv + c * fs, 0.0, 1.0)).r;"
          + " float fgc = texture2D(u_from, v_uv).g;"
          + " float fb = texture2D(u_from, clamp(v_uv - c * fs, 0.0, 1.0)).b;"
          + " float ts = (1.0 - u_p) * 0.085;"
          + " float tr = texture2D(u_to, clamp(v_uv - c * ts, 0.0, 1.0)).r;"
          + " float tg = texture2D(u_to, v_uv).g;"
          + " float tb = texture2D(u_to, clamp(v_uv + c * ts, 0.0, 1.0)).b;"
          + " vec3 col = mix(vec3(fr, fgc, fb), vec3(tr, tg, tb), u_p);"
          + " float seam = smoothstep(0.42, 0.5, u_p) * smoothstep(0.58, 0.5, u_p);"
          + " col += __SEAM__ * seam * 0.5;"
          + " gl_FragColor = vec4(col, 1.0); }";
        var prog = gl.createProgram();
        gl.attachShader(prog, compile(vert, gl.VERTEX_SHADER));
        gl.attachShader(prog, compile(frag, gl.FRAGMENT_SHADER));
        gl.linkProgram(prog);
        fx.raster = function (el) {
          var c = document.createElement("canvas");
          c.width = canvas.width; c.height = canvas.height;
          var ctx = c.getContext("2d");
          var base = window.getComputedStyle(el).backgroundColor;
          ctx.fillStyle = (base && base !== "rgba(0, 0, 0, 0)") ? base : "__BG__";
          ctx.fillRect(0, 0, c.width, c.height);
          var sr = el.getBoundingClientRect();
          var els = el.querySelectorAll("*");
          for (var i = 0; i < els.length; i++) {
            var node = els[i], cs = window.getComputedStyle(node);
            if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0) { continue; }
            var r = node.getBoundingClientRect();
            if (r.width < 1 || r.height < 1) { continue; }
            var x = r.left - sr.left, y = r.top - sr.top;
            ctx.save();
            ctx.globalAlpha = Math.min(1, parseFloat(cs.opacity) || 1);
            var nbg = cs.backgroundColor;
            if (nbg && nbg !== "rgba(0, 0, 0, 0)" && nbg !== "transparent") { ctx.fillStyle = nbg; ctx.fillRect(x, y, r.width, r.height); }
            var text = "";
            for (var j = 0; j < node.childNodes.length; j++) { if (node.childNodes[j].nodeType === 3) { text += node.childNodes[j].textContent; } }
            text = text.trim();
            if (text && !node.querySelector("div,span,img,svg,canvas")) {
              ctx.font = cs.fontWeight + " " + cs.fontSize + " " + cs.fontFamily;
              ctx.fillStyle = cs.color;
              ctx.textBaseline = "top";
              ctx.fillText(text, x, y + r.height * 0.10);
            }
            ctx.restore();
          }
          return c;
        };
        fx.tex = function (c) {
          var t = gl.createTexture();
          gl.bindTexture(gl.TEXTURE_2D, t);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, c);
          return t;
        };
        fx.draw = function (p) {
          if (!fx.active || !fx.tF || !fx.tT) { return; }
          gl.useProgram(prog);
          gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, fx.tF);
          gl.uniform1i(gl.getUniformLocation(prog, "u_from"), 0);
          gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, fx.tT);
          gl.uniform1i(gl.getUniformLocation(prog, "u_to"), 1);
          gl.uniform1f(gl.getUniformLocation(prog, "u_p"), p);
          var loc = gl.getAttribLocation(prog, "a_pos");
          gl.bindBuffer(gl.ARRAY_BUFFER, quad);
          gl.enableVertexAttribArray(loc);
          gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
          gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        };
        fx.begin = function (fromSel, toSel) {
          try {
            var fromEl = document.querySelector(fromSel);
            var toEl = document.querySelector(toSel);
            var keepO = toEl.style.opacity, keepV = toEl.style.visibility;
            toEl.style.opacity = "1"; toEl.style.visibility = "visible";
            fx.tF = fx.tex(fx.raster(fromEl));
            fx.tT = fx.tex(fx.raster(toEl));
            toEl.style.opacity = keepO; toEl.style.visibility = keepV;
            canvas.style.display = "block";
            fx.active = true;
            fx.draw(0);
          } catch (e) { fx.active = false; }
        };
        fx.end = function () { if (canvas) { canvas.style.display = "none"; } fx.active = false; };
      }
      window.__vidFx = fx;
    }
    var fx2 = window.__vidFx;
    if (fx2 && fx2.gl) {
      tl.call(function () { fx2.begin("#__PREV__", "#__CUR__"); }, null, __P0__);
      tl.set("#__CUR__", { autoAlpha: 1 }, __PSHOW__);
      tl.set("#__PREV__", { autoAlpha: 0 }, __PHIDE__);
      var proxy = { v: 0 };
      tl.to(proxy, { v: 1, duration: __DUR__, ease: "power2.inOut", onUpdate: function () { fx2.draw(proxy.v); } }, __P0__);
      tl.call(function () { fx2.end(); }, null, __PEND__);
    } else {
      tl.to("#__CUR__", { autoAlpha: 1, duration: 0.5, ease: "power2.inOut" }, __FIN__);
      tl.to("#__PREV__", { autoAlpha: 0, duration: 0.5, ease: "power2.inOut" }, __FIN__);
    }
  })();"""


def build_transition(
    kind: str,
    prev_sid: str,
    cur_sid: str,
    boundary_s: float,
    design: dict,
    *,
    vertical: bool = False,
) -> tuple[list[str], list[str]]:
    """Emit one scene boundary. Returns ``(setup_js, boundary_js)``.

    setup_js = t=0 prep statements (incoming-scene hide + initial offsets;
    duplicates of composer container prehides are harmless). boundary_js =
    the tween statements around ``boundary_s``. cut/crossfade/slide match
    the composer's historical emitter strings; whip ports the blur pan
    (xPercent/yPercent travel + transform reset); dip straddles the boundary
    via the always-emitted ``#blackout`` plate; chroma_split emits a guarded
    IIFE (WebGL raster shader with a CSS-crossfade fallback when gl is
    unavailable). ``vertical`` flips the whip axis.
    """

    b = _t(boundary_s)
    prev, cur = f"#{prev_sid}", f"#{cur_sid}"
    setup = [f'  tl.set("{cur}", {{ autoAlpha: 0 }}, 0);']
    lines: list[str] = []

    if kind == "cut":
        lines.append(f'  tl.set("{prev}", {{ autoAlpha: 0 }}, {b});')
        lines.append(f'  tl.set("{cur}", {{ autoAlpha: 1 }}, {b});')
        return setup, lines

    if kind == "slide":
        setup = [f'  tl.set("{cur}", {{ autoAlpha: 0, x: 60 }}, 0);']
        t0 = max(0.0, round(b - 0.45, 3))
        lines.append(
            f'  tl.to("{prev}", {{ autoAlpha: 0, x: -60, duration: 0.45, '
            f'ease: "power1.inOut" }}, {t0});'
        )
        lines.append(
            f'  tl.to("{cur}", {{ autoAlpha: 1, x: 0, duration: 0.45, '
            f'ease: "power1.out" }}, {max(0.0, round(b - 0.40, 3))});'
        )
        return setup, lines

    if kind == "whip":
        t0 = max(0.0, round(b - 0.34, 3))
        axis_in = "yPercent: 100" if vertical else "xPercent: 100"
        axis_out = "yPercent: -19" if vertical else "xPercent: -19"
        axis_reset = "yPercent: 0" if vertical else "xPercent: 0"
        lines.append(f'  tl.set("{cur}", {{ zIndex: 3 }}, {t0});')
        lines.append(f'  tl.set("{cur}", {{ autoAlpha: 1 }}, {_t(t0 + 0.02)});')
        lines.append(
            f'  tl.fromTo("{cur}", {{ {axis_in} }}, {{ {axis_reset}, '
            f'duration: 0.36, ease: "power4.inOut", overwrite: "auto" }}, {t0});'
        )
        lines.append(
            f'  tl.fromTo("{cur}", {{ filter: "blur(14px)" }}, {{ filter: "blur(0px)", '
            f'duration: 0.36, ease: "power2.out", overwrite: "auto" }}, {t0});'
        )
        lines.append(
            f'  tl.to("{prev}", {{ {axis_out}, filter: "blur(14px)", duration: 0.36, '
            f'ease: "power4.inOut", overwrite: "auto" }}, {t0});'
        )
        lines.append(f'  tl.set("{prev}", {{ autoAlpha: 0 }}, {_t(t0 + 0.38)});')
        lines.append(
            f'  tl.set("{prev}", {{ {axis_reset}, filter: "blur(0px)" }}, {_t(t0 + 0.38)});'
        )
        lines.append(f'  tl.set("{cur}", {{ zIndex: 2 }}, {_t(t0 + 0.40)});')
        return setup, lines

    if kind == "dip":
        t_in = max(0.0, round(b - 0.34, 3))
        lines.append(
            f'  tl.to("#blackout", {{ opacity: 1, duration: 0.34, ease: "power2.in" }}, {t_in});'
        )
        lines.append(f'  tl.set("{cur}", {{ autoAlpha: 1 }}, {_t(b + 0.04)});')
        lines.append(f'  tl.set("{prev}", {{ autoAlpha: 0 }}, {_t(b + 0.04)});')
        lines.append(
            f'  tl.to("#blackout", {{ opacity: 0, duration: 0.42, ease: "power2.out" }}, {_t(b + 0.08)});'
        )
        return setup, lines

    if kind == "chroma_split":
        dur = 1.0
        p0 = max(0.0, round(b - 0.36, 3))
        accent = design["palette"]["accent"]
        ar, ag, ab = hex_to_rgb(accent)
        seam = f"vec3({ar / 255:.3f}, {ag / 255:.3f}, {ab / 255:.3f})"
        block = (
            _CHROMA_TEMPLATE.replace("__PREV__", prev_sid)
            .replace("__CUR__", cur_sid)
            .replace("__P0__", str(p0))
            .replace("__PSHOW__", str(_t(p0 + 0.02)))
            .replace("__PHIDE__", str(_t(p0 + 0.12)))
            .replace("__DUR__", str(dur))
            .replace("__PEND__", str(_t(p0 + dur)))
            .replace("__FIN__", str(max(0.0, round(b - 0.40, 3))))
            .replace("__SEAM__", seam)
            .replace("__BG__", design["palette"]["bg"])
        )
        return setup, [block]

    # crossfade (the default)
    t0 = max(0.0, round(b - 0.45, 3))
    lines.append(
        f'  tl.to("{prev}", {{ autoAlpha: 0, duration: 0.45, '
        f'ease: "power1.inOut" }}, {t0});'
    )
    lines.append(
        f'  tl.to("{cur}", {{ autoAlpha: 1, duration: 0.45, '
        f'ease: "power1.inOut" }}, {max(0.0, round(b - 0.40, 3))});'
    )
    return setup, lines


# =============================================================================
# FRAGMENT VALIDATION (compose-time audit; notes, never raises)
# =============================================================================

_JS_ID_RE = re.compile(r'"#([A-Za-z][A-Za-z0-9_-]*)')
_GETEL_RE = re.compile(r'getElementById\("([A-Za-z][A-Za-z0-9_-]*)"\)')
_DOM_ID_RE = re.compile(r'id="([^"]+)"')


def validate_fragment(frag: SceneFragment) -> list[str]:
    """Audit a fragment's reveal/late_id/dom coherence. Returns notes
    (empty list == clean); never raises.

    Checks: (a) every autoAlpha-revealed id is declared in late_ids,
    (b) every late_id is revealed by at least one JS line,
    (c) every id referenced from JS (and every late_id) exists in the dom.
    """

    notes: list[str] = []
    try:
        dom_ids = set(_DOM_ID_RE.findall(frag.dom or ""))
        late = [str(x) for x in (frag.late_ids or [])]
        js_lines = list(frag.entrance_js or []) + list(frag.sub_beat_js or [])

        revealed: set[str] = set()
        referenced: set[str] = set()
        for line in js_lines:
            ids = _JS_ID_RE.findall(line)
            referenced.update(ids)
            referenced.update(_GETEL_RE.findall(line))
            if "autoAlpha: 1" in line and ids:
                revealed.add(ids[0])

        for rid in sorted(revealed):
            if rid not in late:
                notes.append(f"revealed id not declared late: {rid}")
        for lid in late:
            if lid not in revealed:
                notes.append(f"late id never revealed: {lid}")
        for rid in sorted(referenced):
            # Selectors like "#s2-chip0 .vli-dot" reference dom ids too.
            if rid not in dom_ids:
                notes.append(f"js references id missing from dom: {rid}")
        for lid in late:
            if lid not in dom_ids:
                notes.append(f"late id missing from dom: {lid}")
    except Exception as exc:  # pragma: no cover - defensive (never raises)
        notes.append(f"validator error: {exc}")
    return notes
