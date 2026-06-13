#!/bin/bash
# print the current claude.ai connector URL for the hl-bot remote MCP
DIR="$(cd "$(dirname "$0")" && pwd)"
SECRET=$(cat "$DIR/mcp_secret.txt")
HOST=$(cat "$DIR/tunnel_url.txt" 2>/dev/null | tr -d '[:space:]')
echo "${HOST}/${SECRET}/mcp"
