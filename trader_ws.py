#!/usr/bin/env python3
"""
Hyperliquid WebSocket Market Maker with Microprice.

Replaces REST polling with event-driven quoting:
  WS l2Book stream -> microprice -> inventory skew -> quote engine -> cancel/replace

Key differences from trader.py / trader_pingpong.py:
  - Reacts to book updates in real-time (not every 15s)
  - Uses microprice (imbalance-weighted fair value) instead of plain mid
  - Cancel/replace on every material book change
  - Trade flow signal from WS trades stream
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
from typing import Optional, Dict, Any, List

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

# ─── Config ───────────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_status.json")

with open(CONFIG_FILE) as f:
    config = json.load(f)

PRIVATE_KEY = config["wallet_private_key"]
WALLET_ADDRESS = config.get("wallet_address", "")
USE_TESTNET = config.get("use_testnet", True)
PAIRS = config.get("pairs", ["SOL-PERP"])
ORDER_SIZE_USD = config.get("order_size_usd", 10.0)
MIN_SPREAD_BPS = config.get("min_spread_bps", 5)

# Telegram alerts
TG_TOKEN = config.get("tg_token", "")
TG_CHAT_ID = config.get("tg_chat_id", "")

# Tuning
REPRICE_THRESHOLD_BPS = 0.6   # reprice when microprice moves more than this
STALE_ORDER_MS = 10_000       # cancel orders older than this if not repriced
SKEW_PER_UNIT_BPS = 0.5       # inventory penalty as ratio of max position
FLOW_WINDOW_SECS = 5.0        # trade flow lookback
FLOW_ALPHA_BPS = 1.0           # max bps shift from trade flow
MAX_POSITION_USD = 40.0        # max exposure per coin
QUOTE_COOLDOWN_MS = 150        # min ms between quote updates per coin

COIN_MAP = {
    "BTC-PERP": "BTC",
    "ETH-PERP": "ETH",
    "SOL-PERP": "SOL",
}

SIZE_DECIMALS = {"BTC": 5, "ETH": 4, "SOL": 2}
PRICE_DECIMALS = {"BTC": 0, "ETH": 1, "SOL": 2}

# ─── Globals ──────────────────────────────────────────────────────────────────

running = True
start_time = time.time()
all_fills: List[dict] = []
total_trade_count = 0
initial_portfolio_value: Optional[float] = None
last_balances: dict = {}

# Per-coin state
book_state: Dict[str, dict] = {}           # coin -> {bids, asks, best_bid, best_ask, ...}
microprice_state: Dict[str, dict] = {}     # coin -> {micro, mid, signal_bps, ...}
trade_flow: Dict[str, deque] = {}          # coin -> deque of (timestamp, signed_size)
active_oids: Dict[str, dict] = {}          # coin -> {buy_oid, sell_oid, buy_px, sell_px, ts}
last_quote_ts: Dict[str, float] = {}       # coin -> last quote time (ms)
positions: Dict[str, dict] = {}            # coin -> {size, entry_price, unrealized_pnl}

_lock = threading.Lock()


def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data=data, timeout=5)
    except Exception:
        pass


def signal_handler(sig, frame):
    global running
    print("\nShutting down...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ─── Microprice Engine ────────────────────────────────────────────────────────

def compute_microprice(best_bid: float, best_ask: float, bid_size: float, ask_size: float) -> dict:
    """Compute microprice and imbalance signal."""
    mid = (best_bid + best_ask) / 2.0
    total = bid_size + ask_size
    if total <= 0:
        micro = mid
    else:
        micro = (best_ask * bid_size + best_bid * ask_size) / total

    signal_bps = ((micro - mid) / mid) * 10000 if mid > 0 else 0.0

    return {
        "mid": mid,
        "micro": micro,
        "signal_bps": signal_bps,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "imbalance": (bid_size - ask_size) / total if total > 0 else 0,
    }


def compute_trade_flow(coin: str) -> float:
    """Compute net trade flow signal in bps from recent trades.
    Positive = net buying pressure, negative = net selling."""
    if coin not in trade_flow:
        return 0.0

    now = time.time()
    flow = trade_flow[coin]

    # Purge old entries
    while flow and (now - flow[0][0]) > FLOW_WINDOW_SECS:
        flow.popleft()

    if not flow:
        return 0.0

    net_signed_usd = sum(signed_usd for _, signed_usd in flow)
    # Normalize: cap at FLOW_ALPHA_BPS
    # Use ORDER_SIZE_USD as reference scale
    raw = (net_signed_usd / ORDER_SIZE_USD) * FLOW_ALPHA_BPS
    return max(-FLOW_ALPHA_BPS, min(FLOW_ALPHA_BPS, raw))


def compute_fair_price(coin: str) -> Optional[float]:
    """Compute fair price = microprice + trade_flow - inventory_skew."""
    mp = microprice_state.get(coin)
    if not mp:
        return None

    micro = mp["micro"]
    mid = mp["mid"]

    # Trade flow alpha
    flow_bps = compute_trade_flow(coin)
    flow_shift = mid * flow_bps / 10000

    # Inventory skew
    pos = positions.get(coin, {})
    pos_size = pos.get("size", 0)
    pos_usd = pos_size * mid  # signed
    inv_penalty = pos_usd * SKEW_PER_UNIT_BPS / 10000

    fair = micro + flow_shift - inv_penalty
    return fair


# ─── WebSocket Callbacks ─────────────────────────────────────────────────────

def on_l2_book(msg: dict):
    """Called on every l2Book update from WS."""
    try:
        data = msg["data"]
        coin = data["coin"]
        levels = data["levels"]
        bids = levels[0]  # list of {px, sz, n}
        asks = levels[1]

        if not bids or not asks:
            return

        best_bid = float(bids[0]["px"])
        best_ask = float(asks[0]["px"])
        bid_size = float(bids[0]["sz"])
        ask_size = float(asks[0]["sz"])

        # Store full book state
        with _lock:
            book_state[coin] = {
                "bids": bids,
                "asks": asks,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "spread": best_ask - best_bid,
                "ts": time.time(),
            }

            # Update microprice
            mp = compute_microprice(best_bid, best_ask, bid_size, ask_size)
            prev_mp = microprice_state.get(coin, {})
            microprice_state[coin] = mp

        # Check if we need to reprice
        prev_micro = prev_mp.get("micro", 0)
        if prev_micro > 0:
            move_bps = abs(mp["micro"] - prev_micro) / prev_micro * 10000
            if move_bps >= REPRICE_THRESHOLD_BPS:
                trigger_reprice(coin, reason=f"micro moved {move_bps:.1f}bps")

    except Exception as e:
        print(f"  [WS] l2Book error: {e}")


def on_trades(msg: dict):
    """Called on every trades update from WS."""
    try:
        trades = msg["data"]
        if not trades:
            return

        coin = trades[0]["coin"]
        now = time.time()

        if coin not in trade_flow:
            trade_flow[coin] = deque(maxlen=500)

        for t in trades:
            px = float(t["px"])
            sz = float(t["sz"])
            side = t["side"]  # "B" = buy, "A" = sell
            signed_usd = px * sz if side == "B" else -(px * sz)
            trade_flow[coin].append((now, signed_usd))

    except Exception as e:
        print(f"  [WS] trades error: {e}")


def on_order_updates(msg: dict):
    """Called on order status changes (fills, cancels, etc)."""
    try:
        data = msg["data"]
        if not isinstance(data, list):
            data = [data]

        for update in data:
            status = update.get("status", "")
            coin = update.get("coin", "")
            side = update.get("side", "")
            sz = float(update.get("sz", 0))
            px = float(update.get("px", 0))

            if status == "filled":
                global total_trade_count
                total_trade_count += 1
                side_str = "BUY" if side == "B" else "SELL"
                print(f"  >>> FILL: {side_str} {sz} {coin} @ ${px:.2f}")
                tg_send(f"{'🟢' if side == 'B' else '🔴'} <b>FILL</b>: {side_str} {sz} {coin} @ ${px:.2f}")

                # Trigger reprice after fill (inventory changed)
                trigger_reprice(coin, reason="fill")

    except Exception as e:
        print(f"  [WS] orderUpdates error: {e}")


# ─── Quote Engine ─────────────────────────────────────────────────────────────

def trigger_reprice(coin: str, reason: str = ""):
    """Evaluate whether to cancel/replace quotes for a coin."""
    now_ms = time.time() * 1000

    # Cooldown check
    last_ts = last_quote_ts.get(coin, 0)
    if (now_ms - last_ts) < QUOTE_COOLDOWN_MS:
        return

    # Schedule reprice on main thread (thread-safe flag)
    # The main loop will pick this up
    reprice_queue.append((coin, reason))


# Thread-safe reprice queue
reprice_queue: deque = deque(maxlen=100)


def execute_reprice(coin: str, exchange: Exchange, info: Info, address: str, reason: str = ""):
    """Cancel stale orders and place new quotes at fair price."""
    global last_quote_ts

    fair = compute_fair_price(coin)
    if fair is None:
        return

    book = book_state.get(coin)
    if not book:
        return

    mp = microprice_state.get(coin, {})
    mid = mp.get("mid", 0)
    if mid <= 0:
        return

    p_dec = PRICE_DECIMALS.get(coin, 2)
    s_dec = SIZE_DECIMALS.get(coin, 2)

    # Position check
    pos = positions.get(coin, {})
    pos_size = pos.get("size", 0)
    pos_usd = abs(pos_size) * mid

    if pos_usd >= MAX_POSITION_USD:
        # Only place reducing orders
        pass

    # Volatility-adaptive spread: use spread of book as floor
    market_spread = book["spread"]
    min_spread = mid * MIN_SPREAD_BPS / 10000
    half_spread = max(min_spread / 2, market_spread / 2)

    # Quote prices
    bid_price = round(fair - half_spread, p_dec)
    ask_price = round(fair + half_spread, p_dec)

    # Clamp to not cross the book
    bid_price = min(bid_price, round(book["best_bid"], p_dec))
    ask_price = max(ask_price, round(book["best_ask"], p_dec))

    # Size
    size = round(ORDER_SIZE_USD / mid, s_dec)
    if size * mid < 10.0:
        size = round(10.5 / mid, s_dec)

    # Check if existing orders are still close enough
    existing = active_oids.get(coin, {})
    if existing:
        old_bid = existing.get("buy_px", 0)
        old_ask = existing.get("sell_px", 0)
        bid_move = abs(bid_price - old_bid) / mid * 10000 if old_bid else 999
        ask_move = abs(ask_price - old_ask) / mid * 10000 if old_ask else 999

        if bid_move < REPRICE_THRESHOLD_BPS and ask_move < REPRICE_THRESHOLD_BPS:
            return  # no material change, skip

    # Cancel existing orders for this coin
    cancel_coin_orders(exchange, info, address, coin)

    # Place new quotes
    signal_bps = mp.get("signal_bps", 0)
    flow_bps = compute_trade_flow(coin)
    inv_skew = pos_size * mid / MAX_POSITION_USD if MAX_POSITION_USD > 0 else 0

    print(f"  [{coin}] REPRICE ({reason}) fair=${fair:.{p_dec}f} "
          f"micro={signal_bps:+.1f}bps flow={flow_bps:+.1f}bps inv={inv_skew:+.2f}")
    print(f"    BID ${bid_price:.{p_dec}f} | ASK ${ask_price:.{p_dec}f} | size={size}")

    buy_oid = None
    sell_oid = None

    # Place buy if not over-long
    if pos_usd < MAX_POSITION_USD or pos_size <= 0:
        buy_oid = place_order(exchange, coin, True, size, bid_price)

    # Place sell if not over-short
    if pos_usd < MAX_POSITION_USD or pos_size >= 0:
        sell_oid = place_order(exchange, coin, False, size, ask_price)

    active_oids[coin] = {
        "buy_oid": buy_oid,
        "sell_oid": sell_oid,
        "buy_px": bid_price,
        "sell_px": ask_price,
        "ts": time.time(),
    }
    last_quote_ts[coin] = time.time() * 1000


def cancel_coin_orders(exchange: Exchange, info: Info, address: str, coin: str):
    """Cancel all open orders for a specific coin."""
    try:
        open_orders = info.open_orders(address)
        for order in open_orders:
            if order.get("coin") == coin:
                try:
                    exchange.cancel(coin, order["oid"])
                except Exception:
                    pass
    except Exception as e:
        print(f"  Cancel error {coin}: {e}")


def place_order(exchange: Exchange, coin: str, is_buy: bool, size: float, price: float) -> Optional[int]:
    """Place a limit post-only (ALO) order."""
    try:
        result = exchange.order(
            coin, is_buy, size, price,
            {"limit": {"tif": "Alo"}},
        )
        if isinstance(result, str):
            return None
        resp = result.get("response", {}) if isinstance(result, dict) else {}
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        statuses = data.get("statuses", []) if isinstance(data, dict) else []
        if statuses and isinstance(statuses[0], dict) and "resting" in statuses[0]:
            return statuses[0]["resting"]["oid"]
    except Exception as e:
        print(f"  Order error: {e}")
    return None


# ─── Account State ────────────────────────────────────────────────────────────

def refresh_account(info: Info, address: str):
    """Pull account state and update positions."""
    global last_balances, initial_portfolio_value, positions
    try:
        state = info.user_state(address)
        if not state:
            return

        margin = state.get("marginSummary", {})
        account_value = float(margin.get("accountValue", 0))

        last_balances = {
            "account_value": account_value,
            "total_margin": float(margin.get("totalMarginUsed", 0)),
        }

        if initial_portfolio_value is None and account_value > 0:
            initial_portfolio_value = account_value

        new_positions = {}
        for pos_data in state.get("assetPositions", []):
            p = pos_data.get("position", {})
            coin = p.get("coin", "")
            size = float(p.get("szi", 0))
            if size != 0 or coin in [COIN_MAP.get(pair, pair.replace("-PERP", "")) for pair in PAIRS]:
                new_positions[coin] = {
                    "size": size,
                    "entry_price": float(p.get("entryPx", 0)),
                    "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
                }

        with _lock:
            positions.update(new_positions)

    except Exception as e:
        print(f"  Account refresh error: {e}")


def check_fills(info: Info, address: str):
    """Check for recent fills and log them."""
    global total_trade_count
    try:
        fills = info.user_fills(address)
        new_fills = [f for f in fills if float(f.get("time", 0)) / 1000 > start_time]
        new_count = len(new_fills)

        if new_count > total_trade_count:
            for f in new_fills[total_trade_count:]:
                coin = f.get("coin", "")
                side = f.get("side", "")
                price = float(f.get("px", 0))
                size = float(f.get("sz", 0))
                fee = float(f.get("fee", 0))
                closed_pnl = float(f.get("closedPnl", 0))

                all_fills.append({
                    "time": time.time(),
                    "side": side.lower(),
                    "pair": f"{coin}-PERP",
                    "volume": size,
                    "price": price,
                    "cost": price * size,
                    "fee": fee,
                    "closed_pnl": closed_pnl,
                })

            total_trade_count = new_count
    except Exception as e:
        print(f"  Fills error: {e}")


# ─── Status Writer ────────────────────────────────────────────────────────────

def write_status():
    """Write status JSON for dashboard."""
    pv = last_balances.get("account_value", 0)
    portfolio_pnl = pv - initial_portfolio_value if initial_portfolio_value else 0
    bot_realized_pnl = sum(f.get("closed_pnl", 0) for f in all_fills)
    bot_fees = sum(f["fee"] for f in all_fills)

    # Microprice data for dashboard
    micro_data = {}
    for coin, mp in microprice_state.items():
        micro_data[coin] = {
            "mid": round(mp["mid"], 6),
            "micro": round(mp["micro"], 6),
            "signal_bps": round(mp["signal_bps"], 2),
            "imbalance": round(mp["imbalance"], 4),
            "flow_bps": round(compute_trade_flow(coin), 2),
        }

    pair_status = {}
    for p in PAIRS:
        coin = COIN_MAP.get(p, p.replace("-PERP", ""))
        pos = positions.get(coin, {})
        mp = microprice_state.get(coin, {})
        pair_fills_list = [f for f in all_fills if coin in f.get("pair", "")]
        pair_status[p] = {
            "trade_count": len(pair_fills_list),
            "holding": pos.get("size", 0),
            "holding_usd": round(abs(pos.get("size", 0)) * mp.get("mid", 0), 2),
            "unrealized_pnl": round(pos.get("unrealized_pnl", 0), 4),
            "entry_price": pos.get("entry_price", 0),
            "microprice": round(mp.get("micro", 0), 6),
            "signal_bps": round(mp.get("signal_bps", 0), 2),
        }

    oids = []
    for coin, o in active_oids.items():
        if o.get("buy_oid"):
            oids.append({"pair": f"{coin}-PERP", "side": "buy", "price": o["buy_px"], "id": str(o["buy_oid"])})
        if o.get("sell_oid"):
            oids.append({"pair": f"{coin}-PERP", "side": "sell", "price": o["sell_px"], "id": str(o["sell_oid"])})

    status = {
        "running": running,
        "mode": "ws_microprice",
        "exchange": "Hyperliquid",
        "network": "testnet" if USE_TESTNET else "mainnet",
        "uptime_min": round((time.time() - start_time) / 60, 1),
        "portfolio_value": round(pv, 2),
        "portfolio_pnl": round(portfolio_pnl, 4),
        "bot_realized_pnl": round(bot_realized_pnl, 6),
        "bot_net_pnl": round(bot_realized_pnl - bot_fees, 6),
        "initial_portfolio_value": round(initial_portfolio_value, 2) if initial_portfolio_value else None,
        "total_trade_count": total_trade_count,
        "total_fees": round(bot_fees, 6),
        "pairs": PAIRS,
        "pair_status": pair_status,
        "microprice": micro_data,
        "active_orders": oids,
        "recent_fills": all_fills[-20:],
        "balances": last_balances,
        "updated_at": time.time(),
    }
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f)
    except Exception:
        pass


# ─── Setup ────────────────────────────────────────────────────────────────────

def setup_exchange():
    """Initialize Hyperliquid connection with WebSocket enabled."""
    if not PRIVATE_KEY:
        print("ERROR: Set wallet_private_key in config.json")
        sys.exit(1)

    account = Account.from_key(PRIVATE_KEY)
    address = WALLET_ADDRESS if WALLET_ADDRESS else account.address

    base_url = constants.TESTNET_API_URL if USE_TESTNET else constants.MAINNET_API_URL

    # WS enabled (skip_ws=False) for streaming
    info = Info(base_url, skip_ws=False)

    if account.address.lower() != address.lower():
        exchange = Exchange(account, base_url, account_address=address)
        print(f"  Agent wallet: {account.address}")
    else:
        exchange = Exchange(account, base_url)

    print(f"  Connected to {'TESTNET' if USE_TESTNET else 'MAINNET'}")
    print(f"  Main wallet: {address}")

    return info, exchange, address


def subscribe_streams(info: Info, address: str):
    """Subscribe to WS streams for all pairs."""
    for pair in PAIRS:
        coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))

        # l2Book stream
        info.subscribe({"type": "l2Book", "coin": coin}, on_l2_book)
        print(f"  Subscribed: l2Book {coin}")

        # Trades stream
        info.subscribe({"type": "trades", "coin": coin}, on_trades)
        print(f"  Subscribed: trades {coin}")

    # Order updates (fills, cancels) - requires user auth
    try:
        info.subscribe({"type": "orderUpdates", "user": address}, on_order_updates)
        print(f"  Subscribed: orderUpdates")
    except Exception as e:
        print(f"  orderUpdates subscribe failed (non-critical): {e}")


# ─── Main Loop ────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Hyperliquid WS Market Maker (Microprice)")
    print(f"  Pairs: {PAIRS}")
    print(f"  Size: ${ORDER_SIZE_USD}/side | Min spread: {MIN_SPREAD_BPS}bps")
    print(f"  Reprice threshold: {REPRICE_THRESHOLD_BPS}bps")
    print(f"  Max position: ${MAX_POSITION_USD}")
    print("=" * 60)

    info, exchange, address = setup_exchange()

    # Initial account state
    refresh_account(info, address)
    pv = last_balances.get("account_value", 0)
    print(f"  Portfolio: ${pv:.2f}")

    # Subscribe to WS streams
    subscribe_streams(info, address)
    tg_send(f"🚀 <b>HL WS Bot Started</b>\n{'TESTNET' if USE_TESTNET else 'MAINNET'}\n"
            f"Pairs: {', '.join(PAIRS)}\nMode: microprice")

    print("\n  Waiting for book data...")

    cycle = 0
    while running:
        try:
            # Process any pending reprices from WS callbacks
            while reprice_queue:
                coin, reason = reprice_queue.popleft()
                try:
                    execute_reprice(coin, exchange, info, address, reason)
                except Exception as e:
                    print(f"  Reprice error {coin}: {e}")

            # Periodic tasks (every ~2 seconds)
            cycle += 1
            if cycle % 4 == 0:
                refresh_account(info, address)
                check_fills(info, address)

            if cycle % 10 == 0:
                write_status()
                # Print status line
                pv = last_balances.get("account_value", 0)
                for pair in PAIRS:
                    coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
                    mp = microprice_state.get(coin, {})
                    pos = positions.get(coin, {})
                    pos_size = pos.get("size", 0)
                    if mp:
                        flow = compute_trade_flow(coin)
                        print(f"  {coin} micro=${mp.get('micro', 0):.2f} "
                              f"sig={mp.get('signal_bps', 0):+.1f}bps "
                              f"flow={flow:+.1f}bps "
                              f"pos={pos_size:+.3f}")

                elapsed = (time.time() - start_time) / 60
                print(f"  Portfolio: ${pv:.2f} | Fills: {total_trade_count} | {elapsed:.1f}m")

            if cycle % 120 == 0:
                # Telegram status every ~60s
                pv = last_balances.get("account_value", 0)
                pnl = pv - initial_portfolio_value if initial_portfolio_value else 0
                tg_send(f"📊 <b>HL WS Status</b>\nPortfolio: ${pv:.2f}\n"
                        f"PnL: {'+'if pnl>=0 else ''}{pnl:.4f}\n"
                        f"Fills: {total_trade_count}")

            # Also trigger reprice if orders are stale (no WS update triggered it)
            now = time.time()
            for pair in PAIRS:
                coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
                existing = active_oids.get(coin, {})
                if existing and existing.get("ts"):
                    age_ms = (now - existing["ts"]) * 1000
                    if age_ms > STALE_ORDER_MS:
                        reprice_queue.append((coin, "stale"))

        except Exception as e:
            print(f"  Loop error: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(0.5)

    # Shutdown
    print("\nCancelling all orders...")
    for pair in PAIRS:
        coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
        cancel_coin_orders(exchange, info, address, coin)

    check_fills(info, address)
    write_status()

    try:
        info.disconnect_websocket()
    except Exception:
        pass

    pv = last_balances.get("account_value", 0)
    print(f"\nFinal Portfolio: ${pv:.2f} | Fills: {total_trade_count}")
    tg_send(f"🛑 <b>HL WS Bot Stopped</b>\nPortfolio: ${pv:.2f} | Fills: {total_trade_count}")


if __name__ == "__main__":
    main()
