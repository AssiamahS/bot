# Hyperliquid Market Maker Bot

## First Step
Read STATUS.md before making any changes to understand current state.

## Connection
- VPS: ubuntu@44.205.58.31 (Elastic IP)
- SSH key: ~/.ssh/hl-bot-key.pem
- Bot code: ~/hyperliquid-sol/ on VPS
- Main file: trader.py | Config: config.json
- GitHub: github.com/AssiamahS/bot

## Slywatch (Auto Version Control)
- Running in tmux on VPS (tmux attach -t slywatch)
- Auto-commits every file change, auto-pushes to slywatch-snapshots branch
- Tags: slytag <name> | Time travel: slyjump <name> | Manual: slysnap "msg"

## Branch Strategy
- main = production, only merge when proven profitable
- Pokemon branches (bulbasaur, ivysaur...) = sequential development
- Each Pokemon carries forward all previous fixes

## After Every Code Change
Update these files (slywatch auto-commits and pushes them):
1. CHANGELOG.md — what changed and why (append at top)
2. STATUS.md — bot state, params, known issues, session log
3. BRANCHES.md — only if branches were created/merged/abandoned

## Security
- NEVER commit private keys or API tokens
- config.json uses "ENV" for secrets
