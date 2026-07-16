#!/usr/bin/env python3
"""Validate rendered SEO/GEO pages over HTTP."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
WORD_PATTERN = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.skip = 0
        self.hidden = 0
        self.main_depth = 0
        self.stack: list[tuple[str, bool, bool, bool]] = []
        self.parts: list[str] = []
        self.main_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        attr_map = {key.lower(): (value or "") for key, value in attrs}
        compact_style = re.sub(r"\s+", "", attr_map.get("style", "").lower())
        enters_main = normalized == "main"
        skips = normalized in {"script", "style", "noscript", "svg", "template"}
        hides = (
            "hidden" in attr_map
            or attr_map.get("aria-hidden", "").lower() == "true"
            or "display:none" in compact_style
            or "visibility:hidden" in compact_style
        )
        if normalized in VOID_ELEMENTS:
            return
        if enters_main:
            self.main_depth += 1
        if skips:
            self.skip += 1
        if hides:
            self.hidden += 1
        self.stack.append((normalized, enters_main, skips, hides))

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        match = next((index for index in range(len(self.stack) - 1, -1, -1) if self.stack[index][0] == normalized), None)
        if match is None:
            return
        closing = self.stack[match:]
        self.stack = self.stack[:match]
        for _, entered_main, skipped, hidden in reversed(closing):
            if hidden and self.hidden:
                self.hidden -= 1
            if skipped and self.skip:
                self.skip -= 1
            if entered_main and self.main_depth:
                self.main_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip and not self.hidden:
            stripped = data.strip()
            if stripped:
                self.parts.append(stripped)
                if self.main_depth:
                    self.main_parts.append(stripped)

    def text(self) -> str:
        return " ".join(self.parts)

    def main_text(self) -> str:
        return " ".join(self.main_parts)


def fetch(url: str, timeout: int) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "tokenmax-render-validator/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return {
                "url": url,
                "status": getattr(response, "status", 200),
                "body": body.decode(charset, errors="replace"),
                "error": None,
            }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"url": url, "status": exc.code, "body": body, "error": str(exc)}
    except (URLError, TimeoutError, OSError) as exc:
        return {"url": url, "status": 0, "body": "", "error": str(exc)}


def analyze_html(html: str) -> dict[str, Any]:
    parser = TextExtractor()
    parser.feed(html)
    text = parser.text()
    main_text = parser.main_text()
    words = WORD_PATTERN.findall(text)
    main_words = WORD_PATTERN.findall(main_text)
    canonical = re.search(r"<link[^>]+rel=[\"']canonical[\"'][^>]*>", html, re.I) or re.search(
        r"<link[^>]+href=[\"'][^\"']+[\"'][^>]+rel=[\"']canonical[\"']", html, re.I
    )
    jsonld = re.search(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>", html, re.I)
    internal_links = re.findall(r"href=[\"']/(?!/|#)[^\"']+[\"']", html, re.I)
    return {
        "htmlBytes": len(html.encode("utf-8")),
        "textBytes": len(text.encode("utf-8")),
        "textHtmlRatio": round((len(text.encode("utf-8")) / max(len(html.encode("utf-8")), 1)), 4),
        "wordCount": len(words),
        "mainWordCount": len(main_words),
        "hasMain": bool(re.search(r"<main(?:\s|>)", html, re.I)),
        "hasCanonical": bool(canonical),
        "hasJsonLd": bool(jsonld),
        "internalLinkCount": len(internal_links),
        "_similarityText": main_text,
    }


def validate_page(url: str, args: argparse.Namespace) -> dict[str, Any]:
    fetched = fetch(url, args.timeout)
    analysis = analyze_html(fetched["body"]) if fetched["body"] else {
        "htmlBytes": 0,
        "textBytes": 0,
        "textHtmlRatio": 0,
        "wordCount": 0,
        "mainWordCount": 0,
        "hasMain": False,
        "hasCanonical": False,
        "hasJsonLd": False,
        "internalLinkCount": 0,
        "_similarityText": "",
    }
    failures: list[str] = []
    if fetched["status"] != 200:
        failures.append(f"status {fetched['status']}")
    if analysis["textHtmlRatio"] < args.min_text_html_ratio:
        failures.append(f"textHtmlRatio {analysis['textHtmlRatio']} < {args.min_text_html_ratio}")
    if args.min_words and analysis["wordCount"] < args.min_words:
        failures.append(f"wordCount {analysis['wordCount']} < {args.min_words}")
    if args.min_main_words and analysis["mainWordCount"] < args.min_main_words:
        failures.append(f"mainWordCount {analysis['mainWordCount']} < {args.min_main_words}")
    if args.require_canonical and not analysis["hasCanonical"]:
        failures.append("missing canonical")
    if args.require_jsonld and not analysis["hasJsonLd"]:
        failures.append("missing JSON-LD")
    if args.require_internal_links and analysis["internalLinkCount"] == 0:
        failures.append("missing internal links")
    return {
        "url": url,
        "status": fetched["status"],
        "error": fetched["error"],
        **analysis,
        "ok": not failures,
        "failures": failures,
    }


def validate_pairwise(pages: list[dict[str, Any]], max_overlap: float | None, shingle_size: int) -> dict[str, Any]:
    if max_overlap is None:
        return {"enabled": False, "pairsChecked": 0, "violations": 0, "highest": None}

    documents: list[tuple[int, set[str]]] = []
    for index, page in enumerate(pages):
        similarity_text = unicodedata.normalize(
            "NFKD", str(page.get("_similarityText", ""))
        ).encode("ascii", "ignore").decode("ascii")
        tokens = re.findall(r"\b[a-z][a-z'-]{2,}\b", similarity_text.lower())
        shingles = {
            " ".join(tokens[offset : offset + shingle_size])
            for offset in range(max(0, len(tokens) - shingle_size + 1))
        }
        documents.append((index, shingles))

    pairs_checked = 0
    violations = 0
    highest: dict[str, Any] | None = None
    for left_position, (left_index, left) in enumerate(documents):
        for right_index, right in documents[left_position + 1 :]:
            denominator = min(len(left), len(right))
            if not denominator:
                continue
            pairs_checked += 1
            shared = len(left & right)
            overlap = shared / denominator
            if highest is None or overlap > highest["overlap"]:
                highest = {
                    "overlap": round(overlap, 4),
                    "sharedShingles": shared,
                    "left": pages[left_index]["url"],
                    "right": pages[right_index]["url"],
                }
            if overlap > max_overlap:
                violations += 1
                message = (
                    f"pairwise {shingle_size}-word shingle overlap {overlap:.4f} "
                    f"> {max_overlap:.4f} against "
                )
                pages[left_index]["failures"].append(message + pages[right_index]["url"])
                pages[right_index]["failures"].append(message + pages[left_index]["url"])

    for page in pages:
        page["ok"] = not page["failures"]
    return {
        "enabled": True,
        "shingleSize": shingle_size,
        "maxAllowed": max_overlap,
        "pairsChecked": pairs_checked,
        "violations": violations,
        "highest": highest,
    }


def validate_sitemap(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.sitemap_url:
        return None
    fetched = fetch(args.sitemap_url, args.timeout)
    locs = re.findall(r"<loc>\s*([^<]+)\s*</loc>", fetched["body"], re.I)
    failures: list[str] = []
    if fetched["status"] != 200:
        failures.append(f"status {fetched['status']}")
    if args.expect_sitemap_count is not None and len(locs) != args.expect_sitemap_count:
        failures.append(f"loc count {len(locs)} != {args.expect_sitemap_count}")
    for expected in args.expect_sitemap_substring or []:
        if expected not in fetched["body"]:
            failures.append(f"missing sitemap substring {expected}")
    return {
        "url": args.sitemap_url,
        "status": fetched["status"],
        "locCount": len(locs),
        "ok": not failures,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate rendered TokenMax routes over HTTP.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--routes", nargs="+", required=True)
    parser.add_argument("--sitemap-url")
    parser.add_argument("--expect-sitemap-substring", action="append")
    parser.add_argument("--expect-sitemap-count", type=int)
    parser.add_argument("--min-text-html-ratio", type=float, default=0.10)
    parser.add_argument("--min-words", type=int, default=0)
    parser.add_argument("--min-main-words", type=int, default=0)
    parser.add_argument("--max-pairwise-overlap", type=float)
    parser.add_argument("--shingle-size", type=int, default=8)
    parser.add_argument("--require-canonical", action="store_true")
    parser.add_argument("--require-jsonld", action="store_true")
    parser.add_argument("--require-internal-links", action="store_true")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--output")
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()

    if args.shingle_size < 1:
        parser.error("--shingle-size must be at least 1")
    if args.max_pairwise_overlap is not None and not 0 <= args.max_pairwise_overlap <= 1:
        parser.error("--max-pairwise-overlap must be between 0 and 1")

    pages = []
    for route in args.routes:
        url = route if route.startswith(("http://", "https://")) else urljoin(args.base_url.rstrip("/") + "/", route.lstrip("/"))
        pages.append(validate_page(url, args))
    pairwise = validate_pairwise(pages, args.max_pairwise_overlap, args.shingle_size)
    for page in pages:
        page.pop("_similarityText", None)
    sitemap = validate_sitemap(args)
    ok = all(page["ok"] for page in pages) and (sitemap is None or sitemap["ok"])
    report = {"ok": ok, "pages": pages, "pairwise": pairwise, "sitemap": sitemap}
    output = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        from pathlib import Path

        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)
    if not ok and not args.no_fail:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
