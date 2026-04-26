"""Tests for url_fetch.py — gap-4 URL ingest.

All unit tests stub fetch_html via dependency injection. ZERO network in unit suite.
One @pytest.mark.network test does a real fetch — skipped unless explicitly enabled.

R1 regression coverage:
  * test_byte_perfect_html_archive — archive bytes must == raw response bytes
  * test_router_url_branch_fires_via_raw_regex — router routes via raw-regex match,
    NOT via parsed[0] == "vault-ingest" (the iteration-1 dead-code bug)
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Canned HTML used by stubs (>200 chars so default extraction wins, no
# Firecrawl fallback)
# ---------------------------------------------------------------------------

_RICH_HTML_BYTES = (
    b"<!doctype html><html><head>"
    b"<title>Demo Page Title</title>"
    b"</head><body>"
    b"<article><h1>Demo Page Title</h1>"
    b"<p>This is a sufficiently long paragraph to exceed the MIN_EXTRACT_CHARS "
    b"threshold of two hundred characters so the default trafilatura extraction "
    b"wins and the Firecrawl fallback does not fire. We need lots of words here, "
    b"so we keep on writing prose to make sure we cross that threshold cleanly. "
    b"This is the second sentence. And a third for good measure.</p>"
    b"<p>A second paragraph adds even more body so Trafilatura sees a real article "
    b"with multiple blocks and produces a meaningful markdown extraction without "
    b"any odd edge-case behavior.</p>"
    b"</article></body></html>"
)
_RICH_HTML_TEXT = _RICH_HTML_BYTES.decode("utf-8")

_THIN_HTML_BYTES = b"<html><body><p>tiny</p></body></html>"
_THIN_HTML_TEXT = _THIN_HTML_BYTES.decode("utf-8")


def _stub_rich(url: str) -> tuple[bytes, str, str]:
    return _RICH_HTML_BYTES, _RICH_HTML_TEXT, "text/html"


def _stub_thin(url: str) -> tuple[bytes, str, str]:
    return _THIN_HTML_BYTES, _THIN_HTML_TEXT, "text/html"


# ---------------------------------------------------------------------------
# 1-5: slug / detection helpers
# ---------------------------------------------------------------------------


class TestIsURL:
    def test_is_url_matches_http_https(self):
        from url_fetch import is_url

        assert is_url("https://example.com/foo")
        assert is_url("http://example.com")
        assert is_url("  https://example.com  ")  # strip
        assert not is_url("example.com")
        assert not is_url("/local/path")
        assert not is_url("")
        assert not is_url("https://example.com with extra")  # whitespace inside


class TestURLSlug:
    def test_url_slug_lowercases(self):
        from url_fetch import _url_slug

        assert _url_slug("My Page Title") == "my-page-title"
        # Numeric prefixes stripped (mirrors concept_drafter._slugify regex)
        assert _url_slug("1. Hello World") == "hello-world"
        # Punctuation gone
        assert _url_slug("Hello, World!") == "hello-world"


class TestDeriveSlug:
    def test_derive_slug_title_first(self):
        from url_fetch import derive_slug

        assert derive_slug("LLM Wiki", "https://x.com/path") == "llm-wiki"

    def test_derive_slug_falls_back_to_url_last_segment(self):
        from url_fetch import derive_slug

        assert derive_slug("", "https://example.com/posts/llm-wiki/") == "llm-wiki"
        assert derive_slug("", "https://example.com/posts/llm-wiki") == "llm-wiki"

    def test_derive_slug_final_fallback_uses_timestamp(self):
        from url_fetch import derive_slug

        # No title and bare host → clipped-YYYYMMDD-HHMM
        slug = derive_slug("", "https://example.com/")
        assert re.match(r"^clipped-\d{8}-\d{4}$", slug), slug


# ---------------------------------------------------------------------------
# 6-9: fetch() behavior — DI fetch_html, Firecrawl fallback, ISO8601
# ---------------------------------------------------------------------------


class TestFetch:
    def test_fetch_uses_injected_html_callable(self, monkeypatch):
        """fetch() routes through the injected fetch_html, NOT the network."""
        from url_fetch import fetch

        called = {"count": 0}

        def stub(url: str):
            called["count"] += 1
            return _stub_rich(url)

        # Make the test deterministic against Firecrawl env presence
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

        result = fetch("https://example.com/article", fetch_html=stub)

        assert called["count"] == 1
        assert result.url == "https://example.com/article"
        assert result.title == "Demo Page Title"
        assert "demo page title" in result.markdown.lower()
        assert result.html_bytes == _RICH_HTML_BYTES
        assert result.html_text == _RICH_HTML_TEXT
        assert result.content_type == "text/html"

    def test_fetch_returns_iso8601_utc_fetched_at(self, monkeypatch):
        from url_fetch import fetch

        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        result = fetch("https://example.com", fetch_html=_stub_rich)
        # ISO 8601 with +00:00 (UTC)
        parsed = datetime.fromisoformat(result.fetched_at)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)

    def test_fetch_short_extract_attempts_firecrawl_fallback(self, monkeypatch):
        """Thin extract + FIRECRAWL_API_KEY set + richer fallback → fallback wins."""
        import url_fetch

        monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-key")
        rich_md = "# Big Article\n\n" + ("Body. " * 100)
        rich_html = "<html>rich</html>"

        def fake_firecrawl(url: str):
            return rich_md, rich_html

        monkeypatch.setattr(url_fetch, "_firecrawl_fallback", fake_firecrawl)

        result = url_fetch.fetch("https://example.com", fetch_html=_stub_thin)

        assert result.markdown == rich_md
        assert result.html_text == rich_html
        assert result.html_bytes == rich_html.encode("utf-8")

    def test_fetch_no_firecrawl_key_returns_thin_extract(self, monkeypatch):
        """No FIRECRAWL_API_KEY → no fallback attempt, original (thin) markdown returned."""
        import url_fetch

        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

        # Sentinel: if _firecrawl_fallback gets called, blow up (it shouldn't —
        # the env-gate inside it returns None before any HTTP work, but we want
        # to be sure we don't even traverse the fallback branch when extraction
        # is short. Actually fetch() does call _firecrawl_fallback; the function
        # itself short-circuits. So we just assert behavior, not the call.)
        result = url_fetch.fetch("https://example.com", fetch_html=_stub_thin)

        # Thin HTML produces empty/short markdown; no fallback applied → original bytes preserved
        assert result.html_bytes == _THIN_HTML_BYTES


# ---------------------------------------------------------------------------
# 10-12: fetch_and_archive — disk layout, idempotency, immutability
# ---------------------------------------------------------------------------


class TestFetchAndArchive:
    def test_fetch_and_archive_writes_html_and_md_with_frontmatter(
        self, tmp_path, monkeypatch
    ):
        """Both files land in raw/clipped/, md opens with YAML frontmatter."""
        from url_fetch import fetch_and_archive

        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        vault = tmp_path / "vault"

        html_path, md_path, content = fetch_and_archive(
            "https://example.com/demo", vault, fetch_html=_stub_rich
        )

        # Disk layout
        assert html_path.parent == vault / "raw" / "clipped"
        assert md_path.parent == vault / "raw" / "clipped"
        assert html_path.suffix == ".html"
        assert md_path.suffix == ".md"
        # Date-prefixed slugified base
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert html_path.name.startswith(today)
        assert md_path.name.startswith(today)

        md_text = md_path.read_text(encoding="utf-8")
        assert md_text.startswith("---\n")
        assert 'source_url: "https://example.com/demo"' in md_text
        assert 'fetched_at:' in md_text
        assert content.title == "Demo Page Title"

    def test_byte_perfect_html_archive(self, tmp_path, monkeypatch):
        """R1 fix #3 regression: the archived .html bytes are EXACTLY the response bytes.

        Pre-R1 used ``resp.text.encode("utf-8")`` which silently lossy-converts
        non-UTF-8-encoded sources. Karpathy raw/ promise demands byte-perfect.
        """
        from url_fetch import fetch_and_archive

        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        # Construct bytes that round-trip through a non-UTF-8 path would mangle.
        # Use a Latin-1-only byte (0xE9) embedded as a meta-charset Latin-1 page.
        latin1_html_bytes = (
            b"<!doctype html><html><head>"
            b'<meta charset="latin-1">'
            b"<title>Caf\xe9 Article</title></head><body>"
            b"<article><h1>Caf\xe9 Article</h1>"
            b"<p>This article is about caf\xe9s and has enough body text to clear "
            b"the two hundred char threshold so the default extractor wins. "
            b"More words. Even more words. And then some additional sentences. "
            b"To be safe, we keep on writing more prose so the cutoff is cleared.</p>"
            b"</article></body></html>"
        )
        latin1_html_text = latin1_html_bytes.decode("latin-1")

        def stub(url: str):
            return latin1_html_bytes, latin1_html_text, "text/html"

        vault = tmp_path / "vault"
        html_path, _md, _content = fetch_and_archive(
            "https://example.com/cafe", vault, fetch_html=stub
        )

        assert html_path.read_bytes() == latin1_html_bytes
        # Verify the lossy-path would have differed (sanity check that this is a
        # meaningful regression test — UTF-8 re-encode would NOT equal the latin-1 bytes)
        assert latin1_html_text.encode("utf-8") != latin1_html_bytes

    def test_fetch_and_archive_idempotent_on_same_bytes(self, tmp_path, monkeypatch):
        """Same URL + same fetched bytes → second call returns same paths, no exception."""
        from url_fetch import fetch_and_archive

        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        vault = tmp_path / "vault"

        html_path1, md_path1, _ = fetch_and_archive(
            "https://example.com/demo", vault, fetch_html=_stub_rich
        )
        html_path2, md_path2, _ = fetch_and_archive(
            "https://example.com/demo", vault, fetch_html=_stub_rich
        )

        assert html_path1 == html_path2
        assert md_path1 == md_path2
        # No duplicate files in raw/clipped/
        clipped = vault / "raw" / "clipped"
        html_files = list(clipped.glob("*.html"))
        md_files = list(clipped.glob("*.md"))
        assert len(html_files) == 1
        assert len(md_files) == 1

    def test_fetch_and_archive_raises_on_divergent_bytes(self, tmp_path, monkeypatch):
        """Pre-existing archive at the same path with different bytes → FileExistsError.

        raw/ immutability — preserve_raw on_collision='skip' raises when bytes diverge.
        """
        from url_fetch import fetch_and_archive

        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        vault = tmp_path / "vault"

        html_path1, md_path1, _ = fetch_and_archive(
            "https://example.com/demo", vault, fetch_html=_stub_rich
        )

        # Now create a different stub that returns different bytes for the same URL.
        # The slug is derived from the title which is the same in both stubs, so the
        # destination path is the same → divergent bytes → FileExistsError.
        divergent_bytes = _RICH_HTML_BYTES + b"<!-- modified -->"
        divergent_text = divergent_bytes.decode("utf-8")

        def stub_divergent(url: str):
            return divergent_bytes, divergent_text, "text/html"

        with pytest.raises(FileExistsError):
            fetch_and_archive(
                "https://example.com/demo", vault, fetch_html=stub_divergent
            )

        # Original archive untouched
        assert html_path1.read_bytes() == _RICH_HTML_BYTES


# ---------------------------------------------------------------------------
# 13: CLI dispatch — entity_extractor.py fetch-url subcommand
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_fetch_url_invokes_pipeline(self, tmp_path, monkeypatch):
        """CLI subcommand 'fetch-url ... --no-compile' archives both files and exits 0.

        Uses subprocess so we exercise the real argparse + dispatch wiring.
        Stubs network by monkeypatching at the entry point via a sitecustomize-style
        env-driven hook would be heavy; simpler: monkeypatch url_fetch._fetch_html
        in-process using the public ``fetch_and_archive`` instead, then rely on the
        subprocess test to exercise the CLI argv path with a tiny test stub script.
        """
        # Build a small test driver script that imports url_fetch, monkeypatches
        # _fetch_html with a canned bytes/text return, then calls the CLI dispatch.
        vault = tmp_path / "vault"
        vault.mkdir()

        driver = tmp_path / "driver.py"
        driver.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "scripts_dir = Path(r'"
            + str(Path(__file__).resolve().parent.parent)
            + "')\n"
            "sys.path.insert(0, str(scripts_dir))\n"
            "import url_fetch\n"
            "_RICH = "
            + repr(_RICH_HTML_BYTES)
            + "\n"
            "url_fetch._fetch_html = lambda url, *, timeout=20: (_RICH, _RICH.decode('utf-8'), 'text/html')\n"
            "import entity_extractor\n"
            "sys.argv = ["
            "'entity_extractor.py', 'fetch-url', 'https://example.com/demo', "
            f"'--vault-dir', r'{vault}', '--no-compile']\n"
            "entity_extractor.main()\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # Both archive paths printed
        assert "Archived:" in result.stdout
        # Files exist
        clipped = vault / "raw" / "clipped"
        assert any(clipped.glob("*.html"))
        assert any(clipped.glob("*.md"))


# ---------------------------------------------------------------------------
# 14-15: Router integration — raw-regex match BEFORE _parse_command (R1 fix #1)
# ---------------------------------------------------------------------------


class _RouterOnlyManager:
    """Minimal ExtensionManager stand-in for router unit tests."""

    command_regex = re.compile(r"^/(\w+(?:-\w+)*)\b(.*)$")

    def get_router_commands(self):
        return set()

    def get_all_command_names(self):
        return ["status", "clear"]

    async def dispatch(self, command, adapter, incoming, args, collect_only=False):
        return None

    def detect_intents(self, text):
        return []

    def wants_analysis(self, text):
        return False


class _FakeEngine:
    session_store = None

    async def handle_message(self, message, progress=None):
        if False:
            yield None


def _build_test_router():
    """Construct a ChatRouter with no-op engine + minimal manager."""
    from router import ChatRouter

    return ChatRouter(_FakeEngine(), _RouterOnlyManager())


def _build_test_incoming(text: str):
    """Build an IncomingMessage with valid User/Channel/Platform fields."""
    from datetime import datetime as _dt

    from models import Channel, IncomingMessage, Platform, User

    return IncomingMessage(
        text=text,
        user=User(Platform.CLI, "cli-user", "Tester"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=_dt.now(),
    )


class TestRouterURLBranch:
    """The router must short-circuit on /vault-ingest <url> via a raw-regex match
    on the original message text — NOT via parsed[0] == 'vault-ingest' (which is
    the iteration-1 dead-code bug since vault-ingest is a Skill, not a router cmd).
    """

    @pytest.mark.asyncio
    async def test_router_url_branch_fires_via_raw_regex(self, monkeypatch):
        """/vault-ingest https://x.com → _handle_vault_ingest_url is invoked."""
        router = _build_test_router()

        called = {"with_url": None}

        async def fake_handler(adapter, incoming, url):
            called["with_url"] = url

        monkeypatch.setattr(router, "_handle_vault_ingest_url", fake_handler)

        adapter = MagicMock()
        adapter.send = AsyncMock()

        incoming = _build_test_incoming("/vault-ingest https://example.com/article")

        await router._handle_inner(adapter, incoming)

        assert called["with_url"] == "https://example.com/article"

    @pytest.mark.asyncio
    async def test_router_non_url_falls_through(self, monkeypatch):
        """/vault-ingest /local/path falls through to the normal engine path
        (i.e. _handle_vault_ingest_url is NOT called)."""
        router = _build_test_router()
        called = {"hit": False}

        async def fake_handler(adapter, incoming, url):
            called["hit"] = True

        monkeypatch.setattr(router, "_handle_vault_ingest_url", fake_handler)

        adapter = MagicMock()
        adapter.send = AsyncMock()

        incoming = _build_test_incoming("/vault-ingest /some/local/path.md")

        # We don't care if downstream dispatch raises here — we only care about
        # whether the URL fast-path fired.
        try:
            await router._handle_inner(adapter, incoming)
        except Exception:
            pass

        assert called["hit"] is False


# ---------------------------------------------------------------------------
# Real network smoke — opt-in only
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_real_url_smoke(tmp_path, monkeypatch):
    """Live fetch of https://example.com — confirms requests + trafilatura wiring.

    Skipped unless the 'network' mark is enabled, e.g.
    `uv run pytest -m network tests/test_url_fetch.py`.
    """
    from url_fetch import fetch_and_archive

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    vault = tmp_path / "vault"
    html_path, md_path, content = fetch_and_archive("https://example.com", vault)
    assert html_path.exists()
    assert md_path.exists()
    assert content.html_bytes  # non-empty
