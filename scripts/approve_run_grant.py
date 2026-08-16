#!/usr/bin/env python3
"""
scripts/approve_run_grant.py — Offline approval tool for a
RunAuthorizationGrant (plan §5.5, Correction 1/2).

This is the ONLY place an ApprovalArtifact with approval_type="grant"
is ever signed. It holds (or is pointed at) the Ed25519 PRIVATE key --
this script is never imported by, and never runs inside, the
pipeline/worker production process. Production runtime code
(hypogeum.authorization_authority) holds only the matching PUBLIC key
(Config.AUTHORIZATION_APPROVAL_PUBLIC_KEY), sufficient to verify a
signature this script produces but structurally incapable of
forging one.

Reads a grant specification from a JSON file, prompts for explicit
operator confirmation, signs an ApprovalArtifact bound to that exact
grant payload's checksum, and registers the grant (hypogeum.
authorization_authority.register_run_grant() re-verifies the signature
before writing anything -- this script's own signing step is not
"trusted" merely because it ran; the same verification production
code performs runs here too).

Grant spec JSON shape:
{
  "grant_id": "...", "grant_version": 1,
  "channel_id": "...", "episode_id": "...",
  "workflow_id": "...", "workflow_version": "...", "workflow_checksum": "...",
  "channel_package_version": "...", "channel_package_checksum": "...",
  "currency": "USD", "max_total_spend_all_bindings": "10.00",
  "expires_at": "2026-12-31T23:59:59Z",
  "bindings": [
    {"provider": "...", "model": "...", "provider_operation": "...",
     "max_cost_per_call": "0.39", "max_calls": 20, "max_total_spend": "10.00",
     "retry_authorized": false, "fallback_authorized": false}
  ]
}

Usage:
    python3 scripts/approve_run_grant.py --spec grant_spec.json \\
        --private-key ~/secure/approval_ed25519_private_key.pem \\
        --approver-identity "jane@operator" --nonce "$(uuidgen)" \\
        --db-path .hypogeum-assets/hypogeum.db
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.serialization import load_pem_private_key  # noqa: E402

from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact  # noqa: E402
from hypogeum.authorization_authority import compute_grant_checksum, register_run_grant  # noqa: E402
from hypogeum.authorization_store import GrantBinding  # noqa: E402


def _load_private_key(path: Path):
    key_bytes = path.read_bytes()
    key = load_pem_private_key(key_bytes, password=None)
    return key


def _build_bindings(spec: dict) -> "list[GrantBinding]":
    return [
        GrantBinding(
            provider=b["provider"], model=b["model"], provider_operation=b["provider_operation"],
            currency=spec["currency"], max_cost_per_call=Decimal(b["max_cost_per_call"]),
            max_calls=b["max_calls"], max_total_spend=Decimal(b["max_total_spend"]),
            retry_authorized=b.get("retry_authorized", False), retry_max_attempts=b.get("retry_max_attempts"),
            fallback_authorized=b.get("fallback_authorized", False),
            fallback_provider=b.get("fallback_provider"), fallback_model=b.get("fallback_model"),
        )
        for b in spec["bindings"]
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", required=True, type=Path, help="grant specification JSON file")
    parser.add_argument("--private-key", required=True, type=Path, help="Ed25519 private key PEM file (never checked into the repo)")
    parser.add_argument("--approver-identity", required=True, help="human-readable identity of the approving operator")
    parser.add_argument("--nonce", required=True, help="unique nonce for this approval -- reused nonces are rejected")
    parser.add_argument("--approval-id", default=None, help="defaults to '<grant_id>:approval'")
    parser.add_argument("--issued-at", required=True, help="ISO-8601 timestamp")
    parser.add_argument("--db-path", type=Path, default=None, help="defaults to the production database")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    args = parser.parse_args(argv)

    spec = json.loads(args.spec.read_text())
    bindings = _build_bindings(spec)
    max_total_spend_all_bindings = Decimal(spec["max_total_spend_all_bindings"]) if spec.get("max_total_spend_all_bindings") else None

    checksum = compute_grant_checksum(
        channel_id=spec["channel_id"], episode_id=spec["episode_id"], workflow_id=spec["workflow_id"],
        workflow_version=spec["workflow_version"], workflow_checksum=spec["workflow_checksum"],
        bindings=bindings, max_total_spend_all_bindings=max_total_spend_all_bindings, currency=spec["currency"],
        expires_at=spec["expires_at"],
    )

    print("=" * 72)
    print("RUN AUTHORIZATION GRANT -- ABOUT TO SIGN")
    print("=" * 72)
    print(f"  grant_id:      {spec['grant_id']}@{spec['grant_version']}")
    print(f"  channel_id:    {spec['channel_id']}")
    print(f"  episode_id:    {spec['episode_id']}")
    print(f"  workflow:      {spec['workflow_id']}@{spec['workflow_version']} ({spec['workflow_checksum'][:16]}...)")
    print(f"  expires_at:    {spec['expires_at']}")
    print(f"  grant_checksum: {checksum}")
    for b in bindings:
        print(f"  binding: {b.binding_key()} max_calls={b.max_calls} max_total_spend={b.max_total_spend} {b.currency}")
    print("=" * 72)

    if not args.yes:
        confirm = input("Sign and register this exact grant? Type 'yes' to proceed: ")
        if confirm.strip().lower() != "yes":
            print("Aborted -- not signed, not registered.", file=sys.stderr)
            return 1

    private_key = _load_private_key(args.private_key)
    artifact = sign_approval_artifact(
        dict(
            approval_id=args.approval_id or f"{spec['grant_id']}:approval",
            approval_version=1, approval_type="grant", subject_checksum=checksum,
            channel_id=spec["channel_id"], scope_id=spec["episode_id"], workflow_id=spec["workflow_id"],
            workflow_version=spec["workflow_version"], workflow_checksum=spec["workflow_checksum"],
            channel_package_version=spec["channel_package_version"],
            channel_package_checksum=spec["channel_package_checksum"],
            allowed_bindings=tuple(GrantBindingApproval(b.provider, b.model, b.provider_operation) for b in bindings),
            currency=spec["currency"],
            max_cost_per_call=max((b.max_cost_per_call for b in bindings), default=Decimal("0")),
            max_total_spend=max_total_spend_all_bindings or max((b.max_total_spend for b in bindings), default=Decimal("0")),
            max_calls=sum(b.max_calls for b in bindings),
            retry_authorized=any(b.retry_authorized for b in bindings),
            fallback_authorized=any(b.fallback_authorized for b in bindings),
            issued_at=args.issued_at, expires_at=spec["expires_at"],
            approver_identity=args.approver_identity, nonce=args.nonce,
        ),
        private_key=private_key,
    )

    grant = register_run_grant(
        grant_id=spec["grant_id"], grant_version=spec["grant_version"], channel_id=spec["channel_id"],
        episode_id=spec["episode_id"], workflow_id=spec["workflow_id"], workflow_version=spec["workflow_version"],
        workflow_checksum=spec["workflow_checksum"], bindings=bindings,
        max_total_spend_all_bindings=max_total_spend_all_bindings, currency=spec["currency"],
        expires_at=spec["expires_at"], approval_artifact=artifact, db_path=args.db_path,
    )
    print(f"Grant registered: {grant.grant_id}@{grant.grant_version}, status={grant.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
