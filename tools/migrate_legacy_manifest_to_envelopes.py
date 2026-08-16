#!/usr/bin/env python3
"""
tools/migrate_legacy_manifest_to_envelopes.py — Compiles an episode's
legacy `generation_authorization_manifest_*.json` active entries into
real, immutable, content-addressed GenerationExecutionEnvelopes
(hypogeum.execution_envelope.GenerationExecutionEnvelope), per
docs/architecture/AUTHORIZED_GENERATION_ENVELOPE.md §8.2.6 and
docs/architecture/AUTHORIZED_GENERATION_ENVELOPE_IMPLEMENTATION_BLUEPRINT.md
Commit 8.

Despite the historical filename (inherited from the blueprint's own
naming), this tool's DEFAULT and only currently-implemented mode is
`--dry-run`: read-only compilation and validation. It never writes to
the live manifest, the live coverage plan, the live visual_package, any
episode asset, any database table, or any receipt/budget/lease/breaker
store. It never contacts a provider. It never invokes
hypogeum.muapi.submit_scene() or any provider adapter (fake or real).

CORRECTED 2026-08-16 (post-review): the first implementation of this
tool conflated two separate questions -- "is this authorization's own
binding self-consistent" and "does today's legacy production caller
agree with it" -- into a single model_mismatch envelope-construction
blocker. That was wrong: the authorization manifest itself is the
authoritative paid-execution binding (`manifest["model_tier_planned"]`),
not a value re-derived from the live scene's provider_routing. This
version binds provider/model/cost/duration/aspect_ratio strictly from
the immutable authorization evidence, and evaluates legacy-caller
agreement as a SEPARATE, non-blocking-to-envelope-validity dimension
(see FOUR-DIMENSION CLASSIFICATION below).

This version also corrects a second omission: the first implementation
treated CHANNEL_PACKAGE_SPEC.md as if no compiled Channel Package
artifact existed anywhere. It missed hypogeum.channel_config's real,
already-bootstrapped, DB-backed channel_profiles/brand_profiles/
publishing_profiles/model_profiles rows for channel_id="hypogeum" --
these ARE the canonical, live "compiled Channel Package" this
architecture's §3.1 channel_package_checksum/channel_package_version
fields refer to. hypogeum.channel_config.load_channel_config() is now
used directly; no operator-supplied checksum override exists or is
needed for this field anymore.

workflow_version remains a genuine, disclosed, non-bypassable gap:
hypogeum/db.py's `runs.pipeline_version` column exists but is written
by nothing anywhere in this repository (confirmed by grep across
hypogeum/ and by direct inspection of the live database, where every
row's value is empty). WORKFLOW_MODEL.md's "Workflow Registry" section
describes required infrastructure that was never implemented. No CLI
override is offered for this field: per this task's explicit
instruction, an operator-supplied value can only be accepted if it can
be cryptographically verified against a canonical source, and no such
source exists for a registered Workflow version. Every entry is
therefore UNVERIFIABLE on this dimension until a real Workflow Registry
is built -- this is reported explicitly, never silently bypassed.

FOUR-DIMENSION CLASSIFICATION (per entry, independently evaluated,
never conflated):

  envelope_fields_valid          -- would this authorization's own
                                     prompt/checksums/output-path/
                                     authorized provider+model+cost+
                                     duration+aspect_ratio bindings
                                     construct a self-consistent
                                     envelope, given complete dependency
                                     identity? (Independent of whether
                                     that dependency identity actually
                                     exists right now.)
  dependency_identity_available  -- are channel_package_checksum/
                                     channel_package_version (now real),
                                     workflow_version (not yet real),
                                     and plan_checksum all resolvable?
  legacy_path_compatible         -- does today's actual production
                                     caller (hypogeum.agents.
                                     provider_router.build_scene_spec()
                                     against the live scene's own
                                     provider_routing) agree with the
                                     AUTHORIZED binding? A "no" here is
                                     reported as
                                     legacy_execution_path_binding_
                                     mismatch -- it never means the
                                     authorization itself is invalid.
  paid_execution_ready           -- envelope_fields_valid AND
                                     dependency_identity_available AND
                                     legacy_path_compatible, all true.

Every envelope field is derived only from canonical, already-existing
repository evidence and canonical helper functions already used by
production callers and by tools/muapi_batch_preflight.py -- never
invented, guessed, or repaired:

  hypogeum.canary_preflight        check_prompt_checksum(), check_provenance(),
                                    find_planned_shot(), resolve_active_entries(),
                                    scene_by_beat(), sha256_text()
  hypogeum.video_prompt_writer     compile_authoritative_shot_prompt() (recompiled
                                    fresh and compared byte-for-byte against the
                                    live coverage plan's stored primary_prompt),
                                    assemble_video_scene_prompt(),
                                    REQUIRED_SHOT_FIELDS
  hypogeum.agents.provider_router  build_scene_spec() -- used ONLY to evaluate
                                    legacy_path_compatible, never to derive the
                                    envelope's own authorized binding
  hypogeum.deterministic_allocation resolve_canonical_output_slot() -- deterministic,
                                    plan-derived output-slot identity; never touches
                                    the filesystem, replacing hypogeum.agents.
                                    clip_repair_agent.plan_confirmed_shot_allocations()
                                    (still correct for its own real-time-generation
                                    purpose, but never the identity source for an
                                    already-compiled envelope -- see docs/architecture/
                                    PROTECTED_GENERATION_LIFECYCLE_RECONCILIATION.md)
  hypogeum.scene_identity           enforce_global_scene_identity(),
                                    enforce_scene_internal_consistency()
  hypogeum.muapi                    MODEL_CREDIT_COSTS, _tier_to_model(),
                                    _compute_asset_id(), _VEO3_MODELS,
                                    _VEO3_1_MODELS, _VEO3_1_DEFAULT_DURATION
  hypogeum.channel_config           load_channel_config() -- the real,
                                    DB-backed Channel Package for Hypogeum

Usage (read-only, zero provider calls, zero writes to any live state):
    python3 tools/migrate_legacy_manifest_to_envelopes.py \\
        --episode-id ep_f3619022 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.agents.provider_router import build_scene_spec  # noqa: E402
from hypogeum.deterministic_allocation import resolve_canonical_output_slot  # noqa: E402
from hypogeum.canary_preflight import (  # noqa: E402
    CanaryPreflightError as _CheckError,
    check_prompt_checksum,
    check_provenance,
    find_planned_shot,
    resolve_active_entries,
    scene_by_beat,
    sha256_text,
)
from hypogeum.channel_config import compute_channel_package_identity, load_channel_config  # noqa: E402
from hypogeum.config import Config  # noqa: E402
from hypogeum.execution_envelope import (  # noqa: E402
    EnvelopeIntegrityError,
    EnvelopeSchemaError,
    GenerationExecutionEnvelope,
)
from hypogeum.muapi import (  # noqa: E402
    MODEL_CREDIT_COSTS,
    _VEO3_1_DEFAULT_DURATION,
    _VEO3_1_MODELS,
    _VEO3_MODELS,
    _compute_asset_id,
    _tier_to_model,
)
from hypogeum.providers.muapi_video_adapter import (  # noqa: E402
    SCHEMA_VERSION as _VIDEO_PAYLOAD_SCHEMA_VERSION,
    decode_video_payload,
    encode_video_payload,
)
from hypogeum.scene_identity import (  # noqa: E402
    SceneIdentityConflictError,
    enforce_global_scene_identity,
    enforce_scene_internal_consistency,
)
from hypogeum.video_prompt_writer import (  # noqa: E402
    REQUIRED_SHOT_FIELDS,
    assemble_video_scene_prompt,
    compile_authoritative_shot_prompt,
)

_TOOL_VERSION = "migrate_legacy_manifest_to_envelopes.py@2"
_COMPILER_IDENTITY = "hypogeum.video_prompt_writer.compile_authoritative_shot_prompt"
_PROVIDER = "muapi"
_PROVIDER_OPERATION = "video.text_to_video"
_ENVELOPE_SCHEMA_VERSION = "1.0"
_FIXED_8S_MODEL_FAMILIES = _VEO3_MODELS | _VEO3_1_MODELS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")


def _structured_spec_payload(shot: dict) -> dict:
    """Exactly the field set hypogeum.video_prompt_writer.
    compile_authoritative_shot_prompt() requires and consumes
    (REQUIRED_SHOT_FIELDS) -- "the exact input the compiler consumed"
    per architecture doc §3.1's structured_spec_checksum definition.
    Never includes primary_prompt/fallback_prompt (those are the
    compiler's OUTPUT, not its input -- including them would make this
    checksum circular)."""
    return {f: shot.get(f) for f in REQUIRED_SHOT_FIELDS}


def _structured_spec_checksum(shot: dict) -> str:
    return _sha256_bytes(_canonical_json_bytes(_structured_spec_payload(shot)))


def compute_workflow_identity() -> dict:
    """
    No canonical Workflow Registry exists in this repository today: the
    `runs.pipeline_version` column (hypogeum/db.py) is never written by
    any code path (confirmed by grep across hypogeum/ and by inspecting
    the live database, where every row's value is empty), and
    WORKFLOW_MODEL.md's "Workflow Registry" section describes required
    infrastructure ("Every Workflow must be registered... Registration
    includes: Name, Version...") that was never built. Always returns
    unavailable -- this is a single, isolated seam so a future real
    Workflow Registry implementation only needs to change this one
    function, and so tests can monkeypatch it to prove the rest of this
    tool's logic behaves correctly once a registry exists, without the
    CLI ever exposing an operator-supplied override for it.
    """
    return {
        "available": False, "version": None,
        "reason": (
            "no canonical source exists (runs.pipeline_version is never populated; "
            "WORKFLOW_MODEL.md's Workflow Registry is unimplemented)"
        ),
    }


# compute_channel_package_identity() moved to hypogeum.channel_config
# (Commit C of the Workflow Registry program) so it is a real, reusable
# Factory-Core-adjacent function any caller can use, not something only
# this diagnostic tool could compute -- imported above. It now also
# folds in the versioned Channel Package directory (channel_packages/
# {channel_id}/), not just the DB-backed profiles.


@dataclass(frozen=True)
class ExplicitBinding:
    """Part 4 (model binding authority): a caller-approved, immutable
    provider/model/operation binding with an explicit Decimal per-call
    cost ceiling. When supplied to compile_manifest_to_envelopes(), this
    binding is authoritative for every compiled entry -- the manifest's
    own model_tier_planned field and any other tier-derived default are
    never consulted. A ceiling that does not match the model's own known
    cost (hypogeum.muapi.MODEL_CREDIT_COSTS) blocks compilation for that
    entry (envelope_fields_valid=False) rather than silently accepting
    either value. Never applied to a live episode by this module itself
    -- a caller decides when and whether to pass one."""

    provider: str
    model: str
    provider_operation: str
    max_cost_per_call_ceiling: Decimal

    def __post_init__(self):
        if self.max_cost_per_call_ceiling <= 0:
            raise ValueError(f"max_cost_per_call_ceiling must be positive, got {self.max_cost_per_call_ceiling!r}")


@dataclass
class EntryCompileResult:
    shot_id: Optional[str]
    beat_id: Optional[str]
    deliverable: Optional[str]
    auth_index: Optional[int]
    envelope_fields_valid: bool = False
    envelope_fields_reasons: list = field(default_factory=list)
    dependency_identity_available: bool = False
    dependency_identity_reasons: list = field(default_factory=list)
    legacy_path_compatible: bool = False
    legacy_path_reasons: list = field(default_factory=list)
    envelope_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    checksums: dict = field(default_factory=dict)
    envelope_dict: Optional[dict] = None

    @property
    def paid_execution_ready(self) -> bool:
        return self.envelope_fields_valid and self.dependency_identity_available and self.legacy_path_compatible

    def to_report_dict(self, *, include_envelope_dict: bool = False) -> dict:
        d = {
            "shot_id": self.shot_id, "beat": self.beat_id, "deliverable": self.deliverable,
            "auth_index": self.auth_index,
            "envelope_fields_valid": self.envelope_fields_valid,
            "envelope_fields_reasons": self.envelope_fields_reasons,
            "dependency_identity_available": self.dependency_identity_available,
            "dependency_identity_reasons": self.dependency_identity_reasons,
            "legacy_path_compatible": self.legacy_path_compatible,
            "legacy_path_reasons": self.legacy_path_reasons,
            "paid_execution_ready": self.paid_execution_ready,
            "envelope_id": self.envelope_id, "idempotency_key": self.idempotency_key,
            "checksums": self.checksums,
        }
        # Opt-in only (default False, this tool's own original report
        # shape is unchanged for every existing caller) -- a caller
        # that needs to reconstruct the actual GenerationExecutionEnvelope
        # object (plan §6 Commit M) sets this; ordinary --report-path
        # dumps stay compact.
        if include_envelope_dict and self.envelope_dict is not None:
            d["envelope_dict"] = self.envelope_dict
        return d


def _episode_dir(assets_root: Path, episode_id: str) -> Path:
    return assets_root / "episodes" / episode_id


def _unresolvable(shot_id, beat_id, deliverable, auth_index, reason: str) -> EntryCompileResult:
    """A structural pre-resolution failure (no scene/shot found, duplicate
    shot_id, etc.) -- none of the three dimensions can be evaluated at
    all, so all three are conservatively False, never silently True."""
    return EntryCompileResult(
        shot_id=shot_id, beat_id=beat_id, deliverable=deliverable, auth_index=auth_index,
        envelope_fields_valid=False, envelope_fields_reasons=[reason],
        dependency_identity_available=False, dependency_identity_reasons=[reason],
        legacy_path_compatible=False, legacy_path_reasons=[reason],
    )


def compile_manifest_to_envelopes(
    args: argparse.Namespace, config: Optional[Config] = None, *,
    workflow_identity_resolver=None, tag_as_reconstructed: bool = True, authorization_version: int = 1,
    include_envelope_dicts: bool = False, explicit_binding: Optional[ExplicitBinding] = None,
) -> dict:
    """
    Runs the full read-only compilation pass over every active manifest
    entry. Zero provider calls, zero writes to any live file. Reads
    (never writes) the local Channel Package database via
    hypogeum.channel_config.load_channel_config(). Returns the full
    machine-readable dry-run report dict.

    config: optional injected Config (tests pass a fixture-built Config
    so model/tier resolution used for the legacy_path_compatible
    dimension is deterministic and independent of the real process
    environment). Defaults to a real Config() when omitted.

    workflow_identity_resolver: the single isolated seam
    compute_workflow_identity()'s own docstring names for exactly this
    purpose -- a zero-argument callable returning
    {"available": bool, "version": Optional[str], "reason": Optional[str]}.
    Defaults to compute_workflow_identity itself (this tool's own,
    permanently-unavailable stub) -- every existing caller/test is
    completely unaffected. A caller with a real, current Workflow
    Registry resolution (plan §6 Commit M, once hypogeum.
    workflow_executor.resolve_workflow_binding() actually resolves for
    the target channel) supplies a real resolver instead.

    tag_as_reconstructed: when True (default, this tool's own original
    and only behavior until now), every constructed envelope's
    audit_metadata["workflow_binding_reconstructed"] is set to True --
    permanently ineligible for real submission (see hypogeum.
    submission_gate._check_workflow_binding_not_reconstructed()).
    A caller compiling from entirely live, current, provable evidence
    (not reconstructing a pre-existing legacy authorization) passes
    False instead.

    authorization_version: every constructed envelope/authorization_id
    pair is stamped with this version (default 1, this tool's own
    original and only behavior). A caller producing a genuinely NEW
    authorization batch for an episode that already has an existing
    (e.g. legacy-manifest-only) version 1 passes a higher value so the
    resulting idempotency_key (derived from
    f"{authorization_id}:{authorization_version}:{provider_operation}")
    never collides with a version-1 compile of the same entries.

    include_envelope_dicts: when True, each report entry that has a
    real envelope also carries "envelope_dict" (the exact dict
    GenerationExecutionEnvelope.to_dict()/.from_dict() round-trips) --
    default False, this tool's own original, compact report shape,
    unaffected. A caller that needs to reconstruct real envelope
    objects (plan §6 Commit M) sets this.

    explicit_binding: Part 4 (model binding authority). When supplied,
    every entry's authorized provider/model/operation/cost-ceiling
    comes from this single, caller-approved ExplicitBinding instead of
    the manifest's own model_tier_planned field -- e.g. compiling an
    entire proposal set against a caller-approved Lite binding
    (veo3.1-lite-text-to-video, USD 0.39/call). Default None (the
    tool's own original behavior, unaffected). Never applied to a live
    episode by this function itself.
    """
    assets_root = Path(args.assets_root)
    episode_dir = _episode_dir(assets_root, args.episode_id)
    if config is None:
        config = Config()
    if workflow_identity_resolver is None:
        # Late-bound (looked up here, at call time, never captured as a
        # def-time default) so tests that monkeypatch the module-level
        # compute_workflow_identity name directly keep working exactly
        # as before this parameter existed.
        workflow_identity_resolver = compute_workflow_identity

    global_blockers: list = []

    manifest_path = Path(args.manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest_md5 = _md5_bytes(manifest_bytes)
    manifest = json.loads(manifest_bytes)

    vp_path = episode_dir / f"visual_package_{args.episode_id}.json"
    visual_package = _load_json(vp_path)
    scenes = visual_package.get("scenes", [])

    plan_path = episode_dir / f"cinematic_coverage_plan_{args.episode_id}.json"
    plan_bytes = plan_path.read_bytes() if plan_path.exists() else None
    plan_checksum_sha256 = _sha256_bytes(plan_bytes) if plan_bytes is not None else None
    plan_checksum_md5_current = _md5_bytes(plan_bytes) if plan_bytes is not None else None
    plan_checksum_md5_manifest = manifest.get("plan_checksum_after")
    plan_stale = (
        plan_checksum_md5_current is not None
        and plan_checksum_md5_manifest is not None
        and plan_checksum_md5_current != plan_checksum_md5_manifest
    )
    if plan_bytes is None:
        global_blockers.append({"check": "plan_file_missing", "reason": f"{plan_path} does not exist"})
    if plan_stale:
        global_blockers.append({
            "check": "stale_plan_snapshot",
            "reason": (
                f"live cinematic_coverage_plan md5 {plan_checksum_md5_current} != "
                f"manifest plan_checksum_after {plan_checksum_md5_manifest}"
            ),
        })

    compiler_module_path = _ROOT / "hypogeum" / "video_prompt_writer.py"
    compiler_checksum = _sha256_bytes(compiler_module_path.read_bytes())

    db_path = Path(args.db_path) if args.db_path else None
    channel_package = compute_channel_package_identity(args.channel_id, db_path=db_path)

    scene_identity_ok = True
    try:
        enforce_global_scene_identity(scenes)
        enforce_scene_internal_consistency(scenes)
    except SceneIdentityConflictError as exc:
        scene_identity_ok = False
        global_blockers.append({"check": "scene_identity_conflict", "reason": str(exc)})

    active, inactive = resolve_active_entries(manifest)

    seen_shot_ids: dict = {}
    for entry in active:
        sid = entry.get("target_shot_id")
        seen_shot_ids.setdefault(sid, []).append(entry)
    duplicate_shot_ids = {sid for sid, group in seen_shot_ids.items() if len(group) > 1}

    beat_to_scene, duplicate_beats = scene_by_beat(scenes)

    results: list = []

    if not scene_identity_ok:
        for entry in active:
            results.append(_unresolvable(
                entry.get("target_shot_id"), entry.get("beat"), entry.get("deliverable"),
                entry.get("auth_index"), "scene_identity_conflict: see global_blockers",
            ))
        return _finalize_report(
            args, manifest, manifest_md5, plan_checksum_sha256, plan_checksum_md5_current,
            plan_checksum_md5_manifest, active, inactive, results, global_blockers, channel_package,
            include_envelope_dicts=include_envelope_dicts,
        )

    missing_shots_for_allocation = []
    resolved_by_shot: dict = {}
    for entry in active:
        shot_id = entry.get("target_shot_id")
        beat_id = entry.get("beat")
        if entry.get("_state_inconsistency"):
            resolved_by_shot[shot_id] = f"state_inconsistency: {entry['_state_inconsistency']}"
            continue
        if shot_id in duplicate_shot_ids:
            resolved_by_shot[shot_id] = f"duplicate_shot_id: {shot_id!r} appears {len(seen_shot_ids[shot_id])} times among active entries"
            continue
        scene = beat_to_scene.get(beat_id)
        if scene is None:
            resolved_by_shot[shot_id] = f"no scene found with narration_reference == beat {beat_id!r}"
            continue
        planned_shot, match_count = find_planned_shot(scene, shot_id)
        if match_count == 0:
            resolved_by_shot[shot_id] = f"shot_id {shot_id!r} not found in scene {scene.get('scene_id')!r} coverage plan"
            continue
        if match_count > 1:
            resolved_by_shot[shot_id] = f"shot_id {shot_id!r} appears {match_count} times in scene {scene.get('scene_id')!r} coverage plan"
            continue
        missing_shots_for_allocation.append({
            "beat_id": beat_id, "shot_id": shot_id,
            "scene_id": scene.get("scene_id"), "short_id": scene.get("short_id"),
            "shot_order": planned_shot.get("shot_order"),
        })

    # Deterministic, plan-derived output-slot identity (docs/architecture/
    # PROTECTED_GENERATION_LIFECYCLE_RECONCILIATION.md) -- replaces
    # plan_confirmed_shot_allocations() as the identity source for
    # COMPILATION specifically. That function remains correct and
    # unchanged for its own real purpose (finding a live, filesystem-
    # safe slot for a generation attempt about to actually run inside
    # hypogeum.agents.clip_repair_agent); it is a read-only migration
    # diagnostic utility here at most, never the source of an already-
    # compiled envelope's identity. hypogeum.deterministic_allocation.
    # resolve_canonical_output_slot() never touches the filesystem, so
    # adding, deleting, or discovering an unrelated physical file can
    # never change a shot's compiled envelope identity.
    allocations_by_shot: dict = {}
    for missing in missing_shots_for_allocation:
        if missing["shot_order"] is None:
            global_blockers.append({
                "check": "missing_shot_order_for_deterministic_allocation",
                "reason": f"shot {missing['shot_id']!r} has no shot_order in the coverage plan -- "
                          "cannot derive a deterministic output slot",
            })
            continue
        slot = resolve_canonical_output_slot(
            episode_id=args.episode_id, scene_id=missing["scene_id"], shot_order=missing["shot_order"],
            short_id=missing["short_id"], assets_root=assets_root,
        )
        allocations_by_shot[missing["shot_id"]] = {
            "extra_scene_id": slot.extra_scene_id, "output_path": str(slot.output_path),
        }

    seen_envelope_ids: dict = {}
    seen_idempotency_keys: dict = {}
    duplicate_findings: list = []

    for entry in active:
        shot_id = entry.get("target_shot_id")
        beat_id = entry.get("beat")
        deliverable = entry.get("deliverable")
        auth_index = entry.get("auth_index")

        if shot_id in resolved_by_shot:
            results.append(_unresolvable(shot_id, beat_id, deliverable, auth_index, resolved_by_shot[shot_id]))
            continue

        if beat_id in duplicate_beats:
            results.append(_unresolvable(
                shot_id, beat_id, deliverable, auth_index,
                f"beat {beat_id!r} maps to more than one scene via narration_reference",
            ))
            continue

        scene = beat_to_scene[beat_id]
        planned_shot, _ = find_planned_shot(scene, shot_id)
        result = _compile_one_entry(
            entry, scene, planned_shot, config=config, manifest=manifest,
            plan_checksum_sha256=plan_checksum_sha256, compiler_checksum=compiler_checksum,
            allocation=allocations_by_shot.get(shot_id), episode_id=args.episode_id, episode_dir=episode_dir,
            channel_id=args.channel_id, channel_package=channel_package,
            manifest_md5=manifest_md5, workflow_identity_resolver=workflow_identity_resolver,
            tag_as_reconstructed=tag_as_reconstructed, authorization_version=authorization_version,
            explicit_binding=explicit_binding,
        )
        results.append(result)

        if result.envelope_id:
            seen_envelope_ids.setdefault(result.envelope_id, []).append(shot_id)
        if result.idempotency_key:
            seen_idempotency_keys.setdefault(result.idempotency_key, []).append(shot_id)

    for eid, shots in seen_envelope_ids.items():
        if len(shots) > 1:
            duplicate_findings.append({"check": "duplicate_envelope_id", "envelope_id": eid, "shot_ids": shots})
    for key, shots in seen_idempotency_keys.items():
        if len(shots) > 1:
            duplicate_findings.append({"check": "duplicate_idempotency_key", "idempotency_key": key, "shot_ids": shots})

    if duplicate_findings:
        affected_shots = {sid for f in duplicate_findings for sid in f["shot_ids"]}
        for r in results:
            if r.shot_id in affected_shots and r.envelope_fields_valid:
                r.envelope_fields_valid = False
                r.envelope_fields_reasons.append("duplicate_identity: see duplicate_findings in report")

    return _finalize_report(
        args, manifest, manifest_md5, plan_checksum_sha256, plan_checksum_md5_current,
        plan_checksum_md5_manifest, active, inactive, results, global_blockers, channel_package,
        duplicate_findings=duplicate_findings, include_envelope_dicts=include_envelope_dicts,
    )


def _compile_one_entry(
    entry: dict, scene: dict, planned_shot: dict, *, config: Config, manifest: dict,
    plan_checksum_sha256: Optional[str], compiler_checksum: str, allocation: Optional[dict],
    episode_id: str, episode_dir: Path, channel_id: str, channel_package: dict, manifest_md5: str,
    workflow_identity_resolver=None, tag_as_reconstructed: bool = True, authorization_version: int = 1,
    explicit_binding: "Optional[ExplicitBinding]" = None,
) -> EntryCompileResult:
    if workflow_identity_resolver is None:
        workflow_identity_resolver = compute_workflow_identity
    shot_id = entry.get("target_shot_id")
    beat_id = entry.get("beat")
    deliverable = entry.get("deliverable")
    auth_index = entry.get("auth_index")
    result = EntryCompileResult(shot_id=shot_id, beat_id=beat_id, deliverable=deliverable, auth_index=auth_index)

    # ------------------------------------------------------------------
    # Dimension 1: envelope_fields_valid -- this authorization's own
    # binding, from immutable authorization evidence only. Never
    # re-derived from the live scene's provider_routing.
    # ------------------------------------------------------------------
    stored_prompt = (planned_shot.get("primary_prompt") or "").strip()
    if not stored_prompt:
        result.envelope_fields_reasons.append("missing_or_empty_prompt: planned shot has no rendered primary_prompt")

    provenance = None
    if stored_prompt:
        try:
            checksum = check_prompt_checksum(stored_prompt, entry.get("primary_prompt_checksum"))
            result.checksums["stored_prompt_sha256"] = checksum
        except _CheckError as exc:
            result.envelope_fields_reasons.append(f"{exc.check}: {exc.reason}")

        try:
            provenance = check_provenance(entry)
        except _CheckError as exc:
            result.envelope_fields_reasons.append(f"{exc.check}: {exc.reason}")

        try:
            recompiled = compile_authoritative_shot_prompt(scene, planned_shot)
            if recompiled["primary_prompt"] != stored_prompt:
                result.envelope_fields_reasons.append(
                    "stale_compiled_prompt: freshly recompiled primary_prompt differs byte-for-byte "
                    "from the coverage plan's stored primary_prompt for this shot"
                )
            elif recompiled["prompt_checksum"] != entry.get("primary_prompt_checksum"):
                result.envelope_fields_reasons.append(
                    "stale_compiled_prompt_checksum: freshly recompiled prompt_checksum differs from "
                    "the manifest's stored primary_prompt_checksum"
                )
        except Exception as exc:  # noqa: BLE001 -- any compiler failure is a hard block, never silently skipped
            result.envelope_fields_reasons.append(f"compiler_error: {type(exc).__name__}: {exc}")

    # The prompt text itself is resolved here (still gated on the
    # freshness checks above) -- the CANONICAL JSON payload
    # (operation_payload_content/operation_payload_sha256) is built
    # further below, once the authorized model/aspect_ratio/duration
    # and the deterministic output allocation are also resolved, since
    # hypogeum.providers.muapi_video_adapter.encode_video_payload()
    # (the ONLY producer of bytes decode_video_payload() -- the real
    # adapter's own decoder -- accepts for operation_payload_schema_
    # version="video.text_to_video/1.0") requires all of them.
    assembled_prompt = None
    if stored_prompt and not result.envelope_fields_reasons:
        assembled_prompt = assemble_video_scene_prompt(stored_prompt)

    # Authorized binding -- either the caller-approved, immutable
    # explicit_binding (Part 4: model binding authority), or, when none
    # is supplied, the manifest's own model_tier_planned field. Never
    # the live scene's provider_routing -- that is only ever consulted
    # separately, for the legacy_path_compatible diagnostic below.
    authorized_aspect_ratio = entry.get("aspect_ratio")
    if explicit_binding is not None:
        authorized_model = explicit_binding.model
        known_cost = MODEL_CREDIT_COSTS.get(authorized_model)
        if known_cost is None:
            result.envelope_fields_reasons.append(
                f"unknown_model_cost: no known credit cost for explicit_binding model {authorized_model!r}"
            )
            authorized_cost = None
        elif Decimal(str(known_cost)) != explicit_binding.max_cost_per_call_ceiling:
            result.envelope_fields_reasons.append(
                f"explicit_binding_cost_mismatch: caller-approved ceiling "
                f"{explicit_binding.max_cost_per_call_ceiling} does not match model {authorized_model!r}'s "
                f"known cost {known_cost}"
            )
            authorized_cost = None
        else:
            authorized_cost = known_cost
    else:
        authorized_model = manifest.get("model_tier_planned")
        authorized_cost = MODEL_CREDIT_COSTS.get(authorized_model) if authorized_model else None
    authorized_duration_seconds = _VEO3_1_DEFAULT_DURATION if authorized_model in _FIXED_8S_MODEL_FAMILIES else None

    if not authorized_model:
        result.envelope_fields_reasons.append("missing_authorized_model: no explicit_binding and manifest has no model_tier_planned")
    elif authorized_cost is None and not result.envelope_fields_reasons:
        result.envelope_fields_reasons.append(f"unknown_model_cost: no known credit cost for authorized model {authorized_model!r}")
    if not authorized_aspect_ratio:
        result.envelope_fields_reasons.append("missing_authorized_aspect_ratio: manifest entry has no aspect_ratio")
    if authorized_model and authorized_duration_seconds is None:
        result.envelope_fields_reasons.append(
            f"no_canonical_fixed_duration_specification: authorized model {authorized_model!r} is not in a "
            f"known fixed-duration family (hypogeum.muapi._VEO3_MODELS / _VEO3_1_MODELS)"
        )

    if allocation is None:
        result.envelope_fields_reasons.append("output_allocation_missing: resolve_canonical_output_slot() produced no allocation for this shot")
        extra_scene_id = output_path = None
    else:
        extra_scene_id = allocation["extra_scene_id"]
        output_path = allocation["output_path"]
        # Physical output occupancy is deliberately NOT checked here --
        # compile-time envelope validity must never depend on current
        # filesystem state (docs/architecture/PROTECTED_GENERATION_
        # LIFECYCLE_RECONCILIATION.md). Collision detection happens once,
        # at the actual pre-execution boundary, via
        # hypogeum.deterministic_allocation's own check_output_collision()
        # (wired into the shared mandatory gate elsewhere) -- never as a
        # compile-time diagnostic that would make two otherwise-identical
        # compiles disagree depending on what happens to exist on disk.

    # Canonical JSON operation payload (video.text_to_video/1.0) --
    # NEVER raw prompt text. Built only once every field
    # encode_video_payload() requires is itself already resolved and
    # error-free; a round-trip through decode_video_payload() (the
    # REAL adapter's own decoder, imported verbatim -- no second JSON
    # format invented here) is required to succeed before this
    # envelope is ever considered valid, so a caller can never emit
    # bytes the real adapter would itself reject.
    operation_payload_content = None
    operation_payload_sha256 = None
    if (
        assembled_prompt is not None and authorized_model and authorized_aspect_ratio
        and authorized_duration_seconds is not None and extra_scene_id is not None
        and not result.envelope_fields_reasons
    ):
        operation_payload_bytes = encode_video_payload(
            prompt=assembled_prompt, model=authorized_model, aspect_ratio=authorized_aspect_ratio,
            duration_seconds=float(authorized_duration_seconds), reference_image_url=None,
            scene_id=extra_scene_id, short_id=scene.get("short_id"), episode_id=episode_id,
            output_dir=str(episode_dir),
        )
        try:
            decoded = decode_video_payload(operation_payload_bytes)
        except Exception as exc:  # noqa: BLE001 -- a payload the real adapter would reject is a hard block
            result.envelope_fields_reasons.append(
                f"operation_payload_round_trip_failed: {type(exc).__name__}: {exc}"
            )
        else:
            if decoded["prompt"] != assembled_prompt:
                result.envelope_fields_reasons.append(
                    "operation_payload_prompt_not_byte_preserved: decoded JSON prompt field does not match "
                    "the assembled prompt byte-for-byte"
                )
            else:
                operation_payload_content = operation_payload_bytes.decode("utf-8")
                operation_payload_sha256 = _sha256_bytes(operation_payload_bytes)
                result.checksums["operation_payload_sha256"] = operation_payload_sha256

    structured_spec_checksum = _structured_spec_checksum(planned_shot) if stored_prompt else None
    if structured_spec_checksum:
        result.checksums["structured_spec_checksum"] = structured_spec_checksum
    result.checksums["plan_checksum_sha256"] = plan_checksum_sha256
    result.checksums["compiler_checksum"] = compiler_checksum

    # authorization_id / idempotency_key are derived purely from
    # (manifest_id, auth_index, provider_operation) -- no dependency
    # identity is needed to compute them, and duplicate detection must
    # work even when the entry can never become a fully-realized
    # envelope (e.g. workflow_version permanently missing) -- a data-
    # entry error producing two authorizations with the same auth_index
    # is itself an envelope_fields defect, not a dependency-identity one.
    authorization_id = f"{manifest.get('manifest_id')}:{auth_index}" if auth_index is not None else None
    if authorization_id is None:
        result.envelope_fields_reasons.append("missing_auth_index: manifest entry has no auth_index")
    else:
        result.idempotency_key = sha256_text(f"{authorization_id}:{authorization_version}:{_PROVIDER_OPERATION}")

    result.envelope_fields_valid = not result.envelope_fields_reasons

    # ------------------------------------------------------------------
    # Dimension 2: dependency_identity_available -- channel package
    # (now real), workflow_version (still not real), plan checksum.
    # ------------------------------------------------------------------
    if not channel_package["available"]:
        result.dependency_identity_reasons.append(f"channel_package_checksum: {channel_package['reason']}")
    workflow_identity = workflow_identity_resolver()
    workflow_version = workflow_identity["version"]
    if not workflow_identity["available"]:
        result.dependency_identity_reasons.append(f"workflow_version: {workflow_identity['reason']}")
    if not plan_checksum_sha256:
        result.dependency_identity_reasons.append("plan_checksum: plan file unreadable")

    result.dependency_identity_available = not result.dependency_identity_reasons

    # ------------------------------------------------------------------
    # Dimension 3: legacy_path_compatible -- does today's actual
    # production caller agree with the authorized binding? A "no" here
    # is a legacy_execution_path_binding_mismatch, never a claim that
    # the authorization itself is invalid.
    # ------------------------------------------------------------------
    scene_spec = build_scene_spec(scene, entity_hosted_urls={})
    legacy_tier = scene_spec["production_spec"]["primary_tier"]
    legacy_model = _tier_to_model(legacy_tier, None, config)
    legacy_aspect_ratio = scene_spec["visual_spec"]["aspect_ratio"]

    if authorized_model and legacy_model != authorized_model:
        result.legacy_path_reasons.append(
            f"legacy_execution_path_binding_mismatch: current production caller would route tier "
            f"{legacy_tier} -> model {legacy_model!r}, but this authorization binds model {authorized_model!r}"
        )
    if authorized_aspect_ratio and legacy_aspect_ratio != authorized_aspect_ratio:
        result.legacy_path_reasons.append(
            f"legacy_execution_path_binding_mismatch: current production caller derives aspect_ratio "
            f"{legacy_aspect_ratio!r}, but this authorization binds aspect_ratio {authorized_aspect_ratio!r}"
        )
    result.legacy_path_compatible = not result.legacy_path_reasons

    # ------------------------------------------------------------------
    # Only construct a real envelope object when the authorization's own
    # fields are self-consistent AND every dependency identity actually
    # resolves -- never label an entry envelope-valid on a placeholder.
    # legacy_path_compatible is deliberately NOT a precondition here: the
    # envelope binds to the AUTHORIZED model regardless of what the
    # legacy caller would independently choose.
    # ------------------------------------------------------------------
    if result.envelope_fields_valid and result.dependency_identity_available:
        idempotency_key = result.idempotency_key  # already computed above, independent of dependency identity
        expected_output_identity = _compute_asset_id(episode_id, extra_scene_id, scene.get("short_id"))
        plan_version = manifest.get("plan_amendment_id") or "unknown"

        try:
            envelope = GenerationExecutionEnvelope.create(
                envelope_schema_version=_ENVELOPE_SCHEMA_VERSION, envelope_version=1,
                channel_id=channel_id,
                channel_package_version=channel_package["version"],
                channel_package_checksum=channel_package["checksum"],
                execution_id=episode_id, workflow_version=workflow_version,
                plan_version=plan_version, plan_checksum=plan_checksum_sha256,
                scene_id=extra_scene_id, beat_id=beat_id, planned_shot_id=shot_id,
                structured_spec_checksum=structured_spec_checksum,
                compiler_identity=_COMPILER_IDENTITY,
                compiler_version=provenance.get("compiler_version") if provenance else None,
                operation_payload_content=operation_payload_content,
                operation_payload_sha256=operation_payload_sha256,
                operation_payload_schema_version=_VIDEO_PAYLOAD_SCHEMA_VERSION,
                content_provenance={
                    "compiler": provenance.get("compiler") if provenance else None,
                    "compiler_version": provenance.get("compiler_version") if provenance else None,
                    "source_work_item_id": provenance.get("source_shot_id") if provenance else None,
                    "rendered_at": provenance.get("rendered_at") if provenance else None,
                },
                provider=_PROVIDER, model=authorized_model, provider_operation=_PROVIDER_OPERATION,
                duration_seconds=Decimal(str(authorized_duration_seconds)), aspect_ratio=authorized_aspect_ratio, seed=None,
                deterministic_output_path=output_path, expected_output_identity=expected_output_identity,
                authorization_id=authorization_id, authorization_version=authorization_version,
                max_cost_per_call=Decimal(str(authorized_cost)),
                run_level_budget_ref=manifest.get("manifest_id"), episode_level_budget_ref=episode_id,
                currency="USD", retry_authorized=False, fallback_authorized=False,
                idempotency_key=idempotency_key, created_at=_now_iso(),
                expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
                dependency_checksums={
                    "channel_package": channel_package["checksum"], "plan": plan_checksum_sha256,
                    "structured_spec": structured_spec_checksum, "compiler": compiler_checksum,
                },
                audit_metadata={
                    "creation_reason": "migrated_from_legacy_manifest",
                    "source_file_checksum": manifest_md5,
                    "source_file_checksum_algorithm": "md5",
                    "tool_version": _TOOL_VERSION,
                    # Enforced at the submission gate's reconstructed-
                    # binding check (plan §5): an envelope bound against
                    # evidence reconstructed for a pre-existing, already-
                    # authorized legacy entry -- never provably
                    # contemporaneous with a real, current authorization
                    # decision -- is permanently ineligible for real
                    # submission, diagnostic/Observation-Only use only.
                    # True by default (this tool's own original and only
                    # behavior); a caller compiling from entirely live,
                    # current, provable evidence (plan §6 Commit M)
                    # passes tag_as_reconstructed=False instead.
                    "workflow_binding_reconstructed": tag_as_reconstructed,
                },
                authorization_status="authorized",
            )
        except (EnvelopeSchemaError, EnvelopeIntegrityError) as exc:
            result.envelope_fields_valid = False
            result.envelope_fields_reasons.append(f"envelope_construction_failed: {type(exc).__name__}: {exc}")
        else:
            result.envelope_id = envelope.envelope_id
            result.idempotency_key = idempotency_key
            result.envelope_dict = envelope.to_dict()

    return result


def _finalize_report(
    args: argparse.Namespace, manifest: dict, manifest_md5: str,
    plan_checksum_sha256: Optional[str], plan_checksum_md5_current: Optional[str],
    plan_checksum_md5_manifest: Optional[str], active: list, inactive: list,
    results: list, global_blockers: list, channel_package: dict,
    duplicate_findings: Optional[list] = None, include_envelope_dicts: bool = False,
) -> dict:
    envelope_valid = [r for r in results if r.envelope_fields_valid]
    dependency_unverifiable = [r for r in results if not r.dependency_identity_available]
    legacy_path_incompatible = [r for r in results if not r.legacy_path_compatible]
    paid_execution_ready = [r for r in results if r.paid_execution_ready]

    dependency_reason_distribution: dict = {}
    for r in dependency_unverifiable:
        for reason in r.dependency_identity_reasons:
            dependency_reason_distribution[reason] = dependency_reason_distribution.get(reason, 0) + 1

    legacy_path_findings = [
        {"shot_id": r.shot_id, "beat": r.beat_id, "reasons": r.legacy_path_reasons}
        for r in legacy_path_incompatible
    ]

    totals_by_deliverable: dict = {}
    for entry in active:
        d = entry.get("deliverable", "unknown")
        totals_by_deliverable[d] = totals_by_deliverable.get(d, 0) + 1

    overall_result = (
        "BLOCKED" if global_blockers
        else "PAID_EXECUTION_READY" if len(paid_execution_ready) == len(active) and active
        else "NOT_READY"
    )

    return {
        "tool_version": _TOOL_VERSION,
        "generated_at": _now_iso(),
        "mode": "dry_run",
        "episode_id": args.episode_id,
        "channel_id": args.channel_id,
        "manifest_path": str(args.manifest_path),
        "manifest_id": manifest.get("manifest_id"),
        "manifest_md5": manifest_md5,
        "plan_checksum_sha256": plan_checksum_sha256,
        "plan_checksum_md5_current": plan_checksum_md5_current,
        "plan_checksum_md5_manifest_recorded": plan_checksum_md5_manifest,
        "plan_checksum_match": (
            plan_checksum_md5_current == plan_checksum_md5_manifest
            if plan_checksum_md5_current and plan_checksum_md5_manifest else None
        ),
        "channel_package_available": channel_package["available"],
        "channel_package_checksum": channel_package["checksum"],
        "channel_package_version": channel_package["version"],
        "channel_package_reason": channel_package["reason"],
        "active_entry_count": len(active),
        "inactive_entry_count": len(inactive),
        "active_totals_by_deliverable": dict(sorted(totals_by_deliverable.items())),
        "envelope_valid_count": len(envelope_valid),
        "dependency_unverifiable_count": len(dependency_unverifiable),
        "legacy_path_incompatible_count": len(legacy_path_incompatible),
        "paid_execution_ready_count": len(paid_execution_ready),
        "dependency_reason_distribution": dict(sorted(dependency_reason_distribution.items())),
        "legacy_path_findings": sorted(legacy_path_findings, key=lambda r: (r.get("beat") or "", r.get("shot_id") or "")),
        "global_blockers": global_blockers,
        "duplicate_findings": duplicate_findings or [],
        "entries": sorted(
            [r.to_report_dict(include_envelope_dict=include_envelope_dicts) for r in results],
            key=lambda r: (r.get("beat") or "", r.get("shot_id") or ""),
        ),
        "provider_calls_made": 0,
        "spend_usd": 0.0,
        "envelopes_persisted": 0,
        "overall_result": overall_result,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--episode-id", required=True)
    p.add_argument("--channel-id", default="hypogeum")
    p.add_argument("--manifest-path", default=None, help="defaults to the canonical episode manifest path")
    p.add_argument("--assets-root", default=str(_ROOT / ".hypogeum-assets"))
    p.add_argument("--db-path", default=None, help="defaults to hypogeum.channel_config's own default DB path")
    p.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Read-only compilation and validation. This is the only currently-implemented mode; "
             "the flag exists explicitly so a future write-enabled mode can never be silently defaulted-into.",
    )
    p.add_argument("--report-path", default=None, help="optional; if omitted, nothing is written to disk")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if not args.dry_run:
        print("Only --dry-run is currently implemented; refusing to proceed.", file=sys.stderr)
        return 2

    if args.manifest_path is None:
        args.manifest_path = str(
            _episode_dir(Path(args.assets_root), args.episode_id)
            / f"generation_authorization_manifest_{args.episode_id}.json"
        )

    report = compile_manifest_to_envelopes(args)

    if args.report_path:
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = report_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(report, f, indent=2, sort_keys=True, default=str)
            f.write("\n")
        tmp.replace(report_path)

    print(json.dumps(
        {k: v for k, v in report.items() if k != "entries"},
        indent=2, sort_keys=True, default=str,
    ))
    return 0 if report["overall_result"] != "BLOCKED" else 1


if __name__ == "__main__":
    sys.exit(main())
