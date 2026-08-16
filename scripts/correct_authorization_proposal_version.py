#!/usr/bin/env python3
"""
scripts/correct_authorization_proposal_version.py — Atomic, verified
correction of the authorization_version column on every
AuthorizationRecord named by one AuthorizationProposal, from a known
wrong value to each record's own already-persisted envelope's real
authorization_version.

Exists because AuthorizationRecord.create() previously hardcoded
authorization_version=1 unconditionally (fixed in this same commit --
see hypogeum/authorization_lifecycle.py), so every proposal batch
persisted before that fix has authorization_records rows whose
authorization_version does not match their own envelope's
authorization_version -- hypogeum.submission_gate.submit()'s
04_authorization_version_match check would deny every one of them
before the provider boundary. This script corrects ONLY that one
column, on exactly the records the proposal's own already-immutable
authorization_ids_json names, and only ever touches records this
proposal's own checksum names, never re-derives a set from a manifest
or from live compilation.

Never touches envelope bytes/IDs, authorization_ids, prompts, the
proposal row itself, idempotency_keys, costs, or output identities.
current_state and history_json are re-persisted unchanged; only the
authorization_version column's value moves.

Verifies EVERY named mapping before writing anything: aborts with zero
database writes (via BEGIN IMMEDIATE never reached) if any record is
missing, any record/envelope authorization_version does not match the
caller-declared old/new values exactly, or any envelope's own
authorization_id does not match the record it was loaded for
(ambiguous mapping).

Usage (real evidence only, zero provider calls):
    python3 scripts/correct_authorization_proposal_version.py \\
        --db-path .hypogeum-assets/hypogeum.db \\
        --proposal-checksum <checksum> --expected-proposal-id <id> \\
        --old-version 1 --new-version 3 \\
        --evidence-path .hypogeum-assets/episodes/<episode_id>/authorization_version_correction.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import hypogeum.authorization_store as astore  # noqa: E402
import hypogeum.db as db  # noqa: E402


def correct_authorization_proposal_version(
    *, db_path: Optional[Path], proposal_checksum: str, expected_proposal_id: str,
    old_version: int, new_version: int,
) -> dict:
    conn = db.get_connection(db_path)
    try:
        proposal = astore.get_authorization_proposal_by_checksum(conn, proposal_checksum)
        if proposal is None:
            raise SystemExit(f"ABORT: no AuthorizationProposal found for checksum {proposal_checksum!r}. Zero writes made.")
        if proposal.proposal_id != expected_proposal_id:
            raise SystemExit(
                f"ABORT: proposal_id mismatch: found {proposal.proposal_id!r}, "
                f"expected {expected_proposal_id!r}. Zero writes made."
            )

        authorization_ids = proposal.authorization_ids
        if len(set(authorization_ids)) != len(authorization_ids):
            raise SystemExit("ABORT: duplicate authorization_ids in proposal. Zero writes made.")

        plan: list = []
        for aid in authorization_ids:
            record = astore.get_authorization_record(conn, aid)
            if record is None:
                raise SystemExit(f"ABORT: missing AuthorizationRecord for {aid!r}. Zero writes made.")
            if record.current_state.value != "awaiting_explicit_authorization":
                raise SystemExit(
                    f"ABORT: {aid!r} is at {record.current_state.value!r}, not "
                    "'awaiting_explicit_authorization'. Zero writes made."
                )
            if record.authorization_version != old_version:
                raise SystemExit(
                    f"ABORT: {aid!r} authorization_version is {record.authorization_version}, "
                    f"expected {old_version}. Zero writes made."
                )
            try:
                envelope = astore.get_execution_envelope(conn, record.envelope_id, record.envelope_version)
            except astore.EnvelopeStoreError as exc:
                raise SystemExit(f"ABORT: cannot load envelope for {aid!r}: {exc}. Zero writes made.")
            if envelope.authorization_id != aid:
                raise SystemExit(
                    f"ABORT: ambiguous mapping -- envelope for {aid!r} claims authorization_id "
                    f"{envelope.authorization_id!r}. Zero writes made."
                )
            if envelope.authorization_version != new_version:
                raise SystemExit(
                    f"ABORT: {aid!r} envelope.authorization_version is {envelope.authorization_version}, "
                    f"expected {new_version}. Zero writes made."
                )
            plan.append((aid, record, envelope))

        at = datetime.now(timezone.utc).isoformat()
        evidence_rows = []
        conn.execute("BEGIN IMMEDIATE")
        try:
            for aid, record, envelope in plan:
                corrected = dataclasses.replace(record, authorization_version=envelope.authorization_version)
                astore.persist_authorization_record(conn, corrected)
                evidence_rows.append({
                    "authorization_id": aid, "envelope_id": envelope.envelope_id,
                    "envelope_version": envelope.envelope_version,
                    "old_authorization_version": old_version, "new_authorization_version": new_version,
                    "proposal_id": proposal.proposal_id, "proposal_checksum": proposal.proposal_checksum,
                    "corrected_at": at,
                })
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        for aid in authorization_ids:
            rec = astore.get_authorization_record(conn, aid)
            assert rec.authorization_version == new_version, (aid, rec.authorization_version)
            assert rec.current_state.value == "awaiting_explicit_authorization", (aid, rec.current_state)

        return {
            "proposal_id": proposal.proposal_id, "proposal_checksum": proposal.proposal_checksum,
            "corrected_at": at, "count": len(evidence_rows), "rows": evidence_rows,
        }
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--proposal-checksum", required=True)
    parser.add_argument("--expected-proposal-id", required=True)
    parser.add_argument("--old-version", type=int, required=True)
    parser.add_argument("--new-version", type=int, required=True)
    parser.add_argument("--evidence-path", type=Path, default=None)
    args = parser.parse_args(argv)

    result = correct_authorization_proposal_version(
        db_path=Path(args.db_path) if args.db_path else None,
        proposal_checksum=args.proposal_checksum, expected_proposal_id=args.expected_proposal_id,
        old_version=args.old_version, new_version=args.new_version,
    )

    if args.evidence_path:
        args.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_path.write_text(json.dumps(result, indent=2))
        print(f"OK -- {result['count']} corrected and verified. Evidence written to {args.evidence_path}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
