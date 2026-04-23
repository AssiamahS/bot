# Bot Status — Hyperliquid Market Maker

> This file is the single source of truth for what's running on the VPS.
> Claude Code updates this after every session. Slywatch auto-commits it.

## Current State

| Field | Value |
|---|---|
| **Active branch** | `feat/funding-scanner` |
| **Version tag** | `v2.18.0` |
| **Running on VPS** | `ubuntu@44.205.58.31:~/hyperliquid-sol/` |
| **MM leg (trader.py)** | RUNNING (not stopped as previously noted — was silently respawned). Bleeding ~$5/hr at pre-v2.14 settings; fixes now pending deploy. |
| **Funding scanner leg** | LIVE — WLD LONG down -86% ROE (-$1.29 unrealized on $1.34 equity wallet). Price move > funding yield. |
| **Portfolio** | Bot wallet $61.39 (down from $66.72 this session, down from ~$90 lifetime). |
| **Profitable?** | No on both legs. Pending rescue. |
| **Slywatch** | ACTIVE — auto-commit + auto-push to GitHub |
| **Last updated** | 2026-04-23 |

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
