#!/usr/bin/env python3
"""Funding-rate harvest. Short when funding extreme positive (longs paying), long when extreme negative.
Receives funding as PnL boost while holding the position."""

import pandas as pd


def generate_signals(df):
    if "funding_rate" not in df.columns:
        return pd.Series(0, index=df.index, dtype=int)

    fr = df["funding_rate"].astype(float)
    fr_high = fr.rolling(168).quantile(0.85)
    fr_low = fr.rolling(168).quantile(0.15)

    sig = pd.Series(0, index=df.index, dtype=int)
    pos = 0
    for i in range(len(df)):
        f = fr.iloc[i]
        hi = fr_high.iloc[i]
        lo = fr_low.iloc[i]
        if pd.isna(hi) or pd.isna(lo):
            sig.iloc[i] = pos
            continue
        if pos == 0:
            if f > 0 and f >= hi:
                pos = -1
            elif f < 0 and f <= lo:
                pos = 1
        elif pos == -1 and f <= 0:
            pos = 0
        elif pos == 1 and f >= 0:
            pos = 0
        sig.iloc[i] = pos
    return sig
