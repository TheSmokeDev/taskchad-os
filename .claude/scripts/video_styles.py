"""Style registry + design resolution for the framework video pipeline.

A "design" is a plain dict of visual tokens (palette, fonts, motion hints,
flourish flags) that the video renderer consumes for ALL visual decisions.
The registry ships eight styles ported from the public hyperframes.dev design
gallery plus one neutral default, so a single brief can render in any of them.

Gallery sources (tokens fetched 2026-06, faithfully reduced to video roles):
    https://www.hyperframes.dev/design/<style>

Online path note: hyperframes v0.6.88 has no native design-template fetch
command (`hyperframes catalog` browses registry blocks/components only). The
gallery serves canonical per-style token files at
``https://www.hyperframes.dev/design-templates/<style>/frame.md`` (and a
``frame-showcase.html``); download one and pass it through
``resolve_design(design_file=...)`` to use it directly. This module's registry
is the offline source of truth.

Public API (frozen):
    list_styles() -> list[dict]            # [{"name": ..., "tagline": ...}]
    resolve_design(style=None, design_file=None) -> dict
    suggest_style(brief, dossier=None) -> str   # ranked[0]; never raises
    suggest_styles_ranked(brief, dossier=None, *, kind="") -> list[str]
    design_from_tokens(name, colors, fonts, tagline="") -> dict

Resolution precedence:
    design_file param > style param > env VIDEO_DESIGN_FILE > env VIDEO_STYLE
    > the neutral default.

Env vars are read INSIDE the function body, never bound at def time, so
runtime overrides and test monkeypatching always take effect. Invalid
env-sourced values fail open to the neutral default (ambient config must not
break a render); an invalid explicit ``style`` argument raises ``ValueError``
naming the valid styles.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

# =============================================================================
# COLOR HELPERS (shared with the renderer)
# =============================================================================

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """'#RRGGBB' -> (r, g, b). Raises ValueError on malformed input."""

    if not _HEX_RE.match(value or ""):
        raise ValueError(f"not a 6-digit hex color: {value!r}")
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def blend_hex(color_a: str, color_b: str, t: float) -> str:
    """Linear blend of two hex colors. t=0 -> color_a, t=1 -> color_b."""

    ra, ga, ba = hex_to_rgb(color_a)
    rb, gb, bb = hex_to_rgb(color_b)
    t = min(1.0, max(0.0, t))
    return "#{:02X}{:02X}{:02X}".format(
        round(ra + (rb - ra) * t),
        round(ga + (gb - ga) * t),
        round(ba + (bb - ba) * t),
    )


def relative_luminance(value: str) -> float:
    """Cheap 0..1 luminance estimate. Used to pick light-vs-dark treatments."""

    r, g, b = hex_to_rgb(value)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


# =============================================================================
# THE REGISTRY
# =============================================================================
# Each design dict is the FULL contract the renderer consumes:
#   name        kebab slug
#   tagline     one-line description (no em-dashes anywhere)
#   palette     bg / fg / accent / accent_dim (all 6-digit hex)
#   extras      optional named hex colors beyond the core four
#   fonts       display / body / mono family names + a Google Fonts URL
#               (+ optional display_weight for single-weight display faces)
#   motion      entrance_ease (GSAP ease string) + transition default
#               ("crossfade" | "cut" | "slide")
#   flags       style-specific flourish booleans the renderer may honor
#
# accent_dim is a derived role (subtle washes, grids, tints). Where the source
# palette has a real soft token it is used verbatim; otherwise it is a 25%
# blend of accent into bg, computed offline and committed as hex.

_REGISTRY: dict[str, dict] = {
    "neutral": {
        "name": "neutral",
        "tagline": "Clean dark default. Desaturated, quiet, no brand vibe.",
        "palette": {
            "bg": "#14171C",
            "fg": "#E8EBEE",
            "accent": "#9AA7B4",
            "accent_dim": "#2A323B",
        },
        "extras": {"muted": "#7C8894"},
        "fonts": {
            "display": "Inter",
            "body": "Inter",
            "mono": "JetBrains Mono",
            "display_weight": 800,
            "google_fonts_url": (
                "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;800"
                "&family=JetBrains+Mono:wght@400;600&display=swap"
            ),
        },
        "motion": {"entrance_ease": "power3.out", "transition": "crossfade"},
        "flags": {
            "grain": False,
            "vignette": True,
            "hud_scanline": False,
            "typed_eyebrow": False,
            "shader_transitions": False,
        },
    },
    "blockframe": {
        "name": "blockframe",
        "tagline": "Maximalist neobrutalist. Thick black borders, hard offset shadows, candy accents.",
        "palette": {
            # Source tokens: offwhite #FFFDF5 canvas, black ink, yellow #F7CB46
            # accent, cream #FFDC8B as the soft companion.
            "bg": "#FFFDF5",
            "fg": "#000000",
            "accent": "#F7CB46",
            "accent_dim": "#FFDC8B",
        },
        "extras": {
            "pink": "#FE90E8",
            "blue": "#C0F7FE",
            "green": "#99E885",
            "white": "#FFFFFF",
        },
        "fonts": {
            "display": "Inter",
            "body": "Inter",
            "mono": "Space Grotesk",
            "display_weight": 900,
            "google_fonts_url": (
                "https://fonts.googleapis.com/css2?family=Inter:wght@500;700;800;900"
                "&family=Space+Grotesk:wght@500;600;700&display=swap"
            ),
        },
        "motion": {"entrance_ease": "back.out(1.7)", "transition": "cut"},
        "flags": {
            "hard_borders": True,
            "offset_shadow": True,
            "uppercase_display": True,
            "grain": False,
            "vignette": False,
            "hud_scanline": False,
            "typed_eyebrow": False,
            "shader_transitions": False,
        },
    },
    "coral": {
        "name": "coral",
        "tagline": "Bebas Neue uppercase display, coral on cream, hard color regions.",
        "palette": {
            # Source tokens: coral #E85D5D on cream #F5F0E8, near-black ink,
            # coral-dark #D44A4A as the deep companion.
            "bg": "#F5F0E8",
            "fg": "#1A1A1A",
            "accent": "#E85D5D",
            "accent_dim": "#D44A4A",
        },
        "extras": {"cream_dark": "#E8E0D4", "gray": "#6B6B6B", "white": "#FFFFFF"},
        "fonts": {
            "display": "Bebas Neue",
            "body": "Inter",
            "mono": "Inter",  # the style's label chrome is tracked Inter caps
            "display_weight": 400,
            "google_fonts_url": (
                "https://fonts.googleapis.com/css2?family=Bebas+Neue"
                "&family=Inter:wght@300;400;600;700&display=swap"
            ),
        },
        "motion": {"entrance_ease": "power4.out", "transition": "slide"},
        "flags": {
            "uppercase_display": True,
            "color_region_split": True,
            "wallpaper_numeral": True,
            "grain": False,
            "vignette": False,
            "hud_scanline": False,
            "typed_eyebrow": False,
            "shader_transitions": False,
        },
    },
    "capsule": {
        "name": "capsule",
        "tagline": "Pill-shaped editorial. Sun-bleached cream, candy palette, Bodoni Moda.",
        "palette": {
            # Source tokens: cream #F5F5F0 canvas, ink #1A1A1A, coral #E85D4E
            # accent. accent_dim is a committed 25% blend of accent into bg.
            "bg": "#F5F5F0",
            "fg": "#1A1A1A",
            "accent": "#E85D4E",
            "accent_dim": "#F1CFC7",
        },
        "extras": {
            "lime": "#C4D94E",
            "lavender": "#C5B5E0",
            "sky": "#8BB4F7",
            "violet": "#A06CE8",
            "yellow": "#F2D160",
            "peach": "#F5B895",
            "mint": "#A8E6CF",
        },
        "fonts": {
            "display": "Bodoni Moda",
            "body": "Space Grotesk",
            "mono": "Space Grotesk",
            "display_weight": 700,
            "google_fonts_url": (
                "https://fonts.googleapis.com/css2?family=Bodoni+Moda:opsz,wght@"
                "6..96,600;6..96,700;6..96,800&family=Space+Grotesk:wght@400;500;600"
                "&display=swap"
            ),
        },
        "motion": {"entrance_ease": "back.out(1.4)", "transition": "crossfade"},
        "flags": {
            "pill_shapes": True,
            "decorative_pills": True,
            "grain": False,
            "vignette": False,
            "hud_scanline": False,
            "typed_eyebrow": False,
            "shader_transitions": False,
        },
    },
    "cobalt-grid": {
        "name": "cobalt-grid",
        "tagline": "Editorial parchment under a cobalt graph grid, Newsreader display.",
        "palette": {
            # Source tokens: paper #F0EBDE, cobalt ink #1F2BE0 IS the text
            # color, ink-soft #5560E5 accent. accent_dim is the grid tone
            # (10% ink over paper) committed as hex.
            "bg": "#F0EBDE",
            "fg": "#1F2BE0",
            "accent": "#5560E5",
            "accent_dim": "#DBD8DE",
        },
        "extras": {"paper_2": "#E6E0CE"},
        "fonts": {
            "display": "Newsreader",
            "body": "Hanken Grotesk",
            "mono": "DM Mono",
            "display_weight": 400,
            "google_fonts_url": (
                "https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@"
                "6..72,400;6..72,500&family=Hanken+Grotesk:wght@400;600"
                "&family=DM+Mono:wght@400;500&display=swap"
            ),
        },
        "motion": {"entrance_ease": "power2.inOut", "transition": "crossfade"},
        "flags": {
            "graph_grid": True,
            "hairline_rules": True,
            "grain": False,
            "vignette": True,
            "hud_scanline": False,
            "typed_eyebrow": False,
            "shader_transitions": False,
        },
    },
    "editorial-forest": {
        "name": "editorial-forest",
        "tagline": "Green, pink, and cream editorial triad set in Source Serif.",
        "palette": {
            # Source tokens: cream #EFE7D4 canvas, forest green #2E4A2A text,
            # pink-deep #D27E96 accent with pink #E89CB1 as the soft tint.
            "bg": "#EFE7D4",
            "fg": "#2E4A2A",
            "accent": "#D27E96",
            "accent_dim": "#E89CB1",
        },
        "extras": {
            "green_deep": "#243A21",
            "green_lite": "#3A5A36",
            "cream_2": "#E6DCC4",
            "ink": "#1A1A17",
        },
        "fonts": {
            "display": "Source Serif 4",
            "body": "Source Serif 4",
            "mono": "JetBrains Mono",
            "display_weight": 500,
            "google_fonts_url": (
                "https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@"
                "8..60,400;8..60,500;8..60,600&family=JetBrains+Mono:wght@500"
                "&display=swap"
            ),
        },
        "motion": {"entrance_ease": "power3.out", "transition": "crossfade"},
        "flags": {
            "topbar_rule": True,
            "footline": True,
            "grain": False,
            "vignette": False,
            "hud_scanline": False,
            "typed_eyebrow": False,
            "shader_transitions": False,
        },
    },
    "bold-poster": {
        "name": "bold-poster",
        "tagline": "Shrikhand tilted poster display, red accent on white, serif body.",
        "palette": {
            # Source tokens: white canvas, brown-black ink #1C1410, tomato red
            # #D8000F. accent_dim is a committed 25% blend of accent into bg.
            "bg": "#FFFFFF",
            "fg": "#1C1410",
            "accent": "#D8000F",
            "accent_dim": "#F5BFC3",
        },
        "extras": {"light": "#F5F2EF"},
        "fonts": {
            "display": "Shrikhand",
            "body": "Libre Baskerville",
            "mono": "Space Grotesk",
            "display_weight": 400,
            "google_fonts_url": (
                "https://fonts.googleapis.com/css2?family=Shrikhand"
                "&family=Libre+Baskerville:wght@400;700"
                "&family=Space+Grotesk:wght@500;600&display=swap"
            ),
        },
        "motion": {"entrance_ease": "back.out(2)", "transition": "cut"},
        "flags": {
            "tilted_display": True,
            "progress_bar": True,
            "stacked_text_shadow": True,
            "grain": True,
            "vignette": False,
            "hud_scanline": False,
            "typed_eyebrow": False,
            "shader_transitions": False,
        },
    },
    "broadside": {
        "name": "broadside",
        "tagline": "Industrial newsprint. Raw cream on ink, lowercase Barlow display.",
        "palette": {
            # Source tokens: ink-black #111111 canvas, cream #F0ECE5 text,
            # fire-orange #E85D26 accent. accent_dim is a committed 25% blend
            # of accent into bg.
            "bg": "#111111",
            "fg": "#F0ECE5",
            "accent": "#E85D26",
            "accent_dim": "#472416",
        },
        "extras": {
            "cream_muted": "#888880",
            "border_dark": "#282826",
            "ink_alt": "#1A1A18",
        },
        "fonts": {
            "display": "Barlow",
            "body": "Barlow",
            "mono": "IBM Plex Mono",
            "display_weight": 700,
            "google_fonts_url": (
                "https://fonts.googleapis.com/css2?family=Barlow:wght@400;600;700"
                "&family=IBM+Plex+Mono:wght@500&display=swap"
            ),
        },
        "motion": {"entrance_ease": "expo.out", "transition": "cut"},
        "flags": {
            "lowercase_display": True,
            "hairline_rules": True,
            "grain": True,
            "vignette": True,
            "hud_scanline": False,
            "typed_eyebrow": False,
            "shader_transitions": False,
        },
    },
    "blue-professional": {
        "name": "blue-professional",
        "tagline": "Corporate parchment, cobalt accents, Space Grotesk headings.",
        "palette": {
            # Source tokens: parchment #FDFAE7, near-black text #111111,
            # cobalt #1E2BFA primary. accent_dim is a committed 25% blend of
            # accent into bg.
            "bg": "#FDFAE7",
            "fg": "#111111",
            "accent": "#1E2BFA",
            "accent_dim": "#C5C6EC",
        },
        "extras": {
            "text_muted": "#6B6B6B",
            "positive": "#059669",
            "negative": "#DC2626",
        },
        "fonts": {
            "display": "Space Grotesk",
            "body": "Inter",
            "mono": "Space Grotesk",
            "display_weight": 700,
            "google_fonts_url": (
                "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700"
                "&family=Inter:wght@400;500&display=swap"
            ),
        },
        "motion": {"entrance_ease": "power3.out", "transition": "crossfade"},
        "flags": {
            "pill_tags": True,
            "card_chrome": True,
            "grain": False,
            "vignette": False,
            "hud_scanline": False,
            "typed_eyebrow": False,
            "shader_transitions": False,
        },
    },
}

DEFAULT_STYLE = "neutral"


# =============================================================================
# PUBLIC API
# =============================================================================


def list_styles() -> list[dict]:
    """Return [{"name": ..., "tagline": ...}] for every registered style."""

    return [
        {"name": design["name"], "tagline": design["tagline"]}
        for design in _REGISTRY.values()
    ]


def resolve_design(style: str | None = None, design_file: str | None = None) -> dict:
    """Resolve a design dict. See module docstring for the precedence chain.

    Returns a deep copy so callers can tweak tokens without mutating the
    registry (the registry is never runtime state, only a constant source).
    """

    if design_file:
        return _design_from_file(design_file)
    if style:
        return _design_from_name(style)

    # Env reads happen here, inside the body, at call time (never at def
    # time), so overrides and monkeypatching always take effect.
    env_file = os.environ.get("VIDEO_DESIGN_FILE", "").strip()
    if env_file:
        try:
            return _design_from_file(env_file)
        except ValueError:
            pass  # ambient config fails open to the next source
    env_style = os.environ.get("VIDEO_STYLE", "").strip()
    if env_style:
        try:
            return _design_from_name(env_style)
        except ValueError:
            pass  # ambient config fails open to the neutral default

    return copy.deepcopy(_REGISTRY[DEFAULT_STYLE])


# =============================================================================
# NAME RESOLUTION
# =============================================================================


def _slugify(name: str) -> str:
    slug = (name or "").strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    return re.sub(r"-{2,}", "-", slug).strip("-")


def _design_from_name(style: str) -> dict:
    slug = _slugify(style)
    design = _REGISTRY.get(slug)
    if design is None:
        valid = ", ".join(_REGISTRY.keys())
        raise ValueError(f"unknown style {style!r}. Valid styles: {valid}")
    return copy.deepcopy(design)


# =============================================================================
# DESIGN FILE PARSING (lenient: design.md / frame.md style markdown, or JSON)
# =============================================================================

_COLOR_LINE = re.compile(
    r"(?m)^\s*([A-Za-z0-9_-]+)\s*:\s*[\"']?(#[0-9A-Fa-f]{6})\b"
)
_FONT_LINE = re.compile(
    r"(?m)^\s*([A-Za-z0-9_-]+)\s*:.*?fontFamily:\s*[\"']([^\"']+)[\"']"
)
_NAME_LINE = re.compile(r"(?m)^name:\s*[\"']?(.+?)[\"']?\s*$")
_DESC_INLINE = re.compile(r"(?m)^description:[ \t]*(?![>\s])([^\n]+)$")
_DESC_BLOCK = re.compile(r"(?m)^description:\s*>\s*\n\s+([^\n]+)")

# Preference orders for mapping arbitrary color names onto the four roles.
_BG_KEYS = ("bg", "background", "paper", "canvas", "cream", "offwhite", "off-white")
_FG_KEYS = ("fg", "foreground", "text", "ink", "ink-black", "black", "dark")
_ACCENT_KEYS = ("accent", "primary", "brand", "highlight")
_ACCENT_DIM_KEYS = ("accent_dim", "accent-dim", "accent-soft", "accent-light", "muted")

_ROLE_DISPLAY = re.compile(r"display|hero|headline|heading|title|h1|h2|quote|stat", re.I)
_ROLE_BODY = re.compile(r"^body", re.I)
_ROLE_MONO = re.compile(r"label|mono|caption|counter|tag|micro|chrome", re.I)


def _design_from_file(design_file: str) -> dict:
    path = Path(design_file)
    if not path.is_file():
        raise ValueError(f"design file not found: {design_file}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"design file unreadable: {design_file} ({exc})") from exc

    if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        return _design_from_json(text, path)
    return _design_from_markdown(text, path)


def _design_from_json(text: str, path: Path) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"design file is not valid JSON: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"design file JSON must be an object: {path}")

    design = copy.deepcopy(_REGISTRY[DEFAULT_STYLE])
    design["name"] = _slugify(str(data.get("name", ""))) or _slugify(path.stem) or "custom"
    if data.get("tagline"):
        design["tagline"] = str(data["tagline"])
    for section in ("palette", "fonts", "motion", "flags", "extras"):
        value = data.get(section)
        if isinstance(value, dict):
            design.setdefault(section, {})
            design[section].update(value)
    _finalize_palette(design)
    return design


def _design_from_markdown(text: str, path: Path) -> dict:
    """Lenient token scrape of a design.md / frame.md style document.

    Pulls hex colors, fontFamily declarations, the name line, and the
    description. Anything not found falls back to the neutral default so a
    thin file still resolves (the contract is lenient parsing, not strict
    schema validation).
    """

    design = copy.deepcopy(_REGISTRY[DEFAULT_STYLE])

    name_match = _NAME_LINE.search(text)
    raw_name = name_match.group(1).split("(")[0] if name_match else path.stem
    design["name"] = _slugify(raw_name) or "custom"

    desc = _DESC_INLINE.search(text) or _DESC_BLOCK.search(text)
    if desc:
        # Em-dashes never ship in user-facing text ("\u2014" kept as an
        # escape so this source file itself stays clean).
        design["tagline"] = desc.group(1).strip().replace("\u2014", ",")

    colors: dict[str, str] = {}
    for key, value in _COLOR_LINE.findall(text):
        colors.setdefault(key.lower(), value.upper())
    if colors:
        ordered = list(colors.items())
        bg = _first_for_keys(colors, _BG_KEYS) or ordered[0][1]
        fg = _first_for_keys(colors, _FG_KEYS) or _most_contrasting(colors, bg)
        accent = _first_for_keys(colors, _ACCENT_KEYS) or _first_not_in(colors, {bg, fg})
        accent_dim = _first_for_keys(colors, _ACCENT_DIM_KEYS)
        design["palette"] = {
            "bg": bg,
            "fg": fg,
            "accent": accent or fg,
            "accent_dim": accent_dim or "",
        }
        design["extras"] = {
            k: v for k, v in colors.items() if v not in {bg, fg, accent}
        }

    fonts: list[tuple[str, str]] = [(k, v) for k, v in _FONT_LINE.findall(text)]
    if fonts:
        families = list(dict.fromkeys(family for _, family in fonts))
        display = next((f for r, f in fonts if _ROLE_DISPLAY.search(r)), families[-1])
        body = next((f for r, f in fonts if _ROLE_BODY.search(r)), families[0])
        mono = next(
            (f for f in families if "mono" in f.lower()),
            next((f for r, f in fonts if _ROLE_MONO.search(r)), body),
        )
        design["fonts"] = {
            "display": display,
            "body": body,
            "mono": mono,
            "google_fonts_url": _google_fonts_url([display, body, mono]),
        }

    _finalize_palette(design)
    return design


def _first_for_keys(colors: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in colors:
            return colors[key]
    return ""


def _first_not_in(colors: dict[str, str], taken: set[str]) -> str:
    for value in colors.values():
        if value not in taken:
            return value
    return ""


def _most_contrasting(colors: dict[str, str], bg: str) -> str:
    """Pick the color farthest in luminance from bg (lenient fg guess)."""

    bg_lum = relative_luminance(bg)
    best, best_delta = "", -1.0
    for value in colors.values():
        delta = abs(relative_luminance(value) - bg_lum)
        if delta > best_delta:
            best, best_delta = value, delta
    return best


def _google_fonts_url(families: list[str]) -> str:
    unique = list(dict.fromkeys(f for f in families if f))
    parts = [f"family={f.replace(' ', '+')}:wght@400;700" for f in unique]
    return "https://fonts.googleapis.com/css2?" + "&".join(parts) + "&display=swap"


def _finalize_palette(design: dict) -> None:
    """Ensure the four palette roles exist and are valid hex; derive the dim."""

    neutral = _REGISTRY[DEFAULT_STYLE]["palette"]
    palette = design.setdefault("palette", {})
    for role in ("bg", "fg", "accent"):
        value = str(palette.get(role) or "")
        palette[role] = value.upper() if _HEX_RE.match(value) else neutral[role]
    dim = str(palette.get("accent_dim") or "")
    if not _HEX_RE.match(dim):
        # Derived role: 25% accent blended into bg (same rule the committed
        # registry values follow when the source palette has no soft token).
        dim = blend_hex(palette["bg"], palette["accent"], 0.25)
    palette["accent_dim"] = dim.upper()


# =============================================================================
# TOKENS -> DESIGN (research/scrape seam; additive public API)
# =============================================================================


def design_from_tokens(
    name: str,
    colors: dict[str, str],
    fonts: list[str],
    tagline: str = "",
) -> dict:
    """Scraped tokens -> a COMPLETE validated design dict.

    The research stage hands in whatever it found on a page: ``colors`` is a
    name -> hex mapping (names may be CSS custom-prop names, role words, or
    synthetic keys; invalid hex values are dropped), ``fonts`` is an ordered
    family list. Role mapping reuses the design-file rules: preference keys
    first (_BG_KEYS/_FG_KEYS/_ACCENT_KEYS), then first-color/most-contrasting
    fallbacks; unmapped colors land in ``extras``. Fonts map display/body
    from the list order with the first mono-named family as ``mono`` (the
    neutral mono as fallback); a Google Fonts URL is built when any fonts
    were given. Everything else (motion, flags) starts from the neutral
    default, and ``_finalize_palette`` guarantees the four roles are valid
    hex with a derived ``accent_dim``.
    """

    design = copy.deepcopy(_REGISTRY[DEFAULT_STYLE])
    design["name"] = _slugify(str(name or "")) or "derived"
    if tagline:
        # Em-dashes never ship in user-facing text (the dash is kept as an
        # escape so this source file itself stays clean).
        design["tagline"] = str(tagline).replace("\u2014", ",")

    cleaned: dict[str, str] = {}
    for key, value in (colors or {}).items():
        candidate = str(value or "").strip().upper()
        if _HEX_RE.match(candidate):
            cleaned.setdefault(str(key or "").strip().lower(), candidate)
    if cleaned:
        ordered = list(cleaned.items())
        bg = _first_for_keys(cleaned, _BG_KEYS) or ordered[0][1]
        fg = _first_for_keys(cleaned, _FG_KEYS) or _most_contrasting(cleaned, bg)
        accent = _first_for_keys(cleaned, _ACCENT_KEYS) or _first_not_in(cleaned, {bg, fg})
        accent_dim = _first_for_keys(cleaned, _ACCENT_DIM_KEYS)
        design["palette"] = {
            "bg": bg,
            "fg": fg,
            "accent": accent or fg,
            "accent_dim": accent_dim or "",
        }
        design["extras"] = {
            k: v for k, v in cleaned.items() if v not in {bg, fg, accent}
        }

    families = list(dict.fromkeys(f.strip() for f in (fonts or []) if str(f or "").strip()))
    if families:
        display = families[0]
        body = families[1] if len(families) > 1 else families[0]
        mono = next(
            (f for f in families if "mono" in f.lower()),
            _REGISTRY[DEFAULT_STYLE]["fonts"]["mono"],
        )
        design["fonts"] = {
            "display": display,
            "body": body,
            "mono": mono,
            "google_fonts_url": _google_fonts_url([display, body, mono]),
        }

    _finalize_palette(design)
    return design


# =============================================================================
# STYLE SUGGESTION (brief -> best style; heuristic-first, never raises)
# =============================================================================
# Ordered rules: the FIRST matching bucket wins. Corporate runs before sports
# so "win customers" style briefs land professional, not poster.

_SUGGEST_RULES: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), style)
    for pattern, style in (
        (
            r"\b(corporate|b2b|business|enterprise|saas|quarterly|revenue|earnings|"
            r"investors?|board|finance|fintech|customers?|clients?|sales|roi|webinar)\b",
            "blue-professional",
        ),
        (
            r"\b(nature|forest|garden|organic|eco|sustainab\w*|farm\w*|wildlife|"
            r"outdoors?|wellness|botanic\w*|hiking|harvest)\b",
            "editorial-forest",
        ),
        (
            r"\b(news|headlines?|breaking|press|bulletin|newsroom|journalis\w*|dispatch)\b",
            "broadside",
        ),
        (
            r"\b(editorial|essay|analysis|op-ed|longform|deep dive|magazine|journal|retrospective)\b",
            "cobalt-grid",
        ),
        (
            r"\b(sports?|match|game|team|league|cup|championship|tournament|finals?|"
            r"playoffs?|goal|derby|olympics?|soccer|football|basketball|baseball|"
            r"racing|athletes?|stadium|world cup|fifa|win|wins|won|victory)\b",
            "bold-poster",
        ),
        (
            r"\b(hype|energy|launch|drop|festival|concert|party|anthem|loud|electric|tour)\b",
            "coral",
        ),
        (
            r"\b(playful|fun|kids|children|family|cute|candy|whimsical|birthday|"
            r"celebrat\w*|toys?)\b",
            "capsule",
        ),
        (
            r"\b(tech|developer|software|coding|code|programming|ai|startup|hacker|"
            r"brutalis\w*|terminal|api|engineer\w*|open[- ]source)\b",
            "blockframe",
        ),
    )
)


def _suggest_by_keywords(brief: str) -> str:
    lowered = str(brief or "").lower()
    for pattern, style in _SUGGEST_RULES:
        if pattern.search(lowered):
            return style
    return DEFAULT_STYLE


def _suggest_via_lane(brief: str) -> str:
    """One-word style pick through the runtime lanes. "" on any failure.

    Lazy imports keep this module pure for offline use; the lane call is a
    single no-tools, single-turn request with a strict one-word parse.
    """

    try:
        import asyncio

        from runtime.base import RuntimeRequest
        from runtime.capabilities import TEXT_REASONING
        from runtime.lane_router import run_with_runtime_lanes

        names = ", ".join(_REGISTRY.keys())
        prompt = (
            "Pick the single best visual style for this video brief. Reply "
            f"with EXACTLY one name from this list and nothing else: {names}\n\n"
            f"BRIEF: {str(brief)[:600]}"
        )
        result = asyncio.run(
            run_with_runtime_lanes(
                RuntimeRequest(
                    prompt=prompt,
                    cwd=Path(__file__).resolve().parents[2],
                    task_name="video_style_suggest",
                    capability=TEXT_REASONING,
                    max_turns=1,
                    allowed_tools=[],
                )
            )
        )
        text = (result.text or "").strip()
        word = _slugify(text.split()[0]) if text else ""
        return word if word in _REGISTRY else ""
    except Exception:
        return ""


# Wizard-kind affinities: a picked video kind nudges styles that carry its
# energy. Applied only when the caller passes ``kind`` (the wizard does).
_KIND_AFFINITY: dict[str, tuple[str, ...]] = {
    "hype": ("bold-poster", "broadside"),
    "explainer": ("blue-professional", "neutral"),
    "launch": ("blockframe", "cobalt-grid"),
}

# Serif display faces we can recognize by name (derived designs carry only
# family names, never font metadata).
_SERIF_MARKERS = (
    "serif",
    "bodoni",
    "newsreader",
    "playfair",
    "baskerville",
    "garamond",
    "georgia",
    "times",
    "merriweather",
    "lora",
    "spectral",
)


def _looks_serif(family: str) -> bool:
    name = str(family or "").lower()
    if not name or "sans" in name:
        return False
    return any(marker in name for marker in _SERIF_MARKERS)


def _is_bright(value: str) -> bool:
    """Vivid accent check: a strong channel plus real chroma (not white/gray)."""

    r, g, b = hex_to_rgb(value)
    return max(r, g, b) >= 208 and (max(r, g, b) - min(r, g, b)) >= 60


def _dossier_signal_scores(dossier: dict | None) -> dict[str, int]:
    """Style boosts from a research dossier's derived design. Never raises."""

    scores: dict[str, int] = {}
    derived = (dossier or {}).get("derived_design") if isinstance(dossier, dict) else None
    if not isinstance(derived, dict):
        return scores
    palette = derived.get("palette") or {}
    fonts = derived.get("fonts") or {}
    try:
        if relative_luminance(str(palette.get("bg") or "")) < 0.5:
            scores["blockframe"] = scores.get("blockframe", 0) + 2
            scores["cobalt-grid"] = scores.get("cobalt-grid", 0) + 2
    except ValueError:
        pass
    if _looks_serif(str(fonts.get("display") or "")):
        scores["editorial-forest"] = scores.get("editorial-forest", 0) + 1
        scores["broadside"] = scores.get("broadside", 0) + 1
    try:
        if _is_bright(str(palette.get("accent") or "")):
            scores["coral"] = scores.get("coral", 0) + 1
            scores["capsule"] = scores.get("capsule", 0) + 1
    except ValueError:
        pass
    return scores


def suggest_styles_ranked(
    brief: str,
    dossier: dict | None = None,
    *,
    kind: str = "",
) -> list[str]:
    """Rank EVERY registered style for a brief, best first. Never raises.

    Scoring (deterministic): the first matching keyword rule scores +3
    (exactly the ordered-bucket semantics ``suggest_style`` always had), a
    wizard ``kind`` affinity scores +2, and dossier-derived signals (dark
    palette, serif display, bright accent) score +1..2. Ties break on
    registry order. When env VIDEO_SUGGEST_LLM is truthy, one runtime-lane
    one-word call may promote its strict-parsed pick to the front; any
    failure leaves the heuristic ranking untouched.
    """

    try:
        text = str(brief or "")
        scores = {name: 0 for name in _REGISTRY}  # insertion order = registry order
        lowered = text.lower()
        for pattern, style in _SUGGEST_RULES:
            if pattern.search(lowered):
                scores[style] += 3
                break  # first matching bucket wins, exactly like before
        for name in _KIND_AFFINITY.get(str(kind or "").strip().lower(), ()):
            if name in scores:
                scores[name] += 2
        for name, boost in _dossier_signal_scores(dossier).items():
            if name in scores:
                scores[name] += boost

        order = {name: i for i, name in enumerate(_REGISTRY)}
        ranked = sorted(scores, key=lambda name: (-scores[name], order[name]))

        # Env reads happen at call time (never at def time) so overrides and
        # monkeypatching always take effect.
        if os.environ.get("VIDEO_SUGGEST_LLM", "").strip().lower() in {"1", "true", "on", "yes"}:
            refined = _suggest_via_lane(text)
            if refined and refined in scores and ranked[0] != refined:
                ranked.remove(refined)
                ranked.insert(0, refined)
        return ranked
    except Exception:
        return list(_REGISTRY)


def suggest_style(brief: str, dossier: dict | None = None) -> str:
    """Pick the best registered style for a brief. Never raises.

    Back-compatible wrapper over ``suggest_styles_ranked``: the top-ranked
    style ships. One-arg calls keep working; passing a research ``dossier``
    lets its derived-design signals influence the pick. When env
    VIDEO_SUGGEST_LLM is truthy ("1"/"true"/"on"/"yes"), one runtime-lane
    one-word call may refine the pick; a strict parse rejects anything that
    is not a registry name and falls back to the heuristic. Always returns a
    valid style name.
    """

    try:
        ranked = suggest_styles_ranked(brief, dossier)
        return ranked[0] if ranked else DEFAULT_STYLE
    except Exception:
        return DEFAULT_STYLE
