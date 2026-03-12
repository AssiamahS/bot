#!/usr/bin/env python3
"""
Hummingbot P&L Monitor — runs every hour via launchd
Reads hummingbot SQLite DBs and sends macOS notifications.
"""

import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path.home() / "hummingbot" / "data"
LOG_FILE = Path.home() / "hummingbot" / "logs" / "monitor.log"
STRATEGIES = ["conf_pure_mm_1", "conf_pure_mm_eth", "conf_pure_mm_sol"]

# Hummingbot stores price * 1e6 and amount * 1e6
SCALE = 1_000_000


def notify(title, message):
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "{title}"'
    ], capture_output=True)


def get_trades(db_path, hours=None):
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        if hours:
            cutoff = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
            rows = conn.execute(
                "SELECT * FROM TradeFill WHERE timestamp > ? ORDER BY timestamp DESC",
                (cutoff,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM TradeFill ORDER BY timestamp DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def calc_pnl(trades):
    buy_cost = 0
    sell_revenue = 0
    buy_count = 0
    sell_count = 0
    total_fees = 0

    for t in trades:
        if "error" in t:
            continue
        price = t["price"] / SCALE
        amount = t["amount"] / SCALE
        fee = t.get("trade_fee_in_quote", 0) / SCALE
        total_fees += fee

        if t["trade_type"] == "BUY":
            buy_cost += price * amount
            buy_count += 1
        else:
            sell_revenue += price * amount
            sell_count += 1

    return {
        "buys": buy_count,
        "sells": sell_count,
        "total": buy_count + sell_count,
        "buy_cost": buy_cost,
        "sell_revenue": sell_revenue,
        "fees": total_fees,
        "net": sell_revenue - buy_cost - total_fees,
    }


def check_running():
    result = subprocess.run(
        ["pgrep", "-f", "hummingbot_quickstart"],
        capture_output=True, text=True
    )
    return len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0


def run_monitor():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"\n{'='*50}", f"MONITOR — {now}", f"{'='*50}"]

    running = check_running()
    lines.append(f"Instances: {running}")

    if running == 0:
        notify("Hummingbot DOWN", "No instances running!")
        lines.append("WARNING: No hummingbot processes!")

    total_net = 0
    total_hour_trades = 0

    for strat in STRATEGIES:
        db_path = DATA_DIR / f"{strat}.sqlite"
        lines.append(f"\n--- {strat.replace('conf_pure_mm_', '').upper()} ---")

        if not db_path.exists():
            lines.append("  Not started yet")
            continue

        # Lifetime
        all_trades = get_trades(db_path)
        lt = calc_pnl(all_trades)
        total_net += lt["net"]
        lines.append(f"  Lifetime: {lt['total']} trades | Net: ${lt['net']:.4f} | Fees: ${lt['fees']:.4f}")

        # Last hour
        recent = get_trades(db_path, hours=1)
        hr = calc_pnl(recent)
        total_hour_trades += hr["total"]
        lines.append(f"  Last hr:  {hr['total']} trades | Net: ${hr['net']:.4f}")

        # Show last 3 trades
        for t in recent[:3]:
            if "error" not in t:
                p = t["price"] / SCALE
                a = t["amount"] / SCALE
                lines.append(f"    {t['trade_type']} {a:.6f} @ ${p:.2f}")

    lines.append(f"\n{'='*50}")
    lines.append(f"TOTAL NET: ${total_net:.4f} | Hour trades: {total_hour_trades}")
    lines.append(f"{'='*50}")

    report = "\n".join(lines)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(report + "\n")

    print(report)

    # Notifications
    if total_net < -1.0:
        notify("Hummingbot LOSING", f"Net: ${total_net:.2f} — check strategies!")
    elif total_hour_trades > 0:
        notify("Hummingbot", f"{total_hour_trades} trades/hr | Net: ${total_net:.4f}")
    elif running > 0:
        notify("Hummingbot", f"No trades last hour. Net: ${total_net:.4f}")


if __name__ == "__main__":
    run_monitor()
