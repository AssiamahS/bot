#!/usr/bin/env python3
"""Funding-rate scanner leg. One process, scans ALL HL perps every poll,
opens positions on the biggest |funding - baseline| extremes to harvest funding.

Rank logic each tick:
  score = |current_funding - baseline|   (baseline ~ 1.25e-5/hr, HL floor)
  filter: openInterest_usd >= --min-oi-usd (liquidity)
  select: up to --max-positions entries, split between long-funding-collect
          (negative funding) and short-funding-collect (positive funding above baseline)

Exit when funding normalizes (|fr| < exit_multiple * baseline) OR adverse
price move >= --stop-pct OR rm kill.

Single position notional = --usd-per-pos. Total notional capped at
--max-total-usd. Maker-only by default (post-only, rebate).
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

import notify
from risk_manager import RiskManager

API = "https://api.hyperliquid.xyz/info"
LOG_DIR = Path(__file__).parent / "live_logs"
LOG_DIR.mkdir(exist_ok=True)

BASELINE_HR = 1.25e-5  # HL minimum funding rate floor
ENTRY_MULT = 3.0       # enter when |fr - baseline| >= ENTRY_MULT * baseline
EXIT_MULT = 1.5        # exit when |fr| < EXIT_MULT * baseline


def post(body):
    req = request.Request(API, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_meta_and_ctxs():
    """Single call returns [meta, assetCtxs[]] for every perp."""
    return post({"type": "metaAndAssetCtxs"})


def fetch_funding_percentile(coin, current_fr, hours=168):
    """Return percentile (0-100) of current_fr within last N hours of this coin's
    own funding history. Used to gate: a coin whose baseline is already extreme
    shouldn't trigger on its normal level."""
    try:
        end = int(time.time() * 1000)
        start = end - hours * 3_600_000
        rows = post({"type": "fundingHistory", "coin": coin,
                     "startTime": start, "endTime": end})
        rates = sorted(float(r["fundingRate"]) for r in rows)
        if len(rates) < 8:
            return None  # too little history to judge
        if current_fr >= 0:
            # short-funding-collect: want current to be HIGH percentile
            rank = sum(1 for r in rates if r <= current_fr)
        else:
            # long-funding-collect: want current to be LOW percentile
            rank = sum(1 for r in rates if r >= current_fr)
        return 100.0 * (1 - rank / len(rates)) if current_fr < 0 else 100.0 * rank / len(rates)
    except Exception:
        return None


def fetch_unified_equity(addr):
    perp = post({"type": "clearinghouseState", "user": addr})
    perp_eq = float(perp["marginSummary"]["accountValue"])
    spot = post({"type": "spotClearinghouseState", "user": addr})
    usdc = 0.0
    for b in spot.get("balances", []):
        if b["coin"] == "USDC":
            usdc = float(b["total"])
            break
    return perp_eq + usdc, perp


def round_size(raw, sz_decimals):
    if sz_decimals <= 0:
        return float(int(raw))
    return round(raw, sz_decimals)


def round_price(raw, sz_decimals):
    px_decimals = max(0, 6 - sz_decimals)
    return round(float(f"{raw:.5g}"), px_decimals)


def rank_candidates(meta, ctxs, min_oi_usd):
    """Return sorted list of {coin, funding, mark, score, direction} dicts.
    direction = 'long' to collect negative funding, 'short' to collect positive."""
    out = []
    for m, c in zip(meta["universe"], ctxs):
        # skip delisted / HIP-3 markets here (no ':' in plain names)
        coin = m["name"]
        if ":" in coin:  # HIP-3 dex markets have separate dispatcher
            continue
        try:
            fr = float(c.get("funding", 0))
            mark = float(c.get("markPx", 0))
            oi_units = float(c.get("openInterest", 0))
        except (TypeError, ValueError):
            continue
        oi_usd = oi_units * mark
        if oi_usd < min_oi_usd or mark <= 0:
            continue
        excess = fr - BASELINE_HR  # positive = longs overpaying, negative = shorts overpaying
        score = abs(excess)
        if score < ENTRY_MULT * BASELINE_HR:
            continue  # not extreme enough
        direction = "short" if excess > 0 else "long"
        out.append({
            "coin": coin,
            "funding": fr,
            "excess": excess,
            "mark": mark,
            "oi_usd": oi_usd,
            "score": score,
            "direction": direction,
            "sz_decimals": m["szDecimals"],
            "apy_pct": fr * 8760 * 100,
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def open_positions(meta):
    """Map coin -> szi (signed size)."""
    out = {}
    for p in meta.get("assetPositions", []):
        pos = p["position"]
        out[pos["coin"]] = float(pos.get("szi", 0))
    return out


def fetch_open_orders(addr):
    """Returns set of coins with any resting order (used to prevent duplicate
    entries while a prior maker order is still working)."""
    orders = post({"type": "openOrders", "user": addr})
    coins = set()
    for o in orders or []:
        coins.add(o.get("coin"))
    return coins


def place_order(exch, coin, is_buy, size, mark, sz_decimals, maker, maker_bps=3.0):
    if maker:
        offset = maker_bps / 10000.0
        px = round_price(mark * (1 - offset * (1 if is_buy else -1)), sz_decimals)
        tif = "Alo"
    else:
        px = round_price(mark * (1 + 0.005 * (1 if is_buy else -1)), sz_decimals)
        tif = "Ioc"
    return exch.order(coin, is_buy, size, px, {"limit": {"tif": tif}}), px, tif


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--usd-per-pos", type=float, default=15.0)
    p.add_argument("--max-positions", type=int, default=2)
    p.add_argument("--max-total-usd", type=float, default=30.0)
    p.add_argument("--min-oi-usd", type=float, default=5_000_000.0)
    p.add_argument("--stop-pct", type=float, default=5.0)
    p.add_argument("--poll-secs", type=int, default=60)
    p.add_argument("--pctile-min", type=float, default=80.0,
                   help="require current funding to be in top N pct of coin's own 7d history")
    p.add_argument("--maker-bps", type=float, default=3.0,
                   help="maker price offset from mid in bps (default 3bps = quick fills)")
    p.add_argument("--max-daily-loss-pct", type=float, default=3.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--maker", action="store_true", default=True)
    p.add_argument("--taker", dest="maker", action="store_false")
    args = p.parse_args()

    cfg = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())
    wallet = Account.from_key(cfg["wallet_private_key"])
    addr = cfg["wallet_address"]
    rm = RiskManager(max_daily_loss_pct=args.max_daily_loss_pct, max_position_pct=50.0)

    # Retry init
    info = exch = None
    delay = 5
    for attempt in range(8):
        try:
            info = Info(constants.MAINNET_API_URL, skip_ws=True)
            exch = Exchange(wallet, constants.MAINNET_API_URL, account_address=addr)
            break
        except Exception as e:
            print(f"init attempt {attempt+1}: {e}, sleep {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 120)
    if not exch:
        print("init failed, exiting")
        return

    notify.send(f"🤖 *funding-scanner started*\nnotional/pos: `${args.usd_per_pos}` · max pos: `{args.max_positions}` · cap: `${args.max_total_usd}`\ndry-run: `{args.dry_run}` · maker: `{args.maker}`")

    # entry_px per coin for stop-loss tracking
    entry_px = {}
    starting_equity = 0.0
    log_path = LOG_DIR / f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

    while True:
        try:
            if rm.is_killed():
                notify.send(f"🛑 *scanner killed*\n{rm.kill_file.read_text()}")
                return

            meta_ctxs = fetch_meta_and_ctxs()
            meta, ctxs = meta_ctxs[0], meta_ctxs[1]
            equity, perp_state = fetch_unified_equity(addr)
            if starting_equity == 0:
                starting_equity = equity
            pnl_today = equity - starting_equity

            positions = open_positions(perp_state)
            pending_coins = fetch_open_orders(addr)  # resting maker orders
            total_open_usd = sum(abs(s) * float(next(
                (c["markPx"] for m, c in zip(meta["universe"], ctxs) if m["name"] == k), 0
            )) for k, s in positions.items())

            candidates = rank_candidates(meta, ctxs, args.min_oi_usd)
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # === EXIT PASS: close positions that are no longer extreme or hit stop ===
            closed_this_tick = []
            for coin, szi in list(positions.items()):
                if szi == 0:
                    continue
                ctx = next((c for m, c in zip(meta["universe"], ctxs) if m["name"] == coin), None)
                if not ctx:
                    continue
                fr = float(ctx.get("funding", 0))
                mark = float(ctx.get("markPx", 0))
                ep = entry_px.get(coin, mark)
                adverse_pct = ((mark - ep) / ep * 100.0) if szi > 0 else ((ep - mark) / ep * 100.0)

                should_exit = False
                reason = ""
                if abs(fr) < EXIT_MULT * BASELINE_HR:
                    should_exit = True
                    reason = f"funding normalized fr={fr:.2e}"
                elif adverse_pct <= -args.stop_pct:
                    should_exit = True
                    reason = f"stop-loss {adverse_pct:.2f}%"
                # funding flipped sign vs our direction = exit (thesis broken)
                elif (szi > 0 and fr > 0) or (szi < 0 and fr < 0):
                    should_exit = True
                    reason = f"funding flipped fr={fr:.2e}"

                if should_exit and not args.dry_run:
                    sz_decimals = next(m["szDecimals"] for m in meta["universe"] if m["name"] == coin)
                    is_buy = szi < 0  # closing a short = buy back
                    try:
                        resp, px, tif = place_order(exch, coin, is_buy, abs(szi), mark,
                                                    sz_decimals, args.maker,
                                                    maker_bps=args.maker_bps)
                        closed_this_tick.append({"coin": coin, "reason": reason, "px": px, "resp_type": str(resp)[:120]})
                        notify.send(f"↩️ *close* `{coin}` szi={szi:+.4f} @ ${px}\nreason: {reason}")
                        entry_px.pop(coin, None)
                    except Exception as e:
                        notify.send(f"⚠️ close error {coin}: {e}", silent=True)
                elif should_exit and args.dry_run:
                    closed_this_tick.append({"coin": coin, "reason": reason, "dry": True})

            # === ENTRY PASS: open new on top candidates if under cap ===
            opened_this_tick = []
            room = args.max_positions - sum(1 for v in positions.values() if v != 0) + len(closed_this_tick)
            budget_left_usd = args.max_total_usd - total_open_usd
            for c in candidates:
                if room <= 0 or budget_left_usd < args.usd_per_pos:
                    break
                if c["coin"] in positions and positions[c["coin"]] != 0:
                    continue  # already in this coin
                if c["coin"] in pending_coins:
                    continue  # resting maker order still working — don't double up
                # Per-coin percentile gate: current rate must be in top N pct of its own history
                pct = fetch_funding_percentile(c["coin"], c["funding"])
                if pct is not None and pct < args.pctile_min:
                    continue
                ok, rm_reason = rm.allow_order(c["coin"], args.usd_per_pos, equity, pnl_today)
                if not ok:
                    continue
                is_buy = c["direction"] == "long"
                raw_size = args.usd_per_pos / c["mark"]
                size = round_size(raw_size, c["sz_decimals"])
                if size <= 0:
                    continue
                if args.dry_run:
                    opened_this_tick.append({"coin": c["coin"], "dir": c["direction"],
                                             "apy": c["apy_pct"], "pct": pct, "dry": True})
                    room -= 1
                    budget_left_usd -= args.usd_per_pos
                    continue
                try:
                    resp, px, tif = place_order(exch, c["coin"], is_buy, size, c["mark"],
                                                c["sz_decimals"], args.maker,
                                                maker_bps=args.maker_bps)
                    opened_this_tick.append({"coin": c["coin"], "dir": c["direction"],
                                             "apy": c["apy_pct"], "px": px, "size": size})
                    entry_px[c["coin"]] = c["mark"]
                    notify.send(
                        f"🎯 *open {c['direction']}* `{c['coin']}`\nsize: {size} @ ${px} (`{tif}`)\nfunding: `{c['funding']:+.2e}` ({c['apy_pct']:+.0f}% APY)\nOI: `${c['oi_usd']/1e6:.1f}M`"
                    )
                    rm.record_fill(c["coin"], args.usd_per_pos, args.usd_per_pos * 0.00045, f"open_{c['direction']}", 0)
                    room -= 1
                    budget_left_usd -= args.usd_per_pos
                except Exception as e:
                    notify.send(f"⚠️ open error {c['coin']}: {e}", silent=True)

            # Log
            top3 = candidates[:3]
            log_line = {
                "ts": ts, "equity": round(equity, 4), "total_open_usd": round(total_open_usd, 2),
                "pnl_today": round(pnl_today, 4),
                "positions": {k: round(v, 6) for k, v in positions.items() if v != 0},
                "top_candidates": [{"coin": c["coin"], "dir": c["direction"], "apy": round(c["apy_pct"], 1),
                                    "oi_m": round(c["oi_usd"] / 1e6, 1)} for c in top3],
                "opened": opened_this_tick, "closed": closed_this_tick,
            }
            with log_path.open("a") as f:
                f.write(json.dumps(log_line) + "\n")
            top_str = ", ".join(
                "{}({:+.0f}% {})".format(c["coin"], c["apy_pct"], c["direction"])
                for c in top3
            ) if top3 else "none"
            print(f"[{ts}] eq=${equity:.2f} open=${total_open_usd:.2f} pnl=${pnl_today:+.3f} "
                  f"top: {top_str} | opened={len(opened_this_tick)} closed={len(closed_this_tick)}")

            time.sleep(args.poll_secs)
        except KeyboardInterrupt:
            print("interrupted")
            return
        except Exception as e:
            print(f"loop error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
