# MCP tools used for this bot (and which ones actually work)

Every tool listed here has been used at least once in a debug session. The
"status" column reflects the current reality, not the MCP's advertised
feature.

## Bot control

| MCP tool | What it does | Status |
|---|---|---|
| `mcp__hl-bot__bot_status` | Live snapshot of portfolio, fills, trips, PnL | ✅ works |
| `mcp__hl-bot__bot_log` | Tail of `trader.log` from the VPS | ✅ works |
| `mcp__hl-bot__bot_config_get` | Read VPS `config.json` | ✅ works |
| `mcp__hl-bot__bot_start` | Launch the bot in a `screen` session on VPS | ✅ works |
| `mcp__hl-bot__bot_stop` | `killall python3` + remove lockfile on VPS | ✅ works |
| `mcp__hl-bot__bot_restart` | stop + start | ✅ works |
| `mcp__hl-bot__bot_close_all` | Cancel all orders + market-close positions using bot creds | ⚠️ only touches bot wallet, not sub-accounts |
| `mcp__hl-bot__bot_config_set` | Update one key in VPS `config.json` | ❌ BROKEN — the MCP server has a syntax error in its own print statement. Every call errors before writing. Fall back to SSH. |

## Hyperliquid read-only

| MCP tool | What it does | Status |
|---|---|---|
| `mcp__hyperliquid__get_bot_state` | Equity, positions, unrealized PnL, mids | ✅ works (reads the MCP's default wallet — may differ from bot config wallet if sub-accounts are in use) |
| `mcp__hyperliquid__get_portfolio_summary` | Perps + spot + Arbitrum balances | ✅ works |
| `mcp__hyperliquid__get_recent_fills` | Last ~20 fills on the wallet | ✅ works |
| `mcp__hyperliquid__get_open_orders` | Current resting orders | ✅ works |
| `mcp__hyperliquid__get_user_funding` | Funding payment history | ✅ works |
| `mcp__hyperliquid__get_risk_summary` | Exposure, leverage, liquidation distance | ✅ works |
| `mcp__hyperliquid__get_market_edge` | Spread profitability per market | ✅ works — use for pair selection |
| `mcp__hyperliquid__get_liquidation_buffer` | Margin safety estimate | ✅ works |
| `mcp__hyperliquid__get_funding_rates` | Current HL funding rates all coins | ✅ works — drives funding scanner |
| `mcp__hyperliquid__get_l2_book` | L2 order book for any coin | ✅ works |

**Not yet available on any MCP:** order placement on arbitrary wallets,
closing a position on a sub-account, transferring between sub-accounts.
For these we need either a new MCP or an SSH call.

## Code-quality

| MCP tool | What it does | Status |
|---|---|---|
| `mcp__codehawk__review_diff` | Pre-commit lint against `HEAD` | ✅ works, but flags many Python false positives (unreachable code after return/continue — the parser doesn't track try/except) |
| `mcp__codehawk__find_duplicates` | Block-level duplicate detection | ✅ works |
| `mcp__codehawk__find_dead_code` | Unused imports + unreachable code | ⚠️ many false positives for Python |
| `mcp__codehawk__quality_check` | Long files, deep nesting, magic numbers | ✅ works — output can exceed token limits on `trader.py`; scope to specific files |
| `mcp__codehawk__review` | Full repo review | ⚠️ enormous output, scope narrowly |

## Research (agent-reach)

| MCP tool | Why we use it |
|---|---|
| `mcp__agent-reach__web_search` | "how do quants make money at small size" type questions |
| `mcp__agent-reach__github_search` | Find open-source bots to study |
| `mcp__agent-reach__reddit_search` | Community lore on retail algo trading |
| `mcp__agent-reach__twitter_search` | Real-time market maker / HL commentary |
| `mcp__agent-reach__hackernews_search` | Post-mortems and engineering threads |

## VPS ops (slyai)

| MCP tool | What it does | Status |
|---|---|---|
| `mcp__slyai__vps_deploy` | Deploy the SlyAI backend to VPS | ❌ wrong VPS — this targets slyai, not hl-bot |
| `mcp__slyai__vps_status` | SlyAI backend health | ❌ same |
| `mcp__slyai__terminal_context` | Recent shell history from user's terminals | ✅ works, useful for seeing what the user ran |

**Gap:** no MCP for direct SSH to the hl-bot VPS. Deploys happen via
`ssh ubuntu@44.205.58.31 ...` in the user's terminal.

## Telegram

| MCP tool | What it does | Status |
|---|---|---|
| `mcp__telegram__get_history` | Read bot's alert chat history | ❌ session authorization key was used from multiple IPs, needs re-login |
| `mcp__telegram__send_message` | Reply to or alert the bot chat | ❌ same — re-login required |

When restored, this is the definitive record of what alerts actually fired
and when. STATUS.md has drifted from reality; the TG chat has not.

## Rule: if a tool stops working, write it down here

Adding a "status" column entry when an MCP breaks is cheaper than the
next debug session where someone assumes it still works.
