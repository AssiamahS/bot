# Postmortem — $90 → $60 drawdown

Tracing every commit that touched the lossy code paths. All timestamps are
UTC, all money numbers are net of fees.

## Timeline

| Date | Event | Net PnL impact |
|---|---|---|
| 2026-02-25 | Initial bot checked in, no profitability gate | baseline |
| 2026-03-07 | Added profitability-gated quoting + round-trip PnL tracking | Made the leak measurable |
| 2026-03-07 | "Enforce minimum profitable spread (covers 1.5bps maker fee)" | Helped, but floor was 1.5bps not the round-trip 3bps |
| 2026-03-12 | "reduce inventory skew 12bps → 6bps for faster trip completion" | Traded safety for speed — faster force-closes |
| 2026-03-13 | **doctor round 5: "equity-based cap, overweight force close"** | **Introduced the force-close-on-overweight code** that became the killer 5 weeks later |
| 2026-03-13 | "per-coin inv cap" (MAX_INVENTORY_USD = 3.5) | Value was fine at $10 order size, lethal at $25 |
| 2026-03-16 | Slywatch installed, CHANGELOG v13-v20 landed | A burst of complexity chasing edge that wasn't there |
| 2026-04-07 | "Fix critical bugs found by Codex deep audit" | Fixed 4 bugs, missed the inventory cap regression risk |
| 2026-04-07 | autoresearch gen 96 best strategy: **Sharpe –0.96 train, all-negative out-of-sample** | Evolutionary search produced no real strategy |
| 2026-04-17 | **MM leg "stopped" in STATUS.md — but process silently respawned** | Silent continuation of bleed |
| 2026-04-17 | **`order_size_usd` bumped $10 → $25**, `MAX_INVENTORY_USD` left at 3.5 | **Primary leak activated** — every fill now 7.1× over cap |
| 2026-04-17 | Funding-scanner leg live, LONG WLD + SHORT STBL | Scanner: +$0.43 funding, –$1.29 WLD price = –$0.86 |
| 2026-04-17 → 2026-04-23 | MM leg bleeding ~$5/hr, scanner leg –$1.62 | **~$30 drawdown across 6 days** |
| 2026-04-23 | v2.14.0: `MAX_INVENTORY_USD` auto-scales off `ORDER_SIZE_USD` | Force-close deadlock closed |
| 2026-04-23 | v2.15.0: preflight.py blocks bad configs at startup | Regression prevention |

## What each $ of the $30 went to (approximation)

| Cause | ~$ lost | Proof |
|---|---|---|
| Fee ratio 3798× trip net on ~1,400 trips | **$12–18** | `bot_status.total_fees` × sessions, plus avg trip net –$0.037 |
| Force-close taker hedges (v2.13 deadlock) | **$5–10** | Post-2026-04-17 bleed of $5/hr, ~1 hour/day of active bleeding |
| WLD + STBL scanner losses | **$1.62 realized + $1.29 unrealized** | On-chain fills, real numbers |
| Sub-account transfers to fund scanner | **$3** | Inferred from scanner wallet equity trajectory |
| Mark-to-market + small orphan positions | **$1–3** | CHANGELOG 2026-04-04 orphan cleanup, small residuals |
| **Total** | **$24–36** | Matches observed $30 drop |

Rough midpoint: **$28** attributable, $2 inside the error bars of everything else.

## What we actually learned

### 1. Every live MM session has been net negative
No commit in the repo's history mentions a profitable run. The autoresearch
evolved strategy (gen 96) had Sharpe –0.96 in training and all-negative
out-of-sample. The only winning trips in live data were coincidence: PENDLE
and APE single-digit-cent wins that happened because the force-close timer
happened to fire after spread expanded, not because of strategy.

### 2. The strategy had no edge, only bugs covering for bugs
CHANGELOG is a list of fixes:
- v13 risk cap
- v14 profit formula
- v15 zero-book protection
- v16 fill dedup
- v17 adverse gate widening
- v18 cycle speed
- v19 profitability-first
- v20 fill dedup again
- "doctor rounds 1-5"
- "Codex deep audit" (April 7)

Every single one is a defensive patch. None of them were "we found an
edge and are exploiting it better." When a strategy needs 20 patches to
stop bleeding, the patches aren't the problem — the strategy is.

### 3. Two bug classes caused 90% of the money loss

**(a) Math-drift bugs** — a constant in the code silently becomes
incompatible with another constant (`MAX_INVENTORY_USD = 3.5` vs
`order_size_usd = 25`). No CI catches it because nothing validates
cross-constant invariants. v2.15's `preflight.py` now does.

**(b) Strategy-vs-market mismatch** — quoting into spreads (ARK 3bps,
APE 5bps) where round-trip fees are ≥6bps. The bot was literally designed
to lose per trip and hoped to make it up on volume (it did not).

### 4. Lying status files are worse than no status files
`STATUS.md` said "MM leg STOPPED" from 2026-04-17 through 2026-04-23. Live
data showed the bot fill-ticking the entire time. Someone/something
restarted it (tmux auto-respawn? systemd unit? cron?). Because we trusted
the file, we didn't investigate, and it kept bleeding.

v2.15 STATUS now reflects live reality via the live `bot_status` MCP
rather than a hand-edited markdown file. Documentation that can drift
from reality will drift from reality.

### 5. Backtests are only as good as out-of-sample testing
autoresearch gen 96 was picked as "best" based on training Sharpe. On
holdout data it loses on every coin. Every future strategy the repo ships
must be evaluated on OUT-OF-SAMPLE data before anyone trusts the number.

## Action items from this postmortem

1. ✅ **Preflight** (v2.15.0) — refuses to launch with broken math
2. ✅ **Documented playbook** (v2.15.0 `docs/WINNING_PLAYBOOK.md`)
3. ⏳ **Investigate auto-respawn** — why did MM keep restarting? Check
   VPS for systemd unit, crontab, tmux auto-start script
4. ⏳ **Backtest gate** — CI check that refuses to deploy a strategy
   without walk-forward validation showing Sharpe > 1.0 out-of-sample
5. ⏳ **Status from live data, not markdown** — dashboard should read
   `bot_status` MCP, not the STATUS.md file
6. ⏳ **Key rotation** — wallet private key and TG token are in git
   history, rotate both
7. ⏳ **Postmortem on each material loss** — any single-session loss > 5%
   of account triggers a writeup in this doc

## How we answer "have we learned" from now on

This doc is the answer. Every future multi-dollar loss gets a row in the
Timeline, an entry in "$ of the $X lost", and an action item. If we can't
write that row, we don't understand the loss well enough to prevent it.
