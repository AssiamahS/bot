# Why the bot loses money (the math, not the feelings)

Every time the bot has lost money, the reason was one of three things. If a
new strategy proposal doesn't have a concrete answer for all three, it will
lose money too.

## 1. Fees > edge

The single biggest killer. Hyperliquid fees at our volume tier:

| Side | Fee |
|---|---|
| Maker (rests on book, provides liquidity) | +0.0150% (~1.5 bps) |
| Taker (crosses the book, removes liquidity) | +0.0450% (~4.5 bps) |

A round trip (open + close) costs, at best:

| Scenario | Fees (bps) | Notes |
|---|---|---|
| Maker in + maker out | ~3.0 | the goal |
| Maker in + taker out | ~6.0 | what a hedge/force-close costs |
| Taker in + taker out | ~9.0 | what panic looks like |

**Required natural spread to break even on a maker/maker round trip: ≥3 bps.**
In practice, because ~20% of "maker" orders get rejected as post-only and
become takers, the real break-even spread is closer to **5–6 bps**.

Live data (2026-04-23):
- ARK-PERP spread: **2.2–3.9 bps** → structurally unprofitable
- APE-PERP spread: **4.7–14 bps** → marginal, bleeds on adverse selection
- PENDLE-PERP spread: **6–17 bps** → only pair with positive-expectation math
- BTC/ETH: **~1 bps** (most liquid) → impossible for us

The old config was quoting ARK and APE. The math was always negative.

## 2. Adverse selection

Even when spread > fees on paper, we fill on the side the market is *about
to* move against. Market makers who don't model flow lose this way:

- You post a bid at best_bid.
- Seller shows up. Seller knows price is going down (informed flow).
- You fill. Price drops. You bought at peak.
- You try to exit. Spread widens (everyone knows the same thing now).
- You pay taker to cross, or hold and watch it bleed.

Measurable signal: **post-fill price move after 3s.**
- `POST-FILL: +2bps (OK)` = uninformed flow, you earned the spread
- `POST-FILL: -18bps (ADVERSE)` = informed flow, you got run over

In this session's live log, ~50% of our fills were ADVERSE. That's the
definition of "no edge." Real market makers have < 20% adverse rate via
microprice skew, OBI, and trade-flow imbalance.

## 3. Forced exits at the worst price

Code bug compounds (1) and (2). The force-close path in `trader.py` did this
to every trade because `MAX_INVENTORY_USD = 3.5` vs `order_size_usd = 25`:

1. Maker fill at $X (good — low fee, spread earned so far: 0)
2. Cycle detects inventory 7× over cap → force-close fires
3. Bot cancels resting exit, slaps a reduce-only limit
4. Limit stale within seconds → falls through to `market_close`
5. Takes taker fee (+4.5 bps) against current adverse-selection price
6. Realizes a loss of `fee_taker_out + adverse_mid_drift`

Every fill followed this pattern. Fixed in v2.14.0 by scaling
`MAX_INVENTORY_USD` off `ORDER_SIZE_USD` so the cap can't be smaller than one
order. **This single bug cost an estimated $15–25 across the lifetime of the
account.**

## The preflight check

`preflight.py` now runs at startup and refuses to launch if the math is
broken. A partial list of what it blocks:

- `MAX_INVENTORY_USD < ORDER_SIZE_USD * 1.1`
- `MAX_POSITION_NOTIONAL < ORDER_SIZE_USD * 2.0`
- `min_spread_bps < round_trip_fee_floor + 2`
- `safety_bps_strict < maker_fee_bps * 2`
- Any pair in `pairs` whose observed natural spread < required gate for 10
  consecutive samples

If these checks fail, the bot exits with a clear message. We will never
again lose money because of a constant that silently drifted off.

## Summary

The account went from ~$90 to ~$61 through ~1,400 round trips that each
netted about –$0.02 after fees and adverse selection. HL didn't take it, the
market didn't take it — we paid it out, fifteen thousandths of a dollar at
a time. The fixes in v2.14.0 + v2.15.0 close the pipe. The *strategy* still
has to earn edge; no code fix gives edge for free.
