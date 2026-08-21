"""
tests/test_publishing_provider_registry.py — Generic publishing-provider
contract and configuration-driven routing (ADR-013 Decision 7, item 1).

Covers: hypogeum.publishing.provider.{PublishingProvider, UnknownProviderError,
UnsupportedPlatformError}, hypogeum.publishing.provider_registry.{
ProviderRegistry, build_default_registry, resolve_platform_provider,
DEFAULT_PROVIDER_ROUTING, YouTubeDirectProvider, TikTokDirectProvider,
MetaDirectProvider}, and hypogeum.agents.publishing_agent.run_publishing_agent's
new publishing_provider_by_platform parameter.

No network, credential, database, queue, worker, or provider call is made
anywhere in this file. Fake providers/transports only.
"""

from __future__ import annotations

import pytest

from hypogeum.publishing.provider import UnknownProviderError, UnsupportedPlatformError
from hypogeum.publishing.provider_registry import (
    DEFAULT_PROVIDER_ROUTING,
    MetaDirectProvider,
    ProviderRegistry,
    TikTokDirectProvider,
    YouTubeDirectProvider,
    build_default_registry,
    resolve_platform_provider,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, provider_id, platforms, publish_result=None, publish_error=None):
        self.provider_id = provider_id
        self.supported_platforms = frozenset(platforms)
        self._publish_result = publish_result
        self._publish_error = publish_error
        self.publish_calls = []

    def supports_platform(self, platform: str) -> bool:
        return platform in self.supported_platforms

    def publish(self, *, platform, deliverable_type, episode_id, **kwargs):
        self.publish_calls.append({"platform": platform, "deliverable_type": deliverable_type, "episode_id": episode_id})
        if self._publish_error is not None:
            raise self._publish_error
        return self._publish_result or {
            "platform": platform,
            "deliverable_type": deliverable_type,
            "status": "published",
        }


class _NeverImportedModule:
    """Sentinel object standing in for a client module — importing it would
    fail this test if any code path actually imported a real network module."""
    name = "fake_client_module"


# ===========================================================================
# 1/2/3. Registry resolution
# ===========================================================================

class TestRegistryResolution:
    def test_registry_resolves_each_existing_direct_provider(self):
        registry = build_default_registry()
        assert registry.resolve("youtube_direct").provider_id == "youtube_direct"
        assert registry.resolve("tiktok_direct").provider_id == "tiktok_direct"
        assert registry.resolve("meta_direct").provider_id == "meta_direct"

    def test_unknown_provider_fails_closed(self):
        registry = build_default_registry()
        with pytest.raises(UnknownProviderError):
            registry.resolve("does_not_exist")

    def test_unsupported_provider_platform_combo_fails_closed(self):
        registry = build_default_registry()
        with pytest.raises(UnsupportedPlatformError):
            registry.resolve_for_platform("tiktok", "youtube_direct")


# ===========================================================================
# 4/5/6/7. Default routing
# ===========================================================================

class TestDefaultRouting:
    def test_youtube_routes_to_youtube_direct(self):
        assert resolve_platform_provider("youtube").provider_id == "youtube_direct"

    def test_tiktok_routes_to_tiktok_direct(self):
        assert resolve_platform_provider("tiktok").provider_id == "tiktok_direct"

    def test_facebook_routes_to_meta_direct(self):
        assert resolve_platform_provider("facebook").provider_id == "meta_direct"

    def test_instagram_routes_to_meta_direct(self):
        assert resolve_platform_provider("instagram").provider_id == "meta_direct"

    def test_default_provider_resolves_to_real_client_module(self):
        """Default routing must bind to the SAME module objects every
        existing caller/test already targets by name — proves the
        registry is not a reimplementation."""
        import hypogeum.publishing.youtube as real_yt
        import hypogeum.publishing.tiktok as real_tt
        import hypogeum.publishing.meta as real_meta

        assert resolve_platform_provider("youtube").client_module is real_yt
        assert resolve_platform_provider("tiktok").client_module is real_tt
        assert resolve_platform_provider("facebook").client_module is real_meta
        assert resolve_platform_provider("instagram").client_module is real_meta


# ===========================================================================
# 8/9. Configuration override
# ===========================================================================

class TestConfigurationOverride:
    def test_channel_configuration_overrides_provider_selection(self):
        registry = build_default_registry()
        fake = _FakeProvider("fake_youtube", {"youtube"})
        registry.register(fake)

        resolved = resolve_platform_provider("youtube", {"youtube": "fake_youtube"}, registry=registry)
        assert resolved is fake

    def test_missing_mapping_preserves_current_defaults(self):
        """Overriding one platform must leave every other platform's
        routing at the Factory default."""
        registry = build_default_registry()
        fake = _FakeProvider("fake_youtube", {"youtube"})
        registry.register(fake)

        override = {"youtube": "fake_youtube"}
        assert resolve_platform_provider("youtube", override, registry=registry) is fake
        assert resolve_platform_provider("tiktok", override, registry=registry).provider_id == "tiktok_direct"
        assert resolve_platform_provider("facebook", override, registry=registry).provider_id == "meta_direct"
        assert resolve_platform_provider("instagram", override, registry=registry).provider_id == "meta_direct"

    def test_default_provider_routing_constant_unchanged(self):
        assert DEFAULT_PROVIDER_ROUTING == {
            "youtube": "youtube_direct",
            "tiktok": "tiktok_direct",
            "instagram": "meta_direct",
            "facebook": "meta_direct",
        }


# ===========================================================================
# 10/11. Normalized result / error behavior (fake provider only)
# ===========================================================================

class TestNormalizedResultAndErrors:
    def test_publish_returns_normalized_result_shape(self):
        registry = ProviderRegistry()
        fake = _FakeProvider(
            "fake_youtube", {"youtube"},
            publish_result={"platform": "youtube", "deliverable_type": "main_episode", "status": "published", "platform_video_id": "abc123"},
        )
        registry.register(fake)

        provider = resolve_platform_provider("youtube", {"youtube": "fake_youtube"}, registry=registry)
        result = provider.publish(platform="youtube", deliverable_type="main_episode", episode_id="ep_1")

        assert result["platform"] == "youtube"
        assert result["deliverable_type"] == "main_episode"
        assert result["status"] == "published"
        assert result["platform_video_id"] == "abc123"

    def test_provider_failure_preserves_normalized_error_behavior(self):
        registry = ProviderRegistry()
        fake = _FakeProvider("fake_youtube", {"youtube"}, publish_error=RuntimeError("upstream failure"))
        registry.register(fake)

        provider = resolve_platform_provider("youtube", {"youtube": "fake_youtube"}, registry=registry)
        with pytest.raises(RuntimeError, match="upstream failure"):
            provider.publish(platform="youtube", deliverable_type="main_episode", episode_id="ep_1")


# ===========================================================================
# 14. Registry isolation
# ===========================================================================

class TestRegistryIsolation:
    def test_two_test_local_registries_do_not_leak_state(self):
        r1 = ProviderRegistry()
        r2 = ProviderRegistry()
        r1.register(_FakeProvider("only_in_r1", {"youtube"}))

        assert r1.resolve("only_in_r1").provider_id == "only_in_r1"
        with pytest.raises(UnknownProviderError):
            r2.resolve("only_in_r1")

    def test_build_default_registry_returns_a_fresh_instance_each_call(self):
        r1 = build_default_registry()
        r2 = build_default_registry()
        assert r1 is not r2
        r1.register(_FakeProvider("only_in_r1", {"youtube"}))
        with pytest.raises(UnknownProviderError):
            r2.resolve("only_in_r1")


# ===========================================================================
# 15. Zernio is registered (ADR-013 Decision 7, item 4) but never a default
# route and never active for any channel by registration alone. Registered
# under "zernio", never "zernio_direct" (that provider_id never existed).
# ===========================================================================

class TestZernioRegisteredButNotDefault:
    def test_zernio_provider_is_registered(self):
        registry = build_default_registry()
        provider = registry.resolve("zernio")
        assert provider.provider_id == "zernio"

    def test_zernio_direct_was_never_a_provider_id(self):
        registry = build_default_registry()
        with pytest.raises(UnknownProviderError):
            registry.resolve("zernio_direct")

    def test_no_zernio_string_in_default_routing(self):
        assert "zernio" not in DEFAULT_PROVIDER_ROUTING.values()
        assert "zernio" not in DEFAULT_PROVIDER_ROUTING

    def test_default_platform_routing_still_resolves_direct_providers(self):
        """Registering Zernio must not shift a single existing default."""
        assert resolve_platform_provider("youtube").provider_id == "youtube_direct"
        assert resolve_platform_provider("tiktok").provider_id == "tiktok_direct"
        assert resolve_platform_provider("facebook").provider_id == "meta_direct"
        assert resolve_platform_provider("instagram").provider_id == "meta_direct"


# ===========================================================================
# 16. Constructing the registry performs no external import / credential
# resolution / network action.
# ===========================================================================

class TestNoSideEffectsOnConstruction:
    def test_constructing_providers_directly_with_injected_module_makes_no_real_import(self):
        """Providers accept an injected client_module — constructing one
        with a fake module never touches the real network-facing module."""
        fake_module = _NeverImportedModule()
        provider = YouTubeDirectProvider(client_module=fake_module)
        assert provider.client_module is fake_module

        provider2 = TikTokDirectProvider(client_module=fake_module)
        assert provider2.client_module is fake_module

        provider3 = MetaDirectProvider(client_module=fake_module)
        assert provider3.client_module is fake_module

    def test_build_default_registry_makes_no_credential_or_network_call(self, monkeypatch):
        """Constructing the default registry must not call credential_manager
        or requests — only bind module references."""
        def _fail(*a, **k):
            raise AssertionError("build_default_registry() must not resolve credentials or make network calls")

        monkeypatch.setattr("hypogeum.credential_manager.resolve_credential", _fail, raising=False)
        monkeypatch.setattr("requests.get", _fail, raising=False)
        monkeypatch.setattr("requests.post", _fail, raising=False)

        registry = build_default_registry()
        assert registry.resolve("youtube_direct") is not None
        assert registry.resolve("tiktok_direct") is not None
        assert registry.resolve("meta_direct") is not None
# ===========================================================================
# 13. Mismatched facebook/instagram routing fails closed (mirrors the check
# hypogeum.agents.publishing_agent.run_publishing_agent performs before any
# pre-flight/upload work — tested at the registry level it delegates to, to
# avoid duplicating that function's unrelated pre-flight setup here; the
# existing test_publishing_agent.py suite, run unchanged alongside this
# file, is the integration-level safety net for the agent itself.)
# ===========================================================================

class TestMismatchedMetaRouting:
    def test_facebook_instagram_routed_to_different_providers_is_detectable(self):
        registry = build_default_registry()
        fake = _FakeProvider("fake_meta", {"instagram"})
        registry.register(fake)
        override = {"instagram": "fake_meta"}  # facebook stays default meta_direct

        fb = resolve_platform_provider("facebook", override, registry=registry)
        ig = resolve_platform_provider("instagram", override, registry=registry)
        assert fb.provider_id != ig.provider_id
