#!/usr/bin/env python3
"""
Trader.py Integration Patches

This file shows the exact code to add/modify in trader.py to integrate:
1. CVXPY portfolio optimization for capital allocation
2. Delta hedging to maintain neutrality
3. Inventory-based quote skewing
4. Position size limits based on optimization

HOW TO USE:
- Copy the relevant classes/functions into trader.py
- Replace the existing order placement logic with the optimized version
- Add the periodic optimization and hedge loops

This is NOT a standalone file — it's a reference for what to change in trader.py.
"""

# =============================================================================
# IMPORTS TO ADD AT TOP OF trader.py
# =============================================================================
IMPORTS = """
# Add these imports to trader.py
import numpy as np
from mm_optimization.portfolio_optimizer import MarketMakerOptimizer, estimate_spreads_from_orderbook, build_correlation_matrix
from mm_optimization.delta_hedge import DeltaHedger, Position, HedgeOrder
"""

# =============================================================================
# CONFIGURATION CONSTANTS TO ADD
# =============================================================================
CONFIG = """
# Portfolio optimization config
OPTIMIZATION_INTERVAL = 300  # Re-optimize allocation every 5 minutes
MAX_LEVERAGE = 3.0           # Maximum total leverage (3x for $118 = $354 total notional)
MAX_SINGLE_POSITION_PCT = 0.15  # Max 15% of equity per coin ($17.70)
DELTA_TOLERANCE = 0.03       # Max 3% net delta
REBALANCE_THRESHOLD = 0.03   # Trigger hedge at 3% net delta
MIN_VOLUME_24H = 1_000_000   # Only trade coins with >$1M 24h volume
MIN_COINS = 5                # Minimum number of coins to trade
RISK_AVERSION = 2.0          # Higher = more conservative
HEDGE_VIA_BTC = False        # Hedge per-coin (True = hedge all via BTC)
QUOTE_SKEW_FACTOR = 2.0      # bps per 1% inventory
"""


# =============================================================================
# CLASS: OptimizedMarketMaker
# =============================================================================
class OptimizedMarketMaker:
    """
    Drop-in replacement for the core trading logic in trader.py.
    Wraps the existing exchange connection with optimization + hedging.

    Usage in trader.py:
        omm = OptimizedMarketMaker(exchange, info, equity=118.0)
        while True:
            omm.run_cycle()
            time.sleep(5)
    """

    def __init__(self, exchange, info, equity: float = 118.0):
        """
        Args:
            exchange: Hyperliquid exchange SDK instance (for placing orders)
            info: Hyperliquid info SDK instance (for querying state)
            equity: Account equity in USD
        """
        self.exchange = exchange
        self.info = info
        self.equity = equity

        # Initialize optimizer
        self.optimizer = None
        self.current_allocation = {}
        self.last_optimization_time = 0

        # Initialize hedger
        self.hedger = DeltaHedger(
            equity=equity,
            max_delta_pct=5.0,
            rebalance_threshold_pct=3.0,
            hedge_via_btc=False,
        )

        # State
        self.active_coins = []
        self.market_data = {}

    def update_market_data(self):
        """Fetch current market metadata from HL API."""
        import requests

        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs"},
            timeout=10,
        )
        data = resp.json()
        meta = data[0]["universe"]
        ctxs = data[1]

        self.market_data = {}
        for m, c in zip(meta, ctxs):
            coin = m["name"]
            vol = float(c.get("dayNtlVlm", "0"))
            if vol >= 1_000_000:  # MIN_VOLUME_24H
                self.market_data[coin] = {
                    "volume": vol,
                    "funding": float(c.get("funding", "0")),
                    "markPx": float(c.get("markPx", "0")),
                    "szDecimals": m.get("szDecimals", 0),
                    "maxLeverage": m.get("maxLeverage", 20),
                }

    def get_current_positions(self) -> list:
        """Fetch current positions from HL API."""
        import requests

        # Replace ADDRESS with your actual address
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "clearinghouseState", "user": "YOUR_ADDRESS_HERE"},
            timeout=10,
        )
        data = resp.json()

        positions = []
        for p in data.get("assetPositions", []):
            pos = p["position"]
            sz = float(pos.get("szi", "0"))
            if sz != 0:
                positions.append(Position(
                    coin=pos["coin"],
                    size=sz,
                    entry_price=float(pos.get("entryPx", "0")),
                    mark_price=float(pos.get("markPx", pos.get("entryPx", "0"))),
                    unrealized_pnl=float(pos.get("unrealizedPnl", "0")),
                ))

        # Update equity from live data
        self.equity = float(data.get("marginSummary", {}).get("accountValue", self.equity))
        self.hedger.equity = self.equity

        return positions

    def get_orderbook(self, coin: str) -> dict:
        """Fetch L2 orderbook for a coin."""
        import requests

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
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": (best_bid + best_ask) / 2,
                "spread_bps": (best_ask - best_bid) / ((best_bid + best_ask) / 2) * 10000,
            }
        return None

    def run_optimization(self):
        """Run CVXPY portfolio optimization to determine allocation."""
        import time as _time

        now = _time.time()
        if now - self.last_optimization_time < 300:  # OPTIMIZATION_INTERVAL
            return

        self.update_market_data()
        if not self.market_data:
            return

        # Select top coins by volume
        sorted_coins = sorted(
            self.market_data.items(),
            key=lambda x: x[1]["volume"],
            reverse=True,
        )[:15]

        coins = [c for c, _ in sorted_coins]
        n = len(coins)
        if n < 3:
            return

        # Estimate spreads from orderbooks
        spreads = estimate_spreads_from_orderbook(coins)

        # Volume scores (normalized)
        volumes = np.array([self.market_data[c]["volume"] for c in coins])
        volume_scores = volumes / volumes.max()

        # Funding rates
        funding_rates = np.array([self.market_data[c]["funding"] for c in coins])

        # Correlation matrix (simplified)
        corr = build_correlation_matrix(n)

        # Run optimization
        optimizer = MarketMakerOptimizer(
            coins=coins,
            equity=self.equity,
            max_leverage=3.0,
            max_single_position_pct=0.15,
            min_coins=5,
            delta_tolerance=0.03,
        )

        try:
            results = optimizer.optimize_allocation(
                spread_estimates=spreads / 10000,
                volume_scores=volume_scores,
                funding_rates=funding_rates,
                correlation_matrix=corr,
                risk_aversion=2.0,
            )

            self.current_allocation = results["weights"]
            self.active_coins = list(results["weights"].keys())
            self.last_optimization_time = now

            print(f"[OPT] Optimized: {len(self.active_coins)} coins, "
                  f"leverage={results['summary']['total_leverage']:.2f}x, "
                  f"delta={results['summary']['net_delta_pct']:.2f}%")

        except Exception as e:
            print(f"[OPT] Optimization failed: {e}")

    def compute_quote_prices(
        self,
        coin: str,
        mid_price: float,
        base_spread_bps: float,
        positions: list,
    ) -> tuple:
        """
        Compute bid and ask prices with inventory skew.

        Returns (bid_price, ask_price)
        """
        # Get skew adjustment based on current inventory
        bid_offset, ask_offset = self.hedger.get_skew_adjustment(coin, positions)

        # Apply skew to spread
        half_spread = base_spread_bps / 2

        bid_bps = half_spread + bid_offset  # wider when long
        ask_bps = half_spread + ask_offset  # tighter when long

        # Ensure minimum spread
        bid_bps = max(bid_bps, 1.0)  # at least 1 bps
        ask_bps = max(ask_bps, 1.0)

        bid_price = mid_price * (1 - bid_bps / 10000)
        ask_price = mid_price * (1 + ask_bps / 10000)

        return bid_price, ask_price

    def compute_order_size(self, coin: str) -> float:
        """Compute order size based on optimization allocation."""
        if coin not in self.current_allocation:
            return 0.0

        alloc = self.current_allocation[coin]
        target_notional = alloc["total"] * self.equity

        # Get current price
        mark_px = self.market_data.get(coin, {}).get("markPx", 0)
        if mark_px <= 0:
            return 0.0

        size = target_notional / mark_px

        # Round to size decimals
        sz_decimals = self.market_data.get(coin, {}).get("szDecimals", 0)
        size = round(size, sz_decimals)

        return size

    def run_hedge_cycle(self, positions: list):
        """Check and execute delta hedges if needed."""
        delta = self.hedger.compute_portfolio_delta(positions)

        if not delta["needs_hedge"]:
            return

        # Get orderbooks for coins with positions
        orderbooks = {}
        for pos in positions:
            book = self.get_orderbook(pos.coin)
            if book:
                orderbooks[pos.coin] = book

        orders = self.hedger.compute_hedge_orders(positions, orderbooks)

        for order in orders:
            print(f"[HEDGE] {order.coin} {order.side} {order.size} @ {order.price} - {order.reason}")
            # Execute via exchange SDK:
            # self.exchange.order(order.coin, order.side == "buy", order.size, order.price, {"limit": {"tif": "Ioc"}})

    def run_cycle(self):
        """
        Main trading cycle. Call this every 5-10 seconds.

        1. Update optimization (every 5 min)
        2. Get current positions
        3. Run delta hedge if needed
        4. Place/update market-making quotes
        """
        import time as _time

        try:
            # Step 1: Periodic optimization
            self.run_optimization()

            if not self.active_coins:
                print("[CYCLE] No allocation computed yet, skipping")
                return

            # Step 2: Get current positions
            positions = self.get_current_positions()

            # Step 3: Delta hedge
            self.run_hedge_cycle(positions)

            # Step 4: Place quotes for each allocated coin
            for coin in self.active_coins:
                if coin not in self.current_allocation:
                    continue

                alloc = self.current_allocation[coin]
                if alloc["total"] < 0.005:
                    continue

                book = self.get_orderbook(coin)
                if not book:
                    continue

                # Pre-trade delta check
                order_size = self.compute_order_size(coin)
                if order_size <= 0:
                    continue

                # Compute skewed quotes
                base_spread = max(book["spread_bps"], 3.0)  # at least 3 bps
                bid_price, ask_price = self.compute_quote_prices(
                    coin, book["mid"], base_spread, positions
                )

                # Check if adding to this side would breach delta limits
                can_bid = self.hedger.should_accept_fill(
                    coin, "buy", order_size * book["mid"], positions
                )
                can_ask = self.hedger.should_accept_fill(
                    coin, "sell", order_size * book["mid"], positions
                )

                # Place orders (uncomment to actually trade):
                if can_bid:
                    print(f"[QUOTE] {coin} BID {order_size} @ {bid_price:.6f}")
                    # self.exchange.order(coin, True, order_size, bid_price, {"limit": {"tif": "Gtc"}})

                if can_ask:
                    print(f"[QUOTE] {coin} ASK {order_size} @ {ask_price:.6f}")
                    # self.exchange.order(coin, False, order_size, ask_price, {"limit": {"tif": "Gtc"}})

                _time.sleep(0.1)  # Rate limiting

        except Exception as e:
            print(f"[CYCLE] Error: {e}")


# =============================================================================
# MAIN LOOP (replace existing main loop in trader.py)
# =============================================================================
MAIN_LOOP = """
# Replace the existing main loop in trader.py with:

import time
from mm_optimization.trader_patches import OptimizedMarketMaker

# Initialize (use your existing exchange/info SDK instances)
omm = OptimizedMarketMaker(
    exchange=exchange,  # your existing Hyperliquid exchange instance
    info=info,          # your existing Hyperliquid info instance
    equity=118.0,       # will be updated from API
)

print("[BOOT] Starting optimized market maker...")
print(f"[BOOT] Equity: ${omm.equity}")
print(f"[BOOT] Max leverage: 3.0x")
print(f"[BOOT] Delta tolerance: 3%")

while True:
    try:
        omm.run_cycle()
        time.sleep(5)  # 5 second cycle time
    except KeyboardInterrupt:
        print("[STOP] Shutting down...")
        # Cancel all open orders before exit
        # exchange.cancel_all()
        break
    except Exception as e:
        print(f"[ERROR] {e}")
        time.sleep(10)
"""


if __name__ == "__main__":
    print("=" * 70)
    print("TRADER.PY INTEGRATION GUIDE")
    print("=" * 70)
    print()
    print("1. Copy mm_optimization/ folder to VPS alongside trader.py")
    print("2. Install dependencies: pip install cvxpy numpy requests")
    print("3. Add imports from this file to trader.py")
    print("4. Replace main trading loop with OptimizedMarketMaker")
    print("5. Set YOUR_ADDRESS_HERE in get_current_positions()")
    print("6. Uncomment the exchange.order() calls when ready to trade live")
    print()
    print("KEY CHANGES FROM CURRENT trader.py:")
    print("  - Orders are skewed based on inventory (reduces adverse selection)")
    print("  - Delta is monitored and hedged automatically")
    print("  - Capital allocation is optimized via CVXPY every 5 min")
    print("  - Pre-trade checks prevent exceeding delta limits")
    print("  - Only trades high-volume coins (>$1M/day)")
    print()
    print(IMPORTS)
    print(CONFIG)
    print(MAIN_LOOP)
