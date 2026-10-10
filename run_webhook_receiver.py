#!/usr/bin/env python3
"""Serves the Postiz webhook receiver (hypogeum/postiz_webhook.py) on 127.0.0.1 only; a tunnel exposes just
this port. Refuses to start without POSTIZ_WEBHOOK_SECRET."""
import argparse
import sys

from hypogeum.config import Config
from hypogeum.postiz_webhook import create_app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5002)
    args = ap.parse_args()
    config = Config()
    if not config.POSTIZ_WEBHOOK_SECRET:
        sys.stderr.write("POSTIZ_WEBHOOK_SECRET is not set; refusing to start.\n")
        return 2
    create_app(config=config).run(host="127.0.0.1", port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
