#!/usr/bin/env python3
"""
scripts/register_existing_qualified_asset.py — Registers a physical
asset that was already generated and paid for (e.g. via the legacy,
pre-Authorization-Authority submit_scene() path, before any
AuthorizationRecord existed for it) into the durable authorization
lifecycle, using real, caller-supplied evidence. Zero provider calls,
zero new spend, never regenerates or touches the underlying media file.

Reuses the exact same generic hypogeum.authorization_store functions
every real domain caller and test fixture already use --
record_submission_outcome(), advance_to_pending_editorial_review(),
record_editorial_verdict(), allocate_qualified_asset() -- this script
adds no new lifecycle mechanism, only a generic driver that walks a
NEW authorization_id through them using caller-supplied real evidence,
starting from a durably-stored envelope that already exists (reused
verbatim under the new authorization_id, never re-derived).

The source envelope must already be persisted (hypogeum.
authorization_store.persist_execution_envelope()) -- e.g. by a prior
tools/migrate_legacy_manifest_to_envelopes.py compile -- identified by
(source_envelope_id, source_envelope_version). This script never
constructs or reconstructs envelope content itself.

Usage (real evidence only, zero provider calls, zero spend):
    python3 scripts/register_existing_qualified_asset.py \\
        --db-path .hypogeum-assets/hypogeum.db \\
        --source-envelope-id <envelope_id> --source-envelope-version 1 \\
        --new-authorization-id "ep_f3619022:s19__extra1:primary" \\
        --output-checksum <sha256 of the real file> \\
        --technical-validation-json '{"decode_errors": 0, ...}' \\
        --reviewer "Muhamed Abdelgawad" --verdict approved \\
        --allocated-to "b_019__shot1" \\
        --provider-job-id <real historical evidence id, e.g. a costs.cost_id> \\
        --submitting-at <ISO, real historical timestamp> \\
        --submitted-at <ISO, real historical timestamp> \\
        --at <ISO, when this registration itself is being performed>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import hypogeum.authorization_store as astore  # noqa: E402
import hypogeum.db as db  # noqa: E402
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState  # noqa: E402


def register_existing_qualified_asset(
    conn, *, source_envelope_id: str, source_envelope_version: int, new_authorization_id: str,
    output_checksum: str, technical_validation_result: dict, reviewer: str, verdict: str,
    allocated_to: str, provider_job_id: str, submitting_at: str, submitted_at: str, at: str,
) -> AuthorizationRecord:
    """
    Idempotent: replaying with the same new_authorization_id and the
    same evidence at every step returns the already-ALLOCATED record
    unchanged (every underlying astore function this calls is itself
    idempotent). Fails closed if the source envelope is not durably
    stored, or if any step's own fail-closed contract is violated.
    """
    astore.ensure_tables(conn)
    env = astore.get_execution_envelope(conn, source_envelope_id, source_envelope_version)

    existing = astore.get_authorization_record(conn, new_authorization_id)
    if existing is None:
        rec = AuthorizationRecord.create(
            authorization_id=new_authorization_id, authorization_version=env.authorization_version,
            envelope_id=env.envelope_id, envelope_version=env.envelope_version,
        )
        rec = rec.transition(
            AuthorizationState.COMPILED,
            evidence={"compiler_identity": env.compiler_identity, "compiler_version": env.compiler_version}, at=at,
        )
        rec = rec.transition(
            AuthorizationState.AWAITING_AUTHORIZATION,
            evidence={"preflight_result": "reused_already_stored_envelope"}, at=at,
        )
        rec = rec.transition(
            AuthorizationState.AUTHORIZED,
            evidence={
                "authorized_by": "existing_asset_registration",
                "authorization_method": "legacy_pre_authority_generation_backfill",
            },
            at=at,
        )
        astore.persist_authorization_record(conn, rec)

    astore.record_submission_outcome(
        conn, new_authorization_id, outcome="accepted",
        submitting_at=submitting_at, submitted_at=submitted_at, provider_job_id=provider_job_id,
    )
    astore.advance_to_pending_editorial_review(
        conn, new_authorization_id, output_checksum=output_checksum,
        technical_validation_result=technical_validation_result, at=at,
    )
    # record_editorial_verdict()/allocate_qualified_asset() are each
    # idempotent only for their OWN immediate states (QUALIFIED/REJECTED,
    # ALLOCATED/CONSUMED respectively) -- neither knows about states
    # further downstream, so a replay after the record has already
    # advanced past them must skip re-calling, never re-invoke blindly.
    current = astore.get_authorization_record(conn, new_authorization_id).current_state
    if current not in (AuthorizationState.QUALIFIED, AuthorizationState.REJECTED, AuthorizationState.ALLOCATED, AuthorizationState.CONSUMED):
        astore.record_editorial_verdict(conn, new_authorization_id, reviewer=reviewer, verdict=verdict, at=at)
        current = astore.get_authorization_record(conn, new_authorization_id).current_state
    if verdict == "approved" and current not in (AuthorizationState.ALLOCATED, AuthorizationState.CONSUMED):
        astore.allocate_qualified_asset(conn, new_authorization_id, allocated_to=allocated_to, at=at)

    return astore.get_authorization_record(conn, new_authorization_id)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-path", default=None)
    p.add_argument("--source-envelope-id", required=True)
    p.add_argument("--source-envelope-version", type=int, default=1)
    p.add_argument("--new-authorization-id", required=True)
    p.add_argument("--output-checksum", required=True)
    p.add_argument("--technical-validation-json", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--verdict", required=True, choices=["approved", "rejected"])
    p.add_argument("--allocated-to", default=None)
    p.add_argument("--provider-job-id", required=True)
    p.add_argument("--submitting-at", required=True)
    p.add_argument("--submitted-at", required=True)
    p.add_argument("--at", default=None)
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.verdict == "approved" and not args.allocated_to:
        print("--allocated-to is required when --verdict approved", file=sys.stderr)
        return 2
    at = args.at or datetime.now(timezone.utc).isoformat()

    conn = db.get_connection(Path(args.db_path) if args.db_path else None)
    try:
        rec = register_existing_qualified_asset(
            conn, source_envelope_id=args.source_envelope_id, source_envelope_version=args.source_envelope_version,
            new_authorization_id=args.new_authorization_id, output_checksum=args.output_checksum,
            technical_validation_result=json.loads(args.technical_validation_json),
            reviewer=args.reviewer, verdict=args.verdict, allocated_to=args.allocated_to,
            provider_job_id=args.provider_job_id, submitting_at=args.submitting_at,
            submitted_at=args.submitted_at, at=at,
        )
        conn.commit()
    finally:
        conn.close()

    print(json.dumps({
        "authorization_id": rec.authorization_id, "current_state": rec.current_state.value,
        "envelope_id": rec.envelope_id, "envelope_version": rec.envelope_version,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
