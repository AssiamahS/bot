# HL wallet architecture — why "where's my money" keeps coming up

## The two-wallet setup

Hyperliquid recommends API bots use a **pattern with two distinct keys**:

| Key | Address | What it can do | Where it lives |
|---|---|---|---|
| **Main wallet** | `0x253831...5aE` | Own USDC, deposit, withdraw, transfer spot↔perps | MetaMask / hardware wallet / wherever you originally funded |
| **Agent wallet** | `0xa6693a...b4dc` | Place/cancel orders on main's behalf | `config.json` (`wallet_private_key`) |

The agent is authorized by the main wallet via HL's "approve agent wallet"
action. Once approved, the agent key can trade freely on the main's
capital — but can never withdraw or transfer the money itself. If an
attacker steals `config.json`, they can only trade, not drain the
account. That's the whole point.

**Consequence:** when a script needs to move USDC between spot and perps
(because BTC/ETH/SOL/HYPE only exist on the perps side while idle USDC
sits in spot), the agent key can't do it. Only the main key can.

## How the code handles it now (v2.23.0+)

`strategies/margin_helper.py` `ensure_perp_margin()` walks a tiered
fallback automatically. Any script that needs perps margin should call it
rather than trying to transfer directly. The order:

1. **Check first** — if perps already has enough, return immediately.
2. **Agent-key attempt** — try `usd_class_transfer` with whatever key is
   in `config.json`. Works if that key has transfer authority (e.g.
   non-agent / standalone wallet).
3. **Main-key file** — if `.main_key` exists at the repo root
   (gitignored, `chmod 600`), load it and retry the transfer.
4. **Interactive poll** — print the HL UI URL and exact amount, then poll
   `clearinghouseState` every 30s for up to 10 min. Auto-detects the
   moment the transfer lands and continues.

A strategy script never errors out with "transfer failed". It either
succeeds automatically or waits for a human to click.

## Two ways to make it fully hands-off

### Option A — store the main wallet private key in macOS Keychain (recommended)

Run the setup script once:
```bash
python3 scripts/setup_main_key.py
```
It prompts for your MetaMask/Rabby main-wallet private key (hidden input),
validates that it derives to the address in `config.json`, and stores it
in macOS Keychain under service `hyperliquid-sol-main`. Encrypted at rest,
tied to your macOS login — even full disk read-access can't extract it.

Verify anytime:
```bash
python3 scripts/setup_main_key.py --verify
```
Rotate (replace) anytime:
```bash
python3 scripts/setup_main_key.py --rotate
```
Hardware wallets (Ledger/Trezor): NOT SUPPORTED — key never leaves device.
Fall back to Option C (interactive UI poll) in that case.

### Option A' — plaintext `.main_key` file (fallback only)

If you're on a non-macOS machine, create `~/hyperliquid-sol/.main_key`:
```
MAIN_PRIVATE_KEY=0x<your_main_wallet_private_key>
```
Then `chmod 600 .main_key`. Gitignored. `ensure_perp_margin()` checks
Keychain first and falls back to this file. Less secure than Keychain
because the key sits in plaintext on disk.

### Option B — keep the main key elsewhere (more secure)

Leave `.main_key` absent. When a script needs a transfer, the interactive
poll kicks in. Click the UI link, approve the transfer in MetaMask (or
your hardware wallet), and the script continues when it sees the balance
arrive. Takes 10 seconds once, not every time — the poll window is 10
minutes and you only hit it when topping up perps.

This is the HL-recommended setup. Option A is a convenience override.

## What about hardware wallet signing?

HL has a direct-via-UI signing flow for hardware wallets, no private
key ever leaves the device. That's always the interactive-poll path in
this codebase — no special handling needed. The script doesn't know or
care whether the UI transfer came from a soft wallet or a Ledger; it
just sees the balance change on-chain.

## Deposits into HL from the outside world

Withdrawals are `exchange.withdraw3(...)` or the UI. Deposits are via
the bridge on Arbitrum (send USDC to the HL bridge address, takes a few
minutes to show on the exchange). Neither of these is agent-accessible;
both require the main wallet. Neither happens often enough for us to
script — once a year when topping up is fine manually.
