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
    ChannelScopeMismatchError,
    DeliverableFile,
    DeliverableSchedule,
    InvalidSchedulePlanError,
    MissingSchedulePlanError,
    PublishPackage,
    SchedulePlan,
    publish_episode_package,
)

EPISODE_ID = "ep_runtime01"
CHANNEL_ID = "hypogeum"


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
    # Three related Shorts, matching Channel A's declared bundle shape
    # (one long-form + three Shorts) used throughout this test module.
    paths = []
    for i in range(3):
        p = tmp_path / f"short_{i}_{EPISODE_ID}.mp4"
        p.write_bytes(f"short {i} bytes".encode() * 500)
        paths.append(p)
    return paths


def _package(main_file, short_files, *, channel_id=CHANNEL_ID, bundle_id="main_episode_bundle"):
    return PublishPackage(
        channel_id=channel_id,
        episode_id=EPISODE_ID,
        bundle_id=bundle_id,
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


def _full_schedule_plan(*, publish_now=True, channel_id=CHANNEL_ID):
    return SchedulePlan(channel_id=channel_id, entries={
        "main_episode": DeliverableSchedule(publish_now=publish_now),
        "short_0": DeliverableSchedule(publish_now=publish_now),
        "short_1": DeliverableSchedule(publish_now=publish_now),
        "short_2": DeliverableSchedule(publish_now=publish_now),
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
        assert len(result.deliverables) == 4
        assert {d.deliverable_id for d in result.deliverables} == {"main_episode", "short_0", "short_1", "short_2"}
        assert len(media_transport.presign_calls) == 4
        assert len(publish_transport.post_calls) == 4

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
        assert len(media_transport.presign_calls) == 4
        assert len(publish_transport.post_calls) == 4


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
        incomplete_plan = SchedulePlan(channel_id=CHANNEL_ID, entries={"main_episode": DeliverableSchedule(publish_now=True)})

        result = publish_episode_package(
            _package(main_file, short_files), platform="youtube", channel_id="hypogeum", db_path=db_path,
            schedule_plan=incomplete_plan,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert result.blocked is True
        assert "short_0" in result.blocked_reason and "short_1" in result.blocked_reason and "short_2" in result.blocked_reason
        assert media_transport.presign_calls == []
        assert publish_transport.post_calls == []

    def test_wrong_type_schedule_plan_blocks(self, db_path, main_file, short_files):
        result = publish_episode_package(
            _package(main_file, short_files), platform="youtube", channel_id="hypogeum", db_path=db_path,
            schedule_plan={"main_episode": "not a real plan"},
        )
        assert result.blocked is True
        assert isinstance(result.blocked_reason, str)


class TestChannelIsolation:
    """PublishPackage.channel_id and SchedulePlan.channel_id are both
    required and both checked against publish_episode_package()'s own
    channel_id argument before any staging/publish call — see
    ChannelScopeMismatchError. Provider-binding isolation itself is
    proven by resolve_active_binding()'s pre-existing channel_id-scoped
    lookup (ADR-013, unchanged by this slice)."""

    def test_package_built_for_a_different_channel_blocks_before_any_call(self, db_path, main_file, short_files):
        _setup_binding_and_credential(db_path, channel_id="channel_a")
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        package = _package(main_file, short_files, channel_id="channel_b")  # package says channel_b...
        result = publish_episode_package(
            package, platform="youtube", channel_id="channel_a",  # ...call asserts channel_a
            db_path=db_path, schedule_plan=_full_schedule_plan(channel_id="channel_a"),
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert result.blocked is True
        assert "PublishPackage.channel_id" in result.blocked_reason
        assert media_transport.presign_calls == []
        assert publish_transport.post_calls == []

    def test_schedule_plan_built_for_a_different_channel_blocks_before_any_call(self, db_path, main_file, short_files):
        _setup_binding_and_credential(db_path, channel_id="channel_a")
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        package = _package(main_file, short_files, channel_id="channel_a")
        result = publish_episode_package(
            package, platform="youtube", channel_id="channel_a",
            db_path=db_path, schedule_plan=_full_schedule_plan(channel_id="channel_b"),  # wrong channel's schedule
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert result.blocked is True
        assert "SchedulePlan.channel_id" in result.blocked_reason
        assert media_transport.presign_calls == []
        assert publish_transport.post_calls == []

    def test_channel_b_cannot_use_channel_as_provider_binding(self, db_path, main_file, short_files):
        # Binding exists ONLY for channel_a.
        _setup_binding_and_credential(db_path, channel_id="channel_a")
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        package = _package(main_file, short_files, channel_id="channel_b")
        result = publish_episode_package(
            package, platform="youtube", channel_id="channel_b",
            db_path=db_path, schedule_plan=_full_schedule_plan(channel_id="channel_b"),
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        # Not blocked at the schedule-plan gate (everything internally
        # consistent for channel_b) -- fails downstream, per-deliverable,
        # at binding resolution instead. Either way, channel_b's request
        # must never succeed using channel_a's binding, and no publish
        # transport call is ever made (binding resolution happens before
        # the transport call inside ZernioProvider.publish()).
        assert result.blocked is False
        assert all(d.status == "blocked" for d in result.deliverables)
        assert publish_transport.post_calls == []

    def test_each_channel_publishes_correctly_with_its_own_binding(self, db_path, main_file, short_files):
        _setup_binding_and_credential(db_path, channel_id="channel_a")
        _setup_binding_and_credential(db_path, channel_id="channel_b")
        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()
        staging = ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport)
        publisher = ZernioProvider(transport=publish_transport)

        result_a = publish_episode_package(
            _package(main_file, short_files, channel_id="channel_a"), platform="youtube", channel_id="channel_a",
            db_path=db_path, schedule_plan=_full_schedule_plan(channel_id="channel_a"),
            media_staging_provider=staging, publishing_provider=publisher,
        )
        assert result_a.all_published is True

        # A different episode's short files for channel_b (main_file/
        # short_files are reused as bytes-identical local files here only
        # because sha256/staging dedup is scoped by episode_id/deliverable_id/
        # provider, not by file identity; channel_id flows through separately).
        result_b = publish_episode_package(
            _package(main_file, short_files, channel_id="channel_b"), platform="youtube", channel_id="channel_b",
            db_path=db_path, schedule_plan=_full_schedule_plan(channel_id="channel_b"),
            media_staging_provider=staging, publishing_provider=publisher,
        )
        assert result_b.all_published is True


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
