#!/usr/bin/env python3
"""Pull HIP-3 perp data (oil, silver, stocks) from HL public API. Uses dex=xyz."""

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error

API = "https://api.hyperliquid.xyz/info"
DATA_DIR = Path(__file__).parent / "data"
HIP3_DIR = DATA_DIR / "hip3"
FUNDING_DIR = DATA_DIR / "funding"
HIP3_DIR.mkdir(parents=True, exist_ok=True)
FUNDING_DIR.mkdir(parents=True, exist_ok=True)

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
DEFAULT_TARGETS = ["xyz:CL", "xyz:SILVER", "xyz:GOLD", "xyz:NVDA", "xyz:TSLA", "xyz:AAPL", "xyz:MU"]


def post(body: dict) -> any:
    req = request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list:
    out = []
    cursor = start_ms
    step_ms = INTERVAL_MS[interval] * 2000
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
    seen, dedup = set(), []
    for c in out:
        if c["t"] not in seen:
            seen.add(c["t"]); dedup.append(c)
    dedup.sort(key=lambda c: c["t"])
    return dedup


def fetch_funding(coin: str, start_ms: int) -> list:
    out, cursor = [], start_ms
    end = int(time.time() * 1000)
    while cursor < end:
        try:
            chunk = post({"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end})
        except error.HTTPError as e:
            print(f"  funding http {e.code} for {coin}")
            break
        if not chunk:
            break
        out.extend(chunk)
        last_ts = chunk[-1]["time"]
        if last_ts <= cursor or len(chunk) < 500:
            break
        cursor = last_ts + 1
        time.sleep(0.15)
    return out


def slug(coin: str) -> str:
    return coin.replace(":", "_").replace("/", "_")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--interval", default="1h", choices=list(INTERVAL_MS.keys()))
    p.add_argument("--coins", nargs="*", default=DEFAULT_TARGETS)
    p.add_argument("--split", action="store_true", help="write _train (older 70pct) + _test (newer 30pct)")
    args = p.parse_args()

    end = int(time.time() * 1000)
    start = end - args.days * 86_400_000

    for coin in args.coins:
        s = slug(coin)
        print(f"fetching {coin} {args.days}d @ {args.interval}...")
        candles = fetch_candles(coin, args.interval, start, end)
        if not candles:
            print(f"  no candle data")
            continue
        if args.split:
            cut = int(len(candles) * 0.7)
            for label, batch in [("train", candles[:cut]), ("test", candles[cut:])]:
                path = HIP3_DIR / f"{s}_{label}.csv"
                with path.open("w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["datetime", "open", "high", "low", "close", "volume"])
                    for c in batch:
                        ts = datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).isoformat()
                        w.writerow([ts, c["o"], c["h"], c["l"], c["c"], c["v"]])
                print(f"  -> {path.name} ({len(batch)})")
        else:
            path = HIP3_DIR / f"{s}.csv"
            with path.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["datetime", "open", "high", "low", "close", "volume"])
                for c in candles:
                    ts = datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).isoformat()
                    w.writerow([ts, c["o"], c["h"], c["l"], c["c"], c["v"]])
            print(f"  -> {path.name} ({len(candles)})")

        print(f"  funding for {coin}...")
        rows = fetch_funding(coin, start)
        if rows:
            fpath = FUNDING_DIR / f"{s}.csv"
            with fpath.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["datetime", "funding_rate", "premium"])
                for r in rows:
                    ts = datetime.fromtimestamp(r["time"] / 1000, tz=timezone.utc).isoformat()
                    w.writerow([ts, r["fundingRate"], r["premium"]])
            avg = sum(float(r["fundingRate"]) for r in rows) / len(rows)
            print(f"    {len(rows)} rows -> {fpath.name}  avg funding {avg:+.6f}/hr ({avg*8760*100:+.1f}% APY)")
        else:
            print(f"    no funding data")


if __name__ == "__main__":
    main()
