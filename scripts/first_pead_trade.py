#!/usr/bin/env python3
"""First PEAD trade: scan Finnhub earnings, pick biggest surprise, paper-buy.

Defaults to dry-run. Pass --live to actually place orders via the Alpaca
paper endpoint. NEVER points at the live money endpoint; check .alpaca_keys
if you need to verify.

Usage:
    python3 scripts/first_pead_trade.py                  # preview only
    python3 scripts/first_pead_trade.py --live           # place paper orders
    python3 scripts/first_pead_trade.py --days 2 --top 3 # look back 2d, top 3 surprises
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from strategies import sizing
from strategies._alpaca_broker import AlpacaBroker
from strategies.earnings_drift import (
    EarningsEvent,
    signal,
    size_position,
    SURPRISE_THRESHOLD,
)


LOG_PATH = REPO_ROOT / "pead_trades.jsonl"
FINNHUB_KEY_PATH = REPO_ROOT / ".finnhub_key"


def load_finnhub_key() -> str:
    if not FINNHUB_KEY_PATH.exists():
        raise FileNotFoundError(f"Missing {FINNHUB_KEY_PATH}")
    for line in FINNHUB_KEY_PATH.read_text().splitlines():
        if line.startswith("FINNHUB_KEY="):
            return line.split("=", 1)[1].strip()
    raise KeyError("FINNHUB_KEY not in .finnhub_key")


def fetch_earnings(days_back: int, key: str) -> list[dict]:
    today = date.today()
    frm = (today - timedelta(days=days_back)).isoformat()
    to = today.isoformat()
    url = (
        "https://finnhub.io/api/v1/calendar/earnings"
        f"?from={frm}&to={to}&token={urllib.parse.quote(key)}"
    )
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read()).get("earningsCalendar", [])


def score_earnings(events: list[dict]) -> list[tuple[float, dict]]:
    """Return (surprise_pct, event_dict) tuples sorted by descending surprise."""
    scored = []
    for e in events:
        est = e.get("epsEstimate")
        act = e.get("epsActual")
        if est is None or act is None or abs(est) < 0.01:
            continue
        surp = (act - est) / abs(est)
        if abs(surp) < SURPRISE_THRESHOLD:
            continue  # below noise floor, skip
        scored.append((surp, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def append_log(record: dict) -> None:
    record["logged_at"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2, help="earnings calendar lookback")
    ap.add_argument("--top", type=int, default=10, help="max number of positions (diversification)")
    ap.add_argument("--per-position-pct", type=float, default=0.05,
                    help="fraction of equity per position (default 5%% for diversification)")
    ap.add_argument("--max-deploy-pct", type=float, default=0.60,
                    help="cap total deployed at this fraction of cash (default 60%%)")
    ap.add_argument("--min-price", type=float, default=3.0,
                    help="skip tickers under this price (penny-stock noise)")
    ap.add_argument("--live", action="store_true", help="actually place paper orders")
    args = ap.parse_args()

    broker = AlpacaBroker()
    acct = broker.account()
    equity = float(acct["equity"])
    cash = float(acct["cash"])
    print(f"account: {acct['account_number']} equity=${equity:,.2f} cash=${cash:,.2f}")
    if acct.get("status") != "ACTIVE":
        print(f"ERROR: account status is {acct['status']}, not ACTIVE")
        return 1

    # Market status check — warn if closed. Orders still queue for next open.
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://paper-api.alpaca.markets/v2/clock",
            headers={
                "APCA-API-KEY-ID": broker._creds["ALPACA_KEY_ID"],
                "APCA-API-SECRET-KEY": broker._creds["ALPACA_SECRET"],
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            clock = json.loads(r.read())
        if clock.get("is_open"):
            print("market: OPEN")
        else:
            print(f"market: CLOSED (next open {clock.get('next_open')}) — orders will queue")
    except Exception as e:
        print(f"market clock check failed: {e}")

    key = load_finnhub_key()
    events = fetch_earnings(args.days, key)
    scored = score_earnings(events)
    positive = [(s, e) for s, e in scored if s > 0]
    print(f"\nfinnhub: {len(events)} events, {len(positive)} positive surprises > {SURPRISE_THRESHOLD*100:.0f}%")

    if not positive:
        print("no qualifying surprises — nothing to trade")
        return 0

    # Already-held OR already-queued tickers to skip (don't double up). Orders
    # placed before market open are 'accepted' not filled, so checking positions
    # alone misses same-session duplicates.
    held = {p["symbol"]: p for p in broker.positions()}
    open_orders = broker.orders(status="open", limit=100)
    queued = {o["symbol"]: o for o in open_orders if o.get("side") == "buy"}
    blocked = {**held, **queued}
    if blocked:
        print(f"already held/queued: {', '.join(sorted(blocked))}")

    # Diversified sizing: equal-weight per position at --per-position-pct of equity,
    # capped at --max-deploy-pct of cash total. Take the top N by surprise.
    per_pos_usd = equity * args.per_position_pct
    budget_usd = cash * args.max_deploy_pct
    print(f"budget: ${budget_usd:.2f} ({args.max_deploy_pct*100:.0f}% of cash), "
          f"${per_pos_usd:.2f}/position ({args.per_position_pct*100:.1f}% of equity)")

    actions = []
    skipped = 0
    print(f"\ncandidates (surprise desc):")
    print(f"  {'SYM':<7} {'SURP':>8} {'ACT':>7} {'EST':>7} {'PRICE':>9} {'SIZE':>9}  NOTE")

    for surp, ev in positive:
        if len(actions) >= args.top:
            break
        if sum(a["size_usd"] for a in actions) + per_pos_usd > budget_usd:
            break
        sym = ev["symbol"]
        if sym in blocked:
            skipped += 1; continue
        try:
            price = float(broker.latest_trade(sym)["trade"]["p"])
        except Exception:
            skipped += 1; continue
        if price < args.min_price:
            print(f"  {sym:<7} {surp*100:>+7.1f}%                          ${price:>8.2f}            penny-stock skip")
            skipped += 1; continue

        event = EarningsEvent(
            ticker=sym,
            announced_on=date.fromisoformat(ev["date"]),
            estimated_eps=float(ev["epsEstimate"]),
            actual_eps=float(ev["epsActual"]),
            price_at_announce=price,
            price_current=price,
        )
        if signal(event) <= 0:
            skipped += 1; continue
        size_usd = min(per_pos_usd, cash * 0.95)  # equal-weight diversified
        if size_usd < 1.0:
            skipped += 1; continue
        print(f"  {sym:<7} {surp*100:>+7.1f}% {event.actual_eps:>7.2f} {event.estimated_eps:>7.2f} "
              f"${price:>8.2f} ${size_usd:>8.2f}")
        actions.append({"symbol": sym, "size_usd": size_usd, "price": price,
                        "surprise_pct": surp, "actual_eps": event.actual_eps,
                        "estimated_eps": event.estimated_eps})
        cash -= size_usd
    if skipped:
        print(f"  ({skipped} skipped — held, penny-stock, or no quote)")

    if not actions:
        print("\nno placeable actions")
        return 0

    if not args.live:
        print(f"\nDRY RUN — pass --live to actually submit {len(actions)} paper orders")
        return 0

    print(f"\nLIVE — submitting {len(actions)} paper orders")
    for a in actions:
        try:
            resp = broker.buy(a["symbol"], a["size_usd"])
            a["order_id"] = resp.get("id")
            a["status"] = resp.get("status")
            a["submitted_at"] = resp.get("submitted_at")
            print(f"  {a['symbol']:<7} order {a['order_id']} status={a['status']}")
            append_log({"event": "pead_entry", **a})
        except Exception as exc:
            print(f"  {a['symbol']:<7} FAIL: {exc}")
            append_log({"event": "pead_entry_failed", "symbol": a["symbol"],
                        "error": str(exc), "size_usd": a["size_usd"]})

    return 0


if __name__ == "__main__":
    sys.exit(main())
