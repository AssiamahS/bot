#!/bin/bash
# launch funding-harvest bots on multiple HIP-3 markets in parallel
# each gets its own auto-restart loop

cd "$(dirname "$0")"
mkdir -p live_logs

run_bot() {
  local coin=$1
  local usd=$2
  local slug=$(echo "$coin" | tr ':' '_')
  local log="live_logs/${slug}_runner.log"

  if pgrep -f "live_funding.py --coin $coin " > /dev/null; then
    echo "$coin already running"
    return
  fi

  echo "launching $coin @ \$${usd}..."
  local startup_delay=${3:-0}
  (
    sleep "$startup_delay"
    # use a unique long restart delay per coin so cascades de-correlate
    local restart=$((60 + RANDOM % 120))
    while true; do
      echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) starting $coin ===" >> "$log"
      python3 -u live_funding.py --coin "$coin" --usd "$usd" --poll-secs 600 --maker >> "$log" 2>&1
      echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) $coin exited, restart in ${restart}s ===" >> "$log"
      sleep "$restart"
      restart=$((60 + RANDOM % 120))
    done
  ) &
  sleep 1
}

run_bot "xyz:SILVER" 20 0
run_bot "xyz:MU" 20 45
run_bot "xyz:NVDA" 15 90
run_bot "xyz:AAPL" 15 135
run_bot "xyz:TSLA" 15 180

sleep 3
echo ""
echo "running live_funding processes:"
pgrep -afl "live_funding.py" | head -20

# stay foreground: launchd KeepAlive treats exit as death and kills the whole
# process group, which murders every bot ~10s after launch. wait forever instead.
wait
