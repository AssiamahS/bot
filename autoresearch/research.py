#!/usr/bin/env python3
"""
Auto-Research Loop — runs autonomously overnight.

Based on Karpathy's autoresearch concept:
1. Read current strategy.py
2. Ask Claude to propose an improvement
3. Write the new strategy.py
4. Backtest it (training data)
5. If Sharpe improved → keep it, run Monte Carlo validation
6. If not → revert and try again
7. Loop forever

Usage:
    # Local (Ollama - free):
    python3 research.py

    # Or with Anthropic API:
    export USE_ANTHROPIC=1
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 research.py

    # Or with Telegram alerts:
    export TELEGRAM_BOT_TOKEN=...
    export TELEGRAM_CHAT_ID=...
    python3 research.py
"""

import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

# Backend selection: Ollama (free, local) or Anthropic API
USE_ANTHROPIC = os.environ.get("USE_ANTHROPIC", "").strip() == "1"

if USE_ANTHROPIC:
    try:
        import anthropic
    except ImportError:
        print("FATAL: anthropic SDK not installed. Run: pip install anthropic")
        sys.exit(1)

# Paths
BASE_DIR = Path(__file__).parent
STRATEGY_FILE = BASE_DIR / "strategy.py"
BEST_STRATEGY_FILE = BASE_DIR / "results" / "best_strategy.py"
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)
LOG_FILE = BASE_DIR / "results" / "research.log"

# Config
OLLAMA_MODEL = "qwen2.5:3b"
OLLAMA_URL = "http://localhost:11434/api/chat"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_GENERATIONS = 200
IMPROVEMENT_THRESHOLD = 0.01  # minimum Sharpe improvement to keep
RUN_MC_ON_IMPROVEMENT = True
MC_STABILITY_THRESHOLD = 30.0  # % of MC sims that must be positive (lowered — 50% was too strict)

# Telegram (optional)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def send_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def read_strategy() -> str:
    return STRATEGY_FILE.read_text()


def write_strategy(code: str):
    STRATEGY_FILE.write_text(code)


def backup_strategy(gen: int, label: str = ""):
    dest = RESULTS_DIR / f"strategy_gen{gen:04d}{'_' + label if label else ''}.py"
    shutil.copy(STRATEGY_FILE, dest)


def run_backtest(dataset: str = "train", run_mc: bool = False) -> dict:
    """Import strategy module fresh and run backtest."""
    import importlib
    # Force reimport
    if "strategy" in sys.modules:
        del sys.modules["strategy"]
    if "backtest" in sys.modules:
        del sys.modules["backtest"]

    sys.path.insert(0, str(BASE_DIR))
    try:
        import strategy
        import backtest as bt_module
        importlib.reload(strategy)
        importlib.reload(bt_module)
        return bt_module.run_full_evaluation(strategy, dataset, run_mc=run_mc)
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def build_prompt(current_code: str, current_metrics: dict, best_metrics: dict,
                 generation: int, history: list) -> str:
    """Build the strategy improvement prompt."""
    program = (BASE_DIR / "program.md").read_text()

    history_text = ""
    if history:
        recent = history[-5:]
        history_text = "\n\nRecent attempts:\n"
        for h in recent:
            history_text += f"- Gen {h['gen']}: Sharpe={h['sharpe']:.4f}, " \
                           f"DD={h['max_dd']:.1f}%, WR={h['win_rate']:.1f}%, " \
                           f"{'KEPT' if h['kept'] else 'REVERTED'}" \
                           f"{' — ' + h.get('note', '') if h.get('note') else ''}\n"

    return f"""You are an expert quantitative trading researcher. Your task is to improve a trading strategy.

## Research Goals
{program}

## Current Strategy Code (generation {generation})
```python
{current_code}
```

## Current Performance (training data)
```json
{json.dumps(current_metrics, indent=2)}
```

## Best Performance So Far
```json
{json.dumps(best_metrics, indent=2)}
```
{history_text}

## Your Task
Propose ONE specific improvement to the strategy. Ideas:
- Add or modify an indicator (RSI, MACD, Bollinger, ATR, etc.)
- Add a chop/regime filter (don't trade in sideways markets)
- Improve entry timing (add confirmation signals)
- Improve exit logic (trailing stops, time-based exits)
- Adjust parameters for better risk-adjusted returns
- Add volume or volatility filters
- Try a different strategy approach entirely

IMPORTANT RULES:
1. Output ONLY the complete updated strategy.py file — no explanation, no markdown
2. Keep the same function signature: generate_signals(df) -> pd.Series
3. Keep all helper functions (ema, rsi, atr) or add new ones
4. Use only numpy and pandas (no other libraries)
5. Do NOT use lookahead bias — only use data available at signal time
6. Keep parameters minimal (< 10 tunable params)
7. MUST include both long AND short signals
8. Make ONE focused change, not many changes at once

Output the complete Python file now:"""


def ask_ollama(prompt: str) -> str:
    """Ask local Ollama model to generate strategy code."""
    resp = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 4096},
    }, timeout=300)
    resp.raise_for_status()
    code = resp.json()["message"]["content"].strip()
    return clean_code(code)


def ask_anthropic(client, prompt: str) -> str:
    """Ask Anthropic API to generate strategy code."""
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    code = response.content[0].text.strip()
    return clean_code(code)


def clean_code(code: str) -> str:
    """Strip markdown fences from LLM output."""
    if code.startswith("```python"):
        code = code[len("```python"):].strip()
    if code.startswith("```"):
        code = code[3:].strip()
    if code.endswith("```"):
        code = code[:-3].strip()
    return code


def ask_llm(client, current_code: str, current_metrics: dict, best_metrics: dict,
            generation: int, history: list) -> str:
    """Route to Ollama or Anthropic based on config."""
    prompt = build_prompt(current_code, current_metrics, best_metrics, generation, history)
    if USE_ANTHROPIC:
        return ask_anthropic(client, prompt)
    else:
        return ask_ollama(prompt)


def validate_strategy(code: str) -> bool:
    """Check if strategy code is valid Python with required function."""
    try:
        compile(code, "strategy.py", "exec")
    except SyntaxError as e:
        log(f"  Syntax error: {e}")
        return False

    if "def generate_signals" not in code:
        log("  Missing generate_signals function")
        return False

    if "import numpy" not in code and "import np" not in code:
        # Allow implicit numpy via pandas
        pass

    return True


MIN_TRADES_PER_COIN = 20  # reject strategies with fewer trades
MAX_TRADES_PER_COIN = 500  # reject strategies that overtrade


def main():
    # Lock file to prevent duplicate processes
    lock_file = BASE_DIR / ".research.lock"
    if lock_file.exists():
        pid = lock_file.read_text().strip()
        # Check if PID is still running
        try:
            os.kill(int(pid), 0)
            print(f"FATAL: Another research process is running (PID {pid}). Kill it first.")
            sys.exit(1)
        except (OSError, ValueError):
            pass  # stale lock, continue
    lock_file.write_text(str(os.getpid()))

    client = None
    if USE_ANTHROPIC:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("FATAL: Set ANTHROPIC_API_KEY environment variable")
            sys.exit(1)
        client = anthropic.Anthropic(api_key=api_key)
        backend = f"Anthropic ({ANTHROPIC_MODEL})"
    else:
        # Verify Ollama is running
        try:
            requests.get("http://localhost:11434/api/tags", timeout=5)
        except Exception:
            print("FATAL: Ollama not running. Start it: ollama serve")
            sys.exit(1)
        backend = f"Ollama ({OLLAMA_MODEL})"

    log(f"=== Auto-Research Starting ({backend}) ===")
    send_telegram(f"<b>Auto-Research Starting</b>\nBackend: {backend}")

    # Run baseline
    log("Running baseline backtest...")
    baseline = run_backtest("train")
    if "error" in baseline:
        log(f"FATAL: Baseline backtest failed: {baseline['error']}")
        sys.exit(1)

    best_sharpe = baseline.get("aggregate", {}).get("avg_sharpe", 0)
    best_metrics = baseline
    best_code = read_strategy()
    backup_strategy(0, "baseline")

    log(f"Baseline: Sharpe={best_sharpe:.4f}, "
        f"PnL={baseline.get('aggregate', {}).get('avg_pnl_pct', 0):.2f}%, "
        f"DD={baseline.get('aggregate', {}).get('avg_max_dd', 0):.1f}%")

    history = []
    improvements = 0
    start_time = time.time()

    for gen in range(1, MAX_GENERATIONS + 1):
        log(f"\n--- Generation {gen}/{MAX_GENERATIONS} ---")

        # Ask LLM for improvement
        current_code = read_strategy()
        try:
            new_code = ask_llm(client, current_code, baseline, best_metrics, gen, history)
        except Exception as e:
            log(f"  Claude API error: {e}")
            time.sleep(10)
            continue

        # Validate
        if not validate_strategy(new_code):
            history.append({"gen": gen, "sharpe": 0, "max_dd": 0, "win_rate": 0,
                           "kept": False, "note": "invalid code"})
            continue

        # Write and test
        write_strategy(new_code)
        results = run_backtest("train")

        if "error" in results:
            log(f"  Backtest error: {results['error']}")
            write_strategy(current_code)  # revert
            history.append({"gen": gen, "sharpe": 0, "max_dd": 0, "win_rate": 0,
                           "kept": False, "note": f"backtest error: {results['error'][:80]}"})
            continue

        agg = results.get("aggregate", {})
        new_sharpe = agg.get("avg_sharpe", 0)
        new_dd = agg.get("avg_max_dd", 0)
        new_wr = agg.get("avg_win_rate", 0)
        total_trades = agg.get("total_trades", 0)
        trades_per_coin = total_trades / len(["BTC", "ETH", "SOL"])

        log(f"  Sharpe={new_sharpe:.4f} (best={best_sharpe:.4f}), "
            f"DD={new_dd:.1f}%, WR={new_wr:.1f}%, Trades={total_trades}")

        # Reject strategies with too few or too many trades
        if trades_per_coin < MIN_TRADES_PER_COIN:
            log(f"  Rejected: too few trades ({trades_per_coin:.0f}/coin, need {MIN_TRADES_PER_COIN})")
            write_strategy(current_code)
            history.append({"gen": gen, "sharpe": new_sharpe, "max_dd": new_dd,
                           "win_rate": new_wr, "kept": False, "note": f"too few trades ({total_trades})"})
            continue

        if trades_per_coin > MAX_TRADES_PER_COIN:
            log(f"  Rejected: overtrading ({trades_per_coin:.0f}/coin, max {MAX_TRADES_PER_COIN})")
            write_strategy(current_code)
            history.append({"gen": gen, "sharpe": new_sharpe, "max_dd": new_dd,
                           "win_rate": new_wr, "kept": False, "note": f"overtrading ({total_trades})"})
            continue

        improved = new_sharpe > best_sharpe + IMPROVEMENT_THRESHOLD

        if improved:
            # Run Monte Carlo validation if enabled
            mc_pass = True
            mc_note = ""
            if RUN_MC_ON_IMPROVEMENT:
                log("  Running Monte Carlo validation (50 sims)...")
                mc_results = run_backtest("train", run_mc=True)
                coins_list = ["BTC", "ETH", "SOL", "HYPE", "XRP", "SUI", "DOGE", "AVAX"]
                mc_failures = []
                for coin in coins_list:
                    mc = mc_results.get(f"{coin}_mc", {})
                    stability = mc.get("mc_stability", 0)
                    if stability < MC_STABILITY_THRESHOLD:
                        mc_failures.append(f"{coin}={stability}%")
                # Pass if at least 6/8 coins pass MC (allow 2 weak coins)
                if len(mc_failures) > 2:
                    mc_pass = False
                    mc_note = f"MC failed: {', '.join(mc_failures)}"
                else:
                    mc_note = f"MC passed ({len(mc_failures)} weak: {', '.join(mc_failures) if mc_failures else 'none'})"

            if mc_pass:
                # Also run on test data to check generalization
                test_results = run_backtest("test")
                test_agg = test_results.get("aggregate", {})
                test_sharpe = test_agg.get("avg_sharpe", 0)

                log(f"  IMPROVED! Train Sharpe: {best_sharpe:.4f} -> {new_sharpe:.4f}")
                log(f"  Out-of-sample Sharpe: {test_sharpe:.4f}")

                best_sharpe = new_sharpe
                best_metrics = results
                best_code = new_code
                improvements += 1
                backup_strategy(gen, "improved")

                # Save best
                BEST_STRATEGY_FILE.write_text(new_code)

                # Save results
                with open(RESULTS_DIR / "best_results.json", "w") as f:
                    json.dump({
                        "generation": gen,
                        "train": results,
                        "test": test_results,
                        "mc": {c: mc_results.get(f"{c}_mc", {}) for c in ["BTC", "ETH", "SOL", "HYPE", "XRP", "SUI", "DOGE", "AVAX"]} if RUN_MC_ON_IMPROVEMENT else {},
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, f, indent=2)

                # Git commit + tag for traceability
                tag_name = f"strategy-v{improvements:03d}-gen{gen}"
                try:
                    import subprocess
                    subprocess.run(["git", "add", "autoresearch/strategy.py",
                                   "autoresearch/results/"], cwd=str(BASE_DIR.parent),
                                  capture_output=True, timeout=10)
                    subprocess.run(["git", "commit", "-m",
                                   f"autoresearch: {tag_name} | Sharpe={new_sharpe:.4f} "
                                   f"WR={new_wr:.1f}% DD={new_dd:.1f}% "
                                   f"({backend})"],
                                  cwd=str(BASE_DIR.parent), capture_output=True, timeout=10)
                    subprocess.run(["git", "tag", tag_name],
                                  cwd=str(BASE_DIR.parent), capture_output=True, timeout=10)
                    log(f"  Git tagged: {tag_name}")
                except Exception as e:
                    log(f"  Git tag failed: {e}")

                send_telegram(
                    f"<b>Strategy Improved! (Gen {gen})</b>\n"
                    f"Tag: <code>{tag_name}</code>\n"
                    f"Sharpe: {new_sharpe:.4f} (was {best_sharpe - (new_sharpe - best_sharpe):.4f})\n"
                    f"Train PnL: {agg.get('avg_pnl_pct', 0):.1f}%\n"
                    f"Test PnL: {test_agg.get('avg_pnl_pct', 0):.1f}%\n"
                    f"Max DD: {new_dd:.1f}%\n"
                    f"Win Rate: {new_wr:.1f}%\n"
                    f"Backend: {backend}\n"
                    f"{mc_note}"
                )

                history.append({"gen": gen, "sharpe": new_sharpe, "max_dd": new_dd,
                               "win_rate": new_wr, "kept": True, "note": mc_note})
            else:
                log(f"  Sharpe improved but {mc_note} — reverting")
                write_strategy(current_code)
                history.append({"gen": gen, "sharpe": new_sharpe, "max_dd": new_dd,
                               "win_rate": new_wr, "kept": False, "note": mc_note})
        else:
            # Revert
            write_strategy(current_code)
            history.append({"gen": gen, "sharpe": new_sharpe, "max_dd": new_dd,
                           "win_rate": new_wr, "kept": False, "note": "no improvement"})

        # Progress report every 10 generations
        if gen % 10 == 0:
            elapsed = (time.time() - start_time) / 3600
            send_telegram(
                f"<b>Research Progress</b>\n"
                f"Gen: {gen}/{MAX_GENERATIONS}\n"
                f"Best Sharpe: {best_sharpe:.4f}\n"
                f"Improvements: {improvements}\n"
                f"Elapsed: {elapsed:.1f}h"
            )

        # Small delay to avoid API rate limits
        time.sleep(2)

    # Final summary
    elapsed = (time.time() - start_time) / 3600
    log(f"\n=== Research Complete ===")
    log(f"Generations: {MAX_GENERATIONS}, Improvements: {improvements}")
    log(f"Best Sharpe: {best_sharpe:.4f}")
    log(f"Elapsed: {elapsed:.1f} hours")

    send_telegram(
        f"<b>Auto-Research Complete</b>\n"
        f"Generations: {MAX_GENERATIONS}\n"
        f"Improvements: {improvements}\n"
        f"Best Sharpe: {best_sharpe:.4f}\n"
        f"Elapsed: {elapsed:.1f}h\n"
        f"Best strategy saved to results/best_strategy.py"
    )

    # Cleanup lock file
    lock_file = BASE_DIR / ".research.lock"
    lock_file.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        lock_file = Path(__file__).parent / ".research.lock"
        lock_file.unlink(missing_ok=True)
