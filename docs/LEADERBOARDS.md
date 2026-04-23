# Leaderboards — study who's actually winning

Every trader here is on-chain verifiable. You can click any wallet, see
every fill they've ever done, and reverse-engineer their strategy from
their position sizes, entry times, and asset mix.

## Hyperliquid official

- **app.hyperliquid.xyz/leaderboard** — daily / weekly / monthly / all-time
  by PnL and ROI. Raw ground truth.

## Third-party aggregators

- **hyperliquidi.com/leaderboard** — same data, nicer filters, historical
  snapshots.
- **beacontrade.io/leaderboard** — top traders by account value, includes
  position-level breakdown (entry, leverage, margin utilization).

## What to look for

When you find a trader with a stable +PnL curve, check:

1. **Fill frequency** — thousands per day (HFT/rebate) vs. tens per week
   (trend / stat-arb)?
2. **Asset concentration** — BTC/SOL only (rebate farm) or everything
   (systematic)?
3. **Hold time** — seconds (HFT), hours (swing), days (trend)?
4. **Leverage** — 1–2× (conservative quant), 5–10× (directional), 20×+
   (degen, usually blows up eventually)?
5. **Drawdown** — shallow < 10% (real strategy) or cliff-drops > 50%
   (lucky gambler)?

A trader with Sharpe > 2 over 90 days across > 500 trades is running
something real. A trader who went +300% in one week on a single coin is
a lotto ticket.

## Bot comparison

From `hypechain.app/compare/`:

| Bot | Custody | Fees | What it does well |
|---|---|---|---|
| goodcryptoX | non-custodial | freemium | audit + mobile |
| WunderTrading | non-custodial | variable | grid + DCA + copy trading |
| Katoshi AI | non-custodial | 0.01% perps | AI signals, cheap fees |
| Gainium | self-hosted | free | **open source** — read the code |

If we want to copy what works, Gainium (open-source HL bot, self-host) is
the only one with code we can read.

## External references for strategy study

- **Kaggle** — quant competitions (Jane Street, Optiver); winners publish
  post-mortems with real code
- **QuantConnect forums** — community strategies with backtest results
- **offprem.trade dashboard** — our own backtest harness results
  (https://assiamahs.github.io/offprem/)
- **Man AHL / AQR / Two Sigma public papers** — institutional quant
  literature; most of it applies to crypto too
