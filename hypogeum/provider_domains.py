"""
hypogeum/provider_domains.py — the ONE canonical (provider, provider_operation)
-> provider-domain mapping (ADR-016).

A Channel Operating Grant signs `allowed_provider_domains`. Child
authorization must enforce that a paid call's canonical domain is present
in that signed list -- not merely that the (provider, model,
provider_operation) tuple matches a signed binding. This module is the
single source of truth for that canonicalization.

Matching is EXACT. No case-folding, no whitespace trimming, no prefix or
alias resolution -- an unrecognised (provider, operation) pair, or any
alias of a known one, raises UnknownProviderOperationError and the caller
fails closed. A binding for a provider/model/operation whose domain is
absent from the signed grant is denied.
"""

from __future__ import annotations

from typing import Dict, Tuple

__all__ = [
    "PROVIDER_OPERATION_DOMAINS",
    "PROVIDER_DOMAINS",
    "UnknownProviderOperationError",
    "resolve_provider_domain",
]


class UnknownProviderOperationError(Exception):
    """The (provider, provider_operation) pair is not in the canonical
    map. Fails closed -- never mapped to a default domain."""

    def __init__(self, provider: str, operation: str) -> None:
        self.provider = provider
        self.operation = operation
        super().__init__(
            f"unknown paid provider operation ({provider!r}, {operation!r}) -- "
            f"no canonical provider domain; refusing to authorize"
        )


# (provider, provider_operation) -> domain. Providers and operations are
# the exact literals the eight real adapters / gate paths carry on the
# GenerationExecutionEnvelope (hypogeum.openrouter._TEXT_PROVIDER etc.).
PROVIDER_OPERATION_DOMAINS: Dict[Tuple[str, str], str] = {
    ("openrouter", "text.completion"): "openrouter_text",
    ("perplexity", "research.query"): "perplexity_research",
    ("elevenlabs", "voice.narration_segment"): "elevenlabs_voice",
    ("elevenlabs", "music.track"): "elevenlabs_music",
    ("muapi", "video.text_to_video"): "muapi_video",
    ("muapi", "image.reference"): "muapi_image",
    ("muapi", "image.thumbnail"): "muapi_thumbnail",
    ("muapi", "upscale.video"): "muapi_upscale",
}

# The complete canonical domain set (for grant-spec generation / audit).
PROVIDER_DOMAINS = tuple(sorted(set(PROVIDER_OPERATION_DOMAINS.values())))


def resolve_provider_domain(provider: str, operation: str) -> str:
    """Exact-match canonicalization. Raises UnknownProviderOperationError
    for anything not in PROVIDER_OPERATION_DOMAINS -- including aliases
    that differ only by case or surrounding whitespace."""
    key = (provider, operation)
    domain = PROVIDER_OPERATION_DOMAINS.get(key)
    if domain is None:
        raise UnknownProviderOperationError(provider, operation)
    return domain
