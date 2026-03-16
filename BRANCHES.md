# Branch Map — Hyperliquid Market Maker Bot

> Every branch explained. Updated by Claude Code after creating new branches.
> If a branch isn't listed here, it's undocumented legacy code.

## Branch Naming Convention

- **Pokemon names** (bulbasaur, ivysaur, etc.) = Active development branches, sequential
- **Feature names** (queue_preserve, trip_deadlock) = One-off fixes from specific sessions
- **fix/** prefix = Targeted bug fix branches
- **slywatch-snapshots** = Auto-captured history of all VPS changes

## Active Branches

| Branch | Based on | Purpose | Status | Notes |
|---|---|---|---|---|
| `main` | — | Clean baseline, production-ready code | STABLE | Only merge here when bot is proven profitable |
| `slywatch-snapshots` | `restore_features` | Auto-captured VPS history via slywatch | ACTIVE | Every file change auto-committed + pushed |
| `fix/penny-cross-guard-sizing` | main | Fix penny spread detection + guard sizing | ACTIVE | 26 commits ahead of main |
| `restore_features` | main | Restore microprice quoting, scoring, volume tracking | ACTIVE | Base for current VPS code |

## Development Branches (Experimental)

| Branch | Based on | Purpose | Status | Notes |
|---|---|---|---|---|
| `westcoast` | main | West coast trading hours strategy experiment | STALE | 21 ahead, not merged |
| `driver` | main | Driver strategy — aggressive entry/exit | STALE | 14 ahead |
| `nascar` | main | Speed-focused quoting, reduced latency | STALE | 20 ahead |
| `iWill` | main | Will-based execution strategy | STALE | 19 ahead |
| `beach` | main | Spread capture diagnostics, fill edge, trip stats | STALE | Unknown status |

## Pokemon Branch Sequence

> Each Pokemon branch builds on the last. This is the main development line.
> When a Pokemon branch is stable and profitable, it merges to main.

| # | Pokemon | Purpose | Status | Key tags |
|---|---|---|---|---|
| 1 | `bulbasaur` | TBD — Bug audit fixes (7 bugs from 2026-03-16 audit) | NOT STARTED | — |
| 2 | `ivysaur` | TBD | — | — |
| 3 | `venusaur` | TBD | — | — |
| 4 | `charmander` | TBD | — | — |
| 5 | `charmeleon` | TBD | — | — |
| 6 | `charizard` | TBD | — | — |
| 7 | `squirtle` | TBD | — | — |
| 8 | `wartortle` | TBD | — | — |
| 9 | `blastoise` | TBD | — | — |

## Archived / Dead Branches

> Branches that were experiments and didn't pan out. Kept for reference.

| Branch | What it tried | Why it died |
|---|---|---|
| (none yet — document branches here as they're abandoned) | | |

## How to Use This

```bash
# See what's running on VPS right now
cat STATUS.md

# See what each branch is for
cat BRANCHES.md

# Create new Pokemon branch (Claude Code does this)
git checkout -b bulbasaur
# ... make fixes ...
# When stable: git checkout main && git merge bulbasaur

# Tag milestones
slytag bulbasaur-profitable-day1 "Bot +$2, algo working"
slyjump bulbasaur-profitable-day1  # time travel back if it breaks
```
