#!/usr/bin/env python3
"""Move USDC from HL spot to HL perp account so the bot can trade."""

import argparse
import json
import sys
from pathlib import Path

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account


def main():
    p = argparse.ArgumentParser()
    p.add_argument("amount", type=float, help="USDC to move")
    p.add_argument("--reverse", action="store_true", help="perp -> spot instead")
    args = p.parse_args()

    cfg = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())
    pk = cfg["wallet_private_key"]
    addr = cfg["wallet_address"]

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    wallet = Account.from_key(pk)
    print(f"agent (signer): {wallet.address}")
    print(f"main account:   {addr}")
    exch = Exchange(wallet, constants.MAINNET_API_URL, account_address=addr)

    direction = "perp -> spot" if args.reverse else "spot -> perp"
    print(f"transferring ${args.amount} USDC: {direction}")
    to_perp = not args.reverse
    resp = exch.usd_class_transfer(args.amount, to_perp)
    print(json.dumps(resp, indent=2))


if __name__ == "__main__":
    main()
