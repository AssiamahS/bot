"""HL margin management — makes "where the money is" invisible to strategies.

The problem this solves:
  Hyperliquid accounts split USDC between spot (`spotClearinghouseState`)
  and perps (`clearinghouseState`). BTC/ETH/SOL perps trade on the perps
  side. Idle USDC sits on the spot side by default. Moving between them
  requires `exchange.usd_class_transfer(amount, to_perp=True/False)`.

  But: if the private key in config.json is an AGENT KEY (HL's
  recommended security setup for API trading), transfers fail with
  "Must deposit before performing actions" because agents don't have
  transfer authority — only order-placement authority. Transfers need
  the MAIN wallet key.

The solution this module provides:

  ensure_perp_margin(info, exchange, main_wallet_addr, needed_usd):
      Walks a tiered fallback:
        1. Already have enough perps margin? Done, no action.
        2. Do we have a main_key (for transfer authority)? Try transfer.
        3. Is there a .main_key file locally? Load it and try transfer.
        4. Neither — print UI deep-link, poll every 30s up to 10 min
           waiting for the user to do the transfer manually in the HL
           web app. Auto-detect the moment it arrives and continue.

  No more "transfer failed, re-run script". No more "where is the money".
  The strategy scripts just call ensure_perp_margin() and proceed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
MAIN_KEY_PATH = REPO / ".main_key"
POLL_INTERVAL_SECS = 30
POLL_TIMEOUT_SECS = 600  # 10 min
UI_URL = "https://app.hyperliquid.xyz/"


def _perps_equity(info, wallet_addr: str) -> float:
    state = info.user_state(wallet_addr)
    return float(state["marginSummary"]["accountValue"])


def _spot_usdc(info, wallet_addr: str) -> float:
    state = info.spot_user_state(wallet_addr)
    return next((float(b["total"]) for b in state.get("balances", []) if b["coin"] == "USDC"), 0.0)


def _load_main_key() -> Optional[str]:
    if not MAIN_KEY_PATH.exists():
        return None
    for line in MAIN_KEY_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("MAIN_PRIVATE_KEY="):
            return line.split("=", 1)[1].strip()
        if line.startswith("0x") and len(line) >= 64:
            return line
    return None


def _try_transfer(exchange, amount_usd: float, to_perp: bool = True) -> tuple[bool, str]:
    """Returns (success, message)."""
    try:
        resp = exchange.usd_class_transfer(amount_usd, to_perp=to_perp)
        if isinstance(resp, dict) and resp.get("status") == "err":
            return False, str(resp.get("response", resp))
        return True, str(resp)
    except Exception as e:
        return False, str(e)


def ensure_perp_margin(
    info,
    exchange,
    main_wallet_addr: str,
    needed_usd: float,
    *,
    interactive: bool = True,
) -> bool:
    """Guarantee at least `needed_usd` of free margin on perps for main_wallet.

    Returns True when the target is met (possibly after waiting for a
    user-performed transfer). Returns False only if interactive=False
    and the shortfall can't be covered automatically.
    """
    perps = _perps_equity(info, main_wallet_addr)
    if perps >= needed_usd:
        return True

    shortfall = needed_usd - perps
    spot = _spot_usdc(info, main_wallet_addr)
    print(f"[margin] perps=${perps:.2f} needed=${needed_usd:.2f} shortfall=${shortfall:.2f} spot_usdc=${spot:.2f}")

    if spot < shortfall:
        print(f"[margin] spot USDC ${spot:.2f} insufficient for shortfall ${shortfall:.2f}; "
              f"deposit more USDC at {UI_URL} or reduce the basket")
        return False

    # Path 1 — try with the existing exchange object (may be agent key).
    print(f"[margin] attempting spot->perps transfer of ${shortfall:.2f} with current signer...")
    ok, msg = _try_transfer(exchange, shortfall, to_perp=True)
    if ok:
        print(f"[margin] transfer OK: {msg[:120]}")
        time.sleep(3)
        return _perps_equity(info, main_wallet_addr) >= needed_usd

    print(f"[margin] current signer cannot transfer: {msg[:160]}")

    # Path 2 — try with main_key from .main_key file.
    main_key = _load_main_key()
    if main_key:
        print(f"[margin] retrying with main key from .main_key...")
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        try:
            main_exchange = Exchange(Account.from_key(main_key), constants.MAINNET_API_URL)
            ok, msg = _try_transfer(main_exchange, shortfall, to_perp=True)
            if ok:
                print(f"[margin] transfer OK via main key: {msg[:120]}")
                time.sleep(3)
                return _perps_equity(info, main_wallet_addr) >= needed_usd
            print(f"[margin] main-key transfer also failed: {msg[:160]}")
        except Exception as e:
            print(f"[margin] main-key exchange setup failed: {e}")

    # Path 3 — manual UI transfer with polling.
    if not interactive:
        return False

    print("\n" + "=" * 60)
    print(f"  ACTION NEEDED: transfer ${shortfall:.2f} USDC from spot to perps")
    print(f"  1. Open: {UI_URL}")
    print(f"  2. Portfolio → Transfer → spot to perp → ${shortfall:.2f}")
    print(f"  3. Confirm in your wallet")
    print(f"  I'll keep watching — this script will auto-continue when the")
    print(f"  transfer lands. Timing out in {POLL_TIMEOUT_SECS // 60} min.")
    print("=" * 60 + "\n")

    deadline = time.time() + POLL_TIMEOUT_SECS
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECS)
        new_perps = _perps_equity(info, main_wallet_addr)
        if new_perps >= needed_usd:
            print(f"[margin] transfer detected — perps now ${new_perps:.2f}")
            return True
        remaining = int(deadline - time.time())
        print(f"[margin] still waiting... perps=${new_perps:.2f}  (timeout in {remaining}s)")

    print(f"[margin] timed out after {POLL_TIMEOUT_SECS}s — transfer never arrived")
    return False
