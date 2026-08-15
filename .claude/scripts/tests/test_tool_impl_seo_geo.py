"""Focused safety and registration tests for SEO/GEO caller tools."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from runtime import tool_impl_seo_geo, tool_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = dict(tool_registry._REGISTRY)
    tool_registry._REGISTRY.clear()
    yield
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(saved)


def test_registers_all_seo_geo_tools_as_read_only():
    assert tool_impl_seo_geo.register_tools() == 16
    for name in (
        "gsc_overview", "gsc_top_queries", "gsc_top_pages", "gsc_query_page_slice", "ga4_overview",
        "ga4_top_pages", "ga4_traffic_sources", "firecrawl_scrape", "firecrawl_map",
        "seo_exa_search", "seo_exa_fetch", "openseo_read", "fleet_pulse_latest",
        "fleet_measurement_registry_latest",
        "fleet_control_review_latest",
        "fleet_paid_research_latest",
    ):
        entry = tool_registry.get_entry(name)
        assert entry is not None
        assert entry.effect == "read"
        assert entry.toolset == "seo_geo_read"
        assert entry.handler is not None


def test_gsc_query_page_slice_rejects_private_page_before_google_call():
    assert "local/private" in tool_impl_seo_geo._gsc_query_page_slice(
        site_url="sc-domain:your-business.example.com",
        page_url="http://127.0.0.1/private",
    )


def test_control_review_rejects_unknown_mode_without_reading_files():
    assert tool_impl_seo_geo._fleet_control_review_latest("daily") == "error: mode must be weekly or monthly"


def test_paid_research_receipt_is_read_without_provider_call(tmp_path, monkeypatch):
    (tmp_path / "latest.json").write_text('{"provider":{"name":"dataforseo"}}', encoding="utf-8")
    monkeypatch.setattr(tool_impl_seo_geo, "_FLEET_PAID_RESEARCH_ROOT", tmp_path)
    result = tool_impl_seo_geo._fleet_paid_research_latest()
    assert '"dataforseo"' in result


def test_firecrawl_refuses_private_url_before_provider_call(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "configured")
    assert "local/private" in tool_impl_seo_geo._firecrawl_map("http://127.0.0.1:3001/")


def test_firecrawl_reports_missing_credential_without_network(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(tool_impl_seo_geo, "_CLAUDE_FIRECRAWL_MCP_CONFIG", Path("missing-firecrawl-mcp.json"))
    assert "not configured" in tool_impl_seo_geo._firecrawl_scrape("https://example.com")


def test_firecrawl_inherits_the_existing_claude_mcp_credential_source(tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"firecrawl":{"env":{"FIRECRAWL_API_KEY":"inherited-key"}}}}',
        encoding="utf-8",
    )
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(tool_impl_seo_geo, "_CLAUDE_FIRECRAWL_MCP_CONFIG", config)
    assert tool_impl_seo_geo.firecrawl_configured() is True


def test_exa_fetch_refuses_private_url_before_provider_call():
    assert "local/private" in tool_impl_seo_geo._seo_exa_fetch(["http://127.0.0.1:3001/"])


def test_exa_sse_parser_reads_the_final_json_rpc_payload():
    body = (
        'event: message\n'
        'data: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"exa"}}}\n\n'
        'event: message\n'
        'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"ok"}]}}\n\n'
    )
    assert tool_impl_seo_geo._mcp_sse_payload(body) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"content": [{"type": "text", "text": "ok"}]},
    }
