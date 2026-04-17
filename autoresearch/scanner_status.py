#!/usr/bin/env python3
"""One-shot status report for the funding-scanner leg. Reads the latest
scan_*.jsonl, pulls current HL state for positions/orders, and prints a
clean summary — useful for ssh-checks and TG /status.
"""

import json
import sys
from pathlib import Path
from urllib import request

API = "https://api.hyperliquid.xyz/info"
LOG_DIR = Path(__file__).parent / "live_logs"


def post(b):
    r = request.Request(API, data=json.dumps(b).encode(),
                        headers={"Content-Type": "application/json"})
    with request.urlopen(r, timeout=10) as x:
        return json.loads(x.read())


def main():
    cfg_path = Path.home() / "hyperliquid-sol" / "config.json"
    addr = json.load(cfg_path.open())["wallet_address"]

    perp = post({"type": "clearinghouseState", "user": addr})
    equity = float(perp["marginSummary"]["accountValue"])
    orders = post({"type": "openOrders", "user": addr})
    mac = post({"type": "metaAndAssetCtxs"})
    meta, ctxs = mac[0], mac[1]
    funding_by = {m["name"]: float(c.get("funding", 0)) for m, c in zip(meta["universe"], ctxs)}
    mark_by = {m["name"]: float(c.get("markPx", 0)) for m, c in zip(meta["universe"], ctxs)}

    positions = []
    total_notional = 0.0
    funding_income_hr = 0.0
    for p in perp.get("assetPositions", []):
        pos = p["position"]
        coin = pos["coin"]
        szi = float(pos["szi"])
        if szi == 0:
            continue
        entry = float(pos.get("entryPx", 0))
        mark = mark_by.get(coin, 0)
        notional = abs(szi) * mark
        upnl = (mark - entry) * szi
        fr = funding_by.get(coin, 0)
        # funding income per hour on this position (sign: earn when szi*fr<0, lose when >0)
        fund_hr = -szi * mark * fr
        total_notional += notional
        funding_income_hr += fund_hr
        positions.append({
            "coin": coin, "szi": szi, "entry": entry, "mark": mark,
            "notional": notional, "upnl": upnl, "fr_apy": fr * 8760 * 100,
            "fund_hr_usd": fund_hr,
        })

    print(f"=== funding-scanner status ===")
    print(f"equity:           ${equity:.2f}")
    print(f"total_notional:   ${total_notional:.2f}")
    print(f"open_orders:      {len(orders)}")
    if orders:
        for o in orders:
            print(f"  {o['coin']:8s} {o['side']} {o['sz']} @ {o['limitPx']}")
    print(f"positions ({len(positions)}):")
    if not positions:
        print("  (flat)")
    total_upnl = 0
    for p in positions:
        total_upnl += p["upnl"]
        print(f"  {p['coin']:8s} szi={p['szi']:+10.3f} entry=${p['entry']:<10.4g} "
              f"mark=${p['mark']:<10.4g} uPnL=${p['upnl']:+.4f} "
              f"funding={p['fr_apy']:+6.1f}% APY = ${p['fund_hr_usd']:+.4f}/hr")
    print(f"uPnL total:       ${total_upnl:+.4f}")
    print(f"funding/hr:       ${funding_income_hr:+.4f}  (${funding_income_hr * 24:+.3f}/day if sustained)")

    # Latest scan tick
    latest = sorted(LOG_DIR.glob("scan_*.jsonl"))[-1:] if LOG_DIR.exists() else []
    if latest:
        with latest[0].open() as f:
            lines = f.readlines()
        if lines:
            last = json.loads(lines[-1])
            print(f"\nlast scan tick:   {last['ts']}")
            top = last.get("top_candidates", [])
            if top:
                print("  top candidates: " + ", ".join(
                    f"{t['coin']}({t['apy']:+.0f}% {t['dir']})" for t in top[:3]))


if __name__ == "__main__":
    main()
