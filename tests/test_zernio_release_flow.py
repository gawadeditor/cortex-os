"""
tests/test_zernio_release_flow.py — Operator-facing release orchestration.

Covers hypogeum.publishing.zernio_release_flow.publish_channel_release():
the full readiness -> schedule-compile -> stage -> publish sequence, each
stage gating the next, using fake transports and an isolated tmp_path
database. Also reproduces the REAL current blocking shape of ep_f3619022
(title conflict + no real binding) as fixtures, proving the orchestrator
reports the same two blocking reasons the live read-only check already
found against the real production database and files.
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
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.readiness import MetadataOverride
from hypogeum.publishing.zernio import ZernioProvider, ZernioTransportResponse
from hypogeum.publishing.zernio_media_staging import (
    PresignTransportResponse,
    UploadTransportResponse,
    ZernioMediaStagingProvider,
)
from hypogeum.publishing.zernio_publish_runtime import DeliverableFile, PublishPackage
from hypogeum.publishing.zernio_release_flow import publish_channel_release

CHANNEL_ID = "hypogeum"
EPISODE_ID = "ep_release01"


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "release_flow_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def assets_root(tmp_path):
    root = tmp_path / "assets"
    (root / "episodes" / EPISODE_ID).mkdir(parents=True)
    return root


def _setup_channel(db_path, *, channel_id=CHANNEL_ID, day_of_week="thursday", time_of_day="18:00", tz="Asia/Dubai"):
    brand = create_brand_profile(name=f"{channel_id} Brand", db_path=db_path)
    model = create_model_profile(name=f"{channel_id} Model", db_path=db_path)
    pub = create_publishing_profile(name=f"{channel_id} Publishing", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name=channel_id,
        brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )
    create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, timezone=tz,
        publishing_schedule={"day_of_week": day_of_week, "time_of_day": time_of_day},
        db_path=db_path,
    )


def _attribute_episode(db_path, episode_id=EPISODE_ID, channel_id=CHANNEL_ID):
    conn = get_connection(db_path)
    try:
        insert_episode_topic(
            conn, topic_id=f"topic_{episode_id}", channel_id=channel_id, topic_text="A topic",
            created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00", episode_id=episode_id,
        )
        conn.commit()
    finally:
        conn.close()


def _write_bundle_files(assets_root, episode_id=EPISODE_ID, *, n_shorts=3, title="", shorts_titles=None):
    ep_dir = assets_root / "episodes" / episode_id
    pkg = {
        "metadata_source": {
            "episode_title": title, "description": "A real description." if title else "",
            "shorts_titles": shorts_titles if shorts_titles is not None else ([] if not title else [f"s{i}" for i in range(n_shorts)]),
        },
        "shorts_renders": [{"short_id": f"sh_{i}"} for i in range(n_shorts)],
    }
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps(pkg), encoding="utf-8")
    (ep_dir / f"main_{episode_id}.mp4").write_bytes(b"main bytes" * 500)
    for i in range(n_shorts):
        (ep_dir / f"short_{i}_{episode_id}.mp4").write_bytes(f"short {i}".encode() * 500)


def _package(assets_root, episode_id=EPISODE_ID, channel_id=CHANNEL_ID, n_shorts=3):
    ep_dir = assets_root / "episodes" / episode_id
    return PublishPackage(
        channel_id=channel_id, episode_id=episode_id, bundle_id="main_episode_bundle",
        main_episode=DeliverableFile(
            deliverable_id="main_episode", local_path=ep_dir / f"main_{episode_id}.mp4",
            mime_type="video/mp4", media_type="video", filename=f"main_{episode_id}.mp4",
        ),
        shorts=[
            DeliverableFile(
                deliverable_id=f"short_{i}", local_path=ep_dir / f"short_{i}_{episode_id}.mp4",
                mime_type="video/mp4", media_type="video", filename=f"short_{i}_{episode_id}.mp4",
            )
            for i in range(n_shorts)
        ],
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


class TestFullSequenceViaFakes:
    def test_readiness_schedule_stage_publish_all_succeed(self, db_path, assets_root):
        _setup_channel(db_path)
        _attribute_episode(db_path)
        _write_bundle_files(assets_root, title="A Real Title")
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        result = publish_channel_release(
            CHANNEL_ID, EPISODE_ID, "youtube", _package(assets_root),
            assets_root=assets_root, db_path=db_path,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert result.status == "published"
        assert result.readiness.passed is True
        assert result.schedule.blocked is False
        assert result.publish.all_published is True
        assert len(media_transport.presign_calls) == 4  # main + 3 shorts
        assert len(publish_transport.post_calls) == 4
        for call in publish_transport.post_calls:
            assert call.get("scheduledFor") is not None


class TestBlockedAtReadiness:
    def test_reproduces_real_ep_f3619022_blocking_shape(self, db_path, assets_root):
        """title empty + no real binding -- the EXACT real state confirmed
        live-read-only against production ep_f3619022/hypogeum earlier in
        this session (metadata title conflict/absence + D_PUBLISHING_BINDING
        both fail; render/staging-credential checks pass)."""
        _setup_channel(db_path)
        _attribute_episode(db_path)
        _write_bundle_files(assets_root, title="")  # no title, no binding at all

        result = publish_channel_release(
            CHANNEL_ID, EPISODE_ID, "youtube", _package(assets_root),
            assets_root=assets_root, db_path=db_path,
        )

        assert result.status == "blocked_readiness"
        assert result.schedule is None
        assert result.publish is None
        failed = {c.capability for c in result.readiness.checks if c.status == "fail"}
        assert "A_METADATA" in failed
        assert "D_PUBLISHING_BINDING" in failed

    def test_metadata_override_resolves_title_but_binding_still_blocks(self, db_path, assets_root):
        _setup_channel(db_path)
        _attribute_episode(db_path)
        _write_bundle_files(assets_root, title="")

        result = publish_channel_release(
            CHANNEL_ID, EPISODE_ID, "youtube", _package(assets_root),
            assets_root=assets_root, db_path=db_path,
            metadata_override=MetadataOverride(title="Operator Title", description="d", shorts_titles=["a", "b", "c"]),
        )
        assert result.status == "blocked_readiness"
        failed = {c.capability for c in result.readiness.checks if c.status == "fail"}
        assert "A_METADATA" not in failed
        assert "D_PUBLISHING_BINDING" in failed


class TestBlockedAtSchedule:
    def test_no_channel_operations_profile_blocks_before_any_staging_or_publish_call(self, db_path, assets_root):
        # Channel profile exists (readiness needs channel_provider_bindings
        # lookup which needs no profile), but NO channel_operations_profile
        # is created -- schedule compilation must fail closed.
        brand = create_brand_profile(name="X Brand", db_path=db_path)
        model = create_model_profile(name="X Model", db_path=db_path)
        pub = create_publishing_profile(name="X Publishing", db_path=db_path)
        create_channel_profile(
            channel_id=CHANNEL_ID, name=CHANNEL_ID,
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        _attribute_episode(db_path)
        _write_bundle_files(assets_root, title="A Real Title")
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        result = publish_channel_release(
            CHANNEL_ID, EPISODE_ID, "youtube", _package(assets_root),
            assets_root=assets_root, db_path=db_path,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert result.status == "blocked_schedule"
        assert result.readiness.passed is True
        assert result.publish is None
        assert media_transport.presign_calls == []
        assert publish_transport.post_calls == []


class TestNoLiveCalls:
    def test_no_real_socket_opened_anywhere_in_the_full_sequence(self, db_path, assets_root, monkeypatch):
        def _fail_socket(*a, **k):
            raise AssertionError("no real socket may be opened by this test")

        monkeypatch.setattr(socket.socket, "connect", _fail_socket)

        _setup_channel(db_path)
        _attribute_episode(db_path)
        _write_bundle_files(assets_root, title="A Real Title")
        _fully_bind(db_path)

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        result = publish_channel_release(
            CHANNEL_ID, EPISODE_ID, "youtube", _package(assets_root),
            assets_root=assets_root, db_path=db_path,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )
        assert result.status == "published"
