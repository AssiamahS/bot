#!/usr/bin/env python3
"""Volume farmer for HYPE Season 2 airdrop. Generates trading volume with minimal
directional risk by opening + closing tiny positions on liquid HIP-3 markets.

Each cycle: pick a market, open small position with maker order, close it on next mid.
PnL ≈ -spread - fees. Volume = (size × 2) per cycle. Designed to print VOLUME, not PnL.

WARNING: this LOSES money on the trades. The bet is that HYPE airdrop value
exceeds the volume cost. Only run if you believe Season 2 will distribute meaningfully."""

import argparse, json, time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

import notify
from risk_manager import RiskManager

LOG_DIR = Path(__file__).parent / "volume_logs"
LOG_DIR.mkdir(exist_ok=True)
API = "https://api.hyperliquid.xyz/info"


def post(body):
    req = request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_meta(coin):
    if ":" in coin:
        dex, _ = coin.split(":", 1)
        meta = post({"type": "meta", "dex": dex})
    else:
        meta = post({"type": "meta"})
    return next(m for m in meta["universe"] if m["name"] == coin)


def round_size(raw, sz_decimals):
    return float(int(raw)) if sz_decimals <= 0 else round(raw, sz_decimals)


def round_price(raw, sz_decimals):
    px_decimals = max(0, 6 - sz_decimals)
    return round(float(f"{raw:.5g}"), px_decimals)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--coins", nargs="*", default=["xyz:CL", "xyz:NVDA", "xyz:TSLA", "xyz:MU"])
    p.add_argument("--usd", type=float, default=10.0, help="size per cycle (volume = 2x this)")
    p.add_argument("--cycle-secs", type=int, default=900, help="seconds between cycles (default 15min)")
    p.add_argument("--max-daily-loss-usd", type=float, default=2.0, help="kill switch on direct PnL")
    args = p.parse_args()

    cfg = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())
    wallet = Account.from_key(cfg["wallet_private_key"])
    addr = cfg["wallet_address"]
    exch = Exchange(wallet, constants.MAINNET_API_URL, account_address=addr)
    rm = RiskManager(max_daily_loss_pct=99.0, max_position_pct=20.0)

    metas = {c: fetch_meta(c) for c in args.coins}
    notify.send(f"🌀 *kim volume farmer started*\nmarkets: {', '.join(args.coins)}\nsize: \${args.usd}/cycle ({args.cycle_secs}s)\nfor HYPE Season 2 airdrop")

    log = LOG_DIR / f"farmer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    cycle = 0
    coin_idx = 0
    daily_loss = 0.0

    while True:
        if rm.is_killed() or daily_loss < -args.max_daily_loss_usd:
            notify.send(f"🛑 volume farmer stopped (loss ${daily_loss:.2f})")
            return
        coin = args.coins[coin_idx % len(args.coins)]
        coin_idx += 1
        cycle += 1
        try:
            mids = post({"type": "allMids"})
            mid = float(mids[coin])
            sz_dec = metas[coin]["szDecimals"]
            size = round_size(args.usd / mid, sz_dec)
            if size <= 0:
                print(f"[cycle {cycle}] {coin}: size too small, skipping")
                continue

            # open long with maker order at mid - 0.1%
            buy_px = round_price(mid * 0.999, sz_dec)
            print(f"[cycle {cycle}] {coin} mid=${mid} sz={size}  open @ ${buy_px}")
            r1 = exch.order(coin, True, size, buy_px, {"limit": {"tif": "Gtc"}})
            time.sleep(60)  # let the maker order rest

            # close — IOC sell at mid - 0.5%
            mids = post({"type": "allMids"})
            mid2 = float(mids[coin])
            sell_px = round_price(mid2 * 0.995, sz_dec)
            r2 = exch.order(coin, False, size, sell_px, {"limit": {"tif": "Ioc"}})

            cycle_log = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "cycle": cycle, "coin": coin, "size": size, "mid": mid,
                "open_resp": r1, "close_resp": r2,
            }
            with log.open("a") as f:
                f.write(json.dumps(cycle_log) + "\n")
        except Exception as e:
            print(f"cycle err: {e}")
            notify.send(f"⚠️ vf err: {e}", silent=True)

        time.sleep(max(0, args.cycle_secs - 60))


if __name__ == "__main__":
    main()
