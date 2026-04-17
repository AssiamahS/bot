# Changelog — Hyperliquid Market Maker Bot

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
