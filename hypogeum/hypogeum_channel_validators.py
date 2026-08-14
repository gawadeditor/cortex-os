"""
hypogeum/hypogeum_channel_validators.py — Hypogeum Channel Package's own
dominance-leak validator

NOT Factory Core. This module encodes Hypogeum-specific creative-prompt
conventions -- the "SHOT-SPECIFIC DIRECTION FOR {shot_id} (AUTHORITATIVE
-- supersedes...)" marker hypogeum.video_prompt_writer.
compile_authoritative_shot_prompt() produces -- and must never be
imported by hypogeum/submission_gate.py, hypogeum/shadow_observation.py,
or any other Factory Core module. Those modules only ever see a generic
`dominance_leak_validator` callable parameter; hypogeum/muapi.py's
shadow-mode wiring is the one place that loads and binds *this specific*
implementation, matching this repository's currently-unseparated
Factory-Core-plus-Hypogeum-Channel-Package layout (see docs/architecture/
AUTHORIZED_GENERATION_ENVELOPE.md's own note that Hypogeum is the
validation customer, not Factory Core itself).

If a future Channel Package other than Hypogeum needs an equivalent
validator, it gets its own separate module with its own separate
creative conventions -- never a shared "if channel == X" branch inside
this file.
"""

from __future__ import annotations

from typing import Optional, Sequence

from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.shadow_observation import DominanceLeakValidatorResult

__all__ = [
    "VALIDATOR_IDENTITY",
    "VALIDATOR_VERSION",
    "StaleValidatorError",
    "hypogeum_dominance_leak_validator",
    "load_dominance_leak_validator",
]

VALIDATOR_IDENTITY = "hypogeum.hypogeum_channel_validators.hypogeum_dominance_leak_validator"
VALIDATOR_VERSION = "v1"

# The version hypogeum/muapi.py's shadow wiring currently trusts. Kept as
# a separate constant (not just "the module's own VALIDATOR_VERSION")
# specifically so a test can simulate a stale validator by asking for a
# different expected version -- see load_dominance_leak_validator().
_CURRENT_EXPECTED_VALIDATOR_VERSION = "v1"


class StaleValidatorError(Exception):
    """Raised when the loaded validator's own version does not match
    what the caller currently expects. Propagates up through
    hypogeum.submission_gate.run_shadow_checks() and is converted to a
    structured UNVERIFIABLE ShadowDecision by hypogeum.shadow_observation.
    evaluate_shadow()'s outer exception handler -- never silently
    ignored, never treated as a pass."""

    def __init__(self, found_version: str, expected_version: str):
        self.found_version = found_version
        self.expected_version = expected_version
        super().__init__(f"dominance-leak validator version {found_version!r} != expected {expected_version!r}")


def hypogeum_dominance_leak_validator(
    envelope: GenerationExecutionEnvelope, *, sibling_prompts: Optional[Sequence[str]] = None,
) -> DominanceLeakValidatorResult:
    """
    Detects exactly the Stage-B defect class this whole architecture
    exists to close (see docs/architecture/AUTHORIZED_GENERATION_
    ENVELOPE.md's own trigger note): a compiled prompt that either (a)
    lacks the per-shot AUTHORITATIVE direction marker entirely, or (b)
    is byte-identical to a sibling shot's compiled prompt -- the same
    stale shared scene-level prompt reused verbatim across multiple
    distinct planned shots.

    sibling_prompts, when supplied, is every other planned shot's own
    compiled prompt for the same scene/beat -- the caller (hypogeum/
    muapi.py's shadow wiring) is responsible for gathering these from
    the live coverage plan; this function performs no file I/O itself.
    """
    prompt = envelope.provider_bound_content
    shot_id = envelope.planned_shot_id

    has_marker = f"SHOT-SPECIFIC DIRECTION FOR {shot_id}" in prompt and "AUTHORITATIVE" in prompt

    leak_detected = not has_marker
    reason = None
    if leak_detected:
        reason = f"no per-shot AUTHORITATIVE direction marker for shot_id {shot_id!r} found in the compiled prompt"
    elif sibling_prompts:
        for sibling in sibling_prompts:
            if sibling and sibling == prompt:
                leak_detected = True
                reason = "compiled prompt is byte-identical to a sibling planned shot's prompt (stale shared scene-level prompt reused across distinct shots)"
                break

    return DominanceLeakValidatorResult(
        validator_identity=VALIDATOR_IDENTITY, validator_version=VALIDATOR_VERSION,
        channel_package_checksum=envelope.channel_package_checksum,
        planned_shot_id=shot_id,
        provider_bound_prompt_checksum=envelope.provider_bound_content_sha256,
        structured_spec_checksum=envelope.structured_spec_checksum,
        scene_prompt_checksum=None,
        leak_detected=leak_detected, reason=reason,
    )


def load_dominance_leak_validator(
    channel_id: str, *, sibling_prompts: Optional[Sequence[str]] = None,
    expected_validator_version: str = _CURRENT_EXPECTED_VALIDATOR_VERSION,
):
    """
    Returns a callable of shape Callable[[GenerationExecutionEnvelope],
    Optional[str]] -- the exact signature hypogeum.submission_gate's
    dominance_leak_validator parameter expects (None = no leak, a string
    = the leak reason) -- bound to sibling_prompts and version-checked
    against expected_validator_version.

    Raises StaleValidatorError immediately (never returns a callable
    that would silently under-check) if this module's own
    VALIDATOR_VERSION does not match expected_validator_version.

    Only "hypogeum" is a recognized channel_id in this module -- any
    other value raises ValueError, which hypogeum.shadow_observation.
    evaluate_shadow()'s exception handling turns into UNVERIFIABLE, the
    same as any other validator-unavailable condition. This is
    deliberately conservative: a channel this module doesn't know about
    gets "cannot verify," never a silent pass-through.
    """
    if channel_id != "hypogeum":
        raise ValueError(f"no dominance-leak validator available for channel_id {channel_id!r}")

    if VALIDATOR_VERSION != expected_validator_version:
        raise StaleValidatorError(VALIDATOR_VERSION, expected_validator_version)

    def _validator(envelope: GenerationExecutionEnvelope) -> Optional[str]:
        result = hypogeum_dominance_leak_validator(envelope, sibling_prompts=sibling_prompts)
        return result.reason if result.leak_detected else None

    return _validator
