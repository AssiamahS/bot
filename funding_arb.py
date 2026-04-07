import requests
import json

resp = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"})
data = resp.json()

meta = data[0]["universe"]
ctxs = data[1]

coins = []
for m, c in zip(meta, ctxs):
    name = m["name"]
    funding = float(c["funding"])
    volume = float(c["dayNtlVlm"])
    mark = float(c["markPx"])
    oi = float(c["openInterest"])
    oi_usd = oi * mark
    annualized = funding * 3 * 365 * 100
    direction = "SHORT (collect)" if funding > 0 else "LONG (collect)"
    coins.append({
        "name": name,
        "funding": funding,
        "annualized": annualized,
        "abs_annualized": abs(annualized),
        "direction": direction,
        "volume": volume,
        "oi_usd": oi_usd,
        "mark": mark,
    })

coins.sort(key=lambda x: x["abs_annualized"], reverse=True)

print("=" * 110)
print(f"{'HYPERLIQUID FUNDING RATE ARBITRAGE SCANNER':^110}")
print("=" * 110)
print(f"{'Coin':<10} {'Fund Rate':>12} {'Annual %':>10} {'Direction':<18} {'24h Vol ($)':>16} {'OI ($)':>16} {'Mark':>12}")
print("-" * 110)

for c in coins[:20]:
    print(f"{c['name']:<10} {c['funding']:>12.6f} {c['annualized']:>+10.2f}% {c['direction']:<18} {c['volume']:>16,.0f} {c['oi_usd']:>16,.0f} {c['mark']:>12.4f}")

print("\n" + "=" * 110)
print("TOP 5 FUNDING PLAYS - $500 PORTFOLIO ($100 each)")
print("=" * 110)

top5 = coins[:5]
total_monthly = 0
total_yearly = 0

for c in top5:
    yearly = 100 * abs(c["annualized"]) / 100
    monthly = yearly / 12
    total_monthly += monthly
    total_yearly += yearly
    print(f"  {c['name']:<10} | Annual yield: {abs(c['annualized']):>8.2f}% | Direction: {c['direction']:<18} | Monthly: ${monthly:>8.2f} | Yearly: ${yearly:>8.2f}")

print(f"\n  TOTAL PORTFOLIO  | Monthly income: ${total_monthly:>8.2f} | Yearly income: ${total_yearly:>8.2f}")
print(f"  PORTFOLIO YIELD  | Monthly: {total_monthly/500*100:.2f}% | Annualized: {total_yearly/500*100:.2f}%")
print()
print("NOTE: This assumes funding rates stay constant (they won't). Real yield will vary.")
print("      Arb = go delta-neutral on a CEX/DEX spot position + collect funding on HL perp.")
