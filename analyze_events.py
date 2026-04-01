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


def generate_surface_html(events, output_path=None):
    """Generate an interactive volatility surface HTML using Plotly CDN."""
    fills = [e for e in events if e.get("type") == "fill"]
    statuses = [e for e in events if e.get("type") in ("status", "slywatch_snapshot")]

    fills_json = json.dumps([{
        "ts": f.get("ts", ""),
        "coin": f.get("coin", ""),
        "price": f.get("price", 0),
        "side": f.get("side", ""),
        "size": f.get("size", 0),
        "fee": f.get("fee", 0),
        "pnl": f.get("closed_pnl", 0),
    } for f in fills])

    statuses_json = json.dumps([{
        "ts": s.get("ts", ""),
        "portfolio": s.get("portfolio", 0),
        "pnl": s.get("pnl", 0),
        "fills": s.get("fills", 0),
    } for s in statuses])

    trips = [e for e in events if e.get("type") == "trip"]
    trips_json = json.dumps([{
        "ts": t.get("ts", ""),
        "coin": t.get("coin", ""),
        "net_pnl": t.get("net_pnl", 0),
        "trip_num": t.get("trip_num", 0),
    } for t in trips])

    contexts = [e for e in events if e.get("type") == "fill_context"]
    contexts_json = json.dumps([{
        "ts": c.get("ts", ""),
        "coin": c.get("coin", ""),
        "edge_bps": c.get("edge_bps", 0),
        "mid": c.get("mid", 0),
    } for c in contexts])

    html = f"""<!DOCTYPE html>
<html>
<head>
<title>HL Bot - Volatility Surface & Debugger</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0a0a0f; color: #e0e0e0; font-family: 'SF Mono', 'Fira Code', monospace; }}
  .header {{ padding: 16px 24px; border-bottom: 1px solid #1a1a2e; display: flex; justify-content: space-between; align-items: center; }}
  .header h1 {{ font-size: 18px; color: #00ff88; }}
  .stats {{ display: flex; gap: 24px; font-size: 13px; }}
  .stat {{ text-align: center; }}
  .stat .val {{ font-size: 20px; font-weight: bold; }}
  .stat .lbl {{ color: #666; font-size: 11px; }}
  .green {{ color: #00ff88; }}
  .red {{ color: #ff4444; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: #1a1a2e; }}
  .panel {{ background: #0a0a0f; padding: 12px; }}
  .panel h3 {{ font-size: 13px; color: #888; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }}
  .chart {{ width: 100%; height: 350px; }}
  .insights {{ padding: 16px 24px; border-top: 1px solid #1a1a2e; }}
  .insight {{ padding: 8px 12px; margin: 4px 0; background: #111; border-left: 3px solid #ff8800; font-size: 13px; }}
  .insight.good {{ border-color: #00ff88; }}
  .timeline {{ max-height: 300px; overflow-y: auto; font-size: 12px; }}
  .timeline .ev {{ padding: 4px 8px; border-bottom: 1px solid #111; display: flex; gap: 12px; }}
  .timeline .ev:hover {{ background: #111; }}
  .timeline .ts {{ color: #666; min-width: 80px; }}
  .timeline .type {{ min-width: 60px; font-weight: bold; }}
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
<div class="grid">
  <div class="panel">
    <h3>PnL Timeline</h3>
    <div id="pnl-chart" class="chart"></div>
  </div>
  <div class="panel">
    <h3>Fill Prices & Edge</h3>
    <div id="fills-chart" class="chart"></div>
  </div>
  <div class="panel">
    <h3>Volatility Surface (Price Spread by Coin over Time)</h3>
    <div id="vol-surface" class="chart"></div>
  </div>
  <div class="panel">
    <h3>Event Stream</h3>
    <div class="timeline" id="timeline"></div>
  </div>
</div>
<div class="insights" id="insights">
  <h3 style="color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">AI Debugger</h3>
</div>

<script>
const fills = {fills_json};
const statuses = {statuses_json};
const trips = {trips_json};
const contexts = {contexts_json};
const allEvents = [...fills.map(f => ({{...f, type:'fill'}})),
                   ...trips.map(t => ({{...t, type:'trip'}})),
                  ].sort((a,b) => a.ts.localeCompare(b.ts));

// Stats
const totalFills = fills.length;
const totalTrips = trips.length;
const totalPnl = trips.reduce((s,t) => s + (t.net_pnl||0), 0);
const winners = trips.filter(t => t.net_pnl >= 0).length;
const wr = totalTrips > 0 ? (winners/totalTrips*100).toFixed(0) : 0;
const totalVolume = fills.reduce((s,f) => s + f.price * f.size, 0);
const pnlClass = totalPnl >= 0 ? 'green' : 'red';

document.getElementById('stats').innerHTML = `
  <div class="stat"><div class="val">${{totalFills}}</div><div class="lbl">FILLS</div></div>
  <div class="stat"><div class="val">${{totalTrips}}</div><div class="lbl">TRIPS</div></div>
  <div class="stat"><div class="val ${{pnlClass}}">${{totalPnl >= 0 ? '+' : ''}}${{totalPnl.toFixed(4)}}</div><div class="lbl">NET PNL</div></div>
  <div class="stat"><div class="val">${{wr}}%</div><div class="lbl">WIN RATE</div></div>
  <div class="stat"><div class="val">$${{totalVolume.toFixed(0)}}</div><div class="lbl">VOLUME</div></div>
`;

// PnL chart — cumulative from trips
if (trips.length > 0) {{
  let cumPnl = [];
  let running = 0;
  trips.forEach(t => {{
    running += (t.net_pnl || 0);
    cumPnl.push({{x: t.ts, y: running}});
  }});
  Plotly.newPlot('pnl-chart', [{{
    x: cumPnl.map(p => p.x),
    y: cumPnl.map(p => p.y),
    type: 'scatter',
    mode: 'lines+markers',
    line: {{color: cumPnl[cumPnl.length-1].y >= 0 ? '#00ff88' : '#ff4444', width: 2}},
    marker: {{size: 4}},
    fill: 'tozeroy',
    fillcolor: cumPnl[cumPnl.length-1].y >= 0 ? 'rgba(0,255,136,0.1)' : 'rgba(255,68,68,0.1)',
  }}], {{
    paper_bgcolor: '#0a0a0f', plot_bgcolor: '#0a0a0f',
    xaxis: {{color: '#666', gridcolor: '#1a1a2e'}},
    yaxis: {{color: '#666', gridcolor: '#1a1a2e', title: 'Cumulative PnL ($)'}},
    margin: {{l:50,r:20,t:10,b:40}},
    showlegend: false,
  }});
}} else {{
  document.getElementById('pnl-chart').innerHTML = '<p style="color:#666;padding:20px">No trip data yet</p>';
}}

// Fills chart — price dots colored by side
if (fills.length > 0) {{
  const coins = [...new Set(fills.map(f => f.coin))];
  const traces = coins.map(coin => {{
    const cf = fills.filter(f => f.coin === coin);
    return {{
      x: cf.map(f => f.ts),
      y: cf.map(f => f.price),
      mode: 'markers',
      name: coin,
      marker: {{
        size: 8,
        color: cf.map(f => f.side === 'buy' || f.side === 'b' ? '#00ff88' : '#ff4444'),
        symbol: cf.map(f => f.side === 'buy' || f.side === 'b' ? 'triangle-up' : 'triangle-down'),
      }},
      text: cf.map(f => `${{f.side.toUpperCase()}} ${{f.size}} @ $${{f.price}} fee=$${{f.fee.toFixed(4)}}`),
    }};
  }});
  Plotly.newPlot('fills-chart', traces, {{
    paper_bgcolor: '#0a0a0f', plot_bgcolor: '#0a0a0f',
    xaxis: {{color: '#666', gridcolor: '#1a1a2e'}},
    yaxis: {{color: '#666', gridcolor: '#1a1a2e', title: 'Price ($)'}},
    margin: {{l:60,r:20,t:10,b:40}},
    legend: {{font: {{color: '#888'}}}},
  }});
}} else {{
  document.getElementById('fills-chart').innerHTML = '<p style="color:#666;padding:20px">No fill data yet</p>';
}}

// Vol surface — edge distribution if we have contexts, else portfolio over time
if (contexts.length > 0) {{
  const coins = [...new Set(contexts.map(c => c.coin))];
  const traces = coins.map(coin => {{
    const cc = contexts.filter(c => c.coin === coin);
    return {{
      x: cc.map(c => c.ts),
      y: cc.map(c => c.edge_bps),
      type: 'bar',
      name: coin,
      marker: {{color: cc.map(c => c.edge_bps >= 0 ? '#00ff88' : '#ff4444')}},
    }};
  }});
  Plotly.newPlot('vol-surface', traces, {{
    paper_bgcolor: '#0a0a0f', plot_bgcolor: '#0a0a0f',
    xaxis: {{color: '#666', gridcolor: '#1a1a2e'}},
    yaxis: {{color: '#666', gridcolor: '#1a1a2e', title: 'Edge (bps)'}},
    margin: {{l:50,r:20,t:10,b:40}},
    barmode: 'group',
    legend: {{font: {{color: '#888'}}}},
  }});
}} else if (statuses.length > 0) {{
  const validStatuses = statuses.filter(s => s.portfolio > 0);
  Plotly.newPlot('vol-surface', [{{
    x: validStatuses.map(s => s.ts),
    y: validStatuses.map(s => s.portfolio),
    type: 'scatter',
    mode: 'lines',
    line: {{color: '#4488ff', width: 2}},
    fill: 'tozeroy',
    fillcolor: 'rgba(68,136,255,0.1)',
  }}], {{
    paper_bgcolor: '#0a0a0f', plot_bgcolor: '#0a0a0f',
    xaxis: {{color: '#666', gridcolor: '#1a1a2e'}},
    yaxis: {{color: '#666', gridcolor: '#1a1a2e', title: 'Portfolio ($)'}},
    margin: {{l:50,r:20,t:10,b:40}},
    showlegend: false,
  }});
}} else {{
  document.getElementById('vol-surface').innerHTML = '<p style="color:#666;padding:20px">Collecting data... surface will appear after live fills</p>';
}}

// Timeline
const timeline = document.getElementById('timeline');
const recentEvents = allEvents.slice(-100).reverse();
recentEvents.forEach(e => {{
  const div = document.createElement('div');
  div.className = 'ev';
  const ts = (e.ts || '').substring(11, 19) || '??:??:??';
  const type = e.type || '?';
  let detail = '';
  if (type === 'fill') {{
    const arrow = e.side === 'buy' || e.side === 'b' ? '\\u25B2' : '\\u25BC';
    detail = `${{arrow}} ${{e.size}} ${{e.coin}} @ $${{e.price}} fee=$${{(e.fee||0).toFixed(4)}}`;
  }} else if (type === 'trip') {{
    const sign = e.net_pnl >= 0 ? '+' : '';
    detail = `#${{e.trip_num}} ${{e.coin}} ${{sign}}$${{(e.net_pnl||0).toFixed(4)}}`;
  }}
  div.innerHTML = `<span class="ts">${{ts}}</span><span class="type ${{type}}">${{type}}</span><span>${{detail}}</span>`;
  timeline.appendChild(div);
}});

// AI Insights
const insightsDiv = document.getElementById('insights');
const rules = [];
if (totalTrips > 5) {{
  if (totalPnl < 0) rules.push(`NET NEGATIVE: avg trip PnL $${{(totalPnl/totalTrips).toFixed(4)}}. Strategy losing per round trip.`);
  if (parseInt(wr) < 50) rules.push(`LOW WIN RATE: ${{wr}}%. Likely adverse selection or slow exits.`);
}}
const totalFees = fills.reduce((s,f) => s + Math.abs(f.fee), 0);
if (totalVolume > 0) {{
  const feeBps = totalFees / totalVolume * 10000;
  if (feeBps > 3) rules.push(`HIGH FEES: ${{feeBps.toFixed(1)}}bps avg. Fees eating spread capture.`);
}}
if (contexts.length > 5) {{
  const avgEdge = contexts.reduce((s,c) => s + c.edge_bps, 0) / contexts.length;
  if (avgEdge < 0) rules.push(`NEGATIVE EDGE: avg ${{avgEdge.toFixed(1)}}bps. Adverse selection detected.`);
}}
if (rules.length === 0) rules.push('Collecting data... insights will appear after more fills and trips.');
rules.forEach(r => {{
  const d = document.createElement('div');
  d.className = 'insight';
  d.textContent = r;
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
