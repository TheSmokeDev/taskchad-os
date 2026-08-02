"""The eyes — tools that let a persona LOOK, instead of reading a stale card.

The gap this closes, in the operator's words: *"is he actually reading what
Debauchery's saying, is he intelligently parsing Twitter... or is he just
running a script and seeing the shape?"*

He was running the script. A separate pipeline (`x_networking/`,
`discord_alpha/`) swept every couple of hours, graded findings into cards, and
the persona read the CARD. It could describe what the sweep found two hours
ago; it could not go and look. Every tool that would have let it — `x_search`,
`browser_navigate`, `browser_snapshot` — was declared in the `crypto` toolset
and wired to nothing.

These handlers reuse the EXACT machinery the desk uses, which is what makes
them safe to hand to a persona:

* **`x_search` delegates to `x_networking.collector.collect_one`.** That
  function reserves the ban-safety budget (`x_rate.reserve_call`) BEFORE the
  browser moves, so a persona searching X on its own initiative spends the same
  1-sweep/2h allowance the scheduled desk spends. Calling X any other way would
  route around the one guard standing between this account and a ban.
* **Browser reads go through `chat.browser_control`** and therefore the
  existing visible-Chrome CDP session on port 18222 — never a headless
  fallback, never a fresh profile, never a copied profile.

Read-only by construction. Nothing here clicks, types, posts, follows, or DMs.
Those keep their own verbatim-phrase operator gates and are not reachable from
a toolset.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger(__name__)

# CDP port is 18222 on this deployment, never 9222 — 9222 sits inside a Windows
# WSL2/Hyper-V reserved range and bind() returns WSAEACCES.
_DEFAULT_CDP_PORT = 18222
_MAX_RESULT_CHARS = 6000


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[TRUNCATED — {len(text) - limit} more chars]"


def _cdp_port() -> int:
    """Resolved at CALL time (Rule 1) so an operator can retarget live."""
    import os

    raw = os.getenv("UPWORK_CDP_PORT") or os.getenv("HOMIE_CDP_PORT") or ""
    try:
        return int(raw) if raw.strip() else _DEFAULT_CDP_PORT
    except ValueError:
        return _DEFAULT_CDP_PORT


# ---------------------------------------------------------------------------
# X / Twitter
# ---------------------------------------------------------------------------


def _x_search(query: str = "", limit: int = 10, **_: Any) -> str:
    """Search X through the desk's own collector, budget and all."""
    if not query.strip():
        return "error: query is required"

    try:
        from x_networking import collector
    except ImportError:
        return "error: the X networking desk is not installed in this deployment"

    try:
        tweets, err = collector.collect_one(query.strip(), port=_cdp_port())
    except Exception as exc:  # noqa: BLE001
        _logger.warning("x_search failed for %r", query, exc_info=True)
        return f"error: X search failed: {type(exc).__name__}: {exc}"

    if err and collector.is_rate_blocked_error(err):
        # NOT retried and NOT worked around. The rate guard is ban safety for a
        # real account; a persona must be told it is out of budget, not handed
        # a second path to the same door.
        return (
            "X read budget is spent for this window (ban safety). "
            "Nothing was read. Try again after the next window."
        )
    if err and collector.is_incomplete_error(err):
        return (
            f"X search for {query!r} did not complete: {collector.incomplete_reason(err)}. "
            "Treat this as NO data rather than a partial answer."
        )

    if not tweets:
        return f"No X results for {query!r}."

    capped = tweets[: max(1, min(25, int(limit or 10)))]
    lines = [f"{len(capped)} of {len(tweets)} X result(s) for {query!r}:"]
    for t in capped:
        # The collector emits a COMPACT shape from its page script — verified
        # live 2026-07-27, not guessed:
        #   u  author line ("SirFred @SirFrd · 25s")
        #   t  post text
        #   ts ISO timestamp
        #   p  permalink path ("/SirFrd/status/…")
        # Long names were an assumption that rendered every post as "@?" with
        # empty text while the collector was returning real data — the failure
        # looked like a dead tool and was a dead FORMATTER.
        author = " ".join(str(t.get("u") or "?").split())
        text = " ".join(str(t.get("t") or "").split())
        stamp = str(t.get("ts") or "")
        path = str(t.get("p") or "")
        link = f"https://x.com{path}" if path.startswith("/") else path
        lines.append(f"\n{author}  [{stamp}]\n{text[:400]}\n{link}")
    return _truncate("\n".join(lines))


# ---------------------------------------------------------------------------
# Browser reads (visible CDP session)
# ---------------------------------------------------------------------------


def _browser_status(**_: Any) -> str:
    """Is the visible browser reachable right now?"""
    try:
        import browser_control

        return json.dumps(browser_control.browser_status(port=_cdp_port()), default=str)[:1500]
    except Exception as exc:  # noqa: BLE001
        return f"error: browser status unavailable: {type(exc).__name__}: {exc}"


def _browser_tabs(**_: Any) -> str:
    """List open tabs. URLs arrive redacted by the browser layer."""
    try:
        import browser_control

        payload = browser_control.list_cdp_tabs(_cdp_port())
        tabs = payload.get("tabs") or payload.get("targets") or []
        rows = [
            f"{str(t.get('title') or '?')[:90]}  —  {t.get('url') or '?'}"
            for t in tabs
            if isinstance(t, dict)
        ]
        return _truncate("\n".join(rows) or "No open tabs.")
    except Exception as exc:  # noqa: BLE001
        return f"error: cannot list tabs: {type(exc).__name__}: {exc}"


def _browser_snapshot(**_: Any) -> str:
    """Read the active tab as interactive text.

    This is how a persona reads a page it is already looking at — a Discord
    channel, an X thread, a chart. Read-only: a snapshot never clicks.
    """
    try:
        import browser_control

        # `snapshot -i -c` yields INTERACTIVE elements ({ref, role, name}) —
        # click targets, not prose. Reading a Discord channel or an X thread
        # needs the accessibility TEXT, so take the unfiltered snapshot.
        result = browser_control.run_agent_browser(["snapshot"], port=_cdp_port(), timeout=30)
        if not result.ok:
            return f"error: snapshot failed: {str(result.output or '')[:300]}"
        body = str(result.stdout or "").strip()
        return _truncate(body or "Snapshot returned no text (blank or still loading).")
    except Exception as exc:  # noqa: BLE001
        return f"error: snapshot failed: {type(exc).__name__}: {exc}"


_SPECS: tuple[tuple[str, str, str, dict[str, Any], Any], ...] = (
    (
        "x_search",
        # RESEARCH, not crypto. Operator direction: "marketing — Firecrawl, X,
        # web search". X reading is shared substrate, and `research_read` is
        # the narrow capability class that persona domain packs compose onto.
        #
        # Caught by the ownership check, not by review: the old `research`
        # toolset LISTED x_search while it was registered under `crypto`, so
        # every marketing persona was silently refused it. The guard logged the
        # refusal by name.
        "research_read",
        "Search X/Twitter LIVE through the operator's logged-in browser session and "
        "return matching posts with author, engagement, and permalink. Spends the "
        "shared ban-safety read budget — use it deliberately, not speculatively.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "X search query (supports X search operators).",
                },
                "limit": {"type": "integer", "description": "Max posts to return (1-25)."},
            },
            "required": ["query"],
        },
        _x_search,
    ),
    (
        "browser_status",
        "browser_read",
        "Check whether the operator's visible browser session is reachable.",
        {"type": "object", "properties": {}},
        _browser_status,
    ),
    (
        "browser_tabs",
        "browser_read",
        "List the open tabs in the operator's visible browser.",
        {"type": "object", "properties": {}},
        _browser_tabs,
    ),
    (
        "browser_snapshot",
        "browser_read",
        "Read the currently active browser tab as text — use it to actually READ a "
        "Discord channel, an X thread, or a chart page the operator has open.",
        {"type": "object", "properties": {}},
        _browser_snapshot,
    ),
)


def register_tools() -> int:
    """Register the read-only eyes. Never raises; returns the count."""
    from runtime import tool_registry

    registered = 0
    for name, toolset, description, parameters, handler in _SPECS:
        try:
            tool_registry.register_tool(
                name,
                description,
                toolset=toolset,
                parameters=parameters,
                handler=handler,
                effect="read",
                elevatable=True,
            )
            registered += 1
        except Exception:  # noqa: BLE001
            _logger.warning("failed to register eye tool %r", name, exc_info=True)
    return registered


__all__ = ["register_tools"]
