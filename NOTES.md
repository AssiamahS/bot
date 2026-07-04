# hyperliquid-sol — working notes

Surfaced automatically at session start. Append a one-line lesson here after any
non-obvious fix. Outcome-first, plain language.

## Architecture (load-bearing facts)
- The bots run **locally on this Mac via launchd** (`com.kim.bots` → `autoresearch/launch_all.sh`).
  The VPS `44.205.58.31` is **dead** (SSH times out since ~Jun 2026) — do not assume remote.
- `launch_all.sh` runs 5 HIP-3 funding bots on the `xyz` dex: SILVER, MU, NVDA, AAPL, TSLA
  (`live_funding.py --coin xyz:<X> --poll-secs 600 --maker`). KeepAlive `wait`s forever so
  launchd doesn't kill the process group ~10s after launch.
- Config: `config.json` (holds the private key — **never commit it**). Secrets stay local.

## Hard-won lessons (gotchas that cost real money/time)
- **Every HL info endpoint is per-dex.** `clearinghouseState`, `allMids`, and `meta` all need
  `"dex":"xyz"` for HIP-3 markets. Missing `dex=` silently returns nothing → `our_size` reads 0 →
  bots restack opens every poll. This caused ~3x oversized positions overnight (TSLA $106 vs $15).
- **Unified-equity math is per-dex too.** Spot `total` already includes the `hold` backing HIP-3
  margin; add each dex's `accountValue` separately. The old calc double-counted ($115.98 vs $91.01).
- **Telegram alerts with underscores 400 under Markdown** `parse_mode` and get silently dropped.
  Fix: retry the send as plain text. Verify alerts actually arrive after a notify change.
- **Cancel the stale maker quote before re-quoting** — otherwise oid churn / orphaned resting orders.
- **SPCX exists on the xyz dex** (SpaceX pre-IPO perp, funding ~-230% APY, isolated-only 10x) but the
  bot does **not** trade it — only the 5 markets in `launch_all.sh`.

## Workflow rules
- Bump the version tag + push to the `bot` remote on every code change.
- Run codehawk (`review_diff`) before committing.
- Read `STATUS.md` before changing bot behavior.
- 2026-07-04: py-clob-client venv on py3.13 needs two fixes on top of `pip install -r requirements.txt`: `setuptools<81` (eth-abi 4.0.0b2 imports pkg_resources, deleted in setuptools 82) and `eth-abi>=5.1 parsimonious>=0.10` (the default resolve lands parsimonious 0.8.1 whose inspect.getargspec died in py3.11). Also: public Novals83/polymarket-hl-strategy is an older cut than the pm-hl-conservative-plus-repo its own skill targets — --force-side had to be re-added or every open dies on argparse.
