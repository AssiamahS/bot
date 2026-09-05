# How people actually make money doing this

Every strategy below has a public, verifiable track record. If a strategy
isn't on this list, don't run it with real money until it is.

## 1. Maker rebate farming (the $6.8K → $1.5M story)

**Source:** Hyperliquid trader turned $6,800 into $1.5M in 2 weeks by
one-sided quoting BTC/SOL perps to capture maker rebates. Public on-chain,
>$20B volume, ~3% of all maker flow on HL during that period.

- beincrypto.com/hyperliquid-trader-earns-millions-from-maker-strategy
- gate.com/learn/articles/how-to-turn-6800-into-1-5m-with-a-maker-rebate-bot-on-hyper-liquid/12197

**The math:**
- Hyperliquid maker rebate (negative fee) at top volume tier: **–0.003%**
- $1.4B volume × 0.003% = **$420K** in rebates alone (before price PnL)
- Compounding: rebates → more size → more volume → more rebates

**What makes it work:**
- One-sided quoting (bids OR asks, not both) on BTC/SOL
- Cancel/flip as market moves to stay on the "right" side
- Hard exposure cap (~$100K, strict)
- Automated — humans can't do this
- **Needs high volume tier to get the rebate.** At our tier we PAY maker
  fees instead of receiving them. This strategy is not viable for us
  below ~$10K account + sustained volume.

**Can we copy?** Not yet. We'd need $5K+ account, sustained $100K+ daily
volume for a month to reach rebate tier, and latency close to HL's Tokyo
validator. This is the target state, not the starting state.

## 2. Funding-rate harvest (hip3-funding-harvest: Sharpe 21 in backtest)

**Source:** Our own `autoresearch/` backtests on the offprem dashboard
showed `hip3-funding-harvest-test` with Sharpe 21, the highest across 25+
strategies tested. The scanner leg (`autoresearch/live_funding_scan.py`)
was built to generalize this.

**The math:**
- Hyperliquid funding payments are settled every hour
- When funding is extreme (|rate| > 1.25e-5 baseline = 75%+ APY), shorts pay
  longs (or vice versa)
- Go delta-neutral between the funded leg and a hedge, collect funding
- Edge = |funding_rate| − |price_move_against_you| − fees

**Why our first live deployment lost:**
- WLD entered at –75% APY funding (LONG gets paid)
- But WLD price dropped 8.6% before funding could cover it
- Stop-loss rule ("exit if adverse move > 5%") did not fire — bug in
  `live_funding_scan.py`'s exit logic
- Net: +$0.43 funding earned, –$1.29 price loss

**Can we copy?** Yes, if we:
1. Fix the stop-loss logic (make it actually fire)
2. Always hedge — the winning backtest was delta-neutral, our live run was
   directional
3. Set the stop tighter than expected-weekly-funding (currently too loose)

## 3. Pairs / stat-arb (crypto version)

**Source:** Classic. Works at any size. Paper: Gatev, Goetzmann,
Rouwenhorst (1999) — pairs trading on S&P historically produced 0.11% per
day excess return.

**The math:**
- Find two cointegrated assets (e.g., BTC-PERP vs ETH-PERP, or
  SOL-PERP vs HYPE-PERP)
- When spread (e.g., log-ratio) deviates > 2σ from mean, short the rich
  one, long the cheap one
- Exit when spread reverts to mean (or loss cap if it diverges further)
- Edge = mean reversion premium, independent of market direction

**Why it can work for us at $60:**
- Small account is fine — trade 2 equal-size perps
- No rebate requirement
- Fees are roughly balanced against reversion size (1% moves are common
  between correlated pairs)

**Where to start:** BTC/ETH log-ratio over 30-day window.
Backtest path: `autoresearch/backtest.py` already exists — use it.

## 4. Trend following / momentum

**Source:** Man AHL, Winton, every CTA for 40 years. Low-frequency trend
following (50/200-day crossover on liquid perps) has real Sharpe 0.5–1.0
out-of-sample.

**The math:**
- Go long when price > N-day moving average, flat/short when below
- Edge = autocorrelation in returns, especially after macro dislocations
- Very robust to overfitting IF you use long windows (>30 days)

**Tradeoffs:**
- Low turnover = low fees (good for us)
- Long drawdowns (12+ months) — need psychological stamina
- Works best on liquid pairs (BTC, ETH, SOL) — the ones we can't compete
  on market-making

## 5. Cross-exchange arbitrage

**The math:**
- Price of BTC-PERP on Hyperliquid vs same on Binance/Bybit
- When gap > 2× fees + transfer costs, buy cheap / sell rich
- Edge = latency + bridge time + capital efficiency

**Why not yet for us:**
- Needs capital on BOTH exchanges (> $1K each side minimum to cover fees)
- Needs low-latency feed to both venues
- Our $60 total capital makes this impossible until we have $2K+

## Anti-patterns (things that sound smart but aren't)

| Strategy | Why it loses for us |
|---|---|
| "AI-powered" price prediction | GPT/LLMs have no market edge. Every public backtest of LLM-picked trades underperforms buy-and-hold |
| Grid trading | Gives away variance to the market. In a trending market you fill your entire grid against you |
| Martingale / DCA on loss | Max drawdown always = 100% eventually. The one blow-up erases all prior wins |
| Signal services on Telegram/Discord | Selection bias — the posters show winners, hide losers. No audited edge |
| "Pairs" [FET, BLUR, ARK, APE, HYPE, PENDLE] with MM strategy | Our starting config. Natural spreads too tight. Lost $30+. |

## Rule: before any live trade

1. **Backtest with walk-forward validation.** No peeking at holdout data.
   Sharpe on holdout > 1.0, max DD < 20%.
2. **Paper trade 1 week.** Log every decision, fill, PnL. Compare to
   backtest expectation. If divergence > 30%, stop and debug.
3. **Live with 10% of capital.** Scale up only after 30 days of positive
   net-of-fees PnL.
4. **Kill switch.** Automatic stop at –3% daily or –10% weekly.

The old MM config violated 1, 2, 3, and 4. That's how we got here.
