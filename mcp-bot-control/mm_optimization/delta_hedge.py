#!/usr/bin/env python3
"""
Delta Hedging Module for Hyperliquid Market Making Bot

Monitors portfolio delta exposure and executes hedging trades to maintain
delta neutrality. Designed to be called from trader.py on each cycle.

Strategy:
1. Compute portfolio delta (net directional exposure)
2. If delta exceeds threshold, compute hedge trades
3. Execute hedge via limit orders at mid-price (or aggressive if urgent)
4. Track hedging cost and slippage
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class Position:
    coin: str
    size: float  # signed: positive = long, negative = short
    entry_price: float
    mark_price: float
    unrealized_pnl: float = 0.0

    @property
    def notional(self) -> float:
        return abs(self.size * self.mark_price)

    @property
    def signed_notional(self) -> float:
        return self.size * self.mark_price

    @property
    def side(self) -> str:
        return "LONG" if self.size > 0 else "SHORT"


@dataclass
class HedgeOrder:
    coin: str
    side: str  # "buy" or "sell"
    size: float  # absolute
    price: float
    reason: str
    urgency: str = "normal"  # "normal", "urgent"


@dataclass
class DeltaHedger:
    """
    Maintains delta neutrality for the portfolio.

    Parameters:
    - equity: total account equity in USD
    - max_delta_pct: maximum allowed net delta as % of equity (default 5%)
    - rebalance_threshold_pct: trigger hedge when delta exceeds this (default 3%)
    - hedge_via_btc: if True, hedge using BTC (most liquid). Otherwise per-coin.
    - max_hedge_cost_pct: max acceptable slippage for hedge trades (bps)
    """
    equity: float
    max_delta_pct: float = 5.0
    rebalance_threshold_pct: float = 3.0
    hedge_via_btc: bool = False
    max_hedge_cost_bps: float = 5.0
    _last_hedge_time: float = field(default=0.0, init=False)
    _hedge_cooldown: float = field(default=30.0, init=False)  # seconds between hedges

    def compute_portfolio_delta(self, positions: list[Position]) -> dict:
        """
        Compute portfolio-level delta metrics.

        Returns:
        - net_delta_usd: total signed notional exposure
        - gross_exposure_usd: total absolute notional exposure
        - delta_pct: net_delta / equity as percentage
        - per_coin_delta: dict of coin -> signed notional
        - needs_hedge: whether rebalancing is needed
        """
        net_delta = 0.0
        gross_exposure = 0.0
        per_coin = {}

        for pos in positions:
            signed = pos.signed_notional
            net_delta += signed
            gross_exposure += pos.notional
            per_coin[pos.coin] = per_coin.get(pos.coin, 0.0) + signed

        delta_pct = (net_delta / self.equity * 100) if self.equity > 0 else 0.0
        needs_hedge = abs(delta_pct) > self.rebalance_threshold_pct

        return {
            "net_delta_usd": net_delta,
            "gross_exposure_usd": gross_exposure,
            "delta_pct": delta_pct,
            "per_coin_delta": per_coin,
            "needs_hedge": needs_hedge,
            "long_exposure": sum(pos.notional for pos in positions if pos.size > 0),
            "short_exposure": sum(pos.notional for pos in positions if pos.size < 0),
        }

    def compute_hedge_orders(
        self,
        positions: list[Position],
        orderbooks: dict[str, dict],  # coin -> {"best_bid": float, "best_ask": float, "mid": float}
    ) -> list[HedgeOrder]:
        """
        Compute the trades needed to bring delta within tolerance.

        Two modes:
        1. Per-coin hedging: reduce the most imbalanced positions
        2. BTC hedging: use BTC as the hedge instrument (simpler, more liquid)
        """
        delta = self.compute_portfolio_delta(positions)

        if not delta["needs_hedge"]:
            return []

        # Cooldown check
        now = time.time()
        if now - self._last_hedge_time < self._hedge_cooldown:
            logger.debug("Hedge cooldown active, skipping")
            return []

        orders = []
        target_delta_usd = 0.0  # target net zero
        current_delta = delta["net_delta_usd"]
        hedge_needed = current_delta - target_delta_usd  # positive = need to sell, negative = need to buy

        if self.hedge_via_btc and "BTC" in orderbooks:
            # Simple: hedge everything through BTC
            btc_book = orderbooks["BTC"]
            btc_price = btc_book["mid"]

            if hedge_needed > 0:
                # We're net long, need to sell/short BTC
                btc_size = hedge_needed / btc_price
                orders.append(HedgeOrder(
                    coin="BTC",
                    side="sell",
                    size=round(btc_size, 5),
                    price=btc_book["best_bid"],  # sell at bid for immediate fill
                    reason=f"Portfolio delta hedge: net delta ${current_delta:,.2f} -> target $0",
                    urgency="urgent" if abs(delta["delta_pct"]) > self.max_delta_pct else "normal",
                ))
            else:
                # We're net short, need to buy BTC
                btc_size = abs(hedge_needed) / btc_price
                orders.append(HedgeOrder(
                    coin="BTC",
                    side="buy",
                    size=round(btc_size, 5),
                    price=btc_book["best_ask"],  # buy at ask for immediate fill
                    reason=f"Portfolio delta hedge: net delta ${current_delta:,.2f} -> target $0",
                    urgency="urgent" if abs(delta["delta_pct"]) > self.max_delta_pct else "normal",
                ))
        else:
            # Per-coin hedging: reduce the largest directional positions
            per_coin = delta["per_coin_delta"]
            sorted_positions = sorted(per_coin.items(), key=lambda x: abs(x[1]), reverse=True)

            remaining_hedge = abs(hedge_needed)

            for coin, signed_ntl in sorted_positions:
                if remaining_hedge <= 0:
                    break
                if coin not in orderbooks:
                    continue

                book = orderbooks[coin]

                # How much to hedge on this coin
                coin_hedge = min(abs(signed_ntl) * 0.5, remaining_hedge)
                # Only hedge 50% per coin per cycle to avoid overshooting

                coin_price = book["mid"]
                if coin_price <= 0:
                    continue

                coin_size = coin_hedge / coin_price

                if signed_ntl > 0:
                    # Coin is net long, sell some
                    orders.append(HedgeOrder(
                        coin=coin,
                        side="sell",
                        size=round(coin_size, 6),
                        price=book["best_bid"],
                        reason=f"Reduce long delta on {coin}: ${signed_ntl:,.2f}",
                    ))
                else:
                    # Coin is net short, buy some
                    orders.append(HedgeOrder(
                        coin=coin,
                        side="buy",
                        size=round(coin_size, 6),
                        price=book["best_ask"],
                        reason=f"Reduce short delta on {coin}: ${signed_ntl:,.2f}",
                    ))

                remaining_hedge -= coin_hedge

        if orders:
            self._last_hedge_time = now

        return orders

    def should_accept_fill(
        self,
        coin: str,
        side: str,  # "buy" or "sell"
        size: float,
        positions: list[Position],
    ) -> bool:
        """
        Pre-trade check: would this fill push delta beyond max tolerance?
        Call this before placing market-making orders.

        Returns True if the trade is acceptable, False if it would breach delta limits.
        """
        delta = self.compute_portfolio_delta(positions)
        current_delta = delta["net_delta_usd"]

        # Estimate the impact of this fill
        # We need to know the approximate price; use 0 as placeholder
        # In practice, pass the current mark price
        per_coin = delta["per_coin_delta"]
        coin_delta = per_coin.get(coin, 0.0)

        # Conservative estimate: assume the trade makes things worse
        if side == "buy":
            projected_delta = current_delta + size  # increases long exposure
        else:
            projected_delta = current_delta - size  # increases short exposure

        projected_pct = abs(projected_delta / self.equity * 100) if self.equity > 0 else 0
        return projected_pct <= self.max_delta_pct

    def get_skew_adjustment(
        self,
        coin: str,
        positions: list[Position],
    ) -> tuple[float, float]:
        """
        Compute bid/ask skew adjustments based on current inventory.

        If we're long a coin, we should:
        - Make our ask more aggressive (easier to sell/reduce position)
        - Make our bid less aggressive (harder to buy/increase position)

        Returns (bid_offset_bps, ask_offset_bps) to ADD to the base spread.
        Positive offset = wider (less aggressive), negative = tighter (more aggressive).
        """
        delta = self.compute_portfolio_delta(positions)
        per_coin = delta["per_coin_delta"]
        coin_delta = per_coin.get(coin, 0.0)

        if self.equity <= 0:
            return 0.0, 0.0

        # Inventory skew as fraction of equity
        inventory_pct = coin_delta / self.equity

        # Skew factor: how much to adjust quotes (bps per 1% inventory)
        skew_factor = 2.0  # 2 bps per 1% of equity in that coin

        bid_offset = inventory_pct * 100 * skew_factor  # positive when long = widen bid
        ask_offset = -inventory_pct * 100 * skew_factor  # negative when long = tighten ask

        return bid_offset, ask_offset


def example_usage():
    """Demonstrate delta hedger usage."""
    print("Delta Hedger Example")
    print("=" * 60)

    # Simulate positions
    positions = [
        Position("BTC", 0.001, 66000, 66500, unrealized_pnl=0.50),
        Position("ETH", 0.05, 2050, 2048, unrealized_pnl=-0.10),
        Position("SOL", -2.0, 79.0, 78.5, unrealized_pnl=1.00),
        Position("HYPE", 10.0, 34.0, 34.5, unrealized_pnl=5.00),
    ]

    hedger = DeltaHedger(
        equity=118.0,
        max_delta_pct=5.0,
        rebalance_threshold_pct=3.0,
        hedge_via_btc=False,
    )

    # Compute delta
    delta = hedger.compute_portfolio_delta(positions)
    print(f"\nPortfolio Delta:")
    print(f"  Net Delta: ${delta['net_delta_usd']:.2f}")
    print(f"  Gross Exposure: ${delta['gross_exposure_usd']:.2f}")
    print(f"  Delta %: {delta['delta_pct']:.2f}%")
    print(f"  Needs Hedge: {delta['needs_hedge']}")
    print(f"  Per-coin delta:")
    for coin, d in delta["per_coin_delta"].items():
        print(f"    {coin:>10}: ${d:>10.2f}")

    # Compute skew adjustments
    print(f"\nSkew Adjustments:")
    for pos in positions:
        bid_adj, ask_adj = hedger.get_skew_adjustment(pos.coin, positions)
        print(f"  {pos.coin:>10}: bid_offset={bid_adj:+.2f}bps  ask_offset={ask_adj:+.2f}bps")

    # Compute hedge orders
    orderbooks = {
        "BTC": {"best_bid": 66490, "best_ask": 66510, "mid": 66500},
        "ETH": {"best_bid": 2047, "best_ask": 2049, "mid": 2048},
        "SOL": {"best_bid": 78.4, "best_ask": 78.6, "mid": 78.5},
        "HYPE": {"best_bid": 34.4, "best_ask": 34.6, "mid": 34.5},
    }

    orders = hedger.compute_hedge_orders(positions, orderbooks)
    if orders:
        print(f"\nHedge Orders:")
        for o in orders:
            print(f"  {o.coin} {o.side} {o.size} @ {o.price} ({o.reason})")
    else:
        print("\nNo hedge needed.")

    # Pre-trade check
    print(f"\nPre-trade checks:")
    for coin, side, size in [("BTC", "buy", 10.0), ("BTC", "sell", 5.0), ("DOGE", "buy", 50.0)]:
        ok = hedger.should_accept_fill(coin, side, size, positions)
        print(f"  {coin} {side} ${size}: {'ACCEPT' if ok else 'REJECT'}")


if __name__ == "__main__":
    example_usage()
