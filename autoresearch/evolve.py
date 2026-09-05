#!/usr/bin/env python3
"""
evolve — autonomous strategy-learning loop.

Each iteration:
  1. Reads run history from ~/offprem/reports/runs.json
  2. Reads current autoresearch/strategy.py
  3. Asks Claude to propose a new strategy.py that should improve avg sharpe
  4. Writes the new strategy.py
  5. Runs simrun.py --publish with label evolve-NNN
  6. Repeats

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 evolve.py --iterations 10
    python3 evolve.py --iterations 5 --dataset train

The HTML report and aggregate stats are auto-published to:
    https://assiamahs.github.io/offprem/
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import anthropic
except ImportError:
    sys.exit("pip install anthropic")

ROOT = Path(__file__).parent
STRATEGY_PATH = ROOT / "strategy.py"
RUNS_LOG = Path.home() / "offprem" / "reports" / "runs.json"
HISTORY_DIR = ROOT / "evolve_history"
HISTORY_DIR.mkdir(exist_ok=True)

MODEL = "claude-opus-4-7"
MAX_TOKENS = 8000

SYSTEM = """You are a quant researcher iterating on a Hyperliquid 15-min crypto strategy.

You output ONE Python file: a complete strategy.py that exports
    generate_signals(df: pd.DataFrame) -> pd.Series
where the Series has values in {-1, 0, 1} (short, flat, long), aligned to df.index.

Hard rules:
- df has columns: open, high, low, close, volume. DateTimeIndex.
- Return a pd.Series, NOT numpy array.
- No look-ahead: signal at i may only use bars 0..i.
- No external network calls, no extra deps beyond pandas/numpy.
- Keep total file under 200 lines.
- Output ONLY the python code in a single ```python``` block. No prose."""


def get_history(limit: int = 20) -> list:
    if not RUNS_LOG.exists():
        return []
    runs = json.loads(RUNS_LOG.read_text())
    return runs[:limit]


def get_per_coin(slug: str) -> dict:
    """Pull aggregate per-coin stats from a published report by parsing its data payload."""
    report = Path.home() / "offprem" / "reports" / slug / "index.html"
    if not report.exists():
        return {}
    text = report.read_text()
    m = re.search(r"const DATA = (\{.*?\});\s*\n", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return {r["coin"]: {k: r.get(k) for k in ("pnl_pct", "sharpe", "max_dd_pct", "trades", "win_rate")}
                for r in data["aggregate"] if "pnl_pct" in r}
    except Exception:
        return {}


def build_prompt(history: list, current_strategy: str) -> str:
    parts = ["# Current strategy.py\n```python\n" + current_strategy + "\n```\n"]
    if history:
        parts.append("# Past runs (newest first)\n")
        for r in history[:10]:
            line = f"- {r['label']}: avg_return={r['avg_return_pct']:+.2f}%  avg_sharpe={r['avg_sharpe']}"
            per_coin = get_per_coin(r["slug"])
            if per_coin:
                breakdown = ", ".join(f"{c}:{s['pnl_pct']:+.1f}%/{s['sharpe']}" for c, s in per_coin.items())
                line += f"\n    {breakdown}"
            parts.append(line)
        parts.append("")
    parts.append(
        "# Task\n"
        "Propose the NEXT strategy.py. Goal: positive avg_sharpe and avg_return on 15-min OHLCV "
        "across BTC/ETH/SOL/HYPE/XRP/SUI/DOGE/AVAX. Position sizing is fixed at 20% per trade; "
        "fees+slippage are 4.5bps round-trip taker. \n\n"
        "Diagnose what failed in past runs (overtrading? bad regime filter? wrong direction?), "
        "then ship a strategy that addresses it. Avoid overfitting — favor 1-3 robust signals over "
        "many fragile ones. Output the full file in one ```python``` block."
    )
    return "\n".join(parts)


def extract_code(text: str) -> str:
    m = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if not m:
        m = re.search(r"```\n(.*?)```", text, re.DOTALL)
    if not m:
        sys.exit("model returned no ```python block:\n" + text[:500])
    return m.group(1).strip() + "\n"


def validate(code: str) -> str | None:
    if "def generate_signals" not in code:
        return "missing generate_signals()"
    if "import" not in code:
        return "no imports"
    try:
        compile(code, "<strategy>", "exec")
    except SyntaxError as e:
        return f"syntax error: {e}"
    return None


def snapshot(label: str, code: str):
    (HISTORY_DIR / f"{label}.py").write_text(code)


def run_iteration(client, n: int, dataset: str) -> dict:
    label = f"evolve-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{n:03d}"
    print(f"\n=== {label} ===")

    history = get_history()
    current = STRATEGY_PATH.read_text()
    prompt = build_prompt(history, current)

    print("asking claude...")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    code = extract_code(text)

    err = validate(code)
    if err:
        print(f"validation failed: {err} — skipping iteration")
        return {"label": label, "error": err}

    snapshot(label, current)  # save what we're replacing
    STRATEGY_PATH.write_text(code)
    print(f"wrote new strategy.py ({len(code.splitlines())} lines)")

    result = subprocess.run(
        [sys.executable, str(ROOT / "simrun.py"), str(ROOT),
         "--dataset", dataset, "--label", label, "--publish", "--no-open"],
        capture_output=True, text=True,
    )
    print(result.stdout[-800:])
    if result.returncode != 0:
        print("simrun stderr:", result.stderr[-400:])
        STRATEGY_PATH.write_text(current)  # rollback
        return {"label": label, "error": "simrun failed"}

    return {"label": label, "ok": True}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--iterations", type=int, default=5)
    p.add_argument("--dataset", default="test", choices=["train", "test"])
    p.add_argument("--sleep", type=int, default=2, help="seconds between iterations")
    args = p.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("set ANTHROPIC_API_KEY first")
    if not shutil.which("git"):
        sys.exit("git required")

    client = anthropic.Anthropic()
    print(f"evolving over {args.iterations} iterations on dataset={args.dataset}")
    print(f"reports: https://assiamahs.github.io/offprem/")

    for n in range(1, args.iterations + 1):
        try:
            run_iteration(client, n, args.dataset)
        except Exception as e:
            print(f"iteration {n} blew up: {e}")
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
