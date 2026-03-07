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
import threading
import urllib.request
import urllib.parse
from collections import deque
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

# Volatility tracking
price_history = {COIN_MAP.get(p, p.replace("-PERP", "")): [] for p in PAIRS}
VOL_WINDOW = 20

# Risk governor limits
MAX_DRAWDOWN = 0.15
MAX_INVENTORY_USD = 120
MAX_VOLATILITY_BPS = 50
COOLDOWN_SECS = 30
risk_cooldown_until = 0

# Multi-quote state: track our resting orders per coin
live_quotes = {}
round_trips = 0  # completed buy+sell cycles
STALE_BPS = 8  # refresh orders if price moved >8bps from our quote (stay near front)

# Queue quality thresholds
CROWDED_SIZE = 30  # SOL units at top level = crowded queue
THIN_SIZE = 10     # SOL units at top level = thin (good to join)
TIGHT_SPREAD_BPS = 6  # below this, spread too tight to compete
TICK_SIZE = {  # minimum price increment per asset
    "BTC": 1.0,
    "ETH": 0.1,
    "SOL": 0.01,
}

# WebSocket live book data (updated by WS callbacks)
ws_book = {}  # coin -> {"bids": [...], "asks": [...], "ts": time}
ws_book_lock = threading.Lock()
ws_fills_pending = []  # new fills from WS
ws_fills_lock = threading.Lock()
# Event-driven: signal when book changes materially
book_changed = threading.Event()
last_quote_time = {}  # coin -> timestamp of last quote update
MIN_QUOTE_INTERVAL = 0.5  # don't requote faster than 500ms (avoid spam)

# Trade flow tracking (updated by WS trades callback)
recent_trades = {}  # coin -> deque of {"ts", "px", "sz", "side"}
trades_lock = threading.Lock()
FLOW_WINDOW = 5  # seconds to look back for flow signal
FLOW_SHIFT_BPS = 3  # max fair value shift from flow (conservative)
FLOW_WIDEN_BPS = 4  # extra spread during extreme one-way flow


def signal_handler(sig, frame):
    global running
    print("\nShutting down...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def on_l2_book(ws_msg):
    """WebSocket callback for L2 book updates. Signals main loop when book changes."""
    try:
        data = ws_msg.get("data", ws_msg) if isinstance(ws_msg, dict) else ws_msg
        coin = data.get("coin", "")
        levels = data.get("levels", [])
        if len(levels) == 2:
            bids = [{"px": float(l["px"]), "sz": float(l["sz"]), "n": int(l.get("n", 0))} for l in levels[0][:5]]
            asks = [{"px": float(l["px"]), "sz": float(l["sz"]), "n": int(l.get("n", 0))} for l in levels[1][:5]]

            # Check if top-of-book price actually changed
            old_book = ws_book.get(coin)
            price_changed = True
            if old_book and old_book["bids"] and old_book["asks"]:
                old_bid = old_book["bids"][0]["px"]
                old_ask = old_book["asks"][0]["px"]
                price_changed = (bids[0]["px"] != old_bid or asks[0]["px"] != old_ask)

            with ws_book_lock:
                ws_book[coin] = {"bids": bids, "asks": asks, "ts": time.time()}

            # Only wake the main loop if top-of-book price changed
            if price_changed:
                book_changed.set()
    except Exception:
        pass


def on_user_fills(ws_msg):
    """WebSocket callback for user fill events."""
    try:
        data = ws_msg.get("data", ws_msg) if isinstance(ws_msg, dict) else ws_msg
        if isinstance(data, list):
            with ws_fills_lock:
                ws_fills_pending.extend(data)
        elif isinstance(data, dict) and "fills" in data:
            with ws_fills_lock:
                ws_fills_pending.extend(data["fills"])
    except Exception:
        pass


def on_trades(ws_msg):
    """WebSocket callback for market trades. Tracks aggressive flow.
    ws_msg format: {"channel": "trades", "data": [{"coin": "SOL", "side": "B", "px": "84.50", "sz": "1.5", ...}]}"""
    try:
        trades = ws_msg.get("data", []) if isinstance(ws_msg, dict) else ws_msg
        if not isinstance(trades, list):
            trades = [trades]
        now = time.time()
        with trades_lock:
            for t in trades:
                coin = t.get("coin", "")
                if not coin:
                    continue
                if coin not in recent_trades:
                    recent_trades[coin] = deque(maxlen=500)
                recent_trades[coin].append({
                    "ts": now,
                    "px": float(t.get("px", 0)),
                    "sz": float(t.get("sz", 0)),
                    "side": t.get("side", ""),  # "B" = buyer aggressor, "A" = seller aggressor
                })
    except Exception:
        pass


def get_flow_signal(coin, window_secs=None):
    """Calculate trade flow imbalance over rolling window.
    Returns (imbalance, buy_volume, sell_volume).
    imbalance: +1 = all buys, -1 = all sells, 0 = balanced."""
    if window_secs is None:
        window_secs = FLOW_WINDOW
    now = time.time()
    buys = 0.0
    sells = 0.0

    with trades_lock:
        dq = recent_trades.get(coin, deque())
        for t in dq:
            if now - t["ts"] <= window_secs:
                if t["side"] == "B":
                    buys += t["sz"]
                else:
                    sells += t["sz"]

    total = buys + sells
    if total <= 0:
        return 0.0, 0.0, 0.0

    imbalance = (buys - sells) / total
    return imbalance, buys, sells


def get_ws_book(coin):
    """Get latest book from WebSocket, return same format as get_mid_price."""
    with ws_book_lock:
        book = ws_book.get(coin)
    if not book or time.time() - book["ts"] > 10:
        return None  # stale
    bids = book["bids"]
    asks = book["asks"]
    if not bids or not asks:
        return None
    best_bid = bids[0]["px"]
    best_ask = asks[0]["px"]
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": (best_bid + best_ask) / 2,
        "spread": best_ask - best_bid,
        "bid_size": bids[0]["sz"],
        "ask_size": asks[0]["sz"],
        "bid_depth": sum(b["sz"] for b in bids[:3]),
        "ask_depth": sum(a["sz"] for a in asks[:3]),
    }


def setup_exchange():
    """Initialize Hyperliquid connection with WebSocket subscriptions."""
    if not PRIVATE_KEY:
        print("ERROR: Set wallet_private_key in config.json")
        sys.exit(1)

    account = Account.from_key(PRIVATE_KEY)
    address = WALLET_ADDRESS if WALLET_ADDRESS else account.address

    base_url = constants.TESTNET_API_URL if USE_TESTNET else constants.MAINNET_API_URL

    # Info with WebSocket enabled for live book updates
    info = Info(base_url, skip_ws=False)

    # Subscribe to L2 book + trades for each coin
    for pair in PAIRS:
        coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
        info.subscribe({"type": "l2Book", "coin": coin}, on_l2_book)
        info.subscribe({"type": "trades", "coin": coin}, on_trades)
        print(f"  WS subscribed: {coin} l2Book + trades")

    # Subscribe to user fills
    info.subscribe({"type": "userFills", "user": address}, on_user_fills)
    print(f"  WS subscribed: userFills")

    # If API wallet != main wallet, pass account_address so SDK signs as agent
    if account.address.lower() != address.lower():
        exchange = Exchange(account, base_url, account_address=address)
        print(f"  Agent wallet: {account.address}")
    else:
        exchange = Exchange(account, base_url)

    print(f"  Connected to {'TESTNET' if USE_TESTNET else 'MAINNET'}")
    print(f"  Main wallet: {address}")

    # Wait for initial book data
    time.sleep(2)

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


def get_open_orders_by_coin(info, address):
    """Get open orders grouped by coin."""
    result = {}
    try:
        orders = info.open_orders(address)
        for o in orders:
            coin = o.get("coin", "")
            if coin not in result:
                result[coin] = []
            result[coin].append(o)
    except Exception as e:
        print(f"  Open orders error: {e}")
    return result


def run_cycle(info, exchange, address):
    """Multi-quote market making cycle.

    Always quotes both sides (buy + sell) simultaneously.
    Inventory limits control which sides are allowed.
    Only refreshes orders when price drifts >STALE_BPS from our quotes.
    """
    global active_orders, risk_cooldown_until, live_quotes, round_trips

    # Risk cooldown check
    if time.time() < risk_cooldown_until:
        remaining = int(risk_cooldown_until - time.time())
        print(f"  RISK COOLDOWN: {remaining}s remaining")
        write_status()
        return

    # Get account state and check open orders
    get_account_state(info, address)
    account_value = portfolio_value()
    current_orders = get_open_orders_by_coin(info, address)
    active_orders = []  # rebuild for dashboard

    for pair in PAIRS:
        coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
        tick = TICK_SIZE.get(coin, 0.01)
        p_dec = PRICE_DECIMALS.get(coin, 2)
        s_dec = SIZE_DECIMALS.get(coin, 2)

        # Use WebSocket book (instant), fall back to REST poll if stale
        price_data = get_ws_book(coin)
        if not price_data:
            price_data = get_mid_price(info, coin)
        if not price_data:
            continue

        last_prices[pair] = price_data
        mid = price_data["mid"]
        best_bid = price_data["best_bid"]
        best_ask = price_data["best_ask"]
        bid_top_size = price_data["bid_size"]
        ask_top_size = price_data["ask_size"]
        mkt_spread = price_data["spread"]
        mkt_spread_bps = mkt_spread / mid * 10000

        vol_bps = get_volatility_bps(coin, mid)
        imbalance = orderbook_imbalance(price_data)

        # Calculate target spread (volatility-adaptive)
        vol_multiplier = 1.0 + min(vol_bps / 10.0, 2.0)
        spread_bps = MIN_SPREAD_BPS * vol_multiplier
        target_spread = mid * spread_bps / 10000

        # Size calculation
        size = round(ORDER_SIZE_USD / mid, s_dec)
        if size * mid < 10.0:
            size = round(10.5 / mid, s_dec)

        # Check current position
        positions = last_balances.get("positions", {})
        pos = positions.get(coin, {})
        pos_size = pos.get("size", 0)
        pos_usd = abs(pos_size) * mid
        entry_price = pos.get("entry_price", 0)

        # Risk check
        risk_reason = risk_check(account_value, vol_bps, pos_usd)
        if risk_reason:
            print(f"\n  {coin} | RISK PAUSE: {risk_reason}")
            risk_cooldown_until = time.time() + COOLDOWN_SECS
            tg_send(f"⚠️ <b>RISK PAUSE</b> {coin}: {risk_reason}\nCooldown {COOLDOWN_SECS}s")
            continue

        # Get existing orders for this coin
        coin_orders = current_orders.get(coin, [])

        # Trade flow signal
        flow_imb, buy_vol, sell_vol = get_flow_signal(coin)
        flow_total = buy_vol + sell_vol

        print(f"\n  {coin} | Mid: ${mid:.{p_dec}f} | Sprd: {mkt_spread_bps:.1f}bps | Vol: {vol_bps:.1f}bps | Bid:{bid_top_size:.1f} Ask:{ask_top_size:.1f}", end="")
        if abs(imbalance) > 0.1:
            print(f" | OB: {imbalance:+.2f}", end="")
        if flow_total > 0:
            print(f" | Flow:{flow_imb:+.2f} B:{buy_vol:.1f} S:{sell_vol:.1f}", end="")
        print()

        # === STOP-LOSS CHECK ===
        STOP_LOSS_BPS = 30
        if pos_size != 0 and entry_price > 0:
            if pos_size > 0:
                loss_bps = (entry_price - mid) / entry_price * 10000
            else:
                loss_bps = (mid - entry_price) / entry_price * 10000
            if loss_bps > STOP_LOSS_BPS:
                loss_usd = abs(pos_size) * mid * loss_bps / 10000
                print(f"  STOP LOSS: {loss_bps:.0f}bps against us (~${loss_usd:.4f}), closing position")
                tg_send(f"🛑 <b>STOP LOSS</b> {coin}: {loss_bps:.0f}bps loss, closing")
                for o in coin_orders:
                    try: exchange.cancel(coin, o["oid"])
                    except: pass
                try:
                    exchange.market_close(coin)
                except Exception as e:
                    print(f"  Market close error: {e}")
                live_quotes.pop(coin, None)
                continue

        # === FLOW-AWARE QUEUE QUALITY + MULTI-QUOTE MARKET MAKING ===

        inventory_usd = pos_size * mid
        allow_buy = inventory_usd < MAX_INVENTORY_USD
        allow_sell = inventory_usd > -MAX_INVENTORY_USD

        # Shift fair value based on trade flow
        # Buyers lifting asks -> raise fair value, sellers hitting bids -> lower
        fair_mid = mid
        flow_shift_applied = 0
        if abs(flow_imb) > 0.3 and flow_total > 1.0:
            # Scale shift: 0.3-1.0 imbalance -> 0 to FLOW_SHIFT_BPS
            shift_factor = min((abs(flow_imb) - 0.3) / 0.7, 1.0)
            shift_bps = FLOW_SHIFT_BPS * shift_factor
            if flow_imb > 0:
                fair_mid = mid * (1 + shift_bps / 10000)
            else:
                fair_mid = mid * (1 - shift_bps / 10000)
            flow_shift_applied = shift_bps * (1 if flow_imb > 0 else -1)

        # Widen spread during extreme one-way flow (protection)
        if abs(flow_imb) > 0.6 and flow_total > 5.0:
            spread_bps += FLOW_WIDEN_BPS
            target_spread = fair_mid * spread_bps / 10000

        # Skew quotes toward reducing inventory
        skew = 0
        if pos_size != 0:
            inv_ratio = inventory_usd / MAX_INVENTORY_USD
            skew = inv_ratio * target_spread * 0.5

        quotes = live_quotes.get(coin, {})

        # --- SMART BUY PRICE ---
        min_spread_ticks = round(mkt_spread / tick) if tick > 0 else 1

        if min_spread_ticks <= 1:
            # Spread at minimum (1 tick) — always join best bid. Stepping back = no fills.
            buy_price = round(best_bid, p_dec)
            buy_reason = "join bid"
        elif min_spread_ticks >= 3 and bid_top_size < THIN_SIZE:
            # Wide spread + thin queue = step inside for priority
            buy_price = round(best_bid + tick, p_dec)
            buy_reason = "wide+thin, step inside"
        elif min_spread_ticks >= 3 and bid_top_size > CROWDED_SIZE:
            # Wide spread + crowded = step inside to jump queue
            buy_price = round(best_bid + tick, p_dec)
            buy_reason = "wide+crowded, step inside"
        else:
            # 2-tick spread or normal — join best bid
            buy_price = round(best_bid, p_dec)
            buy_reason = "join bid"

        # Apply inventory skew + flow shift
        buy_price = round(buy_price - skew, p_dec)
        # Flow-adjusted floor: don't quote below fair_mid - half_spread
        if flow_shift_applied != 0:
            half_spread = target_spread / 2
            flow_floor = round(fair_mid - half_spread, p_dec)
            if buy_price < flow_floor:
                buy_price = flow_floor
                buy_reason += f" +flow{flow_shift_applied:+.0f}bp"
        # Safety: don't cross the spread
        if buy_price >= best_ask:
            buy_price = round(best_ask - tick, p_dec)

        # --- SMART SELL PRICE ---
        if min_spread_ticks <= 1:
            sell_price = round(best_ask, p_dec)
            sell_reason = "join ask"
        elif min_spread_ticks >= 3 and ask_top_size < THIN_SIZE:
            sell_price = round(best_ask - tick, p_dec)
            sell_reason = "wide+thin, step inside"
        elif min_spread_ticks >= 3 and ask_top_size > CROWDED_SIZE:
            sell_price = round(best_ask - tick, p_dec)
            sell_reason = "wide+crowded, step inside"
        else:
            sell_price = round(best_ask, p_dec)
            sell_reason = "join ask"

        sell_price = round(sell_price - skew, p_dec)
        # Flow-adjusted ceiling: don't quote above fair_mid + half_spread
        if flow_shift_applied != 0:
            half_spread = target_spread / 2
            flow_ceil = round(fair_mid + half_spread, p_dec)
            if sell_price > flow_ceil:
                sell_price = flow_ceil
                sell_reason += f" +flow{flow_shift_applied:+.0f}bp"
        if sell_price <= best_bid:
            sell_price = round(best_bid + tick, p_dec)

        # Ensure minimum spread between our own quotes
        if sell_price <= buy_price:
            sell_price = round(buy_price + tick, p_dec)

        # Check existing orders
        existing_buy_px = None
        existing_buy_oid = None
        for o in coin_orders:
            if o.get("side") == "B":
                existing_buy_px = float(o.get("limitPx", 0))
                existing_buy_oid = o.get("oid")
                break

        existing_sell_px = None
        existing_sell_oid = None
        for o in coin_orders:
            if o.get("side") == "A":
                existing_sell_px = float(o.get("limitPx", 0))
                existing_sell_oid = o.get("oid")
                break

        # --- BUY SIDE ---
        if allow_buy:
            if existing_buy_px is not None:
                drift_bps = abs(existing_buy_px - buy_price) / mid * 10000
                if drift_bps > STALE_BPS:
                    try: exchange.cancel(coin, existing_buy_oid)
                    except: pass
                    print(f"  Refresh BUY: ${existing_buy_px:.{p_dec}f} -> ${buy_price:.{p_dec}f} ({buy_reason}, {drift_bps:.0f}bps drift)")
                    oid = place_order(exchange, coin, True, size, buy_price)
                    if oid:
                        quotes["buy_oid"] = oid
                        quotes["buy_price"] = buy_price
                else:
                    print(f"  BUY resting @ ${existing_buy_px:.{p_dec}f} ({drift_bps:.0f}bps drift, ok)")
            else:
                print(f"  Place BUY @ ${buy_price:.{p_dec}f} ({buy_reason})")
                oid = place_order(exchange, coin, True, size, buy_price)
                if oid:
                    quotes["buy_oid"] = oid
                    quotes["buy_price"] = buy_price
        else:
            if existing_buy_oid:
                print(f"  Cancel BUY (inventory ${inventory_usd:.0f} >= ${MAX_INVENTORY_USD})")
                try: exchange.cancel(coin, existing_buy_oid)
                except: pass

        # --- SELL SIDE ---
        if allow_sell:
            if existing_sell_px is not None:
                drift_bps = abs(existing_sell_px - sell_price) / mid * 10000
                if drift_bps > STALE_BPS:
                    try: exchange.cancel(coin, existing_sell_oid)
                    except: pass
                    print(f"  Refresh SELL: ${existing_sell_px:.{p_dec}f} -> ${sell_price:.{p_dec}f} ({sell_reason}, {drift_bps:.0f}bps drift)")
                    oid = place_order(exchange, coin, False, size, sell_price)
                    if oid:
                        quotes["sell_oid"] = oid
                        quotes["sell_price"] = sell_price
                else:
                    print(f"  SELL resting @ ${existing_sell_px:.{p_dec}f} ({drift_bps:.0f}bps drift, ok)")
            else:
                print(f"  Place SELL @ ${sell_price:.{p_dec}f} ({sell_reason})")
                oid = place_order(exchange, coin, False, size, sell_price)
                if oid:
                    quotes["sell_oid"] = oid
                    quotes["sell_price"] = sell_price
        else:
            if existing_sell_oid:
                print(f"  Cancel SELL (inventory ${inventory_usd:.0f} <= -${MAX_INVENTORY_USD})")
                try: exchange.cancel(coin, existing_sell_oid)
                except: pass

        # Print position status
        if pos_size != 0:
            direction = "LONG" if pos_size > 0 else "SHORT"
            upnl = pos.get("unrealized_pnl", 0)
            print(f"  Pos: {direction} {abs(pos_size)} @ ${entry_price:.{p_dec}f} | uPnL: ${upnl:.4f} | Inv: ${inventory_usd:.2f}")

        live_quotes[coin] = quotes
        last_quote_time[coin] = time.time()

        # Track active orders for dashboard
        for o in current_orders.get(coin, []):
            side_str = "buy" if o.get("side") == "B" else "sell"
            active_orders.append({
                "pair": f"{coin}-PERP", "side": side_str,
                "volume": float(o.get("sz", 0)), "price": float(o.get("limitPx", 0)),
                "id": str(o.get("oid", "")),
            })

    # Check fills
    check_fills(info, address)

    # Refresh state
    get_account_state(info, address)

    pv = portfolio_value()
    elapsed = time.time() - start_time
    print(f"\n{'='*55}")
    print(f"  Portfolio: ${pv:.2f} | Fills: {total_trade_count} | Trips: {round_trips} | {elapsed/60:.1f}m")
    print(f"{'='*55}")
    write_status()


def main():
    print("=" * 55)
    print("  Hyperliquid Market Maker (Event-Driven)")
    print(f"  Pairs: {PAIRS}")
    print(f"  Size: ${ORDER_SIZE_USD}/side | Min quote interval: {MIN_QUOTE_INTERVAL}s")
    print(f"  Min spread: {MIN_SPREAD_BPS} bps | Maker rebate: ~0.2 bps")
    print("=" * 55)

    info, exchange, address = setup_exchange()
    tg_send(f"🚀 <b>Hyperliquid Bot Started</b>\n{'TESTNET' if USE_TESTNET else 'MAINNET'}\nPairs: {', '.join(PAIRS)}\nSize: ${ORDER_SIZE_USD}/side | Spread: {MIN_SPREAD_BPS}bps | Event-driven")

    cycle_count = 0
    last_status_tg = time.time()
    last_account_refresh = 0

    while running:
        try:
            # Wait for book change or timeout (max 5s for account refresh/status)
            triggered = book_changed.wait(timeout=5.0)
            if not running:
                break
            book_changed.clear()

            # Throttle: don't requote faster than MIN_QUOTE_INTERVAL
            now = time.time()
            for coin in [COIN_MAP.get(p, p.replace("-PERP", "")) for p in PAIRS]:
                last_t = last_quote_time.get(coin, 0)
                if now - last_t < MIN_QUOTE_INTERVAL:
                    continue  # too soon for this coin

            # Refresh account state periodically (every 10s, not every cycle)
            if now - last_account_refresh > 10:
                get_account_state(info, address)
                last_account_refresh = now

            run_cycle(info, exchange, address)
            cycle_count += 1

            # Telegram status every ~2 min
            if now - last_status_tg > 120:
                pv = portfolio_value()
                pnl = pv - initial_portfolio_value if initial_portfolio_value else 0
                tg_send(f"📊 <b>HL Status</b>\nPortfolio: ${pv:.2f}\nPnL: {'+'if pnl>=0 else ''}{pnl:.4f}\nFills: {total_trade_count} | Uptime: {(now-start_time)/60:.0f}m")
                last_status_tg = now

        except Exception as e:
            print(f"Error: {e}")
            tg_send(f"⚠️ <b>Error</b>: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(2)  # back off on error

    cancel_all_orders(exchange, info, address)
    check_fills(info, address)
    write_status()
    pv = portfolio_value()
    print(f"\nFinal Portfolio: ${pv:.2f} | Fills: {total_trade_count}")
    tg_send(f"🛑 <b>HL Bot Stopped</b>\nPortfolio: ${pv:.2f} | Fills: {total_trade_count}")


if __name__ == "__main__":
    main()
