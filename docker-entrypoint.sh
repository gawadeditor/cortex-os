#!/bin/sh
set -e

# Create the local DB and the first channel on first start (idempotent)
python scripts/bootstrap_channel.py

exec python run_dashboard.py --host 0.0.0.0 --port 5001
