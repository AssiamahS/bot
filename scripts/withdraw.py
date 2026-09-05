#!/usr/bin/env python3
"""Withdraw USDC from Hyperliquid to Arbitrum.

Usage:
    python3 scripts/withdraw.py           # withdraw full balance (minus $1 fee)
    python3 scripts/withdraw.py 50        # withdraw specific amount
    python3 scripts/withdraw.py --check   # just show current balance
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from eth_account import Account
except ImportError as e:
    print(f"Missing dep: {e}")
    sys.exit(1)

from scripts.setup_main_key import load_main_key_from_keychain

CONFIG = json.loads((REPO / "config.json").read_text())
ADDR = CONFIG["wallet_address"]
HL_API = "https://api.hyperliquid.xyz"
WITHDRAW_FEE = 1.0
MIN_WITHDRAW = 5.0


def get_spot_usdc(info: Info) -> float:
    spot = info.spot_user_state(ADDR)
    for bal in spot.get("balances", []):
        if bal.get("coin") == "USDC":
            return float(bal.get("total", 0))
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("amount", nargs="?", type=float, help="USDC to withdraw (default: all)")
    ap.add_argument("--check", action="store_true", help="show balance only, no withdraw")
    args = ap.parse_args()

    info = Info(HL_API, skip_ws=True)
    balance = get_spot_usdc(info)
    print(f"Spot USDC balance: ${balance:.2f}")
    print(f"Withdraw to:       {ADDR}")

    if args.check:
        return 0

    # Determine amount
    if args.amount:
        amount = args.amount
    else:
        amount = round(balance - WITHDRAW_FEE, 2)
        print(f"Withdrawing full balance minus $1 fee: ${amount:.2f}")

    if amount < MIN_WITHDRAW:
        print(f"Amount ${amount:.2f} is below minimum ${MIN_WITHDRAW}. Aborting.")
        return 1
    if amount > balance:
        print(f"Amount ${amount:.2f} exceeds balance ${balance:.2f}. Aborting.")
        return 1

    confirm = input(f"\nWithdraw ${amount:.2f} USDC to Arbitrum at {ADDR}? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return 0

    print("\nLoading main key from Keychain... (click Allow if prompted)")
    key = load_main_key_from_keychain()
    if not key:
        print("No main key found. Run: python3 scripts/setup_main_key.py")
        return 1

    account = Account.from_key(key)
    exchange = Exchange(account, HL_API)

    print(f"Submitting withdraw_from_bridge({amount}, {ADDR})...")
    result = exchange.withdraw_from_bridge(amount, ADDR)
    print(f"Result: {result}")

    if result.get("status") == "ok":
        print(f"\nWithdraw submitted. ~$3-5 min to arrive on Arbitrum as USDC.")
        print(f"Check MetaMask on Arbitrum One network.")
    else:
        print("Something went wrong — check the result above.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
