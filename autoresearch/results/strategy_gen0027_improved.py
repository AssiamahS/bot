#!/usr/bin/env python3
"""
Trading strategy — THIS FILE IS EDITED BY THE AI AGENT.
Each generation, Claude proposes changes to improve the Sharpe ratio.

Gen 28: Chop Index filter + reduced signal frequency + stricter RSI confirmation
Focus: Filter out sideways/choppy markets, reduce false signals, improve win rate
"""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def chop_index(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Chop Index: identifies choppy/sideways markets.
    Values > 61.8 = choppy (don't trade)
    Values < 38.2 = trending (good to trade)
    """
    atr_val = atr(high, low, close, period)
    high_max = high.rolling(period).max()
    low_min = low.rolling(period).min()
    range_val = high_max - low_min
    
    chop = 100 * np.log10((atr_val.rolling(period).sum() / (range_val + 1e-10))) / np.log10(period)
    return chop


def generate_signals(df: pd.DataFrame) -> pd.Series:
    """
    Generate trading signals: 1 = long, -1 = short, 0 = flat.
    
    Improvements:
    - Chop Index filter to avoid choppy markets
    - Stricter RSI confirmation (must be moving in direction of signal)
    - Momentum confirmation: price must be above/below recent MA
    - Cooldown period to reduce overtrading
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # === PARAMETERS ===
    fast_ema_period = 9
    slow_ema_period = 21
    rsi_period = 14
    rsi_overbought = 70
    rsi_oversold = 30
    atr_period = 14
    chop_period = 14
    chop_max = 61.8  # above this = choppy, skip trades
    chop_min = 38.2  # below this = trending
    volume_ma_period = 20
    min_volume_ratio = 0.75
    cooldown_bars = 3  # minimum bars between trades
    rsi_momentum_threshold = 5  # RSI must move 5+ points toward trade direction

    # === INDICATORS ===
    fast = ema(close, fast_ema_period)
    slow = ema(close, slow_ema_period)
    rsi_val = rsi(close, rsi_period)
    rsi_prev = rsi_val.shift(1)
    chop = chop_index(high, low, close, chop_period)
    vol_ma = df["volume"].rolling(volume_ma_period).mean()
    vol_ratio = df["volume"] / (vol_ma + 1e-10)
    
    # Mid-range MA for momentum confirmation
    mid_ema = ema(close, 14)

    # === SIGNAL LOGIC ===
    signals = pd.Series(0, index=df.index)
    
    # Regime filter: only trade in trending markets (chop < chop_min)
    trending = (chop < chop_min) | (chop.isna())
    
    # Volume filter
    vol_ok = vol_ratio > min_volume_ratio

    # Long signal conditions:
    # 1. Fast EMA crosses above slow EMA
    # 2. In trending regime (chop index low)
    # 3. RSI not overbought AND RSI rising (momentum)
    # 4. Price above mid EMA (uptrend confirmation)
    # 5. Sufficient volume
    long_cross = (fast > slow) & (fast.shift() <= slow.shift())
    long_rsi = (rsi_val < rsi_overbought) & ((rsi_val - rsi_prev) > -rsi_momentum_threshold)
    long_price = close > mid_ema
    long_cond = long_cross & trending & long_rsi & long_price & vol_ok

    # Short signal conditions:
    # 1. Fast EMA crosses below slow EMA
    # 2. In trending regime (chop index low)
    # 3. RSI not oversold AND RSI falling (momentum)
    # 4. Price below mid EMA (downtrend confirmation)
    # 5. Sufficient volume
    short_cross = (fast < slow) & (fast.shift() >= slow.shift())
    short_rsi = (rsi_val > rsi_oversold) & ((rsi_val - rsi_prev) < rsi_momentum_threshold)
    short_price = close < mid_ema
    short_cond = short_cross & trending & short_rsi & short_price & vol_ok

    # Apply cooldown: if we just traded, skip next N bars
    last_trade = pd.Series(0, index=df.index)
    for i in range(1, len(df)):
        if long_cond.iloc[i] or short_cond.iloc[i]:
            last_trade.iloc[i] = i
        else:
            last_trade.iloc[i] = max(last_trade.iloc[i-1] - 1, 0)
    
    cooldown_active = last_trade > 0
    long_cond = long_cond & ~cooldown_active
    short_cond = short_cond & ~cooldown_active

    signals[long_cond] = 1
    signals[short_cond] = -1

    return signals