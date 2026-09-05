#!/usr/bin/env python3
"""
Telegram command listener for remote parameter control.
Runs as a background thread, polls for commands, modifies trader globals.
"""
import threading
import time
import json
import urllib.request
import urllib.parse
import traceback

# Commands:
#   /status  - current bot status
#   /params  - list all tunable params
#   /set <param> <value> - change a param
#   /pos     - current positions
#   /pause   - pause quoting
#   /resume  - resume quoting
#   /stop    - graceful shutdown
#   /trips   - recent round trip stats
#   /help    - show commands


class TelegramCommander:
    """Polls Telegram for commands and modifies trader state."""

    # All tunable parameters: name -> (type, description)
    TUNABLE = {
        "order_size_usd":       (float, "Order size in USD per level"),
        "min_spread_bps":       (float, "Minimum spread in basis points"),
        "profitability_mode":   (str,   "strict or aggressive"),
        "min_profit_bps":       (float, "Min profit per round trip (bps)"),
        "quote_levels":         (int,   "Number of price levels per side"),
        "level_spacing_ticks":  (int,   "Ticks between each level"),
        "max_drawdown":         (float, "Max drawdown before pause (0.15 = 15%)"),
        "max_inventory_usd":    (float, "Max inventory in USD"),
        "max_volatility_bps":   (float, "Max volatility before pause (bps)"),
        "cooldown_secs":        (int,   "Risk cooldown duration (seconds)"),
        "safety_bps_strict":    (float, "Safety margin in strict mode (bps)"),
        "safety_bps_aggressive":(float, "Safety margin in aggressive mode (bps)"),
        "flow_shift_bps":       (float, "Max fair value shift from flow (bps)"),
        "flow_widen_bps":       (float, "Extra spread during extreme flow (bps)"),

        "refresh_secs":         (int,   "Status refresh interval (seconds)"),
    }

    # Map lowercase param name -> trader.py global variable name
    PARAM_TO_GLOBAL = {
        "order_size_usd":       "ORDER_SIZE_USD",
        "min_spread_bps":       "MIN_SPREAD_BPS",
        "profitability_mode":   "PROFITABILITY_MODE",
        "min_profit_bps":       "MIN_PROFIT_BPS",
        "quote_levels":         "QUOTE_LEVELS",
        "level_spacing_ticks":  "LEVEL_SPACING_TICKS",
        "max_drawdown":         "MAX_DRAWDOWN",
        "max_inventory_usd":    "MAX_INVENTORY_USD",
        "max_volatility_bps":   "MAX_VOLATILITY_BPS",
        "cooldown_secs":        "COOLDOWN_SECS",
        "safety_bps_strict":    "SAFETY_BPS_STRICT",
        "safety_bps_aggressive":"SAFETY_BPS_AGGRESSIVE",
        "flow_shift_bps":       "FLOW_SHIFT_BPS",
        "flow_widen_bps":       "FLOW_WIDEN_BPS",

        "refresh_secs":         "REFRESH_SECS",
    }

    def __init__(self, token, chat_id, trader_module):
        self.token = token
        self.chat_id = str(chat_id)
        self.trader = trader_module  # reference to trader module globals
        self.last_update_id = 0
        self._thread = None
        self._running = False

    def send(self, msg):
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": msg,
                "parse_mode": "HTML",
            }).encode()
            urllib.request.urlopen(url, data=data, timeout=5)
        except Exception:
            pass

    def get_updates(self):
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates"
            params = urllib.parse.urlencode({
                "offset": self.last_update_id + 1,
                "timeout": 10,
                "allowed_updates": '["message"]',
            }).encode()
            req = urllib.request.Request(url, data=params, method="POST")
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            return data.get("result", [])
        except Exception:
            return []

    def get_param(self, name):
        """Read a parameter from trader module globals."""
        global_name = self.PARAM_TO_GLOBAL.get(name, name.upper())
        return getattr(self.trader, global_name, None)

    def set_param(self, name, value):
        """Set a parameter in trader module globals."""
        global_name = self.PARAM_TO_GLOBAL.get(name, name.upper())
        if hasattr(self.trader, global_name):
            setattr(self.trader, global_name, value)
            return True
        return False

    def handle_command(self, text, from_id):
        """Process a command and return response text."""
        # Security: only accept commands from authorized chat
        if str(from_id) != self.chat_id:
            return None

        parts = text.strip().split()
        cmd = parts[0].lower() if parts else ""

        if cmd == "/help":
            return (
                "<b>Bot Commands</b>\n"
                "/status - Bot status\n"
                "/params - List tunable params\n"
                "/set &lt;param&gt; &lt;value&gt; - Change param\n"
                "/pos - Current positions\n"
                "/pairs - Active pairs list\n"
                "/droppair &lt;COIN&gt; - Remove pair + close position\n"
                "/addpair &lt;COIN&gt; - Add a new pair\n"
                "/pause - Pause quoting\n"
                "/resume - Resume quoting\n"
                "/stop - Graceful shutdown\n"
                "/trips - Round trip stats\n"
                "/orphans - Close positions not in PAIRS\n"
                "/help - This message"
            )

        elif cmd == "/status":
            pv = self.trader.portfolio_value()
            ipv = self.trader.initial_portfolio_value or pv
            pnl = pv - ipv if ipv else 0
            uptime = (time.time() - self.trader.start_time) / 60
            fills = self.trader.total_trade_count
            trips = self.trader.round_trips
            mode = self.get_param("profitability_mode")
            gate_pct = (self.trader.quotes_skipped_profitability / max(self.trader.quote_attempts, 1)) * 100
            running = self.trader.running

            msg = (
                f"<b>{'RUNNING' if running else 'PAUSED'}</b>\n"
                f"Portfolio: ${pv:.2f}\n"
                f"PnL: {'+'if pnl>=0 else ''}{pnl:.4f}\n"
                f"Fills: {fills} | Trips: {trips}\n"
                f"Uptime: {uptime:.0f}m\n"
                f"Mode: {mode} | Gate skip: {gate_pct:.0f}%"
            )
            # Add trip stats
            ct = self.trader.completed_trips
            if ct:
                avg_net = sum(t["net"] for t in ct) / len(ct)
                total_net = sum(t["net"] for t in ct)
                winners = sum(1 for t in ct if t["net"] >= 0)
                msg += f"\nTrip net: ${total_net:.4f} | Avg: ${avg_net:.4f} | WR: {winners}/{len(ct)}"
            return msg

        elif cmd == "/params":
            lines = ["<b>Tunable Parameters</b>\n"]
            for name, (typ, desc) in self.TUNABLE.items():
                val = self.get_param(name)
                lines.append(f"<code>{name}</code> = {val}  <i>({desc})</i>")
            return "\n".join(lines)

        elif cmd == "/set":
            if len(parts) < 3:
                return "Usage: /set <param> <value>\nExample: /set order_size_usd 50"
            name = parts[1].lower()
            raw_value = parts[2]

            if name not in self.TUNABLE:
                return f"Unknown param: {name}\nUse /params to see available params"

            typ, desc = self.TUNABLE[name]
            try:
                if typ == int:
                    value = int(raw_value)
                elif typ == float:
                    value = float(raw_value)
                else:
                    value = raw_value.lower()
            except ValueError:
                return f"Invalid value. Expected {typ.__name__}: {raw_value}"

            # Validation
            if name == "profitability_mode" and value not in ("strict", "aggressive"):
                return "profitability_mode must be 'strict' or 'aggressive'"
            if name == "max_drawdown" and not (0 < value <= 1):
                return "max_drawdown must be between 0 and 1 (e.g. 0.15 = 15%)"
            if name == "order_size_usd" and value < 10:
                return "order_size_usd must be >= 10 (Hyperliquid minimum)"

            old = self.get_param(name)
            self.set_param(name, value)

            # Also update MIN_CAPTURE_BPS if fee-related params change
            if name in ("min_profit_bps",):
                new_capture = 2 * self.trader.MAKER_FEE_BPS + value
                self.trader.MIN_CAPTURE_BPS = new_capture

            return f"<b>Updated</b>\n<code>{name}</code>: {old} -> {value}"

        elif cmd == "/pos":
            positions = self.trader.last_balances.get("positions", {})
            if not positions:
                return "No open positions"
            lines = ["<b>Positions</b>"]
            for coin, pos in positions.items():
                sz = pos["size"]
                entry = pos["entry_price"]
                upnl = pos["unrealized_pnl"]
                direction = "LONG" if sz > 0 else "SHORT"
                lines.append(f"{coin}: {direction} {abs(sz)} @ ${entry:.2f} | uPnL: ${upnl:.4f}")
            return "\n".join(lines)

        elif cmd == "/pause":
            self.trader.quoting_paused = True
            return "Quoting PAUSED. Existing orders remain. Use /resume to continue."

        elif cmd == "/resume":
            self.trader.quoting_paused = False
            return "Quoting RESUMED."

        elif cmd == "/stop":
            self.trader.running = False
            return "Bot STOPPING. Will cancel all orders and exit."

        elif cmd == "/trips":
            ct = self.trader.completed_trips
            if not ct:
                return "No completed trips yet"
            recent = ct[-10:]
            lines = [f"<b>Last {len(recent)} Trips</b> (of {len(ct)} total)"]
            for i, t in enumerate(recent):
                net_sign = "+" if t["net"] >= 0 else ""
                emoji = "+" if t["net"] >= 0 else "-"
                lines.append(
                    f"{emoji} {t['coin']} ${t['buy_px']:.2f}->${t['sell_px']:.2f} "
                    f"net: {net_sign}${t['net']:.4f} ({t['duration']:.0f}s)"
                )
            total_net = sum(t["net"] for t in ct)
            avg_net = total_net / len(ct)
            winners = sum(1 for t in ct if t["net"] >= 0)
            lines.append(f"\nTotal: ${total_net:.4f} | Avg: ${avg_net:.4f} | WR: {winners}/{len(ct)}")
            return "\n".join(lines)

        elif cmd == "/pairs":
            pairs = self.trader.PAIRS
            coins = [self.trader.COIN_MAP.get(p, p) for p in pairs]
            return f"<b>Active Pairs ({len(pairs)})</b>\n" + "\n".join(f"  {p} ({c})" for p, c in zip(pairs, coins))

        elif cmd == "/droppair":
            if len(parts) < 2:
                return "Usage: /droppair SOL  (coin name, not pair name)"
            coin = parts[1].upper()
            pair = f"{coin}-PERP"
            if pair not in self.trader.PAIRS:
                return f"{pair} not in active pairs: {self.trader.PAIRS}"
            # Remove from all tracking structures
            self.trader.PAIRS.remove(pair)
            self.trader.COIN_MAP.pop(pair, None)
            self.trader.pair_fills.pop(pair, None)
            self.trader.pair_trade_count.pop(pair, None)
            self.trader.live_quotes.pop(coin, None)
            self.trader.trip_tracker.pop(coin, None)
            # Close position if any
            positions = self.trader.last_balances.get("positions", {})
            if coin in positions and positions[coin].get("size", 0) != 0:
                try:
                    self.trader.close_orphan_positions(
                        self.trader.info,
                        self.trader.exchange,
                        self.trader.WALLET_ADDRESS,
                    )
                    return f"Dropped {pair} and closing {coin} position"
                except Exception as e:
                    return f"Dropped {pair} from config but close failed: {e}"
            return f"Dropped {pair}. Remaining: {self.trader.PAIRS}"

        elif cmd == "/addpair":
            if len(parts) < 2:
                return "Usage: /addpair SOL  (coin name)"
            coin = parts[1].upper()
            pair = f"{coin}-PERP"
            if pair in self.trader.PAIRS:
                return f"{pair} already active"
            self.trader.PAIRS.append(pair)
            self.trader.COIN_MAP[pair] = coin
            self.trader.pair_fills[pair] = []
            self.trader.pair_trade_count[pair] = 0
            self.trader.price_history[coin] = []
            # Fetch metadata for new coin
            try:
                self.trader.fetch_asset_metadata()
            except Exception:
                pass
            return f"Added {pair}. Active: {self.trader.PAIRS}"

        elif cmd == "/orphans":
            try:
                closed = self.trader.close_orphan_positions(
                    self.trader.info,
                    self.trader.exchange,
                    self.trader.WALLET_ADDRESS,
                )
                if closed:
                    return f"Closed {closed} orphan position(s)"
                return "No orphan positions found"
            except Exception as e:
                return f"Orphan cleanup error: {e}"

        elif cmd.startswith("/"):
            return f"Unknown command: {cmd}\nUse /help for available commands"

        return None  # Not a command, ignore

    def poll_loop(self):
        """Main polling loop - runs in background thread."""
        self.send("Remote control active. Send /help for commands.")
        while self._running:
            try:
                updates = self.get_updates()
                for update in updates:
                    self.last_update_id = update.get("update_id", self.last_update_id)
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if text and chat_id:
                        response = self.handle_command(text, chat_id)
                        if response:
                            self.send(response)
            except Exception:
                traceback.print_exc()
                time.sleep(5)

    def start(self):
        """Start the command listener in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self.poll_loop, daemon=True, name="tg-commander")
        self._thread.start()
        print("  Telegram commander started")

    def stop(self):
        self._running = False
