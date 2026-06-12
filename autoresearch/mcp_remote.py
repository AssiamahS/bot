#!/usr/bin/env python3
"""Remote MCP server for the funding-harvest bots.

Streamable-HTTP MCP exposed through a cloudflared tunnel so claude.ai
(web + iOS app) can read bot state as a custom connector.

Read-only by design: status, logs, fills, funding. No order placement,
no arbitrary file reads, no shell. Secrets in config.json (private key,
telegram token) are never returned by any tool.

Auth: the MCP endpoint lives under a random URL path (see SECRET below).
Anyone without the full URL gets 404s from starlette routing.
"""

import json
import re
import subprocess
import time
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "live_logs"
CONFIG = HERE.parent / "config.json"
API = "https://api.hyperliquid.xyz/info"

# random path segment = the auth token, kept out of git (repo is public).
# Rotate by deleting mcp_secret.txt, restarting, and updating the connector
# URL in claude.ai.
_SECRET_FILE = HERE / "mcp_secret.txt"
if not _SECRET_FILE.exists():
    import secrets as _secrets
    _SECRET_FILE.write_text(_secrets.token_urlsafe(24))
SECRET = _SECRET_FILE.read_text().strip()
PORT = 8765

COINS = ["xyz:SILVER", "xyz:MU", "xyz:NVDA", "xyz:AAPL", "xyz:TSLA"]

mcp = FastMCP(
    "hl-bot-remote",
    host="127.0.0.1",
    port=PORT,
    streamable_http_path=f"/{SECRET}/mcp",
    # quick-tunnel hostname changes on every restart and allowed_hosts has no
    # subdomain wildcard; the secret path is the gate, server binds loopback
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def wallet_address() -> str:
    return json.loads(CONFIG.read_text())["wallet_address"]


def post(payload: dict) -> dict | list:
    r = requests.post(API, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def slug(coin: str) -> str:
    return coin.replace(":", "_")


@mcp.tool()
def bot_status() -> str:
    """Live status: which bot processes are running, equity, and open
    positions on the xyz dex (TSLA/NVDA/AAPL/MU/SILVER funding bots)."""
    out = {}

    procs = subprocess.run(
        ["pgrep", "-afl", "live_funding.py"], capture_output=True, text=True
    ).stdout
    running = re.findall(r"--coin (\S+) --usd (\S+)", procs)
    out["bots_running"] = [{"coin": c, "usd": u} for c, u in running]
    out["bots_expected"] = COINS

    addr = wallet_address()
    spot = post({"type": "spotClearinghouseState", "user": addr})
    usdc_free = next(
        (float(b["total"]) - float(b["hold"])
         for b in spot.get("balances", []) if b["coin"] == "USDC"),
        0.0,
    )
    ch = post({"type": "clearinghouseState", "user": addr, "dex": "xyz"})
    out["equity"] = {
        "spot_usdc_free": round(usdc_free, 2),
        "xyz_perp_account_value": round(float(ch["marginSummary"]["accountValue"]), 2),
    }
    out["positions"] = [
        {
            "coin": p["position"]["coin"],
            "size": p["position"]["szi"],
            "entry": p["position"]["entryPx"],
            "unrealized_pnl": p["position"]["unrealizedPnl"],
            "funding_paid": p["position"]["cumFunding"]["sinceOpen"],
        }
        for p in ch.get("assetPositions", [])
    ]
    return json.dumps(out, indent=2)


@mcp.tool()
def funding_rates(top_n: int = 10) -> str:
    """Current hourly funding (and APY) for the bot's markets, plus the
    top |APY| markets across the whole xyz HIP-3 dex."""
    meta, ctxs = post({"type": "metaAndAssetCtxs", "dex": "xyz"})
    rows = []
    for a, c in zip(meta["universe"], ctxs):
        try:
            fr = float(c["funding"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({
            "coin": a["name"],
            "mid": c.get("midPx"),
            "funding_hr_pct": round(fr * 100, 5),
            "apy_pct": round(fr * 24 * 365 * 100, 1),
            "max_leverage": a.get("maxLeverage"),
        })
    ours = [r for r in rows if r["coin"] in COINS]
    hot = sorted(rows, key=lambda r: -abs(r["apy_pct"]))[:top_n]
    return json.dumps({"bot_markets": ours, "top_funding_xyz": hot}, indent=2)


@mcp.tool()
def recent_fills(hours: int = 24) -> str:
    """Trade fills on the bot wallet over the last N hours, with per-coin
    counts and net closed PnL."""
    addr = wallet_address()
    since = int((time.time() - hours * 3600) * 1000)
    fills = post({"type": "userFillsByTime", "user": addr, "startTime": since})
    summary: dict[str, dict] = {}
    for f in fills:
        s = summary.setdefault(f["coin"], {"fills": 0, "closed_pnl": 0.0, "fees": 0.0})
        s["fills"] += 1
        s["closed_pnl"] += float(f.get("closedPnl", 0))
        s["fees"] += float(f.get("fee", 0))
    for s in summary.values():
        s["closed_pnl"] = round(s["closed_pnl"], 4)
        s["fees"] = round(s["fees"], 4)
    recent = [
        {
            "time": time.strftime("%m-%d %H:%M", time.gmtime(f["time"] / 1000)),
            "coin": f["coin"],
            "side": f["side"],
            "px": f["px"],
            "sz": f["sz"],
            "closed_pnl": f.get("closedPnl"),
        }
        for f in fills[-30:]
    ]
    return json.dumps(
        {"hours": hours, "total_fills": len(fills), "by_coin": summary,
         "last_30_fills": recent},
        indent=2,
    )


@mcp.tool()
def tail_log(coin: str = "xyz:SILVER", lines: int = 40) -> str:
    """Tail the runner log for one bot. coin must be one of the bot's
    markets (xyz:SILVER, xyz:MU, xyz:NVDA, xyz:AAPL, xyz:TSLA)."""
    if coin not in COINS:
        return f"unknown coin {coin!r}; valid: {COINS}"
    path = LOG_DIR / f"{slug(coin)}_runner.log"
    if not path.exists():
        return f"no log at {path}"
    lines = max(1, min(int(lines), 400))
    text = path.read_text(errors="replace").splitlines()
    return "\n".join(text[-lines:])


@mcp.tool()
def perf_journal(coin: str = "", events: int = 60) -> str:
    """Recent structured bot events (opens/closes/quotes/errors) from the
    newest jsonl journal. Empty coin = all five markets interleaved."""
    coins = [coin] if coin in COINS else COINS
    rows = []
    for c in coins:
        files = sorted(LOG_DIR.glob(f"{slug(c)}_2*.jsonl"))
        if not files:
            continue
        for line in files[-1].read_text(errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: r.get("ts") or r.get("time") or "")
    events = max(1, min(int(events), 300))
    return json.dumps(rows[-events:], indent=2)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
