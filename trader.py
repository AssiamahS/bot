#!/usr/bin/env python3
# perf-stat test line
"""
Hyperliquid Market Maker.
Maker fee is 1.5bps at low volume tiers (rebate only at 25M+ monthly).
Must capture >4bps spread per round trip to profit after fees on both legs.
Writes status to trader_status.json for dashboard.
"""
import time
import json
import math
import signal
import os
import sys
import threading
import socket
socket.setdefaulttimeout(5)  # prevent hanging on slow API calls
import urllib.request
import urllib.parse
from collections import deque

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from tg_commander import TelegramCommander

# === SINGLETON LOCK: prevent multiple instances ===
import fcntl
_lock_file = open("/tmp/trader.lock", "w")
try:
    fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()
except IOError:
    print("ERROR: Another trader instance is already running. Exiting.")
    sys.exit(1)

def _api_call(fn, *args, **kwargs):
    """Wrap REST API calls with retry on 429."""
    for attempt in range(3):
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as e:
            if '429' in str(e):
                wait = (attempt + 1) * 2
                print(f"  429 rate limit, backing off {wait}s...")
                time.sleep(wait)
            else:
                raise
    return None

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_status.json")

# Load config
with open(CONFIG_FILE) as f:
    config = json.load(f)

# Load secrets from .env if config values are "ENV"
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_env_vars = {}
if os.path.exists(_env_path):
    with open(_env_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _env_vars[_k.strip()] = _v.strip()

def _resolve(config_key, env_key):
    val = config.get(config_key, "")
    if val == "ENV":
        return _env_vars.get(env_key, "")
    return val

# Telegram alerts
TG_TOKEN = _resolve("tg_token", "TG_TOKEN")
TG_CHAT_ID = _resolve("tg_chat_id", "TG_CHAT_ID")

def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data=data, timeout=5)
    except Exception:
        pass

PRIVATE_KEY = _resolve("wallet_private_key", "WALLET_PRIVATE_KEY")
WALLET_ADDRESS = config.get("wallet_address", "")
USE_TESTNET = config.get("use_testnet", True)
PAIRS = config.get("pairs", ["BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP", "PURR-PERP", "DYDX-PERP", "IOTA-PERP"])
ORDER_SIZE_USD = config.get("order_size_usd", 10.0)
REFRESH_SECS = config.get("refresh_secs", 15)
MIN_SPREAD_BPS = config.get("min_spread_bps", 15)  # hard floor: no quoting below 15bps market spread  # raised: 10bps minimum displayed spread
PROFITABILITY_MODE = config.get("profitability_mode", "strict").lower()
SAFETY_BPS_STRICT = config.get("safety_bps_strict", 9.0)  # 2*1.5 + 9.0 = 12.0bps required; pro MM minimum edge
SAFETY_BPS_AGGRESSIVE = config.get("safety_bps_aggressive", 0.5)
MIN_TOP_BOOK_DEPTH_USD = 300  # skip markets with thin top-3 book depth

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
MAX_INVENTORY_USD = 20.0  # static fallback; dynamically capped to 10% of equity
MAX_POSITION_NOTIONAL = 40.0  # ~30% of equity; room for 2 active positions without triggering reduce-only
MAX_VOLATILITY_BPS = 40  # pause quoting during volatile moves
COOLDOWN_SECS = 30
risk_cooldown_until = 0

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
adverse_side_locked = {}  # coin -> {"side": "B"/"A", "until": timestamp}
invalid_book_until = {}   # coin -> timestamp, cooldown after zero-book detected - hard lockout after 5+ consecutive fills
# Gating counters
quote_attempts = 0
quotes_skipped_profitability = 0
quotes_placed = 0
# Inventory tracking for mean/variance
inventory_samples = []  # list of inventory_usd values over time
last_profitability_diag = {"market_spread_bps": 0.0, "required_bps": 0.0, "market_ticks": 0, "required_ticks": 0, "expected_net": 0.0}
# --- Request budget protection ---
REQUEST_BUDGET_BUFFER = 200   # pause quoting when within 200 requests of estimated limit
request_count = 0             # total API requests sent this session
volume_traded_usd = 0.0       # total volume traded this session
request_budget_paused = False

def track_request():
    global request_count
    request_count += 1

def track_volume(usd_amount):
    global volume_traded_usd
    volume_traded_usd += abs(usd_amount)

def request_budget_remaining():
    # HL allows ~2 requests per $1 traded, plus base allowance
    allowed = 1000 + volume_traded_usd * 2.0
    return int(allowed - request_count)

# Queue-preserving quoting: two-tier drift thresholds
QUEUE_KEEP_BPS = 3    # tight: stale quotes = adverse fills
REPLACE_BPS = 5       # reprice quickly to stay competitive
# Dynamic: widen tolerance when request budget is low
def get_drift_thresholds():
    remaining = request_budget_remaining()
    if remaining < 500:
        return 5, 10  # conservative when budget low
    elif remaining < 1000:
        return 4, 8   # moderate budget
    return QUEUE_KEEP_BPS, REPLACE_BPS  # normal
MAKER_FEE_BPS = 1.5  # Hyperliquid maker fee at our volume tier
MIN_EDGE_BPS = 2.0   # lower threshold to increase fill rate
MIN_PROFIT_BPS = 3.0  # raised: minimum profit per round trip after fees
QUOTE_LEVELS = 1  # single level until equity > $300 to prevent inventory drift
LEVEL_SPACING_TICKS = 3  # ticks between each level

# Weak pair suspension: if last N trips on a coin are net negative, suspend it
WEAK_PAIR_LOOKBACK = 10  # check last 10 trips per coin
WEAK_PAIR_SUSPEND_SECS = 900  # suspend for 15 minutes
weak_pair_suspended = {}  # coin -> resume_timestamp
# Per-coin trip history for weak pair detection
coin_trips = {}  # coin -> list of completed trip dicts

# Queue quality thresholds
CROWDED_SIZE = 30  # units at top level = crowded queue
TICK_SIZE = {}  # populated dynamically by fetch_asset_metadata()

# WebSocket live book data (updated by WS callbacks)
ws_book = {}  # coin -> {"bids": [...], "asks": [...], "ts": time}
ws_book_lock = threading.Lock()
ws_fills_pending = []  # new fills from WS
seen_fill_ids = set()  # dedup fill processing by unique ID
ws_fills_lock = threading.Lock()
# Event-driven: signal when book changes materially
book_changed = threading.Event()
last_quote_time = {}  # coin -> timestamp of last quote update
# Adaptive quote interval: fast when budget healthy, slow when tight
MIN_QUOTE_INTERVAL_FAST = 2.0   # fast refresh for higher fill rate
MIN_QUOTE_INTERVAL_NORMAL = 4.0 # moderate budget
MIN_QUOTE_INTERVAL_SLOW = 6.0   # low budget

def get_quote_interval():
    """Adaptive: budget + spread-aware refresh speed."""
    remaining = request_budget_remaining()
    if remaining < 500:
        return MIN_QUOTE_INTERVAL_SLOW
    elif remaining < 1000:
        return MIN_QUOTE_INTERVAL_NORMAL
    # Tight spreads need faster refresh
    for pair in PAIRS:
        pd = last_prices.get(pair, {})
        m = pd.get("mid", 0)
        s = pd.get("spread", 0)
        if m > 0 and s > 0:
            if s / m * 10000 < 20:
                return MIN_QUOTE_INTERVAL_FAST
    return MIN_QUOTE_INTERVAL_NORMAL

# Trade flow tracking (updated by WS trades callback)
recent_trades = {}  # coin -> deque of {"ts", "px", "sz", "side"}
trades_lock = threading.Lock()
FLOW_WINDOW = 10  # seconds to look back for flow signal (wider window = smoother signal)
FLOW_SHIFT_BPS = 3  # max fair value shift from flow (conservative)
FLOW_WIDEN_BPS = 4  # extra spread during extreme one-way flow


def signal_handler(sig, frame):
    global running
    print("\nShutting down...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# === DYNAMIC PAIR SCANNER ===
PAIR_SCAN_INTERVAL = 20  # re-scan every N cycles
MIN_PAIRS = 2
MAX_PAIRS = 5
# Whitelist: proven Hyperliquid markets with real depth and volume
# Excludes micro-caps with fake spreads (HMSTR, MEME, NOT, BOME etc)
SCANNER_WHITELIST = [
    "DYDX", "PURR", "WIF", "HYPE", "TIA", "SEI", "STRK",
    "JUP", "ONDO", "PENDLE", "ARB", "OP", "INJ",
    "SUI", "AVAX", "NEAR", "FTM", "ATOM", "DOT",
]
_last_pair_scan = 0
_pair_scan_cycle = 0

def discover_markets(info):
    """Get whitelisted perp markets from exchange metadata."""
    try:
        meta = info.meta()
        universe = meta.get("universe", [])
        available = {a["name"] for a in universe}
        # Only return coins that exist on the exchange AND are in our whitelist
        return [c for c in SCANNER_WHITELIST if c in available]
    except Exception as e:
        print(f"  SCANNER: discover error: {e}")
        return []

def market_quality(info, coin, min_spread_bps=15):
    """Score a market for MM suitability. Higher = better.
    Returns (score, spread_bps, depth_usd) or None if unsuitable."""
    try:
        book = info.l2_snapshot(coin)
        if not book or not book["levels"][0] or not book["levels"][1]:
            return None
        bids = book["levels"][0]
        asks = book["levels"][1]
        best_bid = float(bids[0]["px"])
        best_ask = float(asks[0]["px"])
        mid = (best_bid + best_ask) / 2
        if mid <= 0 or best_bid <= 0 or best_ask <= 0:
            return None  # book not initialized
        spread = best_ask - best_bid
        spread_bps = spread / mid * 10000
        if spread_bps < min_spread_bps:
            return None  # too tight to profit
        if spread_bps > 100:
            return None  # too wide = illiquid/fake spread
        # Top-3 depth in USD
        bid_depth = sum(float(l["sz"]) * float(l["px"]) for l in bids[:3])
        ask_depth = sum(float(l["sz"]) * float(l["px"]) for l in asks[:3])
        depth_usd = min(bid_depth, ask_depth)
        if depth_usd < 1500:
            return None  # too thin for safe quoting
        # Reject if bid or ask side is severely imbalanced (fake spread)
        if bid_depth < 300 or ask_depth < 300:
            return None  # one side too thin
        # Score = spread * log(depth) — strongly favors deep books
        # Penalize very wide spreads (>80bps)
        effective_spread = spread_bps * 0.5 if spread_bps > 80 else spread_bps
        score = effective_spread * math.log(max(depth_usd, 1.0))
        return (score, spread_bps, depth_usd)
    except Exception:
        return None

def select_best_pairs(info, current_pairs, min_spread_bps=15, max_pairs=5):
    """Scan all markets and return the best N pairs for MM."""
    all_coins = discover_markets(info)
    if not all_coins:
        return current_pairs  # fallback to current

    scored = []
    for coin in all_coins:
        result = market_quality(info, coin, min_spread_bps)
        if result:
            score, spread_bps, depth_usd = result
            scored.append((coin, score, spread_bps, depth_usd))

    scored.sort(key=lambda x: -x[1])  # highest score first
    best = scored[:max_pairs]

    if best:
        print(f"  SCANNER: Top {len(best)} pairs:")
        for coin, score, sp, dep in best:
            print(f"    {coin}: score={score:.2f} spread={sp:.0f}bps depth=${dep:.0f}")
        return [f"{coin}-PERP" for coin, _, _, _ in best]
    return current_pairs


def maybe_scan_pairs(info, exchange, on_l2_book_cb, on_trades_cb):
    return  # DISABLED: scanner was overriding config pairs with zero-book markets

def _maybe_scan_pairs_disabled(info, exchange, on_l2_book_cb, on_trades_cb):
    """Run pair scanner every PAIR_SCAN_INTERVAL cycles."""
    global PAIRS, COIN_MAP, _pair_scan_cycle
    _pair_scan_cycle += 1
    if _pair_scan_cycle < PAIR_SCAN_INTERVAL:
        return
    _pair_scan_cycle = 0
    try:
        new_pairs = select_best_pairs(info, PAIRS, MIN_SPREAD_BPS, MAX_PAIRS)
        if set(new_pairs) != set(PAIRS):
            added = set(new_pairs) - set(PAIRS)
            removed = set(PAIRS) - set(new_pairs)
            print(f"  SCANNER UPDATE: +{added or 'none'} -{removed or 'none'}")
            # Cancel orders on removed pairs before switching
            for rp in removed:
                rc = rp.replace("-PERP", "")
                try:
                    exchange.cancel(rc, None)
                except: pass
            PAIRS = new_pairs
            COIN_MAP.clear()
            COIN_MAP.update({p: p.replace("-PERP", "") for p in PAIRS})
            # Initialize tracking for new pairs
            for p in PAIRS:
                c = COIN_MAP[p]
                if p not in pair_fills:
                    pair_fills[p] = []
                    pair_trade_count[p] = 0
                if c not in price_history:
                    price_history[c] = []
                if c not in recent_trades:
                    recent_trades[c] = deque(maxlen=200)
            # Fetch metadata and subscribe WS for new coins
            for p in added:
                c = p.replace("-PERP", "")
                if c not in SIZE_DECIMALS:
                    try:
                        book = info.l2_snapshot(c)
                        if book and book["levels"][0]:
                            px_str = book["levels"][0][0]["px"]
                            if "." in px_str:
                                PRICE_DECIMALS[c] = len(px_str.split(".")[1])
                            else:
                                PRICE_DECIMALS[c] = 0
                            TICK_SIZE[c] = 10 ** (-PRICE_DECIMALS[c])
                            # Also get szDecimals from meta
                            meta = info.meta()
                            for asset in meta.get("universe", []):
                                if asset["name"] == c:
                                    SIZE_DECIMALS[c] = asset.get("szDecimals", 2)
                                    break
                            else:
                                SIZE_DECIMALS[c] = 2
                            print(f"  SCANNER: {c} szDec={SIZE_DECIMALS[c]} pxDec={PRICE_DECIMALS[c]} tick={TICK_SIZE[c]}")
                    except Exception as e:
                        print(f"  SCANNER: {c} metadata error: {e}")
                        PRICE_DECIMALS.setdefault(c, 4)
                        TICK_SIZE.setdefault(c, 0.0001)
                        SIZE_DECIMALS.setdefault(c, 2)
                # Subscribe to WS feeds for new coin
                try:
                    info.subscribe({"type": "l2Book", "coin": c}, on_l2_book_cb)
                    info.subscribe({"type": "trades", "coin": c}, on_trades_cb)
                    print(f"  SCANNER: WS subscribed {c}")
                except Exception as e:
                    print(f"  SCANNER: WS subscribe error {c}: {e}")
            tg_send(f"\U0001f504 Pair scan: {', '.join(PAIRS)}")
    except Exception as e:
        print(f"  SCANNER ERROR: {e}")


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
                "total_notional_pos": float(margin.get("totalNtlPos", 0)),
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
    # Enforce tick rounding: floor bids, ceil asks
    tick = TICK_SIZE.get(coin, 0.0001)
    if tick > 0:
        if is_buy:
            price = math.floor(price / tick) * tick
        else:
            price = math.ceil(price / tick) * tick
        p_dec = PRICE_DECIMALS.get(coin, 4)
        price = round(price, p_dec)
    # Reject zero/negative prices
    if price <= 0:
        print(f"  Order skipped: {coin} price={price} invalid")
        return None
    try:
        order_result = exchange.order(
            coin,
            is_buy,
            size,
            price,
            {"limit": {"tif": "Alo"}},  # Add Liquidity Only = maker only
            reduce_only=reduce_only,
        )
        track_request()  # count API call regardless of outcome
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


def check_fills(info, exchange, address):
    """Check for recent fills and track round-trip profitability."""
    global total_trade_count, round_trips, strategy_pause_until
    try:
        fills = _api_call(info.user_fills, address) or []
        new_fills = [f for f in fills if float(f.get("time", 0)) / 1000 > start_time]
        new_count = len(new_fills)

        # Dedup: only process fills we haven't seen before
        unseen_fills = []
        for f in new_fills:
            # Use tid (trade ID) if available, else hash of key fields
            fill_id = f.get("tid", "") or f"{f.get('time','')}-{f.get('coin','')}-{f.get('side','')}-{f.get('px','')}-{f.get('sz','')}-{f.get('oid','')}"
            if fill_id not in seen_fill_ids:
                seen_fill_ids.add(fill_id)
                unseen_fills.append(f)

        if unseen_fills:
            total_trade_count = new_count
            for f in unseen_fills:
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
                                    exchange.cancel(coin, _o['oid'])
                                    _cancelled += 1
                                except: pass
                        if _cancelled:
                            print(f'  >>> LOCKOUT: cancelled ALL {_cancelled} {coin} resting orders')
                        live_quotes.pop(coin, None)
                    except Exception:
                        pass
                    tg_send(f"🔒 <b>SIDE LOCKED</b> {coin}: {consec}x{side} - blocking for 120s")
                elif consec >= 4:
                    consec_warn = f" ⚠️{consec}x{side} CAUTION"
                    # 4 consecutive fills: reduce size but don't pause
                    adverse_size_mult[coin] = 0.7

                print(f"  >>> FILL: {side} {size} {coin} @ ${price:.2f} fee=${fee:.4f} pnl=${closed_pnl:.4f} edge={edge_bps:+.1f}bps{consec_warn}")
                emoji = "🟢" if side == "B" else "🔴"
                rebate_str = f"Rebate: +${-fee:.4f}" if fee < 0 else f"Fee: ${fee:.4f}"
                tg_msg = f"{emoji} <b>FILL</b>: {side} {size} {coin}\n💰 @ ${price:.2f} | {rebate_str} | Edge: {edge_bps:+.1f}bps"
                if consec >= 5:
                    tg_msg += f"\n🔒 LOCKED: {consec}x{side} - side blocked 120s"
                elif consec >= 3:
                    tg_msg += f"\n⚠️ ADVERSE: {consec}x{side} - paused 30s"
                tg_send(tg_msg)

                # Strategy pause: if last 20 trips avg net < 0, pause 60s
                if len(completed_trips) >= 20:
                    recent_20 = completed_trips[-20:]
                    avg_recent = sum(t["net"] for t in recent_20) / 20
                    if avg_recent < -0.01 and now_fill > strategy_pause_until:
                        strategy_pause_until = now_fill + 60
                        print(f"  >>> STRATEGY PAUSE: last 20 trips avg net ${avg_recent:.4f} < 0, pausing 60s")
                        tg_send(f"⏸️ <b>STRATEGY PAUSE</b>\nLast 20 trips avg: ${avg_recent:.4f}\nPausing new quotes 60s")

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
                    # If exit was force-closed (market order), fee was taker rate
                    # Adjust: taker=4.5bps vs maker=1.5bps, so add the difference
                    if leg.get("force_close"):
                        taker_diff = size * price * (0.045 - 0.015) / 100  # 3bps extra
                        total_fee += taker_diff
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
                track_volume(cost)  # track for request budget calculation

                for p in PAIRS:
                    if coin in p:
                        pair_fills[p].append(fill)
                        pair_trade_count[p] += 1
                        break

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
    # True PnL from trading = realized fill PnL + unrealized position PnL - total fees
    # This excludes deposits/withdrawals unlike portfolio_pnl
    total_unrealized = sum(p.get("unrealized_pnl", 0) for p in last_balances.get("positions", {}).values())
    true_trading_pnl = bot_realized_pnl + total_unrealized - bot_fees

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

    _inv_mean = sum(inventory_samples) / max(len(inventory_samples), 1) if inventory_samples else 0
    _inv_var = sum((x - _inv_mean)**2 for x in inventory_samples) / max(len(inventory_samples), 1) if inventory_samples else 0

    status = {
        "running": running,
        "exchange": "Hyperliquid",
        "network": "testnet" if USE_TESTNET else "mainnet",
        "uptime_min": round((time.time() - start_time) / 60, 1),
        "portfolio_value": round(pv, 2),
        "portfolio_pnl": round(portfolio_pnl, 4),
        "bot_realized_pnl": round(bot_realized_pnl, 6),
        "bot_net_pnl": round(bot_net_pnl, 6),
        "true_trading_pnl": round(true_trading_pnl, 6),
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
        "inventory_mean": round(_inv_mean, 2),
        "inventory_variance": round(_inv_var, 2),
        "last_profitability_diag": last_profitability_diag,
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
        orders = _api_call(info.open_orders, address) or []
        for o in orders:
            coin = o.get("coin", "")
            if coin not in result:
                result[coin] = []
            result[coin].append(o)
    except Exception as e:
        print(f"  Open orders error: {e}")
    return result


MAX_QUOTE_PAIRS = 3  # more pairs = more fill opportunities
MIN_SCORE_THRESHOLD = -2.0  # allow exit-only pairs with moderate negative score


def check_weak_pair(coin):
    """Check if a coin should be suspended based on recent trip performance.
    Returns (is_suspended, reason_string)."""
    now = time.time()

    # Check if currently suspended
    if coin in weak_pair_suspended:
        resume_at = weak_pair_suspended[coin]
        if now < resume_at:
            remaining = int(resume_at - now)
            return True, f"suspended {remaining}s (weak)"
        else:
            del weak_pair_suspended[coin]

    # Check recent trip history
    trips = coin_trips.get(coin, [])
    if len(trips) >= WEAK_PAIR_LOOKBACK:
        recent = trips[-WEAK_PAIR_LOOKBACK:]
        net_sum = sum(t["net"] for t in recent)
        winners = sum(1 for t in recent if t["net"] >= 0)
        if net_sum < 0 and winners < WEAK_PAIR_LOOKBACK * 0.4:
            # Last 10 trips are net negative with <40% win rate — suspend
            weak_pair_suspended[coin] = now + WEAK_PAIR_SUSPEND_SECS
            return True, f"SUSPENDED: last {WEAK_PAIR_LOOKBACK} trips net ${net_sum:.4f}, WR {winners}/{WEAK_PAIR_LOOKBACK}"

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
    global active_orders, risk_cooldown_until, quote_attempts, quotes_skipped_profitability, quotes_placed, last_profitability_diag, request_budget_paused

    # Reset per-cycle counters
    quote_attempts = 0
    quotes_skipped_profitability = 0
    quotes_placed = 0

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
    current_orders = get_open_orders_by_coin(info, address)
    active_orders = []  # rebuild for dashboard
    positions = last_balances.get("positions", {})

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
            print(f"\n  {coin} | {suspend_reason}")
            # Cancel any resting orders on suspended pairs
            coin_orders = current_orders.get(coin, [])
            for o in coin_orders:
                try:
                    exchange.cancel(coin, o["oid"])
                    track_request()
                except: pass
            live_quotes.pop(coin, None)
            continue

        # Check invalid_book cooldown
        if coin in invalid_book_until and time.time() < invalid_book_until[coin]:
            continue

        price_data = get_ws_book(coin)
        if not price_data:
            price_data = get_mid_price(info, coin)
        if not price_data:
            continue

        # === HARD BOOK VALIDATION: no score unless book is fully usable ===
        _mid = price_data.get("mid", 0)
        _bid = price_data.get("best_bid", 0)
        _ask = price_data.get("best_ask", 0)
        _spread = price_data.get("spread", 0)
        _bid_depth = price_data.get("bid_depth", 0)
        _ask_depth = price_data.get("ask_depth", 0)
        _tick = TICK_SIZE.get(coin, 0)

        if _mid <= 0 or _bid <= 0 or _ask <= 0:
            invalid_book_until[coin] = time.time() + 120  # cooldown 2 min
            print(f"\n  {coin} | ZERO BOOK: mid={_mid} bid={_bid} ask={_ask}, blacklisted 2m")
            continue
        if _ask <= _bid:
            print(f"\n  {coin} | INVERTED BOOK: bid={_bid} >= ask={_ask}, skip")
            continue
        if _spread <= 0:
            continue
        if _tick > 0 and round(_spread / _tick) < 1:
            continue
        # Minimum per-side depth check (prevents fake spreads)
        if _bid_depth * _mid < 300 or _ask_depth * _mid < 300:
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
        if i < MAX_QUOTE_PAIRS and score >= MIN_SCORE_THRESHOLD:
            quote_pairs.add(pair)
        elif has_inventory:
            # Score too low for entry, but inventory must be unwound
            exit_only_pairs.add(pair)
            quote_pairs.add(pair)
            reason = f"score {score:+.1f} < {MIN_SCORE_THRESHOLD}" if score < MIN_SCORE_THRESHOLD else f"ranked #{i+1} (max {MAX_QUOTE_PAIRS})"
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
                    exchange.cancel(coin, o["oid"])
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
        mkt_spread = price_data["spread"]

        # Zero-book guard: skip and cooldown if book data not initialized
        if mid <= 0 or best_bid <= 0 or best_ask <= 0:
            invalid_book_until[coin] = time.time() + 120
            print(f"  {coin} | NO BOOK DATA (mid={mid} bid={best_bid} ask={best_ask}), blacklisted 2m")
            continue
        mkt_spread_bps = mkt_spread / mid * 10000
        imbalance = orderbook_imbalance(price_data)

        # Calculate target spread (volatility-adaptive)
        vol_multiplier = 1.0 + min(vol_bps / 10.0, 2.0)
        spread_bps = MIN_SPREAD_BPS * vol_multiplier
        target_spread = mid * spread_bps / 10000

        quote_attempts += 1  # count every pair we consider quoting

        # Fixed size from config — no dynamic scaling until edge is proven profitable
        dynamic_size_usd = ORDER_SIZE_USD
        # Split across levels, respect $10 minimum per order
        # Dynamic quote levels based on spread width
        if mkt_spread_bps > 50:
            dyn_levels = 4
        elif mkt_spread_bps > 25:
            dyn_levels = 3
        else:
            dyn_levels = 1  # tight market, single level to avoid clutter
        num_levels = min(dyn_levels, QUOTE_LEVELS)  # cap at configured max
        level_size_usd = dynamic_size_usd  # each level gets full size
        size = round(level_size_usd / mid, s_dec)
        if size * mid < 10.0:
            size = round(10.5 / mid, s_dec)
        equity_pct = (size * mid) / account_value * 100 if account_value > 0 else 0
        print(f"  SIZE: ${size * mid:.2f} ({size} {coin}) = {equity_pct:.0f}% of ${account_value:.2f} equity")

        # Check current position
        pos = positions.get(coin, {})
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
            continue

        # --- Request budget check ---
        budget_left = request_budget_remaining()
        if budget_left < REQUEST_BUDGET_BUFFER:
            if not request_budget_paused:
                request_budget_paused = True
                print(f"  >>> REQUEST BUDGET LOW: {budget_left} remaining. Pausing quotes.")
            continue
        elif request_budget_paused:
            request_budget_paused = False
            print(f"  >>> REQUEST BUDGET OK: {budget_left} remaining. Resuming.")

        # Get existing orders for this coin
        coin_orders = current_orders.get(coin, [])

        # Trade flow signal
        flow_imb, buy_vol, sell_vol = get_flow_signal(coin)
        flow_total = buy_vol + sell_vol

        # === ADVERSE SELECTION HARD GATES ===
        # These prevent entering toxic setups. Exits always allowed.

        # Gate 1: Order book imbalance — if book heavily skewed, price about to move
        bid_depth_3 = price_data.get("bid_depth", 0)
        ask_depth_3 = price_data.get("ask_depth", 0)
        total_depth = bid_depth_3 + ask_depth_3
        if total_depth > 0:
            book_ratio = bid_depth_3 / total_depth
        else:
            book_ratio = 0.5
        if (book_ratio < 0.10 or book_ratio > 0.90) and pos_size == 0:
            quotes_skipped_profitability += 1
            if coin_orders:
                for o in coin_orders:
                    try: exchange.cancel(coin, o["oid"])
                    except: pass
                live_quotes.pop(coin, None)
            print(f"  ADVERSE GATE: book imbalance {book_ratio:.2f} (need 0.10-0.90), skip {coin}")
            continue

        # Gate 2: Book depth — if top-of-book too thin, not enough liquidity to safely make
        bid_depth_usd = bid_depth_3 * mid
        ask_depth_usd = ask_depth_3 * mid
        if (bid_depth_usd < MIN_TOP_BOOK_DEPTH_USD or ask_depth_usd < MIN_TOP_BOOK_DEPTH_USD) and pos_size == 0:
            quotes_skipped_profitability += 1
            if coin_orders:
                for o in coin_orders:
                    try: exchange.cancel(coin, o["oid"])
                    except: pass
                live_quotes.pop(coin, None)
            print(f"  ADVERSE GATE: thin book bid=${bid_depth_usd:.0f} ask=${ask_depth_usd:.0f} (need ${MIN_TOP_BOOK_DEPTH_USD}), skip {coin}")
            continue

        # Gate 3: Microprice divergence — if microprice diverges from mid, informed flow present
        microprice_div_bps = abs(microprice - mid) / mid * 10000 if mid > 0 else 0
        if microprice_div_bps > 15.0 and pos_size == 0:
            quotes_skipped_profitability += 1
            if coin_orders:
                for o in coin_orders:
                    try: exchange.cancel(coin, o["oid"])
                    except: pass
                live_quotes.pop(coin, None)
            print(f"  ADVERSE GATE: microprice divergence {microprice_div_bps:.1f}bps, skip {coin}")
            continue

        # Gate 4: Heavy one-sided flow — someone is aggressively trading, don't be their exit liquidity
        flow_total_usd = flow_total * mid  # convert token volume to USD
        if flow_total_usd > 100.0 and abs(flow_imb) > 0.70 and pos_size == 0:
            quotes_skipped_profitability += 1
            if coin_orders:
                for o in coin_orders:
                    try: exchange.cancel(coin, o["oid"])
                    except: pass
                live_quotes.pop(coin, None)
            print(f"  ADVERSE GATE: toxic flow {flow_imb:+.2f} vol={flow_total:.1f}, skip {coin}")
            continue

        inv_extra_skew = 0.0

        # === FLOW-AWARE QUEUE QUALITY + MULTI-QUOTE MARKET MAKING ===

        inventory_usd = pos_size * mid
        inv_ratio = 0.0
        if MAX_INVENTORY_USD > 0:
            inv_ratio = max(-1.0, min(1.0, inventory_usd / MAX_INVENTORY_USD))

        # Entry/Exit split: exits always allowed, entries blocked when holding
        is_exit_only = pair in exit_only_pairs
        allow_buy = True
        allow_sell = True

        # Strict entry blocking: ANY inventory = no new entries until flat
        # This is the #1 fix for "18 fills, 2 trips" problem
        if pos_size > 0:
            allow_buy = False   # LONG: only sell to exit
        elif pos_size < 0:
            allow_sell = False  # SHORT: only buy to exit

        # Total live risk cap: position + resting entry orders
        # Prevents accumulating more risk than the account can handle
        MAX_TOTAL_LIVE_RISK_USD = min(25.0, account_value * 0.18)
        entry_notional = 0.0
        for o in coin_orders:
            o_side = o.get("side")
            o_sz = float(o.get("sz", 0))
            o_px = float(o.get("limitPx", 0))
            # Count entry-side resting orders
            if pos_size >= 0 and o_side == "B":
                entry_notional += o_sz * o_px
            elif pos_size <= 0 and o_side == "A":
                entry_notional += o_sz * o_px
        total_live_risk = pos_usd + entry_notional
        if total_live_risk > MAX_TOTAL_LIVE_RISK_USD:
            if pos_size > 0:
                allow_buy = False
            elif pos_size < 0:
                allow_sell = False
            elif pos_size == 0:
                # Flat but too many resting orders — shouldn't happen with QUOTE_LEVELS=1
                pass
            print(f"  {coin} | RISK CAP: live=${total_live_risk:.1f} > cap=${MAX_TOTAL_LIVE_RISK_USD:.1f}")

        # === STOP-LOSS CHECK (after side-block init so it doesn't get overwritten) ===
        STOP_LOSS_BPS = 150  # only engage stop-loss on very large adverse moves
        if pos_size != 0 and entry_price > 0:
            if pos_size > 0:
                loss_bps = (entry_price - mid) / entry_price * 10000
            else:
                loss_bps = (mid - entry_price) / entry_price * 10000
            if loss_bps > STOP_LOSS_BPS:
                print(f"  STOP LOSS MODE: {loss_bps:.0f}bps against, blocking entries")
                if pos_size > 0:
                    allow_buy = False
                else:
                    allow_sell = False
                inv_extra_skew = max(inv_extra_skew, 5.0)

        # Dynamic inventory cap: 10% of equity, capped at MAX_INVENTORY_USD
        max_inv_equity = max(15.0, account_value * 0.15)
        effective_inv_cap = min(MAX_INVENTORY_USD, max_inv_equity)

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
            if overweight_ratio > 6.0:
                # Truly dangerous: taker close
                print(f"  {coin} | EMERGENCY CLOSE: ${pos_usd:.2f} = {overweight_ratio:.1f}x cap")
                for o in coin_orders:
                    try: exchange.cancel(coin, o["oid"])
                    except: pass
                try:
                    _api_call(exchange.market_close, coin)
                    track_request()
                except: pass
                if coin in trip_tracker:
                    trip_tracker[coin]["force_close"] = True
                live_quotes.pop(coin, None)
                continue
            elif overweight_ratio > 3.0:
                # Over cap but not dangerous: aggressive maker exit
                print(f"  {coin} | OVERWEIGHT: ${pos_usd:.2f} = {overweight_ratio:.1f}x cap, maker exit")
                inv_extra_skew = max(inv_extra_skew, 5.0)
                if pos_size > 0:
                    allow_buy = False
                else:
                    allow_sell = False

            # Time-decaying exit urgency: smooth ramp from 0→6bps
            # Starts at 20s, reaches max at 90s, force close at 300s
            if hold_secs > 300:
                # 5+ minutes: last resort force market close
                print(f"  {coin} | HARD MAX HOLD: {hold_secs:.0f}s, taker close")
                for o in coin_orders:
                    try: exchange.cancel(coin, o["oid"])
                    except: pass
                try:
                    _api_call(exchange.market_close, coin)
                    track_request()
                except: pass
                if coin in trip_tracker:
                    trip_tracker[coin]["force_close"] = True
                live_quotes.pop(coin, None)
                continue
            elif hold_secs > 20:
                # Smooth urgency ramp: 20s→0bps, 40s→1bps, 60s→2.5bps, 90s→6bps
                urgency_t = min((hold_secs - 20) / 70.0, 1.0)  # 0→1 over 20-90s
                exit_urgency_bps = urgency_t * urgency_t * 6.0  # quadratic ramp, max 6bps
                inv_extra_skew = max(inv_extra_skew, exit_urgency_bps)
                if hold_secs > 60:
                    print(f"  {coin} | EXIT URGENCY: {hold_secs:.0f}s held, +{exit_urgency_bps:.1f}bps skew")
            if overweight_ratio > 2.5:
                inv_extra_skew = max(inv_extra_skew, 4.0)

        if is_exit_only:
            # Score below threshold — only allow the exit side
            if pos_size > 0:
                allow_buy = False   # long: only sell to exit
            elif pos_size < 0:
                allow_sell = False  # short: only buy to exit
            # Exit-only: keep tight pricing, don't add skew that pushes exit away
            # The exit side will join/penny the best level for fastest maker fill
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
        if flow_total * mid > 100.0:  # $100+ one-sided flow
            if flow_imb < -0.8:
                # Heavy selling pressure: stop buying — UNLESS we're SHORT and need to buy to exit
                if pos_size >= 0:
                    allow_buy = False
            elif flow_imb > 0.8:
                # Heavy buying pressure: stop selling — UNLESS we're LONG and need to sell to exit
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
        if abs(inv_ratio) > 0.70:
            spread_bps += 1
        if abs(inv_ratio) > 0.90:
            spread_bps += 2
        target_spread = mid * spread_bps / 10000

        # Use microprice as fair value base (volume-weighted, more accurate than raw mid)
        fair_mid = microprice
        flow_shift_applied = 0
        if abs(flow_imb) > 0.3 and flow_total * mid > 10.0:  # $10+ flow
            # Scale shift: 0.3-1.0 imbalance -> 0 to FLOW_SHIFT_BPS
            shift_factor = min((abs(flow_imb) - 0.3) / 0.7, 1.0)
            shift_bps = FLOW_SHIFT_BPS * shift_factor
            if flow_imb > 0:
                fair_mid = mid * (1 + shift_bps / 10000)
            else:
                fair_mid = mid * (1 - shift_bps / 10000)
            flow_shift_applied = shift_bps * (1 if flow_imb > 0 else -1)

        # Inventory bias: shift fair value toward flattening
        # Combined with microprice, this gives the bot a single "where should I quote" signal
        if account_value > 0 and abs(inventory_usd) > 1.0:
            BIAS_FACTOR = 0.10  # 15% of inventory/equity ratio applied as price bias
            inv_bias_ratio = inventory_usd / account_value
            inv_bias_bps = inv_bias_ratio * BIAS_FACTOR * 10000
            # Cap bias at half the spread so we don't invert
            inv_bias_bps = max(-mkt_spread_bps / 2, min(inv_bias_bps, mkt_spread_bps / 2))
            fair_mid = fair_mid * (1 - inv_bias_bps / 10000)

        # Widen spread during extreme one-way flow (protection)
        if abs(flow_imb) > 0.6 and flow_total * mid > 50.0:  # $50+ extreme flow
            spread_bps += FLOW_WIDEN_BPS

        # Widen spread on order book imbalance (price move likely)
        if abs(imbalance) > 0.5:
            obi_widen = 3 if abs(imbalance) > 0.7 else 2
            spread_bps += obi_widen

        target_spread = fair_mid * spread_bps / 10000

        # Stronger inventory skew: push quotes harder toward flattening
        INVENTORY_SKEW_BPS = 30.0  # aggressive skew for fast trip completion
        skew_bps = inv_ratio * INVENTORY_SKEW_BPS

        quotes = live_quotes.get(coin, {})

        # --- STRATEGY PAUSE CHECK ---
        if time.time() < strategy_pause_until:
            remaining = int(strategy_pause_until - time.time())
            print(f"  STRATEGY PAUSE: {remaining}s (avg trip net negative)")
            # Cancel all orders while paused
            for o in coin_orders:
                try:
                    exchange.cancel(coin, o["oid"])
                    track_request()
                except: pass
            live_quotes.pop(coin, None)
            continue

        # --- PROFITABILITY-GATED QUOTING (A/B mode) ---
        spread_ticks = round(mkt_spread / tick) if tick > 0 else 1

        # Hard market spread floor: don't enter unless spread covers full round trip
        # Must capture > 2x fees + safety on ENTRY, because exit spread may be tighter
        # Dynamic spread floor: adapts to volatility
        # fees*2 = round-trip cost, vol*0.5 = adverse selection estimate
        dynamic_floor_bps = MAKER_FEE_BPS * 2 + max(vol_bps * 0.5, 3.0) + 2.0 + 2.0  # fees + adverse(vol or 3bps) + exit_slip(2) + profit(2)
        if mkt_spread_bps < dynamic_floor_bps and pos_size == 0:
            quotes_skipped_profitability += 1
            if coin_orders:
                for o in coin_orders:
                    try: exchange.cancel(coin, o["oid"])
                    except: pass
                live_quotes.pop(coin, None)
            print(f"  FLOOR: mkt {mkt_spread_bps:.1f}bps < {dynamic_floor_bps:.0f}bps (dyn: fee={MAKER_FEE_BPS*2:.0f}+vol={vol_bps*0.5:.1f}), skip")
            continue

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
            # Queue-aware pennying: USD-denominated thresholds
            # NEVER penny when spread is too tight — causes post-only crossing
            # Graduated queue stepping: more aggressive to get fills
            bid_sz = price_data.get("bid_size", 0)
            ask_sz = price_data.get("ask_size", 0)
            bid_queue_usd = bid_sz * mid
            ask_queue_usd = ask_sz * mid
            if spread_ticks <= 2:
                # Spread is 1-2 ticks: join, no pennying
                bid_step = 0
                ask_step = 0
            else:
                # Graduated stepping based on queue depth
                # $50+ → 1 tick, $200+ → 2 ticks, $500+ → 3 ticks
                if bid_queue_usd > 500:
                    bid_step = 3
                elif bid_queue_usd > 200:
                    bid_step = 2
                elif bid_queue_usd > 50:
                    bid_step = 1
                else:
                    bid_step = 0

                if ask_queue_usd > 500:
                    ask_step = 3
                elif ask_queue_usd > 200:
                    ask_step = 2
                elif ask_queue_usd > 50:
                    ask_step = 1
                else:
                    ask_step = 0

                # Always step at least 1 tick when spread allows
                if spread_ticks > 4 and bid_step == 0:
                    bid_step = 1
                if spread_ticks > 4 and ask_step == 0:
                    ask_step = 1

                # Cap: never consume more than 1/4 of spread
                max_step = max((spread_ticks - 1) // 4, 0)
                bid_step = min(bid_step, max_step)
                ask_step = min(ask_step, max_step)
            buy_price = round(best_bid + bid_step * tick, p_dec)
            sell_price = round(best_ask - ask_step * tick, p_dec)
            buy_reason = f"penny x{bid_step}" if bid_step > 0 else "join bid"
            sell_reason = f"penny x{ask_step}" if ask_step > 0 else "join ask"

            # Dynamic capture ratio: adapts to spread width
            # Wide spreads → capture more edge; tight spreads → trade faster
            if mkt_spread_bps > 40:
                CAPTURE_RATIO = 0.45
            elif mkt_spread_bps > 25:
                CAPTURE_RATIO = 0.35
            else:
                CAPTURE_RATIO = 0.25
            half_capture = mkt_spread * CAPTURE_RATIO / 2.0
            # Center on microprice (fair_mid), not raw mid
            capture_bid = round(fair_mid - half_capture, p_dec)
            capture_ask = round(fair_mid + half_capture, p_dec)
            # Clamp: don't quote worse than capture ratio suggests
            if buy_price > capture_bid:
                buy_price = capture_bid
                buy_reason += " +capture"
            if sell_price < capture_ask:
                sell_price = capture_ask
                sell_reason += " +capture"
            # But never quote outside the book (capture can't push past best bid/ask)
            buy_price = min(buy_price, best_bid)
            sell_price = max(sell_price, best_ask)
        elif pos_size != 0:
            # Spread too tight BUT we have inventory — allow exit side only
            # Step inside spread aggressively for the exit side
            quotes_skipped_profitability += 1
            if pos_size > 0:
                # LONG: need to sell to exit — penny the ask aggressively
                allow_buy = False
                buy_price = best_bid  # placeholder, won't be used
                sell_price = best_ask  # join best ask, don't penny (preserve price)
                buy_reason = "gated"
                sell_reason = "exit-penny"
                print(f"  TIGHT-SPREAD EXIT: {mkt_spread_bps:.1f}bps < {required_bps:.1f}bps, sell-only exit")
            else:
                # SHORT: need to buy to exit — penny the bid aggressively
                allow_sell = False
                sell_price = best_ask  # placeholder, won't be used
                buy_price = best_bid  # join best bid, don't penny (preserve price)
                buy_reason = "exit-penny"
                sell_reason = "gated"
                print(f"  TIGHT-SPREAD EXIT: {mkt_spread_bps:.1f}bps < {required_bps:.1f}bps, buy-only exit")
        else:
            # Spread too tight, no inventory — gate both sides
            expected_net_tight = mkt_spread * size - 2 * (size * mid * MAKER_FEE_BPS / 10000)
            last_profitability_diag["expected_net"] = round(expected_net_tight, 6)
            if coin_orders:
                for o in coin_orders:
                    try: exchange.cancel(coin, o["oid"])
                    except: pass
            quotes_skipped_profitability += 1
            print(f"  GATE [{PROFITABILITY_MODE}]: mkt={market_ticks}t req={required_ticks}t | {mkt_spread_bps:.1f}bps < {required_bps:.1f}bps | expNet=${expected_net_tight:.4f}")
            live_quotes.pop(coin, None)
            continue

        # Immediate exit push: always push exit 1 tick closer to mid
        if pos_size > 0:
            sell_price = round(sell_price - tick, p_dec)
            sell_reason += " -1tick"
        elif pos_size < 0:
            buy_price = round(buy_price + tick, p_dec)
            buy_reason += " +1tick"

        # Direct exit urgency: tighten exit price based on hold time
        open_leg_q = trip_tracker.get(coin)
        if open_leg_q and pos_size != 0:
            hold_q = time.time() - open_leg_q.get("time", time.time())
            if hold_q > 10:
                # Fast urgency: 10s→1bps, 30s→2bps, 60s→4bps, 90s→8bps
                if hold_q > 90:
                    direct_urgency = 8.0
                elif hold_q > 60:
                    direct_urgency = 4.0
                elif hold_q > 30:
                    direct_urgency = 2.0
                else:
                    direct_urgency = 1.0
                urgency_px = mid * direct_urgency / 10000
                if pos_size > 0:
                    # LONG: tighten sell toward mid
                    sell_price = round(sell_price - urgency_px, p_dec)
                    sell_reason += f" -urg{direct_urgency:.0f}bp"
                else:
                    # SHORT: tighten buy toward mid
                    buy_price = round(buy_price + urgency_px, p_dec)
                    buy_reason += f" -urg{direct_urgency:.0f}bp"

        # INVENTORY MEAN-REVERSION PRICING
        # Shift both bid and ask by inventory_bias = k * inv_ratio
        # This makes the exit side much more aggressive while pushing entry away
        # Professional MM approach: quotes center around mid - bias, not mid
        is_tight_exit = (buy_reason == "exit-penny" or sell_reason == "exit-penny")
        if is_tight_exit:
            capped_extra = 0
        else:
            # Mean-reversion bias: proportional to inventory, capped at half spread
            half_spread_bps = mkt_spread_bps / 2
            max_bias_bps = max(half_spread_bps - 1.0, 2.0)

            # Base skew + urgency extras
            total_skew_bps = abs(skew_bps) + inv_extra_skew
            capped_extra = min(inv_extra_skew, max_bias_bps)

            # Inventory bias: shift BOTH quotes toward flattening
            # k = INVENTORY_SKEW_BPS, scaled by inv_ratio
            bias_bps = min(total_skew_bps, max_bias_bps)
            bias_px = mid * bias_bps / 10000.0

            if inv_ratio > 0:
                # LONG: shift everything DOWN (sell closer to mid, buy further away)
                # Exit side (sell) gets 70% of bias, entry side (buy) gets 30%
                sell_shift = bias_px * 0.7
                buy_shift = bias_px * 0.3
                sell_price = round(sell_price - sell_shift, p_dec)
                buy_price = round(buy_price - buy_shift, p_dec)
            elif inv_ratio < 0:
                # SHORT: shift everything UP (buy closer to mid, sell further away)
                buy_shift = bias_px * 0.7
                sell_shift = bias_px * 0.3
                buy_price = round(buy_price + buy_shift, p_dec)
                sell_price = round(sell_price + sell_shift, p_dec)
            else:
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

        # === REALISTIC PROFITABILITY FORMULA ===
        # realized_capture = quoted_spread * capture_efficiency - fees - adverse - exit_slippage
        # Must exceed profit target to trade
        ADVERSE_BUFFER_BPS = 3.0   # adverse selection allowance
        EXIT_BUFFER_BPS = 2.0     # exit slippage allowance
        PROFIT_TARGET_BPS = 2.0   # minimum profit target per trip
        realistic_spread = min(sell_price - buy_price, mkt_spread)
        spread_capture_bps = realistic_spread / mid * 10000 if mid > 0 else 0
        fee_cost_bps = 2 * MAKER_FEE_BPS  # both legs maker
        adverse_cost_bps = max(vol_bps * 0.3, ADVERSE_BUFFER_BPS)  # at least 3bps
        vol_hold_cost_bps = max(vol_bps * 0.2, EXIT_BUFFER_BPS)    # at least 2bps exit slippage
        net_edge_bps = spread_capture_bps - fee_cost_bps - adverse_cost_bps - vol_hold_cost_bps
        expected_net = realistic_spread * size - 2 * (size * mid * MAKER_FEE_BPS / 10000)
        last_profitability_diag["expected_net"] = round(expected_net, 6)
        last_profitability_diag["net_edge_bps"] = round(net_edge_bps, 2)

        if net_edge_bps < MIN_EDGE_BPS:
            if pos_size == 0:
                # Flat: gate both sides — not profitable after all costs
                if coin_orders:
                    for o in coin_orders:
                        try: exchange.cancel(coin, o["oid"])
                        except: pass
                quotes_skipped_profitability += 1
                print(f"  EDGE GATE: net={net_edge_bps:.1f}bps < {MIN_EDGE_BPS}bps (cap={spread_capture_bps:.1f} -fee={fee_cost_bps:.1f} -adv={adverse_cost_bps:.1f} -vol={vol_hold_cost_bps:.1f})")
                live_quotes.pop(coin, None)
                continue
            else:
                # Have inventory: gate entry side only, always allow exit
                quotes_skipped_profitability += 1
                if pos_size > 0:
                    allow_buy = False
                    print(f"  EDGE GATE: net={net_edge_bps:.1f}bps < {MIN_EDGE_BPS}bps, BUY gated (LONG inv)")
                else:
                    allow_sell = False
                    print(f"  EDGE GATE: net={net_edge_bps:.1f}bps < {MIN_EDGE_BPS}bps, SELL gated (SHORT inv)")

        # quotes_placed moved below — only count when at least one side quotes

        # Adverse selection: apply size reduction if triggered
        if coin in adverse_size_mult:
            if time.time() < adverse_pause_until.get(coin, 0):
                size = round(size * adverse_size_mult[coin], s_dec)
                if size * mid < 10.0:
                    size = round(10.5 / mid, s_dec)
            else:
                adverse_size_mult.pop(coin, None)
                adverse_pause_until.pop(coin, None)

        # Only count as "placed" if at least one side will actually quote
        if allow_buy or allow_sell:
            quotes_placed += 1

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

                # Queue stepping: if best resting buy is behind best_bid and spread allows, step up
                best_resting_buy = max(existing_buys.keys())
                jumped = best_resting_buy < best_bid and spread_ticks > 3
                if crossed or (top_buy_drift > replace_bps) or wrong_count or jumped:
                    # Must replace: crossed book, very stale, wrong level count, or jumped in queue
                    for oid in existing_buys.values():
                        try:
                            exchange.cancel(coin, oid)
                            track_request()
                        except: pass
                    placed = []
                    for lvl_px in buy_levels:
                        oid = place_order(exchange, coin, True, size, lvl_px)
                        if oid:
                            placed.append(lvl_px)
                    reason = "crossed" if crossed else "jumped" if jumped else f"stale {top_buy_drift:.0f}bps"
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
                    try: exchange.cancel(coin, oid)
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

                # Queue stepping: if best resting sell is behind best_ask and spread allows, step down
                best_resting_sell = min(existing_sells.keys())
                jumped = best_resting_sell > best_ask and spread_ticks > 3
                if crossed or (top_sell_drift > replace_bps) or wrong_count or jumped:
                    for oid in existing_sells.values():
                        try:
                            exchange.cancel(coin, oid)
                            track_request()
                        except: pass
                    placed = []
                    for lvl_px in sell_levels:
                        oid = place_order(exchange, coin, False, size, lvl_px)
                        if oid:
                            placed.append(lvl_px)
                    reason = "crossed" if crossed else "jumped" if jumped else f"stale {top_sell_drift:.0f}bps"
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
                    try: exchange.cancel(coin, oid)
                    except: pass

        # Print spread capture diagnostic
        our_spread = sell_levels[0] - buy_levels[0] if buy_levels and sell_levels else 0
        our_spread_bps = our_spread / mid * 10000 if mid > 0 else 0
        realistic_spread_diag = min(our_spread, mkt_spread)
        expected_net = realistic_spread_diag * size - 2 * (size * mid * MAKER_FEE_BPS / 10000)
        gate_str = ""
        if not allow_buy:
            gate_str += " [NO BUY]"
        if not allow_sell:
            gate_str += " [NO SELL]"
        skew_display = f"{skew_bps:+.1f}bps" + (f" +extra:{capped_extra:.1f}" if capped_extra > 0 else "")
        edge_diag = last_profitability_diag.get("net_edge_bps", 0)
        print(f"  Spread: ${our_spread:.{p_dec}f} ({our_spread_bps:.1f}bps) | Edge: {edge_diag:+.1f}bps (cap={spread_capture_bps:.0f}-fee={fee_cost_bps:.0f}-adv={adverse_cost_bps:.1f}-vol={vol_hold_cost_bps:.1f}) | ExpNet: ${expected_net:.4f} | Skew: {skew_display}{gate_str}")

        # Print position status
        if pos_size != 0:
            direction = "LONG" if pos_size > 0 else "SHORT"
            upnl = pos.get("unrealized_pnl", 0)
            print(f"  Pos: {direction} {abs(pos_size)} @ ${entry_price:.{p_dec}f} | uPnL: ${upnl:.4f} | Inv: ${inventory_usd:.2f}")

        live_quotes[coin] = quotes
        last_quote_time[coin] = time.time()

        # Note: active_orders rebuilt after all pairs are quoted (below)

    # Rebuild active_orders from current_orders (already fetched at cycle start)
    for _coin, _orders in current_orders.items():
        for o in _orders:
            side_str = "buy" if o.get("side") == "B" else "sell"
            active_orders.append({
                "pair": f"{_coin}-PERP", "side": side_str,
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

    # Check fills via REST (every 30s to save budget; WS handles real-time detection)
    if not hasattr(run_cycle, '_last_fill_check'):
        run_cycle._last_fill_check = 0
    if time.time() - run_cycle._last_fill_check > 30:
        check_fills(info, exchange, address)
        run_cycle._last_fill_check = time.time()

    pv = portfolio_value()
    elapsed = time.time() - start_time
    print(f"\n{'='*55}")
    # Trip diagnostics
    trip_stats = ""
    if completed_trips:
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
    print(f"  Gate [{PROFITABILITY_MODE}]: {quotes_placed}/{quote_attempts} placed ({gate_pct:.0f}% skipped) | InvMean: ${inv_mean:.1f} InvVar: {inv_var:.0f}")
    print(f"  Budget: {request_budget_remaining()} left (reqs:{request_count} vol:${volume_traded_usd:.0f})")
    print(f"{'='*55}")
    write_status()


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

    # Start Telegram remote command listener
    tg_cmd = TelegramCommander(TG_TOKEN, TG_CHAT_ID, sys.modules[__name__])
    tg_cmd.start()

    tg_send(f"🚀 <b>Hyperliquid Bot Started</b>\n{'TESTNET' if USE_TESTNET else 'MAINNET'}\nPairs: {', '.join(PAIRS)}\nSize: ${ORDER_SIZE_USD}/side | Spread: {MIN_SPREAD_BPS}bps | Event-driven")

    cycle_count = 0
    last_status_tg = time.time()
    last_account_refresh = 0

    while running:
        try:
            # Wait for book change or timeout (max 5s for account refresh/status)
            book_changed.wait(timeout=5.0)
            if not running:
                break
            book_changed.clear()

            # Throttle: don't run cycle if all coins were quoted recently
            now = time.time()
            all_recent = True
            quote_interval = get_quote_interval()
            for coin in [COIN_MAP.get(p, p.replace("-PERP", "")) for p in PAIRS]:
                last_t = last_quote_time.get(coin, 0)
                if now - last_t >= quote_interval:
                    all_recent = False
                    break
            if all_recent:
                continue  # skip this cycle, all coins quoted recently

            # Refresh account state periodically (every 30s to save API calls)
            if now - last_account_refresh > 30:
                get_account_state(info, address)
                last_account_refresh = now

            # Dynamic pair scan every 20 cycles
            maybe_scan_pairs(info, exchange, on_l2_book, on_trades)

            run_cycle(info, exchange, address)
            cycle_count += 1

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
            import traceback
            traceback.print_exc()
            time.sleep(2)  # back off on error

    tg_cmd.stop()
    cancel_all_orders(exchange, info, address)
    check_fills(info, exchange, address)
    write_status()
    pv = portfolio_value()
    print(f"\nFinal Portfolio: ${pv:.2f} | Fills: {total_trade_count}")
    tg_send(f"🛑 <b>HL Bot Stopped</b>\nPortfolio: ${pv:.2f} | Fills: {total_trade_count}")


if __name__ == "__main__":
    main()
