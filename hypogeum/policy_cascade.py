"""
hypogeum/policy_cascade.py — P3-05: Factory Policy Cascade

Factory-level governance rules take precedence over runtime config.

Actual precedence implemented (most to least authoritative):
    factory_policies  →  runtime env (FINANCE_BUDGET_CEILING_USD)

The "channel config" tier described in the P3-05 spec requires a per-channel
policy field that does not yet exist in the schema. Channel-level overrides
are deferred to P3-05b. See TODO below.

Public API:
    get_effective_policy(channel_id=None, factory_id=None, db_path=None) -> EffectivePolicy
    get_effective_budget_ceiling(env_ceiling=0.0, channel_id=None, factory_id=None,
                                 db_path=None) -> float
    validate_factory_policy_constraints(channel_config, factory_id=None,
                                        db_path=None) -> list[str]

Exceptions:
    PolicyViolationError — violations attribute lists human-readable strings

Factory policies JSON shape (stored in factories.factory_policies):
    {
      "monthly_ceiling_usd":               300,  # 0 = no factory-level ceiling
      "episodes_per_month":                4,    # denominator for per-episode ceiling
      "blocked_model_slugs":               [],
      "allowed_model_slugs":               null, # null = all slugs allowed
      "require_human_review_for":          [],
      "budget_increase_requires_approval": true,
      "model_swap_requires_approval":      true
    }

TODO P3-05b — channel_config wiring (deferred):
    create_channel_profile() and update_channel_profile() in channel_config.py
    should call validate_factory_policy_constraints() and raise PolicyViolationError
    on violation. This requires those functions to either (a) receive the resolved
    ModelProfile slugs directly, or (b) load the model profile by ID internally.
    The current channel_config write path accepts profile IDs only, not slugs,
    so wiring was deferred. Until P3-05b is complete, callers must invoke
    validate_factory_policy_constraints() explicitly before writing a model profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hypogeum.factory_entity import FactoryNotFoundError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PolicyViolationError(ValueError):
    """
    A channel configuration violates one or more factory policies.

    Attributes:
        violations — list of human-readable violation strings.
    """
    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__(
            f"Factory policy violation(s): {'; '.join(violations)}"
        )


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------

@dataclass
class EffectivePolicy:
    """
    Resolved policy for a channel after cascading factory policies over
    runtime config.

    Precedence (most → least authoritative):
        factory_policies → runtime env (FINANCE_BUDGET_CEILING_USD)

    Fields:
        budget_ceiling_usd:
            Per-episode effective ceiling. 0.0 = no ceiling enforced.
            Derived from factory monthly_ceiling_usd / episodes_per_month,
            further constrained by the runtime env ceiling when it is lower.

        allowed_model_slugs:
            None = all slugs allowed. Non-None = only listed slugs permitted.

        blocked_model_slugs:
            Slugs that are never permitted regardless of allowed_model_slugs.

        require_human_review_for:
            Topic category strings that require human review before publish.
            Enforcement deferred to P3-04 (approval system).
    """
    budget_ceiling_usd:       float
    allowed_model_slugs:      Optional[list[str]]
    blocked_model_slugs:      list[str]  = field(default_factory=list)
    require_human_review_for: list[str]  = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_policies(
    factory_id: Optional[str],
    db_path:    Optional[Path],
) -> dict:
    """
    Load resolved factory policies dict.

    Suppresses only FactoryNotFoundError (factory not yet bootstrapped) and
    returns all-permissive defaults in that case. All other exceptions —
    including ValueError (multi-factory ambiguity), schema errors, and DB
    connection failures — propagate to the caller so they are not silently
    masked.
    """
    from hypogeum.factory_entity import get_current_factory_id, get_factory_policies, _DEFAULT_POLICIES
    try:
        fid = factory_id if factory_id is not None else get_current_factory_id(db_path=db_path)
        return get_factory_policies(fid, db_path=db_path)
    except FactoryNotFoundError:
        return dict(_DEFAULT_POLICIES)


def _per_episode_ceiling(policies: dict) -> float:
    """
    Derive per-episode ceiling from factory monthly ceiling.

    Returns 0.0 (no constraint) when monthly_ceiling_usd is 0 or unset.
    """
    monthly = float(policies.get("monthly_ceiling_usd") or 0)
    if monthly <= 0:
        return 0.0
    per_month = max(1, int(policies.get("episodes_per_month") or 4))
    return monthly / per_month


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_effective_policy(
    channel_id: Optional[str] = None,
    factory_id: Optional[str] = None,
    db_path:    Optional[Path] = None,
) -> EffectivePolicy:
    """
    Return the resolved EffectivePolicy for a channel.

    Currently reads factory-level policy only. Per-channel overrides are
    deferred to P3-05b (see module docstring TODO).

    channel_id is accepted as a parameter for future per-channel routing but
    is not used today. Pass factory_id explicitly in multi-factory deployments;
    otherwise the single installed factory is resolved automatically.

    Raises:
        ValueError       — if no factory_id given and multiple factories exist
        FactoryNotFoundError is suppressed (returns permissive defaults)
    """
    policies = _load_policies(factory_id, db_path)
    return EffectivePolicy(
        budget_ceiling_usd        = _per_episode_ceiling(policies),
        allowed_model_slugs       = policies.get("allowed_model_slugs"),
        blocked_model_slugs       = list(policies.get("blocked_model_slugs") or []),
        require_human_review_for  = list(policies.get("require_human_review_for") or []),
    )


def get_effective_budget_ceiling(
    env_ceiling: float          = 0.0,
    channel_id:  Optional[str]  = None,
    factory_id:  Optional[str]  = None,
    db_path:     Optional[Path] = None,
) -> float:
    """
    Return the effective per-episode budget ceiling after cascade.

    Note on signature: the spec describes this as get_effective_budget_ceiling(channel_id,
    db_path=None), receiving only channel identity. The implementation adds env_ceiling
    as an explicit first parameter so the finance gate can thread its config value in
    directly. This is an intentional deviation from the spec signature; it avoids the
    need for the caller to pass a Config object into policy_cascade.

    Cascade logic:
        1. Read factory per-episode ceiling = monthly_ceiling_usd / episodes_per_month.
        2. If env_ceiling > 0 and factory ceiling > 0, return min(factory, env).
        3. If only one is set, return that one.
        4. If neither is set, return 0.0 (no ceiling enforced).

    Raises:
        ValueError — if no factory_id given and multiple factories exist.
        FactoryNotFoundError is suppressed (returns env_ceiling as fallback).

    Args:
        env_ceiling:  config.FINANCE_BUDGET_CEILING_USD (0.0 = not configured).
        channel_id:   Reserved for P3-05b per-channel routing; unused today.
        factory_id:   Explicit factory lookup; None = use single installed factory.
        db_path:      Override DB path; always pass in tests.
    """
    policies = _load_policies(factory_id, db_path)
    factory_ceil = _per_episode_ceiling(policies)

    if factory_ceil > 0.0 and env_ceiling > 0.0:
        return min(factory_ceil, env_ceiling)
    if factory_ceil > 0.0:
        return factory_ceil
    return env_ceiling   # may be 0.0 (no ceiling)


def validate_factory_policy_constraints(
    channel_config,
    factory_id:  Optional[str]  = None,
    db_path:     Optional[Path] = None,
) -> list[str]:
    """
    Validate a channel configuration against factory policy constraints.

    Returns a list of violation strings. An empty list means the config is
    valid. Does NOT raise — the call site decides whether to raise
    PolicyViolationError.

    Distinct from channel_config.validate_channel_config(), which validates
    structural correctness of a config dict (required fields, platform names,
    etc.). This function validates policy-level constraints (blocked models,
    budget bounds) set by the factory operator.

    Checks performed:
        1. Model slugs are not in blocked_model_slugs.
        2. Model slugs are in allowed_model_slugs (when not None).
        3. episode_ceiling_usd (if present on config) does not exceed the
           factory per-episode ceiling.

    channel_config may be:
        - A ChannelConfig dataclass with .models: ModelProfile
        - A plain dict with an optional "models" sub-dict
        - None — all checks skipped, returns []

    Raises:
        ValueError — if no factory_id given and multiple factories exist.
        FactoryNotFoundError is suppressed (returns [] — no constraints enforced).

    TODO P3-05b: wire into create_channel_profile / update_channel_profile.
    """
    if channel_config is None:
        return []

    policies = _load_policies(factory_id, db_path)
    blocked      = set(policies.get("blocked_model_slugs") or [])
    allowed      = policies.get("allowed_model_slugs")   # None = all allowed
    factory_ceil = _per_episode_ceiling(policies)

    violations: list[str] = []

    # ── Collect model slugs ────────────────────────────────────────────────
    model_slugs: list[str] = []

    if hasattr(channel_config, "models"):
        # ChannelConfig dataclass path
        models_obj = channel_config.models
        if models_obj is not None:
            for attr in (
                "episode_producer", "research_director", "thumbnail_planner",
                "brand_guardian_p1", "brand_guardian_p2",
            ):
                val = getattr(models_obj, attr, None)
                if val:
                    model_slugs.append(val)
    elif isinstance(channel_config, dict):
        models_dict = channel_config.get("models", {}) or {}
        for key in (
            "episode_producer", "research_director", "thumbnail_planner",
            "brand_guardian_p1", "brand_guardian_p2",
        ):
            val = models_dict.get(key)
            if val:
                model_slugs.append(val)

    # ── Check 1: blocked model slugs ──────────────────────────────────────
    for slug in model_slugs:
        if slug in blocked:
            violations.append(
                f"Model slug {slug!r} is blocked by factory policy."
            )

    # ── Check 2: allowed model slugs (allowlist) ──────────────────────────
    if allowed is not None:
        allowed_set = set(allowed)
        for slug in model_slugs:
            if slug not in allowed_set:
                violations.append(
                    f"Model slug {slug!r} is not in the factory "
                    f"allowed_model_slugs list."
                )

    # ── Check 3: per-episode budget ceiling ───────────────────────────────
    if factory_ceil > 0.0:
        channel_ceil = None
        if isinstance(channel_config, dict):
            channel_ceil = channel_config.get("episode_ceiling_usd")
        elif hasattr(channel_config, "episode_ceiling_usd"):
            channel_ceil = channel_config.episode_ceiling_usd

        if channel_ceil is not None and float(channel_ceil) > factory_ceil:
            violations.append(
                f"Channel episode_ceiling_usd ${channel_ceil:.2f} exceeds "
                f"factory per-episode ceiling ${factory_ceil:.2f}."
            )

    return violations
