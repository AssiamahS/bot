#!/usr/bin/env python3
"""
Dashboard server for Hyperliquid multi-bot setup.
Serves dashboard.html and status JSON from each bot folder.
"""
import http.server
import json
import os
import re
import subprocess
from datetime import datetime

PORT = 8082
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATUS_MAP = {
    "/status/sol.json": os.path.expanduser("~/hyperliquid-sol/trader_status.json"),
    "/status/btc.json": os.path.expanduser("~/hyperliquid-btc/trader_status.json"),
    "/status/eth.json": os.path.expanduser("~/hyperliquid-eth/trader_status.json"),
    "/status/all.json": os.path.expanduser("~/hyperliquid/trader_status.json"),
    # Main dashboard endpoint — points to the active bot
    "/trader_status.json": os.path.expanduser("~/hyperliquid-sol/trader_status.json"),
}

BOT_REPO = os.path.expanduser("~/hyperliquid")
SOL_LOG = os.path.expanduser("~/hyperliquid-sol/sol.log")
TRADER_LOG = os.path.expanduser("~/hyperliquid-sol/trader.log")
LIVE_STATUS = os.path.expanduser("~/hyperliquid-sol/trader_status.json")

BRANCH_ORDER = [
    "main", "modes", "sol", "prof", "pong", "money", "pnl", "last",
    "cooldown", "catch", "beach", "throttle", "model", "final",
    "iWill", "nascar", "driver",
]


def run_cmd(cmd, cwd=None):
    try:
        out = subprocess.check_output(cmd, cwd=cwd, stderr=subprocess.DEVNULL, text=True)
        return out.strip()
    except Exception:
        return ""


def parse_fill_metrics(path):
    if not os.path.exists(path):
        return {"fills": 0, "fee_sum": 0.0, "closed_pnl_sum": 0.0, "net_after_fees": 0.0}
    pat = re.compile(r">>> FILL: .* fee=\$([\-0-9.]+) pnl=\$([\-0-9.]+)")
    fills, fee_sum, closed_sum = 0, 0.0, 0.0
    with open(path, "r", errors="ignore") as f:
        for line in f:
            m = pat.search(line)
            if m:
                fills += 1
                fee_sum += float(m.group(1))
                closed_sum += float(m.group(2))
    return {
        "fills": fills,
        "fee_sum": round(fee_sum, 6),
        "closed_pnl_sum": round(closed_sum, 6),
        "net_after_fees": round(closed_sum - fee_sum, 6),
    }


def load_live_status():
    if not os.path.exists(LIVE_STATUS):
        return {}
    try:
        with open(LIVE_STATUS, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def get_branch_meta():
    raw = run_cmd(
        [
            "git",
            "for-each-ref",
            "--sort=committerdate",
            "--format=%(refname:short)|%(objectname:short)|%(committerdate:iso8601)|%(subject)",
            "refs/remotes/origin",
        ],
        cwd=BOT_REPO,
    )
    out = {}
    for line in raw.splitlines():
        if not line.startswith("origin/"):
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        ref, sha, dt, subj = parts
        name = ref.replace("origin/", "", 1)
        out[name] = {
            "branch": name,
            "commit": sha,
            "commit_time": dt,
            "strategy_summary": subj,
        }
    return out


def make_branch_report():
    meta = get_branch_meta()
    status = load_live_status()
    trader = parse_fill_metrics(TRADER_LOG)
    sol = parse_fill_metrics(SOL_LOG)
    evidence = {
        "main": {
            "fills": trader["fills"],
            "closed_pnl_sum": trader["closed_pnl_sum"],
            "fee_sum": trader["fee_sum"],
            "net_after_fees": trader["net_after_fees"],
            "trip_avg_net": None,
            "trip_fee_ratio": None,
            "avg_edge_bps": None,
            "status": "measured_from_trader_log",
            "notes": "Legacy sample from trader.log",
        },
        "driver": {
            "fills": status.get("total_trade_count", 0) or 0,
            "closed_pnl_sum": None,
            "fee_sum": None,
            "net_after_fees": status.get("portfolio_pnl"),
            "trip_avg_net": status.get("trip_avg_net"),
            "trip_fee_ratio": status.get("trip_fee_ratio"),
            "avg_edge_bps": status.get("avg_edge_bps"),
            "status": "measured_from_live_status",
            "notes": "Strict gate active in current run",
        },
        "nascar": {
            "fills": sol["fills"],
            "closed_pnl_sum": sol["closed_pnl_sum"],
            "fee_sum": sol["fee_sum"],
            "net_after_fees": sol["net_after_fees"],
            "trip_avg_net": status.get("trip_avg_net"),
            "trip_fee_ratio": status.get("trip_fee_ratio"),
            "avg_edge_bps": status.get("avg_edge_bps"),
            "status": "measured_from_sol_log",
            "notes": "Shared log; branch attribution is best-effort",
        },
        "iWill": {
            "fills": sol["fills"],
            "closed_pnl_sum": sol["closed_pnl_sum"],
            "fee_sum": sol["fee_sum"],
            "net_after_fees": sol["net_after_fees"],
            "trip_avg_net": status.get("trip_avg_net"),
            "trip_fee_ratio": status.get("trip_fee_ratio"),
            "avg_edge_bps": status.get("avg_edge_bps"),
            "status": "measured_from_sol_log",
            "notes": "Tick-size correction era; best-effort from retained logs",
        },
    }
    rows = []
    for b in BRANCH_ORDER:
        row = dict(meta.get(b, {"branch": b, "commit": None, "commit_time": None, "strategy_summary": None}))
        row.update(
            evidence.get(
                b,
                {
                    "fills": None,
                    "closed_pnl_sum": None,
                    "fee_sum": None,
                    "net_after_fees": None,
                    "trip_avg_net": None,
                    "trip_fee_ratio": None,
                    "avg_edge_bps": None,
                    "status": "unknown_no_branch_tagged_log",
                    "notes": "Need branch-tagged logs for exact PnL/fills.",
                },
            )
        )
        rows.append(row)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": "https://github.com/AssiamahS/bot",
        "notes": [
            "Uses preserved local logs/status.",
            "Unknown rows are due to shared/overwritten logs across restarts.",
            "For exact branch report cards, run each branch with dedicated log files.",
        ],
        "rows": rows,
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0]

        # Serve status JSON files from bot folders
        if path in STATUS_MAP:
            status_path = STATUS_MAP[path]
            try:
                with open(status_path) as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data.encode())
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')
            return

        # Branch report JSON
        if path == "/branch-report.json":
            data = json.dumps(make_branch_report(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        # Serve dashboard at root
        if path == "/" or path == "/dashboard" or path == "/dashboard.html":
            self.path = "/dashboard.html"
        elif path == "/branch-report" or path == "/branch_report.html":
            self.path = "/branch_report.html"

        super().do_GET()

    def log_message(self, format, *args):
        pass  # quiet


if __name__ == "__main__":
    print(f"Dashboard server on http://localhost:{PORT}")
    print(f"Serving status from: sol, btc, eth bot folders")
    server = http.server.HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
