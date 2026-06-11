#!/usr/bin/env python3
"""Live funding-harvest bot. Polls funding rate on a HL perp, opens position when funding
is in the top/bottom decile of recent history (receives funding while holding).

Defaults to xyz:SILVER (the only HIP-3 market profitable in both train + test backtest).
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

import notify
from risk_manager import RiskManager

API = "https://api.hyperliquid.xyz/info"
LOG_DIR = Path(__file__).parent / "live_logs"
LOG_DIR.mkdir(exist_ok=True)
HISTORY_HOURS = 168  # 7 days


def post(body: dict) -> any:
    req = request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_recent_funding(coin: str, hours: int) -> list[float]:
    end = int(time.time() * 1000)
    start = end - hours * 3_600_000
    rows = post({"type": "fundingHistory", "coin": coin, "startTime": start, "endTime": end})
    return [float(r["fundingRate"]) for r in rows]


def fetch_current_state(addr: str) -> dict:
    return post({"type": "clearinghouseState", "user": addr})


def fetch_unified_equity(addr: str) -> float:
    """Sum of perp accountValue + spot USDC (unified-mode total equity)."""
    perp = post({"type": "clearinghouseState", "user": addr})
    perp_eq = float(perp["marginSummary"]["accountValue"])
    spot = post({"type": "spotClearinghouseState", "user": addr})
    usdc = 0.0
    for b in spot.get("balances", []):
        if b["coin"] == "USDC":
            usdc = float(b["total"])
            break
    return perp_eq + usdc


def fetch_meta(coin_query: str) -> dict:
    """Get szDecimals + maxLeverage for a perp market (works for HIP-3 too)."""
    if ":" in coin_query:
        dex, _ = coin_query.split(":", 1)
        meta = post({"type": "meta", "dex": dex})
    else:
        meta = post({"type": "meta"})
    for m in meta["universe"]:
        if m["name"] == coin_query:
            return m
    raise ValueError(f"market {coin_query} not in meta")


def fetch_mid(coin: str) -> float:
    # HIP-3 markets (e.g. xyz:SILVER) only appear in allMids when the dex is named
    if ":" in coin:
        dex, _ = coin.split(":", 1)
        mids = post({"type": "allMids", "dex": dex})
    else:
        mids = post({"type": "allMids"})
    if coin in mids:
        return float(mids[coin])
    raise ValueError(f"no mid for {coin}")


def round_size(raw: float, sz_decimals: int) -> float:
    if sz_decimals <= 0:
        return float(int(raw))
    return round(raw, sz_decimals)


def round_price(raw: float, sz_decimals: int) -> float:
    px_decimals = max(0, 6 - sz_decimals)
    px = float(f"{raw:.5g}")
    return round(px, px_decimals)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--coin", default="xyz:SILVER")
    p.add_argument("--usd", type=float, default=30.0, help="position notional")
    p.add_argument("--high-quantile", type=float, default=0.85)
    p.add_argument("--low-quantile", type=float, default=0.15)
    p.add_argument("--poll-secs", type=int, default=300)
    p.add_argument("--max-daily-loss-pct", type=float, default=3.0)
    p.add_argument("--dry-run", action="store_true", help="log signals, dont place orders")
    p.add_argument("--maker", action="store_true", help="use Alo (post-only) for rebate; orders may not fill immediately")
    args = p.parse_args()

    cfg = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())
    wallet = Account.from_key(cfg["wallet_private_key"])
    addr = cfg["wallet_address"]
    rm = RiskManager(max_daily_loss_pct=args.max_daily_loss_pct, max_position_pct=50.0)

    # retry with backoff for the SDK init (it calls meta + spotMeta on construct)
    # HIP-3 coins (dex:NAME) need their dex passed so the SDK maps name -> asset id
    perp_dexs = [""] + ([args.coin.split(":", 1)[0]] if ":" in args.coin else [])
    info = exch = market_meta = None
    delay = 5
    for attempt in range(8):
        try:
            info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=perp_dexs)
            exch = Exchange(wallet, constants.MAINNET_API_URL, account_address=addr, perp_dexs=perp_dexs)
            market_meta = fetch_meta(args.coin)
            break
        except Exception as e:
            print(f"init attempt {attempt+1} failed: {e}, sleeping {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 120)
    if not market_meta:
        print("init failed after retries, exiting")
        return
    sz_decimals = market_meta["szDecimals"]
    print(f"market {args.coin}  szDecimals={sz_decimals}  maxLev={market_meta['maxLeverage']}")
    notify.send(f"🤖 *kim live bot started*\nmarket: `{args.coin}`\nposition: `${args.usd}`\ndry-run: `{args.dry_run}`")

    pos_open_dir = 0  # 1 long, -1 short, 0 flat
    pos_entry_funding = 0.0
    starting_equity = 0.0

    log_path = LOG_DIR / f"{args.coin.replace(':','_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

    while True:
        if rm.is_killed():
            print("RISK KILL — exiting")
            notify.send(f"🛑 *kim killed*\n{rm.kill_file.read_text()}")
            return

        try:
            state = fetch_current_state(addr)
            equity = fetch_unified_equity(addr)
            if starting_equity == 0:
                starting_equity = equity
            our_pos = next((p for p in state.get("assetPositions", []) if p["position"]["coin"] == args.coin), None)
            our_size = float(our_pos["position"]["szi"]) if our_pos else 0.0

            funding_history = fetch_recent_funding(args.coin, HISTORY_HOURS)
            current_funding = funding_history[-1] if funding_history else 0
            funding_history_sorted = sorted(funding_history)
            n = len(funding_history_sorted)
            hi = funding_history_sorted[int(n * args.high_quantile)] if n else 0
            lo = funding_history_sorted[int(n * args.low_quantile)] if n else 0
            apy_now = current_funding * 8760 * 100

            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            log_line = {"ts": ts, "equity": equity, "pos": our_size, "fr": current_funding,
                        "hi": hi, "lo": lo, "apy_now": apy_now}

            decision = "hold"
            if our_size == 0:
                if current_funding > 0 and current_funding >= hi:
                    decision = "open_short"  # short to receive positive funding
                elif current_funding < 0 and current_funding <= lo:
                    decision = "open_long"
            else:
                if our_size > 0 and current_funding >= 0:
                    decision = "close_long"
                elif our_size < 0 and current_funding <= 0:
                    decision = "close_short"

            log_line["decision"] = decision
            print(f"[{ts}] eq=${equity:.2f}  pos={our_size:+.4f}  fr={current_funding:+.6f} (apy {apy_now:+.1f}%)  hi={hi:+.6f} lo={lo:+.6f}  -> {decision}")

            if decision != "hold" and not args.dry_run:
                pnl_today = equity - starting_equity
                ok, reason = rm.allow_order(args.coin, args.usd, equity, pnl_today)
                if not ok:
                    print(f"  RISK BLOCKED: {reason}")
                    notify.send(f"⛔ {reason}", silent=True)
                else:
                    is_buy = decision in ("open_long", "close_short")
                    mid = fetch_mid(args.coin)
                    raw_size = args.usd / mid if "open" in decision else abs(our_size)
                    size = round_size(raw_size, sz_decimals)
                    # maker mode: rest order INSIDE the spread (away from mid) → rebate. Taker mode: cross.
                    if args.maker:
                        px = round_price(mid * (1 - 0.001 * (1 if is_buy else -1)), sz_decimals)
                        tif = "Alo"
                    else:
                        px = round_price(mid * (1 + 0.005 * (1 if is_buy else -1)), sz_decimals)
                        tif = "Ioc"
                    print(f"  -> {decision} {size} {args.coin} @ {px} ({'maker' if args.maker else 'taker'})")
                    try:
                        resp = exch.order(args.coin, is_buy, size, px, {"limit": {"tif": tif}})
                        log_line["order_resp"] = resp
                        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
                        if statuses and "filled" in statuses[0]:
                            f = statuses[0]["filled"]
                            fill_msg = f"✅ {decision} {f['totalSz']} {args.coin} @ ${f['avgPx']}"
                            print(f"  {fill_msg}")
                            notify.send(fill_msg)
                            rm.record_fill(args.coin, args.usd, args.usd * 0.00045, decision, 0)
                        else:
                            err_msg = f"❌ order error: {statuses}"
                            print(f"  {err_msg}")
                            notify.send(err_msg, silent=True)
                    except Exception as e:
                        print(f"  order exception: {e}")
                        notify.send(f"⚠️ order exception: {e}", silent=True)

            with log_path.open("a") as fp:
                fp.write(json.dumps(log_line) + "\n")
        except Exception as e:
            print(f"loop error: {e}")
            notify.send(f"⚠️ loop error: {e}", silent=True)

        time.sleep(args.poll_secs)


if __name__ == "__main__":
    main()
