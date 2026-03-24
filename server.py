#!/usr/bin/env python3
"""
Dashboard server for Hyperliquid multi-bot setup.
- HTTP on port 8082: serves dashboard.html and /status/*.json
- WebSocket on port 8084: pushes live status updates from VPS
"""
import asyncio
import concurrent.futures
import http.server
import json
import os
import subprocess
import threading
import time

import websockets

HTTP_PORT = 8082
WS_PORT = 8084
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POLL_INTERVAL = 5  # seconds between VPS polls

VPS_HOST = "ubuntu@44.205.58.31"
VPS_KEY = os.path.expanduser("~/.ssh/hl-bot-key.pem")
VPS_SSH = ["ssh", "-i", VPS_KEY, "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", VPS_HOST]
VPS_SSH_SYNC = ["ssh", "-i", VPS_KEY, "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=accept-new", VPS_HOST]

# Bot name -> path on VPS
VPS_STATUS_FILES = {
    "sol": "~/hyperliquid-sol/trader_status.json",
    "btc": "~/hyperliquid-btc/trader_status.json",
    "eth": "~/hyperliquid-eth/trader_status.json",
}

# --- Shared state ---
latest_status = {}  # bot_name -> dict
clients = set()     # connected websocket clients
cached_events = {"data": None, "fetched_at": 0}  # VPS events cache
EVENTS_CACHE_TTL = 25  # seconds — refresh slightly under the 30s poll


async def read_vps_status(bot):
    """Read a bot's status JSON from VPS over SSH."""
    path = VPS_STATUS_FILES.get(bot)
    if not path:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *VPS_SSH, f"cat {path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0 and stdout:
            return json.loads(stdout)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        pass
    return None


async def read_all_vps_statuses():
    """Read all bot status files from VPS in one SSH call."""
    # Single SSH call to cat all files at once
    cat_cmds = " && ".join(
        f'echo "---{bot}---" && cat {path} 2>/dev/null || echo "null"'
        for bot, path in VPS_STATUS_FILES.items()
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *VPS_SSH, cat_cmds,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0 or not stdout:
            return {}
    except asyncio.TimeoutError:
        return {}

    result = {}
    output = stdout.decode()
    for bot in VPS_STATUS_FILES:
        marker = f"---{bot}---"
        idx = output.find(marker)
        if idx == -1:
            continue
        start = idx + len(marker) + 1
        # Find next marker or end
        next_markers = [output.find(f"---{b}---", start) for b in VPS_STATUS_FILES if b != bot]
        next_markers = [m for m in next_markers if m > 0]
        end = min(next_markers) if next_markers else len(output)
        chunk = output[start:end].strip()
        if chunk and chunk != "null":
            try:
                result[bot] = json.loads(chunk)
            except json.JSONDecodeError:
                pass
    return result


# --- WebSocket server ---

async def ws_handler(websocket):
    """Handle a single WebSocket client."""
    clients.add(websocket)
    try:
        # Send current state for all bots on connect
        for bot, data in latest_status.items():
            await websocket.send(json.dumps({"bot": bot, "data": data}))

        async for message in websocket:
            try:
                msg = json.loads(message)
                if msg.get("type") == "subscribe":
                    subscribed_bot = msg.get("bot")
                    if subscribed_bot == "all":
                        for bot, data in latest_status.items():
                            await websocket.send(json.dumps({"bot": bot, "data": data}))
                    elif subscribed_bot in latest_status:
                        await websocket.send(json.dumps({
                            "bot": subscribed_bot,
                            "data": latest_status[subscribed_bot],
                        }))
            except json.JSONDecodeError:
                pass
    except websockets.ConnectionClosed:
        pass
    finally:
        clients.discard(websocket)


async def broadcast(bot, data):
    """Push a status update to all connected clients."""
    if not clients:
        return
    msg = json.dumps({"bot": bot, "data": data})
    await asyncio.gather(
        *(c.send(msg) for c in clients.copy()),
        return_exceptions=True,
    )


async def poll_vps_status():
    """Poll VPS status files and broadcast changes."""
    global latest_status
    print("Fetching initial status from VPS...")
    latest_status = await read_all_vps_statuses()
    print(f"Got status for: {', '.join(latest_status.keys()) or 'none'}")

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        new_statuses = await read_all_vps_statuses()
        for bot, data in new_statuses.items():
            old = latest_status.get(bot)
            if data != old:
                latest_status[bot] = data
                await broadcast(bot, data)


async def run_ws_server():
    """Start the WebSocket server and VPS poller."""
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        print(f"WebSocket server on ws://localhost:{WS_PORT}/ws")
        await poll_vps_status()


# --- Events fetcher (for command_center.html) ---

def _read_local_events(local_path: str) -> list:
    """Read events from local events.jsonl file."""
    events = []
    if os.path.exists(local_path):
        with open(local_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return events


def _read_vps_events() -> list:
    """Read events from VPS via SSH. Returns empty list on failure."""
    try:
        result = subprocess.run(
            VPS_SSH_SYNC + ["cat ~/hyperliquid-sol/events.jsonl 2>/dev/null"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            events = []
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return events
    except (subprocess.TimeoutExpired, Exception):
        pass
    return []


def _merge_events(local: list, vps: list) -> list:
    """Merge and deduplicate events from local and VPS sources."""
    seen = set()
    for e in local:
        seen.add((e.get("source", ""), e.get("ts", ""), e.get("type", "")))
    for e in vps:
        key = (e.get("source", ""), e.get("ts", ""), e.get("type", ""))
        if key not in seen:
            local.append(e)
            seen.add(key)
    return local


def fetch_vps_events():
    """Fetch events.jsonl from VPS, parse into categorized arrays.
    Returns dict with fills, statuses, trips, risks, contexts, allEvents.
    Note: SSH calls are synchronous — caller should run in a thread pool
    to avoid blocking the HTTP server."""
    now = time.time()
    if cached_events["data"] and (now - cached_events["fetched_at"]) < EVENTS_CACHE_TTL:
        return cached_events["data"]

    local_path = os.path.join(BASE_DIR, "events.jsonl")
    raw_events = _merge_events(_read_local_events(local_path), _read_vps_events())

    # Sort chronologically
    raw_events.sort(key=lambda e: e.get("ts", ""))

    # Categorize
    fills, statuses, trips, risks, contexts, all_events = [], [], [], [], [], []
    for e in raw_events:
        t = e.get("type", "")
        ts = e.get("ts", "")

        # allEvents entry (flat schema command_center expects)
        all_events.append({
            "ts": ts, "type": t,
            "coin": e.get("coin", ""),
            "side": e.get("side", ""),
            "price": e.get("price", 0),
            "size": e.get("size", 0),
            "fee": e.get("fee", 0),
            "pnl": e.get("closed_pnl", 0) or e.get("net_pnl", 0) or 0,
            "reason": e.get("reason", ""),
            "trip_num": e.get("trip_num", 0),
            "message": e.get("message", ""),
        })

        if t == "fill":
            fills.append({
                "ts": ts, "coin": e.get("coin", ""),
                "price": e.get("price", 0), "side": e.get("side", ""),
                "size": e.get("size", 0), "fee": e.get("fee", 0),
                "pnl": e.get("closed_pnl", 0),
            })
        elif t == "status":
            statuses.append({
                "ts": ts, "portfolio": e.get("portfolio", 0),
                "pnl": e.get("pnl", 0), "fills": e.get("fills", 0),
            })
        elif t == "trip":
            trips.append({
                "ts": ts, "coin": e.get("coin", ""),
                "net_pnl": e.get("net_pnl", 0),
                "trip_num": e.get("trip_num", 0),
            })
        elif t == "risk":
            risks.append({
                "ts": ts, "coin": e.get("coin", ""),
                "reason": e.get("reason", ""),
            })

    data = {
        "fills": fills, "statuses": statuses, "trips": trips,
        "risks": risks, "contexts": contexts, "allEvents": all_events,
        "fetched_at": now,
    }
    cached_events["data"] = data
    cached_events["fetched_at"] = now
    return data


# --- HTTP server (runs in a thread, serves from latest_status) ---

_events_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0]

        # Serve events from VPS events.jsonl (for command_center.html)
        if path == "/events/all.json":
            try:
                data = fetch_vps_events()
                payload = json.dumps(data)
            except Exception as ex:
                payload = json.dumps({"error": str(ex)})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())
            return

        # Serve live status from VPS cache
        bot_map = {
            "/status/sol.json": "sol",
            "/status/btc.json": "btc",
            "/status/eth.json": "eth",
            "/status/all.json": None,
            "/trader_status.json": None,
        }
        if path in bot_map:
            bot = bot_map[path]
            if bot and bot in latest_status:
                data = json.dumps(latest_status[bot])
            elif bot is None and latest_status:
                data = json.dumps(list(latest_status.values())[0])
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode())
            return

        if path in ("/", "/dashboard", "/dashboard.html"):
            self.path = "/dashboard.html"

        super().do_GET()

    def log_message(self, format, *args):
        pass


def run_http_server():
    """Run HTTP server in a thread."""
    server = http.server.HTTPServer(("", HTTP_PORT), Handler)
    print(f"HTTP server on http://localhost:{HTTP_PORT}")
    server.serve_forever()


# --- Main ---

if __name__ == "__main__":
    print("Dashboard server starting...")
    print(f"Polling VPS at {VPS_HOST} every {POLL_INTERVAL}s")

    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    try:
        asyncio.run(run_ws_server())
    except KeyboardInterrupt:
        print("\nServer stopped")
