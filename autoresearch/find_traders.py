#!/usr/bin/env python3
"""
find_traders — pull HL leaderboard + per-wallet portfolio history,
rank by quality (Sharpe, max DD, consistency), publish to offprem as a leaderboard report.

The actually-profitable bots aren't on GitHub — they're wallets on the leaderboard.
This script finds them and ranks them so we can copy.
"""

import argparse
import json
import math
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_API = "https://api.hyperliquid.xyz/info"
OFFPREM = Path.home() / "offprem"


def http_post(url: str, body: dict) -> any:
    req = request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def http_get(url: str) -> any:
    with request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def fetch_leaderboard() -> list:
    data = http_get(LEADERBOARD)
    return data.get("leaderboardRows", [])


def fetch_portfolio(addr: str) -> list:
    return http_post(INFO_API, {"type": "portfolio", "user": addr})


def compute_quality(addr: str) -> dict:
    """Pull account value history, compute Sharpe, max DD, return summary."""
    try:
        raw = fetch_portfolio(addr)
    except Exception as e:
        return {"error": str(e)}
    series = {}
    for window, payload in raw:
        avh = payload.get("accountValueHistory", [])
        if not avh:
            continue
        values = [float(v[1]) for v in avh]
        if len(values) < 5:
            continue
        rets = [(values[i] / values[i - 1]) - 1 for i in range(1, len(values)) if values[i - 1] > 0]
        if not rets:
            continue
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / max(len(rets) - 1, 1)
        std_r = math.sqrt(var) if var > 0 else 0
        peak = values[0]
        max_dd = 0.0
        for v in values:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)
        # bars-per-year: depends on window. For "day" the points are hourly (24/d * 365 = 8760)
        # for "week" they're 4-hourly, "month" daily, "allTime" weekly. Approximate.
        bpy = {"day": 8760, "week": 2190, "month": 365, "allTime": 52}.get(window, 365)
        sharpe = (mean_r / std_r) * math.sqrt(bpy) if std_r > 0 else 0
        net_pct = (values[-1] / values[0] - 1) * 100 if values[0] > 0 else 0
        series[window] = {
            "net_pct": round(net_pct, 2),
            "sharpe": round(sharpe, 2),
            "max_dd_pct": round(max_dd * 100, 2),
            "samples": len(values),
        }
    return series


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=30, help="how many leaders to deep-analyze")
    p.add_argument("--rank-by", default="month", choices=["day", "week", "month", "allTime"])
    p.add_argument("--min-vol", type=float, default=1_000_000, help="USD volume in ranking window")
    p.add_argument("--min-account", type=float, default=10_000)
    args = p.parse_args()

    print("fetching HL leaderboard...")
    rows = fetch_leaderboard()
    print(f"  {len(rows)} wallets returned")

    enriched = []
    for r in rows:
        perf = {w[0]: w[1] for w in r["windowPerformances"]}
        wp = perf.get(args.rank_by, {})
        try:
            pnl = float(wp.get("pnl", 0))
            roi = float(wp.get("roi", 0))
            vol = float(wp.get("vlm", 0))
            acct = float(r["accountValue"])
        except (TypeError, ValueError):
            continue
        if vol < args.min_vol or acct < args.min_account:
            continue
        enriched.append({
            "addr": r["ethAddress"], "account_value": acct,
            "pnl": pnl, "roi_pct": roi * 100, "vol_usd": vol,
        })

    enriched.sort(key=lambda x: x["roi_pct"], reverse=True)
    enriched = enriched[: args.top]
    print(f"  {len(enriched)} pass filters; deep-analyzing top {args.top}...")

    for i, t in enumerate(enriched):
        print(f"  [{i+1}/{len(enriched)}] {t['addr'][:10]}... roi={t['roi_pct']:+.1f}%")
        t["quality"] = compute_quality(t["addr"])
        time.sleep(0.2)

    # filter to only those with meaningful month sharpe
    keepers = [t for t in enriched if isinstance(t.get("quality"), dict)
               and t["quality"].get(args.rank_by, {}).get("sharpe", 0) > 1.0]
    keepers.sort(key=lambda x: x["quality"][args.rank_by]["sharpe"], reverse=True)
    print(f"  {len(keepers)} traders with sharpe>1.0 in {args.rank_by} window")

    out_dir = OFFPREM / "reports" / "leaderboard"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.joinpath("data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rank_by": args.rank_by, "all": enriched, "keepers": keepers,
    }, indent=2))
    write_html(out_dir, keepers, enriched, args.rank_by)
    update_runs_log(keepers, args.rank_by)
    git_publish()


def write_html(out_dir: Path, keepers: list, full: list, rank_by: str):
    rows = []
    for i, t in enumerate(keepers, 1):
        q = t["quality"].get(rank_by, {})
        addr = t["addr"]
        short = f"{addr[:6]}…{addr[-4:]}"
        rows.append(
            f'<tr><td>{i}</td>'
            f'<td><a href="https://app.hyperliquid.xyz/explorer/address/{addr}">{short}</a></td>'
            f'<td>${t["account_value"]:,.0f}</td>'
            f'<td class="{"pos" if t["roi_pct"]>=0 else "neg"}">{t["roi_pct"]:+.1f}%</td>'
            f'<td>${t["pnl"]:,.0f}</td>'
            f'<td>${t["vol_usd"]/1e6:,.1f}M</td>'
            f'<td>{q.get("sharpe", "-")}</td>'
            f'<td class="neg">-{q.get("max_dd_pct", "-")}%</td>'
            f'<td><a href="https://app.copin.io/trader/{addr}/HYPERLIQUID">copin</a> · '
            f'<a href="https://hyperdash.info/trader/{addr}">hyperdash</a></td></tr>'
        )

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>HL leaderboard · top traders to copy</title>
<style>
  body {{ background:#0e1117; color:#e6e6e6; font:14px/1.5 -apple-system,system-ui,sans-serif; margin:0; padding:32px; max-width:1300px; }}
  h1 {{ margin:0 0 6px; font-size:22px; }}
  .sub {{ color:#7d8a96; margin-bottom:24px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid #222; text-align:right; font-variant-numeric:tabular-nums; }}
  th:nth-child(2), td:nth-child(2), th:last-child, td:last-child {{ text-align:left; }}
  th {{ background:#161b22; color:#9fb0c0; font-weight:500; }}
  tr:hover {{ background:#1a2029; }}
  a {{ color:#5a9fd4; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .pos {{ color:#16c784; }} .neg {{ color:#ea3943; }}
</style></head><body>
<h1>Hyperliquid · top traders to copy ({rank_by} window)</h1>
<div class="sub">filtered for sharpe&gt;1.0 from top {len(full)} by ROI · click address for HL explorer · click copin/hyperdash for trade history</div>
<table>
<thead><tr><th>#</th><th>Wallet</th><th>Account</th><th>ROI</th><th>PnL</th><th>Volume</th><th>Sharpe</th><th>Max DD</th><th>Inspect</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="sub">Updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · data from stats-data.hyperliquid.xyz</p>
</body></html>"""
    (out_dir / "index.html").write_text(html)


def update_runs_log(keepers: list, rank_by: str):
    runs_log = OFFPREM / "reports" / "runs.json"
    runs = json.loads(runs_log.read_text()) if runs_log.exists() else []
    runs = [r for r in runs if r["slug"] != "leaderboard"]
    avg_roi = round(sum(t["roi_pct"] for t in keepers) / max(len(keepers), 1), 2) if keepers else 0
    avg_sharpe = round(sum(t["quality"][rank_by]["sharpe"] for t in keepers) / max(len(keepers), 1), 2) if keepers else 0
    runs.insert(0, {
        "slug": "leaderboard", "label": f"HL leaderboard top traders ({rank_by})",
        "source": "stats-data.hyperliquid.xyz", "dataset": rank_by,
        "avg_return_pct": avg_roi, "avg_sharpe": avg_sharpe,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    runs_log.write_text(json.dumps(runs, indent=2))

    # rebuild top index
    rows_html = "\n".join(
        f'<tr><td><a href="reports/{r["slug"]}/">{r["label"]}</a></td>'
        f'<td>{r["source"]}</td><td>{r["dataset"]}</td>'
        f'<td class="{"pos" if r["avg_return_pct"]>=0 else "neg"}">{r["avg_return_pct"]:+.2f}%</td>'
        f'<td>{r["avg_sharpe"]}</td><td>{r["ts"][:16].replace("T"," ")}</td></tr>'
        for r in runs
    )
    (OFFPREM / "index.html").write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>offprem · backtest reports</title>
<style>
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
<div class="sub">strategies + traders run against BTC/ETH/SOL/HYPE/XRP/SUI/DOGE/AVAX</div>
<table><thead><tr><th>Strategy</th><th>Source</th><th>Dataset</th><th>Avg Return</th><th>Avg Sharpe</th><th>Run at (UTC)</th></tr></thead>
<tbody>{rows_html}</tbody></table></body></html>""")


def git_publish():
    subprocess.run(["git", "-C", str(OFFPREM), "add", "."], check=True)
    diff = subprocess.run(["git", "-C", str(OFFPREM), "diff", "--cached", "--name-only"],
                          capture_output=True, text=True)
    if not diff.stdout.strip():
        print("nothing to commit")
        return
    subprocess.run(["git", "-C", str(OFFPREM), "commit", "-m", "update HL leaderboard scan"], check=True)
    subprocess.run(["git", "-C", str(OFFPREM), "push"], check=True)
    print("published: https://assiamahs.github.io/offprem/reports/leaderboard/")


if __name__ == "__main__":
    main()
