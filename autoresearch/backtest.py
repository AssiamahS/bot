#!/usr/bin/env python3
"""
Backtesting engine — FIXED, not edited by AI.
Runs a strategy against historical data and returns performance metrics.
Includes Monte Carlo simulation for robustness testing.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# Trading costs (Hyperliquid)
TAKER_FEE = 0.00035  # 0.035%
MAKER_FEE = -0.0001  # -0.01% rebate (we assume taker for safety)
SLIPPAGE = 0.0001    # 0.01% slippage estimate

INITIAL_CAPITAL = 100.0
MAX_POSITION_PCT = 0.20  # 20% of equity per trade
COINS = ["BTC", "ETH", "SOL", "HYPE", "XRP", "SUI", "DOGE", "AVAX"]


def load_data(coin: str, dataset: str = "train") -> pd.DataFrame:
    """Load candle data for a coin."""
    path = DATA_DIR / f"{coin}_{dataset}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Data not found: {path}. Run prepare.py first.")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df


def run_backtest(df: pd.DataFrame, signals: pd.Series) -> dict:
    """Run backtest on a single coin's data with given signals."""
    equity = INITIAL_CAPITAL
    position = 0  # 1, -1, or 0
    entry_price = 0.0
    peak_equity = equity
    max_drawdown = 0.0

    trades = []
    equity_curve = [equity]

    for i in range(1, len(df)):
        price = df["close"].iloc[i]
        signal = signals.iloc[i] if i < len(signals) else 0

        # Update equity if in position
        if position != 0:
            pnl_pct = (price / entry_price - 1) * position
            current_equity = equity * (1 + pnl_pct * MAX_POSITION_PCT)
        else:
            current_equity = equity

        # Check for signal change
        if signal != 0 and signal != position:
            # Close existing position if any
            if position != 0:
                pnl_pct = (price / entry_price - 1) * position
                cost = TAKER_FEE + SLIPPAGE
                net_pnl_pct = pnl_pct - cost
                equity *= (1 + net_pnl_pct * MAX_POSITION_PCT)
                trades.append({
                    "pnl_pct": net_pnl_pct,
                    "pnl_usd": equity - equity_curve[-1],
                    "side": "long" if position == 1 else "short",
                })

            # Open new position
            position = signal
            entry_price = price
            cost = TAKER_FEE + SLIPPAGE
            equity *= (1 - cost * MAX_POSITION_PCT)  # entry cost

        elif signal == 0 and position != 0:
            # Close position
            pnl_pct = (price / entry_price - 1) * position
            cost = TAKER_FEE + SLIPPAGE
            net_pnl_pct = pnl_pct - cost
            equity *= (1 + net_pnl_pct * MAX_POSITION_PCT)
            trades.append({
                "pnl_pct": net_pnl_pct,
                "pnl_usd": equity - equity_curve[-1],
                "side": "long" if position == 1 else "short",
            })
            position = 0

        equity_curve.append(equity)
        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / peak_equity
        max_drawdown = max(max_drawdown, dd)

    # Close any remaining position at end
    if position != 0:
        price = df["close"].iloc[-1]
        pnl_pct = (price / entry_price - 1) * position
        cost = TAKER_FEE + SLIPPAGE
        net_pnl_pct = pnl_pct - cost
        equity *= (1 + net_pnl_pct * MAX_POSITION_PCT)
        trades.append({
            "pnl_pct": net_pnl_pct,
            "pnl_usd": equity - equity_curve[-1],
            "side": "long" if position == 1 else "short",
        })

    # Calculate metrics
    equity_arr = np.array(equity_curve)
    returns = np.diff(equity_arr) / equity_arr[:-1]
    returns = returns[np.isfinite(returns)]

    winning = [t for t in trades if t["pnl_pct"] > 0]
    losing = [t for t in trades if t["pnl_pct"] <= 0]

    sharpe = 0.0
    if len(returns) > 0 and np.std(returns) > 0:
        # Annualized Sharpe (15-min bars, ~35040 per year)
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(35040)

    sortino = 0.0
    if len(returns) > 0:
        downside = returns[returns < 0]
        if len(downside) > 0 and np.std(downside) > 0:
            sortino = np.mean(returns) / np.std(downside) * np.sqrt(35040)

    # Profit factor
    gross_wins = sum(t["pnl_pct"] for t in winning) if winning else 0
    gross_losses = abs(sum(t["pnl_pct"] for t in losing)) if losing else 1e-10
    profit_factor = round(gross_wins / gross_losses, 4) if gross_losses > 0 else 0

    return {
        "final_equity": round(equity, 2),
        "pnl_pct": round((equity / INITIAL_CAPITAL - 1) * 100, 2),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "total_trades": len(trades),
        "win_rate": round(len(winning) / max(len(trades), 1) * 100, 2),
        "avg_win": round(np.mean([t["pnl_pct"] for t in winning]) * 100, 4) if winning else 0,
        "avg_loss": round(np.mean([t["pnl_pct"] for t in losing]) * 100, 4) if losing else 0,
        "profit_factor": profit_factor,
    }


def monte_carlo(df: pd.DataFrame, signals_func, n_sims: int = 100) -> dict:
    """
    Run Monte Carlo simulations by shuffling candle blocks.
    Tests strategy robustness against random price patterns.
    """
    block_size = 60  # 1-hour blocks
    n_blocks = len(df) // block_size
    sharpes = []

    for sim in range(n_sims):
        # Shuffle blocks of candles (preserves intra-block structure)
        indices = np.arange(n_blocks)
        np.random.shuffle(indices)

        shuffled_dfs = []
        for idx in indices:
            start = idx * block_size
            end = start + block_size
            shuffled_dfs.append(df.iloc[start:end].copy())

        shuffled = pd.concat(shuffled_dfs, ignore_index=True)
        shuffled.index = pd.RangeIndex(len(shuffled))

        # Re-generate signals on shuffled data
        try:
            sigs = signals_func(shuffled)
            result = run_backtest(shuffled, sigs)
            sharpes.append(result["sharpe"])
        except Exception:
            continue

    if not sharpes:
        return {"mc_mean_sharpe": 0, "mc_5th_pct_sharpe": 0, "mc_stability": 0}

    return {
        "mc_mean_sharpe": round(np.mean(sharpes), 4),
        "mc_median_sharpe": round(np.median(sharpes), 4),
        "mc_5th_pct_sharpe": round(np.percentile(sharpes, 5), 4),
        "mc_95th_pct_sharpe": round(np.percentile(sharpes, 95), 4),
        "mc_stability": round(np.mean([1 for s in sharpes if s > 0]) / len(sharpes) * 100, 1),
        "mc_n_sims": len(sharpes),
    }


def run_full_evaluation(strategy_module, dataset: str = "train", run_mc: bool = False) -> dict:
    """Run backtest across all coins and optionally Monte Carlo."""
    results = {}

    for coin in COINS:
        try:
            df = load_data(coin, dataset)
            signals = strategy_module.generate_signals(df)
            bt = run_backtest(df, signals)
            results[coin] = bt

            if run_mc and dataset == "train":
                mc = monte_carlo(df, strategy_module.generate_signals, n_sims=50)
                results[f"{coin}_mc"] = mc
        except Exception as e:
            results[coin] = {"error": str(e)}

    # Aggregate
    valid = [r for r in [results.get(c) for c in COINS] if r and "sharpe" in r]
    if valid:
        results["aggregate"] = {
            "avg_sharpe": round(np.mean([r["sharpe"] for r in valid]), 4),
            "avg_pnl_pct": round(np.mean([r["pnl_pct"] for r in valid]), 2),
            "avg_max_dd": round(np.mean([r["max_drawdown_pct"] for r in valid]), 2),
            "avg_win_rate": round(np.mean([r["win_rate"] for r in valid]), 2),
            "total_trades": sum(r["total_trades"] for r in valid),
        }

    return results


if __name__ == "__main__":
    # Quick test with current strategy
    import strategy

    print("=== Training Data ===")
    train_results = run_full_evaluation(strategy, "train", run_mc=False)
    print(json.dumps(train_results, indent=2))

    print("\n=== Test Data (out-of-sample) ===")
    test_results = run_full_evaluation(strategy, "test", run_mc=False)
    print(json.dumps(test_results, indent=2))
