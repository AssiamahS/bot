#!/usr/bin/env python3
"""Run a strategy across HIP-3 markets (oil, silver, stocks). Same engine as simrun
but loads from data/hip3/ and data/funding/<slug>.csv."""

import argparse, importlib.util, json, sys, webbrowser
from pathlib import Path
import numpy as np
import pandas as pd

import simrun  # reuse render_html, run_with_trace constants


HIP3_DIR = Path(__file__).parent / "data" / "hip3"
FUNDING_DIR = Path(__file__).parent / "data" / "funding"
DEFAULT_COINS = ["xyz_CL", "xyz_SILVER", "xyz_GOLD", "xyz_NVDA", "xyz_TSLA", "xyz_AAPL", "xyz_MU"]


def load_hip3(slug: str, dataset: str) -> pd.DataFrame:
    path = HIP3_DIR / f"{slug}_{dataset}.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    fpath = FUNDING_DIR / f"{slug}.csv"
    if fpath.exists():
        f = pd.read_csv(fpath)
        f.index = pd.to_datetime(f.iloc[:, 0], utc=True, format="ISO8601")
        df = df.join(f["funding_rate"].astype(float), how="left")
        df["funding_rate"] = df["funding_rate"].ffill().fillna(0.0)
    else:
        df["funding_rate"] = 0.0
    return df


def build_payload(strategy_module, dataset: str, coins: list) -> dict:
    panels, aggregate = [], []
    for coin in coins:
        try:
            df = load_hip3(coin, dataset)
            if len(df) < 50:
                panels.append({"coin": coin, "error": f"only {len(df)} bars"})
                aggregate.append({"coin": coin, "error": "too few bars"})
                continue
            signals = strategy_module.generate_signals(df)
            r = simrun.run_with_trace(df, signals)
            ts = df.index.strftime("%Y-%m-%d %H:%M").tolist()
            panels.append({
                "coin": coin, "timestamps": ts,
                "open": df["open"].round(4).tolist(), "high": df["high"].round(4).tolist(),
                "low": df["low"].round(4).tolist(), "close": df["close"].round(4).tolist(),
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("folder", nargs="?", default=".")
    p.add_argument("--dataset", default="test", choices=["train", "test"])
    p.add_argument("--coins", nargs="*", default=DEFAULT_COINS)
    p.add_argument("--label", default=None)
    p.add_argument("--publish", action="store_true")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    folder = Path(args.folder).resolve()
    strat = simrun.load_strategy(folder)
    payload = build_payload(strat, args.dataset, args.coins)
    label = args.label or f"hip3-{folder.name}"
    out = folder / "sim_report_hip3.html"
    out.write_text(simrun.render_html(payload, label))
    print(f"wrote {out}")
    for r in payload["aggregate"]:
        if "error" in r:
            print(f"  {r['coin']}: ERROR {r['error']}")
        else:
            print(f"  {r['coin']}: {r['pnl_pct']:+.2f}%  sharpe={r['sharpe']}  trades={r['trades']}")
    if args.publish:
        simrun.publish_to_offprem(out, label, payload, f"hip3:{folder}")
    if not args.no_open:
        webbrowser.open(f"file://{out}")


if __name__ == "__main__":
    main()
