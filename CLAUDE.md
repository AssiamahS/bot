# Project: Hyperliquid Market Maker Bot

## Architecture
- **VPS**: ubuntu@54.234.75.223 (AWS EC2 t3.micro, us-east-1)
- **SSH key**: ~/.ssh/hl-bot-key.pem
- **Bot code**: ~/hyperliquid-sol/ on VPS
- **Main file**: trader.py (market maker), trader_pingpong.py (ping-pong strategy)
- **Config**: config.json (wallet, pairs, params — secrets use "ENV" placeholder)
- **GitHub**: github.com/AssiamahS/bot

## Slywatch (Auto Version Control)
- Running in tmux on VPS (`tmux attach -t slywatch`)
- Every file change auto-commits within 5s
- Auto-pushes to `slywatch-snapshots` branch on GitHub
- Smart commit messages: `slywatch: trader.py (+12/-4) — INVENTORY_SKEW_BPS = 30`

## Tagging System
- `slytag <name> "description"` — bookmark a working version
- `slyjump <name>` — branch from a tagged commit (time travel)
- `slysnap "description"` — manual snapshot with message
- `slyhistory` — show recent commits with hashes

## Branch Strategy
- **main** = clean, production-ready, only merge when bot is proven profitable
- **Pokemon branches** (bulbasaur → ivysaur → venusaur...) = sequential development
- Each Pokemon carries forward all previous fixes
- Merge to main only when stable + profitable for extended period
- See BRANCHES.md for full map

## Required After Every Code Change
Read the /patch command in ~/.claude/commands/patch.md and follow it.
You MUST update these three files after any change:
1. **CHANGELOG.md** — What changed and why (append at top)
2. **STATUS.md** — Current bot state, params, known issues
3. **BRANCHES.md** — Branch map if branches were created/merged/abandoned

## Key Files to Read First
- `STATUS.md` — What's running right now, known bugs, recent sessions
- `BRANCHES.md` — What each branch is for, Pokemon sequence
- `CHANGELOG.md` — Full history of every change with reasoning
- `config.json` — Current trading parameters

## Security
- NEVER commit real private keys or API tokens to git
- config.json uses "ENV" for secrets — actual values are in environment
- Telegram token was previously leaked in commit cea53fe — needs rotation

## Trading Context
- Hyperliquid DEX perps (SOL-PERP, HYPE-PERP, etc.)
- Market making strategy: quote both sides, capture spread
- Key params: min_spread_bps, order_size_usd, safety_bps
- Bot runs 24/7 on VPS, monitored via Telegram commands
