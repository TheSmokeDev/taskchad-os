"""The crypto desk's own tools — market state a persona can read on demand.

Answers the operator's "does he need a TradingView API?" with **no**. Every
endpoint behind these tools is public and keyless — `crypto_candles.py` states
it directly: *"No API key, no secret, no account: every endpoint this module
touches is [public]"*. OHLCV comes from OKX/Kraken/Coinbase public endpoints,
Polymarket and DexScreener are open. Nothing here needs an account, a
subscription, or a browser.

That matters beyond cost: an API read is faster than driving a browser, does
not spend the X ban-safety budget, and cannot be broken by a page redesign.
`agent-browser` stays for what only a logged-in session can see (X, Discord);
market data has no reason to go through it.

Read-only. The paper ladder and play ledger are exposed as READS only —
`crypto_plays_read`, `crypto_paper_read`. Nothing here opens, sizes, or closes
a position. Those keep their own gates, and this file must never grow one.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 6000


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[TRUNCATED — {len(text) - limit} more chars]"


def _crypto_candles(
    symbol: str = "BTC/USDT", timeframe: str = "1h", limit: int = 40, **_: Any
) -> str:
    """Recent OHLCV for one symbol.

    BLOCKING by design in `fetch_ohlcv` — safe here because a tool handler runs
    inside the runtime's bounded dispatch, not on the bot event loop. (The
    framework rule is that the BOT never blocks its loop; a tool call already
    rides the runtime's own deadline.)
    """
    try:
        from cognition import crypto_candles
    except ImportError:
        return "error: the crypto candles module is not available in this deployment"

    try:
        candles = crypto_candles.fetch_ohlcv(symbol.strip() or None, timeframe.strip() or None)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("crypto_candles failed for %r", symbol, exc_info=True)
        return f"error: candle fetch failed: {type(exc).__name__}: {exc}"

    # CandleSet is a two-shape contract, verified against the dataclass rather
    # than guessed: OK implies a non-empty `frame` and no reason; UNAVAILABLE
    # implies frame=None, rows=0 and a reason. `rows` is an int COUNT, not the
    # data — reading it as the payload is what produced "unexpected candle
    # payload: int". The split exists so a caller cannot mistake "we could not
    # look" for "we looked and the market was flat", and passing that
    # distinction to the model is the whole point.
    status = str(getattr(candles, "status", "")).upper()
    if "UNAVAILABLE" in status or getattr(candles, "frame", None) is None:
        reason = getattr(candles, "reason", "") or "no reason given"
        return (
            f"Could NOT read {symbol} {timeframe}: {reason}. "
            "This is 'we could not look', NOT 'the market was flat'."
        )

    frame = candles.frame
    tail_n = max(1, min(200, int(limit or 40)))
    try:
        rendered = str(frame.tail(tail_n))
    except AttributeError:
        rendered = str(frame)

    header = f"{symbol} {timeframe} — {getattr(candles, 'rows', '?')} candles"
    if getattr(candles, "closed_only", False):
        header += ", closed candles only"
    if getattr(candles, "complete", True) is False:
        # Load-bearing: an incomplete frame is a PARTIAL answer, never proof
        # that history ends where the frame ends. A persona sizing a position
        # off a truncated window would be reading a bound as a fact.
        header += (
            "\nWARNING: INCOMPLETE — a bound (budget/page cap) tripped before the "
            "requested window was covered. Do NOT treat the earliest candle as "
            "the start of history."
        )
    return _truncate(f"{header}\n{rendered}")


def _crypto_desk_snapshot(persona_id: str = "crypto", **_: Any) -> str:
    """The whole desk in one read: ledger state plus the live price block."""
    try:
        from cognition import crypto_desk_snapshot
    except ImportError:
        return "error: the crypto desk module is not available in this deployment"
    try:
        return _truncate(crypto_desk_snapshot.build_crypto_desk_snapshot(persona_id or "crypto"))
    except Exception as exc:  # noqa: BLE001
        _logger.warning("crypto_desk_snapshot failed", exc_info=True)
        return f"error: desk snapshot failed: {type(exc).__name__}: {exc}"


def _crypto_polymarket(query: str = "", limit: int = 8, **_: Any) -> str:
    """Prediction-market odds for a topic."""
    if not query.strip():
        return "error: query is required"
    try:
        from cognition import crypto_polymarket
    except ImportError:
        return "error: the polymarket module is not available in this deployment"
    try:
        normalized = query.strip().casefold()
        if normalized in {"crypto", "crypto board", "crypto markets"}:
            feed = crypto_polymarket.fetch_crypto_markets(limit=max(1, min(25, int(limit or 8))))
        else:
            feed = crypto_polymarket.search_markets(
                query.strip(), limit=max(1, min(25, int(limit or 8)))
            )
    except Exception as exc:  # noqa: BLE001
        return f"error: polymarket lookup failed: {type(exc).__name__}: {exc}"

    quotes = getattr(feed, "quotes", None) or getattr(feed, "markets", None) or []
    if not quotes:
        return f"No Polymarket markets for {query!r}."
    return _truncate(
        f"{len(quotes)} market(s) for {query!r}:\n" + "\n".join(str(q) for q in quotes)
    )


def _crypto_dexscreener(query: str = "", **_: Any) -> str:
    """Liquidity/price receipt for a token.

    A TICKER is not a token identity — anyone can deploy a second token wearing
    the same symbol — so the module stamps ticker matches as degraded. That
    caveat is passed through to the model verbatim rather than smoothed away:
    a persona sizing a position off the wrong token is the failure this warning
    exists to prevent.
    """
    if not query.strip():
        return "error: query is required (ticker or contract address)"
    try:
        from cognition import crypto_plays_dexscreener as dex
    except ImportError:
        return "error: the dexscreener module is not available in this deployment"
    try:
        looks_like_address = query.strip().startswith("0x") or len(query.strip()) > 30
        receipt = (
            dex.fetch_token_receipt_by_address(query.strip())
            if looks_like_address
            else dex.fetch_token_receipt(query.strip())
        )
    except Exception as exc:  # noqa: BLE001
        return f"error: dexscreener lookup failed: {type(exc).__name__}: {exc}"

    if receipt is None:
        return f"No DexScreener pair found for {query!r}."
    note = ""
    if getattr(receipt, "match", "") == "ticker":
        note = (
            "\nWARNING: matched by TICKER, not address. This is the deepest pool "
            "wearing that symbol and may not be the intended token."
        )
    return _truncate(f"{receipt}{note}")


_SPECS: tuple[tuple[str, str, str, dict[str, Any], Any], ...] = (
    (
        "crypto_candles",
        "crypto",
        "Fetch recent OHLCV candles for a symbol from public exchange data "
        "(no account needed). Use for actual price/structure, never a guess.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "e.g. BTC/USDT"},
                "timeframe": {"type": "string", "description": "e.g. 15m, 1h, 4h, 1d"},
                "limit": {"type": "integer", "description": "How many recent candles (1-200)."},
            },
        },
        _crypto_candles,
    ),
    (
        "crypto_desk_snapshot",
        "crypto",
        "Read the crypto desk in one call: play ledger state plus the live price "
        "block. Start here before answering anything about current positioning.",
        {
            "type": "object",
            "properties": {"persona_id": {"type": "string"}},
        },
        _crypto_desk_snapshot,
    ),
    (
        "crypto_polymarket",
        "crypto",
        "Search Polymarket for prediction-market odds on a topic — what the "
        "market actually prices, not what commentators say.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        _crypto_polymarket,
    ),
    (
        "crypto_dexscreener",
        "crypto",
        "Look up on-chain liquidity/price for a token by contract address "
        "(preferred) or ticker. Address matches are authoritative; ticker "
        "matches are flagged as degraded.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Contract address or ticker."}
            },
            "required": ["query"],
        },
        _crypto_dexscreener,
    ),
)


def register_tools() -> int:
    """Register the crypto read tools. Never raises; returns the count."""
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
            _logger.warning("failed to register crypto tool %r", name, exc_info=True)
    return registered


__all__ = ["register_tools"]
