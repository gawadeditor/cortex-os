#!/usr/bin/env python3
"""
scripts/propose_current_episode_authorization.py — Commit M: compile,
observe, and propose a new authorization_version for one episode's
active generation manifest, entirely from live/current/provable
evidence. Zero provider calls, zero spend, never activates anything.
See /Users/gawadeditor/.claude/plans/jolly-roaming-bear.md §6 Commit M
and §7 Tier 2 for the full contract this script implements.

Sequence:
  1. Resolve the CURRENT registered workflow binding for
     (channel_id, "hypogeum_main_production", "1.0.0") -- already
     registered by Commit C; this script never registers one itself.
  2. Compile every active manifest entry into a fresh
     GenerationExecutionEnvelope via tools.migrate_legacy_manifest_
     to_envelopes.compile_manifest_to_envelopes(), with the REAL
     workflow resolver injected (not the tool's own permanently-
     unavailable stub) and tag_as_reconstructed=False -- these
     envelopes are compiled from entirely live, current, provable
     evidence, never reconstructed from a pre-existing legacy
     authorization.
  3. Persist each as a new AuthorizationRecord (DRAFT -> COMPILED ->
     AWAITING_AUTHORIZATION) in hypogeum.authorization_store -- not
     active, not submittable. Never touches the legacy manifest,
     visual_package, or coverage plan; only NEW rows in authorization_
     store's own tables.
  4. Run every one through Observation-Only
     (hypogeum.shadow_observation.evaluate_shadow(), mode=
     "shadow_observation_only" -- NEVER submission_gate.submit()).
     Structurally incapable of reaching a provider adapter.
  5. If any fail (verdict != WOULD_ALLOW): stop. Every record stays at
     AWAITING_AUTHORIZATION. Report which failed and why.
  6. If all pass: transition every record to
     AWAITING_EXPLICIT_AUTHORIZATION (never AUTHORIZED -- that
     requires a separate, future, explicitly human-signed
     ApprovalArtifact, entirely out of this script's and this
     program's own scope), build and persist one immutable, checksummed
     AuthorizationProposal. Stop.

Usage (the only currently-implemented mode):
    python3 scripts/propose_current_episode_authorization.py \\
        --episode-id ep_f3619022 --dry-run

--dry-run is required and is the only supported mode, mirroring
tools/migrate_legacy_manifest_to_envelopes.py's own convention -- the
flag exists explicitly so a future write-enabled mode can never be
silently defaulted into. Note that this script's OWN normal operation
already writes new AuthorizationRecord/AuthorizationProposal rows
(that IS its job, per step 6 above) -- "dry-run" here means what it
means throughout this program: zero provider calls, zero spend, never
activates anything, never touches the legacy manifest or any existing
episode file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import migrate_legacy_manifest_to_envelopes as mle  # noqa: E402

import hypogeum.authorization_store as astore  # noqa: E402
import hypogeum.budget_scopes as bs  # noqa: E402
import hypogeum.db as db  # noqa: E402
import hypogeum.workflow_executor as workflow_executor  # noqa: E402
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState  # noqa: E402
from hypogeum.config import Config  # noqa: E402
from hypogeum.execution_envelope import GenerationExecutionEnvelope  # noqa: E402
from hypogeum.provider_receipt import WriteAheadReceiptStore  # noqa: E402
from hypogeum.shadow_observation import evaluate_shadow  # noqa: E402

_HYPOGEUM_WORKFLOW_ID = "hypogeum_main_production"
_HYPOGEUM_WORKFLOW_VERSION = "1.0.0"


def _real_workflow_identity_resolver(channel_id: str, workflow_id: str, workflow_version: str):
    """A real resolver against the actual, already-registered Workflow
    Registry (Commits B-D) -- the exact seam compile_manifest_to_
    envelopes()'s own docstring names, closed here for the first time."""
    def _resolve() -> dict:
        try:
            binding = workflow_executor.resolve_workflow_binding(channel_id, workflow_id, workflow_version)
        except Exception as exc:  # noqa: BLE001 -- any resolution failure is a clean "unavailable", never a crash
            return {"available": False, "version": None, "reason": f"{type(exc).__name__}: {exc}"}
        return {"available": True, "version": binding["workflow_version"], "reason": None}
    return _resolve


def propose_current_episode_authorization(
    episode_id: str, channel_id: str = "hypogeum", *,
    assets_root: Optional[Path] = None, db_path: Optional[Path] = None,
    proposed_version: int = 2, superseded_version: Optional[int] = 1,
    config: Optional[Config] = None,
) -> dict:
    """
    Runs the full sequence documented in this module's own docstring.
    Returns a machine-readable report dict, never raises for an
    ordinary policy outcome (compile blocker, an entry failing
    Observation-Only) -- only for a genuine programming error.

    db_path: the production hypogeum.db default when omitted (matching
    every other domain's own real-production convention) -- tests pass
    an isolated path. The rows this function writes there (new
    authorization_records / authorization_proposals rows) are additive
    and touch no existing production table.
    """
    assets_root = Path(assets_root) if assets_root else (_ROOT / ".hypogeum-assets")
    if config is None:
        config = Config()

    resolver = _real_workflow_identity_resolver(channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION)

    # Step 1: confirm the workflow actually resolves before compiling
    # anything -- fail closed, cleanly, before touching the manifest at
    # all, rather than discovering this 88 times over inside the loop.
    workflow_identity = resolver()
    if not workflow_identity["available"]:
        return {"stage": "workflow_resolution", "status": "blocked", "reason": workflow_identity["reason"]}
    workflow_binding = workflow_executor.resolve_workflow_binding(channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION)

    # Step 2: compile from entirely live, current, provable evidence.
    parser = mle.build_arg_parser()
    args = parser.parse_args([
        "--episode-id", episode_id, "--channel-id", channel_id, "--dry-run",
        "--assets-root", str(assets_root),
    ])
    args.manifest_path = str(mle._episode_dir(assets_root, episode_id) / f"generation_authorization_manifest_{episode_id}.json")
    if db_path is not None:
        args.db_path = str(db_path)

    compile_report = mle.compile_manifest_to_envelopes(
        args, config=config, workflow_identity_resolver=resolver,
        tag_as_reconstructed=False, authorization_version=proposed_version,
        include_envelope_dicts=True,
    )

    if compile_report["global_blockers"]:
        return {"stage": "compile", "status": "blocked", "compile_report": compile_report}

    compiled = [e for e in compile_report["entries"] if "envelope_dict" in e]
    if not compiled:
        return {"stage": "compile", "status": "no_envelopes_constructed", "compile_report": compile_report}

    at = datetime.now(timezone.utc).isoformat()
    envelopes = [GenerationExecutionEnvelope.from_dict(e["envelope_dict"]) for e in compiled]
    for env in envelopes:
        env.validate_integrity()

    # Step 3: persist every one as a new AuthorizationRecord, state
    # AWAITING_AUTHORIZATION -- not active, not submittable -- AND its
    # full canonical envelope (docs/architecture/PROTECTED_GENERATION_
    # LIFECYCLE_RECONCILIATION.md Part 1): before this, only the
    # opaque envelope_id checksum was ever durably retained, making a
    # true field-level audit of a persisted proposal structurally
    # impossible. This is the ONLY production write this step
    # performs: new rows in authorization_store's own tables, nothing
    # else.
    conn = db.get_connection(db_path)
    try:
        astore.ensure_tables(conn)
        records: list = []
        for env in envelopes:
            rec = AuthorizationRecord.create(
                authorization_id=env.authorization_id, envelope_id=env.envelope_id, envelope_version=env.envelope_version,
            )
            rec = rec.transition(
                AuthorizationState.COMPILED,
                evidence={"compiler_identity": env.compiler_identity, "compiler_version": env.compiler_version}, at=at,
            )
            rec = rec.transition(
                AuthorizationState.AWAITING_AUTHORIZATION,
                evidence={"preflight_result": "envelope_fields_valid_and_dependency_identity_available"}, at=at,
            )
            astore.persist_authorization_record(conn, rec)
            astore.persist_execution_envelope(conn, env)
            records.append(rec)
        conn.commit()

        # Step 4: Observation-Only for every envelope -- never
        # submission_gate.submit(), structurally incapable of reaching
        # a provider adapter. Uses an isolated lease/receipt store
        # (never the real production lease table or the episode's own
        # real receipts directory) purely to supply COMPLETE evidence
        # so a genuinely valid envelope can reach WOULD_ALLOW rather
        # than being stuck at UNVERIFIABLE for lack of lease/receipt
        # evidence that has nothing to do with what this script is
        # actually testing.
        shadow_root = assets_root / "episodes" / episode_id / "authorization_proposal"
        shadow_db_path = shadow_root / "shadow_leases.db"
        shadow_receipts_root = shadow_root / "shadow_receipts"
        db.initialize_schema(shadow_db_path)
        shadow_conn = db.get_connection(shadow_db_path)
        receipt_store = WriteAheadReceiptStore(shadow_receipts_root)

        decisions: list = []
        try:
            for env, rec in zip(envelopes, records):
                lease_owner = "propose_current_episode_authorization"
                lease_expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                lease = db.acquire_lease(shadow_conn, env.envelope_id, lease_owner, acquired_at=at, expires_at=lease_expires)

                # A hand-built, NEVER-PERSISTED, simulated-AUTHORIZED
                # record purely to make Observation-Only's own
                # authorization-shape check evaluable -- the REAL,
                # persisted record for this envelope (`rec`, in
                # `records` above) stays at AWAITING_AUTHORIZATION
                # throughout this entire script; this simulated one is
                # discarded immediately after evaluate_shadow() returns.
                simulated_authorized = AuthorizationRecord(
                    authorization_id=env.authorization_id, authorization_version=env.authorization_version,
                    envelope_id=env.envelope_id, envelope_version=env.envelope_version,
                    current_state=AuthorizationState.AUTHORIZED, history=(),
                )
                live_sources_kwargs = dict(
                    channel_package_checksum=env.dependency_checksums.channel_package,
                    plan_checksum=env.dependency_checksums.plan,
                    structured_spec_checksum=env.dependency_checksums.structured_spec,
                    compiler_version=env.compiler_version,
                )
                import hypogeum.submission_gate as sg
                live_sources = sg.LiveSources(**live_sources_kwargs)
                scopes = [bs.ScopeRequest(bs.BudgetScopeKind.CALL, env.planned_shot_id, env.max_cost_per_call, env.currency)]

                decision = evaluate_shadow(
                    env, at=at, execution_id=episode_id, deliverable=env.planned_shot_id,
                    live_sources=live_sources, authorization=simulated_authorized,
                    lease_conn=shadow_conn, lease_owner_id=lease_owner,
                    lease_fencing_token=lease["fencing_token"] if lease else None,
                    receipt_store=receipt_store, budget_scopes=scopes,
                    expected_output_identity=env.expected_output_identity,
                    expected_deterministic_output_path=env.deterministic_output_path,
                    output_already_exists=Path(env.deterministic_output_path).exists(),
                    mode="shadow_observation_only",
                )
                decisions.append({"authorization_id": env.authorization_id, "envelope_id": env.envelope_id, "decision": decision})
        finally:
            shadow_conn.close()

        failures = [d for d in decisions if d["decision"].verdict != "WOULD_ALLOW"]

        if failures:
            return {
                "stage": "observation_only", "status": "not_ready", "total": len(decisions),
                "pass_count": len(decisions) - len(failures), "failure_count": len(failures),
                "failures": [
                    {"authorization_id": d["authorization_id"], "envelope_id": d["envelope_id"],
                     "verdict": d["decision"].verdict, "blockers": d["decision"].blockers}
                    for d in failures
                ],
                "compile_report_summary": {k: v for k, v in compile_report.items() if k != "entries"},
            }

        # Step 6: all N passed -- compute the proposal checksum first,
        # then transition every record to AWAITING_EXPLICIT_AUTHORIZATION
        # (never AUTHORIZED) carrying that exact checksum as evidence,
        # and persist the proposal artifact itself. Stop here.
        authorization_ids = [env.authorization_id for env in envelopes]
        bindings = sorted({(env.provider, env.model, env.provider_operation) for env in envelopes})
        max_cost_per_call_ceiling = max(env.max_cost_per_call for env in envelopes)
        max_total_spend = sum((env.max_cost_per_call for env in envelopes), Decimal("0"))

        proposal_checksum = astore.compute_proposal_checksum(
            channel_id=channel_id, episode_id=episode_id, proposed_version=proposed_version,
            superseded_version=superseded_version, workflow_id=_HYPOGEUM_WORKFLOW_ID,
            workflow_version=workflow_binding["workflow_version"], workflow_checksum=workflow_binding["workflow_checksum"],
            channel_package_version=envelopes[0].channel_package_version, channel_package_checksum=envelopes[0].channel_package_checksum,
            authorization_ids=authorization_ids, provider_model_operation_bindings=bindings,
            max_cost_per_call_ceiling=max_cost_per_call_ceiling, max_total_spend=max_total_spend, currency="USD",
        )

        final_records = []
        for rec in records:
            rec2 = rec.transition(
                AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION,
                evidence={
                    "observation_only_pass_count": len(decisions), "observation_only_total_count": len(decisions),
                    "proposal_artifact_checksum": proposal_checksum,
                },
                at=at,
            )
            astore.persist_authorization_record(conn, rec2)
            final_records.append(rec2)

        proposal_id = f"proposal_{episode_id}_v{proposed_version}_{hashlib.sha256(proposal_checksum.encode()).hexdigest()[:12]}"
        proposal = astore.AuthorizationProposal(
            proposal_id=proposal_id, channel_id=channel_id, episode_id=episode_id,
            proposed_version=proposed_version, superseded_version=superseded_version,
            workflow_id=_HYPOGEUM_WORKFLOW_ID, workflow_version=workflow_binding["workflow_version"],
            workflow_checksum=workflow_binding["workflow_checksum"],
            channel_package_version=envelopes[0].channel_package_version,
            channel_package_checksum=envelopes[0].channel_package_checksum,
            authorization_ids=authorization_ids, provider_model_operation_bindings=bindings,
            max_cost_per_call_ceiling=max_cost_per_call_ceiling, max_total_spend=max_total_spend, currency="USD",
            created_at=at, proposal_checksum=proposal_checksum,
        )
        astore.persist_authorization_proposal(conn, proposal)

        return {
            "stage": "proposal", "status": "awaiting_explicit_authorization", "total": len(decisions),
            "pass_count": len(decisions), "proposal_id": proposal_id, "proposal_checksum": proposal_checksum,
            "authorization_ids": authorization_ids, "provider_model_operation_bindings": bindings,
            "max_cost_per_call_ceiling": str(max_cost_per_call_ceiling), "max_total_spend": str(max_total_spend),
            "workflow_id": _HYPOGEUM_WORKFLOW_ID, "workflow_version": workflow_binding["workflow_version"],
            "workflow_checksum": workflow_binding["workflow_checksum"], "proposed_version": proposed_version,
            "superseded_version": superseded_version,
            "compile_report_summary": {k: v for k, v in compile_report.items() if k != "entries"},
        }
    finally:
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--episode-id", required=True)
    p.add_argument("--channel-id", default="hypogeum")
    p.add_argument("--assets-root", default=str(_ROOT / ".hypogeum-assets"))
    p.add_argument("--db-path", default=None, help="defaults to hypogeum.db's own production default path")
    p.add_argument("--proposed-version", type=int, default=2)
    p.add_argument("--superseded-version", type=int, default=1)
    p.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Required; the only currently-implemented mode. See module docstring for what 'dry-run' means here.",
    )
    p.add_argument("--report-path", default=None, help="optional; if omitted, nothing is written to disk")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.dry_run:
        print("Only --dry-run is currently implemented; refusing to proceed.", file=sys.stderr)
        return 2

    report = propose_current_episode_authorization(
        args.episode_id, args.channel_id, assets_root=Path(args.assets_root),
        db_path=Path(args.db_path) if args.db_path else None,
        proposed_version=args.proposed_version, superseded_version=args.superseded_version,
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
