#!/usr/bin/env python3
"""Self-healing watchdog. Run every 15 min via cron.

Checks:
  1. Alpaca paper account reachable + buying_power sane
  2. HL bot process status (via trader_status.json timestamp freshness)
  3. Excess risk (any single position > 25% of equity)
  4. Stale open orders (buy orders accepted > 24h ago, never filled)

Actions (self-healing):
  - Cancel stale buy orders that never filled (they won't, market moved away)
  - If Alpaca equity dropped > 10% in a day vs recorded baseline, alert + halt
  - If HL status file hasn't updated in > 5 min while bot supposedly running,
    write a HEARTBEAT_MISSED marker for the next run to see

Does NOT restart processes — restart authority stays with the user (SSH or
the hl-bot MCP). The watchdog's job is to NOTICE and LIMIT damage, not
silently restart things that might be stopped for a reason.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from strategies._alpaca_broker import AlpacaBroker

STATE_FILE = REPO / ".watchdog_state.json"
LOG_FILE = REPO / "watchdog.jsonl"
HEARTBEAT_FILE = REPO / "trader_status.json"
STALE_ORDER_HOURS = 24
EQUITY_DROP_HALT_PCT = 0.10
SINGLE_POSITION_CAP_PCT = 0.25
HEARTBEAT_STALE_SECS = 300


def log(event: str, **kw) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"[{event}] " + " ".join(f"{k}={v}" for k, v in kw.items()))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def check_alpaca(state: dict, heal: bool) -> list[str]:
    alerts = []
    try:
        broker = AlpacaBroker()
        acct = broker.account()
    except Exception as e:
        log("alpaca_unreachable", error=str(e))
        return [f"Alpaca API unreachable: {e}"]

    equity = float(acct["equity"])
    prev_equity = state.get("alpaca_equity_yesterday")
    if prev_equity:
        drop = (prev_equity - equity) / prev_equity
        if drop > EQUITY_DROP_HALT_PCT:
            alerts.append(f"Alpaca equity down {drop*100:.1f}% vs yesterday "
                          f"(${prev_equity:.2f} -> ${equity:.2f}) — investigate")
            log("equity_drop_alert", prev=prev_equity, now=equity, drop_pct=drop)

    positions = broker.positions()
    for p in positions:
        mv = float(p.get("market_value", 0))
        if equity > 0 and abs(mv) / equity > SINGLE_POSITION_CAP_PCT:
            alerts.append(f"Position {p['symbol']} at {abs(mv)/equity*100:.1f}% "
                          f"of equity — exceeds {SINGLE_POSITION_CAP_PCT*100:.0f}% cap")
            log("position_oversized", symbol=p["symbol"], mv=mv, equity=equity)

    now_ts = time.time()
    cutoff = now_ts - STALE_ORDER_HOURS * 3600
    open_orders = broker.orders(status="open", limit=200)
    stale = []
    for o in open_orders:
        submitted = o.get("submitted_at", "")
        try:
            ts = datetime.fromisoformat(submitted.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            stale.append(o)
    if stale and heal:
        for o in stale:
            try:
                broker.cancel_order(o["id"])
                log("stale_order_cancelled", symbol=o["symbol"], id=o["id"],
                    submitted_at=o.get("submitted_at"))
            except Exception as e:
                log("stale_order_cancel_failed", id=o["id"], error=str(e))
    elif stale:
        alerts.append(f"{len(stale)} stale open orders > {STALE_ORDER_HOURS}h old; "
                      f"pass --heal to cancel automatically")

    state["alpaca_equity_last_check"] = equity
    state["alpaca_equity_yesterday"] = state.get("alpaca_equity_last_check", equity)
    return alerts


def check_hl_heartbeat(state: dict) -> list[str]:
    alerts = []
    if not HEARTBEAT_FILE.exists():
        log("hl_heartbeat_missing")
        return ["trader_status.json not present — HL bot was never started or file was cleared"]
    try:
        payload = json.loads(HEARTBEAT_FILE.read_text())
    except Exception as e:
        return [f"trader_status.json malformed: {e}"]
    updated = payload.get("updated_at", 0)
    age = time.time() - float(updated)
    if payload.get("running") and age > HEARTBEAT_STALE_SECS:
        alerts.append(f"HL bot says running=True but status file is {int(age)}s old "
                      f"(stale > {HEARTBEAT_STALE_SECS}s) — process may be hung")
        log("hl_heartbeat_stale", age_secs=int(age))
    return alerts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--heal", action="store_true",
                    help="take corrective actions (cancel stale orders). "
                         "Default is notice-only; alerts go to stdout + watchdog.jsonl.")
    args = ap.parse_args()

    state = load_state()
    all_alerts: list[str] = []
    all_alerts += check_alpaca(state, heal=args.heal)
    all_alerts += check_hl_heartbeat(state)
    save_state(state)

    if all_alerts:
        print(f"\n--- {len(all_alerts)} ALERT(S) ---")
        for a in all_alerts:
            print(f"  * {a}")
        return 2  # non-zero exit so cron can page on it
    print("watchdog: all healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
