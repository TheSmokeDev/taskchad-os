"""Tests for .archon/scripts/site-validate.py — one test per numbered check.

Design rule (CLAUDE.md Testing Principle): each test feeds a fixture that
VIOLATES exactly one check and asserts that check reports it. A neutered check
turns its test red — these are violation fixtures, not pass fixtures. The
base fixture itself is proven green first (test_base_fixture_passes_all)
so a failure can only mean the targeted violation was caught.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / ".archon" / "scripts" / "site-validate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("site_validate", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sv = _load_module()

FINE_PRINT = "Educational content only. Not advice of any kind."
ARTICLE_WORDS = " ".join(f"alpha{i} bravo{i} charlie{i}" for i in range(20))  # 60 unique words
HOME_WORDS = " ".join(f"delta{i} echo{i} foxtrot{i}" for i in range(20))


def make_config() -> dict:
    return {
        "slug": "acme-test",
        "base_path": "/acme-test",
        "canonical_base": "https://example.com/acme-test",
        "pages": [
            {"id": "home", "path": "", "template": "home"},
            {"id": "guide", "path": "guide", "template": "article"},
        ],
        "nav": [{"href": "/acme-test", "label": "Home"}],
        "nav_cta": {"href": "/acme-test/guide", "label": "Guide"},
        "extra_pages_allow": ["legacy/**"],
        "banned_phrases": ["peace of mind"],
        "allowed_phrases": [],
        "fine_print": FINE_PRINT,
        "fine_print_sha256": sv.hashlib.sha256(FINE_PRINT.encode()).hexdigest(),
        "number_whitelist": ["(555) 000-1111", "+15550001111"],
        "org_names": ["Acme Advisory"],
        "person_names": ["Alex Advisor"],
        "contact_email": "alex@example.com",
        "copy_gates": {"min_words": {"article": 30}, "max_overlap": 0.10},
        "meta_robots_noindex": False,
        "structured_data_pages": ["home"],
        "page_assets": {"home": {"og_image": "og.png", "hero_poster": "hero.webp"}},
    }


def page_html(body_words: str, jsonld: str = "", extra_body: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <title>T</title>{jsonld}
  </head>
  <body>
    <header><nav class="site-nav"><a href="/acme-test">Home</a></nav></header>
    <main>
      <section class="hero"><h1>Heading</h1></section>
      <section class="section"><p>{body_words}</p></section>{extra_body}
    </main>
    <footer>
      <p class="fine-print">{FINE_PRINT}</p>
    </footer>
    <script src="/acme-test/assets/site.js"></script>
  </body>
</html>
"""


JSONLD_OK = """
    <script type="application/ld+json">
      {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "name": "Acme Advisory",
         "telephone": "+15550001111", "email": "alex@example.com"}
      ]}
    </script>"""

CSS_OK = """:root {
  --primary: #111111;
  --primary-2: #222222;
  --ink: #101010;
  --muted: #555555;
  --accent: #cc9900;
  --surface: #fafafa;
  --line: #dddddd;
  --white: #ffffff;
}
body { color: var(--ink); }
"""


@pytest.fixture()
def site(tmp_path: Path) -> Path:
    """A minimal site that passes ALL 13 checks against make_config()."""
    root = tmp_path / "build"
    (root / "assets").mkdir(parents=True)
    (root / "guide").mkdir()
    (root / "index.html").write_text(page_html(HOME_WORDS, jsonld=JSONLD_OK), encoding="utf-8")
    (root / "guide" / "index.html").write_text(page_html(ARTICLE_WORDS), encoding="utf-8")
    (root / "assets" / "site.css").write_text(CSS_OK, encoding="utf-8")
    (root / "assets" / "site.js").write_text("(function () {})();\n", encoding="utf-8")
    (root / "assets" / "og.png").write_bytes(b"P" * 2048)
    (root / "assets" / "hero.webp").write_bytes(b"W" * 2048)
    (root / "vercel.json").write_text(
        json.dumps(
            {
                "cleanUrls": True,
                "headers": [
                    {"source": "/(.*)", "headers": [{"key": "X-Robots-Tag", "value": "noindex, nofollow"}]}
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def clean_repo(tmp_path: Path) -> Path:
    """A fake repo root for check 12 with a clean template pack."""
    repo = tmp_path / "repo"
    (repo / "site-templates" / "v1").mkdir(parents=True)
    (repo / "site-templates" / "v1" / "page.html").write_text(
        "<html><body>{{copy.headline}}</body></html>", encoding="utf-8"
    )
    (repo / ".archon" / "scripts").mkdir(parents=True)
    return repo


def failures(site_dir: Path, config: dict, check: int, repo: Path | None = None) -> list[str]:
    report = sv.validate_site(site_dir, config, born_clean_root=repo, checks={check})
    return report.failures.get(check, [])


def test_base_fixture_passes_all(site, clean_repo):
    report = sv.validate_site(site, make_config(), born_clean_root=clean_repo)
    assert report.failures == {}, f"base fixture must be green: {report.failures}"


def test_check_1_page_set(site):
    (site / "guide" / "index.html").unlink()
    (site / "rogue").mkdir()
    (site / "rogue" / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    found = failures(site, make_config(), 1)
    assert any("page_plan page missing: guide/index.html" in f for f in found)
    assert any("rogue/index.html" in f for f in found)


def test_check_2_internal_refs_resolve(site):
    html = (site / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        '<a href="/acme-test">Home</a>', '<a href="/acme-test/ghost-page">Ghost</a>'
    )
    (site / "index.html").write_text(html, encoding="utf-8")
    found = failures(site, make_config(), 2)
    assert any("/acme-test/ghost-page" in f for f in found)


def test_check_3_absolute_local_paths(site):
    html = (site / "index.html").read_text(encoding="utf-8")
    html = html.replace("<p>", '<p data-src="C:\\Users\\someone\\file.png">', 1)
    (site / "index.html").write_text(html, encoding="utf-8")
    found = failures(site, make_config(), 3)
    assert any("absolute local path" in f for f in found)
    # the https:// lookbehind: URLs must NOT trip the drive-letter pattern
    (site / "index.html").write_text(
        page_html(HOME_WORDS + ' see <a href="https://example.com/x">x</a>', jsonld=JSONLD_OK),
        encoding="utf-8",
    )
    assert failures(site, make_config(), 3) == []


def test_check_4_tokens_and_unresolved_slots(site):
    (site / "assets" / "site.css").write_text("body { color: black; }", encoding="utf-8")
    found = failures(site, make_config(), 4)
    assert any("not tokenized" in f for f in found)
    (site / "assets" / "site.css").write_text(CSS_OK, encoding="utf-8")
    html = (site / "index.html").read_text(encoding="utf-8").replace(
        "<h1>Heading</h1>", "<h1>{{copy.hero_headline}}</h1>"
    )
    (site / "index.html").write_text(html, encoding="utf-8")
    found = failures(site, make_config(), 4)
    assert any("unresolved slot" in f for f in found)


def test_check_5_banned_phrases(site):
    html = (site / "index.html").read_text(encoding="utf-8").replace(
        "<h1>Heading</h1>", "<h1>Total peace of mind — guaranteed</h1>"
    )
    (site / "index.html").write_text(html, encoding="utf-8")
    found = failures(site, make_config(), 5)
    assert any("banned phrase 'peace of mind'" in f for f in found)
    assert any("em-dash" in f for f in found)


def test_check_6_fine_print_hash(site):
    html = (site / "guide" / "index.html").read_text(encoding="utf-8").replace(
        FINE_PRINT, FINE_PRINT + " Plus an unapproved sentence."
    )
    (site / "guide" / "index.html").write_text(html, encoding="utf-8")
    found = failures(site, make_config(), 6)
    assert found == ["guide/index.html: fine-print missing or does not hash-match the profile"]


def test_check_7_forms_mailto_only(site):
    extra = '<form action="https://collector.example.com/submit"><input name="x" /></form>'
    (site / "index.html").write_text(
        page_html(HOME_WORDS, jsonld=JSONLD_OK, extra_body=extra), encoding="utf-8"
    )
    found = failures(site, make_config(), 7)
    assert any("form has action=" in f for f in found)
    extra = '<script src="https://cdn.example.com/tracker.js"></script>'
    (site / "index.html").write_text(
        page_html(HOME_WORDS, jsonld=JSONLD_OK, extra_body=extra), encoding="utf-8"
    )
    found = failures(site, make_config(), 7)
    assert any("third-party script" in f for f in found)


def test_check_8_noindex(site):
    (site / "vercel.json").unlink()
    found = failures(site, make_config(), 8)
    assert any("X-Robots-Tag" in f for f in found)
    # profile-required meta robots is enforced per page
    config = make_config()
    config["meta_robots_noindex"] = True
    (site / "vercel.json").write_text(
        json.dumps({"headers": [{"source": "/(.*)", "headers": [{"key": "X-Robots-Tag", "value": "noindex"}]}]}),
        encoding="utf-8",
    )
    found = failures(site, config, 8)
    assert any("meta robots noindex required" in f for f in found)


def test_check_9_packet_fact_whitelist(site):
    html = (site / "index.html").read_text(encoding="utf-8").replace(
        "<h1>Heading</h1>", "<h1>Call (999) 123-4567 or ask about License #AB12345</h1>"
    )
    (site / "index.html").write_text(html, encoding="utf-8")
    found = failures(site, make_config(), 9)
    assert any("(999) 123-4567" in f for f in found)
    assert any("License #AB12345" in f for f in found)
    # prose containing the word "licensed" must NOT trip the license pattern
    (site / "index.html").write_text(
        page_html(HOME_WORDS + " a licensed advisor helps", jsonld=JSONLD_OK), encoding="utf-8"
    )
    assert failures(site, make_config(), 9) == []


def test_check_10_jsonld_packet_facts(site):
    bad = JSONLD_OK.replace("Acme Advisory", "Totally Invented Partners LLC")
    (site / "index.html").write_text(page_html(HOME_WORDS, jsonld=bad), encoding="utf-8")
    found = failures(site, make_config(), 10)
    assert any("Totally Invented Partners LLC" in f for f in found)
    # a required structured-data page with NO block at all also fails
    (site / "index.html").write_text(page_html(HOME_WORDS), encoding="utf-8")
    found = failures(site, make_config(), 10)
    assert any("no JSON-LD block" in f for f in found)


def test_check_11_page_map_assets(site):
    (site / "assets" / "og.png").unlink()
    (site / "assets" / "hero.webp").write_bytes(b"tiny")
    found = failures(site, make_config(), 11)
    assert any("og_image asset missing" in f for f in found)
    assert any("suspiciously small" in f for f in found)


def test_check_12_born_clean(site, clean_repo):
    (clean_repo / "site-templates" / "v1" / "leaky.html").write_text(
        "<p>Call Alex Advisor at (555) 000-1111</p>", encoding="utf-8"
    )
    found = failures(site, make_config(), 12, repo=clean_repo)
    assert any("client literal" in f and "leaky.html" in f for f in found)


def test_check_13_copy_gates(site):
    (site / "guide" / "index.html").write_text(page_html("too short"), encoding="utf-8")
    found = failures(site, make_config(), 13)
    assert any("words < min 30" in f for f in found)
    # uniqueness: two pages with identical long main text exceed max_overlap
    (site / "index.html").write_text(page_html(ARTICLE_WORDS, jsonld=JSONLD_OK), encoding="utf-8")
    (site / "guide" / "index.html").write_text(page_html(ARTICLE_WORDS), encoding="utf-8")
    found = failures(site, make_config(), 13)
    assert any("shingle overlap" in f for f in found)


def test_cli_exit_codes(site, clean_repo, tmp_path):
    config_path = tmp_path / "validate.json"
    config_path.write_text(json.dumps(make_config()), encoding="utf-8")
    assert sv.main([str(site), "--config", str(config_path), "--born-clean-root", str(clean_repo)]) == 0
    (site / "vercel.json").unlink()
    assert sv.main([str(site), "--config", str(config_path), "--born-clean-root", str(clean_repo)]) == 1
