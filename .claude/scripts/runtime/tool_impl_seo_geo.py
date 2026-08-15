"""Read-only SEO/GEO caller tools for the ``seo_geo`` Homie persona.

These tools deliberately wrap existing first-party integrations rather than
turning the persona into an unrestricted shell.  They are safe to offer in
interactive SEO/GEO conversations and return clear, compact receipts whenever
an optional provider is not configured.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

_logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 12_000
_FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v1"
_EXA_MCP_ENDPOINT = "https://mcp.exa.ai/mcp"
_CLAUDE_FIRECRAWL_MCP_CONFIG = Path.home() / ".claude" / "mcp.json"
_OPENSEO_ENDPOINT = "http://127.0.0.1:3001/mcp"
_FLEET_PULSE_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo" / "data" / "fleet-pulse"
_FLEET_MEASUREMENT_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo" / "data" / "fleet-measurement"
_FLEET_CONTROL_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo" / "data" / "fleet-control"
_FLEET_PAID_RESEARCH_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo" / "data" / "fleet-paid-research"


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[TRUNCATED — {len(text) - limit} more chars]"


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _json(payload: Any) -> str:
    if is_dataclass(payload):
        payload = asdict(payload)
    if isinstance(payload, list):
        payload = [asdict(item) if is_dataclass(item) else item for item in payload]
    return _truncate(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _public_http_url(value: str) -> tuple[str | None, str | None]:
    """Allow public HTTP(S) URLs only; never forward local/private targets."""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None, "url is invalid"
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return None, "url must be an absolute http(s) URL"
    if host == "localhost" or host.endswith(".local"):
        return None, "local/private URLs are not allowed"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        return None, "local/private URLs are not allowed"
    return parsed.geturl(), None


def _firecrawl_api_key() -> str:
    """Resolve Firecrawl from the persona env or the existing Claude MCP source.

    The machine's Claude MCP already owns the Firecrawl credential.  Reusing
    that source avoids copying a secret into a persona profile or returning it
    to a model.  An explicit profile environment value still takes precedence.
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if api_key:
        return api_key
    try:
        payload = json.loads(_CLAUDE_FIRECRAWL_MCP_CONFIG.read_text(encoding="utf-8"))
        configured = payload.get("mcpServers", {}).get("firecrawl", {})
        inherited = str(configured.get("env", {}).get("FIRECRAWL_API_KEY", "")).strip()
        if inherited and not inherited.startswith("${"):
            return inherited
    except (OSError, ValueError, TypeError):
        pass
    return ""


def firecrawl_configured() -> bool:
    """Whether a server-side Firecrawl credential is available, without revealing it."""
    return bool(_firecrawl_api_key())


def _firecrawl_request(path: str, body: dict[str, Any]) -> dict[str, Any] | str:
    api_key = _firecrawl_api_key()
    if not api_key:
        return "unavailable: Firecrawl is not configured for seo_geo"
    try:
        response = requests.post(
            f"{_FIRECRAWL_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"data": payload}
    except (requests.RequestException, ValueError) as exc:
        _logger.info("Firecrawl read failed: %s", type(exc).__name__)
        return f"unavailable: Firecrawl request failed ({type(exc).__name__})"


def _firecrawl_scrape(url: str = "", only_main_content: bool = True, **_: Any) -> str:
    target, error = _public_http_url(url)
    if error:
        return f"error: {error}"
    result = _firecrawl_request(
        "/scrape",
        {"url": target, "formats": ["markdown"], "onlyMainContent": bool(only_main_content)},
    )
    if isinstance(result, str):
        return result
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    markdown = str(data.get("markdown") or "") if isinstance(data, dict) else ""
    return _json({
        "url": target,
        "title": data.get("metadata", {}).get("title") if isinstance(data, dict) else None,
        "markdown": markdown,
        "markdown_chars": len(markdown),
    })


def _firecrawl_map(url: str = "", limit: int = 100, **_: Any) -> str:
    target, error = _public_http_url(url)
    if error:
        return f"error: {error}"
    result = _firecrawl_request(
        "/map",
        {"url": target, "limit": _bounded_int(limit, default=100, minimum=1, maximum=500)},
    )
    if isinstance(result, str):
        return result
    links = result.get("links") or result.get("data") or []
    if not isinstance(links, list):
        links = []
    normalized = [item.get("url") if isinstance(item, dict) else item for item in links]
    return _json({"url": target, "discovered": len(normalized), "links": normalized[:250]})


def _mcp_sse_payload(body: str) -> dict[str, Any] | None:
    """Return the final JSON-RPC payload from a compact MCP SSE response."""
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

    for line in body.splitlines():
        if line.startswith("data:"):
            chunks.append(line[5:].lstrip())
        elif not line.strip():
            collect()
            chunks = []
    collect()
    return payloads[-1] if payloads else None


def _exa_mcp_call(name: str, arguments: dict[str, Any]) -> str:
    """Call Exa's read-only MCP endpoint without exposing any credential.

    Exa's public MCP endpoint currently supports an anonymous session.  This
    wrapper deliberately has no credential fallback or write operation: if the
    service later requires authentication, the persona reports it as unavailable
    instead of borrowing an unrelated secret.
    """
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-03-26",
        "User-Agent": "Homie-SEO-GEO/1.0",
    }
    try:
        initialize = requests.post(
            _EXA_MCP_ENDPOINT,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "homie-seo-geo", "version": "1.0"},
                },
            },
            timeout=30,
        )
        initialize.raise_for_status()
        session_id = initialize.headers.get("Mcp-Session-Id")
        if not session_id:
            return "unavailable: Exa did not create an MCP session"
        session_headers = {**headers, "Mcp-Session-Id": session_id}
        initialized = requests.post(
            _EXA_MCP_ENDPOINT,
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            timeout=15,
        )
        initialized.raise_for_status()
        response = requests.post(
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
    except requests.RequestException as exc:
        _logger.info("Exa read failed: %s", type(exc).__name__)
        return f"unavailable: Exa research request failed ({type(exc).__name__})"

    # The stream endpoint can omit a charset even though the SSE payload is UTF-8.
    # Force it before reading ``text`` so source quotes do not become mojibake.
    response.encoding = "utf-8"
    payload = _mcp_sse_payload(response.text)
    if not payload:
        return "unavailable: Exa returned no readable MCP result"
    if isinstance(payload.get("error"), dict):
        return "unavailable: Exa rejected the research request"
    result = payload.get("result")
    blocks = result.get("content") if isinstance(result, dict) else None
    text = "\n\n".join(
        str(block.get("text", ""))
        for block in (blocks if isinstance(blocks, list) else [])
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    return _truncate(text) if text else "unavailable: Exa returned no readable research content"


def _seo_exa_search(query: str = "", limit: int = 5, **_: Any) -> str:
    """Search public web sources through the SEO/GEO-scoped Exa capability."""
    normalized = str(query or "").strip()
    if not normalized:
        return "error: query is required"
    return _exa_mcp_call(
        "web_search_exa",
        {
            "query": normalized[:800],
            "numResults": _bounded_int(limit, default=5, minimum=1, maximum=10),
        },
    )


def _seo_exa_fetch(urls: list[str] | None = None, max_characters: int = 4_000, **_: Any) -> str:
    """Fetch a small public source set through Exa, rejecting local targets."""
    if not isinstance(urls, list) or not urls:
        return "error: urls must contain at least one public http(s) URL"
    targets: list[str] = []
    for value in urls[:5]:
        target, error = _public_http_url(str(value))
        if error:
            return f"error: {error}"
        assert target is not None
        targets.append(target)
    return _exa_mcp_call(
        "web_fetch_exa",
        {
            "urls": targets,
            "maxCharacters": _bounded_int(max_characters, default=4_000, minimum=500, maximum=8_000),
        },
    )


def _gsc_overview(days: int = 28, **_: Any) -> str:
    try:
        from integrations.search_console_api import get_overall_stats

        return _json(get_overall_stats(days=_bounded_int(days, default=28, minimum=1, maximum=90)))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: Google Search Console overview failed ({type(exc).__name__})"


def _gsc_top_queries(days: int = 28, limit: int = 20, **_: Any) -> str:
    try:
        from integrations.search_console_api import get_top_queries

        return _json(get_top_queries(
            days=_bounded_int(days, default=28, minimum=1, maximum=90),
            max_results=_bounded_int(limit, default=20, minimum=1, maximum=100),
        ))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: Google Search Console query read failed ({type(exc).__name__})"


def _gsc_top_pages(days: int = 28, limit: int = 20, **_: Any) -> str:
    try:
        from integrations.search_console_api import get_top_pages

        return _json(get_top_pages(
            days=_bounded_int(days, default=28, minimum=1, maximum=90),
            max_results=_bounded_int(limit, default=20, minimum=1, maximum=100),
        ))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: Google Search Console page read failed ({type(exc).__name__})"


def _gsc_query_page_slice(
    site_url: str = "",
    page_url: str = "",
    query: str = "",
    days: int = 28,
    limit: int = 100,
    start_row: int = 0,
    **_: Any,
) -> str:
    """Read bounded finalized query/page evidence for one GSC property.

    A caller must provide a GSC property for a satellite.  When filtering by
    page, the caller supplies its exact canonical URL rather than a relative
    path so YourBusiness's apex-to-www distinction is never silently lost.
    """
    try:
        from integrations.search_console_api import get_query_page_slice

        normalized_site = str(site_url or "").strip() or None
        normalized_page = str(page_url or "").strip()
        if normalized_page:
            canonical_page, error = _public_http_url(normalized_page)
            if error:
                return f"error: page_url {error}"
            normalized_page = canonical_page or ""
        return _json(get_query_page_slice(
            site_url=normalized_site,
            page_url=normalized_page or None,
            query=str(query or "").strip() or None,
            days=_bounded_int(days, default=28, minimum=1, maximum=90),
            max_results=_bounded_int(limit, default=100, minimum=1, maximum=250),
            start_row=_bounded_int(start_row, default=0, minimum=0, maximum=25_000),
        ))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: Google Search Console query/page read failed ({type(exc).__name__})"


def _ga4_overview(days: int = 28, **_: Any) -> str:
    try:
        from integrations.analytics_api import get_overview

        return _json(get_overview(days=_bounded_int(days, default=28, minimum=1, maximum=90)))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: GA4 overview failed ({type(exc).__name__})"


def _ga4_top_pages(days: int = 28, limit: int = 20, **_: Any) -> str:
    try:
        from integrations.analytics_api import get_top_pages

        return _json(get_top_pages(
            days=_bounded_int(days, default=28, minimum=1, maximum=90),
            max_results=_bounded_int(limit, default=20, minimum=1, maximum=100),
        ))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: GA4 top-pages read failed ({type(exc).__name__})"


def _ga4_traffic_sources(days: int = 28, limit: int = 20, **_: Any) -> str:
    try:
        from integrations.analytics_api import get_traffic_sources

        return _json(get_traffic_sources(
            days=_bounded_int(days, default=28, minimum=1, maximum=90),
            max_results=_bounded_int(limit, default=20, minimum=1, maximum=100),
        ))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: GA4 traffic-source read failed ({type(exc).__name__})"


_OPENSEO_OPERATIONS = {
    "projects": "list_projects",
    "rank_tracker": "get_rank_tracker",
    "saved_keywords": "list_saved_keywords",
    "gsc_performance": "get_search_console_performance",
}


def _openseo_read(
    operation: str = "projects",
    project_id: str = "",
    days: int = 28,
    **_: Any,
) -> str:
    """Call only documented free OpenSEO MCP reads on loopback."""
    tool = _OPENSEO_OPERATIONS.get(str(operation).strip())
    if tool is None:
        return "error: operation must be projects, rank_tracker, saved_keywords, or gsc_performance"
    if tool != "list_projects" and not project_id.strip():
        return "error: project_id is required for this OpenSEO read"
    arguments: dict[str, Any] = {}
    if project_id.strip():
        arguments["projectId"] = project_id.strip()
    if tool == "get_search_console_performance":
        arguments["days"] = _bounded_int(days, default=28, minimum=1, maximum=90)
    try:
        response = requests.post(
            _OPENSEO_ENDPOINT,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "mcp-protocol-version": "2025-03-26"},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            return "unavailable: OpenSEO MCP returned an error"
        result = payload.get("result", {}).get("structuredContent", {})
        return _json({"operation": operation, "result": result})
    except (requests.RequestException, ValueError) as exc:
        _logger.info("OpenSEO read failed: %s", type(exc).__name__)
        return f"unavailable: OpenSEO MCP read failed ({type(exc).__name__})"


def _fleet_pulse_latest(**_: Any) -> str:
    report = _FLEET_PULSE_ROOT / "latest.md"
    if not report.is_file():
        return "unavailable: no SEO/GEO fleet-pulse receipt exists yet"
    try:
        return _truncate(report.read_text(encoding="utf-8"))
    except OSError as exc:
        return f"unavailable: could not read fleet-pulse receipt ({type(exc).__name__})"


def _fleet_measurement_registry_latest(**_: Any) -> str:
    """Read the latest local 27-brand measurement coverage receipt."""
    report = _FLEET_MEASUREMENT_ROOT / "registry.md"
    if not report.is_file():
        return "unavailable: no SEO/GEO fleet measurement registry exists yet"
    try:
        return _truncate(report.read_text(encoding="utf-8"))
    except OSError as exc:
        return f"unavailable: could not read fleet measurement registry ({type(exc).__name__})"


def _fleet_control_review_latest(mode: str = "weekly", **_: Any) -> str:
    """Read a zero-spend weekly or monthly control review generated from receipts."""
    normalized = str(mode or "weekly").strip().lower()
    if normalized not in {"weekly", "monthly"}:
        return "error: mode must be weekly or monthly"
    report = _FLEET_CONTROL_ROOT / f"{normalized}-latest.md"
    if not report.is_file():
        return f"unavailable: no {normalized} SEO/GEO control review exists yet"
    try:
        return _truncate(report.read_text(encoding="utf-8"))
    except OSError as exc:
        return f"unavailable: could not read fleet control review ({type(exc).__name__})"


def _fleet_paid_research_latest(**_: Any) -> str:
    """Read a saved brokered paid-research receipt without invoking a provider."""
    markdown_report = _FLEET_PAID_RESEARCH_ROOT / "latest.md"
    json_report = _FLEET_PAID_RESEARCH_ROOT / "latest.json"
    try:
        if markdown_report.is_file():
            return _truncate(markdown_report.read_text(encoding="utf-8"))
        if json_report.is_file():
            payload = json.loads(json_report.read_text(encoding="utf-8"))
            return _json(payload)
    except (OSError, ValueError, TypeError) as exc:
        return f"unavailable: could not read paid-research receipt ({type(exc).__name__})"
    return "unavailable: no brokered SEO/GEO paid-research receipt exists yet"


_SPECS: tuple[tuple[str, str, str, dict[str, Any], Any, str | None], ...] = (
    ("gsc_overview", "seo_geo_read", "Read the configured Search Console property's settled performance.", {"type": "object", "properties": {"days": {"type": "integer", "description": "Settled-day window, 1-90."}}}, _gsc_overview, "search_console.overview"),
    ("gsc_top_queries", "seo_geo_read", "Read top GSC queries for the configured property.", {"type": "object", "properties": {"days": {"type": "integer"}, "limit": {"type": "integer"}}}, _gsc_top_queries, "search_console.top_queries"),
    ("gsc_top_pages", "seo_geo_read", "Read top GSC pages for the configured property.", {"type": "object", "properties": {"days": {"type": "integer"}, "limit": {"type": "integer"}}}, _gsc_top_pages, "search_console.top_pages"),
    ("gsc_query_page_slice", "seo_geo_read", "Read bounded finalized GSC query/page evidence for a property. Use a full canonical page URL when filtering by page; results are not exhaustive.", {"type": "object", "properties": {"site_url": {"type": "string", "description": "Optional GSC property, e.g. sc-domain:example.com."}, "page_url": {"type": "string", "description": "Optional full canonical page URL."}, "query": {"type": "string"}, "days": {"type": "integer"}, "limit": {"type": "integer", "maximum": 250}, "start_row": {"type": "integer"}}}, _gsc_query_page_slice, "search_console.query_page_slice"),
    ("ga4_overview", "seo_geo_read", "Read GA4 overview metrics for the configured property.", {"type": "object", "properties": {"days": {"type": "integer"}}}, _ga4_overview, "analytics.overview"),
    ("ga4_top_pages", "seo_geo_read", "Read GA4 top pages for the configured property.", {"type": "object", "properties": {"days": {"type": "integer"}, "limit": {"type": "integer"}}}, _ga4_top_pages, "analytics.top_pages"),
    ("ga4_traffic_sources", "seo_geo_read", "Read GA4 traffic sources for the configured property.", {"type": "object", "properties": {"days": {"type": "integer"}, "limit": {"type": "integer"}}}, _ga4_traffic_sources, "analytics.traffic_sources"),
    ("firecrawl_scrape", "seo_geo_read", "Use Firecrawl to extract one public page as clean markdown. Requires configured Firecrawl credentials.", {"type": "object", "properties": {"url": {"type": "string"}, "only_main_content": {"type": "boolean"}}, "required": ["url"]}, _firecrawl_scrape, None),
    ("firecrawl_map", "seo_geo_read", "Use Firecrawl to map a public site, capped at 500 discovered URLs. Requires configured Firecrawl credentials.", {"type": "object", "properties": {"url": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["url"]}, _firecrawl_map, None),
    ("seo_exa_search", "seo_geo_read", "Search current public web sources through Exa, capped at ten results. Read-only; no Exa credential is exposed to the persona.", {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}, _seo_exa_search, None),
    ("seo_exa_fetch", "seo_geo_read", "Fetch up to five public sources through Exa for source-backed SEO/GEO research. Read-only.", {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}, "maxItems": 5}, "max_characters": {"type": "integer"}}, "required": ["urls"]}, _seo_exa_fetch, None),
    ("openseo_read", "seo_geo_read", "Run a documented free read against local OpenSEO: projects, rank tracker, saved keywords, or GSC performance.", {"type": "object", "properties": {"operation": {"type": "string", "enum": sorted(_OPENSEO_OPERATIONS)}, "project_id": {"type": "string"}, "days": {"type": "integer"}}}, _openseo_read, None),
    ("fleet_pulse_latest", "seo_geo_read", "Read the most recent read-only 27-site SEO/GEO fleet-pulse receipt.", {"type": "object", "properties": {}}, _fleet_pulse_latest, None),
    ("fleet_measurement_registry_latest", "seo_geo_read", "Read the latest 27-brand local measurement registry. It distinguishes source declarations from production tag, event, and terminal lead proof.", {"type": "object", "properties": {}}, _fleet_measurement_registry_latest, None),
    ("fleet_control_review_latest", "seo_geo_read", "Read the latest zero-spend weekly or monthly control review created from saved fleet receipts.", {"type": "object", "properties": {"mode": {"type": "string", "enum": ["weekly", "monthly"]}}}, _fleet_control_review_latest, None),
    ("fleet_paid_research_latest", "seo_geo_read", "Read the latest brokered DataForSEO paid-research receipt. This tool never runs a provider call.", {"type": "object", "properties": {}}, _fleet_paid_research_latest, None),
)


def register_tools() -> int:
    """Register the actual read-only SEO/GEO tools; safe to call repeatedly."""
    from runtime import tool_registry

    registered = 0
    for name, toolset, description, parameters, handler, integration_action in _SPECS:
        try:
            tool_registry.register_tool(
                name,
                description,
                toolset=toolset,
                parameters=parameters,
                handler=handler,
                effect="read",
                integration_action=integration_action,
                elevatable=True,
            )
            registered += 1
        except Exception:  # noqa: BLE001
            _logger.warning("failed to register SEO/GEO tool %r", name, exc_info=True)
    return registered


__all__ = ["register_tools"]
