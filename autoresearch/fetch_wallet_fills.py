#!/usr/bin/env python3
"""Pull a wallet's full fill history from HL public API. Paginates back through time.

Output: data/fills/<addr>.csv with columns:
    time, coin, side, px, sz, dir, closedPnl, fee, hash

Usage:
    python3 fetch_wallet_fills.py 0xcf67...    # one wallet
    python3 fetch_wallet_fills.py --top 5      # top 5 from leaderboard
"""

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

API = "https://api.hyperliquid.xyz/info"
LB = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
DATA_DIR = Path(__file__).parent / "data" / "fills"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def post(body: dict) -> any:
    req = request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get(url: str) -> any:
    with request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def fetch_fills(addr: str, max_pages: int = 20) -> list:
    """Paginate userFillsByTime backwards. Each call returns up to 2000 fills."""
    now = int(time.time() * 1000)
    end_time = now
    start_time = now - 365 * 86_400_000  # request up to 1 year of history
    out, seen = [], set()
    for page in range(max_pages):
        body = {"type": "userFillsByTime", "user": addr,
                "startTime": start_time, "endTime": end_time}
        try:
            chunk = post(body)
        except Exception as e:
            print(f"  page {page}: {e}")
            break
        if not chunk:
            break
        new = [f for f in chunk if f.get("hash") not in seen]
        for f in new:
            seen.add(f.get("hash"))
        out.extend(new)
        oldest = min(f["time"] for f in chunk)
        if oldest >= end_time or len(new) == 0 or len(chunk) < 2000:
            break
        end_time = oldest - 1
        time.sleep(0.2)
    out.sort(key=lambda f: f["time"])
    return out


def write_fills(addr: str, fills: list) -> Path:
    path = DATA_DIR / f"{addr}.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "datetime", "coin", "side", "px", "sz", "dir", "closedPnl", "fee", "hash"])
        for x in fills:
            ts = datetime.fromtimestamp(x["time"] / 1000, tz=timezone.utc).isoformat()
            w.writerow([
                x["time"], ts, x.get("coin", ""), x.get("side", ""),
                x.get("px", ""), x.get("sz", ""), x.get("dir", ""),
                x.get("closedPnl", ""), x.get("fee", ""), x.get("hash", ""),
            ])
    return path


def summarize(fills: list) -> dict:
    if not fills:
        return {"trades": 0}
    realized = sum(float(f.get("closedPnl", 0) or 0) for f in fills)
    fees = sum(float(f.get("fee", 0) or 0) for f in fills)
    coins = {}
    for f in fills:
        c = f.get("coin", "?")
        pnl = float(f.get("closedPnl", 0) or 0)
        coins[c] = coins.get(c, 0) + pnl
    top_coins = sorted(coins.items(), key=lambda x: x[1], reverse=True)[:5]
    return {
        "trades": len(fills),
        "first": datetime.fromtimestamp(fills[0]["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "last": datetime.fromtimestamp(fills[-1]["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "realized_pnl": round(realized, 2),
        "total_fees": round(fees, 2),
        "top_coins": top_coins,
    }


def top_wallets(n: int) -> list:
    rows = get(LB)["leaderboardRows"]
    enriched = []
    for r in rows:
        try:
            acct = float(r["accountValue"])
        except (TypeError, ValueError):
            continue
        for w, p in r["windowPerformances"]:
            if w == "month":
                try:
                    roi = float(p.get("roi", 0))
                    vol = float(p.get("vlm", 0))
                    if vol > 1_000_000 and acct > 10_000:
                        enriched.append((roi, r["ethAddress"], acct))
                except (TypeError, ValueError):
                    pass
                break
    enriched.sort(reverse=True)
    return [(addr, acct) for _, addr, acct in enriched[:n]]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("addrs", nargs="*", help="wallet addresses")
    p.add_argument("--top", type=int, default=0, help="pull top N from leaderboard instead")
    p.add_argument("--max-pages", type=int, default=20)
    args = p.parse_args()

    targets = []
    if args.top:
        targets = top_wallets(args.top)
    if args.addrs:
        targets += [(a, 0) for a in args.addrs]
    if not targets:
        print("usage: pass addresses or --top N")
        return

    for addr, acct in targets:
        print(f"fetching {addr}{f' (account ${acct:,.0f})' if acct else ''}...")
        fills = fetch_fills(addr, args.max_pages)
        path = write_fills(addr, fills)
        s = summarize(fills)
        if s["trades"] == 0:
            print(f"  no fills")
            continue
        print(f"  {s['trades']} fills · {s['first']} → {s['last']} · realized ${s['realized_pnl']:,.2f} · fees ${s['total_fees']:,.2f}")
        print(f"  top coins: {', '.join(f'{c}:${v:,.0f}' for c,v in s['top_coins'])}")
        print(f"  -> {path.name}")


if __name__ == "__main__":
    main()
