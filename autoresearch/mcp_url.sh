#!/bin/bash
# print the current claude.ai connector URL for the hl-bot remote MCP
DIR="$(dirname "$0")"
SECRET=$(cat "$DIR/mcp_secret.txt")
HOST=$(grep -o "https://[a-z-]*\.trycloudflare\.com" "$DIR/live_logs/cloudflared.log" | tail -1)
echo "${HOST}/${SECRET}/mcp"
