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

import re
import threading
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
    "cognitive_learning": {
        "description": "Persona-private expectations and learning records.",
        "tools": ["record_expectation"],
        "includes": [],
    },
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
    "mail_write": {
        "description": "Outlook send proposals; operator approval executes the exact stored email once.",
        "tools": ["outlook_send_email"],
        "includes": [],
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


# ---------------------------------------------------------------------------
# Capability-plugin toolset overlay (issue #531)
# ---------------------------------------------------------------------------
#
# A plugin contributes a toolset by INSERTING a row into ``TOOLSETS`` itself,
# not by wrapping the resolver. That is deliberate: ``resolve_toolset`` and
# ``resolve_toolset_closure`` both read this dict directly, and a parallel
# overlay dict would mean two structures that must be kept in agreement — the
# exact drift class the module docstring already refuses for the tool catalog.
#
# The static literal above stays the baseline: registration REFUSES any name
# that already exists (static or another plugin's), so a plugin can add a
# toolset but can never shadow one. Removal is compare-and-remove against both
# the ownership record and the physically installed row (Rule 2), so a disposer
# can only delete what its own load installed.


class ToolsetRegistryError(ValueError):
    """Raised on a plugin toolset registration that would shadow or corrupt."""


_PLUGIN_TOOLSET_LOCK = threading.RLock()
# name -> (plugin_id, plugin_version, id(installed row))
_PLUGIN_TOOLSET_OWNERS: dict[str, tuple[str, str, int]] = {}
_TOOLSET_GENERATION: int = 0
_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _validated_plugin_toolset(name: str, toolset: Toolset) -> Toolset:
    if type(name) is not str or not _PLUGIN_NAME_RE.fullmatch(name):
        raise ToolsetRegistryError("plugin toolset name has an invalid shape")
    if type(toolset) is not dict or set(toolset) != {
        "description",
        "tools",
        "includes",
    }:
        raise ToolsetRegistryError(
            "plugin toolset must use the closed description/tools/includes shape"
        )
    description = toolset["description"]
    if type(description) is not str or not description.strip() or len(description) > 400:
        raise ToolsetRegistryError("plugin toolset description is invalid")

    normalized: dict[str, list[str]] = {}
    for field_name in ("tools", "includes"):
        values = toolset[field_name]
        if type(values) is not list or len(values) > 128:
            raise ToolsetRegistryError(f"plugin toolset {field_name} must be a bounded list")
        if any(type(item) is not str or not _PLUGIN_NAME_RE.fullmatch(item) for item in values):
            raise ToolsetRegistryError(f"plugin toolset {field_name} has an invalid name")
        if len(values) != len(set(values)):
            raise ToolsetRegistryError(f"plugin toolset {field_name} contains duplicates")
        normalized[field_name] = list(values)
    if name in normalized["includes"]:
        raise ToolsetRegistryError("plugin toolset may not include itself")
    missing = sorted(item for item in normalized["includes"] if item not in TOOLSETS)
    if missing:
        raise ToolsetRegistryError(
            f"plugin toolset includes unknown toolset {missing[0]!r}"
        )
    return {
        "description": description.strip(),
        "tools": normalized["tools"],
        "includes": normalized["includes"],
    }


def get_toolset_generation() -> int:
    """Current toolset-structure generation. Bumps on every overlay mutation.

    Consumers derive tool catalogs from this registry; the counter exists so a
    test can PROVE an unload is observable on the next assembly rather than
    assert it in a comment (same contract as ``tool_registry.get_generation``).
    """
    return _TOOLSET_GENERATION


def register_plugin_toolset(
    name: str,
    toolset: Toolset,
    *,
    plugin_id: str,
    plugin_version: str,
) -> None:
    """Install one plugin-owned toolset row.

    Raises:
        ToolsetRegistryError: on a blank owner, a blank name, or a name that is
            already present in ``TOOLSETS`` — including the static baseline and
            any other plugin's row.
    """
    global _TOOLSET_GENERATION

    if type(plugin_id) is not str or not plugin_id.strip():
        raise ToolsetRegistryError("plugin toolset registration requires a plugin id")
    if type(plugin_version) is not str or not plugin_version.strip():
        raise ToolsetRegistryError("plugin toolset registration requires a plugin version")
    row = _validated_plugin_toolset(name, toolset)

    with _PLUGIN_TOOLSET_LOCK:
        # Physical check, not a bookkeeping check: the static literal and every
        # other owner's row both live in TOOLSETS, and that dict is the only
        # thing the resolver reads.
        if name in TOOLSETS:
            owner = _PLUGIN_TOOLSET_OWNERS.get(name)
            owner_label = f"{owner[0]}@{owner[1]}" if owner else "<baseline>"
            raise ToolsetRegistryError(
                f"toolset {name!r} is already registered by {owner_label}; "
                f"refusing to shadow it for {plugin_id!r}"
            )
        TOOLSETS[name] = row
        _PLUGIN_TOOLSET_OWNERS[name] = (
            plugin_id.strip(),
            plugin_version.strip(),
            id(row),
        )
        _TOOLSET_GENERATION += 1


def unregister_plugin_toolset(
    name: str,
    *,
    plugin_id: str,
    plugin_version: str,
) -> bool:
    """Compare-and-remove one plugin-owned toolset. True if it was removed.

    Returns False when the row is already gone (disposal is idempotent). Raises
    when the installed row belongs to a different owner, or when the ownership
    record and the physically installed row have diverged — a stale record must
    never authorize deleting somebody else's structure.
    """
    global _TOOLSET_GENERATION

    with _PLUGIN_TOOLSET_LOCK:
        owner = _PLUGIN_TOOLSET_OWNERS.get(name)
        installed = TOOLSETS.get(name)
        if owner is None:
            if installed is None:
                return False
            raise ToolsetRegistryError(
                f"toolset {name!r} is not plugin-owned; refusing to unregister it"
            )
        owner_id, owner_version, row_identity = owner
        if owner_id != plugin_id or owner_version != plugin_version:
            raise ToolsetRegistryError(
                f"toolset {name!r} is owned by {owner_id}@{owner_version}; "
                f"refusing to unregister it for {plugin_id}@{plugin_version}"
            )
        if installed is None:
            _PLUGIN_TOOLSET_OWNERS.pop(name, None)
            _TOOLSET_GENERATION += 1
            return False
        if id(installed) != row_identity:
            raise ToolsetRegistryError(
                f"toolset {name!r} was replaced after registration; refusing "
                "to unregister a row this owner did not install"
            )
        del TOOLSETS[name]
        _PLUGIN_TOOLSET_OWNERS.pop(name, None)
        _TOOLSET_GENERATION += 1
        return True


def plugin_toolset_owner(name: str) -> tuple[str, str] | None:
    """Return ``(plugin_id, plugin_version)`` for a plugin-owned toolset."""
    owner = _PLUGIN_TOOLSET_OWNERS.get(name)
    installed = TOOLSETS.get(name)
    if owner is None or installed is None or id(installed) != owner[2]:
        return None
    return owner[0], owner[1]


def list_plugin_toolsets() -> tuple[tuple[str, str, str], ...]:
    """Every plugin-owned toolset as ``(name, plugin_id, plugin_version)``.

    Diagnostics only. Structure lives in ``TOOLSETS``; this answers ownership.
    """
    return tuple(
        (name, owner[0], owner[1])
        for name, owner in sorted(_PLUGIN_TOOLSET_OWNERS.items())
        if name in TOOLSETS and id(TOOLSETS[name]) == owner[2]
    )
