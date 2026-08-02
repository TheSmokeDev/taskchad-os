"""The crypto desk's analysis + ledger surface — the other 20 modules.

`tool_impl_crypto.py` wired 4 modules (candles, desk snapshot, Polymarket,
DexScreener). The slice actually contains 24, and the persona could not reach
the ones that make it a DESK rather than a chart viewer: it could not compute an
indicator, read a support level, check funding, size a position, or look at its
own open plays.

That was not a design decision. Those tools were DECLARED in the `crypto`
toolset (`crypto_indicators`, `crypto_levels`, `crypto_plays_read`,
`crypto_paper_read`) and never registered — so the toolset promised them, the
ownership check refused them, and the persona was told nothing. This module
closes that gap and adds the rest of the read surface.

DESIGN — every wrapper here obeys three rules learned the expensive way:

1. **Call the module the way its existing consumer calls it.**
   `crypto_desk_snapshot.py` is the working reference for this whole slice;
   these wrappers copy its call shapes verbatim rather than inventing new ones.
2. **Pass the two-shape contract through.** Nearly every module here answers
   OK-with-data or UNAVAILABLE-with-a-reason. A wrapper that flattens
   "I could not look" into an empty result teaches the persona that a broken
   feed is a flat market — the single most dangerous lie on a trading desk.
3. **Return the module's OWN rendering when it has one.** `crypto_indicators`
   ships `render_chart_context()`, which emits an addressed table plus an
   explicit "do NOT cite prices from your training data" header. Re-rendering
   that by hand would drop the grounding instruction.

Read-only. Writes (recording a play, grading, paper fills, execution) live in
`tool_impl_crypto_trade.py` behind their own guards.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 6000
_DEFAULT_PERSONA = "crypto"


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[TRUNCATED — {len(text) - limit} more chars]"


def _timeframes(raw: str, fallback: tuple[str, ...] = ("1h", "4h")) -> tuple[str, ...]:
    parts = tuple(t.strip() for t in str(raw or "").split(",") if t.strip())
    return parts or fallback


# ---------------------------------------------------------------------------
# Indicators — the chart read
# ---------------------------------------------------------------------------


def _crypto_indicators(timeframes: str = "1h,4h", max_chars: int = 2500, **_: Any) -> str:
    """Full indicator read across timeframes: RSI, MACD, ADX, ATR, Bollinger, EMA.

    Returns the module's OWN rendering. That output carries an explicit
    "these are the authoritative prices for this run, do NOT cite prices from
    your training data" header and an `as of <close>` stamp — the two things
    that stop a model answering from memory. Re-formatting it here would
    silently drop both.
    """
    try:
        from cognition import crypto_candles, crypto_indicators
    except ImportError as exc:
        return f"error: chart modules unavailable ({exc})"

    try:
        sets = {
            tf: crypto_candles.fetch_ohlcv(timeframe=tf)
            for tf in _timeframes(timeframes)
        }
        unavailable = [
            tf for tf, cs in sets.items() if not getattr(cs, "available", False)
        ]
        read = crypto_indicators.read_chart(sets)
        rendered = crypto_indicators.render_chart_context(
            read, max_chars=max(400, min(_MAX_RESULT_CHARS, int(max_chars or 2500)))
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("crypto_indicators failed", exc_info=True)
        return f"error: chart read failed: {type(exc).__name__}: {exc}"

    if unavailable:
        # Named, not silently dropped: a missing timeframe changes what the
        # remaining ones MEAN (a 4h read with no 1h is a different picture),
        # and the persona cannot notice an absence it was never told about.
        rendered += (
            f"\n\nWARNING: no usable candles for {', '.join(unavailable)} — "
            "those timeframes are missing from this read, not flat."
        )
    return _truncate(rendered)


# ---------------------------------------------------------------------------
# Levels — fib + CME gaps
# ---------------------------------------------------------------------------


def _crypto_levels(timeframe: str = "4h", **_: Any) -> str:
    """Fibonacci retracement levels and unfilled CME gaps for one timeframe."""
    try:
        from cognition import crypto_candles, crypto_levels
    except ImportError as exc:
        return f"error: level modules unavailable ({exc})"

    tf = (timeframe or "4h").strip() or "4h"
    try:
        candles = crypto_candles.fetch_ohlcv(timeframe=tf)
        if not getattr(candles, "available", False):
            reason = getattr(candles, "reason", None) or "no reason given"
            return (
                f"Could NOT read {tf} candles: {reason}. "
                "No swing was measured — this is 'could not look', not 'no levels'."
            )
        frame = candles.require_frame()
        lines: list[str] = []

        _state, levels = crypto_levels.fib_from_frame(frame, tf)
        lines.append(f"## Fib levels ({tf})")
        if not levels.available:
            why = levels.reason.value if levels.reason else "unknown"
            lines.append(f"- UNAVAILABLE ({why}): {levels.detail}")
        else:
            # PROVISIONAL means the swing's anchor is not confirmed yet, so the
            # levels can still move. Surfacing it keeps a persona from sizing
            # off a number that has not settled.
            anchor = "confirmed" if levels.anchor_confirmed else "PROVISIONAL"
            lines.append(
                f"- Swing {levels.direction.value} {levels.low:,.2f} -> "
                f"{levels.high:,.2f} ({anchor} anchor)"
            )
            lines.extend(f"- {lv.label}: {lv.price:,.2f}" for lv in levels.levels)

        report = crypto_levels.find_cme_gaps(frame, tf)
        lines.append(f"\n## CME gaps ({tf})")
        if not report.available:
            why = report.reason.value if report.reason else "unknown"
            lines.append(f"- UNAVAILABLE ({why}): {report.detail}")
        else:
            lines.append(f"- {report.sessions_checked} session boundary/ies priced.")
            for gap in getattr(report, "gaps", []) or []:
                lines.append(f"- {gap}")
            if not (getattr(report, "gaps", []) or []):
                lines.append("- No unfilled gaps.")
        return _truncate("\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        _logger.warning("crypto_levels failed", exc_info=True)
        return f"error: level read failed: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Funding — the carry
# ---------------------------------------------------------------------------


def _crypto_funding(**_: Any) -> str:
    """Perp funding regime: who is paying to hold the crowded side."""
    try:
        from cognition import crypto_desk_upgrades
    except ImportError as exc:
        return f"error: funding module unavailable ({exc})"
    try:
        return _truncate(crypto_desk_upgrades.read_funding_regime().render())
    except Exception as exc:  # noqa: BLE001
        return f"error: funding read failed: {type(exc).__name__}: {exc}"


def _crypto_bar_clock(timeframe: str = "1h", **_: Any) -> str:
    """Seconds until the current candle closes.

    A desk that acts mid-candle is acting on a number that can still change.
    """
    try:
        from cognition import crypto_desk_upgrades
    except ImportError as exc:
        return f"error: unavailable ({exc})"
    try:
        import time

        secs = crypto_desk_upgrades.seconds_until_bar_close(
            int(time.time() * 1000), timeframe or "1h"
        )
        return f"{secs:.0f}s until the {timeframe} candle closes ({secs/60:.1f} min)."
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Leverage — sizing and liquidation
# ---------------------------------------------------------------------------


def _crypto_position_size(
    account_equity: float = 0.0,
    risk_pct: float = 1.0,
    entry: float = 0.0,
    stop: float = 0.0,
    **_: Any,
) -> str:
    """Position size from account risk — the number that decides survival."""
    try:
        from cognition import crypto_leverage
    except ImportError as exc:
        return f"error: leverage module unavailable ({exc})"
    try:
        # Signature verified at runtime, not guessed: keyword-only
        # `entry` / `stop_loss` / `equity` / `risk`, where `risk` is a FRACTION
        # (0.01 = 1%). The tool takes a percent because that is how a desk
        # states it; the conversion happens here so the model cannot get the
        # magnitude wrong by a factor of 100.
        result = crypto_leverage.calculate_position_size(
            entry=float(entry),
            stop_loss=float(stop),
            equity=float(account_equity),
            risk=float(risk_pct) / 100.0,
        )
        return _truncate(str(result))
    except TypeError as exc:
        # Signature mismatch is a WIRING bug, not a market condition. Say so
        # rather than returning something that reads like a market answer.
        return f"error: position-size call signature mismatch ({exc})"
    except Exception as exc:  # noqa: BLE001
        return f"error: sizing failed: {type(exc).__name__}: {exc}"


def _unified_symbol(raw: str) -> str | None:
    """Coerce a spot-style symbol to the ccxt-unified PERP form the tier table uses.

    The maintenance-tier table is keyed `'BTC/USDT:USDT'`; `'BTC/USDT'` misses
    it entirely and returns SYMBOL_UNKNOWN, which silently degrades to a
    default tier and a WRONG liquidation price. A wrong liquidation price is
    worse than none — it is the number a trader sizes their survival against.
    """
    sym = (raw or "").strip().upper()
    if not sym:
        return None
    if ":" in sym:
        return sym
    if "/" in sym:
        return f"{sym}:{sym.split('/', 1)[1]}"
    return sym


def _crypto_liquidation(
    entry: float = 0.0,
    leverage: float = 1.0,
    side: str = "long",
    notional: float = 0.0,
    symbol: str = "BTC/USDT",
    **_: Any,
) -> str:
    """Estimated liquidation price and distance for a leveraged position."""
    try:
        from cognition import crypto_leverage
    except ImportError as exc:
        return f"error: leverage module unavailable ({exc})"
    try:
        est = crypto_leverage.estimate_liquidation(
            _unified_symbol(symbol),
            entry=float(entry),
            leverage=float(leverage),
            side=(side or "long").strip().lower(),
            notional=float(notional) or None,
        )
        line = getattr(crypto_leverage, "format_liquidation_line", None)
        return _truncate(line(est) if callable(line) else str(est))
    except TypeError as exc:
        return f"error: liquidation call signature mismatch ({exc})"
    except Exception as exc:  # noqa: BLE001
        return f"error: liquidation estimate failed: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# The play ledger — read side
# ---------------------------------------------------------------------------


def _crypto_plays_read(persona_id: str = _DEFAULT_PERSONA, limit: int = 10, **_: Any) -> str:
    """Open plays and recent graded outcomes — the desk's own book.

    `list_open_checked` returns `(rows, ok)`. The `ok` flag is the two-shape
    contract again: an empty list with ok=False means the LEDGER COULD NOT BE
    READ, which is not the same as having no positions. Reporting the second
    when the first is true would tell a persona it is flat while it is exposed.
    """
    pid = (persona_id or _DEFAULT_PERSONA).strip() or _DEFAULT_PERSONA
    try:
        from cognition import crypto_plays
    except ImportError as exc:
        return f"error: play ledger unavailable ({exc})"

    out: list[str] = []
    try:
        open_rows, open_ok = crypto_plays.list_open_checked(pid)
        if not open_ok:
            out.append(
                "## Open plays\n- LEDGER UNREADABLE — this is NOT 'no open plays'. "
                "Treat exposure as UNKNOWN until it reads."
            )
        elif not open_rows:
            out.append("## Open plays\n- None open.")
        else:
            out.append("## Open plays")
            out.extend(f"- {row}" for row in list(open_rows)[: max(1, int(limit or 10))])
    except Exception as exc:  # noqa: BLE001
        out.append(f"## Open plays\n- error: {type(exc).__name__}: {exc}")

    try:
        graded, graded_ok = crypto_plays.list_recent_graded_checked(pid)
        if not graded_ok:
            out.append("\n## Recent grades\n- UNREADABLE (not 'none').")
        elif not graded:
            out.append("\n## Recent grades\n- None graded yet.")
        else:
            out.append("\n## Recent grades")
            out.extend(f"- {row}" for row in list(graded)[: max(1, int(limit or 10))])
    except Exception as exc:  # noqa: BLE001
        out.append(f"\n## Recent grades\n- error: {type(exc).__name__}: {exc}")

    try:
        out.append(f"\n## Track record\n{crypto_plays.split_track_record(pid)}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"\n## Track record\n- error: {type(exc).__name__}: {exc}")

    return _truncate("\n".join(out))


def _crypto_paper_read(
    persona_id: str = _DEFAULT_PERSONA,
    limit: int = 20,
    settle_status: str = "",
    **_: Any,
) -> str:
    """Paper-trading decisions: what the desk did on the simulator, and how it settled.

    The last of the four names that were DECLARED in the `crypto` toolset and
    never registered. `CheckedRows(ok, rows, reason)` carries the same
    could-not-read vs nothing-there distinction as the live ledger, and it is
    passed through for the same reason: a paper record that reads as empty when
    the DB is unreachable would corrupt the persona's own track record — the
    input its learning loop grades itself on.
    """
    pid = (persona_id or _DEFAULT_PERSONA).strip() or _DEFAULT_PERSONA
    try:
        from cognition import crypto_paper
    except ImportError as exc:
        return f"error: paper module unavailable ({exc})"
    try:
        checked = crypto_paper.list_decisions_checked(
            pid,
            limit=max(1, min(100, int(limit or 20))),
            settle_status=(settle_status or "").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001
        return f"error: paper read failed: {type(exc).__name__}: {exc}"

    if not getattr(checked, "ok", False):
        return (
            f"Paper ledger UNREADABLE ({getattr(checked, 'reason', '') or 'no reason'}). "
            "This is NOT 'no paper trades' — the record could not be read."
        )
    rows = list(getattr(checked, "rows", ()) or ())
    if not rows:
        return f"No paper decisions recorded for {pid!r}."
    return _truncate(
        f"{len(rows)} paper decision(s) for {pid!r}:\n"
        + "\n".join(f"- {r}" for r in rows)
    )


def _crypto_proof(returns: str = "", interval: str = "1d", **_: Any) -> str:
    """Sharpe, max drawdown and ruin check over a return series.

    The "is this edge real or is it noise" tool. A desk that reports a win rate
    without a drawdown number is reporting half a result — and `account_blown`
    is the half that ends the account.
    """
    raw = [p.strip() for p in str(returns or "").replace(",", " ").split() if p.strip()]
    if not raw:
        return "error: returns is required — a space or comma separated list, e.g. '0.01 -0.004 0.02'"
    try:
        series = [float(p) for p in raw]
    except ValueError as exc:
        return f"error: returns must all be numbers ({exc})"
    if len(series) < 2:
        return "error: at least 2 returns are needed to measure anything"

    try:
        from cognition import crypto_proof
    except ImportError as exc:
        return f"error: proof module unavailable ({exc})"
    try:
        m = crypto_proof.compute_metrics(series, (interval or "1d").strip())
    except Exception as exc:  # noqa: BLE001
        return f"error: metrics failed: {type(exc).__name__}: {exc}"

    if not getattr(m, "available", False):
        return f"UNAVAILABLE: {getattr(m, 'reason', None) or 'no reason given'}"

    lines = [
        f"{m.bars} bars @ {m.interval} ({m.calendar.value if hasattr(m.calendar,'value') else m.calendar})",
        f"- total return:      {m.total_return:+.2%}",
        f"- Sharpe (annual):   {m.sharpe_annualized:.2f}",
        f"- Sharpe (periodic): {m.sharpe_periodic:.3f}",
        f"- max drawdown:      {m.max_drawdown:.2%}",
    ]
    if getattr(m, "account_blown", False):
        # The one outcome no ratio survives. Never a footnote.
        lines.append(
            "\n*** ACCOUNT BLOWN — the series reaches total loss. Every ratio "
            "above is arithmetic on a dead account. ***"
        )
    if m.bars < 30:
        # A great Sharpe over 12 bars is a coin flip with good manners.
        lines.append(
            f"\nNOTE: {m.bars} bars is a SMALL sample. These numbers are not yet "
            "evidence of an edge."
        )
    return _truncate("\n".join(lines))


def _crypto_call_anchor(chain: str = "", address: str = "", **_: Any) -> str:
    """Price-stamp a token NOW, so a call can be graded honestly later.

    Without an anchor at call time, a play's outcome is graded against whatever
    price the grader happens to look up — which quietly rewards revisionism.
    `resolve_call_anchor` takes a duck-typed contract: any object with
    `.chain_id` and `.address`, so two strings are enough.
    """
    if not chain.strip() or not address.strip():
        return "error: chain and address are both required"
    try:
        import types

        from cognition import crypto_call_anchor
    except ImportError as exc:
        return f"error: anchor module unavailable ({exc})"
    try:
        anchor = crypto_call_anchor.resolve_call_anchor(
            types.SimpleNamespace(chain_id=chain.strip(), address=address.strip())
        )
    except Exception as exc:  # noqa: BLE001
        return f"error: anchor lookup failed: {type(exc).__name__}: {exc}"

    if anchor is None:
        # None is a DESIGNED outcome, not an error: unknown pair, unreachable
        # upstream, or a non-positive price. Saying "unanchored" is honest;
        # inventing a price would corrupt every future grade of this call.
        return (
            f"UNANCHORED — no usable price for {address.strip()} on {chain.strip()}. "
            "A call recorded now cannot be graded against an entry price."
        )
    return (
        f"anchored at ${anchor.price_usd:.10g} "
        f"(source: {anchor.source}, observed {anchor.observed_at})"
    )


def _crypto_looks_read(persona_id: str = _DEFAULT_PERSONA, limit: int = 10, **_: Any) -> str:
    """What the persona has already LOOKED at — past X/Discord reads and receipts.

    Read-only and lock-free. This does NOT go look; it reads receipts a prior
    look already wrote. Deliberately distinct from `x_search`/`browser_snapshot`,
    which spend real rate-limit budget and drive the visible browser.
    """
    pid = (persona_id or _DEFAULT_PERSONA).strip() or _DEFAULT_PERSONA
    try:
        from cognition import crypto_look_receipts
    except ImportError as exc:
        return f"error: look receipts unavailable ({exc})"
    try:
        looks, ok = crypto_look_receipts.list_recent_looks_checked(
            pid, limit=max(1, min(50, int(limit or 10)))
        )
    except Exception as exc:  # noqa: BLE001
        return f"error: look history failed: {type(exc).__name__}: {exc}"

    if not ok:
        return "Look history UNREADABLE — this is NOT 'has never looked'."
    if not looks:
        return f"No recorded looks for {pid!r} yet."
    return _truncate(
        f"{len(looks)} recent look(s) for {pid!r}:\n"
        + "\n".join(f"- {row}" for row in looks)
    )


def _crypto_hit_rate(persona_id: str = _DEFAULT_PERSONA, **_: Any) -> str:
    """The persona's own resolved hit rate — how often its calls were right.

    Voids are excluded from the denominator by the module, which is the honest
    treatment: a struck bet is not a loss and should not dilute a record in
    either direction.
    """
    pid = (persona_id or _DEFAULT_PERSONA).strip() or _DEFAULT_PERSONA
    try:
        from cognition import crypto_plays, crypto_reflection
    except ImportError as exc:
        return f"error: reflection module unavailable ({exc})"
    try:
        graded, ok = crypto_plays.list_recent_graded_checked(pid)
        if not ok:
            return "Ledger UNREADABLE — cannot compute a hit rate. NOT a zero."
        rate = crypto_reflection.hit_rate(list(graded or ()), ledger_ok=ok)
    except Exception as exc:  # noqa: BLE001
        return f"error: hit rate failed: {type(exc).__name__}: {exc}"

    if not getattr(rate, "available", False):
        return f"Hit rate unavailable: {getattr(rate, 'reason', None) or 'not enough resolved plays'}"
    return (
        f"hit rate {rate.rate:.1%} ({rate.hits}/{rate.resolved} resolved; "
        "voids excluded from the denominator)"
    )


def _crypto_safety_check(chain: str = "", address: str = "", **_: Any) -> str:
    """Token safety veto — honeypot, tax, LP lock, deployer reputation.

    The check that exists because a token can look liquid and still be a trap.
    """
    if not chain.strip() or not address.strip():
        return "error: chain and address are both required"
    try:
        from cognition import crypto_plays_safety
    except ImportError as exc:
        return f"error: safety module unavailable ({exc})"
    try:
        fn = (
            getattr(crypto_plays_safety, "evaluate_token_safety", None)
            or getattr(crypto_plays_safety, "check_token", None)
            or getattr(crypto_plays_safety, "safety_report", None)
        )
        if fn is None:
            return "error: no safety entrypoint found in crypto_plays_safety"
        return _truncate(str(fn(chain.strip(), address.strip())))
    except Exception as exc:  # noqa: BLE001
        return f"error: safety check failed: {type(exc).__name__}: {exc}"


_SPECS: tuple[tuple[str, str, str, dict[str, Any], Any], ...] = (
    (
        "crypto_indicators",
        "crypto",
        "Read live indicators across timeframes — RSI, MACD, ADX, ATR, Bollinger, EMA — "
        "computed from CLOSED candles only. These are the authoritative prices for the "
        "run; never answer with prices from training data.",
        {
            "type": "object",
            "properties": {
                "timeframes": {
                    "type": "string",
                    "description": "Comma-separated, e.g. '1h,4h' or '15m,1h,1d'.",
                },
                "max_chars": {"type": "integer", "description": "Cap on the rendered table."},
            },
        },
        _crypto_indicators,
    ),
    (
        "crypto_levels",
        "crypto",
        "Fibonacci retracement levels and unfilled CME gaps for a timeframe — the "
        "structural price levels to trade around.",
        {
            "type": "object",
            "properties": {
                "timeframe": {"type": "string", "description": "e.g. 4h, 1d."}
            },
        },
        _crypto_levels,
    ),
    (
        "crypto_funding",
        "crypto",
        "Perp funding regime — who is paying to hold the crowded side. Read it before "
        "sizing a leveraged position; funding is the carry cost.",
        {"type": "object", "properties": {}},
        _crypto_funding,
    ),
    (
        "crypto_bar_clock",
        "crypto",
        "Seconds until the current candle closes. Acting mid-candle means acting on a "
        "number that can still change.",
        {
            "type": "object",
            "properties": {"timeframe": {"type": "string"}},
        },
        _crypto_bar_clock,
    ),
    (
        "crypto_position_size",
        "crypto",
        "Compute position size from account equity, risk percent, entry and stop. The "
        "number that decides survival — size before entering, never after.",
        {
            "type": "object",
            "properties": {
                "account_equity": {"type": "number"},
                "risk_pct": {"type": "number", "description": "Percent of equity at risk."},
                "entry": {"type": "number"},
                "stop": {"type": "number"},
            },
            "required": ["account_equity", "entry", "stop"],
        },
        _crypto_position_size,
    ),
    (
        "crypto_liquidation",
        "crypto",
        "Estimated liquidation price and distance for a leveraged position.",
        {
            "type": "object",
            "properties": {
                "entry": {"type": "number"},
                "leverage": {"type": "number"},
                "side": {"type": "string", "enum": ["long", "short"]},
                "notional": {"type": "number"},
                "symbol": {
                    "type": "string",
                    "description": "e.g. BTC/USDT — coerced to the perp form the tier table uses.",
                },
            },
            "required": ["entry", "leverage"],
        },
        _crypto_liquidation,
    ),
    (
        "crypto_plays_read",
        "crypto",
        "Read the desk's own book: open plays, recent graded outcomes, and track record. "
        "Check this before proposing a new position — you may already be in it.",
        {
            "type": "object",
            "properties": {
                "persona_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        _crypto_plays_read,
    ),
    (
        "crypto_paper_read",
        "crypto",
        "Read paper-trading decisions and how they settled — the simulator record the "
        "desk grades itself on. Check it before claiming a track record.",
        {
            "type": "object",
            "properties": {
                "persona_id": {"type": "string"},
                "limit": {"type": "integer"},
                "settle_status": {
                    "type": "string",
                    "description": "Optional filter, e.g. 'settled' or 'open'.",
                },
            },
        },
        _crypto_paper_read,
    ),
    (
        "crypto_call_anchor",
        "crypto",
        "Stamp a token's price RIGHT NOW so a call can be graded honestly later. Run it "
        "when you make a call, not when you grade one — an unanchored call is ungradeable.",
        {
            "type": "object",
            "properties": {
                "chain": {"type": "string", "description": "e.g. solana, ethereum, base."},
                "address": {"type": "string", "description": "Token contract address."},
            },
            "required": ["chain", "address"],
        },
        _crypto_call_anchor,
    ),
    (
        "crypto_looks_read",
        "crypto",
        "Read what this persona has already looked at — past X/Discord reads and their "
        "receipts. Read-only; it does not spend browser budget or go look again.",
        {
            "type": "object",
            "properties": {
                "persona_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        _crypto_looks_read,
    ),
    (
        "crypto_hit_rate",
        "crypto",
        "This persona's own resolved hit rate — how often its graded calls were right. "
        "Check it before asserting a track record.",
        {
            "type": "object",
            "properties": {"persona_id": {"type": "string"}},
        },
        _crypto_hit_rate,
    ),
    (
        "crypto_proof",
        "crypto",
        "Measure a return series: Sharpe, max drawdown, and whether the account would "
        "have blown up. Use it before claiming an edge — a win rate without a drawdown "
        "number is half a result.",
        {
            "type": "object",
            "properties": {
                "returns": {
                    "type": "string",
                    "description": "Space or comma separated per-period returns as decimals, e.g. '0.01 -0.004 0.02'.",
                },
                "interval": {"type": "string", "description": "Bar interval, e.g. 1d, 4h."},
            },
            "required": ["returns"],
        },
        _crypto_proof,
    ),
    (
        "crypto_safety_check",
        "crypto",
        "Token safety veto for a contract address — honeypot, tax, LP lock, deployer "
        "reputation. Run it before any position in a token you did not already hold.",
        {
            "type": "object",
            "properties": {
                "chain": {"type": "string", "description": "e.g. ethereum, base, solana."},
                "address": {"type": "string", "description": "Contract address."},
            },
            "required": ["chain", "address"],
        },
        _crypto_safety_check,
    ),
)


def register_tools() -> int:
    """Register the desk analysis + ledger read tools. Never raises."""
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
            _logger.warning("failed to register desk tool %r", name, exc_info=True)
    return registered


__all__ = ["register_tools"]
