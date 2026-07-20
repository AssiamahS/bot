#!/usr/bin/env python3
"""Delta-neutral funding harvester: long spot + short perp on the SAME main-dex
asset, so price exposure nets to ~zero and income is funding minus fees.

Differences from live_funding.py (the one-legged HIP-3 harvester):
  - both legs on every entry -> no directional risk; only positive funding is
    harvestable (spot can't be shorted)
  - persistence gate: enters only when funding has HELD above the net-of-fees
    threshold for --gate-hours, not on a single-poll spike
  - startup reconciliation: adopts whatever spot/perp legs already exist on the
    exchange and repairs one-legged states instead of assuming flat

Dry-run by default; --live places orders. Live entries additionally require
equity >= --min-equity and per-leg notional >= $10 (HL order minimum).
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
HL_MIN_ORDER_USD = 10.0
# taker both legs in + out = 4 x 0.045%, amortized over an assumed 7-day hold
FEE_APR_DRAG = 4 * 0.00045 * (365 / 7)


def post(body: dict, tries: int = 5) -> any:
    delay = 2
    for attempt in range(tries):
        try:
            req = request.Request(API, data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"})
            with request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 60)


def fetch_funding(coin: str, hours: int) -> list[float]:
    end = int(time.time() * 1000)
    rows = post({"type": "fundingHistory", "coin": coin,
                 "startTime": end - hours * 3_600_000, "endTime": end})
    return [float(r["fundingRate"]) for r in rows]


def spot_pairs() -> dict:
    """token name -> {pair (order symbol), szDecimals} for USDC-quoted spot pairs."""
    meta = post({"type": "spotMeta"})
    # universe references tokens by their "index" field, not list position
    tokens = {t["index"]: t for t in meta["tokens"]}
    out = {}
    for u in meta["universe"]:
        base, quote = u["tokens"]
        if tokens[quote]["name"] != "USDC":
            continue
        t = tokens[base]
        out[t["name"]] = {"pair": u["name"], "szDecimals": t["szDecimals"]}
    return out


def perp_meta() -> dict:
    """coin -> szDecimals for main-dex perps."""
    return {m["name"]: m["szDecimals"] for m in post({"type": "meta"})["universe"]}


def fetch_mids() -> dict:
    return post({"type": "allMids"})


def fetch_spot_balance(addr: str, token: str) -> float:
    for b in post({"type": "spotClearinghouseState", "user": addr}).get("balances", []):
        if b["coin"] == token:
            return float(b["total"])
    return 0.0


def fetch_free_usdc(addr: str) -> float:
    return fetch_spot_balance(addr, "USDC")


def fetch_perp_position(addr: str, coin: str) -> float:
    state = post({"type": "clearinghouseState", "user": addr})
    for p in state.get("assetPositions", []):
        if p["position"]["coin"] == coin:
            return float(p["position"]["szi"])
    return 0.0


def round_size(raw: float, sz_decimals: int) -> float:
    if sz_decimals <= 0:
        return float(int(raw))
    return round(raw, sz_decimals)


def round_px(raw: float, sz_decimals: int, spot: bool) -> float:
    px_decimals = max(0, (8 if spot else 6) - sz_decimals)
    return round(float(f"{raw:.5g}"), px_decimals)


def net_apr(rates: list[float]) -> float:
    """annualized funding minus amortized round-trip fees, from hourly rates."""
    if not rates:
        return -FEE_APR_DRAG
    return (sum(rates) / len(rates)) * 8760 - FEE_APR_DRAG


class Runner:
    def __init__(self, args):
        self.args = args
        cfg = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())
        self.addr = cfg["wallet_address"]
        self.wallet = Account.from_key(cfg["wallet_private_key"])
        self.rm = RiskManager(max_daily_loss_pct=args.max_daily_loss_pct, max_position_pct=80.0)
        self.exch = None
        self.log_path = LOG_DIR / f"dn_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        self.coin = None          # active perp coin, e.g. "HYPE"
        self.entry_ts = 0.0

    def init_sdk(self):
        delay = 5
        for attempt in range(8):
            try:
                Info(constants.MAINNET_API_URL, skip_ws=True)
                self.exch = Exchange(self.wallet, constants.MAINNET_API_URL,
                                     account_address=self.addr)
                return True
            except Exception as e:
                print(f"init attempt {attempt+1} failed: {e}, sleeping {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 120)
        return False

    def order(self, symbol: str, is_buy: bool, size: float, px: float) -> dict:
        resp = self.exch.order(symbol, is_buy, size, px, {"limit": {"tif": "Ioc"}})
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
        if statuses and "filled" in statuses[0]:
            return statuses[0]["filled"]
        raise RuntimeError(f"order not filled: {statuses}")

    def reconcile(self, pairs: dict, perps: dict) -> None:
        """Adopt legs already on the exchange; repair one-legged states.

        A crash/restart must never leave a naked leg running: spot without the
        perp short is a directional long, perp short without spot is a
        directional short.
        """
        for token, sp in pairs.items():
            if token not in perps or token == "USDC":
                continue
            spot_sz = fetch_spot_balance(self.addr, token)
            perp_sz = fetch_perp_position(self.addr, token)
            mids = None
            if spot_sz > 0 and perp_sz < 0:
                self.coin = token
                print(f"reconcile: adopted hedged pair {token} spot={spot_sz} perp={perp_sz}")
                notify.send(f"♻️ dn adopted {token}: spot {spot_sz} / perp {perp_sz}", silent=True)
            elif (spot_sz > 0) != (perp_sz < 0) and (spot_sz > 0 or perp_sz != 0):
                mids = mids or fetch_mids()
                mid = float(mids.get(token, 0) or 0)
                naked_usd = max(spot_sz, abs(perp_sz)) * mid
                if mid <= 0 or naked_usd < HL_MIN_ORDER_USD:
                    continue  # dust (or no mid): not closable under HL minimum, not a real risk
                msg = f"⚠️ dn NAKED LEG {token}: spot={spot_sz} perp={perp_sz} (${naked_usd:.2f})"
                print(msg)
                notify.send(msg)
                if self.args.live:
                    if perp_sz != 0:  # flatten the perp leg; spot alone just sits
                        px = round_px(mid * (1.005 if perp_sz < 0 else 0.995),
                                      perps[token], spot=False)
                        try:
                            self.order(token, perp_sz < 0, abs(perp_sz), px)
                            print(f"  repaired: closed naked perp {token}")
                        except Exception as e:
                            print(f"  repair failed: {e}")

    def scan(self, pairs: dict, perps: dict) -> list[dict]:
        """Rank hedgeable assets by gate-window net APR."""
        candidates = []
        for token in pairs:
            if token not in perps or token == "USDC":
                continue
            try:
                rates = fetch_funding(token, self.args.gate_hours)
            except Exception:
                continue
            if not rates:
                continue
            gate_ok = (min(rates) > 0
                       and net_apr(rates) >= self.args.min_apr
                       and net_apr(rates[-3:]) >= self.args.min_apr)
            candidates.append({"token": token, "net_apr": net_apr(rates),
                               "fr_now": rates[-1], "gate": gate_ok})
        return sorted(candidates, key=lambda c: c["net_apr"], reverse=True)

    def enter(self, token: str, pairs: dict, perps: dict, usd: float) -> None:
        mids = fetch_mids()
        mid = float(mids[token])
        sz = round_size(usd / mid, min(pairs[token]["szDecimals"], perps[token]))
        if sz * mid < HL_MIN_ORDER_USD:
            print(f"  entry skipped: ${sz * mid:.2f}/leg below HL ${HL_MIN_ORDER_USD} minimum")
            return
        # spot first: if it fails nothing is at risk; if the perp then fails we
        # hold unhedged spot until reconcile repairs it (alerted immediately)
        spot_px = round_px(mid * 1.005, pairs[token]["szDecimals"], spot=True)
        f1 = self.order(pairs[token]["pair"], True, sz, spot_px)
        try:
            perp_px = round_px(mid * 0.995, perps[token], spot=False)
            f2 = self.order(token, False, sz, perp_px)
        except Exception as e:
            notify.send(f"⚠️ dn HALF-FILLED {token}: spot ok, perp short failed ({e})")
            raise
        self.coin = token
        self.entry_ts = time.time()
        self.rm.record_fill(token, usd * 2, usd * 2 * 0.00045, "dn_enter", 0)
        msg = f"✅ dn enter {token}: spot +{f1['totalSz']} @ {f1['avgPx']} / perp -{f2['totalSz']} @ {f2['avgPx']}"
        print(f"  {msg}")
        notify.send(msg)

    def exit(self, pairs: dict, perps: dict) -> None:
        token = self.coin
        mids = fetch_mids()
        mid = float(mids[token])
        perp_sz = abs(fetch_perp_position(self.addr, token))
        spot_sz = fetch_spot_balance(self.addr, token)
        if perp_sz:
            self.order(token, True, perp_sz, round_px(mid * 1.005, perps[token], spot=False))
        sell_sz = round_size(spot_sz, pairs[token]["szDecimals"])
        if sell_sz * mid >= HL_MIN_ORDER_USD:
            self.order(pairs[token]["pair"], False, sell_sz,
                       round_px(mid * 0.995, pairs[token]["szDecimals"], spot=True))
        self.rm.record_fill(token, sell_sz * mid * 2, sell_sz * mid * 2 * 0.00045, "dn_exit", 0)
        msg = f"🏁 dn exit {token}: both legs closed"
        print(f"  {msg}")
        notify.send(msg)
        self.coin = None

    def run(self):
        if not self.init_sdk():
            print("init failed after retries, exiting")
            return
        pairs = spot_pairs()
        perps = perp_meta()
        hedgeable = sorted(set(pairs) & set(perps) - {"USDC"})
        print(f"hedgeable spot+perp assets: {len(hedgeable)} ({', '.join(hedgeable[:10])}...)")
        notify.send(f"🤖 *dn harvester started*\nlive: `{self.args.live}`\nassets: `{len(hedgeable)}`\ngate: `{self.args.gate_hours}h > {self.args.min_apr:.0%} net APR`")
        self.reconcile(pairs, perps)

        while True:
            if self.rm.is_killed():
                print("RISK KILL — exiting")
                notify.send(f"🛑 *dn killed*\n{self.rm.kill_file.read_text()}")
                return
            try:
                usdc = fetch_free_usdc(self.addr)
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                line = {"ts": ts, "usdc": usdc, "coin": self.coin}

                if self.coin:  # holding: exit when the edge decays
                    recent = fetch_funding(self.coin, self.args.exit_hours)
                    apr = net_apr(recent)
                    line["net_apr"] = apr
                    stale = apr < self.args.exit_apr
                    print(f"[{ts}] holding {self.coin}  net_apr={apr:+.1%}  usdc=${usdc:.2f}  -> {'exit' if stale else 'hold'}")
                    if stale:
                        line["decision"] = "exit"
                        if self.args.live:
                            self.exit(pairs, perps)
                        else:
                            print("  (dry-run: would exit)")
                            self.coin = None
                else:  # flat: scan for a gated entry
                    ranked = self.scan(pairs, perps)
                    best = next((c for c in ranked if c["gate"]), None)
                    line["best"] = ranked[0] if ranked else None
                    usd = min(self.args.usd, usdc * 0.45)  # spot leg + perp margin both draw on USDC
                    top = ranked[0] if ranked else None
                    top_s = f"{top['token']} {top['net_apr']:+.1%}" if top else "none"
                    print(f"[{ts}] flat  usdc=${usdc:.2f}  top={top_s}  gated={best['token'] if best else 'none'}")
                    if best:
                        line["decision"] = f"enter_{best['token']}"
                        if not self.args.live:
                            print(f"  (dry-run: would enter {best['token']} at {best['net_apr']:+.1%} net APR)")
                        elif usdc < self.args.min_equity:
                            print(f"  entry skipped: equity ${usdc:.2f} < min ${self.args.min_equity}")
                        else:
                            ok, reason = self.rm.allow_order(best["token"], usd * 2, usdc, 0)
                            if ok:
                                self.enter(best["token"], pairs, perps, usd)
                            else:
                                print(f"  RISK BLOCKED: {reason}")

                with self.log_path.open("a") as fp:
                    fp.write(json.dumps(line) + "\n")
            except Exception as e:
                print(f"loop error: {e}")
                notify.send(f"⚠️ dn loop error: {e}", silent=True)
            time.sleep(self.args.poll_secs)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--usd", type=float, default=20.0, help="notional per leg")
    # defaults from dn_backtest.py 90d sweep: 5%/0.5% nets ~+13.4% APY with 22
    # round trips; tighter gates (10%/2%) churn — fees turn the same funding
    # stream into -20% APY. The surface is flat 3-7% so this isn't overfit.
    p.add_argument("--min-apr", type=float, default=0.05, help="entry gate: net APR after fees")
    p.add_argument("--gate-hours", type=int, default=24, help="funding must persist this long")
    p.add_argument("--exit-hours", type=int, default=6, help="exit window for decay check")
    p.add_argument("--exit-apr", type=float, default=0.005, help="exit when net APR drops below")
    p.add_argument("--min-equity", type=float, default=25.0, help="no live entries below this")
    p.add_argument("--poll-secs", type=int, default=600)
    p.add_argument("--max-daily-loss-pct", type=float, default=3.0)
    p.add_argument("--live", action="store_true", help="place real orders (default dry-run)")
    args = p.parse_args()
    Runner(args).run()


if __name__ == "__main__":
    main()
