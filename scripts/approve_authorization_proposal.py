#!/usr/bin/env python3
"""
scripts/approve_authorization_proposal.py — Offline approval tool for
a one-off explicit authorization (plan §5.5, Correction 1/2). Used for
Canary-class flows and Commit M's future legacy-episode cutover.

This is the ONLY place an ApprovalArtifact with
approval_type="authorization_proposal" is ever signed. It holds (or is
pointed at) the Ed25519 PRIVATE key -- this script is never imported
by, and never runs inside, the pipeline/worker production process.

Signs an ApprovalArtifact bound to the exact checksum of the envelope
(or proposal) being approved and, unless --sign-only is given, calls
hypogeum.authorization_authority.issue_authorization_from_explicit_
approval() -- which re-verifies the signature itself before issuing
anything.

Proposal spec JSON shape:
{
  "subject_checksum": "...",   -- the exact envelope_id or proposal checksum
  "channel_id": "...", "scope_id": "...",
  "workflow_id": "...", "workflow_version": "...", "workflow_checksum": "...",
  "channel_package_version": "...", "channel_package_checksum": "...",
  "currency": "USD", "max_cost_per_call": "0.39", "max_total_spend": "34.32",
  "max_calls": 88, "expires_at": "2026-12-31T23:59:59Z"
}

Usage:
    python3 scripts/approve_authorization_proposal.py --spec proposal_spec.json \\
        --private-key ~/secure/approval_ed25519_private_key.pem \\
        --approver-identity "jane@operator" --nonce "$(uuidgen)" \\
        --issued-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --sign-only
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.serialization import load_pem_private_key  # noqa: E402

from hypogeum.approval_artifact import sign_approval_artifact  # noqa: E402


def _load_private_key(path: Path):
    return load_pem_private_key(path.read_bytes(), password=None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", required=True, type=Path, help="proposal specification JSON file")
    parser.add_argument("--private-key", required=True, type=Path, help="Ed25519 private key PEM file (never checked into the repo)")
    parser.add_argument("--approver-identity", required=True, help="human-readable identity of the approving operator")
    parser.add_argument("--nonce", required=True, help="unique nonce for this approval -- reused nonces are rejected")
    parser.add_argument("--approval-id", required=True, help="a stable identifier for this exact approval")
    parser.add_argument("--issued-at", required=True, help="ISO-8601 timestamp")
    parser.add_argument("--out", type=Path, default=None, help="write the signed artifact JSON here instead of stdout")
    parser.add_argument("--sign-only", action="store_true", help="sign and print/write the artifact; do not issue an authorization")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    args = parser.parse_args(argv)

    spec = json.loads(args.spec.read_text())

    print("=" * 72)
    print("AUTHORIZATION PROPOSAL -- ABOUT TO SIGN")
    print("=" * 72)
    print(f"  subject_checksum:  {spec['subject_checksum']}")
    print(f"  channel_id:        {spec['channel_id']}")
    print(f"  scope_id:          {spec['scope_id']}")
    print(f"  workflow:          {spec['workflow_id']}@{spec['workflow_version']}")
    print(f"  max_calls:         {spec['max_calls']}")
    print(f"  max_total_spend:   {spec['max_total_spend']} {spec['currency']}")
    print(f"  expires_at:        {spec['expires_at']}")
    print("=" * 72)
    print("THIS IS AN OFFLINE, HUMAN APPROVAL STEP. Verify the subject_checksum")
    print("above against the exact proposal artifact you intend to approve")
    print("before proceeding -- this script does not re-derive it for you.")
    print("=" * 72)

    if not args.yes:
        confirm = input("Sign this exact approval? Type 'yes' to proceed: ")
        if confirm.strip().lower() != "yes":
            print("Aborted -- not signed.", file=sys.stderr)
            return 1

    private_key = _load_private_key(args.private_key)
    artifact = sign_approval_artifact(
        dict(
            approval_id=args.approval_id, approval_version=1, approval_type="authorization_proposal",
            subject_checksum=spec["subject_checksum"], channel_id=spec["channel_id"], scope_id=spec["scope_id"],
            workflow_id=spec["workflow_id"], workflow_version=spec["workflow_version"],
            workflow_checksum=spec["workflow_checksum"], channel_package_version=spec["channel_package_version"],
            channel_package_checksum=spec["channel_package_checksum"], allowed_bindings=(),
            currency=spec["currency"], max_cost_per_call=Decimal(spec["max_cost_per_call"]),
            max_total_spend=Decimal(spec["max_total_spend"]), max_calls=spec["max_calls"],
            retry_authorized=spec.get("retry_authorized", False), fallback_authorized=spec.get("fallback_authorized", False),
            issued_at=args.issued_at, expires_at=spec["expires_at"],
            approver_identity=args.approver_identity, nonce=args.nonce,
        ),
        private_key=private_key,
    )

    from hypogeum.authorization_store import artifact_to_json  # noqa: E402
    artifact_json = artifact_to_json(artifact)
    if args.out:
        args.out.write_text(artifact_json)
        print(f"Signed artifact written to {args.out}")
    else:
        print(artifact_json)

    if args.sign_only:
        print("--sign-only: artifact signed, no authorization issued.")
        return 0

    print(
        "Signing produced the artifact above. Issuing an authorization from it "
        "requires the exact envelope this proposal names -- run that step "
        "separately via hypogeum.authorization_authority."
        "issue_authorization_from_explicit_approval(envelope, approval_artifact=artifact, at=...)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
