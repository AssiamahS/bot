#!/usr/bin/env python3
"""Diagnose WHERE your Hyperliquid USDC actually lives. Read-only, no key."""
from __future__ import annotations
import json, sys
from hyperliquid.info import Info
from hyperliquid.utils import constants

DEFAULT_ADDRESS = "0x253831C30282760880c4833E600e897f07AdC5aE"

def usdc_from_spot(spot_state):
    for bal in spot_state.get("balances", []):
        if bal.get("coin") == "USDC":
            return float(bal.get("total", 0.0) or 0.0)
    return 0.0

def summarize_perp(state):
    ms = state.get("marginSummary", {}) or {}
    positions = []
    for ap in state.get("assetPositions", []):
        p = ap.get("position", {}) or {}
        szi = float(p.get("szi", 0) or 0)
        if szi != 0:
            positions.append({"coin": p.get("coin"), "szi": szi,
                "entryPx": p.get("entryPx"), "unrealizedPnl": p.get("unrealizedPnl"),
                "positionValue": p.get("positionValue")})
    return {"accountValue": float(ms.get("accountValue", 0) or 0),
            "totalMarginUsed": float(ms.get("totalMarginUsed", 0) or 0),
            "withdrawable": float(state.get("withdrawable", 0) or 0),
            "open_positions": positions}

def main():
    address = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ADDRESS
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    print(f"Address: {address}\n")
    dex_names = []
    print("=== Builder perp DEXs (HIP-3) ===")
    try:
        for d in info.perp_dexs():
            if not d:
                continue
            print("  " + json.dumps(d))
            if d.get("name"):
                dex_names.append(d["name"])
    except Exception as e:
        print(f"  perp_dexs() error: {e}")
    print()
    total = 0.0
    main_s = summarize_perp(info.user_state(address, dex=""))
    total += main_s["accountValue"]
    print('=== MAIN PERPS (dex="") ===')
    print(f"  accountValue : ${main_s['accountValue']:.2f}")
    print(f"  withdrawable : ${main_s['withdrawable']:.2f}")
    print(f"  marginUsed   : ${main_s['totalMarginUsed']:.2f}")
    for p in main_s["open_positions"]:
        print(f"    position {p['coin']} szi={p['szi']} uPnL={p['unrealizedPnl']}")
    print()
    spot_usdc = usdc_from_spot(info.spot_user_state(address))
    total += spot_usdc
    print(f"=== SPOT ===\n  USDC : ${spot_usdc:.2f}\n")
    builder = {}
    for name in dex_names:
        try:
            s = summarize_perp(info.user_state(address, dex=name))
        except Exception as e:
            print(f"=== DEX {name!r} === error: {e}\n")
            continue
        if s["accountValue"] > 0 or s["open_positions"]:
            builder[name] = s
            total += s["accountValue"]
            print(f"=== DEX {name!r} ===")
            print(f"  accountValue : ${s['accountValue']:.2f}")
            print(f"  withdrawable : ${s['withdrawable']:.2f}")
            print(f"  marginUsed   : ${s['totalMarginUsed']:.2f}")
            for p in s["open_positions"]:
                print(f"    position {p['coin']} szi={p['szi']} uPnL={p['unrealizedPnl']} value={p['positionValue']}")
            print()
    print("=" * 52)
    print(f"TOTAL across all accounts: ${total:.2f}")
    print("=" * 52)
    print("\n=== WHAT TO DO NEXT ===")
    if builder:
        for name, s in builder.items():
            if s["open_positions"]:
                print(f"DEX {name!r} has OPEN positions — flatten to 0 first; only free margin moves out.")
            print(f"DEX {name!r}: ${s['withdrawable']:.2f} is free to move to main account.")
        first = next(iter(builder))
        print(f"\nRun: python3 scripts/cashout.py --dex {first}")
    elif main_s["withdrawable"] >= 1.0:
        print("Money is in main perps. Run: python3 scripts/cashout.py")
    elif spot_usdc >= 1.0:
        print("Money is in SPOT. Run: python3 scripts/cashout.py")
    else:
        print("No free USDC found anywhere. Check open positions above.")

if __name__ == "__main__":
    main()
