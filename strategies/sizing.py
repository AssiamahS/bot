"""Position sizing. Every strategy in this repo delegates to this module.

Three methods, described with worked examples in docs/MATH.md:

- fixed_fractional  — risk fixed % of account per trade given a stop distance
- fractional_kelly  — half-Kelly from win probability and win/loss ratio
- vol_scaled        — target equal risk across assets using rolling volatility

All three clamp to MAX_SINGLE_POSITION_FRAC. Size of 0 means "do not trade."
"""
from __future__ import annotations

from typing import Iterable

# Never put more than 20% of the account into a single position, even if a
# strategy math says we should. One-parameter kill switch for blow-ups.
MAX_SINGLE_POSITION_FRAC = 0.20

# Hyperliquid fee tier for accounts under ~$1M cumulative volume/month.
MAKER_FEE_BPS = 1.5
TAKER_FEE_BPS = 4.5


def _clamp(size_usd: float, account_equity: float) -> float:
    cap = account_equity * MAX_SINGLE_POSITION_FRAC
    if size_usd <= 0:
        return 0.0
    if size_usd > cap:
        return cap
    return size_usd


def fixed_fractional(
    account_equity: float,
    risk_fraction: float,
    stop_distance_pct: float,
) -> float:
    """Size so that hitting the stop loses exactly `risk_fraction` of equity.

    risk_fraction: 0.01 = risk 1% of account per trade (typical)
    stop_distance_pct: (entry - stop) / entry, e.g. 0.05 = 5% stop
    """
    if account_equity <= 0 or stop_distance_pct <= 0:
        return 0.0
    risk_usd = account_equity * risk_fraction
    notional = risk_usd / stop_distance_pct
    return _clamp(notional, account_equity)


def fractional_kelly(
    account_equity: float,
    win_prob: float,
    win_loss_ratio: float,
    fraction_of_full: float = 0.5,
) -> float:
    """Half-Kelly by default. Pass fraction_of_full=1.0 for full Kelly — don't.

    win_prob: historical win rate, 0 < p < 1
    win_loss_ratio: avg_win / avg_loss (b in Kelly notation), > 0
    """
    if account_equity <= 0 or not (0 < win_prob < 1) or win_loss_ratio <= 0:
        return 0.0
    p = win_prob
    q = 1.0 - p
    b = win_loss_ratio
    f_full = (p * b - q) / b
    if f_full <= 0:
        return 0.0  # no edge
    f_scaled = f_full * fraction_of_full
    return _clamp(account_equity * f_scaled, account_equity)


def vol_scaled(
    account_equity: float,
    asset_price: float,
    asset_daily_vol_frac: float,
    target_daily_vol_frac: float = 0.005,
) -> float:
    """Target equal-dollar-risk across assets with different volatilities.

    asset_daily_vol_frac: stddev of 20-day log-returns, e.g. 0.03 for BTC
    target_daily_vol_frac: fraction of account_equity at 1-sigma, default 0.5%
    """
    if account_equity <= 0 or asset_price <= 0 or asset_daily_vol_frac <= 0:
        return 0.0
    target_risk_usd = account_equity * target_daily_vol_frac
    asset_daily_vol_usd = asset_price * asset_daily_vol_frac
    if asset_daily_vol_usd <= 0:
        return 0.0
    units = target_risk_usd / asset_daily_vol_usd
    notional = units * asset_price
    return _clamp(notional, account_equity)


def break_even_bps(round_trip_mode: str = "maker-maker") -> float:
    """Minimum bps of gross edge a strategy must capture to break even.

    Compare a strategy's expected_gross_edge_bps against this number BEFORE
    risking live capital. If expected < break_even, the strategy loses by
    construction (ARK/APE market-making at natural 3-5bps spreads vs 5-9bps
    round-trip cost was the death loop that drained the MM bot).
    """
    slippage = 1.0  # bps, conservative
    if round_trip_mode == "maker-maker":
        return 2 * MAKER_FEE_BPS + slippage + 2.0  # +2 bps target edge
    if round_trip_mode == "maker-taker":
        return MAKER_FEE_BPS + TAKER_FEE_BPS + slippage + 2.0
    if round_trip_mode == "taker-taker":
        return 2 * TAKER_FEE_BPS + slippage + 2.0
    raise ValueError(f"unknown round_trip_mode: {round_trip_mode!r}")


def portfolio_allocator(
    account_equity: float,
    weights: dict[str, float],
) -> dict[str, float]:
    """Given a dict of {strategy_name: weight_fraction}, return dollars per strategy.

    Weights must sum to <= 1.0. The remainder stays in cash.
    Each returned dollar amount is still subject to per-position clamping
    at the strategy level.
    """
    total = sum(weights.values())
    if total <= 0:
        return {k: 0.0 for k in weights}
    if total > 1.000001:
        raise ValueError(f"weights sum to {total:.4f} > 1.0")
    return {k: account_equity * w for k, w in weights.items()}
