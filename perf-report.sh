#!/usr/bin/env bash
###############################################################################
# perf-report.sh — Performance Journal Reporter
#
# Reads perf_journal.jsonl and shows you:
#   - Timeline of every code change
#   - Bot stats at the time of each change
#   - Delta between changes (did portfolio go up or down?)
#   - Which files were changed
#
# Usage:
#   ./perf-report.sh                    # show last 20 entries
#   ./perf-report.sh -n 50              # show last 50
#   ./perf-report.sh --winners          # only show changes where PnL improved
#   ./perf-report.sh --losers           # only show changes where PnL dropped
#   ./perf-report.sh --file trader.py   # only show changes to trader.py
#   ./perf-report.sh --csv              # output as CSV for spreadsheet
#
# Requires: jq
###############################################################################

set -euo pipefail

JOURNAL="${PERF_JOURNAL:-$HOME/hyperliquid-sol/perf_journal.jsonl}"
COUNT=20
FILTER=""
FILE_FILTER=""
CSV_MODE=false

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        -n) COUNT="$2"; shift 2 ;;
        --winners) FILTER="winners"; shift ;;
        --losers) FILTER="losers"; shift ;;
        --file) FILE_FILTER="$2"; shift 2 ;;
        --csv) CSV_MODE=true; shift ;;
        --journal) JOURNAL="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: perf-report.sh [-n COUNT] [--winners|--losers] [--file NAME] [--csv]"
            exit 0
            ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [ ! -f "$JOURNAL" ]; then
    echo "No journal found at $JOURNAL"
    echo "Run slywatch-perf.sh first to start recording."
    exit 1
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

if $CSV_MODE; then
    echo "timestamp,commit,files,portfolio,pnl_delta,trips,trip_delta,win_rate,avg_edge,trip_net"
fi

# Read journal, compute deltas between consecutive entries
prev_portfolio=0
prev_trips=0
prev_trip_net=0
entry_num=0

# Apply file filter if specified
if [ -n "$FILE_FILTER" ]; then
    entries=$(grep "$FILE_FILTER" "$JOURNAL" | tail -n "$COUNT")
else
    entries=$(tail -n "$COUNT" "$JOURNAL")
fi

total_entries=$(echo "$entries" | wc -l | tr -d ' ')

echo "$entries" | while IFS= read -r line; do
    [ -z "$line" ] && continue
    entry_num=$((entry_num + 1))

    ts=$(echo "$line" | jq -r '.timestamp // "?"')
    commit=$(echo "$line" | jq -r '.commit // "?"')
    files=$(echo "$line" | jq -r '.files // [] | join(", ")')

    # Perf metrics (handle null perf gracefully)
    has_perf=$(echo "$line" | jq 'has("perf") and .perf != null')

    if [ "$has_perf" = "true" ]; then
        portfolio=$(echo "$line" | jq -r '.perf.portfolio // 0')
        trips=$(echo "$line" | jq -r '.perf.trips // 0')
        win_rate=$(echo "$line" | jq -r '.perf.win_rate // 0')
        trip_net=$(echo "$line" | jq -r '.perf.trip_net // 0')
        avg_edge=$(echo "$line" | jq -r '.perf.avg_edge // 0')
        fills=$(echo "$line" | jq -r '.perf.fills // 0')
        mode=$(echo "$line" | jq -r '.perf.mode // "?"')
        stale=$(echo "$line" | jq -r '.perf.stale // ""')

        # Compute deltas (using bc for float math)
        if [ "$entry_num" -gt 1 ] && [ "$prev_portfolio" != "0" ]; then
            pnl_delta=$(echo "$portfolio - $prev_portfolio" | bc -l 2>/dev/null || echo "0")
            trip_delta=$((trips - prev_trips))
            net_delta=$(echo "$trip_net - $prev_trip_net" | bc -l 2>/dev/null || echo "0")
        else
            pnl_delta="0"
            trip_delta=0
            net_delta="0"
        fi

        # Apply filter
        if [ "$FILTER" = "winners" ]; then
            is_positive=$(echo "$pnl_delta > 0" | bc -l 2>/dev/null || echo "0")
            [ "$is_positive" != "1" ] && { prev_portfolio=$portfolio; prev_trips=$trips; prev_trip_net=$trip_net; continue; }
        elif [ "$FILTER" = "losers" ]; then
            is_negative=$(echo "$pnl_delta < 0" | bc -l 2>/dev/null || echo "0")
            [ "$is_negative" != "1" ] && { prev_portfolio=$portfolio; prev_trips=$trips; prev_trip_net=$trip_net; continue; }
        fi

        if $CSV_MODE; then
            echo "${ts},${commit},\"${files}\",${portfolio},${pnl_delta},${trips},${trip_delta},${win_rate},${avg_edge},${trip_net}"
        else
            # Format delta with color
            if [ "$entry_num" -gt 1 ] && [ "$prev_portfolio" != "0" ]; then
                is_pos=$(echo "$pnl_delta >= 0" | bc -l 2>/dev/null || echo "1")
                if [ "$is_pos" = "1" ]; then
                    delta_str="${GREEN}+\$${pnl_delta}${NC}"
                else
                    delta_str="${RED}\$${pnl_delta}${NC}"
                fi
                trip_delta_str=""
                if [ "$trip_delta" -gt 0 ]; then
                    trip_delta_str=" ${DIM}(+${trip_delta} trips)${NC}"
                fi
            else
                delta_str="${DIM}baseline${NC}"
                trip_delta_str=""
            fi

            stale_tag=""
            if [ "$stale" = "true" ]; then
                stale_tag=" ${YELLOW}[BOT STOPPED]${NC}"
            fi

            echo ""
            echo -e "${BOLD}${CYAN}${ts}${NC}  ${DIM}${commit}${NC}${stale_tag}"
            echo -e "  Files: ${files}"
            echo -e "  Portfolio: ${BOLD}\$${portfolio}${NC}  ${delta_str}${trip_delta_str}"
            echo -e "  Trips: ${trips} | WR: ${win_rate}% | Edge: ${avg_edge}bps | Net: \$${trip_net} | Fills: ${fills}"
        fi

        prev_portfolio=$portfolio
        prev_trips=$trips
        prev_trip_net=$trip_net
    else
        if ! $CSV_MODE; then
            echo ""
            echo -e "${BOLD}${CYAN}${ts}${NC}  ${DIM}${commit}${NC}"
            echo -e "  Files: ${files}"
            echo -e "  ${DIM}(no bot stats — bot may have been stopped)${NC}"
        fi
    fi
done

if ! $CSV_MODE; then
    echo ""
    echo -e "${DIM}─────────────────────────────────────────────${NC}"
    echo -e "${DIM}Showing ${total_entries} entries from ${JOURNAL}${NC}"
    echo -e "${DIM}Use --winners / --losers to filter | --csv to export${NC}"
fi
