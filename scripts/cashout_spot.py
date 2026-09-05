#!/usr/bin/env python3
"""Cash out spot USDC from Hyperliquid to Arbitrum.

Bridge withdrawals debit the PERPS account, not spot. So we:
  1. usd_class_transfer: spot USDC -> perps
  2. withdraw_from_bridge: perps -> Arbitrum One

Reuses your existing config.json + Keychain key loader.

Usage:
  python3 scripts/cashout_spot.py --check   # show balances, sign nothing
  python3 scripts/cashout_spot.py           # full cash-out
  python3 scripts/cashout_spot.py 50        # cash out $50 of spot
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from eth_account import Account

from scripts.setup_main_key import load_main_key_from_keychain

CONFIG = json.loads((REPO / "config.json").read_text())
ADDR = CONFIG["wallet_address"]
HL_API = "https://api.hyperliquid.xyz"
WITHDRAW_FEE = 1.0
MIN_WITHDRAW = 5.0


def spot_usdc(info: Info) -> float:
    st = info.spot_user_state(ADDR)
    for bal in st.get("balances", []):
        if bal.get("coin") == "USDC":
            return float(bal.get("total", 0) or 0)
    return 0.0


def perp_withdrawable(info: Info) -> float:
    st = info.user_state(ADDR, dex="")
    return float(st.get("withdrawable", 0) or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("amount", nargs="?", type=float, help="spot USDC to move (default: all)")
    ap.add_argument("--check", action="store_true", help="show balances only")
    args = ap.parse_args()

    info = Info(HL_API, skip_ws=True)
    spot = spot_usdc(info)
    perp = perp_withdrawable(info)
    print(f"Spot USDC          : ${spot:.2f}")
    print(f"Perps withdrawable : ${perp:.2f}")
    print(f"Destination        : {ADDR}")
    if args.check:
        return 0

    move = round(args.amount if args.amount else spot, 2)
    if move > spot:
        print(f"Requested ${move:.2f} > spot ${spot:.2f}. Aborting.")
        return 1
    if move < MIN_WITHDRAW:
        print(f"${move:.2f} below minimum ${MIN_WITHDRAW}. Aborting.")
        return 1

    if input(f"\nMove ${move:.2f} spot->perps then withdraw to Arbitrum? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return 0

    print("\nLoading main key from Keychain... (click Allow if prompted)")
    key = load_main_key_from_keychain()
    if not key:
        print("No main key found. Run: python3 scripts/setup_main_key.py")
        return 1
    exchange = Exchange(Account.from_key(key), HL_API)

    print(f"Step 1: usd_class_transfer({move}, to_perp=True)...")
    r1 = exchange.usd_class_transfer(move, True)
    print(f"  result: {r1}")
    if not isinstance(r1, dict) or r1.get("status") != "ok":
        print("  spot->perps transfer failed. Stopping.")
        return 1

    target = round(perp + move, 2)
    for _ in range(20):
        time.sleep(1.5)
        perp = perp_withdrawable(info)
        if perp >= target - 0.5:
            break
    print(f"  perps withdrawable now: ${perp:.2f}")

    amount = round(perp, 2)  # fee is deducted from this; you receive amount - $1
    print(f"\nStep 2: withdraw_from_bridge({amount}, {ADDR})...")
    r2 = exchange.withdraw_from_bridge(amount, ADDR)
    print(f"  result: {r2}")
    if isinstance(r2, dict) and r2.get("status") == "ok":
        print(f"\nDone. ~${amount - WITHDRAW_FEE:.2f} lands on Arbitrum One in 3-5 min.")
        print("Check MetaMask on the Arbitrum One network.")
        return 0
    print("  Withdrawal returned an error — see result above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
