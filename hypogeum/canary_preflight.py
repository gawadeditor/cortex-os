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
