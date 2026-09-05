#!/usr/bin/env bash
# Runs cloudflared quick-tunnel and writes the assigned trycloudflare.com URL
# to tunnel_url.txt the moment it appears. launchd WatchPaths on that file
# triggers an automatic hl-mcp restart so it picks up the new issuer URL.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/live_logs/cloudflared.log"
URL_FILE="$DIR/tunnel_url.txt"

/opt/homebrew/bin/cloudflared tunnel --url http://127.0.0.1:8765 2>&1 | \
while IFS= read -r line; do
    printf '%s\n' "$line" >> "$LOG"
    if [[ "$line" =~ (https://[a-z0-9-]+\.trycloudflare\.com) ]]; then
        NEW="${BASH_REMATCH[1]}"
        OLD="$(cat "$URL_FILE" 2>/dev/null | tr -d '[:space:]')"
        # only write (and trigger WatchPaths restart) when URL actually changes
        if [[ "$NEW" != "$OLD" ]]; then
            printf '%s\n' "$NEW" > "$URL_FILE"
        fi
    fi
done
