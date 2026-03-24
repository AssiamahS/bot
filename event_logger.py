#!/usr/bin/env python3
"""
Unified Event Logger — stitches ALL trading data into one timeline.

Sources:
  1. telegram_history.jsonl  — Mac archive, Mar 7-16 (Telegram bot messages)
  2. perf_journal.jsonl      — VPS, slywatch snapshots (code-change perf captures)
  3. trader.py live events   — imported as a module for future live logging

Output: events.jsonl — one JSON line per event, chronological, unified schema.

Event types:
  bot_start, bot_stop, bot_connect, fill, status, risk, trip, error,
  slywatch_snapshot, code_change, other

Usage:
  # Build full timeline from archives:
  python3 event_logger.py build [--telegram FILE] [--perf FILE] [--output FILE]

  # Show timeline summary:
  python3 event_logger.py summary [--input FILE]

  # Live mode (appends events as they happen):
  python3 event_logger.py live [--output FILE]
"""

import json
import re
import sys
import os
import argparse
from datetime import datetime, timezone
from typing import Optional


EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.jsonl")


# ─── Telegram message parsers ────────────────────────────────────────────────

def parse_telegram_fill(msg: str, date: str) -> Optional[dict]:
    """Parse: 🔴 FILL: A 0.0056 ETH\n💰 @ $1974.00 | Fee: $0.0017 | PnL: $0.0000"""
    m = re.search(
        r'FILL:\s*(\w+)\s+([\d.]+)\s+(\w+).*?@\s*\$([\d.]+).*?(?:Fee|Rebate):\s*\+?\$?([\d.]+).*?PnL:\s*\$?([-\d.]+)',
        msg, re.DOTALL
    )
    if not m:
        # Fallback: try simpler pattern for format variations
        m = re.search(r'FILL.*?(\w+)\s+([\d.]+)\s+(\w+).*?@\s*\$([\d.]+)', msg, re.DOTALL)
        if not m:
            return {"type": "fill", "ts": date, "raw_parse_failed": True, "raw": msg[:200]}
        side_raw, size, coin, price = m.groups()
        fee, pnl = "0", "0"
    else:
        side_raw, size, coin, price, fee, pnl = m.groups()
    # 'A' = sell (Ask side), 'B' = buy (Bid side), or full words
    side = side_raw.upper()
    if side in ('A', 'SELL', 'S'):
        side = 'sell'
    elif side in ('B', 'BUY'):
        side = 'buy'
    else:
        side = side_raw.lower()

    return {
        "type": "fill",
        "ts": date,
        "coin": coin,
        "side": side,
        "size": float(size),
        "price": float(price),
        "fee": float(fee),
        "closed_pnl": float(pnl),
        "cost": round(float(size) * float(price), 4),
    }


def parse_telegram_status(msg: str, date: str) -> Optional[dict]:
    """Parse HL Status or Status Update messages."""
    data = {"type": "status", "ts": date}

    m = re.search(r'Portfolio:\s*\$([\d.]+)', msg)
    if m:
        data["portfolio"] = float(m.group(1))

    m = re.search(r'PnL:\s*([+-]?[\d.]+)', msg)
    if m:
        data["pnl"] = float(m.group(1))

    m = re.search(r'Fills:\s*(\d+)', msg)
    if m:
        data["fills"] = int(m.group(1))

    m = re.search(r'Uptime:\s*(\d+)m', msg)
    if m:
        data["uptime_min"] = int(m.group(1))

    # Extended status fields (later bot versions)
    m = re.search(r'Trips:\s*(\d+)', msg)
    if m:
        data["trips"] = int(m.group(1))

    m = re.search(r'Net:\s*\$([-\d.]+)', msg)
    if m:
        data["trip_net"] = float(m.group(1))

    m = re.search(r'WR:\s*(\d+)/(\d+)', msg)
    if m:
        data["win_rate_wins"] = int(m.group(1))
        data["win_rate_total"] = int(m.group(2))

    m = re.search(r'FeeR:\s*([\d.]+)', msg)
    if m:
        data["fee_ratio"] = float(m.group(1))

    return data


def parse_telegram_start(msg: str, date: str) -> Optional[dict]:
    """Parse bot start messages."""
    data = {"type": "bot_start", "ts": date}

    if 'Kraken' in msg:
        data["exchange"] = "kraken"
    elif 'Hyperliquid' in msg or 'HL' in msg:
        data["exchange"] = "hyperliquid"

    m = re.search(r'Pairs?:\s*(.+?)(?:\n|$)', msg)
    if m:
        data["pairs"] = [p.strip() for p in m.group(1).split(',')]

    m = re.search(r'Size:\s*\$([\d.]+)', msg)
    if m:
        data["order_size_usd"] = float(m.group(1))

    # Mode (strict/normal)
    m = re.search(r'Mode:\s*(\w+)', msg)
    if m:
        data["mode"] = m.group(1)

    return data


def parse_telegram_stop(msg: str, date: str) -> Optional[dict]:
    data = {"type": "bot_stop", "ts": date}
    m = re.search(r'Portfolio:\s*\$([\d.]+)', msg)
    if m:
        data["portfolio"] = float(m.group(1))
    m = re.search(r'fills:\s*(\d+)', msg, re.IGNORECASE)
    if m:
        data["fills"] = int(m.group(1))
    return data


def parse_telegram_risk(msg: str, date: str) -> Optional[dict]:
    data = {"type": "risk", "ts": date}
    m = re.search(r'RISK\s+PAUSE\s+(\w+):\s*(.+?)(?:\n|$)', msg)
    if m:
        data["coin"] = m.group(1)
        data["reason"] = m.group(2).strip()
    m = re.search(r'Cooldown\s+(\d+)s', msg)
    if m:
        data["cooldown_secs"] = int(m.group(1))
    return data


def parse_telegram_trip(msg: str, date: str) -> Optional[dict]:
    data = {"type": "trip", "ts": date}
    m = re.search(r'Trip\s*#?(\d+)\s*(\w+)?', msg)
    if m:
        data["trip_num"] = int(m.group(1))
        if m.group(2):
            data["coin"] = m.group(2)
    # Trip summary with PnL
    m = re.search(r'Net:\s*\$([-\d.]+)', msg)
    if m:
        data["net_pnl"] = float(m.group(1))
    return data


def parse_telegram_error(msg: str, date: str) -> Optional[dict]:
    data = {"type": "error", "ts": date}
    m = re.search(r'Error[:\s]*(.+?)(?:\n|$)', msg)
    if m:
        data["message"] = m.group(1).strip()
    else:
        data["message"] = msg[:200]
    return data


def classify_telegram(msg: str) -> str:
    """Classify a telegram message into event type."""
    if 'FILL' in msg:
        return 'fill'
    elif 'Bot Started' in msg or 'Kraken Bot Started' in msg or 'HL Bot Started' in msg:
        return 'bot_start'
    elif 'Bot Stopped' in msg or 'Stopped' in msg:
        return 'bot_stop'
    elif 'connected' in msg.lower() and 'bot' in msg.lower():
        return 'bot_connect'
    elif 'HL Status' in msg or 'Status Update' in msg:
        return 'status'
    elif 'RISK' in msg:
        return 'risk'
    elif 'Trip' in msg or 'Round Trip' in msg:
        return 'trip'
    elif 'Error' in msg:
        return 'error'
    return 'other'


def parse_telegram_message(record: dict) -> Optional[dict]:
    """Parse a single telegram_history.jsonl record into a unified event."""
    if record.get('sender') != 'Sly_KillSwitch_Bot':
        return None

    msg = record['message']
    date = record['date']
    event_type = classify_telegram(msg)

    parsers = {
        'fill': parse_telegram_fill,
        'bot_start': parse_telegram_start,
        'bot_stop': parse_telegram_stop,
        'status': parse_telegram_status,
        'risk': parse_telegram_risk,
        'trip': parse_telegram_trip,
        'error': parse_telegram_error,
    }

    parser = parsers.get(event_type)
    if parser:
        event = parser(msg, date)
    else:
        event = {"type": event_type, "ts": date}

    if event:
        event["source"] = "telegram"
        event["source_id"] = record.get("id")
        event["raw"] = msg[:500]

    return event


# ─── Slywatch perf_journal parser ────────────────────────────────────────────

def parse_perf_journal_entry(record: dict) -> dict:
    """Parse a single perf_journal.jsonl record into a unified event."""
    perf = record.get("perf", {})
    return {
        "type": "slywatch_snapshot",
        "ts": record.get("timestamp", ""),
        "source": "slywatch",
        "commit": record.get("commit", ""),
        "files_changed": record.get("files", []),
        "portfolio": perf.get("portfolio"),
        "pnl": perf.get("pnl"),
        "bot_net_pnl": perf.get("bot_net_pnl"),
        "true_pnl": perf.get("true_pnl"),
        "trips": perf.get("trips"),
        "trip_net": perf.get("trip_net"),
        "trip_avg_net": perf.get("trip_avg_net"),
        "win_rate": perf.get("win_rate"),
        "fills": perf.get("fills"),
        "avg_edge": perf.get("avg_edge"),
        "uptime_min": perf.get("uptime_min"),
        "pairs": perf.get("pairs"),
        "mode": perf.get("mode"),
    }


# ─── Live event logger (for trader.py integration) ───────────────────────────

class EventLogger:
    """Append-only event logger for live trading.

    Usage in trader.py:
        from event_logger import EventLogger
        logger = EventLogger("events.jsonl")
        logger.log_fill(coin="SOL", side="buy", size=0.5, price=130.0, fee=-0.0002, closed_pnl=0.01)
        logger.log_status(portfolio=129.5, pnl=-0.08, fills=27, uptime_min=3400)
        logger.log_start(exchange="hyperliquid", pairs=["DYDX-PERP"], order_size_usd=10.5, mode="strict")
        logger.log_stop(portfolio=129.5, fills=27)
        logger.log_risk(coin="SOL", reason="drawdown 5.6%", cooldown_secs=30)
        logger.log_trip(trip_num=16, coin="SOL", net_pnl=0.003)
        logger.log_error("something broke")
    """

    def __init__(self, path: str = None):
        self.path = path or EVENTS_FILE

    def _write(self, event: dict):
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        event["source"] = "live"
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception as e:
            print(f"EventLogger write error: {e}")

    def log_fill(self, coin: str, side: str, size: float, price: float,
                 fee: float = 0, closed_pnl: float = 0):
        self._write({
            "type": "fill",
            "coin": coin,
            "side": side,
            "size": size,
            "price": price,
            "fee": fee,
            "closed_pnl": closed_pnl,
            "cost": round(size * price, 4),
        })

    def log_status(self, portfolio: float, pnl: float, fills: int,
                   uptime_min: float, **extra):
        event = {
            "type": "status",
            "portfolio": portfolio,
            "pnl": pnl,
            "fills": fills,
            "uptime_min": round(uptime_min, 1),
        }
        event.update(extra)
        self._write(event)

    def log_start(self, exchange: str, pairs: list, order_size_usd: float,
                  mode: str = None, **extra):
        event = {
            "type": "bot_start",
            "exchange": exchange,
            "pairs": pairs,
            "order_size_usd": order_size_usd,
        }
        if mode:
            event["mode"] = mode
        event.update(extra)
        self._write(event)

    def log_stop(self, portfolio: float, fills: int):
        self._write({
            "type": "bot_stop",
            "portfolio": portfolio,
            "fills": fills,
        })

    def log_risk(self, coin: str, reason: str, cooldown_secs: int = 30):
        self._write({
            "type": "risk",
            "coin": coin,
            "reason": reason,
            "cooldown_secs": cooldown_secs,
        })

    def log_trip(self, trip_num: int, coin: str = None, net_pnl: float = None):
        event = {"type": "trip", "trip_num": trip_num}
        if coin:
            event["coin"] = coin
        if net_pnl is not None:
            event["net_pnl"] = net_pnl
        self._write(event)

    def log_error(self, message: str):
        self._write({"type": "error", "message": message[:500]})

    def log_custom(self, event_type: str, **data):
        data["type"] = event_type
        self._write(data)


TS_DISPLAY_LEN = 19  # "2026-03-07T05:47:35" — trim fractional seconds + tz for display


def read_jsonl(path: str) -> list[dict]:
    """Read a JSONL file, skipping blank lines and parse errors."""
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


# ─── Build unified timeline ──────────────────────────────────────────────────

def normalize_ts(ts_str: str) -> str:
    """Normalize any timestamp to ISO 8601 for sorting."""
    if not ts_str:
        return ""
    # Already ISO
    if 'T' in str(ts_str):
        return str(ts_str)
    # Telegram format: "2026-03-07 05:47:35+00:00"
    try:
        dt = datetime.fromisoformat(str(ts_str))
        return dt.isoformat()
    except Exception:
        return str(ts_str)


def build_timeline(telegram_path: str = None, perf_path: str = None,
                   output_path: str = None) -> list:
    """Parse all sources and write unified events.jsonl."""
    events = []

    # 1. Telegram history
    if telegram_path and os.path.exists(telegram_path):
        print(f"Reading telegram: {telegram_path}")
        count = 0
        for record in read_jsonl(telegram_path):
            event = parse_telegram_message(record)
            if event:
                event["ts"] = normalize_ts(event.get("ts", ""))
                events.append(event)
                count += 1
        print(f"  -> {count} events from telegram")

    # 2. Slywatch perf journal
    if perf_path and os.path.exists(perf_path):
        print(f"Reading perf_journal: {perf_path}")
        count = 0
        for record in read_jsonl(perf_path):
            event = parse_perf_journal_entry(record)
            event["ts"] = normalize_ts(event.get("ts", ""))
            events.append(event)
            count += 1
        print(f"  -> {count} events from slywatch")

    # 3. Existing events.jsonl (live events, append mode)
    output_path = output_path or EVENTS_FILE
    if os.path.exists(output_path):
        print(f"Reading existing events: {output_path}")
        count = 0
        for event in read_jsonl(output_path):
            if event.get("source") == "live":
                events.append(event)
                count += 1
        print(f"  -> {count} existing live events preserved")

    # Sort by timestamp
    events.sort(key=lambda e: e.get("ts", ""))

    # Deduplicate (same source + source_id, or same ts + type + source within 1s)
    seen = set()
    deduped = []
    for e in events:
        key = (e.get("source"), e.get("source_id"), e.get("ts"), e.get("type"))
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    # Write
    with open(output_path, "w") as f:
        for event in deduped:
            f.write(json.dumps(event, default=str) + "\n")

    print(f"\nWrote {len(deduped)} events to {output_path}")
    return deduped


# ─── Summary ─────────────────────────────────────────────────────────────────

def print_type_source_breakdown(source_counts: dict, type_counts: dict):
    """Print source and type breakdown tables."""
    print()
    print("  By source:")
    for s, c in sorted(source_counts.items()):
        print(f"    {s:20s} {c:>6d}")
    print()
    print("  By type:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {t:20s} {c:>6d}")


def print_fill_stats(events: list):
    """Print fill statistics from events."""
    fills = [e for e in events if e.get("type") == "fill"]
    if not fills:
        return
    total_cost = sum(e.get("cost", 0) for e in fills)
    total_fees = sum(e.get("fee", 0) for e in fills)
    total_pnl = sum(e.get("closed_pnl", 0) for e in fills)
    print()
    print(f"  Fill stats:")
    print(f"    Total fills:   {len(fills)}")
    print(f"    Total volume:  ${total_cost:,.2f}")
    print(f"    Total fees:    ${total_fees:.4f}")
    print(f"    Total PnL:     ${total_pnl:.4f}")


def show_summary(input_path: str = None):
    """Print a summary of the event timeline."""
    path = input_path or EVENTS_FILE
    if not os.path.exists(path):
        print(f"No events file at {path}")
        return

    events = read_jsonl(path)

    if not events:
        print("No events found.")
        return

    # Count by type and source
    type_counts = {}
    source_counts = {}
    for e in events:
        t = e.get("type", "unknown")
        s = e.get("source", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
        source_counts[s] = source_counts.get(s, 0) + 1

    first_ts = events[0].get("ts", "?")
    last_ts = events[-1].get("ts", "?")

    print(f"{'='*60}")
    print(f"  Event Timeline Summary")
    print(f"{'='*60}")
    print(f"  Total events: {len(events)}")
    print(f"  Date range:   {first_ts[:TS_DISPLAY_LEN]} -> {last_ts[:TS_DISPLAY_LEN]}")

    print_type_source_breakdown(source_counts, type_counts)
    print_fill_stats(events)

    # Bot sessions
    starts = sum(1 for e in events if e.get("type") == "bot_start")
    stops = sum(1 for e in events if e.get("type") == "bot_stop")
    print()
    print(f"  Bot sessions: {starts} starts, {stops} stops")

    # Latest portfolio
    statuses = [e for e in events if e.get("type") in ("status", "slywatch_snapshot") and e.get("portfolio")]
    if statuses:
        latest = statuses[-1]
        print(f"  Latest portfolio: ${latest.get('portfolio', 0):.2f} (as of {latest.get('ts', '?')[:TS_DISPLAY_LEN]})")

    print(f"{'='*60}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Unified Event Logger for HL trading bot")
    sub = parser.add_subparsers(dest="command")

    # build
    build_cmd = sub.add_parser("build", help="Build unified timeline from archives")
    build_cmd.add_argument("--telegram", default=None, help="Path to telegram_history.jsonl")
    build_cmd.add_argument("--perf", default=None, help="Path to perf_journal.jsonl")
    build_cmd.add_argument("--output", default=None, help="Output path (default: events.jsonl)")

    # summary
    sum_cmd = sub.add_parser("summary", help="Show timeline summary")
    sum_cmd.add_argument("--input", default=None, help="Path to events.jsonl")

    args = parser.parse_args()

    if args.command == "build":
        telegram = args.telegram
        perf = args.perf
        # Auto-detect paths
        base = os.path.dirname(os.path.abspath(__file__))
        if not telegram:
            candidate = os.path.join(base, "telegram_history.jsonl")
            if os.path.exists(candidate):
                telegram = candidate
        if not perf:
            candidate = os.path.join(base, "perf_journal.jsonl")
            if os.path.exists(candidate):
                perf = candidate

        if not telegram and not perf:
            print("No source files found. Provide --telegram and/or --perf paths.")
            print("Example: python3 event_logger.py build --telegram telegram_history.jsonl --perf perf_journal.jsonl")
            sys.exit(1)

        build_timeline(telegram, perf, args.output)

    elif args.command == "summary":
        show_summary(args.input)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
