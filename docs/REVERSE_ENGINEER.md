# Reverse-engineering the HL leaderboard

The `app.hyperliquid.xyz/leaderboard` data is fully public and pullable via
`stats-data.hyperliquid.xyz/Mainnet/leaderboard`. Every wallet's fill
history is pullable via `POST /info {"type":"userFills","user":ADDR}`. No
login, no key, nothing hidden. This is the one honest signal in crypto.

34,628 wallets on the leaderboard. Filtered for "plausibly copyable" —
accounts between $10K and $10M, all-time PnL > $1K, volume > $100K. Ranked
by `log(pnl) * sqrt(roi)` so we don't mistake a lucky whale for an edge
and we don't mistake a noisy micro-account for signal.

## Top 3 archetypes (distinct strategies, not just top 3 by PnL)

### 1. HIP-3 commodity shorts — `0x863b676e5e...`
- **Account**: $1.35M, all-time PnL $4.85M, volume $410M
- **Markets**: 83% Brent Oil (`xyz:BRENTOIL`), 17% Crude (`xyz:CL`)
- **All 300 of last 300 fills: `Open Short`** — systematically shorting oil
- **Zero open positions currently** → closes each leg cleanly
- **This is exactly what `autoresearch/live_funding_scan.py` in this repo
  already implements.** The `hip3-funding-harvest-test` backtest showed
  Sharpe 21 — the highest across all 25+ strategies we tested. The top
  leaderboard trader is running the same thesis on real money and making
  millions off it.

### 2. Directional swing — `0x42b9493c50...`
- **Account**: $800K, all-time PnL $3.63M on only $3.64M volume
- **PnL/volume ratio: 99.8%** — every trade matters, no churn
- **All 300 of last 300 fills on one coin: `@107`** — HL-native token
- **Strategy inference**: concentrated long-term bets on 1–2 tokens
- **Currently in drawdown** (-$25K last 300 fills) — even top traders have
  bad weeks
- **Not copyable at $60**: single concentrated swings = all-or-nothing

### 3. Rebate HFT — `0x29998ebd5b...`
- **Account**: $42K, all-time PnL $2.21M, volume **$1.42 BILLION**
- **Turnover ratio: 33,833×** — pure HFT rebate capture
- **Markets**: ETH, ZEC, `@62` (HL-native)
- **Directions**: 63% close_long, 27% sell, 10% open_long
- **Strategy**: one-sided quoting with rebate tier economics, exactly the
  `$6.8K → $1.5M` Hyperliquid story (see `docs/WINNING_PLAYBOOK.md`)
- **Not copyable at $60**: requires cumulative volume > $1M/month to hit
  maker rebate tier. We pay fees, not receive them.

## What's copyable at our $60 size

**Only archetype 1 — HIP-3 commodity harvesting.** Specifically:

1. The markets (`xyz:BRENTOIL`, `xyz:CL`, plus the other HIP-3 synthetics
   like `xyz:SILVER`, `xyz:GOLD`, `xyz:NATGAS` etc.) are **less contested**
   by HFT firms than BTC/ETH — fewer quant shops know how to price oil on
   a DEX.
2. Natural spreads on HIP-3 commodities are **wider** (often 15–40 bps) —
   above our fee floor even at retail tier.
3. **Funding rate extremes** on HIP-3 perps hit ±100–200% APY regularly
   because retail traders pile into one side of the oil / metals trade.
4. Our existing code already knows how to do this: `autoresearch/
   live_funding_scan.py` + `fetch_hip3.py`. The only reason it lost money
   in its first live deployment (2026-04-17 WLD long) was **missing the
   hedge** and a broken stop-loss. Fix those and the strategy has real
   theoretical edge backed by on-chain leaderboard proof.

## The specific plan

| Step | Detail | Status |
|---|---|---|
| 1 | Fix `live_funding_scan.py` stop-loss (adverse-move exit was silent-failing on the WLD position) | pending |
| 2 | Always hedge the funded leg with an opposite spot/perp to go delta-neutral | pending |
| 3 | Target HIP-3 commodity perps specifically (add them to the scanner's universe — they're NOT in the default HL perp list) | pending |
| 4 | Cap notional at 2x account equity (not the current $30 cap, which was too tight to accumulate edge) | pending |
| 5 | Backtest on the 30-day offprem `hip3-funding-harvest-test` data before going live again | pending |

Target outcome: match **$4.8M** top trader, scaled down. Their edge per
fill is tiny (they've done $410M volume for $4.8M = ~1.2 bps per trade).
Scaled to our $60 account, that's $60 × (1.2 bps × turnover). At 50×
annual turnover (very achievable at this size) → $60 × 50 × 0.00012 =
**$36/year** from HIP-3 alone. Stackable on top of PEAD stocks (~$15/yr)
and the directional HODL basket (~$2-20/yr depending on BTC).

Total realistic year on current $1,000 Alpaca + $60 HL: **$50-80/yr
positive expected value**. Not retirement. But positive EV with three
independent strategies beats any single-strategy setup we've had.

## Daily operation

1. `scripts/first_pead_trade.py --live` — already running paper on Alpaca
2. `scripts/crypto_hodl.py --live` — BTC/ETH/SOL/HYPE once user completes
   spot → perps transfer in HL UI (agent wallet can't move money)
3. `scripts/hip3_funding.py` — TO BUILD next: scans HIP-3 markets,
   enters hedged funding positions, exits on funding normalization or
   adverse-price stop. `live_funding_scan.py` is the 80%-done starting
   point; we need the hedge + the stop.

That's the plan. Three uncorrelated income streams, each with a
verifiable on-chain example of someone doing it profitably.
