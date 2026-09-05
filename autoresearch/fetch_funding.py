#!/usr/bin/env python3
"""Pull funding-rate history from Hyperliquid public API into data/funding/<COIN>.csv.

Funding harvest is the highest-Sharpe edge on HL (hourly settlement, no directional risk).
This data lets strategies make positioning decisions based on funding direction.
"""

import argparse
import csv
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request, error
import json

API = "https://api.hyperliquid.xyz/info"
DATA_DIR = Path(__file__).parent / "data" / "funding"
DATA_DIR.mkdir(parents=True, exist_ok=True)
COINS = ["BTC", "ETH", "SOL", "HYPE", "XRP", "SUI", "DOGE", "AVAX"]


def post(body: dict) -> any:
    req = request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def fetch_funding(coin: str, start_ms: int, end_ms: int) -> list:
    """HL caps each call to 500 records; page until we cover the range."""
    out = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            chunk = post({"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms})
        except error.HTTPError as e:
            print(f"  http {e.code} for {coin} at {cursor}")
            break
        if not chunk:
            break
        out.extend(chunk)
        last_ts = chunk[-1]["time"]
        if last_ts <= cursor:
            break
        cursor = last_ts + 1
        time.sleep(0.15)
        if len(chunk) < 500:
            break
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--coins", nargs="*", default=COINS)
    args = p.parse_args()

    end = int(time.time() * 1000)
    start = end - args.days * 24 * 60 * 60 * 1000

    for coin in args.coins:
        print(f"fetching {coin} funding ({args.days}d)...")
        rows = fetch_funding(coin, start, end)
        if not rows:
            print(f"  no data")
            continue
        path = DATA_DIR / f"{coin}.csv"
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["datetime", "funding_rate", "premium"])
            for r in rows:
                ts = datetime.fromtimestamp(r["time"] / 1000, tz=timezone.utc).isoformat()
                w.writerow([ts, r["fundingRate"], r["premium"]])
        rates = [float(r["fundingRate"]) for r in rows]
        avg = sum(rates) / len(rates) if rates else 0
        annualized = avg * 24 * 365 * 100
        print(f"  {len(rows)} rows -> {path.name}  avg {avg:+.6f}/hr  ~{annualized:+.1f}% APY")


if __name__ == "__main__":
    main()
