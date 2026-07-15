"""GitHub trending scrape — ported from the TheHomie signal-engine workshop.

github.com/trending has no official API; this parses the `article.Box-row`
cards with the stdlib HTMLParser. Unversioned scraping is inherently fragile,
so the contract is: malformed/changed HTML yields fewer (or zero) items — it
NEVER raises past :func:`fetch_trending`. Trending is garnish in the digest.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any

GITHUB_TRENDING_URL = "https://github.com/trending"

_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


class TrendingParser(HTMLParser):
    """Extracts repo cards from the GitHub Trending page.

    Each trending repo lives in an ``<article class="Box-row">``:
    - repo link: ``<h2><a href="/owner/repo">``
    - description: the muted ``<p>`` inside the article
    - total stars: text of ``<a href=".../stargazers">``
    - language: ``<span class="d-inline-block ml-0 ...">``
    - stars today: ``<span class="d-inline-block float-sm-right">N stars today``
    """

    def __init__(self) -> None:
        super().__init__()
        self.repos: list[dict[str, str]] = []
        self._in_article = False
        self._current: dict[str, str] = {}
        self._h2_found = False
        self._capture_p = False
        self._p_found = False
        self._capture_stars = False
        self._capture_lang = False
        self._capture_today = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        css_class = attr_dict.get("class") or ""

        if tag == "article" and "Box-row" in css_class:
            self._in_article = True
            self._current = {}
            self._h2_found = False
            self._p_found = False
            return
        if not self._in_article:
            return

        if tag == "h2":
            self._h2_found = True
        if tag == "a" and self._h2_found and not self._current.get("full_name"):
            href = (attr_dict.get("href") or "").strip()
            if href.count("/") == 2 and href.startswith("/"):
                self._current["full_name"] = href.lstrip("/")
                return
        if tag == "p" and not self._p_found:
            if "f6" in css_class or "color-fg-muted" in css_class or "my-1" in css_class:
                self._p_found = True
                self._capture_p = True
                return
        if tag == "a" and (attr_dict.get("href") or "").endswith("/stargazers"):
            self._capture_stars = True
            return
        if tag == "span" and "d-inline-block" in css_class:
            if "float-sm-right" in css_class:
                self._capture_today = True
            elif "ml-0" in css_class:
                self._capture_lang = True

    def handle_endtag(self, tag: str) -> None:
        if not self._in_article:
            return
        if tag == "article":
            if self._current.get("full_name"):
                self.repos.append(self._current)
            self._in_article = False
            self._capture_p = False
            self._capture_stars = False
            self._capture_lang = False
            self._capture_today = False
            return
        if tag == "a":
            self._capture_stars = False
        if tag == "p":
            self._capture_p = False
        if tag == "span":
            self._capture_lang = False
            self._capture_today = False

    def handle_data(self, data: str) -> None:
        if not self._in_article:
            return
        text = data.strip()
        if not text:
            return
        if self._capture_p:
            self._current["description"] = (
                self._current.get("description", "") + text
            )
        if self._capture_stars and re.search(r"[\d,k.]", text, re.IGNORECASE):
            self._current.setdefault("stars", text.replace(",", "").strip())
        if self._capture_lang:
            self._current.setdefault("language", text)
        if self._capture_today:
            match = re.search(r"([\d,]+)\s*stars?\s*today", text, re.IGNORECASE)
            if match:
                self._current["stars_today"] = match.group(1).replace(",", "")


def parse_trending_html(html: str) -> list[dict[str, str]]:
    """Parse trending HTML; malformed input yields partial/empty results."""
    try:
        parser = TrendingParser()
        parser.feed(html)
        return parser.repos
    except Exception:
        return []


def fetch_trending(timeout: float = 30.0, since: str = "weekly") -> list[dict[str, str]]:
    """Fetch and parse github.com/trending. Returns [] on any failure."""
    url = f"{GITHUB_TRENDING_URL}?since={since}"
    req = urllib.request.Request(url)
    for k, v in _SCRAPE_HEADERS.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    return parse_trending_html(html)


def filter_by_keywords(
    items: list[dict[str, Any]], keywords: list[str]
) -> list[dict[str, Any]]:
    """Case-insensitive substring match over full_name + description."""
    if not keywords:
        return items
    out = []
    for item in items:
        text = f"{item.get('full_name', '')} {item.get('description', '')}".lower()
        if any(kw in text for kw in keywords):
            out.append(item)
    return out
