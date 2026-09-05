#!/usr/bin/env python3
"""Pull historical OHLCV candles from HL public API. Up to 5000 candles per call."""

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error

API = "https://api.hyperliquid.xyz/info"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
COINS = ["BTC", "ETH", "SOL", "HYPE", "XRP", "SUI", "DOGE", "AVAX"]
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def post(body: dict) -> any:
    req = request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_range(coin: str, interval: str, start_ms: int, end_ms: int) -> list:
    out = []
    cursor = start_ms
    # use small windows so each call comfortably stays under the 5000-candle cap
    candles_per_window = 2000
    step_ms = INTERVAL_MS[interval] * candles_per_window
    while cursor < end_ms:
        chunk_end = min(cursor + step_ms, end_ms)
        try:
            chunk = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                                            "startTime": cursor, "endTime": chunk_end}})
        except error.HTTPError as e:
            print(f"  http {e.code} for {coin}@{cursor}")
            break
        if chunk:
            out.extend(chunk)
            last_t = chunk[-1]["t"]
            cursor = max(last_t + INTERVAL_MS[interval], chunk_end + 1)
        else:
            cursor = chunk_end + 1
        time.sleep(0.15)
    # dedupe by timestamp
    seen, dedup = set(), []
    for c in out:
        if c["t"] not in seen:
            seen.add(c["t"])
            dedup.append(c)
    dedup.sort(key=lambda c: c["t"])
    return dedup


def write_csv(coin: str, interval: str, candles: list, suffix: str = ""):
    name = f"{coin}{('_' + suffix) if suffix else ''}.csv"
    path = DATA_DIR / name
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for c in candles:
            ts = datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).isoformat()
            w.writerow([ts, c["o"], c["h"], c["l"], c["c"], c["v"]])
    return path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--interval", default="15m", choices=list(INTERVAL_MS.keys()))
    p.add_argument("--coins", nargs="*", default=COINS)
    p.add_argument("--split", action="store_true", help="write _train (older 70pct) + _test (newer 30pct)")
    args = p.parse_args()

    end = int(time.time() * 1000)
    start = end - args.days * 86_400_000

    for coin in args.coins:
        print(f"fetching {coin} {args.days}d @ {args.interval}...")
        candles = fetch_range(coin, args.interval, start, end)
        if not candles:
            print(f"  no data")
            continue
        if args.split:
            cut = int(len(candles) * 0.7)
            train_path = write_csv(coin, args.interval, candles[:cut], "train")
            test_path = write_csv(coin, args.interval, candles[cut:], "test")
            print(f"  {len(candles)} candles -> {train_path.name} ({cut}) + {test_path.name} ({len(candles) - cut})")
        else:
            path = write_csv(coin, args.interval, candles)
            print(f"  {len(candles)} candles -> {path.name}")


if __name__ == "__main__":
    main()
