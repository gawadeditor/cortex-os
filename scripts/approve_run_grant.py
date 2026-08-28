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
  "production_plan_content_hash": "<64-hex content_hash of the verified persisted plan>",
  "channel_package_version": "...", "channel_package_checksum": "...",
  "currency": "USD", "max_total_spend_all_bindings": "10.00",
  "expires_at": "2026-12-31T23:59:59Z",
  "bindings": [
    {"provider": "...", "model": "...", "provider_operation": "...",
     "max_cost_per_call": "0.39", "max_calls": 20, "max_total_spend": "10.00",
     "retry_authorized": false, "fallback_authorized": false}
  ]
}

production_plan_content_hash is REQUIRED (Phase 2 plan-bound
authorization): the grant is signed over -- and forever bound to --
that exact verified persisted Production Plan. Derive it (and the
bindings) mechanically with --from-plan instead of hand-typing:

    python3 scripts/approve_run_grant.py --spec grant_spec.json \
        --from-plan .hypogeum-assets/episodes/ep_x/production_plan_ep_x.json ...

--from-plan loads the persisted Production Plan JSON, re-verifies its
content_hash integrity exactly like the runtime does, derives bindings
via hypogeum.production_plan.production_plan_to_grant_binding_spec(),
and uses the plan's own content_hash as production_plan_content_hash.
A spec that also carries bindings/hash conflicting with the plan is
aborted, never silently reconciled.

Usage:
    python3 scripts/approve_run_grant.py --spec grant_spec.json \\
        --private-key ~/secure/approval_ed25519_private_key.pem \\
        --approver-identity "jane@operator" --nonce "$(uuidgen)" \\
        --db-path .hypogeum-assets/hypogeum.db
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.serialization import load_pem_private_key  # noqa: E402

from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact  # noqa: E402
from hypogeum.authorization_authority import (  # noqa: E402
    compute_grant_checksum,
    register_channel_operating_grant,
    register_run_grant,
)
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


def _require_plan_hash(spec: dict) -> str:
    """Fail closed unless the spec carries a well-formed 64-hex-char
    Production Plan content_hash. An unbound grant can never be minted
    through this ceremony."""
    plan_hash = spec.get("production_plan_content_hash")
    if not isinstance(plan_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_hash):
        print(
            "ERROR: spec is missing a valid 'production_plan_content_hash' "
            "(64 hex chars) -- Phase 2 requires every grant to be bound to one "
            "exact verified persisted Production Plan. Derive it with --from-plan.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return plan_hash


def _apply_from_plan(args: argparse.Namespace, spec: dict) -> None:
    """--from-plan: derive bindings + hash from the persisted plan file,
    after verifying its stored content_hash against a fresh recomputation
    (the same fail-closed integrity check load_production_plan() enforces
    at runtime). Spec/plan conflicts abort; nothing is silently merged."""
    from hypogeum.production_plan import (
        ProductionPlan,
        payload_content_hash,
        production_plan_to_grant_binding_spec,
    )

    payload = json.loads(args.from_plan.read_text())
    stored_hash = payload.get("content_hash") if isinstance(payload, dict) else None
    recomputed = payload_content_hash(payload)
    if stored_hash != recomputed:
        print(
            f"ERROR: plan file {args.from_plan} failed integrity verification "
            f"(stored content_hash {stored_hash!r} != recomputed {recomputed!r}) "
            "-- refusing to derive a grant from an unverifiable plan.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    fragment = production_plan_to_grant_binding_spec(ProductionPlan.from_dict(payload))
    plan_hash = fragment["production_plan_content_hash"]
    spec_hash = spec.get("production_plan_content_hash")
    if spec_hash is not None and spec_hash != plan_hash:
        print(
            f"ERROR: spec production_plan_content_hash {spec_hash!r} != plan file's "
            f"verified content_hash {plan_hash!r} -- aborting instead of reconciling.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if "bindings" in spec and spec["bindings"] and spec["bindings"] != fragment["bindings"]:
        print(
            "ERROR: spec carries bindings that differ from the plan-derived "
            "bindings -- aborting instead of reconciling.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    spec["bindings"] = fragment["bindings"]
    spec["production_plan_content_hash"] = plan_hash


def _register_channel_operating(args, spec: dict) -> int:
    """ADR-016: sign + register ONE Channel Operating Grant.

    One offline signature authorizes many autonomous episodes for exactly
    one channel over a bounded operating period. It carries NO production
    plan hash and NO episode_id -- episode_id is the '*' sentinel. It
    creates no budget capacity: the weekly ledger, the PRE_PLAN
    reservation, and the plan-bound reservation stay authoritative.

    Spec JSON shape:
    {
      "grant_id": "...", "grant_version": 1,
      "channel_id": "hypogeum",
      "workflow_id": "hypogeum_main_production", "workflow_version": "1.0.0",
      "workflow_checksum": "<64 hex>",
      "channel_package_version": "1.0", "channel_package_checksum": "<64 hex>",
      "allowed_provider_domains": ["openrouter_text", "perplexity_research", "elevenlabs_voice", "muapi_video"],
      "cadence_snapshot": "videos_per_week:1",
      "weekly_exposure_ceiling_usd": "55.00",
      "currency": "USD",
      "valid_from": "2026-08-01T00:00:00Z",
      "expires_at": "2026-12-01T00:00:00Z",
      "operating_period_ref": "hypogeum:2026-W35..W48",
      "bindings": [ { "provider": "...", "model": "...", "provider_operation": "...",
                     "max_cost_per_call": "0.50", "max_calls": 2000, "max_total_spend": "55.00" } ]
    }
    """
    from decimal import Decimal as _D

    bindings = _build_bindings(spec)
    ceiling = _D(str(spec["weekly_exposure_ceiling_usd"]))
    domains = list(spec["allowed_provider_domains"])
    checksum = compute_grant_checksum(
        channel_id=spec["channel_id"], episode_id="*", workflow_id=spec["workflow_id"],
        workflow_version=spec["workflow_version"], workflow_checksum=spec["workflow_checksum"],
        production_plan_content_hash=None, bindings=bindings,
        max_total_spend_all_bindings=ceiling, currency=spec["currency"], expires_at=spec["expires_at"],
        grant_kind="channel_operating", channel_package_version=spec["channel_package_version"],
        channel_package_checksum=spec["channel_package_checksum"], allowed_provider_domains=domains,
        cadence_snapshot=spec["cadence_snapshot"], weekly_exposure_ceiling_usd=ceiling,
        valid_from=spec["valid_from"], operating_period_ref=spec["operating_period_ref"],
    )
    print("=" * 72)
    print("CHANNEL OPERATING GRANT -- ABOUT TO SIGN (ADR-016)")
    print("=" * 72)
    print(f"  grant_id:        {spec['grant_id']}@{spec['grant_version']}")
    print(f"  channel_id:      {spec['channel_id']}  (episode_id = '*', many episodes)")
    print(f"  workflow:        {spec['workflow_id']}@{spec['workflow_version']}")
    print(f"  channel package: {spec['channel_package_version']} ({spec['channel_package_checksum'][:16]}...)")
    print(f"  domains:         {', '.join(sorted(domains))}")
    print(f"  cadence:         {spec['cadence_snapshot']}")
    print(f"  weekly ceiling:  USD {ceiling}   (a CEILING, not capacity)")
    print(f"  valid:           {spec['valid_from']}  ->  {spec['expires_at']}")
    print(f"  grant_checksum:  {checksum}")
    print("=" * 72)
    if not args.yes:
        confirm = input("Sign and register this Channel Operating Grant? Type 'yes': ")
        if confirm.strip().lower() != "yes":
            print("Aborted -- not signed, not registered.", file=sys.stderr)
            return 1

    private_key = _load_private_key(args.private_key)
    artifact = sign_approval_artifact(
        dict(
            approval_id=args.approval_id or f"{spec['grant_id']}:approval",
            approval_version=1, approval_type="channel_operating_grant", subject_checksum=checksum,
            channel_id=spec["channel_id"], scope_id="*", workflow_id=spec["workflow_id"],
            workflow_version=spec["workflow_version"], workflow_checksum=spec["workflow_checksum"],
            channel_package_version=spec["channel_package_version"],
            channel_package_checksum=spec["channel_package_checksum"],
            allowed_bindings=tuple(GrantBindingApproval(b.provider, b.model, b.provider_operation) for b in bindings),
            currency=spec["currency"],
            max_cost_per_call=max((b.max_cost_per_call for b in bindings), default=_D("0")),
            max_total_spend=ceiling, max_calls=sum(b.max_calls for b in bindings),
            retry_authorized=any(b.retry_authorized for b in bindings),
            fallback_authorized=any(b.fallback_authorized for b in bindings),
            issued_at=args.issued_at, expires_at=spec["expires_at"],
            approver_identity=args.approver_identity, nonce=args.nonce,
        ),
        private_key=private_key,
    )
    grant = register_channel_operating_grant(
        grant_id=spec["grant_id"], grant_version=spec["grant_version"], channel_id=spec["channel_id"],
        workflow_id=spec["workflow_id"], workflow_version=spec["workflow_version"],
        workflow_checksum=spec["workflow_checksum"],
        channel_package_version=spec["channel_package_version"],
        channel_package_checksum=spec["channel_package_checksum"],
        allowed_provider_domains=domains, cadence_snapshot=spec["cadence_snapshot"],
        weekly_exposure_ceiling_usd=ceiling, bindings=bindings, currency=spec["currency"],
        valid_from=spec["valid_from"], expires_at=spec["expires_at"],
        operating_period_ref=spec["operating_period_ref"], approval_artifact=artifact,
        db_path=args.db_path,
    )
    print(f"Channel Operating Grant registered: {grant.grant_id}@{grant.grant_version}, status={grant.status}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--kind", choices=["run", "channel-operating"], default="run",
        help="'run' (default): per-episode plan-bound grant. 'channel-operating' "
             "(ADR-016): one signature, many autonomous episodes for one channel.",
    )
    parser.add_argument("--spec", required=True, type=Path, help="grant specification JSON file")
    parser.add_argument(
        "--from-plan", required=False, type=Path, default=None,
        help="persisted Production Plan JSON file -- derives bindings and "
             "production_plan_content_hash from the verified plan instead of hand-typing them",
    )
    parser.add_argument("--private-key", required=True, type=Path, help="Ed25519 private key PEM file (never checked into the repo)")
    parser.add_argument("--approver-identity", required=True, help="human-readable identity of the approving operator")
    parser.add_argument("--nonce", required=True, help="unique nonce for this approval -- reused nonces are rejected")
    parser.add_argument("--approval-id", default=None, help="defaults to '<grant_id>:approval'")
    parser.add_argument("--issued-at", required=True, help="ISO-8601 timestamp")
    parser.add_argument("--db-path", type=Path, default=None, help="defaults to the production database")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    args = parser.parse_args(argv)

    spec = json.loads(args.spec.read_text())
    if args.kind == "channel-operating":
        return _register_channel_operating(args, spec)
    if args.from_plan is not None:
        _apply_from_plan(args, spec)
    plan_content_hash = _require_plan_hash(spec)
    bindings = _build_bindings(spec)
    max_total_spend_all_bindings = Decimal(spec["max_total_spend_all_bindings"]) if spec.get("max_total_spend_all_bindings") else None

    checksum = compute_grant_checksum(
        channel_id=spec["channel_id"], episode_id=spec["episode_id"], workflow_id=spec["workflow_id"],
        workflow_version=spec["workflow_version"], workflow_checksum=spec["workflow_checksum"],
        production_plan_content_hash=plan_content_hash,
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
    print(f"  plan binding:  {plan_content_hash[:16]}... (verified persisted Production Plan)")
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
        workflow_checksum=spec["workflow_checksum"], production_plan_content_hash=plan_content_hash,
        bindings=bindings,
        max_total_spend_all_bindings=max_total_spend_all_bindings, currency=spec["currency"],
        expires_at=spec["expires_at"], approval_artifact=artifact, db_path=args.db_path,
    )
    print(f"Grant registered: {grant.grant_id}@{grant.grant_version}, status={grant.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
