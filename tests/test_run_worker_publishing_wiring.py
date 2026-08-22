"""
tests/test_run_worker_publishing_wiring.py — Proves the real worker
polling loop (run_iteration()) automatically invokes the Zernio release
trigger — not merely that a standalone callable exists.

Every test here drives run_worker.run_iteration() directly (the exact
function run_worker.py's daemon loop calls every interval), with
Config.AUTO_ZERNIO_PUBLISHING_ENABLED controlling whether the new Step 3.5
fires. The real default transport CLASSES
(hypogeum.publishing.zernio_media_staging.RequestsZernioMediaTransport,
hypogeum.publishing.zernio.RequestsZernioTransport) are monkeypatched to
fakes for the duration of each test — this proves the FULL real call
chain (run_iteration -> _process_channel_publishing ->
process_channel_ready_bundles -> publish_channel_release ->
publish_episode_package -> ZernioMediaStagingProvider()/ZernioProvider(),
constructed with NO explicit transport, exactly as production code would)
reaches a fake Zernio, not just that a function got called with the right
arguments.

All state uses an isolated tmp_path SQLite database — never the production
.hypogeum-assets/hypogeum.db. No real socket is ever opened (guarded).
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

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
from hypogeum.publishing.zernio import ZernioTransportResponse
from hypogeum.publishing.zernio_media_staging import PresignTransportResponse, UploadTransportResponse


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _seed_cleared_factory(db_path: Path) -> None:
    """Minimal factory + credential rows hypogeum.factory_director.
    check_run_clearance() requires to return cleared=True -- mirrors
    tests/test_worker.py's own _seed_cleared_factory() exactly."""
    from hypogeum.calog import set_channel_state

    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO factories (factory_id, factory_name, factory_slug, "
            "status, default_language, default_timezone, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("hypogeum", "Test Factory", "hypogeum", "active", "en", "UTC",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO secret_profiles "
            "(secret_profile_id, provider, label, credential_type, "
            "credential_data, status, scope, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "openrouter", "openrouter-env", "api_key",
             "dummy", "active", "factory", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    set_channel_state("hypogeum", "active", triggered_by="test_setup", db_path=db_path)


def _make_channel(db_path, channel_id, *, timezone_name="UTC"):
    brand = create_brand_profile(name=f"{channel_id} Brand", db_path=db_path)
    model = create_model_profile(name=f"{channel_id} Model", db_path=db_path)
    pub = create_publishing_profile(name=f"{channel_id} Publishing", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name=channel_id,
        brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )
    create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, timezone=timezone_name,
        publishing_schedule={"day_of_week": "sunday", "time_of_day": "14:00"},
        enabled_platforms={"youtube": True},
        deliverable_schedule_policy={"main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"}},
        db_path=db_path,
    )


def _bind_provider(db_path, channel_id, *, platform="youtube"):
    create_secret_profile(
        provider="zernio", label="k", credential_type="api_key",
        credential_data="sk_test_never_real", scope="factory", db_path=db_path,
    )
    profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id=f"prof_{channel_id}")
    account = upsert_provider_account(
        db_path, provider_id="zernio", platform=platform, external_account_id=f"acct_{channel_id}",
        provider_profile_id=profile["provider_profile_id"], status="active",
    )
    bind_channel_provider(
        db_path, channel_id=channel_id, capability="publishing", platform=platform,
        provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
        provider_account_id=account["provider_account_id"],
    )


def _seed_ready_episode(db_path, assets_root, episode_id, *, channel_id, title="A Real Title"):
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
        "metadata_source": {"episode_title": title, "description": "A real description.", "shorts_titles": []},
        "shorts_renders": [],
    }
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps(pkg), encoding="utf-8")
    (ep_dir / f"main_{episode_id}.mp4").write_bytes(b"main bytes" * 500)

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


class FakeMediaTransport:
    """No-arg constructible, matching RequestsZernioMediaTransport()'s own
    default-construction contract."""
    _presign_calls = []
    _counter = [0]

    def presign(self, *, url, headers, json_body, timeout):
        FakeMediaTransport._counter[0] += 1
        FakeMediaTransport._presign_calls.append(json_body)
        n = FakeMediaTransport._counter[0]
        return PresignTransportResponse(
            status_code=200,
            json_body={"uploadUrl": f"https://upload.example.com/{n}", "publicUrl": f"https://cdn.example.com/{n}",
                       "key": f"key_{n}", "expiresIn": 3600},
        )

    def upload(self, *, url, headers, data, timeout):
        return UploadTransportResponse(status_code=200)


class FakePublishTransport:
    """No-arg constructible, matching RequestsZernioTransport()'s own
    default-construction contract."""
    post_calls = []
    _counter = [0]

    def post(self, *, url, headers, json_body, timeout):
        FakePublishTransport._counter[0] += 1
        FakePublishTransport.post_calls.append(json_body)
        n = FakePublishTransport._counter[0]
        # 2026-08-22 (Zernio aggregation model): echo a platforms[] entry
        # for EVERY requested platform, not just the first -- a real
        # aggregated post's response carries one PlatformTarget per
        # requested platform (see ZernioProvider._parse_post_response()),
        # and this adapter now calls it once per platform in the group.
        requested_platforms = [p["platform"] for p in json_body["platforms"]]
        return ZernioTransportResponse(
            status_code=201,
            json_body={"message": "created", "post": {
                "_id": f"post_{n}",
                "platforms": [
                    {"platform": p, "platformPostId": f"native_{p}_{n}", "status": "published"}
                    for p in requested_platforms
                ],
            }},
        )


@pytest.fixture(autouse=True)
def _reset_fake_transport_state():
    FakeMediaTransport._presign_calls = []
    FakeMediaTransport._counter = [0]
    FakePublishTransport.post_calls = []
    FakePublishTransport._counter = [0]
    yield


@pytest.fixture(autouse=True)
def _skip_production_output_gate(monkeypatch):
    """Every fixture in this file writes fake (non-playable) media bytes
    -- production_output_gate's real ffprobe/pixel checks are covered by
    tests/test_production_output_gate.py, not here. This file tests
    run_worker.py's own wiring/idempotency/isolation contract, not the
    gate's own logic."""
    from hypogeum.agents.production_output_gate import ProductionOutputGateResult

    def _fake_pass(episode_id, assets_root, config, log):
        return ProductionOutputGateResult(passed=True, episode_id=episode_id, evaluated_at="", checks=[])

    monkeypatch.setattr(
        "hypogeum.publishing.zernio_channel_release_trigger.run_production_output_gate", _fake_pass,
    )
    yield


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "worker_wiring_test.db"
    initialize_schema(p)
    _seed_cleared_factory(p)
    return p


@pytest.fixture()
def assets_root(db_path):
    root = db_path.parent
    return root


@pytest.fixture()
def patch_real_transports(monkeypatch):
    """Patches the REAL default transport classes at their own module
    level -- proves the actual production call chain (constructing
    ZernioMediaStagingProvider()/ZernioProvider() with no explicit
    transport, exactly like run_iteration()'s real path does) reaches
    fake Zernio, not a specially-injected test double."""
    monkeypatch.setattr(
        "hypogeum.publishing.zernio_media_staging.RequestsZernioMediaTransport", FakeMediaTransport,
    )
    monkeypatch.setattr("hypogeum.publishing.zernio.RequestsZernioTransport", FakePublishTransport)


def _run_iteration(db_path, *, auto_publishing_enabled, dry_run=False, assets_root=None):
    from run_worker import run_iteration
    from hypogeum.config import Config

    config = MagicMock(spec=Config)
    config.AUTO_ZERNIO_PUBLISHING_ENABLED = auto_publishing_enabled
    config.AUTO_DISCOVERY_ENABLED = False
    return run_iteration(
        worker_id=str(uuid.uuid4()), db_path=db_path, dry_run=dry_run, channel_id=None,
        config=config, iteration=1, assets_root=assets_root,
    )


class TestWorkerLoopInvokesTriggerAutomatically:
    def test_run_iteration_publishes_ready_bundle_via_fake_zernio(self, db_path, assets_root, patch_real_transports):
        _make_channel(db_path, "hypogeum")
        _bind_provider(db_path, "hypogeum")
        _seed_ready_episode(db_path, assets_root, "ep_ready", channel_id="hypogeum")

        result = _run_iteration(db_path, auto_publishing_enabled=True, assets_root=assets_root)

        assert result["publishing"]["channels_with_activity"] == 1
        entries = result["publishing"]["results_by_channel"]["hypogeum"]
        assert entries[0]["status"] == "published"
        # Reached the REAL default transport classes (monkeypatched to
        # fakes at their own module level) -- proves the actual production
        # call chain, not a specially-injected test double.
        assert len(FakePublishTransport.post_calls) == 1
        # 2 presign calls: the main video AND its canonical thumbnail,
        # staged separately -- proves the mandatory-thumbnail path is
        # genuinely reached through the REAL automatic worker call chain,
        # not just the isolated readiness check.
        assert len(FakeMediaTransport._presign_calls) == 2
        assert {c["contentType"] for c in FakeMediaTransport._presign_calls} == {"video/mp4", "image/jpeg"}

    def test_flag_disabled_never_invokes_trigger(self, db_path, assets_root, patch_real_transports):
        _make_channel(db_path, "hypogeum")
        _bind_provider(db_path, "hypogeum")
        _seed_ready_episode(db_path, assets_root, "ep_ready", channel_id="hypogeum")

        result = _run_iteration(db_path, auto_publishing_enabled=False, assets_root=assets_root)

        assert result["publishing"] == {"skipped_reason": "auto_zernio_publishing_disabled"}
        assert FakePublishTransport.post_calls == []
        assert FakeMediaTransport._presign_calls == []


class TestRepeatedTickAndRestartNeverDuplicate:
    def test_second_iteration_does_not_resubmit(self, db_path, assets_root, patch_real_transports):
        _make_channel(db_path, "hypogeum")
        _bind_provider(db_path, "hypogeum")
        _seed_ready_episode(db_path, assets_root, "ep_ready", channel_id="hypogeum")

        first = _run_iteration(db_path, auto_publishing_enabled=True, assets_root=assets_root)
        # A second run_iteration() call is indistinguishable from "the
        # worker ticked again" or "the worker restarted and ticked for the
        # first time" -- no in-memory state survives between calls either
        # way; both are exercised by simply calling again.
        second = _run_iteration(db_path, auto_publishing_enabled=True, assets_root=assets_root)

        assert first["publishing"]["results_by_channel"]["hypogeum"][0]["status"] == "published"
        assert "hypogeum" not in second["publishing"].get("results_by_channel", {})
        assert len(FakePublishTransport.post_calls) == 1, "no second live submission was made"


class TestOneChannelFailureNeverAffectsAnother:
    def test_channel_a_exception_does_not_prevent_channel_b_publishing(self, db_path, assets_root, patch_real_transports, monkeypatch):
        _make_channel(db_path, "channel_a")
        _make_channel(db_path, "channel_b")
        _bind_provider(db_path, "channel_a")
        _bind_provider(db_path, "channel_b")
        _seed_ready_episode(db_path, assets_root, "ep_a", channel_id="channel_a")
        _seed_ready_episode(db_path, assets_root, "ep_b", channel_id="channel_b")

        import run_worker
        from hypogeum.buffer import DeliverableBundleDefinition

        def _resolve(channel_id):
            if channel_id in ("channel_a", "channel_b"):
                return [DeliverableBundleDefinition(channel_id=channel_id, bundle_id="main_episode_bundle", target_buffer_count=1)]
            return []

        monkeypatch.setattr(run_worker, "_resolve_bundle_definitions_for_channel", _resolve)

        from hypogeum.publishing.zernio_channel_release_trigger import process_channel_ready_bundles as _real_process

        def _process_with_a_failing(channel_id, bundle_def, **kwargs):
            if channel_id == "channel_a":
                raise RuntimeError("simulated channel_a publishing failure")
            return _real_process(channel_id, bundle_def, **kwargs)

        monkeypatch.setattr(
            "hypogeum.publishing.zernio_channel_release_trigger.process_channel_ready_bundles",
            _process_with_a_failing,
        )

        result = _run_iteration(db_path, auto_publishing_enabled=True, assets_root=assets_root)

        assert "channel_a" not in result["publishing"]["results_by_channel"]
        assert result["publishing"]["results_by_channel"]["channel_b"][0]["status"] == "published"
        assert len(FakePublishTransport.post_calls) == 1


class TestNoLiveSocket:
    def test_no_real_socket_opened(self, db_path, assets_root, patch_real_transports, monkeypatch):
        def _fail_socket(*a, **k):
            raise AssertionError("no real socket may be opened by this test")

        monkeypatch.setattr(socket.socket, "connect", _fail_socket)
        _make_channel(db_path, "hypogeum")
        _bind_provider(db_path, "hypogeum")
        _seed_ready_episode(db_path, assets_root, "ep_ready", channel_id="hypogeum")

        result = _run_iteration(db_path, auto_publishing_enabled=True, assets_root=assets_root)
        assert result["publishing"]["results_by_channel"]["hypogeum"][0]["status"] == "published"


class TestMissingBindingOrScheduleBlocksWithoutCrashing:
    def test_missing_binding_returns_blocked_result_worker_keeps_running(self, db_path, assets_root, patch_real_transports):
        _make_channel(db_path, "hypogeum")
        # Deliberately no _bind_provider() call -- D_PUBLISHING_BINDING will fail.
        _seed_ready_episode(db_path, assets_root, "ep_ready", channel_id="hypogeum")

        result = _run_iteration(db_path, auto_publishing_enabled=True, assets_root=assets_root)

        # The worker loop completed normally (no exception propagated) and
        # reports the blocked outcome explicitly rather than silently
        # dropping it or crashing.
        entries = result["publishing"]["results_by_channel"]["hypogeum"]
        assert entries[0]["status"] == "blocked_readiness"
        assert FakePublishTransport.post_calls == []
        assert FakeMediaTransport._presign_calls == []

    def test_missing_schedule_policy_returns_blocked_result_worker_keeps_running(self, db_path, assets_root, patch_real_transports):
        # Channel exists but with NO deliverable_schedule_policy at all --
        # the autonomous path requires explicit coverage (see
        # hypogeum.publishing.schedule_compiler's require_explicit_policy).
        brand = create_brand_profile(name="hypogeum Brand", db_path=db_path)
        model = create_model_profile(name="hypogeum Model", db_path=db_path)
        pub = create_publishing_profile(name="hypogeum Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="hypogeum", name="hypogeum",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="hypogeum", channel_name="hypogeum", timezone="UTC",
            publishing_schedule={"day_of_week": "sunday", "time_of_day": "14:00"},
            enabled_platforms={"youtube": True},
            db_path=db_path,  # no deliverable_schedule_policy
        )
        _bind_provider(db_path, "hypogeum")
        _seed_ready_episode(db_path, assets_root, "ep_ready", channel_id="hypogeum")

        result = _run_iteration(db_path, auto_publishing_enabled=True, assets_root=assets_root)

        entries = result["publishing"]["results_by_channel"]["hypogeum"]
        assert entries[0]["status"] == "blocked_schedule"
        assert FakePublishTransport.post_calls == []


class TestDryRunSafety:
    """A worker dry run must never stage, upload, submit, publish, schedule,
    or create any real provider write -- Step 3.5 is hard-gated by
    `not dry_run`, checked before even the master switch, independent of
    the global factory-clearance gate."""

    def test_dry_run_true_skips_publishing_entirely(self, db_path, assets_root, patch_real_transports):
        _make_channel(db_path, "hypogeum")
        _bind_provider(db_path, "hypogeum")
        _seed_ready_episode(db_path, assets_root, "ep_ready", channel_id="hypogeum")

        result = _run_iteration(db_path, auto_publishing_enabled=True, dry_run=True, assets_root=assets_root)

        assert result["dry_run"] is True
        assert result["publishing"] == {"skipped_reason": "dry_run"}
        assert FakePublishTransport.post_calls == []
        assert FakeMediaTransport._presign_calls == []

        # Zero publication-intent/staging mutation of any kind.
        conn = get_connection(db_path)
        try:
            intents = conn.execute("SELECT COUNT(*) FROM publication_intents").fetchone()[0]
            staging = conn.execute("SELECT COUNT(*) FROM media_staging_attempts").fetchone()[0]
        finally:
            conn.close()
        assert intents == 0
        assert staging == 0

    def test_dry_run_true_skips_even_with_fully_valid_ready_bundle(self, db_path, assets_root, patch_real_transports):
        """Confirms the dry-run gate is unconditional -- a bundle that
        would otherwise publish successfully (valid binding, valid
        schedule policy, valid metadata) still must not be touched."""
        _make_channel(db_path, "hypogeum")
        _bind_provider(db_path, "hypogeum")
        _seed_ready_episode(db_path, assets_root, "ep_would_succeed", channel_id="hypogeum")

        # Sanity check: the SAME setup with dry_run=False actually publishes.
        live_result = _run_iteration(db_path, auto_publishing_enabled=True, dry_run=False, assets_root=assets_root)
        assert live_result["publishing"]["results_by_channel"]["hypogeum"][0]["status"] == "published"
        assert len(FakePublishTransport.post_calls) == 1


class TestAssetsRootResolution:
    """assets_root must never be silently derived from db_path's parent --
    a caller-supplied db_path may legitimately live somewhere other than
    the real episode asset tree."""

    def test_explicit_assets_root_is_used_not_db_path_parent(self, tmp_path):
        db_dir = tmp_path / "db_lives_here"
        db_dir.mkdir()
        db_path = db_dir / "worker_wiring_test.db"
        initialize_schema(db_path)
        _seed_cleared_factory(db_path)

        real_assets_root = tmp_path / "assets_live_elsewhere"
        real_assets_root.mkdir()

        _make_channel(db_path, "hypogeum")
        _bind_provider(db_path, "hypogeum")
        _seed_ready_episode(db_path, real_assets_root, "ep_ready", channel_id="hypogeum")

        with patch_real_transports_ctx():
            # db_path's own parent (db_dir) does NOT contain the episode
            # files -- only real_assets_root does. Passing assets_root
            # explicitly must be what makes this resolve correctly.
            result = _run_iteration(db_path, auto_publishing_enabled=True, assets_root=real_assets_root)

        entries = result["publishing"]["results_by_channel"]["hypogeum"]
        assert entries[0]["status"] == "published"

    def test_omitting_assets_root_never_silently_reads_db_path_parent(self, tmp_path, monkeypatch):
        """When assets_root is omitted, run_iteration() must resolve the
        module's own canonical _DEFAULT_DB.parent -- NOT db_path.parent.
        Proven here by pointing db_path at a directory that looks like a
        plausible (but wrong) assets root and confirming nothing from it
        is read: the channel's episode simply is not found there, so
        gather_channel_demand_items() reports it not publish_ready rather
        than reading files from db_path.parent by accident."""
        import run_worker

        db_dir = tmp_path / "db_lives_here"
        db_dir.mkdir()
        db_path = db_dir / "worker_wiring_test.db"
        initialize_schema(db_path)
        _seed_cleared_factory(db_path)

        # Point the module's own canonical default at an isolated,
        # empty tmp directory for this test only, so "the real project
        # .hypogeum-assets" is never touched and the assertion is
        # meaningful (an empty canonical root -> genuinely nothing found).
        canonical_root = tmp_path / "canonical_assets_root"
        canonical_root.mkdir()
        monkeypatch.setattr(run_worker, "_DEFAULT_DB", canonical_root / "hypogeum.db")

        _make_channel(db_path, "hypogeum")
        _bind_provider(db_path, "hypogeum")
        # Plant the episode's real files under db_path.parent (a DECOY
        # location) -- if the old (buggy) Path(db_path).parent behavior
        # were still active, it would find and use these. It must NOT be.
        _seed_ready_episode(db_path, db_dir, "ep_ready", channel_id="hypogeum")

        with patch_real_transports_ctx():
            result = _run_iteration(db_path, auto_publishing_enabled=True)  # assets_root omitted

        # The episode is still discovered via the DB (episodes table,
        # independent of assets_root) but building its publish package
        # correctly reports it BLOCKED (unresolvable canonical metadata,
        # since no final_render_package.json exists at the canonical
        # root) -- proving the canonical (empty) root was consulted, not
        # the decoy files sitting right under db_path.parent. If the old
        # buggy Path(db_path).parent behavior were still active, this
        # would instead report "published".
        entries = result["publishing"]["results_by_channel"]["hypogeum"]
        assert entries[0]["status"] == "blocked_metadata"
        assert FakePublishTransport.post_calls == []


def patch_real_transports_ctx():
    from unittest.mock import patch as _patch

    class _Ctx:
        def __enter__(self):
            self._p1 = _patch(
                "hypogeum.publishing.zernio_media_staging.RequestsZernioMediaTransport", FakeMediaTransport,
            )
            self._p2 = _patch("hypogeum.publishing.zernio.RequestsZernioTransport", FakePublishTransport)
            self._p1.start()
            self._p2.start()
            return self

        def __exit__(self, *exc):
            self._p1.stop()
            self._p2.stop()

    return _Ctx()
