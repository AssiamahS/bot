#!/usr/bin/env python3
"""
Gamma Allocator — Execution bridge between the gamma forecast optimizer and Hyperliquid.

Takes optimizer output (target weights) and executes trades to reach target allocation.
Runs as a standalone process or integrates into trader.py's main loop.

Usage:
    # Standalone: compute optimal allocation and execute
    python3 gamma_allocator.py

    # Dry run (show what it would do, no trades)
    python3 gamma_allocator.py --dry-run

    # Custom params
    python3 gamma_allocator.py --risk-aversion 2.0 --max-weight 0.50 --rebalance-threshold 5.0
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np

# Add mm_optimization to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "mcp-bot-control"))
from mm_optimization.gamma_forecast_optimizer import (
    compute_log_returns,
    estimate_covariance,
    fetch_candles,
    fetch_universe,
    fit_gamma_forecast,
    optimize_allocation,
)

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gamma_state.json")


def load_config():
    with open(CONFIG_FILE) as f:
        config = json.load(f)

    def _cfg(key, default=""):
        val = config.get(key, default)
        if val == "ENV":
            return os.environ.get(key.upper(), os.environ.get(key, default))
        return val

    return config, _cfg


def setup_exchange():
    config, _cfg = load_config()
    private_key = _cfg("wallet_private_key")
    address = config.get("wallet_address", "")
    use_testnet = config.get("use_testnet", False)

    account = Account.from_key(private_key)
    base = constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL
    info = Info(base, skip_ws=True)
    exchange = Exchange(account, base)

    return info, exchange, address


def get_account_state(info, address):
    """Get current equity, positions, and margin.

    On Hyperliquid, spot USDC serves as cross-margin collateral for perps.
    We combine perps account value + spot USDC to get true available equity.
    """
    state = info.user_state(address)
    margin = state.get("marginSummary", {})
    perps_equity = float(margin.get("accountValue", 0))

    # Spot USDC also counts as collateral
    spot_equity = 0.0
    try:
        spot_state = info.spot_user_state(address)
        for bal in spot_state.get("balances", []):
            if bal.get("coin") == "USDC":
                spot_equity = float(bal.get("total", 0))
                break
    except Exception:
        pass

    equity = perps_equity + spot_equity

    positions = {}
    for pos in state.get("assetPositions", []):
        p = pos.get("position", {})
        coin = p.get("coin", "")
        size = float(p.get("szi", 0))
        entry_px = float(p.get("entryPx", 0))
        notional = abs(size) * entry_px
        unrealized = float(p.get("unrealizedPnl", 0))
        positions[coin] = {
            "size": size,
            "entry_px": entry_px,
            "notional": notional,
            "unrealized_pnl": unrealized,
            "side": "long" if size > 0 else "short" if size < 0 else "flat",
        }

    return {"equity": equity, "perps_equity": perps_equity, "spot_equity": spot_equity, "positions": positions}


def get_asset_metadata(info):
    """Fetch size decimals and price info for all coins."""
    meta = info.meta()
    universe = meta.get("universe", [])
    size_decimals = {}
    for asset in universe:
        size_decimals[asset["name"]] = asset.get("szDecimals", 2)
    return size_decimals


def get_mid_prices(info, coins):
    """Get current mid prices for a list of coins."""
    prices = {}
    for coin in coins:
        try:
            book = info.l2_snapshot(coin)
            levels = book.get("levels", [[], []])
            if levels[0] and levels[1]:
                bid = float(levels[0][0]["px"])
                ask = float(levels[1][0]["px"])
                prices[coin] = (bid + ask) / 2
        except Exception as e:
            print(f"  Price fetch error for {coin}: {e}")
    return prices


def compute_target_allocation(
    coins=None,
    lookback_days=180,
    risk_aversion=2.0,
    max_weight=0.50,
    min_volume=2_000_000,
    top_n=10,
):
    """Run gamma forecast optimizer and return target weights."""
    if coins is None:
        # Auto-select top coins by volume
        universe = fetch_universe()
        eligible = [c for c in universe if c["volume_24h"] >= min_volume and c["mark_price"] > 0]
        eligible.sort(key=lambda x: x["volume_24h"], reverse=True)
        coins = [c["coin"] for c in eligible[:top_n]]

    print(f"  Computing gamma forecast for {coins} ({lookback_days}d lookback)...")

    returns_dict = {}
    forecasts = {}
    for coin in coins:
        try:
            candles = fetch_candles(coin, "1d", lookback_days)
            rets = compute_log_returns(candles)
            if len(rets) >= 30:
                returns_dict[coin] = rets
                forecasts[coin] = fit_gamma_forecast(rets)
            time.sleep(0.1)
        except Exception as e:
            print(f"  Skipping {coin}: {e}")

    active = list(returns_dict.keys())
    if len(active) < 2:
        raise ValueError(f"Need at least 2 coins with data, got {len(active)}")

    cov_coins, cov_matrix = estimate_covariance(returns_dict)
    gamma_daily = np.array([forecasts[c]["gamma_daily_return"] for c in cov_coins])

    result = optimize_allocation(
        coins=cov_coins,
        expected_returns=gamma_daily,
        cov_matrix=cov_matrix,
        risk_aversion=risk_aversion,
        max_weight=max_weight,
    )

    return result["weights"], forecasts, result["portfolio"]


def compute_trades(
    target_weights,
    current_positions,
    equity,
    mid_prices,
    size_decimals,
    rebalance_threshold_pct=5.0,
):
    """
    Compare target allocation to current positions and compute required trades.

    Returns list of trades: [{"coin": "ETH", "side": "buy", "size": 0.5, "notional": 900}, ...]
    """
    trades = []

    all_coins = set(list(target_weights.keys()) + list(current_positions.keys()))

    for coin in all_coins:
        target_weight = target_weights.get(coin, 0)
        target_notional = equity * target_weight
        mid_px = mid_prices.get(coin)

        if mid_px is None or mid_px <= 0:
            if target_weight > 0:
                print(f"  WARNING: No price for {coin}, skipping")
            continue

        # Current position notional (signed: positive = long)
        current_size = current_positions.get(coin, {}).get("size", 0)
        current_notional = current_size * mid_px

        # Delta needed
        delta_notional = target_notional - current_notional
        delta_pct = abs(delta_notional) / equity * 100 if equity > 0 else 0

        # Skip if within rebalance threshold
        if delta_pct < rebalance_threshold_pct:
            if delta_pct > 0.5:
                print(f"  {coin}: drift {delta_pct:.1f}% < threshold {rebalance_threshold_pct}%, skipping")
            continue

        # Compute size in coin units
        sz_dec = size_decimals.get(coin, 2)
        raw_size = abs(delta_notional) / mid_px
        size = round(raw_size, sz_dec)

        if size <= 0:
            continue

        # Minimum notional check ($1 minimum for HL)
        if size * mid_px < 1.0:
            print(f"  {coin}: trade too small (${size * mid_px:.2f}), skipping")
            continue

        is_buy = delta_notional > 0

        trades.append({
            "coin": coin,
            "side": "buy" if is_buy else "sell",
            "is_buy": is_buy,
            "size": size,
            "notional": round(abs(delta_notional), 2),
            "target_weight": target_weight,
            "current_notional": round(current_notional, 2),
            "target_notional": round(target_notional, 2),
            "mid_px": mid_px,
        })

    # Sort: sells first (free up margin), then buys by size
    trades.sort(key=lambda t: (t["is_buy"], -t["notional"]))
    return trades


def execute_trades(exchange, info, trades, address, use_limit=True):
    """Execute the computed trades on Hyperliquid."""
    results = []

    for trade in trades:
        coin = trade["coin"]
        is_buy = trade["is_buy"]
        size = trade["size"]
        side_str = "BUY" if is_buy else "SELL"

        try:
            if use_limit:
                # Place limit order at mid price (aggressive limit)
                book = info.l2_snapshot(coin)
                levels = book.get("levels", [[], []])
                if is_buy and levels[1]:
                    # Buy at best ask (cross the spread to fill immediately)
                    price = float(levels[1][0]["px"])
                elif not is_buy and levels[0]:
                    # Sell at best bid
                    price = float(levels[0][0]["px"])
                else:
                    print(f"  {coin}: no book, skipping")
                    continue

                print(f"  {side_str} {size} {coin} @ ${price:.4f} (${trade['notional']:.0f})")
                result = exchange.order(
                    coin, is_buy, size, price,
                    {"limit": {"tif": "Ioc"}},  # Immediate or Cancel — fills or dies
                )
            else:
                # Market order via market_close trick — not ideal, use IOC limit instead
                print(f"  {side_str} {size} {coin} MARKET (${trade['notional']:.0f})")
                result = exchange.order(
                    coin, is_buy, size, 0,
                    {"market": {}},
                )

            results.append({"trade": trade, "result": result, "status": "sent"})

            # Check fill
            if isinstance(result, dict):
                resp = result.get("response", {})
                data = resp.get("data", {}) if isinstance(resp, dict) else {}
                statuses = data.get("statuses", []) if isinstance(data, dict) else []
                if statuses:
                    s = statuses[0]
                    if isinstance(s, dict):
                        if "filled" in s:
                            print(f"    FILLED: {s['filled']}")
                        elif "resting" in s:
                            print(f"    RESTING: oid={s['resting']['oid']}")
                        elif "error" in s:
                            print(f"    ERROR: {s['error']}")

            time.sleep(0.3)  # rate limit buffer

        except Exception as e:
            print(f"  {coin} order error: {e}")
            results.append({"trade": trade, "result": str(e), "status": "error"})

    return results


def save_state(target_weights, portfolio_metrics, forecasts, trades_executed):
    """Save allocation state for tracking."""
    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target_weights": target_weights,
        "portfolio_metrics": portfolio_metrics,
        "forecasts_summary": {
            coin: {
                "gamma_annual_return": fc["gamma_annual_return"],
                "gamma_annual_vol": fc["gamma_annual_vol"],
                "gamma_sharpe": fc["gamma_sharpe"],
            }
            for coin, fc in forecasts.items()
            if fc.get("valid")
        },
        "trades_executed": len(trades_executed),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  State saved to {STATE_FILE}")


def run_allocator(
    coins=None,
    lookback_days=180,
    risk_aversion=2.0,
    max_weight=0.50,
    rebalance_threshold_pct=5.0,
    dry_run=False,
    min_volume=2_000_000,
    top_n=10,
):
    """Full pipeline: forecast -> optimize -> diff -> execute."""
    print("=" * 60)
    print("GAMMA ALLOCATOR — Forecast + Execute")
    print(f"{'DRY RUN' if dry_run else 'LIVE'} | Risk: {risk_aversion} | Max weight: {max_weight*100:.0f}%")
    print("=" * 60)

    # 1. Setup exchange connection
    print("\n[1/5] Connecting to Hyperliquid...")
    info, exchange, address = setup_exchange()
    size_decimals = get_asset_metadata(info)

    # 2. Get current account state
    print("\n[2/5] Reading account state...")
    account = get_account_state(info, address)
    equity = account["equity"]
    positions = account["positions"]

    print(f"  Equity: ${equity:.2f}")
    if positions:
        print(f"  Current positions:")
        for coin, pos in positions.items():
            print(f"    {coin}: {pos['side']} {pos['size']} (${pos['notional']:.2f}) pnl=${pos['unrealized_pnl']:.2f}")
    else:
        print(f"  No open positions")

    if equity < 5:
        print("\n  ERROR: Equity too low (< $5). Need to transfer USDC to perps margin.")
        print("  Transfer via Hyperliquid UI or API before running allocator.")
        return None

    # 3. Compute target allocation
    print("\n[3/5] Running gamma forecast optimizer...")
    target_weights, forecasts, port_metrics = compute_target_allocation(
        coins=coins,
        lookback_days=lookback_days,
        risk_aversion=risk_aversion,
        max_weight=max_weight,
        min_volume=min_volume,
        top_n=top_n,
    )

    print(f"\n  Target allocation (${equity:.2f} equity):")
    for coin, weight in sorted(target_weights.items(), key=lambda x: -x[1]):
        fc = forecasts.get(coin, {})
        ann = fc.get("gamma_annual_return", 0) * 100
        print(f"    {coin:>6}: {weight*100:>5.1f}% = ${weight*equity:>8.2f}  (gamma: {ann:+.1f}%/yr)")

    print(f"\n  Portfolio forecast: {port_metrics['expected_annual_return_pct']:+.1f}% annual | "
          f"{port_metrics['annual_volatility_pct']:.1f}% vol | "
          f"Sharpe {port_metrics['sharpe_ratio']:.2f}")

    # 4. Compute required trades
    print("\n[4/5] Computing trades...")
    mid_prices = get_mid_prices(info, list(target_weights.keys()))
    trades = compute_trades(
        target_weights, positions, equity, mid_prices, size_decimals, rebalance_threshold_pct
    )

    if not trades:
        print("  No trades needed — portfolio is within threshold.")
        save_state(target_weights, port_metrics, forecasts, [])
        return {"status": "aligned", "trades": 0}

    print(f"\n  Trades to execute:")
    total_notional = 0
    for t in trades:
        print(f"    {t['side'].upper():>4} {t['size']} {t['coin']} @ ~${t['mid_px']:.2f} "
              f"(${t['notional']:.0f}) | current=${t['current_notional']:.0f} -> target=${t['target_notional']:.0f}")
        total_notional += t["notional"]
    print(f"  Total trade notional: ${total_notional:.0f}")

    # 5. Execute
    if dry_run:
        print("\n[5/5] DRY RUN — no trades executed.")
        save_state(target_weights, port_metrics, forecasts, [])
        return {"status": "dry_run", "trades": trades}

    print("\n[5/5] Executing trades...")
    results = execute_trades(exchange, info, trades, address)

    filled = sum(1 for r in results if r["status"] == "sent")
    errors = sum(1 for r in results if r["status"] == "error")
    print(f"\n  Sent: {filled} | Errors: {errors}")

    save_state(target_weights, port_metrics, forecasts, results)

    # Verify final state
    time.sleep(1)
    final = get_account_state(info, address)
    print(f"\n  Final equity: ${final['equity']:.2f}")
    if final["positions"]:
        print(f"  Final positions:")
        for coin, pos in final["positions"].items():
            weight = target_weights.get(coin, 0)
            actual_weight = pos["notional"] / final["equity"] if final["equity"] > 0 else 0
            print(f"    {coin}: {pos['side']} ${pos['notional']:.2f} "
                  f"(actual: {actual_weight*100:.1f}% | target: {weight*100:.1f}%)")

    return {"status": "executed", "trades": results, "final_equity": final["equity"]}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gamma Allocator — Execute optimal portfolio")
    parser.add_argument("--coins", nargs="+", default=None, help="Specific coins (e.g. BTC ETH SOL)")
    parser.add_argument("--lookback", type=int, default=180, help="Historical lookback days")
    parser.add_argument("--risk-aversion", type=float, default=2.0, help="Risk aversion (higher=safer)")
    parser.add_argument("--max-weight", type=float, default=0.50, help="Max single position weight")
    parser.add_argument("--rebalance-threshold", type=float, default=5.0, help="Min drift pct to trigger rebalance")
    parser.add_argument("--min-volume", type=float, default=2_000_000, help="Min 24h volume for auto-select")
    parser.add_argument("--top", type=int, default=10, help="Top N coins by volume")
    parser.add_argument("--dry-run", action="store_true", help="Show trades without executing")
    args = parser.parse_args()

    run_allocator(
        coins=args.coins,
        lookback_days=args.lookback,
        risk_aversion=args.risk_aversion,
        max_weight=args.max_weight,
        rebalance_threshold_pct=args.rebalance_threshold,
        dry_run=args.dry_run,
        min_volume=args.min_volume,
        top_n=args.top,
    )
