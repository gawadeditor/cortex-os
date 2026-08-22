"""
tests/test_cross_platform_publishing.py — Cross-platform (TikTok/
Instagram/Facebook) Zernio distribution, per the Hypogeum matrix: main
episode -> YouTube only; every Short -> YouTube, TikTok, Instagram,
Facebook. Covers hypogeum.publishing.cross_platform_readiness and the
per-platform deliverable routing added to
hypogeum.publishing.zernio_channel_release_trigger.process_channel_ready_bundles().

All state uses an isolated tmp_path SQLite database and fake transports —
never the production database, never a real socket, never a live Zernio
call. No live cross-platform POST occurs anywhere in this file.
"""

from __future__ import annotations

import json
import socket

import pytest

from hypogeum.channel_config import (
    create_brand_profile,
    create_channel_operations_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
)
from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import get_connection, initialize_schema, insert_episode, insert_episode_topic
from hypogeum.publishing.cross_platform_readiness import check_cross_platform_readiness
from hypogeum.publishing.provider_repository import bind_channel_provider, upsert_provider_account, upsert_provider_profile
from hypogeum.publishing.zernio import ZernioProvider, ZernioTransportResponse
from hypogeum.publishing.zernio_channel_release_trigger import DeliverableBundleDefinition, process_channel_ready_bundles
from hypogeum.publishing.zernio_media_staging import PresignTransportResponse, UploadTransportResponse, ZernioMediaStagingProvider
from hypogeum.publishing.zernio_platform_contracts import ZERNIO_PLATFORM_CONTRACTS

CHANNEL_ID = "hypogeum"
EPISODE_ID = "ep_xplat01"
BUNDLE = DeliverableBundleDefinition(channel_id=CHANNEL_ID, bundle_id="main_episode_bundle", target_buffer_count=1)

# Real short_ids used by _seed_episode below are sh_0/sh_1/sh_2 -- these
# are NOT "_placeholder" so resolve_short_deliverable_id() prefixes them
# as-is: short_sh_0 / short_sh_1 / short_sh_2.
SHORT_DELIVERABLE_IDS = ["short_sh_0", "short_sh_1", "short_sh_2"]

HYPOGEUM_MATRIX = {
    "youtube": {"main_episode": {"enabled": True}, "shorts": {"enabled": True}},
    "tiktok": {"main_episode": {"enabled": False}, "shorts": {"enabled": True, "privacy": "PUBLIC_TO_EVERYONE"}},
    "instagram": {"main_episode": {"enabled": False}, "shorts": {"enabled": True}},
    "facebook": {"main_episode": {"enabled": False}, "shorts": {"enabled": True}},
}

ALL_PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")


@pytest.fixture(autouse=True)
def _fake_video_duration(monkeypatch):
    # Every test in this file writes synthetic (non-real-video) .mp4
    # fixture files -- ffprobe cannot measure a real duration from them.
    # Real-duration measurement itself (via
    # hypogeum.artifact_compatibility._probe_duration_seconds) is proven
    # separately, against the REAL ep_f3619022 Short files, in the
    # cross-platform readiness checkpoint (see that document for the
    # measured real durations and the resulting real blockers). Here, a
    # fixed valid duration (well within every verified platform's
    # documented range) isolates the ROUTING/policy/caption/privacy
    # behavior this file actually tests from real ffprobe measurement.
    monkeypatch.setattr(
        "hypogeum.publishing.cross_platform_readiness._probe_duration_seconds",
        lambda path: 30.0,
    )


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "xplat_test.db"
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


def _setup_channel(db_path, *, platform_deliverable_policy=None, deliverable_schedule_policy=None):
    brand = create_brand_profile(name="hypogeum Brand", db_path=db_path)
    model = create_model_profile(name="hypogeum Model", db_path=db_path)
    pub = create_publishing_profile(name="hypogeum Publishing", db_path=db_path)
    create_channel_profile(
        channel_id=CHANNEL_ID, name=CHANNEL_ID,
        brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )
    if deliverable_schedule_policy is None:
        deliverable_schedule_policy = {
            "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"},
            "short_sh_0": {"day_of_week": "monday", "time_of_day": "18:00"},
            "short_sh_1": {"day_of_week": "wednesday", "time_of_day": "18:00"},
            "short_sh_2": {"day_of_week": "friday", "time_of_day": "18:00"},
        }
    create_channel_operations_profile(
        channel_id=CHANNEL_ID, channel_name=CHANNEL_ID, timezone="UTC",
        publishing_schedule={"day_of_week": "sunday", "time_of_day": "14:00"},
        enabled_platforms={p: True for p in ALL_PLATFORMS},
        deliverable_schedule_policy=deliverable_schedule_policy,
        platform_deliverable_policy=platform_deliverable_policy if platform_deliverable_policy is not None else HYPOGEUM_MATRIX,
        db_path=db_path,
    )


def _seed_episode(db_path, assets_root, episode_id=EPISODE_ID, *, n_shorts=3, with_captions=True, channel_id=CHANNEL_ID):
    conn = get_connection(db_path)
    try:
        insert_episode(conn, episode_id=episode_id, created_at=_now_iso(), topic="A Real Topic", status="complete")
        insert_episode_topic(
            conn, topic_id=f"topic_{episode_id}", channel_id=channel_id, topic_text="A topic",
            created_at=_now_iso(), updated_at=_now_iso(), episode_id=episode_id,
        )
        conn.commit()
    finally:
        conn.close()

    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True)
    shorts_titles = [{"short_id": f"short_sh_{i}", "title": f"A Real Short Title {i}"} for i in range(n_shorts)]
    shorts_captions = (
        [{"short_id": f"short_sh_{i}", "caption": f"A real, script-grounded caption for short {i}.", "source": "test_fixture", "version": "v1"} for i in range(n_shorts)]
        if with_captions else []
    )
    pkg = {
        "metadata_source": {
            "episode_title": "A Real Episode Title", "description": "A real description.",
            "shorts_titles": shorts_titles, "shorts_captions": shorts_captions,
        },
        "shorts_renders": [{"short_id": f"sh_{i}"} for i in range(n_shorts)],
    }
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps(pkg), encoding="utf-8")
    (ep_dir / f"main_{episode_id}.mp4").write_bytes(b"main bytes" * 500)
    for i in range(n_shorts):
        (ep_dir / f"short_{i}_{episode_id}.mp4").write_bytes(f"short {i}".encode() * 500)

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
    return ep_dir


def _bind_all_platforms(db_path, channel_id=CHANNEL_ID):
    create_secret_profile(
        provider="zernio", label="k", credential_type="api_key",
        credential_data="sk_test_never_real", scope="factory", db_path=db_path,
    )
    profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1")
    for platform in ALL_PLATFORMS:
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform=platform, external_account_id=f"acct_{platform}",
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
    """Records every call; can be configured to fail for a specific platform."""

    def __init__(self, fail_platforms=()):
        self.post_calls = []
        self.put_calls = []
        self.get_calls = []
        self._counter = 0
        self._fail_platforms = set(fail_platforms)
        self._posts_by_id: dict = {}

    def post(self, *, url, headers, json_body, timeout):
        self._counter += 1
        self.post_calls.append(json_body)
        requested_platforms = [p["platform"] for p in json_body["platforms"]]
        # 2026-08-22 (Zernio aggregation model): a real multi-platform post
        # returns one platforms[] entry PER requested platform in the
        # SAME response -- echo all of them, not just the first, so
        # ZernioProvider._parse_post_response(platform=p) resolves
        # correctly for every platform in an aggregated request. A
        # platform in fail_platforms gets its OWN PlatformTarget marked
        # status="failed"/errorMessage set -- a real per-platform
        # rejection WITHIN an otherwise-successful 201, never a whole-
        # request transport failure -- proving one platform's failure
        # never affects another sharing the same aggregated post.
        primary = requested_platforms[0]
        post_id = f"post_{primary}_{self._counter}"
        post_obj = {
            "_id": post_id,
            "platforms": [
                (
                    {"platform": p, "platformPostId": None, "status": "failed", "errorMessage": "simulated platform failure"}
                    if p in self._fail_platforms
                    else {"platform": p, "platformPostId": f"native_{p}_{self._counter}", "status": "published"}
                )
                for p in requested_platforms
            ],
        }
        self._posts_by_id[post_id] = post_obj
        return ZernioTransportResponse(status_code=201, json_body={"message": "created", "post": post_obj})

    def get(self, *, url, headers, timeout):
        self.get_calls.append(url)
        post_id = url.rsplit("/", 1)[-1]
        post_obj = self._posts_by_id.get(post_id)
        if post_obj is None:
            return ZernioTransportResponse(status_code=404, json_body={"error": "not found"})
        return ZernioTransportResponse(status_code=200, json_body={"post": post_obj})

    def put(self, *, url, headers, json_body, timeout):
        self._counter += 1
        self.put_calls.append(json_body)
        post_id = url.rsplit("/", 1)[-1]
        merged_platforms = json_body.get("platforms", [])
        requested_platforms = [p["platform"] for p in merged_platforms]
        if any(p in self._fail_platforms for p in requested_platforms):
            return ZernioTransportResponse(status_code=500, json_body={"error": "simulated platform failure"})
        post_obj = {
            "_id": post_id,
            "platforms": [
                {"platform": p, "platformPostId": f"native_{p}_{self._counter}", "status": "published"}
                for p in requested_platforms
            ],
        }
        self._posts_by_id[post_id] = post_obj
        return ZernioTransportResponse(status_code=200, json_body={"message": "updated", "post": post_obj})


def _run(db_path, assets_root, *, publish_transport=None, media_transport=None):
    publish_transport = publish_transport or FakePublishTransport()
    media_transport = media_transport or FakeMediaTransport()
    results = process_channel_ready_bundles(
        CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root,
        platforms=list(ALL_PLATFORMS),
        media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
        publishing_provider=ZernioProvider(transport=publish_transport),
        # This file's fixtures write fake (non-playable) media bytes --
        # production_output_gate's real ffprobe/pixel checks are covered
        # by tests/test_production_output_gate.py, not here.
        enable_production_output_gate=False,
    )
    return results, publish_transport, media_transport


class TestDeliverableRoutingMatrix:
    def test_main_episode_routes_only_to_youtube(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        results, _, _ = _run(db_path, assets_root)

        main_platforms = {r.platform for r in results if r.item_id == EPISODE_ID and _touches_main(r)}
        assert main_platforms == {"youtube"}

    def test_each_short_routes_to_all_four_platforms(self, db_path, assets_root):
        """
        2026-08-22 (Zernio aggregation model): main_episode ({youtube}
        alone) and the 3 Shorts (all 4 platforms together) are two
        DIFFERENT deliverable-groups that both happen to include
        "youtube" -- so "youtube" now legitimately appears in results
        TWICE (one ReadyBundle per group it belongs to), each pointing at
        its OWN group's ReleasePublishResult. This test checks across
        ALL of a platform's entries (not just the first, which used to be
        a safe assumption pre-aggregation), matching how the real,
        already-scheduled Hypogeum posts work: the main-episode Zernio
        post and the Shorts' Zernio post(s) are genuinely separate posts,
        even though both include a youtube platforms[] entry.
        """
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        results, _, _ = _run(db_path, assets_root)

        platforms_attempted = {r.platform for r in results}
        assert platforms_attempted == set(ALL_PLATFORMS)
        for platform in ALL_PLATFORMS:
            entries = [r for r in results if r.platform == platform]
            deliverable_ids: set = set()
            for entry in entries:
                if entry.result.publish:
                    deliverable_ids |= {d.deliverable_id for d in entry.result.publish.deliverables}
            for short_id in SHORT_DELIVERABLE_IDS:
                assert short_id in deliverable_ids, f"{short_id} missing from platform={platform}"

    def test_no_main_episode_intent_created_for_tiktok_instagram_facebook(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        _run(db_path, assets_root)

        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT platform FROM publication_intents WHERE episode_id=? AND deliverable_id='main_episode'",
                (EPISODE_ID,),
            ).fetchall()
        finally:
            conn.close()
        assert {r["platform"] for r in rows} == {"youtube"}


def _touches_main(ready_bundle):
    if not ready_bundle.result.publish:
        return False
    return any(d.deliverable_id == "main_episode" for d in ready_bundle.result.publish.deliverables)


class TestIdempotityIdentityIncludesPlatform:
    def test_platform_is_part_of_the_idempotency_identity(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        _run(db_path, assets_root)

        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT deliverable_id, platform, status FROM publication_intents WHERE episode_id=? AND deliverable_id=?",
                (EPISODE_ID, "short_sh_0"),
            ).fetchall()
        finally:
            conn.close()
        platforms_seen = {r["platform"] for r in rows}
        assert platforms_seen == set(ALL_PLATFORMS)
        assert all(r["status"] == "succeeded" for r in rows)
        # Exactly one distinct intent PER platform -- never merged/deduped
        # across platforms for the same deliverable.
        assert len(rows) == len(ALL_PLATFORMS)

    def test_succeeded_youtube_intent_does_not_suppress_other_platform_intents(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        # First run: only YouTube requested/attempted.
        results1 = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, platforms=["youtube"],
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
            enable_production_output_gate=False,
        )
        assert any(r.platform == "youtube" and r.result.status == "published" for r in results1)

        # Second run, now requesting all four platforms (simulating TikTok/
        # Instagram/Facebook being enabled after YouTube already
        # succeeded) -- TikTok must still be attempted and succeed; a
        # succeeded YouTube intent must never suppress a distinct TikTok
        # intent for the same deliverable.
        results2 = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, platforms=list(ALL_PLATFORMS),
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
            enable_production_output_gate=False,
        )
        tiktok_result = next(r for r in results2 if r.platform == "tiktok")
        assert tiktok_result.result.status == "published"

    def test_rerunning_the_same_platform_intent_cannot_create_a_duplicate(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        media_transport = FakeMediaTransport()
        _run(db_path, assets_root, media_transport=media_transport)
        _run(db_path, assets_root, media_transport=media_transport)  # repeat, same media transport (staging ledger still shared)

        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT COUNT(*) c FROM publication_intents WHERE episode_id=? AND deliverable_id=? AND platform='tiktok'",
                (EPISODE_ID, "short_sh_0"),
            ).fetchone()
        finally:
            conn.close()
        assert rows["c"] == 1


class TestPartialPlatformFailureIsolated:
    def test_one_platform_failure_does_not_affect_another(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport(fail_platforms={"tiktok"})
        results, _, _ = _run(db_path, assets_root, publish_transport=publish_transport)

        tiktok = next(r for r in results if r.platform == "tiktok")
        instagram = next(r for r in results if r.platform == "instagram")
        youtube = next(r for r in results if r.platform == "youtube")
        assert tiktok.result.status in ("partial", "blocked_publish")
        assert instagram.result.status == "published"
        assert youtube.result.status == "published"


class TestCrossPlatformReadinessBlocking:
    def test_missing_platform_binding_blocks_before_media_staging(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        # Deliberately bind only YouTube -- TikTok/Instagram/Facebook have
        # no binding at all.
        create_secret_profile(provider="zernio", label="k", credential_type="api_key", credential_data="sk_test_never_real", scope="factory", db_path=db_path)
        profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1")
        account = upsert_provider_account(db_path, provider_id="zernio", platform="youtube", external_account_id="acct_yt", provider_profile_id=profile["provider_profile_id"], status="active")
        bind_channel_provider(db_path, channel_id=CHANNEL_ID, capability="publishing", platform="youtube", provider_id="zernio", provider_profile_id=profile["provider_profile_id"], provider_account_id=account["provider_account_id"])

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        tiktok_checks = result.for_platform("tiktok")
        assert tiktok_checks and all(c.status == "fail" for c in tiktok_checks if c.deliverable_id != "main_episode")
        assert any("no enabled channel_provider_bindings" in c.reason for c in tiktok_checks)

    def test_missing_canonical_caption_blocks_before_media_staging(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root, with_captions=False)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        tiktok_short = next(c for c in result.for_platform("tiktok") if c.deliverable_id == "short_sh_0")
        assert tiktok_short.status == "fail"
        assert "caption unresolved" in tiktok_short.reason

    def test_unsupported_media_size_blocks(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        # Instagram's documented Reels size limit is 300MB -- inflate the
        # short file past it (sparse file, no real disk cost).
        oversized = ep_dir / f"short_0_{EPISODE_ID}.mp4"
        with open(oversized, "wb") as f:
            f.truncate(301 * 1024 * 1024)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        ig_short = next(c for c in result.for_platform("instagram") if c.deliverable_id == "short_sh_0")
        assert ig_short.status == "fail"
        assert "exceeds" in ig_short.reason

    def test_unsupported_duration_blocks(self, db_path, assets_root, monkeypatch):
        # Facebook Reels' documented duration range is 3-60s -- simulate a
        # real-measured 90s Short (matches the real ep_f3619022 finding:
        # every one of its 3 real Shorts genuinely exceeds Facebook's
        # limit -- see the cross-platform readiness checkpoint).
        monkeypatch.setattr(
            "hypogeum.publishing.cross_platform_readiness._probe_duration_seconds",
            lambda path: 90.0,
        )
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        fb_short = next(c for c in result.for_platform("facebook") if c.deliverable_id == "short_sh_0")
        assert fb_short.status == "fail"
        assert "duration range" in fb_short.reason
        # TikTok's range (3-600s) still permits 90s -- proving this is a
        # genuinely platform-specific failure, not a global one.
        tiktok_short = next(c for c in result.for_platform("tiktok") if c.deliverable_id == "short_sh_0")
        assert tiktok_short.status == "pass", tiktok_short.reason

    def test_unmeasurable_duration_fails_closed_never_assumes_compliant(self, db_path, assets_root, monkeypatch):
        monkeypatch.setattr(
            "hypogeum.publishing.cross_platform_readiness._probe_duration_seconds",
            lambda path: 0.0,
        )
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        tiktok_short = next(c for c in result.for_platform("tiktok") if c.deliverable_id == "short_sh_0")
        assert tiktok_short.status == "fail"
        assert "could not measure a real duration" in tiktok_short.reason

    def test_required_privacy_value_included_when_configured(self, db_path, assets_root):
        _setup_channel(db_path)  # HYPOGEUM_MATRIX sets tiktok.shorts.privacy
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        tiktok_short = next(c for c in result.for_platform("tiktok") if c.deliverable_id == "short_sh_0")
        assert tiktok_short.status == "pass", tiktok_short.reason
        assert tiktok_short.evidence["privacy_value"] == "PUBLIC_TO_EVERYONE"

    def test_missing_required_privacy_value_blocks(self, db_path, assets_root):
        policy = json.loads(json.dumps(HYPOGEUM_MATRIX))
        del policy["tiktok"]["shorts"]["privacy"]
        _setup_channel(db_path, platform_deliverable_policy=policy)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        tiktok_short = next(c for c in result.for_platform("tiktok") if c.deliverable_id == "short_sh_0")
        assert tiktok_short.status == "fail"
        assert "privacy_level" in tiktok_short.reason

    def test_existing_succeeded_intent_blocks_resubmission(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        _run(db_path, assets_root)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "existing" in yt_main.reason

    def test_no_policy_configured_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path, platform_deliverable_policy={})
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        assert result.passed is False
        assert any("no platform_deliverable_policy configured" in c.reason for c in result.checks)

    def test_platform_specific_failure_never_reported_as_global_success(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root, with_captions=False)
        _bind_all_platforms(db_path)
        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        assert result.passed is False
        # YouTube (no caption requirement) still individually passes even
        # though TikTok/Instagram/Facebook fail on missing captions.
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "pass"


class TestZeroLiveCallsAndDryRunSafety:
    def test_no_real_socket_opened(self, db_path, assets_root, monkeypatch):
        def _fail_socket(*a, **k):
            raise AssertionError("no real socket may be opened by this test")
        monkeypatch.setattr(socket.socket, "connect", _fail_socket)
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        results, _, _ = _run(db_path, assets_root)
        assert any(r.result.status == "published" for r in results)

    def test_dry_run_worker_gate_produces_zero_calls_across_all_platforms(self, db_path, assets_root):
        # Uses the existing, already-tested run_worker.py dry_run gate
        # (checked BEFORE AUTO_ZERNIO_PUBLISHING_ENABLED, unconditionally)
        # -- extended here only to confirm it holds even with all four
        # platforms configured/bound, not just the single-platform case
        # test_run_worker_publishing_wiring.py already covers.
        import uuid
        from unittest.mock import MagicMock
        from run_worker import run_iteration
        from hypogeum.config import Config

        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        config = MagicMock(spec=Config)
        config.AUTO_ZERNIO_PUBLISHING_ENABLED = True
        config.AUTO_DISCOVERY_ENABLED = False

        result = run_iteration(
            worker_id=str(uuid.uuid4()), db_path=db_path, dry_run=True, channel_id=None,
            config=config, iteration=1, assets_root=assets_root,
        )
        assert result["publishing"] == {"skipped_reason": "dry_run"}


class TestVerifiedContractsCoverRequiredPlatforms:
    def test_tiktok_instagram_facebook_have_verified_contracts(self):
        for platform in ("tiktok", "instagram", "facebook"):
            contract = ZERNIO_PLATFORM_CONTRACTS[platform]
            assert contract.verified is True
            assert contract.caption_field == "content"


class TestCaptionsAndPlatformSettingsReachTheRealRequest:
    def test_canonical_caption_reaches_real_content_field(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport)

        tiktok_calls = [c for c in publish_transport.post_calls if any(p["platform"] == "tiktok" for p in c["platforms"])]
        assert len(tiktok_calls) == 3
        for call in tiktok_calls:
            assert call["content"].startswith("A real, script-grounded caption for short")

    def test_missing_caption_never_sends_a_short_to_a_non_youtube_platform(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root, with_captions=False)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        results, _, _ = _run(db_path, assets_root, publish_transport=publish_transport)

        # process_channel_ready_bundles() itself does not consult
        # cross_platform_readiness -- that gate is the caller's
        # responsibility (see Part 4.1 audit in the checkpoint). This
        # test instead proves the LOWER-level guarantee already in place:
        # a Short whose caption never resolved carries no content at
        # all, never a fabricated or server-defaulted one.
        tiktok_calls = [c for c in publish_transport.post_calls if any(p["platform"] == "tiktok" for p in c["platforms"])]
        for call in tiktok_calls:
            assert "content" not in call or not call.get("content")

    def test_tiktok_privacy_setting_reaches_the_real_request(self, db_path, assets_root):
        _setup_channel(db_path)  # HYPOGEUM_MATRIX configures tiktok.shorts.privacy = PUBLIC_TO_EVERYONE
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport)

        tiktok_calls = [c for c in publish_transport.post_calls if any(p["platform"] == "tiktok" for p in c["platforms"])]
        assert len(tiktok_calls) == 3
        for call in tiktok_calls:
            assert call["tiktokSettings"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
            # The contract's own required consent flags are included too
            # -- never left to a server-side default for an autonomous
            # submission.
            assert call["tiktokSettings"]["content_preview_confirmed"] is True
            assert call["tiktokSettings"]["express_consent_given"] is True

    def test_tiktok_settings_never_attached_to_instagram_or_facebook_platform_entries(self, db_path, assets_root):
        """
        2026-08-22 (Zernio aggregation model): instagram/facebook/tiktok
        now legitimately share ONE request body (one deliverable, one
        group, one POST per the aggregation model) -- so `tiktokSettings`
        DOES legitimately appear at the request ROOT whenever tiktok is
        one of the group's platforms; that alone is correct per the
        verified contract ("merged into each platform's
        platformSpecificData" server-side), not a leak. What must still
        never happen is tiktokSettings-shaped data attached to
        instagram/facebook's OWN platforms[] entries specifically -- this
        checks THAT, the real isolation guarantee, instead of the
        pre-aggregation assumption that whole request bodies stay
        single-platform.
        """
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport)

        calls_with_tiktok = [c for c in publish_transport.post_calls if any(p["platform"] == "tiktok" for p in c["platforms"])]
        assert calls_with_tiktok, "expected at least one aggregated call including tiktok"
        for call in calls_with_tiktok:
            for platform in ("instagram", "facebook"):
                entry = next((p for p in call["platforms"] if p["platform"] == platform), None)
                if entry is not None:
                    assert "tiktokSettings" not in entry
                    assert "privacy_level" not in entry

    def test_scheduled_timestamp_and_timezone_reach_each_platform_call(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport)

        for call in publish_transport.post_calls:
            assert call.get("scheduledFor") is not None
            assert call["platforms"][0]["accountId"] == f"acct_{call['platforms'][0]['platform']}"


class TestCrossPlatformReadinessWiredIntoTheRealWritePath:
    """
    Part 4.1: check_cross_platform_readiness() must be consulted by
    process_channel_ready_bundles() itself, BEFORE staging/presign/POST
    for each (deliverable, platform) -- not only reachable as an
    independently-called checker.
    """

    def test_missing_caption_produces_zero_presign_and_post_calls_for_that_platform(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root, with_captions=False)
        _bind_all_platforms(db_path)
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport, media_transport=media_transport)

        # No TikTok/Instagram/Facebook presign or post call at all -- the
        # real write path itself refused these combinations before ever
        # reaching staging, not merely an independent checker reporting
        # failure on the side.
        for platform in ("tiktok", "instagram", "facebook"):
            assert not any(any(p["platform"] == platform for p in c["platforms"]) for c in publish_transport.post_calls)
        thumbnail_and_video_only = {c["filename"] for c in media_transport.presign_calls}
        assert not any("short" in f and f not in (f"short_{i}_{EPISODE_ID}.mp4" for i in range(3)) for f in thumbnail_and_video_only)
        # YouTube is unaffected -- captions are not required there.
        assert any(any(p["platform"] == "youtube" for p in c["platforms"]) for c in publish_transport.post_calls)

    def test_missing_tiktok_privacy_produces_zero_tiktok_writes_only(self, db_path, assets_root):
        policy = json.loads(json.dumps(HYPOGEUM_MATRIX))
        del policy["tiktok"]["shorts"]["privacy"]
        _setup_channel(db_path, platform_deliverable_policy=policy)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport)

        assert not any(any(p["platform"] == "tiktok" for p in c["platforms"]) for c in publish_transport.post_calls)
        assert any(any(p["platform"] == "instagram" for p in c["platforms"]) for c in publish_transport.post_calls)
        assert any(any(p["platform"] == "facebook" for p in c["platforms"]) for c in publish_transport.post_calls)


class TestBufferPlatformSpecificCompletion:
    """
    Part 4.2: TikTok/Instagram/Facebook must become 'complete' once their
    OWN required deliverables (Shorts only) succeed -- never blocked
    waiting for a main_episode intent on a platform that never routes it.
    """

    def test_tiktok_is_complete_with_shorts_only_no_main_episode_intent_required(self, db_path, assets_root):
        from hypogeum.buffer import DeliverableBundleDefinition as _BD, gather_channel_demand_items

        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        _run(db_path, assets_root)  # publishes everything: youtube (4) + tiktok/instagram/facebook (3 shorts each)

        conn = get_connection(db_path)
        try:
            tiktok_main = conn.execute(
                "SELECT COUNT(*) c FROM publication_intents WHERE episode_id=? AND platform='tiktok' AND deliverable_id='main_episode'",
                (EPISODE_ID,),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert tiktok_main == 0, "no main_episode intent should ever exist for tiktok"

        items = gather_channel_demand_items(
            _BD(channel_id=CHANNEL_ID, bundle_id="main_episode_bundle", target_buffer_count=1),
            item_ids=[EPISODE_ID], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root, platforms=("tiktok",),
        )
        assert items[0].already_published is True, "tiktok must be complete with only its 3 Shorts succeeded -- no main_episode required"


class TestAggregationThroughTheRealTrigger:
    """
    2026-08-22 (Zernio aggregation model) -- integration-level proofs
    through process_channel_ready_bundles() itself (the real autonomous
    trigger run_worker.py invokes), not only direct ZernioProvider unit
    tests (see tests/test_publishing_zernio.py's
    TestPublishMultiPlatformSinglePost/PartialFailureRetry for those).
    """

    def test_one_short_targeting_four_platforms_causes_exactly_one_post(self, db_path, assets_root):
        """
        2026-08-22: request identity must never be inferred from platform
        order or a tautological condition. Each of the 4 canonical
        deliverables in _seed_episode resolves a DISTINCT, real title
        ("A Real Episode Title" for main; "A Real Short Title {0,1,2}" for
        each Short -- see zernio_channel_release_trigger.py's
        build_publish_package_from_episode_dir(), which threads
        DeliverableFile.title into both the POST's top-level "title" and
        every mediaItems[].title via _serialize_media_item()). That title
        is used here as the unambiguous per-deliverable correlation key,
        cross-checked against mediaItems[].title independently, so a
        Short's media can never be silently attributed to another
        deliverable's POST.
        """
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport)

        expected_titles = {
            "main_episode": "A Real Episode Title",
            "short_sh_0": "A Real Short Title 0",
            "short_sh_1": "A Real Short Title 1",
            "short_sh_2": "A Real Short Title 2",
        }

        # Exactly 4 total POST calls for main(1) + 3 Shorts(1 each), never
        # 4 (main) + 12 (3 shorts x 4 platforms each) = 16 the pre-
        # aggregation architecture would have produced.
        assert len(publish_transport.post_calls) == 4, (
            f"expected exactly 4 total POST calls (1 main + 3 Shorts, each Short "
            f"aggregated across all 4 platforms in ONE call), got {len(publish_transport.post_calls)}"
        )

        calls_by_title: dict = {}
        for call in publish_transport.post_calls:
            title = call.get("title")
            assert title in expected_titles.values(), f"unexpected/untraceable POST title: {title!r}"
            assert title not in calls_by_title, (
                f"more than one POST shares title {title!r} -- deliverable identity is ambiguous"
            )
            calls_by_title[title] = call
            # mediaItems[].title must independently agree with the POST's
            # own top-level title -- proves the media inside this POST
            # belongs to the SAME deliverable this POST is for, never a
            # different one bundled in by mistake.
            for item in call.get("mediaItems", []):
                assert item.get("title") == title, (
                    f"mediaItems title {item.get('title')!r} disagrees with enclosing POST "
                    f"title {title!r} -- media may belong to a different deliverable"
                )

        assert set(calls_by_title) == set(expected_titles.values()), (
            "did not find exactly one POST per canonical deliverable "
            f"(main_episode + 3 Shorts): got titles {sorted(calls_by_title)}"
        )

        main_call = calls_by_title[expected_titles["main_episode"]]
        assert {p["platform"] for p in main_call["platforms"]} == {"youtube"}, (
            "main episode POST must contain YouTube only"
        )

        for short_id in ("short_sh_0", "short_sh_1", "short_sh_2"):
            short_call = calls_by_title[expected_titles[short_id]]
            assert {p["platform"] for p in short_call["platforms"]} == set(ALL_PLATFORMS), (
                f"Short {short_id} POST did not target exactly the four intended platforms"
            )

    def test_the_aggregated_short_request_contains_all_four_platform_account_entries(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport)

        short_call = next(
            c for c in publish_transport.post_calls
            if any(p["platform"] == "tiktok" for p in c["platforms"])
        )
        sent_platforms = {p["platform"] for p in short_call["platforms"]}
        assert sent_platforms == set(ALL_PLATFORMS)
        for entry in short_call["platforms"]:
            assert entry["accountId"] == f"acct_{entry['platform']}"

    def test_title_and_content_are_non_empty_in_the_real_aggregated_request(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport)

        for call in publish_transport.post_calls:
            assert (call.get("title") or "").strip() != "", f"empty title in real request: {call}"
            assert (call.get("content") or "").strip() != "", f"empty content in real request: {call}"

    def test_correct_media_and_thumbnail_references_included(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        publish_transport = FakePublishTransport()
        _run(db_path, assets_root, publish_transport=publish_transport)

        main_call = next(c for c in publish_transport.post_calls if c["platforms"][0]["platform"] == "youtube" and len(c["platforms"]) == 1)
        assert main_call["mediaItems"], "main episode call must carry its video mediaItem"
        assert main_call["mediaItems"][0]["thumbnail"], "main episode call must carry its canonical thumbnail reference"

    def test_missing_title_causes_zero_external_calls(self, db_path, assets_root):
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        # Overwrite the episode_title with an empty string -- unresolvable title.
        ep_dir = assets_root / "episodes" / EPISODE_ID
        pkg = json.loads((ep_dir / f"final_render_package_{EPISODE_ID}.json").read_text())
        pkg["metadata_source"]["episode_title"] = ""
        (ep_dir / f"final_render_package_{EPISODE_ID}.json").write_text(json.dumps(pkg), encoding="utf-8")
        _bind_all_platforms(db_path)
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        results, _, _ = _run(db_path, assets_root, publish_transport=publish_transport, media_transport=media_transport)

        assert all(r.result.status == "blocked_metadata" for r in results)
        assert media_transport.presign_calls == []
        assert publish_transport.post_calls == []
        assert publish_transport.put_calls == []

    def test_production_output_gate_failure_causes_zero_staging_post_and_put_calls(self, db_path, assets_root, monkeypatch):
        from hypogeum.agents.production_output_gate import GateCheckResult, ProductionOutputGateResult

        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        def _fake_fail(episode_id, assets_root_arg, config, log):
            return ProductionOutputGateResult(
                passed=False, episode_id=episode_id, evaluated_at="",
                checks=[GateCheckResult(capability="D_SHORTS_CAPTIONS", status="fail", reason="simulated burned-caption failure")],
            )

        monkeypatch.setattr(
            "hypogeum.publishing.zernio_channel_release_trigger.run_production_output_gate", _fake_fail,
        )
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root,
            platforms=list(ALL_PLATFORMS),
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
            enable_production_output_gate=True,
        )

        assert results, "expected a blocked result for every requested platform"
        assert all(r.result.status == "blocked_output_gate" for r in results)
        assert media_transport.presign_calls == []
        assert publish_transport.post_calls == []
        assert publish_transport.put_calls == []

    def test_partial_retry_through_the_real_trigger_targets_the_same_provider_post(self, db_path, assets_root):
        """First poll: only youtube+tiktok enabled. Second poll (simulating
        instagram/facebook being enabled afterward): the real trigger must
        add them to the SAME existing Zernio post via GET+PUT, never a
        second POST -- proven end-to-end through process_channel_ready_bundles(),
        not only ZernioProvider directly."""
        partial_policy = json.loads(json.dumps(HYPOGEUM_MATRIX))
        partial_policy["instagram"]["shorts"]["enabled"] = False
        partial_policy["facebook"]["shorts"]["enabled"] = False
        _setup_channel(db_path, platform_deliverable_policy=partial_policy)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        first = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root,
            platforms=list(ALL_PLATFORMS),
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
            enable_production_output_gate=False,
        )
        assert any(r.platform == "tiktok" and r.result.status == "published" for r in first)
        post_calls_after_first = len(publish_transport.post_calls)

        # Second poll: full policy now enables instagram/facebook too.
        from hypogeum.channel_config import update_channel_operations_profile
        update_channel_operations_profile(CHANNEL_ID, db_path=db_path, platform_deliverable_policy=HYPOGEUM_MATRIX)
        second = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root,
            platforms=list(ALL_PLATFORMS),
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
            enable_production_output_gate=False,
        )
        assert any(r.platform == "instagram" and r.result.status == "published" for r in second)
        # No NEW post call was made for the retry -- only a GET+PUT pair.
        assert len(publish_transport.post_calls) == post_calls_after_first, "retry must never create a second POST"
        assert len(publish_transport.put_calls) > 0, "retry must add the new platforms via PUT"
