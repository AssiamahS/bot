#!/usr/bin/env python3
"""Hummingbot Risk Controller v1 - monitors strategy logs and alerts on limit breaches."""

import os
import re
import time
import json

LOG_FILE = os.path.expanduser("~/hummingbot/logs/logs_conf_pure_mm_1.log")
POLL_SECONDS = 30

# Thresholds
MAX_BASE_PCT = 60.0        # alert if BTC% exceeds this
MAX_DAILY_LOSS = 0.50      # alert if total PnL drops below -$0.50
MAX_FAILED_ORDERS = 10     # alert if too many order failures in recent window


def parse_events(text: str) -> dict:
    """Extract metrics from recent log text."""
    metrics = {}

    # Count order failures (minimum order size errors)
    failures = re.findall(r"MarketOrderFailureEvent", text)
    metrics["failed_orders"] = len(failures)

    # Count successful creates
    creates = re.findall(r"(Buy|Sell)OrderCreatedEvent", text)
    metrics["created_orders"] = len(creates)

    # Count fills
    fills = re.findall(r"(Buy|Sell)OrderCompletedEvent", text)
    metrics["fills"] = len(fills)

    # Count cancels
    cancels = re.findall(r"OrderCancelledEvent", text)
    metrics["cancels"] = len(cancels)

    # Extract latest prices from order creation logs
    prices = re.findall(r"at ([\d.]+) USD", text)
    if prices:
        metrics["latest_order_price"] = float(prices[-1])

    return metrics


def tail_lines(path: str, n: int = 500) -> str:
    """Read last n lines of a file."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, n * 500)
            f.seek(max(0, size - block))
            data = f.read().decode("utf-8", errors="ignore")
            lines = data.splitlines()
            return "\n".join(lines[-n:])
    except Exception as e:
        return f"ERROR reading log: {e}"


def main():
    print(f"Risk Controller v1")
    print(f"Watching: {LOG_FILE}")
    print(f"Limits: BTC% > {MAX_BASE_PCT}%, Loss > ${MAX_DAILY_LOSS}")
    print(f"Polling every {POLL_SECONDS}s")
    print("-" * 50)

    last_size = 0

    while True:
        try:
            current_size = os.path.getsize(LOG_FILE)
            if current_size == last_size:
                time.sleep(POLL_SECONDS)
                continue

            # Read recent chunk
            text = tail_lines(LOG_FILE, 200)
            last_size = current_size

            metrics = parse_events(text)
            ts = time.strftime("%H:%M:%S")

            # Status line
            status = (
                f"[{ts}] "
                f"Orders: {metrics['created_orders']} created, "
                f"{metrics['fills']} filled, "
                f"{metrics['cancels']} cancelled, "
                f"{metrics['failed_orders']} FAILED"
            )
            print(status)

            # Alert: too many failures (sign of min order size or balance issue)
            if metrics["failed_orders"] > MAX_FAILED_ORDERS:
                print(f"  WARNING: {metrics['failed_orders']} order failures in recent window!")
                print(f"  Likely cause: order size below Kraken minimum (0.00005 BTC)")
                print(f"  Action needed: increase order_amount or reduce order_levels")

            # Alert: no fills at all
            if metrics["fills"] == 0 and metrics["created_orders"] > 5:
                print(f"  WARNING: {metrics['created_orders']} orders created but 0 fills")
                print(f"  Spreads may be too wide or market is quiet")

        except KeyboardInterrupt:
            print("\nRisk controller stopped.")
            return
        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
