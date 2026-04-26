"""URL fetcher for /vault-ingest URL ingest (gap-4).

Default stack: requests + trafilatura (pure Python, no API key).
Firecrawl fallback: env-gated via FIRECRAWL_API_KEY for JS-heavy pages.
Tests stub `fetch_html` directly via dependency injection — zero network in unit suite.

R1 fixes baked in:
  - FetchedContent carries BOTH html_bytes (raw provenance) and html_text (decoded for
    trafilatura). The archive on disk uses html_bytes — byte-perfect Karpathy raw/.
  - Title extraction uses an explicit None guard around trafilatura.extract_metadata —
    NEVER references a stub _Empty() class.
  - Archiving routes through entity_extractor.preserve_raw(..., subdir="clipped",
    on_collision="skip"); we don't roll a separate idempotent-write helper.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"^https?://\S+$")
DEFAULT_TIMEOUT_S = 20
MIN_EXTRACT_CHARS = 200  # below this, try Firecrawl fallback


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FetchedContent:
    """Result of a URL fetch + extract.

    html_bytes is the raw response body (byte-perfect provenance, written to .html).
    html_text is the decoded string used by trafilatura for extraction.
    """

    url: str
    title: str
    html_bytes: bytes
    html_text: str
    markdown: str
    fetched_at: str  # ISO 8601 UTC
    content_type: str


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_url(text: str) -> bool:
    """True iff ``text`` is a single ``https?://...`` URL with no whitespace."""
    return bool(URL_RE.match(text.strip()))


def _url_slug(text: str) -> str:
    """Lowercase-kebab slug. Mirrors concept_drafter._slugify regex but lowercases.

    Strips leading numeric prefixes ("1. ", "2- "), removes punctuation,
    collapses whitespace/underscores into single hyphens.
    """
    s = re.sub(r"^[\d]+[\.\-\s]+", "", text.strip())
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s.lower()


def derive_slug(title: str, url: str) -> str:
    """Three-tier slug derivation.

    1. Title → kebab. If non-empty, win.
    2. Last path segment of the URL → kebab.
    3. Final fallback: ``clipped-{YYYYMMDD-HHMM}`` (uses UTC).
    """
    if title:
        slug = _url_slug(title)
        if slug:
            return slug
    parsed = urlparse(url)
    last_segment = parsed.path.rstrip("/").split("/")[-1] if parsed.path else ""
    if last_segment:
        slug = _url_slug(last_segment)
        if slug:
            return slug
    return f"clipped-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"


# ---------------------------------------------------------------------------
# Network — tests stub fetch_html via dependency injection
# ---------------------------------------------------------------------------


def _fetch_html(url: str, *, timeout: int = DEFAULT_TIMEOUT_S) -> tuple[bytes, str, str]:
    """Default fetcher.

    Returns (raw_bytes, decoded_text, content_type). Stubbable in tests via
    the ``fetch_html`` parameter on ``fetch()`` / ``fetch_and_archive()``.

    Uses a browser-shaped User-Agent. Several blog hosts (Bear Blog,
    Cloudflare-fronted sites) reject custom CLI UAs with 403 — but a
    descriptive identifier is appended so server logs still see who we are.
    """
    import requests

    resp = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36 TheHomie-VaultIngest/1.0"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "text/html").split(";")[0].strip()
    return resp.content, resp.text, content_type


def _firecrawl_fallback(url: str) -> tuple[str, str] | None:
    """Optional fallback. Returns (markdown, html) or None if not configured/failed.

    Env-gated: only fires when ``FIRECRAWL_API_KEY`` is set. Used when default
    extraction returned <MIN_EXTRACT_CHARS chars (probably JS-rendered page).
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return None
    try:
        import requests

        resp = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"url": url, "formats": ["markdown", "html"]},
            timeout=DEFAULT_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {}) or {}
        md = data.get("markdown") or ""
        html = data.get("html") or ""
        if md:
            return md, html
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def fetch(
    url: str,
    *,
    fetch_html: Callable[..., tuple[bytes, str, str]] | None = None,
) -> FetchedContent:
    """Fetch + extract. ``fetch_html`` is dependency-injected for tests.

    Stub returns must be ``(html_bytes, html_text, content_type)``.
    """
    fetcher = fetch_html or _fetch_html
    html_bytes, html_text, content_type = fetcher(url)

    import trafilatura

    md = (
        trafilatura.extract(
            html_text,
            output_format="markdown",
            include_links=True,
            include_images=False,
            with_metadata=False,
        )
        or ""
    )

    # Explicit None guard (R1 fix #2) — extract_metadata can return None for
    # malformed/empty HTML. Never reference a stub _Empty() class.
    meta = trafilatura.extract_metadata(html_text)
    title = (meta.title if meta and getattr(meta, "title", None) else "") or ""

    # Firecrawl fallback when default extraction is too thin (JS-heavy page).
    if len(md.strip()) < MIN_EXTRACT_CHARS:
        fc = _firecrawl_fallback(url)
        if fc:
            fc_md, fc_html = fc
            if len(fc_md.strip()) > len(md.strip()):
                md = fc_md
                if fc_html:
                    html_text = fc_html
                    html_bytes = fc_html.encode("utf-8")

    return FetchedContent(
        url=url,
        title=title,
        html_bytes=html_bytes,
        html_text=html_text,
        markdown=md,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        content_type=content_type,
    )


# ---------------------------------------------------------------------------
# Archive — writes html + md into {vault}/raw/clipped/ via preserve_raw
# ---------------------------------------------------------------------------


def fetch_and_archive(
    url: str,
    vault_dir: Path,
    *,
    fetch_html: Callable[..., tuple[bytes, str, str]] | None = None,
) -> tuple[Path, Path, FetchedContent]:
    """Fetch URL, archive html+md to ``{vault}/raw/clipped/``, return (html_path, md_path, content).

    Markdown gets ``source_url`` and ``fetched_at`` injected as YAML frontmatter
    BEFORE archive — fetcher is the deterministic source of those fields.

    Idempotency / immutability semantics ride on
    ``entity_extractor.preserve_raw(..., subdir="clipped", on_collision="skip")``:
      * same URL same day, same bytes → returns existing paths, no exception.
      * same URL same day, different bytes → FileExistsError (raw/ provenance).
    """
    content = fetch(url, fetch_html=fetch_html)
    slug = derive_slug(content.title, url)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    md_with_fm = (
        f"---\n"
        f'source_url: "{content.url}"\n'
        f'fetched_at: "{content.fetched_at}"\n'
        f"date: {today}\n"
        f"---\n\n"
        f"# {content.title or slug}\n\n"
        f"{content.markdown}\n"
    )

    # Stage to OS tempdir with the slug-only filename (no date), then preserve_raw
    # with always_date_prefix=True lands files in raw/clipped/{date}-{slug}.{ext}.
    # Using always_date_prefix=True (not the vault-ingest fallback path) ensures
    # the date prefix is unconditional — same URL on the same day produces the
    # same destination path, which is what makes on_collision="skip" idempotent
    # on byte-equal repeat fetches.
    #
    # Lazy-import to avoid circulars (entity_extractor imports a few things at
    # module level that we don't want to drag into url_fetch's import tree).
    from entity_extractor import preserve_raw

    with tempfile.TemporaryDirectory(prefix="url_fetch_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        html_src = tmpdir / f"{slug}.html"
        md_src = tmpdir / f"{slug}.md"
        html_src.write_bytes(content.html_bytes)
        md_src.write_text(md_with_fm, encoding="utf-8")

        html_path = preserve_raw(
            html_src,
            vault_dir,
            always_date_prefix=True,
            on_collision="skip",
            subdir="clipped",
        )
        md_path = preserve_raw(
            md_src,
            vault_dir,
            always_date_prefix=True,
            on_collision="skip",
            subdir="clipped",
        )

    return html_path, md_path, content
