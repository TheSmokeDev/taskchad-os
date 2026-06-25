---
name: crypto-watch
description: Track cryptocurrency prices and fire alerts when a coin crosses a threshold. Use when the user asks for a crypto price, wants to watch a coin, set a price alert, or schedule periodic market check-ins. Uses the keyless CoinGecko API.
version: 1.0.0
author: TaskChad OS
license: MIT
platforms: [linux, macos, windows]
metadata:
  taskchad:
    category: finance
    tags: [crypto, prices, alerts, coingecko, watchlist]
    related_skills: [daily-brief]
    mutates: true
    capability_gate: chat.send
---

# Crypto Watch

Quote crypto prices and run threshold alerts. Read-only market data; the only
mutation is sending an alert message.

## Spot price

CoinGecko's simple-price endpoint is keyless:

```bash
uv run python optional-skills/finance/crypto-watch/scripts/prices.py \
  bitcoin ethereum solana --vs usd
```

Coin IDs are CoinGecko slugs (`bitcoin`, not `BTC`). Resolve tickers to slugs via
the `/coins/list` endpoint if the user gives a symbol.

## Watchlist + alerts

Store watches in `vault/finance/crypto-watchlist.json`:

```json
[
  {"coin": "bitcoin", "vs": "usd", "above": 80000, "below": null, "armed": true}
]
```

On each scheduled tick:

1. Fetch current prices for all armed watches in one batched call.
2. For each, compare against `above` / `below`.
3. On a crossing, send one alert and set `armed: false` (re-arm manually) so it
   doesn't spam every tick.

## Delivery (gated)

Alerts are outbound messages — send them through the `chat.send` capability gate
with an audit trail. Register the tick as a scheduled job (see
`.claude/sections/03_*`); do not busy-poll.

## Disclaimer

Surface a brief "not financial advice" note the first time a session gives
prices or sets an alert.
