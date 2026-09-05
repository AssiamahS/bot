#!/usr/bin/env python3
"""
Backtest: 4H Liquidity Sweep + Engulfing (from @QuantIndicator tweet, claimed 68% WR).

Rules tested (long side; shorts mirrored):
  - Sweep: bar's low trades BELOW the lowest low of the prior LOOKBACK bars
    (raid on resting liquidity under a swing low)...
  - ...but the bar CLOSES back above that swept level (rejection), AND
  - the bar is a bullish engulfing: green body that engulfs the prior bar's body.
  - Entry: next bar open. Stop: sweep bar's extreme. Target: R_MULT x risk.
  - One position at a time per coin. Fees: Hyperliquid taker both sides + slippage.

Usage: python3 sweep_backtest.py [--coins SOL,BTC,ETH] [--rr 2.0] [--lookback 10]
"""

import argparse
import time

import numpy as np
import pandas as pd
import requests

HL_API = "https://api.hyperliquid.xyz/info"
TAKER_FEE = 0.00045   # 0.045% taker (worst-case public tier)
SLIPPAGE = 0.0001

def fetch_4h(coin: str, days: int = 900) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - days * 86400_000
    r = requests.post(HL_API, json={
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "4h", "startTime": start, "endTime": end},
    }, timeout=30)
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame([{
        "ts": c["t"], "open": float(c["o"]), "high": float(c["h"]),
        "low": float(c["l"]), "close": float(c["c"]), "volume": float(c["v"]),
    } for c in rows])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("dt")


def find_setups(df: pd.DataFrame, lookback: int) -> list:
    """Return list of (index, direction) where direction +1 long / -1 short."""
    setups = []
    lows = df["low"].values
    highs = df["high"].values
    opens = df["open"].values
    closes = df["close"].values
    for i in range(lookback + 1, len(df)):
        prior_low = lows[i - lookback:i].min()
        prior_high = highs[i - lookback:i].max()
        o, c, po, pc = opens[i], closes[i], opens[i - 1], closes[i - 1]
        # bullish sweep + engulf
        if (lows[i] < prior_low and c > prior_low
                and c > o and pc < po           # green bar after red bar
                and c > po and o < pc):         # body engulfs prior body
            setups.append((i, +1))
        # bearish sweep + engulf
        elif (highs[i] > prior_high and c < prior_high
                and c < o and pc > po
                and c < po and o > pc):
            setups.append((i, -1))
    return setups


def backtest(df: pd.DataFrame, setups: list, rr: float) -> list:
    """Simulate trades bar-by-bar. Returns list of R-multiples net of costs."""
    trades = []
    busy_until = -1
    highs, lows, opens = df["high"].values, df["low"].values, df["open"].values
    for i, direction in setups:
        if i <= busy_until or i + 1 >= len(df):
            continue
        entry = opens[i + 1]
        stop = lows[i] if direction > 0 else highs[i]
        risk = abs(entry - stop)
        if risk <= 0 or risk / entry < 0.0005:  # degenerate/too-tight stop
            continue
        target = entry + direction * rr * risk
        outcome = None
        for j in range(i + 1, min(i + 1 + 60, len(df))):  # max 60 bars (10 days)
            hit_stop = lows[j] <= stop if direction > 0 else highs[j] >= stop
            hit_tgt = highs[j] >= target if direction > 0 else lows[j] <= target
            if hit_stop and hit_tgt:
                outcome = -1.0  # conservative: assume stop first on same bar
            elif hit_stop:
                outcome = -1.0
            elif hit_tgt:
                outcome = rr
            if outcome is not None:
                busy_until = j
                break
        if outcome is None:  # timed out: mark-to-market at last bar of window
            j = min(i + 60, len(df) - 1)
            outcome = direction * (df["close"].values[j] - entry) / risk
            busy_until = j
        cost_r = (TAKER_FEE * 2 + SLIPPAGE * 2) * entry / risk  # fees in R units
        trades.append(outcome - cost_r)
    return trades


def summarize(coin: str, trades: list, rr: float) -> dict:
    if not trades:
        return {"coin": coin, "n": 0}
    arr = np.array(trades)
    wins = arr[arr > 0]
    return {
        "coin": coin, "n": len(arr),
        "win_rate": len(wins) / len(arr),
        "avg_R": arr.mean(),
        "total_R": arr.sum(),
        "profit_factor": (wins.sum() / -arr[arr <= 0].sum()) if (arr <= 0).any() else float("inf"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="SOL,BTC,ETH,HYPE,DOGE")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--lookback", type=int, default=10)
    args = ap.parse_args()

    print(f"4H Sweep+Engulf | lookback={args.lookback} | target={args.rr}R | "
          f"taker {TAKER_FEE*100:.3f}%/side")
    all_trades = []
    for coin in args.coins.split(","):
        df = fetch_4h(coin)
        setups = find_setups(df, args.lookback)
        trades = backtest(df, setups, args.rr)
        all_trades += trades
        s = summarize(coin, trades, args.rr)
        if s["n"]:
            print(f"{coin:>5}: {s['n']:3d} trades over {df.index[0].date()}→{df.index[-1].date()} | "
                  f"WR {s['win_rate']*100:5.1f}% | avg {s['avg_R']:+.3f}R | "
                  f"total {s['total_R']:+.1f}R | PF {s['profit_factor']:.2f}")
        else:
            print(f"{coin:>5}: no trades")
        time.sleep(0.3)
    s = summarize("ALL", all_trades, args.rr)
    if s["n"]:
        print(f"\n  ALL: {s['n']} trades | WR {s['win_rate']*100:.1f}% | "
              f"avg {s['avg_R']:+.3f}R | total {s['total_R']:+.1f}R | PF {s['profit_factor']:.2f}")
        # break-even WR for this R:R (gross): 1/(1+rr)
        print(f"  break-even WR at {args.rr}R (before costs): {100/(1+args.rr):.1f}%")


if __name__ == "__main__":
    main()
