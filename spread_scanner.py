#!/usr/bin/env python3
"""
Hyperliquid Spread Scanner — finds pairs with spreads above your break-even.
Fetches all active perps, calculates real-time spread in bps, ranks by opportunity.
"""
import json
import sys
from hyperliquid.info import Info
from hyperliquid.utils import constants

MIN_SPREAD_BPS = 4.5  # your break-even
MIN_DAILY_VOLUME = 10_000  # skip dead pairs (USD)

def scan():
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    # Get all asset metadata
    meta = info.meta()
    universe = meta.get("universe", [])

    print(f"Scanning {len(universe)} pairs on Hyperliquid mainnet...\n")
    print(f"{'Pair':<20} {'Spread BPS':>10} {'Spread $':>10} {'Mid $':>12} {'BidSz':>8} {'AskSz':>8} {'szDec':>6} {'pxDec':>6}")
    print("-" * 90)

    opportunities = []

    for i, asset in enumerate(universe):
        coin = asset["name"]
        sz_decimals = asset.get("szDecimals", 2)

        try:
            book = info.l2_snapshot(coin)
            if not book or len(book.get("levels", [])) != 2:
                continue

            bids = book["levels"][0]
            asks = book["levels"][1]
            if not bids or not asks:
                continue

            best_bid = float(bids[0]["px"])
            best_ask = float(asks[0]["px"])
            bid_sz = float(bids[0]["sz"])
            ask_sz = float(asks[0]["sz"])

            if best_bid <= 0 or best_ask <= 0:
                continue

            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
            spread_bps = spread / mid * 10000

            # Determine price decimals from the actual price
            px_str = bids[0]["px"]
            if "." in px_str:
                px_decimals = len(px_str.split(".")[1])
            else:
                px_decimals = 0

            if spread_bps >= MIN_SPREAD_BPS:
                opportunities.append({
                    "coin": coin,
                    "spread_bps": round(spread_bps, 1),
                    "spread_usd": round(spread, 6),
                    "mid": mid,
                    "bid_sz": bid_sz,
                    "ask_sz": ask_sz,
                    "sz_decimals": sz_decimals,
                    "px_decimals": px_decimals,
                    "bid_depth_3": sum(float(b["sz"]) for b in bids[:3]),
                    "ask_depth_3": sum(float(a["sz"]) for a in asks[:3]),
                })

        except Exception as e:
            continue

    # Sort by spread (widest first)
    opportunities.sort(key=lambda x: x["spread_bps"], reverse=True)

    for o in opportunities:
        print(f"{o['coin']+'-PERP':<20} {o['spread_bps']:>10.1f} {o['spread_usd']:>10.6f} {o['mid']:>12.4f} {o['bid_sz']:>8.1f} {o['ask_sz']:>8.1f} {o['sz_decimals']:>6} {o['px_decimals']:>6}")

    print(f"\n{'='*90}")
    print(f"Found {len(opportunities)} pairs with spread >= {MIN_SPREAD_BPS} bps")

    if opportunities:
        print(f"\nTop 10 opportunities:")
        print(f"{'#':<4} {'Pair':<16} {'Spread':>8} {'Mid $':>12} {'Min Order $':>12} {'szDec':>6}")
        print("-" * 62)
        for i, o in enumerate(opportunities[:10]):
            min_order = round(10 ** (-o["sz_decimals"]) * o["mid"], 4)
            print(f"{i+1:<4} {o['coin']+'-PERP':<16} {o['spread_bps']:>7.1f}bp {o['mid']:>12.4f} {min_order:>12.4f} {o['sz_decimals']:>6}")

    # Save results
    with open("scan_results.json", "w") as f:
        json.dump(opportunities, f, indent=2)
    print(f"\nFull results saved to scan_results.json")

    return opportunities

if __name__ == "__main__":
    scan()
