#!/usr/bin/env python3
"""HL perp-based HODL basket — equal-weight long BTC/ETH/SOL/HYPE at 1x.

Hyperliquid spot doesn't list BTC/ETH/SOL directly; they exist only as perps.
1x leverage on a perp is a near-HODL proxy: notional == margin, so the worst
case at 1x is the position value going to zero (which requires the coin to
drop ~100%). No force-liquidation at 50%/70% moves like 10x cross. The
trade-off is funding fees, which average ~+/-0.01%/day on BTC and are a
rounding error on a $10 position.

Why this exists: the market-maker lost money by being delta-neutral, so when
BTC ripped 40% we captured zero. This script puts a fraction of the HL
account into actual directional exposure.

Default: DRY RUN. Pass --live to submit real orders. Paper practice doesn't
exist on HL, so 'live' here means real money. Default basket is $10 per
coin = $40 of the $61 account = ~66% deployed, keeps $21 dry.

Safety checks:
  - Refuses to run if the MM bot is writing trader_status.json recently
    (i.e. active). Avoids the MM bot fighting HODL positions.
  - Adds each coin to orphan_exempt_coins in config.json so that if the MM
    bot is later restarted it won't flatten these positions as 'orphans'.
  - Refuses to place an order larger than 25% of account equity per coin.
  - Uses market orders with a default slippage cap of 50 bps.
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

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from strategies import sizing


LOG_PATH = REPO / "crypto_positions.jsonl"
STATUS_PATH = REPO / "trader_status.json"
CONFIG_PATH = REPO / "config.json"
BASKET = ["BTC", "ETH", "SOL", "HYPE"]
DEFAULT_PER_COIN_USD = 10.0
MAX_COIN_FRAC = 0.25     # 25% of equity max per coin
MM_ACTIVE_THRESHOLD_SECS = 120  # if trader_status updated < 2 min ago, MM is live


def log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  log: {record.get('event', '?')} {record.get('coin', '')}")


def mm_bot_active() -> bool:
    if not STATUS_PATH.exists():
        return False
    try:
        s = json.loads(STATUS_PATH.read_text())
    except Exception:
        return False
    if not s.get("running"):
        return False
    age = time.time() - float(s.get("updated_at", 0))
    return age < MM_ACTIVE_THRESHOLD_SECS


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=4) + "\n")


def ensure_orphan_exempt(coins: list[str]) -> None:
    cfg = load_config()
    current = set(cfg.get("orphan_exempt_coins", []))
    needed = set(coins)
    if needed.issubset(current):
        return
    cfg["orphan_exempt_coins"] = sorted(current | needed)
    if "orphan_position_mode" not in cfg:
        cfg["orphan_position_mode"] = "alert"  # don't auto-close unknown positions
    save_config(cfg)
    print(f"  config.json updated: orphan_exempt_coins += {sorted(needed - current)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually place orders")
    ap.add_argument("--per-coin", type=float, default=DEFAULT_PER_COIN_USD)
    ap.add_argument("--coins", nargs="+", default=BASKET, help="override basket")
    ap.add_argument("--slippage-bps", type=float, default=50.0)
    args = ap.parse_args()

    cfg = load_config()
    priv = cfg["wallet_private_key"]
    wallet = cfg.get("wallet_address") or Account.from_key(priv).address

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(Account.from_key(priv), constants.MAINNET_API_URL)

    state = info.user_state(wallet)
    equity = float(state["marginSummary"]["accountValue"])
    withdrawable = float(state.get("withdrawable", 0))
    positions = {p["position"]["coin"]: p["position"] for p in state.get("assetPositions", [])}

    # Spot side (USDC sits here, separate from perps clearinghouse)
    spot_state = info.spot_user_state(wallet)
    spot_usdc = next((float(b["total"]) for b in spot_state.get("balances", []) if b["coin"] == "USDC"), 0.0)

    total_capital = equity + spot_usdc
    print(f"wallet {wallet[:10]}…")
    print(f"  perps equity:  ${equity:,.2f}   (positions: {list(positions) or 'none'})")
    print(f"  spot USDC:     ${spot_usdc:,.2f}")
    print(f"  TOTAL:         ${total_capital:,.2f}")

    if mm_bot_active():
        print("ABORT: MM bot heartbeat is fresh — stop the bot first, HODL and MM on the "
              "same wallet fight each other")
        log({"event": "abort", "reason": "mm_bot_active"})
        return 1

    per_coin = args.per_coin
    per_coin_cap = total_capital * MAX_COIN_FRAC
    if per_coin > per_coin_cap:
        print(f"per_coin ${per_coin} > cap ${per_coin_cap:.2f} ({MAX_COIN_FRAC*100:.0f}% of total) — clamping")
        per_coin = per_coin_cap

    total_budget = per_coin * len(args.coins)
    if total_budget > total_capital * 0.75:
        print(f"ABORT: basket would deploy ${total_budget:.2f} > 75% of total capital (${total_capital:.2f})")
        return 1

    # Perps needs enough margin to actually place the orders. At 1x leverage
    # margin_required == notional. If perps balance is too low, we have to
    # transfer from spot first.
    margin_needed = total_budget * 1.05  # 5% safety buffer for price slippage
    transfer_needed = max(0.0, margin_needed - equity)
    if transfer_needed > 0:
        if transfer_needed > spot_usdc:
            print(f"ABORT: need ${margin_needed:.2f} in perps, have ${equity:.2f}; "
                  f"short ${transfer_needed:.2f} and spot only has ${spot_usdc:.2f}")
            return 1
        print(f"\nwill transfer ${transfer_needed:.2f} from spot → perps before placing orders")

    mids = info.all_mids()
    print(f"\nbasket ({len(args.coins)} coins, ${per_coin:.2f} each = ${total_budget:.2f} total):")
    actions = []
    for coin in args.coins:
        mid = float(mids.get(coin, 0))
        if mid <= 0:
            print(f"  {coin}: no mid price available, skipping")
            continue
        if coin in positions:
            sz = float(positions[coin].get("szi", 0))
            if abs(sz) * mid >= per_coin * 0.5:
                print(f"  {coin}: already have ${abs(sz)*mid:.2f} notional — skipping")
                continue
        # size in base units (e.g. 0.0001 BTC)
        size_base = round(per_coin / mid, 6)
        slip = mid * (1 + args.slippage_bps / 10000)
        print(f"  {coin}: mid=${mid:,.4f}  buy size={size_base}  notional=${size_base*mid:.2f}  "
              f"limit<=${slip:,.4f}")
        actions.append({"coin": coin, "mid": mid, "size_base": size_base, "limit_px": slip})

    if not args.live:
        print(f"\nDRY RUN — pass --live to submit {len(actions)} orders")
        return 0

    # Live path. Pre-flight the orphan-exempt config so a later MM restart
    # doesn't auto-flatten these positions.
    ensure_orphan_exempt(args.coins)

    # Move USDC spot → perps if needed.
    if transfer_needed > 0:
        try:
            tr = exchange.usd_class_transfer(transfer_needed, to_perp=True)
            print(f"  transfer ${transfer_needed:.2f} spot→perps: {tr}")
            log({"event": "spot_to_perps", "amount_usd": transfer_needed, "resp": str(tr)[:200]})
            # Small wait for balance to update clearinghouse-side.
            time.sleep(3)
        except Exception as e:
            print(f"  transfer failed: {e}")
            log({"event": "spot_to_perps_failed", "error": str(e)})
            return 1

    print(f"\nLIVE — setting 1x leverage and submitting {len(actions)} orders")
    for a in actions:
        coin = a["coin"]
        try:
            exchange.update_leverage(1, coin, is_cross=True)
        except Exception as e:
            print(f"  {coin}: update_leverage failed: {e}")

        try:
            resp = exchange.order(
                coin, True, a["size_base"], a["limit_px"],
                {"limit": {"tif": "Gtc"}},
                reduce_only=False,
            )
            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            result = statuses[0] if statuses else {}
            oid = result.get("resting", {}).get("oid") or result.get("filled", {}).get("oid")
            print(f"  {coin}: order {oid} {list(result)}")
            log({"event": "crypto_entry", "coin": coin, "size_base": a["size_base"],
                 "limit_px": a["limit_px"], "order_id": oid, "resp_status": list(result)})
        except Exception as e:
            print(f"  {coin}: order failed: {e}")
            log({"event": "crypto_entry_failed", "coin": coin, "error": str(e)})

    return 0


if __name__ == "__main__":
    sys.exit(main())
