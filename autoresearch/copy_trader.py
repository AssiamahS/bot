#!/usr/bin/env python3
"""
copy_trader — mirror a target wallet's fills via HL WebSocket.

Default mode is PAPER (logs only, no orders). Pass --live + an account address +
HL_PRIVATE_KEY env to actually trade. Position sizing is a fixed fraction of YOUR
account balance, not a 1:1 size copy.

Usage:
    python3 copy_trader.py --target 0xcf67...                     # paper, log fills
    python3 copy_trader.py --target 0xcf67... --size-pct 5        # paper, sized
    python3 copy_trader.py --target 0xcf67... --size-pct 5 --live # live (sets real orders)

Requires: pip install hyperliquid-python-sdk
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants
    from eth_account import Account
except ImportError:
    sys.exit("pip install hyperliquid-python-sdk eth_account")

LOG_DIR = Path(__file__).parent / "copy_logs"
LOG_DIR.mkdir(exist_ok=True)


def make_handler(target: str, exchange, info, size_pct: float, live: bool, log_path: Path, my_address: str):
    """Returns a callback for HL websocket userFills events."""
    def handle(msg: dict):
        if msg.get("channel") != "userFills":
            return
        data = msg.get("data", {})
        fills = data.get("fills", [])
        if data.get("isSnapshot"):
            print(f"snapshot: {len(fills)} historical fills (ignored)")
            return
        for f in fills:
            line = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "target": target, "coin": f.get("coin"), "side": f.get("side"),
                "px": f.get("px"), "sz": f.get("sz"), "dir": f.get("dir"),
                "hash": f.get("hash"),
            }
            with log_path.open("a") as fp:
                fp.write(json.dumps(line) + "\n")
            print(f"[{line['ts']}] {line['side']} {line['sz']} {line['coin']} @ {line['px']}  ({line['dir']})")

            if not live:
                continue

            # live mode: convert target's absolute size into our $-equivalent at size_pct of OUR account
            try:
                my_state = info.user_state(my_address)
                my_equity = float(my_state["marginSummary"]["accountValue"])
            except Exception as e:
                print(f"  failed to read my equity: {e}")
                continue
            target_px = float(f["px"])
            target_sz = float(f["sz"])
            notional_target = target_px * target_sz
            scale = (my_equity * size_pct / 100) / max(notional_target, 1)
            my_sz = round(target_sz * scale, 4)
            if my_sz <= 0:
                print(f"  size too small after scaling, skipping")
                continue
            is_buy = f["side"] == "B"
            try:
                resp = exchange.market_open(f["coin"], is_buy, my_sz, slippage=0.005)
                print(f"  -> live order: {resp}")
            except Exception as e:
                print(f"  -> order failed: {e}")
    return handle


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", required=True, help="wallet address to copy")
    p.add_argument("--size-pct", type=float, default=2.0, help="pct of YOUR equity to allocate per signal")
    p.add_argument("--live", action="store_true", help="actually place orders (otherwise paper)")
    p.add_argument("--testnet", action="store_true")
    args = p.parse_args()

    base = constants.TESTNET_API_URL if args.testnet else constants.MAINNET_API_URL
    info = Info(base, skip_ws=False)

    exchange = None
    my_address = ""
    if args.live:
        pk = os.environ.get("HL_PRIVATE_KEY")
        if not pk:
            cfg_path = Path.home() / "hyperliquid-sol" / "config.json"
            if cfg_path.exists():
                try:
                    cfg = json.loads(cfg_path.read_text())
                    pk = cfg.get("wallet_private_key")
                    if pk:
                        print(f"using key from {cfg_path}")
                except Exception:
                    pass
        if not pk:
            sys.exit("no key — set HL_PRIVATE_KEY env or wallet_private_key in ~/hyperliquid-sol/config.json")
        wallet = Account.from_key(pk)
        my_address = wallet.address
        exchange = Exchange(wallet, base)
        state = info.user_state(my_address)
        equity = float(state["marginSummary"]["accountValue"])
        print(f"LIVE MODE · my address {my_address} · equity ${equity:,.2f} · sizing {args.size_pct}%")
    else:
        print(f"PAPER MODE · copying {args.target} · would size {args.size_pct}% of equity")

    log_path = LOG_DIR / f"{args.target}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    handler = make_handler(args.target, exchange, info, args.size_pct, args.live, log_path, my_address)

    print(f"subscribing to userFills for {args.target}")
    print(f"log: {log_path}")
    info.subscribe({"type": "userFills", "user": args.target}, handler)
    try:
        while True:
            import time
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
