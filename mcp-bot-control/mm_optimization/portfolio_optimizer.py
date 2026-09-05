#!/usr/bin/env python3
"""
CVXPY Portfolio Optimizer for Hyperliquid Market Making
Optimizes capital allocation across trading pairs while maintaining delta neutrality.

Formulation:
- Decision variables: allocation weights w_i for each coin, long/short split
- Objective: maximize expected spread capture (volume * spread * weight) minus risk
- Constraints: delta neutrality, max position size, total leverage, correlation-based risk
"""

import numpy as np

try:
    import cvxpy as cp
except ImportError:
    print("CVXPY not installed. Run: pip install cvxpy")
    raise


class MarketMakerOptimizer:
    """
    CVXPY-based portfolio optimizer for market making allocation.

    Given N coins, decides:
    - What fraction of capital to allocate to each coin
    - Target long/short balance per coin
    - Overall portfolio risk budget

    Key inputs:
    - spread_estimates: expected bid-ask spread capture per coin (bps)
    - volume_scores: normalized 24h volume (higher = more fill opportunity)
    - funding_rates: current funding rates (positive = longs pay shorts)
    - correlation_matrix: estimated pairwise correlations between coins
    - equity: total account equity in USD
    """

    def __init__(
        self,
        coins: list[str],
        equity: float,
        max_leverage: float = 3.0,
        max_single_position_pct: float = 0.15,
        min_coins: int = 5,
        delta_tolerance: float = 0.05,
    ):
        self.coins = coins
        self.n = len(coins)
        self.equity = equity
        self.max_leverage = max_leverage
        self.max_single_position_pct = max_single_position_pct
        self.min_coins = min(min_coins, self.n)
        self.delta_tolerance = delta_tolerance

    def optimize_allocation(
        self,
        spread_estimates: np.ndarray,
        volume_scores: np.ndarray,
        funding_rates: np.ndarray,
        correlation_matrix: np.ndarray,
        risk_aversion: float = 2.0,
    ) -> dict:
        """
        Solve the portfolio optimization problem.

        Returns dict with:
        - weights: allocation weight per coin (fraction of equity)
        - long_weights: long exposure per coin
        - short_weights: short exposure per coin
        - expected_pnl: estimated daily PnL
        - risk_score: portfolio risk metric
        """
        assert len(spread_estimates) == self.n
        assert len(volume_scores) == self.n
        assert len(funding_rates) == self.n
        assert correlation_matrix.shape == (self.n, self.n)

        # Decision variables
        # w_long[i] = fraction of equity allocated long to coin i (>= 0)
        # w_short[i] = fraction of equity allocated short to coin i (>= 0)
        w_long = cp.Variable(self.n, nonneg=True)
        w_short = cp.Variable(self.n, nonneg=True)

        # Total allocation per coin (absolute)
        w_total = w_long + w_short

        # Net exposure per coin (signed)
        w_net = w_long - w_short

        # --- Objective: maximize expected return - risk ---

        # Expected spread capture per coin: proportional to volume * spread * allocation
        # Higher volume = more fills, higher spread = more profit per fill
        expected_spread_capture = cp.multiply(
            cp.multiply(spread_estimates, volume_scores), w_total
        )

        # Funding income: short positions earn positive funding, long positions pay
        # funding_rates[i] > 0 means longs pay shorts
        funding_income = cp.multiply(funding_rates, -w_net)  # short earns positive funding

        # Total expected return (daily)
        expected_return = cp.sum(expected_spread_capture) + cp.sum(funding_income)

        # Portfolio variance using correlation matrix
        # Covariance proxy: diag(vol_scores) @ correlation @ diag(vol_scores)
        # We use the net exposure for risk calculation
        volatility_estimates = np.sqrt(volume_scores + 1e-6)  # proxy: higher volume = higher vol
        vol_diag = np.diag(volatility_estimates)
        cov_matrix = vol_diag @ correlation_matrix @ vol_diag

        # Make sure cov_matrix is PSD
        eigvals = np.linalg.eigvalsh(cov_matrix)
        if np.min(eigvals) < 0:
            cov_matrix += (-np.min(eigvals) + 1e-6) * np.eye(self.n)

        portfolio_risk = cp.quad_form(w_net, cov_matrix)

        # Objective: maximize return - risk_aversion * risk
        objective = cp.Maximize(expected_return - risk_aversion * portfolio_risk)

        # --- Constraints ---
        constraints = [
            # Total leverage constraint
            cp.sum(w_total) <= self.max_leverage,

            # Per-coin max position
            w_total <= self.max_single_position_pct,

            # Delta neutrality: net exposure as fraction of total must be small
            cp.sum(w_net) <= self.delta_tolerance,
            cp.sum(w_net) >= -self.delta_tolerance,

            # Per-coin delta balance: each coin shouldn't be too one-sided
            # Allow some directional exposure per coin for funding capture
            w_long <= 2 * w_short + 0.02,  # long can be at most 2x short + small buffer
            w_short <= 2 * w_long + 0.02,  # short can be at most 2x long + small buffer

            # Minimum allocation - ensure diversification
            # At least min_coins should have non-trivial allocation
            cp.sum(w_total) >= 0.1,  # at least 10% of equity deployed
        ]

        # Solve
        problem = cp.Problem(objective, constraints)
        try:
            problem.solve(solver=cp.SCS, verbose=False, max_iters=5000)
        except cp.SolverError:
            try:
                problem.solve(solver=cp.ECOS, verbose=False)
            except cp.SolverError:
                problem.solve(verbose=False)

        if problem.status not in ["optimal", "optimal_inaccurate"]:
            raise ValueError(f"Optimization failed: {problem.status}")

        # Extract results
        w_long_val = np.maximum(w_long.value, 0)
        w_short_val = np.maximum(w_short.value, 0)
        w_total_val = w_long_val + w_short_val
        w_net_val = w_long_val - w_short_val

        # Filter out tiny allocations
        threshold = 0.001  # 0.1% of equity minimum
        mask = w_total_val >= threshold
        active_coins = [self.coins[i] for i in range(self.n) if mask[i]]

        results = {
            "status": problem.status,
            "objective_value": problem.value,
            "weights": {
                self.coins[i]: {
                    "total": float(w_total_val[i]),
                    "long": float(w_long_val[i]),
                    "short": float(w_short_val[i]),
                    "net": float(w_net_val[i]),
                    "notional_usd": float(w_total_val[i] * self.equity),
                }
                for i in range(self.n)
                if mask[i]
            },
            "summary": {
                "active_coins": len(active_coins),
                "total_leverage": float(np.sum(w_total_val)),
                "net_delta_pct": float(np.sum(w_net_val) * 100),
                "expected_daily_return": float(expected_return.value) if expected_return.value else 0,
                "portfolio_risk": float(portfolio_risk.value) if portfolio_risk.value else 0,
                "equity": self.equity,
            },
        }

        return results


def estimate_spreads_from_orderbook(coins: list[str]) -> np.ndarray:
    """
    Query HL orderbook for each coin and estimate the typical spread.
    Returns spread in bps for each coin.
    """
    import requests

    spreads = []
    for coin in coins:
        try:
            resp = requests.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "l2Book", "coin": coin},
                timeout=5,
            )
            book = resp.json()
            levels = book.get("levels", [[], []])
            if levels[0] and levels[1]:
                best_bid = float(levels[0][0]["px"])
                best_ask = float(levels[1][0]["px"])
                mid = (best_bid + best_ask) / 2
                spread_bps = (best_ask - best_bid) / mid * 10000 if mid > 0 else 10
            else:
                spread_bps = 10  # default
            spreads.append(spread_bps)
        except Exception:
            spreads.append(10)  # default 10 bps

    return np.array(spreads)


def build_correlation_matrix(n: int, group_corr: float = 0.5) -> np.ndarray:
    """
    Build a simple correlation matrix.
    In practice, you'd estimate this from price returns.
    Uses a single-factor model: all assets correlated to crypto beta.
    """
    # Single-factor model: corr(i,j) = beta_i * beta_j
    # BTC/ETH have high beta, alts have varying beta
    betas = np.random.uniform(0.3, 0.9, n)
    betas[0] = 1.0  # BTC if present
    if n > 1:
        betas[1] = 0.9  # ETH if present

    corr = np.outer(betas, betas) * group_corr
    np.fill_diagonal(corr, 1.0)

    # Ensure PSD
    eigvals, eigvecs = np.linalg.eigh(corr)
    eigvals = np.maximum(eigvals, 1e-6)
    corr = eigvecs @ np.diag(eigvals) @ eigvecs.T

    # Normalize to correlation
    d = np.sqrt(np.diag(corr))
    corr = corr / np.outer(d, d)

    return corr


def run_optimization_example():
    """Run an example optimization with real HL data."""
    import requests

    print("Fetching market data from Hyperliquid...")
    resp = requests.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"},
        timeout=10,
    )
    data = resp.json()
    meta = data[0]["universe"]
    ctxs = data[1]

    # Select top coins by volume
    coin_data = []
    for m, c in zip(meta, ctxs):
        vol = float(c.get("dayNtlVlm", "0"))
        if vol > 1_000_000:  # minimum $1M daily volume
            coin_data.append({
                "coin": m["name"],
                "volume": vol,
                "funding": float(c.get("funding", "0")),
                "markPx": float(c.get("markPx", "0")),
            })

    coin_data.sort(key=lambda x: x["volume"], reverse=True)
    coins_to_trade = coin_data[:15]  # Top 15 by volume

    coins = [c["coin"] for c in coins_to_trade]
    n = len(coins)

    print(f"Selected {n} coins for optimization:")
    for c in coins_to_trade:
        print(f"  {c['coin']:>10}: vol=${c['volume']:>12,.0f}  funding={c['funding']*100:.4f}%")

    # Get spread estimates
    print("\nEstimating spreads from orderbook...")
    spreads = estimate_spreads_from_orderbook(coins)
    print("Spreads (bps):", {coins[i]: f"{spreads[i]:.1f}" for i in range(n)})

    # Volume scores (normalized)
    volumes = np.array([c["volume"] for c in coins_to_trade])
    volume_scores = volumes / volumes.max()

    # Funding rates
    funding_rates = np.array([c["funding"] for c in coins_to_trade])

    # Correlation matrix
    corr = build_correlation_matrix(n)

    # Run optimization
    equity = 118.0  # $118
    optimizer = MarketMakerOptimizer(
        coins=coins,
        equity=equity,
        max_leverage=3.0,
        max_single_position_pct=0.15,
        min_coins=5,
        delta_tolerance=0.03,  # 3% max net delta
    )

    print(f"\nRunning optimization for ${equity} equity...")
    results = optimizer.optimize_allocation(
        spread_estimates=spreads / 10000,  # convert bps to fraction
        volume_scores=volume_scores,
        funding_rates=funding_rates,
        correlation_matrix=corr,
        risk_aversion=2.0,
    )

    print(f"\nOptimization status: {results['status']}")
    print(f"Objective value: {results['objective_value']:.6f}")
    print(f"\nSummary:")
    summary = results["summary"]
    print(f"  Active coins: {summary['active_coins']}")
    print(f"  Total leverage: {summary['total_leverage']:.2f}x")
    print(f"  Net delta: {summary['net_delta_pct']:.2f}%")
    print(f"  Expected daily return: {summary['expected_daily_return']*100:.4f}%")

    print(f"\nAllocations:")
    sorted_weights = sorted(
        results["weights"].items(),
        key=lambda x: x[1]["notional_usd"],
        reverse=True,
    )
    for coin, w in sorted_weights:
        if w["total"] >= 0.005:
            print(
                f"  {coin:>10}: total={w['total']*100:.1f}%  "
                f"long={w['long']*100:.1f}%  short={w['short']*100:.1f}%  "
                f"net={w['net']*100:+.1f}%  "
                f"${w['notional_usd']:.2f}"
            )

    return results


if __name__ == "__main__":
    run_optimization_example()
