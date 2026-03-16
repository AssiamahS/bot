# Bot Status — Hyperliquid Market Maker

> This file is the single source of truth for what's running on the VPS.
> Claude Code updates this after every session. Slywatch auto-commits it.

## Current State

| Field | Value |
|---|---|
| **Active branch** | `slywatch-snapshots` (off `restore_features`) |
| **Running on VPS** | `ubuntu@54.234.75.223:~/hyperliquid-sol/` |
| **Bot status** | RUNNING |
| **Profitable?** | Under evaluation |
| **Slywatch** | ACTIVE — auto-commit + auto-push to GitHub |
| **Last updated** | 2026-03-16 |

## Current Strategy

- **Pairs**: HYPE-PERP
- **Order size**: $10.50 USD
- **Min spread**: 20 BPS
- **Profitability mode**: strict
- **Safety BPS**: 9.0
- **Refresh interval**: 2s

## Known Issues (Open)

1. **Exposure cap bypassed** — `allow_buy`/`allow_sell` reset unconditionally (trader.py:1043 vs 1107)
2. **Throttle does nothing** — `continue` skips inner loop only, not `run_cycle()` (trader.py:1486)
3. **Duplicate global declaration** — `strategy_pause_until` declared twice (trader.py:553 + 608)
4. **Hardcoded Telegram creds** — `trader_pingpong.py:23-24` has token in source
5. **Net vs gross inventory** — signed `sz` used instead of `abs(sz)` (trader.py:725)
6. **Double API call per cycle** — `get_account_state` called twice (trader.py:902 + 1416)
7. **Misleading field name** — `withdrawable` stores `totalNtlPos` (trader.py:469)

## Recent Sessions

| Date | What happened | Branch | Tags created |
|---|---|---|---|
| 2026-03-16 | Slywatch installed, bug audit completed, 7 bugs identified | slywatch-snapshots | — |
| 2026-03-13 | Doctor rounds 1-5: inv_extra_skew fix, exposure cap, tight-spread exit, skew inversion | restore_features | — |
| 2026-03-12 | Full audit, adaptive interval, request budget, queue preserve, trip deadlock fixes | restore_features | — |
