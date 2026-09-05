#!/usr/bin/env python3
"""
Trading strategy — THIS FILE IS EDITED BY THE AI AGENT.
Improved v2 with added Bollinger Bands and adjusted entry conditions for BTC, ETH, SOL.
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


def bollinger_bands(close, window=20, dev_multiple=2):
    rolling_mean = close.rolling(window).mean()
    rolling_std = close.rolling(window).std()
    upper_band = rolling_mean + (rolling_std * dev_multiple)
    lower_band = rolling_mean - (rolling_std * dev_multiple)
    return upper_band, lower_band


def generate_signals(df):
    close, high, low = df["close"], df["high"], df["low"]

    # EMA crossovers with RSI filter
    fast_ema = ema(close, 26)
    slow_ema = ema(close, 50)
    rsi_val = rsi(close, 14)

    signals = pd.Series(0, index=df.index)
    
    # Main crossover signal
    up_trend = (fast_ema > slow_ema) & (rsi_val < 70)
    down_trend = (fast_ema < slow_ema) & (rsi_val > 30)

    # Bollinger Bands for additional filtering
    upper_band, lower_band = bollinger_bands(close, window=20, dev_multiple=2)
    
    signals[up_trend] = 1  # Bullish EMA cross with RSI under 70
    signals[down_trend & (close < lower_band)] = -1  # Bearish EMA cross AND price below BB lower band

    return signals