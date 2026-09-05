"""Post-Earnings Announcement Drift (PEAD) strategy.

Thesis (Bernard & Thomas 1989, replicated ~50x): stocks with earnings
surprises continue drifting in the direction of the surprise for 30-60
days after the announcement. The market under-reacts.

At retail size after fees, the captureable edge is 1-3% annually, with
Sharpe ~0.5. This is NOT get-rich-quick. But it's positive expected value,
unlike the MM bot that preceded it.

This file is a reference implementation with TODOs marked for the two
things we don't have yet:
  1. An equities broker with an API (Alpaca is the obvious pick — free
     paper trading, $1 min live, fractional shares)
  2. An earnings data source (Finnhub / Polygon / yfinance free tier)

To go live we need both. The sizing and position management math is
ready to run and imports from strategies/sizing.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from strategies import sizing


# ---- data types ------------------------------------------------------------

@dataclass
class EarningsEvent:
    ticker: str
    announced_on: date
    estimated_eps: float
    actual_eps: float
    price_at_announce: float
    price_current: float

    @property
    def surprise_pct(self) -> float:
        """Percentage surprise. Positive = beat, negative = miss.

        Uses |estimate| in the denominator so negative-EPS surprises are
        handled correctly (a firm expected to lose $0.10 that only lost
        $0.05 is a +50% surprise).
        """
        if abs(self.estimated_eps) < 1e-6:
            return 0.0
        return (self.actual_eps - self.estimated_eps) / abs(self.estimated_eps)

    @property
    def days_since(self) -> int:
        return (date.today() - self.announced_on).days

    @property
    def drift_since(self) -> float:
        return (self.price_current - self.price_at_announce) / self.price_at_announce


# ---- signal ----------------------------------------------------------------

SURPRISE_THRESHOLD = 0.05   # 5% surprise floor — filters noise
HOLD_MAX_DAYS = 45          # close no matter what at 45d — drift decays
HOLD_MIN_DAYS = 1           # enter day after announcement


def signal(event: EarningsEvent) -> int:
    """Return +1 / -1 / 0 for long / short / flat.

    Only flags extreme surprises (>5%). Academic result shows edge
    concentrates in the tails; middle surprises are too noisy to capture
    after fees.
    """
    if not (HOLD_MIN_DAYS <= event.days_since <= HOLD_MAX_DAYS):
        return 0
    s = event.surprise_pct
    if s > SURPRISE_THRESHOLD:
        return +1
    if s < -SURPRISE_THRESHOLD:
        return -1
    return 0


# ---- sizing ----------------------------------------------------------------

# Strategy-level risk budget. Start at 1% per trade = at $60 account,
# $0.60 at risk per position. Scales with account_equity automatically.
RISK_FRACTION = 0.01
STOP_DISTANCE_PCT = 0.08  # PEAD positions have ~8% drawdown tolerance


def size_position(account_equity: float, event: EarningsEvent) -> float:
    """Dollar notional to commit to this position. 0 = don't trade."""
    if signal(event) == 0:
        return 0.0
    base = sizing.fixed_fractional(
        account_equity=account_equity,
        risk_fraction=RISK_FRACTION,
        stop_distance_pct=STOP_DISTANCE_PCT,
    )
    # Weight by surprise magnitude (bigger surprise → bigger position, capped).
    weight = min(1.5, abs(event.surprise_pct) / SURPRISE_THRESHOLD)
    return base * weight


# ---- exit logic ------------------------------------------------------------

def should_exit(event: EarningsEvent) -> Optional[str]:
    """Return a reason to exit, or None to hold.

    Exits on:
      - hold window expired
      - stop loss hit (drift moved against signal by STOP_DISTANCE_PCT)
      - target hit (drift moved with signal by STOP_DISTANCE_PCT * 2)
    """
    s = signal(event)
    if event.days_since > HOLD_MAX_DAYS:
        return "hold_window_expired"
    if s == 0:
        return None
    signed_drift = event.drift_since if s > 0 else -event.drift_since
    if signed_drift <= -STOP_DISTANCE_PCT:
        return "stop_loss"
    if signed_drift >= STOP_DISTANCE_PCT * 2:
        return "target"
    return None


# ---- broker shim (TODO: wire to Alpaca) ------------------------------------

class BrokerInterface:
    """Minimal surface we need. Implement against Alpaca in a separate module.

    TODO: `pip install alpaca-py` and write strategies/_alpaca_broker.py
    implementing this protocol. Needs env vars ALPACA_KEY_ID and
    ALPACA_SECRET. Paper trading URL is https://paper-api.alpaca.markets.
    """

    def account_equity(self) -> float:
        raise NotImplementedError

    def buy(self, ticker: str, notional_usd: float) -> dict:
        """Fractional-share buy. Alpaca supports $1 minimum."""
        raise NotImplementedError

    def sell(self, ticker: str, qty: float) -> dict:
        raise NotImplementedError

    def position(self, ticker: str) -> Optional[dict]:
        raise NotImplementedError


# ---- earnings data shim (TODO: wire to Finnhub or yfinance) ----------------

def fetch_upcoming_earnings(days_ahead: int = 7) -> list[dict]:
    """Return dicts with {ticker, announce_date, estimated_eps}.

    TODO: use Finnhub /calendar/earnings (free tier 60 req/min) or
    yfinance's earnings_dates property. Cache locally so we don't hit the
    API on every run.
    """
    raise NotImplementedError(
        "Wire to a real earnings source (Finnhub or yfinance). "
        "See docs/MATH.md for why PEAD needs this data."
    )


def fetch_actual_eps(ticker: str, announce_date: date) -> Optional[float]:
    """Return the reported EPS once the announcement hits. None if not out yet."""
    raise NotImplementedError


# ---- daily tick ------------------------------------------------------------

def run_once(broker: BrokerInterface, events: list[EarningsEvent]) -> list[dict]:
    """One pass of the strategy. Call from a daily cron on the VPS.

    Returns a list of actions taken for logging.
    """
    equity = broker.account_equity()
    actions = []
    for ev in events:
        pos = broker.position(ev.ticker)
        if pos:
            reason = should_exit(ev)
            if reason:
                qty = float(pos["qty"])
                side = "buy" if qty < 0 else "sell"
                broker.sell(ev.ticker, abs(qty))
                actions.append({"action": "exit", "ticker": ev.ticker, "reason": reason})
            continue
        sig = signal(ev)
        if sig == 0:
            continue
        notional = size_position(equity, ev)
        if notional <= 0:
            continue
        if sig > 0:
            broker.buy(ev.ticker, notional)
            actions.append({"action": "enter_long", "ticker": ev.ticker, "notional": notional})
        # NOTE: shorting requires Alpaca margin account, not cash.
    return actions


if __name__ == "__main__":
    # Quick sanity check — does the math work on a toy event?
    from pprint import pprint
    ev = EarningsEvent(
        ticker="TOY",
        announced_on=date.today() - timedelta(days=2),
        estimated_eps=1.00,
        actual_eps=1.15,  # 15% beat
        price_at_announce=100.0,
        price_current=103.0,
    )
    print(f"surprise: {ev.surprise_pct:+.2%}")
    print(f"signal:   {signal(ev):+d}")
    print(f"size at $61 account: ${size_position(61.0, ev):.2f}")
    print(f"size at $10K account: ${size_position(10_000.0, ev):.2f}")
    print(f"exit?: {should_exit(ev)}")
