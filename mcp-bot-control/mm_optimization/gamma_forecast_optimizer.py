#!/usr/bin/env python3
"""
Gamma Forecast Portfolio Optimizer for Hyperliquid
Uses gamma distribution fitting on historical returns + CVXPY convex optimization
to output optimal yearly allocation percentages per coin.

Usage:
    python gamma_forecast_optimizer.py
    python gamma_forecast_optimizer.py --equity 1000 --horizon 365 --top 20
"""

import argparse
import time
from datetime import datetime, timedelta

import cvxpy as cp
import numpy as np
import requests
from scipy import stats


# ─── Data Fetching ───────────────────────────────────────────────────────────

def fetch_candles(coin: str, interval: str = "1d", lookback_days: int = 180) -> list[dict]:
    """Fetch historical candles from Hyperliquid."""
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - (lookback_days * 86400 * 1000)

    resp = requests.post(
        "https://api.hyperliquid.xyz/info",
        json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": start_ts, "endTime": end_ts},
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_universe() -> list[dict]:
    """Fetch all coins + market context from Hyperliquid."""
    resp = requests.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    meta = data[0]["universe"]
    ctxs = data[1]

    coins = []
    for m, c in zip(meta, ctxs):
        vol = float(c.get("dayNtlVlm", "0"))
        coins.append({
            "coin": m["name"],
            "volume_24h": vol,
            "funding": float(c.get("funding", "0")),
            "mark_price": float(c.get("markPx", "0")),
            "open_interest": float(c.get("openInterest", "0")),
        })
    return coins


# ─── Gamma Forecasting ──────────────────────────────────────────────────────

def compute_log_returns(candles: list[dict]) -> np.ndarray:
    """Extract log returns from candle close prices."""
    closes = np.array([float(c["c"]) for c in candles if float(c["c"]) > 0])
    if len(closes) < 10:
        return np.array([])
    return np.diff(np.log(closes))


def fit_gamma_forecast(returns: np.ndarray) -> dict:
    """
    Fit gamma distributions to positive and negative return magnitudes separately.

    Gamma distribution captures the fat-tailed, skewed nature of crypto returns
    better than a normal distribution. We model:
    - Positive returns: Gamma(a+, loc+, scale+)
    - Negative returns: Gamma(a-, loc-, scale-)
    - Probability of positive return: p_up

    Expected annualized return = (p_up * E[gain] - p_down * E[loss]) * 365
    """
    if len(returns) < 20:
        return {"valid": False}

    pos_returns = returns[returns > 0]
    neg_returns = -returns[returns < 0]  # flip to positive for gamma fitting
    p_up = len(pos_returns) / len(returns)
    p_down = 1 - p_up

    result = {
        "valid": True,
        "n_obs": len(returns),
        "p_up": p_up,
        "daily_mean": float(np.mean(returns)),
        "daily_std": float(np.std(returns)),
    }

    # Fit gamma to positive returns
    if len(pos_returns) >= 5:
        try:
            a_pos, loc_pos, scale_pos = stats.gamma.fit(pos_returns, floc=0)
            result["gamma_pos"] = {"a": a_pos, "loc": loc_pos, "scale": scale_pos}
            result["expected_gain"] = float(stats.gamma.mean(a_pos, loc=loc_pos, scale=scale_pos))
            # VaR from gamma tail
            result["gain_95"] = float(stats.gamma.ppf(0.95, a_pos, loc=loc_pos, scale=scale_pos))
        except Exception:
            result["expected_gain"] = float(np.mean(pos_returns))
            result["gain_95"] = float(np.percentile(pos_returns, 95))
    else:
        result["expected_gain"] = float(np.mean(pos_returns)) if len(pos_returns) > 0 else 0.0
        result["gain_95"] = 0.0

    # Fit gamma to negative returns (magnitude)
    if len(neg_returns) >= 5:
        try:
            a_neg, loc_neg, scale_neg = stats.gamma.fit(neg_returns, floc=0)
            result["gamma_neg"] = {"a": a_neg, "loc": loc_neg, "scale": scale_neg}
            result["expected_loss"] = float(stats.gamma.mean(a_neg, loc=loc_neg, scale=scale_neg))
            result["loss_95"] = float(stats.gamma.ppf(0.95, a_neg, loc=loc_neg, scale=scale_neg))
        except Exception:
            result["expected_loss"] = float(np.mean(neg_returns))
            result["loss_95"] = float(np.percentile(neg_returns, 95))
    else:
        result["expected_loss"] = float(np.mean(neg_returns)) if len(neg_returns) > 0 else 0.0
        result["loss_95"] = 0.0

    # Daily expected return from gamma model
    daily_er = p_up * result["expected_gain"] - p_down * result["expected_loss"]
    result["gamma_daily_return"] = daily_er

    # Annualized (compound)
    result["gamma_annual_return"] = float((1 + daily_er) ** 365 - 1)

    # Gamma-based volatility estimate (using second moments)
    second_moment = (
        p_up * (result["expected_gain"] ** 2 + (result.get("gamma_pos", {}).get("scale", result["daily_std"]) ** 2) * result.get("gamma_pos", {}).get("a", 1))
        + p_down * (result["expected_loss"] ** 2 + (result.get("gamma_neg", {}).get("scale", result["daily_std"]) ** 2) * result.get("gamma_neg", {}).get("a", 1))
    )
    daily_var = second_moment - daily_er ** 2
    result["gamma_daily_vol"] = float(np.sqrt(max(daily_var, 1e-10)))
    result["gamma_annual_vol"] = result["gamma_daily_vol"] * np.sqrt(365)

    # Sharpe-like ratio
    if result["gamma_annual_vol"] > 1e-6:
        result["gamma_sharpe"] = result["gamma_annual_return"] / result["gamma_annual_vol"]
    else:
        result["gamma_sharpe"] = 0.0

    return result


# ─── Covariance Estimation ──────────────────────────────────────────────────

def estimate_covariance(returns_dict: dict[str, np.ndarray]) -> tuple[list[str], np.ndarray]:
    """
    Estimate covariance matrix from historical returns using Ledoit-Wolf shrinkage.
    Aligns return series to the same length (truncates to shortest).
    """
    coins = list(returns_dict.keys())
    min_len = min(len(r) for r in returns_dict.values())
    aligned = np.column_stack([returns_dict[c][-min_len:] for c in coins])

    # Ledoit-Wolf shrinkage for stability
    sample_cov = np.cov(aligned, rowvar=False)
    n = sample_cov.shape[0]

    # Shrinkage target: diagonal (uncorrelated) with average variance
    target = np.diag(np.diag(sample_cov))
    # Optimal shrinkage intensity (simplified Ledoit-Wolf)
    shrinkage = 0.3  # conservative default for crypto
    cov = (1 - shrinkage) * sample_cov + shrinkage * target

    # Ensure PSD
    eigvals = np.linalg.eigvalsh(cov)
    if np.min(eigvals) < 0:
        cov += (-np.min(eigvals) + 1e-8) * np.eye(n)

    return coins, cov


# ─── Convex Optimization ────────────────────────────────────────────────────

def optimize_allocation(
    coins: list[str],
    expected_returns: np.ndarray,
    cov_matrix: np.ndarray,
    risk_aversion: float = 2.0,
    max_weight: float = 0.35,
    min_weight: float = 0.0,
    max_leverage: float = 1.0,
) -> dict:
    """
    CVXPY convex optimization for long-only yearly allocation.

    Maximize: expected_annual_return - (risk_aversion/2) * portfolio_variance
    Subject to: weights sum to 1, individual bounds, leverage constraint.
    """
    n = len(coins)
    w = cp.Variable(n)

    # Annualize returns and covariance
    annual_returns = expected_returns * 365
    annual_cov = cov_matrix * 365

    # Objective: maximize risk-adjusted return
    portfolio_return = annual_returns @ w
    portfolio_risk = cp.quad_form(w, annual_cov)
    objective = cp.Maximize(portfolio_return - (risk_aversion / 2) * portfolio_risk)

    # Constraints
    constraints = [
        cp.sum(w) == 1.0,          # fully invested
        w >= min_weight,            # long-only (or allow short with negative min)
        w <= max_weight,            # max single position
        cp.norm(w, 1) <= max_leverage,  # leverage cap
    ]

    prob = cp.Problem(objective, constraints)
    try:
        prob.solve(solver=cp.SCS, verbose=False, max_iters=10000)
    except cp.SolverError:
        try:
            prob.solve(solver=cp.ECOS, verbose=False)
        except cp.SolverError:
            prob.solve(verbose=False)

    if prob.status not in ("optimal", "optimal_inaccurate"):
        raise ValueError(f"Optimization failed: {prob.status}")

    weights = np.maximum(w.value, 0)
    weights /= weights.sum()  # re-normalize

    # Clean tiny weights
    weights[weights < 0.005] = 0
    weights /= weights.sum()

    # Portfolio metrics
    port_return = float(annual_returns @ weights)
    port_vol = float(np.sqrt(weights @ (annual_cov @ weights)))
    sharpe = port_return / port_vol if port_vol > 1e-6 else 0.0

    return {
        "status": prob.status,
        "weights": {coins[i]: float(weights[i]) for i in range(n) if weights[i] > 0.001},
        "portfolio": {
            "expected_annual_return_pct": port_return * 100,
            "annual_volatility_pct": port_vol * 100,
            "sharpe_ratio": sharpe,
        },
    }


# ─── Main Pipeline ──────────────────────────────────────────────────────────

def run_gamma_forecast_optimization(
    equity: float = 1000,
    top_n: int = 15,
    lookback_days: int = 180,
    risk_aversion: float = 2.0,
    max_weight: float = 0.35,
    min_volume: float = 2_000_000,
) -> dict:
    """
    Full pipeline: fetch data -> gamma forecast -> convex optimize -> allocation.
    """
    print("=" * 70)
    print("GAMMA FORECAST PORTFOLIO OPTIMIZER")
    print(f"Equity: ${equity:,.0f} | Lookback: {lookback_days}d | Risk aversion: {risk_aversion}")
    print("=" * 70)

    # 1. Fetch universe
    print("\n[1/4] Fetching Hyperliquid universe...")
    universe = fetch_universe()
    eligible = [c for c in universe if c["volume_24h"] >= min_volume and c["mark_price"] > 0]
    eligible.sort(key=lambda x: x["volume_24h"], reverse=True)
    selected = eligible[:top_n]
    coin_names = [c["coin"] for c in selected]

    print(f"  Selected {len(coin_names)} coins (min vol ${min_volume:,.0f}):")
    for c in selected:
        print(f"    {c['coin']:>10}  vol=${c['volume_24h']:>14,.0f}  funding={c['funding']*100:+.4f}%")

    # 2. Fetch historical candles + compute returns
    print(f"\n[2/4] Fetching {lookback_days}d historical candles...")
    returns_dict = {}
    forecasts = {}
    failed = []

    for coin in coin_names:
        try:
            candles = fetch_candles(coin, "1d", lookback_days)
            rets = compute_log_returns(candles)
            if len(rets) >= 30:
                returns_dict[coin] = rets
                forecasts[coin] = fit_gamma_forecast(rets)
                print(f"    {coin:>10}: {len(rets)} days | "
                      f"gamma_annual={forecasts[coin]['gamma_annual_return']*100:+.1f}% | "
                      f"vol={forecasts[coin]['gamma_annual_vol']*100:.1f}% | "
                      f"sharpe={forecasts[coin]['gamma_sharpe']:.2f}")
            else:
                failed.append(coin)
                print(f"    {coin:>10}: insufficient data ({len(rets)} days)")
        except Exception as e:
            failed.append(coin)
            print(f"    {coin:>10}: fetch error - {e}")
        time.sleep(0.1)  # rate limit

    active_coins = list(returns_dict.keys())
    if len(active_coins) < 3:
        raise ValueError(f"Need at least 3 coins with data, got {len(active_coins)}")

    # 3. Build covariance matrix
    print(f"\n[3/4] Estimating covariance matrix ({len(active_coins)} coins)...")
    cov_coins, cov_matrix = estimate_covariance(returns_dict)

    # Expected daily returns from gamma model
    gamma_daily_returns = np.array([forecasts[c]["gamma_daily_return"] for c in cov_coins])

    # 4. Convex optimization
    print(f"\n[4/4] Running CVXPY convex optimization...")
    result = optimize_allocation(
        coins=cov_coins,
        expected_returns=gamma_daily_returns,
        cov_matrix=cov_matrix,
        risk_aversion=risk_aversion,
        max_weight=max_weight,
    )

    # ─── Output ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("OPTIMAL ALLOCATION (Gamma Forecast + Convex Optimization)")
    print("=" * 70)

    sorted_weights = sorted(result["weights"].items(), key=lambda x: x[1], reverse=True)

    print(f"\n{'Coin':>10}  {'Weight':>8}  {'Amount':>10}  {'Gamma Ann.':>12}  {'Vol':>8}  {'Sharpe':>8}")
    print("-" * 65)
    for coin, weight in sorted_weights:
        fc = forecasts[coin]
        amt = weight * equity
        print(
            f"{coin:>10}  {weight*100:>7.1f}%  ${amt:>9,.0f}  "
            f"{fc['gamma_annual_return']*100:>+10.1f}%  "
            f"{fc['gamma_annual_vol']*100:>6.1f}%  "
            f"{fc['gamma_sharpe']:>7.2f}"
        )

    port = result["portfolio"]
    print("-" * 65)
    print(f"{'PORTFOLIO':>10}  {'100.0':>7}%  ${equity:>9,.0f}  "
          f"{port['expected_annual_return_pct']:>+10.1f}%  "
          f"{port['annual_volatility_pct']:>6.1f}%  "
          f"{port['sharpe_ratio']:>7.2f}")

    expected_gain = equity * port["expected_annual_return_pct"] / 100
    print(f"\nExpected yearly gain: ${expected_gain:+,.0f} ({port['expected_annual_return_pct']:+.1f}%)")
    print(f"Portfolio volatility: {port['annual_volatility_pct']:.1f}%")
    print(f"Sharpe ratio: {port['sharpe_ratio']:.2f}")

    # Add gamma forecast details to result
    result["forecasts"] = forecasts
    result["equity"] = equity
    result["expected_yearly_gain_usd"] = expected_gain

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gamma Forecast Portfolio Optimizer")
    parser.add_argument("--equity", type=float, default=1000, help="Total capital in USD")
    parser.add_argument("--top", type=int, default=15, help="Number of top coins to consider")
    parser.add_argument("--lookback", type=int, default=180, help="Historical lookback in days")
    parser.add_argument("--risk-aversion", type=float, default=2.0, help="Risk aversion (higher = more conservative)")
    parser.add_argument("--max-weight", type=float, default=0.35, help="Max single position weight")
    parser.add_argument("--min-volume", type=float, default=2_000_000, help="Min 24h volume filter")
    args = parser.parse_args()

    result = run_gamma_forecast_optimization(
        equity=args.equity,
        top_n=args.top,
        lookback_days=args.lookback,
        risk_aversion=args.risk_aversion,
        max_weight=args.max_weight,
        min_volume=args.min_volume,
    )
