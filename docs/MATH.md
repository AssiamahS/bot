# The math — position sizing and edge equations

Every strategy in `strategies/` imports from `strategies/sizing.py`. Every
position size is a function of:

1. Current account equity (so size scales as the account grows or shrinks).
2. The strategy's historical win-rate and risk/reward.
3. A volatility estimate of the asset.
4. A hard cap (never more than X% of account on a single trade).

If a code path ever decides a size without running it through `sizing.py`,
it's a bug. The MM bot that lost $30 sized positions from a constant
(`ORDER_SIZE_USD = 25`) that had no relationship to account equity or
asset volatility. That's why $25 at 10× leverage on one coin = 41% of a
$61 account.

## Position sizing — three methods

### 1. Fixed fractional (the safest, the one we default to)

```
risk_per_trade = account_equity × risk_fraction      # risk_fraction = 0.01 (1%)
stop_distance_pct = abs(entry - stop_price) / entry
position_notional = risk_per_trade / stop_distance_pct
```

**Example:** $61 account, 1% risk, 5% stop:
- risk_per_trade = $0.61
- position_notional = $0.61 / 0.05 = **$12.20**
- If stop hits: lose $0.61 (= 1%)
- If target hits (+10%): gain $1.22 (= 2%)

Clear, boring, works at any account size. Never blows up the account in a
single trade.

### 2. Fractional Kelly (aggressive but mathematically optimal)

```
# Kelly fraction: f* = (p*b - q) / b
# where p = win probability, q = 1-p, b = win_size / loss_size
f_full = (p*b - q) / b
f_fractional = 0.5 × f_full      # half-Kelly to reduce variance
position_notional = account_equity × f_fractional
```

**Example:** strategy with 55% win rate, avg win 1.5× avg loss:
- p = 0.55, q = 0.45, b = 1.5
- f_full = (0.55 × 1.5 − 0.45) / 1.5 = 0.25 (25% of bankroll)
- f_half = 12.5% of bankroll

Full Kelly maximizes long-run growth but has wild variance (expect –50%
drawdowns). Half-Kelly is the industry-standard compromise. **Never use
full Kelly with a real account.**

### 3. Volatility-scaled (for leverage-trading, equal-risk across assets)

```
target_risk_usd = account_equity × target_vol_frac   # e.g. 0.5% daily vol target
asset_daily_vol = stddev(log_returns, N=20) × price  # rolling 20-day vol
position_notional = target_risk_usd / asset_daily_vol
```

**Example:** $61 account, 0.5% daily vol target, BTC with ~3% daily vol:
- target_risk_usd = $0.30
- BTC_daily_vol_usd = $77,800 × 0.03 = $2,334 per 1 BTC
- position_size = $0.30 / $2,334 = **0.000129 BTC ≈ $10 notional**

Ensures equal risk regardless of which asset we trade. BTC at 3% vol gets
less notional than DOGE at 10% vol. This is what institutional systematic
trend-followers use.

## Fee & slippage budget (lifted into every strategy)

```
edge_per_trade_bps = gross_edge_bps - round_trip_fee_bps - slippage_bps - funding_cost_bps
```

Numbers for Hyperliquid at our tier:
- `round_trip_fee_bps` = 3 (both maker) to 9 (both taker)
- `slippage_bps` = 1–5 depending on order depth and book imbalance
- `funding_cost_bps` = 0 for spot, variable for perps
- Required `gross_edge_bps` to break even: **≥ 5 bps maker-maker, ≥ 14 bps when forced to take**

Any strategy with expected gross edge below these floors cannot be net
profitable. The MM bot's "edge" was –0.2 bps — 5 bps below the floor.

## Edge equations per strategy

### Post-Earnings Announcement Drift (PEAD)

```
surprise = (actual_eps - estimated_eps) / |estimated_eps|
signal = 1 if surprise > threshold else (-1 if surprise < -threshold else 0)
expected_return = beta × abs(surprise) × decay_factor(days_since)
```

Academic result (Bernard & Thomas 1989, confirmed dozens of times):
top-decile surprise stocks outperform bottom-decile by ~5% over 60 days.
Retail-capturable edge after fees: ~1–3% annually at small size.

**Minimum viable:**
- Hold 5+ stocks at once (diversification)
- Enter day-after-earnings, exit at 30 days
- 1–2% per position
- Rebalance monthly

### Trend-following (N-day moving average crossover)

```
signal = 1 if price > MA(N) else 0              # long-only
position = signal × account_equity × leverage   # typically leverage ∈ [0.5, 1.5]
```

Academic: 12-month lookback momentum has Sharpe 0.5–1.0 across equities,
crypto, and commodities (Moskowitz, Ooi, Pedersen 2012). Works especially
well in crypto where trends are long.

**Minimum viable:**
- Daily rebalance (not intraday — fees eat you)
- Single asset, long-only
- 50/200-day MA dual cross (only long when price > both)
- Vol-scale size so 50% drawdown = 25% account loss

### Funding harvest (what the scanner leg attempted)

```
expected_funding_per_day = funding_rate × 24          # funding paid hourly
expected_price_move = asset_vol_daily × sqrt(hold_days)
net_edge = expected_funding × hold_days - expected_price_move × hedge_quality
```

`hedge_quality` = 0 if delta-neutral (fully hedged), 1 if directional.
Our first live deploy ran at `hedge_quality = 1` (unhedged WLD long).
Price vol of WLD was ~5% daily × sqrt(6 days) = 12% expected move.
Funding: 75% APY / 365 × 6 = 1.2%. Expected edge: 1.2% − 12% = −10.8%.
**It was always going to lose.** Must be hedged to be viable.

### Stat-arb (pairs trading)

```
spread = log(price_A) - beta × log(price_B)           # beta from rolling regression
z_score = (spread - spread_mean) / spread_stddev
signal_A = -z_score, signal_B = +z_score              # short the rich, long the cheap
expected_return = alpha × z_score × mean_reversion_speed
```

Textbook: Gatev, Goetzmann, Rouwenhorst (1999) — pairs trading historically
earned ~11 bps/day excess on S&P. Crypto equivalent: BTC/ETH, SOL/HYPE
log-ratio. Needs 30+ day backtest to find cointegrated pairs.

## Proportional sizing rule (the one-liner)

Every position in this repo, from every strategy, must satisfy:

```python
assert 0 < size_usd <= account_equity * MAX_SINGLE_POSITION_FRAC
# MAX_SINGLE_POSITION_FRAC default = 0.20 (20%)
```

At $61 account: no single position over $12.20. If the strategy wants
more, it must open multiple uncorrelated positions or wait for more
equity. This is non-negotiable and enforced in `strategies/sizing.py`.

## What a realistic year looks like at $61

If we run a basket of trend-follow + PEAD + stat-arb + funding harvest
with proper sizing, realistic out-of-sample expectations:

| Strategy | Allocation | Expected Sharpe | Expected $/year on $61 |
|---|---|---|---|
| Trend-follow BTC | 40% | 0.6 | +$2 to +$8 |
| PEAD (stocks via Alpaca) | 20% | 0.5 | +$0.50 to +$2 |
| Stat-arb ETH/BTC | 20% | 0.7 | +$1 to +$3 |
| Funding harvest (hedged) | 20% | 1.0 | +$1 to +$4 |
| **Total** | 100% | ~0.7 blended | **+$4 to +$17** |

Before you laugh: the MM bot lost $30 in 6 days. A real portfolio at this
size earns $10-ish per year. **The goal right now is not to get rich; the
goal is to stop losing.** Once we have audited positive-Sharpe equity
curves, scaling up is a capital problem, not a strategy problem.
