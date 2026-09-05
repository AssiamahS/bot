#!/usr/bin/env python3
"""Daily PEAD position manager: checks all open PEAD positions and exits any
that have hit stop, target, or hold-window expiration. Appends every decision
to pead_trades.jsonl. Designed to be run from cron once per market day
(e.g. 15:55 ET, 5 minutes before close).

Safe to run any number of times; it only acts on positions whose trigger
conditions are met. Paper account by default; flip .alpaca_keys to live when
the paper track record is real.

Exit rules (from strategies.earnings_drift):
- stop_loss:         unrealized P/L <= -8% from entry
- target:            unrealized P/L >= +16% from entry
- hold_window:       position older than 45 calendar days (we approximate
                     with Alpaca's 'created_at' on the current position,
                     which = entry time)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from strategies._alpaca_broker import AlpacaBroker
from strategies.earnings_drift import HOLD_MAX_DAYS

LOG_PATH = REPO / "pead_trades.jsonl"
STOP_PCT = 0.08
TARGET_PCT = 0.16


def append(record: dict) -> None:
    record["logged_at"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def evaluate(pos: dict) -> tuple[str | None, float]:
    """Return (exit_reason, unrealized_pl_pct). reason=None means hold."""
    unrealized_plpc = float(pos.get("unrealized_plpc", 0))  # Alpaca returns fraction, e.g. 0.05 = +5%
    if unrealized_plpc <= -STOP_PCT:
        return "stop_loss", unrealized_plpc
    if unrealized_plpc >= TARGET_PCT:
        return "target", unrealized_plpc
    # Age check — if Alpaca returns a purchase date we'd use it; fall back via orders
    return None, unrealized_plpc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually place closing orders")
    ap.add_argument("--max-age-days", type=int, default=HOLD_MAX_DAYS)
    args = ap.parse_args()

    broker = AlpacaBroker()
    acct = broker.account()
    print(f"account={acct['account_number']} equity=${float(acct['equity']):,.2f}")

    positions = broker.positions()
    if not positions:
        print("no open positions")
        append({"event": "pead_check", "exits": 0, "holds": 0})
        return 0

    # Map ticker -> earliest buy order time (approximate entry date).
    all_orders = broker.orders(status="all", limit=500)
    entry_time: dict[str, str] = {}
    for o in all_orders:
        if o.get("side") != "buy" or o.get("status") not in ("filled", "partially_filled"):
            continue
        sym = o["symbol"]
        ts = o.get("filled_at") or o.get("submitted_at")
        if ts and (sym not in entry_time or ts < entry_time[sym]):
            entry_time[sym] = ts

    print(f"  {'SYM':<7} {'QTY':>8} {'ENTRY':>9} {'MARK':>9} {'PL%':>7} {'AGE':>5}  ACTION")
    exits, holds = 0, 0
    now = datetime.now(timezone.utc)
    for pos in positions:
        sym = pos["symbol"]
        qty = float(pos["qty"])
        entry = float(pos["avg_entry_price"])
        mark = float(pos["current_price"])
        plpc = float(pos.get("unrealized_plpc", 0))
        age_days = None
        if sym in entry_time:
            ts = datetime.fromisoformat(entry_time[sym].replace("Z", "+00:00"))
            age_days = (now - ts).days
        reason, _ = evaluate(pos)
        if reason is None and age_days is not None and age_days > args.max_age_days:
            reason = "hold_window_expired"
        note = reason if reason else "hold"
        age_str = f"{age_days}d" if age_days is not None else "?"
        print(f"  {sym:<7} {qty:>8.2f} ${entry:>8.2f} ${mark:>8.2f} {plpc*100:>+6.2f}% {age_str:>5}  {note}")
        if reason is None:
            holds += 1
            continue
        if args.live:
            try:
                broker.close_position(sym)
                print(f"    -> closed {sym} ({reason})")
                append({"event": "pead_exit", "symbol": sym, "reason": reason,
                        "qty": qty, "entry": entry, "mark": mark, "pl_pct": plpc})
                exits += 1
            except Exception as exc:
                print(f"    -> FAIL closing {sym}: {exc}")
                append({"event": "pead_exit_failed", "symbol": sym, "error": str(exc)})
        else:
            print(f"    -> would close (dry run; use --live)")
            exits += 1

    print(f"\nexits={exits} holds={holds}")
    append({"event": "pead_check", "exits": exits, "holds": holds, "dry_run": not args.live})
    return 0


if __name__ == "__main__":
    sys.exit(main())
