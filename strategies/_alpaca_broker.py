"""Alpaca REST broker implementing strategies.earnings_drift.BrokerInterface.

Uses stdlib urllib only — no external dependencies (we don't want alpaca-py
pulling in dateparser, pandas, etc. for what is a handful of REST calls).

Reads credentials from ~/hyperliquid-sol/.alpaca_keys (not committed; see
.gitignore). Paper trading endpoint by default. To go live (NOT recommended
until a paper strategy has 30 days of positive PnL), swap the ALPACA_ENDPOINT
line in .alpaca_keys to https://api.alpaca.markets.

Fractional shares are supported via the `notional` param in the order payload;
that's how we can buy $1 of AAPL without caring about share price. Alpaca
accepts notional orders down to $1.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


DEFAULT_KEYS_PATH = Path(__file__).resolve().parent.parent / ".alpaca_keys"


def _load_creds(path: Path = DEFAULT_KEYS_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Create it with ALPACA_KEY_ID=, ALPACA_SECRET=, "
            f"ALPACA_ENDPOINT= (see docs/MATH.md)."
        )
    creds = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        creds[k.strip()] = v.strip()
    for required in ("ALPACA_KEY_ID", "ALPACA_SECRET", "ALPACA_ENDPOINT"):
        if required not in creds:
            raise KeyError(f"{path} missing {required}")
    return creds


class AlpacaBroker:
    """Minimal Alpaca REST client. Implements strategies.earnings_drift.BrokerInterface."""

    def __init__(self, creds: Optional[dict] = None, timeout: int = 15):
        self._creds = creds or _load_creds()
        self._base = self._creds["ALPACA_ENDPOINT"].rstrip("/")
        self._timeout = timeout
        self._headers = {
            "APCA-API-KEY-ID": self._creds["ALPACA_KEY_ID"],
            "APCA-API-SECRET-KEY": self._creds["ALPACA_SECRET"],
            "Content-Type": "application/json",
        }

    # --- low-level -----------------------------------------------------

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Alpaca {method} {path} -> {e.code} {e.read().decode()[:300]}")

    # --- account -------------------------------------------------------

    def account(self) -> dict:
        return self._request("GET", "/v2/account")

    def account_equity(self) -> float:
        return float(self.account()["equity"])

    # --- positions -----------------------------------------------------

    def positions(self) -> list[dict]:
        try:
            return self._request("GET", "/v2/positions")
        except RuntimeError as e:
            if "404" in str(e):
                return []
            raise

    def position(self, ticker: str) -> Optional[dict]:
        try:
            return self._request("GET", f"/v2/positions/{ticker.upper()}")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    def close_position(self, ticker: str) -> dict:
        return self._request("DELETE", f"/v2/positions/{ticker.upper()}")

    # --- orders --------------------------------------------------------

    def buy(self, ticker: str, notional_usd: float) -> dict:
        """Fractional market buy. Alpaca requires notional >= $1."""
        if notional_usd < 1.0:
            raise ValueError(f"notional ${notional_usd:.2f} < $1 Alpaca minimum")
        return self._request("POST", "/v2/orders", {
            "symbol": ticker.upper(),
            "notional": f"{notional_usd:.2f}",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        })

    def sell(self, ticker: str, qty: float) -> dict:
        """Sell by share quantity. For notional sells, use close_position instead."""
        if qty <= 0:
            raise ValueError(f"qty {qty} must be > 0")
        return self._request("POST", "/v2/orders", {
            "symbol": ticker.upper(),
            "qty": f"{qty}",
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        })

    def orders(self, status: str = "all", limit: int = 50) -> list[dict]:
        return self._request("GET", f"/v2/orders?status={status}&limit={limit}")

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", f"/v2/orders/{order_id}")

    # --- market data (basic; full bar data uses a separate endpoint) --

    def latest_trade(self, ticker: str) -> dict:
        """Most recent trade for a ticker — useful to fetch a reference price."""
        data_base = self._creds.get("ALPACA_DATA_ENDPOINT", "https://data.alpaca.markets").rstrip("/")
        url = f"{data_base}/v2/stocks/{ticker.upper()}/trades/latest"
        req = urllib.request.Request(url, headers=self._headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Alpaca data {url} -> {e.code} {e.read().decode()[:300]}")


if __name__ == "__main__":
    b = AlpacaBroker()
    acct = b.account()
    print(f"status={acct['status']} cash=${float(acct['cash']):,.2f} "
          f"equity=${float(acct['equity']):,.2f}")
    print(f"open_positions={len(b.positions())} open_orders={len(b.orders(status='open'))}")
