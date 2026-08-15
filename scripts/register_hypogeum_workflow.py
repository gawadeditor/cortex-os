#!/usr/bin/env python3
"""
scripts/register_hypogeum_workflow.py — Index Hypogeum's versioned
Channel Package workflow artifact into hypogeum.workflow_registry.

This script contains no stage definition of its own -- it knows only
the channel identifier and reads the already-authored, already-committed
artifact file at channel_packages/hypogeum/workflow/
hypogeum_main_production.json (declared in
channel_packages/hypogeum/channel_package_manifest.json). It computes
that file's checksum and registers it. The actual stage order, handler
references, and policies live entirely in that file, never in this
script or anywhere under hypogeum/.

Safe to run repeatedly: re-running with an unchanged artifact file is a
no-op (idempotent, per hypogeum.workflow_registry.register_workflow()'s
own contract). Re-running after editing the artifact file's content
without bumping its declared workflow_version fails closed
(WorkflowIntegrityError) -- this script does not "fix" that; the
channel_package_manifest.json's own version fields must be bumped by an
operator first.

Usage:
    python3 scripts/register_hypogeum_workflow.py
    python3 scripts/register_hypogeum_workflow.py --verify-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.workflow_executor import (  # noqa: E402
    channel_package_dir,
    load_channel_package_manifest,
    load_workflow_artifact_bytes,
)
from hypogeum.workflow_registry import (  # noqa: E402
    WorkflowIntegrityError,
    register_workflow,
    resolve_workflow_version,
)

_CHANNEL_ID = "hypogeum"


def _registered_workflow_entries(manifest: dict) -> "list[dict]":
    return [a for a in manifest.get("artifacts", []) if a.get("role") == "workflow_definition"]


def register(channel_id: str = _CHANNEL_ID, *, verify_only: bool = False) -> int:
    manifest = load_channel_package_manifest(channel_id)
    entries = _registered_workflow_entries(manifest)
    if not entries:
        print(f"no workflow_definition artifacts declared for channel {channel_id!r}", file=sys.stderr)
        return 1

    ok = True
    for entry in entries:
        workflow_id = entry["workflow_id"]
        workflow_version = entry["workflow_version"]
        artifact_bytes = load_workflow_artifact_bytes(channel_id, workflow_id)

        if verify_only:
            existing = resolve_workflow_version(channel_id, workflow_id, workflow_version)
            if existing is None:
                print(f"NOT REGISTERED: {channel_id}/{workflow_id}@{workflow_version}")
                ok = False
            else:
                print(f"registered: {channel_id}/{workflow_id}@{workflow_version} checksum={existing.workflow_checksum[:12]}")
            continue

        try:
            registered = register_workflow(channel_id, workflow_id, workflow_version, artifact_bytes)
        except WorkflowIntegrityError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            ok = False
            continue
        print(f"registered: {channel_id}/{workflow_id}@{workflow_version} checksum={registered.workflow_checksum[:12]}")

    return 0 if ok else 1


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel-id", default=_CHANNEL_ID)
    p.add_argument("--verify-only", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    return register(args.channel_id, verify_only=args.verify_only)


if __name__ == "__main__":
    sys.exit(main())
