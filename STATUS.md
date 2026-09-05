# Bot Status — Hyperliquid Market Maker

> This file is the single source of truth for what's running on the VPS.
> Claude Code updates this after every session. Slywatch auto-commits it.

## Current State

| Field | Value |
|---|---|
| **Active branch** | `feat/funding-scanner` |
| **Version tag** | `v2.28.1` |
| **Delta-neutral harvester** | LIVE (armed, flat) since 2026-07-19. `autoresearch/live_delta_neutral.py` — long spot + short perp same main-dex asset, income = funding − fees, ~zero price risk. Gate retuned from `dn_backtest.py` 90d evidence: entry 24h positive funding AND >5% net APR, exit <0.5% trailing 6h → backtests ~+13.4% APY (old 10%/2% gate churned to −20.6%). PURR is the workhorse. 8 hedgeable assets. Live entries blocked until equity ≥ $25 (account holds $1 — see Portfolio). |
| **Running on** | LOCAL Mac via launchd `com.kim.bots` (VPS 44.205.58.31 unreachable — AWS billing). |
| **MM leg (trader.py)** | STOPPED since Apr 23. Strategy retired: 7W/24L round-trips, fees 3798× gross edge. Do not restart as-is. |
| **HIP-3 funding harvester** | LIVE AND TRADING. v2.26.3 fixed order placement (allMids/Exchange missing dex); v2.26.4 resting quotes + cancel-before-requote; v2.26.5 TG alert drops; v2.26.6 position blindness (clearinghouseState needs dex= — bots stacked to $282 notional overnight before the fix; trimmed back to per-market targets 2026-06-12). EVERYTHING in the info API is per-dex — pass dex= or HIP-3 data is silently missing. 5 markets: xyz:SILVER/MU/NVDA/AAPL/TSLA, $15–20 each, maker, 600s poll. |
| **Portfolio** | ~$1.00 unified equity + $0.43 Arbitrum. User withdrew $86.39 to own MetaMask 2026-06-22 08:40 via `scripts/withdraw.py` (on-chain ledger confirms). Bots run but can't open new positions until refunded (~$75–100 to `0x2538…C5aE` on Arbitrum USDC restores full size). UNIFIED account — spot doubles as perps margin. Equity formula: free spot (total − hold) + accountValue per dex, never spot total + dex AV (double-counts). |
| **Agent wallet** | Rotated 2026-06-10: old `0xa669…` expired ("User or API Wallet does not exist"). New agent `0x5889…F646` (`kimbot2026`) approved via Keychain main key. |
| **Profitable?** | Early signs OK: $89.57 → $91.53 (+$1.96) over first ~22h of live trading, including the cost of trimming the stacked positions. Funding harvesting works; needs a longer sample. |
| **Slywatch** | INACTIVE (was on dead VPS). Manual commits + tag + push to `bot` remote. |
| **Polymarket BTC 5m leg** | BUILT + dry-run verified 2026-07-04, NOT live. `polymarket/` — buys the leading side of `btc-updown-5m-*` rounds near close (ask ≥ 0.70), SL 25%, exits ~20s before settle. Start: `polymarket/skills/5min-btc-polymarket/scripts/btc5m_ctl.sh start --profile conservative --dry-run`. Going live needs PM_PRIVATE_KEY/PM_FUNDER in `polymarket/pm-hl-conservative-plus-repo/.env` + USDC deposited into Polymarket on Polygon. No Polymarket creds exist on this machine yet (polymarket-trade MCP still has REPLACE_ placeholders). |
| **Last updated** | 2026-07-19 |

## Rescue Actions Required (2026-04-23)

1. **Close WLD long immediately** — one more 2% adverse move liquidates.
2. **Deploy v2.14.0 fixes to VPS** — see CHANGELOG 2026-04-23 for the patch.
3. **Update VPS config.json** — drop ARK-PERP and APE-PERP (permanent negative-edge);
   keep PENDLE-PERP only; raise `min_spread_bps` 15 → 25; raise `safety_bps_strict` 4.0 → 8.0.
4. **Investigate why MM leg restarted** — STATUS said STOPPED on 2026-04-17, live data shows
   it was fill-ticking. Check systemd/tmux/crontab on VPS.

## Current Strategy

- **Pairs**: ARK-PERP, APE-PERP, PENDLE-PERP
- **Order size**: $25 USD (bumped from $10 — fee ratio was 390×)
- **Min spread**: 15 BPS
- **Profitability mode**: strict
- **Safety BPS**: 4.0
- **Refresh interval**: 5s (adaptive)
- **Max strikes**: 5 (past this → pair disabled pending manual /reenable)
- **Stall alert**: TG fires when all pairs sidelined >15min

## Known Issues (Open)

1. **Exposure cap bypassed** — `allow_buy`/`allow_sell` reset unconditionally (trader.py:1043 vs 1107)
2. **Throttle does nothing** — `continue` skips inner loop only, not `run_cycle()` (trader.py:1486)
3. **Duplicate global declaration** — `strategy_pause_until` declared twice (trader.py:553 + 608)
4. **Hardcoded Telegram creds** — `trader_pingpong.py:23-24` has token in source
5. **Net vs gross inventory** — signed `sz` used instead of `abs(sz)` (trader.py:725)
6. **Double API call per cycle** — `get_account_state` called twice (trader.py:902 + 1416)
7. **Misleading field name** — `withdrawable` stores `totalNtlPos` (trader.py:469)

## Latest Session (2026-04-17)

- Found bot paralyzed: all 3 pairs at strike 115, suspended, zero fills for ~60min, no alert.
- Root cause: `check_weak_pair` re-evaluated the same stale losing trips on every resume, escalating strikes unboundedly. Suspension timer capped at 2h but strike counter was uncapped.
- Fix in `trader.py`:
	- `weak_pair_trip_mark[coin]` snapshots trip-count on resume; eval only runs after `WEAK_PAIR_LOOKBACK` *new* trips.
	- `WEAK_PAIR_MAX_STRIKES = 5` — past this, coin moves into `weak_pair_disabled`, needs manual /reenable.
	- Main-loop watchdog: TG alert when all configured pairs are sidelined > `WEAK_PAIR_STALL_ALERT_SECS` (15min).
- Config: `order_size_usd` 10 → 25 (fee ratio was 390× gross PnL).
- Bot restarted; live log shows all 3 pairs scoring and PENDLE buy placed immediately.

## Latest Session (2026-04-04)

- Added orphan inventory cleanup in `trader.py` to prevent stale positions outside configured `PAIRS` from being ignored.
- New config controls:
	- `orphan_position_mode`: `close` (default) / `alert` / `ignore`
	- `orphan_close_retry_secs`: throttle between close attempts
	- `orphan_exempt_coins`: allowlist for manual holds
- Cleanup runs before quote logic and also during pause/cooldown paths.

## Recent Sessions

| Date | What happened | Branch | Tags created |
|---|---|---|---|
| 2026-03-16 | Slywatch installed, bug audit completed, 7 bugs identified | slywatch-snapshots | — |
| 2026-03-13 | Doctor rounds 1-5: inv_extra_skew fix, exposure cap, tight-spread exit, skew inversion | restore_features | — |
| 2026-03-12 | Full audit, adaptive interval, request budget, queue preserve, trip deadlock fixes | restore_features | — |
