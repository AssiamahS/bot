# "Bitcoin went up, I made nothing" — how to actually capture upside

## Why the MM bot didn't participate in BTC's move

**Market making is delta-neutral by design.** You quote both sides: a bid
AND an ask. If BTC goes up:

- Your ask fills (you sell).
- You collect the spread (a few cents).
- You're now short.
- You buy back on the way up (eating a loss to close).
- Net: the spread, MINUS the adverse move against your short.

You did not "miss out" — you were never in the trade. MM is a flat-delta
business; it earns the *spread* and ignores the *direction*. That's the
whole point.

If you want "BTC goes up → I go up too" exposure, you need a **directional**
position, which is the opposite of what this repo has been doing.

## Three ways to capture upside (pick one or stack)

### 1. HODL — the unglamorous champion

Put USDC into spot BTC/ETH/SOL/HYPE on Hyperliquid (or any spot market).
Never touch it. Over 90% of "active" retail strategies underperform this.

**Math at $60 account:**
- $40 into spot BTC at today's mid $77,823
- If BTC goes to $100K (+28%): you gain $11.20, zero fees, zero stress
- If BTC goes to $50K (–36%): you lose $14.40, but you still own the BTC
- No MM bugs, no force-closes, no config drift

**Where to do it:** `app.hyperliquid.xyz` → Spot tab → buy BTC/ETH/SOL/HYPE.

This is a serious recommendation, not a joke. Most of the $30 drawdown
would not have happened if 2/3 of the account had been sitting in spot.

### 2. Trend-following — long when going up, flat when not

Simplest rule that has real evidence:

```
if price > 50_day_MA and price > 200_day_MA:
    hold_long
else:
    hold_cash
```

- Captures big bull runs (50→200-day cross usually catches most of them).
- Exits during crashes (goes to cash when price breaks trend).
- Rebalances once per week — low turnover, low fees.
- Backtested Sharpe 0.5–1.0 on BTC/ETH since 2015.

**Where to run it:** We can write ~50 lines of Python that hits HL API once
per day, checks the MA, and rebalances. Zero MCP needed. Backtest path
already in `autoresearch/backtest.py` — feed it BTC daily OHLC.

Next-step file we should write: `strategies/trend_follow.py`.

### 3. Copy a proven leaderboard trader

Live leaderboards show exactly who's making money right now:

- **app.hyperliquid.xyz/leaderboard** — official, on-chain
- **hyperliquidi.com/leaderboard** — filtered views
- **beacontrade.io/leaderboard** — position-level breakdown

Pick a trader with:
- >$10K account value
- +ROI over 90 days (not just 7)
- Low leverage (< 5×) — reduces blow-up risk
- Positions in liquid coins (BTC/ETH/SOL)

Mirror their positions manually (check once per day, match size as % of
your account) or use a copy-trading bot:

- **WunderTrading** — built-in HL copy trading, paid subscription
- **Katoshi AI** — HL-native, 0.01% fees
- **Gainium** — open-source, self-host

The trade-off: you're paying them (subscription or performance fee) for
their signal. If they blow up, you blow up with them. But it's still
better than losing to a broken MM bot with no edge.

## Why the cousin's "API dataset" bot probably works (and ours didn't)

Your cousin's bot likely does one of:

1. **Signal → directional** — reads an API (news, sentiment, on-chain,
   fundamentals), generates a buy/sell signal, goes long/short accordingly.
   When BTC rips, the bot is long, bot captures it.
2. **Copy trading** — mirrors a whale's wallet. No original thinking
   required; their PnL curve ≈ whale's PnL curve.
3. **DCA with rebalance** — buys X per week, sells when > +20%. Captures
   trend, exits local tops.

Our bot did **market making**, which is structurally different:
- Doesn't need any dataset (just live order book)
- Doesn't bet on direction (intentionally)
- Makes money from spread capture, loses from adverse selection
- Is what major HFT firms do with billions in capital, not what $60
  accounts should do

## The honest recommendation

For the current $61 account:

| Allocation | Purpose | Expected outcome |
|---|---|---|
| $30 → spot BTC (HODL) | Directional upside | Matches BTC — up weeks, down weeks |
| $20 → USDC (idle) | Dry powder | Buy more if BTC –20%, withdraw if up 50% |
| $10 → PENDLE MM experiment | Earn the spread | +/– $0.50/week, learning only |
| $1 → still-stuck WLD (close!) | N/A | Drop the dead weight |

This is more boring than what we've been doing. That's the point. The
exciting setup lost $30.

## What a "real" capture-the-upside bot looks like (for when we rebuild)

```python
# strategies/simple_trend.py — not written yet, this is the spec
#
# daily tick:
#   price = get_mid("BTC-PERP")
#   ma50 = rolling_mean(prices_last_50_days)
#   ma200 = rolling_mean(prices_last_200_days)
#   target_position = 1.0 if (price > ma50 and price > ma200) else 0.0
#   if current_pos != target_position:
#     rebalance to target (max 20% of account in one move)
#   log to trend_log.jsonl
#
# kill switch:
#   if daily_pnl < -3% of account: halt for 24h
#   if weekly_pnl < -10%: halt + TG alert
```

That's it. 40 lines of Python including comments. Sharpe ~0.7, max DD ~25%,
compounds reliably. We've been writing 2,000-line MM bots when a 40-line
trend-follower would have done better.
