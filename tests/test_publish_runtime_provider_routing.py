"""
tests/test_publish_runtime_provider_routing.py — per-platform provider
routing in zernio_publish_runtime.publish_episode_package() (Postiz
migration, 2026-10-03).

Isolated tmp_path DB; fake staging + fake publishers; no network.
"""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema
from hypogeum.publishing import zernio_publish_runtime as runtime
from hypogeum.publishing.postiz import PostizProvider
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.zernio import ZernioProvider
from hypogeum.publishing.zernio_media_staging import ZernioMediaStagingProvider
from hypogeum.publishing.zernio_publish_runtime import (
    DeliverableFile,
    DeliverableSchedule,
    PublishPackage,
    SchedulePlan,
    publish_episode_package,
)
from tests.test_zernio_publish_runtime import FakeMediaTransport

CHANNEL = "twinkle_mini_tales"


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "routing.db"
    initialize_schema(p)
    return p


def _bind(db_path, platform, provider_id):
    profile = upsert_provider_profile(db_path, provider_id=provider_id, external_profile_id=f"{provider_id}_prof")
    account = upsert_provider_account(
        db_path, provider_id=provider_id, platform=platform, external_account_id=f"{provider_id}_{platform}",
        provider_profile_id=profile["provider_profile_id"], status="active",
    )
    bind_channel_provider(
        db_path, channel_id=CHANNEL, capability="publishing", platform=platform, provider_id=provider_id,
        provider_profile_id=profile["provider_profile_id"], provider_account_id=account["provider_account_id"],
    )


def test_routes_each_platform_by_its_own_binding(db_path):
    _bind(db_path, "youtube", "zernio")
    _bind(db_path, "facebook", "postiz")
    _bind(db_path, "instagram", "postiz")
    routed = runtime._route_platforms_by_binding(
        ["youtube", "facebook", "tiktok", "instagram"], channel_id=CHANNEL, logical_alias="default",
        db_path=db_path,
    )
    as_names = [(type(p).__name__, plats) for p, plats in routed]
    # tiktok has no binding at all -> stays on Zernio (which fails closed itself)
    assert as_names == [("ZernioProvider", ["youtube", "tiktok"]),
                        ("PostizProvider", ["facebook", "instagram"])]


def test_no_postiz_binding_means_everything_stays_on_zernio(db_path):
    _bind(db_path, "youtube", "zernio")
    routed = runtime._route_platforms_by_binding(["youtube", "tiktok"], channel_id=CHANNEL,
                                                 logical_alias="default", db_path=db_path)
    assert len(routed) == 1 and isinstance(routed[0][0], ZernioProvider)


class _FakeMulti:
    def __init__(self, name, *, raise_exc=None):
        self.name, self.raise_exc, self.calls = name, raise_exc, []

    def publish_multi_platform(self, *, platforms, deliverable_type, episode_id, **kwargs):
        self.calls.append(list(platforms))
        if self.raise_exc:
            raise self.raise_exc
        return {p: {"platform": p, "status": "published", "provider_content_id": f"{self.name}_{p}"}
                for p in platforms}


def _package(tmp_path):
    f = tmp_path / "short.mp4"
    f.write_bytes(b"x" * 2000)
    return PublishPackage(
        channel_id=CHANNEL, episode_id="ep_route",
        main_episode=DeliverableFile(deliverable_id="main_episode", local_path=f, mime_type="video/mp4",
                                     media_type="video", filename=f.name),
    )


def _run(db_path, tmp_path, routed, monkeypatch):
    monkeypatch.setattr(runtime, "_route_platforms_by_binding", lambda *a, **k: routed)
    from hypogeum.credential_manager import create_secret_profile
    create_secret_profile(provider="zernio", label="k", credential_type="api_key",
                          credential_data="sk_test", scope="factory", db_path=db_path)
    media = FakeMediaTransport()
    return publish_episode_package(
        _package(tmp_path), platforms=["youtube", "facebook"], channel_id=CHANNEL, db_path=db_path,
        schedule_plan=SchedulePlan(channel_id=CHANNEL, entries={"main_episode": DeliverableSchedule(publish_now=True)}),
        media_staging_provider=ZernioMediaStagingProvider(presign_transport=media, upload_transport=media),
    )


def test_mixed_routing_merges_results_from_both_providers(db_path, tmp_path, monkeypatch):
    z, p = _FakeMulti("zernio"), _FakeMulti("postiz")
    result = _run(db_path, tmp_path, [(z, ["youtube"]), (p, ["facebook"])], monkeypatch)
    (outcome,) = result.deliverables
    assert outcome.status == "published"
    assert outcome.platform_results["youtube"]["provider_content_id"] == "zernio_youtube"
    assert outcome.platform_results["facebook"]["provider_content_id"] == "postiz_facebook"
    assert z.calls == [["youtube"]] and p.calls == [["facebook"]]


def test_one_provider_failing_only_fails_its_own_platforms(db_path, tmp_path, monkeypatch):
    z, p = _FakeMulti("zernio"), _FakeMulti("postiz", raise_exc=RuntimeError("postiz down"))
    result = _run(db_path, tmp_path, [(z, ["youtube"]), (p, ["facebook"])], monkeypatch)
    (outcome,) = result.deliverables
    assert outcome.status == "partial"
    assert outcome.platform_results["youtube"]["status"] == "published"
    assert outcome.platform_results["facebook"]["status"] == "failed"
    assert "postiz down" in outcome.platform_results["facebook"]["error"]


def test_single_provider_exception_still_blocks_deliverable(db_path, tmp_path, monkeypatch):
    z = _FakeMulti("zernio", raise_exc=RuntimeError("boom"))
    result = _run(db_path, tmp_path, [(z, ["youtube", "facebook"])], monkeypatch)
    (outcome,) = result.deliverables
    assert outcome.status == "blocked" and "boom" in outcome.error


def test_postiz_provider_is_constructible_without_config_access():
    # Routing builds a PostizProvider per call; construction must not read
    # configuration or touch the network.
    p = PostizProvider()
    assert p.provider_id == "postiz" and p._explicit_base_url is None
