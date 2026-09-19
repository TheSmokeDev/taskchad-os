"""Shared, read-only public research clients for authority workflows.

This module owns provider transport only.  It does not decide editorial
relevance, spend Firecrawl credits without a caller-owned budget reservation,
or expose credentials in results and exceptions.  All returned documents are
normalized to the same public-safe shape before the Business Signal Engine sees
them.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from business_signal.models import (
    AuthoritySourceClass,
    assert_public_safe_text,
    validate_public_source_url,
)

_LOGGER = logging.getLogger(__name__)
_EXA_MCP_ENDPOINT = "https://mcp.exa.ai/mcp"
_FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v1"
_DEFAULT_FIRECRAWL_MCP_CONFIG = Path.home() / ".claude" / "mcp.json"
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]{1,300})\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\])]+")


class ResearchSourceError(RuntimeError):
    """Provider-neutral public research failure with no credential details."""


@dataclass(frozen=True, slots=True)
class ResearchDocument:
    """Normalized public evidence discovered from one read-only source."""

    lane: str
    title: str
    url: str
    snippet: str
    published_at: datetime | None
    source_class: AuthoritySourceClass
    primary_source: bool
    provider: str
    verified_repository: bool = False
    repository: str | None = None

    def with_snippet(self, snippet: str, *, title: str | None = None) -> ResearchDocument:
        return replace(
            self,
            snippet=assert_public_safe_text(str(snippet or "").strip())[:12_000],
            title=assert_public_safe_text(str(title or self.title).strip())[:500],
        )


def classify_public_source(url: str) -> tuple[AuthoritySourceClass, bool]:
    """Classify provenance conservatively from the public hostname."""

    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host == "github.com" or host == "api.github.com":
        return "repository", True
    official_suffixes = (
        "developers.google.com",
        "support.google.com",
        "blog.google",
        "openai.com",
        "docs.github.com",
        "schema.org",
        "microsoft.com",
        "bing.com",
        "anthropic.com",
    )
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in official_suffixes):
        return "official_documentation", True
    vendor_suffixes = ("ahrefs.com", "semrush.com", "moz.com", "searchengineland.com")
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in vendor_suffixes):
        return "vendor_research", False
    practitioner_suffixes = (
        "medium.com",
        "substack.com",
        "linkedin.com",
        "reddit.com",
        "dev.to",
    )
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in practitioner_suffixes):
        return "practitioner_self_report", False
    # Discovery snippets from an otherwise unknown publisher are not promoted
    # to primary evidence merely because they are public.
    return "practitioner_self_report", False


class ResearchSourcesClient:
    """Synchronous transport client; callers use ``asyncio.to_thread``."""

    def __init__(
        self,
        *,
        http_get: Callable[..., Any] | None = None,
        http_post: Callable[..., Any] | None = None,
        firecrawl_config_path: Path | None = None,
    ) -> None:
        self._get = http_get or requests.get
        self._post = http_post or requests.post
        self._firecrawl_config_path = firecrawl_config_path or _DEFAULT_FIRECRAWL_MCP_CONFIG

    def exa_search(self, query: str, *, lane: str, limit: int = 5) -> list[ResearchDocument]:
        normalized = " ".join(str(query or "").split())
        if not normalized:
            raise ValueError("Exa query is required")
        bounded_limit = max(1, min(int(limit), 10))
        raw = self._exa_mcp_call(
            "web_search_exa",
            {"query": normalized[:800], "numResults": bounded_limit},
        )
        return _parse_exa_documents(raw, lane=lane, limit=bounded_limit)

    def firecrawl_scrape(self, url: str, *, lane: str) -> ResearchDocument:
        """Read one page after the caller has reserved its budget credit."""

        target = validate_public_source_url(url)
        key = self._firecrawl_api_key()
        if not key:
            raise ResearchSourceError("Firecrawl is not configured")
        try:
            response = self._post(
                f"{_FIRECRAWL_BASE_URL}/scrape",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={"url": target, "formats": ["markdown"], "onlyMainContent": True},
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            _LOGGER.info("Firecrawl authority read failed (%s)", type(exc).__name__)
            raise ResearchSourceError(
                f"Firecrawl authority read failed ({type(exc).__name__})"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ResearchSourceError("Firecrawl returned no readable document")
        markdown = assert_public_safe_text(str(data.get("markdown") or "").strip())
        if not markdown:
            raise ResearchSourceError("Firecrawl returned an empty document")
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        title = assert_public_safe_text(str(metadata.get("title") or target).strip())[:500]
        source_class, primary = classify_public_source(target)
        return ResearchDocument(
            lane=lane,
            title=title,
            url=target,
            snippet=markdown[:12_000],
            published_at=_parse_datetime(
                metadata.get("publishedTime") or metadata.get("published_at")
            ),
            source_class=source_class,
            primary_source=primary,
            provider="firecrawl",
        )

    def firecrawl_configured(self) -> bool:
        """Return credential availability without returning the credential."""

        return bool(self._firecrawl_api_key())

    def github_repository(self, repo: str) -> ResearchDocument:
        """Read current repository metadata and the latest public release."""

        normalized = str(repo or "").strip()
        if not _GITHUB_REPO_RE.fullmatch(normalized):
            raise ValueError("repository must be owner/name")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "TheHomie-AuthoritySignal/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.getenv("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            repo_response = self._get(
                f"https://api.github.com/repos/{normalized}", headers=headers, timeout=30
            )
            repo_response.raise_for_status()
            metadata = repo_response.json()
            release: dict[str, Any] = {}
            release_response = self._get(
                f"https://api.github.com/repos/{normalized}/releases/latest",
                headers=headers,
                timeout=30,
            )
            if getattr(release_response, "status_code", 0) != 404:
                release_response.raise_for_status()
                parsed_release = release_response.json()
                if isinstance(parsed_release, dict):
                    release = parsed_release
        except (requests.RequestException, ValueError, TypeError) as exc:
            _LOGGER.info("GitHub authority read failed (%s)", type(exc).__name__)
            raise ResearchSourceError(
                f"GitHub authority read failed ({type(exc).__name__})"
            ) from exc
        if not isinstance(metadata, dict):
            raise ResearchSourceError("GitHub returned invalid repository metadata")

        html_url = validate_public_source_url(
            str(metadata.get("html_url") or f"https://github.com/{normalized}")
        )
        release_name = str(release.get("name") or release.get("tag_name") or "").strip()
        description = str(metadata.get("description") or "").strip()
        topics = metadata.get("topics") if isinstance(metadata.get("topics"), list) else []
        parts = [f"Repository: {normalized}."]
        if description:
            parts.append(f"Description: {description}")
        if release_name:
            parts.append(f"Latest release: {release_name}.")
        if topics:
            parts.append("Public topics: " + ", ".join(str(topic) for topic in topics[:20]) + ".")
        snippet = assert_public_safe_text(" ".join(parts))[:12_000]
        published_at = _parse_datetime(
            release.get("published_at")
            or metadata.get("pushed_at")
            or metadata.get("updated_at")
        )
        return ResearchDocument(
            lane="open_source_distribution",
            title=assert_public_safe_text(
                f"{normalized}{f' {release_name}' if release_name else ''}"
            )[:500],
            url=html_url,
            snippet=snippet,
            published_at=published_at,
            source_class="repository",
            primary_source=True,
            provider="github",
            verified_repository=True,
            repository=normalized,
        )

    def _firecrawl_api_key(self) -> str:
        key = os.getenv("FIRECRAWL_API_KEY", "").strip()
        if key:
            return key
        try:
            payload = json.loads(self._firecrawl_config_path.read_text(encoding="utf-8"))
            configured = payload.get("mcpServers", {}).get("firecrawl", {})
            inherited = str(configured.get("env", {}).get("FIRECRAWL_API_KEY", "")).strip()
            if inherited and not inherited.startswith("${"):
                return inherited
        except (OSError, ValueError, TypeError):
            pass
        return ""

    def _exa_mcp_call(self, name: str, arguments: dict[str, Any]) -> str:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-03-26",
            "User-Agent": "TheHomie-AuthoritySignal/1.0",
        }
        try:
            initialized = self._post(
                _EXA_MCP_ENDPOINT,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "homie-authority-signal",
                            "version": "1.0",
                        },
                    },
                },
                timeout=30,
            )
            initialized.raise_for_status()
            session_id = initialized.headers.get("Mcp-Session-Id")
            if not session_id:
                raise ResearchSourceError("Exa did not create a research session")
            session_headers = {**headers, "Mcp-Session-Id": session_id}
            acknowledged = self._post(
                _EXA_MCP_ENDPOINT,
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                timeout=15,
            )
            acknowledged.raise_for_status()
            response = self._post(
                _EXA_MCP_ENDPOINT,
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                timeout=45,
            )
            response.raise_for_status()
        except ResearchSourceError:
            raise
        except requests.RequestException as exc:
            _LOGGER.info("Exa authority read failed (%s)", type(exc).__name__)
            raise ResearchSourceError(
                f"Exa authority read failed ({type(exc).__name__})"
            ) from exc
        response.encoding = "utf-8"
        payload = _mcp_sse_payload(response.text)
        if not payload or isinstance(payload.get("error"), dict):
            raise ResearchSourceError("Exa returned no readable research result")
        result = payload.get("result")
        blocks = result.get("content") if isinstance(result, dict) else None
        text = "\n\n".join(
            str(block.get("text", ""))
            for block in (blocks if isinstance(blocks, list) else [])
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if not text:
            raise ResearchSourceError("Exa returned empty research content")
        return text[:60_000]


def _mcp_sse_payload(body: str) -> dict[str, Any] | None:
    payloads: list[dict[str, Any]] = []
    chunks: list[str] = []

    def collect() -> None:
        if not chunks:
            return
        try:
            payload = json.loads("\n".join(chunks))
        except ValueError:
            return
        if isinstance(payload, dict):
            payloads.append(payload)

    for line in str(body or "").splitlines():
        if line.startswith("data:"):
            chunks.append(line[5:].lstrip())
        elif not line.strip():
            collect()
            chunks = []
    collect()
    return payloads[-1] if payloads else None


def _parse_exa_documents(raw: str, *, lane: str, limit: int) -> list[ResearchDocument]:
    rows: list[dict[str, Any]] = []
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        candidates = parsed.get("results") or parsed.get("data") or []
        if isinstance(candidates, list):
            rows = [row for row in candidates if isinstance(row, dict)]
    elif isinstance(parsed, list):
        rows = [row for row in parsed if isinstance(row, dict)]

    documents: list[ResearchDocument] = []
    seen_urls: set[str] = set()
    for row in rows:
        url = str(row.get("url") or row.get("id") or "").strip()
        title = str(row.get("title") or url).strip()
        snippet = str(row.get("text") or row.get("summary") or row.get("description") or title)
        document = _make_document(
            lane=lane,
            title=title,
            url=url,
            snippet=snippet,
            published_at=row.get("publishedDate") or row.get("published_at") or row.get("date"),
            provider="exa",
        )
        if document and document.url not in seen_urls:
            seen_urls.add(document.url)
            documents.append(document)
        if len(documents) >= limit:
            return documents

    if documents:
        return documents

    # Exa's MCP text renderer is not guaranteed to emit JSON.  Parse explicit
    # links conservatively and use only bounded nearby text as the snippet.
    link_rows = list(_MARKDOWN_LINK_RE.finditer(raw))
    for match in link_rows:
        start = max(0, match.start() - 300)
        end = min(len(raw), match.end() + 700)
        document = _make_document(
            lane=lane,
            title=match.group(1),
            url=match.group(2),
            snippet=raw[start:end],
            published_at=None,
            provider="exa",
        )
        if document and document.url not in seen_urls:
            seen_urls.add(document.url)
            documents.append(document)
        if len(documents) >= limit:
            return documents

    if documents:
        return documents
    for url in _BARE_URL_RE.findall(raw):
        document = _make_document(
            lane=lane,
            title=url,
            url=url,
            snippet=raw[:1_000],
            published_at=None,
            provider="exa",
        )
        if document and document.url not in seen_urls:
            seen_urls.add(document.url)
            documents.append(document)
        if len(documents) >= limit:
            break
    return documents


def _make_document(
    *,
    lane: str,
    title: str,
    url: str,
    snippet: str,
    published_at: Any,
    provider: str,
) -> ResearchDocument | None:
    try:
        normalized_url = validate_public_source_url(url)
        normalized_title = assert_public_safe_text(title)[:500]
        normalized_snippet = assert_public_safe_text(snippet)[:12_000]
    except ValueError:
        return None
    if not normalized_title or not normalized_snippet:
        return None
    source_class, primary = classify_public_source(normalized_url)
    return ResearchDocument(
        lane=lane,
        title=normalized_title,
        url=normalized_url,
        snippet=normalized_snippet,
        published_at=_parse_datetime(published_at),
        source_class=source_class,
        primary_source=primary,
        provider=provider,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "ResearchDocument",
    "ResearchSourceError",
    "ResearchSourcesClient",
    "classify_public_source",
]
