# Hyperliquid Market Maker Wallet Analysis Report

**Date:** 2026-04-01
**Analyzed Address:** HLP Maker Vault (`0x010461C14e146ac35Fe42271BDC1134EE31C703a`)

---

## 1. HLP Maker Vault Overview

| Metric | Value |
|--------|-------|
| Account Value | $134,757,117 |
| Total Margin Used | $859,771 |
| Total Notional Position | $17,195,414 |
| Active Positions | 189 |
| Positions Long | 92 |
| Positions Short | 97 |

## 2. Delta Neutrality Analysis

| Metric | Value |
|--------|-------|
| Long Notional | $10,640,253 |
| Short Notional | $6,556,539 |
| Net Exposure | $4,083,714 |
| Net / Equity | **3.03%** |
| Net / Total Abs | **23.75%** |

**Key Finding:** HLP keeps net delta at ~3% of equity. They are NOT perfectly delta-neutral but maintain very low directional risk relative to account size. The delta ratio (net/total) of 24% shows they allow moderate per-coin directional exposure but portfolio-level delta stays tight.

## 3. Position Sizing

| Metric | Value |
|--------|-------|
| Max position (BTC) | 1.977% of equity |
| Median position | 0.038% of equity |
| Mean position | 0.068% of equity |
| Total notional / equity | 12.8% |

**Distribution:**
- 0-0.01% equity: 33 positions (smallest, tail coins)
- 0.01-0.05% equity: 78 positions (bulk of portfolio)
- 0.05-0.10% equity: 51 positions (mid-tier)
- 0.10-0.50% equity: 25 positions (major alts)
- 1.00-5.00% equity: 2 positions (BTC, ETH only)

**Key Finding:** Extremely conservative sizing. For $118 equity:
- BTC/ETH: max $2.33 notional per position (1.98% of equity)
- Most coins: $0.04-$0.06 (0.04-0.05% of equity) — this is impractical at $118 scale
- Realistic minimum for our bot: $5-$15 per coin, meaning fewer coins (5-10 max)

## 4. Top Positions (% of Equity)

| Coin | Side | Notional | % Equity | uPnL |
|------|------|----------|----------|------|
| BTC | LONG | $2,664,707 | 1.977% | -$30,278 |
| ETH | LONG | $2,099,594 | 1.558% | -$31,598 |
| SOL | LONG | $291,364 | 0.216% | -$3,149 |
| KAITO | SHORT | $260,556 | 0.193% | -$4,960 |
| WIF | LONG | $246,912 | 0.183% | -$2,953 |

## 5. Fill Pattern Analysis (Last 2000 Fills)

| Metric | Value |
|--------|-------|
| Total fills | 2,000 |
| Buy fills | 1,246 (62.3%) |
| Sell fills | 754 (37.7%) |
| Unique coins | ~100+ |
| Buy/Sell ratio | 1.65:1 |

**Most Active Coins:**
| Coin | Fills | Buys | Sells | Imbalance | Pattern |
|------|-------|------|-------|-----------|---------|
| LIT | 259 | 126 | 133 | BALANCED | Classic two-sided MM |
| STABLE | 121 | 109 | 12 | SKEWED | Accumulating (likely funding play) |
| ZRO | 36 | 13 | 23 | BALANCED | Two-sided MM |
| XPL | 33 | 26 | 7 | SKEWED | Accumulating |
| EIGEN | 27 | 21 | 6 | SKEWED | Accumulating |
| AR | 25 | 13 | 12 | BALANCED | Two-sided MM |
| TRX | 23 | 12 | 11 | BALANCED | Two-sided MM |

**Key Finding:** HLP uses two strategies:
1. **Two-sided MM** on liquid coins (balanced buy/sell) — pure spread capture
2. **Directional accumulation** on coins with favorable funding — funding rate arbitrage

## 6. Funding Analysis

| Metric | Value |
|--------|-------|
| Total funding (sample) | -$68.97 |
| Entries sampled | 2,000 |

Net funding is slightly negative for HLP — they don't optimize for funding, they optimize for spread capture. Funding is a secondary consideration.

---

## 7. Strategy Reverse Engineering Summary

### What HLP Does:
1. **Spread 189 positions across nearly all coins** — extreme diversification
2. **Keep 97% of equity in reserve** — only 12.8% deployed as notional
3. **Maintain <3% net delta / equity** — near-neutral but not perfectly hedged
4. **Size by volume** — BTC/ETH get largest allocations, long tail gets tiny positions
5. **Two-sided quoting on liquid pairs** — balanced buy/sell fills
6. **Directional accumulation on funding opportunities** — one-sided fills on specific coins
7. **Use 20x leverage per position** — but tiny position sizes mean real leverage is <1x

### What Our $118 Bot Should Do:
1. **Trade 5-10 coins max** (not 189 — min order sizes prevent extreme diversification)
2. **Allocate 10-15% of equity as notional** ($12-$18 total)
3. **Target <3% net delta** (max $3.54 net directional exposure)
4. **Focus on top-5 volume coins** (BTC, ETH, SOL, HYPE, XRP)
5. **Skew quotes based on inventory** — widen bid when long, tighten ask
6. **Hedge every 30 seconds** — check delta and rebalance
7. **Use CVXPY to optimize allocation every 5 minutes** — adapt to changing spreads/funding

### Key Differences from Current Bot:
| Current Bot | Optimized Bot |
|------------|---------------|
| Takes directional risk | Delta-neutral with 3% tolerance |
| Random pair selection | CVXPY-optimized allocation |
| No inventory management | Quote skewing based on position |
| No hedge mechanism | Automatic hedging when delta > 3% |
| Gets picked off by informed traders | Pre-trade delta checks reject bad fills |
| Accumulates one-sided positions | Balanced two-sided quoting |

---

## 8. Expected Impact

For a $118 account at 3x max leverage ($354 notional):
- Target spread capture: 3-5 bps per round trip
- Expected fills per day: 50-100 (conservative)
- Average notional per fill: $7 (across all coins)
- Gross daily PnL: $7 * 75 fills * 4 bps = $0.21/day
- Net after fees (0.25bps maker): ~$0.19/day
- Daily return: ~0.16% of equity
- Monthly: ~4.8% (if everything goes right)

This is conservative. The main improvement is NOT losing money from directional exposure, which has been the primary drag on performance.
