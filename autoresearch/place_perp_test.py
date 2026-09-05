#!/usr/bin/env python3
"""Single tiny perp test order to confirm unified-account perp trading works."""

import argparse, json
from pathlib import Path
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--coin", default="ETH")
    p.add_argument("--usd", type=float, default=20.0)
    p.add_argument("--side", default="long", choices=["long", "short"])
    p.add_argument("--slippage-pct", type=float, default=0.5)
    args = p.parse_args()

    cfg = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())
    wallet = Account.from_key(cfg["wallet_private_key"])
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exch = Exchange(wallet, constants.MAINNET_API_URL, account_address=cfg["wallet_address"])

    meta = info.meta()
    asset_idx = next(i for i, m in enumerate(meta["universe"]) if m["name"] == args.coin)
    sz_decimals = meta["universe"][asset_idx]["szDecimals"]

    mid = float(info.all_mids()[args.coin])
    is_buy = args.side == "long"
    raw_px = mid * (1 + args.slippage_pct / 100 * (1 if is_buy else -1))
    # HL perp price: max 5 significant figures, max 6-szDecimals decimal places
    px_decimals = max(0, 6 - sz_decimals)
    limit_px = float(f"{raw_px:.5g}")
    limit_px = round(limit_px, px_decimals)
    raw_size = args.usd / mid
    size = round(raw_size, sz_decimals) if sz_decimals > 0 else int(raw_size)
    print(f"{args.side} {size} {args.coin} @ limit ${limit_px} (mid ${mid}, ~${size * mid:.2f})")
    resp = exch.order(args.coin, is_buy, size, limit_px, {"limit": {"tif": "Ioc"}})
    print(json.dumps(resp, indent=2))


if __name__ == "__main__":
    main()
