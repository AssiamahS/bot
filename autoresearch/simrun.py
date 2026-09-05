#!/usr/bin/env python3
"""
simrun — drop a folder (or paste a GitHub URL) with strategy.py, get a
         TradingView-style HTML report. Optionally publish to GitHub Pages.

Usage:
    python simrun.py [folder]                       # local folder
    python simrun.py --repo https://github.com/u/r  # clone + run
    python simrun.py --repo URL --strategy-path sub/dir
    python simrun.py . --publish --label my-mean-rev

The strategy.py must export:
    generate_signals(df: pd.DataFrame) -> pd.Series   # 1=long, -1=short, 0=flat

Output:
    <folder>/sim_report.html  (auto-opens in browser)
    With --publish: also pushes to ~/offprem/<label>/index.html and updates the
    site index. Repo: github.com/AssiamahS/offprem (GitHub Pages).
"""

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

OFFPREM_REPO = "git@github.com:AssiamahS/offprem.git"
OFFPREM_LOCAL = Path.home() / "offprem"
OFFPREM_PAGES_URL = "https://assiamahs.github.io/offprem"

DATA_DIR = Path(__file__).parent / "data"
FUNDING_DIR = DATA_DIR / "funding"
COINS = ["BTC", "ETH", "SOL", "HYPE", "XRP", "SUI", "DOGE", "AVAX"]

TAKER_FEE = 0.00035
SLIPPAGE = 0.0001
INITIAL_CAPITAL = 100.0
MAX_POSITION_PCT = 0.20
BARS_PER_YEAR = 8760  # 1h bars


def load_strategy(folder: Path):
    strat_path = folder / "strategy.py"
    if not strat_path.exists():
        sys.exit(f"strategy.py not found in {folder}")
    spec = importlib.util.spec_from_file_location("user_strategy", strat_path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(folder))
    spec.loader.exec_module(mod)
    if not hasattr(mod, "generate_signals"):
        sys.exit("strategy.py must export generate_signals(df) -> pd.Series")
    return mod


def load_data(coin: str, dataset: str) -> pd.DataFrame:
    path = DATA_DIR / f"{coin}_{dataset}.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    fpath = FUNDING_DIR / f"{coin}.csv"
    if fpath.exists():
        f = pd.read_csv(fpath)
        f.index = pd.to_datetime(f.iloc[:, 0], utc=True, format="ISO8601")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        df = df.join(f["funding_rate"].astype(float), how="left")
        df["funding_rate"] = df["funding_rate"].ffill().fillna(0.0)
    else:
        df["funding_rate"] = 0.0
    return df


def run_with_trace(df: pd.DataFrame, signals: pd.Series) -> dict:
    equity = INITIAL_CAPITAL
    position = 0
    entry_price = 0.0
    peak = equity
    max_dd = 0.0
    equity_curve = [equity]
    entries, exits, trades = [], [], []

    def close_position(i, price):
        nonlocal equity, position
        pnl = (price / entry_price - 1) * position
        net = pnl - (TAKER_FEE + SLIPPAGE)
        equity *= (1 + net * MAX_POSITION_PCT)
        exits.append((i, float(price), "long" if position == 1 else "short", float(net)))
        trades.append(net)

    has_funding = "funding_rate" in df.columns
    for i in range(1, len(df)):
        price = df["close"].iloc[i]
        signal = int(signals.iloc[i]) if i < len(signals) else 0

        # accrue hourly funding while holding a position
        # funding > 0: longs pay shorts. position=1 (long) loses, position=-1 (short) gains.
        if has_funding and position != 0:
            funding = float(df["funding_rate"].iloc[i])
            equity *= (1 - funding * position * MAX_POSITION_PCT)

        if signal != 0 and signal != position:
            if position != 0:
                close_position(i, price)
            position = signal
            entry_price = price
            equity *= (1 - (TAKER_FEE + SLIPPAGE) * MAX_POSITION_PCT)
            entries.append((i, float(price), "long" if signal == 1 else "short"))
        elif signal == 0 and position != 0:
            close_position(i, price)
            position = 0

        equity_curve.append(equity)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    if position != 0:
        close_position(len(df) - 1, df["close"].iloc[-1])

    returns = np.diff(equity_curve) / np.array(equity_curve[:-1])
    returns = returns[np.isfinite(returns)]
    sharpe = sortino = 0.0
    if len(returns) > 0 and np.std(returns) > 0:
        sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(BARS_PER_YEAR))
        downside = returns[returns < 0]
        if len(downside) > 0 and np.std(downside) > 0:
            sortino = float(np.mean(returns) / np.std(downside) * np.sqrt(BARS_PER_YEAR))

    wins = [t for t in trades if t > 0]
    return {
        "equity_curve": [round(e, 4) for e in equity_curve],
        "entries": entries,
        "exits": exits,
        "stats": {
            "final_equity": round(equity, 2),
            "pnl_pct": round((equity / INITIAL_CAPITAL - 1) * 100, 2),
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
            "max_dd_pct": round(max_dd * 100, 2),
            "trades": len(trades),
            "win_rate": round(len(wins) / max(len(trades), 1) * 100, 1),
        },
    }


def build_payload(strategy_module, dataset: str) -> dict:
    panels, aggregate = [], []
    for coin in COINS:
        try:
            df = load_data(coin, dataset)
            signals = strategy_module.generate_signals(df)
            r = run_with_trace(df, signals)
            ts = df.index.strftime("%Y-%m-%d %H:%M").tolist()
            panels.append({
                "coin": coin,
                "timestamps": ts,
                "open": df["open"].round(4).tolist(),
                "high": df["high"].round(4).tolist(),
                "low": df["low"].round(4).tolist(),
                "close": df["close"].round(4).tolist(),
                "equity": r["equity_curve"],
                "entries": [{"ts": ts[i], "price": p, "side": s} for (i, p, s) in r["entries"]],
                "exits": [{"ts": ts[i], "price": p, "side": s, "pnl": pnl} for (i, p, s, pnl) in r["exits"]],
                "stats": r["stats"],
            })
            aggregate.append({"coin": coin, **r["stats"]})
        except Exception as e:
            panels.append({"coin": coin, "error": str(e)})
            aggregate.append({"coin": coin, "error": str(e)})
    return {"panels": panels, "aggregate": aggregate, "dataset": dataset}


HTML_TEMPLATE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>simrun — {label} ({dataset})</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#0e1117; color:#e6e6e6; font: 14px/1.4 -apple-system,system-ui,sans-serif; margin:0; padding:24px; max-width:1400px; }}
  h1 {{ font-weight:600; margin:0 0 6px; font-size:20px; }}
  h2 {{ font-weight:600; margin:24px 0 8px; font-size:15px; color:#9fb0c0; }}
  .sub {{ color:#7d8a96; font-size:12px; margin-bottom:18px; }}
  table {{ border-collapse:collapse; width:100%; margin-bottom:20px; font-size:13px; }}
  th, td {{ padding:6px 10px; border-bottom:1px solid #222; text-align:right; font-variant-numeric:tabular-nums; }}
  th:first-child, td:first-child {{ text-align:left; }}
  th {{ background:#161b22; color:#9fb0c0; font-weight:500; }}
  tr:hover {{ background:#1a2029; }}
  .pos {{ color:#16c784; }}
  .neg {{ color:#ea3943; }}
  .panel {{ background:#161b22; border-radius:8px; padding:12px; margin-bottom:18px; }}
  .panel-head {{ display:flex; justify-content:space-between; align-items:center; padding:0 6px 8px; }}
  .panel-head strong {{ font-size:16px; }}
  .badge {{ background:#21262d; padding:2px 8px; border-radius:4px; font-size:12px; color:#9fb0c0; }}
  .err {{ color:#ea3943; padding:12px; }}
</style>
</head>
<body>
<h1>simrun · {label} · <span class="badge">{dataset}</span></h1>
<div class="sub">Initial capital ${initial} · Position size {posn}% · Taker {taker}bps + slippage {slip}bps</div>
<div id="aggregate"></div>
<h2>Per-coin breakdown</h2>
<div id="panels"></div>
<script>
const DATA = {payload};

function fmtPct(x) {{
  if (x === undefined || x === null) return '-';
  const cls = x >= 0 ? 'pos' : 'neg';
  return `<span class="${{cls}}">${{x.toFixed(2)}}%</span>`;
}}

let agg = '<table><thead><tr><th>Coin</th><th>Return</th><th>Sharpe</th><th>Sortino</th><th>Max DD</th><th>Trades</th><th>Win Rate</th></tr></thead><tbody>';
for (const r of DATA.aggregate) {{
  if (r.error) {{
    agg += `<tr><td><strong>${{r.coin}}</strong></td><td colspan="6" class="neg">${{r.error}}</td></tr>`;
    continue;
  }}
  agg += `<tr><td><strong>${{r.coin}}</strong></td><td>${{fmtPct(r.pnl_pct)}}</td><td>${{r.sharpe}}</td><td>${{r.sortino}}</td><td>${{fmtPct(-r.max_dd_pct)}}</td><td>${{r.trades}}</td><td>${{r.win_rate.toFixed(1)}}%</td></tr>`;
}}
agg += '</tbody></table>';
document.getElementById('aggregate').innerHTML = agg;

const panelsEl = document.getElementById('panels');
for (const p of DATA.panels) {{
  const div = document.createElement('div');
  div.className = 'panel';
  if (p.error) {{
    div.innerHTML = `<div class="panel-head"><strong>${{p.coin}}</strong></div><div class="err">${{p.error}}</div>`;
    panelsEl.appendChild(div);
    continue;
  }}
  div.innerHTML = `<div class="panel-head"><strong>${{p.coin}}</strong><span class="badge">Return ${{fmtPct(p.stats.pnl_pct)}} · Sharpe ${{p.stats.sharpe}} · ${{p.stats.trades}} trades</span></div><div id="chart-${{p.coin}}" style="height:520px;"></div>`;
  panelsEl.appendChild(div);

  const candles = {{
    x: p.timestamps, open: p.open, high: p.high, low: p.low, close: p.close,
    type: 'candlestick', name: p.coin,
    increasing: {{ line: {{ color:'#16c784' }} }},
    decreasing: {{ line: {{ color:'#ea3943' }} }},
    yaxis: 'y'
  }};
  const equity = {{
    x: p.timestamps, y: p.equity, type:'scatter', mode:'lines',
    name:'Equity', line: {{ color:'#5a9fd4', width:2 }}, yaxis:'y2'
  }};
  const longs = p.entries.filter(e => e.side === 'long');
  const shorts = p.entries.filter(e => e.side === 'short');
  const buyMarkers = {{
    x: longs.map(e => e.ts), y: longs.map(e => e.price), mode:'markers',
    type:'scatter', name:'Long entry',
    marker: {{ symbol:'triangle-up', size:12, color:'#16c784', line: {{ color:'#fff', width:1 }} }}
  }};
  const sellMarkers = {{
    x: shorts.map(e => e.ts), y: shorts.map(e => e.price), mode:'markers',
    type:'scatter', name:'Short entry',
    marker: {{ symbol:'triangle-down', size:12, color:'#ea3943', line: {{ color:'#fff', width:1 }} }}
  }};
  const exitMarkers = {{
    x: p.exits.map(e => e.ts), y: p.exits.map(e => e.price), mode:'markers',
    type:'scatter', name:'Exit',
    marker: {{ symbol:'x', size:9, color:'#e6e6e6' }},
    text: p.exits.map(e => `PnL: ${{(e.pnl*100).toFixed(2)}}%`),
    hovertemplate: '%{{x}}<br>$%{{y}}<br>%{{text}}<extra></extra>'
  }};
  const layout = {{
    paper_bgcolor:'#161b22', plot_bgcolor:'#0e1117',
    font: {{ color:'#e6e6e6' }},
    xaxis: {{ rangeslider: {{ visible:false }}, gridcolor:'#1f2630' }},
    yaxis: {{ title:'Price (USD)', gridcolor:'#1f2630', side:'left' }},
    yaxis2: {{ title:'Equity (USD)', overlaying:'y', side:'right', gridcolor:'transparent' }},
    legend: {{ orientation:'h', y:1.08 }},
    margin: {{ l:60, r:60, t:30, b:40 }}
  }};
  Plotly.newPlot(`chart-${{p.coin}}`, [candles, buyMarkers, sellMarkers, exitMarkers, equity], layout, {{ responsive:true }});
}}
</script>
</body></html>"""


def render_html(payload: dict, label: str) -> str:
    return HTML_TEMPLATE.format(
        label=label,
        dataset=payload["dataset"],
        initial=int(INITIAL_CAPITAL),
        posn=int(MAX_POSITION_PCT * 100),
        taker=int(TAKER_FEE * 10000),
        slip=int(SLIPPAGE * 10000),
        payload=json.dumps(payload, default=str),
    )


def clone_repo(url: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="simrun-repo-"))
    print(f"cloning {url} -> {tmp}")
    subprocess.run(["git", "clone", "--depth", "1", url, str(tmp)], check=True, capture_output=True)
    return tmp


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in s).strip("-").lower() or "report"


def publish_to_offprem(report_html: Path, label: str, payload: dict, source_desc: str):
    if not OFFPREM_LOCAL.exists():
        print(f"cloning offprem -> {OFFPREM_LOCAL}")
        subprocess.run(["git", "clone", OFFPREM_REPO, str(OFFPREM_LOCAL)], check=True)
    else:
        subprocess.run(["git", "-C", str(OFFPREM_LOCAL), "pull", "--ff-only"], check=False, capture_output=True)

    slug = slugify(label)
    dest_dir = OFFPREM_LOCAL / "reports" / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(report_html, dest_dir / "index.html")

    runs_log = OFFPREM_LOCAL / "reports" / "runs.json"
    runs = json.loads(runs_log.read_text()) if runs_log.exists() else []
    agg = [r for r in payload["aggregate"] if "pnl_pct" in r]
    avg_return = round(sum(r["pnl_pct"] for r in agg) / max(len(agg), 1), 2)
    avg_sharpe = round(sum(r["sharpe"] for r in agg) / max(len(agg), 1), 3)
    runs = [r for r in runs if r["slug"] != slug]
    runs.insert(0, {
        "slug": slug, "label": label, "source": source_desc,
        "dataset": payload["dataset"], "avg_return_pct": avg_return,
        "avg_sharpe": avg_sharpe, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    runs_log.write_text(json.dumps(runs, indent=2))

    write_index(runs)
    (OFFPREM_LOCAL / ".nojekyll").touch()

    subprocess.run(["git", "-C", str(OFFPREM_LOCAL), "add", "."], check=True)
    diff = subprocess.run(["git", "-C", str(OFFPREM_LOCAL), "diff", "--cached", "--name-only"], capture_output=True, text=True)
    if not diff.stdout.strip():
        print("no changes to commit")
        return
    msg = f"add {slug} report ({avg_return:+.2f}% avg, sharpe {avg_sharpe})"
    subprocess.run(["git", "-C", str(OFFPREM_LOCAL), "commit", "-m", msg], check=True)
    subprocess.run(["git", "-C", str(OFFPREM_LOCAL), "push"], check=True)
    print(f"published: {OFFPREM_PAGES_URL}/reports/{slug}/")


def write_index(runs: list):
    rows = "\n".join(
        f'<tr><td><a href="reports/{r["slug"]}/">{r["label"]}</a></td>'
        f'<td>{r["source"]}</td><td>{r["dataset"]}</td>'
        f'<td class="{"pos" if r["avg_return_pct"]>=0 else "neg"}">{r["avg_return_pct"]:+.2f}%</td>'
        f'<td>{r["avg_sharpe"]}</td><td>{r["ts"][:16].replace("T"," ")}</td></tr>'
        for r in runs
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>offprem · backtest reports</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#0e1117; color:#e6e6e6; font:14px/1.4 -apple-system,system-ui,sans-serif; margin:0; padding:32px; max-width:1100px; }}
  h1 {{ margin:0 0 6px; font-size:22px; }}
  .sub {{ color:#7d8a96; margin-bottom:24px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid #222; text-align:right; font-variant-numeric:tabular-nums; }}
  th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align:left; }}
  th {{ background:#161b22; color:#9fb0c0; font-weight:500; }}
  tr:hover {{ background:#1a2029; }}
  a {{ color:#5a9fd4; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .pos {{ color:#16c784; }} .neg {{ color:#ea3943; }}
</style></head><body>
<h1>offprem · backtest reports</h1>
<div class="sub">strategies run through simrun against BTC/ETH/SOL/HYPE/XRP/SUI/DOGE/AVAX</div>
<table><thead><tr><th>Strategy</th><th>Source</th><th>Dataset</th><th>Avg Return</th><th>Avg Sharpe</th><th>Run at (UTC)</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""
    (OFFPREM_LOCAL / "index.html").write_text(html)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("folder", nargs="?", default=".", help="folder containing strategy.py")
    p.add_argument("--repo", help="clone this git URL and run from it")
    p.add_argument("--strategy-path", default=".", help="subdir within --repo containing strategy.py")
    p.add_argument("--dataset", default="test", choices=["train", "test"])
    p.add_argument("--label", help="report label (default: folder/repo name)")
    p.add_argument("--publish", action="store_true", help="push report to AssiamahS/offprem GitHub Pages")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    if args.repo:
        repo_root = clone_repo(args.repo)
        folder = (repo_root / args.strategy_path).resolve()
        source_desc = args.repo
        default_label = args.repo.rstrip("/").split("/")[-1].replace(".git", "")
    else:
        folder = Path(args.folder).resolve()
        source_desc = f"local:{folder}"
        default_label = folder.name

    label = args.label or default_label
    strat = load_strategy(folder)
    payload = build_payload(strat, args.dataset)
    out = folder / "sim_report.html"
    out.write_text(render_html(payload, label))
    print(f"wrote {out}")
    for r in payload["aggregate"]:
        if "error" in r:
            print(f"  {r['coin']}: ERROR {r['error']}")
        else:
            print(f"  {r['coin']}: {r['pnl_pct']:+.2f}%  sharpe={r['sharpe']}  trades={r['trades']}")

    if args.publish:
        publish_to_offprem(out, label, payload, source_desc)
    if not args.no_open:
        webbrowser.open(f"file://{out}")


if __name__ == "__main__":
    main()
