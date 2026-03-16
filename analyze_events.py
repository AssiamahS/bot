#!/usr/bin/env python3
"""
Event analyzer + AI debugger for HL trading bot.
Reads events.jsonl and produces diagnostics.

Usage:
  python3 analyze_events.py                    # full analysis
  python3 analyze_events.py --last 1h          # last hour
  python3 analyze_events.py --coin SOL         # filter by coin
  python3 analyze_events.py --json             # machine-readable output
  python3 analyze_events.py --surface          # generate volatility surface HTML
"""

import json
import sys
import os
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.jsonl")


def load_events(path=None, since=None, coin=None):
    path = path or EVENTS_FILE
    if not os.path.exists(path):
        print(f"No events file at {path}")
        return []

    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue

            if since and e.get("ts", "") < since:
                continue
            if coin and e.get("coin", "") != coin:
                continue
            events.append(e)
    return events


def analyze_fills(events):
    fills = [e for e in events if e.get("type") == "fill"]
    if not fills:
        return {"count": 0}

    total_volume = sum(e.get("cost", e.get("size", 0) * e.get("price", 0)) for e in fills)
    total_fees = sum(e.get("fee", 0) for e in fills)
    total_pnl = sum(e.get("closed_pnl", 0) for e in fills)

    buys = [e for e in fills if e.get("side") in ("buy", "b")]
    sells = [e for e in fills if e.get("side") in ("sell", "a", "s")]

    # Per-coin breakdown
    by_coin = defaultdict(lambda: {"count": 0, "volume": 0, "fees": 0, "pnl": 0})
    for f in fills:
        c = f.get("coin", "?")
        by_coin[c]["count"] += 1
        by_coin[c]["volume"] += f.get("cost", 0)
        by_coin[c]["fees"] += f.get("fee", 0)
        by_coin[c]["pnl"] += f.get("closed_pnl", 0)

    return {
        "count": len(fills),
        "buys": len(buys),
        "sells": len(sells),
        "total_volume": round(total_volume, 2),
        "total_fees": round(total_fees, 6),
        "total_pnl": round(total_pnl, 6),
        "net_after_fees": round(total_pnl - total_fees, 6),
        "avg_fee_per_fill": round(total_fees / len(fills), 6) if fills else 0,
        "by_coin": dict(by_coin),
    }


def analyze_trips(events):
    trips = [e for e in events if e.get("type") == "trip"]
    if not trips:
        return {"count": 0}

    pnls = [t.get("net_pnl", 0) for t in trips if t.get("net_pnl") is not None]
    winners = [p for p in pnls if p >= 0]
    losers = [p for p in pnls if p < 0]

    by_coin = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0, "losses": 0})
    for t in trips:
        c = t.get("coin", "?")
        by_coin[c]["count"] += 1
        net = t.get("net_pnl", 0)
        by_coin[c]["pnl"] += net or 0
        if net and net >= 0:
            by_coin[c]["wins"] += 1
        elif net and net < 0:
            by_coin[c]["losses"] += 1

    return {
        "count": len(trips),
        "with_pnl": len(pnls),
        "total_pnl": round(sum(pnls), 6) if pnls else 0,
        "avg_pnl": round(sum(pnls) / len(pnls), 6) if pnls else 0,
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": round(len(winners) / len(pnls) * 100, 1) if pnls else 0,
        "best_trip": round(max(pnls), 6) if pnls else 0,
        "worst_trip": round(min(pnls), 6) if pnls else 0,
        "by_coin": dict(by_coin),
    }


def analyze_risk(events):
    risks = [e for e in events if e.get("type") == "risk"]
    if not risks:
        return {"count": 0}

    reasons = defaultdict(int)
    by_coin = defaultdict(int)
    for r in risks:
        reasons[r.get("reason", "unknown")] += 1
        by_coin[r.get("coin", "?")] += 1

    return {
        "count": len(risks),
        "by_reason": dict(reasons),
        "by_coin": dict(by_coin),
    }


def analyze_sessions(events):
    starts = [e for e in events if e.get("type") == "bot_start"]
    stops = [e for e in events if e.get("type") == "bot_stop"]
    errors = [e for e in events if e.get("type") == "error"]

    return {
        "starts": len(starts),
        "stops": len(stops),
        "errors": len(errors),
        "recent_errors": [e.get("message", "")[:100] for e in errors[-5:]],
    }


def analyze_edge(events):
    """Analyze fill edge data from fill_context events."""
    contexts = [e for e in events if e.get("type") == "fill_context"]
    if not contexts:
        return {"count": 0, "note": "no fill_context events yet (needs live data)"}

    edges = [c.get("edge_bps", 0) for c in contexts]
    positive = [e for e in edges if e > 0]
    negative = [e for e in edges if e < 0]

    return {
        "count": len(contexts),
        "avg_edge_bps": round(sum(edges) / len(edges), 2) if edges else 0,
        "positive_edge_pct": round(len(positive) / len(edges) * 100, 1) if edges else 0,
        "negative_edge_pct": round(len(negative) / len(edges) * 100, 1) if edges else 0,
        "avg_positive": round(sum(positive) / len(positive), 2) if positive else 0,
        "avg_negative": round(sum(negative) / len(negative), 2) if negative else 0,
    }


def ai_debug(fills_analysis, trips_analysis, risk_analysis, edge_analysis):
    """Rule-based AI debugger. Returns list of insights."""
    insights = []

    # Check fee ratio
    if fills_analysis["count"] > 0:
        fee_pct = abs(fills_analysis["total_fees"]) / max(fills_analysis["total_volume"], 1) * 10000
        if fee_pct > 3:
            insights.append(f"HIGH FEES: paying {fee_pct:.1f}bps avg per fill. Fees are eating into spread capture.")

    # Check win rate
    if trips_analysis.get("with_pnl", 0) > 5:
        wr = trips_analysis["win_rate"]
        if wr < 50:
            insights.append(f"LOW WIN RATE: {wr}%. Likely adverse selection or exit ladder too passive.")
        if wr > 70:
            insights.append(f"HIGH WIN RATE: {wr}% but check if avg winner is smaller than avg loser.")

    # Check PnL direction
    if trips_analysis.get("total_pnl", 0) < 0 and trips_analysis["count"] > 5:
        avg = trips_analysis["avg_pnl"]
        insights.append(f"NET NEGATIVE: avg trip PnL ${avg:.4f}. Strategy is losing money per round trip.")

    # Check risk blocks
    if risk_analysis["count"] > 10:
        insights.append(f"FREQUENT RISK BLOCKS: {risk_analysis['count']} pauses. Bot spending too much time in cooldown.")
        if risk_analysis.get("by_reason"):
            top = max(risk_analysis["by_reason"], key=risk_analysis["by_reason"].get)
            insights.append(f"  Top risk cause: {top} ({risk_analysis['by_reason'][top]} times)")

    # Check edge
    if edge_analysis.get("count", 0) > 5:
        avg_edge = edge_analysis.get("avg_edge_bps", 0)
        if avg_edge < 0:
            insights.append(f"NEGATIVE EDGE: avg {avg_edge:.1f}bps. Fills are happening at worse-than-mid prices (adverse selection).")
        pos_pct = edge_analysis.get("positive_edge_pct", 0)
        if pos_pct < 50:
            insights.append(f"POOR FILL QUALITY: only {pos_pct:.0f}% of fills have positive edge.")

    # Per-coin analysis
    if trips_analysis.get("by_coin"):
        for coin, stats in trips_analysis["by_coin"].items():
            if stats["count"] >= 3 and stats["pnl"] < 0:
                wr = stats["wins"] / stats["count"] * 100 if stats["count"] > 0 else 0
                insights.append(f"WEAK COIN: {coin} — {stats['count']} trips, PnL ${stats['pnl']:.4f}, WR {wr:.0f}%")

    if not insights:
        insights.append("No obvious issues detected. Collect more data for better analysis.")

    return insights


def print_report(events, as_json=False):
    fills = analyze_fills(events)
    trips = analyze_trips(events)
    risk = analyze_risk(events)
    sessions = analyze_sessions(events)
    edge = analyze_edge(events)
    insights = ai_debug(fills, trips, risk, edge)

    if as_json:
        print(json.dumps({
            "fills": fills,
            "trips": trips,
            "risk": risk,
            "sessions": sessions,
            "edge": edge,
            "insights": insights,
            "event_count": len(events),
        }, indent=2))
        return

    timestamps = [e.get("ts", "") for e in events if e.get("ts")]
    first = timestamps[0] if timestamps else "?"
    last = timestamps[-1] if timestamps else "?"

    print(f"{'='*60}")
    print(f"  HL Bot Event Analysis")
    print(f"  {len(events)} events | {first[:19]} -> {last[:19]}")
    print(f"{'='*60}")

    print(f"\n  FILLS")
    print(f"    Count:     {fills['count']} ({fills.get('buys',0)} buys / {fills.get('sells',0)} sells)")
    print(f"    Volume:    ${fills.get('total_volume', 0):,.2f}")
    print(f"    Fees:      ${fills.get('total_fees', 0):.4f}")
    print(f"    PnL:       ${fills.get('total_pnl', 0):.4f}")
    print(f"    Net:       ${fills.get('net_after_fees', 0):.4f}")
    if fills.get("by_coin"):
        for coin, stats in fills["by_coin"].items():
            print(f"      {coin:6s}  {stats['count']:3d} fills  vol=${stats['volume']:.2f}  pnl=${stats['pnl']:.4f}")

    print(f"\n  TRIPS")
    print(f"    Count:     {trips['count']}")
    if trips.get("with_pnl"):
        print(f"    Win Rate:  {trips['win_rate']}% ({trips['winners']}W / {trips['losers']}L)")
        print(f"    Avg PnL:   ${trips['avg_pnl']:.4f}")
        print(f"    Total PnL: ${trips['total_pnl']:.4f}")
        print(f"    Best:      ${trips['best_trip']:.4f}  |  Worst: ${trips['worst_trip']:.4f}")

    if edge.get("count", 0) > 0:
        print(f"\n  FILL QUALITY")
        print(f"    Avg edge:  {edge['avg_edge_bps']:.1f}bps")
        print(f"    Positive:  {edge['positive_edge_pct']:.0f}%  |  Negative: {edge['negative_edge_pct']:.0f}%")

    print(f"\n  SESSIONS")
    print(f"    Starts: {sessions['starts']}  |  Stops: {sessions['stops']}  |  Errors: {sessions['errors']}")

    if risk["count"] > 0:
        print(f"\n  RISK")
        print(f"    Blocks: {risk['count']}")
        for reason, count in risk.get("by_reason", {}).items():
            print(f"      {reason}: {count}")

    print(f"\n  {'='*56}")
    print(f"  AI DEBUGGER")
    print(f"  {'='*56}")
    for i, insight in enumerate(insights, 1):
        print(f"  {i}. {insight}")
    print(f"  {'='*56}")


def _compute_vol_surface(fills):
    """Pre-compute volatility surface data: rolling vol per coin in time buckets."""
    from collections import defaultdict
    import math

    by_coin = defaultdict(list)
    for f in fills:
        if f.get("price") and f.get("ts") and f.get("coin"):
            by_coin[f["coin"]].append({"ts": f["ts"], "price": f["price"]})

    coins = sorted(by_coin.keys())
    if not coins:
        return {"coins": [], "time_labels": [], "z": [], "fill_markers": []}

    # Sort each coin's fills by time
    for c in coins:
        by_coin[c].sort(key=lambda x: x["ts"])

    # Create time buckets (30-min windows)
    all_ts = sorted(set(f["ts"] for fs in by_coin.values() for f in fs))
    if not all_ts:
        return {"coins": [], "time_labels": [], "z": [], "fill_markers": []}

    # Bucket by hour
    buckets = []
    seen_buckets = set()
    for ts in all_ts:
        bucket = ts[:13]  # YYYY-MM-DDTHH
        if bucket not in seen_buckets:
            seen_buckets.add(bucket)
            buckets.append(bucket)
    buckets.sort()

    # Compute rolling volatility (price range / mean as bps) per coin per bucket
    z_matrix = []
    for coin in coins:
        row = []
        fills_for_coin = by_coin[coin]
        for bucket in buckets:
            bucket_fills = [f for f in fills_for_coin if f["ts"][:13] == bucket]
            if len(bucket_fills) >= 2:
                prices = [f["price"] for f in bucket_fills]
                mean_p = sum(prices) / len(prices)
                if mean_p > 0:
                    price_range = max(prices) - min(prices)
                    vol_bps = (price_range / mean_p) * 10000
                else:
                    vol_bps = 0
            elif len(bucket_fills) == 1:
                vol_bps = 0
            else:
                vol_bps = None  # no data
            row.append(vol_bps)
        z_matrix.append(row)

    return {
        "coins": coins,
        "time_labels": buckets,
        "z": z_matrix,
    }


def generate_surface_html(events, output_path=None):
    """Generate an interactive command center with 3D volatility surface."""
    fills = [e for e in events if e.get("type") == "fill"]
    statuses = [e for e in events if e.get("type") in ("status", "slywatch_snapshot")]
    trips = [e for e in events if e.get("type") == "trip"]
    contexts = [e for e in events if e.get("type") == "fill_context"]
    risks = [e for e in events if e.get("type") == "risk"]
    all_events = sorted(
        [e for e in events if e.get("type") in ("fill", "trip", "risk", "bot_start", "bot_stop", "error")],
        key=lambda e: e.get("ts", "")
    )

    # Pre-compute surface data server-side
    surface = _compute_vol_surface(fills)

    fills_json = json.dumps([{
        "ts": f.get("ts", ""), "coin": f.get("coin", ""),
        "price": f.get("price", 0), "side": f.get("side", ""),
        "size": f.get("size", 0), "fee": f.get("fee", 0),
        "pnl": f.get("closed_pnl", 0),
    } for f in fills])

    statuses_json = json.dumps([{
        "ts": s.get("ts", ""), "portfolio": s.get("portfolio", 0),
        "pnl": s.get("pnl", 0), "fills": s.get("fills", 0),
    } for s in statuses])

    trips_json = json.dumps([{
        "ts": t.get("ts", ""), "coin": t.get("coin", ""),
        "net_pnl": t.get("net_pnl", 0), "trip_num": t.get("trip_num", 0),
    } for t in trips])

    contexts_json = json.dumps([{
        "ts": c.get("ts", ""), "coin": c.get("coin", ""),
        "edge_bps": c.get("edge_bps", 0), "mid": c.get("mid", 0),
    } for c in contexts])

    risks_json = json.dumps([{
        "ts": r.get("ts", ""), "coin": r.get("coin", ""),
        "reason": r.get("reason", ""),
    } for r in risks])

    surface_json = json.dumps(surface)
    all_events_json = json.dumps([{
        "ts": e.get("ts", ""), "type": e.get("type", ""),
        "coin": e.get("coin", ""), "side": e.get("side", ""),
        "price": e.get("price", 0), "size": e.get("size", 0),
        "fee": e.get("fee", 0), "pnl": e.get("closed_pnl", e.get("net_pnl", 0)),
        "reason": e.get("reason", ""), "trip_num": e.get("trip_num", 0),
        "message": e.get("message", ""),
    } for e in all_events])

    html = f"""<!DOCTYPE html>
<html>
<head>
<title>HL Bot Command Center</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0a0a0f; color: #e0e0e0; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; }}
  .header {{ padding: 12px 20px; border-bottom: 1px solid #1a1a2e; display: flex; justify-content: space-between; align-items: center; }}
  .header h1 {{ font-size: 16px; color: #00ff88; }}
  .stats {{ display: flex; gap: 20px; }}
  .stat {{ text-align: center; }}
  .stat .val {{ font-size: 18px; font-weight: bold; }}
  .stat .lbl {{ color: #555; font-size: 10px; text-transform: uppercase; }}
  .green {{ color: #00ff88; }}
  .red {{ color: #ff4444; }}
  .yellow {{ color: #ffaa00; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: auto auto auto; gap: 1px; background: #1a1a2e; }}
  .panel {{ background: #0a0a0f; padding: 10px; }}
  .panel h3 {{ font-size: 11px; color: #555; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1px; }}
  .chart {{ width: 100%; height: 340px; }}
  .chart-tall {{ width: 100%; height: 420px; }}
  .full-width {{ grid-column: 1 / -1; }}
  .replay-bar {{ padding: 10px 20px; background: #0d0d14; border-top: 1px solid #1a1a2e; border-bottom: 1px solid #1a1a2e; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .replay-bar label {{ color: #555; font-size: 11px; text-transform: uppercase; }}
  .replay-bar input[type=range] {{ flex: 1; accent-color: #00ff88; min-width: 200px; }}
  .replay-bar .time-display {{ color: #00ff88; min-width: 160px; font-size: 12px; }}
  .replay-bar .state-display {{ color: #888; font-size: 11px; }}
  .range-btns {{ display: flex; gap: 4px; }}
  .range-btns button {{ background: #1a1a2e; color: #888; border: 1px solid #2a2a3e; padding: 4px 10px; font-size: 11px; font-family: inherit; cursor: pointer; border-radius: 3px; }}
  .range-btns button.active {{ background: #00ff88; color: #0a0a0f; border-color: #00ff88; font-weight: bold; }}
  .range-btns button:hover {{ border-color: #00ff88; }}
  .insights {{ padding: 12px 20px; border-top: 1px solid #1a1a2e; }}
  .insights h3 {{ font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }}
  .insight {{ padding: 6px 10px; margin: 3px 0; background: #111; border-left: 3px solid #ff8800; font-size: 12px; }}
  .insight.good {{ border-color: #00ff88; }}
  .timeline {{ max-height: 320px; overflow-y: auto; }}
  .timeline .ev {{ padding: 3px 6px; border-bottom: 1px solid #0d0d14; display: flex; gap: 10px; font-size: 11px; cursor: pointer; }}
  .timeline .ev:hover {{ background: #151520; }}
  .timeline .ev.highlighted {{ background: #1a1a30; border-left: 2px solid #00ff88; }}
  .timeline .ts {{ color: #444; min-width: 75px; }}
  .timeline .type {{ min-width: 55px; font-weight: bold; }}
  .fill {{ color: #4488ff; }}
  .trip {{ color: #ff8800; }}
  .risk {{ color: #ff4444; }}
  .error {{ color: #ff2222; }}
  .bot_start {{ color: #00ff88; }}
  .bot_stop {{ color: #ff4444; }}
</style>
</head>
<body>

<div class="header">
  <h1>HL Bot Command Center</h1>
  <div class="stats" id="stats"></div>
</div>

<div class="replay-bar">
  <label>Replay</label>
  <div class="range-btns" id="range-btns">
    <button data-range="all" class="active">ALL</button>
    <button data-range="1m">1M</button>
    <button data-range="1w">1W</button>
    <button data-range="1d">1D</button>
    <button data-range="6h">6H</button>
    <button data-range="1h">1H</button>
  </div>
  <input type="range" id="replay-slider" min="0" max="100" value="100">
  <span class="time-display" id="replay-time">--</span>
  <span class="state-display" id="replay-state"></span>
</div>

<div class="grid">
  <div class="panel full-width">
    <h3>3D Volatility Surface (Price Range bps per Coin over Time) — drag to rotate</h3>
    <div id="vol-surface" class="chart-tall"></div>
  </div>
  <div class="panel">
    <h3>Cumulative PnL</h3>
    <div id="pnl-chart" class="chart"></div>
  </div>
  <div class="panel">
    <h3>Fill Prices (green=buy, red=sell)</h3>
    <div id="fills-chart" class="chart"></div>
  </div>
  <div class="panel">
    <h3>Portfolio Value</h3>
    <div id="portfolio-chart" class="chart"></div>
  </div>
  <div class="panel">
    <h3>Event Stream</h3>
    <div class="timeline" id="timeline"></div>
  </div>
</div>

<div class="insights" id="insights">
  <h3>AI Debugger</h3>
</div>

<script>
const fills = {fills_json};
const statuses = {statuses_json};
const trips = {trips_json};
const contexts = {contexts_json};
const risks = {risks_json};
const allEvents = {all_events_json};

const darkLayout = {{ paper_bgcolor: '#0a0a0f', plot_bgcolor: '#0a0a0f' }};
const axStyle = {{ color: '#555', gridcolor: '#1a1a2e' }};

// === TIME FILTER HELPER ===
function filterByRange(arr, range) {{
  if (range === 'all' || !arr.length) return arr;
  const lastTs = arr.reduce((m, e) => e.ts > m ? e.ts : m, '');
  const last = new Date(lastTs);
  const ms = {{ '1m': 30*86400000, '1w': 7*86400000, '1d': 86400000, '6h': 6*3600000, '1h': 3600000 }};
  const cutoff = new Date(last.getTime() - (ms[range] || 0));
  return arr.filter(e => new Date(e.ts) >= cutoff);
}}

// === BUILD SURFACE IN BROWSER ===
function computeSurface(rangeFills) {{
  const byCoin = {{}};
  rangeFills.forEach(f => {{
    if (!f.price || !f.coin || !f.ts) return;
    if (!byCoin[f.coin]) byCoin[f.coin] = [];
    byCoin[f.coin].push(f);
  }});
  const coins = Object.keys(byCoin).sort();
  if (!coins.length) return null;
  // Bucket by hour
  const bucketSet = new Set();
  coins.forEach(c => byCoin[c].forEach(f => bucketSet.add((f.ts||'').substring(0,13))));
  const buckets = [...bucketSet].sort();
  if (!buckets.length) return null;
  // Compute vol per coin per bucket
  const z = coins.map(coin => {{
    const cf = byCoin[coin];
    return buckets.map(b => {{
      const bf = cf.filter(f => (f.ts||'').substring(0,13) === b);
      if (bf.length < 2) return 0;
      const prices = bf.map(f => f.price);
      const mean = prices.reduce((s,p) => s+p, 0) / prices.length;
      if (mean <= 0) return 0;
      return (Math.max(...prices) - Math.min(...prices)) / mean * 10000;
    }});
  }});
  return {{ coins, buckets, z }};
}}

// === RENDER ALL CHARTS ===
function renderAll(range) {{
  const rFills = filterByRange(fills, range);
  const rTrips = filterByRange(trips, range);
  const rStatuses = filterByRange(statuses, range);
  const rRisks = filterByRange(risks, range);
  const rContexts = filterByRange(contexts, range);

  // Stats
  const nF = rFills.length, nT = rTrips.length;
  const pnl = rTrips.reduce((s,t) => s + (t.net_pnl||0), 0);
  const w = rTrips.filter(t => t.net_pnl >= 0).length;
  const wr = nT > 0 ? (w/nT*100).toFixed(0) : 0;
  const vol = rFills.reduce((s,f) => s + f.price * f.size, 0);
  const pc = pnl >= 0 ? 'green' : 'red';
  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="val">${{nF}}</div><div class="lbl">Fills</div></div>
    <div class="stat"><div class="val">${{nT}}</div><div class="lbl">Trips</div></div>
    <div class="stat"><div class="val ${{pc}}">${{pnl>=0?'+':''}}${{pnl.toFixed(4)}}</div><div class="lbl">Net PnL</div></div>
    <div class="stat"><div class="val">${{wr}}%</div><div class="lbl">Win Rate</div></div>
    <div class="stat"><div class="val">$${{vol.toFixed(0)}}</div><div class="lbl">Volume</div></div>
    <div class="stat"><div class="val yellow">${{rRisks.length}}</div><div class="lbl">Risk Blocks</div></div>
  `;

  // 3D Surface
  const surf = computeSurface(rFills);
  if (surf && surf.coins.length > 0) {{
    const surfTrace = {{
      z: surf.z, x: surf.buckets, y: surf.coins,
      type: 'surface',
      colorscale: [[0,'#0a0a2e'],[0.2,'#1a1a6e'],[0.4,'#4444aa'],[0.6,'#ff8800'],[0.8,'#ff4444'],[1,'#ff0000']],
      colorbar: {{ title: 'Vol (bps)', titlefont: {{color:'#888'}}, tickfont: {{color:'#666'}} }},
      hovertemplate: 'Coin: %{{y}}<br>Time: %{{x}}<br>Vol: %{{z:.1f}} bps<extra></extra>',
      lighting: {{ ambient: 0.6, diffuse: 0.5, specular: 0.3 }},
      contours: {{ z: {{ show: true, usecolormap: true, highlightcolor: '#fff', project: {{ z: true }} }} }},
    }};
    // Fill markers on surface
    const markers = [];
    rFills.forEach(f => {{
      const ci = surf.coins.indexOf(f.coin);
      const b = (f.ts||'').substring(0,13);
      const ti = surf.buckets.indexOf(b);
      if (ci >= 0 && ti >= 0) {{
        markers.push({{ x: b, y: f.coin, z: (surf.z[ci][ti]||0) + 2,
          side: f.side, text: `${{(f.side||'').toUpperCase()}} ${{f.size}} ${{f.coin}} @ $${{f.price}}` }});
      }}
    }});
    const traces = [surfTrace];
    if (markers.length) {{
      traces.push({{
        x: markers.map(m=>m.x), y: markers.map(m=>m.y), z: markers.map(m=>m.z),
        mode: 'markers', type: 'scatter3d',
        marker: {{ size: 5, color: markers.map(m => m.side==='buy'||m.side==='b' ? '#00ff88':'#ff4444'), symbol: 'diamond' }},
        text: markers.map(m=>m.text), hoverinfo: 'text', name: 'Fills',
      }});
    }}
    Plotly.newPlot('vol-surface', traces, {{
      ...darkLayout,
      scene: {{
        xaxis: {{ title: 'Time', ...axStyle, tickangle: -30, nticks: 10 }},
        yaxis: {{ title: 'Coin', ...axStyle }},
        zaxis: {{ title: 'Volatility (bps)', ...axStyle }},
        bgcolor: '#0a0a0f',
        camera: {{ eye: {{ x: 1.8, y: -1.5, z: 1.2 }} }},
      }},
      margin: {{ l:0, r:0, t:10, b:0 }}, showlegend: false,
    }});
  }} else {{
    document.getElementById('vol-surface').innerHTML = '<p style="color:#555;padding:40px;text-align:center">No fill data in this range for surface</p>';
  }}

  // PnL chart
  if (rTrips.length > 0) {{
    let cum = [], r2 = 0;
    rTrips.forEach(t => {{ r2 += (t.net_pnl||0); cum.push({{x:t.ts, y:r2}}); }});
    Plotly.newPlot('pnl-chart', [{{
      x: cum.map(p=>p.x), y: cum.map(p=>p.y),
      type:'scatter', mode:'lines+markers',
      line: {{color: r2>=0?'#00ff88':'#ff4444', width:2}}, marker: {{size:3}},
      fill:'tozeroy', fillcolor: r2>=0?'rgba(0,255,136,0.08)':'rgba(255,68,68,0.08)',
    }}], {{ ...darkLayout, xaxis: axStyle, yaxis: {{...axStyle, title:'Cum. PnL ($)'}}, margin:{{l:50,r:10,t:10,b:30}}, showlegend:false }});
  }} else {{ document.getElementById('pnl-chart').innerHTML = '<p style="color:#555;padding:20px">No trips in range</p>'; }}

  // Fills chart
  if (rFills.length > 0) {{
    const coins = [...new Set(rFills.map(f=>f.coin))];
    Plotly.newPlot('fills-chart', coins.map(coin => {{
      const cf = rFills.filter(f=>f.coin===coin);
      return {{
        x: cf.map(f=>f.ts), y: cf.map(f=>f.price), mode:'markers', name: coin,
        marker: {{ size:7, color: cf.map(f=>f.side==='buy'||f.side==='b'?'#00ff88':'#ff4444'),
          symbol: cf.map(f=>f.side==='buy'||f.side==='b'?'triangle-up':'triangle-down') }},
        text: cf.map(f=>`${{(f.side||'').toUpperCase()}} ${{f.size}} @ $${{f.price}}`),
      }};
    }}), {{ ...darkLayout, xaxis: axStyle, yaxis: {{...axStyle, title:'Price ($)'}}, margin:{{l:55,r:10,t:10,b:30}}, legend:{{font:{{color:'#666',size:10}}}} }});
  }} else {{ document.getElementById('fills-chart').innerHTML = '<p style="color:#555;padding:20px">No fills in range</p>'; }}

  // Portfolio chart
  const vs = rStatuses.filter(s=>s.portfolio>0);
  if (vs.length > 0) {{
    Plotly.newPlot('portfolio-chart', [{{
      x: vs.map(s=>s.ts), y: vs.map(s=>s.portfolio),
      type:'scatter', mode:'lines', line:{{color:'#4488ff',width:1.5}},
      fill:'tozeroy', fillcolor:'rgba(68,136,255,0.06)',
    }}], {{ ...darkLayout, xaxis: axStyle, yaxis: {{...axStyle, title:'Portfolio ($)'}}, margin:{{l:50,r:10,t:10,b:30}}, showlegend:false }});
  }} else {{ document.getElementById('portfolio-chart').innerHTML = '<p style="color:#555;padding:20px">No portfolio data in range</p>'; }}

  // AI Insights (rebuild for range)
  const insightsDiv = document.getElementById('insights');
  insightsDiv.innerHTML = '<h3 style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">AI Debugger</h3>';
  const rules = [];
  if (nT > 5) {{
    if (pnl < 0) rules.push({{ text: `NET NEGATIVE: avg trip PnL $${{(pnl/nT).toFixed(4)}}. Strategy losing per round trip.`, bad: true }});
    if (parseInt(wr) < 50) rules.push({{ text: `LOW WIN RATE: ${{wr}}%. Likely adverse selection or slow exits.`, bad: true }});
    if (parseInt(wr) >= 60) rules.push({{ text: `GOOD WIN RATE: ${{wr}}%.`, bad: false }});
  }}
  const tFees = rFills.reduce((s,f) => s + Math.abs(f.fee), 0);
  if (vol > 0) {{
    const feeBps = tFees / vol * 10000;
    if (feeBps > 3) rules.push({{ text: `HIGH FEES: ${{feeBps.toFixed(1)}}bps avg. Fees eating spread capture.`, bad: true }});
  }}
  if (rRisks.length > 10) rules.push({{ text: `FREQUENT RISK BLOCKS: ${{rRisks.length}} pauses. Bot in cooldown too often.`, bad: true }});
  if (rContexts.length > 5) {{
    const ae = rContexts.reduce((s,c) => s + c.edge_bps, 0) / rContexts.length;
    rules.push({{ text: ae < 0 ? `NEGATIVE EDGE: avg ${{ae.toFixed(1)}}bps. Adverse selection.` : `POSITIVE EDGE: avg ${{ae.toFixed(1)}}bps.`, bad: ae < 0 }});
  }}
  const coinTrips = {{}};
  rTrips.forEach(t => {{ coinTrips[t.coin] = coinTrips[t.coin] || []; coinTrips[t.coin].push(t); }});
  Object.entries(coinTrips).forEach(([coin, ts]) => {{
    if (ts.length >= 3) {{
      const cp = ts.reduce((s,t) => s + (t.net_pnl||0), 0);
      const cw = ts.filter(t => t.net_pnl >= 0).length / ts.length * 100;
      if (cp < -0.01) rules.push({{ text: `WEAK: ${{coin}} — ${{ts.length}} trips, PnL $${{cp.toFixed(4)}}, WR ${{cw.toFixed(0)}}%`, bad: true }});
    }}
  }});
  if (!rules.length) rules.push({{ text: 'Collecting data...', bad: false }});
  rules.forEach(r => {{
    const d = document.createElement('div');
    d.className = 'insight' + (r.bad ? '' : ' good');
    d.textContent = r.text;
    insightsDiv.appendChild(d);
  }});
}}

// === REPLAY + TIME RANGE ===
const slider = document.getElementById('replay-slider');
const timeDisplay = document.getElementById('replay-time');
const stateDisplay = document.getElementById('replay-state');
const timeline = document.getElementById('timeline');
let filteredEvents = allEvents;
let currentRange = 'all';

function getFilteredEvents(range) {{
  if (range === 'all' || !allEvents.length) return allEvents;
  const last = new Date(allEvents[allEvents.length - 1].ts);
  const ms = {{ '1m': 30*86400000, '1w': 7*86400000, '1d': 86400000, '6h': 6*3600000, '1h': 3600000 }};
  const cutoff = new Date(last.getTime() - (ms[range] || 0));
  return allEvents.filter(e => new Date(e.ts) >= cutoff);
}}

function formatEventDetail(e) {{
  const type = e.type || '?';
  if (type === 'fill') {{
    const arrow = e.side === 'buy' || e.side === 'b' ? '\\u25B2' : '\\u25BC';
    return `${{arrow}} ${{e.size}} ${{e.coin}} @ $${{e.price}} fee=$${{(e.fee||0).toFixed(4)}}`;
  }} else if (type === 'trip') {{
    const sign = e.pnl >= 0 ? '+' : '';
    return `#${{e.trip_num}} ${{e.coin}} ${{sign}}$${{(e.pnl||0).toFixed(4)}}`;
  }} else if (type === 'risk') {{
    return `${{e.coin}} ${{e.reason}}`;
  }} else if (type === 'error') {{
    return (e.message || '').substring(0, 60);
  }} else if (type === 'bot_start') {{
    return 'Session started';
  }} else if (type === 'bot_stop') {{
    return 'Session stopped';
  }}
  return '';
}}

function updateReplay(idx) {{
  if (!filteredEvents.length) return;
  const e = filteredEvents[idx];
  const ts = e.ts || '';
  timeDisplay.textContent = ts.replace('T', ' ').substring(0, 19);
  const subset = filteredEvents.slice(0, idx + 1);
  const nFills = subset.filter(x => x.type === 'fill').length;
  const nTrips = subset.filter(x => x.type === 'trip').length;
  const cumPnl = subset.filter(x => x.type === 'trip').reduce((s,x) => s + (x.pnl||0), 0);
  stateDisplay.textContent = `Fills: ${{nFills}} | Trips: ${{nTrips}} | PnL: $${{cumPnl.toFixed(4)}} | Event ${{idx+1}}/${{filteredEvents.length}}`;
  document.querySelectorAll('.timeline .ev').forEach((el, i) => {{
    el.classList.toggle('highlighted', i === filteredEvents.length - 1 - idx);
  }});
}}

function buildTimeline() {{
  timeline.innerHTML = '';
  const displayEvents = filteredEvents.slice(-500).reverse();
  displayEvents.forEach((e, i) => {{
    const div = document.createElement('div');
    div.className = 'ev';
    const ts = (e.ts || '').replace('T',' ').substring(0, 16) || '??';
    const type = e.type || '?';
    const detail = formatEventDetail(e);
    div.innerHTML = `<span class="ts">${{ts}}</span><span class="type ${{type}}">${{type}}</span><span>${{detail}}</span>`;
    const evIdx = filteredEvents.length - 1 - i;
    div.onclick = () => {{ slider.value = evIdx; updateReplay(evIdx); }};
    timeline.appendChild(div);
  }});
}}

function setRange(range) {{
  currentRange = range;
  filteredEvents = getFilteredEvents(range);
  document.querySelectorAll('#range-btns button').forEach(b => b.classList.toggle('active', b.dataset.range === range));
  slider.max = Math.max(filteredEvents.length - 1, 0);
  slider.value = filteredEvents.length - 1;
  buildTimeline();
  updateReplay(filteredEvents.length - 1);
  // Rebuild all charts for this range
  renderAll(range);
  // Update range info
  if (filteredEvents.length > 0) {{
    const first = filteredEvents[0].ts.replace('T',' ').substring(0,16);
    const last = filteredEvents[filteredEvents.length-1].ts.replace('T',' ').substring(0,16);
    const nF = filteredEvents.filter(x => x.type==='fill').length;
    const nT = filteredEvents.filter(x => x.type==='trip').length;
    timeDisplay.textContent = `${{first}} -> ${{last}} (${{filteredEvents.length}} events, ${{nF}} fills, ${{nT}} trips)`;
  }}
}}

document.querySelectorAll('#range-btns button').forEach(b => {{
  b.addEventListener('click', () => setRange(b.dataset.range));
}});
slider.addEventListener('input', () => updateReplay(parseInt(slider.value)));

// Initialize
setRange('all');

// === AI INSIGHTS ===
const insightsDiv = document.getElementById('insights');
const rules = [];
if (totalTrips > 5) {{
  if (totalPnl < 0) rules.push({{ text: `NET NEGATIVE: avg trip PnL $${{(totalPnl/totalTrips).toFixed(4)}}. Strategy losing per round trip.`, bad: true }});
  if (parseInt(wr) < 50) rules.push({{ text: `LOW WIN RATE: ${{wr}}%. Likely adverse selection or exit ladder too passive.`, bad: true }});
  if (parseInt(wr) >= 60) rules.push({{ text: `GOOD WIN RATE: ${{wr}}%.`, bad: false }});
}}
const totalFees = fills.reduce((s,f) => s + Math.abs(f.fee), 0);
if (totalVolume > 0) {{
  const feeBps = totalFees / totalVolume * 10000;
  if (feeBps > 3) rules.push({{ text: `HIGH FEES: ${{feeBps.toFixed(1)}}bps avg. Fees eating spread capture.`, bad: true }});
}}
if (totalRisks > 10) rules.push({{ text: `FREQUENT RISK BLOCKS: ${{totalRisks}} pauses. Bot spending too much time in cooldown.`, bad: true }});
if (contexts.length > 5) {{
  const avgEdge = contexts.reduce((s,c) => s + c.edge_bps, 0) / contexts.length;
  if (avgEdge < 0) rules.push({{ text: `NEGATIVE EDGE: avg ${{avgEdge.toFixed(1)}}bps. Adverse selection detected.`, bad: true }});
  else rules.push({{ text: `POSITIVE EDGE: avg ${{avgEdge.toFixed(1)}}bps.`, bad: false }});
}}
// Per-coin analysis
const coinTrips = {{}};
trips.forEach(t => {{ coinTrips[t.coin] = coinTrips[t.coin] || []; coinTrips[t.coin].push(t); }});
Object.entries(coinTrips).forEach(([coin, ts]) => {{
  if (ts.length >= 3) {{
    const coinPnl = ts.reduce((s,t) => s + (t.net_pnl||0), 0);
    const coinWr = ts.filter(t => t.net_pnl >= 0).length / ts.length * 100;
    if (coinPnl < -0.01) rules.push({{ text: `WEAK: ${{coin}} — ${{ts.length}} trips, PnL $${{coinPnl.toFixed(4)}}, WR ${{coinWr.toFixed(0)}}%`, bad: true }});
  }}
}});
if (rules.length === 0) rules.push({{ text: 'Collecting data... insights appear after fills and trips.', bad: false }});
rules.forEach(r => {{
  const d = document.createElement('div');
  d.className = 'insight' + (r.bad ? '' : ' good');
  d.textContent = r.text;
  insightsDiv.appendChild(d);
}});
</script>
</body>
</html>""";

    output = output_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "command_center.html")
    with open(output, "w") as f:
        f.write(html)
    print(f"Generated: {output}")
    return output


def main():
    parser = argparse.ArgumentParser(description="HL Bot Event Analyzer & AI Debugger")
    parser.add_argument("--input", default=None, help="Path to events.jsonl")
    parser.add_argument("--last", default=None, help="Time window (e.g., 1h, 30m, 24h)")
    parser.add_argument("--coin", default=None, help="Filter by coin")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--surface", action="store_true", help="Generate volatility surface HTML")

    args = parser.parse_args()

    since = None
    if args.last:
        amount = int(args.last[:-1])
        unit = args.last[-1]
        if unit == 'h':
            delta = timedelta(hours=amount)
        elif unit == 'm':
            delta = timedelta(minutes=amount)
        elif unit == 'd':
            delta = timedelta(days=amount)
        else:
            delta = timedelta(hours=amount)
        since = (datetime.now(timezone.utc) - delta).isoformat()

    events = load_events(args.input, since=since, coin=args.coin)

    if args.surface:
        generate_surface_html(events)
    else:
        print_report(events, as_json=args.json)


if __name__ == "__main__":
    main()
