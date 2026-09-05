# MM Optimization Module

Portfolio optimization and delta hedging for the Hyperliquid market making bot.

## Files

| File | Purpose |
|------|---------|
| `wallet_analysis.py` | Queries HL API to analyze profitable MM wallets |
| `portfolio_optimizer.py` | CVXPY optimization for capital allocation |
| `delta_hedge.py` | Delta-neutral hedging logic |
| `trader_patches.py` | Integration code for trader.py |
| `analysis_report.md` | Findings from HLP vault analysis |

## Installation

```bash
pip install cvxpy numpy requests
```

## Quick Start

### 1. Run wallet analysis (research)
```bash
python3 wallet_analysis.py
```

### 2. Run optimization example
```bash
python3 portfolio_optimizer.py
```

### 3. Test delta hedger
```bash
python3 delta_hedge.py
```

### 4. Integrate into trader.py
```bash
# On VPS:
scp -r mm_optimization/ ubuntu@44.205.58.31:~/hyperliquid-sol/
ssh ubuntu@44.205.58.31

# Edit trader.py following trader_patches.py instructions
# Key changes:
#   1. Import OptimizedMarketMaker from trader_patches
#   2. Replace main loop with omm.run_cycle()
#   3. Set your wallet address in get_current_positions()
#   4. Uncomment exchange.order() calls when ready
```

## Architecture

```
Every 5 seconds (run_cycle):
  |
  +-- Every 5 min: run_optimization()
  |     |-- Fetch market metadata (volumes, funding)
  |     |-- Estimate spreads from orderbooks
  |     |-- CVXPY: optimize allocation weights
  |     +-- Output: which coins, how much per coin
  |
  +-- get_current_positions()
  |     +-- Fetch live positions from HL API
  |
  +-- run_hedge_cycle()
  |     |-- Compute portfolio delta
  |     |-- If delta > 3%: compute hedge orders
  |     +-- Execute hedge trades (IOC at best bid/ask)
  |
  +-- For each allocated coin:
        |-- Get orderbook
        |-- Compute skewed bid/ask (inventory-aware)
        |-- Pre-trade delta check
        +-- Place GTC limit orders
```

## Key Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Max leverage | 3.0x | Conservative for $118 |
| Max per-coin | 15% equity | $17.70 max per coin |
| Delta tolerance | 3% | Matches HLP vault behavior |
| Hedge cooldown | 30s | Avoid over-hedging |
| Optimization interval | 5 min | Adapt to changing conditions |
| Min volume | $1M/day | Avoid illiquid coins |
| Risk aversion | 2.0 | Moderate (increase for more conservative) |
