"""
hypogeum/production_plan.py — Compiled Production Plan (2026-08-24).

The missing Factory invariant identified by the ep_f3619022/ep_af078f34
forensic audit: before the first paid provider call, one immutable
compiled Production Plan must enumerate every potentially billable asset
for the deliverable shape the episode's own Channel Package-derived
script_package already declares (for Hypogeum today: exactly one main
episode plus exactly three derived Shorts — see
compile_production_plan()'s own docstring for how that shape is derived,
never hardcoded here), including bounded contingency assets, unique
prompt fingerprints, required and usable durations, aspect ratios,
provider/model prices, enhancement costs, thumbnail costs, and retry
bounds. The SAME canonical coverage arithmetic Assembly uses later
(hypogeum.agents.coverage_check.coverage_arithmetic()) proves, in
"planned mode" (conservative provider-duration guarantees, before any
generation), that the plan can render successfully — the exact check that
did not exist anywhere in the pipeline before this module, and whose
absence is the root cause both ep_af078f34's forensic trace and this
module's own docstring history record.

Channel-agnostic: nothing here hardcodes Hypogeum's shape, cadence, or
cost policy. The deliverable shape is derived from the real, already-
channel-configured script_package (its sections + shorts_scripts, which
upstream Narrative Architect/Script Engine already compiled from the
Channel Package) — never a Hypogeum-specific literal in this module.

Lifecycle:
  1. compile_production_plan() — called once per episode, after Cinematic
     Director (batched, fail-closed — see hypogeum.agents.cinematic_director)
     and after Coverage Plan shot-prompt rendering, BEFORE Finance
     authorization and before any paid provider call. Enumerates one
     PlannedAsset per required shot (the beat's primary) and one
     PlannedAsset per legitimate contingency shot (every additional
     planned, distinct shot the Cinematic Coverage Plan already supplied
     for that beat — never more than the plan actually has, which is what
     keeps a contingency asset from ever being forced to duplicate an
     already-declared prompt; see hypogeum.agents.clip_repair_agent's own
     represented_shot_ids fix for the consumption-side half of this).
  2. validate_plan_coverage() — the canonical pre-spend coverage gate.
     Rejects the plan (ProductionPlanCoverageError) before Finance and
     before any paid call if, even counting every declared contingency
     asset, a beat or deliverable cannot mathematically satisfy
     hypogeum.agents.coverage_check's own formula.
  3. persist_production_plan() — writes the plan once, keyed by its own
     deterministic content_hash. Immutable: a second call for the same
     episode_id with a DIFFERENT content_hash is refused (this module
     never silently replaces an already-authorized plan); an identical
     hash is a no-op.
  4. Finance authorizes against plan.normal_expected_cost_usd /
     plan.maximum_bounded_cost_usd (hypogeum.agents.finance_agent).
  5. Downstream stages (VPD Phase 2, clip_repair_agent, Assembly) consume
     declared assets by asset_id and may update their execution status,
     but may not invent a creative asset, prompt, or identity outside
     this plan — see each of those modules' own enforcement.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR, coverage_arithmetic
from hypogeum.config import Config

PLAN_SCHEMA_VERSION = "1.0"

ROLE_REQUIRED = "required"
ROLE_CONTINGENCY = "contingency"

MAIN_DELIVERABLE_ID = "main_episode"


class ProductionPlanCoverageError(Exception):
    """
    Raised by validate_plan_coverage() when a beat or deliverable cannot
    mathematically satisfy hypogeum.agents.coverage_check's own coverage
    formula even counting every declared contingency asset. Fails closed
    BEFORE Finance authorization and before any paid provider call — this
    is the generic replacement for the class of failure that let
    ep_af078f34 spend $48 before Assembly discovered, after the fact, that
    its plan could never have rendered.

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


def normalize_prompt(prompt: str) -> str:
    """Canonical prompt normalization shared by every prompt-identity check
    in the Factory (this module, clip_repair_agent's duplicate-prompt
    guard, and the forensic audit tooling) — lowercase, whitespace-collapsed.
    Never reimplement this differently in another module."""
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
    shot_diversity_key() (the one existing canonical "what makes two shots
    the same shot" identity function) rather than re-deriving a second,
    possibly-divergent notion of shot identity here.
    """
    from hypogeum.agents.cinematic_director import shot_diversity_key  # noqa: PLC0415
    key = shot_diversity_key(shot)
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class PlannedAsset:
    deliverable_id: str
    beat_id: str
    shot_id: str
    asset_id: str
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
    enhancement_required: bool
    enhancement_price_usd: float
    retry_allowance: int


@dataclass
class ProductionPlan:
    schema_version: str
    channel_id: str
    episode_id: str
    deliverable_ids: list  # e.g. ["main_episode", "short_001", "short_002", "short_003"]
    assets: list  # list[PlannedAsset]
    thumbnail_count: int
    thumbnail_unit_price_usd: float
    normal_expected_cost_usd: float
    maximum_bounded_cost_usd: float
    content_hash: str
    compiled_at: str

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "channel_id": self.channel_id,
            "episode_id": self.episode_id,
            "deliverable_ids": list(self.deliverable_ids),
            "assets": [asdict(a) for a in self.assets],
            "thumbnail_count": self.thumbnail_count,
            "thumbnail_unit_price_usd": self.thumbnail_unit_price_usd,
            "normal_expected_cost_usd": self.normal_expected_cost_usd,
            "maximum_bounded_cost_usd": self.maximum_bounded_cost_usd,
            "content_hash": self.content_hash,
            "compiled_at": self.compiled_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "ProductionPlan":
        assets = [PlannedAsset(**a) for a in d["assets"]]
        return ProductionPlan(
            schema_version=d["schema_version"], channel_id=d["channel_id"],
            episode_id=d["episode_id"], deliverable_ids=list(d["deliverable_ids"]),
            assets=assets, thumbnail_count=d["thumbnail_count"],
            thumbnail_unit_price_usd=d["thumbnail_unit_price_usd"],
            normal_expected_cost_usd=d["normal_expected_cost_usd"],
            maximum_bounded_cost_usd=d["maximum_bounded_cost_usd"],
            content_hash=d["content_hash"], compiled_at=d["compiled_at"],
        )


def _content_hash(deliverable_ids: list, assets: list, thumbnail_count: int) -> str:
    """
    Deterministic content hash over the parts of the plan that define WHAT
    is authorized (deliverables + every asset's identity/prompt/signature/
    price) — excludes compiled_at (a timestamp would make every compile
    non-reproducible) so two compiles of the identical inputs always
    produce the identical hash.
    """
    canonical = {
        "deliverable_ids": list(deliverable_ids),
        "thumbnail_count": thumbnail_count,
        "assets": sorted(
            [
                {k: v for k, v in asdict(a).items()} for a in assets
            ],
            key=lambda a: (a["deliverable_id"], a["beat_id"], a["asset_id"]),
        ),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _beat_deliverable_map(script_package: dict) -> dict:
    """
    {beat_id: deliverable_id} derived from the episode's own real
    script_package — never a Hypogeum-specific literal. Main-episode beats
    map to MAIN_DELIVERABLE_ID; each Short's own beats map to its own
    short_id. This IS the deliverable shape (one main + however many
    Shorts the Channel Package already caused Narrative Architect/Script
    Engine to compile) — compile_production_plan() reads it from here
    rather than assuming any fixed count.
    """
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
    cinematic_coverage_plan: dict,
    channel_id: str,
    episode_id: str,
    config: Config,
) -> ProductionPlan:
    """
    Enumerate every potentially billable video-generation asset for this
    episode from its already-batched, already-coverage-diverse Cinematic
    Coverage Plan (hypogeum.agents.cinematic_director — Commit A of the
    2026-08-24 factory-level correction): one REQUIRED PlannedAsset per
    beat (its shot_order=1 primary), and one CONTINGENCY PlannedAsset per
    additional planned, distinct shot the plan already supplies for that
    beat (shot_order > 1) — never more than the plan actually has, which
    is what makes a later contingency generation attempt structurally
    unable to duplicate an already-declared prompt (see
    hypogeum.agents.clip_repair_agent's represented_shot_ids fix for the
    consumption-side half of this same invariant).

    conservative_usable_duration_seconds applies
    COVERAGE_CONSERVATIVE_DURATION_FACTOR (generic, channel-agnostic,
    default 0.95) to the provider's requested clip duration
    (hypogeum.muapi.get_requested_clip_duration_seconds()) — a real file
    may come back marginally short or fail qualification; planned-mode
    coverage validation must never be more optimistic than actual-mode
    validation will be.

    Thumbnail cost is counted once per deliverable (one thumbnail per main
    episode / Short) at THUMBNAIL_UNIT_PRICE_USD (generic; default derived
    from muapi.MODEL_CREDIT_COSTS's real gpt-image-2 price) — the exact
    cost category ep_af078f34's own Finance authorization never included.
    """
    from hypogeum.muapi import MODEL_CREDIT_COSTS, get_requested_clip_duration_seconds

    conservative_factor = max(0.0, min(1.0, getattr(config, "COVERAGE_CONSERVATIVE_DURATION_FACTOR", 0.95)))
    requested_duration = get_requested_clip_duration_seconds(config)
    conservative_duration = requested_duration * conservative_factor

    tier = getattr(config, "MUAPI_DEFAULT_TIER", 2)
    model = getattr(config, f"MUAPI_T{tier}_MODEL", "veo3.1-lite-text-to-video")
    unit_price = MODEL_CREDIT_COSTS.get(model, 2.5)

    enhancement_price = float(getattr(config, "ENHANCEMENT_UNIT_PRICE_USD", 0.09))
    thumbnail_unit_price = float(MODEL_CREDIT_COSTS.get("gpt-image-2-text-to-image", 0.09))
    retry_allowance = max(0, int(getattr(config, "COVERAGE_FILL_MAX_SLOT_RETRIES", 1)))

    beat_deliverable = _beat_deliverable_map(script_package)
    deliverable_ids = sorted(set(beat_deliverable.values()), key=lambda d: (d != MAIN_DELIVERABLE_ID, d))

    assets: list = []
    for beat_plan in cinematic_coverage_plan.get("beat_plans", []):
        beat_id = beat_plan["beat_id"]
        deliverable_id = beat_deliverable.get(beat_id, MAIN_DELIVERABLE_ID)
        aspect_ratio = "9:16" if deliverable_id != MAIN_DELIVERABLE_ID else "16:9"
        shots = sorted(beat_plan.get("shots", []), key=lambda s: s.get("shot_order", 0))
        for shot in shots:
            order = shot.get("shot_order", 1)
            role = ROLE_REQUIRED if order == 1 else ROLE_CONTINGENCY
            prompt = shot.get("primary_prompt", "") or ""
            asset_id = beat_id if order == 1 else f"{beat_id}__extra{order - 1}"
            assets.append(PlannedAsset(
                deliverable_id=deliverable_id,
                beat_id=beat_id,
                shot_id=shot.get("shot_id", f"{beat_id}__shot{order}"),
                asset_id=asset_id,
                role=role,
                prompt=prompt,
                prompt_fingerprint=prompt_fingerprint(prompt),
                shot_signature=shot_structured_signature(shot),
                aspect_ratio=aspect_ratio,
                requested_duration_seconds=requested_duration,
                conservative_usable_duration_seconds=conservative_duration,
                provider="muapi",
                model=model,
                unit_price_usd=unit_price,
                enhancement_required=True,
                enhancement_price_usd=enhancement_price,
                retry_allowance=retry_allowance,
            ))

    required_cost = sum(a.unit_price_usd + a.enhancement_price_usd for a in assets if a.role == ROLE_REQUIRED)
    contingency_cost = sum(a.unit_price_usd + a.enhancement_price_usd for a in assets if a.role == ROLE_CONTINGENCY)
    thumbnail_count = len(deliverable_ids)
    thumbnail_cost = thumbnail_count * thumbnail_unit_price

    normal_expected = required_cost + thumbnail_cost
    maximum_bounded = required_cost + contingency_cost + thumbnail_cost

    content_hash = _content_hash(deliverable_ids, assets, thumbnail_count)

    return ProductionPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        channel_id=channel_id,
        episode_id=episode_id,
        deliverable_ids=deliverable_ids,
        assets=assets,
        thumbnail_count=thumbnail_count,
        thumbnail_unit_price_usd=thumbnail_unit_price,
        normal_expected_cost_usd=round(normal_expected, 4),
        maximum_bounded_cost_usd=round(maximum_bounded, 4),
        content_hash=content_hash,
        compiled_at=datetime.now(timezone.utc).isoformat(),
    )


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
    of conservative_usable_duration_seconds across every declared asset for
    that beat (required + all contingency), never a real probed file.

    A beat passes only if required assets ALONE are sufficient, or become
    sufficient once every declared contingency asset for that beat is
    counted too (contingency existing is expected and legitimate — that is
    what it is for). A beat that cannot pass even with every declared
    contingency asset is mathematically incapable of satisfying coverage
    and must be reported as failing — the caller (validate_plan_coverage's
    own caller in the pipeline) raises ProductionPlanCoverageError for
    this, before Finance, before any paid call.
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


def _plan_path(episode_id: str, output_dir: Path) -> Path:
    return Path(output_dir) / f"production_plan_{episode_id}.json"


def persist_production_plan(plan: ProductionPlan, output_dir: Path) -> Path:
    """
    Write the plan once, keyed by its own deterministic content_hash.
    Immutable: refuses to overwrite an already-persisted plan whose
    content_hash differs (a real change in inputs must compile a fresh
    plan under deliberate operator action, never silently replace an
    already-authorized one). Writing the identical plan again (same hash)
    is a no-op that returns the existing path unchanged.
    """
    path = _plan_path(plan.episode_id, Path(output_dir))
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("content_hash") == plan.content_hash:
            return path
        raise ProductionPlanImmutabilityError(plan.episode_id, existing.get("content_hash"), plan.content_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


class ProductionPlanImmutabilityError(Exception):
    """
    Raised by persist_production_plan() when a caller attempts to persist
    a Production Plan for an episode that already has a DIFFERENT plan on
    disk (different content_hash). A compiled, authorized plan is
    immutable for the life of its episode -- a genuine need to replan
    requires deliberate operator action (removing the existing checkpoint),
    never a silent overwrite from a re-run.
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


def _raise_immutability_error(plan: ProductionPlan, existing_hash: str) -> None:
    raise ProductionPlanImmutabilityError(plan.episode_id, existing_hash, plan.content_hash)


def load_production_plan(episode_id: str, output_dir: Path) -> Optional[ProductionPlan]:
    """Load the persisted Production Plan for an episode, or None if none exists yet."""
    path = _plan_path(episode_id, Path(output_dir))
    if not path.exists():
        return None
    return ProductionPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))
