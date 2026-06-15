#!/usr/bin/env python3
"""
scripts/import_credentials.py — Import .env credentials as secret_profiles.

Reads current .env values and creates one secret_profile per provider.
Idempotent: uses a fixed secret_profile_id per provider (env-{provider}),
so running this script twice leaves the DB unchanged on the second run.

Providers imported (if the corresponding env var is set):
    openrouter   ← OPENROUTER_API_KEY
    perplexity   ← PERPLEXITY_API_KEY
    muapi        ← MUAPI_API_KEY
    elevenlabs   ← ELEVENLABS_API_KEY
    youtube      ← YOUTUBE_REFRESH_TOKEN

Usage:
    python3 scripts/import_credentials.py
    python3 scripts/import_credentials.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.credential_manager import (
    SecretProfile,
    create_secret_profile,
    get_secret_profile,
)
from hypogeum.db import initialize_schema


# Fixed profile IDs — deterministic and idempotent across runs.
_PROVIDER_MAP = [
    # (fixed_id,         provider,     label,                cred_type,      env_var)
    ("env-openrouter",  "openrouter",  "openrouter-env",     "api_key",      "OPENROUTER_API_KEY"),
    ("env-perplexity",  "perplexity",  "perplexity-env",     "api_key",      "PERPLEXITY_API_KEY"),
    ("env-muapi",       "muapi",       "muapi-env",          "api_key",      "MUAPI_API_KEY"),
    ("env-elevenlabs",  "elevenlabs",  "elevenlabs-env",     "api_key",      "ELEVENLABS_API_KEY"),
    ("env-youtube",     "youtube",     "youtube-env",        "oauth2_token", "YOUTUBE_REFRESH_TOKEN"),
]


def _load_dotenv() -> None:
    """Load .env into os.environ without overriding existing vars."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import .env credentials as secret_profiles (idempotent)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing to DB",
    )
    args = parser.parse_args()

    _load_dotenv()
    initialize_schema()

    created = 0
    skipped = 0
    missing = 0

    for fixed_id, provider, label, cred_type, env_var in _PROVIDER_MAP:
        value = os.environ.get(env_var)
        if not value:
            print(f"  SKIP  {provider:12s}  {env_var} not set")
            missing += 1
            continue

        existing = get_secret_profile(fixed_id)
        if existing is not None:
            print(f"  SKIP  {provider:12s}  already imported (id={fixed_id})")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  DRY   {provider:12s}  would import {env_var} → id={fixed_id}")
            created += 1
            continue

        create_secret_profile(
            provider=provider,
            label=label,
            credential_type=cred_type,
            credential_data=value,
            scope="factory",
            secret_profile_id=fixed_id,
        )
        print(f"  OK    {provider:12s}  id={fixed_id}")
        created += 1

    action = "would create" if args.dry_run else "created"
    print(f"\nDone: {action} {created}, skipped {skipped} existing, {missing} not set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
