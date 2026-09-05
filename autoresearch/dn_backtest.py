#!/usr/bin/env python3
"""Backtest the live_delta_neutral gate on real funding history.

Replays the exact entry/exit rules over hourly funding prints for every
hedgeable spot+perp asset and reports what the harvester would have earned
per $100 of capital. No price data needed: the position is delta-neutral, so
P&L = funding collected - fee drag (4 taker legs per round trip).

Usage: python3 dn_backtest.py [--days 90] [--sweep]
"""

import argparse
import json
import time
from urllib import request

API = "https://api.hyperliquid.xyz/info"
FEE_ROUND_TRIP = 4 * 0.00045  # spot in/out + perp in/out, taker


def post(body: dict) -> any:
    req = request.Request(API, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def hedgeable() -> list[str]:
    meta = post({"type": "spotMeta"})
    tokens = {t["index"]: t for t in meta["tokens"]}
    spot = {tokens[u["tokens"][0]]["name"] for u in meta["universe"]
            if tokens[u["tokens"][1]]["name"] == "USDC"}
    perp = {m["name"] for m in post({"type": "meta"})["universe"]}
    return sorted(spot & perp - {"USDC"})


def funding_series(coin: str, days: int) -> list[float]:
    """hourly funding rates, oldest first, paginated past the API chunk cap."""
    end = int(time.time() * 1000)
    cursor = end - days * 86_400_000
    out = []
    while cursor < end:
        rows = post({"type": "fundingHistory", "coin": coin,
                     "startTime": cursor, "endTime": end})
        if not rows:
            break
        out.extend(float(r["fundingRate"]) for r in rows)
        last = rows[-1]["time"]
        if last + 1 <= cursor:
            break
        cursor = last + 1
        if len(rows) < 2:
            break
        time.sleep(0.2)
    return out


def net_apr(rates: list[float]) -> float:
    if not rates:
        return 0.0
    return (sum(rates) / len(rates)) * 8760 - FEE_ROUND_TRIP * (365 / 7)


def simulate(rates: list[float], min_apr: float, exit_apr: float,
             gate_hours: int = 24, exit_hours: int = 6) -> dict:
    """Walk the series hour by hour with the live gate/exit rules."""
    in_pos = False
    trades = 0
    hours_in = 0
    funding_sum = 0.0
    for i in range(gate_hours, len(rates)):
        window = rates[i - gate_hours:i]
        if not in_pos:
            if (min(window) > 0 and net_apr(window) >= min_apr
                    and net_apr(window[-3:]) >= min_apr):
                in_pos = True
                trades += 1
        else:
            funding_sum += rates[i]
            hours_in += 1
            if net_apr(rates[max(0, i - exit_hours):i]) < exit_apr:
                in_pos = False
    net = funding_sum - trades * FEE_ROUND_TRIP
    return {"trades": trades, "hours_in": hours_in,
            "funding_pct": funding_sum * 100, "net_pct": net * 100}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--sweep", action="store_true", help="sweep gate thresholds")
    args = p.parse_args()

    coins = hedgeable()
    print(f"assets: {coins}")
    series = {}
    for c in coins:
        series[c] = funding_series(c, args.days)
        print(f"  {c}: {len(series[c])}h of funding history")

    grids = [(0.05, 0.01), (0.10, 0.02), (0.15, 0.03)] if args.sweep else [(0.10, 0.02)]
    for min_apr, exit_apr in grids:
        print(f"\n=== gate {min_apr:.0%} net APR / exit {exit_apr:.0%} ===")
        total_net = 0.0
        for c, rates in series.items():
            r = simulate(rates, min_apr, exit_apr)
            total_net += r["net_pct"]
            if r["trades"]:
                print(f"  {c:8s} trades={r['trades']:3d}  hours_in={r['hours_in']:5d}"
                      f"  funding={r['funding_pct']:+.3f}%  net={r['net_pct']:+.3f}%")
        days = args.days
        print(f"  TOTAL net per $100 single-asset-at-a-time capital over {days}d:"
              f" {total_net:+.3f}%  (~{total_net * 365 / days:+.1f}% APY if one asset held at a time)")


if __name__ == "__main__":
    main()
