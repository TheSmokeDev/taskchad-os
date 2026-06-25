#!/usr/bin/env python3
"""Keyless crypto spot prices via CoinGecko for the crypto-watch skill.

Usage:
    prices.py bitcoin ethereum --vs usd [--json]

Coin IDs are CoinGecko slugs (e.g. "bitcoin", "ethereum", "solana").
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

API = "https://api.coingecko.com/api/v3/simple/price"


def fetch(coins: list[str], vs: str) -> dict:
    params = urllib.parse.urlencode(
        {"ids": ",".join(coins), "vs_currencies": vs,
         "include_24hr_change": "true"}
    )
    req = urllib.request.Request(f"{API}?{params}", headers={"User-Agent": "taskchad-crypto-watch"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 — CLI surface, report and exit
        sys.exit(f"CoinGecko request failed: {exc}")


def main() -> None:
    p = argparse.ArgumentParser(description="Crypto spot prices (CoinGecko).")
    p.add_argument("coins", nargs="+", help="CoinGecko slugs, e.g. bitcoin")
    p.add_argument("--vs", default="usd", help="Quote currency (default usd).")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    data = fetch([c.lower() for c in args.coins], args.vs.lower())

    if args.json:
        print(json.dumps(data, indent=2))
        return

    if not data:
        sys.exit("No prices returned — check the coin slugs.")

    for coin, quote in data.items():
        price = quote.get(args.vs.lower())
        change = quote.get(f"{args.vs.lower()}_24h_change")
        arrow = "▲" if (change or 0) >= 0 else "▼"
        chg = f"{arrow} {change:+.2f}%" if change is not None else ""
        print(f"{coin:<12} {price:>14,.2f} {args.vs.upper()}  {chg}")


if __name__ == "__main__":
    main()
