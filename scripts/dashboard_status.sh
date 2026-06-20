#!/usr/bin/env bash
# scripts/dashboard_status.sh
# Show status of the background Factory OS dashboard.

PIDFILE="/tmp/hypogeum-dashboard.pid"
LOGFILE="/tmp/hypogeum-dashboard.log"
PORT="${1:-5001}"
HOST="127.0.0.1"

echo "=== Factory OS Dashboard Status ==="

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "  Status: RUNNING"
        echo "  PID:    $PID"
        echo "  URL:    http://$HOST:$PORT"
        echo "  Log:    $LOGFILE"
        # Check HTTP reachability
        if command -v curl &>/dev/null; then
            HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://$HOST:$PORT/api/state" 2>/dev/null || echo "000")
            if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "503" ]; then
                echo "  HTTP:   reachable (HTTP $HTTP_CODE)"
            else
                echo "  HTTP:   not reachable (HTTP $HTTP_CODE) — may still be starting"
            fi
        fi
    else
        echo "  Status: STOPPED (stale PID $PID)"
        rm -f "$PIDFILE"
    fi
else
    # Check if running without PID file
    FOUND=$(pgrep -f "run_dashboard.py" 2>/dev/null || true)
    if [ -n "$FOUND" ]; then
        echo "  Status: RUNNING (PID $FOUND, no PID file)"
        echo "  URL:    http://$HOST:$PORT"
    else
        echo "  Status: NOT RUNNING"
        echo "  Start:  ./scripts/start_dashboard_background.sh"
    fi
fi

if [ -f "$LOGFILE" ]; then
    echo ""
    echo "--- Last 5 log lines ---"
    tail -5 "$LOGFILE"
fi
