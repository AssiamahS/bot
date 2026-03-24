# hyperliquid-sol — Multi-Agent Rules

## MANDATORY: Activity Log (ACTIVITY.md)

Every bot MUST append to `ACTIVITY.md` for EVERY file operation — edits, reads, scans, deploys, reviews, messages. This is a granular ops log so all bots can see each other's work in real-time.

**Format:**
```
[ISO timestamp] Bot [N] [ACTION] [file/target] — [1-line detail]
```

**Actions:** EDIT, READ, CREATE, DELETE, SCAN, DEPLOY, RUN, REVIEW, MSG

**Examples:**
```
[2026-03-23T16:30:12] Bot 1 EDIT dashboard.html — added WebSocket reconnect logic lines 45-62
[2026-03-23T16:31:05] Bot 2 READ trader.py — checking spread gate logic before edit
[2026-03-23T16:31:45] Bot 2 EDIT trader.py — raised min spread gate from 5 bps to 15 bps line 203
[2026-03-23T16:32:00] Bot 3 SCAN trader.py — CodeHawk review after Bot 2 edit, 0 new criticals
[2026-03-23T16:33:10] Bot 4 READ trader.py — noticed Bot 2 changed spread gate, updating data_bridge defaults
```

**Rules:**
1. Log EVERY file operation, not just completions — if you read a file, log it
2. Log BEFORE responding to the user or other bots
3. One line per operation. No multi-line entries.
4. Use real ISO timestamps, not "now"
5. Never delete or edit previous log entries
6. Before editing a file, READ ACTIVITY.md tail to check if another bot is working on it

## STATUS.md (Task Summaries)

Use `STATUS.md` for high-level task completion summaries (what you accomplished, not every step).

**Format:**
```
Bot [N] — [Role]: [task summary] [ISO timestamp]
```

## Agent Roles

See `AGENTS.md` for role assignments. Stay in your lane:
- Bot 1 (Frontend): dashboard.html, command_center.html
- Bot 2 (Backend): trader.py, risk_controller, spread_scanner
- Bot 3 (QA): CodeHawk scans, never edits code directly
- Bot 4 (Data Bridge): data_bridge.py, port 8084
- Bot 5 (PM): Coordination, priorities, STATUS.md review

Do NOT touch files outside your role without PM (Bot 5) approval.

## Branch Rules

- Always work on a feature branch, never commit to main/master directly
- Branch naming: see Pokemon convention in project memory
- Check `git branch` before starting work — ask if on a stale/junk branch
