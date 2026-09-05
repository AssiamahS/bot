# Self-rating: where we are, where 10/10 is

Scored honestly. Updated every time the bot ships a material change.

## 2026-04-23 — v2.20.0

**Current: 7/10**

### What moved us from 5 → 7 in this session
- v2.14 killed the force-close deadlock. Can't lose the same $30 the same
  way twice.
- v2.15 added `preflight.py` — config changes that would have re-caused
  the v2.13 drain now refuse to launch.
- v2.17 wired real (paper) money trading on an independent venue (Alpaca),
  not on the same exchange we'd been losing on.
- v2.18/v2.19 placed 11 live paper trades on real earnings surprises with
  diversified sizing and queue-dedup.
- v2.20 added exit automation (`scripts/pead_check.py`) and a watchdog
  (`scripts/watchdog.py`) + a cron template so both run on their own.

### What's still keeping us under 10/10

| # | Gap | Fix | Owner |
|---|---|---|---|
| 1 | VPS still runs v2.13 code — Hyperliquid side is one user action from bleeding again | `ssh ubuntu@44.205.58.31` interactively and run `git pull bot feat/funding-scanner && python3 preflight.py` | user |
| 2 | Only one strategy generating paper PnL (PEAD). Need 2–3 uncorrelated streams for portfolio Sharpe > 1 | Ship `strategies/trend_follow.py` on HL daily tick once VPS is updated; wire stat-arb after 30d of PEAD data | me |
| 3 | No 30 days of live PnL yet. Paper without a record means nothing | Run cron daily for 4 weeks and measure | both |
| 4 | Self-healing is partial — watchdog *alerts* on stale orders, doesn't auto-cancel unless --heal is set; no auto-restart of HL bot | Add systemd unit on VPS (survives reboot, auto-restarts on crash); extend watchdog to call bot_restart MCP on hang detection | me + user |
| 5 | Market orders at open — opening auction can give 50-bps-worse fills than mid | Switch to limit orders at prior close ± 50 bps, cancel-on-timeout | me |
| 6 | Crypto exposure is zero. "BTC goes up, we make nothing" is still true | Deploy `strategies/trend_follow.py` on BTC daily via HL | me once VPS is ready |
| 7 | Dashboard is a markdown file the user reads by hand | Add a small HTML from `autoresearch/pnl_dashboard.py` already in the repo; schedule it to refresh to ~/kim/dashboard.html | low priority |

### The rule of the rating

- **5/10**: Not actively losing money, but no strategy with measurable edge running.
- **7/10** (now): Paper strategy placing trades with real edge, exit logic automated,
  config regressions blocked at startup. One venue, one strategy, no live PnL yet.
- **8/10**: 30 days of positive paper PnL on PEAD. Trend-follow ready to ship.
- **9/10**: Two strategies running live (paper) on independent venues, 90 days of
  net-positive combined PnL, blended Sharpe > 1.
- **10/10**: All four strategies in `docs/WINNING_PLAYBOOK.md` running live on real
  money, 6 months of net-positive return, documented drawdowns recovered, any new
  strategy must pass backtest gate before deploy.

10/10 is not "infinite money." It's "this codebase reliably captures the edge the
strategies advertise, with self-healing, alerting, and a track record." That is
the bar.
