#!/usr/bin/env python3
"""MCP server for controlling the Hyperliquid trading bot on VPS."""

import json
import subprocess
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hl-bot-control")

VPS = "ubuntu@44.205.58.31"
SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no"]
BOT_DIR = "~/hyperliquid-sol"


def ssh_cmd(cmd: str, timeout: int = 30) -> str:
    """Run a command on the VPS via SSH."""
    try:
        result = subprocess.run(
            ["ssh"] + SSH_OPTS + [VPS, cmd],
            capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout + result.stderr
        return output.strip() if output.strip() else "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: SSH command timed out"
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
def bot_start() -> str:
    """Start the trading bot on the VPS in a detached screen session."""
    return ssh_cmd(
        f"screen -dmS trader bash -c 'cd {BOT_DIR} && python3 -u trader.py >> trader.log 2>&1' "
        f"&& sleep 1 && screen -ls | grep trader && echo 'Bot started successfully' || echo 'WARNING: screen session not found'"
    )


@mcp.tool()
def bot_stop() -> str:
    """Stop the trading bot on the VPS by killing python3 and removing the lock file."""
    return ssh_cmd(
        "killall -9 python3 2>/dev/null; rm -f /tmp/trader.lock; echo 'Bot stopped'"
    )


@mcp.tool()
def bot_status() -> str:
    """Get current bot status including portfolio, PnL, fills, trips, win rate, and positions."""
    raw = ssh_cmd(f"cat {BOT_DIR}/trader_status.json")
    try:
        data = json.loads(raw)
        lines = []
        for key in ["portfolio_value", "total_pnl", "unrealized_pnl", "realized_pnl",
                     "fills_count", "trips_count", "win_rate", "uptime",
                     "positions", "active_pairs", "last_update"]:
            if key in data:
                val = data[key]
                if isinstance(val, (dict, list)):
                    lines.append(f"{key}: {json.dumps(val, indent=2)}")
                else:
                    lines.append(f"{key}: {val}")
        # Include any keys we didn't explicitly list
        shown = set(["portfolio_value", "total_pnl", "unrealized_pnl", "realized_pnl",
                      "fills_count", "trips_count", "win_rate", "uptime",
                      "positions", "active_pairs", "last_update"])
        for key, val in data.items():
            if key not in shown:
                if isinstance(val, (dict, list)):
                    lines.append(f"{key}: {json.dumps(val, indent=2)}")
                else:
                    lines.append(f"{key}: {val}")
        return "\n".join(lines) if lines else raw
    except (json.JSONDecodeError, TypeError):
        return raw


@mcp.tool()
def bot_log(lines: int = 30) -> str:
    """Return the last N lines of trader.log from the VPS.

    Args:
        lines: Number of log lines to return (default 30)
    """
    return ssh_cmd(f"tail -n {lines} {BOT_DIR}/trader.log")


@mcp.tool()
def bot_close_all() -> str:
    """Cancel all open orders and market-close all positions using the bot's credentials."""
    script = (
        'import json, sys\n'
        'from eth_account import Account\n'
        'from hyperliquid.info import Info\n'
        'from hyperliquid.exchange import Exchange\n'
        'from hyperliquid.utils import constants\n'
        'with open("config.json") as f: config = json.load(f)\n'
        'account = Account.from_key(config["wallet_private_key"])\n'
        'address = config["wallet_address"]\n'
        'info = Info(constants.MAINNET_API_URL, skip_ws=True)\n'
        'exchange = Exchange(account, constants.MAINNET_API_URL, account_address=address)\n'
        'import time\n'
        'orders = info.open_orders(address)\n'
        'for o in orders:\n'
        '    try: exchange.cancel(o["coin"], o["oid"])\n'
        '    except: pass\n'
        'print("Cancelled " + str(len(orders)) + " orders")\n'
        'time.sleep(1)\n'
        'state = info.user_state(address)\n'
        'for pos in state.get("assetPositions", []):\n'
        '    p = pos.get("position", {})\n'
        '    sz = float(p.get("szi", 0))\n'
        '    if sz != 0:\n'
        '        coin = p["coin"]\n'
        '        print("Closing " + coin + " sz=" + str(sz))\n'
        '        try: exchange.market_close(coin)\n'
        '        except Exception as e: print(str(e))\n'
        'print("Done")\n'
    )
    return ssh_cmd(f"cd {BOT_DIR} && python3 -c {_shell_quote(script)}", timeout=60)


@mcp.tool()
def bot_config_get() -> str:
    """Read and return the bot's config.json from the VPS."""
    raw = ssh_cmd(f"cat {BOT_DIR}/config.json")
    try:
        data = json.loads(raw)
        # Redact private key for safety
        if "wallet_private_key" in data:
            key = data["wallet_private_key"]
            data["wallet_private_key"] = key[:6] + "..." + key[-4:] if len(key) > 10 else "***"
        return json.dumps(data, indent=2)
    except (json.JSONDecodeError, TypeError):
        return raw


@mcp.tool()
def bot_config_set(key: str, value: str) -> str:
    """Update a specific key in the bot's config.json on the VPS.

    Args:
        key: The config key to update (e.g. min_spread_bps, pairs, order_size_usd)
        value: The new value (will be auto-parsed as JSON if possible, otherwise treated as string)
    """
    if key == "wallet_private_key":
        return "ERROR: Refusing to update wallet_private_key via MCP for security reasons"

    # Try to parse value as JSON (handles numbers, lists, booleans)
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value

    # Use jq-style update via python on the VPS
    update_script = f'''
import json
with open("config.json") as f:
    config = json.load(f)
config[{json.dumps(key)}] = {json.dumps(parsed)}
with open("config.json", "w") as f:
    json.dump(config, f, indent=2)
print(f"Updated {json.dumps(key)}: {{json.dumps({json.dumps(parsed)})}}")
'''
    return ssh_cmd(f"cd {BOT_DIR} && python3 -c {_shell_quote(update_script)}")


@mcp.tool()
def bot_restart() -> str:
    """Restart the bot (stop then start)."""
    stop_result = bot_stop()
    start_result = bot_start()
    return f"STOP: {stop_result}\nSTART: {start_result}"


def _shell_quote(s: str) -> str:
    """Quote a string for use as a single shell argument."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    mcp.run(transport="stdio")
