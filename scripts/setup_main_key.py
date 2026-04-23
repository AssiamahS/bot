#!/usr/bin/env python3
"""Store the HL main-wallet private key in macOS Keychain so scripts can
perform transfers/deposits/withdraws automatically without asking you
every time.

Why Keychain and not a plaintext file:
  - Encrypted at rest (tied to your macOS login)
  - Never touches disk in plaintext, ever
  - Survives reboots
  - `security` CLI requires your password to export, so even full disk
    read-access doesn't hand over the key

How to use:
    python3 scripts/setup_main_key.py          # prompts for the key, validates, stores
    python3 scripts/setup_main_key.py --verify # checks it's stored and correct
    python3 scripts/setup_main_key.py --rotate # replace with a new key

Your main key goes into Keychain under:
    service:  hyperliquid-sol-main
    account:  <your wallet address>

Read back from code via `load_main_key_from_keychain()` in this file.
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from pathlib import Path

try:
    from eth_account import Account
except ImportError:
    print("Missing dependency: pip install eth_account")
    sys.exit(1)


SERVICE = "hyperliquid-sol-main"
REPO = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO / "config.json"


def expected_main_address() -> str:
    cfg = json.loads(CONFIG_PATH.read_text())
    return cfg.get("wallet_address", "")


def keychain_set(account: str, value: str) -> None:
    subprocess.run([
        "security", "add-generic-password",
        "-a", account,
        "-s", SERVICE,
        "-w", value,
        "-U",  # update if exists
        "-T", "",  # deny all apps by default
    ], check=True)


def keychain_get(account: str) -> str | None:
    try:
        r = subprocess.run([
            "security", "find-generic-password",
            "-a", account,
            "-s", SERVICE,
            "-w",
        ], check=True, capture_output=True, text=True)
        return r.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def keychain_delete(account: str) -> None:
    subprocess.run([
        "security", "delete-generic-password",
        "-a", account,
        "-s", SERVICE,
    ], check=False, capture_output=True)


def load_main_key_from_keychain(address: str | None = None) -> str | None:
    """Public API used by margin_helper and other scripts."""
    acct = address or expected_main_address()
    if not acct:
        return None
    return keychain_get(acct)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="check the stored key")
    ap.add_argument("--rotate", action="store_true", help="replace the stored key")
    args = ap.parse_args()

    expected = expected_main_address()
    if not expected:
        print("config.json has no wallet_address; set it before running this.")
        return 1

    if args.verify:
        key = keychain_get(expected)
        if not key:
            print(f"no key stored for {expected[:12]}… in Keychain service {SERVICE!r}")
            return 1
        derived = Account.from_key(key).address
        ok = derived.lower() == expected.lower()
        print(f"Keychain entry exists: {ok}")
        print(f"  expected main address: {expected}")
        print(f"  derived from stored:   {derived}")
        return 0 if ok else 2

    if args.rotate:
        keychain_delete(expected)
        print("old key deleted. paste the new one below.")

    existing = keychain_get(expected)
    if existing and not args.rotate:
        print(f"A key is already stored for {expected[:12]}…")
        print("Run with --rotate to replace it, or --verify to check it.")
        return 0

    print("=" * 64)
    print("HL main wallet key setup")
    print("=" * 64)
    print(f"Expected main address (from config.json): {expected}")
    print()
    print("Export your MAIN wallet private key (the one that holds the USDC,")
    print("not the agent key currently in config.json).")
    print()
    print("MetaMask: ⋮ → Account details → Show private key → password → copy.")
    print("Rabby:    extension → ⋮ → Manage Addresses → pick → Private Key.")
    print("Ledger:   NOT SUPPORTED (key never leaves device). Use the")
    print("          interactive UI-poll path via margin_helper instead.")
    print()
    print("Paste below. Your input is hidden (getpass). Ctrl-C to abort.")
    print("=" * 64)

    try:
        key = getpass.getpass("main private key (0x...): ").strip()
    except KeyboardInterrupt:
        print("\naborted")
        return 1

    if not key.startswith("0x") or len(key) < 64:
        print("that doesn't look like a private key (expected 0x… 64 hex chars)")
        return 1

    try:
        derived = Account.from_key(key).address
    except Exception as e:
        print(f"key invalid: {e}")
        return 1

    if derived.lower() != expected.lower():
        print(f"derived address {derived} != config wallet_address {expected}")
        print("That's the WRONG key. Agent key? Different wallet? Aborting.")
        return 1

    keychain_set(expected, key)
    print()
    print(f"OK. key stored under service={SERVICE!r} account={expected}")
    print(f"verify anytime with:  python3 {Path(__file__).name} --verify")
    print(f"rotate anytime with:  python3 {Path(__file__).name} --rotate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
