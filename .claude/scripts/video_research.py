"""Research stage for the framework video pipeline (URL and theme dossiers).

Turns one query (a single URL or a free-text theme) into a research DOSSIER
the video pipeline consumes: background facts for the copywriter, a design
derived from the page's own colors and fonts, reference images for
identity-locked art, and a claims allowlist for the claim-safety gate.

Pure deterministic module: no runtime lanes, stdlib at module level, and
network only at call time through ``url_fetch`` (plus the pluggable search
provider) inside function bodies. ``build_dossier`` NEVER raises; total
failure returns ``ok=False`` with notes, and every network touch lands in
the dossier's ``audit`` list.

Dossier schema (canonical):
    {
      "ok": bool,
      "mode": "url" | "theme",
      "query": str,
      "url": str,                   # the fetched page ("" when nothing fetched)
      "title": str,
      "summary_text": str,          # <= MAX_SUMMARY_CHARS
      "facts": [str],               # <= MAX_FACTS, number-bearing preferred
      "claims_text": str,           # title + facts + numeric sentences
      "derived_design": dict|None,  # complete validated design dict, or None
      "images": [{"url", "path", "kind"}],
      "search": [{"title", "url", "snippet"}],
      "audit": [{"t", "action", "target", "ok", "bytes", "ms", "note"}],
      "notes": [str],
    }

The dossier additionally caches ``html_text`` (the fetched page body) so a
caller can collect reference images AFTER its output dirs exist (two-phase
use inside a render). Callers strip ``html_text`` before persisting the
dossier to disk.

Dependency-injection seams (zero network in the unit suite):
    build_dossier(query, fetch_html=..., search=...)
    fetch_page(url, fetch_html=...)
    collect_reference_images(html_text, base_url, dest_dir, fetch_bytes=...)
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

# =============================================================================
# CONSTANTS (honest caps; env-tunable values are read inside function bodies)
# =============================================================================

MAX_PAGE_BYTES = 3_000_000
MAX_SUMMARY_CHARS = 2_400
MAX_FACTS = 12
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 2_000_000
FETCH_TIMEOUT_S = 20  # matches url_fetch.DEFAULT_TIMEOUT_S (its fetcher owns it)
MAX_SEARCH_RESULTS = 5
MAX_THEME_PAGES = 2

# Search providers, tried in order. Each name resolves to a module attribute
# ``_search_<name>`` AT CALL TIME (module-attribute lookup, so tests and
# deployments can monkeypatch providers or add new ones without touching
# ``search_web``).
_PROVIDER_ORDER = ("exa",)


# =============================================================================
# AUDIT
# =============================================================================


def _audit_entry(
    action: str,
    target: str,
    *,
    ok: bool,
    size: int = 0,
    ms: float = 0.0,
    note: str = "",
) -> dict:
    """One audit row for a network touch. Schema: t/action/target/ok/bytes/ms/note."""

    return {
        "t": round(time.time(), 3),
        "action": action,
        "target": str(target or "")[:300],
        "ok": bool(ok),
        "bytes": int(size),
        "ms": int(round(ms)),
        "note": str(note or ""),
    }


# =============================================================================
# PAGE FETCH (wraps url_fetch; the only HTML network path in this module)
# =============================================================================


def fetch_page(url: str, *, fetch_html=None, audit: list | None = None) -> dict | None:
    """Fetch + extract one page through ``url_fetch.fetch``. Never raises.

    Returns ``{"url", "title", "markdown", "html_text", "bytes", "ms"}`` or
    None on any failure. Pages larger than ``MAX_PAGE_BYTES`` are truncated
    (html and markdown) with a note in the audit entry. Appends one audit
    entry when ``audit`` is given and prints one status line either way.
    """

    started = time.monotonic()
    try:
        import url_fetch

        content = url_fetch.fetch(url, fetch_html=fetch_html)
        ms = (time.monotonic() - started) * 1000.0
        size = len(content.html_bytes or b"")
        html_text = content.html_text or ""
        markdown = content.markdown or ""
        note = ""
        if size > MAX_PAGE_BYTES:
            html_text = html_text[:MAX_PAGE_BYTES]
            markdown = markdown[:MAX_PAGE_BYTES]
            note = f"page truncated to {MAX_PAGE_BYTES} bytes"
        print(f"[video_research] GET {url} -> ok bytes={size} ms={int(round(ms))}")
        if audit is not None:
            audit.append(_audit_entry("fetch", url, ok=True, size=size, ms=ms, note=note))
        return {
            "url": str(url),
            "title": content.title or "",
            "markdown": markdown,
            "html_text": html_text,
            "bytes": size,
            "ms": int(round(ms)),
        }
    except Exception as exc:
        ms = (time.monotonic() - started) * 1000.0
        print(
            f"[video_research] GET {url} -> error {type(exc).__name__} ms={int(round(ms))}"
        )
        if audit is not None:
            audit.append(
                _audit_entry(
                    "fetch", url, ok=False, ms=ms, note=f"{type(exc).__name__}: {exc}"
                )
            )
        return None


# =============================================================================
# PROSE -> FACTS / SUMMARY
# =============================================================================

_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _clean_prose(markdown: str) -> str:
    """Markdown extraction -> plain prose (links unwrapped, markers stripped)."""

    text = _MD_LINK.sub(r"\1", str(markdown or ""))
    lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"^[\s#>\-*|]+", "", line)
        line = line.replace("**", " ").replace("`", " ")
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(str(text or "")) if s.strip()]


def _facts_from_text(text: str, limit: int = MAX_FACTS) -> list[str]:
    """Sentence facts from prose, number-bearing sentences first, deduped."""

    sentences = [s for s in _split_sentences(text) if 20 <= len(s) <= 240]
    numbered = [s for s in sentences if re.search(r"\d", s)]
    numbered_set = set(numbered)
    plain = [s for s in sentences if s not in numbered_set]
    out: list[str] = []
    seen: set[str] = set()
    for sentence in numbered + plain:
        key = sentence.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(sentence)
        if len(out) >= limit:
            break
    return out


# =============================================================================
# DESIGN DERIVATION (page colors + fonts -> video_styles.design_from_tokens)
# =============================================================================

_HEX_TOKEN = re.compile(r"#[0-9A-Fa-f]{6}\b")
_STYLE_BLOCK = re.compile(r"<style[^>]*>([\s\S]*?)</style>", re.IGNORECASE)
_STYLE_ATTR = re.compile(r"style\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_CSS_VAR = re.compile(r"--([A-Za-z0-9_-]+)\s*:\s*(#[0-9A-Fa-f]{6})\b")
_FONT_FAMILY = re.compile(r"font-family\s*:\s*([^;{}]+)", re.IGNORECASE)
_GOOGLE_FONTS_HREF = re.compile(
    r"href\s*=\s*[\"']([^\"']*fonts\.googleapis\.com/css2[^\"']*)[\"']", re.IGNORECASE
)
_FAMILY_PARAM = re.compile(r"family=([^&:]+)")

_GENERIC_FONTS = {
    "sans-serif",
    "serif",
    "monospace",
    "system-ui",
    "ui-sans-serif",
    "ui-serif",
    "ui-monospace",
    "ui-rounded",
    "cursive",
    "fantasy",
    "inherit",
    "initial",
    "unset",
    "math",
    "emoji",
}


def _collect_style_text(html_text: str) -> str:
    """All CSS-bearing text on the page: <style> blocks + style attributes."""

    chunks = [m.group(1) for m in _STYLE_BLOCK.finditer(html_text)]
    chunks += [m.group(1) for m in _STYLE_ATTR.finditer(html_text)]
    return "\n".join(chunks)


def _site_name(url: str, title: str = "") -> str:
    """A short design name for the page: host label, then title, then a stub."""

    host = urlparse(str(url or "")).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    label = host.split(".")[0] if host else ""
    if label:
        return label
    return str(title or "").strip() or "derived"


def derive_design_from_page(html_text: str, url: str, title: str = "") -> dict | None:
    """Page tokens -> a COMPLETE validated design dict, or None.

    Colors come from inline ``style=`` attributes, ``<style>`` blocks, and
    CSS custom properties (named custom props keep their names so the role
    mapping can recognize background/ink/accent); anonymous hexes rank by
    frequency. Fonts come from Google Fonts ``css2`` link hrefs and
    ``font-family`` declarations. Returns None when the page does not carry
    at least two distinct hex colors. Never raises.
    """

    try:
        text = str(html_text or "")
        if not text.strip():
            return None
        style_text = _collect_style_text(text)

        colors: dict[str, str] = {}
        for match in _CSS_VAR.finditer(style_text):
            colors.setdefault(match.group(1).strip().lower(), match.group(2).upper())
        counts: dict[str, int] = {}
        for match in _HEX_TOKEN.finditer(style_text):
            value = match.group(0).upper()
            counts[value] = counts.get(value, 0) + 1
        named_values = set(colors.values())
        ranked = sorted(
            (v for v in counts if v not in named_values),
            key=lambda v: (-counts[v], v),
        )
        for i, value in enumerate(ranked):
            colors.setdefault(f"c{i}", value)
        if len(set(colors.values())) < 2:
            return None

        fonts: list[str] = []
        for match in _GOOGLE_FONTS_HREF.finditer(text):
            for family in _FAMILY_PARAM.findall(match.group(1)):
                name = unquote(family).replace("+", " ").strip()
                if name and name.lower() not in _GENERIC_FONTS:
                    fonts.append(name)
        for match in _FONT_FAMILY.finditer(style_text):
            first = match.group(1).split(",")[0].strip().strip("'\"").strip()
            if (
                first
                and not first.lower().startswith("var(")
                and first.lower() not in _GENERIC_FONTS
            ):
                fonts.append(first)
        fonts = list(dict.fromkeys(fonts))

        host = urlparse(str(url or "")).netloc or "the source page"
        import video_styles

        return video_styles.design_from_tokens(
            _site_name(url, title),
            colors,
            fonts,
            tagline=f"Palette and type derived from {host}",
        )
    except Exception:
        return None


# =============================================================================
# REFERENCE IMAGES (og/twitter/icon/largest-img; raster-only, magic-checked)
# =============================================================================

_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_LINK_TAG = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def _attr(tag: str, name: str) -> str:
    match = re.search(rf"{name}\s*=\s*[\"']([^\"']*)[\"']", tag, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _image_candidates(html_text: str, base_url: str) -> list[tuple[str, str]]:
    """Ordered, deduped (kind, absolute_url) image candidates from a page."""

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, raw: str) -> None:
        raw = (raw or "").strip()
        if not raw or raw.startswith("data:"):
            return
        absolute = urljoin(str(base_url or ""), raw)
        if not absolute.lower().startswith(("http://", "https://")):
            return
        if absolute in seen:
            return
        seen.add(absolute)
        out.append((kind, absolute))

    meta_tags = _META_TAG.findall(html_text)
    for tag in meta_tags:
        key = (_attr(tag, "property") or _attr(tag, "name")).lower()
        if key in {"og:image", "og:image:url", "og:image:secure_url"}:
            add("og", _attr(tag, "content"))
    for tag in meta_tags:
        key = (_attr(tag, "property") or _attr(tag, "name")).lower()
        if key in {"twitter:image", "twitter:image:src"}:
            add("twitter", _attr(tag, "content"))
    for tag in _LINK_TAG.findall(html_text):
        if "apple-touch-icon" in _attr(tag, "rel").lower():
            add("icon", _attr(tag, "href"))

    sized: list[tuple[int, str]] = []
    for tag in _IMG_TAG.findall(html_text):
        src = _attr(tag, "src")
        if not src:
            continue
        try:
            width = int(re.sub(r"\D", "", _attr(tag, "width")) or 0)
            height = int(re.sub(r"\D", "", _attr(tag, "height")) or 0)
        except ValueError:
            width, height = 0, 0
        sized.append((width * height, src))
    if sized:
        sized.sort(key=lambda pair: -pair[0])
        add("img", sized[0][1])
    return out


def _raster_ext(data: bytes, content_type: str) -> str:
    """Extension when content-type AND magic bytes agree on png/jpg/webp."""

    ctype = (content_type or "").split(";")[0].strip().lower()
    if data[:8] == b"\x89PNG\r\n\x1a\n" and ctype == "image/png":
        return ".png"
    if data[:3] == b"\xff\xd8\xff" and ctype in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and ctype == "image/webp":
        return ".webp"
    return ""


def _fetch_image_bytes(url: str) -> tuple[bytes, str]:
    """Default raw fetch for image candidates (rides url_fetch's fetcher)."""

    import url_fetch

    raw, _text, content_type = url_fetch._fetch_html(url, timeout=FETCH_TIMEOUT_S)
    return raw, content_type


def collect_reference_images(
    html_text: str,
    base_url: str,
    dest_dir: str | Path,
    *,
    fetch_bytes=None,
    max_images: int | None = None,
    audit: list | None = None,
) -> list[dict]:
    """Download up to ``max_images`` raster references from a fetched page.

    Candidates in preference order: og:image, twitter:image,
    apple-touch-icon, the largest declared content ``<img>``. Each candidate
    must pass BOTH a content-type check and a magic-byte check (png/jpg/webp
    only; svg and spoofed types are rejected) and fit ``MAX_IMAGE_BYTES``.
    Saved as ``dest_dir/ref{i}.<ext>``. Returns ``[{"url", "path", "kind"}]``;
    empty list on any trouble. Never raises.
    """

    results: list[dict] = []
    try:
        cap = MAX_IMAGES if max_images is None else max(0, int(max_images))
        if cap == 0:
            return []
        fetcher = fetch_bytes or _fetch_image_bytes
        dest = Path(dest_dir)
        for kind, url in _image_candidates(str(html_text or ""), str(base_url or "")):
            if len(results) >= cap:
                break
            started = time.monotonic()
            try:
                data, content_type = fetcher(url)
            except Exception as exc:
                if audit is not None:
                    audit.append(
                        _audit_entry(
                            "image",
                            url,
                            ok=False,
                            ms=(time.monotonic() - started) * 1000.0,
                            note=f"{type(exc).__name__}: {exc}",
                        )
                    )
                continue
            ms = (time.monotonic() - started) * 1000.0
            data = data or b""
            if not data or len(data) > MAX_IMAGE_BYTES:
                if audit is not None:
                    audit.append(
                        _audit_entry(
                            "image",
                            url,
                            ok=False,
                            size=len(data),
                            ms=ms,
                            note="empty or over the size cap",
                        )
                    )
                continue
            ext = _raster_ext(data, content_type)
            if not ext:
                if audit is not None:
                    audit.append(
                        _audit_entry(
                            "image",
                            url,
                            ok=False,
                            size=len(data),
                            ms=ms,
                            note="not a png/jpg/webp raster",
                        )
                    )
                continue
            dest.mkdir(parents=True, exist_ok=True)
            path = dest / f"ref{len(results)}{ext}"
            path.write_bytes(data)
            results.append({"url": url, "path": str(path), "kind": kind})
            if audit is not None:
                audit.append(_audit_entry("image", url, ok=True, size=len(data), ms=ms))
        return results
    except Exception:
        return results


# =============================================================================
# WEB SEARCH (provider-pluggable via module-attribute lookup)
# =============================================================================


def search_web(query: str, *, max_results: int = MAX_SEARCH_RESULTS) -> tuple[list[dict], str]:
    """Theme search across the configured providers. Returns (results, note).

    Providers resolve AT CALL TIME by module-attribute lookup
    (``getattr(module, f"_search_{name}")`` over ``_PROVIDER_ORDER``) so
    tests and deployments can swap or add providers. A provider returns its
    raw result list, or None when it is not configured (e.g. no API key).
    No configured provider -> ``([], "no search provider configured")``; a
    provider exception -> ``([], note)``. Never raises.
    """

    module = sys.modules[__name__]
    try:
        limit = max(0, int(max_results))
    except (TypeError, ValueError):
        limit = MAX_SEARCH_RESULTS
    for name in _PROVIDER_ORDER:
        provider = getattr(module, f"_search_{name}", None)
        if provider is None:
            continue
        try:
            results = provider(query, limit)
        except Exception as exc:
            return [], f"search provider {name} failed: {type(exc).__name__}: {exc}"
        if results is None:
            continue
        cleaned: list[dict] = []
        for item in list(results)[:limit]:
            if not isinstance(item, dict):
                continue
            cleaned.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                    "snippet": str(item.get("snippet") or item.get("text") or ""),
                }
            )
        return cleaned, ""
    return [], "no search provider configured"


def _search_exa(query: str, max_results: int) -> list[dict] | None:
    """Exa search provider. Returns None when EXA_API_KEY is unset (in-body)."""

    api_key = os.environ.get("EXA_API_KEY", "").strip()
    if not api_key:
        return None

    import json
    from urllib import request

    payload = json.dumps({"query": str(query), "numResults": int(max_results)}).encode(
        "utf-8"
    )
    req = request.Request(
        "https://api.exa.ai/search",
        data=payload,
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    out: list[dict] = []
    for item in (data or {}).get("results") or []:
        if isinstance(item, dict):
            out.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                    "snippet": str(item.get("snippet") or item.get("text") or ""),
                }
            )
    return out


# =============================================================================
# CLAIMS ALLOWLIST
# =============================================================================


def claims_text_from(dossier: dict | None) -> str:
    """Claim-gate allowlist text: title + facts + numeric summary sentences."""

    try:
        data = dossier or {}
        parts: list[str] = []
        title = str(data.get("title") or "").strip()
        if title:
            parts.append(title)
        for fact in list(data.get("facts") or []):
            text = str(fact or "").strip()
            if text and text not in parts:
                parts.append(text)
        for sentence in _split_sentences(str(data.get("summary_text") or "")):
            if re.search(r"\d", sentence) and sentence not in parts:
                parts.append(sentence)
        return "\n".join(parts)
    except Exception:
        return ""


# =============================================================================
# DOSSIER
# =============================================================================


def build_dossier(
    query: str,
    *,
    assets_dir: str | Path | None = None,
    fetch_html=None,
    search=None,
) -> dict:
    """One research dossier for a URL or theme query. NEVER raises.

    ``url_fetch.is_url(query)`` picks the mode. URL mode: fetch the page,
    split facts (number-bearing first), derive a design from the page's own
    tokens, and (when ``assets_dir`` is given) collect reference images.
    Theme mode: search the web, fetch the top ``MAX_THEME_PAGES`` results,
    merge their facts; ``derived_design`` stays None and at most ONE og
    reference image is collected. Every network touch appends an audit row.
    ``html_text`` is cached on the dossier for two-phase image collection;
    strip it before persisting.
    """

    query_text = str(query or "").strip()
    dossier: dict[str, Any] = {
        "ok": False,
        "mode": "theme",
        "query": query_text,
        "url": "",
        "title": "",
        "summary_text": "",
        "facts": [],
        "claims_text": "",
        "derived_design": None,
        "images": [],
        "search": [],
        "audit": [],
        "notes": [],
        "html_text": "",
    }
    try:
        if not query_text:
            dossier["notes"].append("empty research query")
            return dossier
        import url_fetch

        if url_fetch.is_url(query_text):
            dossier["mode"] = "url"
            _build_url_dossier(
                dossier, query_text, assets_dir=assets_dir, fetch_html=fetch_html
            )
        else:
            _build_theme_dossier(
                dossier,
                query_text,
                assets_dir=assets_dir,
                fetch_html=fetch_html,
                search=search,
            )
        dossier["claims_text"] = claims_text_from(dossier)
        return dossier
    except Exception as exc:
        dossier["ok"] = False
        dossier["notes"].append(f"research failed: {type(exc).__name__}: {exc}")
        return dossier


def _build_url_dossier(dossier: dict, url: str, *, assets_dir, fetch_html) -> None:
    dossier["url"] = url
    page = fetch_page(url, fetch_html=fetch_html, audit=dossier["audit"])
    if page is None:
        dossier["notes"].append(f"could not fetch {url}")
        return
    prose = _clean_prose(page["markdown"])
    dossier["title"] = page["title"]
    dossier["summary_text"] = prose[:MAX_SUMMARY_CHARS]
    dossier["facts"] = _facts_from_text(prose)
    dossier["html_text"] = page["html_text"]
    design = derive_design_from_page(page["html_text"], url, title=page["title"])
    dossier["derived_design"] = design
    if design is None:
        dossier["notes"].append("no usable palette on the page")
    if assets_dir:
        dossier["images"] = collect_reference_images(
            page["html_text"], url, assets_dir, audit=dossier["audit"]
        )
    dossier["ok"] = True


def _build_theme_dossier(
    dossier: dict, query: str, *, assets_dir, fetch_html, search
) -> None:
    searcher = search or search_web
    results, note = searcher(query)
    dossier["search"] = [r for r in list(results or []) if isinstance(r, dict)]
    dossier["audit"].append(
        _audit_entry("search", query, ok=bool(dossier["search"]), note=note)
    )
    if note:
        dossier["notes"].append(note)

    prose_parts: list[str] = []
    facts: list[str] = []
    for entry in dossier["search"][:MAX_THEME_PAGES]:
        page_url = str((entry or {}).get("url") or "")
        if not page_url:
            continue
        page = fetch_page(page_url, fetch_html=fetch_html, audit=dossier["audit"])
        if page is None:
            continue
        prose = _clean_prose(page["markdown"])
        prose_parts.append(prose)
        facts.extend(_facts_from_text(prose))
        if not dossier["url"]:
            dossier["url"] = page_url
            dossier["title"] = page["title"]
            dossier["html_text"] = page["html_text"]

    merged: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        key = fact.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(fact)
        if len(merged) >= MAX_FACTS:
            break
    dossier["facts"] = merged
    dossier["summary_text"] = " ".join(prose_parts)[:MAX_SUMMARY_CHARS]
    dossier["title"] = dossier["title"] or query
    dossier["derived_design"] = None
    if assets_dir and dossier["html_text"]:
        dossier["images"] = collect_reference_images(
            dossier["html_text"],
            dossier["url"],
            assets_dir,
            max_images=1,
            audit=dossier["audit"],
        )
    dossier["ok"] = bool(dossier["facts"] or dossier["summary_text"] or dossier["search"])
    if not dossier["ok"]:
        dossier["notes"].append("theme research found nothing usable")
