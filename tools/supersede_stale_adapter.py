#!/usr/bin/env python3
"""
tools/supersede_stale_adapter.py

Operator action: move an already-registered (provider, model,
provider_operation) adapter binding onto the CURRENT adapter
implementation checksum, when the adapter class's module source has
legitimately changed since the binding was first registered (normal code
evolution across releases) and register_adapter() therefore fails closed
with AdapterIntegrityError ("already registered with implementation
checksum X; attempted registration has different checksum Y").

Thin, evidenced wrapper over
hypogeum.adapter_registry.supersede_adapter_registration -- preserves the
immutable prior row, appends one AdapterSupersessionRecord, inserts the
new active row. Never deletes or rewrites history.

SAFETY: dry-run by default; refuses `.hypogeum-assets/hypogeum.db` without
--i-understand-this-is-production. The adapter module file MUST be
committed (byte-identical to --source-commit's stored version) or the
underlying call fails closed.

    python3 tools/supersede_stale_adapter.py \
        --provider muapi --model veo3.1-lite-text-to-video \
        --operation video.text_to_video \
        --adapter hypogeum.providers.muapi_video_adapter:MuapiVideoAdapter \
        --reason "adapter module evolved across the Authorization Authority video-gate commits" \
        --apply --db .hypogeum-assets/hypogeum.db --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent
_PROD_DB = (_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()


def _load_adapter(spec: str):
    mod_name, _, cls_name = spec.partition(":")
    if not cls_name:
        raise SystemExit(f"--adapter must be 'module.path:ClassName', got {spec!r}")
    return getattr(importlib.import_module(mod_name), cls_name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--provider", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--operation", required=True)
    ap.add_argument("--adapter", required=True, help="'module.path:ClassName'")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--actor", default="channel operator (tools/supersede_stale_adapter.py)")
    ap.add_argument("--source-commit", default=None, help="defaults to current git HEAD")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    args = ap.parse_args(argv)

    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and args.apply and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.",
              file=sys.stderr)
        return 2

    source_commit = args.source_commit or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(_ROOT)
    ).decode().strip()

    import hypogeum.adapter_registry as ar
    from hypogeum.db import get_connection

    adapter_cls = _load_adapter(args.adapter)
    live_checksum = ar.compute_adapter_implementation_checksum(adapter_cls)

    conn = get_connection(db)
    try:
        row = conn.execute(
            "SELECT adapter_implementation_checksum, registration_version, status "
            "FROM adapter_registrations WHERE provider = ? AND model = ? "
            "AND provider_operation = ? AND status = 'active'",
            (args.provider, args.model, args.operation),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        print(f"No ACTIVE registration for ({args.provider}, {args.model}, {args.operation}) "
              f"-- use register_adapter() for a first registration.")
        return 1

    prev = row["adapter_implementation_checksum"]
    print(f"({args.provider}, {args.model}, {args.operation})")
    print(f"  active registration : v{row['registration_version']}  checksum {prev[:16]}…")
    print(f"  live adapter source : {live_checksum[:16]}…  ({args.adapter})")
    print(f"  source_commit       : {source_commit}")
    if prev == live_checksum:
        print("  -> already current, nothing to supersede.")
        return 0
    print()

    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply to supersede.")
        return 0

    binding = ar.supersede_adapter_registration(
        args.provider, args.model, args.operation, adapter_cls,
        previous_checksum=prev, actor=args.actor, reason=args.reason,
        source_commit=source_commit, confirm_supersede=True,
        repo_root=_ROOT, db_path=db,
    )
    print(f"Superseded. new active checksum {getattr(binding, 'adapter_implementation_checksum', live_checksum)[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
