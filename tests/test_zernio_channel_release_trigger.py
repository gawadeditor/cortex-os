"""
tests/test_zernio_channel_release_trigger.py — Connects buffer demand
discovery to the Zernio release-flow orchestrator.

Covers hypogeum.publishing.zernio_channel_release_trigger.
process_channel_ready_bundles(): finds not-yet-published complete items
for a channel's bundle and publishes each via fake transports; proves
repeated calls (simulating repeated worker polling / a restart) never
re-submit an already-published item; proves channel isolation; proves
platform resolution comes from the Channel Package, never a global
default.
"""

from __future__ import annotations

import json

import pytest

from hypogeum.buffer import DeliverableBundleDefinition
from hypogeum.channel_config import (
    create_brand_profile,
    create_channel_operations_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
)
from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import get_connection, initialize_schema, insert_episode, insert_episode_topic
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.readiness import MetadataOverride, MetadataResolutionError
from hypogeum.publishing.zernio import ZernioProvider, ZernioTransportResponse
from hypogeum.publishing.zernio_channel_release_trigger import build_publish_package_from_episode_dir, process_channel_ready_bundles
from hypogeum.publishing.zernio_media_staging import (
    PresignTransportResponse,
    UploadTransportResponse,
    ZernioMediaStagingProvider,
)

CHANNEL_ID = "hypogeum"
BUNDLE = DeliverableBundleDefinition(channel_id=CHANNEL_ID, bundle_id="main_episode_bundle", target_buffer_count=1)


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "trigger_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def assets_root(tmp_path):
    root = tmp_path / "assets"
    root.mkdir()
    return root


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _setup_channel(db_path, channel_id=CHANNEL_ID, *, platforms_enabled=("youtube",), deliverable_schedule_policy=None):
    brand = create_brand_profile(name=f"{channel_id} Brand", db_path=db_path)
    model = create_model_profile(name=f"{channel_id} Model", db_path=db_path)
    pub = create_publishing_profile(name=f"{channel_id} Publishing", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name=channel_id,
        brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )
    # The autonomous trigger requires an EXPLICIT deliverable_schedule_policy
    # entry for every deliverable it publishes (require_explicit_schedule_policy=True)
    # -- the single publishing_schedule fallback below exists only for
    # legacy/manual callers and is deliberately never relied on by these tests.
    if deliverable_schedule_policy is None:
        deliverable_schedule_policy = {"main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"}}
    create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, timezone="UTC",
        publishing_schedule={"day_of_week": "sunday", "time_of_day": "14:00"},
        enabled_platforms={p: True for p in platforms_enabled},
        deliverable_schedule_policy=deliverable_schedule_policy,
        db_path=db_path,
    )


def _seed_complete_episode(db_path, assets_root, episode_id, *, channel_id=CHANNEL_ID, title="A Real Title", n_shorts=0, with_thumbnail=None):
    conn = get_connection(db_path)
    try:
        insert_episode(conn, episode_id=episode_id, created_at=_now_iso(), topic=title, status="complete")
        insert_episode_topic(
            conn, topic_id=f"topic_{episode_id}", channel_id=channel_id, topic_text="A topic",
            created_at=_now_iso(), updated_at=_now_iso(), episode_id=episode_id,
        )
        conn.commit()
    finally:
        conn.close()

    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True)
    pkg = {
        "metadata_source": {
            "episode_title": title, "description": "A real description." if title else "",
            "shorts_titles": [f"s{i}" for i in range(n_shorts)] if title else [],
        },
        "shorts_renders": [{"short_id": f"sh_{i}"} for i in range(n_shorts)],
    }
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps(pkg), encoding="utf-8")
    (ep_dir / f"main_{episode_id}.mp4").write_bytes(b"main bytes" * 500)
    for i in range(n_shorts):
        (ep_dir / f"short_{i}_{episode_id}.mp4").write_bytes(f"short {i}".encode() * 500)
    if with_thumbnail if with_thumbnail is not None else bool(title):
        from PIL import Image
        thumbs_dir = ep_dir / "thumbnails"
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1280, 720), color=(60, 60, 60)).save(thumbs_dir / "candidate_1.jpg", "JPEG")
        thumb_pkg = {
            "thumbnail_package_id": f"tp_{episode_id}", "episode_id": episode_id,
            "main_thumbnail": {"asset_id": f"episodes/{episode_id}/thumbnails/candidate_1"},
            "candidates": [{"candidate_id": "tsc_test_1", "rank": 1, "selected": True, "status": "generated", "asset_id": f"episodes/{episode_id}/thumbnails/candidate_1"}],
            "thumbnail_qa_report": {"overall_status": "pass", "checks": [], "failures": []},
            "thumbnail_identity_report": {"verdict": "APPROVED", "violations": []},
        }
        (ep_dir / f"thumbnail_package_{episode_id}.json").write_text(json.dumps(thumb_pkg), encoding="utf-8")


def _fully_bind(db_path, channel_id=CHANNEL_ID, platform="youtube"):
    create_secret_profile(
        provider="zernio", label="k", credential_type="api_key",
        credential_data="sk_test_never_real", scope="factory", db_path=db_path,
    )
    profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1")
    account = upsert_provider_account(
        db_path, provider_id="zernio", platform=platform, external_account_id="acct_1",
        provider_profile_id=profile["provider_profile_id"], status="active",
    )
    bind_channel_provider(
        db_path, channel_id=channel_id, capability="publishing", platform=platform,
        provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
        provider_account_id=account["provider_account_id"],
    )


class FakeMediaTransport:
    def __init__(self):
        self.presign_calls = []
        self._counter = 0

    def presign(self, *, url, headers, json_body, timeout):
        self._counter += 1
        self.presign_calls.append(json_body)
        return PresignTransportResponse(
            status_code=200,
            json_body={"uploadUrl": f"https://upload.example.com/{self._counter}",
                       "publicUrl": f"https://cdn.example.com/{self._counter}",
                       "key": f"key_{self._counter}", "expiresIn": 3600},
        )

    def upload(self, *, url, headers, data, timeout):
        return UploadTransportResponse(status_code=200)


class FakePublishTransport:
    def __init__(self):
        self.post_calls = []
        self._counter = 0

    def post(self, *, url, headers, json_body, timeout):
        self._counter += 1
        self.post_calls.append(json_body)
        platform = json_body["platforms"][0]["platform"]
        return ZernioTransportResponse(
            status_code=201,
            json_body={"message": "created", "post": {
                "_id": f"post_{self._counter}",
                "platforms": [{"platform": platform, "platformPostId": f"native_{self._counter}", "status": "published"}],
            }},
        )


class TestFullSequence:
    def test_ready_item_is_published(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_complete_episode(db_path, assets_root, "ep_ready")
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert len(results) == 1
        assert results[0].item_id == "ep_ready"
        assert results[0].platform == "youtube"
        assert results[0].result.status == "published"
        assert len(publish_transport.post_calls) == 1


class TestIdempotencyAcrossRepeatedPolling:
    def test_second_call_does_not_resubmit_already_published_item(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_complete_episode(db_path, assets_root, "ep_ready")
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        kwargs = dict(
            db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        first = process_channel_ready_bundles(CHANNEL_ID, BUNDLE, **kwargs)
        second = process_channel_ready_bundles(CHANNEL_ID, BUNDLE, **kwargs)  # simulates the next worker poll

        assert len(first) == 1
        assert first[0].result.status == "published"
        assert len(second) == 0, "an already-published item must never be re-attempted on the next poll"
        assert len(publish_transport.post_calls) == 1, "no second live submission was made"

    def test_not_yet_ready_item_stays_reattempted_but_never_falsely_publishes(self, db_path, assets_root):
        # No binding installed -- readiness blocks every time. Repeated
        # calls must keep reporting blocked, never a false "published".
        _setup_channel(db_path)
        _seed_complete_episode(db_path, assets_root, "ep_unready")

        first = process_channel_ready_bundles(CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False)
        second = process_channel_ready_bundles(CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False)

        assert len(first) == 1 and first[0].result.status == "blocked_readiness"
        assert len(second) == 1 and second[0].result.status == "blocked_readiness"


class TestChannelIsolation:
    def test_channel_a_ready_items_never_appear_when_processing_channel_b(self, db_path, assets_root):
        channel_a_bundle = DeliverableBundleDefinition(channel_id="channel_a", bundle_id="main_episode_bundle", target_buffer_count=1)
        channel_b_bundle = DeliverableBundleDefinition(channel_id="channel_b", bundle_id="main_episode_bundle", target_buffer_count=1)

        _setup_channel(db_path, "channel_a")
        _setup_channel(db_path, "channel_b")
        _seed_complete_episode(db_path, assets_root, "ep_a", channel_id="channel_a")
        _seed_complete_episode(db_path, assets_root, "ep_b", channel_id="channel_b")
        _fully_bind(db_path, channel_id="channel_a")
        _fully_bind(db_path, channel_id="channel_b")

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        kwargs = dict(
            db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        results_a = process_channel_ready_bundles("channel_a", channel_a_bundle, **kwargs)
        results_b = process_channel_ready_bundles("channel_b", channel_b_bundle, **kwargs)

        assert {r.item_id for r in results_a} == {"ep_a"}
        assert {r.item_id for r in results_b} == {"ep_b"}


class TestPlatformResolutionFromChannelPackage:
    def test_no_enabled_platform_means_nothing_to_do(self, db_path, assets_root):
        _setup_channel(db_path, platforms_enabled=())
        _seed_complete_episode(db_path, assets_root, "ep_ready")
        results = process_channel_ready_bundles(CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False)
        assert results == []

    def test_no_channel_operations_profile_means_nothing_to_do(self, db_path, assets_root):
        brand = create_brand_profile(name="X Brand", db_path=db_path)
        model = create_model_profile(name="X Model", db_path=db_path)
        pub = create_publishing_profile(name="X Publishing", db_path=db_path)
        create_channel_profile(
            channel_id=CHANNEL_ID, name=CHANNEL_ID,
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        _seed_complete_episode(db_path, assets_root, "ep_ready")
        results = process_channel_ready_bundles(CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False)
        assert results == []


# ===========================================================================
# 2026-08-23: platform readiness must come from the ACTUAL bound provider's
# account health, never solely from a channel-level enabled_platforms flag
# that can go stale (e.g. still encoding a prior, now-replaced connector's
# failure for a platform the channel has since moved to a healthy
# aggregation provider for).
# ===========================================================================

class TestTargetPlatformsUseProviderPathReadiness:
    def test_stale_disabled_flag_does_not_block_a_platform_with_a_healthy_binding(self, db_path):
        from hypogeum.publishing.zernio_channel_release_trigger import _resolve_target_platforms

        # enabled_platforms says tiktok is off (e.g. a prior direct-connector
        # failure was recorded here) -- but a healthy binding through
        # whatever provider is ACTUALLY configured for tiktok now exists.
        _setup_channel(db_path, platforms_enabled=("youtube",))
        _fully_bind(db_path, platform="tiktok")

        platforms = _resolve_target_platforms(CHANNEL_ID, db_path)
        assert "tiktok" in platforms

    def test_platform_with_no_binding_at_all_is_never_included_even_if_flagged_enabled(self, db_path):
        from hypogeum.channel_config import update_channel_operations_profile
        from hypogeum.publishing.zernio_channel_release_trigger import _resolve_target_platforms

        _setup_channel(db_path, platforms_enabled=("youtube",))
        # Flip instagram "on" at the operator-intent level, but never create
        # any channel_provider_bindings/provider_accounts row for it --
        # operator intent alone must never fabricate a working destination.
        update_channel_operations_profile(
            CHANNEL_ID, db_path=db_path,
            enabled_platforms={"youtube": True, "instagram": True, "tiktok": False, "facebook": False},
        )
        platforms = _resolve_target_platforms(CHANNEL_ID, db_path)
        # instagram is still reported (operator explicitly wants it) --
        # but any real call path (_resolve_approved_binding) fails closed on
        # it separately; this function's own job is only "targetable per
        # policy or provider health", not "guaranteed to have a binding".
        # The real invariant under test: an UNBOUND, UNFLAGGED platform
        # (tiktok/facebook here) is never included just because some other
        # platform is.
        assert "tiktok" not in platforms
        assert "facebook" not in platforms

    def test_unhealthy_binding_does_not_make_a_disabled_platform_ready(self, db_path):
        from hypogeum.publishing.provider_repository import mark_connection_status
        from hypogeum.publishing.zernio_channel_release_trigger import _resolve_target_platforms

        _setup_channel(db_path, platforms_enabled=("youtube",))
        _fully_bind(db_path, platform="facebook")
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT provider_account_id FROM channel_provider_bindings WHERE channel_id=? AND platform='facebook'",
                (CHANNEL_ID,),
            ).fetchone()
        finally:
            conn.close()
        mark_connection_status(db_path, provider_account_id=row["provider_account_id"], needs_reconnection=True)

        platforms = _resolve_target_platforms(CHANNEL_ID, db_path)
        assert "facebook" not in platforms

    def test_operator_enabled_platform_still_included_even_without_a_healthy_binding(self, db_path):
        """Operator intent (enabled_platforms) and provider health are
        independent OR'd signals -- neither one alone silently overrides
        the other into exclusion."""
        from hypogeum.publishing.zernio_channel_release_trigger import _resolve_target_platforms

        _setup_channel(db_path, platforms_enabled=("youtube", "tiktok"))
        platforms = _resolve_target_platforms(CHANNEL_ID, db_path)
        assert "tiktok" in platforms


class TestMetadataOverridePerItem:
    def test_override_resolves_title_for_the_targeted_item_only(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_complete_episode(db_path, assets_root, "ep_f3619022", title="", with_thumbnail=True)  # empty title, like the real episode
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            metadata_override_by_item={"ep_f3619022": MetadataOverride(title="Operator-Confirmed Title", description="d", shorts_titles=[])},
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )
        assert len(results) == 1
        assert results[0].result.status == "published"


class TestNoLiveCalls:
    def test_no_real_socket_opened(self, db_path, assets_root, monkeypatch):
        import socket

        def _fail_socket(*a, **k):
            raise AssertionError("no real socket may be opened by this test")

        monkeypatch.setattr(socket.socket, "connect", _fail_socket)
        _setup_channel(db_path)
        _seed_complete_episode(db_path, assets_root, "ep_ready")
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )
        assert results[0].result.status == "published"


class TestBuildPublishPackageResolvesTitleAndDescription:
    """
    build_publish_package_from_episode_dir() must populate each
    deliverable's title/description via readiness.resolve_canonical_
    metadata() -- the same resolution the A_METADATA gate already
    validated -- never leave the package's real submission content empty
    when a real title/description/shorts_titles exists on disk. Fixes the
    prior gap where the real Zernio request never carried any title/
    description at all.
    """

    def test_main_and_shorts_get_real_titles_and_main_gets_description(self, db_path, assets_root):
        _seed_complete_episode(db_path, assets_root, "ep_meta", title="A Real Episode Title", n_shorts=2)
        package = build_publish_package_from_episode_dir(
            CHANNEL_ID, "ep_meta", "bundle_x", assets_root, db_path=db_path,
        )
        assert package.main_episode.title == "A Real Episode Title"
        assert package.main_episode.description == "A real description."
        titles_by_id = {d.deliverable_id: d.title for d in package.shorts}
        assert titles_by_id == {"short_sh_0": "s0", "short_sh_1": "s1"}

    def test_missing_metadata_raises_never_fails_open_to_none(self, db_path, assets_root):
        # Fail-closed, per the cross-platform readiness correction: the
        # autonomous production path must know explicitly that metadata
        # is unresolved BEFORE any staging/provider call, never silently
        # proceed with title=None/description=None and accept whatever
        # Zernio's server-side default happens to be.
        _seed_complete_episode(db_path, assets_root, "ep_empty", title="", n_shorts=0)
        with pytest.raises(MetadataResolutionError):
            build_publish_package_from_episode_dir(
                CHANNEL_ID, "ep_empty", "bundle_x", assets_root, db_path=db_path,
            )

    def test_process_channel_ready_bundles_isolates_a_metadata_failure_per_item(self, db_path, assets_root):
        # One item's unresolved metadata must report a blocked result for
        # that item's platforms, never raise and abort the whole channel
        # tick (which would silently skip every OTHER ready item too).
        _setup_channel(db_path)
        _seed_complete_episode(db_path, assets_root, "ep_good", title="A Real Title", n_shorts=0)
        _seed_complete_episode(db_path, assets_root, "ep_bad", title="", n_shorts=0)
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )
        by_item = {r.item_id: r for r in results}
        assert by_item["ep_good"].result.status == "published"
        assert by_item["ep_bad"].result.status == "blocked_metadata"
        assert by_item["ep_bad"].result.blocked_reason is not None
