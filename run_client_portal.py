#!/usr/bin/env python3
"""Serves the client portal (hypogeum/client_portal.py) on 127.0.0.1 only. Not exposed to the internet until
Phase 7 points a tunnel at this port -- and only this port."""
import argparse
import logging
import sys

from hypogeum.client_portal import create_app
from hypogeum.config import Config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5003)
    args = ap.parse_args()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)   # the access log would print one-time login tokens
    create_app(config=Config()).run(host="127.0.0.1", port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
