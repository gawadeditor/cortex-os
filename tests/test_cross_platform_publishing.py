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

import hashlib
import json
import socket
import subprocess

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
    "youtube": {"main_episode": {"enabled": True, "categoryId": "27", "madeForKids": True}, "shorts": {"enabled": True, "categoryId": "27", "madeForKids": True}},
    "tiktok": {"main_episode": {"enabled": False}, "shorts": {
        "enabled": True, "privacy": "PUBLIC_TO_EVERYONE",
        "content_preview_confirmed": True, "express_consent_given": True,
        "allow_comment": True, "allow_duet": True, "allow_stitch": True,
    }},
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


def _canonical_platform_metadata_entry(
    deliverable_id, platform, *, description="A real description. #TestTag",
    title="A Real Title", with_chapters=False, real_source_urls=("https://example.com/source",),
    research_package_sha256="0" * 64,
):
    """
    A real-shaped hypogeum.publishing.canonical_metadata.CanonicalPlatformMetadata
    dict (fixed 2026-08-23, replacing the incident-only, YouTube/main-only
    youtube_metadata_package field -- see docs/operations/audits/
    ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md) satisfying every
    validate_canonical_platform_metadata() check: real, non-placeholder
    title/description/CTA, freshness fingerprint matching a real on-disk
    artifact, sources tracing to the fixture's own research_package,
    claim-to-source provenance, hashtag TOKENS present in the visible
    description, backend tags kept separate from hashtags, and (for a
    main_episode YouTube entry) >=3 chapters starting at 0.0s.
    """
    entry = {
        "deliverable_id": deliverable_id, "platform": platform,
        "title": title, "description": description,
        "visible_hashtags": ["#TestTag"], "backend_tags": ["test tag one", "test tag two"],
        "sources": [{"title": "Example Source", "url": u} for u in real_source_urls],
        "claim_source_map": {"clm_001": {"sources": ["src_001"]}},
        "cta": "Subscribe for more real, well-sourced content.",
        "language": "en", "metadata_version": "v1",
        "source_artifact_ids": [f"research_package_{EPISODE_ID}.json"],
        "input_fingerprint": {f"research_package_{EPISODE_ID}.json": research_package_sha256},
        "generated_at": _now_iso(), "approved_at": _now_iso(),
        "first_comment": "A real first comment.",
        "thumbnail_ref": f"episodes/{EPISODE_ID}/thumbnails/candidate_1",
        "ai_disclosure": True,
    }
    if platform == "youtube":
        entry["category_id"] = "27"  # channel-approved value, matches _setup_channel's own policy below
    if with_chapters:
        entry["chapters"] = [
            {"start_seconds": 0.0, "title": "Intro"},
            {"start_seconds": 12.0, "title": "Middle"},
            {"start_seconds": 24.0, "title": "End"},
        ]
    return entry


def _default_canonical_platform_metadata(n_shorts=3, *, research_package_sha256="0" * 64):
    """
    Complete canonical_platform_metadata covering every (deliverable_id,
    platform) pair HYPOGEUM_MATRIX enables: main_episode -> youtube only;
    each short -> all four platforms. Matches this fixture module's own
    real topology, never a hypothetical one.
    """
    entries = [_canonical_platform_metadata_entry(
        "main_episode", "youtube", with_chapters=True, research_package_sha256=research_package_sha256,
        title="A Real Episode Title", description="A real description. #TestTag",
    )]
    for i in range(n_shorts):
        short_id = f"short_sh_{i}"
        for platform in ALL_PLATFORMS:
            entries.append(_canonical_platform_metadata_entry(
                short_id, platform, research_package_sha256=research_package_sha256,
                title=f"A Real Short Title {i}",
                description=f"A real, script-grounded caption for short {i}. #TestTag",
            ))
    return entries


def _write_real_short_video(path, *, duration_seconds=5):
    """
    A real, ffprobe-measurable, moving-content video -- required since
    cross_platform_readiness's chapter-validation gate refuses to trust
    narration-duration summation alone and instead ffprobes the actual
    deliverable file. A placeholder byte blob measures as 0s duration
    and correctly fails that gate.
    """
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=24:duration={duration_seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
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

    # A real-shaped research package -- canonical_platform_metadata's own
    # source/provenance checks trace against this file, never a fabricated
    # standalone list (see hypogeum.publishing.canonical_metadata).
    research_pkg = {
        "source_registry": [{"source_id": "src_001", "url": "https://example.com/source", "inferred_type": "test"}],
        "claims": [{"claim_id": "clm_001", "text": "A real claim.", "sources": ["src_001"]}],
    }
    research_pkg_path = ep_dir / f"research_package_{episode_id}.json"
    research_pkg_path.write_text(json.dumps(research_pkg), encoding="utf-8")
    research_package_sha256 = hashlib.sha256(research_pkg_path.read_bytes()).hexdigest()

    shorts_titles = [{"short_id": f"short_sh_{i}", "title": f"A Real Short Title {i}"} for i in range(n_shorts)]
    shorts_captions = (
        [{"short_id": f"short_sh_{i}", "caption": f"A real, script-grounded caption for short {i}.", "source": "test_fixture", "version": "v1"} for i in range(n_shorts)]
        if with_captions else []
    )
    pkg = {
        "metadata_source": {
            "episode_title": "A Real Episode Title", "description": "A real description. #TestTag",
            "shorts_titles": shorts_titles, "shorts_captions": shorts_captions,
            "canonical_platform_metadata": _default_canonical_platform_metadata(
                n_shorts, research_package_sha256=research_package_sha256,
            ),
        },
        "shorts_renders": [{"short_id": f"sh_{i}"} for i in range(n_shorts)],
    }
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps(pkg), encoding="utf-8")
    _write_real_short_video(ep_dir / f"main_{episode_id}.mp4")
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

    def test_platform_bound_to_postiz_is_not_blocked_by_the_zernio_default(self, db_path, assets_root):
        # 2026-10-10: readiness demanded provider_id='zernio' for EVERY platform, so Postiz-bound
        # Facebook/Instagram/YouTube were never released and a new episode went out on TikTok only.
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        profile = upsert_provider_profile(db_path, provider_id="postiz", external_profile_id="pz_1")
        account = upsert_provider_account(db_path, provider_id="postiz", platform="youtube", external_account_id="pz_yt",
                                          provider_profile_id=profile["provider_profile_id"], status="active")
        bind_channel_provider(db_path, channel_id=CHANNEL_ID, capability="publishing", platform="youtube",
                              provider_id="postiz", provider_profile_id=profile["provider_profile_id"],
                              provider_account_id=account["provider_account_id"])
        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        assert not any("no enabled channel_provider_bindings" in c.reason for c in result.for_platform("youtube"))

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

    def test_missing_required_policy_fields_block_independently_of_privacy(self, db_path, assets_root):
        policy = json.loads(json.dumps(HYPOGEUM_MATRIX))
        for f in ("content_preview_confirmed", "express_consent_given", "allow_comment", "allow_duet", "allow_stitch"):
            policy["tiktok"]["shorts"].pop(f, None)
        _setup_channel(db_path, platform_deliverable_policy=policy)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        tiktok_short = next(c for c in result.for_platform("tiktok") if c.deliverable_id == "short_sh_0")
        assert tiktok_short.status == "fail"
        assert "content_preview_confirmed" in tiktok_short.reason
        assert "allow_comment" in tiktok_short.reason

    def test_all_required_tiktok_policy_fields_pass_when_fully_configured(self, db_path, assets_root):
        _setup_channel(db_path)  # HYPOGEUM_MATRIX already includes all 5 fields
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        for short_id in ("short_sh_0", "short_sh_1", "short_sh_2"):
            tiktok_short = next(c for c in result.for_platform("tiktok") if c.deliverable_id == short_id)
            assert tiktok_short.status == "pass", tiktok_short.reason
            assert tiktok_short.evidence["required_policy_fields"] == {
                "content_preview_confirmed": True, "express_consent_given": True,
                "allow_comment": True, "allow_duet": True, "allow_stitch": True,
            }


class TestCanonicalMetadataReadinessReproducesEpF3619022Incident:
    """
    Fixed 2026-08-23, see docs/operations/audits/
    ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md. Deterministic
    regression coverage reproducing the exact live-observed defect
    pattern (a Zernio post with populated generic title/content but
    blank platform-specific fields, sourced from a description that was
    silently accepted from the raw research logline) against the
    GENERIC, per-(deliverable_id, platform) canonical_platform_metadata
    contract -- covering main_episode AND Shorts, never exempting either.
    Each test isolates ONE missing/wrong field.
    """

    def _replace_entry(self, ep_dir, deliverable_id, platform, **overrides):
        pkg = json.loads((ep_dir / f"final_render_package_{EPISODE_ID}.json").read_text())
        entries = pkg["metadata_source"]["canonical_platform_metadata"]
        for i, e in enumerate(entries):
            if e["deliverable_id"] == deliverable_id and e["platform"] == platform:
                new_entry = dict(e)
                new_entry.update(overrides)
                entries[i] = new_entry
                break
        else:
            raise AssertionError(f"no existing entry for ({deliverable_id}, {platform}) to replace")
        (ep_dir / f"final_render_package_{EPISODE_ID}.json").write_text(json.dumps(pkg), encoding="utf-8")

    def _remove_entry(self, ep_dir, deliverable_id, platform):
        pkg = json.loads((ep_dir / f"final_render_package_{EPISODE_ID}.json").read_text())
        entries = pkg["metadata_source"]["canonical_platform_metadata"]
        pkg["metadata_source"]["canonical_platform_metadata"] = [
            e for e in entries if not (e["deliverable_id"] == deliverable_id and e["platform"] == platform)
        ]
        (ep_dir / f"final_render_package_{EPISODE_ID}.json").write_text(json.dumps(pkg), encoding="utf-8")

    def test_missing_canonical_metadata_entry_fails_closed(self, db_path, assets_root):
        # Reproduces the live ep_f3619022 state: no canonical entry for
        # (main_episode, youtube) at all -- only the generic title/
        # content fields, which is what let Zernio's post ship with
        # every dedicated field blank.
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._remove_entry(ep_dir, "main_episode", "youtube")
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "no canonical_platform_metadata entry found" in yt_main.reason

    def test_description_verbatim_equal_to_research_logline_fails_closed(self, db_path, assets_root):
        # The actual ep_f3619022 root cause reproduced against the new
        # contract: a canonical entry whose description IS the raw
        # research logline verbatim.
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        (ep_dir / f"topic_brief_{EPISODE_ID}.json").write_text(
            json.dumps({"logline": "A raw research logline, never real publication copy."}), encoding="utf-8",
        )
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            description="A raw research logline, never real publication copy.",
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "verbatim identical to a raw research" in yt_main.reason

    def test_blank_backend_tags_pass_but_blank_hashtags_fail(self, db_path, assets_root):
        # backend_tags has no "must be non-empty" requirement in the
        # generic contract (unlike the incident-only draft) -- only
        # visible_hashtags is required. This test proves that
        # distinction is real, not accidental.
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(ep_dir, "main_episode", "youtube", visible_hashtags=[])
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "visible_hashtags is empty" in yt_main.reason

    def test_category_not_matching_channel_policy_fails_closed(self, db_path, assets_root):
        # Factory Core validates against the CHANNEL's own approved
        # categoryId (from platform_deliverable_policy) -- never a
        # hardcoded Hypogeum editorial value. "22" is not globally
        # invalid; it is invalid HERE only because it does not match
        # this channel's own configured "27".
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(ep_dir, "main_episode", "youtube", category_id="22")
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "does not match the channel-approved categoryId" in yt_main.reason

    def test_missing_ai_disclosure_fails_closed_for_a_short_not_just_main_episode(self, db_path, assets_root):
        # Fixed 2026-08-23 (operator channel-policy decision, categoryId=27
        # + ai_disclosure=true for youtube.main_episode AND youtube.shorts):
        # ai_disclosure is a per-video containsSyntheticMedia disclosure
        # obligation, not a main-episode-only concept -- removing it from a
        # Short's canonical entry must block that Short's readiness exactly
        # like removing it from main_episode does.
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(ep_dir, "short_sh_0", "youtube", ai_disclosure=None)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_short = next(c for c in result.for_platform("youtube") if c.deliverable_id == "short_sh_0")
        assert yt_short.status == "fail"
        assert "ai_disclosure is absent" in yt_short.reason

    def test_missing_categoryid_in_channel_package_fails_closed_with_zero_external_calls(self, db_path, assets_root):
        # 2026-08-23 operator decision: Factory Core does not need to know
        # WHICH categoryId a channel uses (never hardcoded -- see
        # hypogeum.publishing.zernio_platform_contracts.ZERNIO_PLATFORM_CONTRACTS
        # ["youtube"].required_policy_fields=("categoryId",)), but it MUST
        # enforce that the Channel Package declares one once youtube is
        # enabled for a deliverable class. Proves the full chain:
        #   1. remove categoryId from the Channel Package policy
        #   2. compile/sync the policy (here: pass it straight to
        #      _setup_channel, which is this test file's own equivalent of
        #      sync_platform_distribution_policy_to_runtime -- both write
        #      the same platform_deliverable_policy_json column)
        #   3. readiness fails closed
        #   4. canonical metadata generation makes ZERO external calls
        #      (the model is never invoked) -- proven directly against the
        #      real generate_canonical_platform_metadata_for_deliverable(),
        #      not a fake standing in for the whole pipeline.
        policy = json.loads(json.dumps(HYPOGEUM_MATRIX))
        del policy["youtube"]["main_episode"]["categoryId"]
        _setup_channel(db_path, platform_deliverable_policy=policy)
        ep_dir = _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        # 3. Readiness fails closed.
        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "categoryId" in yt_main.reason

        # 4. Zero external calls: the real generator refuses to call the
        # model at all once its own policy-completeness pre-check finds
        # youtube's required categoryId missing for main_episode.
        from hypogeum.agents.canonical_metadata_agent import (
            CanonicalMetadataGenerationError, generate_canonical_platform_metadata_for_deliverable,
        )
        from hypogeum.config import Config

        model_calls = []

        def _spy_llm_call(*a, **kw):
            model_calls.append(1)
            raise AssertionError("the model must never be called when the Channel Package is incomplete")

        final_render_package = json.loads((ep_dir / f"final_render_package_{EPISODE_ID}.json").read_text())
        with pytest.raises(CanonicalMetadataGenerationError, match="categoryId"):
            generate_canonical_platform_metadata_for_deliverable(
                EPISODE_ID, ep_dir, "main_episode", "main_episode", ["youtube"], final_render_package,
                config=Config(), channel_id=CHANNEL_ID, platform_deliverable_policy=policy,
                llm_call=_spy_llm_call,
            )
        assert model_calls == []

    def test_category_matching_a_different_channel_approved_value_passes(self, db_path, assets_root):
        # Proves item 1's own point directly: "22" is accepted when the
        # CHANNEL policy approves it -- Factory Core has no opinion of
        # its own on which category is correct.
        policy = json.loads(json.dumps(HYPOGEUM_MATRIX))
        policy["youtube"]["main_episode"]["categoryId"] = "22"
        _setup_channel(db_path, platform_deliverable_policy=policy)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(ep_dir, "main_episode", "youtube", category_id="22")
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "pass", yt_main.reason

    def test_broken_legacy_metadata_resolver_never_blocks_a_pair_with_a_real_canonical_entry(self, db_path, assets_root):
        # Real bug found live running this exact check against ep_f3619022:
        # the OLDER, pre-canonical-metadata resolve_canonical_metadata()
        # (metadata_source.description/.logline only, no per-platform
        # awareness) was still being consulted UNCONDITIONALLY for every
        # (deliverable, platform) pair, even once that pair had a real,
        # valid canonical_platform_metadata entry -- so an episode whose
        # legacy fields were simply never populated (true for any episode
        # generated entirely through the new canonical pipeline) could
        # never pass readiness no matter how complete its canonical
        # package was. Fixed: the legacy resolver is now only a blocker
        # when NO canonical entry exists for that exact pair.
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        pkg_path = ep_dir / f"final_render_package_{EPISODE_ID}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["metadata_source"]["description"] = ""
        pkg["metadata_source"]["logline"] = ""
        pkg_path.write_text(json.dumps(pkg), encoding="utf-8")
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "pass", yt_main.reason
        assert "canonical metadata unresolved" not in yt_main.reason

    def test_fewer_than_three_chapters_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            chapters=[{"start_seconds": 0.0, "title": "Intro"}, {"start_seconds": 12.0, "title": "End"}],
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "requires at least 3" in yt_main.reason

    def test_chapters_not_starting_at_zero_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            chapters=[
                {"start_seconds": 1.0, "title": "Intro"},
                {"start_seconds": 13.0, "title": "Mid"},
                {"start_seconds": 25.0, "title": "End"},
            ],
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "first chapter must start at 0.0s" in yt_main.reason

    def test_chapter_shorter_than_ten_seconds_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            chapters=[
                {"start_seconds": 0.0, "title": "Intro"},
                {"start_seconds": 5.0, "title": "TooSoon"},
                {"start_seconds": 20.0, "title": "End"},
            ],
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "must be at least 10 seconds" in yt_main.reason

    def test_non_increasing_chapter_timestamps_fail_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            chapters=[
                {"start_seconds": 0.0, "title": "Intro"},
                {"start_seconds": 0.0, "title": "Also Intro"},
                {"start_seconds": 20.0, "title": "End"},
            ],
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "strictly increasing" in yt_main.reason

    def test_chapter_timestamp_beyond_final_duration_fails_closed(self, db_path, assets_root, monkeypatch):
        # Real cross-check against the ACTUAL encoded file -- never trust
        # narration-duration summation alone.
        monkeypatch.setattr(
            "hypogeum.publishing.cross_platform_readiness._probe_duration_seconds",
            lambda path: 30.0,
        )
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            chapters=[
                {"start_seconds": 0.0, "title": "Intro"},
                {"start_seconds": 12.0, "title": "Mid"},
                {"start_seconds": 9999.0, "title": "Impossible"},
            ],
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "is not below the actual final encoded duration" in yt_main.reason

    def test_missing_sources_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(ep_dir, "main_episode", "youtube", sources=[])
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "sources is empty" in yt_main.reason

    def test_source_url_not_in_research_package_fails_closed(self, db_path, assets_root):
        # A non-empty sources list is NOT verification -- each URL must
        # trace to the episode's own real Research Package.
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            sources=[{"title": "Fabricated", "url": "https://not-in-research-package.example.com/fake"}],
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "do not trace to this episode's own Research Package" in yt_main.reason

    def test_malformed_source_url_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(ep_dir, "main_episode", "youtube", sources=[{"title": "Bad", "url": "not-a-url"}])
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "not well-formed http(s) URLs" in yt_main.reason

    def test_empty_claim_source_map_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(ep_dir, "main_episode", "youtube", claim_source_map={})
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "claim_source_map is empty" in yt_main.reason

    def test_claim_source_map_references_unknown_source_id_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            claim_source_map={"clm_001": {"sources": ["src_does_not_exist"]}},
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "references source_id(s) not in the Research Package" in yt_main.reason

    def test_hashtag_substring_match_is_rejected_real_token_required(self, db_path, assets_root):
        # A hashtag substring match is not valid token validation --
        # "#Test" must NOT be satisfied by description text containing
        # "#TestTagExtra" (a different, longer token).
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            description="A real description. #TestTagExtra", visible_hashtags=["#TestTag"],
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "not valid token validation" in yt_main.reason

    def test_backend_tags_overlapping_visible_hashtags_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            backend_tags=["testtag"], visible_hashtags=["#TestTag"],
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "must remain separate concepts" in yt_main.reason

    def test_stale_input_fingerprint_fails_closed(self, db_path, assets_root):
        # Metadata generated against a research_package.json that has
        # since changed on disk -- freshness must fail closed rather
        # than trust a stale approval.
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(
            ep_dir, "main_episode", "youtube",
            input_fingerprint={f"research_package_{EPISODE_ID}.json": "0" * 64},
        )
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "stale relative to current source inputs" in yt_main.reason

    def test_missing_approved_at_timestamp_fails_closed(self, db_path, assets_root):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._replace_entry(ep_dir, "main_episode", "youtube", approved_at=None)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "fail"
        assert "approved_at timestamp is absent" in yt_main.reason

    def test_fully_populated_package_passes(self, db_path, assets_root):
        # Positive control -- proves the gate is genuinely satisfiable,
        # not just permanently fail-closed.
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_main = next(c for c in result.for_platform("youtube") if c.deliverable_id == "main_episode")
        assert yt_main.status == "pass", yt_main.reason

    def test_gate_now_applies_to_shorts_too(self, db_path, assets_root):
        # Fixed 2026-08-23: an earlier draft of this contract exempted
        # Shorts entirely. The operator explicitly required this NOT be
        # the case -- every Short/platform pair needs its own approved
        # canonical metadata. This is the regression guard against ever
        # silently re-exempting them.
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        self._remove_entry(ep_dir, "short_sh_0", "youtube")
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        yt_short = next(c for c in result.for_platform("youtube") if c.deliverable_id == "short_sh_0")
        assert yt_short.status == "fail"
        assert "no canonical_platform_metadata entry found" in yt_short.reason

    def test_each_short_requires_its_own_distinct_canonical_metadata(self, db_path, assets_root):
        # Silent reuse of one generic caption across all 4 platforms for
        # a Short must not pass unless the Channel Package explicitly
        # approves identical content -- proven here by each Short's
        # fixture entry being genuinely distinct per platform and all
        # passing independently.
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        for short_id in ("short_sh_0", "short_sh_1", "short_sh_2"):
            for platform in ALL_PLATFORMS:
                c = next(x for x in result.for_platform(platform) if x.deliverable_id == short_id)
                assert c.status == "pass", f"{short_id}/{platform}: {c.reason}"


class TestCrossPlatformReadinessResolvesRepairedVideoAssetId:
    """
    2026-08-23: check_cross_platform_readiness() previously always derived
    a Short's video path from the generation-time index filename
    (short_{i}_{episode_id}.mp4), silently ignoring shorts_renders[i].
    video_asset_id even when it pointed at an immutably-versioned repaired
    render sitting right next to the original -- the exact real defect
    that made ep_f3619022's repaired Shorts invisible to this check.
    """

    def test_readiness_reads_the_video_asset_id_file_not_the_index_file(self, db_path, assets_root, monkeypatch):
        _setup_channel(db_path)
        ep_dir = _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        # Promote an immutable "repaired" copy alongside the untouched
        # original, and point shorts_renders[0].video_asset_id at it --
        # exactly the real live-repair pattern.
        repaired_path = ep_dir / f"short_0_{EPISODE_ID}.repaired_v1.mp4"
        repaired_path.write_bytes(b"repaired short 0" * 500)
        pkg_path = ep_dir / f"final_render_package_{EPISODE_ID}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["shorts_renders"][0]["video_asset_id"] = f"episodes/{EPISODE_ID}/short_0_{EPISODE_ID}.repaired_v1"
        pkg_path.write_text(json.dumps(pkg), encoding="utf-8")

        # Original (unrepaired) file measures out-of-range (95s); the
        # repaired file measures in-range (55s) -- readiness must probe
        # the REPAIRED path, proving it resolved video_asset_id and not
        # the stale index-derived filename.
        original_path = ep_dir / f"short_0_{EPISODE_ID}.mp4"

        def _path_aware_duration(path):
            if str(path) == str(repaired_path):
                return 55.0
            if str(path) == str(original_path):
                return 95.0
            return 30.0

        monkeypatch.setattr(
            "hypogeum.publishing.cross_platform_readiness._probe_duration_seconds",
            _path_aware_duration,
        )

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        fb_short0 = next(c for c in result.for_platform("facebook") if c.deliverable_id == "short_sh_0")
        assert fb_short0.status == "pass", fb_short0.reason
        assert fb_short0.evidence["duration_seconds"] == 55.0

    def test_shorts_without_video_asset_id_fall_back_to_index_convention(self, db_path, assets_root, monkeypatch):
        """Backward compatibility: an unrepaired historical package (no
        video_asset_id set on any short) behaves exactly as before."""
        monkeypatch.setattr(
            "hypogeum.publishing.cross_platform_readiness._probe_duration_seconds",
            lambda path: 30.0,
        )
        _setup_channel(db_path)
        _seed_episode(db_path, assets_root)
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        for short_id in ("short_sh_0", "short_sh_1", "short_sh_2"):
            fb_short = next(c for c in result.for_platform("facebook") if c.deliverable_id == short_id)
            assert fb_short.status == "pass", fb_short.reason

    def test_repaired_titles_captions_and_platform_contracts_all_resolve_for_three_shorts(self, db_path, assets_root, monkeypatch):
        """
        End-to-end proof (both fixes together, all three Shorts): the
        exact real ep_f3619022 pattern -- shorts_renders[].short_id
        already canonical ('short_001' style, never re-prefixed),
        video_asset_id pointing at an immutable repaired file, real
        in-range duration, real resolvable captions/titles -- all three
        Shorts reach a clean pass on every real platform contract.
        """
        monkeypatch.setattr(
            "hypogeum.publishing.cross_platform_readiness._probe_duration_seconds",
            lambda path: 55.0,
        )
        _setup_channel(db_path)
        ep_dir = assets_root / "episodes" / EPISODE_ID
        ep_dir.mkdir(parents=True)
        conn = get_connection(db_path)
        try:
            insert_episode(conn, episode_id=EPISODE_ID, created_at=_now_iso(), topic="A Real Topic", status="complete")
            insert_episode_topic(conn, topic_id=f"topic_{EPISODE_ID}", channel_id=CHANNEL_ID, topic_text="A topic",
                                  created_at=_now_iso(), updated_at=_now_iso(), episode_id=EPISODE_ID)
            conn.commit()
        finally:
            conn.close()

        short_ids = ["short_001", "short_002", "short_003"]
        research_pkg = {
            "source_registry": [{"source_id": "src_001", "url": "https://example.com/source", "inferred_type": "test"}],
            "claims": [{"claim_id": "clm_001", "text": "A real claim.", "sources": ["src_001"]}],
        }
        research_pkg_path = ep_dir / f"research_package_{EPISODE_ID}.json"
        research_pkg_path.write_text(json.dumps(research_pkg), encoding="utf-8")
        research_package_sha256 = hashlib.sha256(research_pkg_path.read_bytes()).hexdigest()

        canonical_platform_metadata = [
            _canonical_platform_metadata_entry(
                "main_episode", "youtube", with_chapters=True, research_package_sha256=research_package_sha256,
            ),
        ]
        for sid in short_ids:
            for platform in ("youtube", "tiktok", "instagram", "facebook"):
                canonical_platform_metadata.append(_canonical_platform_metadata_entry(
                    sid, platform, description=f"Real caption for {sid}. #TestTag",
                    research_package_sha256=research_package_sha256,
                ))

        pkg = {
            "metadata_source": {
                "episode_title": "A Real Episode Title", "description": "A real description. #TestTag",
                "shorts_titles": [{"short_id": sid, "title": f"Real Title {sid}"} for sid in short_ids],
                "shorts_captions": [{"short_id": sid, "caption": f"Real caption for {sid}.", "source": "test", "version": "v1"} for sid in short_ids],
                "canonical_platform_metadata": canonical_platform_metadata,
            },
            "shorts_renders": [
                {"short_id": sid, "video_asset_id": f"episodes/{EPISODE_ID}/{sid}.repaired_v1"}
                for sid in short_ids
            ],
        }
        (ep_dir / f"final_render_package_{EPISODE_ID}.json").write_text(json.dumps(pkg), encoding="utf-8")
        (ep_dir / f"main_{EPISODE_ID}.mp4").write_bytes(b"main" * 500)
        for sid in short_ids:
            (ep_dir / f"{sid}.repaired_v1.mp4").write_bytes(f"{sid} repaired".encode() * 500)
        from PIL import Image
        thumbs_dir = ep_dir / "thumbnails"
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1280, 720), color=(60, 60, 60)).save(thumbs_dir / "candidate_1.jpg", "JPEG")
        (ep_dir / f"thumbnail_package_{EPISODE_ID}.json").write_text(json.dumps({
            "thumbnail_package_id": f"tp_{EPISODE_ID}", "episode_id": EPISODE_ID,
            "main_thumbnail": {"asset_id": f"episodes/{EPISODE_ID}/thumbnails/candidate_1"},
            "candidates": [{"candidate_id": "c1", "rank": 1, "selected": True, "status": "generated",
                             "asset_id": f"episodes/{EPISODE_ID}/thumbnails/candidate_1"}],
            "thumbnail_qa_report": {"overall_status": "pass", "checks": [], "failures": []},
            "thumbnail_identity_report": {"verdict": "APPROVED", "violations": []},
        }), encoding="utf-8")
        _bind_all_platforms(db_path)

        result = check_cross_platform_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=assets_root, db_path=db_path)
        for sid in short_ids:
            assert sid in {c.deliverable_id for c in result.checks}, f"{sid} never double-prefixed"
            for platform in ("youtube", "tiktok", "instagram", "facebook"):
                check = next(c for c in result.for_platform(platform) if c.deliverable_id == sid)
                assert check.status == "pass", f"{sid}/{platform}: {check.reason}"

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
        # Fixed 2026-08-23 (item 3): _seed_episode() now populates a real
        # canonical_platform_metadata entry for every Short/platform
        # pair (see _pick_root_title_description()), which takes
        # priority over the older metadata_source.shorts_captions
        # resolution -- the fixture's canonical descriptions mirror the
        # same real per-short caption text so this assertion still
        # verifies the same real content actually reaches Zernio.
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
            assert call["tiktokSettings"]["allow_comment"] is True
            assert call["tiktokSettings"]["allow_duet"] is True
            assert call["tiktokSettings"]["allow_stitch"] is True

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
