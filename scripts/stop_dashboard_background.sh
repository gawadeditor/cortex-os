#!/usr/bin/env bash
# scripts/stop_dashboard_background.sh
# Stop the background Factory OS dashboard process.

set -euo pipefail

PIDFILE="/tmp/hypogeum-dashboard.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "No PID file found at $PIDFILE — dashboard may not be running."
    # Try to find it anyway
    FOUND=$(pgrep -f "run_dashboard.py" 2>/dev/null || true)
    if [ -n "$FOUND" ]; then
        echo "Found dashboard process(es): $FOUND"
        echo "Kill with: kill $FOUND"
    fi
    exit 0
fi

PID=$(cat "$PIDFILE")

if ! kill -0 "$PID" 2>/dev/null; then
    echo "Process $PID not running (stale PID file). Cleaning up."
    rm -f "$PIDFILE"
    exit 0
fi

kill "$PID"
echo "Sent SIGTERM to PID $PID."

# Wait for clean exit
for i in $(seq 1 5); do
    sleep 1
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "Dashboard stopped."
        rm -f "$PIDFILE"
        exit 0
    fi
done

# Force kill if still running
echo "Process still running — sending SIGKILL."
kill -9 "$PID" 2>/dev/null || true
rm -f "$PIDFILE"
echo "Dashboard force-stopped."
