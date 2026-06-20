#!/usr/bin/env bash
# scripts/start_dashboard_background.sh
# Start the Factory OS dashboard in the background on 127.0.0.1:5001.
# Logs to /tmp/hypogeum-dashboard.log. PID stored in /tmp/hypogeum-dashboard.pid.
# Usage: ./scripts/start_dashboard_background.sh [port]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT="${1:-5001}"
HOST="127.0.0.1"
LOGFILE="/tmp/hypogeum-dashboard.log"
PIDFILE="/tmp/hypogeum-dashboard.pid"

# Check if already running
if [ -f "$PIDFILE" ]; then
    EXISTING_PID=$(cat "$PIDFILE")
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "Dashboard already running (PID $EXISTING_PID)."
        echo "  URL: http://$HOST:$PORT"
        echo "  Log: $LOGFILE"
        echo "  Stop: ./scripts/stop_dashboard_background.sh"
        exit 0
    else
        echo "Stale PID file found ($EXISTING_PID), removing."
        rm -f "$PIDFILE"
    fi
fi

# Start dashboard
cd "$ROOT"
nohup python3 run_dashboard.py --host "$HOST" --port "$PORT" \
    >> "$LOGFILE" 2>&1 &
DASHBOARD_PID=$!

echo "$DASHBOARD_PID" > "$PIDFILE"

# Wait briefly and verify it started
sleep 1
if ! kill -0 "$DASHBOARD_PID" 2>/dev/null; then
    echo "ERROR: Dashboard failed to start. Check $LOGFILE"
    rm -f "$PIDFILE"
    tail -20 "$LOGFILE"
    exit 1
fi

echo "Factory OS dashboard started."
echo "  PID:  $DASHBOARD_PID"
echo "  URL:  http://$HOST:$PORT"
echo "  Log:  $LOGFILE"
echo "  Stop: ./scripts/stop_dashboard_background.sh"
