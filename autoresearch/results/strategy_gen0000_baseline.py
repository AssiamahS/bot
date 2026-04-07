#!/usr/bin/env python3
"""
Trading strategy — THIS FILE IS EDITED BY THE AI AGENT.
Baseline v2: EMA crossover + RSI + Choppiness Index filter.
"""

import numpy as np
import pandas as pd


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))


def choppiness_index(high, low, close, period=14):
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_sum = tr.rolling(period).sum()
    high_max = high.rolling(period).max()
    low_min = low.rolling(period).min()
    return 100 * np.log10(atr_sum / (high_max - low_min + 1e-10)) / np.log10(period)


def generate_signals(df):
    close, high, low = df["close"], df["high"], df["low"]

    fast = ema(close, 12)
    slow = ema(close, 26)
    rsi_val = rsi(close, 14)
    chop = choppiness_index(high, low, close, 14)

    signals = pd.Series(0, index=df.index)
    trending = chop < 55.0

    long_cond = (fast > slow) & (fast.shift(1) <= slow.shift(1)) & trending & (rsi_val < 65)
    short_cond = (fast < slow) & (fast.shift(1) >= slow.shift(1)) & trending & (rsi_val > 35)

    signals[long_cond] = 1
    signals[short_cond] = -1
    return signals
