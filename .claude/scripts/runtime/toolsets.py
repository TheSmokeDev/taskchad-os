"""Static toolset registry — Hermes shape (dict-of-dicts, not dataclass).

Auto-discovery extension: toolsets carrying ``live_source`` and ``live_filter``
resolve their contents at every ``resolve_toolset()`` call via
``list_capabilities()``. No cache — the registry captures structural intent
only; the actual tools come from the live aggregator surface.

The static dict literal below is the single source of truth for toolset
structure. There is no build function, no cache variable, and no refresh API.
This is the Hermes-faithful pivot: data-shape parity with
``hermes-agent/toolsets.py`` (lines 68+ for the literal, lines 504-554 for the
resolver). The single deviation is the optional ``live_source`` /
``live_filter`` pair, which generalizes Hermes' own plugin late-lookup pattern
(``get_toolset()`` lines 472-501) for The Homie's adopter story.

Modules in this package never import from ``runtime.capabilities`` here at
load time — both modules late-import each other inside functions, so this file
remains a leaf module.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class Toolset(TypedDict):
    """Toolset shape (Hermes-faithful, with optional auto-discovery extension).

    Required fields match Hermes verbatim. ``live_source`` and ``live_filter``
    are NotRequired and are The Homie's product-justified extension for
    auto-discovery (no analogue in Hermes).
    """

    description: str
    tools: list[str]
    includes: list[str]
    # The Homie's auto-discovery extension (not in Hermes):
    live_source: NotRequired[str]
    live_filter: NotRequired[str]


# ---------------------------------------------------------------------------
# Capability classes
# ---------------------------------------------------------------------------
#
# ``core`` shipped as one wide Hermes-compatible bundle that mixed recall with
# terminal and write authority. Existing profiles may rely on that effective
# grant, so ``core`` remains as a compatibility wrapper. New persona blueprints
# compile against the two explicit classes below instead:
#
# * ``safe_core`` — profile-scoped indexed memory search and skill reading.
# * ``operator_exec`` — broad file reads, shell/process access, writes, patching,
#   and draft-skill mutation. It is never implied by persona creation.
#
# The split is structural rather than cosmetic. A domain pack may include
# ``safe_core`` but it may not inherit ``operator_exec``. Scheduled curriculum
# study does not use either class; it keeps its existing ``model_only`` runtime.
_HOMIE_SAFE_CORE_TOOLS: list[str] = [
    "memory_search",
    "search_files",
    "skills_list",
    "skill_view",
    "request_tool",
]

_HOMIE_OPERATOR_EXEC_TOOLS: list[str] = [
    "terminal",
    "process",
    # ``read_file`` is here because its current confinement spans the repo and
    # the whole ~/.homie tree rather than the active persona only.
    "read_file",
    "write_file",
    "patch",
    "skill_manage",
]

# Backward-compatible flattened name used by the existing tool-calling tests
# and diagnostics. It is the exact effective membership of legacy ``core``.
_HOMIE_CORE_TOOLS: list[str] = [
    *_HOMIE_SAFE_CORE_TOOLS,
    *_HOMIE_OPERATOR_EXEC_TOOLS,
]

_RESEARCH_READ_TOOLS: list[str] = [
    "web_search",
    "web_extract",
    "firecrawl_scrape",
    "firecrawl_search",
    "exa_search",
    "x_search",
]

_SEO_GEO_READ_TOOLS: list[str] = [
    "gsc_overview",
    "gsc_top_queries",
    "gsc_top_pages",
    "gsc_query_page_slice",
    "ga4_overview",
    "ga4_top_pages",
    "ga4_traffic_sources",
    "firecrawl_scrape",
    "firecrawl_map",
    "seo_exa_search",
    "seo_exa_fetch",
    "openseo_read",
    "fleet_pulse_latest",
    "fleet_measurement_registry_latest",
    "fleet_control_review_latest",
    "fleet_paid_research_latest",
]

_REPO_READ_TOOLS: list[str] = [
    "gh_issue_view",
    "gh_issue_list",
    "gh_pr_view",
    "gh_pr_list",
    "gh_run_list",
    "repo_search",
]

_BUSINESS_READ_TOOLS: list[str] = [
    "sheets_read",
]

_BROWSER_READ_TOOLS: list[str] = [
    "browser_status",
    "browser_tabs",
    "browser_navigate",
    "browser_snapshot",
    "browser_console",
]

# Static module-level registry. Hermes shape: dict of dicts.
#
# Auto-discovery toolsets (those carrying ``live_source``) resolve their
# contents by calling ``list_capabilities(sources=[live_source])`` on every
# ``resolve_toolset()`` call. There is no cache layer between the registry
# and the live aggregator — staleness window is zero.
TOOLSETS: dict[str, Toolset] = {
    # -----------------------------------------------------------------------
    # Blueprint-safe classes and domain packs.
    # -----------------------------------------------------------------------
    "safe_core": {
        "description": "Safe persona floor: scoped memory search, skill reads, and request-tool escalation",
        "tools": _HOMIE_SAFE_CORE_TOOLS,
        "includes": [],
    },
    "operator_exec": {
        "description": "Explicit operator-exec authority: shell, process, broad files, writes",
        "tools": _HOMIE_OPERATOR_EXEC_TOOLS,
        "includes": ["safe_core"],
    },
    "research_read": {
        "description": "Read-only web, Firecrawl, Exa, and X research",
        "tools": _RESEARCH_READ_TOOLS,
        "includes": ["safe_core"],
    },
    "seo_geo_read": {
        "description": "Read-only SEO/GEO intelligence: GSC, GA4, Firecrawl, local OpenSEO, and fleet receipts",
        "tools": _SEO_GEO_READ_TOOLS,
        "includes": ["research_read"],
    },
    "repo_read": {
        "description": "Read-only repository and GitHub inspection",
        "tools": _REPO_READ_TOOLS,
        "includes": ["safe_core"],
    },
    "business_read": {
        "description": "Bounded read-only access to configured business data integrations",
        "tools": _BUSINESS_READ_TOOLS,
        "includes": ["safe_core"],
    },
    "browser_read": {
        "description": "Visible-browser navigation and observation without browser writes",
        "tools": _BROWSER_READ_TOOLS,
        "includes": ["research_read"],
    },
    "ai_engineering": {
        "description": "AI engineering domain pack: web/browser research plus repository reads",
        "tools": [],
        "includes": ["browser_read", "repo_read", "business_read"],
    },
    "founder_operations": {
        "description": "Founder/operator domain pack: market research plus repository reads",
        "tools": [],
        "includes": ["research_read", "repo_read", "business_read"],
    },
    # -----------------------------------------------------------------------
    # Legacy compatibility toolsets.
    #
    # These preserve the effective grants of profiles authored before persona
    # blueprints. In particular, ``core`` still resolves to terminal and writes,
    # and research/browser/repo still inherit that wide legacy floor. New
    # profiles must compile against the explicit classes above.
    # -----------------------------------------------------------------------
    "core": {
        "description": "Legacy wide core compatibility alias (safe core + operator exec)",
        "tools": [],
        "includes": ["safe_core", "operator_exec"],
    },
    "research": {
        "description": (
            "Read-only research: web search, Firecrawl scrape/crawl, Exa, and X "
            "reads, plus the legacy wide core grant."
        ),
        "tools": [],
        "includes": ["research_read", "operator_exec"],
    },
    "repo": {
        "description": "Legacy repository/GitHub reads plus operator-exec authority",
        "tools": [],
        "includes": ["repo_read", "operator_exec"],
    },
    "browser": {
        "description": (
            "Visible-Chrome browser automation via the BrowserOps CDP session. "
            "READ verbs only — navigate/snapshot/read. Browser WRITE actions "
            "(post, DM, connect, profile edit) stay default-denied behind their "
            "own operator-approval gates. Retains the legacy operator-exec floor."
        ),
        "tools": [],
        "includes": ["browser_read", "operator_exec"],
    },
    "crypto": {
        "description": (
            "Crypto desk: live candles/indicators/levels, DexScreener + Polymarket "
            "reads, the play ledger, and the paper ladder — composed on top of "
            "browser (X/Discord reads) and repo."
        ),
        # Operator direction 2026-07-27: "he already uses Twitter... he needs
        # browser ops and shit... the repo, GH... X and Firecrawl to do
        # research." The desk's work CROSSES these surfaces constantly, and a
        # scoped persona that has to stop mid-thought because the next step is
        # in another toolset is the same "I can't do it" this epic exists to
        # kill — just relocated.
        #
        # So `crypto` composes rather than enumerating: browser pulls in
        # research, research pulls in core. One line here is the whole desk.
        # Every name here must be REGISTERED somewhere, or the ownership check
        # refuses it and the persona is silently short a tool it was promised.
        # That is not hypothetical: `crypto_indicators`, `crypto_levels`,
        # `crypto_plays_read` and `crypto_paper_read` sat in this list unwired
        # for the whole first pass of the epic — declared, refused, invisible.
        "tools": [
            # Market read
            "crypto_candles",
            "crypto_indicators",
            "crypto_levels",
            "crypto_funding",
            "crypto_bar_clock",
            "crypto_desk_snapshot",
            "crypto_dexscreener",
            "crypto_mintscan",
            "crypto_polymarket",
            "crypto_last30days_read",
            "crypto_prediction_markets",
            "crypto_prediction_book",
            "crypto_source_tape",
            "crypto_chart",
            # Risk + sizing — read-only maths, no order path
            "crypto_position_size",
            "crypto_liquidation",
            "crypto_safety_check",
            "crypto_proof",
            "crypto_call_anchor",
            "crypto_hit_rate",
            "crypto_looks_read",
            # The book
            "crypto_plays_read",
            "crypto_paper_read",
            # The paper order path is a standing persona capability. The guard
            # mode is hard-coded DRY_RUN; no tool argument can select LIVE.
            "crypto_mandate_read",
            "crypto_preflight",
            "crypto_submit_bracket",
        ],
        "includes": ["browser", "repo"],
    },
    "social": {
        "description": (
            "Social/marketing research: Firecrawl + X + web search via research, "
            "plus browser reads. Read-only — every social WRITE keeps its own "
            "operator-approval gate and is never reachable from a toolset."
        ),
        "tools": [],
        "includes": ["browser"],
    },
    "x_social_write": {
        "description": (
            "X write verbs (follow accounts, enable notifications). Membership "
            "grants REACH only: every write tool here creates an operator-approval "
            "proposal, and execution requires the dedicated action gate "
            "(/act approve) — writes are never reachable from this grant alone."
        ),
        "tools": ["x_follow_accounts", "x_enable_notifications"],
        "includes": ["browser_read"],
    },
    "ga4_fleet_write": {
        "description": (
            "GA4 fleet write verbs (provision a brand's property/stream, deploy "
            "its tag to Vercel + verify). Membership grants REACH only: every "
            "write tool here creates an operator-approval proposal, and execution "
            "requires the dedicated action gate (/act approve) — writes are "
            "never reachable from this grant alone."
        ),
        "tools": ["ga4_provision_site", "ga4_deploy_tag"],
        "includes": ["seo_geo_read"],
    },
    "chat_commands": {
        "description": "All registered chat commands (auto-discovered from extension manager)",
        # No hand-listed tools — auto-discovery via live_source.
        "tools": [],
        "includes": [],
        "live_source": "chat_extensions",
        "live_filter": "chat.command.",
    },
    "chat_intents": {
        "description": "All registered chat intent detectors (auto-discovered)",
        "tools": [],
        "includes": [],
        "live_source": "chat_extensions",
        "live_filter": "chat.intent.",
    },
    "chat_all": {
        "description": "All chat capabilities (commands + intents)",
        "tools": [],
        # NOTE (R2 Minor 2): each child toolset declares
        # ``live_source="chat_extensions"``, so resolving ``chat_all`` calls
        # ``list_capabilities(sources=["chat_extensions"])`` TWICE — once for
        # each child. The same source is aggregated twice on every resolve.
        # Acceptable for cold-path callers (admin / diagnostics); do not call
        # ``resolve_toolset("chat_all")`` from hot paths. See
        # ``capabilities.resolve_toolset`` for the resolver implementation.
        "includes": ["chat_commands", "chat_intents"],
    },
    "integrations": {
        "description": "All registered integrations (auto-discovered from integrations registry)",
        "tools": [],
        "includes": [],
        "live_source": "integrations",
        "live_filter": "integration.",
    },
}
