#!/usr/bin/env python3
"""Risk manager — wraps every order. Kills the bot if daily loss exceeds threshold.

Usage in a strategy:
    from risk_manager import RiskManager
    rm = RiskManager(max_daily_loss_pct=3.0, max_position_pct=30.0)
    if rm.allow_order(coin, usd_size, current_equity, signed_pnl_today):
        place_order(...)
        rm.record_fill(usd_size, fee)
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).parent / "risk_logs"
LOG_DIR.mkdir(exist_ok=True)


class RiskManager:
    def __init__(self, max_daily_loss_pct: float = 3.0, max_position_pct: float = 30.0,
                 max_total_notional_pct: float = 200.0, kill_file: Path = None):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_position_pct = max_position_pct
        self.max_total_notional_pct = max_total_notional_pct
        self.kill_file = kill_file or (LOG_DIR / "KILLED")
        self.today = date.today().isoformat()
        self.daily_log = LOG_DIR / f"{self.today}.jsonl"

    def is_killed(self) -> bool:
        return self.kill_file.exists()

    def kill(self, reason: str):
        self.kill_file.write_text(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }))
        self._log({"event": "KILL", "reason": reason})

    def reset(self):
        if self.kill_file.exists():
            self.kill_file.unlink()

    def allow_order(self, coin: str, usd_size: float, equity: float,
                    pnl_today_usd: float, current_total_notional: float = 0.0) -> tuple[bool, str]:
        if self.is_killed():
            return False, f"bot killed (see {self.kill_file})"
        pct_pnl = (pnl_today_usd / equity) * 100 if equity > 0 else 0
        if pct_pnl <= -self.max_daily_loss_pct:
            self.kill(f"daily loss {pct_pnl:.2f}% exceeded {-self.max_daily_loss_pct}%")
            return False, f"daily loss limit hit"
        pos_pct = (usd_size / equity) * 100 if equity > 0 else 100
        if pos_pct > self.max_position_pct:
            return False, f"order ${usd_size:.2f} = {pos_pct:.1f}% > {self.max_position_pct}% limit"
        if current_total_notional + usd_size > equity * (self.max_total_notional_pct / 100):
            return False, f"would exceed max total notional ({self.max_total_notional_pct}%)"
        return True, "ok"

    def record_fill(self, coin: str, usd_size: float, fee: float, side: str, pnl: float = 0):
        self._log({
            "event": "FILL", "coin": coin, "side": side,
            "usd_size": usd_size, "fee": fee, "pnl": pnl,
        })

    def _log(self, event: dict):
        event["ts"] = datetime.now(timezone.utc).isoformat()
        with self.daily_log.open("a") as f:
            f.write(json.dumps(event) + "\n")

    def daily_pnl(self) -> float:
        if not self.daily_log.exists():
            return 0.0
        total = 0.0
        with self.daily_log.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                    total += e.get("pnl", 0) - e.get("fee", 0)
                except Exception:
                    pass
        return total


if __name__ == "__main__":
    rm = RiskManager()
    print(f"daily pnl: ${rm.daily_pnl():.2f}")
    print(f"killed: {rm.is_killed()}")
    print(f"log: {rm.daily_log}")
