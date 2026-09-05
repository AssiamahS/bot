#!/usr/bin/env python3
"""Aggregate live_logs/ and write a dashboard HTML to ~/kim/dashboard.html.

Reads every <market>_<ts>.jsonl, summarizes per-market state + bot uptime + recent fills.
Pulls current account balance from HL public API.

Designed to be run via cron every 5 min."""

import json, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

LOG_DIR = Path(__file__).parent / "live_logs"
KIM = Path.home() / "kim"
ADDR = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())["wallet_address"]
HLP_VAULT = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
API = "https://api.hyperliquid.xyz/info"


def post(body):
    delay = 2
    for attempt in range(5):
        try:
            req = request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == 4:
                raise
            time.sleep(delay)
            delay *= 2


def get_balances():
    perp = post({"type": "clearinghouseState", "user": ADDR})
    spot = post({"type": "spotClearinghouseState", "user": ADDR})
    vaults = post({"type": "userVaultEquities", "user": ADDR})
    spot_usdc = next((float(b["total"]) for b in spot.get("balances", []) if b["coin"] == "USDC"), 0)
    perp_eq = float(perp["marginSummary"]["accountValue"])
    hlp_eq = next((float(v["equity"]) for v in vaults if v["vaultAddress"].lower() == HLP_VAULT.lower()), 0)
    other_vault_eq = sum(float(v["equity"]) for v in vaults if v["vaultAddress"].lower() != HLP_VAULT.lower())
    positions = perp.get("assetPositions", [])
    return {
        "spot_usdc": spot_usdc, "perp_eq": perp_eq,
        "hlp_eq": hlp_eq, "other_vaults_eq": other_vault_eq,
        "total": spot_usdc + perp_eq + hlp_eq + other_vault_eq,
        "positions": [{"coin": p["position"]["coin"], "szi": float(p["position"]["szi"]),
                       "entry": float(p["position"]["entryPx"] or 0),
                       "uPnl": float(p["position"]["unrealizedPnl"])} for p in positions],
    }


def get_running_bots():
    try:
        out = subprocess.check_output(["pgrep", "-af", "live_funding.py"], text=True)
        bots = []
        for line in out.strip().split("\n"):
            if "--coin" in line:
                pid = line.split()[0]
                coin = line.split("--coin")[1].split()[0]
                usd = line.split("--usd")[1].split()[0] if "--usd" in line else "?"
                bots.append({"pid": pid, "coin": coin, "usd": usd})
        return bots
    except subprocess.CalledProcessError:
        return []


def latest_market_state(slug: str) -> dict:
    files = sorted(LOG_DIR.glob(f"{slug}_2026*.jsonl"))
    if not files:
        return {}
    with files[-1].open() as f:
        last_line = ""
        for line in f:
            last_line = line
        if not last_line.strip():
            return {}
        try:
            return json.loads(last_line)
        except Exception:
            return {}


def render():
    bal = get_balances()
    bots = get_running_bots()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def fmt_pos(p):
        side = "LONG" if p["szi"] > 0 else "SHORT"
        cls = "pos" if p["uPnl"] >= 0 else "neg"
        return f'<tr><td>{p["coin"]}</td><td>{side} {abs(p["szi"]):.4f}</td><td>${p["entry"]:.2f}</td><td class="{cls}">${p["uPnl"]:+.2f}</td></tr>'

    pos_rows = "".join(fmt_pos(p) for p in bal["positions"]) or '<tr><td colspan="4" class="sub">no open positions</td></tr>'

    bot_rows = []
    for b in bots:
        slug = b["coin"].replace(":", "_")
        state = latest_market_state(slug)
        if state:
            apy = state.get("apy_now", 0)
            decision = state.get("decision", "?")
            ts = state.get("ts", "")[:19].replace("T", " ")
            cls = "pos" if "open" in decision else ("neg" if "close" in decision else "sub")
            bot_rows.append(f'<tr><td>{b["coin"]}</td><td>${b["usd"]}</td><td>{apy:+.1f}%</td><td class="{cls}">{decision}</td><td class="sub">{ts}</td></tr>')
        else:
            bot_rows.append(f'<tr><td>{b["coin"]}</td><td>${b["usd"]}</td><td colspan="3" class="sub">starting...</td></tr>')

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>kim · live dashboard</title>
<meta http-equiv="refresh" content="60">
<style>
  body {{ background:#0e1117; color:#e6e6e6; font:14px/1.55 -apple-system,system-ui,sans-serif; margin:0; padding:32px; max-width:1100px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  h2 {{ font-size:15px; color:#9fb0c0; font-weight:500; margin:24px 0 8px; }}
  .sub {{ color:#7d8a96; }}
  .big {{ font-size:36px; font-weight:600; margin:8px 0; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid #222; text-align:right; font-variant-numeric:tabular-nums; }}
  th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:last-child, td:last-child {{ text-align:left; }}
  th {{ background:#161b22; color:#9fb0c0; font-weight:500; }}
  .pos {{ color:#16c784; }} .neg {{ color:#ea3943; }}
  .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0; }}
  .card {{ background:#161b22; padding:12px 16px; border-radius:6px; }}
  .card .lbl {{ color:#7d8a96; font-size:12px; }}
  .card .val {{ font-size:18px; font-weight:600; font-variant-numeric:tabular-nums; }}
</style></head><body>
<h1>kim · live dashboard</h1>
<div class="sub">auto-refresh 60s · updated {now}</div>

<h2>Account</h2>
<div class="big pos">${bal["total"]:,.2f}</div>
<div class="grid">
  <div class="card"><div class="lbl">Spot USDC</div><div class="val">${bal["spot_usdc"]:,.2f}</div></div>
  <div class="card"><div class="lbl">Perp equity</div><div class="val">${bal["perp_eq"]:,.2f}</div></div>
  <div class="card"><div class="lbl">HLP vault</div><div class="val">${bal["hlp_eq"]:,.2f}</div></div>
  <div class="card"><div class="lbl">Other vaults</div><div class="val">${bal["other_vaults_eq"]:,.2f}</div></div>
</div>

<h2>Open positions</h2>
<table>
<thead><tr><th>Coin</th><th>Side / Size</th><th>Entry</th><th>Unrealized PnL</th></tr></thead>
<tbody>{pos_rows}</tbody></table>

<h2>Running bots ({len(bots)})</h2>
<table>
<thead><tr><th>Market</th><th>Size</th><th>Funding APY</th><th>Decision</th><th>Last update (UTC)</th></tr></thead>
<tbody>{''.join(bot_rows) or '<tr><td colspan="5" class="sub">no bots running</td></tr>'}</tbody></table>

<p class="sub">Reports: <a style="color:#5a9fd4" href="https://assiamahs.github.io/offprem/">offprem</a> · <a style="color:#5a9fd4" href="https://assiamahs.github.io/kim/">kim research</a></p>
</body></html>"""
    (KIM / "dashboard.html").write_text(html)
    print(f"wrote {KIM / 'dashboard.html'}  total=${bal['total']:.2f}  bots={len(bots)}  positions={len(bal['positions'])}")


if __name__ == "__main__":
    render()
