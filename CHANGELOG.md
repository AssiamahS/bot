# Changelog — Hyperliquid Market Maker Bot

## 2026-04-23 — v2.20.0 — Exit automation + self-healing watchdog

### Added
- `scripts/pead_check.py` — daily exit manager for the PEAD portfolio.
  Evaluates every open position against –8% stop / +16% target / 45-day
  hold window. Dry-run by default; `--live` to actually close. Appends
  every decision to pead_trades.jsonl for PnL reconciliation. Without
  this, the 11 paper orders we just placed would sit forever.
- `scripts/watchdog.py` — runs every 15 min via cron. Notices:
    * Alpaca equity drop > 10% vs last-day baseline
    * Any single position > 25% of equity
    * Buy orders accepted > 24h ago and never filled (dead orders)
    * HL bot status file stale > 5 min while `running=True`
  `--heal` flag authorizes automatic cancellation of dead orders. Does
  NOT auto-restart the HL bot — stop state is deliberate until proven
  otherwise.
- `scripts/crontab.example` — template crontab wiring watchdog + pead_check
  on US market hours (Mon–Fri, New York time). Entry script stays disabled
  until 5 days of positive PEAD PnL is measured.
- `docs/SELF_RATING.md` — current 7/10 self-assessment. What moved us from
  5 → 7 this session; what each remaining gap is worth and who owns the fix.

### Why
User asked "make it 10/10 so it's profitable, it should be self-healing."
Exit automation was the single biggest missing piece: without it we open
positions with no closing mechanism. Watchdog + cron = "doesn't need
someone watching it." Doc is the ongoing scorecard so we can't lie to
ourselves about where we actually are.

## 2026-04-23 — v2.19.0 — PEAD diversification + queue-dedup

### Changed
- `scripts/first_pead_trade.py` — default behavior flipped from "top 3
  concentrated at 18.75% each" to "top 10 diversified at 5% each, capped
  at 60% of cash deployed." Penny-stock skip at $3 min. Dedup against
  both `/v2/positions` AND `/v2/orders?status=open` so we don't double up
  on tickers from a prior same-session run (the reason I fixed this on
  second execution — it tried to re-buy CCI and LBRT from the v2.18 run).
- New flags: `--per-position-pct`, `--max-deploy-pct`, `--min-price`.

### Why
User asked "can this run all S&P 500 stocks at once?" — yes, and the
wider basket actually fits the PEAD edge better. Academic result relies
on a diversified portfolio (20+ names), not concentrated bets on 3.
Penny-stock filter prevents the noise-heavy sub-$3 names from eating the
budget. Queue-dedup means we can re-run the script safely without
double-allocating to the same ticker.

## 2026-04-23 — v2.18.0 — Finnhub earnings feed + first PEAD trade script

### Added
- `scripts/first_pead_trade.py` — fetches Finnhub earnings calendar,
  filters for > 5% EPS surprises, sizes via `strategies.sizing`, and
  either previews (default) or places paper orders via the Alpaca broker
  (`--live`). Logs each submission to `pead_trades.jsonl`.
- `.finnhub_key` — gitignored, 600 perms, holds the free-tier API key.

### Verified on live data
Today's top positive surprises (Apr 20–23):
```
CCI  +161.5%  (Crown Castle, REIT)
BDN  +153.1%  (Brandywine Realty)
LBRT +145.2%  (Liberty Oilfield Services)
```
Dry run sized each at $187.50 on the $1K paper account (18.75% of equity,
under the 20% cap). All three beat enough to clear the 5% noise floor
and then some.

## 2026-04-23 — v2.17.0 — Alpaca paper broker wired end-to-end

### Added
- `strategies/_alpaca_broker.py` — concrete `AlpacaBroker` implementing
  the `BrokerInterface` protocol from `earnings_drift.py`. Pure stdlib
  (urllib + json); no alpaca-py dependency. Supports account read,
  position list, fractional-share notional market buys (Alpaca $1 min),
  share-qty sells, order list/cancel, latest-trade price lookup.
- `scripts/alpaca_smoke_test.py` — end-to-end check: loads `.alpaca_keys`,
  hits `/v2/account`, fetches AAPL latest trade, simulates a +15% EPS
  surprise, and reports the dollar size PEAD would submit. Does NOT
  place any order. Run before every live-ish session.
- `.gitignore` — `.alpaca_keys` excluded (`chmod 600`). Keys live only on
  the local filesystem, never in git.

### Verified end-to-end
Paper account `PA3AVAG3J1DC`, $1,000 starting equity. Smoke test output:
```
surprise_pct: +15.00%
signal:       +1
size (USD):   $187.50  (18.75% of equity — under 20% cap)
```
The next PEAD event with a >5% surprise will be the first live (paper)
trade through this stack. `scripts/first_pead_trade.py` to follow once we
wire an earnings calendar feed (Finnhub free tier or yfinance).

## 2026-04-23 — v2.16.0 — Strategy library + position-sizing math

### Added
- `strategies/sizing.py` — three sizing methods, each with explicit math:
  `fixed_fractional`, `fractional_kelly` (half-Kelly default, never full),
  `vol_scaled` for equal-risk across assets. Hard 20% cap per position
  enforced at the end of every path. `break_even_bps()` helper returns the
  minimum edge a strategy must capture to survive fees at our tier
  (5 bps maker/maker, 14 bps taker/taker).
- `strategies/earnings_drift.py` — Post-Earnings Announcement Drift
  (Bernard & Thomas 1989). Signal fires on > 5% EPS surprise; half-stop at
  8%, target at 16%, hold window 45 days. Weighted by surprise magnitude.
  Broker interface stubbed — TODO to wire Alpaca + Finnhub (both have free
  tiers with fractional shares / API access). Math runs today; user sees
  $11.44 size at $61 account on a +15% surprise.
- `strategies/trend_follow.py` — 50/200 dual MA on Hyperliquid perps,
  long-only, daily tick. Vol-scaled sizing so BTC (3% daily vol) gets less
  notional than DOGE (10% daily vol). This is what captures "BTC went up"
  moves that the delta-neutral MM bot never participated in.
- `docs/MATH.md` — the equations. Fixed fractional, Kelly, vol-scaled, all
  with worked examples at $61 account size. PEAD equation, trend equation,
  funding-harvest equation, stat-arb equation. Fee budget math showing why
  any strategy with < 5 bps expected edge loses at our tier. Realistic
  year-ahead expectation table: $4-$17 total on a $61 account across four
  strategies. Not get-rich-quick. Positive expected value.

### Why
User asked: "if a company has good earnings we should be able to put a
dollar in there, find equations and quant math, proportional to account."
Answer: PEAD + fractional-share brokers (Alpaca) makes the $1-stock-buy
thesis actually implementable; the math is in sizing.py. This commit puts
both the math and a reference implementation in the repo so future strategy
work doesn't reinvent either. Every strategy delegates sizing to a single
module so we can never again size from a naked constant (the root cause of
the v2.13 $30 drawdown documented in POSTMORTEM.md).

## 2026-04-23 — v2.15.0 — Preflight config validator + strategy docs

### Added
- `docs/POSTMORTEM.md` — commit-by-commit walkthrough of the $90 → $60
  drawdown. Maps each $ lost to a cause, traces when the force-close bug
  was introduced (2026-03-13 "doctor round 5"), and lists outstanding
  action items (investigate bot auto-respawn, rotate keys, etc).
- `docs/MCP_SETUP.md` — inventory of every MCP tool used, with current
  status (works / broken / gap). `bot_config_set` is documented as broken,
  `mcp__telegram__*` as session-locked, direct-SSH as a gap.
- `preflight.py` — config + math sanity checker, run automatically at
  `trader.main()` startup. Refuses to launch if any of these are true:
  - `MAX_INVENTORY_USD < ORDER_SIZE_USD * 1.1` (the v2.13 force-close
    deadlock that cost ~$15–25)
  - `MAX_POSITION_NOTIONAL < ORDER_SIZE_USD * 1.5`
  - `min_spread_bps < 2*maker_fee + 2bps edge target`
  - `safety_bps_strict < 2*maker_fee`
  - A pair in `pairs` is on the `KNOWN_BAD_PAIRS` list or has observed
    natural spread below required gate (ARK, BTC, ETH currently flagged
    for our fee tier).
  Override with `PREFLIGHT=warn` env var for dev/debug only.
- `docs/WHY_WE_LOSE.md` — the honest math of the $90 → $60 drawdown.
  Documents fees vs spreads, adverse selection, and the force-close bug
  mechanism.
- `docs/WINNING_PLAYBOOK.md` — five strategies with verifiable track
  records (maker-rebate farming, funding harvest, pairs/stat-arb, trend
  following, cross-exchange arb) with honest notes on which are viable at
  our account size.
- `docs/DIRECTIONAL.md` — answers "why didn't I make money when BTC went
  up?" MM is delta-neutral by design; covers HODL, trend-following, and
  copy-trading as ways to actually participate in upside.
- `docs/LEADERBOARDS.md` — pointers to Hyperliquid's on-chain leaderboard
  (app.hyperliquid.xyz/leaderboard) plus beacontrade.io and
  hyperliquidi.com so we can study who's actually winning.

### Why
User asked "why am I losing all this money, how do others make money, we
should be able to trade anything, write it down in the repo." Without
documented math and a startup guard, another silent regression (like v2.13
flipping `MAX_INVENTORY_USD` from 35 to 3.5) would drain the account again.
The repo now fails loudly instead of bleeding quietly.

## 2026-04-23 — v2.14.0 — Force-close deadlock fix + TG token de-hardcode

### Fixes
- **Primary PnL leak — inventory cap vs order size mismatch (trader.py):**
  `MAX_INVENTORY_USD = 3.5` combined with `order_size_usd = 25` meant every
  fill was instantly 7.1× over cap → `overweight_ratio > 4.0` triggered
  `OVERWEIGHT FORCE CLOSE` on every single round trip, taker-fee exits, guaranteed
  loss. Changed to `max(30.0, ORDER_SIZE_USD * 1.2)` so the cap auto-scales with
  configured order size. Same fix applied to `MAX_POSITION_NOTIONAL`.
- **Per-coin equity fraction too tight (trader.py):** `account_value * 0.10` =
  $6 at $61 equity, clamped effective cap below one full order. Raised to 0.50
  and flipped min→max: `effective_inv_cap = max(MAX_INVENTORY_USD, max_inv_equity)`.
- **Max-hold timer cut winning trips short (trader.py):** the completed_trips
  dataset shows winners held 7–24 min while losers held <5 min. The 300s force-close
  was evicting profitable inventory. Raised to 900s.
- **Exit-urgency trigger too early (trader.py):** `hold_secs > 60` pushed the max
  14bps exit skew within 1 min, bleeding edge on healthy trades. Raised to 300s.
- **Misleading `withdrawable` field (trader.py:703):** status JSON stored
  `totalNtlPos` under the key `withdrawable`. Now reads the actual `withdrawable`
  from account state (fallback `totalRawUsd`); `totalNtlPos` moved to its own key.
- **Hardcoded Telegram creds (trader_pingpong.py:23–24):** moved to env/config
  lookup (`TG_TOKEN`, `TG_CHAT_ID`). Token never in source anymore.
- **Unused imports:** removed `from typing import Optional` in both files.

### Why
Live data from 2026-04-23 session: 31 trips, 7W/24L (23% WR), avg trip net
-$0.037, trip_fee_ratio 3798×, portfolio -$5.33/hr. Tracing the log showed
"OVERWEIGHT FORCE CLOSE" firing on nearly every fill — the strategy never
got the chance to earn the spread because it force-closed at a taker fee
~2s after every maker fill. This is the #1 mechanism behind the $90 → $60
drawdown. Winners in the dataset (APE +$0.101 @ 24min hold, PENDLE +$0.039
@ 7min hold) all survived this path by coincidence of timing.

### Recommended config.json (apply on VPS, not committed)
- `pairs`: `["PENDLE-PERP"]` — drop ARK (spread 2–4bps) and APE (spread 4–8bps),
  both below the round-trip fee floor. PENDLE showed real 11–17bps spreads
  and is the only pair with winning trips in the dataset.
- `min_spread_bps`: 15 → 25
- `safety_bps_strict`: 4.0 → 8.0
- `order_size_usd`: keep 25

### Deploy
```
ssh ubuntu@44.205.58.31
cd ~/hyperliquid-sol && git fetch bot && git checkout feat/funding-scanner && git pull bot feat/funding-scanner
# edit config.json per above
# restart MM process (tmux / systemd — verify which is respawning it)
```

## 2026-04-17 — Funding-rate scanner leg (feat/funding-scanner)

### Added
- `autoresearch/live_funding_scan.py` — scans every HL perp each poll (60s),
  ranks by |funding - 1.25e-5 baseline|, filters by $5M min OI, opens up to
  2 maker-only positions on the highest funding extremes. Exits when
  funding normalizes, flips sign vs our side, or adverse price move >=5%.
  RiskManager 3% daily-loss kill, $30 total-notional cap.
- First live deployment found: LONG WLD @ -75% APY (`$19M OI`), SHORT STBL @
  +62% APY (`$5.2M OI`). Posted @ mid ±0.1% as post-only ALO.

### Changed
- Stopped MM leg (`trader.py`) — market spreads on ARK/APE/PENDLE collapsed
  to 3-4bps vs 7bps required gate; bot was correctly idle but not earning.
  MM capital now reallocated to scanner leg. Will resume MM when a regime
  returns where it's backtest-positive.

### Why
- 9 days of MM data showed -$0.21 net, avg edge -1.3bps, trip-fee-ratio
  390x. Even after today's weak-gate fix, live MM lost $0.75 in 1 hour as
  soon as it resumed trading. MM strategy has no edge at this account size
  on these pairs. The `offprem` backtest dashboard
  (https://assiamahs.github.io/offprem/) shows `hip3-funding-harvest-test`
  with Sharpe 21 — the highest in 25+ strategies tested. Funding-harvest
  is the validated lane. Generalized it from a single hardcoded `xyz:SILVER`
  to a live scanner so it actually has trades to make instead of polling
  a quiet market for days.

## 2026-04-17 — Weak-pair deadlock fix + stall watchdog

### Fixes
- **Weak-pair strike deadlock (trader.py):** on resume, suspension was re-firing
  immediately on the same stale losing trips, escalating strikes unboundedly
  (observed: strike 115 on ARK/APE/PENDLE, bot paralyzed for ~60min with zero
  fills and no alert). Fix: snapshot trip-count on resume via
  `weak_pair_trip_mark[coin]` and only re-evaluate once `WEAK_PAIR_LOOKBACK`
  *new* trips accumulate.
- **Strike hard cap:** strikes past `WEAK_PAIR_MAX_STRIKES` (5) move the coin
  into `weak_pair_disabled` and require manual re-enable via TG. Prevents
  runaway escalation even if the fresh-slate logic misses an edge case.
- **All-pairs-sidelined watchdog:** main loop now detects when every
  configured pair is either suspended or disabled and fires a TG alert after
  `WEAK_PAIR_STALL_ALERT_SECS` (15min), rate-limited to once per 30min.
  Silent paralysis was the #1 failure mode.
- **Order size bumped $10 → $25** (config.json): fee ratio was 390× gross PnL
  at $10 clips. At $25, per-fill maker fee (~0.5bps of $25 = $0.00125) is a
  smaller fraction of the 5bps+ target edge.

### Why
- Log evidence: `ARK | suspended 4597s (weak, strike 115)` etc. across all 3
  pairs simultaneously. `quotes_skipped_weak: 79504` over 9 days, 20 fills
  total. The bot was architecturally unable to recover from a losing streak
  without a manual restart. Restart-only recovery is not acceptable.

## 2026-04-04 — Orphan Inventory Auto-Flatten

### Fixes
- Added explicit orphan position management in `trader.py`: coins with open positions that are not in `PAIRS` are now detected every cycle.
- Added orphan cleanup modes via config:
	- `orphan_position_mode`: `close` (default), `alert`, or `ignore`
	- `orphan_close_retry_secs`: retry throttle for repeated close attempts
	- `orphan_exempt_coins`: allowlist for intentionally held manual positions
- Cleanup now runs before normal quoting logic, so stale inventory is handled even during remote pause or risk cooldown.
- Orphan cleanup cancels any resting orders on the orphan coin and then sends `market_close` when mode is `close`.

### Why
- Root cause of zombie inventory: run cycle only quoted/manages symbols in `PAIRS`, so positions from old configs were never unwound.

## 2026-03-16 — Slywatch + Profitability Overhaul

### Infrastructure
- Slywatch installed on VPS — auto-commits every file change, auto-pushes to slywatch-snapshots branch on GitHub. Runs in tmux, survives disconnects. Smart commit messages show files, line counts, and key constant changes.

### Patches Applied (v13-v20)
- v13 Risk cap: Added position size limits relative to equity
- v14 Profit formula: Realistic profitability gate (spread_capture - fees - adverse - exit_slippage > profit_target)
- v15 Zero book protection: Skip coins with empty or near-empty orderbooks
- v16 Fill deduplication: Fixed duplicate fill counting from WebSocket/REST — was inflating fill count 10x+ and skewing edge metrics negative
- v17 Adverse gate widening: Book imbalance 0.20-0.80 to 0.10-0.90, microprice divergence 6bps to 15bps. Low-price coins ($0.09) have noisy signals that triggered false positives
- v18 Cycle speed: MAX_QUOTE_PAIRS 2 to 3, exit urgency starts at 10s (was 30s), caps at 8bps (was 4bps), 1-tick exit push after fills
- v19 Profitability-first: INVENTORY_SKEW_BPS 7 to 30, fully dynamic spread floor based on cost structure, strict entry blocking (any inventory = exit-only mode)
- v20 Fill dedup: Added seen_fill_ids set using trade IDs to prevent counting the same fill multiple times

### Key Bugs Found
- Exposure cap bypassed by unconditional allow_buy = True reset after the cap check
- Throttle logic continue only skips inner loop, never prevents run_cycle()
- Hardcoded Telegram token in trader_pingpong.py (security risk)
- total_inventory_usd uses signed values — longs and shorts cancel, understating real exposure
- Double get_account_state API call per cycle (wasted latency)

### Current State
- Bot quoting DYDX and PURR on wide spreads (20-45bps)
- HYPE excluded (spread < fees, impossible to profit)
- Dynamic floor: fees + adverse_buffer + exit_slippage + profit_target — no hard floor
- Strict one-in-one-out cycling with aggressive exit skew
