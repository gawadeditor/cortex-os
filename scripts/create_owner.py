#!/usr/bin/env python3
"""
scripts/create_owner.py — Bootstrap the Owner user and first API key.

Prints the raw API key to stdout once. Store it securely — it cannot be
recovered (only its SHA-256 hash is stored in the DB).

Usage:
    python3 scripts/create_owner.py
    python3 scripts/create_owner.py --email owner@example.com --name "Factory Owner"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.auth import create_api_key, create_user
from hypogeum.db import initialize_schema


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap the Owner user and first API key"
    )
    parser.add_argument(
        "--email",
        default="owner@hypogeum.local",
        help="Owner email address (default: owner@hypogeum.local)",
    )
    parser.add_argument(
        "--name",
        default="Factory Owner",
        help="Owner display name (default: Factory Owner)",
    )
    args = parser.parse_args()

    initialize_schema()

    user = create_user(email=args.email, name=args.name, role="owner")
    key_id, raw_key = create_api_key(user.user_id, label="bootstrap")

    print("Owner user created:")
    print(f"  user_id : {user.user_id}")
    print(f"  email   : {user.email}")
    print(f"  role    : {user.role}")
    print()
    print("API key (shown once — store securely):")
    print(f"  key_id  : {key_id}")
    print(f"  raw_key : {raw_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
