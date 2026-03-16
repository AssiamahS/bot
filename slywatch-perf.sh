#!/usr/bin/env bash
###############################################################################
# slywatch-perf.sh — Performance-Aware Code Watcher
#
# Hooks into slywatch's auto-commit workflow and captures a performance
# snapshot from trader_status.json every time code changes.
#
# What it does:
#   1. Watches ~/hyperliquid-sol/ for file changes (*.py, *.json, *.env)
#   2. On change: reads trader_status.json for current bot stats
#   3. Auto-commits the code change WITH a performance tag in the message
#   4. Appends to perf_journal.jsonl — one line per change event
#   5. Pushes to GitHub (slywatch-snapshots branch)
#
# Output commit message example:
#   slywatch-perf: trader.py (+12/-4) | $128.50 | 14 trips | WR:64% | +$0.0342
#
# Install on VPS:
#   chmod +x slywatch-perf.sh
#   tmux new -s slywatch
#   ./slywatch-perf.sh
#   # Ctrl+B, D to detach
#
# Requires: fswatch, git, jq (apt install -y fswatch jq)
###############################################################################

set -euo pipefail

# === CONFIG ===
WATCH_DIR="${1:-$HOME/hyperliquid-sol}"
STATUS_FILE="$WATCH_DIR/trader_status.json"
PERF_JOURNAL="$WATCH_DIR/perf_journal.jsonl"
DEBOUNCE_SECS=5
GIT_REMOTE="bot"       # your GitHub remote name
GIT_BRANCH="slywatch-snapshots"

# Files to watch for changes
WATCH_PATTERNS=("*.py" "*.json" "*.env" "*.md" "*.toml" "*.yaml" "*.yml" "*.cfg" "*.sh")

# Files to IGNORE (don't trigger snapshots for these)
# NOTE: fswatch --exclude uses POSIX extended regex, not globs
IGNORE_PATTERNS=("trader_status\\.json" "perf_journal\\.jsonl" "\\.git" "__pycache__" "\\.pyc$" "\\.log$" "\\.DS_Store" "hummingbot-api")

# === COLORS ===
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${CYAN}[slywatch-perf $(date '+%H:%M:%S')]${NC} $1"; }
log_good() { echo -e "${GREEN}[slywatch-perf $(date '+%H:%M:%S')]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[slywatch-perf $(date '+%H:%M:%S')]${NC} $1"; }
log_err() { echo -e "${RED}[slywatch-perf $(date '+%H:%M:%S')]${NC} $1"; }

# === DEPENDENCY CHECK ===
for cmd in fswatch git jq; do
    if ! command -v "$cmd" &>/dev/null; then
        log_err "Missing dependency: $cmd"
        log_err "Install with: sudo apt install -y fswatch jq"
        exit 1
    fi
done

if [ ! -d "$WATCH_DIR/.git" ]; then
    log_err "$WATCH_DIR is not a git repo. Run 'git init' first."
    exit 1
fi

# === FUNCTIONS ===

snapshot_perf() {
    # Read current bot performance from trader_status.json
    # Returns a compact JSON string, or empty if file missing/invalid
    if [ ! -f "$STATUS_FILE" ]; then
        echo ""
        return
    fi

    # Check file age — if older than 5 min, bot might be stopped
    local file_age
    file_age=$(( $(date +%s) - $(stat -c %Y "$STATUS_FILE" 2>/dev/null || stat -f %m "$STATUS_FILE" 2>/dev/null || echo 0) ))

    local stale=""
    if [ "$file_age" -gt 300 ]; then
        stale="true"
    fi

    # Extract key metrics using jq
    jq -c '{
        ts: (.updated_at // 0),
        portfolio: (.portfolio_value // 0),
        pnl: (.portfolio_pnl // 0),
        bot_net_pnl: (.bot_net_pnl // 0),
        true_pnl: (.true_trading_pnl // 0),
        trips: (.round_trips // 0),
        trip_net: (.trip_net_pnl // 0),
        trip_avg_net: (.trip_avg_net // 0),
        trip_winners: (.trip_winners // 0),
        trip_losers: (.trip_losers // 0),
        win_rate: (if (.round_trips // 0) > 0 then
            ((.trip_winners // 0) * 100 / (.round_trips // 1)) | round
        else 0 end),
        fills: (.total_trade_count // 0),
        avg_edge: (.avg_edge_bps // 0),
        pos_edge_pct: (.positive_edge_pct // 0),
        gate_skip_pct: (.gate_skip_pct // 0),
        uptime_min: (.uptime_min // 0),
        pairs: (.pairs // []),
        mode: (.profitability_mode // "unknown"),
        stale: "'$stale'"
    }' "$STATUS_FILE" 2>/dev/null || echo ""
}

format_commit_msg() {
    local changed_files="$1"
    local perf_json="$2"

    # Get git diff stats for changed files
    local diff_stat=""
    cd "$WATCH_DIR"
    local added=0
    local removed=0
    for f in $changed_files; do
        if [ -f "$f" ]; then
            local stat
            stat=$(git diff --numstat -- "$f" 2>/dev/null || echo "0 0")
            local a r
            a=$(echo "$stat" | awk '{s+=$1} END {print s+0}')
            r=$(echo "$stat" | awk '{s+=$2} END {print s+0}')
            added=$((added + a))
            removed=$((removed + r))
        fi
    done
    diff_stat="+${added}/-${removed}"

    # Build commit message
    local file_list
    file_list=$(echo "$changed_files" | tr '\n' ',' | sed 's/,$//')

    if [ -n "$perf_json" ] && [ "$perf_json" != "" ]; then
        local portfolio trips win_rate trip_net
        portfolio=$(echo "$perf_json" | jq -r '.portfolio // 0')
        trips=$(echo "$perf_json" | jq -r '.trips // 0')
        win_rate=$(echo "$perf_json" | jq -r '.win_rate // 0')
        trip_net=$(echo "$perf_json" | jq -r '.trip_net // 0')
        stale_tag=$(echo "$perf_json" | jq -r 'if .stale == "true" then " [STALE]" else "" end')

        echo "slywatch-perf: ${file_list} (${diff_stat}) | \$${portfolio} | ${trips} trips | WR:${win_rate}% | \$${trip_net}${stale_tag}"
    else
        echo "slywatch-perf: ${file_list} (${diff_stat}) | no bot stats"
    fi
}

journal_entry() {
    local changed_files="$1"
    local perf_json="$2"
    local commit_hash="$3"

    # Build a journal line: timestamp + changed files + perf snapshot + commit
    local entry
    entry=$(jq -nc \
        --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        --arg files "$changed_files" \
        --arg commit "$commit_hash" \
        --argjson perf "${perf_json:-null}" \
        '{
            timestamp: $ts,
            files: ($files | split("\n") | map(select(. != ""))),
            commit: $commit,
            perf: $perf
        }')

    echo "$entry" >> "$PERF_JOURNAL"
}

should_ignore() {
    local file="$1"
    for pat in "${IGNORE_PATTERNS[@]}"; do
        case "$file" in
            *"$pat"*) return 0 ;;
        esac
    done
    return 1
}

process_changes() {
    cd "$WATCH_DIR"

    # Get list of changed/new files (tracked + untracked)
    local changed
    changed=$(git status --porcelain 2>/dev/null | awk '{print $NF}' | sort -u)

    if [ -z "$changed" ]; then
        return
    fi

    # Filter out ignored files
    local real_changes=""
    while IFS= read -r f; do
        if ! should_ignore "$f"; then
            real_changes="${real_changes}${f}\n"
        fi
    done <<< "$changed"

    real_changes=$(echo -e "$real_changes" | sed '/^$/d')
    if [ -z "$real_changes" ]; then
        return
    fi

    log "Changes detected: $(echo "$real_changes" | tr '\n' ' ')"

    # Snapshot performance BEFORE committing
    local perf
    perf=$(snapshot_perf)

    # Stage only the real changed files (avoid submodules, journal, etc)
    while IFS= read -r f; do
        git add -- "$f" 2>/dev/null || true
    done <<< "$real_changes"

    # Exclude perf_journal.jsonl from the diff stat (it changes every time)
    local commit_msg
    commit_msg=$(format_commit_msg "$real_changes" "$perf")

    git commit -m "$commit_msg" --quiet 2>/dev/null || {
        log_warn "Nothing to commit (maybe .gitignore filtered everything)"
        return
    }

    local commit_hash
    commit_hash=$(git rev-parse --short HEAD)

    # Write journal entry
    journal_entry "$real_changes" "$perf" "$commit_hash"

    # Push to GitHub
    git push "$GIT_REMOTE" "$GIT_BRANCH" --quiet 2>/dev/null && {
        log_good "Committed ${commit_hash}: ${commit_msg}"
    } || {
        log_warn "Committed ${commit_hash} locally (push failed — will retry next change)"
    }
}

# === MAIN LOOP ===

log "Starting slywatch-perf"
log "Watching: $WATCH_DIR"
log "Status file: $STATUS_FILE"
log "Journal: $PERF_JOURNAL"
log "Debounce: ${DEBOUNCE_SECS}s"
log "Remote: ${GIT_REMOTE}/${GIT_BRANCH}"
echo ""

# Build fswatch include/exclude args
FSWATCH_ARGS=()
for pat in "${IGNORE_PATTERNS[@]}"; do
    FSWATCH_ARGS+=(--exclude "$pat")
done

# Ensure we're on the right branch
cd "$WATCH_DIR"
current_branch=$(git branch --show-current 2>/dev/null || echo "")
if [ "$current_branch" != "$GIT_BRANCH" ]; then
    log_warn "Current branch is '$current_branch', expected '$GIT_BRANCH'"
    log_warn "Switching to $GIT_BRANCH..."
    git checkout "$GIT_BRANCH" 2>/dev/null || git checkout -b "$GIT_BRANCH" 2>/dev/null || {
        log_err "Failed to switch to $GIT_BRANCH"
        exit 1
    }
fi

# Initial snapshot on startup
log "Taking initial performance snapshot..."
perf=$(snapshot_perf)
if [ -n "$perf" ]; then
    portfolio=$(echo "$perf" | jq -r '.portfolio')
    trips=$(echo "$perf" | jq -r '.trips')
    wr=$(echo "$perf" | jq -r '.win_rate')
    net=$(echo "$perf" | jq -r '.trip_net')
    log_good "Bot status: \$${portfolio} | ${trips} trips | WR:${wr}% | Net:\$${net}"
else
    log_warn "No bot stats available (trader_status.json missing or bot stopped)"
fi
echo ""

# Watch for changes using fswatch with debounce
last_process=0

fswatch -r -l "$DEBOUNCE_SECS" "${FSWATCH_ARGS[@]}" "$WATCH_DIR" | while read -r event; do
    now=$(date +%s)
    # Extra debounce: don't process more than once per DEBOUNCE_SECS
    if [ $((now - last_process)) -ge "$DEBOUNCE_SECS" ]; then
        last_process=$now
        process_changes
    fi
done
