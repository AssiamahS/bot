#!/usr/bin/env python3
"""
Trading strategy — THIS FILE IS EDITED BY THE AI AGENT.
Baseline v2 with improved EMA crossover and RSI filter, and added Choppiness Index for regime detection.
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

    fast = ema(close, 26)
    slow = ema(close, 50)
    rsi_val = rsi(close, 14)

    signals = pd.Series(0, index=df.index)
    
    # EMA crossover with RSI filter
    up_trend = (fast > slow) & (rsi_val < 70)
    down_trend = (fast < slow) & (rsi_val > 30)
    chop = choppiness_index(high, low, close, 14) < 50

    signals[up_trend] = 1
    signals[down_trend & chop] = -1

    return signals