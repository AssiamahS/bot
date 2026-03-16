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
MOMENTUM_WINDOW = 0.8          # seconds lookback for microprice velocity
MOMENTUM_THRESHOLD_BPS = 2.0   # pause quoting if velocity exceeds this

# Exit-mode state machine
EXIT_MODE_POSITION_USD = 12.0  # switch to exit mode above this inventory (lowered from 15)
EXIT_ENTRY_SPREAD_WIDEN = 2.5  # multiply entry-side half_spread by this (wider, discourage adds)

# Time-based exit escalation ladder
# Each tier: (max_age_secs, exit_spread_mult, entry_size_mult, description)
# exit_spread_mult: multiplier on half_spread for exit side (lower = tighter = more aggressive)
# entry_size_mult: multiplier on entry side size (0 = disabled)
EXIT_ESCALATION = [
    (5,   0.5,  0.3,  "normal_exit"),      # 0-5s:   half spread, 30% entry size
    (15,  0.25, 0.0,  "tight_exit"),        # 5-15s:  quarter spread, no entry
    (30,  0.0,  0.0,  "join_best"),         # 15-30s: join best bid/ask, no entry
    (60,  -0.5, 0.0,  "cross_spread"),      # 30-60s: cross spread (become taker), no entry
    (999, -1.0, 0.0,  "emergency_cross"),   # 60s+:   aggressive taker cross
]

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
micro_history: Dict[str, deque] = {}       # coin -> deque of (timestamp, microprice)
inventory_entered_at: Dict[str, float] = {}  # coin -> timestamp when position became non-zero
inventory_mode: Dict[str, str] = {}          # coin -> "neutral" | "long_exit" | "short_exit"

# Round-trip tracking
round_trips: List[dict] = []       # completed trips: {coin, entry_time, exit_time, hold_secs, pnl}
prev_positions: Dict[str, float] = {}  # coin -> previous pos_size for detecting flattens

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


def compute_micro_velocity(coin: str, mid: float) -> float:
    """Compute microprice velocity in bps over MOMENTUM_WINDOW.
    Positive = book pressure moving up, negative = moving down."""
    hist = micro_history.get(coin)
    if not hist or len(hist) < 2:
        return 0.0

    now = time.time()
    new_micro = hist[-1][1]

    # Find the oldest sample within the momentum window
    old_micro = hist[0][1]
    for t, micro in reversed(hist):
        if now - t >= MOMENTUM_WINDOW:
            old_micro = micro
            break

    if mid <= 0:
        return 0.0
    return ((new_micro - old_micro) / mid) * 10000


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

    # Inventory skew (normalized by max position, not raw USD)
    pos = positions.get(coin, {})
    pos_size = pos.get("size", 0)
    pos_ratio = (pos_size * mid) / MAX_POSITION_USD if MAX_POSITION_USD > 0 else 0
    inv_shift = pos_ratio * SKEW_PER_UNIT_BPS * mid / 10000

    fair = micro + flow_shift - inv_shift
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

            # Record microprice history for momentum/velocity
            if coin not in micro_history:
                micro_history[coin] = deque(maxlen=50)
            micro_history[coin].append((time.time(), mp["micro"]))

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


def update_inventory_mode(coin: str, pos_size: float, mid: float):
    """Update the inventory regime: neutral / long_exit / short_exit."""
    pos_usd = pos_size * mid  # signed
    abs_usd = abs(pos_usd)
    now = time.time()

    prev_mode = inventory_mode.get(coin, "neutral")

    if abs_usd < EXIT_MODE_POSITION_USD * 0.3:
        # Near flat — reset to neutral
        new_mode = "neutral"
        if prev_mode != "neutral":
            age = now - inventory_entered_at.get(coin, now)
            print(f"  [{coin}] FLATTENED after {age:.1f}s -> neutral")
        inventory_entered_at.pop(coin, None)
    elif pos_usd > 0 and abs_usd >= EXIT_MODE_POSITION_USD:
        new_mode = "long_exit"
        if prev_mode != "long_exit":
            inventory_entered_at.setdefault(coin, now)
            print(f"  [{coin}] LONG EXIT mode (${abs_usd:.0f})")
    elif pos_usd < 0 and abs_usd >= EXIT_MODE_POSITION_USD:
        new_mode = "short_exit"
        if prev_mode != "short_exit":
            inventory_entered_at.setdefault(coin, now)
            print(f"  [{coin}] SHORT EXIT mode (${abs_usd:.0f})")
    else:
        # Between 30% and 100% of threshold — keep current mode or neutral
        new_mode = prev_mode if prev_mode != "neutral" else "neutral"

    inventory_mode[coin] = new_mode
    return new_mode


def execute_reprice(coin: str, exchange: Exchange, info: Info, address: str, reason: str = ""):
    """Cancel stale orders and place new quotes at fair price.

    Operates in two regimes:
      neutral:    two-sided quoting with microprice + flow + momentum
      long_exit:  tighten ask, widen/disable bid, prioritize flattening
      short_exit: tighten bid, widen/disable ask, prioritize flattening
    """
    global last_quote_ts

    # Read shared state under lock
    with _lock:
        book = book_state.get(coin)
        mp = microprice_state.get(coin, {})
        pos = positions.get(coin, {})

    if not book or not mp:
        return

    mid = mp.get("mid", 0)
    if mid <= 0:
        return

    pos_size = pos.get("size", 0)
    pos_usd = abs(pos_size) * mid

    # Update inventory regime
    mode = update_inventory_mode(coin, pos_size, mid)

    # Spread profitability gate: skip for neutral only (exit mode always quotes)
    market_spread_bps = (book["spread"] / mid) * 10000 if mid > 0 else 0
    if mode == "neutral" and market_spread_bps < MIN_SPREAD_BPS:
        return

    # Momentum filter: skew quotes during fast moves
    velocity_bps = compute_micro_velocity(coin, mid)
    quote_bid = True
    quote_ask = True

    if velocity_bps > MOMENTUM_THRESHOLD_BPS:
        quote_ask = False
        print(f"  [{coin}] Momentum UP {velocity_bps:+.2f}bps -> bids only")
    elif velocity_bps < -MOMENTUM_THRESHOLD_BPS:
        quote_bid = False
        print(f"  [{coin}] Momentum DOWN {velocity_bps:+.2f}bps -> asks only")

    fair = compute_fair_price(coin)
    if fair is None:
        return

    p_dec = PRICE_DECIMALS.get(coin, 2)
    s_dec = SIZE_DECIMALS.get(coin, 2)
    tick = 10 ** -p_dec

    # Base spread
    market_spread = book["spread"]
    min_spread = mid * MIN_SPREAD_BPS / 10000
    half_spread = max(min_spread / 2, market_spread / 2)

    # ─── Regime-specific spread via escalation ladder ───
    bid_half = half_spread
    ask_half = half_spread
    bid_size_mult = 1.0
    ask_size_mult = 1.0
    inv_age = time.time() - inventory_entered_at.get(coin, time.time())
    escalation_tier = "neutral"
    use_taker = False  # whether exit crosses spread

    if mode in ("long_exit", "short_exit"):
        # Find escalation tier based on inventory age
        exit_mult = 0.5
        entry_mult = 0.3
        for max_age, spread_mult, entry_size_mult, tier_name in EXIT_ESCALATION:
            if inv_age <= max_age:
                exit_mult = spread_mult
                entry_mult = entry_size_mult
                escalation_tier = tier_name
                break

        # Negative spread_mult means cross the spread (taker exit)
        if exit_mult <= 0:
            use_taker = True

        if mode == "long_exit":
            # Exit side = ask (sell to flatten)
            if use_taker:
                ask_half = exit_mult * half_spread  # negative = inside spread
            else:
                ask_half = half_spread * exit_mult
            bid_half = half_spread * EXIT_ENTRY_SPREAD_WIDEN
            bid_size_mult = entry_mult
            if entry_mult == 0:
                quote_bid = False
        else:  # short_exit
            # Exit side = bid (buy to flatten)
            if use_taker:
                bid_half = exit_mult * half_spread  # negative = inside spread
            else:
                bid_half = half_spread * exit_mult
            ask_half = half_spread * EXIT_ENTRY_SPREAD_WIDEN
            ask_size_mult = entry_mult
            if entry_mult == 0:
                quote_ask = False

        print(f"  [{coin}] EXIT {escalation_tier} age={inv_age:.0f}s mult={exit_mult}")

    # ─── Quote prices ───
    bid_price = round(fair - bid_half, p_dec)
    ask_price = round(fair + ask_half, p_dec)

    # ─── Queue position management ───
    # In neutral mode with momentum, step back 1 tick from best to avoid
    # being the first picked off during trends (queue-aware quoting)
    if mode == "neutral":
        if velocity_bps > 0.5:
            # Upward pressure: step ask back 1 tick behind best
            ask_price = max(ask_price, round(book["best_ask"] + tick, p_dec))
        elif velocity_bps < -0.5:
            # Downward pressure: step bid back 1 tick behind best
            bid_price = min(bid_price, round(book["best_bid"] - tick, p_dec))

        # Normal clamp: don't cross the book
        bid_price = min(bid_price, round(book["best_bid"], p_dec))
        ask_price = max(ask_price, round(book["best_ask"], p_dec))

    elif use_taker:
        # Taker exit: cross the spread to flatten immediately
        if mode == "long_exit":
            # Sell at or below best bid to guarantee fill
            ask_price = round(book["best_bid"] - tick, p_dec)
            print(f"  [{coin}] TAKER EXIT: sell @ ${ask_price:.{p_dec}f} (crossing spread)")
        else:
            # Buy at or above best ask to guarantee fill
            bid_price = round(book["best_ask"] + tick, p_dec)
            print(f"  [{coin}] TAKER EXIT: buy @ ${bid_price:.{p_dec}f} (crossing spread)")
    else:
        # Exit mode but not yet taker: clamp normally
        bid_price = min(bid_price, round(book["best_bid"], p_dec))
        ask_price = max(ask_price, round(book["best_ask"], p_dec))

    # Size (with exit-mode multipliers)
    base_size = round(ORDER_SIZE_USD / mid, s_dec)
    if base_size * mid < 10.0:
        base_size = round(10.5 / mid, s_dec)

    bid_size = round(base_size * bid_size_mult, s_dec) if bid_size_mult > 0 else 0
    ask_size = round(base_size * ask_size_mult, s_dec) if ask_size_mult > 0 else 0

    # In exit mode, exit side uses actual position size for clean flatten
    if mode == "long_exit" and pos_size > 0:
        ask_size = round(abs(pos_size), s_dec)
    elif mode == "short_exit" and pos_size < 0:
        bid_size = round(abs(pos_size), s_dec)

    # Minimum notional check
    if bid_size > 0 and bid_size * mid < 10.0:
        bid_size = round(10.5 / mid, s_dec)
    if ask_size > 0 and ask_size * mid < 10.0:
        ask_size = round(10.5 / mid, s_dec)

    # Check if existing orders are still close enough
    existing = active_oids.get(coin, {})
    if existing and mode == "neutral":
        old_bid = existing.get("buy_px", 0)
        old_ask = existing.get("sell_px", 0)
        bid_move = abs(bid_price - old_bid) / mid * 10000 if old_bid else 999
        ask_move = abs(ask_price - old_ask) / mid * 10000 if old_ask else 999

        if bid_move < REPRICE_THRESHOLD_BPS and ask_move < REPRICE_THRESHOLD_BPS:
            return  # no material change, skip
    # In exit mode, always reprice (urgency)

    # Cancel existing orders for this coin
    cancel_coin_orders(exchange, info, address, coin)

    # Place new quotes
    signal_bps = mp.get("signal_bps", 0)
    flow_bps = compute_trade_flow(coin)
    inv_skew = pos_size * mid / MAX_POSITION_USD if MAX_POSITION_USD > 0 else 0

    sides_parts = []
    if quote_bid and bid_size > 0:
        sides_parts.append("BID")
    if quote_ask and ask_size > 0:
        sides_parts.append("ASK")
    sides_str = "+".join(sides_parts) if sides_parts else "NONE"

    mode_tag = f" <{escalation_tier}>" if mode != "neutral" else ""
    age_tag = f" age={inv_age:.0f}s" if mode != "neutral" else ""

    print(f"  [{coin}] REPRICE ({reason}) fair=${fair:.{p_dec}f} "
          f"micro={signal_bps:+.1f}bps flow={flow_bps:+.1f}bps vel={velocity_bps:+.1f}bps "
          f"inv={inv_skew:+.2f}{mode_tag}{age_tag} [{sides_str}]")
    print(f"    BID ${bid_price:.{p_dec}f} x{bid_size} | ASK ${ask_price:.{p_dec}f} x{ask_size}")

    buy_oid = None
    sell_oid = None

    # Place buy if allowed
    if quote_bid and bid_size > 0 and (pos_usd < MAX_POSITION_USD or pos_size <= 0):
        # Use IOC for taker exits (crossing spread), ALO for maker quotes
        is_taker_exit = use_taker and mode == "short_exit"
        buy_oid = place_order(exchange, coin, True, bid_size, bid_price,
                              taker=is_taker_exit, reduce_only=is_taker_exit)

    # Place sell if allowed
    if quote_ask and ask_size > 0 and (pos_usd < MAX_POSITION_USD or pos_size >= 0):
        is_taker_exit = use_taker and mode == "long_exit"
        sell_oid = place_order(exchange, coin, False, ask_size, ask_price,
                               taker=is_taker_exit, reduce_only=is_taker_exit)

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


def place_order(exchange: Exchange, coin: str, is_buy: bool, size: float, price: float,
                taker: bool = False, reduce_only: bool = False) -> Optional[int]:
    """Place a limit order. ALO (post-only) by default, IOC for taker exits."""
    try:
        order_type = {"limit": {"tif": "Ioc"}} if taker else {"limit": {"tif": "Alo"}}
        result = exchange.order(
            coin, is_buy, size, price,
            order_type,
            reduce_only=reduce_only,
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

        # Detect round-trip completions (position went non-zero -> ~zero)
        now = time.time()
        for coin, new_pos in new_positions.items():
            new_size = new_pos["size"]
            old_size = prev_positions.get(coin, 0)

            if abs(old_size) > 0 and abs(new_size) < abs(old_size) * 0.1:
                # Position flattened (or nearly)
                entry_time = inventory_entered_at.get(coin, now)
                hold_secs = now - entry_time
                # Get realized PnL from recent fills for this coin
                recent_coin_fills = [f for f in all_fills
                                     if coin in f.get("pair", "") and f["time"] > entry_time]
                trip_pnl = sum(f.get("closed_pnl", 0) for f in recent_coin_fills)
                trip_fees = sum(f.get("fee", 0) for f in recent_coin_fills)

                round_trips.append({
                    "coin": coin,
                    "entry_time": entry_time,
                    "exit_time": now,
                    "hold_secs": round(hold_secs, 1),
                    "pnl": round(trip_pnl, 6),
                    "fees": round(trip_fees, 6),
                    "net": round(trip_pnl - trip_fees, 6),
                })
                print(f"  [{coin}] ROUND TRIP #{len(round_trips)} completed | "
                      f"hold={hold_secs:.1f}s pnl=${trip_pnl:.4f} fees=${trip_fees:.4f} net=${trip_pnl - trip_fees:.4f}")
                tg_send(f"✅ <b>Round Trip #{len(round_trips)}</b> {coin}\n"
                        f"Hold: {hold_secs:.1f}s | Net: ${trip_pnl - trip_fees:.4f}")

            prev_positions[coin] = new_size

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

def compute_flatten_stats() -> dict:
    """Compute flatten time percentiles and PnL-by-holding-time buckets."""
    if not round_trips:
        return {
            "avg_hold_secs": 0, "median_hold_secs": 0,
            "p95_hold_secs": 0, "avg_trip_net": 0,
            "pnl_by_hold_time": {},
        }

    hold_times = sorted(t["hold_secs"] for t in round_trips)
    n = len(hold_times)

    avg_hold = sum(hold_times) / n
    median_hold = hold_times[n // 2]
    p95_hold = hold_times[int(n * 0.95)] if n >= 2 else hold_times[-1]
    avg_net = sum(t["net"] for t in round_trips) / n

    # PnL bucketed by holding time
    buckets = {"<5s": [], "5-20s": [], ">20s": []}
    for t in round_trips:
        h = t["hold_secs"]
        if h < 5:
            buckets["<5s"].append(t["net"])
        elif h <= 20:
            buckets["5-20s"].append(t["net"])
        else:
            buckets[">20s"].append(t["net"])

    pnl_by_hold = {}
    for bucket, nets in buckets.items():
        if nets:
            pnl_by_hold[bucket] = {
                "count": len(nets),
                "avg_net": round(sum(nets) / len(nets), 6),
                "total_net": round(sum(nets), 6),
            }

    return {
        "avg_hold_secs": round(avg_hold, 1),
        "median_hold_secs": round(median_hold, 1),
        "p95_hold_secs": round(p95_hold, 1),
        "avg_trip_net": round(avg_net, 6),
        "pnl_by_hold_time": pnl_by_hold,
    }


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
            "velocity_bps": round(compute_micro_velocity(coin, mp["mid"]) if mp["mid"] > 0 else 0, 2),
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
            "mode": inventory_mode.get(coin, "neutral"),
            "inventory_age_s": round(time.time() - inventory_entered_at.get(coin, time.time()), 1) if inventory_mode.get(coin, "neutral") != "neutral" else 0,
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
        "round_trips_completed": len(round_trips),
        "completion_ratio": round(len(round_trips) / (total_trade_count / 2) if total_trade_count >= 2 else 0, 3),
        "flatten_stats": compute_flatten_stats(),
        "recent_trips": round_trips[-10:],
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
                        vel = compute_micro_velocity(coin, mp.get("mid", 1))
                        mode = inventory_mode.get(coin, "neutral")
                        age = time.time() - inventory_entered_at.get(coin, time.time()) if mode != "neutral" else 0
                        mode_str = f" [{mode} {age:.0f}s]" if mode != "neutral" else ""
                        print(f"  {coin} micro=${mp.get('micro', 0):.2f} "
                              f"sig={mp.get('signal_bps', 0):+.1f}bps "
                              f"flow={flow:+.1f}bps "
                              f"vel={vel:+.1f}bps "
                              f"pos={pos_size:+.3f}{mode_str}")

                elapsed = (time.time() - start_time) / 60
                stats = compute_flatten_stats()
                trips = len(round_trips)
                comp_ratio = trips / (total_trade_count / 2) if total_trade_count >= 2 else 0
                print(f"  Portfolio: ${pv:.2f} | Fills: {total_trade_count} | Trips: {trips} "
                      f"({comp_ratio:.0%}) | Hold: {stats['avg_hold_secs']:.1f}s avg "
                      f"{stats['median_hold_secs']:.1f}s med {stats['p95_hold_secs']:.1f}s p95 | {elapsed:.1f}m")

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
