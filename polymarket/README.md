# Polymarket BTC 5m Up/Down leg

Momentum-into-close strategy on Polymarket's `btc-updown-5m-*` markets: in the
final minutes of each 5-minute round, buy the leading side when its CLOB ask is
at/above threshold (default 0.70), hold to near-settlement with a stop-loss,
exit ~20s before close. Uses Hyperliquid candles as the BTC momentum oracle.

## Provenance (vendored 2026-07-04)

- `skills/5min-btc-polymarket/` — github.com/Novals83/5min-btc-polymarket @ 1c9aa81
  (strategy runner + control scripts; source of the viral @igus_ai tweet)
- `pm-hl-conservative-plus-repo/` — github.com/Novals83/polymarket-hl-strategy @ 33c3125
  (order execution engine the skill delegates to; dir renamed to match the
  path the skill expects)

Both audited before vendoring: only network calls are gamma-api.polymarket.com,
clob.polymarket.com (official py-clob-client) and api.hyperliquid.xyz/info
(public candle data). Keys never leave the official client.

Local change vs upstream: `btc5m_ctl.sh start` gained a `--dry-run` flag
(upstream always passes `--execute`).

## Layout

```
polymarket/
├── pm-hl-conservative-plus-repo/   # execution engine, .venv (py3.13), .env (gitignored)
└── skills/5min-btc-polymarket/scripts/btc5m_ctl.sh   # start|status|stop|report|logs
```

## Run

```bash
cd ~/hyperliquid-sol/polymarket/skills/5min-btc-polymarket

# dry-run (no orders, no creds needed)
scripts/btc5m_ctl.sh start --profile conservative --dry-run

# live — ONLY after filling pm-hl-conservative-plus-repo/.env
scripts/btc5m_ctl.sh start --profile conservative --stake-usd 5
scripts/btc5m_ctl.sh status | logs | report | stop
```

## Going live — required creds (`pm-hl-conservative-plus-repo/.env`)

- `PM_PRIVATE_KEY` — Polygon wallet key that funds/signs Polymarket orders
- `PM_FUNDER` / `PM_ADDRESS` — Polymarket proxy (funder) address
- `PM_API_KEY` / `PM_API_SECRET` / `PM_API_PASSPHRASE` — CLOB API creds
  (derivable from the private key; runner auto-derives when only key is set)
- Account needs USDC on Polygon deposited into Polymarket

`.env` is gitignored — never commit it. Keep stake small (`PM_MAX_NOTIONAL_USD`,
`--stake-usd`); the strategy buys favorites at 0.70-0.99, so one loss wipes
several wins. The tweet's +13k$ claim is unverified marketing, not a backtest.
