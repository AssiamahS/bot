#!/usr/bin/env python3
"""Place ONE small spot buy to prove the live wire. Default: $30 of PURR/USDC IOC."""

import argparse
import json
from pathlib import Path

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--coin", default="PURR/USDC")
    p.add_argument("--usd", type=float, default=30.0)
    p.add_argument("--slippage-pct", type=float, default=0.5)
    args = p.parse_args()

    cfg = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())
    wallet = Account.from_key(cfg["wallet_private_key"])
    main_addr = cfg["wallet_address"]

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exch = Exchange(wallet, constants.MAINNET_API_URL, account_address=main_addr)

    mids = info.all_mids()
    spot_meta = info.spot_meta()
    sz_decimals = 2
    mid = 0.0
    for i, pair in enumerate(spot_meta["universe"]):
        if pair["name"] == args.coin:
            base_idx = pair["tokens"][0]
            base_tok = next(t for t in spot_meta["tokens"] if t["index"] == base_idx)
            sz_decimals = base_tok["szDecimals"]
            mid = float(mids.get(f"@{i}", 0)) or float(mids.get(args.coin, 0))
            break
    if mid <= 0:
        mid = float(mids.get(args.coin, 0))
    if mid <= 0:
        print(f"no mid for {args.coin}")
        return

    limit_px = round(mid * (1 + args.slippage_pct / 100), 6)
    raw_size = args.usd / mid
    size = round(raw_size, sz_decimals) if sz_decimals > 0 else int(raw_size)
    print(f"buying {size} {args.coin} @ limit ${limit_px} (mid ${mid}, sz_decimals={sz_decimals}, ~${size * mid:.2f})")

    resp = exch.order(args.coin, True, size, limit_px, {"limit": {"tif": "Ioc"}})
    print(json.dumps(resp, indent=2))


if __name__ == "__main__":
    main()
