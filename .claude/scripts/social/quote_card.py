"""Branded quote-card renderer for image-required channels (Instagram).

Renders a 1080x1080 PNG with the post body as a clean, on-brand YourBusiness
quote card. Pillow only — no network, no LLM. All paths resolved at call
time (Rule 1); no module-scope config or font caching.
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── Brand palette (sampled from the YourBusiness brand card) ────────────
_NAVY_TOP = (1, 62, 117)       # #013E75 — dominant brand navy
_NAVY_BOTTOM = (6, 33, 64)     # #062140 — deeper navy for gradient floor
_WHITE = (250, 250, 250)       # #FAFAFA
_ORANGE = (253, 89, 1)         # #FD5901 — brand CTA accent

_CANVAS = 1080
_PADDING = 96                  # generous side/top padding
_FOOTER_H = 120                # reserved footer band for the wordmark

# Headline auto-scale bounds.
_HEADLINE_MAX_PT = 88
_HEADLINE_MIN_PT = 40
_WORDMARK_PT = 40
_TITLE_PT = 34

# Candidate fonts, best-first. Headline wants a bold weight; footer a
# semibold. Fall back to Arial Bold, then PIL's bundled default.
_BOLD_FONTS = ("segoeuib.ttf", "arialbd.ttf", "verdanab.ttf", "tahomabd.ttf")
_SEMIBOLD_FONTS = ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf")


def _windows_fonts_dir() -> Path:
    return Path("C:/Windows/Fonts")


def _load_font(candidates: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    """Return the first available TTF at the requested size, else PIL default."""
    fonts_dir = _windows_fonts_dir()
    for name in candidates:
        path = fonts_dir / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    # Last resort: a real TTF anywhere it loads, else the bitmap default.
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _gradient_background() -> Image.Image:
    """Vertical navy gradient, top (#013E75) to bottom (#062140)."""
    img = Image.new("RGB", (_CANVAS, _CANVAS), _NAVY_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(_CANVAS):
        t = y / (_CANVAS - 1)
        r = round(_NAVY_TOP[0] + (_NAVY_BOTTOM[0] - _NAVY_TOP[0]) * t)
        g = round(_NAVY_TOP[1] + (_NAVY_BOTTOM[1] - _NAVY_TOP[1]) * t)
        b = round(_NAVY_TOP[2] + (_NAVY_BOTTOM[2] - _NAVY_TOP[2]) * t)
        draw.line([(0, y), (_CANVAS, y)], fill=(r, g, b))
    return img


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def _wrap(body: str, font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw,
          max_width: int) -> list[str]:
    """Greedy word-wrap honoring the rendered pixel width of ``font``."""
    lines: list[str] = []
    # Preserve author paragraph breaks, wrap each paragraph independently.
    for paragraph in body.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append("")
            continue
        words = paragraph.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                # A single word wider than the box: hard-break it.
                if draw.textlength(word, font=font) > max_width:
                    lines.extend(_break_long_word(word, font, draw, max_width))
                    current = ""
                else:
                    current = word
        if current:
            lines.append(current)
    return lines


def _break_long_word(word: str, font: ImageFont.FreeTypeFont,
                     draw: ImageDraw.ImageDraw, max_width: int) -> list[str]:
    pieces: list[str] = []
    chunk = ""
    for ch in word:
        if draw.textlength(chunk + ch, font=font) <= max_width:
            chunk += ch
        else:
            if chunk:
                pieces.append(chunk)
            chunk = ch
    if chunk:
        pieces.append(chunk)
    return pieces


def _fit_headline(
    body: str,
    draw: ImageDraw.ImageDraw,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Shrink the headline font until the wrapped text fits the text box.

    At the minimum size, truncate with an ellipsis on the last line that fits.
    """
    for pt in range(_HEADLINE_MAX_PT, _HEADLINE_MIN_PT - 1, -4):
        font = _load_font(_BOLD_FONTS, pt)
        lines = _wrap(body, font, draw, max_width)
        total_h = len(lines) * round(_line_height(font) * 1.18)
        if total_h <= max_height:
            return font, lines

    # Floor reached: truncate to the lines that fit, ellipsize the last one.
    font = _load_font(_BOLD_FONTS, _HEADLINE_MIN_PT)
    lines = _wrap(body, font, draw, max_width)
    line_h = round(_line_height(font) * 1.18)
    max_lines = max(1, max_height // line_h)
    if len(lines) <= max_lines:
        return font, lines
    kept = lines[: max_lines]
    last = kept[-1]
    ellipsis = "…"
    while last and draw.textlength(last + ellipsis, font=font) > max_width:
        last = last[:-1].rstrip()
    kept[-1] = (last + ellipsis) if last else ellipsis
    return font, kept


def _draw_wordmark(img: Image.Image, draw: ImageDraw.ImageDraw) -> None:
    """Footer: orange accent bar + 'YourBusiness' wordmark (white + orange)."""
    bar_y = _CANVAS - _FOOTER_H
    # Thin orange accent bar above the footer band.
    draw.rectangle([(_PADDING, bar_y), (_PADDING + 220, bar_y + 6)], fill=_ORANGE)

    font = _load_font(_SEMIBOLD_FONTS, _WORDMARK_PT)
    quote, moto = "Quote", "Moto"
    text_y = bar_y + 28
    draw.text((_PADDING, text_y), quote, font=font, fill=_WHITE)
    quote_w = draw.textlength(quote, font=font)
    draw.text((_PADDING + quote_w, text_y), moto, font=font, fill=_ORANGE)
    # ".com" tail, muted white, after the wordmark.
    moto_w = draw.textlength(moto, font=font)
    tail_font = _load_font(_SEMIBOLD_FONTS, _WORDMARK_PT - 6)
    draw.text(
        (_PADDING + quote_w + moto_w + 6, text_y + 6),
        ".com",
        font=tail_font,
        fill=(160, 185, 215),
    )


def _card_filename(body: str) -> str:
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:10]
    return f"card-{digest}.png"


def render_quote_card(
    body: str,
    *,
    title: str = "",
    out_dir: Path | None = None,
) -> Path:
    """Render a 1080x1080 branded quote card PNG and return its local Path.

    ``body`` is the post text shown as the headline. ``title`` (optional) is
    rendered as a small kicker above the headline. ``out_dir`` defaults to
    ``config.DATA_DIR / 'social_cards'`` (resolved at call time, Rule 1).
    """
    if out_dir is None:
        import config

        out_dir = config.DATA_DIR / "social_cards"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    img = _gradient_background()
    draw = ImageDraw.Draw(img)

    text_left = _PADDING
    text_width = _CANVAS - 2 * _PADDING
    content_top = _PADDING
    content_bottom = _CANVAS - _FOOTER_H - 40

    # Optional kicker title above the headline.
    cursor_y = content_top
    if title.strip():
        title_font = _load_font(_SEMIBOLD_FONTS, _TITLE_PT)
        kicker = title.strip().replace("\n", " ")
        if draw.textlength(kicker, font=title_font) > text_width:
            ell = "…"
            while kicker and draw.textlength(kicker + ell, font=title_font) > text_width:
                kicker = kicker[:-1].rstrip()
            kicker = (kicker + ell) if kicker else ell
        draw.text((text_left, cursor_y), kicker, font=title_font, fill=_ORANGE)
        cursor_y += round(_line_height(title_font) * 1.6)

    headline_max_h = content_bottom - cursor_y
    font, lines = _fit_headline(body, draw, text_width, headline_max_h)
    line_h = round(_line_height(font) * 1.18)
    for line in lines:
        draw.text((text_left, cursor_y), line, font=font, fill=_WHITE)
        cursor_y += line_h

    _draw_wordmark(img, draw)

    out_path = out_dir / _card_filename(body)
    img.save(out_path, format="PNG")
    return out_path
