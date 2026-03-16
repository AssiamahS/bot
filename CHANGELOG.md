# Changelog — Hyperliquid Market Maker Bot

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
