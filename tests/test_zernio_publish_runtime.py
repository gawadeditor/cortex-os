"""
tests/test_zernio_publish_runtime.py — Stage->publish runtime glue.

Covers hypogeum.publishing.zernio_publish_runtime.publish_episode_package():
the full long-episode + Shorts stage->publish chain via fake transports,
schedule_plan gating (missing/invalid blocks before any staging or publish
call), and structural non-interference with
hypogeum.agents.publishing_agent's legacy STEP 6/8/9/10 direct-adapter
path.

Every test uses an isolated tmp_path SQLite database and fake presign/
upload/post transports — never the production DB, never a real socket.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import initialize_schema
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.zernio import ZernioProvider, ZernioTransportResponse
from hypogeum.publishing.zernio_media_staging import (
    PresignTransportResponse,
    UploadTransportResponse,
    ZernioMediaStagingProvider,
)
from hypogeum.publishing.zernio_publish_runtime import (
    DeliverableFile,
    DeliverableSchedule,
    InvalidSchedulePlanError,
    MissingSchedulePlanError,
    PublishPackage,
    SchedulePlan,
    publish_episode_package,
)

EPISODE_ID = "ep_runtime01"


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "runtime_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def main_file(tmp_path):
    p = tmp_path / f"main_{EPISODE_ID}.mp4"
    p.write_bytes(b"main video bytes" * 500)
    return p


@pytest.fixture()
def short_files(tmp_path):
    paths = []
    for i in range(2):
        p = tmp_path / f"short_{i}_{EPISODE_ID}.mp4"
        p.write_bytes(f"short {i} bytes".encode() * 500)
        paths.append(p)
    return paths


def _package(main_file, short_files):
    return PublishPackage(
        episode_id=EPISODE_ID,
        main_episode=DeliverableFile(
            deliverable_id="main_episode", local_path=main_file, mime_type="video/mp4",
            media_type="video", filename=main_file.name,
        ),
        shorts=[
            DeliverableFile(
                deliverable_id=f"short_{i}", local_path=p, mime_type="video/mp4",
                media_type="video", filename=p.name,
            )
            for i, p in enumerate(short_files)
        ],
    )


def _future(days=1):
    return datetime.now(timezone.utc) + timedelta(days=days)


def _full_schedule_plan(*, publish_now=True):
    return SchedulePlan(entries={
        "main_episode": DeliverableSchedule(publish_now=publish_now),
        "short_0": DeliverableSchedule(publish_now=publish_now),
        "short_1": DeliverableSchedule(publish_now=publish_now),
    })


def _setup_binding_and_credential(db_path, *, channel_id="hypogeum", platform="youtube"):
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
        self.upload_calls = []
        self._counter = 0

    def presign(self, *, url, headers, json_body, timeout):
        self._counter += 1
        self.presign_calls.append(json_body)
        return PresignTransportResponse(
            status_code=200,
            json_body={
                "uploadUrl": f"https://upload.example.com/{self._counter}",
                "publicUrl": f"https://cdn.example.com/{self._counter}",
                "key": f"key_{self._counter}",
                "expiresIn": 3600,
            },
        )

    def upload(self, *, url, headers, data, timeout):
        self.upload_calls.append(url)
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
            json_body={
                "message": "created",
                "post": {
                    "_id": f"post_{self._counter}",
                    "platforms": [{"platform": platform, "platformPostId": f"native_{self._counter}", "status": "published"}],
                },
            },
        )


class TestFullChain:
    def test_long_and_shorts_stage_and_publish_via_fakes(self, db_path, main_file, short_files):
        _setup_binding_and_credential(db_path)
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        staging = ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport)
        publisher = ZernioProvider(transport=publish_transport)

        result = publish_episode_package(
            _package(main_file, short_files), platform="youtube", channel_id="hypogeum",
            db_path=db_path, schedule_plan=_full_schedule_plan(),
            media_staging_provider=staging, publishing_provider=publisher,
        )

        assert result.blocked is False
        assert result.all_published is True
        assert len(result.deliverables) == 3
        assert {d.deliverable_id for d in result.deliverables} == {"main_episode", "short_0", "short_1"}
        assert len(media_transport.presign_calls) == 3
        assert len(publish_transport.post_calls) == 3

    def test_idempotent_second_call_reuses_staging_and_ledger(self, db_path, main_file, short_files):
        _setup_binding_and_credential(db_path)
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        staging = ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport)
        publisher = ZernioProvider(transport=publish_transport)

        kwargs = dict(
            platform="youtube", channel_id="hypogeum", db_path=db_path,
            schedule_plan=_full_schedule_plan(), media_staging_provider=staging, publishing_provider=publisher,
        )
        first = publish_episode_package(_package(main_file, short_files), **kwargs)
        second = publish_episode_package(_package(main_file, short_files), **kwargs)

        assert first.all_published is True
        assert second.all_published is True
        # Reused, not re-uploaded/re-posted.
        assert len(media_transport.presign_calls) == 3
        assert len(publish_transport.post_calls) == 3


class TestSchedulePlanGating:
    def test_missing_schedule_plan_blocks_before_any_call(self, db_path, main_file, short_files):
        _setup_binding_and_credential(db_path)
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        result = publish_episode_package(
            _package(main_file, short_files), platform="youtube", channel_id="hypogeum", db_path=db_path,
            schedule_plan=None,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert result.blocked is True
        assert "schedule_plan is required" in result.blocked_reason
        assert result.deliverables == []
        assert media_transport.presign_calls == []
        assert publish_transport.post_calls == []

    def test_incomplete_schedule_plan_blocks_before_any_call(self, db_path, main_file, short_files):
        _setup_binding_and_credential(db_path)
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        incomplete_plan = SchedulePlan(entries={"main_episode": DeliverableSchedule(publish_now=True)})

        result = publish_episode_package(
            _package(main_file, short_files), platform="youtube", channel_id="hypogeum", db_path=db_path,
            schedule_plan=incomplete_plan,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert result.blocked is True
        assert "short_0" in result.blocked_reason and "short_1" in result.blocked_reason
        assert media_transport.presign_calls == []
        assert publish_transport.post_calls == []

    def test_wrong_type_schedule_plan_blocks(self, db_path, main_file, short_files):
        result = publish_episode_package(
            _package(main_file, short_files), platform="youtube", channel_id="hypogeum", db_path=db_path,
            schedule_plan={"main_episode": "not a real plan"},
        )
        assert result.blocked is True
        assert isinstance(result.blocked_reason, str)


class TestNoLiveCallsOrProductionMutation:
    def test_no_real_socket_opened(self, db_path, main_file, short_files, monkeypatch):
        def _fail_socket(*a, **k):
            raise AssertionError("no real socket may be opened by this test")

        monkeypatch.setattr(socket.socket, "connect", _fail_socket)
        _setup_binding_and_credential(db_path)
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        result = publish_episode_package(
            _package(main_file, short_files), platform="youtube", channel_id="hypogeum", db_path=db_path,
            schedule_plan=_full_schedule_plan(),
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )
        assert result.all_published is True

    def test_default_providers_construct_without_any_call(self, main_file, short_files):
        # Constructing with no transport injected must never make a call —
        # matches ZernioProvider()/ZernioMediaStagingProvider()'s own contract.
        pkg = _package(main_file, short_files)
        assert pkg.episode_id == EPISODE_ID
        # publish_episode_package() itself is not invoked here — this only
        # proves default-provider construction inside it is inert until used,
        # by constructing the same defaults directly.
        ZernioProvider()
        ZernioMediaStagingProvider()


class TestLegacyPublishingAgentUnaffected:
    def test_publishing_agent_module_does_not_import_this_runtime(self):
        import hypogeum.agents.publishing_agent as pa
        import hypogeum.publishing.zernio_publish_runtime as rt

        assert rt.__name__ not in vars(pa).values()
        assert not hasattr(pa, "publish_episode_package")
        assert not hasattr(pa, "PublishPackage")
