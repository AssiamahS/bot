#!/bin/bash
# kim · live silver funding bot launcher
# auto-restarts if it crashes

cd "$(dirname "$0")"
LOG=live_logs/silver_live.log

while true; do
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) starting silver bot ===" >> "$LOG"
  python3 live_funding.py --coin "xyz:SILVER" --usd 25 --poll-secs 600 >> "$LOG" 2>&1
  exit_code=$?
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) bot exited code=$exit_code, restarting in 30s ===" >> "$LOG"
  sleep 30
done
