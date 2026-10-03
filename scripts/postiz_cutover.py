#!/usr/bin/env python3
"""
scripts/postiz_cutover.py — move publishing between Zernio and Postiz.

    python3 scripts/postiz_cutover.py status
    python3 scripts/postiz_cutover.py register [--apply]
    python3 scripts/postiz_cutover.py switch twinkle_mini_tales facebook,instagram --to postiz [--apply]
    python3 scripts/postiz_cutover.py switch twinkle_mini_tales facebook --to zernio --apply   # undo

Without --apply nothing is written. `register` reads the Postiz integrations
live and stores the API key (from --key-file) in Credential Manager if no
active provider='postiz' profile exists yet; it routes nothing by itself.
See hypogeum/publishing/postiz_cutover.py and docs/plans/POSTIZ_MIGRATION_PLAN.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.credential_manager import create_secret_profile, list_secret_profiles  # noqa: E402
from hypogeum.publishing import postiz_cutover as cut  # noqa: E402


def _print_status() -> None:
    for channel_id, platforms in cut.status().items():
        print(f"{channel_id}:")
        for platform, provider in platforms.items():
            print(f"  {platform:<10} -> {provider or '(no binding)'}")


def _preview_with_temp_credential(key_path: Path) -> int:
    """Dry-run preview: list + match integrations using a throwaway DB that
    holds the key, so nothing is written to the real database."""
    import tempfile
    from hypogeum.db import initialize_schema
    from hypogeum.publishing.postiz import PostizProvider
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "preview.db"
        initialize_schema(db)
        create_secret_profile(provider="postiz", label="preview", credential_type="api_key",
                              credential_data=key_path.read_text().strip(), scope="factory", db_path=db)
        matched = cut.match_integrations(PostizProvider().list_integrations(db_path=db))
    for (channel_id, platform), integ in sorted(matched.items()):
        print(f"  {channel_id:<20} {platform:<10} <- {integ.get('profile') or integ.get('name')} ({integ['id']})")
    print("[dry-run] re-run with --apply to store the key and register these accounts (routes nothing)")
    return 0


def _register(args) -> int:
    key_path = Path(args.key_file).expanduser()
    have_key = any(p.status == "active" for p in list_secret_profiles(provider="postiz", scope="factory"))
    from hypogeum.publishing.postiz import PostizProvider
    if not have_key:
        if not key_path.exists():
            print(f"no active postiz credential and no key file at {key_path}")
            return 1
        if not args.apply:
            print(f"[dry-run] would store the Postiz API key from {key_path} in Credential Manager")
            return _preview_with_temp_credential(key_path)
        create_secret_profile(provider="postiz", label="Postiz public API (self-hosted)",
                              credential_type="api_key", credential_data=key_path.read_text().strip(),
                              scope="factory")
        print("stored Postiz API key in Credential Manager")
    integrations = PostizProvider().list_integrations()
    matched = cut.match_integrations(integrations)
    for (channel_id, platform), integ in sorted(matched.items()):
        print(f"  {channel_id:<20} {platform:<10} <- {integ.get('profile') or integ.get('name')} ({integ['id']})")
    if not args.apply:
        print("[dry-run] re-run with --apply to register these accounts (routes nothing)")
        return 0
    cut.register_postiz_accounts(integrations)
    print(f"registered {len(matched)} Postiz accounts")
    return 0


def _switch(args) -> int:
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    current = cut.status().get(args.channel_id, {})
    for platform in platforms:
        print(f"  {args.channel_id}/{platform}: {current.get(platform) or '(none)'} -> {args.to}")
    if not args.apply:
        print("[dry-run] re-run with --apply to switch")
        return 0
    for platform in platforms:
        r = cut.switch_platform(args.channel_id, platform, args.to)
        print(f"  switched {args.channel_id}/{platform}: {r['from']} -> {r['to']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    reg = sub.add_parser("register")
    reg.add_argument("--key-file", default="~/postiz-trial/.api_key")
    reg.add_argument("--apply", action="store_true")
    sw = sub.add_parser("switch")
    sw.add_argument("channel_id")
    sw.add_argument("platforms", help="comma-separated, e.g. facebook,instagram")
    sw.add_argument("--to", required=True, choices=("postiz", "zernio"))
    sw.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.cmd == "status":
        _print_status()
        return 0
    if args.cmd == "register":
        return _register(args)
    return _switch(args)


if __name__ == "__main__":
    sys.exit(main())
