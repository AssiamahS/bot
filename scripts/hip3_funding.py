#!/usr/bin/env python3
"""HIP-3 funding harvester — scans the 66 synthetic perps on HL (stocks,
commodities, indices) for extreme funding rates, enters to collect.

Reverse-engineered from leaderboard top trader 0x863b676e5e (account $1.3M,
all-time PnL +$4.85M). Their 300 recent fills were all `Open Short` on
xyz:BRENTOIL and xyz:CL — a systematic short-funding-collect on oil. This
script generalizes that across the full HIP-3 universe.

Entry rule:
  score_magnitude = |current_funding_rate - BASELINE|
  - If funding > +3x baseline: go SHORT (longs are paying, we collect)
  - If funding < -3x baseline: go LONG  (shorts are paying, we collect)
  percentile gate (last 7d): enters only when current funding is in top/
  bottom decile of recent history — avoids trading coins whose baseline
  is already extreme.

Exit rule (see scripts/hip3_check.py for the daily run):
  - funding normalized (|fr| < 1.5x baseline)
  - adverse price move >= 3% against us (stop loss)
  - 7 days elapsed

Self-healing:
  - Uses margin_helper.ensure_perp_margin — no spot/perps dance required
  - Skips markets where we already have an open position (no doubling)
  - Limit orders at mid +/- 100 bps slippage cap (wide spreads on HIP-3)

Sizing: 1% of equity per position, fixed_fractional with a 3% stop. At a
$61 account, $0.61 risk / 3% stop = $20 notional max per position. Cap
total HIP-3 deployment at 50% of equity.

Run from cron hourly (funding settles hourly on HL).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from strategies import sizing
from strategies.margin_helper import ensure_perp_margin


LOG_PATH = REPO / "hip3_trades.jsonl"
CONFIG_PATH = REPO / "config.json"
API = "https://api.hyperliquid.xyz/info"

BASELINE_HR = 1.25e-5       # HL minimum funding rate floor (hourly)
ENTRY_MULT = 3.0            # enter when |fr| >= ENTRY_MULT * baseline
EXIT_MULT = 1.5             # exit when |fr| < EXIT_MULT * baseline (used by hip3_check)
PERCENTILE_MIN = 80         # current funding must be in top 80th/bottom 20th pct of 7d history
STOP_PCT = 0.03
RISK_PER_TRADE = 0.01
MAX_TOTAL_DEPLOY_FRAC = 0.50
MAX_POSITIONS = 5
SLIPPAGE_BPS = 100          # HIP-3 spreads can be wide


def api(body: dict):
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def log(rec: dict):
    rec["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def fetch_hip3_universe() -> list[dict]:
    meta_ctxs = api({"type": "metaAndAssetCtxs", "dex": "xyz"})
    meta, ctxs = meta_ctxs[0], meta_ctxs[1]
    universe = meta["universe"]
    out = []
    for i, c in enumerate(universe):
        ctx = ctxs[i] if i < len(ctxs) else {}
        out.append({
            "name": c["name"],
            "szDecimals": c.get("szDecimals", 2),
            "maxLeverage": c.get("maxLeverage", 3),
            "mid": float(ctx.get("midPx") or 0),
            "mark": float(ctx.get("markPx") or 0),
            "oi": float(ctx.get("openInterest") or 0),
            "funding": float(ctx.get("funding") or 0),
            "premium": float(ctx.get("premium") or 0),
        })
    return out


def funding_percentile(coin: str, current_fr: float, hours: int = 168) -> float | None:
    try:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - hours * 3_600_000
        rows = api({"type": "fundingHistory", "coin": coin,
                    "startTime": start_ms, "endTime": end_ms})
        if len(rows) < 12:
            return None
        rates = sorted(float(r["fundingRate"]) for r in rows)
        # If positive extreme, want the top; if negative, bottom.
        if current_fr >= 0:
            rank = sum(1 for r in rates if r <= current_fr)
            return 100.0 * rank / len(rates)
        else:
            rank = sum(1 for r in rates if r >= current_fr)
            return 100.0 * rank / len(rates)
    except Exception:
        return None


def rank_candidates(universe: list[dict], min_oi_usd: float) -> list[dict]:
    cands = []
    for m in universe:
        if m["mid"] <= 0 or m["mark"] <= 0:
            continue
        oi_usd = m["oi"] * m["mid"]
        if oi_usd < min_oi_usd:
            continue
        fr = m["funding"]
        extremeness = abs(fr) - ENTRY_MULT * BASELINE_HR
        if extremeness <= 0:
            continue
        pct = funding_percentile(m["name"], fr)
        if pct is None or pct < PERCENTILE_MIN:
            continue  # coin's own history doesn't support this as extreme
        side = "short" if fr > 0 else "long"
        cands.append({**m, "oi_usd": oi_usd, "pct": pct, "side": side,
                      "score": extremeness * (pct / 100.0)})
    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands


def already_open(info, wallet: str) -> set[str]:
    state = info.user_state(wallet)
    return {p["position"]["coin"] for p in state.get("assetPositions", []) if float(p["position"].get("szi", 0)) != 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually place orders")
    ap.add_argument("--top", type=int, default=MAX_POSITIONS)
    ap.add_argument("--min-oi-usd", type=float, default=50_000.0)
    ap.add_argument("--risk-per-trade", type=float, default=RISK_PER_TRADE)
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    priv = cfg["wallet_private_key"]
    wallet = cfg.get("wallet_address") or Account.from_key(priv).address
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(Account.from_key(priv), constants.MAINNET_API_URL)

    state = info.user_state(wallet)
    equity = float(state["marginSummary"]["accountValue"])
    # Include spot USDC in total capital for sizing purposes (margin_helper
    # transfers it in for us automatically)
    spot_state = info.spot_user_state(wallet)
    spot_usdc = next((float(b["total"]) for b in spot_state.get("balances", []) if b["coin"] == "USDC"), 0.0)
    total_cap = equity + spot_usdc
    open_coins = already_open(info, wallet)
    print(f"wallet {wallet[:10]}…  perps=${equity:.2f}  spot=${spot_usdc:.2f}  total=${total_cap:.2f}")
    print(f"open positions: {open_coins or 'none'}")

    print("\n[scan] pulling HIP-3 universe...")
    uni = fetch_hip3_universe()
    print(f"[scan] {len(uni)} HIP-3 markets")
    cands = rank_candidates(uni, args.min_oi_usd)
    print(f"[scan] {len(cands)} pass entry+percentile filter\n")

    per_trade_risk = total_cap * args.risk_per_trade
    per_trade_notional = per_trade_risk / STOP_PCT  # fixed_fractional: risk / stop
    budget_total = total_cap * MAX_TOTAL_DEPLOY_FRAC
    print(f"sizing: ${per_trade_notional:.2f}/position (risk ${per_trade_risk:.2f} at {STOP_PCT*100:.0f}% stop)")
    print(f"budget: ${budget_total:.2f} total ({MAX_TOTAL_DEPLOY_FRAC*100:.0f}% of capital)")

    print(f"\ncandidates (top {args.top}):")
    print(f"  {'NAME':<18} {'SIDE':<6} {'FR(hr)':>10} {'PCT':>6} {'OI_USD':>12} {'MID':>10}")
    actions = []
    total_notional = 0.0
    for c in cands[: args.top]:
        if c["name"] in open_coins:
            print(f"  {c['name']:<18} already open, skip")
            continue
        if total_notional + per_trade_notional > budget_total:
            print(f"  {c['name']:<18} budget exhausted, skip")
            continue
        print(f"  {c['name']:<18} {c['side']:<6} {c['funding']*100:>+9.4f}% {c['pct']:>5.0f}% "
              f"${c['oi_usd']:>11,.0f} ${c['mid']:>9,.4f}")
        actions.append({
            "coin": c["name"], "side": c["side"], "mid": c["mid"],
            "szDecimals": c["szDecimals"], "notional": per_trade_notional,
            "funding": c["funding"], "pct": c["pct"],
        })
        total_notional += per_trade_notional

    if not actions:
        print("\nno entries this tick")
        log({"event": "hip3_scan", "entries": 0})
        return 0

    if not args.live:
        print(f"\nDRY RUN — pass --live to submit {len(actions)} orders (total ${total_notional:.2f})")
        return 0

    # Ensure enough perps margin for the whole basket (with 10% buffer)
    if not ensure_perp_margin(info, exchange, wallet, total_notional * 1.1, interactive=True):
        print("margin shortfall — abort")
        log({"event": "hip3_abort", "reason": "margin"})
        return 1

    print(f"\nLIVE — submitting {len(actions)} orders")
    for a in actions:
        coin = a["coin"]
        size = round(a["notional"] / a["mid"], a["szDecimals"])
        # Set leverage to 1x (minimum directional risk)
        try:
            exchange.update_leverage(1, coin, is_cross=True)
        except Exception as e:
            print(f"  {coin} update_leverage: {e}")
        # Limit price: for short, set below mid; for long, above mid. Use
        # aggressive-IOC-like limits to capture fill while bounding slippage.
        slip_frac = SLIPPAGE_BPS / 10000
        if a["side"] == "short":
            is_buy = False
            limit_px = a["mid"] * (1 - slip_frac)
        else:
            is_buy = True
            limit_px = a["mid"] * (1 + slip_frac)
        try:
            resp = exchange.order(coin, is_buy, size, limit_px,
                                  {"limit": {"tif": "Gtc"}}, reduce_only=False)
            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            status_info = statuses[0] if statuses else {}
            oid = status_info.get("resting", {}).get("oid") or status_info.get("filled", {}).get("oid")
            print(f"  {coin:<18} {a['side']:<6} size={size} limit={limit_px:.4f} oid={oid}")
            log({"event": "hip3_entry", **a, "size": size, "limit": limit_px,
                 "oid": oid, "status": list(status_info)})
        except Exception as e:
            print(f"  {coin:<18} FAIL: {e}")
            log({"event": "hip3_entry_failed", **a, "error": str(e)})

    return 0


if __name__ == "__main__":
    sys.exit(main())
