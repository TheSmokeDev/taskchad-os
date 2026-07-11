"""Tests for .archon/scripts/profile-compile.py (client-site factory P1).

Self-contained: a synthetic minimal profile is built in tmp_path — no test
depends on clients/ content (private, sanitizer-denied, absent publicly).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parents[3] / ".archon" / "scripts" / "profile-compile.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("profile_compile", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load_module()


def base_profile() -> dict:
    palette = {
        "primary": "#111111",
        "primary_2": "#222222",
        "ink": "#101010",
        "muted": "#555555",
        "accent": "#cc9900",
        "accent_deep": "#aa7700",
        "accent_soft": "#eeddaa",
        "surface": "#fafafa",
        "surface_2": "#f0f0f0",
        "white": "#ffffff",
        "line": "#dddddd",
        "line_strong": "#cccccc",
    }
    font = {"family": "Test Sans", "stack": '"Test Sans", sans-serif', "file": "fonts/test.woff2"}
    return {
        "schema_version": 1,
        "identity": {
            "slug": "acme-test",
            "display_name": "Alex Advisor",
            "org_name": "Acme Advisory",
            "vertical": "testing",
        },
        "brand": {
            "brand_mark": "A",
            "palette": palette,
            "typography": {"display": dict(font), "body": dict(font), "mono": dict(font)},
            "banned_phrases": ["peace of mind"],
            "opening_move": {
                "questions": [{"key": "q1", "question": "Ready?", "aria": "Ready"}],
                "priorities": {"q1": {"title": "Do it", "note": "Soon."}},
            },
        },
        "facts": {
            "advisor": {"name": "Alex Advisor", "title": "Specialist"},
            "org_names": ["Acme Advisory"],
            "contact": {
                "phone_display": "(555) 000-1111",
                "phone_tel": "+15550001111",
                "email": "alex@example.com",
            },
            "services": [
                {
                    "id": "widgets",
                    "name": "Widget Planning",
                    "short_label": "Widgets",
                    "form_label": "Widget Planning",
                    "path": "services/widgets",
                }
            ],
        },
        "page_plan": {
            "nav": [{"page": "home", "label": "Home"}],
            "nav_cta": {"page": "contact", "label": "Book"},
            "footer_links": [{"page": "home", "label": "Home"}, {"url": "https://example.com/", "label": "Ext"}],
            "pages": [
                {
                    "id": "home",
                    "path": "",
                    "template": "home",
                    "meta": {"title": "Home", "description": "D", "og_image": "og.png"},
                    "hero": {"poster": "hero.webp"},
                },
                {
                    "id": "contact",
                    "path": "contact",
                    "template": "consultation",
                    "meta": {"title": "Contact", "description": "D", "og_image": "og.png"},
                    "hero": {"poster": "hero.webp"},
                },
            ],
        },
        "images": {"persona_pack": "none", "assets_dir": "assets-src"},
        "compliance": {"fine_print": "Educational only. Not advice."},
        "copy_gates": {"min_words": {"article": 100}, "max_overlap": 0.1},
        "deploy": {
            "held": True,
            "base_path": "/acme-test",
            "canonical_base": "https://example.com/acme-test",
        },
    }


def write_profile(tmp_path: Path, profile: dict) -> Path:
    target = tmp_path / "client.yaml"
    target.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    return target


def write_compilable_profile(tmp_path: Path) -> Path:
    """A profile whose media refs MATERIALIZE — compile_profile checks the
    filesystem (fail-at-prepare), so compile-path tests stock the assets."""
    profile = base_profile()
    assets = tmp_path / "assets-src"
    assets.mkdir()
    (assets / "og.png").write_bytes(b"P" * 64)
    (assets / "hero.webp").write_bytes(b"W" * 64)
    profile["images"]["assets_dir"] = str(assets)
    return write_profile(tmp_path, profile)


def test_valid_profile_has_no_errors():
    assert pc.validate_profile(base_profile()) == []


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda p: p.pop("compliance"), "missing required section: compliance"),
        (lambda p: p["brand"]["palette"].pop("accent"), "brand.palette.accent"),
        (lambda p: p["brand"]["typography"]["body"].pop("stack"), "typography.body.stack"),
        (lambda p: p["page_plan"]["nav"].append({"page": "ghost", "label": "X"}), "unknown page: 'ghost'"),
        (lambda p: p["page_plan"]["pages"].append(dict(p["page_plan"]["pages"][0])), "duplicate page id"),
        (lambda p: p["facts"]["services"][0].pop("form_label"), "missing form_label"),
        (lambda p: p["copy_gates"].update(max_overlap=3), "max_overlap"),
        (lambda p: p["deploy"].update(held=False), "deploy.held must be true"),
        (lambda p: p["deploy"].update(canonical_base="http://insecure.example"), "canonical_base"),
        (lambda p: p["page_plan"]["pages"][0].update(template="mystery"), "unknown template"),
        (
            lambda p: p["brand"]["opening_move"]["priorities"].pop("q1"),
            "priorities missing entry",
        ),
    ],
)
def test_schema_violations_are_reported(mutate, fragment):
    profile = copy.deepcopy(base_profile())
    mutate(profile)
    errors = pc.validate_profile(profile)
    assert errors, f"expected a schema error containing {fragment!r}"
    assert any(fragment in e for e in errors), f"{fragment!r} not in {errors}"


def test_load_profile_raises_with_all_errors(tmp_path):
    """Every error is collected in one pass (missing SECTIONS short-circuit,
    so use two same-depth violations)."""
    profile = base_profile()
    profile["deploy"]["held"] = False
    profile["deploy"]["canonical_base"] = "http://insecure.example"
    path = write_profile(tmp_path, profile)
    with pytest.raises(pc.ProfileError) as exc_info:
        pc.load_profile(path)
    assert len(exc_info.value.errors) >= 2


def test_compile_writes_three_views(tmp_path):
    path = write_compilable_profile(tmp_path)
    written = pc.compile_profile(path)
    assert set(written) == {"packet.json", "design.json", "validate.json"}
    for target in written.values():
        assert target.exists()


def test_validate_view_carries_gate_config(tmp_path):
    path = write_compilable_profile(tmp_path)
    written = pc.compile_profile(path)
    view = json.loads(written["validate.json"].read_text(encoding="utf-8"))
    assert view["fine_print_sha256"] == pc.fine_print_hash("Educational only. Not advice.")
    assert "(555) 000-1111" in view["number_whitelist"]
    assert view["person_names"] == ["Alex Advisor", "Alex Advisor"] or "Alex Advisor" in view["person_names"]
    assert view["nav_cta"]["href"] == "/acme-test/contact"
    assert view["pages"][0]["path"] == ""
    assert view["contact_email"] == "alex@example.com"


def test_fine_print_hash_is_whitespace_normalized():
    assert pc.fine_print_hash("A  B\n C") == pc.fine_print_hash("A B C")


def test_packet_is_the_only_fact_surface(tmp_path):
    """The packet view must carry the writer's facts and nothing filesystem-y."""
    path = write_compilable_profile(tmp_path)
    written = pc.compile_profile(path)
    packet = json.loads(written["packet.json"].read_text(encoding="utf-8"))
    assert packet["contact"]["email"] == "alex@example.com"
    assert packet["services"][0]["id"] == "widgets"
    assert "assets_dir" not in json.dumps(packet)


def test_real_client_profile_compiles_if_present():
    real = _SCRIPT.parents[2] / "clients" / "rebecca-dominguez-experior-financial" / "client.yaml"
    if not real.exists():
        pytest.skip("private clients/ profile not present (public checkout)")
    profile = pc.load_profile(real)
    assert profile["deploy"]["held"] is True
    assert len(profile["page_plan"]["pages"]) == 10
