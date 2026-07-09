"""Still ad-card engine - AI scene + crisp HTML text overlay, exported as one PNG.

The STILL/PNG sibling of the video pipeline's ``compose_html`` + hyperframes
render loop. Image models bake garbled letters, so no copy is ever generated
into the scene; the AI produces a text-free SCENE and this module overlays a
clean, real-HTML brand panel (eyebrow, headline, accent word, subhead, pill
CTA) on top, then exports a single crisp PNG.

Contract (every public function fails open, never raises):
    compose_card_html(scene_asset_rel, design, copy, width, height) -> str
        Assemble a self-contained, hyperframes-compatible ``index.html`` - the
        SCENE as a full-bleed background layer, a rounded brand panel filled
        with ``palette.bg`` carrying all copy as real HTML text (never baked
        into the image). Fonts load from ``fonts.google_fonts_url`` via <link>.
    render_card_png(project_dir, png_path) -> bool
        Render the static card to a PNG. hyperframes has no still/screenshot
        export (its ``render`` emits mp4/webm only), so this renders a short
        1-frame MP4 via ``video_pipeline.run_hyperframes_render`` and extracts
        frame 0 with ``ffmpeg -frames:v 1``. Fail-open -> False.
    generate_card(scene_prompt, copy, *, design, aspect, out_dir, refs,
                  attempts, scene_png) -> str | None
        Full path: generate (or reuse) the scene, compose the card HTML, write
        the project, render the PNG. Returns the absolute PNG path or None.

This module is brand/persona-AGNOSTIC: ``design`` and ``refs`` are INPUTS. No
hardcoded brand, no persona names, nothing personal. It ships public.

Rule 1: every env/config value is resolved at call time (no module cache).
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from string import Template

logger = logging.getLogger(__name__)

_GSAP_CDN = "https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

# Aspect -> (width, height). Default is the vertical IG ad-card format.
_ASPECT_DIMS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "4:5": (1080, 1350),
}

# Neutral fallbacks so a thin/empty design still composes a legible card.
_DEFAULT_PALETTE = {
    "bg": "#FFFFFF",
    "fg": "#111111",
    "accent": "#E8602C",
    "accent_dim": "#F3D9CB",
}
_DEFAULT_FONTS = {
    "display": "Arial, Helvetica, sans-serif",
    "body": "Arial, Helvetica, sans-serif",
    "mono": "monospace",
    "display_weight": 800,
    "google_fonts_url": "",
}

# Static-card timeline: no animation, so any single frame is the card. A short
# duration keeps the 1-frame render fast.
_CARD_DURATION_S = "1.0"

_CARD_HTML_TEMPLATE = Template(
    """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=${width}, height=${height}" />
    ${fonts_link}
    <script src="${gsap_cdn}"></script>
    <style>
      * { margin: 0; padding: 0; box-sizing: border-box; }
      html, body { width: ${width}px; height: ${height}px; overflow: hidden; }
      #root {
        position: relative; width: ${width}px; height: ${height}px;
        background: ${bg}; font-family: ${body_font};
      }
      #scene {
        position: absolute; inset: 0; z-index: 0;
        background-image: url("${scene}");
        background-size: cover; background-position: center;
      }
      #panel {
        position: absolute; z-index: 2; ${panel_pos}
        background: ${bg}; border-radius: ${radius}px; padding: ${pad}px;
        display: flex; flex-direction: column; gap: ${gap}px;
        box-shadow: 0 ${shadow1}px ${shadow2}px rgba(0, 0, 0, 0.18);
      }
      #eyebrow {
        font-family: ${display_font}; font-weight: 700; letter-spacing: 0.08em;
        text-transform: uppercase; font-size: ${eyebrow_pt}px; color: ${accent};
      }
      #headline {
        font-family: ${display_font}; font-weight: ${display_weight};
        line-height: 1.03; font-size: ${headline_pt}px; color: ${fg};
      }
      #accentword {
        font-family: ${display_font}; font-weight: ${display_weight};
        line-height: 1.03; font-size: ${headline_pt}px; color: ${accent};
      }
      #subhead {
        font-family: ${body_font}; font-weight: 500; font-size: ${subhead_pt}px;
        line-height: 1.3; color: ${fg}; opacity: 0.82;
      }
      #cta {
        align-self: flex-start; margin-top: ${cta_mt}px;
        font-family: ${display_font}; font-weight: 800; font-size: ${cta_pt}px;
        color: ${bg}; background: ${accent};
        padding: ${cta_py}px ${cta_px}px; border-radius: 999px;
        letter-spacing: 0.01em;
      }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="${duration}" data-width="${width}" data-height="${height}">
      <div id="scene"></div>
      <div id="panel">
${panel_children}
      </div>
    </div>
    <script>
      window.__timelines = window.__timelines || {};
      const tl = gsap.timeline({ paused: true });
      tl.set("#panel", { autoAlpha: 1 }, 0);
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
"""
)


def _resolve_ffmpeg_timeout() -> int:
    """Frame-extract timeout in seconds at call time: env
    IMAGE_CARD_FFMPEG_TIMEOUT_S > 60 (Rule 1). Never raises."""

    raw = os.environ.get("IMAGE_CARD_FFMPEG_TIMEOUT_S", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass  # ambient config never breaks a render
    return 60


def _dims_for_aspect(aspect: str) -> tuple[int, int]:
    """(width, height) for an aspect string; defaults to the 9:16 card."""

    return _ASPECT_DIMS.get(str(aspect or "").strip(), _ASPECT_DIMS["9:16"])


def _merge(defaults: dict, override: object) -> dict:
    """Shallow-merge an override dict over defaults; non-dicts ignored."""

    merged = dict(defaults)
    if isinstance(override, dict):
        for key, value in override.items():
            if value not in (None, ""):
                merged[key] = value
    return merged


def _panel_position(width: int, height: int, margin: int) -> str:
    """CSS for the brand panel: a top-left box (never full width) so a subject
    framed on the right stays clear of the copy. Content-height, anchored
    top-left; a slightly wider box on landscape cards."""

    box_width = "58%" if height > width else "46%"
    return f"left: {margin}px; top: {margin}px; width: {box_width}; max-width: {width - 2 * margin}px;"


def _copy_element(element_id: str, value: object) -> str:
    """One panel child as escaped real HTML, or '' when the copy slot is empty."""

    text = str(value or "").strip()
    if not text:
        return ""
    return f'        <div id="{element_id}">{html.escape(text)}</div>'


def compose_card_html(
    scene_asset_rel: str,
    design: dict,
    copy: dict,
    width: int,
    height: int,
) -> str:
    """Build a clean, self-contained IG ad-card ``index.html``.

    The SCENE (``scene_asset_rel``, e.g. ``assets/scene.png``) is a full-bleed
    background layer; a rounded brand panel filled with ``palette.bg`` carries
    the copy top to bottom: eyebrow/logo line (``copy['eyebrow']``, accent), a
    bold HEADLINE (``copy['headline']``, display font, fg), an optional accent
    word (``copy['accent']``), a subhead (``copy['subhead']``), and a pill CTA
    (``copy['cta']``, accent background). Every word is real HTML text, never
    baked into the image. Fonts load from ``fonts.google_fonts_url`` via <link>.

    Agnostic: palette + fonts come entirely from ``design``. Never raises - on
    any failure it still returns a minimal valid card string.
    """

    try:
        palette = _merge(_DEFAULT_PALETTE, (design or {}).get("palette"))
        fonts = _merge(_DEFAULT_FONTS, (design or {}).get("fonts"))
        copy = copy or {}

        width = int(width) if int(width) > 0 else _ASPECT_DIMS["9:16"][0]
        height = int(height) if int(height) > 0 else _ASPECT_DIMS["9:16"][1]

        margin = round(width * 0.055)
        fonts_url = str(fonts.get("google_fonts_url", "") or "").strip()
        fonts_link = (
            f'<link rel="stylesheet" href="{html.escape(fonts_url, quote=True)}" />'
            if fonts_url
            else ""
        )

        children = "\n".join(
            frag
            for frag in (
                _copy_element("eyebrow", copy.get("eyebrow")),
                _copy_element("headline", copy.get("headline")),
                _copy_element("accentword", copy.get("accent")),
                _copy_element("subhead", copy.get("subhead")),
                _copy_element("cta", copy.get("cta")),
            )
            if frag
        )

        return _CARD_HTML_TEMPLATE.substitute(
            width=width,
            height=height,
            duration=_CARD_DURATION_S,
            gsap_cdn=_GSAP_CDN,
            fonts_link=fonts_link,
            scene=html.escape(str(scene_asset_rel or ""), quote=True),
            bg=palette["bg"],
            fg=palette["fg"],
            accent=palette["accent"],
            display_font=str(fonts.get("display") or _DEFAULT_FONTS["display"]),
            body_font=str(fonts.get("body") or _DEFAULT_FONTS["body"]),
            display_weight=int(fonts.get("display_weight", 800) or 800),
            panel_pos=_panel_position(width, height, margin),
            radius=round(width * 0.03),
            pad=round(width * 0.06),
            gap=round(width * 0.018),
            shadow1=round(width * 0.01),
            shadow2=round(width * 0.03),
            eyebrow_pt=round(width * 0.028),
            headline_pt=round(width * 0.085),
            subhead_pt=round(width * 0.034),
            cta_pt=round(width * 0.032),
            cta_py=round(width * 0.022),
            cta_px=round(width * 0.045),
            cta_mt=round(width * 0.01),
            panel_children=children,
        )
    except Exception as exc:  # fail open - a card string always comes back
        logger.warning("compose_card_html failed: %s", exc)
        safe = html.escape(str((copy or {}).get("headline", "")))
        return (
            "<!doctype html><html><head><meta charset='UTF-8'></head>"
            '<body><div id="root" data-composition-id="main" data-start="0" '
            f'data-duration="{_CARD_DURATION_S}" data-width="{width}" '
            f'data-height="{height}"><div id="headline">{safe}</div></div>'
            "</body></html>"
        )


def _write_card_project(project_dir: Path, html_text: str) -> None:
    """Write ``index.html`` + the hyperframes manifest into the project dir.

    ``assets/`` is created if absent but never cleared - the scene already
    lives there. Mirrors the video pipeline's project shape."""

    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "assets").mkdir(exist_ok=True)
    (project_dir / "index.html").write_text(html_text, encoding="utf-8")
    manifest = {
        "paths": {
            "blocks": "compositions",
            "components": "compositions/components",
            "assets": "assets",
        }
    }
    (project_dir / "hyperframes.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def render_card_png(project_dir: Path, png_path: Path) -> bool:
    """Render the static card project to a PNG. Returns True on success.

    hyperframes has no still/screenshot export (its ``render`` emits mp4/webm
    only), so this renders a short 1-frame MP4 via
    ``video_pipeline.run_hyperframes_render`` and extracts frame 0 with
    ``ffmpeg -frames:v 1``. Fail-open -> False; never raises."""

    try:
        import video_pipeline

        project_dir = Path(project_dir)
        png_path = Path(png_path)
        png_path.parent.mkdir(parents=True, exist_ok=True)

        mp4_path = project_dir / "_card.mp4"
        result = video_pipeline.run_hyperframes_render(project_dir, mp4_path, fps=1)
        if not (isinstance(result, dict) and result.get("ok")):
            logger.warning(
                "hyperframes render failed: %s",
                (result or {}).get("error") if isinstance(result, dict) else result,
            )
            return False

        exe = shutil.which("ffmpeg") or "ffmpeg"
        proc = subprocess.run(
            [exe, "-y", "-i", str(mp4_path), "-frames:v", "1", str(png_path)],
            capture_output=True,
            text=True,
            timeout=_resolve_ffmpeg_timeout(),
        )
        if proc.returncode != 0 or not png_path.is_file():
            logger.warning(
                "ffmpeg frame extract failed rc=%s: %s",
                proc.returncode,
                (proc.stderr or "")[-300:],
            )
            return False
        return True
    except Exception as exc:  # subprocess/timeout/import - fail open
        logger.warning("render_card_png failed: %s", exc)
        return False


def _card_filename(copy: dict) -> str:
    seed = json.dumps(copy or {}, sort_keys=True) + datetime.now().isoformat()
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return f"card-{digest}.png"


def generate_card(
    scene_prompt: str,
    copy: dict,
    *,
    design: dict,
    aspect: str = "9:16",
    out_dir: str,
    refs: list[str] | None = None,
    attempts: int = 3,
    scene_png: str | None = None,
) -> str | None:
    """Generate a still ad card PNG. Returns the absolute path, or None.

    When ``scene_png`` is given it is used as the background scene; otherwise a
    text-free scene is generated via ``video_imagegen.generate_image`` (identity
    locked to ``refs`` when supplied, ``attempts`` retries). The scene lands in
    the project's ``assets/``; the card HTML is composed over it and rendered to
    a PNG. Agnostic: ``design`` and ``refs`` are inputs. Never raises."""

    try:
        out_dir_path = Path(out_dir)
        out_dir_path.mkdir(parents=True, exist_ok=True)
        width, height = _dims_for_aspect(aspect)

        project_dir = Path(tempfile.mkdtemp(prefix="card-", dir=str(out_dir_path)))
        assets_dir = project_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        if scene_png:
            src = Path(scene_png)
            if not src.is_file():
                logger.warning("generate_card: scene_png not found: %s", scene_png)
                return None
            suffix = src.suffix.lower() if src.suffix.lower() in _IMAGE_SUFFIXES else ".png"
            dst = assets_dir / f"scene{suffix}"
            shutil.copyfile(src, dst)
            scene_rel = f"assets/{dst.name}"
        else:
            import video_imagegen

            scene_rel = video_imagegen.generate_image(
                scene_prompt,
                design or {},
                aspect,
                str(assets_dir),
                name="scene",
                refs=refs,
                attempts=attempts,
            )
            if not scene_rel:
                logger.warning("generate_card: scene generation returned nothing")
                return None

        html_text = compose_card_html(scene_rel, design or {}, copy or {}, width, height)
        _write_card_project(project_dir, html_text)

        png_path = out_dir_path / _card_filename(copy or {})
        if render_card_png(project_dir, png_path):
            return str(png_path)
        return None
    except Exception as exc:  # fail open - a failed card is None, never a crash
        logger.warning("generate_card failed: %s", exc)
        return None


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    ap = argparse.ArgumentParser(description="Still ad-card engine (scene + HTML overlay)")
    ap.add_argument("--scene", "--brief", dest="scene", default="",
                    help="Scene prompt for the AI background (text-free)")
    ap.add_argument("--scene-png", default=None,
                    help="Use this pre-generated PNG as the scene (skips generation)")
    ap.add_argument("--eyebrow", default="")
    ap.add_argument("--headline", default="")
    ap.add_argument("--accent", default="", help="Optional accent word/line")
    ap.add_argument("--subhead", default="")
    ap.add_argument("--cta", default="")
    ap.add_argument("--design-file", default=None,
                    help="Brand design JSON/MD (palette + fonts)")
    ap.add_argument("--persona-pack", default="",
                    help="Image persona pack for identity-locked scenes")
    ap.add_argument("--aspect", default="9:16", choices=list(_ASPECT_DIMS.keys()))
    ap.add_argument("--out", required=True, help="Output directory for the PNG")
    ap.add_argument("--attempts", type=int, default=3)
    args = ap.parse_args()

    import video_styles

    design_dict: dict = {}
    if args.design_file:
        try:
            design_dict = video_styles.resolve_design(design_file=args.design_file) or {}
        except Exception as exc:  # noqa: BLE001 - CLI convenience, fail open
            logger.warning("design resolve failed: %s", exc)

    refs_list: list[str] = []
    if args.persona_pack:
        try:
            from social.content_factory import _resolve_persona_refs

            refs_list = _resolve_persona_refs(args.persona_pack)
        except Exception as exc:  # noqa: BLE001
            logger.warning("persona refs resolve failed: %s", exc)

    copy_dict = {
        "eyebrow": args.eyebrow,
        "headline": args.headline,
        "accent": args.accent,
        "subhead": args.subhead,
        "cta": args.cta,
    }

    path = generate_card(
        args.scene,
        copy_dict,
        design=design_dict,
        aspect=args.aspect,
        out_dir=args.out,
        refs=refs_list or None,
        attempts=args.attempts,
        scene_png=args.scene_png,
    )
    print(path or "")
