"""
hypogeum/publishing/provider.py — Generic publishing-provider contract
(ADR-013 Decision 7, item 1).

Mirrors the existing hypogeum.publishing.staging.StagingProvider Protocol
pattern (a small, structural interface plus a resolver — see that module's
own docstring) applied to publishing providers instead of staging backends.

A "provider" is a system that can publish a deliverable to one or more
destination platforms. It is never itself a destination platform — platform
identity (`youtube`, `tiktok`, `instagram`, `facebook`) stays exactly as
already modeled in `publishing_outcomes.platform` / `platform_analytics.platform`
(see DECISION_LOG.md ADR-013 Decision 1). `hypogeum.publishing.provider_registry`
resolves which provider handles a given platform, driven by configuration.

This module owns the contract shape only. It never resolves credentials
(Credential Manager remains the sole source, per ADR-013 Decision 3) and
never references any channel identity (Factory Core stays channel-agnostic).
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Exceptions — fail closed, never silently fall back
# ---------------------------------------------------------------------------

class ProviderResolutionError(RuntimeError):
    """Base class for provider/platform resolution failures."""


class UnknownProviderError(ProviderResolutionError):
    """Raised when a provider_id has no registered implementation."""


class UnsupportedPlatformError(ProviderResolutionError):
    """Raised when a resolved provider does not support the requested platform."""


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

@runtime_checkable
class PublishingProvider(Protocol):
    """
    Protocol for a publishing provider (a direct platform integration today;
    an aggregator/transport such as Zernio in a later slice — ADR-013
    Decision 7, item 4).

    Implementations own no credential material and no channel identity.
    Credential resolution stays in hypogeum.credential_manager; channel
    configuration (provider preference, enabled platforms) stays in the
    Channel Package / caller-supplied configuration.
    """

    provider_id: str
    """Stable, neutral provider identifier (e.g. 'youtube_direct'). Never a
    platform name."""

    supported_platforms: frozenset
    """Destination platforms this provider can publish to (subset of
    {'youtube', 'tiktok', 'instagram', 'facebook', ...})."""

    def supports_platform(self, platform: str) -> bool:
        """Capability check. Never resolves credentials or makes a network call."""
        ...

    def publish(
        self,
        *,
        platform: str,
        deliverable_type: str,
        episode_id: str,
        **kwargs,
    ) -> dict:
        """
        Publish one requested deliverable to one destination platform.

        Args:
            platform:         Destination platform. Must be in
                              self.supported_platforms — implementations
                              raise UnsupportedPlatformError otherwise.
            deliverable_type: 'main_episode' or 'short' (matches
                              hypogeum.publishing.record's existing vocabulary).
            episode_id:       Episode identifier for log correlation.
            **kwargs:         Provider-specific publish inputs (credentials,
                              file paths, metadata) — never inspected by the
                              registry or by callers outside the provider
                              itself.

        Returns:
            A normalized result dict compatible with
            hypogeum.publishing.record.make_result_entry()'s shape — at
            minimum {"platform": ..., "deliverable_type": ..., "status": ...}.
        """
        ...
