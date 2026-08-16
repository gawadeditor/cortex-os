#!/usr/bin/env python3
"""
scripts/propose_replacement_authorization.py — Phase 3 (replacement
proposal) of the ep_f3619022 canonical-operation-payload-compiler-bug
program: compiles a brand-new, identity-disjoint authorization proposal
for the SAME set of planned shots an existing (older) proposal already
authorized, using the corrected canonical JSON operation-payload
compiler (see tools/migrate_legacy_manifest_to_envelopes.py's
encode_video_payload()/decode_video_payload() round-trip), then --
ONLY after the new proposal has already been durably persisted -- retires
the old proposal's AUTHORIZED/SUBMITTING AuthorizationRecords via
hypogeum.authorization_store.reconcile_all_superseded_authorizations().

Zero provider calls. Zero HTTP requests. Zero spend. Never signs or
activates the new proposal -- it is left at AWAITING_EXPLICIT_
AUTHORIZATION, exactly like every other proposal this program has ever
produced; a separate, future, explicitly human-signed ApprovalArtifact
is required before it can ever be executed.

Never edits, overwrites, or deletes: the live manifest file, the old
proposal's own envelopes, or the old AuthorizationRecords' own history.
A brand-new, disjoint manifest_id (hence a brand-new, disjoint
authorization_id namespace -- authorization_records.authorization_id is
a single-column PRIMARY KEY, so reusing the old authorization_id under
a new authorization_version would silently overwrite, not preserve, the
old record's durable history) is written to an ISOLATED path under the
episode's own directory, never overwriting generation_authorization_
manifest_{episode_id}.json itself. The old 85 records are retired
(AUTHORIZED|SUBMITTING -> INVALIDATED_SOURCE_CHANGED), never rewritten
in place, and their full transition history remains exactly as it was
plus this one final, evidenced transition.

Usage:
    python3 scripts/propose_replacement_authorization.py \\
        --episode-id ep_f3619022 --old-proposal-id proposal_ep_f3619022_v3_04a7be6e4143 \\
        --proposed-version 4 --old-compiler-head 89a5786 --dry-run
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import migrate_legacy_manifest_to_envelopes as mle  # noqa: E402

import hypogeum.authorization_store as astore  # noqa: E402
import hypogeum.db as db  # noqa: E402
from hypogeum.config import Config  # noqa: E402

from propose_current_episode_authorization import propose_current_episode_authorization  # noqa: E402

_COMPILER_TOOL_RELPATH = "tools/migrate_legacy_manifest_to_envelopes.py"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_show_file_bytes(commit_ish: str, relpath: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{commit_ish}:{relpath}"], cwd=str(_ROOT),
        capture_output=True, check=True,
    )
    return result.stdout


def build_replacement_manifest(
    live_manifest: dict, *, target_auth_indices: "set[int]", new_manifest_id: str,
) -> dict:
    """Pure function, zero I/O: a deep copy of live_manifest restricted
    to exactly the entries named by target_auth_indices, under a new,
    disjoint manifest_id. Never mutates live_manifest. Fails closed
    (ValueError) if the live manifest does not contain an active entry
    for every requested auth_index -- never silently proceeds with a
    smaller replacement set than requested."""
    manifest = copy.deepcopy(live_manifest)
    active, _inactive = mle.resolve_active_entries(manifest)
    active_by_index = {e.get("auth_index"): e for e in active}
    missing = sorted(target_auth_indices - set(active_by_index))
    if missing:
        raise ValueError(f"live manifest has no active entry for auth_index(es): {missing}")
    manifest["manifest_id"] = new_manifest_id
    manifest["entries"] = [active_by_index[i] for i in sorted(target_auth_indices)]
    manifest["active_entry_count"] = len(manifest["entries"])
    return manifest


def propose_replacement_authorization(
    episode_id: str, channel_id: str, *, old_proposal_id: str, proposed_version: int,
    old_compiler_head: str, assets_root: Optional[Path] = None, db_path: Optional[Path] = None,
    config: Optional[Config] = None,
) -> dict:
    assets_root = Path(assets_root) if assets_root else (_ROOT / ".hypogeum-assets")
    if config is None:
        config = Config()
    at = datetime.now(timezone.utc).isoformat()

    conn = db.get_connection(db_path)
    try:
        astore.ensure_tables(conn)
        old_proposal = astore.get_authorization_proposal(conn, old_proposal_id)
        if old_proposal is None:
            return {"stage": "load_old_proposal", "status": "blocked", "reason": f"no such proposal: {old_proposal_id!r}"}

        old_authorization_ids = list(old_proposal.authorization_ids)
        old_manifest_id_prefix = old_authorization_ids[0].rsplit(":", 1)[0]
        target_auth_indices = {int(aid.rsplit(":", 1)[1]) for aid in old_authorization_ids}
        if len(target_auth_indices) != len(old_authorization_ids):
            return {
                "stage": "derive_target_set", "status": "blocked",
                "reason": "old proposal's authorization_ids did not yield a unique auth_index per entry",
            }

        # Verify every old authorization_id is currently AUTHORIZED or
        # SUBMITTING -- exactly the two states reconcile_all_superseded_
        # authorizations() is willing to retire later. Fails closed
        # before compiling anything if that is not true (e.g. this
        # script is accidentally pointed at an already-superseded or
        # not-yet-activated proposal).
        from hypogeum.authorization_lifecycle import AuthorizationState
        old_records = {}
        wrong_state = {}
        for aid in old_authorization_ids:
            rec = astore.get_authorization_record(conn, aid)
            if rec is None:
                wrong_state[aid] = "missing"
            elif rec.current_state not in (AuthorizationState.AUTHORIZED, AuthorizationState.SUBMITTING):
                wrong_state[aid] = rec.current_state.value
            else:
                old_records[aid] = rec
        if wrong_state:
            return {
                "stage": "verify_old_records", "status": "blocked",
                "reason": "not every old authorization_id is AUTHORIZED or SUBMITTING", "detail": wrong_state,
            }

        # Compiler checksums -- the actual "changed_dependency" evidence
        # for the supersession: tools/migrate_legacy_manifest_to_envelopes.py
        # itself, before (old_compiler_head) and after (current working
        # tree / about-to-be-committed) the canonical-JSON-payload fix.
        old_compiler_checksum = _sha256_bytes(_git_show_file_bytes(old_compiler_head, _COMPILER_TOOL_RELPATH))
        new_compiler_checksum = _sha256_bytes((_ROOT / _COMPILER_TOOL_RELPATH).read_bytes())
        if old_compiler_checksum == new_compiler_checksum:
            return {
                "stage": "verify_compiler_changed", "status": "blocked",
                "reason": (
                    f"{_COMPILER_TOOL_RELPATH} at {old_compiler_head!r} is byte-identical to the current "
                    "working copy -- refusing to claim a fix that is not actually present"
                ),
            }

        live_manifest_path = mle._episode_dir(assets_root, episode_id) / f"generation_authorization_manifest_{episode_id}.json"
        live_manifest = mle._load_json(live_manifest_path)
        if live_manifest.get("manifest_id") != old_manifest_id_prefix:
            return {
                "stage": "verify_manifest_identity", "status": "blocked",
                "reason": (
                    f"live manifest_id {live_manifest.get('manifest_id')!r} does not match the old proposal's "
                    f"own authorization_id prefix {old_manifest_id_prefix!r}"
                ),
            }

        new_manifest_id = f"{old_manifest_id_prefix}_replacement_v{proposed_version}"
        try:
            replacement_manifest = build_replacement_manifest(
                live_manifest, target_auth_indices=target_auth_indices, new_manifest_id=new_manifest_id,
            )
        except ValueError as exc:
            return {"stage": "build_replacement_manifest", "status": "blocked", "reason": str(exc)}

        replacement_dir = mle._episode_dir(assets_root, episode_id) / f"authorization_replacement_v{proposed_version}"
        replacement_dir.mkdir(parents=True, exist_ok=True)
        replacement_manifest_path = replacement_dir / f"generation_authorization_manifest_{new_manifest_id}.json"
        replacement_manifest_path.write_text(json.dumps(replacement_manifest, indent=2, sort_keys=True))

        old_to_new_authorization_id = {
            f"{old_manifest_id_prefix}:{i}": f"{new_manifest_id}:{i}" for i in target_auth_indices
        }
        supersedes_map = {new_id: old_id for old_id, new_id in old_to_new_authorization_id.items()}

        report = propose_current_episode_authorization(
            episode_id, channel_id, assets_root=assets_root, db_path=db_path,
            proposed_version=proposed_version, superseded_version=old_proposal.proposed_version,
            config=config, manifest_path_override=replacement_manifest_path, supersedes_map=supersedes_map,
        )
        if report.get("status") != "awaiting_explicit_authorization":
            return {
                "stage": "compile_replacement_proposal", "status": "blocked",
                "replacement_manifest_path": str(replacement_manifest_path), "propose_report": report,
            }

        new_authorization_ids = set(report["authorization_ids"])
        expected_new_ids = set(old_to_new_authorization_id.values())
        if new_authorization_ids != expected_new_ids:
            return {
                "stage": "verify_replacement_completeness", "status": "blocked",
                "reason": "the compiled replacement proposal's authorization_ids do not exactly match the "
                           "expected 1:1 replacement set",
                "expected": sorted(expected_new_ids), "actual": sorted(new_authorization_ids),
                "propose_report": report,
            }

        # Requirement: no replacement output collides with an existing
        # qualified asset. Nothing in this 85-shot set was ever
        # generated (Phase 1 confirmed the only real attempt failed
        # before ever reaching the provider boundary) -- verify that
        # remains true rather than assuming it.
        collisions = []
        for new_id in sorted(new_authorization_ids):
            rec = astore.get_authorization_record(conn, new_id)
            env = astore.get_execution_envelope(conn, rec.envelope_id, rec.envelope_version)
            if Path(env.deterministic_output_path).exists():
                collisions.append({"authorization_id": new_id, "path": env.deterministic_output_path})
        if collisions:
            return {
                "stage": "verify_no_output_collision", "status": "blocked",
                "reason": "one or more replacement outputs already exist on disk -- stopping before superseding "
                           "anything old",
                "collisions": collisions, "propose_report": report,
            }

        # Only now -- after the new proposal is durably persisted and
        # verified complete/collision-free -- retire the old records.
        superseded_records = astore.reconcile_all_superseded_authorizations(
            conn, old_to_new_authorization_id, at=at,
            changed_dependency=f"operation_payload_compiler:{_COMPILER_TOOL_RELPATH}",
            old_checksum=old_compiler_checksum, new_checksum=new_compiler_checksum,
            reason="corrected_canonical_operation_payload_compilation",
        )

        return {
            "stage": "complete", "status": "awaiting_explicit_authorization",
            "old_proposal_id": old_proposal_id, "old_proposal_checksum": old_proposal.proposal_checksum,
            "old_authorization_ids": sorted(old_authorization_ids),
            "new_proposal_id": report["proposal_id"], "new_proposal_checksum": report["proposal_checksum"],
            "new_authorization_ids": sorted(new_authorization_ids),
            "supersession_count": len(superseded_records),
            "old_to_new_authorization_id": old_to_new_authorization_id,
            "replacement_manifest_path": str(replacement_manifest_path),
            "old_compiler_checksum": old_compiler_checksum, "new_compiler_checksum": new_compiler_checksum,
            "max_cost_per_call_ceiling": report["max_cost_per_call_ceiling"],
            "max_total_spend": report["max_total_spend"],
            "workflow_id": report["workflow_id"], "workflow_version": report["workflow_version"],
            "workflow_checksum": report["workflow_checksum"],
            "provider_model_operation_bindings": report["provider_model_operation_bindings"],
        }
    finally:
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--episode-id", required=True)
    p.add_argument("--channel-id", default="hypogeum")
    p.add_argument("--old-proposal-id", required=True)
    p.add_argument("--proposed-version", type=int, required=True)
    p.add_argument("--old-compiler-head", required=True, help="git commit-ish the buggy compiler last existed at")
    p.add_argument("--assets-root", default=str(_ROOT / ".hypogeum-assets"))
    p.add_argument("--db-path", default=None, help="defaults to hypogeum.db's own production default path")
    p.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Required; the only currently-implemented mode -- zero provider calls either way.",
    )
    p.add_argument("--report-path", default=None)
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.dry_run:
        print("Only --dry-run is currently implemented; refusing to proceed.", file=sys.stderr)
        return 2

    report = propose_replacement_authorization(
        args.episode_id, args.channel_id, old_proposal_id=args.old_proposal_id,
        proposed_version=args.proposed_version, old_compiler_head=args.old_compiler_head,
        assets_root=Path(args.assets_root), db_path=Path(args.db_path) if args.db_path else None,
    )

    if args.report_path:
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = report_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(report, f, indent=2, sort_keys=True, default=str)
            f.write("\n")
        tmp.replace(report_path)

    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report.get("status") != "blocked" else 1


if __name__ == "__main__":
    sys.exit(main())
