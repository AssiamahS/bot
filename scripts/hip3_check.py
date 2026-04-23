#!/usr/bin/env python3
"""HIP-3 position exit manager. Mirrors scripts/pead_check.py but for the
HIP-3 funding-harvest portfolio on Hyperliquid.

Exit rules:
  - funding_normalized:  |current fr| < EXIT_MULT * baseline (1.5x = 1.875e-5/hr)
  - stop_loss:           unrealized PnL <= -3% of position notional
  - target:              unrealized PnL >= +6% of position notional (book gains)
  - hold_expired:        position older than 7 days

Runs hourly from cron (funding settles hourly on HL).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

LOG_PATH = REPO / "hip3_trades.jsonl"
CONFIG_PATH = REPO / "config.json"

BASELINE_HR = 1.25e-5
EXIT_MULT = 1.5
STOP_PCT = 0.03
TARGET_PCT = 0.06
HOLD_MAX_DAYS = 7
API = "https://api.hyperliquid.xyz/info"


def api(body: dict):
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def log(rec: dict):
    rec["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def current_funding(coin: str) -> float | None:
    try:
        meta_ctxs = api({"type": "metaAndAssetCtxs", "dex": "xyz"})
        universe = meta_ctxs[0]["universe"]
        ctxs = meta_ctxs[1]
        for i, c in enumerate(universe):
            if c["name"] == coin:
                return float(ctxs[i].get("funding") or 0)
    except Exception:
        return None
    return None


def first_entry_ts(coin: str, wallet: str) -> float | None:
    """Scan last 500 fills for the earliest entry on this coin."""
    try:
        fills = api({"type": "userFills", "user": wallet})
    except Exception:
        return None
    earliest = None
    for f in fills:
        if f.get("coin") != coin:
            continue
        if "Open" not in f.get("dir", ""):
            continue
        t_ms = float(f.get("time", 0))
        if earliest is None or t_ms < earliest:
            earliest = t_ms
    return earliest / 1000 if earliest else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    priv = cfg["wallet_private_key"]
    wallet = cfg.get("wallet_address") or Account.from_key(priv).address
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(Account.from_key(priv), constants.MAINNET_API_URL)

    state = info.user_state(wallet)
    equity = float(state["marginSummary"]["accountValue"])
    positions = [p["position"] for p in state.get("assetPositions", [])]
    # Only act on HIP-3 positions (names contain a colon)
    hip3_pos = [p for p in positions if ":" in p.get("coin", "")]
    print(f"wallet {wallet[:10]}…  equity=${equity:.2f}  hip3_positions={len(hip3_pos)}")

    if not hip3_pos:
        log({"event": "hip3_check", "exits": 0, "holds": 0})
        return 0

    print(f"  {'COIN':<18} {'SIZE':>10} {'ENTRY':>10} {'MARK':>10} {'uPNL%':>7} "
          f"{'FR%':>8} {'AGE':>6}  ACTION")
    exits = 0
    holds = 0
    now = time.time()
    for p in hip3_pos:
        coin = p["coin"]
        sz = float(p.get("szi", 0))
        if sz == 0:
            continue
        entry = float(p.get("entryPx") or 0)
        pos_val = float(p.get("positionValue") or 0)
        upnl = float(p.get("unrealizedPnl") or 0)
        upnl_pct = upnl / max(abs(pos_val), 0.01)
        mark = pos_val / abs(sz) if sz else 0
        fr = current_funding(coin)
        age_sec = None
        ts = first_entry_ts(coin, wallet)
        if ts:
            age_sec = now - ts

        reason = None
        if upnl_pct <= -STOP_PCT:
            reason = "stop_loss"
        elif upnl_pct >= TARGET_PCT:
            reason = "target"
        elif fr is not None and abs(fr) < EXIT_MULT * BASELINE_HR:
            reason = "funding_normalized"
        elif age_sec is not None and age_sec > HOLD_MAX_DAYS * 86400:
            reason = "hold_expired"

        age_str = f"{age_sec/86400:.1f}d" if age_sec else "?"
        fr_str = f"{(fr or 0)*100:+6.4f}%" if fr is not None else "?"
        print(f"  {coin:<18} {sz:>+10.3f} ${entry:>9.4f} ${mark:>9.4f} {upnl_pct*100:>+6.2f}% "
              f"{fr_str:>8} {age_str:>6}  {reason or 'hold'}")

        if not reason:
            holds += 1
            continue
        if not args.live:
            exits += 1
            continue
        try:
            resp = exchange.market_close(coin)
            print(f"    -> closed ({reason}) resp={str(resp)[:120]}")
            log({"event": "hip3_exit", "coin": coin, "reason": reason,
                 "size": sz, "entry": entry, "mark": mark, "upnl": upnl})
            exits += 1
        except Exception as e:
            print(f"    -> close failed: {e}")
            log({"event": "hip3_exit_failed", "coin": coin, "error": str(e)})

    print(f"\nexits={exits} holds={holds}")
    log({"event": "hip3_check", "exits": exits, "holds": holds, "dry_run": not args.live})
    return 0


if __name__ == "__main__":
    sys.exit(main())
