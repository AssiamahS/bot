#!/usr/bin/env python3
"""
Branch report server for Hyperliquid bot experiments.

Serves:
  - /                 -> branch_report.html
  - /branch-report.json -> branch report card data
  - /branch_report.html -> static UI
"""
import http.server
import json
import os
import re
import subprocess
from datetime import datetime

PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_REPO = os.path.expanduser("~/hyperliquid")
SOL_REPO = os.path.expanduser("~/hyperliquid-sol")
SOL_LOG = os.path.expanduser("~/hyperliquid-sol/sol.log")
TRADER_LOG = os.path.expanduser("~/hyperliquid-sol/trader.log")
STATUS_JSON = os.path.expanduser("~/hyperliquid-sol/trader_status.json")

# Branches requested for report card order
BRANCH_ORDER = [
    "main",
    "modes",
    "sol",
    "prof",
    "pong",
    "money",
    "pnl",
    "last",
    "cooldown",
    "catch",
    "beach",
    "throttle",
    "model",
    "final",
    "iWill",
    "nascar",
    "driver",
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
    fill_re = re.compile(r">>> FILL: .* fee=\$([\-0-9.]+) pnl=\$([\-0-9.]+)")
    fills = 0
    fee_sum = 0.0
    closed_sum = 0.0
    with open(path, "r", errors="ignore") as f:
        for line in f:
            m = fill_re.search(line)
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


def parse_gate_metrics(path):
    if not os.path.exists(path):
        return {"gate_lines": 0, "fills": 0}
    gate_re = re.compile(r"GATE \[")
    fill_re = re.compile(r">>> FILL:")
    gates = 0
    fills = 0
    with open(path, "r", errors="ignore") as f:
        for line in f:
            if gate_re.search(line):
                gates += 1
            if fill_re.search(line):
                fills += 1
    return {"gate_lines": gates, "fills": fills}


def load_status():
    if not os.path.exists(STATUS_JSON):
        return {}
    try:
        with open(STATUS_JSON, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def get_remote_branch_meta():
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
    result = {}
    for line in raw.splitlines():
        if not line.startswith("origin/"):
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        ref, sha, dt, subj = parts
        name = ref.replace("origin/", "", 1)
        result[name] = {
            "branch": name,
            "commit": sha,
            "commit_time": dt,
            "strategy_summary": subj,
        }
    return result


def build_branch_rows():
    branch_meta = get_remote_branch_meta()
    status = load_status()
    trader_metrics = parse_fill_metrics(TRADER_LOG)
    sol_metrics = parse_fill_metrics(SOL_LOG)
    gate_metrics = parse_gate_metrics(SOL_LOG)

    # Best-effort evidence map from preserved files only.
    # Anything else is unknown due to shared/overwritten logs.
    evidence_map = {
        "main": {
            "fills": trader_metrics["fills"],
            "closed_pnl_sum": trader_metrics["closed_pnl_sum"],
            "fee_sum": trader_metrics["fee_sum"],
            "net_after_fees": trader_metrics["net_after_fees"],
            "trip_avg_net": None,
            "trip_fee_ratio": None,
            "avg_edge_bps": None,
            "status": "measured_from_trader_log",
            "notes": "Legacy multi-pair run sample from trader.log.",
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
            "notes": "Strict profitability gate currently active.",
        },
        "nascar": {
            "fills": sol_metrics["fills"],
            "closed_pnl_sum": sol_metrics["closed_pnl_sum"],
            "fee_sum": sol_metrics["fee_sum"],
            "net_after_fees": sol_metrics["net_after_fees"],
            "trip_avg_net": status.get("trip_avg_net"),
            "trip_fee_ratio": status.get("trip_fee_ratio"),
            "avg_edge_bps": status.get("avg_edge_bps"),
            "status": "measured_from_sol_log",
            "notes": f"Gate lines seen: {gate_metrics['gate_lines']}",
        },
        "iWill": {
            "fills": sol_metrics["fills"],
            "closed_pnl_sum": sol_metrics["closed_pnl_sum"],
            "fee_sum": sol_metrics["fee_sum"],
            "net_after_fees": sol_metrics["net_after_fees"],
            "trip_avg_net": status.get("trip_avg_net"),
            "trip_fee_ratio": status.get("trip_fee_ratio"),
            "avg_edge_bps": status.get("avg_edge_bps"),
            "status": "measured_from_sol_log",
            "notes": "Tick-size correction era; current retained log has gate-dominant behavior.",
        },
    }

    rows = []
    for name in BRANCH_ORDER:
        meta = branch_meta.get(name, {"branch": name, "commit": None, "commit_time": None, "strategy_summary": None})
        ev = evidence_map.get(
            name,
            {
                "fills": None,
                "closed_pnl_sum": None,
                "fee_sum": None,
                "net_after_fees": None,
                "trip_avg_net": None,
                "trip_fee_ratio": None,
                "avg_edge_bps": None,
                "status": "unknown_no_branch_tagged_log",
                "notes": "Need per-branch run logs to compute exact PnL/fills.",
            },
        )
        row = {**meta, **ev}
        rows.append(row)
    return rows


def make_report():
    now = datetime.now().isoformat(timespec="seconds")
    rows = build_branch_rows()
    return {
        "generated_at": now,
        "repo": "https://github.com/AssiamahS/bot",
        "notes": [
            "This report uses preserved local logs/status only.",
            "Some branches show unknown metrics because logs were overwritten/shared across restarts.",
            "For exact per-branch PnL, run each branch with dedicated log files (branch+commit tagged).",
        ],
        "rows": rows,
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.path = "/branch_report.html"
            return super().do_GET()
        if path == "/branch-report.json":
            payload = make_report()
            body = json.dumps(payload, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, _fmt, *_args):
        pass


if __name__ == "__main__":
    print(f"Branch report server: http://localhost:{PORT}")
    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
