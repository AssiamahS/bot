"""Preflight config & math sanity checks.

Run at bot startup. Blocks launch if configuration has math that cannot be
profitable (e.g. MAX_INVENTORY_USD smaller than ORDER_SIZE_USD, which was the
v2.13 force-close deadlock bug that cost the account $15-25).

CLI:
    python3 preflight.py                # validates ./config.json, exits 1 on fail
    python3 preflight.py --warn-only    # prints warnings but exits 0
"""
import json
import os
import sys

# Hyperliquid fee tier for accounts below ~$1M volume/month.
MAKER_FEE_BPS = 1.5
TAKER_FEE_BPS = 4.5
ROUND_TRIP_FEE_FLOOR_BPS = MAKER_FEE_BPS * 2  # 3.0 bps best case

# Pairs whose observed natural spread has averaged below the round-trip fee
# floor for extended windows. Listed here so config drift can't silently
# re-enable them without explicit override.
KNOWN_BAD_PAIRS = {
    "ARK-PERP": "natural spread 2-4 bps, below 3 bps maker/maker floor",
    "BTC-PERP": "natural spread ~1 bp, institutional-flow dominated",
    "ETH-PERP": "natural spread ~1 bp, institutional-flow dominated",
}

# Mapping of pair -> typical natural spread in bps from observed live data.
# Used to sanity-check that min_spread_bps is set realistically.
OBSERVED_SPREAD_BPS = {
    "ARK-PERP": 3.0,
    "APE-PERP": 6.0,
    "PENDLE-PERP": 10.0,
    "SOL-PERP": 2.0,
    "BTC-PERP": 1.0,
    "ETH-PERP": 1.0,
    "HYPE-PERP": 5.0,
    "FET-PERP": 4.0,
    "BLUR-PERP": 4.0,
}


def _check(cond, msg, failures):
    if not cond:
        failures.append(msg)


def validate(cfg: dict, trader_constants: dict | None = None) -> list[str]:
    """Return a list of failure messages. Empty list = config is sane."""
    fails: list[str] = []
    order_size = float(cfg.get("order_size_usd", 0))
    min_spread = float(cfg.get("min_spread_bps", 0))
    safety = float(cfg.get("safety_bps_strict", 0))
    pairs = list(cfg.get("pairs", []))

    # ---- order size sanity ----
    _check(
        order_size >= 5.0,
        f"order_size_usd={order_size} < $5 minimum. Fees dominate below this.",
        fails,
    )
    _check(
        order_size <= 500.0,
        f"order_size_usd={order_size} > $500. Likely a config error.",
        fails,
    )

    # ---- spread floor ----
    required = ROUND_TRIP_FEE_FLOOR_BPS + 2.0  # +2 bps edge target
    _check(
        min_spread >= required,
        f"min_spread_bps={min_spread} < {required} (fee floor + edge). "
        f"Will quote into negative-expectation trades.",
        fails,
    )

    # ---- safety margin ----
    _check(
        safety >= MAKER_FEE_BPS * 2,
        f"safety_bps_strict={safety} < {MAKER_FEE_BPS * 2} (2x maker fee). "
        f"No room for adverse selection.",
        fails,
    )

    # ---- pair selection ----
    for p in pairs:
        if p in KNOWN_BAD_PAIRS:
            fails.append(f"pair {p!r} is on the KNOWN_BAD_PAIRS list: {KNOWN_BAD_PAIRS[p]}")
        obs = OBSERVED_SPREAD_BPS.get(p)
        if obs is not None and obs < required:
            fails.append(
                f"pair {p!r} observed spread ~{obs} bps < required {required} bps. "
                f"Structurally unprofitable at our fee tier."
            )

    # ---- trader.py constants (if inspected) ----
    if trader_constants:
        max_inv = float(trader_constants.get("MAX_INVENTORY_USD", 0))
        max_ntl = float(trader_constants.get("MAX_POSITION_NOTIONAL", 0))
        _check(
            max_inv >= order_size * 1.1,
            f"MAX_INVENTORY_USD={max_inv} < order_size*1.1 ({order_size * 1.1:.2f}). "
            f"Every fill will trigger force-close — this is the v2.13 deadlock bug.",
            fails,
        )
        _check(
            max_ntl >= order_size * 1.5,
            f"MAX_POSITION_NOTIONAL={max_ntl} < order_size*1.5 ({order_size * 1.5:.2f}). "
            f"Single order fills will exceed total exposure cap.",
            fails,
        )

    return fails


def _load_trader_constants(trader_path: str) -> dict:
    """Extract key constants from trader.py by literal line parse.

    Kept deliberately dumb — we don't want to import trader.py (side effects).
    """
    out: dict = {}
    want = {"MAX_INVENTORY_USD", "MAX_POSITION_NOTIONAL"}
    try:
        with open(trader_path) as f:
            for line in f:
                for name in want:
                    if line.startswith(f"{name} ="):
                        # evaluate the RHS in a tiny sandbox so expressions like
                        # `max(30.0, ORDER_SIZE_USD * 1.2)` resolve.
                        rhs = line.split("=", 1)[1].split("#")[0].strip()
                        try:
                            out[name] = float(eval(rhs, {"max": max, "min": min, "ORDER_SIZE_USD": out.get("ORDER_SIZE_USD", 25.0)}))
                        except Exception:
                            pass
    except Exception:
        pass
    return out


def main() -> int:
    warn_only = "--warn-only" in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(here, "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    trader = _load_trader_constants(os.path.join(here, "trader.py"))
    fails = validate(cfg, trader_constants=trader)
    if not fails:
        print("preflight: OK")
        return 0
    tag = "WARN" if warn_only else "FAIL"
    print(f"preflight: {tag} — {len(fails)} issue(s)")
    for msg in fails:
        print(f"  - {msg}")
    return 0 if warn_only else 1


if __name__ == "__main__":
    sys.exit(main())
