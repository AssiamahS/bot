"""
data_bridge.py — Hyperliquid-Sol Data Bridge
Serves trade_log.jsonl + telegram_history.jsonl to dashboard.html
Port 8085 | async FastAPI | in-memory cache | WebSocket push

Terminal 4 agent owns this file.
"""

import asyncio
import json
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR        = Path(__file__).parent
TRADE_LOG       = Path(os.getenv("TRADE_LOG",       str(BASE_DIR / "trade_log.jsonl")))
TRADER_STATUS   = Path(os.getenv("TRADER_STATUS",   str(BASE_DIR / "trader_status.json")))
TELEGRAM_LOG    = Path(os.getenv("TELEGRAM_LOG",    str(Path.home() / "telegram_history.jsonl")))
CACHE_TTL_SEC   = int(os.getenv("CACHE_TTL_SEC",    "5"))
MAX_TRADES      = int(os.getenv("MAX_TRADES",       "500"))
MAX_RECENT      = 50   # recent trades/fills returned in API responses
PORT            = int(os.getenv("PORT",             "8085"))

# ── In-memory state ───────────────────────────────────────────────────────────

class AppState:
    trades:        deque[dict]    = deque(maxlen=MAX_TRADES)
    status:        dict           = {}
    telegram:      list[dict]     = []
    last_refresh:  datetime | None = None
    ws_clients:    set[WebSocket] = set()
    _lock:         asyncio.Lock   = asyncio.Lock()

state = AppState()

# ── Pydantic response models ──────────────────────────────────────────────────

class Position(BaseModel):
    coin:        str
    side:        str
    size:        float
    entry_price: float
    unrealized_pnl: float

class Trade(BaseModel):
    ts:          str
    coin:        str
    side:        str
    size:        float
    price:       float
    pnl:         float | None = None
    fee:         float | None = None

class StatusResponse(BaseModel):
    bot_live:        bool
    total_pnl:       float
    daily_pnl:       float
    win_rate:        float
    total_trades:    int
    open_positions:  list[Position]
    recent_trades:   list[Trade]
    last_updated:    str
    data_source:     str   # "live" | "trade_log" | "telegram" | "stale"

class HealthResponse(BaseModel):
    status:       str
    uptime_sec:   float
    cache_age_sec: float | None
    trade_count:  int
    ws_clients:   int

# ── Data loaders ──────────────────────────────────────────────────────────────

def _adapt_event(event: dict) -> dict | None:
    """Convert an events.jsonl record to the Trade schema.

    - Only 'fill' events are kept
    - 'closed_pnl' is mapped to 'pnl'
    - 'unrealized_pnl' defaults to 0.0
    """
    if event.get("type") != "fill":
        return None
    return {
        "ts":             event.get("ts"),
        "coin":           event.get("coin"),
        "side":           event.get("side"),
        "size":           event.get("size"),
        "price":          event.get("price"),
        "fee":            event.get("fee"),
        "pnl":            event.get("closed_pnl"),
        "unrealized_pnl": event.get("unrealized_pnl", 0.0),
    }


_trade_log_mtime: float = 0.0
_trade_log_cache: list[dict] = []


async def load_trade_log() -> list[dict]:
    global _trade_log_mtime, _trade_log_cache
    if not TRADE_LOG.exists():
        return []
    # Skip re-read if file hasn't changed since last load
    current_mtime = TRADE_LOG.stat().st_mtime
    if current_mtime == _trade_log_mtime and _trade_log_cache:
        return _trade_log_cache
    trades = []
    async with aiofiles.open(TRADE_LOG, "r") as f:
        async for line in f:
            line = line.strip()
            if line:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                adapted = _adapt_event(raw)
                if adapted is not None:
                    trades.append(adapted)
    _trade_log_cache = trades[-MAX_TRADES:]
    _trade_log_mtime = current_mtime
    return _trade_log_cache

async def load_trader_status() -> dict | None:
    if not TRADER_STATUS.exists():
        return None
    try:
        async with aiofiles.open(TRADER_STATUS, "r") as f:
            return json.loads(await f.read())
    except (json.JSONDecodeError, OSError):
        return None

async def load_telegram_history() -> list[dict]:
    if not TELEGRAM_LOG.exists():
        return []
    entries = []
    async with aiofiles.open(TELEGRAM_LOG, "r") as f:
        async for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries[-MAX_TRADES:]

# ── Derived metrics ───────────────────────────────────────────────────────────

def compute_metrics(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {
            "total_pnl": 0.0,
            "daily_pnl": 0.0,
            "win_rate":  0.0,
            "total_trades": 0,
            "open_positions": [],
            "recent_trades": [],
        }

    today = datetime.now(timezone.utc).date().isoformat()
    closed = [t for t in trades if t.get("pnl") is not None]
    winners = [t for t in closed if float(t.get("pnl", 0)) > 0]
    today_closed = [t for t in closed if str(t.get("ts", "")).startswith(today)]

    total_pnl = sum(float(t.get("pnl", 0)) for t in closed)
    daily_pnl = sum(float(t.get("pnl", 0)) for t in today_closed)
    win_rate  = len(winners) / len(closed) if closed else 0.0

    # Build open positions from trades without a close pnl
    open_map: dict[str, dict] = {}
    for t in trades:
        coin = t.get("coin", "UNKNOWN")
        if t.get("pnl") is None and t.get("size", 0) != 0:
            open_map[coin] = {
                "coin":            coin,
                "side":            t.get("side", "long"),
                "size":            float(t.get("size", 0)),
                "entry_price":     float(t.get("price", 0)),
                "unrealized_pnl":  float(t.get("unrealized_pnl", 0)),
            }

    recent = sorted(trades, key=lambda x: x.get("ts", ""), reverse=True)[:MAX_RECENT]

    return {
        "total_pnl":      round(total_pnl, 4),
        "daily_pnl":      round(daily_pnl, 4),
        "win_rate":       round(win_rate, 4),
        "total_trades":   len(closed),
        "open_positions": list(open_map.values()),
        "recent_trades":  recent,
    }

# ── Cache refresh ─────────────────────────────────────────────────────────────

async def refresh_cache() -> None:
    async with state._lock:
        live_status = await load_trader_status()
        trades_raw  = await load_trade_log()
        telegram    = await load_telegram_history()

        state.telegram     = telegram
        state.last_refresh = datetime.now(timezone.utc)

        # Check if trader_status.json is fresh (updated within 120s)
        is_live = False
        if live_status:
            age = datetime.now(timezone.utc).timestamp() - (live_status.get("updated_at") or 0)
            is_live = live_status.get("running", False) and age < 120

        if is_live:
            state.status = {**live_status, "data_source": "live", "bot_live": True}
            state.trades = deque(
                live_status.get("recent_trades", trades_raw), maxlen=MAX_TRADES
            )
        else:
            metrics = compute_metrics(trades_raw)
            state.trades  = deque(trades_raw, maxlen=MAX_TRADES)
            state.status  = {
                **metrics,
                "bot_live":    False,
                "last_updated": state.last_refresh.isoformat(),
                "data_source":  "trade_log" if trades_raw else (
                    "telegram" if telegram else "stale"
                ),
            }

async def cache_loop() -> None:
    while True:
        try:
            await refresh_cache()
            payload = build_dashboard_dict()
            await broadcast_ws(payload)
        except Exception as e:
            print(f"[cache_loop] error: {e}")
        await asyncio.sleep(CACHE_TTL_SEC)

# ── WebSocket broadcast ───────────────────────────────────────────────────────

async def broadcast_ws(payload: dict) -> None:
    # Wrap in {bot, data} envelope that dashboard.html expects
    dead: set[WebSocket] = set()
    for ws in state.ws_clients:
        try:
            bot = getattr(ws, "_subscribed_bot", "sol")
            await ws.send_json({"bot": bot, "data": payload})
        except Exception:
            dead.add(ws)
    state.ws_clients -= dead

# ── Response builder ──────────────────────────────────────────────────────────
# Outputs the exact schema dashboard.html renderDashboard() expects.

def aggregate_pair_stats(trades: list[dict]) -> tuple[dict, float, float]:
    """Aggregate per-coin stats from trade history. Returns (pair_status, total_fees, total_rebates)."""
    pair_status: dict[str, dict] = {}
    total_fees = 0.0
    total_rebates = 0.0
    for t in trades:
        coin = t.get("coin", "UNKNOWN")
        pair = f"{coin}-PERP"
        fee = float(t.get("fee") or 0)
        total_fees += fee
        if fee < 0:
            total_rebates += abs(fee)
        if pair not in pair_status:
            pair_status[pair] = {
                "trade_count": 0, "holding": 0.0, "holding_usd": 0.0,
                "unrealized_pnl": 0.0, "entry_price": 0.0, "volume_traded": 0.0,
                "fees_paid": 0.0,
            }
        ps = pair_status[pair]
        ps["trade_count"] += 1
        ps["fees_paid"] += fee
        ps["volume_traded"] += float(t.get("size") or 0) * float(t.get("price") or 0)
        ps["entry_price"] = float(t.get("price") or 0)
    return pair_status, total_fees, total_rebates


def trades_to_fills(trades: list[dict]) -> list[dict]:
    """Convert trades to recent_fills format dashboard expects."""
    recent_fills = []
    for t in sorted(trades, key=lambda x: x.get("ts", ""), reverse=True)[:MAX_RECENT]:
        ts_str = t.get("ts", "")
        try:
            ts_unix = datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            ts_unix = 0
        recent_fills.append({
            "time":       ts_unix,
            "pair":       f"{t.get('coin', 'UNKNOWN')}-PERP",
            "side":       t.get("side", ""),
            "price":      float(t.get("price") or 0),
            "volume":     float(t.get("size") or 0),
            "fee":        float(t.get("fee") or 0),
            "closed_pnl": float(t.get("pnl") or 0),
        })
    return recent_fills


def build_dashboard_dict() -> dict:
    """Build response matching dashboard.html's renderDashboard() expectations."""
    s = state.status
    trades = list(state.trades)

    pair_status, total_fees, total_rebates = aggregate_pair_stats(trades)
    recent_fills = trades_to_fills(trades)
    total_pnl = s.get("total_pnl", 0.0)

    return {
        "running":                s.get("bot_live", False),
        "network":                "mainnet",
        "updated_at":             datetime.now(timezone.utc).timestamp(),
        "portfolio_value":        0.0,
        "initial_portfolio_value": 0.0,
        "portfolio_pnl":          total_pnl,
        "bot_net_pnl":            total_pnl,
        "bot_realized_pnl":       total_pnl,
        "total_trade_count":      s.get("total_trades", 0),
        "total_fees":             round(total_fees, 4),
        "total_rebates":          round(total_rebates, 4),
        "pairs":                  list(pair_status.keys()),
        "pair_status":            pair_status,
        "prices":                 {},
        "active_orders":          [],
        "recent_fills":           recent_fills,
        "data_source":            s.get("data_source", "stale"),
    }

# ── App lifecycle ─────────────────────────────────────────────────────────────

_start_time = datetime.now(timezone.utc)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await refresh_cache()
    task = asyncio.create_task(cache_loop())
    yield
    task.cancel()

app = FastAPI(
    title="Hyperliquid-Sol Data Bridge",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_dashboard():
    return FileResponse(BASE_DIR / "dashboard.html", media_type="text/html")

@app.get("/dashboard.html")
async def serve_dashboard_explicit():
    return FileResponse(BASE_DIR / "dashboard.html", media_type="text/html")

@app.get("/status/sol.json")
async def compat_status_sol():
    return build_dashboard_dict()

@app.get("/api/status")
async def get_status():
    return build_dashboard_dict()

@app.get("/api/trades")
async def get_trades(limit: int = 100, coin: str | None = None):
    trades = list(state.trades)
    if coin:
        trades = [t for t in trades if t.get("coin", "").upper() == coin.upper()]
    return {"trades": trades[-limit:], "total": len(trades)}

@app.get("/api/telegram")
async def get_telegram(limit: int = MAX_RECENT):
    return {"messages": state.telegram[-limit:], "total": len(state.telegram)}

@app.get("/api/positions")
async def get_positions():
    return {"positions": state.status.get("open_positions", [])}

@app.get("/health", response_model=HealthResponse)
async def health():
    age = None
    if state.last_refresh:
        age = (datetime.now(timezone.utc) - state.last_refresh).total_seconds()
    return {
        "status":        "ok",
        "uptime_sec":    (datetime.now(timezone.utc) - _start_time).total_seconds(),
        "cache_age_sec": age,
        "trade_count":   len(state.trades),
        "ws_clients":    len(state.ws_clients),
    }

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws._subscribed_bot = "sol"  # default
    state.ws_clients.add(ws)
    try:
        async with state._lock:
            payload = build_dashboard_dict()
        await ws.send_json({"bot": "sol", "data": payload})
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                if msg.get("type") == "subscribe":
                    ws._subscribed_bot = msg.get("bot", "sol")
                    async with state._lock:
                        payload = build_dashboard_dict()
                    await ws.send_json({"bot": ws._subscribed_bot, "data": payload})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        state.ws_clients.discard(ws)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "data_bridge:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
