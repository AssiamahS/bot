#!/usr/bin/env python3
"""
Hyperliquid Market Maker.
Maker fee is 1.5bps at low volume tiers (rebate only at 25M+ monthly).
Must capture >4bps spread per round trip to profit after fees on both legs.
Writes status to trader_status.json for dashboard.
"""
import time
import json
import signal
import os
import sys
import threading
import socket
socket.setdefaulttimeout(5)  # prevent hanging on slow API calls
import urllib.request
import urllib.parse
from collections import deque
from typing import Optional

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from tg_commander import TelegramCommander
from event_logger import EventLogger

# === SINGLETON LOCK: prevent multiple instances ===
import fcntl
import atexit
_lock_file = open("/tmp/trader.lock", "w")
try:
    fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()
except IOError:
    print("ERROR: Another trader instance is already running. Exiting.")
    sys.exit(1)

def _cleanup_lock():
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_UN)
        _lock_file.close()
        os.remove("/tmp/trader.lock")
    except Exception:
        pass

atexit.register(_cleanup_lock)

# === EVENT LOGGER ===
BUILD_ID = "git:b67fe43fe"
event_log = EventLogger()

def _api_call(fn, *args, **kwargs):
    """Wrap REST API calls with rate limit check. No retry on 429 — skip instead."""
    if not rate_limiter_check():
        print(f"  RATE LIMIT: skipping API call (>{MAX_REQUESTS_PER_MIN}/min)")
        return None
    try:
        rate_limiter_record()
        result = fn(*args, **kwargs)
        return result
    except Exception as e:
        if '429' in str(e):
            print(f"  429 from HL — skipping cycle (no retry)")
            return None
        raise

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_status.json")

# Load config
with open(CONFIG_FILE) as f:
    config = json.load(f)


# Allow config values to reference env vars: set value to "ENV" to load from environment
def _cfg(key, default=""):
    val = config.get(key, default)
    if val == "ENV":
        return os.environ.get(key.upper(), os.environ.get(key, default))
    return val
# Telegram alerts — loaded from config, not hardcoded
TG_TOKEN = _cfg("tg_token")
TG_CHAT_ID = _cfg("tg_chat_id")

def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data=data, timeout=5)
    except Exception:
        pass

PRIVATE_KEY = _cfg("wallet_private_key")
WALLET_ADDRESS = config.get("wallet_address", "")
USE_TESTNET = config.get("use_testnet", True)
PAIRS = config.get("pairs", ["BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP", "PURR-PERP", "DYDX-PERP", "IOTA-PERP"])
ORDER_SIZE_USD = config.get("order_size_usd", 10.0)
REFRESH_SECS = config.get("refresh_secs", 15)
MIN_SPREAD_BPS = config.get("min_spread_bps", 10)  # raised: 10bps minimum displayed spread
PROFITABILITY_MODE = config.get("profitability_mode", "strict").lower()
SAFETY_BPS_STRICT = config.get("safety_bps_strict", 1.5)
SAFETY_BPS_AGGRESSIVE = config.get("safety_bps_aggressive", 0.5)

# Dynamic asset metadata — populated from exchange on startup
# COIN_MAP: "SOL-PERP" -> "SOL", auto-generated from PAIRS
COIN_MAP = {p: p.replace("-PERP", "") for p in PAIRS}

# These get populated by fetch_asset_metadata() on startup
SIZE_DECIMALS = {}
PRICE_DECIMALS = {}

def fetch_asset_metadata():
    """Fetch szDecimals and price precision from Hyperliquid meta endpoint.
    Must be called before trading starts."""
    from hyperliquid.info import Info as _Info
    from hyperliquid.utils import constants as _c
    base = _c.TESTNET_API_URL if USE_TESTNET else _c.MAINNET_API_URL
    _info = _Info(base, skip_ws=True)
    meta = _info.meta()
    universe = meta.get("universe", [])

    for asset in universe:
        coin = asset["name"]
        SIZE_DECIMALS[coin] = asset.get("szDecimals", 2)

    # Derive price decimals from live book tick
    for pair in PAIRS:
        coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
        if coin not in SIZE_DECIMALS:
            print(f"  WARNING: {coin} not found in exchange metadata, defaulting szDecimals=2")
            SIZE_DECIMALS[coin] = 2
        try:
            book = _info.l2_snapshot(coin)
            if book and book["levels"][0]:
                px_str = book["levels"][0][0]["px"]
                if "." in px_str:
                    PRICE_DECIMALS[coin] = len(px_str.split(".")[1])
                else:
                    PRICE_DECIMALS[coin] = 0
                # Derive tick size
                TICK_SIZE[coin] = 10 ** (-PRICE_DECIMALS[coin])
                print(f"  {coin}: szDec={SIZE_DECIMALS[coin]} pxDec={PRICE_DECIMALS[coin]} tick={TICK_SIZE[coin]}")
        except Exception as e:
            PRICE_DECIMALS.setdefault(coin, 4)
            TICK_SIZE.setdefault(coin, 0.0001)
            print(f"  {coin}: using defaults (error: {e})")

running = True
start_time = time.time()
all_fills = []
active_orders = []
last_prices = {}
last_balances = {"positions": {}}  # start with empty positions so scoring works before first API pull
pair_fills = {p: [] for p in PAIRS}
pair_trade_count = {p: 0 for p in PAIRS}
total_trade_count = 0
initial_portfolio_value = None

# Volatility tracking
price_history = {COIN_MAP.get(p, p.replace("-PERP", "")): [] for p in PAIRS}
VOL_WINDOW = 20

# Risk governor limits (scaled for small portfolio)
MAX_DRAWDOWN = 0.50  # raised: account already absorbed prior losses, protect from here
MAX_INVENTORY_USD = 3.5  # tighter cap per coin, forces faster exits
MAX_POSITION_NOTIONAL = 25.0  # hard cap: total exposure across all coins cannot exceed this
MAX_VOLATILITY_BPS = 50
COOLDOWN_SECS = 30
risk_cooldown_until = 0

# Delta hedger: monitor portfolio-level directional exposure and flatten when too skewed
HEDGE_DELTA_THRESHOLD_PCT = 10.0  # trigger hedge when net delta > 10% of equity
HEDGE_MAX_DELTA_PCT = 20.0  # hard limit: block new entries above this
HEDGE_COOLDOWN_SECS = 60  # minimum seconds between hedge actions
_last_hedge_time = 0

def check_and_hedge(info, exchange, address):
    """Check portfolio delta and reduce the most imbalanced position if needed.
    Uses limit orders (maker) to avoid taker fees."""
    global _last_hedge_time
    now = time.time()
    if now - _last_hedge_time < HEDGE_COOLDOWN_SECS:
        return

    equity = portfolio_value()
    if equity <= 0:
        return

    positions = last_balances.get("positions", {})
    if not positions:
        return

    # Compute net delta (signed notional across all positions)
    net_delta = 0.0
    per_coin = {}
    for coin, pos in positions.items():
        sz = pos.get("size", 0)
        mid = 0
        # Get mid from ws_book or last_prices
        book = get_ws_book(coin)
        if book:
            mid = book["mid"]
        else:
            for p in PAIRS:
                if coin in p and p in last_prices:
                    mid = last_prices[p].get("mid", 0)
                    break
        if mid <= 0:
            continue
        signed_ntl = sz * mid
        net_delta += signed_ntl
        per_coin[coin] = {"signed_ntl": signed_ntl, "size": sz, "mid": mid}

    delta_pct = abs(net_delta) / equity * 100
    if delta_pct < HEDGE_DELTA_THRESHOLD_PCT:
        return

    # Find the coin contributing most to the imbalance
    # Sort by absolute contribution, reduce the biggest offender
    sorted_coins = sorted(per_coin.items(), key=lambda x: abs(x[1]["signed_ntl"]), reverse=True)

    for coin, data in sorted_coins:
        signed_ntl = data["signed_ntl"]
        # Only reduce positions that are in the same direction as net delta
        if (net_delta > 0 and signed_ntl <= 0) or (net_delta < 0 and signed_ntl >= 0):
            continue

        sz = data["size"]
        mid = data["mid"]
        s_dec = SIZE_DECIMALS.get(coin, 2)
        p_dec = PRICE_DECIMALS.get(coin, 2)
        tick = TICK_SIZE.get(coin, 0.01)

        # Reduce by 50% of this position (don't close entirely, just reduce delta)
        reduce_sz = round(abs(sz) * 0.5, s_dec)
        if reduce_sz * mid < 1.0:
            continue  # too small to bother

        book = get_ws_book(coin)
        if not book:
            continue

        if sz > 0:
            # Long: sell to reduce
            hedge_px = round(book["best_bid"], p_dec)  # sell at bid for faster fill
            oid = place_order(exchange, coin, False, reduce_sz, hedge_px, reduce_only=True)
            side_str = "reducing LONG"
        else:
            # Short: buy to reduce
            hedge_px = round(book["best_ask"], p_dec)  # buy at ask for faster fill
            oid = place_order(exchange, coin, True, reduce_sz, hedge_px, reduce_only=True)
            side_str = "reducing SHORT"

        _last_hedge_time = now
        print(f"  >>> HEDGE: delta={delta_pct:.1f}%, {side_str} {coin} {reduce_sz} @ ${hedge_px}")
        tg_send(f"🔄 <b>HEDGE</b>: delta={delta_pct:.1f}%, {side_str} {coin}\nSize: {reduce_sz} @ ${hedge_px}")
        event_log.log_custom("hedge", coin=coin, delta_pct=round(delta_pct, 1), reduce_sz=reduce_sz, side=side_str)
        break  # one hedge per cycle


# Multi-quote state: track our resting orders per coin
live_quotes = {}
round_trips = 0  # completed buy+sell cycles
# Round-trip tracking: measure actual profit per completed cycle
# A round trip = buy fill followed by sell fill (or vice versa) on same coin
trip_tracker = {}  # coin -> {"side": "buy"/"sell", "price": float, "size": float, "fee": float, "time": float}
completed_trips = []  # list of {"coin", "buy_px", "sell_px", "size", "gross", "fees", "net", "duration"}
fill_edges = []  # edge in bps per fill, for realized spread tracking
post_fill_checks = []  # list of {"coin", "side", "price", "check_at"} for delayed edge tracking
consecutive_side_per_coin = {}  # coin -> {"side": str, "count": int, "first_time": float}
adverse_pause_until = {}  # coin -> timestamp, pause vulnerable side after consecutive fills
strategy_pause_until = 0  # global pause when avg trip net is negative
quoting_paused = False  # remote pause via Telegram /pause command
adverse_size_mult = {}  # coin -> size multiplier after adverse detection
adverse_side_locked = {}  # coin -> {"side": "B"/"A", "until": timestamp} - hard lockout after 5+ consecutive fills
# Gating counters
quote_attempts = 0
quotes_skipped_profitability = 0
quotes_placed = 0
quotes_skipped_budget = 0
quotes_skipped_risk = 0
quotes_skipped_weak = 0
# Inventory tracking for mean/variance
inventory_samples = []  # list of inventory_usd values over time
last_profitability_diag = {"market_spread_bps": 0.0, "required_bps": 0.0, "market_ticks": 0, "required_ticks": 0, "expected_net": 0.0}
# --- Real sliding window rate limiter ---
# Hyperliquid limit: 1200 requests/min per IP. We use 600/min (50% safety margin).
MAX_REQUESTS_PER_MIN = 600
_request_timestamps = deque()
_rate_limit_lock = threading.Lock()  # thread safety: WS callbacks + main loop both access timestamps
request_count = 0  # lifetime counter for dashboard
volume_traded_usd = 0.0
request_budget_paused = False

def rate_limiter_check():
    """Return True if we can make another request within the rate limit."""
    with _rate_limit_lock:
        now = time.time()
        while _request_timestamps and now - _request_timestamps[0] > 60:
            _request_timestamps.popleft()
        return len(_request_timestamps) < MAX_REQUESTS_PER_MIN

def rate_limiter_record():
    """Record a request timestamp."""
    global request_count
    with _rate_limit_lock:
        _request_timestamps.append(time.time())
        request_count += 1

def track_request():
    """Track an API request (for calls not going through _api_call)."""
    rate_limiter_record()

def track_volume(usd_amount):
    global volume_traded_usd
    volume_traded_usd += abs(usd_amount)

def request_budget_remaining():
    """Requests remaining in current 60s window."""
    with _rate_limit_lock:
        now = time.time()
        while _request_timestamps and now - _request_timestamps[0] > 60:
            _request_timestamps.popleft()
        return MAX_REQUESTS_PER_MIN - len(_request_timestamps)

# Queue-preserving quoting: two-tier drift thresholds
QUEUE_KEEP_BPS = 8    # keep resting order if drift <= this (preserve queue position)
REPLACE_BPS = 15      # only cancel+replace if drift exceeds this
# Dynamic: widen tolerance when rate limit budget is low
def get_drift_thresholds():
    remaining = request_budget_remaining()
    if remaining < 100:
        return 12, 20  # very conservative when near limit
    elif remaining < 200:
        return 10, 18  # conservative
    return QUEUE_KEEP_BPS, REPLACE_BPS  # normal
MAKER_FEE_BPS = 1.5  # Hyperliquid maker fee at our volume tier
TAKER_FEE_BPS = 3.5  # Hyperliquid taker fee - AVOID THIS
MAKER_ONLY = True  # Never place orders that would cross the spread
MIN_PROFIT_BPS = 5.0  # minimum profit per round trip after fees (raised from 3.0)
MIN_CAPTURE_BPS = 2 * MAKER_FEE_BPS + MIN_PROFIT_BPS  # = 8.0 bps
MIN_TRIP_NET_USD = 0.005  # hard floor: skip setups where expected net < $0.005
QUOTE_LEVELS = 1  # single level per side until consistently profitable
LEVEL_SPACING_TICKS = 2  # ticks between levels (only used if QUOTE_LEVELS > 1)

# Weak pair suspension: escalating — gets longer each time a coin re-fails
WEAK_PAIR_LOOKBACK = 5  # trigger faster: only need 5 trips to evaluate
WEAK_PAIR_BASE_SUSPEND = 900  # 15 min first offense
WEAK_PAIR_MAX_SUSPEND = 7200  # 2 hour max suspension
WEAK_PAIR_MAX_STRIKES = 5  # beyond this, pair is disabled (needs manual re-enable)
WEAK_PAIR_STALL_ALERT_SECS = 900  # tg alert if all pairs suspended this long
weak_pair_suspended = {}  # coin -> resume_timestamp
weak_pair_strikes = {}  # coin -> number of times suspended (escalation counter)
weak_pair_disabled = set()  # coins hard-disabled after MAX_STRIKES — manual re-enable
weak_pair_trip_mark = {}  # coin -> trip-count snapshot at last resume (for fresh-slate eval)
# Per-coin trip history for weak pair detection
coin_trips = {}  # coin -> list of completed trip dicts
_last_stall_alert = 0.0  # unix ts of last "all pairs suspended" tg alert
_all_suspended_since = None  # unix ts when all pairs first went suspended (None = not all suspended)

# Queue quality thresholds
CROWDED_SIZE = 30  # units at top level = crowded queue
THIN_SIZE = 10     # units at top level = thin (good to join)
TIGHT_SPREAD_BPS = 6  # below this, spread too tight to compete
TICK_SIZE = {}  # populated dynamically by fetch_asset_metadata()

# WebSocket live book data (updated by WS callbacks)
ws_book = {}  # coin -> {"bids": [...], "asks": [...], "ts": time}
ws_book_lock = threading.Lock()
ws_fills_pending = []  # new fills from WS
ws_subscribe_time = 0  # set when WS userFills subscribed, used to filter startup replays
ws_fills_lock = threading.Lock()
# Event-driven: signal when book changes materially
book_changed = threading.Event()
fill_received = threading.Event()  # wake main loop only on fills
last_quote_time = {}  # coin -> timestamp of last quote update
# Adaptive quote interval: fast when budget healthy, slow when tight
MIN_QUOTE_INTERVAL_FAST = 4.0   # when rate limit healthy (>300 remaining)
MIN_QUOTE_INTERVAL_NORMAL = 6.0 # moderate (150-300)
MIN_QUOTE_INTERVAL_SLOW = 10.0  # low (<150)

def get_quote_interval():
    remaining = request_budget_remaining()
    if remaining < 150:
        return MIN_QUOTE_INTERVAL_SLOW
    elif remaining < 300:
        return MIN_QUOTE_INTERVAL_NORMAL
    return MIN_QUOTE_INTERVAL_FAST

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
            if False:  # fills-only: do not wake on book changes
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
                fill_received.set()
        elif isinstance(data, dict) and "fills" in data:
            with ws_fills_lock:
                ws_fills_pending.extend(data["fills"])
                fill_received.set()
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
    bid_vol = bids[0]["sz"]
    ask_vol = asks[0]["sz"]
    # Microprice: volume-weighted mid biased toward side with more pressure
    if bid_vol + ask_vol > 0:
        microprice = (best_bid * ask_vol + best_ask * bid_vol) / (bid_vol + ask_vol)
    else:
        microprice = (best_bid + best_ask) / 2
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": (best_bid + best_ask) / 2,
        "microprice": microprice,
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

    # Fetch asset metadata (szDecimals, tick sizes) before anything else
    fetch_asset_metadata()

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
    global ws_subscribe_time
    ws_subscribe_time = time.time()
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
    """Risk governor: returns reason string if trading should pause, else None.
    Inventory is handled by side-gating, not cooldown — only pause on drawdown/volatility.
    """
    if initial_portfolio_value and initial_portfolio_value > 0 and equity > 0:
        drawdown = (initial_portfolio_value - equity) / initial_portfolio_value
        if drawdown > MAX_DRAWDOWN:
            # Only trigger if equity is a reasonable number (not a 429 partial read)
            if equity > 5.0:  # sanity: ignore obviously wrong equity values
                return f"drawdown {drawdown*100:.1f}%"
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
        # Only clear positions on non-transient errors (429 = keep existing data)
        if '429' not in str(e) and 'rate' not in str(e).lower():
            last_balances["positions"] = {}
    return None


def cancel_all_orders(exchange, info, address):
    """Cancel all open orders."""
    try:
        open_orders = _api_call(info.open_orders, address) or []
        if open_orders:
            for order in open_orders:
                coin = order.get("coin", "")
                oid = order.get("oid", 0)
                try:
                    cancel_order(exchange, coin, oid)
                except Exception:
                    pass
            print(f"  Cancelled {len(open_orders)} orders")
            return len(open_orders)
    except Exception as e:
        print(f"  Cancel error: {e}")
    return 0


def place_order(exchange, coin, is_buy, size, price, reduce_only=False):
    """Place a limit post-only order."""
    if not rate_limiter_check():
        print(f"  RATE LIMIT: skipping order {coin}")
        return None
    try:
        rate_limiter_record()
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


def cancel_order(exchange, coin, oid):
    """Cancel an order and track the API request. Use this instead of raw exchange.cancel()."""
    if not rate_limiter_check():
        print(f"  RATE LIMIT: skipping cancel {coin} {oid}")
        return
    rate_limiter_record()
    exchange.cancel(coin, oid)


def check_fills(info, exchange, address):
    """Check for recent fills and track round-trip profitability."""
    global total_trade_count, round_trips, strategy_pause_until
    try:
        fills = _api_call(info.user_fills, address) or []
        new_fills = [f for f in fills if float(f.get("time", 0)) / 1000 > start_time]
        new_count = len(new_fills)

        if new_count > total_trade_count:
            for f in new_fills[total_trade_count:]:
                coin = f.get("coin", "")
                side = f.get("side", "").upper()  # "A" (sell) or "B" (buy)
                price = float(f.get("px", 0))
                size = float(f.get("sz", 0))
                fee = float(f.get("fee", 0))
                cost = price * size
                closed_pnl = float(f.get("closedPnl", 0))

                # Calculate fill edge vs mid price
                fill_book = get_ws_book(coin)
                fill_mid = fill_book.get("mid", price) if fill_book else price
                if side == "B":
                    edge = fill_mid - price  # positive = bought below mid (good)
                else:
                    edge = price - fill_mid  # positive = sold above mid (good)
                edge_bps = edge / fill_mid * 10000 if fill_mid > 0 else 0

                fill_edges.append(edge_bps)
                # Schedule post-fill edge check in 3 seconds (PFASR)
                post_fill_checks.append({"coin": coin, "side": side, "price": price, "check_at": time.time() + 3.0})

                # Adverse selection detector with timing (PER-COIN)
                now_fill = time.time()
                coin_consec = consecutive_side_per_coin.get(coin)
                if coin_consec and coin_consec["side"] == side:
                    coin_consec["count"] += 1
                else:
                    # Different side or first fill on this coin
                    if coin_consec and coin_consec["side"] != side:
                        # Opposite-side fill clears lockout
                        if coin in adverse_side_locked:
                            print(f"  >>> LOCKOUT CLEARED: {coin} opposite fill")
                            adverse_side_locked.pop(coin, None)
                            adverse_pause_until.pop(coin, None)
                            adverse_size_mult.pop(coin, None)
                    consecutive_side_per_coin[coin] = {"side": side, "count": 1, "first_time": now_fill}
                    coin_consec = consecutive_side_per_coin[coin]
                consec = coin_consec["count"]
                consec_window = now_fill - coin_consec["first_time"]
                consec_warn = ""

                # Escalating adverse selection protection
                if consec >= 5:
                    consec_warn = f" ⚠️{consec}x{side} LOCKED"
                    adverse_pause_until[coin] = now_fill + 60.0
                    adverse_size_mult[coin] = 0.5
                    # Hard lockout: stop quoting this side entirely
                    adverse_side_locked[coin] = {"side": side, "until": now_fill + 120.0}
                    # Cancel ALL resting orders for this coin immediately
                    # (pre-lockout orders can still fill if only cancelled by side)
                    try:
                        _ords = _api_call(info.open_orders, address) or []
                        _cancelled = 0
                        for _o in _ords:
                            if _o.get('coin') == coin:
                                try:
                                    cancel_order(exchange, coin, _o['oid'])
                                    _cancelled += 1
                                except: pass
                        if _cancelled:
                            print(f'  >>> LOCKOUT: cancelled ALL {_cancelled} {coin} resting orders')
                        live_quotes.pop(coin, None)
                    except Exception:
                        pass
                    tg_send(f"🔒 <b>SIDE LOCKED</b> {coin}: {consec}x{side} - blocking for 120s")
                elif consec >= 3:
                    consec_warn = f" ⚠️{consec}x{side} PAUSED"
                    adverse_pause_until[coin] = now_fill + 30.0
                    adverse_size_mult[coin] = 0.5
                    # Cancel resting orders on the adverse side to stop bleeding
                    try:
                        _ords = _api_call(info.open_orders, address) or []
                        for _o in _ords:
                            if _o.get('coin') == coin and _o.get('side') == side:
                                try: cancel_order(exchange, coin, _o['oid'])
                                except: pass
                    except: pass

                print(f"  >>> FILL: {side} {size} {coin} @ ${price:.2f} fee=${fee:.4f} pnl=${closed_pnl:.4f} edge={edge_bps:+.1f}bps{consec_warn}")
                emoji = "🟢" if side == "B" else "🔴"
                rebate_str = f"Rebate: +${-fee:.4f}" if fee < 0 else f"Fee: ${fee:.4f}"
                tg_msg = f"{emoji} <b>FILL</b>: {side} {size} {coin}\n💰 @ ${price:.2f} | {rebate_str} | Edge: {edge_bps:+.1f}bps"
                if consec >= 5:
                    tg_msg += f"\n🔒 LOCKED: {consec}x{side} - side blocked 120s"
                elif consec >= 3:
                    tg_msg += f"\n⚠️ ADVERSE: {consec}x{side} - paused 30s"
                tg_send(tg_msg)

                # Strategy pause: escalating — 5min base, doubles each consecutive trigger, 30min cap
                if len(completed_trips) >= 10:
                    recent_n = completed_trips[-10:]
                    avg_recent = sum(t["net"] for t in recent_n) / len(recent_n)
                    if avg_recent < 0 and now_fill > strategy_pause_until:
                        strategy_pause_count = getattr(sys.modules[__name__], '_strategy_pause_count', 0) + 1
                        sys.modules[__name__]._strategy_pause_count = strategy_pause_count
                        pause_secs = min(300 * (2 ** (strategy_pause_count - 1)), 1800)  # 5m -> 10m -> 20m -> 30m cap
                        strategy_pause_until = now_fill + pause_secs
                        print(f"  >>> STRATEGY PAUSE #{strategy_pause_count}: last 10 trips avg net ${avg_recent:.4f} < 0, pausing {pause_secs//60}min")
                        tg_send(f"⏸️ <b>STRATEGY PAUSE #{strategy_pause_count}</b>\nLast 10 trips avg: ${avg_recent:.4f}\nPausing {pause_secs//60}min")

                # --- ROUND TRIP TRACKING ---
                leg = trip_tracker.get(coin)
                if leg is None:
                    # First leg of a new trip
                    trip_tracker[coin] = {"side": side, "price": price, "size": size, "fee": fee, "time": time.time()}
                elif leg["side"] != side:
                    # Opposite side = completing a round trip!
                    trip_size = min(leg["size"], size)
                    total_fee = leg["fee"] + fee
                    if leg["side"] == "B":
                        gross = (price - leg["price"]) * trip_size  # bought low, sold high
                    else:
                        gross = (leg["price"] - price) * trip_size  # sold high, bought low
                    net = gross - total_fee
                    duration = time.time() - leg["time"]

                    trip = {
                        "coin": coin, "size": trip_size,
                        "buy_px": leg["price"] if leg["side"] == "B" else price,
                        "sell_px": price if leg["side"] == "B" else leg["price"],
                        "gross": round(gross, 6), "fees": round(total_fee, 6),
                        "net": round(net, 6), "duration": round(duration, 1),
                    }
                    completed_trips.append(trip)
                    event_log.log_trip(trip_num=round_trips, coin=coin, net_pnl=net)
                    # Track per-coin trip history for weak pair detection
                    if coin not in coin_trips:
                        coin_trips[coin] = []
                    coin_trips[coin].append(trip)
                    round_trips += 1

                    net_sign = "+" if net >= 0 else ""
                    print(f"  >>> TRIP #{round_trips}: {trip['buy_px']:.2f}->{trip['sell_px']:.2f} | Gross: ${gross:.4f} | Fees: ${total_fee:.4f} | Net: {net_sign}${net:.4f} | {duration:.0f}s")
                    tg_send(f"{'✅' if net >= 0 else '❌'} <b>Trip #{round_trips}</b> {coin}\nBuy ${trip['buy_px']:.2f} -> Sell ${trip['sell_px']:.2f}\nGross: ${gross:.4f} | Fees: ${total_fee:.4f}\n<b>Net: {net_sign}${net:.4f}</b> | {duration:.0f}s")

                    # Reset tracker for remaining size
                    remaining = size - trip_size
                    if remaining > 0.001:
                        trip_tracker[coin] = {"side": side, "price": price, "size": remaining, "fee": fee * remaining / size, "time": time.time()}
                    else:
                        trip_tracker.pop(coin, None)
                else:
                    # Same side fill = accumulating, keep FIRST fill price as anchor
                    # (averaging dilutes the anchor and makes exit harder)
                    total_size = leg["size"] + size
                    trip_tracker[coin] = {"side": side, "price": leg["price"], "size": total_size, "fee": leg["fee"] + fee, "time": leg["time"]}

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
                event_log.log_fill(coin=coin, side=side.lower(), size=size, price=price, fee=fee, closed_pnl=closed_pnl)
                event_log.log_custom("fill_context", coin=coin, edge_bps=round(edge_bps, 2), mid=round(fill_mid, 6), build_id=BUILD_ID)
                track_volume(cost)  # track for request budget calculation

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

    total_inventory_usd = 0.0
    for p in PAIRS:
        c = COIN_MAP.get(p, p.replace("-PERP", ""))
        pos = last_balances.get("positions", {}).get(c, {})
        sz = pos.get("size", 0)
        mid = last_prices.get(p, {}).get("mid", 0)
        total_inventory_usd += sz * mid

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
        "round_trips": round_trips,
        "completed_trips": completed_trips[-20:],
        "trip_net_pnl": round(sum(t["net"] for t in completed_trips), 6),
        "trip_winners": sum(1 for t in completed_trips if t["net"] >= 0),
        "trip_losers": sum(1 for t in completed_trips if t["net"] < 0),
        "trip_avg_gross": round(sum(t["gross"] for t in completed_trips) / len(completed_trips), 6) if completed_trips else 0,
        "trip_avg_fees": round(sum(t["fees"] for t in completed_trips) / len(completed_trips), 6) if completed_trips else 0,
        "trip_avg_net": round(sum(t["net"] for t in completed_trips) / len(completed_trips), 6) if completed_trips else 0,
        "trip_fee_ratio": round(sum(t["fees"] for t in completed_trips) / max(sum(t["gross"] for t in completed_trips), 0.0001), 2) if completed_trips else 0,
        "turnover_x": round(sum(f["cost"] for f in all_fills) / pv, 1) if pv > 0 else 0,
        "turnover_per_hr": round((sum(f["cost"] for f in all_fills) / pv) / max((time.time() - start_time) / 3600, 0.01), 1) if pv > 0 else 0,
        "avg_edge_bps": round(sum(fill_edges) / len(fill_edges), 1) if fill_edges else 0,
        "positive_edge_pct": round(sum(1 for e in fill_edges if e > 0) / len(fill_edges) * 100, 0) if fill_edges else 0,
        "profitability_mode": PROFITABILITY_MODE,
        "quote_attempts": quote_attempts,
        "quotes_skipped_profitability": quotes_skipped_profitability,
        "quotes_placed": quotes_placed,
        "gate_skip_pct": round(quotes_skipped_profitability / max(quote_attempts, 1) * 100, 1),
        "inventory_usd": round(total_inventory_usd, 4),
        "inventory_mean": round(sum(inventory_samples) / max(len(inventory_samples), 1), 2),
        "inventory_variance": round(sum((x - sum(inventory_samples) / max(len(inventory_samples), 1))**2 for x in inventory_samples) / max(len(inventory_samples), 1), 2) if inventory_samples else 0,
        "last_profitability_diag": last_profitability_diag,
        "updated_at": time.time(),
    }
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f)
    except Exception:
        pass


def get_open_orders_by_coin(info, address):
    """Get open orders grouped by coin (REST call)."""
    result = {}
    try:
        orders = _api_call(info.open_orders, address) or []
        for o in orders:
            coin = o.get("coin", "")
            if coin not in result:
                result[coin] = []
            result[coin].append(o)
    except Exception as e:
        print(f"  Open orders error: {e}")
    return result

# Cached open orders — reconciled via REST every ORDERS_RECONCILE_SECS
_cached_orders = {}  # coin -> [orders]
_cached_orders_time = 0
ORDERS_RECONCILE_SECS = 60

def _get_cached_orders(info, address):
    """Return cached open orders, refreshing from REST every 60s."""
    global _cached_orders, _cached_orders_time
    now = time.time()
    if now - _cached_orders_time > ORDERS_RECONCILE_SECS:
        _cached_orders = get_open_orders_by_coin(info, address)
        _cached_orders_time = now
        print(f"  ORDERS RECONCILE: {sum(len(v) for v in _cached_orders.values())} orders across {len(_cached_orders)} coins")
    return _cached_orders


MAX_QUOTE_PAIRS = 2  # only quote top 2 — focus beats diversification at this account size
MIN_SCORE_THRESHOLD = -12.0  # allow exit-only pairs with moderate negative score


def check_weak_pair(coin):
    """Check if a coin should be suspended based on recent trip performance.
    Escalating: each re-suspension doubles the timeout (15m -> 30m -> 1h -> 2h cap).
    After resume, requires WEAK_PAIR_LOOKBACK *new* trips before re-evaluating —
    fixes the strike-escalation deadlock where the same stale losing trips
    suspended the pair forever.
    Returns (is_suspended, reason_string)."""
    now = time.time()

    # Hard-disabled: stays out until operator re-enables
    if coin in weak_pair_disabled:
        strikes = weak_pair_strikes.get(coin, WEAK_PAIR_MAX_STRIKES)
        return True, f"DISABLED (strike {strikes} >= {WEAK_PAIR_MAX_STRIKES}) — manual re-enable required"

    # Check if currently suspended
    if coin in weak_pair_suspended:
        resume_at = weak_pair_suspended[coin]
        if now < resume_at:
            remaining = int(resume_at - now)
            strikes = weak_pair_strikes.get(coin, 1)
            return True, f"suspended {remaining}s (weak, strike {strikes})"
        else:
            # Resume: snapshot current trip count so we only judge NEW trips
            del weak_pair_suspended[coin]
            weak_pair_trip_mark[coin] = len(coin_trips.get(coin, []))
            tg_send(f"▶️ <b>RESUMED</b> {coin} (strike {weak_pair_strikes.get(coin, 0)}) — evaluating next {WEAK_PAIR_LOOKBACK} trips")

    trips = coin_trips.get(coin, [])
    mark = weak_pair_trip_mark.get(coin, 0)
    new_trips = trips[mark:]

    # Need at least LOOKBACK *new* trips (since resume / startup) to judge
    if len(new_trips) >= WEAK_PAIR_LOOKBACK:
        recent = new_trips[-WEAK_PAIR_LOOKBACK:]
        net_sum = sum(t["net"] for t in recent)
        winners = sum(1 for t in recent if t["net"] >= 0)
        avg_fees = sum(t.get("fees", 0) for t in recent) / len(recent)
        avg_gross = sum(abs(t.get("gross", 0)) for t in recent) / len(recent)
        fee_ratio = avg_fees / avg_gross if avg_gross > 0 else 999

        if net_sum < 0 and winners < WEAK_PAIR_LOOKBACK * 0.4:
            strikes = weak_pair_strikes.get(coin, 0) + 1
            weak_pair_strikes[coin] = strikes

            # Hard cap: after MAX_STRIKES, disable and require manual re-enable
            if strikes >= WEAK_PAIR_MAX_STRIKES:
                weak_pair_disabled.add(coin)
                tg_send(f"🚫 <b>PAIR DISABLED</b> {coin}: strike {strikes} (max {WEAK_PAIR_MAX_STRIKES})\nLast {WEAK_PAIR_LOOKBACK} trips: ${net_sum:.4f} WR:{winners}/{WEAK_PAIR_LOOKBACK}\nConsider rotating to a different symbol. Send /reenable {coin} to retry.")
                return True, f"DISABLED: strike {strikes} >= max {WEAK_PAIR_MAX_STRIKES}"

            suspend_secs = min(WEAK_PAIR_BASE_SUSPEND * (2 ** (strikes - 1)), WEAK_PAIR_MAX_SUSPEND)
            weak_pair_suspended[coin] = now + suspend_secs
            tg_send(f"⏸️ <b>WEAK PAIR</b> {coin}: strike {strikes}\nLast {WEAK_PAIR_LOOKBACK} trips: ${net_sum:.4f} WR:{winners}/{WEAK_PAIR_LOOKBACK}\nSuspended {suspend_secs//60}min")
            return True, f"SUSPENDED: strike {strikes} ({suspend_secs//60}min) | last {WEAK_PAIR_LOOKBACK} trips net ${net_sum:.4f}, WR {winners}/{WEAK_PAIR_LOOKBACK} FeeR:{fee_ratio:.2f}"

        # Redemption: 60%+ wins since resume — decay one strike, rearm the mark
        if winners >= WEAK_PAIR_LOOKBACK * 0.6 and coin in weak_pair_strikes:
            weak_pair_strikes[coin] = max(0, weak_pair_strikes[coin] - 1)
            weak_pair_trip_mark[coin] = len(trips)  # fresh window for next eval

    return False, ""


def rank_pair_score(coin, price_data, vol_bps, positions):
    """Score a pair for quoting quality. Higher = better setup.
    score = spread_bps - fee_penalty - vol_penalty - flow_penalty - crowd_penalty - inventory_penalty - hold_penalty
    Returns (score, breakdown_dict)."""
    mid = price_data["mid"]
    mkt_spread_bps = price_data["spread"] / mid * 10000 if mid > 0 else 0

    # Soft penalty for tight spreads (profitability gate handles the hard filter)
    # No hard -100 here — let exit-only mode work even on tight pairs

    # Fee penalty: 2x maker fee (both legs)
    fee_penalty = 2 * MAKER_FEE_BPS

    # Volatility penalty: high vol = more adverse selection risk
    vol_penalty = min(vol_bps * 0.4, 12.0)

    # One-sided flow penalty (stronger)
    flow_imb, buy_vol, sell_vol = get_flow_signal(coin)
    flow_penalty = abs(flow_imb) * 4.0  # strong flow = up to 4bps penalty

    # Queue crowding penalty
    bid_sz = price_data.get("bid_size", 0)
    ask_sz = price_data.get("ask_size", 0)
    avg_top = (bid_sz + ask_sz) / 2
    crowd_penalty = min(avg_top / CROWDED_SIZE * 1.5, 3.0) if CROWDED_SIZE > 0 else 0

    # Inventory penalty: holding this coin reduces attractiveness
    # Scales from 0 (no position) to 8bps (at max inventory)
    # Does NOT block trading — side-gating in phase 2 handles that
    pos = positions.get(coin, {})
    pos_usd = abs(pos.get("size", 0)) * mid
    # Quadratic inventory penalty: gentle at low sizes, steep near max
    inv_ratio = min(pos_usd / MAX_INVENTORY_USD, 1.0) if MAX_INVENTORY_USD > 0 else 0
    inv_penalty = inv_ratio * inv_ratio * 6.0  # 0 at empty, 1.5bps at 50%, 6.0bps at max

    # Hold-time penalty: if we have an open leg on this coin, penalize based on age
    open_leg = trip_tracker.get(coin)
    hold_penalty = 0.0
    if open_leg:
        hold_secs = time.time() - open_leg.get("time", time.time())
        hold_penalty = min(hold_secs / 60.0 * 1.0, 3.0)  # 1bps per minute held, cap 3

    score = mkt_spread_bps - fee_penalty - vol_penalty - flow_penalty - crowd_penalty - inv_penalty - hold_penalty

    # Note: MIN_TRIP_NET_USD check removed — at $2 order size, even 10bps score
    # only nets $0.002. The spread/fee filters already ensure edge is positive.

    breakdown = {
        "spread": round(mkt_spread_bps, 1),
        "fee": round(fee_penalty, 1),
        "vol": round(vol_penalty, 1),
        "flow": round(flow_penalty, 1),
        "crowd": round(crowd_penalty, 1),
        "inv": round(inv_penalty, 1),
        "hold": round(hold_penalty, 1),
        "score": round(score, 1),
    }
    return score, breakdown


def run_cycle(info, exchange, address):
    """Ranked selective market making cycle.

    Scores all pairs, only quotes the top MAX_QUOTE_PAIRS.
    Cancels orders on pairs that fall out of the top rank.
    """
    global active_orders, risk_cooldown_until, live_quotes, round_trips, strategy_pause_until, quote_attempts, quotes_skipped_profitability, quotes_placed, quotes_skipped_budget, quotes_skipped_risk, quotes_skipped_weak, last_profitability_diag, request_budget_paused, quoting_paused

    # Risk cooldown check
    if time.time() < risk_cooldown_until:
        remaining = int(risk_cooldown_until - time.time())
        print(f"  RISK COOLDOWN: {remaining}s remaining")
        write_status()
        return

    # Remote pause check
    if quoting_paused:
        print("  PAUSED (remote /pause). Send /resume to continue.")
        write_status()
        return

    # Use cached account state (refreshed every 30s in main loop)
    account_value = portfolio_value()
    if account_value <= 0:
        get_account_state(info, address)
        account_value = portfolio_value()
    # Use cached open orders (reconciled periodically in main loop, not every cycle)
    current_orders = _get_cached_orders(info, address)
    active_orders = []  # rebuild for dashboard
    positions = last_balances.get("positions", {})

    # === DELTA HEDGE CHECK ===
    check_and_hedge(info, exchange, address)

    # === PHASE 1: Score and rank all pairs ===
    pair_scores = []
    pair_data = {}  # cache price_data per pair for phase 2

    for pair in PAIRS:
        coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
        tick = TICK_SIZE.get(coin, 0.01)
        p_dec = PRICE_DECIMALS.get(coin, 2)

        # Check weak pair suspension first
        is_suspended, suspend_reason = check_weak_pair(coin)
        if is_suspended:
            # Check if we have inventory that needs unwinding
            pos = positions.get(coin, {})
            pos_size = pos.get("size", 0)
            if pos_size != 0:
                # Weak but holding inventory — force into exit-only mode, don't skip
                print(f"\n  {coin} | {suspend_reason} | INVENTORY ${abs(pos_size) * (get_ws_book(coin) or {}).get('mid', 0):.2f} — exit-only")
                # Will be handled as exit-only in phase 2 via exit_only_pairs
            else:
                print(f"\n  {coin} | {suspend_reason}")
                quotes_skipped_weak += 1
                # Cancel any resting orders on suspended pairs with no inventory
                coin_orders = current_orders.get(coin, [])
                for o in coin_orders:
                    try:
                        cancel_order(exchange, coin, o["oid"])
                    except: pass
                live_quotes.pop(coin, None)
                continue

        price_data = get_ws_book(coin)
        if not price_data:
            price_data = get_mid_price(info, coin)
        if not price_data:
            continue

        last_prices[pair] = price_data
        mid = price_data["mid"]
        vol_bps = get_volatility_bps(coin, mid)

        score, breakdown = rank_pair_score(coin, price_data, vol_bps, positions)
        pair_scores.append((pair, coin, score, breakdown))
        pair_data[pair] = {"price_data": price_data, "vol_bps": vol_bps}

        mkt_spread_bps = price_data["spread"] / mid * 10000
        spread_ticks = round(price_data["spread"] / tick) if tick > 0 else 0
        print(f"\n  {coin} | Mid: ${mid:.{p_dec}f} | Sprd: {mkt_spread_bps:.1f}bps ({spread_ticks}t) | Score: {score:+.1f} [s:{breakdown['spread']} -f:{breakdown['fee']} -v:{breakdown['vol']} -fl:{breakdown['flow']} -cr:{breakdown['crowd']} -in:{breakdown['inv']} -h:{breakdown['hold']}]", end="")

        # Show flow if present
        flow_imb, buy_vol, sell_vol = get_flow_signal(coin)
        if buy_vol + sell_vol > 0:
            print(f" | Flow:{flow_imb:+.2f}", end="")

        # Show per-coin trip stats if available
        ct = coin_trips.get(coin, [])
        if ct:
            recent = ct[-WEAK_PAIR_LOOKBACK:]
            net = sum(t["net"] for t in recent)
            wr = sum(1 for t in recent if t["net"] >= 0)
            print(f" | Trips:{len(ct)} WR:{wr}/{len(recent)} Net:${net:.4f}", end="")
        print()

    # Sort by score descending
    pair_scores.sort(key=lambda x: x[2], reverse=True)

    # Show ranking
    ranked_display = " > ".join(f"{ps[1]}({ps[2]:+.1f})" for ps in pair_scores)
    print(f"  RANK: {ranked_display} | Quoting top {MAX_QUOTE_PAIRS} (min score {MIN_SCORE_THRESHOLD})")

    # Determine which pairs to quote vs skip
    # Must be in top MAX_QUOTE_PAIRS AND score above threshold
    quote_pairs = set()
    skip_pairs = set()
    exit_only_pairs = set()  # pairs where only exit side is allowed
    for i, (pair, coin, score, _) in enumerate(pair_scores):
        has_inventory = coin in positions and positions[coin].get("size", 0) != 0
        is_weak = coin in weak_pair_suspended and time.time() < weak_pair_suspended[coin]
        if i < MAX_QUOTE_PAIRS and score >= MIN_SCORE_THRESHOLD and not is_weak:
            quote_pairs.add(pair)
        elif has_inventory:
            # Score too low, ranked out, or weak-suspended — but inventory must be unwound
            exit_only_pairs.add(pair)
            quote_pairs.add(pair)
            if is_weak:
                reason = "weak-suspended with inventory"
            elif score < MIN_SCORE_THRESHOLD:
                reason = f"score {score:+.1f} < {MIN_SCORE_THRESHOLD}"
            else:
                reason = f"ranked #{i+1} (max {MAX_QUOTE_PAIRS})"
            print(f"  {coin} | EXIT-ONLY: {reason}, allowing exit side")
        else:
            skip_pairs.add(pair)

    # Cancel orders on skipped pairs
    for pair in skip_pairs:
        coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
        coin_orders = current_orders.get(coin, [])
        if coin_orders:
            for o in coin_orders:
                try:
                    cancel_order(exchange, coin, o["oid"])
                except Exception:
                    pass
            print(f"  {coin} | RANKED OUT — cancelled {len(coin_orders)} orders")
            live_quotes.pop(coin, None)

    # === PHASE 2: Quote only the top-ranked pairs ===
    for pair in PAIRS:
        if pair not in quote_pairs:
            continue

        coin = COIN_MAP.get(pair, pair.replace("-PERP", ""))
        tick = TICK_SIZE.get(coin, 0.01)
        p_dec = PRICE_DECIMALS.get(coin, 2)
        s_dec = SIZE_DECIMALS.get(coin, 2)

        cached = pair_data.get(pair)
        if not cached:
            continue
        price_data = cached["price_data"]
        vol_bps = cached["vol_bps"]

        mid = price_data["mid"]
        microprice = price_data.get("microprice", mid)
        best_bid = price_data["best_bid"]
        best_ask = price_data["best_ask"]
        bid_top_size = price_data["bid_size"]
        ask_top_size = price_data["ask_size"]
        mkt_spread = price_data["spread"]
        mkt_spread_bps = mkt_spread / mid * 10000
        imbalance = orderbook_imbalance(price_data)

        # Calculate target spread (volatility-adaptive)
        vol_multiplier = 1.0 + min(vol_bps / 10.0, 2.0)
        spread_bps = MIN_SPREAD_BPS * vol_multiplier
        target_spread = mid * spread_bps / 10000

        # Fixed size from config — no dynamic scaling until edge is proven profitable
        dynamic_size_usd = ORDER_SIZE_USD
        # Split across levels, respect $10 minimum per order
        max_levels = max(1, int(dynamic_size_usd / 10.5))  # each level needs >$10
        num_levels = min(QUOTE_LEVELS, max_levels)
        level_size_usd = dynamic_size_usd / num_levels
        size = round(level_size_usd / mid, s_dec)
        if size * mid < 10.5:
            size = round(11.0 / mid, s_dec)  # buffer for step-back pricing
        equity_pct = (size * mid) / account_value * 100 if account_value > 0 else 0
        print(f"  SIZE: ${size * mid:.2f} ({size} {coin}) = {equity_pct:.0f}% of ${account_value:.2f} equity")

        # Check current position
        pos = positions.get(coin, {})

        # Small position exit: if position is under $10, market_close it directly
        # (can't place limit orders under HL's $10 minimum)
        coin_orders = current_orders.get(coin, [])
        pos_size_check = pos.get("size", 0)
        if pos_size_check != 0 and abs(pos_size_check) * mid < 10.0:
            print(f"  {coin} | SMALL POS EXIT: ${abs(pos_size_check) * mid:.2f} < $10 min, market closing")
            for o in coin_orders:
                try: cancel_order(exchange, coin, o["oid"])
                except: pass
            try:
                result = _api_call(exchange.market_close, coin)
                if result is None:
                    print(f"  {coin} market_close rate limited, retry next cycle")
                    continue
                # Verify closure
                time.sleep(0.5)
                get_account_state(info, address)
                new_pos = last_balances.get("positions", {}).get(coin, {}).get("size", 0)
                if new_pos == 0:
                    trip_tracker.pop(coin, None)
                    print(f"  {coin} small pos closed successfully")
                else:
                    print(f"  {coin} still open after market_close (size={new_pos}), will retry")
            except Exception as e:
                if "min order size" in str(e).lower() or "too small" in str(e).lower():
                    trip_tracker.pop(coin, None)
                    print(f"  {coin} position below min close size, clearing tracker")
                else:
                    print(f"  {coin} small pos market_close error: {e}")
            live_quotes.pop(coin, None)
            continue
        pos_size = pos.get("size", 0)
        pos_usd = abs(pos_size) * mid
        entry_price = pos.get("entry_price", 0)

        # Risk check
        risk_reason = risk_check(account_value, vol_bps, pos_usd)
        if risk_reason:
            print(f"  {coin} | RISK PAUSE: {risk_reason}")
            if time.time() > risk_cooldown_until:
                risk_cooldown_until = time.time() + COOLDOWN_SECS
                tg_send(f"⚠️ <b>RISK PAUSE</b> {coin}: {risk_reason}\nCooldown {COOLDOWN_SECS}s")
            quotes_skipped_risk += 1
            event_log.log_risk(coin=coin, reason=risk_reason, cooldown_secs=COOLDOWN_SECS)
            continue

        # --- Rate limit check (real sliding window: 600/min of HL's 1200/min) ---
        budget_left = request_budget_remaining()
        if budget_left < 50:  # within 50 of limit, pause quoting
            if not request_budget_paused:
                request_budget_paused = True
                print(f"  >>> RATE LIMIT: {budget_left}/{MAX_REQUESTS_PER_MIN} remaining in window. Pausing quotes.")
            quotes_skipped_budget += 1
            continue
        elif request_budget_paused:
            request_budget_paused = False
            print(f"  >>> RATE LIMIT OK: {budget_left}/{MAX_REQUESTS_PER_MIN} remaining. Resuming.")

        # Get existing orders for this coin
        coin_orders = current_orders.get(coin, [])

        # Trade flow signal
        flow_imb, buy_vol, sell_vol = get_flow_signal(coin)
        flow_total = buy_vol + sell_vol

        # === STOP-LOSS CHECK ===
        STOP_LOSS_BPS = 40  # tightened from 60: on $10 orders, 60bps = $0.06 loss, too much
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
                    try: cancel_order(exchange, coin, o["oid"])
                    except: pass
                try:
                    # Refresh position before closing to avoid stale reduce-only errors
                    get_account_state(info, address)
                    fresh_pos = last_balances.get("positions", {}).get(coin, {})
                    fresh_size = fresh_pos.get("size", 0)
                    if fresh_size != 0:
                        result = _api_call(exchange.market_close, coin)
                        if result:
                            print(f"  {coin} market close sent")
                        else:
                            print(f"  {coin} market close failed (429), will retry next cycle")
                    else:
                        print(f"  {coin} position already flat, skip market_close")
                except Exception as e:
                    print(f"  Market close error: {e}")
                live_quotes.pop(coin, None)
                continue

        # === FLOW-AWARE QUEUE QUALITY + MULTI-QUOTE MARKET MAKING ===

        inventory_usd = pos_size * mid
        inv_ratio = 0.0
        if MAX_INVENTORY_USD > 0:
            inv_ratio = max(-1.0, min(1.0, inventory_usd / MAX_INVENTORY_USD))

        # Entry/Exit split: exits always allowed, entries gated by score
        is_exit_only = pair in exit_only_pairs
        allow_buy = True
        allow_sell = True
        inv_extra_skew = 0.0

        # Per-coin inventory cap: block entry side when over limits
        max_inv_equity = account_value * 0.10  # 10% of equity per coin (tighter cap = faster rebalance)
        effective_inv_cap = min(MAX_INVENTORY_USD, max_inv_equity)
        if pos_usd > effective_inv_cap:
            if pos_size > 0:
                allow_buy = False  # LONG over cap: block more buying
            elif pos_size < 0:
                allow_sell = False  # SHORT over cap: block more selling

        # Total exposure check: cap total notional across all coins
        total_exposure = sum(abs(p.get("size", 0)) * last_prices.get(f"{c}-PERP", {}).get("mid", 0)
                            for c, p in positions.items())
        if total_exposure > MAX_POSITION_NOTIONAL:
            if pos_size > 0:
                allow_buy = False  # only allow sell to reduce
            elif pos_size < 0:
                allow_sell = False  # only allow buy to reduce
            elif total_exposure > MAX_POSITION_NOTIONAL * 1.2:
                print(f"  {coin} | EXPOSURE CAP: ${total_exposure:.2f} > ${MAX_POSITION_NOTIONAL}")
                continue

        # === MAX HOLD TIME: escalating exit urgency ===
        open_leg = trip_tracker.get(coin)
        # Seed trip_tracker for pre-existing positions (e.g. from before restart)
        if pos_size != 0 and open_leg is None:
            trip_tracker[coin] = {
                "side": "B" if pos_size > 0 else "A",
                "price": entry_price,
                "size": abs(pos_size),
                "fee": 0,
                "time": time.time(),  # start counting from NOW
            }
            open_leg = trip_tracker[coin]
            print(f"  {coin} | SEEDED trip_tracker for pre-existing {'LONG' if pos_size > 0 else 'SHORT'} {abs(pos_size)}")
        if open_leg and pos_size != 0:
            hold_secs = time.time() - open_leg.get("time", time.time())
            overweight_ratio = pos_usd / max(effective_inv_cap, 0.01)

            # Overweight force close: if inventory is 4x+ over cap, force close immediately
            if overweight_ratio > 4.0:
                print(f"  {coin} | OVERWEIGHT FORCE CLOSE: ${pos_usd:.2f} = {overweight_ratio:.1f}x cap")
                for o in coin_orders:
                    try: cancel_order(exchange, coin, o["oid"])
                    except: pass
                try:
                    book = get_ws_book(coin)
                    if book and pos_size != 0:
                        close_sz = abs(pos_size)
                        tick = TICK_SIZE.get(coin, 0.01)
                        if pos_size > 0:
                            close_px = round(book["best_bid"] - tick, PRICE_DECIMALS.get(coin, 2))
                            _api_call(exchange.order, coin, False, close_sz, close_px, {"limit": {"tif": "Gtc"}}, reduce_only=True)
                        else:
                            close_px = round(book["best_ask"] + tick, PRICE_DECIMALS.get(coin, 2))
                            _api_call(exchange.order, coin, True, close_sz, close_px, {"limit": {"tif": "Gtc"}}, reduce_only=True)
                        print(f"  {coin} limit close @ {close_px}")
                    else:
                        _api_call(exchange.market_close, coin)
                except Exception as e:
                    print(f"  {coin} overweight limit close error: {e}")
                    try: _api_call(exchange.market_close, coin)
                    except: pass
                live_quotes.pop(coin, None)
                continue

            if hold_secs > 300:  # 5min: force close (limit, not market) — was 10min, tightened
                # 5+ minutes: force close with LIMIT order (avoid taker fees)
                print(f"  {coin} | MAX HOLD: {hold_secs:.0f}s, force closing (limit)")
                for o in coin_orders:
                    try: cancel_order(exchange, coin, o["oid"])
                    except: pass
                try:
                    book = get_ws_book(coin)
                    if book and pos_size != 0:
                        close_sz = abs(pos_size)
                        tick = TICK_SIZE.get(coin, 0.01)
                        if pos_size > 0:
                            close_px = round(book["best_bid"] - tick, PRICE_DECIMALS.get(coin, 2))
                            _api_call(exchange.order, coin, False, close_sz, close_px, {"limit": {"tif": "Gtc"}}, reduce_only=True)
                        else:
                            close_px = round(book["best_ask"] + tick, PRICE_DECIMALS.get(coin, 2))
                            _api_call(exchange.order, coin, True, close_sz, close_px, {"limit": {"tif": "Gtc"}}, reduce_only=True)
                        print(f"  {coin} limit close @ {close_px}")
                    else:
                        _api_call(exchange.market_close, coin)
                except Exception as e:
                    print(f"  {coin} limit close error: {e}, fallback market_close")
                    try: _api_call(exchange.market_close, coin)
                    except: pass
                # DON'T pop trip_tracker — let market close fill complete the trip in check_fills
                live_quotes.pop(coin, None)
                continue
            elif hold_secs > 60 or overweight_ratio > 2.0:
                # 1+ minutes OR 2x+ over cap: max exit urgency (was 2min, tightened)
                inv_extra_skew = max(inv_extra_skew, 14.0)

        if is_exit_only:
            # Score below threshold — only allow the exit side
            if pos_size > 0:
                allow_buy = False   # long: only sell to exit
            elif pos_size < 0:
                allow_sell = False  # short: only buy to exit
            # Extra aggressive exit: tighter spread on exit side
            # Use max() to preserve max-hold urgency if already set
            exit_skew = abs(inv_ratio) * 4.0 if abs(inv_ratio) > 0.3 else 0
            inv_extra_skew = max(inv_extra_skew, exit_skew)
        else:
            # Full quoting mode — apply soft bias for inventory management
            # Use max() to preserve max-hold urgency if already set
            if abs(inv_ratio) > 0.50:
                inv_extra_skew = max(inv_extra_skew, abs(inv_ratio) * 8.0)
            if abs(inv_ratio) > 0.85:
                inv_extra_skew = max(inv_extra_skew, abs(inv_ratio) * 14.0)
                spread_bps += 4
            if abs(inv_ratio) > 1.1:
                spread_bps += 6

        # Flow-based adverse selection protection
        # If trade flow is heavily one-sided, stop quoting the side that gets picked off
        # BUT: never block the exit side when holding inventory
        if flow_total > 2.0:
            if flow_imb < -0.5:
                # Selling pressure: stop buying — UNLESS we're SHORT and need to buy to exit
                if pos_size >= 0:
                    allow_buy = False
            elif flow_imb > 0.5:
                # Buying pressure: stop selling — UNLESS we're LONG and need to sell to exit
                if pos_size <= 0:
                    allow_sell = False

        # Hard lockout: block side entirely after consecutive adverse fills
        lock = adverse_side_locked.get(coin)
        if lock:
            if time.time() < lock["until"]:
                remaining_lock = int(lock["until"] - time.time())
                locked_side = lock["side"]
                # Lockout exemption: if we're LONG and SELL is locked, allow sell to exit
                if locked_side == "B":
                    if pos_size < 0:
                        pass  # exempt: need to buy to exit short
                    else:
                        allow_buy = False
                        print(f"  {coin} | BUY LOCKED ({remaining_lock}s)")
                else:
                    if pos_size > 0:
                        pass  # exempt: need to sell to exit long
                    else:
                        allow_sell = False
                        print(f"  {coin} | SELL LOCKED ({remaining_lock}s)")
            else:
                adverse_side_locked.pop(coin, None)

        # Defensive spread widening when inventory is heavy
        if abs(inv_ratio) > 0.50:
            spread_bps += 2
        if abs(inv_ratio) > 0.75:
            spread_bps += 4
        target_spread = mid * spread_bps / 10000

        # Use microprice as fair value base (volume-weighted, more accurate than raw mid)
        fair_mid = microprice
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

        # Widen spread on order book imbalance (price move likely)
        if abs(imbalance) > 0.5:
            obi_widen = 3 if abs(imbalance) > 0.7 else 2
            spread_bps += obi_widen

        target_spread = fair_mid * spread_bps / 10000

        # Stronger inventory skew: push quotes harder toward flattening
        INVENTORY_SKEW_BPS = 12.0  # aggressive: push quotes away from inventory fast
        skew_bps = inv_ratio * INVENTORY_SKEW_BPS
        skew_px = mid * skew_bps / 10000.0

        quotes = live_quotes.get(coin, {})

        # --- STRATEGY PAUSE CHECK ---
        if time.time() < strategy_pause_until:
            remaining = int(strategy_pause_until - time.time())
            print(f"  STRATEGY PAUSE: {remaining}s (avg trip net negative)")
            # Cancel all orders while paused
            for o in coin_orders:
                try:
                    cancel_order(exchange, coin, o["oid"])
                except: pass
            live_quotes.pop(coin, None)
            continue

        # --- PROFITABILITY-GATED QUOTING (A/B mode) ---
        quote_attempts += 1
        spread_ticks = round(mkt_spread / tick) if tick > 0 else 1

        # Mode-dependent required spread
        safety_bps = SAFETY_BPS_STRICT if PROFITABILITY_MODE == "strict" else SAFETY_BPS_AGGRESSIVE
        required_bps = 2 * MAKER_FEE_BPS + safety_bps
        required_ticks = round((mid * required_bps / 10000) / tick) if tick > 0 else 999
        market_ticks = spread_ticks
        last_profitability_diag = {
            "market_spread_bps": round(mkt_spread_bps, 3),
            "required_bps": round(required_bps, 3),
            "market_ticks": market_ticks,
            "required_ticks": required_ticks,
            "expected_net": 0.0,
        }

        # Track inventory for mean/variance
        inventory_samples.append(inventory_usd)
        if len(inventory_samples) > 1000:
            inventory_samples.pop(0)

        if spread_ticks >= required_ticks:
            # ANTI-ADVERSE-SELECTION: quote BEHIND best bid/ask, not at it.
            # Joining best level = getting picked off by informed flow.
            # Stepping back = only fill when price already moved in our favor.
            bid_sz = price_data.get("bid_size", 0)
            ask_sz = price_data.get("ask_size", 0)

            if spread_ticks >= required_ticks * 4:
                # Very wide spread: safe to join (plenty of edge even if adverse)
                bid_step = 0
                ask_step = 0
            elif spread_ticks >= required_ticks * 2:
                # Wide spread: step back 1 tick from best (protect against 1-tick sweeps)
                bid_step = -1
                ask_step = -1
            else:
                # Tight/normal spread: step back 2 ticks (stronger adverse protection)
                # Only get filled if price moves 2+ ticks past best level
                bid_step = -2
                ask_step = -2

            buy_price = round(best_bid + bid_step * tick, p_dec)
            sell_price = round(best_ask - bid_step * tick, p_dec)  # negative step = wider
            buy_reason = f"back{bid_step}" if bid_step < 0 else "join bid"
            sell_reason = f"back{ask_step}" if ask_step < 0 else "join ask"
        elif pos_size != 0:
            # Spread too tight BUT we have inventory — allow exit side only
            # Step inside spread aggressively for the exit side
            quotes_skipped_profitability += 1
            if pos_size > 0:
                # LONG: need to sell to exit — join the ask (no penny, stay maker)
                allow_buy = False
                buy_price = best_bid  # placeholder, won't be used
                sell_price = round(best_ask, p_dec)  # join ask, stay maker
                buy_reason = "gated"
                sell_reason = "exit-penny"
                print(f"  TIGHT-SPREAD EXIT: {mkt_spread_bps:.1f}bps < {required_bps:.1f}bps, sell-only exit")
            else:
                # SHORT: need to buy to exit — join the bid (no penny, stay maker)
                allow_sell = False
                sell_price = best_ask  # placeholder, won't be used
                buy_price = round(best_bid, p_dec)  # join bid, stay maker
                buy_reason = "exit-penny"
                sell_reason = "gated"
                print(f"  TIGHT-SPREAD EXIT: {mkt_spread_bps:.1f}bps < {required_bps:.1f}bps, buy-only exit")
        else:
            # Spread too tight, no inventory — gate both sides
            expected_net_tight = mkt_spread * size - 2 * (size * mid * MAKER_FEE_BPS / 10000)
            last_profitability_diag["expected_net"] = round(expected_net_tight, 6)
            if coin_orders:
                for o in coin_orders:
                    try: cancel_order(exchange, coin, o["oid"])
                    except: pass
            quotes_skipped_profitability += 1
            print(f"  GATE [{PROFITABILITY_MODE}]: mkt={market_ticks}t req={required_ticks}t | {mkt_spread_bps:.1f}bps < {required_bps:.1f}bps | expNet=${expected_net_tight:.4f}")
            live_quotes.pop(coin, None)
            continue

        # Apply inventory skew ASYMMETRICALLY
        # LONG (inv_ratio > 0): push sell DOWN (aggressive exit), push buy DOWN (less entry)
        # SHORT (inv_ratio < 0): push buy UP (aggressive exit), push sell UP (less entry)
        # SKIP skew on tight-spread exit path — exit penny pricing is already aggressive
        is_tight_exit = (buy_reason == "exit-penny" or sell_reason == "exit-penny")
        if is_tight_exit:
            # Tight-spread exit: don't apply skew, keep aggressive penny pricing
            capped_extra = 0
        else:
            # Cap skew so it never exceeds half the current spread (prevents inversion)
            half_spread_bps = mkt_spread_bps / 2
            max_skew_bps = max(half_spread_bps - 1.0, 2.0)  # leave at least 1bps margin
            capped_extra = min(inv_extra_skew, max_skew_bps)
            entry_skew_bps = skew_bps + capped_extra * (1 if inv_ratio > 0 else -1)
            exit_skew_bps = -min(abs(capped_extra), max_skew_bps)  # push exit toward mid, capped
            entry_skew_px = mid * entry_skew_bps / 10000.0
            exit_skew_px = mid * exit_skew_bps / 10000.0
            if inv_ratio > 0:
                # LONG: buy is entry (push away), sell is exit (push toward mid = lower price)
                buy_price = round(buy_price - entry_skew_px, p_dec)
                sell_price = round(sell_price + exit_skew_px, p_dec)  # sell lower = closer to mid = faster exit
            elif inv_ratio < 0:
                # SHORT: sell is entry (push away), buy is exit (push toward mid = higher price)
                sell_price = round(sell_price + entry_skew_px, p_dec)
                buy_price = round(buy_price - exit_skew_px, p_dec)  # buy higher = closer to mid = faster exit
            else:
                # Flat: no inventory skew (skew_bps=0 when inv_ratio=0)
                pass

        # Apply flow shift
        if flow_shift_applied != 0:
            half_spread = target_spread / 2
            flow_floor = round(fair_mid - half_spread, p_dec)
            flow_ceil = round(fair_mid + half_spread, p_dec)
            if buy_price < flow_floor:
                buy_price = flow_floor
                buy_reason += f" +flow{flow_shift_applied:+.0f}bp"
            if sell_price > flow_ceil:
                sell_price = flow_ceil
                sell_reason += f" +flow{flow_shift_applied:+.0f}bp"

        # Safety: never cross the spread — fall back to joining, not penny-minus-tick
        if buy_price >= best_ask:
            buy_price = best_bid  # join best bid, don't try to penny
        if sell_price <= best_bid:
            sell_price = best_ask  # join best ask, don't try to penny
        # Final hard checks: bid must be strictly below best_ask, ask strictly above best_bid
        if buy_price >= best_ask:
            print(f"  SKIP BUY {coin}: bid ${buy_price} >= ask ${best_ask}")
            allow_buy = False
        if sell_price <= best_bid:
            print(f"  SKIP SELL {coin}: ask ${sell_price} <= bid ${best_bid}")
            allow_sell = False
        if sell_price <= buy_price:
            sell_price = round(buy_price + tick, p_dec)
            if sell_price <= best_bid:
                allow_sell = False

        # FINAL profitability check — but NEVER gate the exit side
        expected_net = (sell_price - buy_price) * size - 2 * (size * mid * MAKER_FEE_BPS / 10000)
        last_profitability_diag["expected_net"] = round(expected_net, 6)
        if expected_net <= 0:
            if pos_size == 0:
                # Flat: gate both sides, no inventory to unwind
                if coin_orders:
                    for o in coin_orders:
                        try: cancel_order(exchange, coin, o["oid"])
                        except: pass
                quotes_skipped_profitability += 1
                print(f"  GATE [{PROFITABILITY_MODE}]: expNet=${expected_net:.4f} <= 0 (flat, skip)")
                live_quotes.pop(coin, None)
                continue
            else:
                # Have inventory: gate entry side only, always allow exit
                quotes_skipped_profitability += 1
                if pos_size > 0:
                    allow_buy = False  # gate entry, allow sell exit
                    print(f"  GATE [{PROFITABILITY_MODE}]: expNet=${expected_net:.4f} <= 0, gating BUY only (LONG inv)")
                else:
                    allow_sell = False  # gate entry, allow buy exit
                    print(f"  GATE [{PROFITABILITY_MODE}]: expNet=${expected_net:.4f} <= 0, gating SELL only (SHORT inv)")

        quotes_placed += 1

        # Adverse selection: apply size reduction if triggered
        if coin in adverse_size_mult:
            if time.time() < adverse_pause_until.get(coin, 0):
                size = round(size * adverse_size_mult[coin], s_dec)
                if size * mid < 10.0:
                    size = round(10.5 / mid, s_dec)
            else:
                adverse_size_mult.pop(coin, None)
                adverse_pause_until.pop(coin, None)

        # Build desired price levels
        spacing = LEVEL_SPACING_TICKS * tick
        buy_levels = [round(buy_price - i * spacing, p_dec) for i in range(num_levels)]
        sell_levels = [round(sell_price + i * spacing, p_dec) for i in range(num_levels)]

        # Collect existing orders by side
        existing_buys = {}  # price -> oid
        existing_sells = {}
        for o in coin_orders:
            px = float(o.get("limitPx", 0))
            oid = o.get("oid")
            if o.get("side") == "B":
                existing_buys[round(px, p_dec)] = oid
            elif o.get("side") == "A":
                existing_sells[round(px, p_dec)] = oid

        # --- BUY LEVELS ---
        if allow_buy:
            keep_bps, replace_bps = get_drift_thresholds()
            # Tighten drift thresholds on EXIT side when inventory is heavy
            if inv_ratio < -0.5:  # SHORT: buy is the exit side
                keep_bps = min(keep_bps, 5)
                replace_bps = min(replace_bps, 10)
            if existing_buys:
                closest_buy = min(existing_buys.keys(), key=lambda p: abs(p - buy_levels[0]))
                top_buy_drift = abs(closest_buy - buy_levels[0]) / mid * 10000
                crossed = any(px >= best_ask for px in existing_buys)
                wrong_count = len(existing_buys) != num_levels

                if crossed or (top_buy_drift > replace_bps) or wrong_count:
                    # Must replace: crossed book, very stale, or wrong level count
                    for oid in existing_buys.values():
                        try:
                            cancel_order(exchange, coin, oid)
                        except: pass
                    placed = []
                    for lvl_px in buy_levels:
                        oid = place_order(exchange, coin, True, size, lvl_px)
                        if oid:
                            placed.append(lvl_px)
                    reason = "crossed" if crossed else f"stale {top_buy_drift:.0f}bps"
                    prices_str = " ".join(f"${p:.{p_dec}f}" for p in placed)
                    print(f"  BUY x{len(placed)}: {prices_str} (repriced: {reason})")
                    quotes["buy_levels"] = buy_levels
                elif top_buy_drift <= keep_bps:
                    # Queue preserved: order is competitive
                    prices_str = " ".join(f"${p:.{p_dec}f}" for p in sorted(existing_buys.keys(), reverse=True))
                    print(f"  BUY x{len(existing_buys)} resting: {prices_str} ({top_buy_drift:.0f}bps drift, queue kept)")
                else:
                    # Middle zone: drift between keep and replace — hold position
                    prices_str = " ".join(f"${p:.{p_dec}f}" for p in sorted(existing_buys.keys(), reverse=True))
                    print(f"  BUY x{len(existing_buys)} resting: {prices_str} ({top_buy_drift:.0f}bps drift, hold)")
            else:
                # No existing buys — place fresh
                placed = []
                for lvl_px in buy_levels:
                    oid = place_order(exchange, coin, True, size, lvl_px)
                    if oid:
                        placed.append(lvl_px)
                prices_str = " ".join(f"${p:.{p_dec}f}" for p in placed)
                print(f"  BUY x{len(placed)}: {prices_str} ({buy_reason})")
                quotes["buy_levels"] = buy_levels
        else:
            # Over inventory limit, cancel all buys
            if existing_buys:
                print(f"  Cancel {len(existing_buys)} BUYs (no buy: inv={inv_ratio:.0%})")
                for oid in existing_buys.values():
                    try: cancel_order(exchange, coin, oid)
                    except: pass

        # --- SELL LEVELS ---
        if allow_sell:
            keep_bps, replace_bps = get_drift_thresholds()
            # Tighten drift thresholds on EXIT side when inventory is heavy
            if inv_ratio > 0.5:  # LONG: sell is the exit side
                keep_bps = min(keep_bps, 5)
                replace_bps = min(replace_bps, 10)
            if existing_sells:
                closest_sell = min(existing_sells.keys(), key=lambda p: abs(p - sell_levels[0]))
                top_sell_drift = abs(closest_sell - sell_levels[0]) / mid * 10000
                crossed = any(px <= best_bid for px in existing_sells)
                wrong_count = len(existing_sells) != num_levels

                if crossed or (top_sell_drift > replace_bps) or wrong_count:
                    for oid in existing_sells.values():
                        try:
                            cancel_order(exchange, coin, oid)
                        except: pass
                    placed = []
                    for lvl_px in sell_levels:
                        oid = place_order(exchange, coin, False, size, lvl_px)
                        if oid:
                            placed.append(lvl_px)
                    reason = "crossed" if crossed else f"stale {top_sell_drift:.0f}bps"
                    prices_str = " ".join(f"${p:.{p_dec}f}" for p in placed)
                    print(f"  SELL x{len(placed)}: {prices_str} (repriced: {reason})")
                    quotes["sell_levels"] = sell_levels
                elif top_sell_drift <= keep_bps:
                    prices_str = " ".join(f"${p:.{p_dec}f}" for p in sorted(existing_sells.keys()))
                    print(f"  SELL x{len(existing_sells)} resting: {prices_str} ({top_sell_drift:.0f}bps drift, queue kept)")
                else:
                    prices_str = " ".join(f"${p:.{p_dec}f}" for p in sorted(existing_sells.keys()))
                    print(f"  SELL x{len(existing_sells)} resting: {prices_str} ({top_sell_drift:.0f}bps drift, hold)")
            else:
                placed = []
                for lvl_px in sell_levels:
                    oid = place_order(exchange, coin, False, size, lvl_px)
                    if oid:
                        placed.append(lvl_px)
                prices_str = " ".join(f"${p:.{p_dec}f}" for p in placed)
                print(f"  SELL x{len(placed)}: {prices_str} ({sell_reason})")
                quotes["sell_levels"] = sell_levels
        else:
            if existing_sells:
                print(f"  Cancel {len(existing_sells)} SELLs (no sell: inv={inv_ratio:.0%})")
                for oid in existing_sells.values():
                    try: cancel_order(exchange, coin, oid)
                    except: pass

        # Print spread capture diagnostic
        our_spread = sell_levels[0] - buy_levels[0] if buy_levels and sell_levels else 0
        our_spread_bps = our_spread / mid * 10000 if mid > 0 else 0
        expected_net = our_spread * size - 2 * (size * mid * MAKER_FEE_BPS / 10000)
        gate_str = ""
        if not allow_buy:
            gate_str += " [NO BUY]"
        if not allow_sell:
            gate_str += " [NO SELL]"
        skew_display = f"{skew_bps:+.1f}bps" + (f" +extra:{capped_extra:.1f}" if capped_extra > 0 else "")
        print(f"  Spread: ${our_spread:.{p_dec}f} ({our_spread_bps:.1f}bps) | Gate: mkt={market_ticks}t req={required_ticks}t ({required_bps:.1f}bps) | ExpNet/trip: ${expected_net:.4f} | Skew: {skew_display}{gate_str}")

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

    # Process post-fill edge checks (PFASR: delayed adverse selection detection)
    now_pf = time.time()
    remaining_checks = []
    for pfc in post_fill_checks:
        if now_pf >= pfc["check_at"]:
            pfb = get_ws_book(pfc["coin"])
            if pfb:
                pf_mid = pfb["mid"]
                if pfc["side"] == "B":
                    pf_edge = (pf_mid - pfc["price"]) / pf_mid * 10000
                else:
                    pf_edge = (pfc["price"] - pf_mid) / pf_mid * 10000
                marker = "OK" if pf_edge > 0 else "ADVERSE"
                print(f"  POST-FILL [{pfc['coin']}]: {pf_edge:+.1f}bps after 3s ({marker})")
        else:
            remaining_checks.append(pfc)
    post_fill_checks[:] = remaining_checks

    # Fills now processed via WebSocket in _process_ws_fills() (main loop)
    # REST check_fills() only used at shutdown for final reconciliation

    pv = portfolio_value()
    elapsed = time.time() - start_time
    print(f"\n{'='*55}")
    # Trip diagnostics
    trip_stats = ""
    if completed_trips:
        avg_gross = sum(t["gross"] for t in completed_trips) / len(completed_trips)
        avg_fees = sum(t["fees"] for t in completed_trips) / len(completed_trips)
        avg_net = sum(t["net"] for t in completed_trips) / len(completed_trips)
        total_gross = sum(t["gross"] for t in completed_trips)
        total_fees_trip = sum(t["fees"] for t in completed_trips)
        fee_ratio = total_fees_trip / total_gross if total_gross > 0 else 999
        winners = sum(1 for t in completed_trips if t["net"] >= 0)
        win_rate = winners / len(completed_trips) * 100
        trip_stats = f" | AvgNet: ${avg_net:.4f} WR: {win_rate:.0f}% FeeR: {fee_ratio:.2f}"
    # Turnover and realized spread
    total_vol = sum(f["cost"] for f in all_fills)
    hours = max(elapsed / 3600, 0.01)
    turnover = total_vol / pv if pv > 0 else 0
    turnover_hr = turnover / hours
    edge_stats = ""
    if fill_edges:
        avg_edge = sum(fill_edges) / len(fill_edges)
        pos_edges = sum(1 for e in fill_edges if e > 0)
        edge_stats = f" | Edge: {avg_edge:+.1f}bps ({pos_edges}/{len(fill_edges)} pos)"
    # Inventory mean/variance
    inv_mean = sum(inventory_samples) / len(inventory_samples) if inventory_samples else 0
    inv_var = sum((x - inv_mean)**2 for x in inventory_samples) / len(inventory_samples) if len(inventory_samples) > 1 else 0
    gate_pct = quotes_skipped_profitability / quote_attempts * 100 if quote_attempts > 0 else 0
    print(f"  Portfolio: ${pv:.2f} | Fills: {total_trade_count} | Trips: {round_trips}{trip_stats} | {elapsed/60:.1f}m")
    print(f"  Vol: ${total_vol:.0f} | Turnover: {turnover:.1f}x ({turnover_hr:.1f}x/hr){edge_stats}")
    skip_detail = f"prof:{quotes_skipped_profitability} budg:{quotes_skipped_budget} risk:{quotes_skipped_risk} weak:{quotes_skipped_weak}"
    print(f"  Gate [{PROFITABILITY_MODE}]: {quotes_placed}/{quote_attempts} placed | Skips: {skip_detail} | InvMean: ${inv_mean:.1f} InvVar: {inv_var:.0f}")
    # Delta summary
    _net_delta = 0.0
    for _c, _p in last_balances.get("positions", {}).items():
        _sz = _p.get("size", 0)
        _mid = 0
        for _pair in PAIRS:
            if _c in _pair and _pair in last_prices:
                _mid = last_prices[_pair].get("mid", 0)
                break
        _net_delta += _sz * _mid
    _delta_pct = abs(_net_delta) / pv * 100 if pv > 0 else 0
    _delta_dir = "LONG" if _net_delta > 0 else "SHORT" if _net_delta < 0 else "FLAT"
    print(f"  Delta: ${_net_delta:.2f} ({_delta_dir} {_delta_pct:.1f}%) | Hedge threshold: {HEDGE_DELTA_THRESHOLD_PCT}%")
    print(f"  RateLimit: {request_budget_remaining()}/{MAX_REQUESTS_PER_MIN} in window (lifetime:{request_count})")
    print(f"{'='*55}")
    write_status()


def _process_ws_fills(info, exchange, address):
    """Process fills from WebSocket queue instead of REST API.
    Replaces the REST check_fills() call in the main loop."""
    global total_trade_count, round_trips, strategy_pause_until

    global _cached_orders_time

    with ws_fills_lock:
        pending = list(ws_fills_pending)
        ws_fills_pending.clear()

    if not pending:
        return

    # Invalidate cached orders — fills mean order state changed
    _cached_orders_time = 0

    # Filter out startup replay fills (fills older than when WS subscribed)
    if ws_subscribe_time > 0:
        filtered = []
        for pf in pending:
            fill_time_ms = int(pf.get("time", 0))
            fill_time_s = fill_time_ms / 1000 if fill_time_ms > 1e12 else fill_time_ms
            if fill_time_s >= ws_subscribe_time - 5:  # 5s grace
                filtered.append(pf)
            else:
                coin = pf.get("coin", "?")
                print(f"  >>> SKIP REPLAY: {coin} fill from before subscribe time")
        if len(filtered) < len(pending):
            print(f"  >>> Filtered {len(pending) - len(filtered)} replay fills, processing {len(filtered)} new")
        pending = filtered
    if not pending:
        return

    for f in pending:
        coin = f.get("coin", "")
        side = f.get("side", "").upper()  # "A" (sell) or "B" (buy)
        price = float(f.get("px", 0))
        size = float(f.get("sz", 0))
        fee = float(f.get("fee", 0))
        cost = price * size
        closed_pnl = float(f.get("closedPnl", 0))

        # Calculate fill edge vs mid price
        fill_book = get_ws_book(coin)
        fill_mid = fill_book.get("mid", price) if fill_book else price
        if side == "B":
            edge = fill_mid - price
        else:
            edge = price - fill_mid
        edge_bps = edge / fill_mid * 10000 if fill_mid > 0 else 0

        fill_edges.append(edge_bps)
        if len(fill_edges) > 2000:
            fill_edges[:] = fill_edges[-1000:]
        post_fill_checks.append({"coin": coin, "side": side, "price": price, "check_at": time.time() + 3.0})

        # Adverse selection detector (per-coin)
        now_fill = time.time()
        coin_consec = consecutive_side_per_coin.get(coin)
        if coin_consec and coin_consec["side"] == side:
            coin_consec["count"] += 1
        else:
            if coin_consec and coin_consec["side"] != side:
                if coin in adverse_side_locked:
                    print(f"  >>> LOCKOUT CLEARED: {coin} opposite fill")
                    adverse_side_locked.pop(coin, None)
                    adverse_pause_until.pop(coin, None)
                    adverse_size_mult.pop(coin, None)
            consecutive_side_per_coin[coin] = {"side": side, "count": 1, "first_time": now_fill}
            coin_consec = consecutive_side_per_coin[coin]
        consec = coin_consec["count"]
        consec_warn = ""

        if consec >= 5:
            consec_warn = f" ⚠️{consec}x{side} LOCKED"
            adverse_pause_until[coin] = now_fill + 60.0
            adverse_size_mult[coin] = 0.5
            adverse_side_locked[coin] = {"side": side, "until": now_fill + 120.0}
            # Cancel ALL resting orders for this coin
            try:
                _ords = _api_call(info.open_orders, address) or []
                _cancelled = 0
                for _o in _ords:
                    if _o.get('coin') == coin:
                        try:
                            cancel_order(exchange, coin, _o['oid'])
                            _cancelled += 1
                        except: pass
                if _cancelled:
                    print(f'  >>> LOCKOUT: cancelled ALL {_cancelled} {coin} resting orders')
                live_quotes.pop(coin, None)
            except Exception:
                pass
            tg_send(f"🔒 <b>SIDE LOCKED</b> {coin}: {consec}x{side} - blocking for 120s")
        elif consec >= 3:
            consec_warn = f" ⚠️{consec}x{side} PAUSED"
            adverse_pause_until[coin] = now_fill + 30.0
            adverse_size_mult[coin] = 0.5
            try:
                _ords = _api_call(info.open_orders, address) or []
                for _o in _ords:
                    if _o.get('coin') == coin and _o.get('side') == side:
                        try:
                            cancel_order(exchange, coin, _o['oid'])
                        except: pass
            except: pass

        print(f"  >>> FILL: {side} {size} {coin} @ ${price:.2f} fee=${fee:.4f} pnl=${closed_pnl:.4f} edge={edge_bps:+.1f}bps{consec_warn}")
        emoji = "🟢" if side == "B" else "🔴"
        rebate_str = f"Rebate: +${-fee:.4f}" if fee < 0 else f"Fee: ${fee:.4f}"
        tg_msg = f"{emoji} <b>FILL</b>: {side} {size} {coin}\n💰 @ ${price:.2f} | {rebate_str} | Edge: {edge_bps:+.1f}bps"
        if consec >= 5:
            tg_msg += f"\n🔒 LOCKED: {consec}x{side} - side blocked 120s"
        elif consec >= 3:
            tg_msg += f"\n⚠️ ADVERSE: {consec}x{side} - paused 30s"
        tg_send(tg_msg)

        # Strategy pause
        if len(completed_trips) >= 20:
            recent_20 = completed_trips[-20:]
            avg_recent = sum(t["net"] for t in recent_20) / 20
            if avg_recent < 0 and now_fill > strategy_pause_until:
                strategy_pause_until = now_fill + 60
                print(f"  >>> STRATEGY PAUSE: last 20 trips avg net ${avg_recent:.4f} < 0, pausing 60s")
                tg_send(f"⏸️ <b>STRATEGY PAUSE</b>\nLast 20 trips avg: ${avg_recent:.4f}\nPausing new quotes 60s")

        # Round trip tracking
        leg = trip_tracker.get(coin)
        if leg is None:
            trip_tracker[coin] = {"side": side, "price": price, "size": size, "fee": fee, "time": time.time()}
        elif leg["side"] != side:
            trip_size = min(leg["size"], size)
            total_fee = leg["fee"] + fee
            if leg["side"] == "B":
                gross = (price - leg["price"]) * trip_size
            else:
                gross = (leg["price"] - price) * trip_size
            net = gross - total_fee
            duration = time.time() - leg["time"]

            trip = {
                "coin": coin, "size": trip_size,
                "buy_px": leg["price"] if leg["side"] == "B" else price,
                "sell_px": price if leg["side"] == "B" else leg["price"],
                "gross": round(gross, 6), "fees": round(total_fee, 6),
                "net": round(net, 6), "duration": round(duration, 1),
            }
            completed_trips.append(trip)
            if len(completed_trips) > 2000:
                completed_trips[:] = completed_trips[-1000:]
            event_log.log_trip(trip_num=round_trips, coin=coin, net_pnl=net)
            if coin not in coin_trips:
                coin_trips[coin] = []
            coin_trips[coin].append(trip)
            if len(coin_trips[coin]) > 500:
                coin_trips[coin] = coin_trips[coin][-250:]
            round_trips += 1

            net_sign = "+" if net >= 0 else ""
            print(f"  >>> TRIP #{round_trips}: {trip['buy_px']:.2f}->{trip['sell_px']:.2f} | Gross: ${gross:.4f} | Fees: ${total_fee:.4f} | Net: {net_sign}${net:.4f} | {duration:.0f}s")
            tg_send(f"{'✅' if net >= 0 else '❌'} <b>Trip #{round_trips}</b> {coin}\nBuy ${trip['buy_px']:.2f} -> Sell ${trip['sell_px']:.2f}\nGross: ${gross:.4f} | Fees: ${total_fee:.4f}\n<b>Net: {net_sign}${net:.4f}</b> | {duration:.0f}s")

            # Reset strategy pause escalation after 5 consecutive profitable trips
            if len(completed_trips) >= 5:
                last_5 = completed_trips[-5:]
                if all(t["net"] >= 0 for t in last_5):
                    old_count = getattr(sys.modules[__name__], '_strategy_pause_count', 0)
                    if old_count > 0:
                        sys.modules[__name__]._strategy_pause_count = 0
                        print(f"  >>> STRATEGY PAUSE RESET: 5 consecutive winners, clearing {old_count} strikes")

            remaining = size - trip_size
            if remaining > 0.001:
                trip_tracker[coin] = {"side": side, "price": price, "size": remaining, "fee": fee * remaining / size, "time": time.time()}
            else:
                trip_tracker.pop(coin, None)
        else:
            total_size = leg["size"] + size
            trip_tracker[coin] = {"side": side, "price": leg["price"], "size": total_size, "fee": leg["fee"] + fee, "time": leg["time"]}

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
        if len(all_fills) > 2000:
            all_fills[:] = all_fills[-1000:]
        total_trade_count += 1
        event_log.log_fill(coin=coin, side=side.lower(), size=size, price=price, fee=fee, closed_pnl=closed_pnl)
        event_log.log_custom("fill_context", coin=coin, edge_bps=round(edge_bps, 2), mid=round(fill_mid, 6), build_id=BUILD_ID)
        track_volume(cost)

        for p in PAIRS:
            if coin in p:
                pair_fills[p].append(fill)
                pair_trade_count[p] += 1
                break


def close_orphan_positions(info, exchange, address):
    """Detect and close positions on coins NOT in PAIRS config.
    Runs on startup and periodically. Uses limit orders (maker) to avoid taker fees."""
    managed_coins = set(COIN_MAP.values())
    state = _api_call(info.user_state, address)
    if not state:
        return 0

    closed = 0
    for pos_entry in state.get("assetPositions", []):
        p = pos_entry.get("position", {})
        coin = p.get("coin", "")
        size = float(p.get("szi", 0))
        if size == 0 or coin in managed_coins:
            continue

        # Orphan position found — close it
        entry_px = float(p.get("entryPx", 0))
        upnl = float(p.get("unrealizedPnl", 0))
        print(f"  ORPHAN: {coin} size={size} entry=${entry_px} uPnL=${upnl:.4f} — closing")
        tg_send(f"🧹 <b>ORPHAN CLOSE</b>: {coin}\nSize: {size} | Entry: ${entry_px} | uPnL: ${upnl:.4f}")

        # Cancel any resting orders on this coin first
        try:
            open_ords = _api_call(info.open_orders, address) or []
            for o in open_ords:
                if o.get("coin") == coin:
                    try:
                        cancel_order(exchange, coin, o["oid"])
                    except Exception:
                        pass
        except Exception:
            pass

        # Close via market_close (uses IOC, fastest exit)
        try:
            result = _api_call(exchange.market_close, coin)
            if result:
                print(f"  ORPHAN CLOSED: {coin}")
                closed += 1
            else:
                print(f"  ORPHAN CLOSE FAILED: {coin} (429 or error), will retry next check")
        except Exception as e:
            print(f"  ORPHAN CLOSE ERROR: {coin}: {e}")

    if closed:
        print(f"  Closed {closed} orphan position(s)")
        tg_send(f"🧹 Closed {closed} orphan position(s)")
    return closed


def main():
    print("=" * 55)
    print("  Hyperliquid Market Maker (Event-Driven)")
    print(f"  Pairs: {PAIRS}")
    print(f"  Size: ${ORDER_SIZE_USD}/side | Quote interval: {MIN_QUOTE_INTERVAL_FAST}-{MIN_QUOTE_INTERVAL_SLOW}s (adaptive)")
    safety = SAFETY_BPS_STRICT if PROFITABILITY_MODE == "strict" else SAFETY_BPS_AGGRESSIVE
    req = 2 * MAKER_FEE_BPS + safety
    print(f"  Mode: {PROFITABILITY_MODE} | Required: {req:.1f}bps | Maker fee: {MAKER_FEE_BPS}bps")
    print("=" * 55)

    info, exchange, address = setup_exchange()

    # Expose info/exchange at module level for Telegram commands
    import sys
    sys.modules[__name__].info = info
    sys.modules[__name__].exchange = exchange

    # Close orphan positions before anything else
    print("  Checking for orphan positions...")
    close_orphan_positions(info, exchange, address)
    tg_cmd = TelegramCommander(TG_TOKEN, TG_CHAT_ID, sys.modules[__name__])
    tg_cmd.start()

    tg_send(f"🚀 <b>Hyperliquid Bot Started</b>\n{'TESTNET' if USE_TESTNET else 'MAINNET'}\nPairs: {', '.join(PAIRS)}\nSize: ${ORDER_SIZE_USD}/side | Spread: {MIN_SPREAD_BPS}bps | Event-driven")
    event_log.log_start(exchange="hyperliquid", pairs=PAIRS, order_size_usd=ORDER_SIZE_USD, mode=PROFITABILITY_MODE, build_id=BUILD_ID)

    cycle_count = 0
    last_status_tg = time.time()
    last_account_refresh = 0
    last_orphan_check = time.time()  # periodic orphan sweep
    last_orders_reconcile = 0  # periodic REST open_orders reconciliation
    ORDERS_RECONCILE_SECS = 60  # only fetch open_orders from REST every 60s
    ORPHAN_CHECK_SECS = 300  # check for orphan positions every 5 minutes
    FILL_DEBOUNCE_SECS = 2.0  # batch fills for 2s before reacting
    last_fill_cycle = 0  # timestamp of last fill-triggered cycle

    while running:
        try:
            # Wait for fills or 30s periodic refresh
            triggered = fill_received.wait(timeout=30.0)
            if not running:
                break
            fill_received.clear()
            now = time.time()

            # Fill debounce: if woken by fill, wait 2s to batch rapid fills
            if triggered and now - last_fill_cycle < FILL_DEBOUNCE_SECS:
                continue  # skip this cycle, let fills accumulate
            if triggered:
                last_fill_cycle = now

            # Process WS fills (replaces redundant REST user_fills)
            _process_ws_fills(info, exchange, address)

            # Refresh account state periodically (every 30s)
            if now - last_account_refresh > 30:
                get_account_state(info, address)
                last_account_refresh = now

            # Periodic orphan position cleanup (every 5 min)
            if now - last_orphan_check > ORPHAN_CHECK_SECS:
                close_orphan_positions(info, exchange, address)
                last_orphan_check = now

            run_cycle(info, exchange, address)
            cycle_count += 1

            # Stall watchdog: alert when EVERY configured pair is sidelined
            # (suspended or disabled) — silent paralysis is the #1 failure mode.
            global _all_suspended_since, _last_stall_alert
            try:
                active_coins = [COIN_MAP.get(p, p.replace("-PERP", "")) for p in PAIRS]
                sidelined = [c for c in active_coins
                             if c in weak_pair_disabled
                             or (c in weak_pair_suspended and now < weak_pair_suspended[c])]
                if active_coins and len(sidelined) == len(active_coins):
                    if _all_suspended_since is None:
                        _all_suspended_since = now
                    stall_secs = now - _all_suspended_since
                    if stall_secs >= WEAK_PAIR_STALL_ALERT_SECS and now - _last_stall_alert > 1800:
                        tg_send(f"🛑 <b>ALL PAIRS SIDELINED</b> for {int(stall_secs/60)}min\nPairs: {', '.join(sidelined)}\nBot is not quoting. Review pair selection or /reenable.")
                        _last_stall_alert = now
                else:
                    _all_suspended_since = None
            except Exception:
                pass

            # Telegram status every ~2 min
            if now - last_status_tg > 120:
                pv = portfolio_value()
                pnl = pv - initial_portfolio_value if initial_portfolio_value else 0
                trip_msg = ""
                if completed_trips:
                    avg_net = sum(t["net"] for t in completed_trips) / len(completed_trips)
                    total_trip_pnl = sum(t["net"] for t in completed_trips)
                    winners = sum(1 for t in completed_trips if t["net"] >= 0)
                    total_gross = sum(t["gross"] for t in completed_trips)
                    total_fees_t = sum(t["fees"] for t in completed_trips)
                    fee_ratio = total_fees_t / total_gross if total_gross > 0 else 999
                    trip_msg = f"\n📈 Trips: {round_trips} | Net: ${total_trip_pnl:.4f}\nAvg: ${avg_net:.4f} | WR: {winners}/{round_trips} | FeeR: {fee_ratio:.2f}"
                tg_send(f"📊 <b>HL Status</b>\nPortfolio: ${pv:.2f}\nPnL: {'+'if pnl>=0 else ''}{pnl:.4f}\nFills: {total_trade_count} | Uptime: {(now-start_time)/60:.0f}m{trip_msg}")
                last_status_tg = now

        except Exception as e:
            print(f"Error: {e}")
            tg_send(f"⚠️ <b>Error</b>: {e}")
            event_log.log_error(str(e))
            import traceback
            traceback.print_exc()
            time.sleep(2)  # back off on error

    tg_cmd.stop()
    cancel_all_orders(exchange, info, address)
    # Process any remaining WS fills (don't use REST check_fills to avoid double-counting)
    _process_ws_fills(info, exchange, address)
    write_status()
    pv = portfolio_value()
    print(f"\nFinal Portfolio: ${pv:.2f} | Fills: {total_trade_count}")
    event_log.log_stop(portfolio=pv, fills=total_trade_count)
    tg_send(f"🛑 <b>HL Bot Stopped</b>\nPortfolio: ${pv:.2f} | Fills: {total_trade_count}")


if __name__ == "__main__":
    main()
