"""
hypogeum/production_plan.py — Compiled Production Plan (2026-08-24, corrected).

The missing Factory invariant identified by the ep_f3619022/ep_af078f34
forensic audit: before the first paid VIDEO/VISUAL provider call, one
immutable, cost-complete, coverage-validated compiled Production Plan must
enumerate every potentially billable asset for the deliverable shape the
episode's Channel Package EXPLICITLY declares (never inferred from
script_package — see load_deliverable_contract()), priced exclusively
from the same authoritative constants the real reservation/ledger path
already uses (never an independently invented number — see "Authoritative
price sources" below), using the SAME real per-scene provider/model
routing hypogeum.muapi.submit_scene() will actually use, addressed by the
SAME canonical scene asset IDs the real ledger records
(hypogeum.muapi._compute_asset_id()) — never a parallel beat-ID identity
system.

Enforcement lives at the existing common provider-submission boundary
(hypogeum.muapi._submit_scene_via_gate(), the single real entry to
hypogeum.submission_gate.submit() for video generation) — this module does
NOT define a second, parallel authorization registry. It compiles and
persists the plan; muapi.py looks a scene's canonical asset_id up in the
persisted plan (via load_production_plan() + ProductionPlan.asset_by_id())
and refuses to compile a GenerationExecutionEnvelope for an undeclared
asset_id, a changed prompt, or a changed model, before any network call.

Channel-agnostic: this module hardcodes no channel's shape, cadence, or
cost policy. compile_production_plan() loads and validates an explicit
deliverable_contract_v1.json artifact from channel_packages/{channel_id}/
(checksum-verified against that channel's own channel_package_manifest.json,
mirroring hypogeum.channel_config.load_platform_distribution_policy()'s
exact fail-closed discipline) and REJECTS the episode if its real,
compiled script_package does not match that contract's declared shape
exactly. channel_id is used only to locate the contract — it is never
itself treated as the contract (see load_deliverable_contract()).

Authoritative price sources (never independently invented here):
  video generation             — hypogeum.muapi.MODEL_CREDIT_COSTS, the SAME
      table hypogeum.muapi.MUAPIBudgetGuard.check_and_record_attempt()
      already uses for real pre-submission budget reservation. Each asset's
      model is resolved the SAME way submit_scene() resolves it
      (hypogeum.muapi._tier_to_model() against that SCENE's own real
      production_spec.provider_routing.primary_tier) — never a single
      MUAPI_DEFAULT_TIER model applied uniformly to every asset.
  thumbnails                    — same MODEL_CREDIT_COSTS table, keyed by
      config.THUMBNAIL_IMAGE_MODEL. Candidate counts/attempt bounds come
      from hypogeum.schemas's own hard-enforced main-episode requirement
      (exactly 3 candidates — _validate_thumbnail_package) and
      hypogeum.agents.thumbnail_generation_agent._MAX_ATTEMPTS_PER_CANDIDATE
      (the real per-candidate retry bound for the non-unified generation
      path) — never invented counts. The main episode's dominant
      "unified_integrated" generation path (typography_integration=
      "embedded") makes exactly one attempt per candidate (no retry loop),
      so main-episode thumbnails are the same 3 calls in both normal and
      maximum. Shorts' true candidate count is schema-unbounded (Thumbnail
      Director's own Phase-1 Claude planning decides it, and that planning
      has not run yet when this plan compiles) — config.
      THUMBNAIL_SHORT_CANDIDATE_CAP is an explicit, documented, Factory
      Core-level conservative cap (not a per-channel policy, not invented
      per-episode) used only to bound the MAXIMUM; see that config field's
      own docstring for the honest limitation this represents.
  source enhancement            — hypogeum.providers.muapi_video_upscaler.
      UPSCALE_CEILING_USD, the SAME Decimal already passed as
      max_cost_per_call to the real Generation Execution Envelope
      authorization/budget-scope reservation for every real enhancement
      call. Whether a given clip actually NEEDS enhancement is decided by
      hypogeum.source_enhancement.qualify_asset() by comparing that clip's
      REAL, POST-GENERATION probed resolution against
      hypogeum.artifact_compatibility.DELIVERABLE_RESOLUTION_POLICY — a
      fact that does not exist at plan-compile time (before generation).
      This is therefore priced ONLY in maximum_bounded_cost_usd (one
      enhancement attempt assumed per declared asset, the real ceiling —
      qualify_asset() enhances at most once per clip), and explicitly
      EXCLUDED from normal_expected_cost_usd: no authoritative historical
      enhancement rate is registered anywhere in this codebase to justify
      any other normal-case number, and inventing one here would repeat
      exactly the mistake this correction removes elsewhere.
  narration                     — hypogeum.elevenlabs.TTS_USD_PER_CHAR, the
      SAME per-character constant already used both for real pre-call
      max_cost reservation and real post-call cost_usd ledger recording.
      narration_segments[].character_cost is a raw character COUNT (not
      USD, not credits) — confirmed against hypogeum/elevenlabs.py's own
      producer (character_cost=char_count) and its test
      (test_character_cost_equals_text_length) — so
      sum(character_cost) * TTS_USD_PER_CHAR exactly mirrors the real
      charge-recording call it is adjacent to in that module.
  music                         — hypogeum.elevenlabs.MUSIC_USD_PER_SEC, the
      SAME per-second constant, same dual real usage as narration above.
  LLM planning calls (OpenRouter) — no per-token authoritative registry
      exists anywhere in this codebase for this category (real OpenRouter
      cost is token-variable and not reserved against pre-call anywhere
      today); this module carries forward finance_agent.py's own
      pre-existing flat per-call estimate unchanged, EXPLICITLY LABELED
      non-authoritative/order-of-magnitude-only in both this docstring and
      the field's own name — seePLANNING_CALL_ESTIMATE_USD below. This is
      a known, documented, scoped-out limitation of this correction pass,
      not a solved category: a real fix needs batch count × selected
      model × bounded max tokens × bounded retries × a real per-token
      OpenRouter price registry, none of which exists today.

A model or category with NO authoritative entry in its registry (video
model, thumbnail model, or an unbounded MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE)
is a REJECTED plan (ProductionPlanPricingError), never a silent numeric
fallback.

Narration/music timing nuance (documented, not hidden): Audio Agent runs
BEFORE Visual Package Director in the real pipeline, so narration is
normally ALREADY GENERATED (a real, already-incurred ElevenLabs cost) by
the time this module compiles a plan — narration cost here is the real,
already-known character_cost from narration_package, not a forward guess.
Music, by contrast, is sourced during Assembly (after this plan compiles)
from a timeline that does not exist yet, so music cost here remains a
genuine forward estimate (section_count × the channel's configured
ELEVENLABS_MUSIC_DEFAULT_DURATION × MUSIC_USD_PER_SEC — the real per-second
price, not an invented per-section flat guess).

Retry accounting — SEPARATE bounded operations, never conflated (2026-08-24
correction of an earlier draft that multiplied (video + enhancement) by
video's own attempt bound, which double-counted enhancement's cost across
every video retry even though enhancement is a structurally different,
independently-bounded provider call):
  video generation   — required assets × 1 attempt (normal) or ×
                        max_chargeable_attempts (maximum); contingency
                        assets are never in normal (they may never be
                        generated at all) and × max_chargeable_attempts in
                        maximum.
  source enhancement — 0 in normal (unknowable at plan time — see above);
                        exactly one attempt per declared asset in maximum.
  thumbnails          — see "Authoritative price sources" above.
  narration / music / planning — see their own sections above; each is its
                        own independent subtotal, never multiplied by
                        video's retry bound.
max_chargeable_attempts itself comes from config.
MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE — the real, already-enforced bound
MUAPIBudgetGuard.check_and_record_attempt() uses to hard-stop repeated
paid attempts at one scene_id. "0" means "unlimited" in that guard's own
semantics; a Production Plan cannot form a maximum bounded cost against an
unlimited attempt count, so that is a rejected plan
(ProductionPlanPricingError), never an infinite or silently-assumed-small
number. Non-chargeable transient retries (HTTP 402 quota-retry, and
provider-side result polling) are NEVER priced here: they either re-issue
the identical request at no charge or read status only; only a real
submission that could plausibly be billed counts toward
max_chargeable_attempts.

Provider clip duration — resolve_canonical_clip_duration() is a BEST-EFFORT
planning input, not a claimed guarantee (2026-08-24 correction: an earlier
draft called this "provider-guaranteed," which a direct trace disproved —
hypogeum.muapi._validate_video_file() explicitly documents duration
validation as "omitted in V1," and no rejection/retry-on-undersized-clip
logic exists anywhere in this codebase). Planned-mode coverage
(validate_plan_coverage(), below) uses the requested duration as its input
because no better authoritative number exists and no per-provider
conservative-margin policy is registered anywhere either — this is a
documented, honest limitation, not a solved guarantee. The REAL, trustworthy
coverage check is hypogeum.agents.coverage_check.compute_beat_coverage()'s
own "actual mode," which reads real ffprobe output after generation; that
check must remain a real, enforced pre-Assembly gate (not merely
informational) for coverage to be trustworthy end to end — this module
alone cannot make that promise at plan-compile time.

Lifecycle:
  1. load_deliverable_contract() + validate_deliverable_shape() — the
     Channel Package is authoritative; script_package is validated
     against it, never the other way around.
  2. compile_production_plan() — one cost-complete artifact: video
     generation, source enhancement, thumbnails, narration, music, and
     planning-call costs, ALL in the same immutable object, using each
     scene's own REAL routing and REAL canonical scene asset ID. One
     normal_expected_cost_usd, one maximum_bounded_cost_usd, each the sum
     of independently-bounded per-operation subtotals.
  3. validate_plan_coverage() — the canonical pre-spend coverage gate
     (per beat AND per deliverable, never aggregate duration alone), using
     the SAME hypogeum.agents.coverage_check.coverage_arithmetic()
     Assembly's actual-mode check uses.
  4. persist_production_plan() — atomic write-once, immutable.
     load_production_plan() re-verifies the persisted content_hash by
     RECOMPUTING it from the payload (payload_content_hash()) and
     rejecting missing/malformed/mismatched hashes
     (ProductionPlanIntegrityError) before any caller sees the plan.
  5. Finance (hypogeum.agents.finance_agent.run_post_vpd_authorization)
     authorizes the plan's own normal/maximum totals directly — it may
     not append narration/music/OpenRouter costs of its own; they are
     already inside the plan.
  6. hypogeum.muapi._submit_scene_via_gate() looks the real scene's
     canonical asset_id up via ProductionPlan.asset_by_id() (loaded from
     the persisted plan) and refuses to proceed for an undeclared
     asset_id, a changed prompt, or a changed model — the existing
     submission_gate.submit() boundary, not a second registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from hypogeum.agents.coverage_check import coverage_arithmetic
from hypogeum.config import Config

PLAN_SCHEMA_VERSION = "1.1"

ROLE_REQUIRED = "required"
ROLE_CONTINGENCY = "contingency"

MAIN_DELIVERABLE_ID = "main_episode"

# hypogeum.schemas._validate_thumbnail_package hard-enforces exactly 3 main
# candidates for EVERY channel (SchemaError otherwise) -- this is a
# Factory-Core-wide schema invariant, not a per-channel policy, so it is
# safe to treat as authoritative here the same way a price constant is.
THUMBNAIL_MAIN_CANDIDATE_COUNT = 3

# finance_agent.py's own pre-existing flat per-call OpenRouter planning-call
# estimate (predates this module) -- carried forward unchanged, not
# reinvented, because no per-token authoritative registry for this category
# exists anywhere in the codebase today (see module docstring). Named
# explicitly as an ESTIMATE, not a price, to keep that limitation visible
# at every call site.
PLANNING_CALL_ESTIMATE_USD = 0.05


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ChannelPackagePolicyDriftError(RuntimeError):
    """Same fail-closed contract as
    hypogeum.channel_config.load_platform_distribution_policy(): an
    unreadable/unregistered/checksum-mismatched Channel Package artifact."""


class DeliverableContractViolationError(Exception):
    """
    Raised when an episode's real, compiled script_package does not match
    its channel's own deliverable_contract_v1.json exactly — e.g. a
    different Short count, a Short not derived from the declared main
    episode, or any standalone Short when the contract declares
    allow_standalone_shorts=false. The Channel Package's contract is
    authoritative; a mismatch is a hard planning failure, never silently
    reconciled.
    """
    def __init__(self, channel_id: str, episode_id: str, reason: str) -> None:
        self.channel_id = channel_id
        self.episode_id = episode_id
        self.reason = reason
        super().__init__(
            f"Episode {episode_id!r} (channel {channel_id!r}): compiled deliverable "
            f"shape does not match the Channel Package's deliverable_contract_v1.json "
            f"-- {reason}"
        )


class ProductionPlanPricingError(Exception):
    """
    Raised when a potentially billable operation has no authoritative
    finite price in any real price registry this module is permitted to
    read from. Never silently defaults to an invented or estimated number
    -- see this module's own "Authoritative price sources" docstring.
    """
    def __init__(self, episode_id: str, category: str, identifier: str) -> None:
        self.episode_id = episode_id
        self.category = category
        self.identifier = identifier
        super().__init__(
            f"Episode {episode_id!r}: no authoritative price registered for "
            f"{category} {identifier!r} -- refusing to plan with an invented "
            f"or estimated price. Register a real price in the authoritative "
            f"source for this category before this operation can be planned."
        )


class ProductionPlanCoverageError(Exception):
    """
    Raised by validate_plan_coverage() when a beat or deliverable cannot
    mathematically satisfy hypogeum.agents.coverage_check's own coverage
    formula even counting every declared contingency asset. Fails closed
    BEFORE Finance authorization and before any paid provider call.

    Attributes:
        episode_id     — the episode whose plan was rejected.
        failing_beats  — [{"beat_id", "deliverable_id", "narration_duration_seconds",
                           "max_possible_coverage_seconds", "deficit_seconds"}, ...]
    """
    def __init__(self, episode_id: str, failing_beats: list) -> None:
        self.episode_id = episode_id
        self.failing_beats = failing_beats
        beat_ids = [b["beat_id"] for b in failing_beats]
        super().__init__(
            f"Episode {episode_id!r}: Production Plan rejected before Finance "
            f"authorization -- {len(failing_beats)} beat(s) cannot satisfy "
            f"coverage even with every declared contingency asset: {beat_ids}. "
            f"No paid provider call may proceed."
        )


class ProductionPlanMissingError(Exception):
    """
    Raised by a resume/legacy caller (hypogeum.agents.visual_package_director.
    run_visual_package_director_from_plan(), scripts/recover_scenes.py, or
    any other path that skips fresh Phase-1 planning) when no compiled
    Production Plan exists on disk for this episode. A checkpoint or
    legacy episode from before this correction has no plan to load and
    must fail closed with a clear migration requirement -- it may not
    silently proceed to MUAPI generation ungoverned by any plan, and this
    module never compiles a substitute plan on a resume caller's behalf
    (compilation requires the real script_package/narration_package inputs
    a resume path does not have).
    """
    def __init__(self, episode_id: str) -> None:
        self.episode_id = episode_id
        super().__init__(
            f"Episode {episode_id!r}: no compiled Production Plan found on disk. "
            f"This resume/legacy path cannot proceed to any paid provider call "
            f"without one -- re-run this episode from Phase 1 (fresh VPD "
            f"planning) so a Production Plan is compiled, or migrate this "
            f"checkpoint by compiling and persisting a plan for it manually "
            f"before resuming."
        )


class ProductionPlanImmutabilityError(Exception):
    """
    Raised by persist_production_plan() when a caller attempts to persist
    a Production Plan for an episode that already has a DIFFERENT plan on
    disk (different content_hash). A compiled, authorized plan is
    immutable for the life of its episode.
    """
    def __init__(self, episode_id: str, existing_hash: str, new_hash: str) -> None:
        self.episode_id = episode_id
        self.existing_hash = existing_hash
        self.new_hash = new_hash
        super().__init__(
            f"Episode {episode_id!r}: a Production Plan already exists with "
            f"content_hash={existing_hash!r}, which differs from the newly "
            f"compiled plan's content_hash={new_hash!r}. Production Plans are "
            f"immutable -- refusing to overwrite."
        )


class ProductionPlanIntegrityError(Exception):
    """
    Raised by load_production_plan() when a persisted Production Plan file
    fails integrity verification and must never be loaded:

      - the stored content_hash is missing or malformed (not 64 hex chars), or
      - the canonical content hash RECOMPUTED from the loaded payload does
        not equal the stored content_hash -- i.e. the file's contents
        (assets, prompts, fingerprints, models, prices, totals) changed
        after persistence.

    Fail-closed by design: an unverifiable plan grants no authorization,
    so it is rejected rather than silently accepted. Recovery requires
    recompiling and re-persisting a fresh plan for the episode through
    the normal Phase-1 path (persist_production_plan() refuses to
    overwrite a differing existing file, so the tampered file must be
    removed by an operator first).

    This recomputed-hash comparison detects accidental or uncoordinated
    modification of the persisted file; it does NOT provide cryptographic
    authenticity -- content_hash is an unkeyed digest, so an actor able
    to rewrite the payload can recompute it, and malicious-tamper
    resistance requires the separate signed grant/approval binding
    ceremony (hypogeum.authorization_authority), not this check.
    """
    def __init__(self, episode_id: str, plan_path: Path, reason: str) -> None:
        self.episode_id = episode_id
        self.plan_path = plan_path
        self.reason = reason
        super().__init__(
            f"Episode {episode_id!r}: persisted Production Plan at {plan_path} "
            f"failed integrity verification -- {reason}"
        )


# ---------------------------------------------------------------------------
# Prompt / shot identity
# ---------------------------------------------------------------------------

def normalize_prompt(prompt: str) -> str:
    """Canonical prompt normalization shared by every prompt-identity check
    in the Factory (this module, clip_repair_agent's duplicate-prompt
    guard, and the forensic audit tooling). Never reimplement differently."""
    if not prompt:
        return ""
    return re.sub(r"\s+", " ", prompt.strip().lower())


def prompt_fingerprint(prompt: str) -> str:
    """Deterministic SHA-256 fingerprint of a normalized prompt."""
    return hashlib.sha256(normalize_prompt(prompt).encode("utf-8")).hexdigest()


def shot_structured_signature(shot: dict) -> str:
    """
    Deterministic structural signature for one planned shot, independent
    of its rendered prompt text — reuses hypogeum.agents.cinematic_director.
    shot_diversity_key() rather than a second, possibly-divergent notion
    of shot identity.
    """
    from hypogeum.agents.cinematic_director import shot_diversity_key  # noqa: PLC0415
    key = shot_diversity_key(shot)
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Channel Package deliverable contract — authoritative, never inferred
# ---------------------------------------------------------------------------

def load_deliverable_contract(channel_id: str, *, packages_root: Optional[Path] = None) -> dict:
    """
    Reads channel_packages/{channel_id}/deliverable_contract_v1.json fresh
    from disk (never cached) and verifies its sha256 against the
    'deliverable_contract' artifact entry registered in that channel's own
    channel_package_manifest.json — mirrors
    hypogeum.channel_config.load_platform_distribution_policy()'s exact
    fail-closed discipline. channel_id locates the contract; it is never
    itself the contract.

    Raises ChannelPackagePolicyDriftError (never returns a partial or
    stale result) if the manifest, the artifact file, or the checksum
    match fails in any way.
    """
    from hypogeum.workflow_executor import (  # noqa: PLC0415
        channel_package_dir,
        load_channel_package_manifest,
    )

    try:
        manifest = load_channel_package_manifest(channel_id, packages_root=packages_root)
    except Exception as exc:
        raise ChannelPackagePolicyDriftError(
            f"channel_package_manifest.json unreadable for channel_id={channel_id!r}: {exc}"
        ) from exc

    entry = next(
        (a for a in manifest.get("artifacts", []) if a.get("role") == "deliverable_contract"),
        None,
    )
    if entry is None:
        raise ChannelPackagePolicyDriftError(
            f"no 'deliverable_contract' artifact entry registered in "
            f"channel_package_manifest.json for channel_id={channel_id!r}"
        )

    artifact_path = channel_package_dir(channel_id, packages_root=packages_root) / entry["path"]
    try:
        raw = artifact_path.read_bytes()
    except OSError as exc:
        raise ChannelPackagePolicyDriftError(
            f"deliverable_contract artifact unreadable at {artifact_path}: {exc}"
        ) from exc

    live_checksum = hashlib.sha256(raw).hexdigest()
    expected_checksum = entry.get("sha256")
    if live_checksum != expected_checksum:
        raise ChannelPackagePolicyDriftError(
            f"live deliverable_contract checksum {live_checksum!r} for "
            f"channel_id={channel_id!r} no longer matches the checksum "
            f"{expected_checksum!r} registered in channel_package_manifest.json"
        )

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChannelPackagePolicyDriftError(
            f"deliverable_contract artifact at {artifact_path} is not valid JSON: {exc}"
        ) from exc

    if "deliverables" not in doc:
        raise ChannelPackagePolicyDriftError(
            f"deliverable_contract artifact at {artifact_path} has no 'deliverables' key"
        )
    return doc


def _compiled_deliverable_shape(script_package: dict) -> dict:
    """
    {deliverable_id: {"kind", "derived_from"}} derived from the episode's
    real script_package — used ONLY to validate against the Channel
    Package's own declared contract (validate_deliverable_shape()), never
    as the source of truth for what SHOULD exist.
    """
    shape: dict = {MAIN_DELIVERABLE_ID: {"kind": "main_episode", "derived_from": None}}
    for short in script_package.get("shorts_scripts", []):
        short_id = short.get("short_id", "")
        if short_id:
            shape[short_id] = {"kind": "derived_short", "derived_from": MAIN_DELIVERABLE_ID}
    return shape


def validate_deliverable_shape(
    contract: dict, script_package: dict, *, channel_id: str, episode_id: str,
    short_native: bool = False,
) -> None:
    """
    Validates the episode's real, compiled script_package against the
    Channel Package's own deliverable_contract — never the reverse. Raises
    DeliverableContractViolationError on any mismatch: a different
    deliverable count, a different deliverable_id set, a Short not
    declared as derived_from the main episode when the contract requires
    it, or any standalone Short when allow_standalone_shorts is false.

    short_native: when True (Config.SHORT_NATIVE_MODE — a shorts-only
    channel, see Config.apply_short_native_preset()), the run produces
    exactly ONE vertical deliverable that IS the main_episode, with no
    long-form arc and no derived Shorts. The on-disk contract's derived-
    Short entries are then advisory-only: the compiled shape is required
    to be exactly {main_episode} and only that deliverable's own kind is
    checked. This keeps a channel's cadence (7 shorts/wk, 2 long + 3
    shorts, …) a pure dashboard setting — flipping it must never require
    re-checksumming deliverable_contract_v1.json or re-signing the
    Channel Operating Grant bound to the package identity.
    """
    declared = {d["deliverable_id"]: d for d in contract.get("deliverables", [])}
    compiled = _compiled_deliverable_shape(script_package)

    if short_native:
        if set(compiled.keys()) != {MAIN_DELIVERABLE_ID}:
            raise DeliverableContractViolationError(
                channel_id, episode_id,
                f"short-native run must compile exactly [{MAIN_DELIVERABLE_ID!r}], "
                f"got {sorted(compiled.keys())}",
            )
        decl_main = declared.get(MAIN_DELIVERABLE_ID)
        if decl_main is not None and decl_main.get("kind") != compiled[MAIN_DELIVERABLE_ID].get("kind"):
            raise DeliverableContractViolationError(
                channel_id, episode_id,
                f"deliverable {MAIN_DELIVERABLE_ID!r}: declared kind "
                f"{decl_main.get('kind')!r} != compiled kind "
                f"{compiled[MAIN_DELIVERABLE_ID].get('kind')!r}",
            )
        return

    if set(declared.keys()) != set(compiled.keys()):
        raise DeliverableContractViolationError(
            channel_id, episode_id,
            f"declared deliverable_ids {sorted(declared.keys())} != "
            f"compiled deliverable_ids {sorted(compiled.keys())}",
        )

    allow_standalone = bool(contract.get("allow_standalone_shorts", False))
    for deliverable_id, decl in declared.items():
        comp = compiled[deliverable_id]
        if decl.get("kind") != comp.get("kind"):
            raise DeliverableContractViolationError(
                channel_id, episode_id,
                f"deliverable {deliverable_id!r}: declared kind {decl.get('kind')!r} "
                f"!= compiled kind {comp.get('kind')!r}",
            )
        if decl.get("kind") == "derived_short":
            if not allow_standalone and comp.get("derived_from") != decl.get("derived_from"):
                raise DeliverableContractViolationError(
                    channel_id, episode_id,
                    f"Short {deliverable_id!r} is not derived from "
                    f"{decl.get('derived_from')!r} as the contract requires, and "
                    f"allow_standalone_shorts is false",
                )


# ---------------------------------------------------------------------------
# Authoritative pricing — never an independently invented constant
# ---------------------------------------------------------------------------

def resolve_video_price(
    model: str, *, episode_id: str, channel_price: Optional[float] = None,
) -> float:
    """
    hypogeum.muapi.MODEL_CREDIT_COSTS is the SAME table
    MUAPIBudgetGuard.check_and_record_attempt() already uses for real
    pre-submission budget reservation. No silent fallback (unlike that
    guard's own _DEFAULT_UNKNOWN_MODEL_COST safety net) -- planning must
    know the real price or refuse to plan.

    `channel_price` is the ONE explicit exception: a model adopted per
    channel via the Video Model bake-off carries a price captured from the
    live MUAPI catalogue at adoption time, persisted on the ModelProfile.
    That is an authoritative provider number, not an estimate, so it is
    accepted for a `model` that is not in MODEL_CREDIT_COSTS. A model that
    is in the table always uses the table value.
    """
    from hypogeum.muapi import MODEL_CREDIT_COSTS  # noqa: PLC0415
    price = MODEL_CREDIT_COSTS.get(model)
    if price is None:
        if channel_price is not None and float(channel_price) > 0:
            return float(channel_price)
        raise ProductionPlanPricingError(episode_id, "video_generation_model", model)
    return float(price)


def resolve_enhancement_price(*, episode_id: str) -> float:
    """
    hypogeum.providers.muapi_video_upscaler.UPSCALE_CEILING_USD -- the
    exact Decimal already passed as max_cost_per_call to the real
    Generation Execution Envelope authorization for every real enhancement
    call (hypogeum.providers.muapi_video_upscaler.MuapiVideoUpscalerProvider.
    enhance()). Not this module's own number.
    """
    from hypogeum.providers.muapi_video_upscaler import UPSCALE_CEILING_USD  # noqa: PLC0415
    return float(UPSCALE_CEILING_USD)


def resolve_thumbnail_price(model: str, *, episode_id: str) -> float:
    """Same authoritative table as resolve_video_price() -- thumbnail
    generation is priced from the SAME MODEL_CREDIT_COSTS entries real
    thumbnail budget-guard calls already use."""
    return resolve_video_price(model, episode_id=episode_id)


def resolve_narration_price_per_char(*, episode_id: str) -> float:
    """hypogeum.elevenlabs.TTS_USD_PER_CHAR -- the same per-character
    constant already used for both real pre-call max_cost reservation and
    real post-call cost_usd ledger recording of narration."""
    from hypogeum.elevenlabs import TTS_USD_PER_CHAR  # noqa: PLC0415
    return float(TTS_USD_PER_CHAR)


def resolve_music_price_per_second(*, episode_id: str) -> float:
    """hypogeum.elevenlabs.MUSIC_USD_PER_SEC -- same dual real usage as
    resolve_narration_price_per_char()."""
    from hypogeum.elevenlabs import MUSIC_USD_PER_SEC  # noqa: PLC0415
    return float(MUSIC_USD_PER_SEC)


def resolve_thumbnail_attempts_per_candidate(*, episode_id: str) -> int:
    """hypogeum.agents.thumbnail_generation_agent._MAX_ATTEMPTS_PER_CANDIDATE
    -- the real per-candidate retry bound for the non-unified ("first
    success wins") thumbnail generation path (Shorts, and the legacy
    single-candidate main fallback). The dominant main-episode path
    (unified_integrated, typography_integration="embedded") makes exactly
    one attempt per candidate with no retry loop at all -- see
    THUMBNAIL_MAIN_CANDIDATE_COUNT's own docstring note above."""
    from hypogeum.agents.thumbnail_generation_agent import (  # noqa: PLC0415
        _MAX_ATTEMPTS_PER_CANDIDATE,
    )
    return int(_MAX_ATTEMPTS_PER_CANDIDATE)


def resolve_max_chargeable_attempts(config: Config, *, episode_id: str) -> int:
    """
    hypogeum.config.Config.MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE -- the real,
    already-enforced bound MUAPIBudgetGuard.check_and_record_attempt()
    uses to hard-stop repeated paid attempts at one scene_id. "0" means
    "unlimited" in that guard's own semantics; a Production Plan cannot
    form a MAXIMUM bounded cost against an unlimited attempt count, so
    this is a rejected plan (ProductionPlanPricingError), never an
    infinite or silently-assumed-small number.
    """
    value = int(getattr(config, "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", 0))
    if value <= 0:
        raise ProductionPlanPricingError(
            episode_id, "max_chargeable_attempts_bound",
            f"MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE={value!r} (0/unlimited)",
        )
    return value


def resolve_canonical_clip_duration(config: Config) -> float:
    """
    The requested clip duration a fresh generation request will receive
    (hypogeum.muapi.get_requested_clip_duration_seconds()) -- a BEST-EFFORT
    planning input, NOT a claimed provider guarantee. See this module's own
    "Provider clip duration" docstring section above for the direct trace
    that disproved the earlier "guaranteed" framing: no post-download
    duration validation exists in this codebase today
    (hypogeum.muapi._validate_video_file() explicitly defers it), so there
    is no authoritative minimum-usable-duration distinct from the
    requested value to use instead. Planned-mode coverage
    (validate_plan_coverage()) therefore carries the same honesty
    limitation; the real safety net is the post-generation, real-ffprobe
    "actual mode" coverage check, which must remain a genuine pre-Assembly
    gate.
    """
    from hypogeum.muapi import get_requested_clip_duration_seconds  # noqa: PLC0415
    return get_requested_clip_duration_seconds(config)


# ---------------------------------------------------------------------------
# Plan data model
# ---------------------------------------------------------------------------

@dataclass
class PlannedAsset:
    deliverable_id: str
    beat_id: str
    shot_id: str
    asset_id: str  # canonical -- hypogeum.muapi._compute_asset_id(episode_id, scene_id, short_id)
    scene_id: str  # the real scene_id submit_scene()/the ledger will use, e.g. "s01" or "s01__extra1"
    short_id: Optional[str]
    role: str  # ROLE_REQUIRED | ROLE_CONTINGENCY
    prompt: str
    prompt_fingerprint: str
    shot_signature: str
    aspect_ratio: str
    requested_duration_seconds: float
    conservative_usable_duration_seconds: float
    provider: str
    model: str
    unit_price_usd: float
    enhancement_price_usd: float
    max_chargeable_attempts: int


@dataclass
class ProductionPlan:
    schema_version: str
    channel_id: str
    episode_id: str
    deliverable_contract_id: str
    deliverable_ids: list  # e.g. ["main_episode", "short_001", "short_002", "short_003"]
    assets: list  # list[PlannedAsset]
    thumbnail_main_candidate_count: int
    thumbnail_short_candidate_cap: int
    thumbnail_attempts_per_candidate: int
    thumbnail_unit_price_usd: float
    thumbnail_cost_normal_usd: float
    thumbnail_cost_maximum_usd: float
    narration_char_count: int
    narration_price_per_char_usd: float
    narration_cost_usd: float
    narration_is_realized: bool  # True: Audio Agent already ran; this is a real, already-incurred cost
    music_section_count: int
    music_seconds_per_section: float
    music_price_per_second_usd: float
    music_cost_usd: float
    planning_call_estimate_usd: float
    enhancement_cost_maximum_usd: float
    normal_expected_cost_usd: float
    maximum_bounded_cost_usd: float
    content_hash: str
    compiled_at: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["assets"] = [asdict(a) for a in self.assets]
        return d

    @staticmethod
    def from_dict(d: dict) -> "ProductionPlan":
        assets = [PlannedAsset(**a) for a in d["assets"]]
        kwargs = {k: v for k, v in d.items() if k != "assets"}
        return ProductionPlan(assets=assets, **kwargs)

    def asset_by_id(self, asset_id: str) -> Optional[PlannedAsset]:
        """The single lookup the provider-submission boundary
        (hypogeum.muapi._submit_scene_via_gate()) uses -- no separate
        registry class. Returns None for an undeclared asset_id."""
        for a in self.assets:
            if a.asset_id == asset_id:
                return a
        return None


def _content_hash(plan_kwargs: dict, assets: list) -> str:
    """
    Deterministic content hash over everything that defines WHAT is
    authorized — excludes compiled_at (a timestamp would make every
    compile of identical inputs non-reproducible).
    """
    canonical = {k: v for k, v in plan_kwargs.items() if k not in ("compiled_at", "content_hash")}
    canonical["assets"] = sorted(
        [dict(asdict(a)) for a in assets],
        key=lambda a: (a["deliverable_id"], a["beat_id"], a["asset_id"]),
    )
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def payload_content_hash(payload: dict) -> str:
    """
    Recompute the canonical content hash from a SERIALIZED plan payload —
    the exact dict persist_production_plan() wrote to disk — using the
    SAME _content_hash() computation compile_production_plan() used at
    persist time. No serialization semantics are reinterpreted here: the
    payload's non-asset fields become plan_kwargs verbatim, and hashing
    proceeds identically from there.

    The "assets" field is required and must be a list of asset objects
    (mirroring ProductionPlan.from_dict()'s own strictness): a missing,
    null, mapping, string, or otherwise non-list "assets" value raises
    KeyError/TypeError so callers treat it as malformed instead of
    silently canonicalizing it to an empty list. Each list element is
    reconstructed as a PlannedAsset, raising TypeError/KeyError on any
    missing or unexpected field.

    Any mutation of the persisted file — an edited asset, prompt,
    fingerprint, model, price, attempt bound, or cost total — changes
    the returned value relative to the stored content_hash.
    """
    raw_assets = payload["assets"]  # KeyError if absent -- same strictness as from_dict()
    if not isinstance(raw_assets, list):
        raise TypeError(
            f'"assets" must be a list of asset objects, got {type(raw_assets).__name__}'
        )
    assets = [PlannedAsset(**a) for a in raw_assets]
    plan_kwargs = {k: v for k, v in payload.items() if k != "assets"}
    return _content_hash(plan_kwargs, assets)


def _beat_deliverable_map(script_package: dict) -> dict:
    """{beat_id: deliverable_id} derived from the episode's own real
    script_package -- used only after validate_deliverable_shape() has
    already proven this shape matches the Channel Package's contract."""
    mapping: dict = {}
    for section in script_package.get("sections", []):
        for beat in section.get("narration_beats", []):
            mapping[beat["beat_id"]] = MAIN_DELIVERABLE_ID
    for short in script_package.get("shorts_scripts", []):
        short_id = short.get("short_id", "")
        for beat in short.get("narration_beats", []):
            mapping[beat["beat_id"]] = short_id
    return mapping


def compile_production_plan(
    *,
    script_package: dict,
    narration_package: dict,
    visual_package: dict,
    channel_id: str,
    episode_id: str,
    config: Config,
    packages_root: Optional[Path] = None,
) -> ProductionPlan:
    """
    Compile the one immutable, cost-complete Production Plan for this
    episode from the REAL, post-Phase-1 visual_package (each scene's own
    real scene_id, real production_spec.provider_routing tier/prompt, and
    its own attached cinematic_coverage_plan for contingency shots — see
    module docstring). Raises DeliverableContractViolationError before
    compiling any asset if the real script_package does not match the
    Channel Package's deliverable contract, and ProductionPlanPricingError
    if any category has no authoritative finite price.
    """
    from hypogeum.muapi import _compute_asset_id, _resolve_scene_model  # noqa: PLC0415
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt  # noqa: PLC0415

    # A channel that adopted a video model via the Video Model bake-off — the
    # price was captured from the live MUAPI catalogue at adoption, so planning
    # can price a model that is not in muapi.MODEL_CREDIT_COSTS.
    try:
        from hypogeum.channel_config import resolve_channel_video_model  # noqa: PLC0415
        _channel_video_model, _channel_video_price = resolve_channel_video_model(channel_id)
    except Exception:  # noqa: BLE001
        _channel_video_model, _channel_video_price = None, None

    contract = load_deliverable_contract(channel_id, packages_root=packages_root)
    validate_deliverable_shape(
        contract, script_package, channel_id=channel_id, episode_id=episode_id,
        short_native=bool(getattr(config, "SHORT_NATIVE_MODE", False)),
    )

    requested_duration = resolve_canonical_clip_duration(config)
    max_chargeable_attempts = resolve_max_chargeable_attempts(config, episode_id=episode_id)
    # Source enhancement is a bounded but OPTIONAL provider operation. A
    # channel budget policy (hypogeum.channel_budget_policy, ADR-016 §5) may
    # deterministically suppress it for a budget-constrained autonomous
    # episode when the delivery contract is already met by the source
    # material -- signalled via config.SOURCE_ENHANCEMENT_ENABLED == False.
    # Default (attribute absent or truthy): priced exactly as before.
    if getattr(config, "SOURCE_ENHANCEMENT_ENABLED", True) is False:
        enhancement_price = 0.0
    else:
        enhancement_price = resolve_enhancement_price(episode_id=episode_id)

    thumbnail_model = getattr(config, "THUMBNAIL_IMAGE_MODEL", None)
    if not thumbnail_model:
        raise ProductionPlanPricingError(episode_id, "thumbnail_model", "THUMBNAIL_IMAGE_MODEL")
    thumbnail_unit_price = resolve_thumbnail_price(thumbnail_model, episode_id=episode_id)
    thumbnail_attempts_per_candidate = resolve_thumbnail_attempts_per_candidate(episode_id=episode_id)
    thumbnail_short_cap = int(getattr(config, "THUMBNAIL_SHORT_CANDIDATE_CAP", 3))

    beat_deliverable = _beat_deliverable_map(script_package)
    deliverable_ids = sorted(set(beat_deliverable.values()), key=lambda d: (d != MAIN_DELIVERABLE_ID, d))

    assets: list = []
    for scene in visual_package.get("scenes", []):
        beat_id = scene.get("narration_reference", "")
        deliverable_id = beat_deliverable.get(beat_id, MAIN_DELIVERABLE_ID)
        scene_id = scene.get("scene_id", "")
        short_id = scene.get("short_id")
        prod_spec = scene.get("production_spec", {})
        routing = prod_spec.get("provider_routing", {})
        reference_image_url = prod_spec.get("reference_image_url")
        tier = int(routing.get("primary_tier") or getattr(config, "MUAPI_DEFAULT_TIER", 2))
        model = _resolve_scene_model(tier, reference_image_url, config, channel_id)
        _cp = _channel_video_price if model == _channel_video_model else None
        unit_price = resolve_video_price(model, episode_id=episode_id, channel_price=_cp)
        aspect_ratio = prod_spec.get("aspect_ratio", "16:9")
        # The declared prompt is the FULLY ASSEMBLED provider payload text
        # (hypogeum.video_prompt_writer.assemble_video_scene_prompt()),
        # not the raw production_spec.primary_prompt -- submit_scene()
        # itself always composes the raw text through this same,
        # deterministic function before ever calling
        # _submit_scene_via_gate(), so the plan's declared fingerprint
        # must be computed identically or every real submission would
        # mismatch its own declared plan.
        raw_primary_prompt = routing.get("primary_prompt", "") or ""
        primary_prompt = assemble_video_scene_prompt(raw_primary_prompt)

        # Required asset — this scene's own real primary submission.
        assets.append(PlannedAsset(
            deliverable_id=deliverable_id, beat_id=beat_id, shot_id=f"{beat_id}__primary",
            asset_id=_compute_asset_id(episode_id, scene_id, short_id),
            scene_id=scene_id, short_id=short_id,
            role=ROLE_REQUIRED, prompt=primary_prompt, prompt_fingerprint=prompt_fingerprint(primary_prompt),
            shot_signature="", aspect_ratio=aspect_ratio,
            requested_duration_seconds=requested_duration,
            conservative_usable_duration_seconds=requested_duration,
            provider="muapi", model=model, unit_price_usd=unit_price,
            enhancement_price_usd=enhancement_price, max_chargeable_attempts=max_chargeable_attempts,
        ))

        # Contingency assets — shots beyond order 1 in THIS scene's own
        # attached cinematic_coverage_plan (Cinematic Director's batched,
        # coverage-sufficient shot list for this beat — Commit A). Priced
        # at the SAME per-scene routing tier/model as this beat's own
        # primary: contingency shots carry no independent routing anywhere
        # upstream, and clip_repair_agent.py's own real generation call
        # reuses the scene's own tier for exactly this reason.
        coverage_plan = scene.get("cinematic_coverage_plan") or {}
        planned_shots = sorted(coverage_plan.get("shots", []), key=lambda s: s.get("shot_order", 0))
        for shot in planned_shots:
            order = shot.get("shot_order", 1)
            if order <= 1:
                continue
            # clip_repair_agent.py passes this shot's own already-rendered
            # primary_prompt into submit_scene()'s scene_spec, which then
            # runs it through the SAME assemble_video_scene_prompt() call
            # as any other scene -- see the required-asset note above.
            raw_shot_prompt = (shot.get("primary_prompt") or "").strip()
            if not raw_shot_prompt:
                # A contingency shot with no rendered primary_prompt cannot
                # be generated: clip_repair_agent skips exactly this shot
                # ("planned shot has no rendered primary_prompt"), so it must
                # not be a priced PlannedAsset here either -- and feeding ""
                # to assemble_video_scene_prompt() raises. VPD's
                # render_coverage_plan_shot_prompts() is meant to have
                # populated every shot before this call; if one is still
                # bare, the plan simply carries no contingency asset for it.
                continue
            shot_prompt = assemble_video_scene_prompt(raw_shot_prompt)
            extra_scene_id = f"{scene_id}__extra{order - 1}"
            assets.append(PlannedAsset(
                deliverable_id=deliverable_id, beat_id=beat_id,
                shot_id=shot.get("shot_id", f"{beat_id}__shot{order}"),
                asset_id=_compute_asset_id(episode_id, extra_scene_id, short_id),
                scene_id=extra_scene_id, short_id=short_id,
                role=ROLE_CONTINGENCY, prompt=shot_prompt, prompt_fingerprint=prompt_fingerprint(shot_prompt),
                shot_signature=shot_structured_signature(shot), aspect_ratio=aspect_ratio,
                requested_duration_seconds=requested_duration,
                conservative_usable_duration_seconds=requested_duration,
                provider="muapi", model=model, unit_price_usd=unit_price,
                enhancement_price_usd=enhancement_price, max_chargeable_attempts=max_chargeable_attempts,
            ))

    # ------------------------------------------------------------------
    # Video generation — its own bounded operation.
    # ------------------------------------------------------------------
    video_required_normal = sum(a.unit_price_usd for a in assets if a.role == ROLE_REQUIRED)
    video_required_max = sum(
        a.unit_price_usd * a.max_chargeable_attempts for a in assets if a.role == ROLE_REQUIRED
    )
    video_contingency_max = sum(
        a.unit_price_usd * a.max_chargeable_attempts for a in assets if a.role == ROLE_CONTINGENCY
    )

    # ------------------------------------------------------------------
    # Source enhancement — its own bounded operation, NEVER multiplied by
    # video's own max_chargeable_attempts (a structurally separate provider
    # call with its own, currently undocumented, retry policy). See module
    # docstring's "Authoritative price sources" for why this is 0 in
    # normal and one attempt per declared asset in maximum.
    # ------------------------------------------------------------------
    enhancement_cost_maximum = sum(a.enhancement_price_usd for a in assets)

    # ------------------------------------------------------------------
    # Thumbnails — main episode is a fixed, schema-enforced 3 candidates
    # at 1 attempt each (both normal and maximum, in the dominant
    # unified_integrated path). Shorts have no schema-enforced upper
    # bound; normal assumes the typical case (each Short's first-ranked
    # candidate succeeds on its first attempt — 1 generation call per
    # Short), maximum bounds every Short at
    # thumbnail_short_candidate_cap candidates ×
    # thumbnail_attempts_per_candidate attempts each.
    # ------------------------------------------------------------------
    short_deliverable_count = sum(1 for d in deliverable_ids if d != MAIN_DELIVERABLE_ID)
    thumbnail_cost_normal = (
        THUMBNAIL_MAIN_CANDIDATE_COUNT * thumbnail_unit_price
        + short_deliverable_count * 1 * thumbnail_unit_price
    )
    thumbnail_cost_maximum = (
        THUMBNAIL_MAIN_CANDIDATE_COUNT * thumbnail_unit_price
        + short_deliverable_count * thumbnail_short_cap * thumbnail_attempts_per_candidate * thumbnail_unit_price
    )

    # ------------------------------------------------------------------
    # Narration — real, already-incurred cost when narration_package has
    # real segments (Audio Agent already ran); 0 otherwise.
    # character_cost is a raw character COUNT (confirmed against
    # hypogeum/elevenlabs.py's own producer/tests — see module docstring).
    # ------------------------------------------------------------------
    narration_segments = narration_package.get("narration_segments", []) if narration_package else []
    narration_char_count = sum(int(seg.get("character_cost", 0) or 0) for seg in narration_segments)
    narration_price_per_char = resolve_narration_price_per_char(episode_id=episode_id)
    narration_cost = round(narration_char_count * narration_price_per_char, 6)
    narration_is_realized = bool(narration_segments)

    # ------------------------------------------------------------------
    # Music — genuine forward estimate; the real timeline does not exist
    # yet (Music Agent runs during Assembly, after this plan compiles).
    # ------------------------------------------------------------------
    music_section_count = len(script_package.get("sections", [])) + len(script_package.get("shorts_scripts", []))
    music_seconds_per_section = float(getattr(config, "ELEVENLABS_MUSIC_DEFAULT_DURATION", 120))
    music_price_per_second = resolve_music_price_per_second(episode_id=episode_id)
    music_cost = round(music_section_count * music_seconds_per_section * music_price_per_second, 6)

    planning_call_estimate = PLANNING_CALL_ESTIMATE_USD

    normal_expected = (
        video_required_normal + thumbnail_cost_normal + narration_cost + music_cost + planning_call_estimate
    )
    maximum_bounded = (
        video_required_max + video_contingency_max + enhancement_cost_maximum
        + thumbnail_cost_maximum + narration_cost + music_cost + planning_call_estimate
    )

    plan_kwargs = dict(
        schema_version=PLAN_SCHEMA_VERSION,
        channel_id=channel_id,
        episode_id=episode_id,
        deliverable_contract_id=contract.get("deliverable_contract_id", ""),
        deliverable_ids=deliverable_ids,
        thumbnail_main_candidate_count=THUMBNAIL_MAIN_CANDIDATE_COUNT,
        thumbnail_short_candidate_cap=thumbnail_short_cap,
        thumbnail_attempts_per_candidate=thumbnail_attempts_per_candidate,
        thumbnail_unit_price_usd=thumbnail_unit_price,
        thumbnail_cost_normal_usd=round(thumbnail_cost_normal, 4),
        thumbnail_cost_maximum_usd=round(thumbnail_cost_maximum, 4),
        narration_char_count=narration_char_count,
        narration_price_per_char_usd=narration_price_per_char,
        narration_cost_usd=narration_cost,
        narration_is_realized=narration_is_realized,
        music_section_count=music_section_count,
        music_seconds_per_section=music_seconds_per_section,
        music_price_per_second_usd=music_price_per_second,
        music_cost_usd=music_cost,
        planning_call_estimate_usd=planning_call_estimate,
        enhancement_cost_maximum_usd=round(enhancement_cost_maximum, 4),
        normal_expected_cost_usd=round(normal_expected, 4),
        maximum_bounded_cost_usd=round(maximum_bounded, 4),
    )
    content_hash = _content_hash(plan_kwargs, assets)

    return ProductionPlan(
        **plan_kwargs,
        assets=assets,
        content_hash=content_hash,
        compiled_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Coverage validation (planned mode)
# ---------------------------------------------------------------------------

@dataclass
class PlanCoverageResult:
    sufficient: bool
    failing_beats: list = field(default_factory=list)
    beat_reports: list = field(default_factory=list)


def validate_plan_coverage(plan: ProductionPlan, narration_package: dict) -> PlanCoverageResult:
    """
    Canonical pre-spend coverage validation — per beat AND per deliverable,
    never aggregate episode duration alone. Calls the SAME
    hypogeum.agents.coverage_check.coverage_arithmetic() Assembly uses in
    actual mode, in "planned mode": available_duration_seconds is the sum
    of conservative_usable_duration_seconds (a best-effort planning input,
    not a claimed guarantee — see resolve_canonical_clip_duration())
    across every declared asset for that beat (required + all contingency).
    """
    durations_by_beat: dict = {}
    for seg in narration_package.get("narration_segments", []):
        durations_by_beat[seg["beat_reference"]] = float(seg.get("duration_seconds", 0.0))

    assets_by_beat: dict = {}
    for a in plan.assets:
        assets_by_beat.setdefault(a.beat_id, []).append(a)

    failing_beats: list = []
    beat_reports: list = []
    for beat_id, assets in assets_by_beat.items():
        narration_duration = durations_by_beat.get(beat_id, 0.0)
        available = sum(a.conservative_usable_duration_seconds for a in assets)
        max_editorial, deficit, sufficient = coverage_arithmetic(narration_duration, available)
        report = {
            "beat_id": beat_id,
            "deliverable_id": assets[0].deliverable_id,
            "narration_duration_seconds": narration_duration,
            "max_possible_coverage_seconds": round(max_editorial, 3),
            "deficit_seconds": round(deficit, 3),
            "sufficient": sufficient,
            "required_shot_count": sum(1 for a in assets if a.role == ROLE_REQUIRED),
            "contingency_shot_count": sum(1 for a in assets if a.role == ROLE_CONTINGENCY),
        }
        beat_reports.append(report)
        if not sufficient:
            failing_beats.append(report)

    return PlanCoverageResult(
        sufficient=not failing_beats, failing_beats=failing_beats, beat_reports=beat_reports,
    )


# ---------------------------------------------------------------------------
# Atomic, immutable persistence
# ---------------------------------------------------------------------------

def _plan_path(episode_id: str, output_dir: Path) -> Path:
    return Path(output_dir) / f"production_plan_{episode_id}.json"


def canonical_episode_dir(episode_id: str) -> Path:
    """The canonical on-disk working directory for an episode:
    <repo>/.hypogeum-assets/episodes/<episode_id>/. This is where
    persist_production_plan() writes when no explicit output_dir is given,
    and the fallback location every path-consistency check probes."""
    return (
        Path(__file__).resolve().parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )


class PlanLifecycle(str, Enum):
    """Authoritative lifecycle/path classification for a paid boundary.

    The CANONICAL episode directory (canonical_episode_dir(), resolved
    independently of any caller value) is the SOLE authority for whether a
    verified Production Plan exists and where it lives.

    PRE_PLAN         — no Production Plan file exists at the canonical
                       episode directory. A boundary running here is
                       genuinely pre-plan and is outside the plan-level
                       weekly reservation guarantee (that is the separate
                       pre-production closure's responsibility).
    POST_PLAN        — a Production Plan file exists at the canonical
                       directory AND the caller-supplied output_dir IS the
                       canonical directory (or output_dir is None). The
                       boundary MUST establish/replay the plan-level weekly
                       reservation.
    INCONSISTENT_PATH — a canonical Production Plan exists but the
                       caller-supplied output_dir is a DIFFERENT directory
                       (whatever it contains -- empty, a different plan,
                       or even a byte-identical same-hash copy), OR a plan
                       file exists only at a non-canonical supplied dir.
                       Closure 0 has no authoritative alias / relocation
                       record system, so a same content_hash alone is NOT
                       sufficient to accept a non-canonical directory. The
                       boundary MUST fail closed before any provider I/O.
    """

    PRE_PLAN = "pre_plan"
    POST_PLAN = "post_plan"
    INCONSISTENT_PATH = "inconsistent_path"


def classify_plan_lifecycle(
    episode_id: str, output_dir: Optional[Path] = None
) -> "PlanLifecycle":
    """
    The single authoritative pre-plan / post-plan / misdirected-path
    classifier for every paid boundary. Resolves the canonical episode
    directory (canonical_episode_dir()) INDEPENDENTLY of output_dir, so a
    caller cannot obtain a pre-plan exemption -- or substitute a copied
    plan file -- by supplying an empty or wrong directory.

    The canonical directory is the ONLY accepted plan location. A
    non-canonical supplied output_dir is INCONSISTENT_PATH regardless of
    what it contains (Closure 0 has no alias/relocation authority). Tests
    that need a tmp directory to BE canonical monkeypatch
    canonical_episode_dir() -- that legitimately relocates the authority
    for the test and is unrelated to a production alias.

    See PlanLifecycle for the exact meaning of each result.
    """
    canon = canonical_episode_dir(episode_id)
    canon_has_plan = _plan_path(episode_id, canon).exists()

    supplied_dir = canon if output_dir is None else Path(output_dir)
    try:
        same_dir = supplied_dir.resolve() == canon.resolve()
    except OSError:
        same_dir = str(supplied_dir) == str(canon)

    if not canon_has_plan:
        # No canonical plan => genuine pre-plan, UNLESS a plan file exists
        # only at a non-canonical supplied dir (a plan that is not where it
        # belongs -- a misdirected path, not an exemption).
        if not same_dir and _plan_path(episode_id, supplied_dir).exists():
            return PlanLifecycle.INCONSISTENT_PATH
        return PlanLifecycle.PRE_PLAN

    # A canonical plan exists. The canonical directory is the only
    # authoritative location: accept the supplied dir ONLY if it IS the
    # canonical dir. A same-hash copy elsewhere is still a misdirected path.
    if same_dir:
        return PlanLifecycle.POST_PLAN
    return PlanLifecycle.INCONSISTENT_PATH


def _atomic_write_text(path: Path, content: str) -> None:
    """
    Atomic write: write to a sibling temp file on the SAME filesystem,
    fsync it, os.replace() (an atomic rename on every platform this
    codebase runs on) onto the final path, then fsync the PARENT
    DIRECTORY too so the rename itself is durable (os.replace() alone only
    guarantees atomicity, not durability of the directory entry across a
    crash — the parent fsync is what makes the rename survive a crash
    immediately after it returns). A reader can never observe a
    partially-written file, and a crash mid-write leaves the original
    file (if any) or nothing — never a corrupt fragment.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Directory fsync is best-effort on platforms/filesystems that
        # don't support it (e.g. some network filesystems) — the rename
        # itself already succeeded; this only strengthens the durability
        # guarantee against a crash in the immediate aftermath.
        pass


def persist_production_plan(
    plan: ProductionPlan,
    output_dir: Path,
    *,
    supersede: bool = False,
) -> Path:
    """
    Write the plan once, keyed by its own deterministic content_hash, via
    atomic temp-file + fsync + os.replace + parent-directory fsync (never
    a direct write_text() — see _atomic_write_text()). Immutable: refuses
    to overwrite an already-persisted plan whose content_hash differs.
    Writing the identical plan again (same hash) is a no-op that returns
    the existing path unchanged.

    supersede (2026-08-26): when True and a DIFFERENT verified plan already
    exists, the old file is preserved verbatim as
    production_plan_{episode_id}.superseded_{old_hash[:12]}.json — an
    immutable, inspectable audit artifact — before the replacement is
    written. This is the ONLY sanctioned replacement path: it exists for
    the resume-time recompilation whose declared asset set legitimately
    changed (coverage-plan correction), never for silently rewriting an
    authorized plan.
    """
    path = _plan_path(plan.episode_id, Path(output_dir))
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("content_hash") == plan.content_hash:
            return path
        if not supersede:
            raise ProductionPlanImmutabilityError(plan.episode_id, existing.get("content_hash"), plan.content_hash)
        superseded_path = path.with_name(
            f"{path.stem}.superseded_{str(existing.get('content_hash'))[:12]}.json"
        )
        _atomic_write_text(superseded_path, path.read_text(encoding="utf-8"))
    _atomic_write_text(path, json.dumps(plan.to_dict(), indent=2, ensure_ascii=False))
    return path


def load_production_plan(episode_id: str, output_dir: Path) -> Optional[ProductionPlan]:
    """
    Load the persisted Production Plan for an episode, or None if none
    exists yet.

    Integrity-verified (fail-closed): before returning, the canonical
    content hash is RECOMPUTED from the loaded payload via
    payload_content_hash() and compared against the file's own stored
    content_hash. A missing or malformed stored hash, a payload that
    cannot be reconstructed, or any recomputed/stored mismatch raises
    ProductionPlanIntegrityError -- an unverifiable plan never reaches
    the Finance gate or the provider-submission boundary, because a plan
    whose contents cannot be proven identical to what was authorized
    authorizes nothing.
    """
    path = _plan_path(episode_id, Path(output_dir))
    if not path.exists():
        return None

    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProductionPlanIntegrityError(
            episode_id=episode_id, plan_path=path,
            reason=f"persisted plan is not valid UTF-8 text: {exc}",
        ) from exc
    except OSError as exc:
        raise ProductionPlanIntegrityError(
            episode_id=episode_id, plan_path=path,
            reason=f"persisted plan is unreadable: {exc}",
        ) from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ProductionPlanIntegrityError(
            episode_id=episode_id, plan_path=path,
            reason=f"persisted plan is not valid JSON: {exc}",
        ) from exc

    stored_hash = payload.get("content_hash") if isinstance(payload, dict) else None
    if not isinstance(stored_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", stored_hash):
        raise ProductionPlanIntegrityError(
            episode_id=episode_id, plan_path=path,
            reason=f"stored content_hash is missing or malformed "
                   f"({stored_hash!r}) -- refusing to load an unverifiable plan",
        )

    try:
        recomputed = payload_content_hash(payload)
    except Exception as exc:  # noqa: BLE001 -- any reconstruction failure means the payload is not verifiable
        raise ProductionPlanIntegrityError(
            episode_id=episode_id, plan_path=path,
            reason=f"payload is malformed and cannot be hash-verified: "
                   f"{type(exc).__name__}: {exc}",
        ) from exc

    if recomputed != stored_hash:
        raise ProductionPlanIntegrityError(
            episode_id=episode_id, plan_path=path,
            reason=f"recomputed content_hash {recomputed!r} does not match the "
                   f"stored content_hash {stored_hash!r} -- the plan file "
                   f"changed after persistence",
        )

    return ProductionPlan.from_dict(payload)


# ---------------------------------------------------------------------------
# Bridge to the existing manual grant-approval ceremony
# (scripts/approve_run_grant.py / hypogeum.authorization_authority) — this
# module does NOT auto-issue grants (that would defeat the private-key,
# human-confirmation security model that ceremony is deliberately built
# on). It emits the exact binding numbers an operator needs to author a
# grant spec whose limits equal Finance's own approved maximum, rather
# than an operator hand-typing max_cost_per_call/max_calls disconnected
# from what was actually compiled and authorized.
# ---------------------------------------------------------------------------

def production_plan_to_grant_binding_spec(plan: ProductionPlan) -> dict:
    """
    Grant-spec fragment for scripts/approve_run_grant.py: {"bindings":
    [...], "production_plan_content_hash": ...}. One grant "bindings"
    entry per distinct (provider, model) pair this plan declares, with
    max_calls/max_total_spend set to this plan's own MAXIMUM bounded
    numbers for that model -- never a value invented independently of
    the compiled plan. production_plan_content_hash is the plan's own
    content_hash: the offline approval ceremony requires it in the
    grant spec, signs it into the grant checksum, and every paid video
    submission re-verifies it against the verified persisted plan
    before any network call.
    """
    by_model: dict = {}
    for a in plan.assets:
        key = (a.provider, a.model)
        entry = by_model.setdefault(key, {"max_calls": 0, "max_total_spend": 0.0, "max_cost_per_call": 0.0})
        entry["max_calls"] += a.max_chargeable_attempts
        entry["max_total_spend"] += a.unit_price_usd * a.max_chargeable_attempts
        entry["max_cost_per_call"] = max(entry["max_cost_per_call"], a.unit_price_usd)

    bindings = [
        {
            "provider": provider, "model": model, "provider_operation": "video.text_to_video",
            "max_cost_per_call": f"{v['max_cost_per_call']:.4f}",
            "max_calls": v["max_calls"],
            "max_total_spend": f"{v['max_total_spend']:.4f}",
            "retry_authorized": True, "fallback_authorized": False,
        }
        for (provider, model), v in sorted(by_model.items())
    ]
    return {
        "bindings": bindings,
        "production_plan_content_hash": plan.content_hash,
    }
