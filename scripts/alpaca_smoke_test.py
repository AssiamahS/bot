#!/usr/bin/env python3
"""Smoke test the Alpaca paper account and PEAD sizing math end-to-end.

Does NOT place any order. Just proves:
  1. Credentials in .alpaca_keys load correctly
  2. Account is reachable
  3. Sizing math produces a sensible number for the current equity
  4. A reference price lookup works

Run before any live (paper or real) trade to catch config drift.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategies._alpaca_broker import AlpacaBroker
from strategies.earnings_drift import EarningsEvent, signal, size_position, should_exit


def main() -> int:
    print("=" * 60)
    print("Alpaca smoke test")
    print("=" * 60)

    try:
        broker = AlpacaBroker()
    except Exception as e:
        print(f"CREDS FAIL: {e}")
        return 1

    try:
        acct = broker.account()
    except Exception as e:
        print(f"API FAIL: {e}")
        return 1

    equity = float(acct["equity"])
    cash = float(acct["cash"])
    print(f"  account:     {acct.get('account_number', '?')}")
    print(f"  status:      {acct.get('status')}")
    print(f"  equity:      ${equity:,.2f}")
    print(f"  cash:        ${cash:,.2f}")
    print(f"  positions:   {len(broker.positions())} open")

    try:
        trade = broker.latest_trade("AAPL")
        px = float(trade["trade"]["p"])
        print(f"  AAPL latest: ${px:.2f} (data feed OK)")
    except Exception as e:
        print(f"  AAPL latest: FAIL ({e})")
        px = 200.0

    print()
    print("Sizing dry-run: simulated +15% EPS surprise on AAPL")
    print("-" * 60)
    ev = EarningsEvent(
        ticker="AAPL",
        announced_on=date.today() - timedelta(days=1),
        estimated_eps=2.00,
        actual_eps=2.30,
        price_at_announce=px,
        price_current=px,
    )
    sig = signal(ev)
    sz = size_position(equity, ev)
    print(f"  surprise_pct: {ev.surprise_pct:+.2%}")
    print(f"  signal:       {sig:+d}")
    print(f"  size (USD):   ${sz:.2f}")
    print(f"  size (frac):  {sz / equity * 100:.2f}% of equity")
    print(f"  exit check:   {should_exit(ev)}")

    if sz < 1.0:
        print()
        print("WARN: size < $1, Alpaca will reject. Increase account equity.")
    elif sz > cash:
        print()
        print("WARN: size > cash. Check buying_power vs cash (margin vs paper).")
    else:
        print()
        print(f"OK — a real call would place a market buy for ${sz:.2f} of AAPL.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
