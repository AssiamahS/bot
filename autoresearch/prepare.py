#!/usr/bin/env python3
"""
Download historical candle data from Hyperliquid API for backtesting.
Saves training data (2024) and test data (2025) as separate CSV files.

Run once: python3 prepare.py
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

HL_API = "https://api.hyperliquid.xyz/info"
COINS = ["BTC", "ETH", "SOL", "HYPE", "XRP", "SUI", "DOGE", "AVAX"]
INTERVAL = "15m"  # 15-minute candles (good balance of granularity vs size)

# Date ranges (HL API has ~2 months of candle history)
# Train: Feb 1 - Mar 15, 2026 (6 weeks)
# Test: Mar 16 - Apr 7, 2026 (3 weeks, withheld from AI)
TRAIN_START = datetime(2026, 2, 1, tzinfo=timezone.utc)
TRAIN_END = datetime(2026, 3, 15, 23, 59, tzinfo=timezone.utc)
TEST_START = datetime(2026, 3, 16, tzinfo=timezone.utc)
TEST_END = datetime(2026, 4, 7, 23, 59, tzinfo=timezone.utc)


def fetch_candles(coin: str, start_ts: int, end_ts: int, interval: str = "1m") -> list:
    """Fetch candle data from Hyperliquid API in chunks."""
    all_candles = []
    current_start = start_ts

    while current_start < end_ts:
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": current_start,
                "endTime": end_ts,
            }
        }

        try:
            resp = requests.post(HL_API, json=payload, timeout=30)
            resp.raise_for_status()
            candles = resp.json()
        except Exception as e:
            print(f"  Error fetching {coin} at {current_start}: {e}")
            time.sleep(2)
            continue

        if not candles:
            break

        all_candles.extend(candles)
        # Move start to after last candle
        last_ts = candles[-1].get("t", candles[-1].get("T", 0))
        if last_ts <= current_start:
            break
        current_start = last_ts + 1

        print(f"  {coin}: fetched {len(all_candles)} candles so far...")
        time.sleep(0.2)  # rate limit

    return all_candles


def candles_to_df(candles: list) -> pd.DataFrame:
    """Convert raw candle data to DataFrame."""
    df = pd.DataFrame(candles)
    # Hyperliquid candle format: t, T, s, i, o, c, h, l, v, n
    rename = {
        "t": "open_time", "T": "close_time",
        "o": "open", "h": "high", "l": "low", "c": "close",
        "v": "volume", "n": "num_trades"
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "open_time" in df.columns:
        df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
        df["datetime"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("datetime")

    df = df.sort_index()
    df = df[~df.index.duplicated(keep="first")]

    return df[["open", "high", "low", "close", "volume"]].dropna()


def download_coin(coin: str, start: datetime, end: datetime, label: str):
    """Download and save candle data for one coin."""
    outpath = DATA_DIR / f"{coin}_{label}.csv"
    if outpath.exists():
        existing = pd.read_csv(outpath)
        print(f"  {coin} {label}: already exists ({len(existing)} rows), skipping")
        return

    print(f"  Downloading {coin} {label} data...")
    start_ts = int(start.timestamp() * 1000)
    end_ts = int(end.timestamp() * 1000)

    candles = fetch_candles(coin, start_ts, end_ts, INTERVAL)
    if not candles:
        print(f"  WARNING: No candles returned for {coin} {label}")
        return

    df = candles_to_df(candles)
    df.to_csv(outpath)
    print(f"  {coin} {label}: saved {len(df)} candles to {outpath}")


def main():
    print("=== Hyperliquid Data Preparation ===\n")

    for coin in COINS:
        print(f"\n--- {coin} ---")
        download_coin(coin, TRAIN_START, TRAIN_END, "train")
        download_coin(coin, TEST_START, TEST_END, "test")

    print("\n=== Data download complete ===")
    print(f"Files saved to: {DATA_DIR}")

    # Print summary
    for f in sorted(DATA_DIR.glob("*.csv")):
        df = pd.read_csv(f)
        print(f"  {f.name}: {len(df)} rows, {df['open'].iloc[0]:.2f} -> {df['close'].iloc[-1]:.2f}")


if __name__ == "__main__":
    main()
