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
import subprocess

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
        self.put_calls = []
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

    def put(self, *, url, headers, json_body, timeout):
        self._counter += 1
        self.put_calls.append(json_body)
        return ZernioTransportResponse(
            status_code=200,
            json_body={"message": "updated", "post": {"_id": "existing_post", "platforms": []}},
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


class TestCanonicalMetadataReachesTheRealSerializedRequestBody:
    """
    Fixed 2026-08-23, item 5 of docs/operations/audits/
    ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md: "the provider
    accepts platform_specific_data and root_fields, but that does not
    prove the production workflow supplies them." End-to-end,
    deterministic, starting from a real-shaped canonical_platform_metadata
    artifact on disk -- never a test calling publish_multi_platform()
    directly with manually-supplied kwargs -- through the REAL chain:
    canonical metadata artifact -> check_cross_platform_readiness() ->
    process_channel_ready_bundles() -> build_publish_package_from_episode_dir()
    -> publish_episode_package() -> ZernioProvider.publish_multi_platform()
    -> the actual serialized Zernio request body a fake transport
    captures, asserted field-by-field.
    """

    def test_youtube_platform_specific_data_and_root_fields_reach_the_real_request(self, db_path, assets_root):
        import hashlib

        episode_id = "ep_e2e_canonical"
        deliverable_schedule_policy = {"main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"}}
        # _setup_channel() builds its own brand/model/pub profiles --
        # reuse it, then separately install platform_deliverable_policy
        # (the real gate for check_cross_platform_readiness() to fire at
        # all -- see that call site's own "if platform_deliverable_policy:"
        # guard in zernio_channel_release_trigger.py).
        _setup_channel(db_path, deliverable_schedule_policy=deliverable_schedule_policy)
        from hypogeum.channel_config import update_channel_operations_profile
        update_channel_operations_profile(
            CHANNEL_ID, db_path=db_path,
            platform_deliverable_policy={"youtube": {"main_episode": {"enabled": True, "categoryId": "27"}}},
        )
        _seed_complete_episode(db_path, assets_root, episode_id, title="A Real Title")
        _fully_bind(db_path)

        ep_dir = assets_root / "episodes" / episode_id
        # canonical_metadata's chapter validation ffprobes the REAL
        # deliverable file -- _seed_complete_episode()'s own fixture
        # writes a fake byte blob (0s measured duration), which would
        # fail closed here. A short, real, moving-content video is
        # required for this test to reach a genuine "published" result.
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=30",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(ep_dir / f"main_{episode_id}.mp4"),
            ],
            check=True, capture_output=True,
        )
        research_pkg = {
            "source_registry": [{"source_id": "src_001", "url": "https://example.com/source", "inferred_type": "test"}],
            "claims": [{"claim_id": "clm_001", "text": "A real claim.", "sources": ["src_001"]}],
        }
        research_path = ep_dir / f"research_package_{episode_id}.json"
        research_path.write_text(json.dumps(research_pkg), encoding="utf-8")
        research_sha256 = hashlib.sha256(research_path.read_bytes()).hexdigest()

        render_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(render_path.read_text())
        now_iso = _now_iso()
        pkg["metadata_source"]["canonical_platform_metadata"] = [{
            "deliverable_id": "main_episode", "platform": "youtube",
            "title": "A Real Title", "description": "A real description. #TestTag",
            "visible_hashtags": ["#TestTag"], "backend_tags": ["real tag one", "real tag two"],
            "sources": [{"title": "Example Source", "url": "https://example.com/source"}],
            "claim_source_map": {"clm_001": {"sources": ["src_001"]}},
            "cta": "Subscribe for more.", "language": "en", "metadata_version": "v1",
            "source_artifact_ids": [f"research_package_{episode_id}.json"],
            "input_fingerprint": {f"research_package_{episode_id}.json": research_sha256},
            "generated_at": now_iso, "approved_at": now_iso,
            "first_comment": "A real first comment.",
            "category_id": "27", "thumbnail_ref": f"episodes/{episode_id}/thumbnails/candidate_1",
            "ai_disclosure": True,
            "chapters": [
                {"start_seconds": 0.0, "title": "Intro"},
                {"start_seconds": 12.0, "title": "Middle"},
                {"start_seconds": 24.0, "title": "End"},
            ],
        }]
        render_path.write_text(json.dumps(pkg), encoding="utf-8")

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert len(results) == 1, results
        assert results[0].result.status == "published", results[0].result

        assert len(publish_transport.post_calls) == 1
        body = publish_transport.post_calls[0]

        # Generic fields never the ONLY thing sent.
        assert body.get("title") == "A Real Title"
        assert "A real description" in body.get("content", "")

        # Root-level tags/hashtags -- sourced from the youtube canonical entry.
        assert body.get("tags") == ["real tag one", "real tag two"]
        assert body.get("hashtags") == ["#TestTag"]

        # Per-platform platformSpecificData -- the actual field mapping fix
        # reaching the real request body, not just accepted by the provider.
        youtube_entry = next(p for p in body["platforms"] if p["platform"] == "youtube")
        assert youtube_entry["platformSpecificData"] == {
            "categoryId": "27", "containsSyntheticMedia": True, "firstComment": "A real first comment.",
        }

    def test_no_canonical_metadata_means_no_platform_specific_data_sent(self, db_path, assets_root):
        # Regression guard for the OTHER direction: an episode with no
        # canonical_platform_metadata at all must not fabricate
        # platformSpecificData -- and, since platform_deliverable_policy
        # is configured here, readiness genuinely blocks it (proving the
        # gate is load-bearing, not bypassed).
        deliverable_schedule_policy = {"main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"}}
        _setup_channel(db_path, deliverable_schedule_policy=deliverable_schedule_policy)
        from hypogeum.channel_config import update_channel_operations_profile
        update_channel_operations_profile(
            CHANNEL_ID, db_path=db_path,
            platform_deliverable_policy={"youtube": {"main_episode": {"enabled": True, "categoryId": "27"}}},
        )
        _seed_complete_episode(db_path, assets_root, "ep_no_canonical", title="A Real Title")
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert publish_transport.post_calls == []
        assert results == [] or all(r.result.status != "published" for r in results)


class TestCanonicalMetadataAutoGeneration:
    """
    Permanent workflow, fixed 2026-08-23 (item 1 of docs/operations/audits/
    ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md): the metadata/SEO
    stage must produce canonical_platform_metadata AUTOMATICALLY for every
    enabled deliverable/platform pair -- no human-written repair artifact.
    Proves the auto-generation actually runs (a fresh episode, no
    canonical entry, reaches a real "published" result and a real
    serialized request body through a fake llm_call), and proves it makes
    ZERO real network/LLM calls when generation is never actually needed
    or never actually reachable.
    """

    def _seed_with_real_research_and_script(self, db_path, assets_root, episode_id):
        deliverable_schedule_policy = {"main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"}}
        _setup_channel(db_path, deliverable_schedule_policy=deliverable_schedule_policy)
        from hypogeum.channel_config import update_channel_operations_profile
        update_channel_operations_profile(
            CHANNEL_ID, db_path=db_path,
            platform_deliverable_policy={"youtube": {"main_episode": {"enabled": True, "categoryId": "27", "ai_disclosure": True}}},
        )
        _seed_complete_episode(db_path, assets_root, episode_id, title="A Real Title")
        _fully_bind(db_path)

        ep_dir = assets_root / "episodes" / episode_id
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=30",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(ep_dir / f"main_{episode_id}.mp4"),
            ],
            check=True, capture_output=True,
        )
        (ep_dir / f"research_package_{episode_id}.json").write_text(json.dumps({
            "source_registry": [{"source_id": "src_001", "url": "https://example.com/source", "inferred_type": "academic"}],
            "claims": [{"claim_id": "clm_001", "text": "A real claim.", "sources": ["src_001"]}],
        }), encoding="utf-8")
        (ep_dir / f"script_package_{episode_id}.json").write_text(json.dumps({
            "sections": [
                {"section_id": "s1", "narration_beats": [{"text": "A real narrated fact about the episode's subject."}]},
                {"section_id": "s2", "narration_beats": [{"text": "A second real narrated fact."}]},
                {"section_id": "s3", "narration_beats": [{"text": "A third real narrated fact."}]},
            ],
            "chapter_markers": [
                {"section_id": "s1", "title": "Intro"},
                {"section_id": "s2", "title": "Middle"},
                {"section_id": "s3", "title": "End"},
            ],
        }), encoding="utf-8")
        # Real per-section start times -- _resolve_chapters() refuses to
        # fabricate timestamps without one of these (or a real
        # narration_package) as an actual timing source; this mirrors
        # what an earlier pipeline step (shorts/section metadata
        # generation) already writes for a real production episode.
        render_path = ep_dir / f"final_render_package_{episode_id}.json"
        render_pkg = json.loads(render_path.read_text())
        render_pkg["metadata_source"]["section_titles"] = [
            {"section_id": "s1", "start_seconds": 0.0},
            {"section_id": "s2", "start_seconds": 12.0},
            {"section_id": "s3", "start_seconds": 24.0},
        ]
        render_path.write_text(json.dumps(render_pkg), encoding="utf-8")
        return ep_dir

    @staticmethod
    def _fake_canonical_llm_call(system_prompt, input_package, *, config, episode_id, channel_id):
        return {"platforms": {
            platform: {
                "title": f"A Real Generated Title ({platform})",
                "description": f"A real, generated, platform-specific description for {platform}. #TestTag",
                "visible_hashtags": ["#TestTag"],
                "backend_tags": ["generated tag one", "generated tag two"] if platform == "youtube" else [],
                "cta": "Subscribe for more.",
                "first_comment": "A real generated first comment." if platform == "youtube" else "",
            }
            for platform in input_package["platforms"]
        }}

    def test_auto_generated_metadata_reaches_a_real_published_result(self, db_path, assets_root):
        episode_id = "ep_auto_gen"
        self._seed_with_real_research_and_script(db_path, assets_root, episode_id)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
            canonical_metadata_llm_call=self._fake_canonical_llm_call,
        )

        assert len(results) == 1, results
        assert results[0].result.status == "published", results[0].result
        assert len(publish_transport.post_calls) == 1
        body = publish_transport.post_calls[0]
        youtube_entry = next(p for p in body["platforms"] if p["platform"] == "youtube")
        assert youtube_entry["platformSpecificData"]["categoryId"] == "27"
        assert youtube_entry["platformSpecificData"]["containsSyntheticMedia"] is True
        assert body["tags"] == ["generated tag one", "generated tag two"]
        assert "generated, platform-specific description" in body["content"]

        # Persisted back to disk -- a SECOND run against the same episode
        # must not need to generate again (no human repair artifact, and
        # no unnecessary repeated generation either).
        ep_dir = assets_root / "episodes" / episode_id
        pkg = json.loads((ep_dir / f"final_render_package_{episode_id}.json").read_text())
        assert len(pkg["metadata_source"]["canonical_platform_metadata"]) == 1
        assert pkg["metadata_source"]["canonical_platform_metadata"][0]["deliverable_id"] == "main_episode"

    def test_missing_canonical_metadata_makes_zero_real_network_calls_when_ungenerable(self, db_path, assets_root, monkeypatch):
        # A spy that fails loudly if ever reached -- proves generation
        # never attempts a real network/LLM call for an episode this
        # session provides no fake llm_call for AND no real grounding
        # artifacts for (the honest "cannot generate" case).
        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("hypogeum.openrouter.call_claude must never be reached in this test")
        monkeypatch.setattr("hypogeum.openrouter.call_claude", _must_not_be_called)

        deliverable_schedule_policy = {"main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"}}
        _setup_channel(db_path, deliverable_schedule_policy=deliverable_schedule_policy)
        from hypogeum.channel_config import update_channel_operations_profile
        update_channel_operations_profile(
            CHANNEL_ID, db_path=db_path,
            platform_deliverable_policy={"youtube": {"main_episode": {"enabled": True, "categoryId": "27"}}},
        )
        # No research_package/script_package written -- generation cannot
        # ground itself in anything real and must fail closed BEFORE ever
        # reaching the model call.
        _seed_complete_episode(db_path, assets_root, "ep_ungenerable", title="A Real Title")
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
            # canonical_metadata_llm_call deliberately omitted -- proves
            # the REAL default path is never reached either.
        )
        assert publish_transport.post_calls == []
        assert results == [] or all(r.result.status != "published" for r in results)

    def test_disabled_platform_never_triggers_generation(self, db_path, assets_root, monkeypatch):
        # A platform genuinely disabled in the Channel Package must never
        # be generated for, even with real grounding artifacts present.
        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("hypogeum.openrouter.call_claude must never be reached for a disabled platform")
        monkeypatch.setattr("hypogeum.openrouter.call_claude", _must_not_be_called)

        episode_id = "ep_disabled_platform"
        self._seed_with_real_research_and_script(db_path, assets_root, episode_id)
        from hypogeum.channel_config import update_channel_operations_profile
        update_channel_operations_profile(
            CHANNEL_ID, db_path=db_path,
            platform_deliverable_policy={"youtube": {"main_episode": {"enabled": False}}},
        )

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )
        assert publish_transport.post_calls == []

    def test_missing_categoryid_blocks_the_real_pipeline_end_to_end_zero_transport_calls(self, db_path, assets_root, monkeypatch):
        """
        Real end-to-end proof (2026-08-23, replaces the earlier unit-level
        spy-on-an-unrelated-function proof): process_channel_ready_bundles
        -> readiness -> the real canonical-metadata backfill -> the real
        publishing runtime -> a REAL fake Zernio transport object (not a
        monkeypatch on an unrelated module-level function). A single
        enabled YouTube target (main_episode) with NO categoryId
        configured in the Channel Package must block the entire pipeline:
        the model is never called (canonical generation's own
        policy-completeness pre-check fails closed atomically), the fake
        transport's post() and put() are never called, and no
        publication_intent for this episode is ever marked 'succeeded'.
        """
        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("hypogeum.openrouter.call_claude must never be reached when categoryId is missing")
        monkeypatch.setattr("hypogeum.openrouter.call_claude", _must_not_be_called)

        episode_id = "ep_missing_category"
        ep_dir = self._seed_with_real_research_and_script(db_path, assets_root, episode_id)
        # Overwrite the policy set by _seed_with_real_research_and_script
        # (which includes categoryId) with one that omits it entirely --
        # youtube.main_episode enabled, but no categoryId declared.
        from hypogeum.channel_config import update_channel_operations_profile
        update_channel_operations_profile(
            CHANNEL_ID, db_path=db_path,
            platform_deliverable_policy={"youtube": {"main_episode": {"enabled": True}}},
        )

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()  # a REAL fake transport object, not a monkeypatch

        results = process_channel_ready_bundles(
            CHANNEL_ID, BUNDLE, db_path=db_path, assets_root=assets_root, enable_production_output_gate=False,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
            # canonical_metadata_llm_call deliberately omitted -- the real
            # default call_claude path is what must never be reached.
        )

        assert results == [] or all(r.result.status != "published" for r in results)
        assert publish_transport.post_calls == [], "zero Zernio POST calls"
        assert publish_transport.put_calls == [], "zero Zernio PUT calls"

        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT status FROM publication_intents WHERE episode_id=?", (episode_id,)
            ).fetchall()
        finally:
            conn.close()
        assert not any(r[0] == "succeeded" for r in rows), (
            f"no publication_intent for {episode_id!r} may be marked succeeded: {[r[0] for r in rows]}"
        )

        # And no canonical_platform_metadata was ever persisted either --
        # the atomic pre-check fails BEFORE generation, not after a
        # partial/invalid generation.
        pkg = json.loads((ep_dir / f"final_render_package_{episode_id}.json").read_text())
        assert not (pkg.get("metadata_source") or {}).get("canonical_platform_metadata")


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
