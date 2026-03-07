#!/usr/bin/env python3
"""
Market data recorder for Hyperliquid.
Records orderbook snapshots to CSV for backtesting.
Stores one file per day per coin.
"""
import time
import csv
import os
from datetime import datetime

from hyperliquid.info import Info
from hyperliquid.utils import constants

COINS = ["SOL", "BTC", "ETH"]
INTERVAL = 1  # seconds between snapshots
DATA_DIR = os.path.dirname(os.path.abspath(__file__))

info = Info(constants.MAINNET_API_URL, skip_ws=True)


def get_filename(coin):
    date_str = datetime.now().strftime("%Y_%m_%d")
    return os.path.join(DATA_DIR, f"{coin.lower()}_{date_str}.csv")


def record_snapshot(coin, writer):
    try:
        l2 = info.l2_snapshot(coin)
        if not l2 or len(l2["levels"]) != 2:
            return
        bids = l2["levels"][0]
        asks = l2["levels"][1]
        if not bids or not asks:
            return

        best_bid = float(bids[0]["px"])
        best_ask = float(asks[0]["px"])
        mid = (best_bid + best_ask) / 2
        bid_depth = sum(float(b["sz"]) for b in bids[:5])
        ask_depth = sum(float(a["sz"]) for a in asks[:5])

        writer.writerow([
            int(time.time()),
            best_bid,
            best_ask,
            round(mid, 4),
            round(bid_depth, 4),
            round(ask_depth, 4),
        ])
    except Exception as e:
        print(f"  {coin} error: {e}")


def main():
    print(f"Recording market data for {COINS}")
    print(f"Interval: {INTERVAL}s | Dir: {DATA_DIR}")

    files = {}
    writers = {}
    current_date = None

    while True:
        today = datetime.now().strftime("%Y_%m_%d")

        # Rotate files at midnight
        if today != current_date:
            for f in files.values():
                f.close()
            files = {}
            writers = {}
            current_date = today

            for coin in COINS:
                fname = get_filename(coin)
                is_new = not os.path.exists(fname)
                fh = open(fname, "a", newline="")
                w = csv.writer(fh)
                if is_new:
                    w.writerow(["timestamp", "best_bid", "best_ask", "mid", "bid_depth", "ask_depth"])
                files[coin] = fh
                writers[coin] = w
            print(f"  New day: {today}")

        for coin in COINS:
            record_snapshot(coin, writers[coin])
            files[coin].flush()

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
