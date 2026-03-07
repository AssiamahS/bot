#!/usr/bin/env python3
"""
Hyperliquid Market Maker.
Maker rebate (~0.002%) means tight spreads are profitable.
Posts limit orders at best bid/ask, collects maker rebates on fills.
Writes status to trader_status.json for dashboard.
"""
import time
import json
import signal
import os
import sys
import urllib.request
import urllib.parse
from typing import Optional

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

# Telegram alerts
TG_TOKEN = "8687483047:AAHTNtpdRdJbQub1Gaubnnz87BBdKbFkzNU"
TG_CHAT_ID = "8727843043"

def tg_send(msg):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data=data, timeout=5)
    except Exception:
        pass

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_status.json")

# Load config
with open(CONFIG_FILE) as f:
    config = json.load(f)

PRIVATE_KEY = config["wallet_private_key"]
WALLET_ADDRESS = config.get("wallet_address", "")
USE_TESTNET = config.get("use_testnet", True)
PAIRS = config.get("pairs", ["SOL-PERP", "BTC-PERP", "ETH-PERP"])
ORDER_SIZE_USD = config.get("order_size_usd", 10.0)
REFRESH_SECS = config.get("refresh_secs", 15)
MIN_SPREAD_BPS = config.get("min_spread_bps", 5)  # 5 bps = 0.05%

# Hyperliquid asset indices (mainnet)
# These map coin names to their index on Hyperliquid
COIN_MAP = {
    "BTC-PERP": "BTC",
    "ETH-PERP": "ETH",
    "SOL-PERP": "SOL",
}

# Size decimals per asset (Hyperliquid requires specific precision)
SIZE_DECIMALS = {
    "BTC": 5,
    "ETH": 4,
    "SOL": 2,
}

# Price tick sizes (Hyperliquid specific)
PRICE_DECIMALS = {
    "BTC": 0,  # $1 ticks
    "ETH": 1,  # $0.1 ticks
    "SOL": 2,  # $0.01 ticks
}

running = True
start_time = time.time()
all_fills = []
active_orders = []
last_prices = {}
last_balances = {}
pair_fills = {p: [] for p in PAIRS}
pair_trade_count = {p: 0 for p in PAIRS}
total_trade_count = 0
initial_portfolio_value = None

# Volatility tracking: store recent mid prices per coin to compute short-term vol
price_history = {COIN_MAP.get(p, p.replace("-PERP", "")): [] for p in PAIRS}
VOL_WINDOW = 20  # number of cycles to track (~5 min at 15s refresh)

# Risk governor limits
MAX_DRAWDOWN = 0.15       # 15% equity drop -> pause trading
MAX_INVENTORY_USD = 40    # max exposure per asset before pause
MAX_VOLATILITY_BPS = 50   # 50bps short-term vol -> pause (0.5% moves)
COOLDOWN_SECS = 30        # how long to pause when risk triggers
risk_cooldown_until = 0   # timestamp when cooldown ends


def signal_handler(sig, frame):
    global running
    print("\nShutting down...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def setup_exchange():
    """Initialize Hyperliquid connection."""
    if not PRIVATE_KEY:
        print("ERROR: Set wallet_private_key in config.json")
        print("  1. Go to app.hyperliquid.xyz")
        print("  2. Create an API sub-account (or use your main wallet)")
        print("  3. Export the private key for the signing wallet")
        print("  4. Paste it into config.json")
        sys.exit(1)

    account = Account.from_key(PRIVATE_KEY)
    address = WALLET_ADDRESS if WALLET_ADDRESS else account.address

    base_url = constants.TESTNET_API_URL if USE_TESTNET else constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)

    # If API wallet != main wallet, pass account_address so SDK signs as agent
    if account.address.lower() != address.lower():
        exchange = Exchange(account, base_url, account_address=address)
        print(f"  Agent wallet: {account.address}")
    else:
        exchange = Exchange(account, base_url)

    print(f"  Connected to {'TESTNET' if USE_TESTNET else 'MAINNET'}")
    print(f"  Main wallet: {address}")

    return info, exchange, address


def get_mid_price(info, coin):
    """Get current mid price and top-of-book depth for a coin."""
    try:
        l2 = info.l2_snapshot(coin)
        if l2 and len(l2["levels"]) == 2:
            bids = l2["levels"][0]
            asks = l2["levels"][1]
            if bids and asks:
                best_bid = float(bids[0]["px"])
                best_ask = float(asks[0]["px"])
                # Sum top 3 levels of depth for imbalance signal
                bid_depth = sum(float(b["sz"]) for b in bids[:3])
                ask_depth = sum(float(a["sz"]) for a in asks[:3])
                return {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "mid": (best_bid + best_ask) / 2,
                    "spread": best_ask - best_bid,
                    "bid_size": float(bids[0]["sz"]),
                    "ask_size": float(asks[0]["sz"]),
                    "bid_depth": bid_depth,
                    "ask_depth": ask_depth,
                }
    except Exception as e:
        print(f"  Price error for {coin}: {e}")
    return None


def get_volatility_bps(coin, mid):
    """Track mid prices and return recent volatility in bps.
    Uses standard deviation of returns over VOL_WINDOW cycles."""
    history = price_history.get(coin, [])
    history.append(mid)
    # Keep only recent history
    if len(history) > VOL_WINDOW:
        history = history[-VOL_WINDOW:]
    price_history[coin] = history

    if len(history) < 3:
        return 0  # not enough data yet

    # Calculate returns in bps
    returns = []
    for i in range(1, len(history)):
        ret = (history[i] - history[i - 1]) / history[i - 1] * 10000
        returns.append(ret)

    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
    return variance ** 0.5  # stdev in bps


def orderbook_imbalance(price_data):
    """Return imbalance ratio: positive = more bid pressure (bullish), negative = more ask pressure.
    Range roughly -1 to +1."""
    bid_d = price_data.get("bid_depth", 0)
    ask_d = price_data.get("ask_depth", 0)
    total = bid_d + ask_d
    if total == 0:
        return 0
    return (bid_d - ask_d) / total


def risk_check(equity, vol_bps, inventory_usd):
    """Risk governor: returns reason string if trading should pause, else None."""
    if initial_portfolio_value and initial_portfolio_value > 0:
        drawdown = (initial_portfolio_value - equity) / initial_portfolio_value
        if drawdown > MAX_DRAWDOWN:
            return f"drawdown {drawdown*100:.1f}%"
    if inventory_usd > MAX_INVENTORY_USD:
        return f"inventory ${inventory_usd:.0f} > ${MAX_INVENTORY_USD}"
    if vol_bps > MAX_VOLATILITY_BPS:
        return f"volatility {vol_bps:.0f}bps"
    return None


def quote_score(spread_bps, imbalance, vol_bps, inventory_ratio):
    """Score the quoting setup. Only quote when score >= 2 out of 4."""
    score = 0
    if spread_bps >= 8:
        score += 1  # spread wide enough to profit
    if abs(imbalance) > 0.15:
        score += 1  # meaningful orderbook signal
    if vol_bps < 30:
        score += 1  # not too volatile
    if abs(inventory_ratio) < 0.6:
        score += 1  # inventory manageable
    return score


def get_account_state(info, address):
    """Get account balances and positions. Checks both perps and spot."""
    global last_balances
    try:
        state = info.user_state(address)
        if state:
            margin = state.get("marginSummary", {})
            perps_value = float(margin.get("accountValue", 0))

            # Also check spot balance (portfolio margin uses spot USDC as collateral)
            spot_usdc = 0
            try:
                spot = info.spot_user_state(address)
                for b in spot.get("balances", []):
                    if b["coin"] == "USDC" and float(b["total"]) > 0:
                        spot_usdc = float(b["total"])
            except Exception:
                pass

            total_value = perps_value + spot_usdc

            last_balances = {
                "account_value": total_value,
                "perps_value": perps_value,
                "spot_usdc": spot_usdc,
                "total_margin": float(margin.get("totalMarginUsed", 0)),
                "withdrawable": float(margin.get("totalNtlPos", 0)),
            }

            positions = {}
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                coin = p.get("coin", "")
                size = float(p.get("szi", 0))
                entry = float(p.get("entryPx", 0))
                upnl = float(p.get("unrealizedPnl", 0))
                if size != 0:
                    positions[coin] = {
                        "size": size,
                        "entry_price": entry,
                        "unrealized_pnl": upnl,
                    }

            last_balances["positions"] = positions
            return state
    except Exception as e:
        print(f"  Account state error: {e}")
    return None


def cancel_all_orders(exchange, info, address):
    """Cancel all open orders."""
    try:
        open_orders = info.open_orders(address)
        if open_orders:
            for order in open_orders:
                coin = order.get("coin", "")
                oid = order.get("oid", 0)
                try:
                    exchange.cancel(coin, oid)
                except Exception:
                    pass
            print(f"  Cancelled {len(open_orders)} orders")
            return len(open_orders)
    except Exception as e:
        print(f"  Cancel error: {e}")
    return 0


def place_order(exchange, coin, is_buy, size, price, reduce_only=False):
    """Place a limit post-only order."""
    try:
        order_result = exchange.order(
            coin,
            is_buy,
            size,
            price,
            {"limit": {"tif": "Alo"}},  # Add Liquidity Only = maker only
            reduce_only=reduce_only,
        )
        # SDK may return a string error or a dict
        if isinstance(order_result, str):
            print(f"  Order rejected: {order_result}")
            return None
        resp = order_result.get("response", {}) if isinstance(order_result, dict) else {}
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        statuses = data.get("statuses", []) if isinstance(data, dict) else []
        if statuses and isinstance(statuses[0], dict) and "resting" in statuses[0]:
            oid = statuses[0]["resting"]["oid"]
            side = "BUY" if is_buy else "SELL"
            print(f"  {side:4s} {size} {coin} @ ${price} -> {oid}")
            active_orders.append({
                "pair": f"{coin}-PERP",
                "side": "buy" if is_buy else "sell",
                "volume": size,
                "price": price,
                "id": str(oid),
            })
            return oid
        elif statuses and isinstance(statuses[0], dict) and "error" in statuses[0]:
            print(f"  Order rejected: {statuses[0]['error']}")
    except Exception as e:
        print(f"  Order error: {e}")
    return None


def check_fills(info, address):
    """Check for recent fills."""
    global total_trade_count
    try:
        fills = info.user_fills(address)
        # Only process fills since bot started
        new_fills = [f for f in fills if float(f.get("time", 0)) / 1000 > start_time]
        new_count = len(new_fills)

        if new_count > total_trade_count:
            for f in new_fills[total_trade_count:]:
                coin = f.get("coin", "")
                side = f.get("side", "")
                price = float(f.get("px", 0))
                size = float(f.get("sz", 0))
                fee = float(f.get("fee", 0))
                cost = price * size
                closed_pnl = float(f.get("closedPnl", 0))

                print(f"  >>> FILL: {side} {size} {coin} @ ${price:.2f} fee=${fee:.4f} pnl=${closed_pnl:.4f}")
                emoji = "🟢" if side.lower() == "buy" else "🔴"
                rebate_str = f"Rebate: +${-fee:.4f}" if fee < 0 else f"Fee: ${fee:.4f}"
                tg_send(f"{emoji} <b>FILL</b>: {side.upper()} {size} {coin}\n💰 @ ${price:.2f} | {rebate_str} | PnL: ${closed_pnl:.4f}")

                fill = {
                    "time": time.time(),
                    "side": side.lower(),
                    "pair": f"{coin}-PERP",
                    "volume": size,
                    "price": price,
                    "cost": cost,
                    "fee": fee,
                    "closed_pnl": closed_pnl,
                }
                all_fills.append(fill)

                for p in PAIRS:
                    if coin in p:
                        pair_fills[p].append(fill)
                        pair_trade_count[p] += 1
                        break

            total_trade_count = new_count
    except Exception as e:
        print(f"  Fills error: {e}")


def portfolio_value():
    """Total account value."""
    return last_balances.get("account_value", 0)


def write_status():
    """Write status JSON for dashboard."""
    pv = portfolio_value()
    global initial_portfolio_value
    if initial_portfolio_value is None and pv > 0:
        initial_portfolio_value = pv

    portfolio_pnl = pv - initial_portfolio_value if initial_portfolio_value else 0
    # Bot PnL = sum of realized PnL from fills (what the bot actually earned)
    bot_realized_pnl = sum(f.get("closed_pnl", 0) for f in all_fills)
    bot_fees = sum(f["fee"] for f in all_fills)
    bot_net_pnl = bot_realized_pnl - bot_fees  # pnl after fees

    pair_status = {}
    for p in PAIRS:
        coin = COIN_MAP.get(p, p.replace("-PERP", ""))
        pos = last_balances.get("positions", {}).get(coin, {})
        pair_status[p] = {
            "trade_count": pair_trade_count[p],
            "fees_paid": round(sum(f["fee"] for f in pair_fills[p]), 6),
            "volume_traded": round(sum(f["cost"] for f in pair_fills[p]), 2),
            "holding": pos.get("size", 0),
            "holding_usd": round(abs(pos.get("size", 0)) * last_prices.get(p, {}).get("mid", 0), 2),
            "unrealized_pnl": round(pos.get("unrealized_pnl", 0), 4),
            "entry_price": pos.get("entry_price", 0),
            "recent_fills": pair_fills[p][-10:],
        }

    status = {
        "running": running,
        "exchange": "Hyperliquid",
        "network": "testnet" if USE_TESTNET else "mainnet",
        "uptime_min": round((time.time() - start_time) / 60, 1),
        "portfolio_value": round(pv, 2),
        "portfolio_pnl": round(portfolio_pnl, 4),
        "bot_realized_pnl": round(bot_realized_pnl, 6),
        "bot_net_pnl": round(bot_net_pnl, 6),
        "initial_portfolio_value": round(initial_portfolio_value, 2) if initial_portfolio_value else None,
        "total_trade_count": total_trade_count,
        "total_fees": round(sum(f["fee"] for f in all_fills), 6),
        "total_rebates": round(sum(-f["fee"] for f in all_fills if f["fee"] < 0), 6),
        "pairs": PAIRS,
        "pair_status": pair_status,
        "order_size_usd": ORDER_SIZE_USD,
        "active_orders": active_orders,
        "recent_fills": all_fills[-20:],
        "balances": last_balances,
        "prices": last_prices,
        "updated_at": time.time(),
    }
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f)
    except Exception:
        pass


def run_cycle(info, exchange, address):
    """Run one trading cycle."""
    global active_orders, risk_cooldown_until

    # Risk cooldown check
    if time.time() < risk_cooldown_until:
        remaining = int(risk_cooldown_until - time.time())
        print(f"  RISK COOLDOWN: {remaining}s remaining")
        write_status()
        return

    # Cancel existing orders
    cancel_all_orders(exchange, info, address)
    active_orders = []

    # Get account state
    get_account_state(info, address)
    account_value = portfolio_value()

    # Get prices and place orders
    for pair in PAIRS:
        coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
        price_data = get_mid_price(info, coin)
        if not price_data:
            continue

        last_prices[pair] = price_data
        mid = price_data["mid"]
        best_bid = price_data["best_bid"]
        best_ask = price_data["best_ask"]
        market_spread = price_data["spread"]

        # VOLATILITY-ADAPTIVE SPREAD: widen during high vol, tighten during low vol
        vol_bps = get_volatility_bps(coin, mid)
        # Base spread is MIN_SPREAD_BPS, add up to 2x more when vol is high
        vol_multiplier = 1.0 + min(vol_bps / 10.0, 2.0)  # vol of 10bps -> 2x spread, 20+ -> 3x
        adaptive_spread_bps = MIN_SPREAD_BPS * vol_multiplier
        min_spread = mid * adaptive_spread_bps / 10000
        half_spread = max(min_spread / 2, market_spread / 2)

        p_dec = PRICE_DECIMALS.get(coin, 2)
        s_dec = SIZE_DECIMALS.get(coin, 2)

        # ORDERBOOK IMBALANCE
        imbalance = orderbook_imbalance(price_data)

        # Check current position for inventory management
        positions = last_balances.get("positions", {})
        pos = positions.get(coin, {})
        pos_size = pos.get("size", 0)
        pos_usd = abs(pos_size) * mid
        max_position_usd = ORDER_SIZE_USD * 3  # allow up to 3x for scalps

        # ASYMMETRIC SPREAD (prop firm style)
        # Instead of shifting mid, widen the side we DON'T want filled
        # and tighten the side we DO want filled
        inventory_skew = 0  # range -1 to +1
        if pos_size != 0:
            inventory_skew = max(min(pos_size * mid / max_position_usd, 1.0), -1.0)
            # positive = long inventory, negative = short inventory

        # Buy spread: wider when long (don't want more), tighter when short (want to buy)
        # Sell spread: wider when short (don't want more), tighter when long (want to sell)
        buy_half = half_spread * (1 + inventory_skew * 0.8)   # up to 1.8x when long
        sell_half = half_spread * (1 - inventory_skew * 0.8)  # up to 1.8x when short

        # Orderbook imbalance nudge (shift both prices in momentum direction)
        ob_shift = mid * imbalance * 2 / 10000  # up to 2bps shift

        buy_price = round(min(best_bid + (10 ** -p_dec), mid + ob_shift - buy_half), p_dec)
        sell_price = round(max(best_ask - (10 ** -p_dec), mid + ob_shift + sell_half), p_dec)

        # Ensure order value is above $10 minimum
        raw_size = ORDER_SIZE_USD / mid
        size = round(raw_size, s_dec)
        if size * mid < 10.0:
            size = round(10.5 / mid, s_dec)

        actual_spread_bps = ((sell_price - buy_price) / mid) * 10000

        # RISK GOVERNOR: check before placing any orders
        risk_reason = risk_check(account_value, vol_bps, pos_usd)
        if risk_reason:
            print(f"\n  {coin} | RISK PAUSE: {risk_reason}")
            risk_cooldown_until = time.time() + COOLDOWN_SECS
            tg_send(f"⚠️ <b>RISK PAUSE</b> {coin}: {risk_reason}\nCooldown {COOLDOWN_SECS}s")
            continue

        # QUOTE SCORING: only trade when conditions are favorable
        score = quote_score(actual_spread_bps, imbalance, vol_bps, inventory_skew)

        print(f"\n  {coin} | Mid: ${mid:.{p_dec}f} | Spread: {actual_spread_bps:.1f}bps | Vol: {vol_bps:.1f}bps | Score: {score}/4", end="")
        if abs(inventory_skew) > 0.05:
            print(f" | Inv: {inventory_skew:+.2f}", end="")
        if abs(imbalance) > 0.1:
            print(f" | OB: {imbalance:+.2f}", end="")
        print()
        print(f"  Buy @ ${buy_price:.{p_dec}f} (half:{buy_half:.{p_dec}f}) | Sell @ ${sell_price:.{p_dec}f} (half:{sell_half:.{p_dec}f}) | Size: {size}")
        if pos_size != 0:
            print(f"  Position: {pos_size} (${pos_usd:.2f} {'LONG' if pos_size > 0 else 'SHORT'})")

        if score < 2:
            print(f"  SKIP QUOTING (score {score}/4 too low)")
            continue

        # Place buy only if not too long
        if pos_usd < max_position_usd or pos_size <= 0:
            if account_value > ORDER_SIZE_USD:
                place_order(exchange, coin, True, size, buy_price)
        else:
            print(f"  Skip BUY (already long ${pos_usd:.2f})")

        # Place sell only if not too short
        if pos_usd < max_position_usd or pos_size >= 0:
            place_order(exchange, coin, False, size, sell_price)
        else:
            print(f"  Skip SELL (already short ${pos_usd:.2f})")

        # IMBALANCE SCALP: when orderbook strongly favors one side,
        # place an extra aggressive order in that direction
        SCALP_THRESHOLD = 0.55  # imbalance must be > this to trigger scalp
        if abs(imbalance) > SCALP_THRESHOLD and pos_usd < max_position_usd:
            scalp_size = size  # same size as MM orders
            tp_bps = 15  # take profit at 15bps (0.15%)
            if imbalance > SCALP_THRESHOLD:
                # Bullish pressure - aggressive buy
                scalp_buy = round(best_bid + (10 ** -p_dec), p_dec)
                scalp_tp = round(scalp_buy * (1 + tp_bps / 10000), p_dec)
                print(f"  SCALP BUY signal (imb={imbalance:+.2f}) @ ${scalp_buy:.{p_dec}f} -> TP ${scalp_tp:.{p_dec}f}")
                if account_value > ORDER_SIZE_USD * 2:
                    oid = place_order(exchange, coin, True, scalp_size, scalp_buy)
                    if oid:
                        place_order(exchange, coin, False, scalp_size, scalp_tp, reduce_only=True)
            elif imbalance < -SCALP_THRESHOLD:
                # Bearish pressure - aggressive sell
                scalp_sell = round(best_ask - (10 ** -p_dec), p_dec)
                scalp_tp = round(scalp_sell * (1 - tp_bps / 10000), p_dec)
                print(f"  SCALP SELL signal (imb={imbalance:+.2f}) @ ${scalp_sell:.{p_dec}f} -> TP ${scalp_tp:.{p_dec}f}")
                oid = place_order(exchange, coin, False, scalp_size, scalp_sell)
                if oid:
                    place_order(exchange, coin, True, scalp_size, scalp_tp, reduce_only=True)

    # Check fills
    check_fills(info, address)

    # Refresh state
    get_account_state(info, address)

    pv = portfolio_value()
    elapsed = time.time() - start_time
    print(f"\n{'='*55}")
    print(f"  Portfolio: ${pv:.2f} | Fills: {total_trade_count} | {elapsed/60:.1f}m")
    print(f"{'='*55}")
    write_status()


def main():
    print("=" * 55)
    print("  Hyperliquid Market Maker")
    print(f"  Pairs: {PAIRS}")
    print(f"  Size: ${ORDER_SIZE_USD}/side | Refresh: {REFRESH_SECS}s")
    print(f"  Min spread: {MIN_SPREAD_BPS} bps | Maker rebate: ~0.2 bps")
    print("=" * 55)

    info, exchange, address = setup_exchange()
    tg_send(f"🚀 <b>Hyperliquid Bot Started</b>\n{'TESTNET' if USE_TESTNET else 'MAINNET'}\nPairs: {', '.join(PAIRS)}\nSize: ${ORDER_SIZE_USD}/side | Spread: {MIN_SPREAD_BPS}bps")

    cycle_count = 0
    while running:
        try:
            run_cycle(info, exchange, address)
            cycle_count += 1
            if cycle_count % 20 == 0:
                pv = portfolio_value()
                pnl = pv - initial_portfolio_value if initial_portfolio_value else 0
                tg_send(f"📊 <b>HL Status</b>\nPortfolio: ${pv:.2f}\nPnL: {'+'if pnl>=0 else ''}{pnl:.4f}\nFills: {total_trade_count} | Uptime: {(time.time()-start_time)/60:.0f}m")
        except Exception as e:
            print(f"Error: {e}")
            tg_send(f"⚠️ <b>Error</b>: {e}")
            import traceback
            traceback.print_exc()
        for _ in range(REFRESH_SECS):
            if not running:
                break
            time.sleep(1)

    cancel_all_orders(exchange, info, address)
    check_fills(info, address)
    write_status()
    pv = portfolio_value()
    print(f"\nFinal Portfolio: ${pv:.2f} | Fills: {total_trade_count}")
    tg_send(f"🛑 <b>HL Bot Stopped</b>\nPortfolio: ${pv:.2f} | Fills: {total_trade_count}")


if __name__ == "__main__":
    main()
