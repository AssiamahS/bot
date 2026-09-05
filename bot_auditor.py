#!/usr/bin/env python3
"""
Hyperliquid Bot Auditor - Monitors trading bot health and sends Telegram alerts.

Cron setup (every 4 hours, with 8am daily summary):
    crontab -e
    0 */4 * * * cd /root/hyperliquid-sol && /usr/bin/python3 bot_auditor.py >> /root/hyperliquid-sol/logs/auditor.log 2>&1

Environment variables (set in crontab or .env):
    TELEGRAM_BOT_TOKEN=your_bot_token
    TELEGRAM_CHAT_ID=your_chat_id
    HL_WALLET=0x253831C30282760880c4833E600e897f07AdC5aE
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("FATAL: requests not installed. Run: pip3 install requests")
    sys.exit(1)

# --- Configuration ---
HL_API = "https://api.hyperliquid.xyz/info"
STATE_FILE = Path.home() / ".bot_auditor_state.json"

WALLET = os.environ.get("HL_WALLET", "0x253831C30282760880c4833E600e897f07AdC5aE")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Alert thresholds
BALANCE_DROP_PCT = 5.0       # alert if balance drops more than this %
CONSECUTIVE_LOSSES = 3       # alert after N consecutive losing trades
IDLE_HOURS = 12              # alert if no trades in this many hours
CRITICAL_BALANCE = 80.0      # alert if balance drops below this
DAILY_SUMMARY_HOUR = 8       # UTC hour for daily summary


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}")


def send_telegram(message: str, parse_mode: str = "HTML"):
    """Send a Telegram message. Silently fails if credentials missing."""
    if not TG_TOKEN or not TG_CHAT_ID:
        log("WARNING: Telegram credentials not set, skipping alert")
        log(f"Message was: {message}")
        return False

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log(f"Telegram API error: {resp.status_code} {resp.text}")
            return False
        return True
    except Exception as e:
        log(f"Telegram send failed: {e}")
        return False


def hl_post(payload: dict) -> dict:
    """POST to Hyperliquid info API."""
    try:
        resp = requests.post(HL_API, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log(f"HL API error for {payload.get('type', '?')}: {e}")
        return {}


def get_perps_state() -> dict:
    return hl_post({"type": "clearinghouseState", "user": WALLET})


def get_spot_state() -> dict:
    return hl_post({"type": "spotClearinghouseState", "user": WALLET})


def get_fills() -> list:
    data = hl_post({"type": "userFills", "user": WALLET})
    if isinstance(data, list):
        return data
    return []


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log("WARNING: Corrupted state file, starting fresh")
    return {}


def save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f"ERROR: Failed to save state: {e}")


def parse_balance(perps_state: dict, spot_state: dict = None) -> float:
    """Extract total account value from perps + spot."""
    total = 0.0
    try:
        margin = perps_state.get("marginSummary", {})
        total += float(margin.get("accountValue", 0))
    except (ValueError, TypeError, KeyError):
        pass
    if spot_state:
        for b in spot_state.get("balances", []):
            if b.get("coin") == "USDC":
                total += float(b.get("total", 0))
    return total


def parse_positions(perps_state: dict) -> list:
    """Extract open positions."""
    positions = []
    for pos in perps_state.get("assetPositions", []):
        p = pos.get("position", {})
        coin = p.get("coin", "?")
        size = float(p.get("szi", 0))
        entry_px = float(p.get("entryPx", 0))
        unrealized_pnl = float(p.get("unrealizedPnl", 0))
        if abs(size) > 0:
            positions.append({
                "coin": coin,
                "size": size,
                "entry_px": entry_px,
                "unrealized_pnl": unrealized_pnl,
            })
    return positions


def analyze_fills(fills: list, since_ts: float = 0) -> dict:
    """Analyze recent fills for win/loss patterns."""
    if not fills:
        return {
            "total": 0, "recent": 0, "winners": 0, "losers": 0,
            "consecutive_losses": 0, "last_fill_ts": 0,
            "realized_pnl": 0.0, "coins_traded": set(),
        }

    # Fills come newest-first from API
    recent_fills = []
    for f in fills:
        fill_time = f.get("time", 0)
        if isinstance(fill_time, str):
            try:
                fill_time = int(fill_time)
            except ValueError:
                fill_time = 0
        # Convert ms to seconds if needed
        if fill_time > 1e12:
            fill_time = fill_time / 1000
        if fill_time > since_ts:
            recent_fills.append(f)

    # Count consecutive losses from most recent fills
    consecutive_losses = 0
    total_pnl = 0.0
    winners = 0
    losers = 0
    coins = set()
    last_fill_ts = 0

    for f in fills[:50]:  # Look at last 50 fills max
        pnl = float(f.get("closedPnl", 0))
        coin = f.get("coin", "?")
        coins.add(coin)

        fill_time = f.get("time", 0)
        if isinstance(fill_time, str):
            try:
                fill_time = int(fill_time)
            except ValueError:
                fill_time = 0
        if fill_time > 1e12:
            fill_time = fill_time / 1000

        if fill_time > last_fill_ts:
            last_fill_ts = fill_time

        if pnl != 0:
            total_pnl += pnl
            if pnl > 0:
                winners += 1
            else:
                losers += 1

    # Count consecutive losses from the top (most recent)
    for f in fills:
        pnl = float(f.get("closedPnl", 0))
        if pnl == 0:
            continue  # skip fills with no closed PnL (opens)
        if pnl < 0:
            consecutive_losses += 1
        else:
            break

    return {
        "total": len(fills),
        "recent": len(recent_fills),
        "winners": winners,
        "losers": losers,
        "consecutive_losses": consecutive_losses,
        "last_fill_ts": last_fill_ts,
        "realized_pnl": total_pnl,
        "coins_traded": coins,
    }


def format_usd(val: float) -> str:
    sign = "+" if val > 0 else ""
    return f"{sign}${val:.2f}"


def run_audit():
    log("Bot auditor starting...")
    now = time.time()
    now_utc = datetime.now(timezone.utc)

    # Load previous state
    prev_state = load_state()
    prev_balance = prev_state.get("last_balance", 0)
    prev_check_ts = prev_state.get("last_check_ts", 0)
    last_daily_date = prev_state.get("last_daily_date", "")
    alert_count = prev_state.get("alert_count", 0)

    # Fetch current data
    perps = get_perps_state()
    spot = get_spot_state()
    if not perps and not spot:
        send_telegram("!! Bot Auditor: Failed to fetch Hyperliquid data. API may be down.")
        log("ERROR: No data returned")
        return

    fills = get_fills()

    # Parse data (combine perps + spot USDC)
    balance = parse_balance(perps or {}, spot)
    positions = parse_positions(perps)
    fill_analysis = analyze_fills(fills, since_ts=prev_check_ts)

    log(f"Balance: ${balance:.2f} | Positions: {len(positions)} | Recent fills: {fill_analysis['recent']}")

    # --- Alert checks ---
    alerts = []

    # 1. Balance drop > threshold
    if prev_balance > 0:
        drop_pct = ((prev_balance - balance) / prev_balance) * 100
        if drop_pct >= BALANCE_DROP_PCT:
            alerts.append(
                f"<b>BALANCE DROP</b>: ${prev_balance:.2f} -> ${balance:.2f} "
                f"({drop_pct:.1f}% drop in last check interval)"
            )

    # 2. Consecutive losing trades
    if fill_analysis["consecutive_losses"] >= CONSECUTIVE_LOSSES:
        alerts.append(
            f"<b>LOSING STREAK</b>: {fill_analysis['consecutive_losses']} consecutive losing trades"
        )

    # 3. Bot idle
    if fill_analysis["last_fill_ts"] > 0:
        hours_since_trade = (now - fill_analysis["last_fill_ts"]) / 3600
        if hours_since_trade >= IDLE_HOURS:
            alerts.append(
                f"<b>BOT IDLE</b>: No trades in {hours_since_trade:.1f} hours"
            )
    elif prev_check_ts > 0:
        # No fills at all
        alerts.append("<b>BOT IDLE</b>: No fill history found")

    # 4. Critical balance
    if 0 < balance < CRITICAL_BALANCE:
        alerts.append(
            f"<b>CRITICAL BALANCE</b>: ${balance:.2f} (below ${CRITICAL_BALANCE:.0f} threshold)"
        )

    # Send alerts
    if alerts:
        header = f"!! HL Bot Alert ({now_utc.strftime('%H:%M UTC')})\n"
        body = "\n".join(f"- {a}" for a in alerts)

        pos_lines = ""
        if positions:
            pos_lines = "\n\n<b>Open positions:</b>\n"
            for p in positions:
                direction = "LONG" if p["size"] > 0 else "SHORT"
                pos_lines += (
                    f"  {p['coin']} {direction} "
                    f"size={abs(p['size']):.4f} "
                    f"pnl={format_usd(p['unrealized_pnl'])}\n"
                )

        msg = f"{header}\n{body}{pos_lines}"
        send_telegram(msg)
        alert_count += 1
        log(f"Sent {len(alerts)} alert(s)")

    # --- Daily summary ---
    today_str = now_utc.strftime("%Y-%m-%d")
    is_summary_hour = now_utc.hour == DAILY_SUMMARY_HOUR or (
        DAILY_SUMMARY_HOUR - 2 <= now_utc.hour <= DAILY_SUMMARY_HOUR + 2
        and last_daily_date != today_str
    )

    if last_daily_date != today_str and is_summary_hour:
        pnl_since_last = balance - prev_balance if prev_balance > 0 else 0
        pnl_emoji = "green" if pnl_since_last >= 0 else "red"

        coins_str = ", ".join(sorted(fill_analysis["coins_traded"])) if fill_analysis["coins_traded"] else "none"

        pos_summary = "None"
        if positions:
            pos_parts = []
            for p in positions:
                direction = "L" if p["size"] > 0 else "S"
                pos_parts.append(f"{p['coin']}({direction} {format_usd(p['unrealized_pnl'])})")
            pos_summary = ", ".join(pos_parts)

        win_rate = 0
        total_decided = fill_analysis["winners"] + fill_analysis["losers"]
        if total_decided > 0:
            win_rate = (fill_analysis["winners"] / total_decided) * 100

        summary = (
            f"<b>HL Bot Daily Summary</b> ({today_str})\n\n"
            f"<b>Balance:</b> ${balance:.2f}\n"
            f"<b>Change since last check:</b> {format_usd(pnl_since_last)}\n"
            f"<b>Win rate (last 50):</b> {win_rate:.0f}% ({fill_analysis['winners']}W / {fill_analysis['losers']}L)\n"
            f"<b>Consecutive losses:</b> {fill_analysis['consecutive_losses']}\n"
            f"<b>Realized PnL (last 50):</b> {format_usd(fill_analysis['realized_pnl'])}\n"
            f"<b>Coins traded:</b> {coins_str}\n"
            f"<b>Open positions:</b> {pos_summary}\n"
            f"<b>Alerts today:</b> {alert_count}"
        )
        send_telegram(summary)
        last_daily_date = today_str
        alert_count = 0  # reset daily
        log("Sent daily summary")

    # Save state
    new_state = {
        "last_balance": balance,
        "last_check_ts": now,
        "last_daily_date": last_daily_date,
        "alert_count": alert_count,
        "last_positions": [p["coin"] for p in positions],
        "history": prev_state.get("history", []),
    }

    # Append to history (keep last 90 entries = ~15 days at 4h intervals)
    new_state["history"].append({
        "ts": now,
        "balance": balance,
        "positions": len(positions),
        "fills_since_last": fill_analysis["recent"],
    })
    new_state["history"] = new_state["history"][-90:]

    save_state(new_state)
    log(f"State saved. Balance: ${balance:.2f}")


if __name__ == "__main__":
    try:
        run_audit()
    except Exception as e:
        log(f"FATAL: {e}")
        # Try to send telegram alert even on crash
        try:
            send_telegram(f"!! Bot Auditor CRASHED: {e}")
        except Exception:
            pass
        sys.exit(1)
