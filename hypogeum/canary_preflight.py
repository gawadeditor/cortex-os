"""
hypogeum/canary_preflight.py — Shared, zero-cost, fail-closed preflight
primitives for MUAPI video generation authorization checks.

Extracted (2026-08-14) from tools/muapi_canary_submit.py so that both the
single-shot canary CLI and tools/muapi_batch_preflight.py resolve prompts,
checksums, provenance, and model/cost exactly the same way -- one tested
implementation, never two independently-maintained copies of the same
fail-closed check.

Channel-, episode-, and provider-policy-agnostic at the function-signature
level: every function here takes plain dicts/strings/Config and returns
plain dicts/strings, or raises CanaryPreflightError. No Hypogeum-specific
term, beat/shot ID convention, or archaeological vocabulary appears in this
module -- only generic authorization-entry field names ("primary_prompt",
"primary_prompt_checksum", "prompt_provenance") already used by both
callers.

Never performs a provider request, a provider credential check that
contacts the provider, or any filesystem write.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from hypogeum.config import Config
from hypogeum.muapi import MODEL_CREDIT_COSTS, _tier_to_model


class CanaryPreflightError(Exception):
    """One fail-closed preflight check failed. Carries the exact check name
    so a caller (or a test) can assert on which gate blocked."""

    def __init__(self, check: str, reason: str):
        self.check = check
        self.reason = reason
        super().__init__(f"[{check}] {reason}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_prompt_checksum(primary_prompt: str, expected_checksum: Optional[str]) -> str:
    """Recomputes sha256 of the exact UTF-8 prompt string and compares it
    against the manifest's stored checksum. Returns the computed checksum
    on success; raises CanaryPreflightError on any mismatch or absence."""
    computed = sha256_text(primary_prompt)
    if not expected_checksum:
        raise CanaryPreflightError("prompt_checksum_present", "manifest entry has no primary_prompt_checksum")
    if computed != expected_checksum:
        raise CanaryPreflightError(
            "prompt_checksum_match",
            f"recomputed sha256 {computed} != manifest checksum {expected_checksum}",
        )
    return computed


def check_provenance(entry: dict) -> dict:
    prov = entry.get("prompt_provenance")
    if not prov or not prov.get("compiler") or not prov.get("source_shot_id"):
        raise CanaryPreflightError("prompt_provenance_present", "manifest entry has no valid prompt_provenance")
    return prov


def is_entry_active(entry: dict) -> tuple[bool, Optional[str]]:
    """Returns (is_active, inconsistency_reason). An entry is active iff
    every terminal-state field on it (if present) says "active"; absence
    of a field means "active" -- the exact default the single-shot canary
    gate (_resolve_authorization's own check) uses:
    entry.get("canary_status", "active"). If two such fields disagree,
    that is a state inconsistency a caller must not silently resolve."""
    statuses = {}
    for field in ("canary_status", "authorization_status"):
        if field in entry:
            statuses[field] = entry[field]
    values = set(statuses.values())
    if len(values) > 1:
        return False, f"conflicting state fields: {statuses}"
    if values and next(iter(values)) != "active":
        return False, None
    return True, None


def resolve_active_entries(manifest: dict) -> tuple[list, list]:
    """Splits manifest["entries"] into (active, inactive) using
    is_entry_active(). Never trusts any top-level summary field
    (active_entry_count, etc.) -- callers must always recompute from the
    entries themselves. Each returned active entry is a shallow copy
    carrying an extra "_state_inconsistency" key; callers must treat a
    non-None value as a blocker, never silently resolve it."""
    active, inactive = [], []
    for entry in manifest.get("entries", []):
        active_flag, inconsistency = is_entry_active(entry)
        entry = dict(entry)  # never mutate the caller's manifest dict
        entry["_state_inconsistency"] = inconsistency
        (active if active_flag else inactive).append(entry)
    return active, inactive


def scene_by_beat(scenes: list) -> tuple[dict, set]:
    """Maps beat_id -> scene via scene["narration_reference"], the exact
    lookup key hypogeum.agents.clip_repair_agent.fill_confirmed_missing_
    shots() uses (plan_by_beat) to find a shot's real embedded coverage
    plan. Returns (by_beat, duplicate_beats) -- a beat mapping to more
    than one scene is a structural conflict, never silently resolved by
    picking one."""
    by_beat: dict = {}
    dupes: set = set()
    for s in scenes:
        beat = s.get("narration_reference")
        if beat is None:
            continue
        if beat in by_beat:
            dupes.add(beat)
        by_beat.setdefault(beat, s)
    return by_beat, dupes


def find_planned_shot(scene: dict, shot_id: str) -> tuple[Optional[dict], int]:
    """Finds shot_id in scene["cinematic_coverage_plan"]["shots"] -- the
    only copy hypogeum.agents.clip_repair_agent ever reads, and the exact
    canonical embedded/live production source real submission resolves a
    planned shot from. Returns (shot_or_None, match_count) so a caller can
    distinguish "not found" from "found more than once"."""
    plan = scene.get("cinematic_coverage_plan") or {}
    matches = [sh for sh in plan.get("shots", []) if sh.get("shot_id") == shot_id]
    if not matches:
        return None, 0
    return matches[0], len(matches)


def resolve_model_and_cost(config: Config, tier: int, required_model: str, max_charge_usd: float) -> tuple[str, float]:
    """Resolves the model a given tier would actually receive from the
    current Config, verifies it matches required_model, and verifies its
    known per-generation credit cost does not exceed max_charge_usd.
    Returns (model, cost) on success; raises CanaryPreflightError otherwise.
    Never contacts the provider -- purely a local config/table lookup."""
    model = _tier_to_model(tier, None, config)
    if model != required_model:
        raise CanaryPreflightError(
            "model_matches_required",
            f"tier {tier} resolves to {model!r}, required {required_model!r}",
        )
    cost = MODEL_CREDIT_COSTS.get(model)
    if cost is None:
        raise CanaryPreflightError("cost_known", f"no known credit cost for model {model!r}")
    if cost > max_charge_usd:
        raise CanaryPreflightError(
            "cost_within_cap",
            f"model {model!r} costs ${cost}, exceeds allowed max ${max_charge_usd}",
        )
    return model, cost
