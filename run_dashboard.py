#!/usr/bin/env python3
"""
run_dashboard.py — Hypogeum Factory OS Local Dashboard

Serves a read-only HTML dashboard at http://localhost:5001

Routes:
    GET /           Renders the full dashboard HTML page
    GET /api/state  Returns FactoryState + Director report as JSON

Usage:
    python3 run_dashboard.py
    python3 run_dashboard.py --port 5002
    python3 run_dashboard.py --host 0.0.0.0

The page auto-refreshes every 30 seconds. No writes to the DB.
No external CDN. No JavaScript framework.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from flask import Flask, jsonify, render_template

from hypogeum.dashboard_api import get_factory_state
from hypogeum.factory_director import assess_factory

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = _ROOT / "hypogeum" / "templates"

app = Flask(__name__, template_folder=str(_TEMPLATE_DIR))
app.config["TEMPLATES_AUTO_RELOAD"] = True


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _to_dict(obj) -> object:
    """Recursively convert dataclasses to dicts for JSON serialisation."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_dict(getattr(obj, f.name))
                for f in dataclasses.fields(obj)}
    if isinstance(obj, list):
        return [_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


def _safe_state():
    """
    Return (state, report, error_msg).
    Never raises — on any failure returns degraded placeholder objects.
    """
    try:
        state  = get_factory_state()
        report = assess_factory()
        return state, report, None
    except Exception as exc:
        return None, None, str(exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    state, report, err = _safe_state()
    return render_template(
        "dashboard.html",
        state=state,
        report=report,
        error=err,
    )


@app.route("/api/state")
def api_state():
    state, report, err = _safe_state()
    if err or state is None:
        return jsonify({"error": err or "state unavailable"}), 503

    payload = {
        "factory_state":  _to_dict(state),
        "director_report": _to_dict(report),
    }
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Hypogeum Factory OS local dashboard"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5001,
                        help="Port (default: 5001)")
    args = parser.parse_args()

    print(f"Factory OS Dashboard → http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
