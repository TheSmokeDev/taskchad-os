"""Tests for the still ad-card engine (scene + crisp HTML overlay -> PNG)."""

from __future__ import annotations

from pathlib import Path

import image_card
from image_card import compose_card_html, generate_card, render_card_png

_DESIGN = {
    "palette": {"bg": "#FFFFFF", "fg": "#0A1A2F", "accent": "#F47B25",
                "accent_dim": "#FCE0CC"},
    "fonts": {"display": "Nunito", "body": "Nunito", "display_weight": 800,
              "google_fonts_url": "https://fonts.googleapis.com/css2?family=Nunito"},
}
_COPY = {
    "eyebrow": "NEW DRIVER CA",
    "headline": "Cheaper coverage in minutes",
    "accent": "no phone calls",
    "subhead": "Compare real quotes side by side.",
    "cta": "Get my rate",
}


# --- compose_card_html: crisp text, brand palette, scene as background -------


def test_compose_contains_copy_as_real_html_text():
    html = compose_card_html("assets/scene.png", _DESIGN, _COPY, 1080, 1920)
    # Every copy slot appears as a real HTML text node (escaped), never baked.
    assert "Cheaper coverage in minutes" in html
    assert "Get my rate" in html
    assert "NEW DRIVER CA" in html
    assert "Compare real quotes side by side." in html
    assert 'id="headline"' in html and 'id="cta"' in html


def test_compose_uses_brand_palette_colors():
    html = compose_card_html("assets/scene.png", _DESIGN, _COPY, 1080, 1920)
    assert "#FFFFFF" in html  # palette.bg (panel + root)
    assert "#0A1A2F" in html  # palette.fg (headline)
    assert "#F47B25" in html  # palette.accent (eyebrow + CTA + accent word)


def test_compose_loads_google_fonts_link():
    html = compose_card_html("assets/scene.png", _DESIGN, _COPY, 1080, 1920)
    assert "<link rel=\"stylesheet\"" in html
    assert "fonts.googleapis.com" in html


def test_compose_scene_is_background_not_baked_text():
    html = compose_card_html("assets/scene.png", _DESIGN, _COPY, 1080, 1920)
    # The scene is a full-bleed background layer; the copy is NOT part of it.
    assert 'background-image: url("assets/scene.png")' in html
    # No copy string is ever fed into the image asset reference.
    scene_line = next(
        line for line in html.splitlines() if "background-image" in line
    )
    assert "Cheaper coverage" not in scene_line
    assert "Get my rate" not in scene_line


def test_compose_is_hyperframes_compatible():
    html = compose_card_html("assets/scene.png", _DESIGN, _COPY, 1080, 1920)
    assert 'data-composition-id="main"' in html
    assert 'data-width="1080"' in html and 'data-height="1920"' in html
    assert 'window.__timelines["main"]' in html


def test_compose_escapes_copy_html():
    html = compose_card_html(
        "assets/scene.png", _DESIGN, {"headline": "Save <big> & <fast>"}, 1080, 1920
    )
    assert "&lt;big&gt;" in html and "&amp;" in html
    assert "<big>" not in html


def test_compose_omits_empty_copy_slots():
    html = compose_card_html(
        "assets/scene.png", _DESIGN, {"headline": "Only me"}, 1080, 1920
    )
    assert 'id="headline"' in html
    assert 'id="eyebrow"' not in html
    assert 'id="cta"' not in html


def test_compose_agnostic_with_empty_design():
    # Empty design still composes a legible card off neutral fallbacks.
    html = compose_card_html("assets/scene.png", {}, {"headline": "Hi"}, 1080, 1920)
    assert 'data-composition-id="main"' in html
    assert "Hi" in html


# --- render_card_png: hyperframes render + ffmpeg frame extract --------------


def test_render_card_png_calls_render_then_ffmpeg(monkeypatch, tmp_path):
    calls: dict = {"render": None, "ffmpeg": None}

    import video_pipeline

    def fake_render(project_dir, mp4_path, *, fps):
        calls["render"] = (Path(project_dir), Path(mp4_path), fps)
        Path(mp4_path).write_bytes(b"fake-mp4")
        return {"ok": True, "error": "", "command": "npx hyperframes render"}

    def fake_ffmpeg(cmd, **kwargs):
        calls["ffmpeg"] = cmd
        # ffmpeg would write the PNG; simulate the frame-0 extract.
        out = Path(cmd[-1])
        out.write_bytes(b"\x89PNG\r\n")

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr(video_pipeline, "run_hyperframes_render", fake_render)
    monkeypatch.setattr(image_card.subprocess, "run", fake_ffmpeg)

    project = tmp_path / "proj"
    (project / "assets").mkdir(parents=True)
    png = tmp_path / "out" / "card.png"
    ok = render_card_png(project, png)

    assert ok is True
    assert png.is_file()
    # render ran with a 1-frame fps and the ffmpeg extract used -frames:v 1.
    assert calls["render"][2] == 1
    assert "-frames:v" in calls["ffmpeg"] and "1" in calls["ffmpeg"]
    assert str(png) == calls["ffmpeg"][-1]


def test_render_card_png_false_when_render_fails(monkeypatch, tmp_path):
    import video_pipeline

    monkeypatch.setattr(
        video_pipeline, "run_hyperframes_render",
        lambda *a, **k: {"ok": False, "error": "boom"},
    )
    ran_ffmpeg = {"called": False}

    def _no_ffmpeg(*a, **k):
        ran_ffmpeg["called"] = True

    monkeypatch.setattr(image_card.subprocess, "run", _no_ffmpeg)

    ok = render_card_png(tmp_path / "proj", tmp_path / "card.png")
    assert ok is False
    assert ran_ffmpeg["called"] is False  # never reaches ffmpeg on render failure


# --- generate_card: fail-open + orchestration --------------------------------


def test_generate_card_returns_none_when_scene_gen_fails(monkeypatch, tmp_path):
    import video_imagegen

    monkeypatch.setattr(video_imagegen, "generate_image", lambda *a, **k: None)
    called = {"render": False}
    monkeypatch.setattr(
        image_card, "render_card_png",
        lambda *a, **k: called.__setitem__("render", True) or True,
    )

    result = generate_card(
        "a scene", _COPY, design=_DESIGN, out_dir=str(tmp_path / "out")
    )
    assert result is None
    assert called["render"] is False  # never renders without a scene


def test_generate_card_happy_path_returns_png(monkeypatch, tmp_path):
    import video_imagegen

    def fake_gen(prompt, design, aspect, assets_dir, *, name, refs=None, attempts=1):
        # Simulate the scene landing in the project's assets/.
        Path(assets_dir, "scene.png").write_bytes(b"\x89PNG")
        return "assets/scene.png"

    monkeypatch.setattr(video_imagegen, "generate_image", fake_gen)

    composed: dict = {}

    def fake_render(project_dir, png_path):
        composed["html"] = Path(project_dir, "index.html").read_text(encoding="utf-8")
        Path(png_path).parent.mkdir(parents=True, exist_ok=True)
        Path(png_path).write_bytes(b"\x89PNG")
        return True

    monkeypatch.setattr(image_card, "render_card_png", fake_render)

    out = tmp_path / "out"
    result = generate_card(
        "a scene", _COPY, design=_DESIGN, aspect="9:16", out_dir=str(out)
    )
    assert result is not None
    assert Path(result).is_file()
    # The rendered project HTML carried the crisp overlay copy.
    assert "Cheaper coverage in minutes" in composed["html"]
    assert 'background-image: url("assets/scene.png")' in composed["html"]


def test_generate_card_uses_scene_png_without_generation(monkeypatch, tmp_path):
    import video_imagegen

    def _boom(*a, **k):
        raise AssertionError("generate_image must not be called with scene_png")

    monkeypatch.setattr(video_imagegen, "generate_image", _boom)
    monkeypatch.setattr(image_card, "render_card_png", lambda *a, **k: True)

    scene = tmp_path / "pre.png"
    scene.write_bytes(b"\x89PNG")
    result = generate_card(
        "ignored", _COPY, design=_DESIGN, out_dir=str(tmp_path / "out"),
        scene_png=str(scene),
    )
    assert result is not None


def test_generate_card_never_raises(monkeypatch, tmp_path):
    # A render explosion degrades to None, never propagates.
    import video_imagegen

    def fake_gen(prompt, design, aspect, assets_dir, *, name, refs=None, attempts=1):
        Path(assets_dir, "scene.png").write_bytes(b"\x89PNG")
        return "assets/scene.png"

    monkeypatch.setattr(video_imagegen, "generate_image", fake_gen)
    monkeypatch.setattr(
        image_card, "render_card_png",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")),
    )
    result = generate_card(
        "s", _COPY, design=_DESIGN, out_dir=str(tmp_path / "out"),
    )
    # The render raised, but generate_card swallows it — no exception escapes.
    assert result is None
