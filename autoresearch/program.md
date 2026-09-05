# Hyperliquid Crypto Trading Strategy Research

## Goal
Find a profitable **directional trading** strategy for cryptocurrency perpetual futures on Hyperliquid exchange.
This is NOT a market-making strategy. We are taking directional positions (long or short) based on signals.
Optimize for **Sharpe ratio** (risk-adjusted returns) and **minimum drawdown**.

## Constraints
- Trading pairs: BTC, ETH, SOL, HYPE, XRP, SUI, DOGE, AVAX (15-minute candle data)
- Starting capital: $100
- Position sizing: max 20% of equity per trade
- Must include transaction costs: 0.035% taker fee (we cross the spread to enter/exit)
- Slippage estimate: 0.01% per trade
- Strategy must work across multiple coins (not overfit to one)
- No lookahead bias — only use data available at the time of the signal
- Minimum 20 trades per coin over the test period (no "just don't trade" strategies)
- Maximum 500 trades per coin (no scalping/overtrading)

## What to optimize
1. **Primary**: Sharpe ratio > 1.5 (annualized)
2. **Secondary**: Max drawdown < 15%
3. **Tertiary**: Win rate > 45%
4. **Profit factor** > 1.3 (gross wins / gross losses)

## Strategy types to explore (pick ONE per generation)
- Momentum / trend following (EMA crossovers, breakouts, Donchian channels)
- Mean reversion (RSI extremes, Bollinger Band bounces, z-score)
- Volatility-based (ATR breakouts, Keltner squeeze, volatility expansion)
- Multi-timeframe confluence (use rolling windows of different lengths)
- Regime detection (trending vs ranging via ADX, Choppiness Index, Hurst exponent)
- Price action (higher highs/lower lows, swing detection, support/resistance)

## Important rules
- Every strategy MUST have exit rules (stop loss AND take profit)
- Every strategy MUST have a regime/chop filter — do NOT trade in sideways markets
- Every strategy MUST produce at least 20 trades per coin
- Keep parameters minimal (< 8 tunable parameters) to reduce overfitting
- Use ONLY numpy and pandas — no external libraries
- Stop loss should be ATR-based (adaptive to volatility)
- If previous attempts produced 0 trades, LOOSEN the entry conditions
