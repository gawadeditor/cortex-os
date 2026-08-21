"""
hypogeum/publishing/provider_registry.py — Provider registry and
configuration-driven routing (ADR-013 Decision 7, item 1).

Resolves, per destination platform, WHICH publishing provider implements
it — driven by an optional caller-supplied mapping (Channel Package /
runtime configuration), falling back to DEFAULT_PROVIDER_ROUTING. This
replaces the previously hardcoded

    from hypogeum.publishing import youtube as yt_client
    from hypogeum.publishing import meta as meta_client
    from hypogeum.publishing import tiktok as tt_client

in hypogeum.agents.publishing_agent.run_publishing_agent() with an
equivalent, but configurable, resolution step. Every concrete provider
below wraps its existing module WITHOUT reimplementing any network logic —
`.client_module` is literally the same `hypogeum.publishing.{youtube,tiktok,
meta}` module object every existing caller and test already targets, so
default-routed behavior (every existing caller; no publishing_provider_by_platform
given) is byte-for-byte unchanged.

No Zernio provider is registered here (ADR-013 Decision 7, item 4 — a later
slice). Registries are plain instances, never a hidden mutable global:
build_default_registry() returns a fresh ProviderRegistry every call, and
constructing one performs no import beyond what the process already has
loaded, no credential resolution, and no network action.
"""

from __future__ import annotations

from typing import Optional

from hypogeum.publishing.provider import (
    PublishingProvider,
    UnknownProviderError,
    UnsupportedPlatformError,
)

# ---------------------------------------------------------------------------
# Factory-default routing — generic, not Hypogeum-specific. A Channel
# Package may override any subset of these via the publishing_provider_by_platform
# mapping accepted by run_publishing_agent()/run_pipeline().
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER_ROUTING: dict = {
    "youtube": "youtube_direct",
    "tiktok": "tiktok_direct",
    "instagram": "meta_direct",
    "facebook": "meta_direct",
}


# ---------------------------------------------------------------------------
# Concrete direct providers — thin wrappers around the existing modules.
# Each exposes .client_module so callers that still need the full existing
# multi-step orchestration (hypogeum.agents.publishing_agent's STEP 6-13)
# can keep calling it exactly as before. .publish() is the new normalized
# single-deliverable capability the generic contract requires; it covers
# the straightforward single-call cases today (YouTube video, TikTok
# direct-upload init, Facebook video) and is intentionally NOT wired into
# run_publishing_agent()'s existing multi-step flow in this slice — see
# DECISION_LOG.md ADR-013 Decision 7 for the phased delivery order.
# ---------------------------------------------------------------------------

class YouTubeDirectProvider:
    provider_id = "youtube_direct"
    supported_platforms = frozenset({"youtube"})

    def __init__(self, client_module=None):
        if client_module is None:
            import hypogeum.publishing.youtube as client_module
        self.client_module = client_module

    def supports_platform(self, platform: str) -> bool:
        return platform in self.supported_platforms

    def publish(self, *, platform: str, deliverable_type: str, episode_id: str, **kwargs) -> dict:
        if not self.supports_platform(platform):
            raise UnsupportedPlatformError(
                f"{self.provider_id!r} does not support platform {platform!r}"
            )
        platform_video_id = self.client_module.upload_video(
            config=kwargs["config"],
            access_token=kwargs["access_token"],
            video_path=kwargs["video_path"],
            metadata=kwargs["metadata"],
            episode_id=episode_id,
        )
        return {
            "platform": platform,
            "deliverable_type": deliverable_type,
            "status": "published",
            "platform_video_id": platform_video_id,
        }


class TikTokDirectProvider:
    provider_id = "tiktok_direct"
    supported_platforms = frozenset({"tiktok"})

    def __init__(self, client_module=None):
        if client_module is None:
            import hypogeum.publishing.tiktok as client_module
        self.client_module = client_module

    def supports_platform(self, platform: str) -> bool:
        return platform in self.supported_platforms

    def publish(self, *, platform: str, deliverable_type: str, episode_id: str, **kwargs) -> dict:
        if not self.supports_platform(platform):
            raise UnsupportedPlatformError(
                f"{self.provider_id!r} does not support platform {platform!r}"
            )
        # TikTok's real publish path is a multi-step init -> chunked upload
        # -> poll sequence (hypogeum.publishing.tiktok.init_tiktok_upload()
        # returns only (publish_id, upload_url); the video is not actually
        # published until upload_tiktok_chunks() + poll_tiktok_publish()/
        # poll_tiktok_inbox_status() complete, and STEP 10 in
        # run_publishing_agent() also selects Direct Post vs Upload-to-Inbox
        # per ADR-011). Reproducing that full sequence here is out of scope
        # for this slice — routing still correctly resolves 'tiktok' to
        # tiktok_direct, but this single-call publish() is not yet
        # implemented. Production TikTok publishing is unaffected: it
        # continues through STEP 10's existing direct orchestration.
        raise NotImplementedError(
            "TikTokDirectProvider.publish() is not yet implemented in the "
            "generic contract (TikTok's real flow is multi-step: init -> "
            "chunked upload -> poll); production TikTok publishing continues "
            "unchanged through the existing run_publishing_agent() STEP 10 "
            "orchestration."
        )


class MetaDirectProvider:
    provider_id = "meta_direct"
    supported_platforms = frozenset({"facebook", "instagram"})

    def __init__(self, client_module=None):
        if client_module is None:
            import hypogeum.publishing.meta as client_module
        self.client_module = client_module

    def supports_platform(self, platform: str) -> bool:
        return platform in self.supported_platforms

    def publish(self, *, platform: str, deliverable_type: str, episode_id: str, **kwargs) -> dict:
        if not self.supports_platform(platform):
            raise UnsupportedPlatformError(
                f"{self.provider_id!r} does not support platform {platform!r}"
            )
        if platform == "facebook":
            platform_video_id = self.client_module.upload_facebook_video(
                config=kwargs["config"],
                access_token=kwargs["access_token"],
                video_path=kwargs["video_path"],
                metadata=kwargs["metadata"],
                episode_id=episode_id,
            )
            return {
                "platform": platform,
                "deliverable_type": deliverable_type,
                "status": "published",
                "platform_video_id": platform_video_id,
            }
        # Instagram's existing publish path is a multi-step stage -> container
        # -> poll -> publish -> cleanup sequence orchestrated today by
        # hypogeum.agents.publishing_agent's STEP 9 (~260 lines), not a single
        # meta.py function. Reproducing that sequence here is out of scope for
        # this slice (ADR-013 Decision 7 item 1 is contract + routing, not a
        # rewrite of existing multi-step orchestration) — routing still
        # correctly resolves 'instagram' to meta_direct (supports_platform()
        # is True), but this single-call publish() is not yet implemented for
        # it. Production Instagram publishing is unaffected: it continues
        # through STEP 9's existing direct orchestration.
        raise NotImplementedError(
            "MetaDirectProvider.publish() for platform='instagram' is not yet "
            "implemented in the generic contract; production Instagram "
            "publishing continues unchanged through the existing "
            "run_publishing_agent() STEP 9 orchestration."
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ProviderRegistry:
    """
    Holds provider instances by provider_id. A plain object, never a module-
    level mutable global — callers construct their own (build_default_registry()
    for production defaults, or a hand-built instance in tests), so two
    registries never share or leak state.
    """

    def __init__(self) -> None:
        self._providers: dict[str, PublishingProvider] = {}

    def register(self, provider: PublishingProvider) -> None:
        self._providers[provider.provider_id] = provider

    def resolve(self, provider_id: str) -> PublishingProvider:
        """Fail closed: raises UnknownProviderError for an unregistered ID."""
        try:
            return self._providers[provider_id]
        except KeyError:
            raise UnknownProviderError(f"unknown publishing provider: {provider_id!r}") from None

    def resolve_for_platform(self, platform: str, provider_id: str) -> PublishingProvider:
        """Resolve provider_id, then fail closed if it does not support platform."""
        provider = self.resolve(provider_id)
        if not provider.supports_platform(platform):
            raise UnsupportedPlatformError(
                f"provider {provider_id!r} does not support platform {platform!r}"
            )
        return provider


def build_default_registry() -> ProviderRegistry:
    """
    Fresh registry with the three existing direct providers registered.
    No Zernio provider is added here (ADR-013 Decision 7, item 4 — later
    slice). Constructing this performs no credential resolution and no
    network action — each provider's __init__ only binds a module reference.
    """
    registry = ProviderRegistry()
    registry.register(YouTubeDirectProvider())
    registry.register(TikTokDirectProvider())
    registry.register(MetaDirectProvider())
    return registry


# ---------------------------------------------------------------------------
# Resolver — the actual "configuration-driven routing" entry point
# ---------------------------------------------------------------------------

def resolve_platform_provider(
    platform: str,
    provider_map: Optional[dict] = None,
    registry: Optional[ProviderRegistry] = None,
) -> PublishingProvider:
    """
    Resolve which provider handles `platform`.

    Args:
        platform:     Destination platform ('youtube', 'tiktok', 'instagram',
                      'facebook').
        provider_map: Optional {platform: provider_id} override (Channel
                      Package / runtime configuration). Any platform absent
                      from this mapping falls back to
                      DEFAULT_PROVIDER_ROUTING — so a caller overriding only
                      one platform leaves every other platform's routing
                      unchanged. None (default, every existing caller)
                      uses DEFAULT_PROVIDER_ROUTING unmodified, preserving
                      current behavior exactly.
        registry:     Optional registry to resolve against (test-local
                      injection). None (default) builds a fresh
                      build_default_registry().

    Returns:
        The resolved PublishingProvider instance.

    Raises:
        UnknownProviderError      — platform not in the effective mapping,
                                    or the mapped provider_id is unregistered.
        UnsupportedPlatformError  — the mapped provider does not declare
                                    support for `platform`.
    """
    registry = registry if registry is not None else build_default_registry()
    effective_map = {**DEFAULT_PROVIDER_ROUTING, **(provider_map or {})}
    if platform not in effective_map:
        raise UnknownProviderError(f"no provider mapping for platform {platform!r}")
    provider_id = effective_map[platform]
    return registry.resolve_for_platform(platform, provider_id)
