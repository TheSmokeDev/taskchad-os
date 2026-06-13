"""Unit tests for the video research stage (video_research).

ZERO network: every network seam is dependency-injected (fetch_html stubs
through url_fetch, fetch_bytes stubs for images, search stubs for theme
mode) or env-gated off (the search provider returns None without a key).
Covers:
  1. build_dossier URL mode: facts (number-bearing first), summary cap,
     audit entries, the cached html_text, derived design
  2. build_dossier theme mode: search + top-page merge, derived_design None,
     at most one reference image
  3. derive_design_from_page: complete validated dict from page tokens,
     None under two distinct hexes
  4. collect_reference_images: candidate order, caps, magic-byte AND
     content-type rejection (svg/spoof/oversize)
  5. search_web: no-key note, stubbed provider via module-attribute lookup,
     provider exception -> note (never a raise)
  6. claims_text_from + the claim-gate integration
  7. born-clean scan of the module and this test file
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import video_research  # noqa: E402

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Raster fixtures: real magic bytes, fake bodies.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake png body"
_JPG_BYTES = b"\xff\xd8\xff\xe0" + b"fake jpg body"
_WEBP_BYTES = b"RIFF" + b"\x10\x00\x00\x00" + b"WEBP" + b"fake webp body"
_SVG_BYTES = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"

# A page rich enough for the default extraction (>200 chars of article prose)
# with named custom props, hex styles, Google Fonts, and image candidates.
PAGE_HTML = """<!doctype html>
<html><head>
<title>Acme Rockets</title>
<meta property="og:image" content="/img/og-card.png" />
<meta name="twitter:image" content="https://cdn.acme.test/tw.jpg" />
<link rel="apple-touch-icon" href="/icons/touch.png" />
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400&display=swap" />
<style>
  :root { --background: #101014; --ink: #FAFAF5; --accent: #FF5500; }
  body { background: #101014; color: #FAFAF5; font-family: Inter, sans-serif; }
  h1 { color: #FF5500; font-family: "Space Grotesk", sans-serif; }
  .tag { color: #22AA88; }
</style>
</head>
<body>
<h1 style="color: #FF5500">Acme Rockets</h1>
<img src="/img/small.png" width="40" height="40" />
<img src="/img/big-photo.jpg" width="1200" height="630" />
<article>
<p>Acme shipped 42 rockets in 2025 and the program kept its full launch
calendar through the storm season without losing a single window. The team
grew to 18 engineers across two buildings. Acme was founded in a garage by
two friends who wanted reusable engines. The launch rate doubled across the
year as the new pad came online. Every test fire was logged and reviewed
before the next attempt, and the review board signed off on each campaign.
The company plans more launches and a bigger pad for the coming season.</p>
</article>
</body></html>"""


def _stub_page(url: str):
    return PAGE_HTML.encode("utf-8"), PAGE_HTML, "text/html"


def _article_page(text: str) -> str:
    return (
        "<!doctype html><html><head><title>Long Read</title></head><body>"
        f"<article><p>{text}</p></article></body></html>"
    )


# =============================================================================
# 1. BUILD_DOSSIER, URL MODE
# =============================================================================


def test_build_dossier_url_mode_facts_design_audit() -> None:
    dossier = video_research.build_dossier(
        "https://acme.test/story", fetch_html=_stub_page
    )
    assert dossier["ok"] is True
    assert dossier["mode"] == "url"
    assert dossier["query"] == "https://acme.test/story"
    assert dossier["url"] == "https://acme.test/story"
    assert "Acme" in dossier["title"]

    # Facts: capped, deduped, number-bearing sentences FIRST.
    assert dossier["facts"]
    assert len(dossier["facts"]) <= video_research.MAX_FACTS
    assert re.search(r"\d", dossier["facts"][0])
    assert any("42" in fact for fact in dossier["facts"])

    # Summary: non-empty, capped.
    assert dossier["summary_text"]
    assert len(dossier["summary_text"]) <= video_research.MAX_SUMMARY_CHARS

    # The page html is cached for two-phase image collection.
    assert "og-card.png" in dossier["html_text"]

    # Derived design: complete and role-mapped from the named custom props.
    design = dossier["derived_design"]
    assert isinstance(design, dict)
    assert design["palette"]["bg"] == "#101014"
    assert design["palette"]["fg"] == "#FAFAF5"
    assert design["palette"]["accent"] == "#FF5500"
    assert _HEX.match(design["palette"]["accent_dim"])
    assert design["fonts"]["display"] == "Space Grotesk"

    # Claims allowlist carries the title and the number-bearing facts.
    assert "Acme" in dossier["claims_text"]
    assert "42" in dossier["claims_text"]

    # Audit: exactly one fetch touch, successful, with sizes.
    fetches = [a for a in dossier["audit"] if a["action"] == "fetch"]
    assert len(fetches) == 1
    assert fetches[0]["ok"] is True
    assert fetches[0]["bytes"] > 0
    assert set(fetches[0].keys()) == {"t", "action", "target", "ok", "bytes", "ms", "note"}

    # No assets_dir given: no images collected, no image audit rows.
    assert dossier["images"] == []
    assert not [a for a in dossier["audit"] if a["action"] == "image"]


def test_build_dossier_url_mode_summary_capped() -> None:
    sentence = "The rocket program shipped 42 units this year and kept going. "
    page = _article_page(
        " ".join(f"Window {i} closed after {i + 3} hours of checks." for i in range(120))
        + " "
        + sentence
    )

    def stub(url: str):
        return page.encode("utf-8"), page, "text/html"

    dossier = video_research.build_dossier("https://acme.test/long", fetch_html=stub)
    assert dossier["ok"] is True
    assert len(dossier["summary_text"]) == video_research.MAX_SUMMARY_CHARS
    assert len(dossier["facts"]) == video_research.MAX_FACTS


def test_build_dossier_url_fetch_failure_is_ok_false(capsys) -> None:
    def boom(url: str):
        raise RuntimeError("network down")

    dossier = video_research.build_dossier("https://acme.test/x", fetch_html=boom)
    assert dossier["ok"] is False
    assert dossier["mode"] == "url"
    assert any("could not fetch" in note for note in dossier["notes"])
    fetches = [a for a in dossier["audit"] if a["action"] == "fetch"]
    assert len(fetches) == 1 and fetches[0]["ok"] is False
    assert "RuntimeError" in fetches[0]["note"]
    # The status line prints on failure too.
    assert "[video_research] GET https://acme.test/x -> error" in capsys.readouterr().out


def test_fetch_page_prints_status_and_truncates(capsys) -> None:
    big = "x" * (video_research.MAX_PAGE_BYTES + 50)
    page = f"<html><head><title>Big</title></head><body><p>{big}</p></body></html>"

    def stub(url: str):
        return page.encode("utf-8"), page, "text/html"

    audit: list = []
    result = video_research.fetch_page("https://acme.test/big", fetch_html=stub, audit=audit)
    assert result is not None
    assert result["bytes"] > video_research.MAX_PAGE_BYTES
    assert len(result["html_text"]) == video_research.MAX_PAGE_BYTES
    assert audit and "truncated" in audit[0]["note"]
    out = capsys.readouterr().out
    assert "[video_research] GET https://acme.test/big -> ok bytes=" in out
    assert "ms=" in out


def test_build_dossier_empty_query_is_ok_false() -> None:
    dossier = video_research.build_dossier("   ")
    assert dossier["ok"] is False
    assert any("empty research query" in note for note in dossier["notes"])


# =============================================================================
# 2. BUILD_DOSSIER, THEME MODE
# =============================================================================

PAGE_ONE = _article_page(
    "The league season ended with 42 matches played across the calendar and "
    "every stadium sold through its allocation before the opening week even "
    "arrived. Supporters traveled in record numbers for the finale."
)
PAGE_TWO = _article_page(
    "Average possession for the champions landed at 58% across the season "
    "and the back line conceded the fewest goals in the division. The squad "
    "kept the same starting eleven for most of the spring stretch."
)


def test_build_dossier_theme_mode_merges_top_pages() -> None:
    pages = {"https://one.test/a": PAGE_ONE, "https://two.test/b": PAGE_TWO}
    fetched: list[str] = []

    def stub_fetch(url: str):
        fetched.append(url)
        page = pages[url]
        return page.encode("utf-8"), page, "text/html"

    def stub_search(query: str, **kwargs):
        return (
            [
                {"title": "Season recap", "url": "https://one.test/a", "snippet": "s1"},
                {"title": "Numbers", "url": "https://two.test/b", "snippet": "s2"},
                {"title": "Extra", "url": "https://three.test/c", "snippet": "s3"},
            ],
            "",
        )

    dossier = video_research.build_dossier(
        "league season recap", fetch_html=stub_fetch, search=stub_search
    )
    assert dossier["ok"] is True
    assert dossier["mode"] == "theme"
    assert len(dossier["search"]) == 3
    # Only the top MAX_THEME_PAGES results are fetched.
    assert fetched == ["https://one.test/a", "https://two.test/b"]
    # Facts merge from BOTH pages.
    assert any("42" in fact for fact in dossier["facts"])
    assert any("58%" in fact for fact in dossier["facts"])
    # Theme dossiers never derive a design.
    assert dossier["derived_design"] is None
    # The first fetched page anchors url/title/html cache.
    assert dossier["url"] == "https://one.test/a"
    assert dossier["title"] == "Long Read"
    assert dossier["html_text"]
    # Audit: one search row + two fetch rows.
    assert [a["action"] for a in dossier["audit"]] == ["search", "fetch", "fetch"]


def test_build_dossier_theme_no_provider_is_ok_false() -> None:
    def no_provider(query: str, **kwargs):
        return [], "no search provider configured"

    def never(url: str):
        raise AssertionError("no page fetch without search results")

    dossier = video_research.build_dossier(
        "some theme", fetch_html=never, search=no_provider
    )
    assert dossier["ok"] is False
    assert "no search provider configured" in dossier["notes"]
    assert any("nothing usable" in note for note in dossier["notes"])
    assert dossier["audit"][0]["action"] == "search"
    assert dossier["audit"][0]["ok"] is False


def test_build_dossier_theme_collects_at_most_one_reference(tmp_path: Path) -> None:
    def stub_fetch(url: str):
        return PAGE_HTML.encode("utf-8"), PAGE_HTML, "text/html"

    def stub_search(query: str, **kwargs):
        return ([{"title": "T", "url": "https://acme.test/story", "snippet": "s"}], "")

    def stub_bytes(url: str):
        return _PNG_BYTES, "image/png"

    # Patch the default image fetcher through the module attribute so the
    # DI-less build_dossier path stays stubbed.
    original = video_research._fetch_image_bytes
    video_research._fetch_image_bytes = stub_bytes
    try:
        dossier = video_research.build_dossier(
            "acme rockets",
            assets_dir=str(tmp_path),
            fetch_html=stub_fetch,
            search=stub_search,
        )
    finally:
        video_research._fetch_image_bytes = original
    assert dossier["ok"] is True
    assert len(dossier["images"]) == 1
    assert dossier["images"][0]["kind"] == "og"


# =============================================================================
# 3. DERIVE_DESIGN_FROM_PAGE
# =============================================================================


def test_derive_design_complete_dict_all_roles_valid() -> None:
    design = video_research.derive_design_from_page(
        PAGE_HTML, "https://www.acme.test/story", title="Acme Rockets"
    )
    assert isinstance(design, dict)
    assert design["name"] == "acme"  # host label, www stripped
    for section in ("palette", "extras", "fonts", "motion", "flags"):
        assert isinstance(design.get(section), dict)
    for role in ("bg", "fg", "accent", "accent_dim"):
        assert _HEX.match(design["palette"][role]), role
    # Fonts from the Google Fonts link, mono falls back to the neutral mono.
    assert design["fonts"]["display"] == "Space Grotesk"
    assert design["fonts"]["body"] == "Inter"
    assert design["fonts"]["mono"]
    assert design["fonts"]["google_fonts_url"].startswith(
        "https://fonts.googleapis.com/css2?"
    )
    # The unmapped page color lands in extras.
    assert "#22AA88" in design["extras"].values()


def test_derive_design_under_two_hexes_is_none() -> None:
    one_hex = "<html><head><style>body { color: #101014; background: #101014; }</style></head><body></body></html>"
    assert video_research.derive_design_from_page(one_hex, "https://x.test") is None
    no_style = "<html><body><p>plain page, no styles at all</p></body></html>"
    assert video_research.derive_design_from_page(no_style, "https://x.test") is None
    assert video_research.derive_design_from_page("", "https://x.test") is None


# =============================================================================
# 4. COLLECT_REFERENCE_IMAGES
# =============================================================================


def test_collect_reference_images_order_caps_and_resolution(tmp_path: Path) -> None:
    served = {
        "https://acme.test/img/og-card.png": (_PNG_BYTES, "image/png"),
        "https://cdn.acme.test/tw.jpg": (_JPG_BYTES, "image/jpeg"),
        "https://acme.test/icons/touch.png": (_PNG_BYTES, "image/png"),
        "https://acme.test/img/big-photo.jpg": (_JPG_BYTES, "image/jpeg"),
    }
    requested: list[str] = []

    def stub_bytes(url: str):
        requested.append(url)
        return served[url]

    audit: list = []
    images = video_research.collect_reference_images(
        PAGE_HTML,
        "https://acme.test/story",
        tmp_path,
        fetch_bytes=stub_bytes,
        audit=audit,
    )
    # MAX_IMAGES cap: og, twitter, icon win; the content img never fetches.
    assert [img["kind"] for img in images] == ["og", "twitter", "icon"]
    assert requested == [
        "https://acme.test/img/og-card.png",  # relative og resolved on base
        "https://cdn.acme.test/tw.jpg",
        "https://acme.test/icons/touch.png",
    ]
    assert [Path(img["path"]).name for img in images] == ["ref0.png", "ref1.jpg", "ref2.png"]
    for img in images:
        assert Path(img["path"]).is_file()
    assert all(a["action"] == "image" and a["ok"] for a in audit)


def test_collect_reference_images_rejects_svg_spoof_oversize(tmp_path: Path) -> None:
    big_png = b"\x89PNG\r\n\x1a\n" + b"0" * video_research.MAX_IMAGE_BYTES
    served = {
        # svg: right type for svg, wrong magic for rasters -> rejected
        "https://acme.test/img/og-card.png": (_SVG_BYTES, "image/svg+xml"),
        # spoof: png magic but html content-type -> rejected (AND rule)
        "https://cdn.acme.test/tw.jpg": (_PNG_BYTES, "text/html"),
        # oversize raster -> rejected
        "https://acme.test/icons/touch.png": (big_png, "image/png"),
        # the largest content img is valid -> the only survivor
        "https://acme.test/img/big-photo.jpg": (_JPG_BYTES, "image/jpeg"),
    }

    def stub_bytes(url: str):
        return served[url]

    audit: list = []
    images = video_research.collect_reference_images(
        PAGE_HTML,
        "https://acme.test/story",
        tmp_path,
        fetch_bytes=stub_bytes,
        audit=audit,
    )
    assert [img["kind"] for img in images] == ["img"]
    assert Path(images[0]["path"]).name == "ref0.jpg"
    notes = " | ".join(a["note"] for a in audit if not a["ok"])
    assert "not a png/jpg/webp raster" in notes
    assert "size cap" in notes


def test_collect_reference_images_max_images_and_failures(tmp_path: Path) -> None:
    def stub_bytes(url: str):
        return _WEBP_BYTES, "image/webp"

    images = video_research.collect_reference_images(
        PAGE_HTML, "https://acme.test/story", tmp_path, fetch_bytes=stub_bytes, max_images=1
    )
    assert len(images) == 1
    assert Path(images[0]["path"]).name == "ref0.webp"

    def always_boom(url: str):
        raise RuntimeError("cdn down")

    audit: list = []
    assert (
        video_research.collect_reference_images(
            PAGE_HTML,
            "https://acme.test/story",
            tmp_path / "other",
            fetch_bytes=always_boom,
            audit=audit,
        )
        == []
    )
    assert audit and all(a["ok"] is False for a in audit)


# =============================================================================
# 5. SEARCH_WEB (module-attribute provider lookup)
# =============================================================================


def test_search_web_no_key_no_provider_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    results, note = video_research.search_web("anything")
    assert results == []
    assert note == "no search provider configured"


def test_search_web_stubbed_provider_via_module_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_exa(query: str, max_results: int):
        assert query == "rockets"
        return [
            {"title": "T1", "url": "https://one.test", "text": "body one"},
            {"title": "T2", "url": "https://two.test", "snippet": "snip two"},
            "not-a-dict",
        ]

    monkeypatch.setattr(video_research, "_search_exa", fake_exa)
    results, note = video_research.search_web("rockets")
    assert note == ""
    assert results == [
        {"title": "T1", "url": "https://one.test", "snippet": "body one"},
        {"title": "T2", "url": "https://two.test", "snippet": "snip two"},
    ]


def test_search_web_provider_exception_returns_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def angry(query: str, max_results: int):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(video_research, "_search_exa", angry)
    results, note = video_research.search_web("rockets")
    assert results == []
    assert "exa" in note and "RuntimeError" in note


def test_search_web_custom_provider_order_and_trim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_research, "_PROVIDER_ORDER", ("fake",))
    monkeypatch.setattr(
        video_research,
        "_search_fake",
        lambda query, max_results: [
            {"title": f"T{i}", "url": f"https://r{i}.test", "snippet": ""}
            for i in range(10)
        ],
        raising=False,
    )
    results, note = video_research.search_web("rockets", max_results=2)
    assert note == ""
    assert len(results) == 2


def test_search_exa_returns_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    assert video_research._search_exa("anything", 5) is None


# =============================================================================
# 6. CLAIMS_TEXT_FROM (+ claim-gate integration)
# =============================================================================


def test_claims_text_from_title_facts_numeric_sentences() -> None:
    dossier = {
        "title": "Acme Rockets",
        "facts": ["Acme shipped 42 rockets in 2025 across the program."],
        "summary_text": "The team grew to 18 engineers. The garage origin story is famous.",
    }
    claims = video_research.claims_text_from(dossier)
    assert "Acme Rockets" in claims
    assert "42" in claims
    assert "18 engineers" in claims
    assert "garage origin" not in claims  # numberless sentences stay out
    assert video_research.claims_text_from(None) == ""
    assert video_research.claims_text_from({}) == ""


def test_claims_text_allowlists_research_metrics_in_claim_gate() -> None:
    import video_pipeline

    claims = video_research.claims_text_from(
        {
            "title": "Season recap",
            "facts": ["Average possession landed at 58% for the champions."],
            "summary_text": "",
        }
    )
    assert "58%" in claims
    line = "They averaged 58% possession"
    # Claim-shaped metric without the research allowlist: rejected.
    assert not video_pipeline.check_claims(line, "a recap brief", "").ok
    # The dossier's claims_text as an extra source lets the same metric pass.
    assert video_pipeline.check_claims(line, "a recap brief", "", claims).ok


# =============================================================================
# 7. BORN-CLEAN (mirrors test_video_pipeline section 10)
# =============================================================================

_EM = chr(0x2014)

_FORBIDDEN = (
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
    "co" + "dex",  # the image-adapter provider name never appears here
)


def test_born_clean_research_module_and_tests() -> None:
    for path in (_SCRIPTS / "video_research.py", Path(__file__)):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for token in _FORBIDDEN:
            assert token.lower() not in lowered, f"{path.name} contains {token!r}"
        assert _EM not in text, f"{path.name} contains an em-dash"


def test_dossier_schema_is_json_safe_minus_html_text() -> None:
    dossier = video_research.build_dossier(
        "https://acme.test/story", fetch_html=_stub_page
    )
    payload = {k: v for k, v in dossier.items() if k != "html_text"}
    encoded = json.dumps(payload)
    assert "html_text" not in json.loads(encoded)
    expected_keys = {
        "ok",
        "mode",
        "query",
        "url",
        "title",
        "summary_text",
        "facts",
        "claims_text",
        "derived_design",
        "images",
        "search",
        "audit",
        "notes",
    }
    assert expected_keys.issubset(set(payload.keys()))
