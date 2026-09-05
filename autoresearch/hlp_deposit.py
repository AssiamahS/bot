#!/usr/bin/env python3
"""Deposit USDC into the HLP (Hyperliquidity Provider) vault. 10-30% APY passive."""

import argparse, json
from pathlib import Path
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

HLP_VAULT = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("amount", type=float, help="USDC amount to deposit")
    p.add_argument("--withdraw", action="store_true", help="withdraw instead of deposit")
    args = p.parse_args()

    cfg = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())
    wallet = Account.from_key(cfg["wallet_private_key"])
    exch = Exchange(wallet, constants.MAINNET_API_URL, account_address=cfg["wallet_address"])
    is_deposit = not args.withdraw
    print(f"{'deposit' if is_deposit else 'withdraw'} ${args.amount} {'->' if is_deposit else '<-'} HLP")
    resp = exch.vault_usd_transfer(HLP_VAULT, is_deposit, int(args.amount * 1_000_000))
    print(json.dumps(resp, indent=2))


if __name__ == "__main__":
    main()
