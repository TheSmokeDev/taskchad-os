"""Tests for the image lane (P3): image-brief.py job derivation and
image-gate.py's two modes. Violation fixtures per gate class — the render
driver itself shells an external CLI and is gated at its seams instead
(pack gate before, rendered gate after; both covered here).
"""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[3] / ".archon" / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ib = _load("image_brief", "image-brief.py")
ig = _load("image_gate", "image-gate.py")
pc = sys.modules["profile_compile"]


def minimal_png(width: int, height: int) -> bytes:
    """A real (tiny) PNG with the declared dimensions, padded past the size floor."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    row = b"\x00" + b"\x80" * width
    idat = zlib.compress(row * height)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )
    return png + b"\x00" * max(0, ig.SIZE_FLOOR + 1024 - len(png))


def make_profile(tmp_path: Path) -> Path:
    assets = tmp_path / "assets-src"
    (assets / "fonts").mkdir(parents=True)
    (assets / "stock.png").write_bytes(b"P" * 64)  # pre-existing asset: never re-jobbed
    font = {"family": "T", "stack": '"T", serif', "file": "fonts/t.woff2"}
    palette = {k: "#123456" for k in (
        "primary", "primary_2", "ink", "muted", "accent", "accent_deep", "accent_soft",
        "surface", "surface_2", "white", "line", "line_strong",
    )}
    profile = {
        "identity": {"slug": "acme", "display_name": "Alex Advisor", "org_name": "Acme Advisory", "vertical": "t"},
        "brand": {
            "brand_mark": "A",
            "palette": palette,
            "typography": {"display": dict(font), "body": dict(font), "mono": dict(font)},
            "voice_tone": "calm",
        },
        "facts": {
            "advisor": {"name": "Alex Advisor", "title": "Specialist"},
            "contact": {"phone_display": "(555) 000-1111", "phone_tel": "+15550001111", "email": "a@e.example"},
            "services": [{"id": "s1", "name": "Widgets", "short_label": "W", "form_label": "W", "path": "services/w"}],
        },
        "page_plan": {
            "nav": [{"page": "home", "label": "Home"}],
            "nav_cta": {"page": "home", "label": "Go"},
            "footer_links": [{"page": "home", "label": "Home"}],
            "pages": [
                {
                    "id": "home", "path": "", "template": "home",
                    "feature_img": "feature-story.png",
                    "meta": {"title": "H", "description": "D", "og_image": "hero-poster.webp"},
                    "hero": {"poster": "hero-poster.webp", "video_webm": "hero.webm", "video_mp4": "hero.mp4"},
                },
                {
                    "id": "about", "path": "about", "template": "consultation",
                    "meta": {"title": "A", "description": "D", "og_image": "stock.png"},
                    "hero": {"poster": "hero-about-poster.png"},
                },
            ],
        },
        "images": {
            "persona_pack": "none",
            "assets_dir": str(assets),
            "page_map": {
                "home": {"hero": {"concept": "a calm workshop at dawn"}, "feature": {"concept": "tools on a bench"}},
                "about": {"hero": {"concept": "a doorway with warm light", "aspect": "16:9"}},
            },
        },
        "video": {"pages": {"home": {"still": f"{tmp_path.as_posix()}/stills/home.png", "look": "kenburns"}}},
        "compliance": {"fine_print": "Educational only."},
        "copy_gates": {"min_words": {}, "max_overlap": 0.1},
        "deploy": {"held": True, "base_path": "/acme", "canonical_base": "https://e.example/acme"},
    }
    path = tmp_path / "client.yaml"
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    return path


def test_job_derivation_roles_and_skips(tmp_path):
    profile_path = make_profile(tmp_path)
    profile = pc.load_profile(profile_path)
    jobs, errors = ib.derive_jobs(profile, profile_path, tmp_path)
    assert errors == []
    by_stem = {j["out_stem"]: j for j in jobs}
    # home hero is a VIDEO page -> job renders the STILL into stills/, not the poster
    assert "home" in by_stem and by_stem["home"]["out_dir"].endswith("stills")
    # feature + about hero render into the overlay; og entries that already
    # exist (stock.png) or are video posters are never jobbed
    assert by_stem["feature-story"]["out_dir"].endswith("assets-generated")
    assert "hero-about-poster" in by_stem
    assert "hero-poster" not in by_stem and "stock" not in by_stem
    assert all(j["aspect"] == "16:9" for j in jobs)


def test_job_derivation_rejects_non_png_renderables(tmp_path):
    profile_path = make_profile(tmp_path)
    profile = pc.load_profile(profile_path)
    profile["page_plan"]["pages"][1]["hero"]["poster"] = "hero-about.webp"
    jobs, errors = ib.derive_jobs(profile, profile_path, tmp_path)
    assert any("must end .png" in e for e in errors)


def test_pack_gate_violations(tmp_path):
    jobs = [
        {"page_id": "home", "role": "hero", "out_stem": "home", "out_name": "home.png",
         "out_dir": str(tmp_path), "aspect": "16:9", "concept": "c"},
        {"page_id": "about", "role": "hero", "out_stem": "about-hero", "out_name": "about-hero.png",
         "out_dir": str(tmp_path), "aspect": "16:9", "concept": "c"},
    ]
    pack = {"assets": [
        {"out_stem": "home", "page_id": "home", "aspect": "1:1",     # aspect not echoed
         "prompt": "short — but with an em-dash and C:\\local\\path plus Alex Advisor posing"},
        {"out_stem": "ghost", "page_id": "x", "aspect": "16:9", "prompt": "p" * 20},  # unknown job
        # about-hero missing entirely
    ]}
    problems = ig.check_pack(pack, jobs, ["Alex Advisor"])
    text = "\n".join(problems)
    assert "page_id/aspect must echo" in text
    assert "unknown job: 'ghost'" in text
    assert "not covered by the pack: 'about-hero'" in text
    assert "em-dash" in text and "local path" in text and "packet person" in text


def test_rendered_gate_missing_small_magic_and_dims(tmp_path):
    job = {"page_id": "p", "role": "hero", "out_stem": "x", "out_name": "x.png",
           "out_dir": str(tmp_path), "aspect": "16:9", "concept": "c"}
    assert any("missing" in p for p in ig.check_rendered(job))
    (tmp_path / "x.png").write_bytes(b"tiny")
    problems = ig.check_rendered(job)
    assert any("floor" in p for p in problems) and any("magic" in p for p in problems)
    # a real PNG with square dims fails the 16:9 ratio check (Pillow present)
    (tmp_path / "x.png").write_bytes(minimal_png(1000, 1000))
    problems = ig.check_rendered(job)
    assert any("ratio" in p for p in problems)
    # correct wide dims pass
    (tmp_path / "x.png").write_bytes(minimal_png(1600, 900))
    assert ig.check_rendered(job) == []


def test_media_refs_accept_page_map_and_video_outputs(tmp_path):
    profile_path = make_profile(tmp_path)
    profile = pc.load_profile(profile_path)
    # home og reuses the video poster; about og pre-exists; posters/features via page_map
    assert pc.validate_media_refs(profile, profile_path, tmp_path) == []
    # drop the page_map entry -> about's rendered-only poster can no longer materialize
    del profile["images"]["page_map"]["about"]
    errors = pc.validate_media_refs(profile, profile_path, tmp_path)
    assert any("hero-about-poster.png" in e for e in errors)
